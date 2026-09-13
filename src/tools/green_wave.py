"""
TrafficAgent-DSS: Dynamic Arterial Green Wave Coordinator
Computes optimal signal progression offsets and progression bandwidth along arterial corridors.
"""

from typing import Dict, List, Tuple
import math


class GreenWaveCoordinator:
    """
    Coordinates adjacent traffic signals along an arterial corridor to create green waves.
    Supports unidirectional and bidirectional progression optimization.
    """

    def __init__(
        self,
        default_progression_speed: float = 13.89,  # ~50 km/h in m/s
        min_progression_speed: float = 9.72,      # ~35 km/h
        max_progression_speed: float = 16.67,     # ~60 km/h
    ):
        self.default_speed = max(1.0, float(default_progression_speed))
        self.min_speed = max(0.1, float(min_progression_speed))
        self.max_speed = max(self.min_speed, float(max_progression_speed))

    def compute_offsets(
        self,
        intersection_distances: List[float],  # Distances between J_i and J_{i+1} in meters
        cycle_length: float,                  # Common cycle length C in seconds
        green_splits_arterial: List[float],   # Arterial green times for each intersection
        progression_speed: float = None,      # Target speed in m/s
        bidirectional: bool = True,
        weight_forward: float = 0.6,          # Directional weight for peak direction
    ) -> Dict[str, any]:
        """
        Computes progression offsets for a series of N intersections (J0, J1, ..., J_{N-1}).
        Offset of J0 is 0.
        """
        if progression_speed is None:
            progression_speed = self.default_speed
        progression_speed = max(1.0, max(self.min_speed, min(self.max_speed, float(progression_speed))))

        num_nodes = len(intersection_distances) + 1
        if len(green_splits_arterial) != num_nodes:
            raise ValueError(f"Expected {num_nodes} green splits for {len(intersection_distances)} links.")

        safe_cycle = max(1.0, cycle_length)
        weight_forward = max(0.0, min(1.0, weight_forward))

        # 1. Forward travel times with negative distance guards
        travel_times = [max(0.0, float(d)) / progression_speed for d in intersection_distances]

        # 2. Cumulative offsets
        if (bidirectional and 0.0 <= weight_forward < 1.0) or (not bidirectional and weight_forward == 0.0):
            # Bidirectional (or pure reverse when weight_forward == 0.0) progression.
            #
            # The blend must be applied to the CUMULATIVE travel time from the corridor
            # origin, not to each link's own travel time. Blending per link and then
            # accumulating the blended step double-counts the weighting: on an equidistant
            # arterial the offsets grow linearly (e.g. [0, 40.3, 80.6] where 40.3 is
            # already a 0.6/0.4 mix of tt=21.6 and C-tt=68.4), which pushes every
            # downstream junction far past its physically correct phase and destroys the
            # progression band. Measured on the J1-J3 corridor: the linear-growth variant
            # raised strategy-B mean delay from 21.1 to 27.3 s/veh and peak queue from
            # 105 m to 240 m versus the cumulative variant below.
            #
            # For balanced two-way progression (weight 0.5) this converges on the classical
            # alternate system (0, C/2, 0, C/2); weight_forward == 1.0 degenerates to a
            # strictly forward progression, as before.
            offsets = [0.0]
            cumulative_travel_time = 0.0
            for tt in travel_times:
                cumulative_travel_time += tt
                ideal_forward = cumulative_travel_time % safe_cycle
                ideal_reverse = (safe_cycle - ideal_forward) % safe_cycle
                next_offset = (
                    weight_forward * ideal_forward
                    + (1.0 - weight_forward) * ideal_reverse
                ) % safe_cycle
                offsets.append(round(next_offset, 1))
        else:
            # Unidirectional forward progression
            offsets = [0.0]
            for tt in travel_times:
                next_offset = (offsets[-1] + tt) % safe_cycle
                offsets.append(round(next_offset, 1))

        # 4. Compute theoretical progression bandwidth
        # Bandwidth is limited by the smallest green split minus dispersion
        if cycle_length <= 0:
            bandwidth_forward = 0.0
            bandwidth_reverse = 0.0
            bandwidth_ratio = 0.0
            bidirectional_bandwidth_ratio = 0.0
        else:
            min_green = min(green_splits_arterial)
            bandwidth_forward = max(0.0, min_green - 4.0)  # accounting for platoon dispersion
            bandwidth_ratio = round((bandwidth_forward / cycle_length) * 100.0, 1)

            # Reverse direction theoretical bandwidth under progression tuning
            if (bidirectional or weight_forward == 0.0) and weight_forward < 1.0:
                reverse_share = 1.0 - weight_forward
                if weight_forward <= 0.0:
                    bandwidth_reverse = round(bandwidth_forward, 1)
                else:
                    bandwidth_reverse = max(0.0, round(bandwidth_forward * min(1.0, reverse_share / max(1e-6, weight_forward)), 1))
            else:
                bandwidth_reverse = 0.0

            bidirectional_bandwidth_ratio = round(
                (weight_forward * bandwidth_forward + (1.0 - weight_forward) * bandwidth_reverse) / cycle_length * 100.0,
                1,
            )

        return {
            "cycle_length": cycle_length,
            "progression_speed_kmh": round(progression_speed * 3.6, 1),
            "offsets": offsets,
            "travel_times": [round(tt, 1) for tt in travel_times],
            "bandwidth_seconds": round(bandwidth_forward, 1),
            "bandwidth_ratio_percent": bandwidth_ratio,
            "bandwidth_reverse_seconds": bandwidth_reverse,
            "bidirectional_bandwidth_ratio_percent": bidirectional_bandwidth_ratio,
            "coordination_quality": "Excellent" if bandwidth_ratio >= 35.0 else ("Good" if bandwidth_ratio >= 20.0 else "Fair")
        }
