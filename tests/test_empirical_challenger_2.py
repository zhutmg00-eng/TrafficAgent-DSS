"""
TrafficAgent-DSS: Empirical Challenge & Stress Test Suite (Challenger 2)
Focuses on AI Decision Agents (`src/agents/`) and FastAPI Decision Hub (`src/web/`).

Stress Test Dimensions:
1. Agents:
   - `diagnose_bottleneck`: None values, float('inf'), float('-inf'), float('nan'), malformed numbers.
   - `generate_decision_report`: None root_causes, None cot_reasoning, None reroute_ratio, None rollout kpis.
   - Strategy B: Description and rationale validation when diversion ratio is 0.0%.
2. LLM Client:
   - Markdown JSON extraction: uppercase ```JSON```, mixed case, untagged, conversational text, malformed blocks.
   - Boolean parsing: "false" must never evaluate to True; test all boolean string aliases and defaults.
3. FastAPI Decision Hub:
   - Endpoints: /api/strategies, /api/report/export, /api/report/download, /api/rollout, /api/decide, /api/diagnose, /api/evaluate/multi-seed.
   - Non-dict inputs (strings, lists, numbers, booleans) -> HTTP 422.
   - Malformed JSON payloads -> HTTP 422.
   - Invalid schema fields and boundary violations -> HTTP 422.
   - Sandbox failure graceful degradation -> HTTP 200 with fallback reason.
   - ZERO unhandled 500 crashes across all tested stress inputs.
"""

import math
import os
import unittest
from typing import Any, Dict

from fastapi.testclient import TestClient

from src.agents.traffic_agent import TrafficDecisionAgent, _safe_float, _parse_bool
from src.agents.llm_client import LLMReasoningClient
from src.web.app import app, agent as global_agent


class TestAgentDiagnosticsStress(unittest.TestCase):
    """Stress tests for TrafficDecisionAgent diagnostic and reporting mechanisms."""

    def setUp(self):
        self.agent = TrafficDecisionAgent()

    def test_safe_float_edge_cases(self):
        """Tests that _safe_float protects against None, Inf, -Inf, NaN, strings, and collections."""
        # None and infinities
        self.assertEqual(_safe_float(None, default=10.0), 10.0)
        self.assertEqual(_safe_float(float("inf"), default=10.0), 10.0)
        self.assertEqual(_safe_float(float("-inf"), default=10.0), 10.0)
        self.assertEqual(_safe_float(float("nan"), default=10.0), 10.0)

        # Malformed strings and objects
        self.assertEqual(_safe_float("invalid_number", default=5.0), 5.0)
        self.assertEqual(_safe_float("", default=5.0), 5.0)
        self.assertEqual(_safe_float([], default=5.0), 5.0)
        self.assertEqual(_safe_float({}, default=5.0), 5.0)
        self.assertEqual(_safe_float(object(), default=5.0), 5.0)

        # Clamping bounds
        self.assertEqual(_safe_float(-50.0, default=0.0, min_val=0.0, max_val=100.0), 0.0)
        self.assertEqual(_safe_float(150.0, default=0.0, min_val=0.0, max_val=100.0), 100.0)
        self.assertEqual(_safe_float(45.5, default=0.0, min_val=0.0, max_val=100.0), 45.5)

    def test_diagnose_bottleneck_none_values(self):
        """Tests diagnose_bottleneck when fields or dictionary are None."""
        state_all_none = {
            "bottleneck_edge": None,
            "queue_m": None,
            "link_length_m": None,
            "speed_kmh": None,
            "occupancy": None,
            "bypass_occupancy": None,
        }
        res = self.agent.diagnose_bottleneck(state_all_none)
        self.assertIsInstance(res, dict)
        self.assertIn("root_causes", res)
        self.assertIn("cot_reasoning", res)
        self.assertEqual(res["input_state"]["queue_m"], 165.0)
        self.assertEqual(res["input_state"]["link_length_m"], 300.0)
        self.assertEqual(res["bottleneck_location"], "J1_J2")

        # Entire payload None or empty
        res_none = self.agent.diagnose_bottleneck(None)
        self.assertIsInstance(res_none, dict)
        res_empty = self.agent.diagnose_bottleneck({})
        self.assertIsInstance(res_empty, dict)

    def test_diagnose_bottleneck_infinities_and_nan(self):
        """Tests diagnose_bottleneck when fields contain float inf, -inf, or nan."""
        state_inf = {
            "bottleneck_edge": "EDGE_STRESS_INF",
            "queue_m": float("inf"),
            "link_length_m": float("-inf"),
            "speed_kmh": float("nan"),
            "occupancy": float("inf"),
            "bypass_occupancy": float("-inf"),
        }
        res = self.agent.diagnose_bottleneck(state_inf)
        self.assertIsInstance(res, dict)
        # Verify clamped finite defaults were applied
        inp = res["input_state"]
        self.assertTrue(math.isfinite(inp["queue_m"]))
        self.assertTrue(math.isfinite(inp["link_length_m"]))
        self.assertTrue(math.isfinite(inp["speed_kmh"]))
        self.assertTrue(math.isfinite(inp["occupancy"]))
        self.assertTrue(math.isfinite(inp["bypass_occupancy"]))

    def test_diagnose_bottleneck_malformed_numbers(self):
        """Tests diagnose_bottleneck when fields contain malformed strings, lists, or dicts."""
        state_malformed = {
            "bottleneck_edge": 99999,  # integer instead of string
            "queue_m": "not_a_number",
            "link_length_m": [300.0],
            "speed_kmh": {"speed": 10.0},
            "occupancy": "0.95_string",
            "bypass_occupancy": True,  # bool converted to float safely
        }
        res = self.agent.diagnose_bottleneck(state_malformed)
        self.assertIsInstance(res, dict)
        self.assertEqual(res["bottleneck_location"], "99999")
        self.assertEqual(res["input_state"]["queue_m"], 165.0)
        self.assertEqual(res["input_state"]["link_length_m"], 300.0)
        self.assertEqual(res["input_state"]["speed_kmh"], 8.2)

    def test_diagnose_bottleneck_extreme_ranges(self):
        """Tests diagnose_bottleneck under negative and huge overflow numbers."""
        state_extreme = {
            "queue_m": -1000.0,
            "link_length_m": -500.0,
            "speed_kmh": -50.0,
            "occupancy": -2.0,
            "bypass_occupancy": 1e20,
        }
        res = self.agent.diagnose_bottleneck(state_extreme)
        inp = res["input_state"]
        # min_val clamping
        self.assertEqual(inp["queue_m"], 0.0)
        self.assertEqual(inp["link_length_m"], 10.0)
        self.assertEqual(inp["speed_kmh"], 0.0)
        self.assertEqual(inp["occupancy"], 0.0)
        # max_val clamping
        self.assertEqual(inp["bypass_occupancy"], 1.0)

    def test_generate_decision_report_guards_none_values(self):
        """Tests generate_decision_report when diagnosis/strategies fields are None."""
        diag_none = {
            "bottleneck_location": None,
            "severity_level": None,
            "spillback_risk": None,
            "root_causes": None,
            "cot_reasoning": None,
        }
        strat_none = {
            "strategy_b": {
                "cycle_length": None,
                "green_split_arterial": None,
                "green_wave_offsets": None,
                "reroute_ratio": None,
                "vms_advisory": None,
                "risk_warning": None,
                "green_wave": None,
            }
        }
        rollout_none = {
            "execution_mode": None,
            "kpis": None,
            "comparisons": None,
            "control_evidence": None,
        }

        report = self.agent.generate_decision_report(diag_none, strat_none, rollout_none)
        self.assertIsInstance(report, str)
        self.assertGreater(len(report), 500)
        self.assertIn("未获得归因结果", report)
        self.assertIn("未获得推理过程", report)
        self.assertIn("未触发动态分流诱导", report)

    def test_generate_decision_report_various_cause_and_cot_formats(self):
        """Tests generate_decision_report with list containing None, single strings, and empty."""
        diag = {
            "bottleneck_location": "J1_J2",
            "root_causes": [None, "", "真实成因一", 123],
            "cot_reasoning": "直接作为单行字符串传递的推理步骤",
        }
        strat = {
            "strategy_b": {
                "reroute_ratio": 0.2,
                "green_wave": True,
            }
        }
        report = self.agent.generate_decision_report(diag, strat, {})
        self.assertIn("真实成因一", report)
        self.assertIn("123", report)
        self.assertIn("直接作为单行字符串传递的推理步骤", report)
        self.assertIn("目标动态分流比例：**20%**", report)

    def test_strategy_b_descriptions_when_diversion_ratio_is_zero(self):
        """Tests Strategy B descriptions and rationale when diversion ratio is 0.0%."""
        # Saturated bypass (0.90) suppresses rerouting to 0.0%
        state_zero_div = {
            "bottleneck_edge": "J1_J2",
            "queue_m": 25.0,
            "link_length_m": 300.0,
            "speed_kmh": 35.0,
            "occupancy": 0.20,
            "bypass_occupancy": 0.90,
        }
        diag = self.agent.diagnose_bottleneck(state_zero_div)
        strat = self.agent.formulate_candidate_strategies(diag)
        strat_b = strat["strategy_b"]

        self.assertEqual(strat_b["reroute_ratio"], 0.0)

        # Strategy B description must state diversion is on standby /熔断
        self.assertIn("动态诱导分流待命熔断（0%）", strat_b["description"])
        self.assertNotIn("动态诱导分流 0%", strat_b["description"])

        # Strategy B rationale must NOT contradict physics by claiming diversion reduces bottleneck load
        for r in strat_b["rationale"]:
            self.assertNotIn("削减瓶颈输入负荷", r)
        self.assertTrue(any("待命熔断" in r for r in strat_b["rationale"]))

        # Decision report must declare diversion is not triggered
        report = self.agent.generate_decision_report(diag, strat, {})
        self.assertIn("当前工况未触发动态分流诱导。", report)
        self.assertNotIn("目标动态分流比例：**0%**", report)


class TestLLMClientRobustness(unittest.TestCase):
    """Stress tests for LLM Reasoning Client JSON extraction and boolean parsing."""

    def test_extract_json_uppercase_markdown_block(self):
        """Tests JSON extraction with uppercase ```JSON markdown fences."""
        raw_text = """Here is the structured diagnostic response:
```JSON
{
  "severity_level": "严重拥堵 (Level 4)",
  "spillback_risk": "极高",
  "root_causes": ["高峰集中汇聚", "事故占道"],
  "cot_reasoning": ["1. 【态势感知】...", "2. 【空间排队】...", "3. 【成因归因】...", "4. 【旁路核查】..."],
  "can_reroute": false
}
```
Please verify and implement."""
        extracted = LLMReasoningClient._extract_json(raw_text)
        self.assertIsNotNone(extracted)
        self.assertEqual(extracted["severity_level"], "严重拥堵 (Level 4)")
        self.assertEqual(extracted["can_reroute"], False)

    def test_extract_json_mixed_case_and_untagged_fences(self):
        """Tests JSON extraction with mixed case ```Json, ```jSoN, and untagged ``` fences."""
        cases = [
            "```Json\n{\"key\": \"val1\"}\n```",
            "```jSoN\n{\"key\": \"val2\"}\n```",
            "```JSON\n{\"key\": \"val3\"}\n```",
            "```\n{\"key\": \"val4\"}\n```",
        ]
        for i, c in enumerate(cases, start=1):
            res = LLMReasoningClient._extract_json(c)
            self.assertIsNotNone(res, f"Failed on case {c}")
            self.assertEqual(res["key"], f"val{i}")

    def test_extract_json_malformed_and_edge_inputs(self):
        """Tests that malformed JSON blocks gracefully return None instead of crashing."""
        malformed_inputs = [
            "```JSON\n{broken json without quote: true\n```",
            "```json\n{\"unclosed_string\": \n```",
            "```\n[1, 2, 3]\n```",  # array, not dict
            "```JSON\n\"pure string\"\n```",  # string, not dict
            "plain conversational text with no json markers",
            "",
            None,
            "    ",
            "{unclosed raw brace",
        ]
        for m in malformed_inputs:
            self.assertIsNone(LLMReasoningClient._extract_json(m))

    def test_extract_json_unfenced_embedded_json(self):
        """Tests extraction of raw JSON dict embedded in explanatory prose."""
        text = 'Pre-text explanation {"severity_level": "Level 3", "valid": true} post-text conclusion.'
        extracted = LLMReasoningClient._extract_json(text)
        self.assertIsNotNone(extracted)
        self.assertEqual(extracted["severity_level"], "Level 3")
        self.assertEqual(extracted["valid"], True)

    def test_boolean_parsing_false_never_evaluates_to_true(self):
        """CRITICAL: Verifies that string 'false' and its variants NEVER evaluate to True."""
        false_inputs = [
            "false",
            "False",
            "FALSE",
            "  false  ",
            "0",
            "no",
            "No",
            "NO",
            "n",
            "N",
            "off",
            "OFF",
            False,
            0,
            0.0,
        ]
        for val in false_inputs:
            parsed = _parse_bool(val, default=True)
            self.assertIs(
                parsed,
                False,
                f"Value {val!r} unexpectedly evaluated to {parsed} (must be False)!",
            )

    def test_boolean_parsing_true_variants(self):
        """Verifies that true variants evaluate to True."""
        true_inputs = [
            "true",
            "True",
            "TRUE",
            "  true  ",
            "1",
            "yes",
            "Yes",
            "YES",
            "y",
            "Y",
            "on",
            "ON",
            True,
            1,
            1.0,
        ]
        for val in true_inputs:
            parsed = _parse_bool(val, default=False)
            self.assertIs(
                parsed,
                True,
                f"Value {val!r} unexpectedly evaluated to {parsed} (must be True)!",
            )

    def test_boolean_parsing_fallback_defaults(self):
        """Verifies that unknown or malformed inputs return the specified default."""
        unknown_inputs = [None, "", "maybe", "unknown", [], {}, object()]
        for val in unknown_inputs:
            self.assertIs(_parse_bool(val, default=True), True)
            self.assertIs(_parse_bool(val, default=False), False)


class TestFastAPIDecisionHubStress(unittest.TestCase):
    """Stress tests for FastAPI Decision Hub REST endpoints using TestClient."""

    def setUp(self):
        self.client = TestClient(app)

    def test_endpoints_reject_non_dict_inputs_with_422(self):
        """
        Stress tests all POST endpoints with non-dict payloads (string, int, bool, list).
        Confirms standard HTTP 422 is returned, and absolutely NO 500 crash occurs.
        """
        post_endpoints = [
            "/api/strategies",
            "/api/report/export",
            "/api/report/download",
            "/api/rollout",
            "/api/decide",
            "/api/diagnose",
            "/api/evaluate/multi-seed",
        ]
        invalid_payloads = [
            "malformed string input",
            98765,
            True,
            False,
            ["list", "instead", "of", "dict"],
        ]

        for ep in post_endpoints:
            for payload in invalid_payloads:
                resp = self.client.post(ep, json=payload)
                self.assertEqual(
                    resp.status_code,
                    422,
                    f"Endpoint {ep} with payload {payload!r} returned {resp.status_code} instead of 422: {resp.text}",
                )

    def test_endpoints_reject_malformed_json_body_with_422(self):
        """Tests that invalid JSON byte payloads return HTTP 422/400 and never 500."""
        post_endpoints = [
            "/api/strategies",
            "/api/report/export",
            "/api/report/download",
            "/api/rollout",
            "/api/decide",
            "/api/diagnose",
            "/api/evaluate/multi-seed",
        ]
        for ep in post_endpoints:
            resp = self.client.post(
                ep,
                content=b"{unclosed json: 123",
                headers={"Content-Type": "application/json"},
            )
            self.assertIn(
                resp.status_code,
                (422, 400),
                f"Endpoint {ep} returned {resp.status_code} on malformed json bytes: {resp.text}",
            )
            self.assertNotEqual(resp.status_code, 500)

    def test_strategies_endpoint_schema_validation(self):
        """Tests /api/strategies schema validation and 422 responses."""
        # Non-dict diagnosis
        resp = self.client.post("/api/strategies", json={"diagnosis": "invalid_string"})
        self.assertEqual(resp.status_code, 422)

        resp = self.client.post("/api/strategies", json={"diagnosis": 12345})
        self.assertEqual(resp.status_code, 422)

        resp = self.client.post("/api/strategies", json={"diagnosis": [1, 2]})
        self.assertEqual(resp.status_code, 422)

        # Malformed input_state inside diagnosis
        resp = self.client.post(
            "/api/strategies",
            json={"diagnosis": {"input_state": {"queue_m": "not_a_float"}}},
        )
        self.assertEqual(resp.status_code, 422)

        # Valid empty or null payload
        resp_empty = self.client.post("/api/strategies", json={})
        self.assertEqual(resp_empty.status_code, 200)
        self.assertTrue(resp_empty.json()["success"])

    def test_report_export_and_download_schema_validation(self):
        """Tests /api/report/export and /api/report/download schema validation."""
        # Non-dict rollout_data
        resp_exp = self.client.post("/api/report/export", json={"rollout_data": "not_dict"})
        self.assertEqual(resp_exp.status_code, 422)

        resp_dl = self.client.post("/api/report/download", json={"rollout_data": "not_dict"})
        self.assertEqual(resp_dl.status_code, 422)

        # Non-dict kpis inside rollout_data
        resp_exp2 = self.client.post("/api/report/export", json={"rollout_data": {"kpis": "not_dict"}})
        self.assertEqual(resp_exp2.status_code, 422)

        resp_dl2 = self.client.post("/api/report/download", json={"rollout_data": {"kpis": "not_dict"}})
        self.assertEqual(resp_dl2.status_code, 422)

        # Non-numeric comparison values
        bad_comp = {
            "rollout_data": {
                "comparisons": {
                    "strategy_b": {"delay_improvement_pct": "cannot_compare_string"}
                }
            }
        }
        resp_bad_pct = self.client.post("/api/report/export", json=bad_comp)
        self.assertEqual(resp_bad_pct.status_code, 422)

        # Valid empty export
        resp_valid = self.client.post("/api/report/export", json={})
        self.assertEqual(resp_valid.status_code, 200)
        self.assertTrue(resp_valid.json()["success"])

        # GET /api/report/download returns 200 with text/markdown
        resp_get_dl = self.client.get("/api/report/download")
        self.assertEqual(resp_get_dl.status_code, 200)
        self.assertIn("text/markdown", resp_get_dl.headers.get("content-type", ""))

    def test_rollout_endpoint_boundary_validation(self):
        """Tests /api/rollout boundaries and parameter validation."""
        invalid_configs = [
            {"duration": 100},  # < 300
            {"duration": 3000},  # > 1800
            {"incident_start": 400, "incident_end": 200},  # start >= end
            {"incident_start": 100, "incident_end": 800, "duration": 600},  # end > duration
            {"seed": -1},  # seed < 0
            {"duration": "invalid_int"},
        ]
        for cfg in invalid_configs:
            resp = self.client.post("/api/rollout", json=cfg)
            self.assertEqual(
                resp.status_code,
                422,
                f"Expected 422 for config {cfg}, got {resp.status_code}: {resp.text}",
            )

        # Valid default rollout
        resp_ok = self.client.post("/api/rollout", json={})
        self.assertEqual(resp_ok.status_code, 200)
        self.assertTrue(resp_ok.json()["success"])

    def test_decide_endpoint_validation_and_pipeline(self):
        """Tests /api/decide end-to-end endpoint with invalid and valid parameters."""
        invalid_decide = [
            {"traffic_state": "not_a_dict"},
            {"rollout_config": "not_a_dict"},
            {"traffic_state": {"queue_m": -50.0}},
            {"traffic_state": {"link_length_m": 0.0}},
            {"traffic_state": {"occupancy": 1.5}},
            {"rollout_config": {"duration": 50}},
            {"rollout_config": {"incident_start": 500, "incident_end": 300}},
        ]
        for payload in invalid_decide:
            resp = self.client.post("/api/decide", json=payload)
            self.assertEqual(
                resp.status_code,
                422,
                f"Expected 422 for {payload}, got {resp.status_code}",
            )

        # Valid one-stop call
        resp_ok = self.client.post("/api/decide", json={})
        self.assertEqual(resp_ok.status_code, 200)
        data = resp_ok.json()
        self.assertTrue(data["success"])
        self.assertIn("diagnosis", data)
        self.assertIn("strategies", data)
        self.assertIn("rollout", data)
        self.assertIn("report_markdown", data)

    def test_diagnose_endpoint_validation(self):
        """Tests /api/diagnose parameter bounds."""
        invalid_states = [
            {"queue_m": -10.0},
            {"link_length_m": 0.0},
            {"speed_kmh": 300.0},
            {"occupancy": 1.5},
            {"bypass_occupancy": -0.2},
        ]
        for st in invalid_states:
            resp = self.client.post("/api/diagnose", json=st)
            self.assertEqual(resp.status_code, 422)

    def test_evaluate_multi_seed_endpoint_validation(self):
        """Tests /api/evaluate/multi-seed parameter bounds and list validation."""
        invalid_inputs = [
            {"seeds": []},  # empty list
            {"seeds": [-1, 2, 3]},  # negative seed
            {"seeds": ["abc"]},  # invalid element
            {"duration": 15},  # < 30
            {"incident_start": 400, "incident_end": 200},  # start >= end
            {"incident_end": 900, "duration": 600},  # end > duration
        ]
        for inp in invalid_inputs:
            resp = self.client.post("/api/evaluate/multi-seed", json=inp)
            self.assertEqual(
                resp.status_code,
                422,
                f"Expected 422 for {inp}, got {resp.status_code}",
            )

    def test_graceful_fallback_when_sumo_binary_missing(self):
        """
        Tests that when SUMO binary is unavailable or fails, /api/rollout and
        /api/evaluate/multi-seed return HTTP 200 with graceful fallback rather than 500.
        """
        old_bin = global_agent.sandbox.sumo_bin
        try:
            global_agent.sandbox.sumo_bin = "non_existent_sumo_challenger_test_bin"

            # 1. /api/rollout with physical sandbox forced
            resp_rollout = self.client.post(
                "/api/rollout",
                json={
                    "run_physical_sandbox": True,
                    "duration": 300,
                    "incident_start": 50,
                    "incident_end": 150,
                },
            )
            self.assertEqual(resp_rollout.status_code, 200)
            data_r = resp_rollout.json()
            self.assertTrue(data_r["success"])
            self.assertEqual(data_r["execution_mode"], "mesoscopic_network")
            self.assertIn("SUMO", data_r.get("fallback_reason", ""))

            # 2. /api/evaluate/multi-seed with physical sandbox forced
            resp_multi = self.client.post(
                "/api/evaluate/multi-seed",
                json={
                    "run_physical_sandbox": True,
                    "seeds": [42, 101],
                    "duration": 300,
                    "incident_start": 50,
                    "incident_end": 150,
                },
            )
            self.assertEqual(resp_multi.status_code, 200)
            data_m = resp_multi.json()
            self.assertTrue(data_m["success"])
            self.assertEqual(data_m["execution_mode"], "calibrated_fallback_no_sumo")
            self.assertIn("SUMO Sandbox notice", data_m.get("fallback_reason", ""))

        finally:
            global_agent.sandbox.sumo_bin = old_bin


if __name__ == "__main__":
    unittest.main()
