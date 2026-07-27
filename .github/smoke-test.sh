#!/usr/bin/env bash
# Start the server and exercise every endpoint.
#
# Runs in CI on a machine with no GPU, which is the point: every collector
# returns empty there, so this covers the graceful-degradation path. The server
# must still start and serve everything.
set -euo pipefail

PORT=18099
BASE="http://127.0.0.1:$PORT"
CONFIG="$(dirname "$0")/ci-config.json"

python3 spark-monitor.py --port "$PORT" --bind 127.0.0.1 --config "$CONFIG" &
SRV=$!
trap 'kill $SRV 2>/dev/null || true' EXIT

for _ in $(seq 1 30); do
  curl -fsS --max-time 2 "$BASE/healthz" >/dev/null 2>&1 && break
  sleep 1
done

fail() { echo "FAILED: $*" >&2; exit 1; }

echo "== endpoints return 200 =="
for p in /healthz /api/stats /api/settings /api/catalog /api/docs \
         /manifest.json /icon.png / "/api/history?h=24"; do
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 "$BASE$p")
  printf '  %-24s %s\n' "$p" "$code"
  [ "$code" = "200" ] || fail "$p returned $code"
done

echo "== unknown paths 404 rather than serving the dashboard =="
for p in /nope /docs/../../etc/passwd /assets/../spark-monitor.py; do
  code=$(curl -s -o /dev/null -w '%{http_code}' "$BASE$p")
  printf '  %-32s %s\n' "$p" "$code"
  [ "$code" != "200" ] || fail "$p should not return 200"
done

echo "== stats payload has the documented shape =="
curl -fsS --max-time 30 "$BASE/api/stats" | python3 -c '
import json, sys
d = json.load(sys.stdin)
for k in ("ts", "cluster_name", "nodes", "groups", "extras", "registry", "topology"):
    assert k in d, f"missing key: {k}"
print("  ok:", ", ".join(sorted(d)))'

echo "== the one write endpoint clamps hostile input =="
out=$(curl -fsS -X POST "$BASE/api/settings" \
      -H 'Content-Type: application/json' \
      -d '{"price_kwh":99999,"currency":"<script>alert(1)</script>","load_w":1,"idle_w":9999}')
echo "  $out"
python3 -c '
import json, sys
c = json.loads(sys.argv[1])
assert c["price_kwh"] <= 10, c
assert c["currency"] == "$", c
assert c["load_w"] > c["idle_w"], c
assert c["idle_w"] <= 1000, c
print("  validation holds")' "$out"

echo "== malformed bodies are rejected =="
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/api/settings" \
       -H 'Content-Type: application/json' -d 'not-json')
[ "$code" = "400" ] || fail "malformed body returned $code, expected 400"
echo "  malformed -> 400"

echo "== other paths reject POST =="
code=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE/api/stats")
[ "$code" = "404" ] || fail "POST /api/stats returned $code, expected 404"
echo "  POST /api/stats -> 404"

echo
echo "smoke test passed"
