"""
TrafficAgent-DSS: System Unit & Integration Test Suite
Validates domain tools, decision agent reasoning, and evaluation metrics.
"""

import sys
import unittest
import zlib
from pathlib import Path

# Add project root to sys.path
root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))

from src.tools.webster import WebsterSignalOptimizer
from src.tools.green_wave import GreenWaveCoordinator
from src.tools.rerouting import DynamicReroutingAllocator
from src.tools.evaluator import PerformanceEvaluator
from src.agents.traffic_agent import TrafficDecisionAgent


class TestTrafficAgentDSS(unittest.TestCase):

    def setUp(self):
        self.webster = WebsterSignalOptimizer()
        self.green_wave = GreenWaveCoordinator()
        self.rerouter = DynamicReroutingAllocator()
        self.evaluator = PerformanceEvaluator()
        self.agent = TrafficDecisionAgent()

    def test_webster_timing(self):
        """Tests Webster signal timing optimization."""
        res = self.webster.compute_timing(
            phase_flows=[1200.0, 400.0, 600.0, 300.0],
            phase_lanes=[3, 1, 2, 1]
        )
        self.assertIn("optimal_cycle", res)
        self.assertIn("green_splits", res)
        self.assertTrue(45 <= res["optimal_cycle"] <= 160)
        self.assertEqual(len(res["green_splits"]), 4)
        # Verify effective green split does not exceed cycle
        total_green = sum(res["green_splits"])
        self.assertTrue(total_green <= res["optimal_cycle"])

    def test_green_wave_offsets(self):
        """Tests progression offset calculation."""
        res = self.green_wave.compute_offsets(
            intersection_distances=[300.0, 300.0],
            cycle_length=90.0,
            green_splits_arterial=[45.0, 45.0, 45.0],
            progression_speed=13.89  # 50 km/h
        )
        self.assertIn("offsets", res)
        self.assertEqual(len(res["offsets"]), 3)
        self.assertEqual(res["offsets"][0], 0.0)
        self.assertTrue(res["bandwidth_seconds"] > 0)
        self.assertIn("coordination_quality", res)

    def test_rerouting_diversion(self):
        """Tests dynamic rerouting trigger and capacity bounding."""
        # Non-congested condition: diversion should be 0
        res_free = self.rerouter.calculate_diversion(
            bottleneck_queue_meters=20.0,
            bottleneck_link_length=300.0,
            bottleneck_occupancy=0.35,
            upstream_flow_vph=1200.0,
            bypass_current_occupancy=0.20,
            bypass_spare_capacity_vph=1500.0
        )
        self.assertFalse(res_free["need_diversion"])
        self.assertEqual(res_free["diversion_ratio"], 0.0)

        # Severe congestion condition: diversion should trigger
        res_heavy = self.rerouter.calculate_diversion(
            bottleneck_queue_meters=180.0,
            bottleneck_link_length=300.0,
            bottleneck_occupancy=0.85,
            upstream_flow_vph=1800.0,
            bypass_current_occupancy=0.30,
            bypass_spare_capacity_vph=1200.0
        )
        self.assertTrue(res_heavy["need_diversion"])
        self.assertTrue(0.10 <= res_heavy["diversion_ratio"] <= 0.40)
        self.assertIn("vms_advisory", res_heavy)

    def test_evaluator_metrics(self):
        """Tests 5-dimensional evaluation and A/B comparison."""
        base_stats = {
            "vehicle_delays": [80.0, 85.0, 90.0],
            "queue_lengths": [240.0, 250.0],
            "vehicle_speeds": [2.5, 3.0],
            "total_co2_mg": 180000000.0,
            "total_fuel_ml": 80000.0,
            "completed_trips": 220,
            "simulation_duration": 600.0
        }
        strat_stats = {
            "vehicle_delays": [45.0, 48.0, 44.0],
            "queue_lengths": [100.0, 110.0],
            "vehicle_speeds": [6.0, 6.5],
            "total_co2_mg": 130000000.0,
            "total_fuel_ml": 58000.0,
            "completed_trips": 290,
            "simulation_duration": 600.0
        }

        kpi_base = self.evaluator.compute_summary_kpi(base_stats)
        kpi_strat = self.evaluator.compute_summary_kpi(strat_stats)

        comp = self.evaluator.compare_schemes(kpi_base, kpi_strat)
        self.assertTrue(comp["delay_improvement_pct"] > 35.0)
        self.assertTrue(comp["queue_improvement_pct"] > 50.0)
        self.assertTrue(comp["speed_improvement_pct"] > 50.0)
        self.assertTrue(comp["throughput_improvement_pct"] > 20.0)
        self.assertTrue(comp["co2_improvement_pct"] > 20.0)
        self.assertIn("radar_scores", comp)

    def test_agent_diagnosis_and_strategies(self):
        """Tests Agent perception, CoT diagnosis and strategy synthesis."""
        state = {
            "bottleneck_edge": "J1_J2",
            "queue_m": 165.0,
            "link_length_m": 300.0,
            "speed_kmh": 8.2,
            "occupancy": 0.82,
            "bypass_occupancy": 0.28
        }
        diagnosis = self.agent.diagnose_bottleneck(state)
        self.assertIn("cot_reasoning", diagnosis)
        self.assertTrue(len(diagnosis["cot_reasoning"]) >= 4)
        self.assertEqual(diagnosis["bottleneck_location"], "J1_J2")

        strategies = self.agent.formulate_candidate_strategies(diagnosis)
        self.assertIn("baseline", strategies)
        self.assertIn("strategy_a", strategies)
        self.assertIn("strategy_b", strategies)
        self.assertTrue(strategies["strategy_b"]["green_wave"])
        self.assertTrue(strategies["strategy_b"]["reroute_ratio"] > 0)


    # ------------------------------------------------------------------ #
    # Regression guards for the audited trustworthiness issues (P0/P1).
    # These tests exist so the fixes cannot silently regress.
    # ------------------------------------------------------------------ #

    def test_diversion_bucketing_is_reproducible(self):
        """P0-4 guard: rerouting assignment must not depend on PYTHONHASHSEED."""
        from src.simulation.sumo_sandbox import _stable_bucket

        first = [_stable_bucket(f"veh_{i}_120") for i in range(50)]
        second = [_stable_bucket(f"veh_{i}_120") for i in range(50)]
        self.assertEqual(first, second)
        # Pins the implementation to a cross-process stable algorithm.
        self.assertEqual(_stable_bucket("probe"), zlib.crc32(b"probe"))

    def test_diagnosis_drives_strategy_parameters(self):
        """P1-3 guard: detector state must actually change the optimised parameters."""
        mild = self.agent.diagnose_bottleneck({
            "bottleneck_edge": "J1_J2",
            "queue_m": 60.0,
            "link_length_m": 300.0,
            "speed_kmh": 32.0,
            "occupancy": 0.45,
            "bypass_occupancy": 0.15,
        })
        severe = self.agent.diagnose_bottleneck({
            "bottleneck_edge": "J1_J2",
            "queue_m": 265.0,
            "link_length_m": 300.0,
            "speed_kmh": 6.5,
            "occupancy": 0.95,
            "bypass_occupancy": 0.62,
        })

        s_mild = self.agent.formulate_candidate_strategies(mild)
        s_severe = self.agent.formulate_candidate_strategies(severe)

        mild_ratio = s_mild["reroute_details"]["diversion_ratio"]
        severe_ratio = s_severe["reroute_details"]["diversion_ratio"]

        # The severe incident must not divert less traffic than the mild one.
        self.assertLess(mild_ratio, severe_ratio)
        self.assertGreater(severe_ratio, 0.0)

        # The audit trail must record which detector values were consumed.
        used = s_severe["input_state_used"]
        self.assertAlmostEqual(used["queue_m"], 265.0)
        self.assertAlmostEqual(used["bottleneck_occupancy"], 0.95)
        self.assertIn("diagnosis", used["source"])

    def test_control_switches_are_forwarded_to_the_sandbox(self):
        """P0-3 guard: A/B differences must come from controls that really reach SUMO."""
        captured = []

        class _RecordingSandbox:
            def run_simulation(self, scheme, duration, incident_start,
                               incident_end, control_params):
                captured.append({"scheme": scheme, "ctl": dict(control_params)})
                return {
                    "scheme": scheme,
                    "simulation_duration": duration,
                    "time_stamps": [0, 5, 10],
                    "queue_lengths": [200.0, 180.0, 160.0],
                    "vehicle_speeds": [3.0, 4.0, 5.0],
                    "bottleneck_speeds_kmh": [10.8, 14.4, 18.0],
                    "vehicle_delays": [70.0, 60.0, 50.0],
                    "completed_trips": 100,
                    "total_co2_mg": 1.0e8,
                    "total_fuel_ml": 5.0e4,
                    "control_evidence": {"scheme": scheme},
                }

        original = self.agent.sandbox
        self.agent.sandbox = _RecordingSandbox()
        try:
            self.agent.execute_what_if_rollout(
                duration=30, incident_start=5, incident_end=20
            )
        finally:
            self.agent.sandbox = original

        self.assertEqual(len(captured), 3)
        by_scheme = {c["scheme"]: c["ctl"] for c in captured}

        self.assertFalse(by_scheme["baseline"]["webster"])
        self.assertFalse(by_scheme["baseline"]["green_wave"])
        self.assertEqual(by_scheme["baseline"]["reroute_ratio"], 0.0)

        self.assertTrue(by_scheme["webster"]["webster"])
        self.assertFalse(by_scheme["webster"]["green_wave"])

        self.assertTrue(by_scheme["agent_dss"]["webster"])
        self.assertTrue(by_scheme["agent_dss"]["green_wave"])
        self.assertGreater(by_scheme["agent_dss"]["reroute_ratio"], 0.0)

        # Green-wave offsets must be real numbers, not an empty placeholder.
        self.assertTrue(by_scheme["agent_dss"]["green_wave_offsets"])

    def test_no_fabricated_kpis_when_rollout_is_missing(self):
        """P0-2 guard: the brief must declare missing data instead of inventing numbers."""
        state = {
            "bottleneck_edge": "J1_J2",
            "queue_m": 165.0,
            "link_length_m": 300.0,
            "speed_kmh": 8.2,
            "occupancy": 0.82,
            "bypass_occupancy": 0.28,
        }
        diagnosis = self.agent.diagnose_bottleneck(state)
        strategies = self.agent.formulate_candidate_strategies(diagnosis)
        report = self.agent.generate_decision_report(diagnosis, strategies, {})

        for fabricated in ("44.6", "56.6", "138.3", "Level A+"):
            self.assertNotIn(fabricated, report)
        self.assertIn("不可用", report)


if __name__ == "__main__":
    unittest.main()
