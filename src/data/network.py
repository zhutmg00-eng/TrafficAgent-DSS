"""
TrafficAgent-DSS: Road-network data model.

Loads a self-contained OSM-derived network (see scripts/build_network_from_osm.py) and
exposes topology + geometry to both the simulation layer and the REST API.

The original project shipped a 3-intersection hand-drawn corridor while claiming a real
OSM network; this module is what actually delivers the real network (591 nodes / 771
links for the Xizhimen hub) and lets the dashboard draw it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

_DEFAULT_NAME = "network_xizhimen.json"


class RoadNetwork:
    """In-memory road network graph with geometry."""

    def __init__(self, data: Dict[str, Any]):
        self.meta: Dict[str, Any] = data.get("meta", {})
        self.nodes: List[Dict[str, Any]] = data.get("nodes", [])
        self.edges: List[Dict[str, Any]] = data.get("edges", [])
        self.node_by_id: Dict[str, Dict[str, Any]] = {n["id"]: n for n in self.nodes}
        self.edge_by_id: Dict[str, Dict[str, Any]] = {e["id"]: e for e in self.edges}
        self._adj: Optional[Dict[str, List[Dict[str, Any]]]] = None
        self._out: Optional[Dict[str, List[Dict[str, Any]]]] = None

    # ---------------------------------------------------------------- load
    @classmethod
    def load(cls, path: str | Path) -> "RoadNetwork":
        p = Path(path)
        return cls(json.loads(p.read_text(encoding="utf-8")))

    @classmethod
    def default(cls, scenario_dir: Optional[str | Path] = None) -> "RoadNetwork":
        if scenario_dir is None:
            scenario_dir = Path(__file__).resolve().parent.parent.parent / "scenarios"
        p = Path(scenario_dir) / _DEFAULT_NAME
        if not p.exists():
            raise FileNotFoundError(f"Default network not found: {p}")
        return cls.load(p)

    # ------------------------------------------------------------- helpers
    @property
    def bounds(self) -> Dict[str, float]:
        xs = [n["x"] for n in self.nodes]
        ys = [n["y"] for n in self.nodes]
        return {"min_x": min(xs), "max_x": max(xs), "min_y": min(ys), "max_y": max(ys)}

    @property
    def total_length_m(self) -> float:
        return sum(float(e.get("length_m", 0.0)) for e in self.edges)

    def outgoing(self) -> Dict[str, List[Dict[str, Any]]]:
        """node_id -> list of edges leaving that node (respects oneway)."""
        if self._out is None:
            out: Dict[str, List[Dict[str, Any]]] = {}
            for e in self.edges:
                out.setdefault(e["from"], []).append(e)
                if not e.get("oneway"):
                    out.setdefault(e["to"], []).append(e)
            self._out = out
        return self._out

    def adjacency(self) -> Dict[str, List[Dict[str, Any]]]:
        """node_id -> [{edge_id, to, dir}] undirected adjacency (for map routing)."""
        if self._adj is None:
            adj: Dict[str, List[Dict[str, Any]]] = {}
            for e in self.edges:
                adj.setdefault(e["from"], []).append({"edge": e["id"], "to": e["to"], "dir": "fwd"})
                adj.setdefault(e["to"], []).append({"edge": e["id"], "to": e["from"], "dir": "rev"})
            self._adj = adj
        return self._adj

    def major_junctions(self, min_degree: int = 4, limit: int = 25) -> List[Dict[str, Any]]:
        js = [n for n in self.nodes if n.get("degree", 0) >= min_degree]
        js.sort(key=lambda n: n.get("degree", 0), reverse=True)
        return js[:limit]

    def pick_bottleneck(self) -> Dict[str, Any]:
        """
        Deterministically chooses a representative arterial bottleneck link:
        the highest-priority (motorway>trunk>primary>...) longest link near the network
        centre. Used when the caller does not specify a target edge.
        """
        prio = {"motorway": 5, "trunk": 4, "primary": 3, "secondary": 2, "tertiary": 1}
        cx = (self.bounds["min_x"] + self.bounds["max_x"]) / 2.0
        cy = (self.bounds["min_y"] + self.bounds["max_y"]) / 2.0

        def score(e: Dict[str, Any]) -> float:
            g = e["geometry"][len(e["geometry"]) // 2]
            dist = ((g[0] - cx) ** 2 + (g[1] - cy) ** 2) ** 0.5
            return prio.get(e.get("highway", ""), 0) * 1000.0 + float(e.get("length_m", 0)) - dist * 0.05

        best = max(self.edges, key=score)
        return best

    # ----------------------------------------------------------------- api
    def to_api(self) -> Dict[str, Any]:
        """Compact topology payload for the frontend map renderer."""
        return {
            "meta": self.meta,
            "bounds": self.bounds,
            "nodes": [
                {"id": n["id"], "x": n["x"], "y": n["y"], "degree": n.get("degree", 0),
                 "kind": n.get("kind", "junction")}
                for n in self.nodes
            ],
            "edges": [
                {
                    "id": e["id"], "from": e["from"], "to": e["to"],
                    "name": e.get("name", ""), "highway": e.get("highway", ""),
                    "lanes": e.get("lanes", 1), "oneway": e.get("oneway", False),
                    "speed_kmh": e.get("speed_kmh", 40), "length_m": e.get("length_m", 0.0),
                    "geometry": e.get("geometry", []),
                }
                for e in self.edges
            ],
        }

    def summary(self) -> Dict[str, Any]:
        return {
            "label": self.meta.get("label", ""),
            "source": self.meta.get("source", ""),
            "stats": self.meta.get("stats", {}),
            "bounds": self.bounds,
        }
