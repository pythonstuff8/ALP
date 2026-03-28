#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [ ! -f .env ]; then
  python3 scripts/generate_dev_keys.py
fi

if ! docker info >/dev/null 2>&1; then
  echo "docker daemon is not available" >&2
  exit 1
fi

cleanup() {
  docker compose down -v >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker compose up --build -d postgres agent-b agent-a

wait_for_ready() {
  local url="$1"
  for _ in $(seq 1 60); do
    if curl -fsS "$url" >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  echo "timed out waiting for $url" >&2
  return 1
}

wait_for_ready "http://127.0.0.1:8080/readyz"
wait_for_ready "http://127.0.0.1:8000/readyz"

SYNC_RESPONSE="$(curl -fsS -X POST http://127.0.0.1:8000/demo/pipeline/sync -H 'content-type: application/json' -d '{"prompt":"Find the next MCP or RAG infrastructure startup idea","context":"Focus on infrastructure teams, reliability, and monetizable pain."}')"
echo "$SYNC_RESPONSE" | python3 -c 'import json,sys; body=json.load(sys.stdin); assert body["result"]["status"]=="success"; print("sync top idea:", body["top_idea"]["id"])'

ASYNC_RESPONSE="$(curl -fsS -X POST http://127.0.0.1:8000/demo/pipeline/async -H 'content-type: application/json' -d '{"prompt":"Generate AI infrastructure ideas for developer tools","context":"Prefer products that can delegate to specialist agents."}')"
TASK_ID="$(printf '%s' "$ASYNC_RESPONSE" | python3 -c 'import json,sys; print(json.load(sys.stdin)["task_id"])')"

for _ in $(seq 1 30); do
  if RESULT="$(curl -fsS "http://127.0.0.1:8000/demo/result/$TASK_ID" 2>/dev/null)"; then
    printf '%s' "$RESULT" | python3 -c 'import json,sys; body=json.load(sys.stdin); assert body["status"]=="success"; print("async result ideas:", len(body["output"]["ideas"]))'
    exit 0
  fi
  sleep 2
done

echo "async result was not available in time" >&2
exit 1
