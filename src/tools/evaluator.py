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
        """
        delays = raw_stats.get("vehicle_delays", [0.0])
        queues = raw_stats.get("queue_lengths", [0.0])
        speeds = raw_stats.get("vehicle_speeds", [10.0])
        co2_mg = raw_stats.get("total_co2_mg", 0.0)
        fuel_ml = raw_stats.get("total_fuel_ml", 0.0)
        completed_trips = raw_stats.get("completed_trips", 0)
        sim_duration_sec = max(1.0, raw_stats.get("simulation_duration", 600.0))

        avg_delay = float(np.mean(delays)) if len(delays) > 0 else 0.0
        max_queue = float(np.max(queues)) if len(queues) > 0 else 0.0
        avg_speed_kmh = float(np.mean(speeds)) * 3.6 if len(speeds) > 0 else 0.0
        throughput_vph = round(completed_trips * (3600.0 / sim_duration_sec), 1)
        tt_variance = float(np.var(delays)) if len(delays) > 1 else 0.0
        co2_kg = round(co2_mg / 1e6, 2)
        fuel_liters = round(fuel_ml / 1e6, 2)

        return {
            "avg_delay_s": round(avg_delay, 1),
            "max_queue_m": round(max_queue, 1),
            "avg_speed_kmh": round(avg_speed_kmh, 1),
            "throughput_vph": throughput_vph,
            "delay_variance": round(tt_variance, 1),
            "co2_emissions_kg": co2_kg,
            "fuel_liters": fuel_liters,
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

        delay_improv = pct_reduction(baseline_kpi["avg_delay_s"], strategy_kpi["avg_delay_s"])
        queue_improv = pct_reduction(baseline_kpi["max_queue_m"], strategy_kpi["max_queue_m"])
        speed_improv = pct_increase(baseline_kpi["avg_speed_kmh"], strategy_kpi["avg_speed_kmh"])
        throughput_improv = pct_increase(baseline_kpi["throughput_vph"], strategy_kpi["throughput_vph"])
        variance_improv = pct_reduction(baseline_kpi["delay_variance"], strategy_kpi["delay_variance"])
        co2_improv = pct_reduction(baseline_kpi["co2_emissions_kg"], strategy_kpi["co2_emissions_kg"])

        # Radar score normalized to [40, 95] for visualization
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
            "radar_scores": radar_scores,
            "overall_effectiveness_grade": (
                "卓越 (Level A+)" if delay_improv >= 25.0 and queue_improv >= 25.0
                else ("良好 (Level A)" if delay_improv >= 15.0 else "一般 (Level B)")
            )
        }
