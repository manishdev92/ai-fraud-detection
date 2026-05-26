#!/usr/bin/env bash
# Local demo — requires API running on :8000
set -euo pipefail
BASE="${BASE_URL:-http://127.0.0.1:8000}"

echo "=== Health ==="
curl -s "$BASE/health" | python3 -m json.tool

echo ""
echo "=== Generate transactions ==="
curl -s -X POST "$BASE/generate-transactions" \
  -H "Content-Type: application/json" \
  -d '{"count": 500, "fraud_ratio": 0.08, "seed": 42}' | python3 -m json.tool

echo ""
echo "=== Run fraud investigation ==="
INV_RESPONSE=$(curl -s -X POST "$BASE/run-fraud-investigation" \
  -H "Content-Type: application/json" \
  -d '{"lookback_hours": 336}')
echo "$INV_RESPONSE" | python3 -m json.tool
INV=$(echo "$INV_RESPONSE" | python3 -c "import sys,json; print(json.load(sys.stdin)['investigation_id'])")

echo ""
echo "=== Findings (investigation $INV) ==="
curl -s "$BASE/findings?investigation_id=$INV&limit=5" | python3 -m json.tool

echo ""
echo "=== Latest report ==="
curl -s "$BASE/reports/latest" | python3 -c "import sys,json; r=json.load(sys.stdin); print(r['title']); print(r['body_markdown'][:800], '...')"
