"""
TrafficAgent-DSS: Dynamic Rerouting & VMS Diversion Allocator
Calculates dynamic flow diversion ratios to prevent bottleneck queue spillback.
"""

from typing import Dict, List


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
        queue_ratio = bottleneck_queue_meters / max(10.0, bottleneck_link_length)

        # Condition 1: If bottleneck is healthy, zero diversion
        if bottleneck_occupancy < self.occ_thresh and queue_ratio < 0.5:
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
        occ_excess = max(0.0, (bottleneck_occupancy - self.occ_thresh) / (1.0 - self.occ_thresh))
        queue_excess = max(0.0, (queue_ratio - self.queue_thresh) / (1.0 - self.queue_thresh))
        severity = 0.5 * occ_excess + 0.5 * queue_excess

        target_diversion = min(self.max_diversion, max(0.10, severity * self.max_diversion))

        # Constrain by bypass spare capacity
        max_possible_by_bypass = bypass_spare_capacity_vph / max(1.0, upstream_flow_vph)
        final_diversion = min(target_diversion, max_possible_by_bypass)
        final_diversion = round(max(0.0, final_diversion), 2)

        diverted_vph = round(upstream_flow_vph * final_diversion)

        vms_text = (
            f"【交通诱导】前方主干路严重拥堵，排队{int(bottleneck_queue_meters)}米。"
            f"建议非直通车辆右转经旁路绕行，预计节省通行时间8-12分钟。"
        )

        return {
            "need_diversion": final_diversion > 0.0,
            "diversion_ratio": final_diversion,
            "diverted_flow_vph": diverted_vph,
            "vms_advisory": vms_text,
            "queue_ratio": round(queue_ratio, 3),
            "severity_score": round(severity, 3),
            "risk_warning": "分流流量处于旁路承载能力安全区间" if final_diversion > 0 else "拥堵受控"
        }
