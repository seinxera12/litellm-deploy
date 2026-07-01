set -euo pipefail
echo "== chat (non-stream) =="
curl -fsS http://localhost:8000/v1/chat/completions -H 'Content-Type: application/json' \
  -d '{"model":"qfind-chat","messages":[{"role":"user","content":"hi"}]}' > /dev/null && echo OK
echo "== embed =="
curl -fsS http://localhost:8001/embed -H 'Content-Type: application/json' \
  -d '{"inputs":"hello"}' > /dev/null && echo OK
echo "== rerank =="
curl -fsS http://localhost:8002/rerank -H 'Content-Type: application/json' \
  -d '{"query":"x","texts":["x","y"]}' > /dev/null && echo OK
echo "ALL CHECKS PASSED"

# ---- Gateway checks (Day 2+) ----
VKEY="${LITELLM_VIRTUAL_KEY:-}"   # read from env or leave empty
if [ -z "$VKEY" ]; then
  echo "SKIP gateway checks: set LITELLM_VIRTUAL_KEY env var to enable"
else
  echo ""
  echo "=== Gateway checks (via LiteLLM port 4000) ==="
  echo "== gateway chat =="
  curl -fsS http://localhost:4000/v1/chat/completions \
    -H "Authorization: Bearer $VKEY" -H 'Content-Type: application/json' \
    -d '{"model":"qfind-chat","messages":[{"role":"user","content":"hi"}]}' > /dev/null && echo OK
  echo "== gateway embed =="
  curl -fsS http://localhost:4000/v1/embeddings \
    -H "Authorization: Bearer $VKEY" -H 'Content-Type: application/json' \
    -d '{"model":"qfind-embed","input":"hello"}' > /dev/null && echo OK
fi

echo "ALL CHECKS PASSED"