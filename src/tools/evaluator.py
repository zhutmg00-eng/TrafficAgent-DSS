"""
TrafficAgent-DSS: Five-Dimensional Traffic Performance Evaluator
Computes comprehensive traffic engineering KPIs and A/B comparative improvements.
"""

from typing import Any, Dict, List, Optional
import math

import numpy as np


class PerformanceEvaluator:
    """
    Evaluates simulation performance across 5 key dimensions:
      1. Travel Efficiency: Average Delay (s/veh)
      2. Spatial Congestion: Maximum Queue Length (m)
      3. Capacity Utilization: Network Bottleneck Throughput (veh/h)
      4. Service Reliability: Delay time-series variance (s^2)
      5. Green & Low-Carbon: CO2 Emissions (kg) & Fuel Consumption (L)

    NOTE on `delay_variance`: the sandbox samples the *network mean* time-loss every 5 s,
    so this KPI is the variance of that 5-second time series — a measure of how unstable
    the corridor's delay level is over time. It is NOT the per-vehicle travel-time
    variance. The two share the unit s^2 but describe different things, and any external
    write-up must not label this figure "per-vehicle travel time variance".
    """

    @staticmethod
    def _samples(values: Any) -> Optional[List[float]]:
        """
        Extracts usable finite floats from a per-step series, or None when there is
        nothing to average.

        Returning None (rather than a manufactured sample list) is what keeps a missing
        run from turning into a confident-looking "0.0 s delay" or "36 km/h" KPI. An
        earlier revision substituted [0.0] / [10.0] here, so a payload with no data still
        produced a full set of plausible numbers and a derived improvement percentage.
        """
        if values is None:
            return None
        cleaned: List[float] = []
        for v in values:
            if v is None:
                continue
            try:
                f = float(v)
            except (TypeError, ValueError):
                continue
            if math.isfinite(f):
                cleaned.append(f)
        return cleaned or None

    @classmethod
    def compute_summary_kpi(cls, raw_stats: Dict[str, Any]) -> Dict[str, Optional[float]]:
        """
        Summarizes raw simulation time-step metrics into standard KPIs.

        Metrics whose underlying samples are absent are reported as None — the caller
        must render them as "no data" instead of a number that was never measured.
        """
        delays = cls._samples(raw_stats.get("vehicle_delays"))

        queues = cls._samples(raw_stats.get("queue_lengths"))

        speeds = cls._samples(raw_stats.get("vehicle_speeds"))
        if speeds is None:
            # Some sandbox revisions report bottleneck speed in km/h instead of m/s.
            bn_speeds = raw_stats.get("bottleneck_speeds_kmh")
            bn_clean = cls._samples(bn_speeds)
            if bn_clean is not None:
                speeds = [s / 3.6 for s in bn_clean]

        def total(key):
            try:
                value = float(raw_stats.get(key))
            except (TypeError, ValueError):
                return None
            return value if math.isfinite(value) and value >= 0 else None

        co2_mg = total("total_co2_mg")
        fuel_mg = total("total_fuel_mg")
        if fuel_mg is None and "total_fuel_mg" not in raw_stats:
            liters = total("total_fuel_liters")
            fuel_mg = liters * 740000.0 if liters is not None else None
        completed_trips = total("completed_trips")
        if completed_trips is not None and not completed_trips.is_integer():
            completed_trips = None
        sim_duration_sec = total("simulation_duration")

        avg_delay = float(np.mean(delays)) if delays else None
        max_queue = float(np.max(queues)) if queues else None
        avg_speed_kmh = float(np.mean(speeds)) * 3.6 if speeds else None
        throughput_vph = (round(completed_trips * (3600.0 / sim_duration_sec), 1)
                          if completed_trips is not None and sim_duration_sec is not None
                          and sim_duration_sec > 0 else None)
        # Variance of the 5-second network-mean delay series (stability over time),
        # NOT the per-vehicle travel-time variance — see the class docstring. Needs at
        # least two samples to mean anything; a single sample reports None, not 0.0.
        delay_variance = round(float(np.var(delays)), 1) if delays and len(delays) > 1 else None
        co2_kg = round(co2_mg / 1e6, 2) if co2_mg is not None else None
        # SUMO getFuelConsumption returns mg/s; fuel mass is in mg.
        # Density for standard gasoline is ~0.74 kg/L (740,000 mg/L).
        fuel_liters = round((fuel_mg / 1e6) / 0.74, 2) if fuel_mg is not None else None
        fuel_kg = round(fuel_mg / 1e6, 2) if fuel_mg is not None else None

        return {
            "avg_delay_s": round(avg_delay, 1) if avg_delay is not None else None,
            "max_queue_m": round(max_queue, 1) if max_queue is not None else None,
            "avg_speed_kmh": round(avg_speed_kmh, 1) if avg_speed_kmh is not None else None,
            "throughput_vph": throughput_vph,
            "delay_variance": delay_variance,
            "co2_emissions_kg": co2_kg,
            "fuel_liters": fuel_liters,
            "fuel_consumption_kg": fuel_kg,
        }

    @staticmethod
    def baseline_radar_scores() -> Dict[str, float]:
        """Returns normalized baseline scores (all 50.0 at zero improvement)."""
        return {
            "通行效率 (Delay)": 50.0,
            "空间治堵 (Queue)": 50.0,
            "容量释放 (Throughput)": 50.0,
            "运行平稳 (Reliability)": 50.0,
            "绿色低碳 (Carbon)": 50.0,
        }

    @staticmethod
    def _pct_change(base: Optional[float], other: Optional[float], *, increase: bool = False) -> Optional[float]:
        """
        Percentage change of `other` relative to `base`, or None when the baseline is
        unavailable.

        Returning None instead of 0.0 is deliberate: a 0.0% is rendered as
        "持平 (0.0%)", i.e. a claim that both schemes performed identically — when the
        truth is that nothing was measured.
        """
        try:
            b = float(base)  # type: ignore[arg-type]
            o = float(other)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        if not math.isfinite(b) or not math.isfinite(o) or b <= 0:
            return None
        delta = (o - b) if increase else (b - o)
        return round((delta / b) * 100.0, 1)

    @classmethod
    def compare_schemes(cls, baseline_kpi: Dict[str, float], strategy_kpi: Dict[str, float]) -> Dict[str, Any]:
        """
        Computes improvement percentages between baseline (Do-Nothing) and strategy.
        Positive improvement % means favorable change (delay reduced, throughput increased).
        Any metric whose baseline is missing or non-positive reports None rather than 0.0.
        """
        base = baseline_kpi or {}
        strat = strategy_kpi or {}

        delay_improv = cls._pct_change(base.get("avg_delay_s"), strat.get("avg_delay_s"))
        queue_improv = cls._pct_change(base.get("max_queue_m"), strat.get("max_queue_m"))
        speed_improv = cls._pct_change(base.get("avg_speed_kmh"), strat.get("avg_speed_kmh"), increase=True)
        throughput_improv = cls._pct_change(base.get("throughput_vph"), strat.get("throughput_vph"), increase=True)
        variance_improv = cls._pct_change(base.get("delay_variance"), strat.get("delay_variance"))
        co2_improv = cls._pct_change(base.get("co2_emissions_kg"), strat.get("co2_emissions_kg"))

        base_fuel = base.get("fuel_liters")
        if base_fuel is None:
            base_fuel = base.get("fuel_consumption_kg")
        strat_fuel = strat.get("fuel_liters")
        if strat_fuel is None:
            strat_fuel = strat.get("fuel_consumption_kg")
        fuel_improv = cls._pct_change(base_fuel, strat_fuel)

        def _score(improv: Optional[float], weight: float) -> Optional[float]:
            """Radar score normalized to [40, 98], or None when the metric is unknown."""
            if improv is None:
                return None
            return min(98.0, max(40.0, 50.0 + improv * weight))

        radar_scores = {
            "通行效率 (Delay)": _score(delay_improv, 1.5),
            "空间治堵 (Queue)": _score(queue_improv, 1.5),
            "容量释放 (Throughput)": _score(throughput_improv, 2.0),
            "运行平稳 (Reliability)": _score(variance_improv, 1.2),
            "绿色低碳 (Carbon)": _score(co2_improv, 2.0),
        }

        if delay_improv is None:
            grade: Optional[str] = None
        elif delay_improv >= 25.0 and queue_improv is not None and queue_improv >= 25.0:
            grade = "卓越 (Level A+)"
        elif delay_improv >= 15.0:
            grade = "良好 (Level A)"
        else:
            grade = "一般 (Level B)"

        return {
            "delay_improvement_pct": delay_improv,
            "queue_improvement_pct": queue_improv,
            "speed_improvement_pct": speed_improv,
            "throughput_improvement_pct": throughput_improv,
            "variance_improvement_pct": variance_improv,
            "co2_improvement_pct": co2_improv,
            "fuel_improvement_pct": fuel_improv,
            "radar_scores": radar_scores,
            "overall_effectiveness_grade": grade,
        }
