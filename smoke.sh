#!/bin/bash
# 网关自检：$1 = base url（默认本机 4556）
B=${1:-http://127.0.0.1:4556}
cd "$(dirname "$0")"
K=$(./.venv/bin/python -c "import json;print(json.load(open('config.json'))['api_keys'][0])" 2>/dev/null)
echo "key=${K:0:12}..."
echo "== healthz =="; curl -s -m 5 "$B/healthz"; echo
echo "== /v1/models (带 key) =="; curl -s -m 10 -H "Authorization: Bearer $K" "$B/v1/models" | head -c 300; echo
echo "== /v1/models (无 key) =="; curl -s -m 10 "$B/v1/models" | head -c 120; echo
echo "== /v1/chat/completions (无账号时预期 502) =="
curl -s -m 20 -w "\nHTTP=%{http_code}\n" -X POST "$B/v1/chat/completions" \
  -H "Authorization: Bearer $K" -H "Content-Type: application/json" \
  -d '{"model":"imodel/spark-x","messages":[{"role":"user","content":"hi"}]}' | head -c 300; echo
echo "== /admin =="; curl -s -m 8 "$B/admin" | head -c 60; echo
echo "== /admin/api/state =="; curl -s -m 8 "$B/admin/api/state" | head -c 400; echo
