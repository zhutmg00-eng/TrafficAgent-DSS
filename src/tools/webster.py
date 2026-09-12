"""
TrafficAgent-DSS: Webster Signal Timing Calculator
Classic traffic engineering algorithm for optimal cycle length and green split.
"""

from typing import Dict, List, Tuple
import math


class WebsterSignalOptimizer:
    """
    Implements Webster's classic method for fixed-time and adaptive signal timing:
      C_0 = (1.5 * L + 5) / (1 - Y)
      g_i = (C_0 - L) * (y_i / Y)
    """

    def __init__(
        self,
        saturation_flow_per_lane: float = 1800.0,
        lost_time_per_phase: float = 3.5,
        min_cycle: float = 45.0,
        max_cycle: float = 160.0,
        min_green: float = 10.0,
        yellow_time: float = 3.0,
        all_red_time: float = 1.0,
    ):
        self.s_per_lane = saturation_flow_per_lane
        self.lost_time_per_phase = lost_time_per_phase
        self.min_cycle = min_cycle
        self.max_cycle = max_cycle
        self.min_green = min_green
        self.yellow_time = yellow_time
        self.all_red_time = all_red_time

    def compute_timing(
        self,
        phase_flows: List[float],
        phase_lanes: List[int],
    ) -> Dict[str, any]:
        """
        Calculates optimal cycle length and phase green times.

        Args:
            phase_flows: List of hourly traffic volumes (veh/h or pcu/h) for each critical phase.
            phase_lanes: Number of lanes allocated for each phase.

        Returns:
            Dict containing:
              - optimal_cycle: Optimal cycle length in seconds
              - green_splits: List of allocated green times (seconds)
              - flow_ratios: Critical flow ratio y_i for each phase
              - total_flow_ratio: Y = sum(y_i)
              - total_lost_time: L in seconds
              - saturation_level: Degree of saturation under optimal plan
        """
        num_phases = len(phase_flows)
        if num_phases == 0 or len(phase_lanes) != num_phases:
            raise ValueError("Mismatched phase flows and lane counts.")

        # 1. Calculate flow ratio y_i for each phase
        flow_ratios = []
        for q, lanes in zip(phase_flows, phase_lanes):
            sat_flow = max(1, lanes) * self.s_per_lane
            y = max(0.01, q / sat_flow)
            flow_ratios.append(y)

        Y = sum(flow_ratios)
        total_lost_time = num_phases * self.lost_time_per_phase

        # 2. Webster Optimal Cycle Length
        # Handle oversaturated condition (Y >= 0.95)
        if Y >= 0.95:
            # Over-saturated state: cap at max practical cycle to maximize capacity
            optimal_cycle = self.max_cycle
            adjusted_Y = min(Y, 0.98)
        else:
            C_0 = (1.5 * total_lost_time + 5.0) / (1.0 - Y)
            optimal_cycle = max(self.min_cycle, min(self.max_cycle, round(C_0)))

        # 3. Available effective green time
        available_green = optimal_cycle - total_lost_time

        # 4. Allocate green splits proportionally to y_i / Y
        raw_greens = [(available_green * (y / max(0.001, Y))) for y in flow_ratios]

        # Enforce minimum green constraint
        green_splits = []
        deficit = 0.0
        for g in raw_greens:
            if g < self.min_green:
                deficit += (self.min_green - g)
                green_splits.append(self.min_green)
            else:
                green_splits.append(g)

        # Rebalance green if deficit was adjusted
        if deficit > 0:
            surplus_indices = [i for i, g in enumerate(green_splits) if g > self.min_green]
            if surplus_indices:
                total_surplus = sum(green_splits[i] - self.min_green for i in surplus_indices)
                for i in surplus_indices:
                    reduction = deficit * ((green_splits[i] - self.min_green) / max(0.001, total_surplus))
                    green_splits[i] = max(self.min_green, green_splits[i] - reduction)

        green_splits = [round(g, 1) for g in green_splits]

        # Degree of saturation x_i = q_i / (s_i * (g_i / C))
        degree_of_saturation = [
            round(q / (max(1, lanes) * self.s_per_lane * (g / optimal_cycle)), 3)
            for q, lanes, g in zip(phase_flows, phase_lanes, green_splits)
        ]

        return {
            "optimal_cycle": optimal_cycle,
            "green_splits": green_splits,
            "flow_ratios": [round(y, 3) for y in flow_ratios],
            "total_flow_ratio": round(Y, 3),
            "total_lost_time": round(total_lost_time, 1),
            "degree_of_saturation": degree_of_saturation,
            "is_oversaturated": Y >= 0.85
        }
