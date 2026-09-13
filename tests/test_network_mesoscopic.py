"""
Tests for the real-network data layer + mesoscopic engine + network API.

Supports both:
  py -3.10 -m unittest discover -s tests -p "test_*.py" -v
  pytest tests/test_network_mesoscopic.py -v
"""

import math
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.data.network import RoadNetwork
from src.simulation.mesoscopic import MesoscopicSimulator
from src.web import network_api as na


class TestNetworkMesoscopic(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.net = RoadNetwork.default(os.path.join(ROOT, "scenarios"))
        cls.sim = MesoscopicSimulator(cls.net)

    # --------------------------------------------------------------- network model
    def test_network_loads_real_osm_graph(self):
        net = self.net
        self.assertGreater(len(net.nodes), 100, "expected a real OSM network, not the 3-node toy corridor")
        self.assertGreater(len(net.edges), 100)
        self.assertGreater(net.total_length_m, 10_000)
        # every edge must have geometry and a positive length
        for e in net.edges[:50]:
            self.assertGreaterEqual(len(e["geometry"]), 2)
            self.assertGreater(e["length_m"], 0)
            self.assertIn(e["from"], net.node_by_id)
            self.assertIn(e["to"], net.node_by_id)

    def test_bottleneck_is_deterministic_and_valid(self):
        net = self.net
        b1 = net.pick_bottleneck()
        b2 = net.pick_bottleneck()
        self.assertEqual(b1["id"], b2["id"])
        self.assertIn(b1["id"], net.edge_by_id)
        self.assertTrue(b1["name"])

    def test_to_api_shape(self):
        net = self.net
        api = net.to_api()
        for key in ("meta", "bounds", "nodes", "edges"):
            self.assertIn(key, api)
        self.assertEqual(set(api["bounds"]), {"min_x", "max_x", "min_y", "max_y"})
        e = api["edges"][0]
        for key in ("id", "from", "to", "name", "highway", "lanes", "speed_kmh", "length_m", "geometry"):
            self.assertIn(key, e)

    # ----------------------------------------------------------- mesoscopic engine
    def test_simulation_is_deterministic(self):
        sim, net = self.sim, self.net
        b = net.pick_bottleneck()["id"]
        kwargs = dict(duration=300, incident_start=60, incident_end=200, bottleneck_id=b)
        r1 = sim.run_scheme("baseline", **kwargs)
        r2 = sim.run_scheme("baseline", **kwargs)
        self.assertEqual(r1["kpis"], r2["kpis"])
        self.assertEqual(r1["bottleneck_series"]["queue"], r2["bottleneck_series"]["queue"])

    def test_per_link_series_present(self):
        sim, net = self.sim, self.net
        r = sim.run_scheme("baseline", duration=300, incident_start=60, incident_end=200)
        self.assertEqual(len(r["edge_series"]), len(net.edges))
        self.assertEqual(len(r["link_summary"]), len(net.edges))
        for key in ("speed", "queue", "flow", "occupancy", "delay"):
            self.assertEqual(len(r["bottleneck_series"][key]), len(r["time_steps"]))

    def test_kpis_are_finite_and_positive(self):
        sim = self.sim
        r = sim.run_scheme("baseline", duration=300, incident_start=60, incident_end=200)
        for k, v in r["kpis"].items():
            self.assertTrue(math.isfinite(v), f"{k} not finite")
            self.assertGreaterEqual(v, 0, f"{k} negative")

    def test_strategy_improves_over_baseline(self):
        sim, net = self.sim, self.net
        b = net.pick_bottleneck()["id"]
        common = dict(duration=600, incident_start=150, incident_end=420, bottleneck_id=b)
        base = sim.run_scheme("baseline", control={"webster": False, "green_wave": False,
                                                   "reroute_ratio": 0.0}, **common)
        strat = sim.run_scheme("agent_dss", control={"webster": True, "green_wave": True,
                                                     "reroute_ratio": 0.25, "cycle_length": 112,
                                                     "green_split_arterial": 84}, **common)
        kb, ks = base["kpis"], strat["kpis"]
        self.assertLess(ks["avg_delay_s"], kb["avg_delay_s"], "strategy should reduce average delay")
        self.assertLess(ks["max_queue_m"], kb["max_queue_m"], "strategy should reduce max queue")
        self.assertGreater(ks["avg_speed_kmh"], kb["avg_speed_kmh"], "strategy should raise average speed")
        self.assertGreaterEqual(ks["throughput_vph"], kb["throughput_vph"], "strategy should not reduce bottleneck discharge")

    def test_incident_degrades_during_window(self):
        sim = self.sim
        r = sim.run_scheme("baseline", duration=600, incident_start=150, incident_end=420,
                           step=10, bottleneck_id=None)
        steps = r["time_steps"]
        q = r["bottleneck_series"]["queue"]
        peak_idx = max(range(len(q)), key=lambda i: q[i])
        # the worst queue must occur inside the incident window
        self.assertTrue(150 <= steps[peak_idx] <= 420)

    # --------------------------------------------------------------- network API
    def test_network_api_rollout_contract(self):
        res = na.run_mesoscopic_rollout(duration=300, incident_start=60, incident_end=200)
        for key in ("engine", "network", "kpis", "comparisons", "time_series",
                    "map_snapshot", "detectors", "radar"):
            self.assertIn(key, res, f"missing {key}")
        self.assertEqual(res["engine"], "mesoscopic_network")
        self.assertEqual(set(res["map_snapshot"]), {"baseline", "strategy_a", "strategy_b"})
        self.assertTrue(res["map_snapshot"]["strategy_b"], "map snapshot must not be empty")
        self.assertTrue(res["detectors"], "detector table must not be empty")
        for d in res["detectors"][:5]:
            self.assertIn(d["level"], ("free", "moderate", "congested", "severe", "unknown"))

    def test_level_classification(self):
        self.assertEqual(na._level(50, 60), "free")
        self.assertEqual(na._level(20, 60), "congested")   # ratio 0.33
        self.assertEqual(na._level(15, 60), "severe")      # ratio 0.25
        self.assertEqual(na._level(0, 0), "unknown")


if __name__ == "__main__":
    unittest.main()
