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

        # Signal program must be a real, deployable plan with non-zero green times.
        sp_b = by_scheme["agent_dss"]["signal_program"]
        self.assertGreater(sp_b["green_main"], 0)
        self.assertGreater(sp_b["green_cross"], 0)
        # The coordinated plan must carry real phase offsets (3 junctions), not placeholders.
        self.assertEqual(len(sp_b["first_green_start"]), 3)
        self.assertTrue(any(v > 0 for v in sp_b["first_green_start"]))
        # The single-point plan must NOT be phase-shifted.
        sp_a = by_scheme["webster"]["signal_program"]
        self.assertEqual(sp_a["first_green_start"], [0.0, 0.0, 0.0])
        # Same Webster timing underneath both plans.
        self.assertAlmostEqual(sp_a["cycle_length"], sp_b["cycle_length"], places=1)

    def test_phase_alignment_math(self):
        """Signal-phase alignment: arterial green must start exactly at the requested offset.

        This is the mechanism that realises the green-wave offset without TraCI offset
        support, so its correctness is load-bearing for the coordinated strategy.
        """
        from src.simulation.sumo_sandbox import _compute_phase_alignment

        C = 45.0
        durs = [24.0, 4.0, 13.0, 4.0]  # arterial / yellow / cross / yellow

        # Offset 0 -> phase 0 begins at t=0 with the full green left to run.
        idx, rem = _compute_phase_alignment(C, durs, 0.0)
        self.assertEqual(idx, 0)
        self.assertAlmostEqual(rem, 24.0)

        # Offset 21.6 -> program sits 23.4 s into its cycle: 0.6 s of green remain,
        # then yellow+cross+yellow, so the arterial green starts 21.6 s later.
        idx, rem = _compute_phase_alignment(C, durs, 21.6)
        self.assertEqual(idx, 0)
        self.assertAlmostEqual(rem, 0.6)
        self.assertAlmostEqual(rem + 4 + 13 + 4, 21.6)

        # Offset 5.0 -> inside the cross-street green (1 s left, then a 4 s yellow).
        idx, rem = _compute_phase_alignment(C, durs, 5.0)
        self.assertEqual(idx, 2)
        self.assertAlmostEqual(rem, 1.0)
        self.assertAlmostEqual(rem + 4, 5.0)

    def test_webster_plan_matches_network_phase_structure(self):
        """The deployed plan must match the network's signal structure (2 release phases).

        Guard for the "Webster computed 4 phases against a 2-phase network" defect:
        the plan's phase count must equal the corridor's actual release phases, the
        clock cycle of the deployed program must equal the planned design cycle, and
        the plan must keep the adaptive (actuated) bounds so the controller can respond
        to the incident (static timing can never beat the adaptive baseline it replaces).
        """
        plan = self.agent._tool_plan(None)
        timing = plan["timing"]
        self.assertEqual(len(timing["green_splits"]), 2)

        sp = plan["signal_program"]
        clock_cycle = sp["green_main"] + 2 * sp["yellow"] + sp["green_cross"]
        self.assertAlmostEqual(clock_cycle, sp["cycle_length"], places=1)
        self.assertAlmostEqual(sp["cycle_length"], plan["actual_cycle"], places=1)

        # Over-saturation correction: design cycle = 2x Webster minimum, clamped to [60, 120].
        expected_cycle = min(120.0, max(60.0, round(timing["optimal_cycle"] * 2.0)))
        self.assertAlmostEqual(sp["cycle_length"], expected_cycle, places=0)

        # The controller must stay adaptive: type actuated with real min/max bounds.
        self.assertEqual(sp["type"], "actuated")
        self.assertLess(sp["min_green_main"], sp["green_main"])
        self.assertGreater(sp["max_green_main"], sp["green_main"])
        self.assertLess(sp["min_green_cross"], sp["green_cross"])
        self.assertGreater(sp["max_green_cross"], sp["green_cross"])

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

        what_if_section = report.split("### 二、")[1].split("### 三、")[0]
        for fabricated in ("44.6", "56.6", "138.3", "Level A+"):
            self.assertNotIn(fabricated, what_if_section)
        for fabricated_pct in ("44.6%", "56.6%", "138.3%", "Level A+"):
            self.assertNotIn(fabricated_pct, report)
        self.assertIn("不可用", report)

    def test_decision_report_negative_percentage_formatting_and_speed_note(self):
        """Guard against contradictory text like negative improvements and ensure speed tradeoff note is present."""
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

        # Mock realistic SUMO 1.27.1 verified rollout results where speed slightly decreases (-9.5%)
        mock_rollout = {
            "execution_mode": "physical_sumo_sandbox",
            "kpis": {
                "baseline": {
                    "avg_delay_s": 28.7,
                    "max_queue_m": 157.5,
                    "avg_speed_kmh": 31.7,
                    "throughput_vph": 5628.0,
                    "delay_variance": 393.2,
                    "co2_emissions_kg": 236.6,
                },
                "strategy_a": {
                    "avg_delay_s": 26.2,
                    "max_queue_m": 97.5,
                    "avg_speed_kmh": 30.0,
                    "throughput_vph": 5772.0,
                    "delay_variance": 260.5,
                    "co2_emissions_kg": 243.9,
                },
                "strategy_b": {
                    "avg_delay_s": 23.2,
                    "max_queue_m": 105.0,
                    "avg_speed_kmh": 28.7,
                    "throughput_vph": 5808.0,
                    "delay_variance": 191.4,
                    "co2_emissions_kg": 231.7,
                },
            },
            "comparisons": {
                "strategy_b": {
                    "delay_improvement_pct": 19.2,
                    "queue_improvement_pct": 33.3,
                    "speed_improvement_pct": -9.5,
                    "throughput_improvement_pct": 3.2,
                    "variance_improvement_pct": 51.3,
                    "co2_improvement_pct": 2.1,
                    "overall_effectiveness_grade": "良好 (Level A)",
                }
            },
        }

        report = self.agent.generate_decision_report(diagnosis, strategies, mock_rollout)

        # Must not contain contradictory phrasing like "提升 -" or "减排 -"
        self.assertNotIn("提升 -", report)
        self.assertNotIn("减排 -", report)
        self.assertNotIn("降低 -", report)
        self.assertIn("下降 9.5%", report)
        self.assertIn("降低 19.2%", report)
        # Explanatory note for the speed tradeoff must be included
        self.assertIn("速度指标说明", report)
        self.assertIn("巡航车队", report)

    def test_strategy_b_fallback_rationale_populated(self):
        """Strategy B must always contain non-empty rationale even in deterministic fallback mode."""
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
        strat_b = strategies["strategy_b"]

        self.assertIn("rationale", strat_b)
        self.assertIsInstance(strat_b["rationale"], list)
        self.assertGreaterEqual(len(strat_b["rationale"]), 3)
        self.assertTrue(any("时域" in r for r in strat_b["rationale"]))

    def test_multi_seed_evaluation_and_seed_forwarding(self):
        """Validates that seeds are accepted, forwarded, and aggregated into statistical metrics."""
        captured_seeds = []

        class _MockMultiSeedSandbox:
            def run_simulation(self, scheme, duration, incident_start, incident_end, control_params, seed=None):
                captured_seeds.append(seed)
                delay_offset = (seed or 0) % 5 * 0.5
                return {
                    "scheme": scheme,
                    "simulation_duration": duration,
                    "seed": seed,
                    "time_stamps": [0, 5],
                    "queue_lengths": [100.0, 90.0],
                    "vehicle_speeds": [8.0, 9.0],
                    "bottleneck_speeds_kmh": [28.8, 32.4],
                    "vehicle_delays": [25.0 + delay_offset, 22.0 + delay_offset],
                    "completed_trips": 950,
                    "total_co2_mg": 2.3e8,
                    "total_fuel_mg": 7.0e7,
                    "control_evidence": {"scheme": scheme, "seed": seed},
                }

        original = self.agent.sandbox
        self.agent.sandbox = _MockMultiSeedSandbox()
        try:
            seeds = [10, 20, 30]
            summary = self.agent.run_multi_seed_evaluation(seeds=seeds, duration=60, incident_start=10, incident_end=30)
            self.assertEqual(summary["sample_size"], 3)
            self.assertIn("summary_by_scheme", summary)
            self.assertIn("strategy_b_improvements", summary)
            self.assertIn("mean", summary["summary_by_scheme"]["strategy_b"]["avg_delay_s"])
            self.assertIn("std", summary["summary_by_scheme"]["strategy_b"]["avg_delay_s"])
            # All 3 seeds should have been run across 3 schemes (3 * 3 = 9 calls)
            self.assertEqual(len(captured_seeds), 9)
            self.assertTrue(all(s in captured_seeds for s in seeds))
        finally:
            self.agent.sandbox = original

    def test_green_wave_bidirectional_and_boundary_safety(self):
        """Validates bidirectional bandwidth metrics and division by zero protection."""
        # Zero cycle safety
        res_zero = self.green_wave.compute_offsets(
            intersection_distances=[300.0],
            cycle_length=0.0,
            green_splits_arterial=[30.0, 30.0]
        )
        self.assertEqual(res_zero["bandwidth_ratio_percent"], 0.0)

        # Standard bidirectional progression
        res = self.green_wave.compute_offsets(
            intersection_distances=[300.0, 300.0],
            cycle_length=90.0,
            green_splits_arterial=[50.0, 50.0, 50.0],
            bidirectional=True,
            weight_forward=0.6
        )
        self.assertIn("bandwidth_reverse_seconds", res)
        self.assertIn("bidirectional_bandwidth_ratio_percent", res)
        self.assertGreater(res["bandwidth_reverse_seconds"], 0)
        self.assertGreater(res["bidirectional_bandwidth_ratio_percent"], 0)

    def test_webster_and_rerouting_boundary_safety(self):
        """Validates zero division guards and threshold boundaries."""
        # Webster with minimum green
        w_res = self.webster.compute_timing(
            phase_flows=[2000.0, 1000.0],
            phase_lanes=[3, 2]
        )
        self.assertTrue(all(dos >= 0 for dos in w_res["degree_of_saturation"]))

        # Rerouting with extreme thresholds 1.0 (no division by zero)
        strict_rerouter = DynamicReroutingAllocator(occupancy_threshold=1.0, spillback_queue_ratio=1.0)
        r_res = strict_rerouter.calculate_diversion(
            bottleneck_queue_meters=200.0,
            bottleneck_link_length=200.0,
            bottleneck_occupancy=1.0,
            upstream_flow_vph=1500.0,
            bypass_current_occupancy=0.3,
            bypass_spare_capacity_vph=1000.0
        )
        self.assertTrue(r_res["need_diversion"])
        self.assertGreater(r_res["diversion_ratio"], 0.0)

    def test_evaluator_fuel_and_speed_input_robustness(self):
        """Validates fuel consumption conversion and alternate speed key handling."""
        raw = {
            "vehicle_delays": [20.0, 24.0],
            "queue_lengths": [80.0],
            "bottleneck_speeds_kmh": [36.0],  # vehicle_speeds key omitted intentionally
            "total_co2_mg": 2.3e8,
            "total_fuel_mg": 7.4e7,  # 74 kg of fuel
            "completed_trips": 100,
            "simulation_duration": 600.0
        }
        kpi = self.evaluator.compute_summary_kpi(raw)
        self.assertEqual(kpi["avg_speed_kmh"], 36.0)
        self.assertEqual(kpi["fuel_consumption_kg"], 74.0)
        # 74 kg / 0.74 kg/L = 100.0 Liters
        self.assertEqual(kpi["fuel_liters"], 100.0)
        self.assertEqual(kpi["co2_emissions_kg"], 230.0)

    def test_green_wave_cumulative_reverse_offset(self):
        """Checks that reverse progression uses cumulative travel times across links."""
        # 3 links of 300m each at 13.89 m/s (travel time 21.6s each)
        gw = GreenWaveCoordinator(default_progression_speed=13.89)
        res = gw.compute_offsets(
            intersection_distances=[300.0, 300.0, 300.0],
            cycle_length=90.0,
            green_splits_arterial=[45.0, 45.0, 45.0, 45.0],
            bidirectional=True,
            weight_forward=0.5,
        )
        offsets = res["offsets"]
        # Offsets should be strictly progressive and distinct
        self.assertEqual(len(offsets), 4)
        self.assertEqual(offsets[0], 0.0)
        self.assertNotEqual(offsets[1], offsets[2])
        self.assertNotEqual(offsets[2], offsets[3])

    def test_webster_four_phase_cycle_feasibility(self):
        """Checks 4-phase minimum cycle feasibility: C >= L + sum(g_min) = 14s + 40s = 54s."""
        w = WebsterSignalOptimizer(
            min_cycle=45.0,
            lost_time_per_phase=3.5,
            min_green=10.0,
        )
        # Very low demand on 4 phases
        res = w.compute_timing(
            phase_flows=[100.0, 100.0, 100.0, 100.0],
            phase_lanes=[2, 2, 2, 2],
        )
        # Cycle length must be at least 54.0s (14s lost + 4 * 10s min green)
        self.assertGreaterEqual(res["optimal_cycle"], 54.0)
        self.assertEqual(sum(res["green_splits"]) + res["total_lost_time"], res["optimal_cycle"])
        for g in res["green_splits"]:
            self.assertGreaterEqual(g, 10.0)

    def test_multi_seed_evaluation_validation_and_statistics(self):
        """Checks that invalid seed lists are rejected and multi-seed stats include SEM and CI."""
        # Empty seeds list must raise ValueError
        with self.assertRaises(ValueError):
            self.agent.run_multi_seed_evaluation(seeds=[])

        # Negative seed must raise ValueError
        with self.assertRaises(ValueError):
            self.agent.run_multi_seed_evaluation(seeds=[-1, 42])

        class _MockTwoSeedSandbox:
            def run_simulation(self, scheme, duration, incident_start, incident_end, control_params, seed=None):
                delay = 30.0 if scheme == "baseline" else 20.0 + (seed or 0) * 0.1
                return {
                    "scheme": scheme,
                    "simulation_duration": duration,
                    "seed": seed,
                    "time_stamps": [0, 5],
                    "queue_lengths": [100.0],
                    "vehicle_speeds": [10.0],
                    "vehicle_delays": [delay],
                    "completed_trips": 500,
                    "total_co2_mg": 1e8,
                    "total_fuel_mg": 3e7,
                    "control_evidence": {"seed": seed},
                }

        orig_sandbox = self.agent.sandbox
        self.agent.sandbox = _MockTwoSeedSandbox()
        try:
            res = self.agent.run_multi_seed_evaluation(seeds=[10, 20])
            self.assertEqual(res["sample_size"], 2)
            strat_b_stat = res["strategy_b_improvements"]["delay_improvement_pct"]
            self.assertIn("sem", strat_b_stat)
            self.assertIn("ci_95", strat_b_stat)
            self.assertEqual(len(strat_b_stat["ci_95"]), 2)
            self.assertLessEqual(strat_b_stat["ci_95"][0], strat_b_stat["ci_95"][1])
        finally:
            self.agent.sandbox = orig_sandbox

    def test_evaluator_fuel_comparison_and_baseline_scores(self):
        """Validates fuel improvement calculation and neutral baseline radar scores."""
        base_kpi = {
            "avg_delay_s": 60.0,
            "max_queue_m": 150.0,
            "avg_speed_kmh": 20.0,
            "throughput_vph": 1200.0,
            "delay_variance": 300.0,
            "co2_emissions_kg": 150.0,
            "fuel_liters": 50.0,
        }
        strat_kpi = {
            "avg_delay_s": 45.0,
            "max_queue_m": 100.0,
            "avg_speed_kmh": 25.0,
            "throughput_vph": 1400.0,
            "delay_variance": 150.0,
            "co2_emissions_kg": 120.0,
            "fuel_liters": 40.0,
        }
        comp = self.evaluator.compare_schemes(base_kpi, strat_kpi)
        self.assertIn("fuel_improvement_pct", comp)
        # 50L -> 40L is 20% reduction
        self.assertEqual(comp["fuel_improvement_pct"], 20.0)

        # Baseline radar scores should be all 50.0
        baseline_radar = PerformanceEvaluator.baseline_radar_scores()
        for dim, score in baseline_radar.items():
            self.assertEqual(score, 50.0)


if __name__ == "__main__":
    unittest.main()

