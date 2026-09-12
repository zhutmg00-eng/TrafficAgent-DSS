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
        self.default_speed = default_progression_speed
        self.min_speed = min_progression_speed
        self.max_speed = max_progression_speed

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
        progression_speed = max(self.min_speed, min(self.max_speed, progression_speed))

        num_nodes = len(intersection_distances) + 1
        if len(green_splits_arterial) != num_nodes:
            raise ValueError(f"Expected {num_nodes} green splits for {len(intersection_distances)} links.")

        # 1. Forward travel times
        travel_times = [d / progression_speed for d in intersection_distances]

        # 2. Cumulative offsets
        offsets = [0.0]
        for tt in travel_times:
            next_offset = (offsets[-1] + tt) % cycle_length
            offsets.append(round(next_offset, 1))

        # 3. Bidirectional balance adjustment if needed
        # In bidirectional coordination, if reverse weight is significant,
        # adjust offsets to minimize reverse bandwidth penalty
        if bidirectional and (1.0 - weight_forward) > 0.2:
            # Check half-cycle tuning: T_travel approx k * (C / 2)
            adjusted_offsets = [0.0]
            for i, tt in enumerate(travel_times):
                k = round(tt / (cycle_length / 2.0))
                # Balanced offset
                ideal_forward = offsets[i+1]
                ideal_reverse = (cycle_length - (tt % cycle_length)) % cycle_length
                blended = (weight_forward * ideal_forward + (1.0 - weight_forward) * ideal_reverse) % cycle_length
                adjusted_offsets.append(round(blended, 1))
            offsets = adjusted_offsets

        # 4. Compute theoretical progression bandwidth
        # Bandwidth is limited by the smallest green split minus dispersion
        min_green = min(green_splits_arterial)
        bandwidth_forward = max(0.0, min_green - 4.0)  # accounting for platoon dispersion
        bandwidth_ratio = round((bandwidth_forward / cycle_length) * 100.0, 1)

        return {
            "cycle_length": cycle_length,
            "progression_speed_kmh": round(progression_speed * 3.6, 1),
            "offsets": offsets,
            "travel_times": [round(tt, 1) for tt in travel_times],
            "bandwidth_seconds": round(bandwidth_forward, 1),
            "bandwidth_ratio_percent": bandwidth_ratio,
            "coordination_quality": "Excellent" if bandwidth_ratio >= 35.0 else ("Good" if bandwidth_ratio >= 20.0 else "Fair")
        }
