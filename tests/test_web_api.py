"""
TrafficAgent-DSS: Web API & RESTful Endpoint Unit Test Suite
Validates all FastAPI routes, request-response validation, and static serving.
"""

import sys
import unittest
from pathlib import Path
from fastapi.testclient import TestClient

# Add project root to sys.path
root_dir = Path(__file__).resolve().parent.parent
if str(root_dir) not in sys.path:
    sys.path.insert(0, str(root_dir))

from src.web.app import app


class TestWebAPI(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    def test_serve_index_html(self):
        """Tests that the root endpoint serves the interactive HTML dashboard."""
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn("TrafficAgent-DSS", response.text)
        self.assertIn("城市交通拥堵治理", response.text)

    def test_system_status(self):
        """Tests system health and metadata inspection endpoint."""
        response = self.client.get("/api/status")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "online")
        self.assertEqual(data["version"], "2.1.0")
        self.assertIn("Decoupled", data["architecture"])
        self.assertIn("agent_brain", data)
        self.assertEqual(data["agent_brain"]["state"], "ready")

    def test_diagnose_endpoint(self):
        """Tests situational diagnosis and Chain-of-Thought reasoning endpoint."""
        # Default payload
        response = self.client.post("/api/diagnose", json={})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["success"])
        self.assertIn("diagnosis", data)
        diag = data["diagnosis"]
        self.assertIn("cot_reasoning", diag)
        self.assertTrue(len(diag["cot_reasoning"]) >= 4)
        self.assertEqual(diag["bottleneck_location"], "J1_J2 (主干线合流段)")

        # Custom payload
        custom_state = {
            "bottleneck_edge": "West_2nd_Ring_Merge",
            "queue_m": 220.0,
            "link_length_m": 350.0,
            "speed_kmh": 6.5,
            "occupancy": 0.88,
            "bypass_occupancy": 0.32
        }
        res_custom = self.client.post("/api/diagnose", json=custom_state)
        self.assertEqual(res_custom.status_code, 200)
        data_custom = res_custom.json()
        self.assertEqual(data_custom["diagnosis"]["bottleneck_location"], "West_2nd_Ring_Merge")

    def test_strategies_endpoint(self):
        """Tests candidate strategy formulation endpoint."""
        response = self.client.post("/api/strategies", json={})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["success"])
        strategies = data["strategies"]
        self.assertIn("baseline", strategies)
        self.assertIn("strategy_a", strategies)
        self.assertIn("strategy_b", strategies)
        self.assertTrue(strategies["strategy_b"]["green_wave"])
        self.assertTrue(strategies["strategy_b"]["reroute_ratio"] > 0)

    def test_rollout_endpoint(self):
        """Tests What-If rollout simulation evaluation endpoint."""
        payload = {
            "duration": 600,
            "incident_start": 150,
            "incident_end": 420,
            "use_rerouting": True,
            "use_green_wave": True,
            "use_webster": True,
            "run_physical_sandbox": False
        }
        response = self.client.post("/api/rollout", json=payload)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["success"])
        self.assertIn("kpis", data)
        self.assertIn("comparisons", data)
        self.assertIn("time_series", data)
        self.assertIn("radar", data)

        # Non-physical mode now runs the real-network mesoscopic engine (no SUMO needed).
        self.assertEqual(data.get("execution_mode"), "mesoscopic_network")
        self.assertIn("detectors", data)
        self.assertIn("map_snapshot", data)

        # Verify comparative improvements (computed by the mesoscopic engine)
        comp_b = data["comparisons"]["strategy_b"]
        self.assertTrue(comp_b["delay_improvement_pct"] > 30.0)
        self.assertTrue(comp_b["queue_improvement_pct"] > 20.0)
        self.assertTrue(comp_b["speed_improvement_pct"] > 20.0)

        # Verify time-series structure
        ts = data["time_series"]
        self.assertIn("time_steps", ts)
        self.assertIn("queue_baseline", ts)
        self.assertIn("queue_strategy_b", ts)
        self.assertEqual(len(ts["time_steps"]), len(ts["queue_baseline"]))

    def test_report_export_endpoint(self):
        """Tests decision briefing generation endpoint."""
        response = self.client.post("/api/report/export", json={})
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["success"])
        self.assertIn("report_markdown", data)
        self.assertIn("城市交通拥堵治理辅助决策建议简报", data["report_markdown"])
        self.assertIn("方案 B", data["report_markdown"])

    def test_report_download_endpoint(self):
        """Tests direct Markdown download stream endpoint."""
        response = self.client.get("/api/report/download")
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response.headers.get("content-disposition", ""))
        self.assertIn("TrafficAgent_Decision_Briefing.md", response.headers.get("content-disposition", ""))
        self.assertIn("城市交通拥堵治理辅助决策建议简报", response.text)

    def test_baseline_dataset_endpoint(self):
        """Tests pre-calibrated baseline dataset delivery endpoint."""
        response = self.client.get("/api/baseline-data")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["success"])
        self.assertIn("traffic_state", data)
        self.assertIn("diagnosis", data)
        self.assertIn("strategies", data)
        self.assertIn("rollout", data)

    def test_rollout_validation_error(self):
        """Tests that invalid incident windows are rejected with 422 Unprocessable Entity."""
        # incident_start >= incident_end
        invalid_payload_1 = {
            "duration": 600,
            "incident_start": 400,
            "incident_end": 200
        }
        res1 = self.client.post("/api/rollout", json=invalid_payload_1)
        self.assertEqual(res1.status_code, 422)

        # incident_end > duration
        invalid_payload_2 = {
            "duration": 600,
            "incident_start": 150,
            "incident_end": 750
        }
        res2 = self.client.post("/api/rollout", json=invalid_payload_2)
        self.assertEqual(res2.status_code, 422)

    def test_rollout_component_ablation(self):
        """Tests rollout sensitivity when traffic control components are disabled."""
        payload_no_control = {
            "duration": 600,
            "incident_start": 150,
            "incident_end": 420,
            "use_rerouting": False,
            "use_green_wave": False,
            "use_webster": False,
            "run_physical_sandbox": False
        }
        res = self.client.post("/api/rollout", json=payload_no_control)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["success"])
        # Verify radar dimensions and values are bounded [0, 100]
        radar = data["radar"]
        self.assertEqual(len(radar["dimensions"]), 5)
        for score in radar["strategy_b"]:
            self.assertTrue(0 <= score <= 100)

    def test_custom_report_download_post(self):
        """Tests POST /api/report/download returns customized report file stream."""
        custom_payload = {
            "diagnosis": {
                "bottleneck_location": "West_2nd_Ring_Test",
                "severity_level": "严重拥堵 (Level 4)",
                "spillback_risk": "极高",
                "root_causes": ["合流匝道拥堵"],
                "cot_reasoning": ["1. 态势感知测试"]
            }
        }
        response = self.client.post("/api/report/download", json=custom_payload)
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response.headers.get("content-disposition", ""))
        self.assertIn("West_2nd_Ring_Test", response.text)

    def test_serve_local_vendor_echarts(self):
        """Tests that local vendor echarts.min.js is served properly for offline resilience."""
        response = self.client.get("/static/js/vendor/echarts.min.js")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(len(response.content) > 500000)

    def test_rollout_with_seed(self):
        """Tests that rollout accepts, respects, and returns random seed."""
        payload = {
            "duration": 600,
            "incident_start": 150,
            "incident_end": 420,
            "run_physical_sandbox": False,
            "seed": 2026,
        }
        response = self.client.post("/api/rollout", json=payload)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["success"])
        self.assertEqual(data.get("seed"), 2026)

    def test_evaluate_multi_seed_endpoint_fast(self):
        """Tests POST /api/evaluate/multi-seed fast calibrated evaluation."""
        payload = {
            "seeds": [42, 101, 2024],
            "duration": 600,
            "incident_start": 150,
            "incident_end": 420,
            "run_physical_sandbox": False,
        }
        response = self.client.post("/api/evaluate/multi-seed", json=payload)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["sample_size"], 3)
        self.assertEqual(data["seeds_tested"], [42, 101, 2024])
        self.assertIn("summary_by_scheme", data)
        self.assertIn("strategy_b_improvements", data)
        # Verify SEM and 95% CI presence
        delay_stat = data["strategy_b_improvements"]["delay_improvement_pct"]
        self.assertIn("sem", delay_stat)
        self.assertIn("ci_95", delay_stat)
        self.assertEqual(len(delay_stat["ci_95"]), 2)
        self.assertTrue(data["statistically_significant"])

    def test_evaluate_multi_seed_validation_errors(self):
        """Tests that invalid multi-seed inputs are rejected with 422."""
        # Empty seeds list
        res1 = self.client.post("/api/evaluate/multi-seed", json={"seeds": []})
        self.assertEqual(res1.status_code, 422)

        # Negative seed
        res2 = self.client.post("/api/evaluate/multi-seed", json={"seeds": [-5, 42]})
        self.assertEqual(res2.status_code, 422)

    def test_evaluate_multi_seed_endpoint_fallback_when_sumo_fails(self):
        """Tests that /api/evaluate/multi-seed gracefully falls back to calibrated mode if SUMO fails."""
        from unittest.mock import patch
        with patch("src.web.app.agent.run_multi_seed_evaluation", side_effect=RuntimeError("SUMO binary missing")):
            payload = {
                "seeds": [10, 20],
                "run_physical_sandbox": True,
            }
            response = self.client.post("/api/evaluate/multi-seed", json=payload)
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertTrue(data["success"])
            self.assertEqual(data["execution_mode"], "calibrated_fallback_no_sumo")
            self.assertIn("fallback_reason", data)
            self.assertIn("SUMO binary missing", data["fallback_reason"])
            self.assertEqual(data["sample_size"], 2)

    def test_strategies_endpoint_invalid_payload_returns_422(self):
        """Tests that invalid diagnosis payload to /api/strategies returns HTTP 422."""
        response = self.client.post("/api/strategies", json={"diagnosis": "invalid_not_a_dict"})
        self.assertEqual(response.status_code, 422)

    def test_report_export_invalid_kpis_returns_422(self):
        """Tests that malformed kpis structure in rollout_data returns HTTP 422."""
        invalid_payload = {
            "rollout_data": {
                "kpis": "invalid_string_instead_of_dict"
            }
        }
        response = self.client.post("/api/report/export", json=invalid_payload)
        self.assertEqual(response.status_code, 422)

    def test_report_download_post_invalid_kpis_returns_422(self):
        """Tests that malformed kpis in POST /api/report/download returns HTTP 422."""
        invalid_payload = {
            "rollout_data": {
                "kpis": 12345
            }
        }
        response = self.client.post("/api/report/download", json=invalid_payload)
        self.assertEqual(response.status_code, 422)

    def test_rollout_endpoint_sumo_failure_fallback_200(self):
        """Tests that /api/rollout gracefully falls back when physical sandbox execution fails."""
        from unittest.mock import patch
        with patch("src.web.app.agent.execute_what_if_rollout", side_effect=RuntimeError("SUMO process deadlocked")):
            payload = {
                "duration": 600,
                "incident_start": 150,
                "incident_end": 420,
                "run_physical_sandbox": True
            }
            response = self.client.post("/api/rollout", json=payload)
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertTrue(data["success"])
            # SUMO failed -> the real-network mesoscopic engine takes over (still a real,
            # computed simulation), and the reason is surfaced honestly.
            self.assertEqual(data["execution_mode"], "mesoscopic_network")
            self.assertIn("fallback_reason", data)
            self.assertIn("SUMO process deadlocked", data["fallback_reason"])
            self.assertIn("kpis", data)

    def test_decide_endpoint_one_stop(self):
        """Tests POST /api/decide executes full end-to-end diagnosis, formulation, rollout, and report."""
        # Test with empty body (default state)
        res = self.client.post("/api/decide", json={})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["success"])
        self.assertIn("traffic_state", data)
        self.assertIn("diagnosis", data)
        self.assertIn("strategies", data)
        self.assertIn("rollout", data)
        self.assertIn("report_markdown", data)
        self.assertIn("城市交通拥堵治理辅助决策建议简报", data["report_markdown"])

        # Test with custom traffic state
        custom_payload = {
            "traffic_state": {
                "bottleneck_edge": "East_Corridor_Ramp",
                "queue_m": 180.0,
                "speed_kmh": 12.0,
                "occupancy": 0.75
            },
            "run_physical_sandbox": False
        }
        res_custom = self.client.post("/api/decide", json=custom_payload)
        self.assertEqual(res_custom.status_code, 200)
        data_custom = res_custom.json()
        self.assertTrue(data_custom["success"])
        self.assertEqual(data_custom["diagnosis"]["bottleneck_location"], "East_Corridor_Ramp")

    def test_get_llm_config_endpoint(self):
        """Tests GET /api/llm/config returns non-sensitive status and masked credentials."""
        res = self.client.get("/api/llm/config")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "success")
        self.assertIn("llm", data)
        self.assertIn("model", data["llm"])
        self.assertIn("base_url", data["llm"])

    def test_post_llm_config_endpoint_updates_model(self):
        """Tests POST /api/llm/config hot-reloads the active model and endpoint."""
        payload = {
            "api_key": "sk-test12345678",
            "base_url": "https://api.deepseek.com/v1",
            "model": "deepseek-chat"
        }
        res = self.client.post("/api/llm/config", json=payload)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["llm"]["model"], "deepseek-chat")
        self.assertEqual(data["llm"]["base_url"], "https://api.deepseek.com/v1")
        self.assertTrue(data["llm"]["configured"])

    def test_post_llm_detect_models_handles_unreachable_endpoint_gracefully(self):
        """Tests POST /api/llm/detect-models gracefully reports errors without throwing 500."""
        payload = {
            "api_key": "sk-dummy-key",
            "base_url": "http://127.0.0.1:59999/v1"  # non-existent port
        }
        res = self.client.post("/api/llm/detect-models", json=payload)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertFalse(data["success"])
        self.assertEqual(data["models"], [])
        self.assertIsNotNone(data["error"])

    def test_post_llm_detect_models_success_mock(self):
        """Tests POST /api/llm/detect-models parses model list using mock."""
        from unittest.mock import patch
        with patch("src.agents.llm_client.LLMReasoningClient.list_available_models") as mock_list:
            mock_list.return_value = (["deepseek-chat", "deepseek-reasoner", "gpt-4o"], None)
            res = self.client.post("/api/llm/detect-models", json={
                "api_key": "sk-valid-key",
                "base_url": "https://api.deepseek.com/v1"
            })
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertTrue(data["success"])
            self.assertEqual(data["count"], 3)
            self.assertIn("deepseek-chat", data["models"])


    def test_get_network_endpoint(self):
        """Tests GET /api/network returns topology and live congestion snapshot."""
        res = self.client.get("/api/network")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("meta", data)
        self.assertIn("nodes", data)
        self.assertIn("edges", data)
        self.assertIn("bounds", data)
        self.assertIn("bottleneck_edge", data)
        self.assertIn("live", data)
        self.assertGreater(len(data["edges"]), 100)

    def test_get_detectors_endpoint(self):
        """Tests GET /api/detectors returns per-link detector records."""
        res = self.client.get("/api/detectors?limit=10")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data.get("engine"), "mesoscopic_network")
        self.assertIn("detectors", data)
        self.assertTrue(len(data["detectors"]) <= 10)
        if data["detectors"]:
            self.assertIn("edge_id", data["detectors"][0])
            self.assertIn("avg_speed_kmh", data["detectors"][0])

    def test_post_action_plan_endpoint(self):
        """Tests POST /api/action-plan returns structured operational directives."""
        res = self.client.post("/api/action-plan", json={})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data.get("success"))
        self.assertIn("steps", data)
        self.assertIn("plain_summary", data)
        self.assertGreater(len(data["steps"]), 0)
        first_step = data["steps"][0]
        self.assertIn("action", first_step)
        self.assertIn("owner", first_step)
        self.assertIn("verify", first_step)

    def test_decide_endpoint_contains_action_plan_and_plain_diagnosis(self):
        """Tests POST /api/decide integrates action_plan, plain_diagnosis, and Section 4 in markdown."""
        res = self.client.post("/api/decide", json={})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["success"])
        # Check plain_diagnosis in diagnosis
        diag = data.get("diagnosis", {})
        self.assertIn("plain_diagnosis", diag)
        # Check action_plan in strategies
        strat = data.get("strategies", {})
        self.assertIn("action_plan", strat)
        action_plan = strat["action_plan"]
        self.assertIn("steps", action_plan)
        self.assertGreater(len(action_plan["steps"]), 0)
        # Check Section 4 in markdown report
        report_md = data.get("report_markdown", "")
        self.assertIn("行动指令清单", report_md)


if __name__ == "__main__":
    unittest.main()


