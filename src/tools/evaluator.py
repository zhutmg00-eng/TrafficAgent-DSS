"""
TrafficAgent-DSS: Five-Dimensional Traffic Performance Evaluator
Computes comprehensive traffic engineering KPIs and A/B comparative improvements.
"""

from typing import Dict, List, Any
import numpy as np


class PerformanceEvaluator:
    """
    Evaluates simulation performance across 5 key dimensions:
      1. Travel Efficiency: Average Delay (s/veh)
      2. Spatial Congestion: Maximum Queue Length (m)
      3. Capacity Utilization: Network Bottleneck Throughput (veh/h)
      4. Service Reliability: Travel Time Variance (s^2)
      5. Green & Low-Carbon: CO2 Emissions (kg) & Fuel Consumption (L)
    """

    @staticmethod
    def compute_summary_kpi(raw_stats: Dict[str, Any]) -> Dict[str, float]:
        """
        Summarizes raw simulation time-step metrics into standard KPIs.
        Safely guards against explicit None values and empty list metrics.
        """
        delays = raw_stats.get("vehicle_delays")
        if delays is None:
            delays = [0.0]
        else:
            delays = [float(d) for d in delays if d is not None]
            if not delays:
                delays = [0.0]

        queues = raw_stats.get("queue_lengths")
        if queues is None:
            queues = [0.0]
        else:
            queues = [float(q) for q in queues if q is not None]
            if not queues:
                queues = [0.0]

        speeds = raw_stats.get("vehicle_speeds")
        if speeds is None and "bottleneck_speeds_kmh" in raw_stats:
            bn_speeds = raw_stats.get("bottleneck_speeds_kmh")
            if bn_speeds is not None:
                speeds = [float(s) / 3.6 for s in bn_speeds if s is not None]
        if speeds is not None:
            speeds = [float(s) for s in speeds if s is not None]
        if not speeds:
            speeds = [10.0]

        raw_co2 = raw_stats.get("total_co2_mg")
        co2_mg = max(0.0, float(raw_co2)) if raw_co2 is not None else 0.0

        raw_fuel = raw_stats.get("total_fuel_mg")
        if raw_fuel is None:
            raw_fuel = raw_stats.get("total_fuel_ml")
        fuel_mg = max(0.0, float(raw_fuel)) if raw_fuel is not None else 0.0

        raw_trips = raw_stats.get("completed_trips")
        completed_trips = max(0, int(raw_trips)) if raw_trips is not None else 0

        raw_duration = raw_stats.get("simulation_duration")
        sim_duration_sec = max(1.0, float(raw_duration)) if raw_duration is not None else 600.0

        avg_delay = float(np.mean(delays)) if len(delays) > 0 else 0.0
        max_queue = float(np.max(queues)) if len(queues) > 0 else 0.0
        avg_speed_kmh = float(np.mean(speeds)) * 3.6 if len(speeds) > 0 else 0.0
        throughput_vph = round(completed_trips * (3600.0 / sim_duration_sec), 1)
        tt_variance = float(np.var(delays)) if len(delays) > 1 else 0.0
        co2_kg = round(co2_mg / 1e6, 2)
        # SUMO getFuelConsumption returns mg/s; fuel mass is in mg.
        # Density for standard gasoline is ~0.74 kg/L (740,000 mg/L).
        fuel_liters = round((fuel_mg / 1e6) / 0.74, 2) if fuel_mg > 0 else 0.0
        fuel_kg = round(fuel_mg / 1e6, 2)

        return {
            "avg_delay_s": round(avg_delay, 1),
            "max_queue_m": round(max_queue, 1),
            "avg_speed_kmh": round(avg_speed_kmh, 1),
            "throughput_vph": throughput_vph,
            "delay_variance": round(tt_variance, 1),
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
    def compare_schemes(baseline_kpi: Dict[str, float], strategy_kpi: Dict[str, float]) -> Dict[str, Any]:
        """
        Computes improvement percentages between baseline (Do-Nothing) and strategy.
        Positive improvement % means favorable change (delay reduced, throughput increased, etc.).
        """
        def pct_reduction(base: float, strat: float) -> float:
            if base <= 0:
                return 0.0
            return round(((base - strat) / base) * 100.0, 1)

        def pct_increase(base: float, strat: float) -> float:
            if base <= 0:
                return 0.0
            return round(((strat - base) / base) * 100.0, 1)

        delay_improv = pct_reduction(baseline_kpi.get("avg_delay_s") or 0.0, strategy_kpi.get("avg_delay_s") or 0.0)
        queue_improv = pct_reduction(baseline_kpi.get("max_queue_m") or 0.0, strategy_kpi.get("max_queue_m") or 0.0)
        speed_improv = pct_increase(baseline_kpi.get("avg_speed_kmh") or 0.0, strategy_kpi.get("avg_speed_kmh") or 0.0)
        throughput_improv = pct_increase(baseline_kpi.get("throughput_vph") or 0.0, strategy_kpi.get("throughput_vph") or 0.0)
        variance_improv = pct_reduction(baseline_kpi.get("delay_variance") or 0.0, strategy_kpi.get("delay_variance") or 0.0)
        co2_improv = pct_reduction(baseline_kpi.get("co2_emissions_kg") or 0.0, strategy_kpi.get("co2_emissions_kg") or 0.0)

        base_fuel = baseline_kpi.get("fuel_liters") or baseline_kpi.get("fuel_consumption_kg") or 0.0
        strat_fuel = strategy_kpi.get("fuel_liters") or strategy_kpi.get("fuel_consumption_kg") or 0.0
        fuel_improv = pct_reduction(base_fuel, strat_fuel)

        # Radar score normalized to [40, 98] for visualization
        radar_scores = {
            "通行效率 (Delay)": min(98.0, max(40.0, 50.0 + delay_improv * 1.5)),
            "空间治堵 (Queue)": min(98.0, max(40.0, 50.0 + queue_improv * 1.5)),
            "容量释放 (Throughput)": min(98.0, max(40.0, 50.0 + throughput_improv * 2.0)),
            "运行平稳 (Reliability)": min(98.0, max(40.0, 50.0 + variance_improv * 1.2)),
            "绿色低碳 (Carbon)": min(98.0, max(40.0, 50.0 + co2_improv * 2.0)),
        }

        return {
            "delay_improvement_pct": delay_improv,
            "queue_improvement_pct": queue_improv,
            "speed_improvement_pct": speed_improv,
            "throughput_improvement_pct": throughput_improv,
            "variance_improvement_pct": variance_improv,
            "co2_improvement_pct": co2_improv,
            "fuel_improvement_pct": fuel_improv,
            "radar_scores": radar_scores,
            "overall_effectiveness_grade": (
                "卓越 (Level A+)" if delay_improv >= 25.0 and queue_improv >= 25.0
                else ("良好 (Level A)" if delay_improv >= 15.0 else "一般 (Level B)")
            )
        }
