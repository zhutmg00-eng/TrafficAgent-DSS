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

        # Verify comparative improvements
        comp_b = data["comparisons"]["strategy_b"]
        self.assertTrue(comp_b["delay_improvement_pct"] > 30.0)
        self.assertTrue(comp_b["queue_improvement_pct"] > 40.0)
        self.assertTrue(comp_b["speed_improvement_pct"] > 80.0)

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


if __name__ == "__main__":
    unittest.main()

