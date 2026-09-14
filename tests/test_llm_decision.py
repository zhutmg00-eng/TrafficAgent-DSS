"""
TrafficAgent-DSS: LLM Control-Decision Layer Test Suite

Covers the layer that puts the LLM *inside* the control loop
(`src/agents/llm_decision.py`) and its integration points:

1. Schema validation — missing fields, wrong types, non-finite numbers, non-dict payloads.
2. Physical feasibility clipping — every variable bounded, cross-street minimum green
   preserved, diversion capped by bypass spare capacity, and each clip audited.
3. Numeric provenance guard — a rationale quoting numbers the tools never supplied must
   be rejected, never published as a checkable-but-false claim.
4. Graceful degradation — unconfigured / SDK-missing / malformed model output yields no
   deployable policy and an explicit mode, never an invented parameter set.
5. Toolchain integration — `_tool_plan(policy=None)` is unchanged; an overlay actually
   moves the deployed values; `apply_diversion_override` keeps derived fields consistent.
6. Deployed-control fidelity — `build_control_params` is the single source of truth for
   what reaches the simulator, pinned against the legacy hand-built dict.
"""

import unittest
from typing import Any, Dict, Optional

from src.agents.llm_decision import (
    HARD_CYCLE_MAX_S,
    HARD_CYCLE_MIN_S,
    LLMDecisionLayer,
    MIN_CROSS_GREEN_S,
    POLICY_BOUNDS,
    YELLOW_TIME_S,
    _rationale_quotes_only_known_numbers,
    clip_policy,
    validate_payload,
)
from src.agents.llm_client import LLMReasoningClient
from src.agents.traffic_agent import TrafficDecisionAgent
from src.tools.rerouting import DynamicReroutingAllocator

DETECTOR_STATE = {
    "queue_m": 165.0,
    "link_length_m": 300.0,
    "occupancy": 0.82,
    "bypass_occupancy": 0.28,
}

DIAGNOSIS: Dict[str, Any] = {"input_state": DETECTOR_STATE}

# A well-formed model proposal sitting comfortably inside the feasible domain.
GOOD_PAYLOAD: Dict[str, Any] = {
    "target_cycle_s": 95.0,
    "arterial_green_share": 0.70,
    "reroute_ratio": 0.20,
    "progression_speed_kmh": 50.0,
    "coordinated": True,
    "decision_rationale": "队列 165 米已接近 300 米路段的一半，取 95 秒周期消化排队。",
}


def _context(**overrides: Any) -> Dict[str, Any]:
    ctx: Dict[str, Any] = {
        "round_index": 1,
        "max_rounds": 2,
        "diagnosis": {"bottleneck_location": "J1_J2", "severity_level": "严重"},
        "detector_state": dict(DETECTOR_STATE),
        "baseline_plan": {
            "design_cycle_s": 90.0,
            "arterial_green_share": 0.7333,
            "arterial_green_s": 60.2,
            "cross_green_s": 21.8,
            "yellow_s": 4.0,
            "green_wave_offsets_s": [0.0, 43.2, 86.4],
            "green_wave_bandwidth_ratio_pct": 33.2,
            "progression_speed_kmh": 50.0,
            "reroute_ratio": 0.19,
            "reroute_capacity_cap": 0.40,
        },
        "feedback": None,
    }
    ctx.update(overrides)
    return ctx


class _StubLLM:
    """Minimal stand-in for LLMReasoningClient — returns a canned (payload, mode, error)."""

    model = "stub-model"

    def __init__(self, payload: Any, mode: str = LLMReasoningClient.MODE_LLM, error: Optional[str] = None):
        self._payload, self._mode, self._error = payload, mode, error

    def chat_json(self, system_prompt: str, user_prompt: str, temperature: float = 0.2, max_tokens: int = 1200):
        return self._payload, self._mode, self._error


class TestSchemaValidation(unittest.TestCase):
    """The model payload must be structurally sound before anything is clipped."""

    def test_valid_payload_accepted(self):
        proposal, errors = validate_payload(GOOD_PAYLOAD)
        self.assertEqual(errors, [])
        self.assertIsNotNone(proposal)
        self.assertEqual(proposal["target_cycle_s"], 95.0)
        self.assertIs(proposal["coordinated"], True)
        self.assertIn("周期", proposal["decision_rationale"])

    def test_non_dict_payload_rejected(self):
        for bad in ("a string", 42, [1, 2, 3], None):
            with self.subTest(bad=bad):
                proposal, errors = validate_payload(bad)
                self.assertIsNone(proposal)
                self.assertTrue(errors)

    def test_missing_fields_rejected(self):
        for field in ("target_cycle_s", "arterial_green_share", "reroute_ratio",
                      "progression_speed_kmh", "coordinated", "decision_rationale"):
            with self.subTest(field=field):
                payload = dict(GOOD_PAYLOAD)
                payload.pop(field)
                proposal, errors = validate_payload(payload)
                self.assertIsNone(proposal)
                self.assertTrue(any(field in e for e in errors), errors)

    def test_non_finite_and_wrong_typed_numbers_rejected(self):
        for field in ("target_cycle_s", "arterial_green_share", "reroute_ratio", "progression_speed_kmh"):
            for bad in (float("inf"), float("-inf"), float("nan"), "90", None, True, [90]):
                with self.subTest(field=field, bad=bad):
                    payload = dict(GOOD_PAYLOAD)
                    payload[field] = bad
                    proposal, errors = validate_payload(payload)
                    self.assertIsNone(proposal, f"{field}={bad!r} should be rejected")
                    self.assertTrue(errors)

    def test_coordinated_must_be_boolean(self):
        # The schema must not accept the *string* "true": downstream code branches on
        # identity with True/False, and a truthy string would silently invert behaviour.
        for bad in ("true", "false", 1, 0, None):
            with self.subTest(bad=bad):
                payload = dict(GOOD_PAYLOAD)
                payload["coordinated"] = bad
                proposal, errors = validate_payload(payload)
                self.assertIsNone(proposal)
                self.assertTrue(any("coordinated" in e for e in errors), errors)

    def test_empty_rationale_rejected(self):
        payload = dict(GOOD_PAYLOAD, decision_rationale="   ")
        proposal, errors = validate_payload(payload)
        self.assertIsNone(proposal)
        self.assertTrue(any("decision_rationale" in e for e in errors), errors)


class TestFeasibilityClipping(unittest.TestCase):
    """Every variable is bounded by the corridor's feasible domain, and each clip is audited."""

    def _clip(self, **overrides: Any):
        payload = dict(GOOD_PAYLOAD)
        payload.update(overrides)
        proposal, errors = validate_payload(payload)
        self.assertEqual(errors, [], errors)
        return clip_policy(proposal, _context())

    def test_in_domain_policy_is_untouched(self):
        applied, adjustments = self._clip()
        self.assertEqual(adjustments, [])
        self.assertEqual(applied["target_cycle_s"], 95.0)
        self.assertEqual(applied["reroute_ratio"], 0.20)
        self.assertEqual(applied["progression_speed_kmh"], 50.0)

    def test_cycle_clipped_to_bounds(self):
        low, high = POLICY_BOUNDS["target_cycle_s"]

        applied, adj = self._clip(target_cycle_s=5.0)
        self.assertEqual(applied["target_cycle_s"], max(low, HARD_CYCLE_MIN_S))
        self.assertTrue(any(a["variable"] == "target_cycle_s" for a in adj))

        applied, adj = self._clip(target_cycle_s=9999.0)
        self.assertEqual(applied["target_cycle_s"], min(high, HARD_CYCLE_MAX_S))
        self.assertTrue(any(a["variable"] == "target_cycle_s" for a in adj))

    def test_green_share_clipped_to_bounds(self):
        low, high = POLICY_BOUNDS["arterial_green_share"]

        applied, adj = self._clip(arterial_green_share=0.01)
        self.assertGreaterEqual(applied["arterial_green_share"], low)
        self.assertTrue(any(a["variable"] == "arterial_green_share" for a in adj))

        applied, adj = self._clip(arterial_green_share=0.99)
        self.assertLessEqual(applied["arterial_green_share"], high)
        self.assertTrue(any(a["variable"] == "arterial_green_share" for a in adj))

    def test_cross_street_minimum_green_preserved(self):
        # A greedy arterial share at a short cycle would starve the side street. The
        # clip must restore the clearance minimum rather than let the model do it.
        applied, adjustments = self._clip(target_cycle_s=60.0, arterial_green_share=0.82)
        available = applied["target_cycle_s"] - 2 * YELLOW_TIME_S
        self.assertGreaterEqual(applied["cross_green_s"], MIN_CROSS_GREEN_S - 1e-6)
        self.assertAlmostEqual(
            applied["arterial_green_s"] + applied["cross_green_s"], available, places=1
        )

    def test_reroute_clipped_to_policy_cap(self):
        low, high = POLICY_BOUNDS["reroute_ratio"]

        applied, adj = self._clip(reroute_ratio=-0.5)
        self.assertEqual(applied["reroute_ratio"], low)

        applied, adj = self._clip(reroute_ratio=5.0)
        self.assertEqual(applied["reroute_ratio"], high)
        self.assertTrue(any(a["variable"] == "reroute_ratio" for a in adj))

    def test_reroute_also_capped_by_bypass_spare_capacity(self):
        # Context says the bypass can only absorb 9% of the upstream flow.
        ctx = _context()
        ctx["baseline_plan"]["reroute_capacity_cap"] = 0.09
        payload = dict(GOOD_PAYLOAD, reroute_ratio=0.35)
        proposal, _ = validate_payload(payload)
        applied, adjustments = clip_policy(proposal, ctx)
        self.assertAlmostEqual(applied["reroute_ratio"], 0.09, places=4)
        self.assertTrue(any(a["variable"] == "reroute_ratio" for a in adjustments))

    def test_progression_speed_clipped(self):
        low, high = POLICY_BOUNDS["progression_speed_kmh"]

        applied, _ = self._clip(progression_speed_kmh=1.0)
        self.assertEqual(applied["progression_speed_kmh"], low)

        applied, _ = self._clip(progression_speed_kmh=500.0)
        self.assertEqual(applied["progression_speed_kmh"], high)

    def test_clip_audit_records_requested_and_applied(self):
        _, adjustments = self._clip(target_cycle_s=9999.0)
        entry = next(a for a in adjustments if a["variable"] == "target_cycle_s")
        self.assertEqual(entry["requested"], 9999.0)
        self.assertEqual(entry["applied"], min(POLICY_BOUNDS["target_cycle_s"][1], HARD_CYCLE_MAX_S))
        self.assertTrue(entry["reason"])


class TestNumericProvenanceGuard(unittest.TestCase):
    """The model may quote inputs; it may never invent an effect."""

    def test_known_numbers_accepted(self):
        allowed = [165.0, 300.0, 90.0, 0.82]
        self.assertTrue(_rationale_quotes_only_known_numbers("队列 165 米，路段 300 米。", allowed))
        self.assertTrue(_rationale_quotes_only_known_numbers("周期取 90 秒。", allowed))

    def test_ratio_may_be_quoted_as_percent(self):
        allowed = [0.82]
        self.assertTrue(_rationale_quotes_only_known_numbers("占有率达 82%。", allowed))

    def test_small_bare_numbers_tolerated(self):
        # Junction labels and phase counts are not quantified performance claims.
        self.assertTrue(_rationale_quotes_only_known_numbers("J1-J3 两个相位。", []))

    def test_invented_percentage_rejected(self):
        # The exact failure mode this guard exists for: a fabricated effect size.
        self.assertFalse(_rationale_quotes_only_known_numbers("预计延误下降 35%。", [165.0, 300.0]))
        self.assertFalse(_rationale_quotes_only_known_numbers("预计延误下降35%", [165.0, 300.0]))

    def test_invented_large_number_rejected(self):
        self.assertFalse(_rationale_quotes_only_known_numbers("排队将减少 420 米。", [165.0, 300.0]))

    def test_empty_text_is_traceable(self):
        self.assertTrue(_rationale_quotes_only_known_numbers("", []))


class TestDecisionLayerBehaviour(unittest.TestCase):
    """End-to-end behaviour of the layer, including every degradation path."""

    def test_unconfigured_model_yields_no_policy_and_explicit_mode(self):
        layer = LLMDecisionLayer(_StubLLM(None, mode=LLMReasoningClient.MODE_UNCONFIGURED,
                                          error="LLM_API_KEY is not set"))
        result = layer.propose(_context())
        self.assertEqual(result["mode"], LLMReasoningClient.MODE_UNCONFIGURED)
        self.assertIsNone(result["applied"])
        self.assertIsNone(result["requested"])
        self.assertTrue(result["errors"])
        self.assertEqual(result["engine"], "deterministic_rule_chain")

    def test_sdk_missing_is_reported(self):
        layer = LLMDecisionLayer(_StubLLM(None, mode=LLMReasoningClient.MODE_SDK_MISSING,
                                          error="openai package is not installed"))
        result = layer.propose(_context())
        self.assertEqual(result["mode"], LLMReasoningClient.MODE_SDK_MISSING)
        self.assertIsNone(result["applied"])

    def test_valid_model_proposal_becomes_a_deployable_policy(self):
        layer = LLMDecisionLayer(_StubLLM(dict(GOOD_PAYLOAD)))
        result = layer.propose(_context())
        self.assertEqual(result["mode"], LLMReasoningClient.MODE_LLM)
        self.assertIsNotNone(result["applied"])
        self.assertEqual(result["engine"], "stub-model")
        self.assertEqual(result["applied"]["target_cycle_s"], 95.0)
        # The deployed green splits must be internally consistent.
        available = 95.0 - 2 * YELLOW_TIME_S
        self.assertAlmostEqual(
            result["applied"]["arterial_green_s"] + result["applied"]["cross_green_s"],
            available, places=1,
        )

    def test_out_of_domain_proposal_is_clipped_not_obeyed(self):
        # NB the rationale must stay consistent with the payload's own numbers: the
        # provenance guard rejects a proposal that quotes values it never supplied.
        payload = dict(
            GOOD_PAYLOAD, target_cycle_s=400.0, reroute_ratio=0.9,
            decision_rationale="检测到排队 165 米、路段 300 米，按该排队规模给定周期。",
        )
        layer = LLMDecisionLayer(_StubLLM(payload))
        result = layer.propose(_context())
        self.assertEqual(result["mode"], LLMReasoningClient.MODE_LLM)
        self.assertEqual(result["requested"]["target_cycle_s"], 400.0)
        self.assertLessEqual(result["applied"]["target_cycle_s"], HARD_CYCLE_MAX_S)
        self.assertLessEqual(result["applied"]["reroute_ratio"], POLICY_BOUNDS["reroute_ratio"][1])
        self.assertTrue(result["adjustments"])

    def test_rationale_inconsistent_with_its_own_payload_is_rejected(self):
        # The model claims a 95 s cycle while asking for 400 s. Neither value can be
        # trusted, so the proposal is refused rather than half-applied.
        payload = dict(GOOD_PAYLOAD, target_cycle_s=400.0)
        layer = LLMDecisionLayer(_StubLLM(payload))
        result = layer.propose(_context())
        self.assertEqual(result["mode"], LLMReasoningClient.MODE_ERROR)
        self.assertIsNone(result["applied"])

    def test_schema_invalid_proposal_degrades_to_error_mode(self):
        payload = dict(GOOD_PAYLOAD)
        payload.pop("reroute_ratio")
        layer = LLMDecisionLayer(_StubLLM(payload))
        result = layer.propose(_context())
        self.assertEqual(result["mode"], LLMReasoningClient.MODE_ERROR)
        self.assertIsNone(result["applied"])
        self.assertTrue(any("reroute_ratio" in e for e in result["errors"]))

    def test_rationale_quoting_invented_numbers_rejects_whole_proposal(self):
        payload = dict(GOOD_PAYLOAD, decision_rationale="本方案预计可让延误下降 35%。")
        layer = LLMDecisionLayer(_StubLLM(payload))
        result = layer.propose(_context())
        self.assertEqual(result["mode"], LLMReasoningClient.MODE_ERROR)
        self.assertIsNone(result["applied"])
        self.assertTrue(any("provenance" in e for e in result["errors"]))

    def test_rationale_quoting_supplied_numbers_is_accepted(self):
        payload = dict(
            GOOD_PAYLOAD,
            decision_rationale="检测到排队 165 米、路段 300 米，取 95 秒周期消化排队。",
        )
        layer = LLMDecisionLayer(_StubLLM(payload))
        result = layer.propose(_context())
        self.assertEqual(result["mode"], LLMReasoningClient.MODE_LLM)
        self.assertIsNotNone(result["applied"])

    def test_feedback_block_is_sent_on_later_rounds(self):
        captured: Dict[str, str] = {}

        class _CapturingLLM(_StubLLM):
            def chat_json(self, system_prompt, user_prompt, temperature=0.2, max_tokens=1200):
                captured["user"] = user_prompt
                return super().chat_json(system_prompt, user_prompt, temperature, max_tokens)

        layer = LLMDecisionLayer(_CapturingLLM(dict(GOOD_PAYLOAD)))
        ctx = _context()
        layer.propose(ctx)
        self.assertNotIn("上一轮", captured["user"])

        ctx["feedback"] = {
            "round": 1,
            "applied_policy": {"target_cycle_s": 95.0},
            "kpi_delta": {"delay_improvement_pct": 12.5},
            "measured": {"avg_delay_s": 26.1},
            "adopted": True,
        }
        layer.propose(ctx)
        self.assertIn("上一轮", captured["user"])
        self.assertIn("12.5", captured["user"])


class TestToolchainIntegration(unittest.TestCase):
    """The overlay must be inert by default and effective when supplied."""

    def setUp(self):
        self.agent = TrafficDecisionAgent()

    def test_policy_none_and_empty_policy_are_identical(self):
        base = self.agent._tool_plan(DIAGNOSIS)
        empty = self.agent._tool_plan(DIAGNOSIS, policy={})
        for key in ("actual_cycle", "arterial_green", "cross_green", "yellow_time"):
            self.assertEqual(base[key], empty[key], f"{key} changed for an empty policy")
        self.assertEqual(base["green_wave"]["offsets"], empty["green_wave"]["offsets"])
        self.assertEqual(base["reroute"]["diversion_ratio"], empty["reroute"]["diversion_ratio"])

    def test_overlay_actually_moves_deployed_values(self):
        base = self.agent._tool_plan(DIAGNOSIS)
        policy = {
            "target_cycle_s": 110.0,
            "arterial_green_share": 0.62,
            "reroute_ratio": 0.05,
            "progression_speed_kmh": 40.0,
            "coordinated": True,
            "decision_rationale": "test",
        }
        over = self.agent._tool_plan(DIAGNOSIS, policy=policy)
        self.assertNotEqual(over["actual_cycle"], base["actual_cycle"])
        self.assertAlmostEqual(over["actual_cycle"], 110.0, places=1)
        self.assertAlmostEqual(
            over["arterial_green"] + over["cross_green"],
            over["actual_cycle"] - 2 * over["yellow_time"], places=1,
        )
        self.assertEqual(over["green_wave"]["progression_speed_kmh"], 40.0)
        self.assertAlmostEqual(over["reroute"]["diversion_ratio"], 0.05, places=4)
        self.assertEqual(over["policy_overlay"]["source"], "llm_decision_layer")

    def test_overlay_marks_deterministic_source_when_absent(self):
        plan = self.agent._tool_plan(DIAGNOSIS)
        self.assertEqual(plan["policy_overlay"]["source"], "deterministic_rule_chain")
        self.assertIsNone(plan["policy_overlay"]["applied"])

    def test_corridor_constants_exposed_for_audit(self):
        plan = self.agent._tool_plan(DIAGNOSIS)
        self.assertIn("upstream_flow_vph", plan["inputs_used"])
        self.assertIn("bypass_spare_capacity_vph", plan["inputs_used"])


class TestControlParamBuilder(unittest.TestCase):
    """
    `build_control_params` is the single source of truth for "what gets deployed".

    It replaced three hand-written copies of the same dict (strategy A, strategy B and
    the closed loop), so these tests pin the legacy construction as the reference: if the
    refactor had changed a deployed value, the simulation would silently stop matching
    the plan the report claims was executed.
    """

    def setUp(self):
        self.agent = TrafficDecisionAgent()
        self.plan = self.agent._tool_plan(DIAGNOSIS)

    def test_coordinated_program_uses_green_wave_offsets(self):
        ctl = self.agent.build_control_params(self.plan, True)
        self.assertEqual(
            ctl["signal_program"]["first_green_start"],
            list(self.plan["green_wave"]["offsets"]),
        )
        self.assertTrue(ctl["green_wave"])

    def test_uncoordinated_program_starts_every_junction_at_zero(self):
        ctl = self.agent.build_control_params(self.plan, False)
        self.assertEqual(ctl["signal_program"]["first_green_start"], [0.0, 0.0, 0.0])
        self.assertFalse(ctl["green_wave"])

    def test_rerouting_switch_forces_zero_diversion(self):
        self.assertGreater(self.plan["reroute"]["diversion_ratio"], 0.0)
        disabled = self.agent.build_control_params(self.plan, True, use_rerouting=False)
        self.assertEqual(disabled["reroute_ratio"], 0.0)
        enabled = self.agent.build_control_params(self.plan, True)
        self.assertAlmostEqual(
            enabled["reroute_ratio"], self.plan["reroute"]["diversion_ratio"], places=6
        )

    def test_webster_switch_is_forwarded(self):
        self.assertFalse(self.agent.build_control_params(self.plan, True, use_webster=False)["webster"])
        self.assertTrue(self.agent.build_control_params(self.plan, True)["webster"])

    def test_signal_program_is_not_mutated_in_place(self):
        before = dict(self.plan["signal_program"])
        self.agent.build_control_params(self.plan, True)
        self.assertEqual(self.plan["signal_program"], before)

    def test_matches_the_legacy_hand_built_control_dict(self):
        for coordinated in (True, False):
            expected = {
                "signal_program": dict(
                    self.plan["signal_program"],
                    first_green_start=(
                        list(self.plan["green_wave"]["offsets"])
                        if coordinated
                        else [0.0, 0.0, 0.0]
                    ),
                ),
                "reroute_ratio": self.plan["reroute"]["diversion_ratio"],
                "green_wave": coordinated,
                "webster": True,
            }
            self.assertEqual(self.agent.build_control_params(self.plan, coordinated), expected)

    def test_rollout_deploys_exactly_the_built_params(self):
        """The bytes handed to the simulator must equal the helper's output, not a copy."""

        captured: list = []

        class _CapturingSandbox:
            def run_simulation(self, scheme="baseline", duration=600, incident_start=150,
                               incident_end=420, control_params=None, seed=None):
                captured.append((scheme, control_params, seed))
                return {
                    "vehicle_delays": [10.0] * 20,
                    "queue_lengths": [0.0] * 20,
                    "vehicle_speeds": [10.0] * 20,
                    "total_co2_mg": 1000.0,
                    "total_fuel_mg": 400.0,
                    "completed_trips": 2000,
                    "simulation_duration": duration,
                }

        self.agent.sandbox = _CapturingSandbox()
        self.agent.execute_what_if_rollout(
            duration=300, incident_start=75, incident_end=210,
            diagnosis=DIAGNOSIS, seed=42,
        )
        deployed = {scheme: ctl for scheme, ctl, _ in captured}

        self.assertEqual(
            deployed["webster"],
            self.agent.build_control_params(self.plan, False, use_rerouting=False),
        )
        self.assertEqual(
            deployed["agent_dss"],
            self.agent.build_control_params(self.plan, True, use_rerouting=True),
        )
        self.assertTrue(all(seed == 42 for _, _, seed in captured))


class TestDiversionOverride(unittest.TestCase):
    """An external diversion decision must keep every derived field consistent."""

    def setUp(self):
        self.alloc = DynamicReroutingAllocator()
        self.plan = self.alloc.calculate_diversion(
            bottleneck_queue_meters=165.0,
            bottleneck_link_length=300.0,
            bottleneck_occupancy=0.82,
            upstream_flow_vph=1800.0,
            bypass_current_occupancy=0.28,
            bypass_spare_capacity_vph=1200.0,
        )

    def test_override_recomputes_derived_fields(self):
        out = self.alloc.apply_diversion_override(
            self.plan, diversion_ratio=0.30, upstream_flow_vph=1800.0,
            bottleneck_queue_meters=165.0, bypass_spare_capacity_vph=1200.0,
        )
        self.assertAlmostEqual(out["diversion_ratio"], 0.30, places=4)
        self.assertEqual(out["diverted_flow_vph"], round(1800.0 * 0.30))
        self.assertTrue(out["need_diversion"])
        self.assertEqual(out["diversion_source"], "external_policy_override")

    def test_override_capped_by_bypass_capacity(self):
        out = self.alloc.apply_diversion_override(
            self.plan, diversion_ratio=0.35, upstream_flow_vph=1800.0,
            bottleneck_queue_meters=165.0, bypass_spare_capacity_vph=180.0,  # only 10%
        )
        self.assertLessEqual(out["diversion_ratio"], 0.10 + 1e-9)
        self.assertEqual(out["diverted_flow_vph"], round(1800.0 * out["diversion_ratio"]))

    def test_override_capped_by_policy_ceiling(self):
        out = self.alloc.apply_diversion_override(
            self.plan, diversion_ratio=0.95, upstream_flow_vph=1800.0,
            bottleneck_queue_meters=165.0, bypass_spare_capacity_vph=1200.0,
        )
        self.assertLessEqual(out["diversion_ratio"], self.alloc.max_diversion + 1e-9)

    def test_zero_override_switches_advisory_off(self):
        out = self.alloc.apply_diversion_override(
            self.plan, diversion_ratio=0.0, upstream_flow_vph=1800.0,
            bottleneck_queue_meters=165.0, bypass_spare_capacity_vph=1200.0,
        )
        self.assertFalse(out["need_diversion"])
        self.assertEqual(out["diverted_flow_vph"], 0)
        # The published sign must not describe an action that was not deployed.
        self.assertNotIn("绕行", out["vms_advisory"])

    def test_override_does_not_mutate_the_input_plan(self):
        original_ratio = self.plan["diversion_ratio"]
        self.alloc.apply_diversion_override(
            self.plan, diversion_ratio=0.0, upstream_flow_vph=1800.0,
            bottleneck_queue_meters=165.0, bypass_spare_capacity_vph=1200.0,
        )
        self.assertEqual(self.plan["diversion_ratio"], original_ratio)


class _ScriptedSandbox:
    """
    Stands in for the SUMO sandbox so closed-loop behaviour is testable without
    launching SUMO. Returns scripted (delay, queue) pairs in call order:
    [0] = baseline, [1] = deterministic reference, [2..] = model rounds.
    """

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def run_simulation(self, scheme="baseline", duration=600, incident_start=150,
                       incident_end=420, control_params=None, seed=None):
        idx = len(self.calls)
        self.calls.append({"scheme": scheme, "control_params": control_params, "seed": seed})
        delay, queue = self.script[min(idx, len(self.script) - 1)]
        return {
            "vehicle_delays": [float(delay)] * 20,
            "queue_lengths": [float(queue)] * 20,
            "vehicle_speeds": [10.0] * 20,
            "total_co2_mg": 1000.0,
            "total_fuel_mg": 400.0,
            "completed_trips": 2000,
            "simulation_duration": duration,
        }


class TestClosedLoopOptimisation(unittest.TestCase):
    """The closed loop must deploy model parameters, and must not oversell the result."""

    WINDOW = dict(duration=300, incident_start=75, incident_end=210)

    def _agent(self, script, payload=None, llm_mode=LLMReasoningClient.MODE_LLM):
        agent = TrafficDecisionAgent()
        agent.sandbox = _ScriptedSandbox(script)
        if llm_mode == LLMReasoningClient.MODE_LLM:
            agent.decision = LLMDecisionLayer(_StubLLM(dict(payload if payload is not None else GOOD_PAYLOAD)))
        else:
            agent.decision = LLMDecisionLayer(_StubLLM(None, mode=llm_mode, error="no key configured"))
        return agent

    def test_model_parameters_are_what_reaches_the_simulator(self):
        agent = self._agent([(10.0, 80.0), (9.0, 80.0), (8.0, 80.0)])
        res = agent.optimize_control_policy_closed_loop(DIAGNOSIS, rounds=1, **self.WINDOW)

        self.assertEqual(res["decision_mode"], "llm_closed_loop")
        self.assertEqual(res["decision_engine"], "stub-model")
        rd = res["rounds"][0]
        self.assertEqual(rd["model_requested"]["target_cycle_s"], GOOD_PAYLOAD["target_cycle_s"])
        self.assertEqual(rd["deployed_after_clipping"]["target_cycle_s"], GOOD_PAYLOAD["target_cycle_s"])

        # The diversion ratio that reached the sandbox must be the model's, not the default.
        round1_controls = agent.sandbox.calls[2]["control_params"]
        self.assertAlmostEqual(round1_controls["reroute_ratio"], GOOD_PAYLOAD["reroute_ratio"], places=4)
        # ...and the deployed cycle must differ from the deterministic reference's.
        det_cycle = agent.sandbox.calls[1]["control_params"]["signal_program"]["cycle_length"]
        self.assertNotAlmostEqual(round1_controls["signal_program"]["cycle_length"], det_cycle, places=1)

    def test_feedback_of_previous_round_is_supplied_to_the_next(self):
        captured = []

        class _SniffingDecision(LLMDecisionLayer):
            def propose(self, context):
                captured.append(context.get("feedback"))
                return super().propose(context)

        agent = self._agent([(10.0, 80.0), (9.0, 80.0), (8.0, 80.0)])
        agent.decision = _SniffingDecision(_StubLLM(dict(GOOD_PAYLOAD)))
        agent.optimize_control_policy_closed_loop(DIAGNOSIS, rounds=2, **self.WINDOW)

        self.assertEqual(len(captured), 2)
        self.assertIsNone(captured[0], "round 1 must not receive feedback")
        self.assertIsNotNone(captured[1], "round 2 must receive round 1's outcome")
        self.assertEqual(captured[1]["round"], 1)
        self.assertIn("kpi_delta", captured[1])
        self.assertIn("applied_policy", captured[1])

    def test_adopted_when_the_model_beats_both_baseline_and_toolchain(self):
        agent = self._agent([(10.0, 80.0), (9.0, 80.0), (7.0, 80.0), (7.5, 80.0)])
        res = agent.optimize_control_policy_closed_loop(DIAGNOSIS, rounds=2, **self.WINDOW)
        self.assertEqual(res["best"]["source"], "llm_decision_layer")
        self.assertEqual(res["best"]["round"], 1)
        self.assertEqual(res["recommendation"]["verdict"], "adopt_best_policy")
        self.assertTrue(res["recommendation"]["adopt_control"])

    def test_not_recommended_when_every_candidate_loses_to_doing_nothing(self):
        # This is the honest-reporting case: the system must say "don't intervene",
        # not present the least-bad option as a recommendation.
        agent = self._agent([(10.0, 80.0), (12.0, 80.0), (13.0, 80.0), (14.0, 80.0)])
        res = agent.optimize_control_policy_closed_loop(DIAGNOSIS, rounds=2, **self.WINDOW)
        self.assertEqual(res["recommendation"]["verdict"], "do_nothing_is_better_under_measured_conditions")
        self.assertFalse(res["recommendation"]["adopt_control"])
        self.assertIn("不建议下发", res["recommendation"]["note"])
        self.assertEqual(res["recommendation"]["baseline_avg_delay_s"], 10.0)

    def test_queue_blowup_round_is_not_adopted(self):
        # Round 1 wins on delay but triples the queue; that must not be adopted.
        agent = self._agent([(10.0, 80.0), (9.0, 80.0), (5.0, 400.0)])
        res = agent.optimize_control_policy_closed_loop(DIAGNOSIS, rounds=1, **self.WINDOW)
        rd = res["rounds"][0]
        self.assertFalse(rd["queue_constraint_respected"])
        self.assertFalse(rd["adopted_as_best"])
        self.assertEqual(res["best"]["source"], "deterministic_rule_chain")

    def test_zero_rounds_never_touches_the_model(self):
        agent = self._agent([(10.0, 80.0), (9.0, 80.0)])
        res = agent.optimize_control_policy_closed_loop(DIAGNOSIS, rounds=0, **self.WINDOW)
        self.assertEqual(res["rounds_executed"], 0)
        self.assertEqual(res["decision_mode"], "deterministic_rule_chain")
        self.assertEqual(len(agent.sandbox.calls), 2, "only baseline + deterministic should run")

    def test_unconfigured_model_degrades_without_inventing_a_result(self):
        agent = self._agent([(10.0, 80.0), (9.0, 80.0)], llm_mode=LLMReasoningClient.MODE_UNCONFIGURED)
        res = agent.optimize_control_policy_closed_loop(DIAGNOSIS, rounds=2, **self.WINDOW)
        self.assertEqual(res["decision_mode"], "deterministic_rule_chain")
        self.assertEqual(res["rounds_executed"], 0)
        self.assertTrue(res["decision_errors"])
        self.assertIsNotNone(res["deterministic_kpi"])

    def test_rounds_are_clamped_to_the_maximum(self):
        agent = self._agent([(10.0, 80.0), (9.0, 80.0)])
        res = agent.optimize_control_policy_closed_loop(DIAGNOSIS, rounds=99, **self.WINDOW)
        self.assertLessEqual(res["rounds_executed"], TrafficDecisionAgent.MAX_CLOSED_LOOP_ROUNDS)

    def test_result_carries_an_audit_trail(self):
        agent = self._agent([(10.0, 80.0), (9.0, 80.0), (8.0, 80.0)])
        res = agent.optimize_control_policy_closed_loop(DIAGNOSIS, rounds=1, **self.WINDOW)
        rd = res["rounds"][0]
        for key in ("model_requested", "deployed_after_clipping", "clipping_adjustments",
                    "measured_kpi", "vs_baseline_pct", "adopted_as_best"):
            self.assertIn(key, rd)
        self.assertEqual(res["execution_mode"], "physical_sumo_sandbox")
        self.assertIn("upstream_flow_vph", res["corridor_inputs"])


if __name__ == "__main__":
    unittest.main()
