#!/usr/bin/env bash
# Fetch a real OSM road network and compile it into scenarios/network_<key>.json
#
# Usage:
#   scripts/fetch_osm_network.sh <key> <south> <west> <north> <east> [label]
# Example (Beijing Xizhimen hub):
#   scripts/fetch_osm_network.sh xizhimen 39.928 116.332 39.958 116.382 "北京西直门枢纽真实路网"
#
# Requires: curl, python3. Overpass mirrors are tried in order (the public endpoint is
# frequently rate-limited; a community mirror is used as fallback).
set -euo pipefail

KEY="${1:?key}"; S="${2:?south}"; W="${3:?west}"; N="${4:?north}"; E="${5:?east}"
LABEL="${6:-${KEY} real road network (OSM)}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RAW="${ROOT}/scenarios/_osm_raw_${KEY}.json"

QUERY="[out:json][timeout:90];(way[\"highway\"~\"^(motorway|trunk|primary|secondary|tertiary|motorway_link|trunk_link|primary_link|secondary_link)$\"](${S},${W},${N},${E}););out geom;"

MIRRORS=(
  "https://overpass.private.coffee/api/interpreter"
  "https://overpass.kumi.systems/api/interpreter"
  "https://overpass-api.de/api/interpreter"
)

echo "[fetch] bbox=${S},${W},${N},${E} key=${KEY}"
ok=0
for M in "${MIRRORS[@]}"; do
  echo "[fetch] trying ${M}"
  if curl -sS -m 120 -X POST --data-binary "$QUERY" "$M" -o "$RAW" \
     && [ "$(wc -c < "$RAW")" -gt 5000 ] && python3 -c "import json,sys;json.load(open('$RAW'))" 2>/dev/null; then
    ok=1; echo "[fetch] OK via ${M}"; break
  fi
done
[ "$ok" = "1" ] || { echo "[fetch] all mirrors failed"; exit 1; }

python3 "${ROOT}/scripts/build_network_from_osm.py" \
  --input "$RAW" --output "${ROOT}/scenarios/network_${KEY}.json" \
  --key "$KEY" --label "$LABEL"
rm -f "$RAW"
echo "[fetch] done -> scenarios/network_${KEY}.json"
