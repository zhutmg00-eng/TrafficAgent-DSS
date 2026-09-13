"""
TrafficAgent-DSS: Real Road-Network Builder (OpenStreetMap → local network JSON)

Converts an Overpass API `out geom;` export (OSM ways with inline geometry) into a
compact, self-contained road-network model that the backend can render and simulate
without any external GIS dependency.

Why this exists
---------------
The original project shipped only a hand-drawn 3-intersection corridor
(`scenarios/corridor.*.xml`) while the README claimed an "OSM 真实路网". This builder
produces an *actual* OSM-derived network with real street names, geometry, lane counts
and speed limits, so the dashboard can show a genuine road map and the data layer has
a real topology to attach detector metrics to.

Output schema (scenarios/network_<key>.json)
--------------------------------------------
{
  "meta":  { source, key, label, bbox, origin:{lat,lon}, crs, generated, stats },
  "nodes": [ { id, x, y, lat, lon, degree, kind } ],
  "edges": [ { id, way_id, name, highway, lanes, oneway, speed_kmh,
               length_m, from, to, geometry:[[x,y],...] } ]
}

Coordinates x/y are local metres (equirectangular projection around the bbox centre),
so the frontend can render an SVG map with no geo libraries.

Usage:
    python scripts/build_network_from_osm.py \
        --input data/osm/xizhimen_raw.json \
        --output scenarios/network_xizhimen.json \
        --label "北京西直门枢纽真实路网"
"""

from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

# Drivable classes we keep, with default speed limits (km/h) and lane counts.
ROAD_CLASSES: Dict[str, Dict[str, float]] = {
    "motorway":       {"speed": 100, "lanes": 3},
    "trunk":          {"speed": 80,  "lanes": 3},
    "primary":        {"speed": 60,  "lanes": 3},
    "secondary":      {"speed": 50,  "lanes": 2},
    "tertiary":       {"speed": 40,  "lanes": 2},
    "motorway_link":  {"speed": 60,  "lanes": 1},
    "trunk_link":     {"speed": 50,  "lanes": 1},
    "primary_link":   {"speed": 40,  "lanes": 1},
    "secondary_link": {"speed": 30,  "lanes": 1},
}

# Highway classes that are one-way by definition unless tagged otherwise.
IMPLICIT_ONEWAY = {"motorway", "motorway_link"}

EARTH_R = 6371000.0


def _parse_maxspeed(value: Any) -> float | None:
    """Parses an OSM maxspeed tag ('50', '50 km/h', '30 mph') into km/h."""
    if value is None:
        return None
    s = str(value).strip().lower()
    if not s or s in ("signals", "variable", "none", "walk", "default"):
        return None
    try:
        if "mph" in s:
            return round(float(s.replace("mph", "").strip()) * 1.60934, 1)
        if "km/h" in s or "kmh" in s:
            return round(float(s.replace("km/h", "").replace("kmh", "").strip()), 1)
        return round(float(s), 1)
    except ValueError:
        return None


def _parse_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(float(str(value).strip()))
    except (ValueError, TypeError):
        return None


def _is_oneway(tags: Dict[str, Any]) -> bool:
    ow = str(tags.get("oneway", "")).strip().lower()
    if ow in ("yes", "true", "1", "reverse"):
        return True
    if ow in ("no", "false", "0"):
        return False
    # junction=roundabout implies oneway
    if str(tags.get("junction", "")).strip().lower() == "roundabout":
        return True
    return tags.get("highway") in IMPLICIT_ONEWAY


def build_network(osm: Dict[str, Any], key: str, label: str) -> Dict[str, Any]:
    ways: List[Dict[str, Any]] = []
    usage: Dict[int, int] = {}

    for el in osm.get("elements", []):
        if el.get("type") != "way":
            continue
        tags = el.get("tags") or {}
        hw = tags.get("highway")
        if hw not in ROAD_CLASSES:
            continue
        geom = el.get("geometry")
        nodelist = el.get("nodes")
        if not geom or not nodelist or len(geom) < 2 or len(nodelist) != len(geom):
            continue
        ways.append(el)
        for nid in nodelist:
            usage[nid] = usage.get(nid, 0) + 1

    if not ways:
        raise ValueError("No drivable OSM ways found in the export.")

    # bbox + projection origin
    lats = [g["lat"] for w in ways for g in w["geometry"]]
    lons = [g["lon"] for w in ways for g in w["geometry"]]
    lat0 = sum(lats) / len(lats)
    lon0 = sum(lons) / len(lons)
    cos_lat = math.cos(math.radians(lat0))

    def project(lat: float, lon: float) -> Tuple[float, float]:
        x = (lon - lon0) * math.radians(1.0) * EARTH_R * cos_lat
        y = (lat - lat0) * math.radians(1.0) * EARTH_R
        return round(x, 2), round(y, 2)

    nodes: Dict[int, Dict[str, Any]] = {}
    edges: List[Dict[str, Any]] = []

    def node_entry(nid: int, lat: float, lon: float) -> Dict[str, Any]:
        if nid not in nodes:
            x, y = project(lat, lon)
            nodes[nid] = {
                "id": f"n{nid}", "osm_id": nid, "x": x, "y": y,
                "lat": round(lat, 6), "lon": round(lon, 6),
                "degree": 0, "kind": "junction",
            }
        return nodes[nid]

    for way in ways:
        tags = way.get("tags") or {}
        hw = tags["highway"]
        cls = ROAD_CLASSES[hw]
        nodelist: List[int] = way["nodes"]
        geom = way["geometry"]

        name = tags.get("name") or tags.get("ref") or f"{hw} 无名路段"
        lanes = _parse_int(tags.get("lanes")) or int(cls["lanes"])
        speed = _parse_maxspeed(tags.get("maxspeed")) or cls["speed"]
        oneway = _is_oneway(tags)

        # Split the way at junction nodes (used by >1 way) and at its ends.
        split_idx = [0]
        for i in range(1, len(nodelist) - 1):
            if usage.get(nodelist[i], 0) > 1:
                split_idx.append(i)
        split_idx.append(len(nodelist) - 1)

        for s_i in range(len(split_idx) - 1):
            a, b = split_idx[s_i], split_idx[s_i + 1]
            seg_nodes = nodelist[a:b + 1]
            seg_geom = geom[a:b + 1]
            if len(seg_nodes) < 2:
                continue

            coords = [project(g["lat"], g["lon"]) for g in seg_geom]
            length = 0.0
            for p, q in zip(coords, coords[1:]):
                length += math.hypot(q[0] - p[0], q[1] - p[1])
            if length < 1.0:
                continue

            n_from = node_entry(seg_nodes[0], seg_geom[0]["lat"], seg_geom[0]["lon"])
            n_to = node_entry(seg_nodes[-1], seg_geom[-1]["lat"], seg_geom[-1]["lon"])

            n_from["degree"] += 1
            n_to["degree"] += 1

            edges.append({
                "id": f"e{way['id']}_{s_i}",
                "way_id": way["id"],
                "name": name,
                "highway": hw,
                "lanes": lanes,
                "oneway": bool(oneway),
                "speed_kmh": speed,
                "length_m": round(length, 1),
                "from": n_from["id"],
                "to": n_to["id"],
                "geometry": [list(c) for c in coords],
            })

    # Degree stats + a couple of geometry-based hints for bottleneck selection.
    degrees = [n["degree"] for n in nodes.values()]
    max_degree = max(degrees) if degrees else 0
    for n in nodes.values():
        if n["degree"] >= max(2, max_degree - 1) and max_degree >= 4:
            n["kind"] = "major_junction"

    total_len = sum(e["length_m"] for e in edges)
    return {
        "meta": {
            "source": "OpenStreetMap (Overpass API, ODbL)",
            "key": key,
            "label": label,
            "bbox": [min(lats), min(lons), max(lats), max(lons)],
            "origin": {"lat": round(lat0, 6), "lon": round(lon0, 6)},
            "crs": "local ENU metres (equirectangular around origin)",
            "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "stats": {
                "nodes": len(nodes),
                "edges": len(edges),
                "total_length_km": round(total_len / 1000.0, 3),
                "signalised_candidates": sum(1 for n in nodes.values() if n["degree"] >= 4),
            },
        },
        "nodes": list(nodes.values()),
        "edges": edges,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Build a local road-network JSON from an Overpass export.")
    ap.add_argument("--input", required=True, help="Overpass 'out geom;' JSON file")
    ap.add_argument("--output", required=True, help="Destination network JSON")
    ap.add_argument("--key", default="xizhimen", help="Short network key")
    ap.add_argument("--label", default="北京西直门枢纽真实路网", help="Human-readable label")
    args = ap.parse_args()

    src = Path(args.input)
    dst = Path(args.output)
    osm = json.loads(src.read_text(encoding="utf-8"))
    net = build_network(osm, key=args.key, label=args.label)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(net, ensure_ascii=False, indent=1), encoding="utf-8")
    st = net["meta"]["stats"]
    print(f"[network] {dst} -> nodes={st['nodes']} edges={st['edges']} "
          f"length={st['total_length_km']} km signals~{st['signalised_candidates']}")


if __name__ == "__main__":
    main()
