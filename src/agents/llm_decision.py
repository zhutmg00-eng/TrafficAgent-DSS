"""
TrafficAgent-DSS: LLM Control-Decision Layer
============================================

Turns the LLM from a *narrator* into a *decision maker* — safely.

Why this module exists
----------------------
Revision v2.2.x had the model producing narrative text only: every control
value was computed by the deterministic toolchain and the model was merely
asked to describe it. That is honest, but it leaves the LLM outside the
decision path — the system decides, the model comments.

This layer closes that gap. The model now proposes **control variables**, and
those variables are actually deployed to the simulator.

Design contract
---------------
1. The model emits **control variables only** (cycle, arterial green share,
   diversion ratio, progression speed, coordination flag). It never emits
   performance figures — delay, queue length and throughput are *measured by
   the simulator*, never predicted by the model. If the model quotes a number
   that does not trace back to an input we handed it, the whole proposal is
   rejected (same red line as the narrative provenance guard).
2. Every variable passes schema validation and is then **clipped into the
   physically feasible domain** of the J1-J3 corridor. Each clip is logged as
   `requested` / `applied` / `reason`, so a reviewer sees exactly what the
   model asked for versus what was deployed.
3. If the model is unconfigured, unreachable, or returns an unusable payload,
   this layer reports the failure honestly and the caller falls back to the
   deterministic rule chain — never to a silently fabricated parameter set.

The module depends only on the standard library plus the project's own LLM
client, so it is unit-testable without an API key or a live model.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any, Dict, List, Optional, Tuple

from src.agents.llm_client import LLMReasoningClient

# --------------------------------------------------------------------------- #
# Feasible domain — J1-J3 corridor signal & diversion constraints
# --------------------------------------------------------------------------- #
# The corridor is a two-phase actuated arterial (arterial EW green + cross NS
# green, 4 s yellows — see scenarios/corridor.net.xml). Variables are bounded
# by traffic-engineering practice for an urban arterial, NOT by what the model
# asks for. A model proposal outside these bounds is clipped, not obeyed.
POLICY_BOUNDS: Dict[str, Tuple[float, float]] = {
    "target_cycle_s": (60.0, 120.0),
    "arterial_green_share": (0.50, 0.82),
    "reroute_ratio": (0.00, 0.40),
    "progression_speed_kmh": (30.0, 60.0),
}

YELLOW_TIME_S = 4.0
NUM_PHASES_TWO_YELLOWS = 2.0  # two release phases => two yellow intervals
MIN_CROSS_GREEN_S = 10.0      # side-street / pedestrian clearance
MIN_ARTERIAL_GREEN_S = 20.0   # arterial minimum green

# Absolute hard rails. The clip above already respects these, but a malformed
# payload (e.g. a cycle of 1e9) must never reach the simulator even if the
# caller overrides POLICY_BOUNDS.
HARD_CYCLE_MIN_S = 45.0
HARD_CYCLE_MAX_S = 180.0

# Numbers the model may legitimately quote: junction labels (J1-J3), the two
# corridor phases, lane counts. Mirrors the narrative guard's tolerance.
_SMALL_BARE_NUMBER_MAX = 9

DECISION_SYSTEM_PROMPT = """你是城市干道信号控制与交通诱导**决策智能体**（不是文案写手）。

【你的职责】
在给定的可行域内，为北京西直门 J1-J3 干道走廊选择一组控制参数，
目标：**最小化全网平均延误**，同时不显著扩大最大排队长度（不得引发下游回溢）。

【你可决策的 5 个变量】（必须全部给出）
1. `target_cycle_s`    信号周期（秒），可行域 [60, 120]
2. `arterial_green_share`  主干道绿信比（0~1），可行域 [0.50, 0.82]
3. `reroute_ratio`     上游 VMS 诱导分流比例（0~1），可行域 [0, 0.40]
4. `progression_speed_kmh`  干线绿波设计车速（km/h），可行域 [30, 60]
5. `coordinated`       是否启用 J1-J3 双向绿波协调（true / false）

【硬约束（必须自行满足，否则你的方案会被裁剪）】
- 支路绿灯 = 周期 − 2×黄灯(4s) − 主干绿灯，**必须 ≥ 10 秒**
- 主干绿灯本身 **不得低于 20 秒**
- `reroute_ratio` 不得超过输入中给出的「旁路容量上限」
- 潮汐特征：早高峰主干道是主方向，支路流量显著低于主干道

【铁律】
- **禁止输出任何延误 / 排队 / 通行量 / 碳排的预测数值**。
  这些指标由 SUMO 仿真器实测，不由你估计。你给的是「参数」，不是「效果」。
- `decision_rationale` 里若引用数字，只能使用输入中**已经出现过的**数字。
- 只输出一个 JSON 对象，不要 markdown 代码块，不要任何解释性文字。

【输出格式】
{
  "target_cycle_s": <number>,
  "arterial_green_share": <number>,
  "reroute_ratio": <number>,
  "progression_speed_kmh": <number>,
  "coordinated": <true|false>,
  "decision_rationale": "<引用输入数字说明为何这样取值的专业理由，1-3 句>"
}
"""

# Round-2 feedback block appended to the user prompt when the model sees the
# result of its own previous decision.
_FEEDBACK_PROMPT_HEADER = """【上一轮你的决策与实测反馈】
你上一轮给出的参数是：{applied}
SUMO 实测结果（相对无干预基线）：
{feedback}
请基于**实测反馈**修正你的参数：若延误已改善但排队显著上升，应降低分流或抬高周期以消化排队；
若改善不明显，可尝试调整绿信比或提高协调。仍然只输出那一个 JSON 对象。
"""

_RATIONALE_NUMBER_RE = re.compile(r"(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(%|％)?")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _finite(value: Any) -> Optional[float]:
    """Returns the value as a finite float, or None when it is not numeric."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _collect_numbers(node: Any, sink: List[float]) -> None:
    """Recursively gathers every finite number appearing in a nested structure."""
    if isinstance(node, bool):
        return
    if isinstance(node, (int, float)):
        f = _finite(node)
        if f is not None:
            sink.append(f)
        return
    if isinstance(node, dict):
        for v in node.values():
            _collect_numbers(v, sink)
    elif isinstance(node, (list, tuple)):
        for v in node:
            _collect_numbers(v, sink)


def _rationale_quotes_only_known_numbers(text: str, allowed: List[float]) -> bool:
    """
    True when every quantified claim in the rationale is traceable to an input.

    Percentages always need a match (a hallucinated "延误下降 30%" is exactly
    the failure mode this guard exists for). Bare integers above 9 also need a
    match; J-labels and the two-phase count stay below that threshold.
    """
    if not text:
        return True

    def traced(value: float) -> bool:
        for a in allowed:
            if abs(value - a) <= max(0.55, abs(a) * 0.015):
                return True
            scaled = a * 100.0  # ratios may be quoted as percentages
            if abs(value - scaled) <= max(0.55, abs(scaled) * 0.015):
                return True
        return False

    for m in _RATIONALE_NUMBER_RE.finditer(text):
        value = float(m.group(1).replace(",", ""))
        if m.group(2):
            if not traced(value):
                return False
        elif value > _SMALL_BARE_NUMBER_MAX and not traced(value):
            return False
    return True


# --------------------------------------------------------------------------- #
# Schema validation
# --------------------------------------------------------------------------- #
REQUIRED_NUMERIC_FIELDS = (
    "target_cycle_s",
    "arterial_green_share",
    "reroute_ratio",
    "progression_speed_kmh",
)


def validate_payload(payload: Any) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """
    Validates the raw model payload against the decision schema.

    Returns (proposal, errors). `proposal` is None whenever `errors` is non-empty.
    """
    errors: List[str] = []
    if not isinstance(payload, dict):
        return None, [f"payload is {type(payload).__name__}, expected a JSON object"]

    proposal: Dict[str, Any] = {}
    for field in REQUIRED_NUMERIC_FIELDS:
        if field not in payload:
            errors.append(f"missing required field '{field}'")
            continue
        value = _finite(payload.get(field))
        if value is None:
            errors.append(f"field '{field}' is not a finite number (got {payload.get(field)!r})")
            continue
        proposal[field] = value

    if "coordinated" not in payload:
        errors.append("missing required field 'coordinated'")
    else:
        coordinated = payload.get("coordinated")
        if not isinstance(coordinated, bool):
            errors.append(f"field 'coordinated' must be a JSON boolean (got {coordinated!r})")
        else:
            proposal["coordinated"] = coordinated

    rationale = payload.get("decision_rationale")
    if rationale is None or not str(rationale).strip():
        errors.append("missing required field 'decision_rationale'")
    else:
        proposal["decision_rationale"] = str(rationale).strip()

    if errors:
        return None, errors
    return proposal, []


# --------------------------------------------------------------------------- #
# Physical feasibility clipping
# --------------------------------------------------------------------------- #
def clip_policy(requested: Dict[str, Any], context: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """
    Clips a validated proposal into the corridor's feasible domain.

    Returns (applied_policy, adjustments) where each adjustment records
    `variable` / `requested` / `applied` / `reason`. `applied_policy` carries the
    derived green splits so downstream code never has to re-derive them.
    """
    adjustments: List[Dict[str, Any]] = []
    applied: Dict[str, Any] = {}

    def clip_scalar(name: str, low: float, high: float, reason_low: str, reason_high: str) -> float:
        raw = float(requested[name])
        if raw < low:
            adjustments.append({
                "variable": name, "requested": raw, "applied": low,
                "reason": f"below feasible minimum ({reason_low})",
            })
            return low
        if raw > high:
            adjustments.append({
                "variable": name, "requested": raw, "applied": high,
                "reason": f"above feasible maximum ({reason_high})",
            })
            return high
        return raw

    # ---- 1. Cycle ---------------------------------------------------------- #
    low, high = POLICY_BOUNDS["target_cycle_s"]
    cycle = clip_scalar(
        "target_cycle_s", max(low, HARD_CYCLE_MIN_S), min(high, HARD_CYCLE_MAX_S),
        f"practical urban arterial cycle {low:.0f}s", f"practical urban arterial cycle {high:.0f}s",
    )
    applied["target_cycle_s"] = round(cycle, 1)

    # ---- 2. Green split ---------------------------------------------------- #
    share_low, share_high = POLICY_BOUNDS["arterial_green_share"]
    share = clip_scalar(
        "arterial_green_share", share_low, share_high,
        "arterial must not lose priority to the side street",
        "side street must retain a serviceable green",
    )

    available_green = cycle - NUM_PHASES_TWO_YELLOWS * YELLOW_TIME_S
    # The share is only meaningful if a minimum cross green survives it.
    max_share_by_cross = (available_green - MIN_CROSS_GREEN_S) / max(1e-6, available_green)
    if share > max_share_by_cross:
        capped = max(share_low, min(share_high, max_share_by_cross))
        adjustments.append({
            "variable": "arterial_green_share", "requested": round(share, 4),
            "applied": round(capped, 4),
            "reason": (
                f"cross green would fall below the {MIN_CROSS_GREEN_S:.0f}s clearance minimum "
                f"at a {cycle:.0f}s cycle"
            ),
        })
        share = capped

    arterial_green = round(available_green * share, 1)
    cross_green = round(available_green - arterial_green, 1)

    if cross_green < MIN_CROSS_GREEN_S:
        cross_green = MIN_CROSS_GREEN_S
        arterial_green = round(available_green - cross_green, 1)
        adjustments.append({
            "variable": "cross_green_s", "requested": round(available_green - arterial_green, 1),
            "applied": cross_green,
            "reason": f"restored side-street clearance minimum ({MIN_CROSS_GREEN_S:.0f}s)",
        })
    if arterial_green < MIN_ARTERIAL_GREEN_S:
        arterial_green = MIN_ARTERIAL_GREEN_S
        cross_green = round(available_green - arterial_green, 1)
        adjustments.append({
            "variable": "arterial_green_s", "requested": arterial_green,
            "applied": arterial_green,
            "reason": f"restored arterial minimum green ({MIN_ARTERIAL_GREEN_S:.0f}s)",
        })

    applied["arterial_green_share"] = round(share, 4)
    applied["arterial_green_s"] = arterial_green
    applied["cross_green_s"] = cross_green
    applied["available_green_s"] = round(available_green, 1)
    applied["yellow_time_s"] = YELLOW_TIME_S

    # ---- 3. Diversion ratio (also capped by real bypass spare capacity) ---- #
    rr_low, rr_high = POLICY_BOUNDS["reroute_ratio"]
    reroute = clip_scalar(
        "reroute_ratio", rr_low, rr_high,
        "negative diversion is meaningless", "diversion above 40% overloads the bypass",
    )
    capacity_cap = context.get("baseline_plan", {}).get("reroute_capacity_cap")
    if isinstance(capacity_cap, (int, float)) and not isinstance(capacity_cap, bool):
        if reroute > float(capacity_cap) + 1e-9:
            adjustments.append({
                "variable": "reroute_ratio", "requested": round(reroute, 4),
                "applied": round(float(capacity_cap), 4),
                "reason": "bypass spare capacity cannot absorb the requested diversion",
            })
            reroute = max(0.0, float(capacity_cap))
    applied["reroute_ratio"] = round(reroute, 4)

    # ---- 4. Progression speed --------------------------------------------- #
    sp_low, sp_high = POLICY_BOUNDS["progression_speed_kmh"]
    speed = clip_scalar(
        "progression_speed_kmh", sp_low, sp_high,
        "progression slower than 30 km/h is not a green wave",
        "progression above 60 km/h exceeds the corridor's design speed",
    )
    applied["progression_speed_kmh"] = round(speed, 1)

    # ---- 5. Coordination flag --------------------------------------------- #
    applied["coordinated"] = bool(requested["coordinated"])

    applied["decision_rationale"] = requested.get("decision_rationale", "")
    return applied, adjustments


# --------------------------------------------------------------------------- #
# Decision layer
# --------------------------------------------------------------------------- #
class LLMDecisionLayer:
    """
    Wraps a project LLM client into a validated control-decision proposer.

    The layer never raises on model failure: it returns a result dict whose
    `mode` tells the caller whether a usable proposal exists.
    """

    def __init__(self, llm_client: LLMReasoningClient):
        self.llm = llm_client

    # -- prompt construction ------------------------------------------------ #
    @staticmethod
    def _user_prompt(context: Dict[str, Any]) -> str:
        payload = {
            "本轮轮次": context.get("round_index"),
            "最大轮数": context.get("max_rounds"),
            "瓶颈诊断": context.get("diagnosis", {}),
            "检测器实测状态": context.get("detector_state", {}),
            "确定性工具链基准（你的起点，可在可行域内改进）": context.get("baseline_plan", {}),
            "可行域": {
                k: {"min": v[0], "max": v[1]} for k, v in POLICY_BOUNDS.items()
            },
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2)

        feedback = context.get("feedback")
        if feedback:
            text += "\n\n" + _FEEDBACK_PROMPT_HEADER.format(
                applied=json.dumps(feedback.get("applied_policy", {}), ensure_ascii=False),
                feedback=json.dumps(feedback.get("kpi_delta", {}), ensure_ascii=False, indent=2),
            )
        return text

    # -- main entry --------------------------------------------------------- #
    def propose(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Asks the model for a control proposal and returns an auditable result.

        Result keys:
          mode        : 'llm' when a deployable policy exists, otherwise the
                        LLM client's fallback mode (unconfigured / sdk_missing / error)
          requested   : raw validated proposal, before clipping (None on failure)
          applied     : deployable policy after clipping (None on failure)
          adjustments : per-variable clip log
          errors      : validation / rejection reasons
        """
        result: Dict[str, Any] = {
            "mode": LLMReasoningClient.MODE_UNCONFIGURED,
            "engine": None,
            "requested": None,
            "applied": None,
            "adjustments": [],
            "rationale": None,
            "errors": [],
        }

        allowed_numbers: List[float] = []
        _collect_numbers(context.get("baseline_plan"), allowed_numbers)
        _collect_numbers(context.get("detector_state"), allowed_numbers)
        _collect_numbers(context.get("feedback"), allowed_numbers)

        payload, mode, error = self.llm.chat_json(
            DECISION_SYSTEM_PROMPT, self._user_prompt(context), temperature=0.2, max_tokens=900
        )
        result["mode"] = mode
        result["engine"] = self.llm.model if mode == LLMReasoningClient.MODE_LLM else "deterministic_rule_chain"

        if mode != LLMReasoningClient.MODE_LLM or not payload:
            result["errors"].append(error or "model unavailable")
            return result

        proposal, errors = validate_payload(payload)
        if proposal is None:
            result["mode"] = LLMReasoningClient.MODE_ERROR
            result["engine"] = "deterministic_rule_chain"
            result["errors"].extend(errors)
            return result

        applied, adjustments = clip_policy(proposal, context)

        # Numeric provenance guard: the model may quote inputs, never invent effects.
        trace_pool = list(allowed_numbers) + [
            proposal.get("target_cycle_s"), proposal.get("arterial_green_share"),
            proposal.get("reroute_ratio"), proposal.get("progression_speed_kmh"),
            applied.get("arterial_green_s"), applied.get("cross_green_s"),
        ]
        trace_pool = [float(v) for v in trace_pool if _finite(v) is not None]
        if not _rationale_quotes_only_known_numbers(applied.get("decision_rationale", ""), trace_pool):
            result["mode"] = LLMReasoningClient.MODE_ERROR
            result["engine"] = "deterministic_rule_chain"
            result["errors"].append(
                "model rationale quoted numbers not traceable to supplied inputs "
                "(decision provenance guard)"
            )
            return result

        result["requested"] = proposal
        result["applied"] = applied
        result["adjustments"] = adjustments
        result["rationale"] = applied.get("decision_rationale", "")
        return result
