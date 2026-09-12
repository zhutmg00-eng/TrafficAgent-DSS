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
from pathlib import Path
from typing import Dict, List, Any, Optional

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
        self.webster = WebsterSignalOptimizer()
        self.green_wave = GreenWaveCoordinator()
        self.rerouter = DynamicReroutingAllocator()
        self.evaluator = PerformanceEvaluator()
        self.llm = LLMReasoningClient()

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
        else:
            engine = "deterministic_template"
            label = "确定性规则模板（未配置大模型 API Key）"
        return {
            "reasoning_mode": mode,
            "reasoning_engine": engine,
            "reasoning_label": label,
            "reasoning_error": error,
        }

    def _tool_plan(self, diagnosis: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Shared deterministic traffic-engineering computation for the J1-J3 corridor.

        Detector-derived quantities (queue length, blockage ratio, cross-section occupancy,
        bypass occupancy) are read from the diagnosis payload, so the optimised parameters
        actually respond to the observed incident instead of a frozen scenario constant.
        Only the phase-level demand split remains a corridor calibration constant, because
        a single cross-section detector cannot resolve per-approach flows.
        """
        state = (diagnosis or {}).get("input_state") or {}

        queue_m = float(state.get("queue_m", 165.0))
        link_len = float(state.get("link_length_m", 300.0))
        occupancy = float(state.get("occupancy", 0.82))
        bypass_occ = float(state.get("bypass_occupancy", 0.28))

        # Corridor-calibrated demand profile (pcu/h per phase) and approach lane counts.
        phase_flows = [1600.0, 400.0, 500.0, 300.0]
        phase_lanes = [3, 1, 2, 1]

        timing = self.webster.compute_timing(phase_flows=phase_flows, phase_lanes=phase_lanes)
        arterial_green = timing["green_splits"][0]
        gw_plan = self.green_wave.compute_offsets(
            intersection_distances=[300.0, 300.0],
            cycle_length=timing["optimal_cycle"],
            green_splits_arterial=[arterial_green, arterial_green, arterial_green],
            progression_speed=13.89,  # 50 km/h
        )
        reroute_plan = self.rerouter.calculate_diversion(
            bottleneck_queue_meters=queue_m,
            bottleneck_link_length=link_len,
            bottleneck_occupancy=occupancy,
            upstream_flow_vph=1800.0,
            bypass_current_occupancy=bypass_occ,
            bypass_spare_capacity_vph=1200.0,
        )
        return {
            "timing": timing,
            "green_wave": gw_plan,
            "reroute": reroute_plan,
            "arterial_green": arterial_green,
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
            },
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
        cot_steps = [
            f"1. 【态势感知】监测到走廊主断面 [{bottleneck_edge}] 平均车速降至 {speed_kmh} km/h，占有率 {int(occupancy*100)}%。",
            f"2. 【空间排队】当前排队长度 {queue_m} 米，占路段库容比 {int(queue_ratio*100)}%，{'已越过' if queue_ratio >= 0.75 else '尚未触及'}回溢警戒线 (75%)。",
            f"3. 【成因归因】路段设计通行能力为 3 车道 (5400 pcu/h)，突发降速与排队激增表明存在【突发占道/瓶颈车道受阻】叠加【高峰车流集中汇聚】。",
            f"4. 【蔓延风险】按当前排队增速推演，上游交叉口存在被回溢车流锁死的风险。",
            f"5. 【旁路核查】平行分流通道当前占有率 {int(bypass_occ*100)}%，{'具备' if bypass_occ < 0.75 else '不具备'}实施动态诱导分流的备用容量。",
        ]
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
        }

    def diagnose_bottleneck(self, traffic_state: Dict[str, Any]) -> Dict[str, Any]:
        """
        Performs situational diagnosis and bottleneck attribution.

        Path A (preferred): LLM chain-of-thought attribution using live detector data.
        Path B (fallback)  : deterministic rule template — used when no API key is
                             configured or the model call fails, and honestly labelled
                             via `reasoning_mode` in the returned payload.
        """
        bottleneck_edge = traffic_state.get("bottleneck_edge", "J1_J2")
        queue_m = float(traffic_state.get("queue_m", 165.0))
        link_len = float(traffic_state.get("link_length_m", 300.0))
        speed_kmh = float(traffic_state.get("speed_kmh", 8.2))
        occupancy = float(traffic_state.get("occupancy", 0.82))
        bypass_occ = float(traffic_state.get("bypass_occupancy", 0.28))

        queue_ratio = queue_m / max(1.0, link_len)
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
                "设计通行能力_pcu_h": 5400,
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
                    "can_reroute": bool(payload.get("can_reroute", fallback["can_reroute"])),
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
            "cycle_length": timing["optimal_cycle"],
            "green_split_arterial": arterial_green,
            "reroute_ratio": 0.0,
            "green_wave": False,
        }

        strategy_b = {
            "id": "strategy_b_agent_dss",
            "name": "方案 B：TrafficAgent-DSS 系统级协同调控 (推荐)",
            "description": (
                f"时空协同控制：Webster 动态调优（周期 {timing['optimal_cycle']}s）"
                f" + J1-J3 双向绿波协调 + 上游 VMS 动态诱导分流 "
                f"{int(reroute_plan['diversion_ratio']*100)}% 至北部旁路。"
            ),
            "cycle_length": timing["optimal_cycle"],
            "green_split_arterial": arterial_green,
            "green_wave_offsets": gw_plan["offsets"],
            "reroute_ratio": reroute_plan["diversion_ratio"],
            "vms_advisory": reroute_plan["vms_advisory"],
            "risk_warning": reroute_plan["risk_warning"],
            "green_wave": True,
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
                    "韦伯斯特最优周期_秒": timing["optimal_cycle"],
                    "主路绿信比_秒": arterial_green,
                    "各相位绿灯_秒": timing["green_splits"],
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
                if desc_a:
                    strategy_a["description"] = desc_a
                strategy_b["description"] = desc_b
                strategy_b["rationale"] = rationale
                if vms:
                    strategy_b["vms_advisory"] = vms
                if risk:
                    strategy_b["risk_warning"] = risk
            else:
                mode = LLMReasoningClient.MODE_ERROR
                error = "model response missing required strategy narrative fields"

        narrative_mode = self.reasoning_metadata(mode, error)

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
        plan = self._tool_plan(diagnosis)
        timing = plan["timing"]
        gw_plan = plan["green_wave"]
        reroute_ratio = plan["reroute"]["diversion_ratio"] if use_rerouting else 0.0

        shared_control = {
            "green_wave_offsets": gw_plan["offsets"],
            "cycle_length": timing["optimal_cycle"],
            "arterial_green": plan["arterial_green"],
        }

        # Run Baseline
        print("[Agent] Rolling out Baseline (Do-Nothing)...")
        res_base = self.sandbox.run_simulation(
            scheme="baseline",
            duration=duration,
            incident_start=incident_start,
            incident_end=incident_end,
            control_params={"reroute_ratio": 0.0, "green_wave": False, "webster": False},
        )
        kpi_base = self.evaluator.compute_summary_kpi(res_base)

        # Run Strategy A (Webster only, single-point adaptive)
        print("[Agent] Rolling out Strategy A (Webster Adaptive)...")
        res_a = self.sandbox.run_simulation(
            scheme="webster",
            duration=duration,
            incident_start=incident_start,
            incident_end=incident_end,
            control_params=dict(
                shared_control, reroute_ratio=0.0, green_wave=False, webster=use_webster
            ),
        )
        kpi_a = self.evaluator.compute_summary_kpi(res_a)
        comp_a = self.evaluator.compare_schemes(kpi_base, kpi_a)

        # Run Strategy B (TrafficAgent-DSS coordinated)
        print("[Agent] Rolling out Strategy B (Coordinated Agent-DSS)...")
        res_b = self.sandbox.run_simulation(
            scheme="agent_dss",
            duration=duration,
            incident_start=incident_start,
            incident_end=incident_end,
            control_params=dict(
                shared_control,
                reroute_ratio=reroute_ratio,
                green_wave=use_green_wave,
                webster=use_webster,
            ),
        )
        kpi_b = self.evaluator.compute_summary_kpi(res_b)
        comp_b = self.evaluator.compare_schemes(kpi_base, kpi_b)

        return {
            "execution_mode": "physical_sumo_sandbox",
            "simulation_duration": duration,
            "time_stamps": res_base["time_stamps"],
            "strategy_inputs": plan["inputs_used"],
            "control_evidence": {
                "baseline": res_base.get("control_evidence", {}),
                "strategy_a": res_a.get("control_evidence", {}),
                "strategy_b": res_b.get("control_evidence", {}),
            },
            "raw_traces": {
                "baseline": {
                    "queues": res_base["queue_lengths"],
                    "speeds": res_base["bottleneck_speeds_kmh"],
                    "delays": res_base["vehicle_delays"],
                },
                "strategy_a": {
                    "queues": res_a["queue_lengths"],
                    "speeds": res_a["bottleneck_speeds_kmh"],
                    "delays": res_a["vehicle_delays"],
                },
                "strategy_b": {
                    "queues": res_b["queue_lengths"],
                    "speeds": res_b["bottleneck_speeds_kmh"],
                    "delays": res_b["vehicle_delays"],
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
            "calibrated_empirical_fallback": "标定经验数据（物理仿真不可用时的降级估算，非实测）",
            "calibrated_empirical_fast": "标定经验数据（快速交互模式，非实测）",
            "unknown": "⚠️ 数据来源未标注 —— 本报告不得作为量化结论对外使用",
        }.get(execution_mode, execution_mode)

        reasoning_label = diagnosis.get("reasoning_label", "确定性规则模板")
        narrative_label = strategies.get("narrative_label", "确定性规则模板")
        evidence = (rollout_results.get("control_evidence") or {}).get("strategy_b", {})

        strat_b = strategies.get("strategy_b") or {}

        # --- Comparison table rows -------------------------------------- #
        def row(label: str, key: str, unit: str, better: str) -> str:
            base_v, a_v, b_v = kpi_base.get(key), kpi_a.get(key), kpi_b.get(key)
            if comp_b and key in (
                "avg_delay_s", "max_queue_m", "avg_speed_kmh", "throughput_vph", "co2_emissions_kg"
            ):
                pct_key = {
                    "avg_delay_s": "delay_improvement_pct",
                    "max_queue_m": "queue_improvement_pct",
                    "avg_speed_kmh": "speed_improvement_pct",
                    "throughput_vph": "throughput_improvement_pct",
                    "co2_emissions_kg": "co2_improvement_pct",
                }[key]
                pct = comp_b.get(pct_key)
                pct_cell = f"**{better} {pct}%**" if pct is not None else "—"
            else:
                pct_cell = "—"
            return (
                f"| **{label}** | {self._fmt(base_v, unit)} | {self._fmt(a_v, unit)} | "
                f"**{self._fmt(b_v, unit)}** | {pct_cell} |"
            )

        table_rows = "\n".join(
            [
                row("平均车辆延误 (s/veh)", "avg_delay_s", " s", "降低"),
                row("最大排队长度 (m)", "max_queue_m", " m", "缩短"),
                row("瓶颈平均车速 (km/h)", "avg_speed_kmh", " km/h", "提升"),
                row("路网通行吞吐量 (veh/h)", "throughput_vph", " veh/h", "提升"),
                row("碳排放总量 (kg CO2)", "co2_emissions_kg", " kg", "减排"),
            ]
        )

        if comp_b:
            grade_line = f"**综合成效评级**：**{comp_b.get('overall_effectiveness_grade', '—')}**"
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

        # --- Recommended actions --------------------------------------- #
        cycle_len = strat_b.get("cycle_length")
        green_split = strat_b.get("green_split_arterial")
        offsets = strat_b.get("green_wave_offsets") or []
        reroute_pct = int(round(strat_b.get("reroute_ratio", 0.0) * 100))
        vms_msg = strat_b.get("vms_advisory") or "（未生成诱导文案）"
        risk_msg = strat_b.get("risk_warning") or "（未生成风险提示）"
        green_wave_active = bool(strat_b.get("green_wave"))

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
            evidence_block = (
                f"- 仿真中实际下发：信号控制指令 `{evidence.get('signal_commands', 0)}` 条、"
                f"重路由指令 `{evidence.get('reroute_commands', 0)}` 条\n"
                f"- 绿波协调：{'已生效' if evidence.get('green_wave_applied') else '未启用'}；"
                f"Webster 自适应：{'已生效' if evidence.get('webster_applied') else '未启用'}；"
                f"分流比例：{evidence.get('reroute_ratio_applied', 0)}\n"
                f"- 全局控制周期：`{evidence.get('cycle_length', '—')}` 秒"
            )
        else:
            evidence_block = "- 本次未采集到控制指令下发记录。"

        return f"""# 城市交通拥堵治理辅助决策建议简报 (Decision Briefing)

**报告编号**：`DSS-2026-EXP-{int(os.getpid())}`  
**决策系统**：TrafficAgent-DSS (基于交通仿真智能体的城市交通拥堵治理决策支持系统)  
**评估状态**：数字孪生沙盒推演完成 (What-If Simulation Verified)

---

### 〇、 数据来源与可信度声明 (Provenance)

| 项目 | 本次取值 |
| :--- | :--- |
| **仿真数据来源** | {mode_label} |
| **归因推理引擎** | {reasoning_label} |
| **方案叙事引擎** | {narrative_label} |

{data_notice}
> 说明：本报告中**所有性能指标均由交通工程工具计算或微观仿真产出**；大语言模型仅参与
> 归因解释与方案叙事，不参与任何数值生成。若上表标注为"降级估算/未启用"，则该组数据
> 不得作为量化结论对外宣称。

---

### 一、 态势感知与拥堵归因诊断 (Diagnosis)
- **拥堵瓶颈断面**：走廊核心路段 `{diagnosis.get('bottleneck_location', '—')}`
- **警情等级**：**{diagnosis.get('severity_level', '—')}**（死锁风险度：{diagnosis.get('spillback_risk', '—')}）
- **主要诱因归结**：
{chr(10).join(f"  - {rc}" for rc in diagnosis.get('root_causes', [])) or "  - （未获得归因结果）"}
- **智能体思维链 (CoT 推理过程)**：
{chr(10).join(f"  > {step}" for step in diagnosis.get('cot_reasoning', [])) or "  > （未获得推理过程）"}

---

### 二、 多方案数字孪生推演与成效比对 (What-If Analysis)

通过 SUMO 高保真微观物理推演沙盒，针对设定推演窗口期执行 A/B 方案平行推演，
五维核心指标对比如下：

| 评估维度 / 指标 | 现状基线 (Do-Nothing) | 方案A (Webster自适应) | 方案B (Agent协同治理) | 方案B改善幅度 |
| :--- | :--- | :--- | :--- | :--- |
{table_rows}

{grade_line}

**控制指令下发核验 (Control Evidence)**：
{evidence_block}

---

### 三、 决策推荐与协同处置指令建议 (Action Recommendation)

**【推荐采纳方案】**：**方案 B：TrafficAgent-DSS 系统级时空协同治理**

1. **诱导分流指令 (VMS Rerouting)**：
   - {reroute_section}
2. **干线信号协调 (Dynamic Green Wave)**：
   - {gw_section}
3. **风险自检与反思提示**：
   - {risk_msg}
"""
