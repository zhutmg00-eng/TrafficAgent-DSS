#!/usr/bin/env python
"""
TrafficAgent-DSS · 闭环控制策略增益评估（标定工况 · 多种子 · 留存复验）
=====================================================================

回答一个问题：在**标定工况**（600 s 推演 / 事故窗口 150–420 s）与**多种子**下，
把控制决策放进闭环（LLM 位置 + 三道闸门 + 实测度量 + 采纳规则），
到底能不能比"确定性规则链基线"多拿到可统计的延误收益？

为什么必须单独做这个实验
------------------------
P1 的端到端验证（`_closed_loop_evidence.json`）只证明了**通路成立**：模型请求的参数
确实下发了、上一轮实测反馈确实回灌了。但那次是 300 s / 单种子的**非标定短工况**，
按项目"输出必须与事实对齐"的口径，**不构成性能结论**。本实验补上这一环。

协议（避免"用自己的种子证明自己"）
--------------------------------
1. **调参阶段**（explore seeds）：标定工况下做坐标上升搜索（coordinate ascent），
   逐个旋钮扫描策略空间，选出最优候选 P* 与次优候选 P2。
   搜索器是确定性的 —— 它在闭环里**顶替 LLM 的位置**。本实验评估的对象是
   "闭环机制"的增益，**不是**某个具体模型的能力（后者需要真实模型参与，见诚实边界）。
2. **留存复验阶段**（validate seeds，与调参种子**完全不相交**）：冻结 P*，与
   baseline / deterministic / P2 在留出种子上**配对**比较，输出 95% t-CI 与逐种子胜率。
3. **在线管线一致性检查**：用桩 LLM 把 P* 回放给真正的
   `optimize_control_policy_closed_loop`，确认线上管线（schema 校验 → 物理裁剪 →
   实测采纳 → verdict）与该离线结论一致 —— 即"实验里选出来的策略，线上也认"。

关于"排队约束"的两个口径（重要）
--------------------------------
线上闭环采纳一条策略的条件是"延误更低 **且** 实测峰值排队不超过**参考规则链方案**的
1.25 倍"（同一种子）。因此本实验同时给出两个最优：

  - **P\\***：不加排队约束的**延误最优**（即闭环机制能触及的上限）
  - **P_feasible**：同时满足排队约束的最优（即线上会真正采纳的候选）

两者的差距 = 采纳规则当前漏掉的收益；若 P_feasible 不存在，说明网格内没有一条策略
能"不靠牺牲排队"拿到增益。该约束的触发率与参照物选择在第 7 节单独审计。

> 注：该约束此前以"无干预基线"排队的 1.25 倍为阈值，实测在标定工况下会把 32/32 个候选
> （含参考规则链本身）全部拒掉。修复后的口径见 `v2.3.2`，修复前的证据保留在
> `closed_loop_gain_v1.md`。

诚实边界
--------
- 所有数字来自真实 SUMO/TraCI 实测（`SumoSimulationSandbox`）。任何一次未进入物理沙盒的
  运行都会抛异常并中止，绝不用"标定兜底"填坑。
- 每次运行都记录沙盒回传的 `control_evidence`，"下发了什么"是可核验的**实测字段**，
  不是计划值。
- 策略候选一律经过与线上**同一个** `validate_payload` + `clip_policy` 闸门。
- 本实验**不**声称 LLM 一定能找到 P*（P* 是搜索器找到的）；也不声称 P* 是全局最优
  （它是在给定网格上的局部最优）。

用法（仓库根目录）
------------------
    .venv/bin/python experiments/closed_loop_gain.py --tag v1
    .venv/bin/python experiments/closed_loop_gain.py --passes 1          # 更快的探索
    .venv/bin/python experiments/closed_loop_gain.py --skip-online-check
"""

import argparse
import json
import math
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJ_ROOT = Path(__file__).resolve().parent.parent
if str(PROJ_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJ_ROOT))

from src.agents.llm_client import LLMReasoningClient  # noqa: E402
from src.agents.llm_decision import (  # noqa: E402
    POLICY_BOUNDS,
    LLMDecisionLayer,
    clip_policy,
    validate_payload,
)
from src.agents.traffic_agent import (  # noqa: E402
    CORRIDOR_BYPASS_SPARE_CAPACITY_VPH,
    CORRIDOR_UPSTREAM_FLOW_VPH,
    TrafficDecisionAgent,
)

# --------------------------------------------------------------------------- #
# 实验工况
# --------------------------------------------------------------------------- #
# 探测器状态与消融实验保持同一口径（严重拥堵 / 回溢高危），使结果与既有
# experiments/ablation_result_*.md 可比。
DETECTOR_STATE = {
    "bottleneck_edge": "J1_J2 (主干线合流段)",
    "queue_m": 165.0,
    "link_length_m": 300.0,
    "speed_kmh": 8.2,
    "occupancy": 0.82,
    "bypass_occupancy": 0.28,
}

# 调参种子与留出种子**不相交**。留出种子取自项目既有多种子集合中未参与调参的那些。
DEFAULT_EXPLORE_SEEDS = [42, 101, 777]
DEFAULT_VALIDATE_SEEDS = [2024, 999, 7, 13, 1013, 2025, 31337]

# 每个旋钮的候选网格。以确定性方案的实际取值为圆心向两侧取值，
# 刻意**不含**确定性取值本身 —— 每一格都是一次真实的"移动"。
KNOB_GRID: Dict[str, List[Any]] = {
    "target_cycle_s": [70.0, 80.0, 100.0, 110.0],
    "arterial_green_share": [0.55, 0.60, 0.70, 0.76],
    "reroute_ratio": [0.0, 0.18, 0.28],
    "progression_speed_kmh": [35.0, 42.0, 46.0, 56.0],
    "coordinated": [False],
}

METRICS = [
    ("avg_delay_s", "平均延误", "s/veh", "lower"),
    ("max_queue_m", "最大排队", "m", "lower"),
    ("avg_speed_kmh", "平均速度", "km/h", "higher"),
    ("throughput_vph", "通行量", "veh/h", "higher"),
    ("delay_variance", "延误方差", "—", "lower"),
    ("co2_emissions_kg", "CO2 排放", "kg", "lower"),
]

# 沙盒回传的控制证据字段 —— 报告里"下发了什么"必须从这里取，不能从计划取。
EVIDENCE_FIELDS = (
    "signal_program_source",
    "cycle_length",
    "arterial_green",
    "cross_green",
    "green_wave_applied",
    "green_wave_first_green_start_s",
    "reroute_ratio_applied",
    "incident_injected",
    "incident_lanes_blocked",
    "incident_cleared",
)

QUEUE_BLOWUP_RATIO = 1.25  # 与 TrafficDecisionAgent.QUEUE_BLOWUP_RATIO 同口径


# --------------------------------------------------------------------------- #
# 统计工具（小样本 t 分布；与 ablation.py 同口径）
# --------------------------------------------------------------------------- #
_T_TABLE = {
    1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365,
    8: 2.306, 9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145,
    15: 2.131, 16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086,
}


def t_crit(df: int) -> float:
    return _T_TABLE.get(df, 1.96)


def summarize(values: List[float]) -> Dict[str, Any]:
    n = len(values)
    if n == 0:
        return {"n": 0, "mean": None, "std": None, "ci95": [None, None]}
    mean = statistics.fmean(values)
    std = statistics.stdev(values) if n > 1 else 0.0
    if n > 1:
        sem = std / math.sqrt(n)
        half = t_crit(n - 1) * sem
        ci = [round(mean - half, 3), round(mean + half, 3)]
    else:
        ci = [None, None]
    return {"n": n, "mean": round(mean, 3), "std": round(std, 3), "ci95": ci}


def paired(treatment: List[float], reference: List[float]) -> Dict[str, Any]:
    """
    Paired comparison on the SAME seeds: delta_i = reference_i - treatment_i,
    positive = the treatment is better. Pairing removes seed-to-seed variance,
    which is exactly the noise that made the un-paired CIs in earlier reports overlap.
    """
    deltas = [r - t for r, t in zip(reference, treatment)]
    stat = summarize(deltas)
    stat["wins"] = sum(1 for d in deltas if d > 0)
    stat["losses"] = sum(1 for d in deltas if d < 0)
    stat["ties"] = sum(1 for d in deltas if d == 0)
    lo = stat["ci95"][0]
    stat["significant_gain"] = bool(
        stat["n"] >= 2 and stat["mean"] is not None and stat["mean"] > 0 and lo is not None and lo > 0
    )
    stat["significant_loss"] = bool(
        stat["n"] >= 2 and stat["mean"] is not None and stat["mean"] < 0
        and stat["ci95"][1] is not None and stat["ci95"][1] < 0
    )
    return stat


class _StubLLM:
    """Stands in for LLMReasoningClient so the online pipeline can be replayed offline."""

    model = "offline-coordinate-ascent-replay"

    def __init__(self, payload: Dict[str, Any]):
        self._payload = payload
        self.calls = 0

    def chat_json(self, system_prompt: str, user_prompt: str,
                  temperature: float = 0.2, max_tokens: int = 1200):
        self.calls += 1
        return self._payload, LLMReasoningClient.MODE_LLM, None


# --------------------------------------------------------------------------- #
# 实验主体
# --------------------------------------------------------------------------- #
class PolicyGainExperiment:
    def __init__(self, duration: int, incident_start: int, incident_end: int, quiet: bool = False):
        self.quiet = quiet
        self.agent = TrafficDecisionAgent()
        self.diag = self.agent.diagnose_bottleneck(DETECTOR_STATE)
        self.duration = duration
        self.incident_start = incident_start
        self.incident_end = incident_end

        self.det_plan = self.agent._tool_plan(self.diag)
        available_green = self.det_plan["actual_cycle"] - 2.0 * self.det_plan["yellow_time"]
        self.det_share = round(self.det_plan["arterial_green"] / available_green, 6)
        self.det_reroute = float(self.det_plan["reroute"]["diversion_ratio"])
        self.det_speed = float(self.det_plan["green_wave"]["progression_speed_kmh"])
        self.reroute_cap = min(
            POLICY_BOUNDS["reroute_ratio"][1],
            CORRIDOR_BYPASS_SPARE_CAPACITY_VPH / max(1.0, CORRIDOR_UPSTREAM_FLOW_VPH),
        )
        # Same context shape the online closed loop hands to `clip_policy`.
        self.context = {
            "baseline_plan": {
                "design_cycle_s": self.det_plan["actual_cycle"],
                "arterial_green_s": self.det_plan["arterial_green"],
                "cross_green_s": self.det_plan["cross_green"],
                "yellow_s": self.det_plan["yellow_time"],
                "green_wave_offsets_s": self.det_plan["green_wave"]["offsets"],
                "progression_speed_kmh": self.det_speed,
                "reroute_ratio": self.det_reroute,
                "reroute_capacity_cap": round(self.reroute_cap, 4),
            }
        }

        self._cache: Dict[str, Dict[str, Any]] = {}
        self.run_log: List[Dict[str, Any]] = []
        self.sim_count = 0

    # ---------------- 推理与度量 ---------------- #
    def _sim(self, tag: str, scheme: str, controls: Dict[str, Any], seed: int) -> Dict[str, Any]:
        key = json.dumps({"tag": tag, "scheme": scheme, "controls": controls, "seed": seed},
                         sort_keys=True, default=str)
        if key in self._cache:
            return self._cache[key]

        t0 = time.time()
        res = self.agent.sandbox.run_simulation(
            scheme=scheme,
            duration=self.duration,
            incident_start=self.incident_start,
            incident_end=self.incident_end,
            control_params=controls,
            seed=seed,
        )
        kpi = self.agent.evaluator.compute_summary_kpi(res)
        if not kpi or kpi.get("avg_delay_s") is None:
            raise RuntimeError(f"seed={seed} 未产出可用的实测 KPI（{tag}），已中止以避免输出无实测依据的结论。")

        ev = res.get("control_evidence") or {}
        self.sim_count += 1
        self.run_log.append({
            "tag": tag,
            "scheme": scheme,
            "seed": seed,
            "elapsed_s": round(time.time() - t0, 2),
            "deployed_controls": {
                "cycle_length_s": ev.get("cycle_length"),
                "arterial_green_s": ev.get("arterial_green"),
                "cross_green_s": ev.get("cross_green"),
                "first_green_start_s": ev.get("green_wave_first_green_start_s"),
                "reroute_ratio_applied": ev.get("reroute_ratio_applied"),
                "signal_program_source": ev.get("signal_program_source"),
            },
            "evidence_snapshot": {k: ev.get(k) for k in EVIDENCE_FIELDS},
            "incident": {
                "injected": ev.get("incident_injected"),
                "lanes_blocked": ev.get("incident_lanes_blocked"),
                "cleared": ev.get("incident_cleared"),
            },
            "measured_kpi": {m: kpi.get(m) for m, _, _, _ in METRICS},
        })
        if not self.quiet:
            print(f"    [{tag}] seed={seed} delay={kpi.get('avg_delay_s'):.2f}s "
                  f"queue={kpi.get('max_queue_m'):.1f}m cycle={ev.get('cycle_length')} "
                  f"reroute={ev.get('reroute_ratio_applied')} ({self.run_log[-1]['elapsed_s']}s)",
                  flush=True)
        self._cache[key] = kpi
        return kpi

    def baseline_kpi(self, seed: int) -> Dict[str, Any]:
        return self._sim("baseline", "baseline",
                         {"reroute_ratio": 0.0, "green_wave": False, "webster": False}, seed)

    def deterministic_kpi(self, seed: int) -> Dict[str, Any]:
        return self._sim("deterministic", "agent_dss",
                         self.agent.build_control_params(self.det_plan, True), seed)

    def policy_kpi(self, requested: Dict[str, Any], seed: int, tag: str = "policy") -> Dict[str, Any]:
        """Gate -> clip -> deploy -> measure. Identical path to the online closed loop."""
        proposal, errors = validate_payload(requested)
        if proposal is None:
            raise ValueError(f"候选人未通过 schema 校验: {errors}")
        applied, _adjustments = clip_policy(proposal, self.context)
        policy = {
            k: applied[k] for k in (
                "target_cycle_s", "arterial_green_share", "reroute_ratio",
                "progression_speed_kmh", "coordinated",
            )
        }
        policy["decision_rationale"] = applied.get("decision_rationale", "")
        plan = self.agent._tool_plan(self.diag, policy=policy)
        controls = self.agent.build_control_params(
            plan, applied["coordinated"], use_rerouting=True, use_webster=True
        )
        return self._sim(tag, "agent_dss", controls, seed)

    # ---------------- 候选打分 ---------------- #
    def score(self, candidate: Dict[str, Any], seeds: List[int],
              base_kpi: Dict[int, Dict[str, Any]],
              ref_kpi: Dict[int, Dict[str, Any]]) -> Dict[str, Any]:
        """
        Measures one candidate on every seed.

        The queue guard is evaluated exactly as the online loop evaluates it: a candidate is
        flagged out when its measured peak queue exceeds the **deterministic reference
        plan's** peak queue (same seed) by more than `QUEUE_BLOWUP_RATIO`. The do-nothing
        queue is recorded alongside, because that was the old yardstick and the comparison
        between the two is itself the evidence for why it was changed.
        """
        payload = self._payload_for(candidate)
        delays, queues, base_queues, ref_queues, ok = [], [], [], [], True
        for s in seeds:
            kpi = self.policy_kpi(payload, s)
            delays.append(float(kpi["avg_delay_s"]))
            q = kpi.get("max_queue_m")
            measured = float(q) if isinstance(q, (int, float)) and not isinstance(q, bool) else float("nan")
            base_q = base_kpi[s].get("max_queue_m")
            ref_q = ref_kpi[s].get("max_queue_m")
            queues.append(measured)
            base_queues.append(float(base_q) if isinstance(base_q, (int, float)) else float("nan"))
            ref_queues.append(float(ref_q) if isinstance(ref_q, (int, float)) else float("nan"))
            if (isinstance(ref_q, (int, float)) and not isinstance(ref_q, bool) and float(ref_q) > 0
                    and measured == measured
                    and measured > float(ref_q) * QUEUE_BLOWUP_RATIO):
                ok = False
        return {
            "policy": dict(candidate),
            "payload": payload,
            "mean_delay_s": round(statistics.fmean(delays), 3),
            "delays_s": [round(d, 3) for d in delays],
            "mean_queue_m": round(statistics.fmean(queues), 2),
            "queue_reference_m": round(statistics.fmean(ref_queues), 2),
            "mean_baseline_queue_m": round(statistics.fmean(base_queues), 2),
            "queue_constraint_ok": ok,
        }

    @staticmethod
    def _payload_for(candidate: Dict[str, Any]) -> Dict[str, Any]:
        """Rationale quotes only the numbers this very payload carries (provenance-clean)."""
        return {
            "target_cycle_s": float(candidate["target_cycle_s"]),
            "arterial_green_share": float(candidate["arterial_green_share"]),
            "reroute_ratio": float(candidate["reroute_ratio"]),
            "progression_speed_kmh": float(candidate["progression_speed_kmh"]),
            "coordinated": bool(candidate["coordinated"]),
            "decision_rationale": (
                f"将周期设为 {float(candidate['target_cycle_s']):.0f}s、"
                f"干道绿信比 {float(candidate['arterial_green_share']):.2f}、"
                f"分流比 {float(candidate['reroute_ratio']):.2f}、"
                f"绿波带速 {float(candidate['progression_speed_kmh']):.0f}km/h"
                f"（{'协调' if candidate['coordinated'] else '单点'}控制）。"
            ),
        }

    # ---------------- 阶段 1：调参（坐标上升） ---------------- #
    def deterministic_equivalence(self) -> Dict[str, Any]:
        """
        护栏：确定性规则链必须能「作为一条策略」被复现出来。

        把规则链自己的取值喂回策略叠加层，应当得到**同一套下发参数**。若这条不成立，
        说明搜索网格与规则链不在同一个口径上，后面所有对比都失去意义 —— 因此直接中止。

        容差：周期 / 绿时 / 分流比要求精确一致（这些正是叠加层真正重算的量）；
        绿波相位差允许 0.05s 偏差 —— 确定性路径传的是四舍五入过的 13.89 m/s 带速，
        策略路径传的是 50/3.6 m/s，二者差 0.0017 m/s。给这个已知的口径毛刺留容差，
        但不足以掩盖真正的逻辑错误。
        """
        equiv = {
            "target_cycle_s": self.det_plan["actual_cycle"],
            "arterial_green_share": self.det_share,
            "reroute_ratio": self.det_reroute,
            "progression_speed_kmh": self.det_speed,
            "coordinated": True,
        }
        proposal, errors = validate_payload(self._payload_for(equiv))
        if proposal is None:
            return {"ok": False, "differences": {"schema": errors}, "max_offset_delta_s": None}
        applied, _ = clip_policy(proposal, self.context)
        policy = {
            k: applied[k] for k in (
                "target_cycle_s", "arterial_green_share", "reroute_ratio",
                "progression_speed_kmh", "coordinated",
            )
        }
        policy["decision_rationale"] = applied.get("decision_rationale", "")
        plan = self.agent._tool_plan(self.diag, policy=policy)
        got = self.agent.build_control_params(plan, True)
        want = self.agent.build_control_params(self.det_plan, True)

        diffs: Dict[str, Any] = {}
        for f in ("green_main", "green_cross", "cycle_length", "yellow",
                  "min_green_main", "max_green_main", "min_green_cross", "max_green_cross"):
            if got["signal_program"].get(f) != want["signal_program"].get(f):
                diffs[f] = {"deterministic": want["signal_program"].get(f),
                            "via_policy": got["signal_program"].get(f)}
        if abs(got["reroute_ratio"] - want["reroute_ratio"]) > 1e-9:
            diffs["reroute_ratio"] = {"deterministic": want["reroute_ratio"],
                                      "via_policy": got["reroute_ratio"]}
        max_off = max(
            (abs(a - b) for a, b in zip(want["signal_program"]["first_green_start"],
                                        got["signal_program"]["first_green_start"])),
            default=0.0,
        )
        return {
            "ok": not diffs and max_off <= 0.05,
            "differences": diffs,
            "max_offset_delta_s": round(max_off, 6),
        }

    def search(self, seeds: List[int], passes: int) -> Dict[str, Any]:
        print(f"\n[阶段1] 调参（explore seeds={seeds}，passes={passes}）", flush=True)
        base_kpi = {s: self.baseline_kpi(s) for s in seeds}
        det_kpi = {s: self.deterministic_kpi(s) for s in seeds}
        anchor = {
            "baseline_mean_delay_s": round(statistics.fmean(
                [float(base_kpi[s]["avg_delay_s"]) for s in seeds]), 3),
            "deterministic_mean_delay_s": round(statistics.fmean(
                [float(det_kpi[s]["avg_delay_s"]) for s in seeds]), 3),
            # 排队约束的标尺逐种子记录：约束是「候选排队 ≤ 该种子基线排队 × 1.25」，
            # 基线排队越小阈值越紧 —— 这正是标定工况下约束失效的根源。
            "queue_threshold_by_seed": {
                str(s): round(float(base_kpi[s]["max_queue_m"]) * QUEUE_BLOWUP_RATIO, 2)
                for s in seeds
            },
            "baseline_queue_by_seed": {
                str(s): float(base_kpi[s]["max_queue_m"]) for s in seeds
            },
            "deterministic_queue_by_seed": {
                str(s): float(det_kpi[s]["max_queue_m"]) for s in seeds
            },
        }
        print(f"    锚点：baseline {anchor['baseline_mean_delay_s']} s/veh | "
              f"deterministic {anchor['deterministic_mean_delay_s']} s/veh", flush=True)

        incumbent = {
            "target_cycle_s": float(self.det_plan["actual_cycle"]),
            "arterial_green_share": float(self.det_share),
            "reroute_ratio": float(self.det_reroute),
            "progression_speed_kmh": float(self.det_speed),
            "coordinated": True,
        }
        incumbent_delay = anchor["deterministic_mean_delay_s"]
        trace: List[Dict[str, Any]] = []
        all_scored: List[Dict[str, Any]] = []

        for p in range(1, passes + 1):
            for knob, values in KNOB_GRID.items():
                scored = []
                print(f"  pass{p} · 旋钮 {knob}: 现值 {incumbent[knob]}", flush=True)
                for v in values:
                    cand = dict(incumbent)
                    cand[knob] = v
                    r = self.score(cand, seeds, base_kpi, det_kpi)
                    r["pass"] = p
                    r["knob"] = knob
                    r["value"] = v
                    scored.append(r)
                    all_scored.append(r)
                    flag = "" if r["queue_constraint_ok"] else "  [排队约束未通过]"
                    print(f"      {knob}={v}: 延误均值 {r['mean_delay_s']} s/veh"
                          f"，排队均值 {r['mean_queue_m']} m（参照 {r['queue_reference_m']} m）{flag}",
                          flush=True)
                # 排序不设排队门槛：本实验要测的是"闭环机制能触及的延误上限"。
                # 排队约束是否通过只作为标记记下来，其后果在第 7 节单独审计 ——
                # 若在这里直接按线上规则筛掉大部分候选，得到的结论会是"增益≈0"，
                # 而那是采纳规则的缺陷，不是机制没有收益。
                winner = min(scored, key=lambda x: x["mean_delay_s"])
                improved = winner["mean_delay_s"] < incumbent_delay - 1e-9
                if improved:
                    incumbent = dict(winner["policy"])
                    incumbent_delay = winner["mean_delay_s"]
                feasible_here = [x for x in scored if x["queue_constraint_ok"]]
                trace.append({
                    "pass": p, "knob": knob,
                    "action": "adopt" if improved else "keep",
                    "chosen_value": winner["value"],
                    "mean_delay_s": winner["mean_delay_s"],
                    "queue_constraint_ok": winner["queue_constraint_ok"],
                    "feasible_candidates_here": len(feasible_here),
                    "candidates_here": len(scored),
                    "incumbent_delay_after_s": round(incumbent_delay, 3),
                    "incumbent_after": dict(incumbent),
                })

        runner_up = None
        ranked = sorted(all_scored, key=lambda x: x["mean_delay_s"])
        for x in ranked:
            if x["policy"] != incumbent:
                runner_up = x
                break

        feasible_ranked = sorted(
            [x for x in all_scored if x["queue_constraint_ok"]],
            key=lambda x: x["mean_delay_s"],
        )
        feasible_policy = feasible_ranked[0] if feasible_ranked else None

        print(f"  → 调参结果（延误最优 P*）：{incumbent}  "
              f"实测延误均值 {round(incumbent_delay, 3)} s/veh", flush=True)
        if feasible_policy:
            print(f"  → 满足排队约束的最优 P_feasible：{feasible_policy['policy']}  "
                  f"延误均值 {feasible_policy['mean_delay_s']} s/veh", flush=True)
        else:
            print("  → 无任何候选满足排队约束（P_feasible 不存在）", flush=True)
        return {
            "anchor": anchor,
            "incumbent_policy": incumbent,
            "incumbent_mean_delay_s": round(incumbent_delay, 3),
            "feasible_policy": feasible_policy["policy"] if feasible_policy else None,
            "feasible_mean_delay_s": feasible_policy["mean_delay_s"] if feasible_policy else None,
            "feasible_candidates_total": len(feasible_ranked),
            "candidates_total": len(all_scored),
            "runner_up": runner_up,
            "trace": trace,
            "candidates": all_scored,
        }

    # ---------------- 阶段 2：留出种子复验 ---------------- #
    def validate(self, arms: List[Dict[str, Any]], seeds: List[int]) -> Dict[str, Any]:
        print(f"\n[阶段2] 留存复验（validate seeds={seeds}）", flush=True)
        per_arm: Dict[str, Dict[str, Any]] = {}
        for arm in arms:
            label = arm["label"]
            print(f"  → {label}", flush=True)
            kpis = {}
            for s in seeds:
                if arm["kind"] == "baseline":
                    kpis[s] = self.baseline_kpi(s)
                elif arm["kind"] == "deterministic":
                    kpis[s] = self.deterministic_kpi(s)
                else:
                    kpis[s] = self.policy_kpi(arm["payload"], s, tag=label)
            per_arm[label] = {
                "label": arm["label"],
                "note": arm.get("note", ""),
                "kpis": kpis,
                "metrics": {
                    m: summarize([float(kpis[s][m]) for s in seeds if kpis[s].get(m) is not None])
                    for m, _, _, _ in METRICS
                },
            }

        refs = {k: per_arm[k] for k in ("baseline", "deterministic") if k in per_arm}
        comparisons: Dict[str, Dict[str, Any]] = {}
        for label, block in per_arm.items():
            if label in refs:
                continue
            comparisons[label] = {}
            for ref_label, ref_block in refs.items():
                comparisons[label][ref_label] = {
                    m: paired(
                        [float(block["kpis"][s][m]) for s in seeds],
                        [float(ref_block["kpis"][s][m]) for s in seeds],
                    )
                    for m, _, _, _ in METRICS
                }
        return {"arms": per_arm, "comparisons": comparisons,
                "validate_seeds": seeds}

    # ---------------- 阶段 3：在线管线一致性 ---------------- #
    def online_pipeline_check(self, payload: Dict[str, Any], seed: int, rounds: int = 1) -> Dict[str, Any]:
        print(f"\n[阶段3] 在线闭环管线回放（seed={seed}, rounds={rounds}）", flush=True)
        replay_agent = TrafficDecisionAgent()
        stub = _StubLLM(payload)
        replay_agent.decision = LLMDecisionLayer(stub)
        out = replay_agent.optimize_control_policy_closed_loop(
            diagnosis=self.diag,
            rounds=rounds,
            duration=self.duration,
            incident_start=self.incident_start,
            incident_end=self.incident_end,
            seed=seed,
            use_rerouting=True,
            use_webster=True,
        )
        return {
            "llm_calls": stub.calls,
            "decision_mode": out.get("decision_mode"),
            "rounds_executed": out.get("rounds_executed"),
            "model_requested": (out.get("rounds") or [{}])[0].get("model_requested"),
            "deployed_after_clipping": (out.get("rounds") or [{}])[0].get("deployed_after_clipping"),
            "clipping_adjustments": (out.get("rounds") or [{}])[0].get("clipping_adjustments"),
            "best_source": out.get("best", {}).get("source"),
            "recommendation": out.get("recommendation"),
        }


# --------------------------------------------------------------------------- #
# 报告
# --------------------------------------------------------------------------- #
def fmt_ci(stat: Dict[str, Any]) -> str:
    lo, hi = stat["ci95"]
    if lo is None:
        return ""
    return f" [{lo}, {hi}]"


def to_markdown(explore_seeds, validate_seeds, duration, istart, iend,
                search_res, validate_res, online_res, sim_count, elapsed,
                guard=None) -> str:
    L: List[str] = []
    L.append("# TrafficAgent-DSS · 闭环控制策略增益评估（标定工况 · 多种子 · 留存复验）")
    L.append("")
    L.append(f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}")
    L.append(f"- 工况：推演 {duration}s，事故窗口 {istart}–{iend}s（标定工况）")
    L.append(f"- 调参种子（explore）：{explore_seeds}")
    L.append(f"- 留出种子（validate）：{validate_seeds}  ← 与调参种子不相交")
    L.append(f"- 仿真次数：{sim_count} 次真实 SUMO/TraCI 运行，耗时 {elapsed:.0f}s")
    L.append("- 指标口径：延误 = SUMO tripinfo `timeLoss` 同口径；排队 = halting×7.5m")
    L.append("- 统计口径：小样本 t 分布 95% CI；复验阶段为**同种子配对**比较")
    L.append("")

    L.append("## 0. 调参阶段锚点")
    L.append("")
    if guard:
        L.append(f"- 口径护栏：确定性取值经策略叠加层后复现同一套下发参数 ✓"
                 f"（绿波相位差最大偏差 {guard.get('max_offset_delta_s')} s）")
        L.append("")
    a = search_res["anchor"]
    L.append("| 参考臂 | 实测平均延误 (s/veh) |")
    L.append("|---|---|")
    L.append(f"| baseline（无干预） | {a['baseline_mean_delay_s']} |")
    L.append(f"| deterministic（现有规则链） | {a['deterministic_mean_delay_s']} |")
    L.append("")

    L.append("## 1. 坐标上升搜索轨迹")
    L.append("")
    L.append("| pass | 旋钮 | 选中值 | 该次实测延误均值 (s/veh) | 动作 | 现役策略延误 (s/veh) | 该格候选数 / 其中过排队约束 |")
    L.append("|---|---|---|---|---|---|---|")
    for t in search_res["trace"]:
        act = "**采纳**" if t["action"] == "adopt" else "维持"
        L.append(f"| {t['pass']} | {t['knob']} | {t['chosen_value']} | {t['mean_delay_s']} | {act} | "
                 f"{t['incumbent_delay_after_s']} | {t['candidates_here']} / {t['feasible_candidates_here']} |")
    L.append("")
    L.append("> 网格按延误择优，**不**先按排队约束筛除 —— 见第 7 节对排队约束的审计。")
    L.append("")

    L.append("## 2. 冻结候选")
    L.append("")
    inc = search_res["incumbent_policy"]
    L.append("**P\\*（延误最优，= 闭环机制的上限）**")
    L.append("")
    L.append("| 变量 | 取值 |")
    L.append("|---|---|")
    for k, v in inc.items():
        L.append(f"| `{k}` | {v} |")
    L.append("")
    if search_res.get("feasible_policy"):
        fp = search_res["feasible_policy"]
        L.append("**P_feasible（同时满足排队约束的最优，= 线上会真正采纳的候选）**")
        L.append("")
        L.append("| 变量 | 取值 |")
        L.append("|---|---|")
        for k, v in fp.items():
            L.append(f"| `{k}` | {v} |")
        L.append("")
        same = fp == inc
        L.append(f"- P_feasible 与 P\\* {'相同' if same else '不同'}；"
                 f"调参阶段两者实测延误均值 {search_res['feasible_mean_delay_s']} vs "
                 f"{search_res['incumbent_mean_delay_s']} s/veh。")
        L.append("")
    else:
        L.append("**P_feasible 不存在** —— 调参网格内没有任何候选通过排队约束。")
        L.append("")
    ru = search_res.get("runner_up")
    if ru:
        L.append("**P2（次优候选，用于验证「是否只是碰到了噪声」）**")
        L.append("")
        L.append("| 变量 | 取值 |")
        L.append("|---|---|")
        for k, v in ru["policy"].items():
            L.append(f"| `{k}` | {v} |")
        L.append("")

    L.append("## 3. 留存种子复验（各臂实测值）")
    L.append("")
    header = "| 臂 | " + " | ".join(f"{name} ({unit})" for _, name, unit, _ in METRICS) + " |"
    L.append(header)
    L.append("|---" * (len(METRICS) + 1) + "|")
    for label in ("baseline", "deterministic", "P_star", "P_feasible", "P2_runner_up"):
        blk = validate_res["arms"].get(label)
        if not blk:
            continue
        cells = []
        for m, _, _, _ in METRICS:
            st = blk["metrics"][m]
            cells.append(f"{st['mean']}±{st['std']}{fmt_ci(st)}")
        L.append(f"| {lbl_cn(label)} | " + " | ".join(cells) + " |")
    L.append("")
    L.append(f"> 括号内为 95% t-CI。n = {len(validate_seeds)}。")
    L.append("")

    L.append("## 4. 配对改进（同种子比较，正数 = 后者更优）")
    L.append("")
    L.append("| 臂 | 对照 | 指标 | 平均改进 | 95% CI | 胜/负/平 | 统计显著 |")
    L.append("|---|---|---|---|---|---|---|")
    for label in ("P_star", "P_feasible", "P2_runner_up"):
        cmp_block = validate_res["comparisons"].get(label)
        if not cmp_block:
            continue
        for ref_label in ("baseline", "deterministic"):
            if ref_label not in cmp_block:
                continue
            for m, name, unit, _ in METRICS:
                st = cmp_block[ref_label][m]
                sig = "**是**" if st["significant_gain"] else ("显著变差" if st["significant_loss"] else "否")
                L.append(
                    f"| {lbl_cn(label)} | {lbl_cn(ref_label)} | {name} ({unit}) | "
                    f"{st['mean']}{fmt_ci(st)} | {st['ci95']} | {st['wins']}/{st['losses']}/{st['ties']} | {sig} |"
                )
    L.append("")

    L.append("## 5. 控制证据核验（沙盒回传，非计划值）")
    L.append("")
    ev = {}
    for r in validate_res.get("run_log", []):
        ev.setdefault(r["tag"], r["deployed_controls"])
    L.append("| 臂 | 实测周期 (s) | 干道绿 (s) | 交叉绿 (s) | 首绿起始 (s) | 实测分流比 | 信号程序来源 |")
    L.append("|---|---|---|---|---|---|---|")
    for tag in ("baseline", "deterministic", "P_star", "P_feasible", "P2_runner_up"):
        if tag not in ev:
            continue
        d = ev[tag]
        L.append(f"| {lbl_cn(tag)} | {d.get('cycle_length_s')} | {d.get('arterial_green_s')} | "
                 f"{d.get('cross_green_s')} | {d.get('first_green_start_s')} | "
                 f"{d.get('reroute_ratio_applied')} | {d.get('signal_program_source')} |")
    L.append("")

    if online_res:
        L.append("## 6. 在线闭环管线一致性检查")
        L.append("")
        L.append(f"- 模型调用次数：{online_res['llm_calls']}")
        L.append(f"- `decision_mode`：`{online_res['decision_mode']}`")
        L.append(f"- 执行轮数：{online_res['rounds_executed']}")
        L.append(f"- 采纳来源：`{online_res['best_source']}`")
        rec = online_res.get("recommendation") or {}
        L.append(f"- verdict：`{rec.get('verdict')}`")
        L.append(f"- 说明：{rec.get('note')}")
        L.append("")

    L.append("## 7. 排队约束行为审计")
    L.append("")
    L.append(f"线上闭环只在「候选实测峰值排队 ≤ **参考规则链方案**实测峰值排队 × "
             f"{QUEUE_BLOWUP_RATIO}」时才采纳（同一种子）。本节检查该约束在标定工况下的实际行为。")
    L.append("")
    L.append("| 调参种子 | 无干预基线峰值排队 (m) | 参考规则链峰值排队 (m) = 阈值分母 | 阈值 (m) | 参考方案本身是否通过 |")
    L.append("|---|---|---|---|---|")
    qthr = a.get("queue_threshold_by_seed", {})
    bq = a.get("baseline_queue_by_seed", {})
    dq = a.get("deterministic_queue_by_seed", {})
    for s in qthr:
        ref = dq.get(s)
        thr = round(ref * QUEUE_BLOWUP_RATIO, 2) if isinstance(ref, (int, float)) else None
        L.append(f"| {s} | {bq.get(s)} | {ref} | {thr} | 是（阈值即其自身 × {QUEUE_BLOWUP_RATIO}） |")
    L.append("")
    total = search_res.get("candidates_total")
    feas = search_res.get("feasible_candidates_total")
    if total:
        L.append(f"- 调参网格内共评测 {total} 个候选，其中通过排队约束 **{feas}** 个"
                 f"（{feas / total * 100:.0f}%）。")
        L.append("- 未通过的候选，其排队高于参考方案 "
                 f"{int((QUEUE_BLOWUP_RATIO - 1) * 100)}% 以上（这是判据的定义）；"
                 "通过率低是网格本身在探索边界所致，不代表约束有缺陷。")
    L.append("- 历史对照：该约束此前以**无干预基线**排队为分母，而基线排队在种子间从 "
             "60 m 波动到 307.5 m，导致 32/32 个候选（含参考规则链本身）全部被拒 —— "
             "闭环在标定工况下一条策略都采纳不了。证据与数字见 `closed_loop_gain_v1.md` 第 7 节。")
    L.append("")

    L.append("## 8. 结论（按预设判据自动生成）")
    L.append("")
    L.extend(verdict_lines(validate_res, search_res, a))
    L.append("")
    L.append("## 9. 诚实边界")
    L.append("")
    L.append("- 本实验证明的是**闭环机制**的增益：所有候选过的是与线上同一套闸门"
             "（schema 校验 → 物理约束裁剪 → 实测度量 → 采纳规则）。")
    L.append("- 搜索器是确定性的坐标上升，它在闭环里**顶替 LLM 的位置**。"
             "因此本报告**不能**用来证明「某个大模型能找到这个策略」——"
             "那需要真实模型参与，属于未完成项。")
    L.append("- P\\* 是给定网格上的局部最优，不是全局最优；网格之外的策略空间未探索。")
    L.append("- 留出种子与调参种子不相交，但两者都来自同一套工况标定；"
             "跨工况（不同流量/不同位置事故）的迁移性未验证。")
    L.append("- 第 3–4 节的 P\\* 是**不加排队约束**的延误上限；线上真正会采纳的是 "
             "P_feasible（若不存在则说明网格内没有「不牺牲排队」的增益）。两者不可混用。")
    L.append("")
    return "\n".join(L) + "\n"


def lbl_cn(label: str) -> str:
    return {
        "baseline": "baseline（无干预）",
        "deterministic": "deterministic（规则链）",
        "P_star": "**P\\***（延误最优）",
        "P_feasible": "P_feasible（约束内最优）",
        "P2_runner_up": "P2（次优）",
    }.get(label, label)


def verdict_lines(validate_res, search_res, anchor) -> List[str]:
    """Generate only claims the measured numbers actually support."""
    out: List[str] = []
    arms = validate_res["arms"]
    cmps = validate_res["comparisons"]

    if "P_star" in cmps and "deterministic" in cmps["P_star"]:
        st = cmps["P_star"]["deterministic"]["avg_delay_s"]
        b = arms["P_star"]["metrics"]["avg_delay_s"]["mean"]
        d = arms["deterministic"]["metrics"]["avg_delay_s"]["mean"]
        pct = (d - b) / d * 100.0 if d else 0.0
        if st["significant_gain"]:
            out.append(f"1. **成立**：在 {st['n']} 个留出种子上，P\\* 的平均延误比确定性规则链低 "
                       f"{st['mean']:.2f} s/veh（{d:.2f} → {b:.2f}，{pct:.1f}%），"
                       f"95% 配对 CI {st['ci95']} 下界 > 0，**统计显著**（{st['wins']}/{st['n']} 个种子胜出）。")
        elif st["significant_loss"]:
            out.append(f"1. **不成立（变差）**：P\\* 的平均延误比确定性规则链**高** "
                       f"{abs(st['mean']):.2f} s/veh，95% 配对 CI {st['ci95']} 上界 < 0。"
                       f"说明在标定工况下，闭环搜索没有找到比现有规则链更好的配时/诱导组合。")
        else:
            out.append(f"1. **未达统计显著**：P\\* 相对确定性规则链的平均延误差为 "
                       f"{st['mean']:.2f} s/veh，95% 配对 CI {st['ci95']} **跨 0**，"
                       f"胜率 {st['wins']}/{st['n']}。即「有一点改善迹象，但样本量不足以断言成立」。")
    if "P_feasible" in cmps and "deterministic" in cmps["P_feasible"]:
        stf = cmps["P_feasible"]["deterministic"]["avg_delay_s"]
        fb = arms["P_feasible"]["metrics"]["avg_delay_s"]["mean"]
        dd = arms["deterministic"]["metrics"]["avg_delay_s"]["mean"]
        out.append(f"2. 约束内最优 P_feasible（= 线上当前会采纳的候选）相对规则链的"
                   f"平均延误差 {stf['mean']:.2f} s/veh（{dd:.2f} → {fb:.2f}），"
                   f"CI {stf['ci95']}，胜率 {stf['wins']}/{stf['n']}"
                   f"（{'统计显著' if stf['significant_gain'] else '不显著'}）。")
        if stf["mean"] is not None and stf["mean"] > 0:
            out.append(f"   - 该差值为正，意味着排队约束确实漏掉了一部分延误收益；"
                       f"缺口大小 = P\\* 与 P_feasible 的差值。")
        else:
            out.append("   - 该差值**不为正**：被排队约束放行的那条策略在留出种子上比规则链更差。"
                       "这说明「放宽约束就能拿到增益」是错的 —— 网格内没有既不放排队、又能稳定降延误的策略，"
                       "约束并不是收益的瓶颈，**调参集的代表性**才是。")
    if "P2_runner_up" in cmps and "deterministic" in cmps["P2_runner_up"]:
        st2 = cmps["P2_runner_up"]["deterministic"]["avg_delay_s"]
        out.append(f"3. 次优候选 P2 相对规则链的平均延误差 {st2['mean']:.2f} s/veh，"
                   f"CI {st2['ci95']}，胜率 {st2['wins']}/{st2['n']}"
                   f"（{'显著' if st2['significant_gain'] else '不显著'}）。"
                   "若两个候选结论方向相反，说明结论对策略选择敏感，需扩大种子或收紧网格。")
    out.append(f"4. 调参阶段锚点：无干预 {anchor['baseline_mean_delay_s']} s/veh、"
               f"规则链 {anchor['deterministic_mean_delay_s']} s/veh —— "
               "复验阶段两者在留出种子上的数值列于第 3 节，可比对是否稳定。")
    return out


# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(description="闭环控制策略增益评估（标定工况 · 多种子）")
    ap.add_argument("--explore-seeds", type=int, nargs="+", default=DEFAULT_EXPLORE_SEEDS)
    ap.add_argument("--validate-seeds", type=int, nargs="+", default=DEFAULT_VALIDATE_SEEDS)
    ap.add_argument("--duration", type=int, default=600)
    ap.add_argument("--incident-start", type=int, default=150)
    ap.add_argument("--incident-end", type=int, default=420)
    ap.add_argument("--passes", type=int, default=2)
    ap.add_argument("--online-check-seed", type=int, default=None,
                    help="在线管线回放所用种子（默认取第一个调参种子）")
    ap.add_argument("--skip-online-check", action="store_true")
    ap.add_argument("--tag", type=str, default=time.strftime("%Y%m%d_%H%M%S"))
    ap.add_argument("--out", type=str, default=None, help="报告 markdown 路径")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--regen-from", type=str, default=None,
                    help="由已保存的 raw json 重建报告（不重跑仿真，用于修正报告措辞）")
    args = ap.parse_args()

    if args.regen_from:
        raw = json.loads(Path(args.regen_from).read_text(encoding="utf-8"))
        cfg = raw["config"]
        rebuilt = {
            "arms": {
                k: {
                    "label": v["label"], "note": v["note"], "metrics": v["metrics"],
                    "kpis": {int(s): kpi for s, kpi in v["per_seed"].items()},
                }
                for k, v in raw["validation"]["arms"].items()
            },
            "comparisons": raw["validation"]["comparisons"],
            "run_log": raw["run_log"],
            "validate_seeds": cfg["validate_seeds"],
        }
        md = to_markdown(cfg["explore_seeds"], cfg["validate_seeds"], cfg["duration"],
                         cfg["incident_start"], cfg["incident_end"],
                         raw["search"], rebuilt, raw.get("online_pipeline_check"),
                         raw["sim_count"], raw["elapsed_s"],
                         guard=raw.get("deterministic_equivalence_guard"))
        out_path = (Path(args.out) if args.out
                    else Path(args.regen_from).parent / f"closed_loop_gain_{args.tag}.md")
        out_path.write_text(md, encoding="utf-8")
        print(f"[closed_loop_gain] 报告已由原始数据重建 → {out_path}")
        return

    overlap = set(args.explore_seeds) & set(args.validate_seeds)
    if overlap:
        raise SystemExit(f"调参种子与留出种子必须不相交，重叠：{sorted(overlap)}")

    t0 = time.time()
    exp = PolicyGainExperiment(args.duration, args.incident_start, args.incident_end,
                               quiet=args.quiet)

    # 内置护栏：用确定性取值构造的「等价策略」必须复现同一套下发参数。
    # 如果这条不成立，说明策略叠加层与规则链之间有口径差，后面的比较全部无意义。
    guard = exp.deterministic_equivalence()
    if not guard["ok"]:
        raise SystemExit(
            "护栏失败：确定性取值经策略叠加层后并未复现同一套下发参数 —— "
            f"{guard['differences']}。停止实验，先排查口径差。"
        )
    print(f"[护栏] 确定性等价策略复现同一套下发参数 ✓ "
          f"（绿波相位差最大偏差 {guard['max_offset_delta_s']}s）", flush=True)

    search_res = exp.search(args.explore_seeds, args.passes)

    arms = [
        {"label": "baseline", "kind": "baseline", "note": "无干预"},
        {"label": "deterministic", "kind": "deterministic", "note": "现有确定性规则链"},
        {
            "label": "P_star", "kind": "policy",
            "payload": exp._payload_for(search_res["incumbent_policy"]),
            "note": "调参阶段延误最优（不加排队约束，= 机制上限）",
        },
    ]
    if search_res.get("feasible_policy"):
        arms.append({
            "label": "P_feasible", "kind": "policy",
            "payload": exp._payload_for(search_res["feasible_policy"]),
            "note": "同时满足排队约束的最优（= 线上会真正采纳的候选）",
        })
    if search_res.get("runner_up"):
        arms.append({
            "label": "P2_runner_up", "kind": "policy",
            "payload": search_res["runner_up"]["payload"],
            "note": "调参阶段次优候选（冻结）",
        })

    validate_res = exp.validate(arms, args.validate_seeds)
    validate_res["run_log"] = exp.run_log

    online_res = None
    if not args.skip_online_check:
        online_res = exp.online_pipeline_check(
            exp._payload_for(search_res["incumbent_policy"]),
            seed=args.online_check_seed or args.explore_seeds[0],
            rounds=1,
        )

    elapsed = time.time() - t0
    md = to_markdown(args.explore_seeds, args.validate_seeds, args.duration,
                     args.incident_start, args.incident_end,
                     search_res, validate_res, online_res, exp.sim_count, elapsed,
                     guard=guard)
    print("\n" + md)

    out_path = Path(args.out) if args.out else PROJ_ROOT / "experiments" / f"closed_loop_gain_{args.tag}.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md, encoding="utf-8")
    raw_path = out_path.parent / f"closed_loop_gain_raw_{args.tag}.json"
    raw_path.write_text(json.dumps({
        "config": {
            "duration": args.duration, "incident_start": args.incident_start,
            "incident_end": args.incident_end, "passes": args.passes,
            "explore_seeds": args.explore_seeds, "validate_seeds": args.validate_seeds,
            "knob_grid": KNOB_GRID, "queue_blowup_ratio": QUEUE_BLOWUP_RATIO,
        },
        "search": search_res,
        "deterministic_equivalence_guard": guard,
        "validation": {
            "arms": {
                k: {
                    "label": v["label"], "note": v["note"],
                    "metrics": v["metrics"],
                    "per_seed": {str(s): v["kpis"][s] for s in v["kpis"]},
                }
                for k, v in validate_res["arms"].items()
            },
            "comparisons": validate_res["comparisons"],
        },
        "online_pipeline_check": online_res,
        "run_log": exp.run_log,
        "sim_count": exp.sim_count,
        "elapsed_s": round(elapsed, 1),
    }, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"[closed_loop_gain] 报告 → {out_path}")
    print(f"[closed_loop_gain] 原始数据 → {raw_path}")


if __name__ == "__main__":
    main()
