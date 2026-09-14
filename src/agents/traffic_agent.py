"""
TrafficAgent-DSS: Traffic Decision Agent Core
Provides situational perception, root-cause diagnosis, strategy formulation,
What-If推演 coordination, and structured decision briefing generation.

推理架构（两层，职责严格分离）：
  * 大语言模型层：负责"归因、解释、方案叙事"——不产出任何性能指标数值。
  * 交通工程工具层：负责"所有数值计算"——Webster 配时、绿波相位差、分流比例、
    SUMO 微观仿真指标。
若 LLM 未配置或调用失败，系统回落到确定性规则模板，并在输出中**如实标注**
降级状态（reasoning_mode），绝不伪造看似合理的数据或结论。
"""

import os
import sys
import json
import math
import re
import inspect
from pathlib import Path
from typing import Dict, List, Any, Optional

# ---------------------------------------------------------------------------- #
# Corridor calibration constants
#
# These describe the calibrated bottleneck cross-section of the J1-J3 corridor
# (scenarios/corridor.net.xml): a 3-lane arterial approach whose design capacity is
# 3 x 1800 = 5400 pcu/h. They are NOT derived from the incoming traffic_state — a single
# cross-section detector cannot resolve lane geometry or saturation flow — so they are
# declared here explicitly rather than buried as literals inside the prompt and the
# rule-template narrative. Any scenario change must update this single place.
# ---------------------------------------------------------------------------- #
CORRIDOR_ARTERIAL_LANES = 3
CORRIDOR_SATURATION_FLOW_PER_LANE_PCU_H = 1800
CORRIDOR_DESIGN_CAPACITY_PCU_H = CORRIDOR_ARTERIAL_LANES * CORRIDOR_SATURATION_FLOW_PER_LANE_PCU_H

# Upstream demand arriving at the bottleneck and the spare capacity of the northern
# parallel bypass, both taken from the calibrated corridor demand profile. They bound
# how much traffic VMS diversion can physically move (alpha <= spare / upstream) and are
# declared once here so the toolchain, the closed-loop optimiser and the LLM decision
# layer all quote the same numbers.
CORRIDOR_UPSTREAM_FLOW_VPH = 1800.0
CORRIDOR_BYPASS_SPARE_CAPACITY_VPH = 1200.0


def _safe_float(val: Any, default: float, min_val: Optional[float] = None, max_val: Optional[float] = None) -> float:
    """Extracts a finite float defending against None, NaN, and Inf, bounded by min/max."""
    if val is None:
        return default
    try:
        v = float(val)
        if not math.isfinite(v):
            return default
    except (ValueError, TypeError):
        return default
    if min_val is not None:
        v = max(min_val, v)
    if max_val is not None:
        v = min(max_val, v)
    return v


def _parse_bool(val: Any, default: bool = False) -> bool:
    """Safely parses boolean values from JSON payloads (strings, booleans, numbers)."""
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        s = val.strip().lower()
        if s in ("false", "0", "no", "n", "off"):
            return False
        if s in ("true", "1", "yes", "y", "on"):
            return True
        return default
    if isinstance(val, (int, float)):
        return bool(val)
    return default

# Add project root to sys.path
root_dir = Path(__file__).resolve().parent.parent.parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))

from src.tools.webster import WebsterSignalOptimizer
from src.tools.green_wave import GreenWaveCoordinator
from src.tools.rerouting import DynamicReroutingAllocator
from src.tools.evaluator import PerformanceEvaluator
from src.simulation.sumo_sandbox import SumoSimulationSandbox
from src.agents.llm_client import LLMReasoningClient
from src.agents.llm_decision import (
    HARD_CYCLE_MAX_S,
    HARD_CYCLE_MIN_S,
    LLMDecisionLayer,
    POLICY_BOUNDS,
)


# --------------------------------------------------------------------------- #
# Prompt templates
# --------------------------------------------------------------------------- #
DIAGNOSIS_SYSTEM_PROMPT = """你是城市交通管理领域的资深交通工程师，专长于城市干道拥堵成因诊断与治理决策。

任务：依据给定的实测检测数据，输出结构化的拥堵归因诊断结论。

严格要求：
1. 只输出一个 JSON 对象，不要输出解释性文字，不要使用 Markdown 代码块。
2. 严禁编造输入中未给出的数值。所有数值判断必须可由输入数据推导得出。
3. 若输入信息不足以判断某结论，应如实说明不确定性，不得虚构事实。
4. cot_reasoning 为思维链，4~6 条，每条以【环节名】开头，使用专业中文表达。

输出 JSON 字段：
{
  "severity_level": "拥堵等级描述（含等级与定性）",
  "spillback_risk": "排队回溢风险描述",
  "root_causes": ["成因一", "成因二", "成因三"],
  "cot_reasoning": ["1. 【态势感知】...", "2. 【空间排队】...", "..."],
  "can_reroute": true 或 false
}"""

STRATEGY_SYSTEM_PROMPT = """你是城市交通管理领域的资深交通工程师，负责将交通工程工具算出的数值结果，
转写为面向交管调度人员的可执行治理方案说明。

严格要求：
1. 只输出一个 JSON 对象，不要输出解释性文字，不要使用 Markdown 代码块。
2. 给定的信号配时、绿波相位差、分流比例等数值是工具计算结果，**必须原样采用，严禁改动**。
3. 不得编造工具未给出数值的其它指标（如改善百分比、延误秒数等）。
4. 语言专业、简洁、可执行，面向一线交管调度人员。

输出 JSON 字段：
{
  "strategy_a_description": "方案A简述（1-2句）",
  "strategy_b_description": "方案B系统级协同简述（1-2句）",
  "strategy_b_rationale": ["协同理由1", "协同理由2", "协同理由3"],
  "vms_advisory": "面向可变信息板发布的诱导文案（30字以内）",
  "risk_warning": "实施风险与防回溢提示（1-2句）"
}"""


class TrafficDecisionAgent:
    """
    Core AI Decision Agent for urban congestion governance.
    Integrates LLM Chain-of-Thought reasoning with rigorous traffic engineering tools.
    """

    def __init__(self, scenario_dir: Optional[str] = None):
        self.scenario_dir = scenario_dir
        self.sandbox = SumoSimulationSandbox(scenario_dir=scenario_dir)
        # Lost time per phase is set to the network's yellow time (4 s), so Webster's
        # optimal cycle and the actual clock cycle of the deployed program agree exactly
        # (C = g_main + 2*yellow + g_cross). With the default 3.5 s the cycle would drift
        # by 1 s per plan, which is enough to de-align a coordinated green wave.
        self.webster = WebsterSignalOptimizer(lost_time_per_phase=4.0)
        self.green_wave = GreenWaveCoordinator()
        self.rerouter = DynamicReroutingAllocator()
        self.evaluator = PerformanceEvaluator()
        self.llm = LLMReasoningClient()
        # Decision layer: lets the model propose *control variables* (validated and
        # clipped into the feasible domain) instead of only describing them. Shares
        # the same LLM client, so hot-swapping the model also swaps the decision engine.
        self.decision = LLMDecisionLayer(self.llm)

    # ------------------------------------------------------------------ #
    # Dynamic LLM Configuration
    # ------------------------------------------------------------------ #
    def update_llm_config(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Hot-updates LLM client credentials and active model."""
        return self.llm.update_config(api_key=api_key, base_url=base_url, model=model, timeout=timeout)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    @staticmethod
    def _clean_str_list(value: Any, min_len: int = 1) -> Optional[List[str]]:
        """Validates a model-provided list of non-empty strings."""
        if not isinstance(value, list):
            return None
        cleaned = [str(v).strip() for v in value if isinstance(v, (str, int, float)) and str(v).strip()]
        return cleaned if len(cleaned) >= min_len else None

    def reasoning_metadata(self, mode: str, error: Optional[str] = None) -> Dict[str, Any]:
        """Builds an auditable description of which reasoning engine was used."""
        if mode == LLMReasoningClient.MODE_LLM:
            engine = self.llm.model
            label = f"大语言模型在线推理（{engine}）"
        elif mode == LLMReasoningClient.MODE_ERROR:
            engine = "deterministic_template"
            label = "确定性规则模板（大模型调用失败，已显式降级）"
        elif mode == LLMReasoningClient.MODE_SDK_MISSING:
            engine = "deterministic_template"
            label = "确定性规则模板（已配置 API Key，但缺少 openai SDK，请 pip install openai）"
        else:
            engine = "deterministic_template"
            label = "确定性规则模板（未配置大模型 API Key）"
        return {
            "reasoning_mode": mode,
            "reasoning_engine": engine,
            "reasoning_label": label,
            "reasoning_error": error,
        }

    # ------------------------------------------------------------------ #
    # LLM narrative numeric provenance guard
    # ------------------------------------------------------------------ #
    _NARRATIVE_NUMBER_RE = re.compile(r"(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(%|％)?")

    @classmethod
    def _narrative_numbers_traceable(
        cls,
        texts: List[str],
        plan: Dict[str, Any],
        reroute_pct: int,
    ) -> bool:
        """
        True when every number quoted in the LLM narrative layer is traceable to a value
        the traffic-engineering tools actually produced for this plan.

        The narrative layer is allowed to *quote* tool output but never to *produce*
        figures. Percentages are the dangerous case (e.g. a hallucinated "延误降低 35%"):
        each one must match a tool value (directly or as a ratio scaled to percent) within
        rounding tolerance. Bare integers 0-9 are tolerated unadorned — junction labels
        (J1-J3) and lane counts are not quantified performance claims. Any violation
        rejects the whole narrative so the deterministic template takes over, with the
        degradation reported honestly via reasoning_mode.
        """
        timing = plan.get("timing") or {}
        gw = plan.get("green_wave") or {}
        rr = plan.get("reroute") or {}
        inputs = plan.get("inputs_used") or {}
        signal = plan.get("signal_program") or {}
        arterial_green = plan.get("arterial_green") or 0.0
        actual_cycle = plan.get("actual_cycle") or 0.0

        candidates: List[Any] = [
            actual_cycle, arterial_green,
            plan.get("cross_green"), plan.get("yellow_time"),
            timing.get("optimal_cycle"), timing.get("degree_of_saturation"),
            gw.get("bandwidth_seconds"), gw.get("bandwidth_ratio_percent"),
            gw.get("progression_speed_kmh"),
            rr.get("diversion_ratio"), rr.get("diverted_flow_vph"),
            inputs.get("queue_m"), inputs.get("link_length_m"),
            inputs.get("bottleneck_occupancy"), inputs.get("bypass_occupancy"),
            reroute_pct,
            round(arterial_green / max(1.0, actual_cycle) * 100.0, 1),
        ]
        candidates.extend((signal.get(k) for k in (
            "green_main", "green_cross", "yellow",
            "min_green_main", "max_green_main", "min_green_cross", "max_green_cross",
        )))
        for seq in (
            timing.get("flow_ratios"), gw.get("offsets"), gw.get("travel_times"),
            inputs.get("phase_flows_pcu_h"), inputs.get("phase_lanes"),
        ):
            candidates.extend(seq or [])

        allowed: List[float] = []
        for v in candidates:
            if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v)):
                allowed.append(float(v))

        def traced(value: float) -> bool:
            for a in allowed:
                if abs(value - a) <= max(0.55, abs(a) * 0.015):
                    return True
                scaled = a * 100.0  # ratios may legitimately be quoted as percentages
                if abs(value - scaled) <= max(0.55, abs(scaled) * 0.015):
                    return True
            return False

        for text in texts:
            if not text:
                continue
            for m in cls._NARRATIVE_NUMBER_RE.finditer(text):
                value = float(m.group(1).replace(",", ""))
                if m.group(2):
                    if not traced(value):
                        return False
                elif value > 9 and not traced(value):
                    return False
        return True

    def _tool_plan(
        self,
        diagnosis: Optional[Dict[str, Any]] = None,
        policy: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Shared deterministic traffic-engineering computation for the J1-J3 corridor.

        Detector-derived quantities (queue length, blockage ratio, cross-section occupancy,
        bypass occupancy) are read from the diagnosis payload, so the optimised parameters
        actually respond to the observed incident instead of a frozen scenario constant.
        Only the phase-level demand split remains a corridor calibration constant, because
        a single cross-section detector cannot resolve per-approach flows.

        `policy` is an optional control-policy overlay produced by the LLM decision layer
        (`src/agents/llm_decision.py`), already schema-validated and clipped into the
        feasible domain. It may override the design cycle, the arterial green share, the
        green-wave progression speed and the diversion ratio. **When it is None this
        function behaves exactly as the purely deterministic chain always has**, so
        existing callers and previously reported numbers are unaffected.
        """
        state = (diagnosis or {}).get("input_state") or {}

        queue_m = float(state.get("queue_m", 165.0))
        link_len = float(state.get("link_length_m", 300.0))
        occupancy = float(state.get("occupancy", 0.82))
        bypass_occ = float(state.get("bypass_occupancy", 0.28))

        # Corridor-calibrated demand profile.
        #
        # The network's TLS structure has exactly TWO release phases per junction
        # (arterial EW green + cross-street NS green, with 4 s yellows — see
        # scenarios/corridor.net.xml), so Webster must be run with two phases as well.
        # Computing a 4-phase plan (as the previous revision did) produced green splits
        # that could not be mapped onto the real signal and de-synchronised the corridor.
        #
        # Critical flow per release phase (larger of the two opposing approaches at the
        # peak, from the calibrated demand profile):
        #   arterial EW: 1980 pcu/h (EB peak) over 3 lanes
        #   cross  NS :  720 pcu/h (J2 section) over 2 lanes
        phase_flows = [1980.0, 720.0]
        phase_lanes = [3, 2]

        timing = self.webster.compute_timing(phase_flows=phase_flows, phase_lanes=phase_lanes)
        yellow_time = 4.0  # matches corridor.net.xml

        # ---- Design cycle selection ------------------------------------------ #
        # Webster's minimum-delay cycle (≈45 s here) assumes under-saturated flow and
        # fixed-time control. This corridor operates under incident-induced
        # over-saturation, where the standard engineering correction is a *longer*
        # cycle: lost time per cycle is fixed, so a short cycle wastes a larger share
        # of it, and longer greens are needed to drain the queued demand. The design
        # cycle is therefore lifted to twice Webster's minimum and clamped to the
        # 60-120 s practical range for urban arterials. The controller stays
        # `actuated`, so the plan is a *baseline* the adaptive logic can stretch or
        # shorten within the min/max bounds.
        design_cycle = min(120.0, max(60.0, round(timing["optimal_cycle"] * 2.0)))

        # ---- Optional LLM policy overlay ------------------------------------- #
        # Each hook below is a *no-op* unless the policy carries that variable, so a
        # policy of {} reproduces the deterministic result bit for bit.
        policy = policy or {}

        def _policy_number(name: str) -> Optional[float]:
            value = policy.get(name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            return float(value) if math.isfinite(float(value)) else None

        policy_cycle = _policy_number("target_cycle_s")
        if policy_cycle is not None:
            design_cycle = min(HARD_CYCLE_MAX_S, max(HARD_CYCLE_MIN_S, policy_cycle))

        policy_share = _policy_number("arterial_green_share")
        policy_speed_kmh = _policy_number("progression_speed_kmh")
        policy_reroute = _policy_number("reroute_ratio")

        # Re-allocate green times within the design cycle using Webster's flow ratios.
        y_main = timing["flow_ratios"][0]
        y_cross = timing["flow_ratios"][1]
        y_sum = max(1e-6, y_main + y_cross)
        available_green = design_cycle - 2.0 * yellow_time
        # Ensure cross street maintains at least 10s minimum green for pedestrian and side-street clearance
        min_cross = 10.0
        max_arterial = available_green - min_cross
        raw_arterial = (
            available_green * policy_share if policy_share is not None
            else available_green * y_main / y_sum
        )
        arterial_green = round(max(20.0, min(max_arterial, raw_arterial)), 1)
        cross_green = round(available_green - arterial_green, 1)

        # Clock cycle actually realised by the deployed program.
        actual_cycle = round(arterial_green + cross_green + 2.0 * yellow_time, 1)

        gw_plan = self.green_wave.compute_offsets(
            intersection_distances=[300.0, 300.0],
            cycle_length=actual_cycle,
            green_splits_arterial=[arterial_green, arterial_green, arterial_green],
            progression_speed=(
                policy_speed_kmh / 3.6 if policy_speed_kmh is not None else 13.89  # 50 km/h default
            ),
        )
        reroute_plan = self.rerouter.calculate_diversion(
            bottleneck_queue_meters=queue_m,
            bottleneck_link_length=link_len,
            bottleneck_occupancy=occupancy,
            upstream_flow_vph=CORRIDOR_UPSTREAM_FLOW_VPH,
            bypass_current_occupancy=bypass_occ,
            bypass_spare_capacity_vph=CORRIDOR_BYPASS_SPARE_CAPACITY_VPH,
        )
        if policy_reroute is not None:
            # Deploy the externally decided ratio, re-deriving the derived fields and the
            # VMS copy so the published advisory matches what actually reaches SUMO.
            reroute_plan = self.rerouter.apply_diversion_override(
                reroute_plan,
                diversion_ratio=policy_reroute,
                upstream_flow_vph=CORRIDOR_UPSTREAM_FLOW_VPH,
                bottleneck_queue_meters=queue_m,
                bypass_spare_capacity_vph=CORRIDOR_BYPASS_SPARE_CAPACITY_VPH,
            )

        policy_overlay = {
            "source": "llm_decision_layer" if policy else "deterministic_rule_chain",
            "applied": {
                "target_cycle_s": policy_cycle,
                "arterial_green_share": policy_share,
                "progression_speed_kmh": policy_speed_kmh,
                "reroute_ratio": policy_reroute,
                "coordinated": policy.get("coordinated"),
            } if policy else None,
            "rationale": policy.get("decision_rationale"),
        }
        return {
            "timing": timing,
            "green_wave": gw_plan,
            "reroute": reroute_plan,
            "arterial_green": arterial_green,
            "cross_green": cross_green,
            "yellow_time": yellow_time,
            "actual_cycle": actual_cycle,
            "policy_overlay": policy_overlay,
            # Ready-to-deploy signal program: Webster-ratio baseline within the corrected
            # design cycle. `type=actuated` keeps the adaptive min/max-green behaviour the
            # incident scenario depends on; `first_green_start` is filled in per strategy
            # (zeros for the uncoordinated plan, green-wave offsets for the coordinated one).
            "signal_program": {
                "type": "actuated",
                "green_main": arterial_green,
                "green_cross": cross_green,
                "yellow": yellow_time,
                "cycle_length": actual_cycle,
                "min_green_main": max(20.0, round(arterial_green * 0.4, 1)),
                "max_green_main": round(arterial_green * 1.4, 1),
                "min_green_cross": 8.0,
                "max_green_cross": round(cross_green * 1.4, 1),
                "first_green_start": [0.0, 0.0, 0.0],
            },
            "inputs_used": {
                "source": (
                    "diagnosis.input_state (detector-derived)"
                    if state
                    else "corridor calibration defaults (no diagnosis supplied)"
                ),
                "queue_m": queue_m,
                "link_length_m": link_len,
                "bottleneck_occupancy": occupancy,
                "bypass_occupancy": bypass_occ,
                "phase_flows_pcu_h": phase_flows,
                "phase_lanes": phase_lanes,
                "upstream_flow_vph": CORRIDOR_UPSTREAM_FLOW_VPH,
                "bypass_spare_capacity_vph": CORRIDOR_BYPASS_SPARE_CAPACITY_VPH,
            },
        }

    def build_control_params(
        self,
        plan: Dict[str, Any],
        coordinated: bool,
        use_rerouting: bool = True,
        use_webster: bool = True,
    ) -> Dict[str, Any]:
        """
        Turns a tool plan into the control-parameter dict the SUMO sandbox consumes.

        Single source of truth for "what actually gets deployed". It used to be written
        out by hand in three places (strategy A, strategy B, and the closed loop) which
        made it possible for the simulated control to drift away from the planned one
        without any test noticing. Behaviour is unchanged: `coordinated=False` deploys the
        single-point program (every junction starting at phase 0), `coordinated=True`
        deploys the green-wave-aligned program, and `use_rerouting=False` deploys a zero
        diversion ratio regardless of what the plan computed.
        """
        program = dict(
            plan["signal_program"],
            first_green_start=(
                list(plan["green_wave"]["offsets"]) if coordinated else [0.0, 0.0, 0.0]
            ),
        )
        return {
            "signal_program": program,
            "reroute_ratio": plan["reroute"]["diversion_ratio"] if use_rerouting else 0.0,
            "green_wave": coordinated,
            "webster": use_webster,
        }

    # ------------------------------------------------------------------ #
    # 1. Diagnosis
    # ------------------------------------------------------------------ #
    def _diagnose_deterministic(
        self,
        bottleneck_edge: str,
        queue_m: float,
        link_len: float,
        speed_kmh: float,
        occupancy: float,
        bypass_occ: float,
        queue_ratio: float,
    ) -> Dict[str, Any]:
        """Deterministic rule-based diagnosis (fallback path, always available)."""
        occ_pct = int(round(min(1.0, max(0.0, occupancy)) * 100))
        queue_ratio_pct = int(round(min(10.0, max(0.0, queue_ratio)) * 100))
        bypass_occ_pct = int(round(min(1.0, max(0.0, bypass_occ)) * 100))

        cot_steps = [
            f"1. 【态势感知】监测到走廊主断面 [{bottleneck_edge}] 平均车速降至 {round(speed_kmh, 1)} km/h，占有率 {occ_pct}%。",
            f"2. 【空间排队】当前排队长度 {round(queue_m, 1)} 米，占路段库容比 {queue_ratio_pct}%，{'已越过' if queue_ratio >= 0.75 else '尚未触及'}回溢警戒线 (75%)。",
            f"3. 【成因归因】路段设计通行能力为 {CORRIDOR_ARTERIAL_LANES} 车道 ({CORRIDOR_DESIGN_CAPACITY_PCU_H} pcu/h，走廊标定值)，突发降速与排队激增表明存在【突发占道/瓶颈车道受阻】叠加【高峰车流集中汇聚】。",
            f"4. 【蔓延风险】按当前排队增速推演，上游交叉口存在被回溢车流锁死的风险。",
            f"5. 【旁路核查】平行分流通道当前占有率 {bypass_occ_pct}%，{'具备' if bypass_occ < 0.75 else '不具备'}实施动态诱导分流的备用容量。",
        ]
        plain_diag = (
            f"{bottleneck_edge} 路段当前排队 {round(queue_m, 1)} 米，车速仅 {round(speed_kmh, 1)} km/h，"
            f"建议立即发布分流诱导并延长主路绿灯。"
        )
        return {
            "severity_level": (
                "严重拥堵 (Level 4 - 重度)"
                if queue_ratio >= 0.5 or speed_kmh < 12
                else "中度拥堵 (Level 3)"
            ),
            "spillback_risk": (
                "极高 (High Risk of Gridlock)" if queue_ratio >= 0.5 else "中等"
            ),
            "root_causes": [
                "突发事故占道导致瓶颈断面通行能力跌落",
                "高峰主干线进出城流量集中汇聚",
                "下游交叉口既有信号配时未适配突发态势",
            ],
            "cot_reasoning": cot_steps,
            "can_reroute": bypass_occ < 0.75,
            "plain_diagnosis": plain_diag,
        }

    def diagnose_bottleneck(self, traffic_state: Dict[str, Any]) -> Dict[str, Any]:
        """
        Performs situational diagnosis and bottleneck attribution.

        Path A (preferred): LLM chain-of-thought attribution using live detector data.
        Path B (fallback)  : deterministic rule template — used when no API key is
                             configured or the model call fails, and honestly labelled
                             via `reasoning_mode` in the returned payload.
        """
        traffic_state = traffic_state or {}
        bottleneck_edge = str(traffic_state.get("bottleneck_edge") or "J1_J2")
        queue_m = _safe_float(traffic_state.get("queue_m"), default=165.0, min_val=0.0, max_val=10000.0)
        link_len = _safe_float(traffic_state.get("link_length_m"), default=300.0, min_val=10.0, max_val=10000.0)
        speed_kmh = _safe_float(traffic_state.get("speed_kmh"), default=8.2, min_val=0.0, max_val=200.0)
        occupancy = _safe_float(traffic_state.get("occupancy"), default=0.82, min_val=0.0, max_val=1.0)
        bypass_occ = _safe_float(traffic_state.get("bypass_occupancy"), default=0.28, min_val=0.0, max_val=1.0)

        queue_ratio = min(10.0, queue_m / max(1.0, link_len))
        plain_diag = (
            f"{bottleneck_edge} 路段当前排队 {round(queue_m, 1)} 米，车速仅 {round(speed_kmh, 1)} km/h，"
            f"建议立即发布分流诱导并延长主路绿灯。"
        )
        fallback = self._diagnose_deterministic(
            bottleneck_edge, queue_m, link_len, speed_kmh, occupancy, bypass_occ, queue_ratio
        )

        user_prompt = json.dumps(
            {
                "瓶颈断面": bottleneck_edge,
                "排队长度_米": queue_m,
                "路段长度_米": link_len,
                "排队占路段库容比": round(queue_ratio, 3),
                "瓶颈瞬时车速_kmh": speed_kmh,
                "断面车道占有率": occupancy,
                "平行旁路占有率": bypass_occ,
                "设计通行能力_pcu_h": CORRIDOR_DESIGN_CAPACITY_PCU_H,
                "回溢警戒线_库容比": 0.75,
                "场景": "晚高峰潮汐高负荷叠加突发占道事故",
            },
            ensure_ascii=False,
            indent=2,
        )

        payload, mode, error = self.llm.chat_json(
            DIAGNOSIS_SYSTEM_PROMPT, user_prompt, temperature=0.2, max_tokens=1200
        )

        if mode == LLMReasoningClient.MODE_LLM and payload:
            cot = self._clean_str_list(payload.get("cot_reasoning"), min_len=4)
            causes = self._clean_str_list(payload.get("root_causes"), min_len=1)
            severity = str(payload.get("severity_level") or "").strip()
            spillback = str(payload.get("spillback_risk") or "").strip()

            if cot and causes and severity and spillback:
                result = {
                    "severity_level": severity,
                    "spillback_risk": spillback,
                    "root_causes": causes,
                    "cot_reasoning": cot,
                    "can_reroute": _parse_bool(payload.get("can_reroute"), fallback["can_reroute"]),
                }
            else:
                # Model responded, but the schema was incomplete -> degrade, don't guess.
                result = dict(fallback)
                mode = LLMReasoningClient.MODE_ERROR
                error = "model response missing required diagnostic fields"
        else:
            result = dict(fallback)

        result["bottleneck_location"] = bottleneck_edge
        result.update(self.reasoning_metadata(mode, error))
        # plain_diagnosis is always ensured (set by deterministic fallback; LLM path gets it from fallback)
        result.setdefault("plain_diagnosis", plain_diag)
        result["input_state"] = {
            "queue_m": queue_m,
            "link_length_m": link_len,
            "speed_kmh": speed_kmh,
            "occupancy": occupancy,
            "bypass_occupancy": bypass_occ,
        }
        return result

    # ------------------------------------------------------------------ #
    # 2. Strategy formulation
    # ------------------------------------------------------------------ #
    def formulate_candidate_strategies(self, diagnosis: Dict[str, Any]) -> Dict[str, Any]:
        """
        Synthesizes candidate governance strategies.

        Numbers come from the traffic-engineering tools (authoritative).
        The LLM writes only the narrative layer (descriptions, rationale, VMS copy),
        and is explicitly forbidden from altering the computed values.
        """
        plan = self._tool_plan(diagnosis)
        timing = plan["timing"]
        gw_plan = plan["green_wave"]
        reroute_plan = plan["reroute"]
        arterial_green = plan["arterial_green"]

        strategy_a = {
            "id": "strategy_a_webster",
            "name": "方案 A：局部自适应信号优化 (Webster Adaptive)",
            "description": "基于 Webster 经典方法动态优化 J1-J3 交叉口信号周期与主路绿信比，提高瓶颈口放行效率。",
            "cycle_length": plan["actual_cycle"],
            "green_split_arterial": arterial_green,
            "green_split_cross": plan["cross_green"],
            "yellow_time": plan["yellow_time"],
            "reroute_ratio": 0.0,
            "green_wave": False,
        }

        reroute_ratio = float(reroute_plan.get("diversion_ratio", 0.0) or 0.0)
        reroute_pct = int(round(reroute_ratio * 100))

        if reroute_pct > 0:
            spatial_rationale = f"空域分流：上游 VMS 动态诱导 {reroute_pct}% 车辆走北部平行旁路，削减瓶颈输入负荷；"
            vms_desc = f" + 上游 VMS 动态诱导分流 {reroute_pct}% 至北部旁路"
        else:
            spatial_rationale = "空域控流：旁路已饱和或主路拥堵可控，动态诱导分流保持待命熔断，防止次生拥堵；"
            vms_desc = " + 动态诱导分流待命熔断（0%）"

        default_rationale = [
            f"时域扩容：Webster 动态调优主路绿信比至 {round(arterial_green / max(1.0, plan['actual_cycle']) * 100, 1)}%，提升瓶颈断面放行效率；",
            spatial_rationale,
            f"走廊协同：J1-J3 干线实施动态绿波协调（相位差 {gw_plan['offsets']}s），防止二次启停与回溢蔓延。",
        ]

        strategy_b = {
            "id": "strategy_b_agent_dss",
            "name": "方案 B：TrafficAgent-DSS 系统级协同调控 (推荐)",
            "description": (
                f"时空协同控制：Webster 动态调优（周期 {plan['actual_cycle']}s）"
                f" + J1-J3 双向绿波协调{vms_desc}。"
            ),
            "cycle_length": plan["actual_cycle"],
            "green_split_arterial": arterial_green,
            "green_split_cross": plan["cross_green"],
            "yellow_time": plan["yellow_time"],
            "green_wave_offsets": gw_plan["offsets"],
            "reroute_ratio": reroute_plan["diversion_ratio"],
            "vms_advisory": reroute_plan["vms_advisory"],
            "risk_warning": reroute_plan["risk_warning"],
            "green_wave": True,
            "rationale": default_rationale,
        }

        # ---- LLM narrative layer (no numbers produced here) ----
        user_prompt = json.dumps(
            {
                "诊断结论": {
                    "瓶颈断面": diagnosis.get("bottleneck_location"),
                    "警情等级": diagnosis.get("severity_level"),
                    "回溢风险": diagnosis.get("spillback_risk"),
                    "主要诱因": diagnosis.get("root_causes", []),
                },
                "工具计算结果_原样采用不得修改": {
                    "韦伯斯特最小延误周期_秒": timing["optimal_cycle"],
                    "设计周期_秒（过饱和修正后）": plan["actual_cycle"],
                    "主路绿灯_秒": arterial_green,
                    "支路绿灯_秒": plan["cross_green"],
                    "黄灯_秒": plan["yellow_time"],
                    "饱和度": timing["degree_of_saturation"],
                    "绿波相位差_秒": gw_plan["offsets"],
                    "绿波带宽_秒": gw_plan["bandwidth_seconds"],
                    "绿波带宽比_百分比": gw_plan["bandwidth_ratio_percent"],
                    "诱导分流比例": reroute_plan["diversion_ratio"],
                    "旁路剩余容量_辆每小时": reroute_plan.get("diverted_flow_vph"),
                },
            },
            ensure_ascii=False,
            indent=2,
        )

        payload, mode, error = self.llm.chat_json(
            STRATEGY_SYSTEM_PROMPT, user_prompt, temperature=0.3, max_tokens=1000
        )

        if mode == LLMReasoningClient.MODE_LLM and payload:
            desc_a = str(payload.get("strategy_a_description") or "").strip()
            desc_b = str(payload.get("strategy_b_description") or "").strip()
            rationale = self._clean_str_list(payload.get("strategy_b_rationale"), min_len=1)
            vms = str(payload.get("vms_advisory") or "").strip()
            risk = str(payload.get("risk_warning") or "").strip()

            if desc_b and rationale:
                narrative_texts = [t for t in (desc_a, desc_b, vms, risk, *rationale) if t]
                if self._narrative_numbers_traceable(narrative_texts, plan, reroute_pct):
                    if desc_a:
                        strategy_a["description"] = desc_a
                    strategy_b["description"] = desc_b
                    strategy_b["rationale"] = rationale
                    if vms:
                        strategy_b["vms_advisory"] = vms
                    if risk:
                        strategy_b["risk_warning"] = risk
                else:
                    # The narrative quoted figures the tools never produced. Reject the
                    # whole narrative instead of publishing an uncheckable claim: the
                    # deterministic template below only quotes computed values.
                    mode = LLMReasoningClient.MODE_ERROR
                    error = (
                        "model narrative quoted numbers not traceable to tool outputs "
                        "(numeric provenance guard)"
                    )
            else:
                mode = LLMReasoningClient.MODE_ERROR
                error = "model response missing required strategy narrative fields"

        narrative_mode = self.reasoning_metadata(mode, error)

        # Build action plan from the computed strategy + diagnosis
        action_plan = self.formulate_action_plan(diagnosis, {
            "strategy_a": strategy_a,
            "strategy_b": strategy_b,
            "reroute_details": reroute_plan,
        })

        return {
            "baseline": {
                "id": "baseline",
                "name": "现状基线：无干预 (Do-Nothing)",
                "description": "维持原有静态定时信号配时，不发布诱导分流信息。",
                "reroute_ratio": 0.0,
                "green_wave": False,
            },
            "strategy_a": strategy_a,
            "strategy_b": strategy_b,
            "webster_details": timing,
            "green_wave_details": gw_plan,
            "reroute_details": reroute_plan,
            "input_state_used": plan["inputs_used"],
            "narrative_mode": narrative_mode["reasoning_mode"],
            "narrative_engine": narrative_mode["reasoning_engine"],
            "narrative_label": narrative_mode["reasoning_label"],
            "narrative_error": narrative_mode["reasoning_error"],
            "action_plan": action_plan,
        }

    # ------------------------------------------------------------------ #
    # 2b. Action Plan
    # ------------------------------------------------------------------ #
    def formulate_action_plan(
        self,
        diagnosis: Dict[str, Any],
        strategies: Dict[str, Any],
        rollout: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Builds a concrete, step-by-step action playbook from the already-computed
        strategy and diagnosis values.  Every numeric parameter is pulled from the
        real tool output — nothing is invented.

        Returns:
            { "plain_summary": str, "steps": [{...}, ...] }
        """
        diag = diagnosis or {}
        strat_b = strategies.get("strategy_b") or {}
        strat_a = strategies.get("strategy_a") or {}
        reroute_det = strategies.get("reroute_details") or {}

        # Numeric parameters may legitimately be absent (e.g. a caller posts only a diagnosis).
        # Keep them as numbers-or-None and format at the point of use: the previous revision
        # substituted the string "—" here and then did arithmetic on it
        # (`green_arterial / max(1.0, cycle_len)`), so POST /api/action-plan raised TypeError
        # as soon as it was called without a fully populated strategies payload.
        def _num(value: Any) -> Optional[float]:
            if value is None:
                return None
            try:
                f = float(value)
            except (TypeError, ValueError):
                return None
            return f if math.isfinite(f) else None

        def _txt(value: Optional[float], unit: str = "") -> str:
            """Render a possibly-missing number; never invent a stand-in."""
            return "—" if value is None else f"{value:g}{unit}"

        cycle_len = _num(strat_b.get("cycle_length"))
        if cycle_len is None:
            cycle_len = _num(strat_a.get("cycle_length"))
        green_arterial = _num(strat_b.get("green_split_arterial"))
        if green_arterial is None:
            green_arterial = _num(strat_a.get("green_split_arterial"))
        green_cross = _num(strat_b.get("green_split_cross"))
        if green_cross is None:
            green_cross = _num(strat_a.get("green_split_cross"))
        yellow = _num(strat_b.get("yellow_time"))
        offsets = strat_b.get("green_wave_offsets") or []
        reroute_ratio = float(strat_b.get("reroute_ratio") or reroute_det.get("diversion_ratio") or 0.0)
        reroute_pct = int(round(reroute_ratio * 100))
        vms_msg = strat_b.get("vms_advisory") or reroute_det.get("vms_advisory") or "（未生成）"
        risk_msg = strat_b.get("risk_warning") or "（未生成）"
        can_reroute = diag.get("can_reroute", False)
        # Detector readings come from the diagnosis' own input state. When the caller did not
        # supply one, say so — the old code silently claimed "当前排队 165.0 米", a corridor
        # default the operator never entered.
        input_state = diag.get("input_state") or {}
        queue_m = _num(input_state.get("queue_m"))
        link_len = _num(input_state.get("link_length_m"))
        speed_kmh = _num(input_state.get("speed_kmh"))
        bottleneck_loc = diag.get("bottleneck_location", "—")
        severity = diag.get("severity_level", "—")

        # Improvement hints from rollout (optional, don't invent)
        delay_imp = "—"
        queue_imp = "—"
        comp = (rollout or {}).get("comparisons", {}).get("strategy_b", {}) if rollout else {}
        if isinstance(comp, dict):
            d = comp.get("delay_improvement_pct")
            if d is not None:
                delay_imp = f"{round(d, 1)}%"
            q = comp.get("queue_improvement_pct")
            if q is not None:
                queue_imp = f"{round(q, 1)}%"

        steps = []

        # --- Step 1: VMS 诱导分流 (immediate) ---
        if reroute_pct > 0 and can_reroute:
            steps.append({
                "n": len(steps) + 1,
                "phase": "立即 (0-2 分钟)",
                "title": "发布 VMS 诱导分流",
                "action": f"在上游可变信息板 (VMS) 发布分流指引，引导 {reroute_pct}% 车辆改走北部平行旁路。",
                "detail": f"目标动态分流比例：**{reroute_pct}%**。VMS 文案: {vms_msg}。",
                "where": f"瓶颈断面 [{bottleneck_loc}] 上游 1~2 公里 VMS 板",
                "when": "接到本指令后 2 分钟内完成发布并确认显示正常。",
                "expected": f"预计分流 {reroute_pct}% 到达需求，瓶颈断面输入负荷降低。",
                "owner": "信息发布员 / 信号控制员",
                "verify": "现场拍照或远程截图确认 VMS 文案显示，旁路占有率上升不超过 10 个百分点。",
            })
        else:
            steps.append({
                "n": len(steps) + 1,
                "phase": "立即 (0-2 分钟)",
                "title": "VMS 分流待命",
                "action": "当前工况未触发动态分流诱导。",
                "detail": "当前工况未触发动态分流诱导。",
                "where": f"瓶颈断面 [{bottleneck_loc}] 上游 VMS 板",
                "when": "持续监测旁路占有率，若旁路占有率降至 60% 以下再行发布。",
                "expected": "避免旁路二次拥堵。",
                "owner": "信息发布员",
                "verify": "每 5 分钟查看旁路占有率数据。",
            })

        # --- Step 2: 信号配时下发 ---
        if green_arterial is not None and cycle_len and cycle_len > 0:
            green_pct_text = f"主路绿灯占比提升至 {round(green_arterial / cycle_len * 100, 1)}%，瓶颈断面放行效率提高。"
        else:
            green_pct_text = "配时参数未完整下发，绿信比以信号机实际回执为准。"
        steps.append({
            "n": len(steps) + 1,
            "phase": "立即 (0-5 分钟)",
            "title": "下发 Webster 动态信号配时",
            "action": f"将 J1-J3 交叉口信号切换为 Webster 动态优化配时。",
            "detail": f"周期 {_txt(cycle_len, ' 秒')} / 主路绿灯 {_txt(green_arterial, ' 秒')} / "
                      f"支路绿灯 {_txt(green_cross, ' 秒')} / 黄灯 {_txt(yellow, ' 秒')}。",
            "where": f"J1、J2、J3 三座交叉口信号机",
            "when": "与 VMS 发布同步下发，信号机确认接受。",
            "expected": green_pct_text,
            "owner": "信号控制员",
            "verify": "信号机返回配时更新确认码；检查主路实际绿灯时长不低于设定值。",
        })

        # --- Step 3: 绿波协调 ---
        if offsets and any(o > 0 for o in offsets):
            offset_str = ", ".join(f"J{i+1}: {o}s" for i, o in enumerate(offsets))
            steps.append({
                "n": len(steps) + 1,
                "phase": "同步 (5-10 分钟)",
                "title": "启用干线绿波协调 (相位差 5-25 秒级)",
                "action": "将 J1-J3 干线切换到绿波协调模式，设置各交叉口相位差。",
                "detail": f"相位差: {offset_str}。绿波协调即通过错开各路口绿灯启动时间，让车流到达下一路口时正好赶上绿灯。",
                "where": f"J1→J2→J3 干线所有交叉口",
                "when": "信号配时下发确认后 5 分钟内切换至绿波模式。",
                "expected": f"减少车队二次启停，预计排队长度缩短 {queue_imp}。",
                "owner": "信号控制员 / 干线协调中心",
                "verify": "查看干线协调状态面板，确认各路口相位差生效；排队长度开始下降。",
            })

        # --- Step 4: 现场/上游管控与回溢防护 ---
        queue_ratio = None
        if queue_m is not None and link_len and link_len > 0:
            queue_ratio = min(1.0, queue_m / link_len)
        if queue_ratio is None:
            spillback_detail = "本次未获得排队长度/路段长度检测数据，无法判定回溢风险（请核对输入状态后再下发）。"
        else:
            spillback警戒 = "已" if queue_ratio >= 0.75 else "尚未"
            spillback_detail = (
                f"当前排队 {round(queue_m, 1)} 米，占路段 {round(queue_ratio * 100, 1)}%，"
                f"{spillback警戒}触及 75% 回溢警戒线。"
            )
        steps.append({
            "n": len(steps) + 1,
            "phase": "持续 (全程监测)",
            "title": "上游管控与回溢防护",
            "action": f"监测上游交叉口排队，若回溢风险升高立即启动上游限速或间歇放行。",
            "detail": spillback_detail,
            "where": f"瓶颈上游各交叉口入口断面",
            "when": "接到本指令后立即启动，每 2 分钟复核一次。",
            "expected": f"防止 {bottleneck_loc} 排队回溢至上游路口造成网格锁死。",
            "owner": "现场交警 / 路口管理员",
            "verify": "上游路口排队长度未超过路口停止线外 50 米；若超标立即采取截流措施。",
        })

        # --- Step 5: 监测验证指标 ---
        speed_target_text = (
            f"瓶颈车速目标 > {round(speed_kmh * 1.5, 1)} km/h"
            if speed_kmh is not None
            else "瓶颈车速目标以现场实测基线为参照（本次未输入车速数据）"
        )
        steps.append({
            "n": len(steps) + 1,
            "phase": "持续 (10-30 分钟)",
            "title": "成效监测与验证",
            "action": "持续跟踪核心指标变化，验证治理措施是否生效。",
            "detail": f"关键监测指标: {speed_target_text}；排队缩短 {queue_imp}；延误降低 {delay_imp}。",
            "where": f"{bottleneck_loc} 断面检测器 + 全线检测器",
            "when": "配时下发后 10 分钟开始首次复核。",
            "expected": f"治理措施生效后，瓶颈车速回升、排队缩短、延误下降。",
            "owner": "指挥中心值班长",
            "verify": "检测器数据趋势连续 3 个信号周期 (约 5 分钟) 改善即视为生效。",
        })

        # --- Step 6: 结束与恢复 ---
        recovery_text = (
            f"恢复前确认瓶颈车速回升至 {round(speed_kmh * 2.5, 1)} km/h 以上且排队低于 50 米。"
            if speed_kmh is not None
            else "恢复前确认瓶颈车速与排队已回落至常态水平（以现场实测为准）。"
        )
        steps.append({
            "n": len(steps) + 1,
            "phase": "恢复 (事故清除后 15-30 分钟)",
            "title": "恢复正常信号配时",
            "action": "事故清除、排队消散后，逐步恢复原有定时信号配时方案。",
            "detail": f"{recovery_text}分两步回落: 先退绿波→再退 Webster 优化→恢复定时方案。",
            "where": f"J1-J3 全线交叉口",
            "when": "确认事故清除、拥堵解除后 15 分钟内执行。",
            "expected": "平稳回落至正常配时，避免车流骤停。",
            "owner": "信号控制员",
            "verify": "全线排队 < 50 米且车速 > 30 km/h 维持 10 分钟。",
        })

        # --- Step 7: 兜底预案 ---
        steps.append({
            "n": len(steps) + 1,
            "phase": "预案 (随时启动)",
            "title": "兜底预案：远端截流 + 路侧引导",
            "action": "若主路措施在 15 分钟内未见明显改善，启动远端路网截流和路侧人工引导。",
            "detail": f"① 远端 (上游 3~5 公里) 路口实施间歇放行，控制进入走廊的车流；② 安排路侧人员在关键合流点人工指挥；③ {risk_msg}",
            "where": "远端路网关键路口 + 瓶颈路段沿线合流点",
            "when": "措施执行 15 分钟后复核仍无改善时启动。",
            "expected": "防止拥堵进一步蔓延至远端路网。",
            "owner": "指挥中心 → 现场交警 → 路侧疏导员",
            "verify": "远端路口排队不再增长；走廊内排队增速转负。",
        })

        # Plain summary
        state_phrase = (
            f"排队 {round(queue_m, 1)} 米、车速 {round(speed_kmh, 1)} km/h"
            if (queue_m is not None and speed_kmh is not None)
            else "排队/车速检测数据未完整输入"
        )
        plain_summary = (
            f"{bottleneck_loc} 当前严重拥堵（{severity}），{state_phrase}。"
            f"建议立即发布 VMS 分流{'（' + str(reroute_pct) + '%）' if reroute_pct > 0 else '待命'}，"
            f"同步切换 Webster 动态信号（周期 {_txt(cycle_len, ' 秒')}、主路绿灯 {_txt(green_arterial, ' 秒')}）"
            + (f"并启用绿波协调（相位差 {', '.join(str(o) + 's' for o in offsets)}）" if offsets else "")
            + f"，预计延误降低{delay_imp}、排队缩短{queue_imp}。"
        )

        return {
            "plain_summary": plain_summary,
            "steps": steps,
        }

    # ------------------------------------------------------------------ #
    # 3. What-If rollout
    # ------------------------------------------------------------------ #
    def execute_what_if_rollout(
        self,
        duration: int = 600,
        incident_start: int = 150,
        incident_end: int = 420,
        use_rerouting: bool = True,
        use_green_wave: bool = True,
        use_webster: bool = True,
        diagnosis: Optional[Dict[str, Any]] = None,
        seed: Optional[int] = None,
        policy: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Executes parallel What-If rollouts in SUMO for Baseline, Strategy A, and Strategy B,
        then evaluates 5-dimensional performance indices.

        The control switches below are forwarded verbatim into the TraCI sandbox, so the
        difference between strategies is caused by controls that are actually applied.

        When a diagnosis payload is supplied, the Webster / green-wave / rerouting parameters
        are derived from the observed incident state instead of the corridor calibration
        defaults, closing the diagnosis -> strategy loop.
        """
        if duration <= 0:
            raise ValueError(f"duration ({duration}) must be positive")
        if incident_start < 0:
            raise ValueError(f"incident_start ({incident_start}) cannot be negative")
        if incident_start >= incident_end:
            raise ValueError(f"incident_start ({incident_start}) must be strictly less than incident_end ({incident_end})")
        if incident_end > duration:
            raise ValueError(f"incident_end ({incident_end}) cannot exceed duration ({duration})")

        safe_seed = None
        if seed is not None:
            try:
                safe_seed = int(seed)
                if safe_seed < 0:
                    raise ValueError(f"seed must be non-negative, got {seed}")
            except (ValueError, TypeError) as err:
                raise ValueError(f"Invalid random seed: {seed} ({err})")

        plan = self._tool_plan(diagnosis, policy=policy)

        # A valid policy may also decide *whether* to coordinate; when it is silent
        # the caller's switch governs, exactly as before.
        policy_coordinated = None
        if policy and isinstance(policy.get("coordinated"), bool):
            policy_coordinated = policy["coordinated"]
        green_wave_active = use_green_wave if policy_coordinated is None else policy_coordinated

        # The signal program actually deployed (single-point, i.e. every junction starting
        # at phase 0, or green-wave-aligned) is produced by `build_control_params` — the
        # single source of truth shared with the closed-loop optimiser. Both use the same
        # Webster timing aligned with the network's two release phases.

        # Probe the sandbox signature ONCE rather than catching TypeError as control flow.
        # The previous `try: ... except TypeError: <rerun without seed>` pattern could not
        # tell "this sandbox does not accept seed" apart from "something inside the
        # simulation raised TypeError": in the latter case it silently launched a *second*
        # SUMO process and then re-raised, wasting a run and hiding the real cause.
        try:
            _sandbox_supports_seed = "seed" in inspect.signature(
                self.sandbox.run_simulation
            ).parameters
        except (TypeError, ValueError):  # pragma: no cover - exotic callables
            _sandbox_supports_seed = False

        def _run_sandbox(scheme_name: str, ctl: Dict[str, Any]) -> Dict[str, Any]:
            kwargs: Dict[str, Any] = dict(
                scheme=scheme_name,
                duration=duration,
                incident_start=incident_start,
                incident_end=incident_end,
                control_params=ctl,
            )
            if _sandbox_supports_seed:
                kwargs["seed"] = safe_seed
            return self.sandbox.run_simulation(**kwargs)

        # Run Baseline
        print("[Agent] Rolling out Baseline (Do-Nothing)...")
        res_base = _run_sandbox(
            "baseline",
            {"reroute_ratio": 0.0, "green_wave": False, "webster": False},
        )
        kpi_base = self.evaluator.compute_summary_kpi(res_base)

        # Run Strategy A (Webster only, single-point adaptive)
        print("[Agent] Rolling out Strategy A (Webster Adaptive)...")
        res_a = _run_sandbox(
            "webster",
            self.build_control_params(plan, False, use_rerouting=False, use_webster=use_webster),
        )
        kpi_a = self.evaluator.compute_summary_kpi(res_a)
        comp_a = self.evaluator.compare_schemes(kpi_base, kpi_a)

        # Run Strategy B (TrafficAgent-DSS coordinated)
        print("[Agent] Rolling out Strategy B (Coordinated Agent-DSS)...")
        res_b = _run_sandbox(
            "agent_dss",
            self.build_control_params(plan, green_wave_active, use_rerouting=use_rerouting, use_webster=use_webster),
        )
        kpi_b = self.evaluator.compute_summary_kpi(res_b)
        comp_b = self.evaluator.compare_schemes(kpi_base, kpi_b)

        def _extract_speeds(r: Dict[str, Any]) -> List[float]:
            if "bottleneck_speeds_kmh" in r:
                return r["bottleneck_speeds_kmh"]
            if "vehicle_speeds" in r:
                return [round(s * 3.6, 1) for s in r["vehicle_speeds"]]
            return []

        return {
            "execution_mode": "physical_sumo_sandbox",
            "simulation_duration": duration,
            "seed": seed,
            "time_stamps": res_base.get("time_stamps", []),
            "strategy_inputs": plan["inputs_used"],
            "control_evidence": {
                "baseline": res_base.get("control_evidence", {}),
                "strategy_a": res_a.get("control_evidence", {}),
                "strategy_b": res_b.get("control_evidence", {}),
            },
            "raw_traces": {
                "baseline": {
                    "queues": res_base.get("queue_lengths", []),
                    "speeds": _extract_speeds(res_base),
                    "delays": res_base.get("vehicle_delays", []),
                },
                "strategy_a": {
                    "queues": res_a.get("queue_lengths", []),
                    "speeds": _extract_speeds(res_a),
                    "delays": res_a.get("vehicle_delays", []),
                },
                "strategy_b": {
                    "queues": res_b.get("queue_lengths", []),
                    "speeds": _extract_speeds(res_b),
                    "delays": res_b.get("vehicle_delays", []),
                },
            },
            "kpis": {
                "baseline": kpi_base,
                "strategy_a": kpi_a,
                "strategy_b": kpi_b,
            },
            "comparisons": {
                "strategy_a": comp_a,
                "strategy_b": comp_b,
            },
        }

    def run_multi_seed_evaluation(
        self,
        seeds: Optional[List[int]] = None,
        duration: int = 600,
        incident_start: int = 150,
        incident_end: int = 420,
        diagnosis: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """
        Executes multi-seed batch simulation runs to evaluate statistical significance
        and confidence intervals across stochastic traffic assignments.
        """
        import numpy as np

        if seeds is None:
            seeds = [42, 101, 2024, 777, 999]

        if not isinstance(seeds, (list, tuple)) or len(seeds) == 0:
            raise ValueError("seeds must be a non-empty sequence of valid integers")

        validated_seeds = []
        for s in seeds:
            try:
                s_int = int(s)
                if s_int < 0:
                    raise ValueError(f"seed must be non-negative, got {s}")
                validated_seeds.append(s_int)
            except (ValueError, TypeError) as err:
                raise ValueError(f"Invalid seed in seeds list: {s} ({err})")
        seeds = validated_seeds

        batch_results = []
        for s in seeds:
            res = self.execute_what_if_rollout(
                duration=duration,
                incident_start=incident_start,
                incident_end=incident_end,
                diagnosis=diagnosis,
                seed=s,
            )
            batch_results.append(res)

        schemes = ["baseline", "strategy_a", "strategy_b"]
        metric_keys = [
            "avg_delay_s", "max_queue_m", "avg_speed_kmh",
            "throughput_vph", "delay_variance", "co2_emissions_kg", "fuel_liters"
        ]
        summary = {}
        for sc in schemes:
            summary[sc] = {}
            for m in metric_keys:
                vals = [
                    float(r["kpis"][sc][m])
                    for r in batch_results
                    if sc in r.get("kpis", {}) and m in r.get("kpis", {}).get(sc, {}) and r["kpis"][sc][m] is not None
                ]
                if vals:
                    mean_v = float(np.mean(vals))
                    std_v = float(np.std(vals, ddof=1)) if len(vals) > 1 else 0.0
                    summary[sc][m] = {
                        "mean": round(mean_v, 2),
                        "std": round(std_v, 2),
                        "min": round(float(np.min(vals)), 2),
                        "max": round(float(np.max(vals)), 2),
                    }

        comp_keys = [
            "delay_improvement_pct", "queue_improvement_pct",
            "speed_improvement_pct", "throughput_improvement_pct",
            "variance_improvement_pct", "co2_improvement_pct", "fuel_improvement_pct"
        ]
        b_improvements = {}
        for ck in comp_keys:
            vals = [
                float(r["comparisons"]["strategy_b"][ck])
                for r in batch_results
                if "comparisons" in r and "strategy_b" in r["comparisons"] and ck in r["comparisons"]["strategy_b"] and r["comparisons"]["strategy_b"][ck] is not None
            ]
            if vals:
                mean_v = float(np.mean(vals))
                std_v = float(np.std(vals, ddof=1)) if len(vals) > 1 else 0.0
                sem_v = float(std_v / np.sqrt(len(vals))) if len(vals) > 1 else 0.0
                ci_low = round(mean_v - 1.96 * sem_v, 2)
                ci_high = round(mean_v + 1.96 * sem_v, 2)
                b_improvements[ck] = {
                    "mean": round(mean_v, 2),
                    "std": round(std_v, 2),
                    "sem": round(sem_v, 2),
                    "ci_95": [ci_low, ci_high],
                }

        delay_stat = b_improvements.get("delay_improvement_pct", {})
        delay_mean = delay_stat.get("mean", 0.0)
        delay_ci = delay_stat.get("ci_95", [0.0, 0.0])
        statistically_significant = (
            len(batch_results) >= 2
            and delay_mean > 0.0
            and delay_ci[0] > 0.0
        )

        return {
            "seeds_tested": seeds,
            "sample_size": len(seeds),
            "summary_by_scheme": summary,
            "strategy_b_improvements": b_improvements,
            "statistically_significant": statistically_significant,
        }

    # ------------------------------------------------------------------ #
    # 3b. Closed-loop control-policy optimisation (LLM inside the loop)
    # ------------------------------------------------------------------ #
    MAX_CLOSED_LOOP_ROUNDS = 4

    # A candidate round is discarded when it buys delay reduction by dumping queue
    # onto the corridor: the project's governance goal is not "minimise one metric".
    QUEUE_BLOWUP_RATIO = 1.25

    def optimize_control_policy_closed_loop(
        self,
        diagnosis: Dict[str, Any],
        rounds: int = 2,
        duration: int = 600,
        incident_start: int = 150,
        incident_end: int = 420,
        seed: Optional[int] = None,
        use_rerouting: bool = True,
        use_webster: bool = True,
    ) -> Dict[str, Any]:
        """
        Puts the LLM **inside the control loop** and measures what happens.

        Round k sends the model: the detector state, the deterministic toolchain
        baseline, and — from round 2 onward — the *measured* KPI delta of its own
        previous decision. The model returns a new parameter set. Every parameter set
        is schema-validated and clipped into the feasible domain before deployment, and
        the outcome is measured by SUMO, never predicted by the model.

        Honest degradation: when no usable proposal exists (model unconfigured, SDK
        missing, unreachable, schema-invalid, or a rationale that quoted numbers the
        tools never produced) the loop stops and the deterministic result stands, with
        `decision_mode = deterministic_rule_chain` and the reason recorded. No
        "optimised" figure is ever invented to fill the gap.

        Returns a fully auditable trace: per round, what the model requested, what was
        actually deployed after clipping, why anything was clipped, and the measured KPI.
        """
        total_requested = int(rounds) if isinstance(rounds, (int, float)) and not isinstance(rounds, bool) else 1
        total_rounds = max(0, min(self.MAX_CLOSED_LOOP_ROUNDS, total_requested))

        # ---- sandbox plumbing (mirrors execute_what_if_rollout) ------------- #
        try:
            supports_seed = "seed" in inspect.signature(self.sandbox.run_simulation).parameters
        except (TypeError, ValueError):  # pragma: no cover - exotic callables
            supports_seed = False
        safe_seed = (
            int(seed) if isinstance(seed, (int, float)) and not isinstance(seed, bool) else None
        )

        def _sim(scheme: str, controls: Dict[str, Any]) -> Dict[str, Any]:
            kwargs: Dict[str, Any] = dict(
                scheme=scheme,
                duration=duration,
                incident_start=incident_start,
                incident_end=incident_end,
                control_params=controls,
            )
            if supports_seed:
                kwargs["seed"] = safe_seed
            return self.sandbox.run_simulation(**kwargs)

        def _controls_from_plan(plan: Dict[str, Any], coordinated: bool) -> Dict[str, Any]:
            return self.build_control_params(
                plan, coordinated, use_rerouting=use_rerouting, use_webster=use_webster
            )

        print("[Agent] Closed-loop 1/3: baseline (do-nothing) rollout...")
        base_kpi = self.evaluator.compute_summary_kpi(
            _sim("baseline", {"reroute_ratio": 0.0, "green_wave": False, "webster": False})
        )

        det_plan = self._tool_plan(diagnosis)
        print("[Agent] Closed-loop 2/3: deterministic rule-chain reference rollout...")
        det_kpi = self.evaluator.compute_summary_kpi(
            _sim("agent_dss", _controls_from_plan(det_plan, True))
        )

        def _delay_of(kpi: Dict[str, Any]) -> Optional[float]:
            v = (kpi or {}).get("avg_delay_s")
            return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None

        baseline_queue = (base_kpi or {}).get("max_queue_m")
        baseline_queue = float(baseline_queue) if isinstance(baseline_queue, (int, float)) else None

        # The deterministic result is the incumbent; a round must beat it to be adopted.
        best: Dict[str, Any] = {
            "source": "deterministic_rule_chain",
            "round": 0,
            "policy": None,
            "plan": det_plan,
            "kpi": det_kpi,
            "delay_s": _delay_of(det_kpi),
        }

        round_trace: List[Dict[str, Any]] = []
        feedback: Optional[Dict[str, Any]] = None
        decision_errors: List[str] = []
        decision_engine = "deterministic_rule_chain"
        decision_mode = "deterministic_rule_chain"

        corridor_inputs = det_plan.get("inputs_used") or {}
        upstream_vph = float(corridor_inputs.get("upstream_flow_vph") or 1800.0)
        bypass_spare_vph = float(corridor_inputs.get("bypass_spare_capacity_vph") or 1200.0)
        reroute_capacity_cap = min(
            POLICY_BOUNDS["reroute_ratio"][1],
            bypass_spare_vph / max(1.0, upstream_vph),
        )

        detector_state = (diagnosis or {}).get("input_state") or {}

        for idx in range(1, total_rounds + 1):
            context: Dict[str, Any] = {
                "round_index": idx,
                "max_rounds": total_rounds,
                "diagnosis": {
                    "bottleneck_location": diagnosis.get("bottleneck_location"),
                    "severity_level": diagnosis.get("severity_level"),
                    "spillback_risk": diagnosis.get("spillback_risk"),
                    "root_causes": diagnosis.get("root_causes", []),
                },
                "detector_state": {
                    "queue_m": detector_state.get("queue_m"),
                    "link_length_m": detector_state.get("link_length_m"),
                    "occupancy": detector_state.get("occupancy"),
                    "bypass_occupancy": detector_state.get("bypass_occupancy"),
                    "queue_ratio": round(
                        float(detector_state.get("queue_m", 0.0) or 0.0)
                        / max(1.0, float(detector_state.get("link_length_m", 1.0) or 1.0)),
                        3,
                    ),
                },
                "baseline_plan": {
                    "design_cycle_s": det_plan["actual_cycle"],
                    "arterial_green_s": det_plan["arterial_green"],
                    "cross_green_s": det_plan["cross_green"],
                    "yellow_s": det_plan["yellow_time"],
                    "green_wave_offsets_s": det_plan["green_wave"]["offsets"],
                    "green_wave_bandwidth_ratio_pct": det_plan["green_wave"]["bandwidth_ratio_percent"],
                    "progression_speed_kmh": det_plan["green_wave"]["progression_speed_kmh"],
                    "reroute_ratio": det_plan["reroute"]["diversion_ratio"],
                    "reroute_capacity_cap": round(reroute_capacity_cap, 4),
                    "corridor_defaults_from": corridor_inputs.get("source"),
                },
                "feedback": feedback,
            }

            proposal = self.decision.propose(context)
            applied = proposal.get("applied")
            decision_engine = proposal.get("engine") or decision_engine

            if applied is None:
                # Stop the loop and keep whatever was already measured. The caller can
                # read `errors` to see exactly why the model could not be used.
                decision_errors = list(proposal.get("errors") or ["no deployable policy"])
                print(f"[Agent] Closed-loop round {idx}: no deployable policy -> {decision_errors}")
                break

            policy = {
                "target_cycle_s": applied["target_cycle_s"],
                "arterial_green_share": applied["arterial_green_share"],
                "reroute_ratio": applied["reroute_ratio"],
                "progression_speed_kmh": applied["progression_speed_kmh"],
                "coordinated": applied["coordinated"],
                "decision_rationale": applied.get("decision_rationale", ""),
            }
            plan = self._tool_plan(diagnosis, policy=policy)
            print(f"[Agent] Closed-loop round {idx}: deploying model-decided policy to SUMO...")
            kpi = self.evaluator.compute_summary_kpi(
                _sim("agent_dss", _controls_from_plan(plan, applied["coordinated"]))
            )
            comparison = self.evaluator.compare_schemes(base_kpi, kpi)
            delay_s = _delay_of(kpi)

            queue_m = kpi.get("max_queue_m")
            queue_ok = True
            if (
                isinstance(queue_m, (int, float)) and not isinstance(queue_m, bool)
                and baseline_queue is not None and baseline_queue > 0
                and float(queue_m) > baseline_queue * self.QUEUE_BLOWUP_RATIO
            ):
                queue_ok = False

            adopted = False
            if delay_s is not None and queue_ok:
                incumbent = best.get("delay_s")
                if incumbent is None or delay_s < float(incumbent):
                    adopted = True
                    best = {
                        "source": "llm_decision_layer",
                        "round": idx,
                        "policy": policy,
                        "plan": plan,
                        "kpi": kpi,
                        "delay_s": delay_s,
                    }

            round_trace.append({
                "round": idx,
                "decision_mode": proposal.get("mode"),
                "decision_engine": proposal.get("engine"),
                "model_requested": proposal.get("requested"),
                "deployed_after_clipping": applied,
                "clipping_adjustments": proposal.get("adjustments") or [],
                "rationale": proposal.get("rationale"),
                "measured_kpi": kpi,
                "vs_baseline_pct": {
                    k: comparison.get(k) for k in (
                        "delay_improvement_pct",
                        "queue_improvement_pct",
                        "throughput_improvement_pct",
                        "co2_improvement_pct",
                        "speed_improvement_pct",
                    )
                },
                "queue_constraint_respected": queue_ok,
                "adopted_as_best": adopted,
            })

            if proposal.get("mode") == LLMReasoningClient.MODE_LLM:
                decision_mode = "llm_closed_loop"

            # Feed the *measured* outcome back for the next round.
            feedback = {
                "round": idx,
                "applied_policy": {
                    "target_cycle_s": applied["target_cycle_s"],
                    "arterial_green_share": applied["arterial_green_share"],
                    "reroute_ratio": applied["reroute_ratio"],
                    "progression_speed_kmh": applied["progression_speed_kmh"],
                    "coordinated": applied["coordinated"],
                },
                "kpi_delta": round_trace[-1]["vs_baseline_pct"],
                "measured": {
                    "avg_delay_s": kpi.get("avg_delay_s"),
                    "max_queue_m": kpi.get("max_queue_m"),
                    "throughput_vph": kpi.get("throughput_vph"),
                },
                "adopted": adopted,
            }

        best_plan = best["plan"]
        best_controls = best_plan.get("policy_overlay", {}).get("applied")

        # Verdict — an "optimal" plan that still loses to doing nothing must say so.
        # Reporting it as the recommended action without this flag would be exactly the
        # kind of output-vs-fact mismatch this project treats as a red line.
        best_delay = _delay_of(best["kpi"])
        base_delay = _delay_of(base_kpi)
        beats_baseline = (
            best_delay is not None and base_delay is not None and best_delay < base_delay
        )
        if beats_baseline:
            verdict = "adopt_best_policy"
            verdict_note = (
                f"最优方案实测平均延误 {best_delay:.1f} s/veh 低于无干预基线 {base_delay:.1f} s/veh，建议下发。"
            )
        else:
            verdict = "do_nothing_is_better_under_measured_conditions"
            verdict_note = (
                "本次推演中所有候选方案（含最优者）的实测平均延误均不低于无干预基线 —— "
                "系统不建议下发控制指令。该结论仅对应本次推演的工况与随机种子；"
                "如需用于决策，请改用标定工况（600s 推演 / 事故窗口 150–420s）并做多种子复验。"
            )

        return {
            "execution_mode": "physical_sumo_sandbox",
            "simulation_duration": duration,
            "seed": seed,
            "decision_mode": decision_mode,
            "decision_engine": decision_engine,
            "decision_errors": decision_errors,
            "rounds_requested": total_requested,
            "rounds_executed": len(round_trace),
            "corridor_inputs": corridor_inputs,
            "baseline_kpi": base_kpi,
            "deterministic_kpi": det_kpi,
            "deterministic_vs_baseline_pct": self.evaluator.compare_schemes(base_kpi, det_kpi),
            "rounds": round_trace,
            "recommendation": {
                "verdict": verdict,
                "adopt_control": beats_baseline,
                "note": verdict_note,
                "best_avg_delay_s": best_delay,
                "baseline_avg_delay_s": base_delay,
            },
            "best": {
                "source": best["source"],
                "round": best["round"],
                "policy": best["policy"],
                "applied_control_parameters": {
                    "cycle_s": best_plan["actual_cycle"],
                    "arterial_green_s": best_plan["arterial_green"],
                    "cross_green_s": best_plan["cross_green"],
                    "yellow_s": best_plan["yellow_time"],
                    "green_wave_offsets_s": best_plan["green_wave"]["offsets"],
                    "green_wave_bandwidth_ratio_pct": best_plan["green_wave"]["bandwidth_ratio_percent"],
                    "progression_speed_kmh": best_plan["green_wave"]["progression_speed_kmh"],
                    "reroute_ratio": best_plan["reroute"]["diversion_ratio"],
                    "coordinated": (
                        (best_controls or {}).get("coordinated")
                        if best_controls else True
                    ),
                    "policy_source": best_plan.get("policy_overlay", {}).get("source"),
                },
                "measured_kpi": best["kpi"],
                "vs_baseline_pct": self.evaluator.compare_schemes(base_kpi, best["kpi"]),
            },
        }

    # ------------------------------------------------------------------ #
    # 4. Decision briefing
    # ------------------------------------------------------------------ #
    @staticmethod
    def _fmt(value: Any, unit: str = "", missing: str = "—") -> str:
        """Formats a metric, returning an explicit placeholder when it is unavailable."""
        if value is None:
            return missing
        if isinstance(value, float):
            return f"{value:g}{unit}"
        return f"{value}{unit}"

    def generate_decision_report(
        self,
        diagnosis: Dict[str, Any],
        strategies: Dict[str, Any],
        rollout_results: Dict[str, Any],
    ) -> str:
        """
        Generates a standardized Markdown Decision Support Briefing.

        Integrity rule: every figure in this report must come from an actual tool
        computation or simulation run. When data is missing the report says so
        explicitly instead of substituting an illustrative value.
        """
        diagnosis = diagnosis or {}
        strategies = strategies or {}
        rollout_results = rollout_results or {}

        kpis = rollout_results.get("kpis") or {}
        comparisons = rollout_results.get("comparisons") or {}
        comp_b = comparisons.get("strategy_b")

        has_kpis = all(kpis.get(k) for k in ("baseline", "strategy_a", "strategy_b"))

        # Derive the comparison from measured KPIs whenever possible.
        if comp_b is None and has_kpis:
            comp_b = self.evaluator.compare_schemes(kpis["baseline"], kpis["strategy_b"])

        kpi_base = kpis.get("baseline") or {}
        kpi_a = kpis.get("strategy_a") or {}
        kpi_b = kpis.get("strategy_b") or {}

        execution_mode = rollout_results.get("execution_mode") or "unknown"
        mode_label = {
            "physical_sumo_sandbox": "SUMO 微观物理仿真（TraCI 实测数据）",
            "mesoscopic_network": "真实路网中观推演引擎（HCM/Webster 排队模型，非微观仿真）",
            "calibrated_empirical_fallback": "标定经验数据（物理仿真不可用时的降级估算，非实测）",
            "calibrated_empirical_fast": "标定经验数据（快速交互模式，非实测）",
            "unknown": "⚠️ 数据来源未标注 —— 本报告不得作为量化结论对外使用",
        }.get(execution_mode, execution_mode)

        # The report header must never claim a verified physical roll-out when the numbers
        # actually came from calibrated (non-measured) data — that would contradict the
        # provenance table printed a few lines below.
        if execution_mode == "physical_sumo_sandbox":
            verification_status = "数字孪生沙盒推演完成 (What-If Simulation Verified)"
        elif execution_mode == "mesoscopic_network":
            verification_status = (
                "真实路网中观推演完成（排队模型计算，非微观车辆仿真）"
                "(Mesoscopic Network Rollout — NOT a micro-simulation)"
            )
        else:
            verification_status = (
                "标定经验数据（非实测）(Calibrated Estimate — NOT a simulation run)"
            )

        reasoning_label = diagnosis.get("reasoning_label", "确定性规则模板")
        narrative_label = strategies.get("narrative_label", "确定性规则模板")
        evidence = (rollout_results.get("control_evidence") or {}).get("strategy_b", {})
        seed_label = str(rollout_results.get("seed") if rollout_results.get("seed") is not None else "未指定（默认随机）")

        strat_b = strategies.get("strategy_b") or {}

        # --- Comparison table rows -------------------------------------- #
        def row(label: str, key: str, unit: str, better: str, worse: str) -> str:
            base_v, a_v, b_v = kpi_base.get(key), kpi_a.get(key), kpi_b.get(key)
            if comp_b and key in (
                "avg_delay_s", "max_queue_m", "avg_speed_kmh", "throughput_vph",
                "delay_variance", "co2_emissions_kg", "fuel_liters"
            ):
                pct_key = {
                    "avg_delay_s": "delay_improvement_pct",
                    "max_queue_m": "queue_improvement_pct",
                    "avg_speed_kmh": "speed_improvement_pct",
                    "throughput_vph": "throughput_improvement_pct",
                    "delay_variance": "variance_improvement_pct",
                    "co2_emissions_kg": "co2_improvement_pct",
                    "fuel_liters": "fuel_improvement_pct",
                }[key]
                pct = comp_b.get(pct_key)
                if pct is not None:
                    if pct > 0:
                        pct_cell = f"**{better} {pct}%**"
                    elif pct < 0:
                        pct_cell = f"**{worse} {abs(pct)}%**"
                    else:
                        pct_cell = "**持平 (0.0%)**"
                else:
                    pct_cell = "—"
            else:
                pct_cell = "—"
            return (
                f"| **{label}** | {self._fmt(base_v, unit)} | {self._fmt(a_v, unit)} | "
                f"**{self._fmt(b_v, unit)}** | {pct_cell} |"
            )

        table_rows = "\n".join(
            [
                row("平均车辆延误 (s/veh)", "avg_delay_s", " s", "降低", "增加"),
                row("最大排队长度 (m)", "max_queue_m", " m", "缩短", "增加"),
                row("瓶颈平均车速 (km/h)", "avg_speed_kmh", " km/h", "提升", "下降"),
                row("路网通行吞吐量 (veh/h)", "throughput_vph", " veh/h", "提升", "下降"),
                row("路网延误时序波动 (s²)", "delay_variance", " s²", "降低", "增加"),
                row("碳排放总量 (kg CO2)", "co2_emissions_kg", " kg", "减排", "增排"),
                row("燃油消耗估算 (L)", "fuel_liters", " L", "降低", "增加"),
            ]
        )

        speed_tradeoff_note = ""
        if comp_b and (comp_b.get("speed_improvement_pct") or 0.0) < 0:
            speed_tradeoff_note = (
                "\n> 💡 **速度指标说明**：方案 B 断面平均车速较基线有所下降；"
                "该指标与延误、吞吐等其他指标存在工程权衡，成因判定须以仿真输出与现场复核为准，"
                "本报告不附加未经推演验证的归因解释。\n"
            )

        if comp_b:
            grade_value = comp_b.get("overall_effectiveness_grade")
        else:
            grade_value = None
        if grade_value:
            grade_line = f"**综合成效评级**：**{grade_value}**"
        else:
            grade_line = (
                "**综合成效评级**：*不可用* —— 本次未获得有效的 A/B 推演对比数据，"
                "故不对治理成效作任何量化结论。"
            )

        if not has_kpis:
            data_notice = (
                "> ⚠️ **数据完整性提示**：本次推演未返回完整的三方案指标数据，"
                "表格中缺失项以 `—` 标注。报告不对缺失指标作任何推测或补齐。\n"
            )
        else:
            data_notice = ""

        # --- Section 二 intro: describe the run that actually produced these numbers -- #
        # Hard-coding "通过 SUMO 高保真微观物理推演沙盒…执行 A/B 方案平行推演" contradicted
        # the provenance table a few lines above whenever the figures came from calibrated
        # or mesoscopic data (see test_report_verification_header_matches_provenance).
        if execution_mode == "physical_sumo_sandbox":
            rollout_intro = (
                "通过 SUMO 高保真微观物理推演沙盒，针对设定推演窗口期执行 A/B 方案平行推演，\n"
                "五维核心指标对比如下："
            )
        elif execution_mode == "mesoscopic_network":
            rollout_intro = (
                "本组数据由**真实路网中观排队推演引擎**（HCM/Webster 排队模型）产出，"
                "未运行微观车辆仿真；针对设定推演窗口期执行 A/B 方案平行推演，\n"
                "五维核心指标对比如下："
            )
        else:
            rollout_intro = (
                "> ⚠️ **本组数据为标定经验数据，非实测**（未运行任何仿真器）。下表数值来自标定"
                "数据集，仅可用于流程演示，不得作为量化结论对外宣称。\n\n五维核心指标对比如下："
            )

        # --- Recommendation: must follow the numbers, not precede them ---------------- #
        # The previous revision unconditionally printed "【推荐采纳方案】：方案 B" — even when
        # strategy B's delay improvement was negative and its grade was merely "一般".
        delay_pct = (comp_b or {}).get("delay_improvement_pct")
        speed_pct = (comp_b or {}).get("speed_improvement_pct")
        if delay_pct is None:
            recommendation_head = (
                "**【推荐采纳方案】**：*不可用* —— 本次未获得可比较的 A/B 推演结果，"
                "不作方案推荐。"
            )
            recommendation_note = (
                "建议先完成一次有效的方案推演（或开启微观 SUMO 推演）后重新生成简报。"
            )
        elif delay_pct > 0:
            recommendation_head = "**【推荐采纳方案】**：**方案 B：TrafficAgent-DSS 系统级时空协同治理**"
            detail = [f"平均延误较基线降低 **{delay_pct}%**"]
            if speed_pct is not None:
                detail.append(f"断面平均车速变化 **{speed_pct:+.1f}%**")
            if grade_value:
                detail.append(f"综合成效评级 **{grade_value}**")
            recommendation_note = "依据：" + "，".join(detail) + "。"
        else:
            recommendation_head = (
                f"**【推荐采纳方案】**：*不建议全面启用方案 B* —— 本次推演中方案 B "
                f"未体现正收益（延误变化 **{delay_pct}%**，负值为恶化）。"
            )
            recommendation_note = (
                "建议保留方案 A（Webster 单点自适应）或复核输入工况后重跑；"
                "不依据本结果做全面推广。"
            )

        # --- Recommended actions --------------------------------------- #
        cycle_len = strat_b.get("cycle_length")
        green_split = strat_b.get("green_split_arterial")
        green_cross = strat_b.get("green_split_cross")
        offsets = strat_b.get("green_wave_offsets") or []
        raw_reroute = strat_b.get("reroute_ratio")
        reroute_val = float(raw_reroute) if raw_reroute is not None else 0.0
        reroute_pct = int(round(reroute_val * 100))
        vms_msg = strat_b.get("vms_advisory") or "（未生成诱导文案）"
        risk_msg = strat_b.get("risk_warning") or "（未生成风险提示）"
        green_wave_active = bool(strat_b.get("green_wave"))

        # Safe numeric refs for f-string arithmetic (guard against None)
        _cl = cycle_len if cycle_len is not None else 0.0
        _gs = green_split if green_split is not None else 0.0
        _green_pct = round(_gs / max(1.0, _cl) * 100, 1) if _cl > 0 else "—"

        # Pull action plan for section 四
        action_plan_data = strategies.get("action_plan") or {}
        plain_summary = action_plan_data.get("plain_summary", "—")
        steps = action_plan_data.get("steps", [])

        gw_section = (
            f"启用干线协同周期 **{self._fmt(cycle_len, ' 秒')}**，主路关键绿灯 "
            f"**{self._fmt(green_split, ' 秒')}**。\n"
            f"   - 双向绿波相位差：" + (
                "、".join(f"`J{i+1}: {o}s`" for i, o in enumerate(offsets))
                if offsets else "未计算"
            ) + "。"
            if green_wave_active
            else f"干线绿波未启用，维持单点 Webster 自适应配时（周期 {self._fmt(cycle_len, ' 秒')}）。"
        )

        reroute_section = (
            f"即刻在上游可变信息板发布指引：*“{vms_msg}”*\n"
            f"   - 目标动态分流比例：**{reroute_pct}%**。"
            if reroute_pct > 0
            else "当前工况未触发动态分流诱导。"
        )

        # --- Control evidence block ------------------------------------ #
        if evidence:
            seed_ev = f"\n- 随机种子参数：`{evidence.get('seed')}`" if evidence.get("seed") is not None else ""
            evidence_block = (
                f"- 仿真中实际下发：信号控制指令 `{evidence.get('signal_commands', 0)}` 条、"
                f"重路由指令 `{evidence.get('reroute_commands', 0)}` 条\n"
                f"- 绿波协调：{'已生效' if evidence.get('green_wave_applied') else '未启用'}；"
                f"Webster 自适应：{'已生效' if evidence.get('webster_applied') else '未启用'}；"
                f"分流比例：{evidence.get('reroute_ratio_applied', 0)}\n"
                f"- 全局控制周期：`{evidence.get('cycle_length', '—')}` 秒"
                f"{seed_ev}"
            )
        else:
            evidence_block = "- 本次未采集到控制指令下发记录。"

        raw_causes = diagnosis.get("root_causes")
        if isinstance(raw_causes, list):
            causes_list = [str(rc) for rc in raw_causes if rc is not None]
        elif raw_causes is not None:
            causes_list = [str(raw_causes)]
        else:
            causes_list = []
        causes_text = chr(10).join(f"  - {rc}" for rc in causes_list) if causes_list else "  - （未获得归因结果）"

        raw_cot = diagnosis.get("cot_reasoning")
        if isinstance(raw_cot, list):
            cot_list = [str(step) for step in raw_cot if step is not None]
        elif raw_cot is not None:
            cot_list = [str(raw_cot)]
        else:
            cot_list = []
        cot_text = chr(10).join(f"  > {step}" for step in cot_list) if cot_list else "  > （未获得推理过程）"

        return f"""# 城市交通拥堵治理辅助决策建议简报 (Decision Briefing)

**报告编号**：`DSS-2026-EXP-{int(os.getpid())}`  
**决策系统**：TrafficAgent-DSS (基于交通仿真智能体的城市交通拥堵治理决策支持系统)  
**评估状态**：{verification_status}

---

### 〇、 数据来源与可信度声明 (Provenance)

| 项目 | 本次取值 |
| :--- | :--- |
| **仿真数据来源** | {mode_label} |
| **归因推理引擎** | {reasoning_label} |
| **方案叙事引擎** | {narrative_label} |
| **推演随机种子** | `{seed_label}` |

{data_notice}
> 说明：本报告中**所有性能指标均由交通工程工具计算或微观仿真产出**；大语言模型仅参与
> 归因解释与方案叙事，不参与任何数值生成。若上表标注为"降级估算/未启用"，则该组数据
> 不得作为量化结论对外宣称。

---

### 一、 态势感知与拥堵归因诊断 (Diagnosis)
- **拥堵瓶颈断面**：走廊核心路段 `{diagnosis.get('bottleneck_location', '—')}`
- **警情等级**：**{diagnosis.get('severity_level', '—')}**（死锁风险度：{diagnosis.get('spillback_risk', '—')}）
- **主要诱因归结**：
{causes_text}
- **智能体思维链 (CoT 推理过程)**：
{cot_text}

---

### 二、 多方案数字孪生推演与成效比对 (What-If Analysis)

{rollout_intro}

| 评估维度 / 指标 | 现状基线 (Do-Nothing) | 方案A (Webster自适应) | 方案B (Agent协同治理) | 方案B改善幅度 |
| :--- | :--- | :--- | :--- | :--- |
{table_rows}

{grade_line}
{speed_tradeoff_note}
**控制指令下发核验 (Control Evidence)**：
{evidence_block}

---

### 三、 决策推荐与协同处置指令建议 (Action Recommendation)

{recommendation_head}

{recommendation_note}

以下为按当前策略参数生成的可执行指令速览（是否采纳以复核结论为准）：

| 指令 | 参数 |
| :--- | :--- |
| 信号周期 | **{self._fmt(cycle_len, ' 秒')}**（Webster 过饱和修正） |
| 主路绿灯 | **{self._fmt(green_split, ' 秒')}**（绿信比 {_green_pct}%） |
| 支路绿灯 | **{self._fmt(green_cross, ' 秒')}** |
| 诱导分流 | **{reroute_pct}%**（VMS: *"{vms_msg}"*） |
| 绿波协调 | {'已启用 — 相位差: ' + '、'.join(f'J{i+1}: {o}s' for i, o in enumerate(offsets)) if offsets else '未启用'} |

> ⚠️ 完整分步行动指令、执行时序、验证指标与兜底预案见下方 **### 四、 行动指令清单 (Action Playbook)**。

---

### 四、 行动指令清单 (Action Playbook)

{plain_summary}

| 序号 | 阶段 | 行动标题 | 关键参数 | 执行方 | 验证方式 |
| :--- | :--- | :--- | :--- | :--- | :--- |
""" + chr(10).join(
            f"| **{s['n']}** | {s['phase']} | {s['title']} | {s['detail']} | {s['owner']} | {s['verify']} |"
            for s in steps
        ) + (
            (
                "\n| **Fallback** | 综合 | 分流状态 | "
                f"{'目标动态分流比例：**' + str(reroute_pct) + '%**。' if reroute_pct > 0 else '当前工况未触发动态分流诱导。'} "
                f"（VMS: *\"{vms_msg}\"*） | 信息发布员 | 每 5 分钟查看旁路占有率数据。 |"
            )
            if not steps
            else ""
        ) + """\n> 执行原则：按序号顺序依次执行；第 4-5 项持续监测至拥堵解除；第 6-7 项为恢复与兜底预案。"""
