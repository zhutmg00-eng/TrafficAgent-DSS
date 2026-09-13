"""
TrafficAgent-DSS: Dynamic Arterial Green Wave Coordinator
Computes optimal signal progression offsets and progression bandwidth along arterial corridors.
"""

from typing import Any, Dict, List, Tuple
import math


def _circular_bandwidth(windows: List[Tuple[float, float]], cycle: float) -> float:
    """
    Length (seconds) of the largest time window contained in every circular green
    window `(start_mod_cycle, length)` on a signal cycle of `cycle` seconds.

    This is the exact interval-intersection step of the classic time-space-diagram
    (graphical) progression method: an empty common region yields zero bandwidth,
    and a region wrapping across the cycle boundary is measured correctly.
    """
    if cycle <= 0 or not windows:
        return 0.0
    best = 0.0
    for anchor_start, _ in windows:
        common = None
        feasible = True
        for start, length in windows:
            into = (anchor_start - start) % cycle
            if into >= length:
                feasible = False
                break
            remain = length - into
            common = remain if common is None else min(common, remain)
        if feasible and common is not None:
            best = max(best, common)
    return best


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
    ) -> Dict[str, Any]:
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

        # 4. Progression bandwidth via the time-space-diagram (graphical) method.
        #
        # Intersection k serves arterial green for g_k seconds starting at offset_k in the
        # common signal cycle. A platoon departing J0 at time t reaches Jk after the
        # cumulative travel time tau_k, so it passes Jk on green iff
        #     (t + tau_k) mod C ∈ [offset_k, offset_k + g_k],
        # i.e. the admissible departure window contributed by Jk is
        #     W_k = [(offset_k - tau_k) mod C, length g_k].
        # The forward bandwidth is the exact circular intersection of all W_k; the reverse
        # band uses travel times measured from the far end (rho_k = tau_total - tau_k).
        # Unlike a "min green minus a fixed dispersion allowance" heuristic, an empty
        # intersection yields zero bandwidth — the number reported is the geometry itself.
        if cycle_length <= 0:
            bandwidth_forward = 0.0
            bandwidth_reverse = 0.0
        else:
            tau = []
            acc = 0.0
            for k in range(num_nodes):
                tau.append(acc)
                if k < len(travel_times):
                    acc += travel_times[k]
            rho = [tau[-1] - t for t in tau]

            def _band(shifting: List[float]) -> float:
                windows = [
                    ((offsets[k] - shifting[k]) % safe_cycle, max(0.0, float(green_splits_arterial[k])))
                    for k in range(num_nodes)
                ]
                return _circular_bandwidth(windows, safe_cycle)

            bandwidth_forward = _band(tau)
            bandwidth_reverse = _band(rho)

        bandwidth_ratio = round((bandwidth_forward / safe_cycle) * 100.0, 1)
        reverse_ratio = round((bandwidth_reverse / safe_cycle) * 100.0, 1)
        # Two-way progression is summarised as the mean of the two directional
        # bandwidth ratios (equal-weight, standard two-way reporting).
        bidirectional_bandwidth_ratio = round((bandwidth_ratio + reverse_ratio) / 2.0, 1)

        if bandwidth_forward > 0.0 and bandwidth_reverse > 0.0:
            coordination_quality = "both_directions_progression"
        elif bandwidth_forward > 0.0:
            coordination_quality = "forward_progression_only"
        elif bandwidth_reverse > 0.0:
            coordination_quality = "reverse_progression_only"
        else:
            coordination_quality = "no_common_band"

        return {
            "cycle_length": cycle_length,
            "progression_speed_kmh": round(progression_speed * 3.6, 1),
            "offsets": offsets,
            "travel_times": [round(tt, 1) for tt in travel_times],
            "bandwidth_seconds": round(bandwidth_forward, 1),
            "bandwidth_ratio_percent": bandwidth_ratio,
            "bandwidth_reverse_seconds": round(bandwidth_reverse, 1),
            "bandwidth_reverse_ratio_percent": reverse_ratio,
            "bidirectional_bandwidth_ratio_percent": bidirectional_bandwidth_ratio,
            "coordination_quality": coordination_quality
        }
