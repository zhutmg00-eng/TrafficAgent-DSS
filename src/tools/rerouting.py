"""
TrafficAgent-DSS: Dynamic Rerouting & VMS Diversion Allocator
Calculates dynamic flow diversion ratios to prevent bottleneck queue spillback.
"""

from typing import Any, Dict, List


class DynamicReroutingAllocator:
    """
    Computes real-time diversion fractions for approaching traffic to alternative routes,
    ensuring downstream bottleneck queue does not spill back into upstream junctions,
    while monitoring alternative route capacity to avoid secondary bottlenecks.
    """

    def __init__(
        self,
        occupancy_threshold: float = 0.70,     # Congestion trigger threshold
        spillback_queue_ratio: float = 0.75,   # Queue / link length threshold
        max_diversion_ratio: float = 0.40,     # Maximum allowable diversion proportion
        bypass_capacity_threshold: float = 0.80 # Bypass overload limit
    ):
        self.occ_thresh = occupancy_threshold
        self.queue_thresh = spillback_queue_ratio
        self.max_diversion = max_diversion_ratio
        self.bypass_limit = bypass_capacity_threshold

    def calculate_diversion(
        self,
        bottleneck_queue_meters: float,
        bottleneck_link_length: float,
        bottleneck_occupancy: float,
        upstream_flow_vph: float,
        bypass_current_occupancy: float,
        bypass_spare_capacity_vph: float,
    ) -> Dict[str, any]:
        """
        Calculates optimal diversion proportion alpha in [0.0, max_diversion].
        """
        bottleneck_queue_meters = max(0.0, float(bottleneck_queue_meters))
        bottleneck_link_length = max(10.0, float(bottleneck_link_length))
        bottleneck_occupancy = max(0.0, min(1.0, float(bottleneck_occupancy)))
        bypass_current_occupancy = max(0.0, min(1.0, float(bypass_current_occupancy)))
        upstream_flow_vph = max(0.0, float(upstream_flow_vph))
        bypass_spare_capacity_vph = max(0.0, float(bypass_spare_capacity_vph))

        queue_ratio = bottleneck_queue_meters / bottleneck_link_length

        # Safe guard: if upstream flow is zero or negative, no vehicles to divert
        if upstream_flow_vph <= 0.0:
            return {
                "need_diversion": False,
                "diversion_ratio": 0.0,
                "diverted_flow_vph": 0.0,
                "vms_advisory": "前方主干路通行顺畅，请按道行驶",
                "risk_warning": "上游流量为0，无分流需求"
            }

        # Condition 1: If bottleneck is healthy, zero diversion
        if bottleneck_occupancy < self.occ_thresh and queue_ratio < self.queue_thresh:
            return {
                "need_diversion": False,
                "diversion_ratio": 0.0,
                "diverted_flow_vph": 0.0,
                "vms_advisory": "前方主干路通行顺畅，请按道行驶",
                "risk_warning": "无拥堵扩散风险"
            }

        # Condition 2: Check bypass spare capacity
        if bypass_current_occupancy >= self.bypass_limit or bypass_spare_capacity_vph <= 50:
            return {
                "need_diversion": False,
                "diversion_ratio": 0.0,
                "diverted_flow_vph": 0.0,
                "vms_advisory": "主路拥堵，旁路亦已饱和，建议减速慢行",
                "risk_warning": "旁路容量不足，启动分流将引发次生恶性瘫痪！已自动熔断分流。"
            }

        # Condition 3: Calculate required diversion to clear excess queue
        # Excess severity score [0.0, 1.0]
        if self.occ_thresh >= 1.0:
            occ_excess = 1.0 if bottleneck_occupancy >= self.occ_thresh else 0.0
        else:
            occ_excess = max(0.0, min(1.0, (bottleneck_occupancy - self.occ_thresh) / max(1e-6, 1.0 - self.occ_thresh)))

        if self.queue_thresh >= 1.0:
            queue_excess = 1.0 if queue_ratio >= self.queue_thresh else 0.0
        else:
            queue_excess = max(0.0, min(1.0, (queue_ratio - self.queue_thresh) / max(1e-6, 1.0 - self.queue_thresh)))

        severity = 0.5 * occ_excess + 0.5 * queue_excess

        if severity <= 0.0:
            target_diversion = 0.0
        else:
            target_diversion = min(self.max_diversion, max(0.10, severity * self.max_diversion))

        # Constrain by bypass spare capacity
        max_possible_by_bypass = bypass_spare_capacity_vph / max(1.0, upstream_flow_vph)
        final_diversion = min(target_diversion, max_possible_by_bypass)
        final_diversion = round(max(0.0, final_diversion), 2)

        diverted_vph = round(upstream_flow_vph * final_diversion)

        if final_diversion > 0.0:
            # No "saves N minutes" claim here. The previous wording promised "预计节省通行
            # 时间8-12分钟" as a literal constant, unrelated to any input or computation, and
            # it propagated into the decision brief as if it were a modelled result. A real
            # VMS advisory states the condition and the advised action; the benefit is
            # established by the What-If rollout, not asserted in the sign text.
            vms_text = (
                f"【交通诱导】前方主干路拥堵，排队{int(bottleneck_queue_meters)}米，"
                f"建议非直通车辆右转经旁路绕行。"
            )
        else:
            vms_text = "前方主干路通行顺畅，请按道行驶"

        return {
            "need_diversion": final_diversion > 0.0,
            "diversion_ratio": final_diversion,
            "diverted_flow_vph": diverted_vph,
            "vms_advisory": vms_text,
            "queue_ratio": round(queue_ratio, 3),
            "severity_score": round(severity, 3),
            "risk_warning": "分流流量处于旁路承载能力安全区间" if final_diversion > 0 else "拥堵受控"
        }

    def apply_diversion_override(
        self,
        plan: Dict[str, Any],
        diversion_ratio: float,
        upstream_flow_vph: float,
        bottleneck_queue_meters: float,
        bypass_spare_capacity_vph: float = 0.0,
    ) -> Dict[str, Any]:
        """
        Re-derives the diversion payload after an external decision overrides the
        ratio this allocator would have chosen on its own (see the LLM decision
        layer, `src/agents/llm_decision.py`).

        The caller clamps `diversion_ratio` into the feasible domain; this method
        only keeps the *derived* fields and the VMS copy consistent with the ratio
        actually deployed, so the published advisory can never describe a
        different action than the one that reached the simulator. The diversion
        ratio is still capped by this allocator's own ceiling and by the bypass
        spare-capacity constraint — an override cannot talk the system into
        overloading the alternative route.
        """
        upstream_flow_vph = max(0.0, float(upstream_flow_vph))
        requested = max(0.0, float(diversion_ratio))

        # Same two hard ceilings the allocator applies to its own decision: the
        # absolute 40% policy cap and the bypass spare-capacity constraint.
        if upstream_flow_vph <= 0.0:
            by_capacity = self.max_diversion
        else:
            by_capacity = max(0.0, float(bypass_spare_capacity_vph)) / max(1.0, upstream_flow_vph)
        applied_ratio = round(min(max(0.0, min(self.max_diversion, requested)), by_capacity), 2)

        diverted_vph = round(upstream_flow_vph * applied_ratio)

        if applied_ratio > 0.0:
            vms_text = (
                f"【交通诱导】前方主干路拥堵，排队{int(max(0.0, float(bottleneck_queue_meters)))}米，"
                f"建议非直通车辆右转经旁路绕行。"
            )
            risk_text = "分流流量处于旁路承载能力安全区间"
        else:
            vms_text = "前方主干路通行顺畅，请按道行驶"
            risk_text = "拥堵受控"

        out = dict(plan)
        out.update({
            "need_diversion": applied_ratio > 0.0,
            "diversion_ratio": applied_ratio,
            "diverted_flow_vph": diverted_vph,
            "vms_advisory": vms_text,
            "risk_warning": risk_text,
            "diversion_source": "external_policy_override",
            "diversion_requested_ratio": round(requested, 4),
        })
        return out
