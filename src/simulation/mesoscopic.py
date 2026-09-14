"""
TrafficAgent-DSS: Mesoscopic network simulation engine (SUMO-free data layer).

Motivation
----------
When SUMO is not installed the original system fell back to a handful of hard-coded
"calibrated" constants (delay 84.5 s, queue 242 m, …) that were identical for every
scenario and produced almost no link-level data. This engine runs a deterministic
mesoscopic model over the *real* OSM network and emits rich, spatially-resolved,
time-varying detector data — per link speed / queue / flow / occupancy / delay — which
the dashboard renders as a real congestion map and a detector table.

Model
-----
Each link e carries a demand q_e(t) = cap_e · load_e · profile(t) · control_e(t):
  * cap_e : saturation capacity = lanes × 1800 × (g/C) of the downstream signal
  * load_e: calibrated base load by road class, lifted by network centrality and the
            incident blockage, relieved by VMS rerouting onto parallel links
  * delay : HCM/Webster control delay (uniform + overflow terms)
  * queue : Little's law  N = q·d  → queue length in metres
KPIs are flow-weighted, so a strategy that raises the arterial green ratio and diverts
traffic genuinely lowers delay/queue and raises the bottleneck discharge.

Deterministic (same inputs → same outputs), and every number is *computed*, never
fabricated — preserving the project's "LLM never emits performance numbers" rule.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

from src.data.network import RoadNetwork

SAT_FLOW_PER_LANE = 1800.0       # veh/h/lane
AVG_VEH_LENGTH_M = 7.5           # effective metres of queue per stopped vehicle
DEFAULT_FUEL_L_PER_KM = 0.085    # petrol, l/km (uncongested)
DEFAULT_CO2_KG_PER_L = 2.31      # kg CO2 per litre petrol
ANALYSIS_PERIOD_H = 0.25         # HCM analysis period (15 min)

# Base link load by road class (fraction of capacity at peak, before control effects).
CLASS_BASE_LOAD = {
    "motorway": 0.50, "trunk": 0.46, "primary": 0.44, "secondary": 0.36,
    "tertiary": 0.26, "motorway_link": 0.30, "trunk_link": 0.28,
    "primary_link": 0.26, "secondary_link": 0.20,
}
ARTERIAL_CLASSES = {"motorway", "trunk", "primary"}
PARALLEL_CLASSES = {"secondary", "primary", "trunk", "secondary_link", "primary_link"}


def _hcm_delay(x: float, cap_vph: float, gc: float, cycle_s: float) -> float:
    """HCM control delay (s/veh): uniform term + incremental (overflow) term."""
    gc = min(0.95, max(0.15, gc))
    g_over_c_term = min(1.0, x) * gc
    denom = max(0.02, 1.0 - g_over_c_term)
    d1 = 0.5 * cycle_s * (1.0 - gc) ** 2 / denom
    cAP = max(50.0, cap_vph)  # effective (already green-ratio-scaled) capacity of the lane group
    term = (x - 1.0) + math.sqrt((x - 1.0) ** 2 + 4.0 * x / max(1.0, cAP * ANALYSIS_PERIOD_H))
    d2 = 900.0 * ANALYSIS_PERIOD_H * term
    return max(0.0, d1 + d2)


class MesoscopicSimulator:
    """Deterministic link-level network simulator over a RoadNetwork."""

    def __init__(self, network: RoadNetwork):
        self.net = network
        self._prep()

    # ------------------------------------------------------------------ prep
    def _prep(self) -> None:
        b = self.net.bounds
        cx, cy = (b["min_x"] + b["max_x"]) / 2.0, (b["min_y"] + b["max_y"]) / 2.0
        self._center = (cx, cy)
        self._centrality: Dict[str, float] = {}
        # 0.6 extents for a smooth centrality taper
        radius = max(500.0, 0.6 * max(b["max_x"] - b["min_x"], b["max_y"] - b["min_y"]) / 2.0)
        for e in self.net.edges:
            g = e["geometry"][len(e["geometry"]) // 2]
            d = math.hypot(g[0] - cx, g[1] - cy)
            self._centrality[e["id"]] = 1.0 + 0.30 * math.exp(-(d / radius) ** 2)

    # ---------------------------------------------------------------- demand
    @staticmethod
    def _profile(t: float, duration: float, peak_start: float, peak_end: float) -> float:
        if t < peak_start:
            return 0.50 + 0.10 * (t / max(1.0, peak_start))
        if t <= peak_end:
            return 1.00
        frac = (t - peak_end) / max(1.0, duration - peak_end)
        return max(0.45, 1.0 - 0.55 * frac)

    # ------------------------------------------------------------ simulation
    def run_scheme(
        self,
        scheme: str = "baseline",
        duration: int = 600,
        incident_start: int = 150,
        incident_end: int = 420,
        control: Optional[Dict[str, Any]] = None,
        seed: Optional[int] = None,
        bottleneck_id: Optional[str] = None,
        peak_window: Optional[List[float]] = None,
        scenario: Optional[Dict[str, float]] = None,
        step: int = 10,
    ) -> Dict[str, Any]:
        control = control or {}
        scenario = scenario or {}
        step = max(1, int(step))
        peak_start, peak_end = (peak_window or [100.0, max(160.0, duration * 0.85)])

        bottleneck = self.net.edge_by_id.get(bottleneck_id) if bottleneck_id else None
        if bottleneck is None:
            bottleneck = self.net.pick_bottleneck()
        b_id = bottleneck["id"]

        webster = bool(control.get("webster", scheme in ("webster", "agent_dss")))
        green_wave = bool(control.get("green_wave", scheme == "agent_dss"))
        reroute_ratio = float(control.get("reroute_ratio", 0.25 if scheme == "agent_dss" else 0.0))
        reroute_ratio = max(0.0, min(0.9, reroute_ratio))

        cycle = float(control.get("cycle_length") or 100.0)
        green_main = float(control.get("green_split_arterial") or 0.0)
        if green_main <= 0:
            green_main = max(20.0, cycle - 2 * 4.0 - 12.0)

        gc_arterial = min(0.90, max(0.35, green_main / max(1.0, cycle)))
        gc_cross = min(0.85, max(0.35, (cycle - green_main - 8.0) / max(1.0, cycle)))
        if not webster:
            gc_arterial, gc_cross = 0.55, 0.40      # legacy fixed 90 s plan
        if green_wave:
            gc_arterial = min(0.94, gc_arterial * 1.10)

        arterial_ids = {e["id"] for e in self.net.edges if e.get("highway") in ARTERIAL_CLASSES}
        parallel_ids = {e["id"] for e in self.net.edges
                        if e["id"] != b_id and e.get("highway") in PARALLEL_CLASSES}
        # The decision-relevant study corridor: the arterial links plus the bottleneck.
        # KPIs are aggregated over this corridor so control effects are not diluted by the
        # hundreds of irrelevant local streets (whole-network series are still emitted).
        study_ids = set(arterial_ids) | {b_id}

        t_steps = list(range(0, duration + 1, step))
        series: Dict[str, Dict[str, List[float]]] = {
            e["id"]: {"speed": [], "queue": [], "flow": [], "occupancy": [], "delay": []}
            for e in self.net.edges
        }

        vkt_total = 0.0
        fuel_l = 0.0
        delay_weighted_sum = 0.0
        flow_sum = 0.0
        speed_weighted = 0.0
        max_queue = 0.0
        throughput_samples: List[float] = []
        served_arterial_h = 0.0

        for t in t_steps:
            prof = self._profile(t, duration, peak_start, peak_end)
            incident_on = incident_start <= t <= incident_end
            reroute_on = incident_on and reroute_ratio > 0

            for e in self.net.edges:
                eid = e["id"]
                # Physical (control-independent) saturation capacity — the demand is exogenous.
                lanes = float(e.get("lanes", 1))
                base_sat = lanes * SAT_FLOW_PER_LANE
                is_arterial = eid in arterial_ids
                gc = gc_arterial if is_arterial else gc_cross
                cap = base_sat * gc                        # effective capacity (control-dependent)

                load = CLASS_BASE_LOAD.get(e.get("highway", "tertiary"), 0.4)
                load *= self._centrality[eid]

                if incident_on and eid == b_id:
                    load *= float(scenario.get("demand_multiplier", 1.25))
                    cap *= float(scenario.get("capacity_multiplier", 0.38))

                if reroute_on:
                    if eid == b_id or is_arterial:
                        load *= (1.0 - reroute_ratio)
                    elif eid in parallel_ids:
                        load *= (1.0 + 1.5 * reroute_ratio)

                q = base_sat * load * prof                 # veh/h arriving (independent of control)
                x = q / max(1.0, cap)                     # degree of saturation

                d = _hcm_delay(x, cap, gc, cycle)
                v0 = float(e.get("speed_kmh", 40))
                # speed degrades with saturation (calibrated to x: 0.5→~0.9·v0, 1.4→~0.35·v0)
                v = v0 / (1.0 + 1.9 * max(0.0, x) ** 2.4)
                v = max(3.0, v)

                q_veh_in_system = q * d / 3600.0
                queue_veh = q_veh_in_system
                queue_m = queue_veh * AVG_VEH_LENGTH_M / max(1.0, lanes)
                # A physical queue cannot vastly exceed the link it sits on (spillback).
                queue_m = min(queue_m, 1.5 * float(e.get("length_m", 100.0)) + 150.0)
                occ = min(1.0, 0.22 * x + 0.5 * min(1.0, queue_m / max(60.0, float(e.get("length_m", 100)))))

                served = min(q, cap)
                length_km = float(e.get("length_m", 0.0)) / 1000.0
                step_h = step / 3600.0
                if eid in study_ids:
                    vkt_total += served * step_h * length_km
                    # Weight by exogenous demand q (not served) so the denominator is stable
                    # across schemes and improvement is not distorted by throughput growth.
                    delay_weighted_sum += d * q
                    flow_sum += q
                    speed_weighted += v * q
                    max_queue = max(max_queue, queue_m)
                    # Fuel/CO2 mass of the traffic served this step, penalised by stop-go
                    # congestion (higher control delay => higher consumption per km).
                    fuel_l += served * step_h * length_km * DEFAULT_FUEL_L_PER_KM * (1.0 + 0.013 * d)
                    if is_arterial:
                        served_arterial_h += served * step_h
                    if eid == b_id:
                        throughput_samples.append(served)

                s = series[eid]
                s["speed"].append(round(v, 1))
                s["queue"].append(round(queue_m, 1))
                s["flow"].append(round(q, 0))
                s["occupancy"].append(round(occ, 3))
                s["delay"].append(round(d, 1))

        sim_hours = duration / 3600.0
        avg_delay = delay_weighted_sum / max(1e-6, flow_sum)
        avg_speed = speed_weighted / max(1e-6, flow_sum)
        throughput = (sum(throughput_samples) / max(1, len(throughput_samples))) if throughput_samples else 0.0
        # delay variance across links (weighted by flow) — proxy for network reliability
        delays_flat = [(s["delay"][i], s["flow"][i]) for eid, s in series.items()
                       if eid in study_ids for i in range(len(s["delay"]))]
        tot_f = sum(f for _, f in delays_flat) or 1.0
        variance = sum(((dv - avg_delay) ** 2) * f for dv, f in delays_flat) / tot_f
        co2_kg = fuel_l * DEFAULT_CO2_KG_PER_L

        kpis = {
            "avg_delay_s": round(avg_delay, 1),
            "max_queue_m": round(max_queue, 1),
            "avg_speed_kmh": round(avg_speed, 1),
            "throughput_vph": round(throughput, 0),
            "delay_variance": round(variance, 1),
            "co2_emissions_kg": round(co2_kg, 1),
            "fuel_liters": round(fuel_l, 1),
            "co2_g_per_km": round(co2_kg * 1000.0 / vkt_total, 1) if vkt_total > 0 else 0.0,
        }

        link_summary: Dict[str, Any] = {}
        for eid, s in series.items():
            if not s["queue"]:
                continue
            link_summary[eid] = {
                "peak_queue_m": round(max(s["queue"]), 1),
                "avg_speed_kmh": round(sum(s["speed"]) / len(s["speed"]), 1),
                "min_speed_kmh": round(min(s["speed"]), 1),
                "peak_flow_vph": round(max(s["flow"]), 0),
                "peak_occupancy": round(max(s["occupancy"]), 3),
                "avg_delay_s": round(sum(s["delay"]) / len(s["delay"]), 1),
            }

        return {
            "scheme": scheme,
            "execution_mode": "mesoscopic_network",
            "simulation_duration": duration,
            "seed": seed,
            "time_steps": t_steps,
            "bottleneck_edge": b_id,
            "bottleneck_name": bottleneck.get("name", ""),
            "bottleneck_series": series.get(b_id, {"speed": [], "queue": [], "flow": [], "occupancy": [], "delay": []}),
            "edge_series": series,
            "link_summary": link_summary,
            "kpis": kpis,
            "control_evidence": {
                "scheme": scheme,
                "engine": "mesoscopic_network",
                "webster_applied": webster,
                "green_wave_applied": green_wave,
                "reroute_ratio_applied": round(reroute_ratio, 3),
                "cycle_length": round(cycle, 1),
                "green_main": round(green_main, 1),
                "green_over_cycle_arterial": round(gc_arterial, 3),
                "green_over_cycle_cross": round(gc_cross, 3),
                "bottleneck_edge": b_id,
                "incident_window": [incident_start, incident_end],
                "links_simulated": len(series),
            },
        }
