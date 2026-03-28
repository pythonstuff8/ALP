#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [ ! -f .env ]; then
  echo ".env is required for provider smoke tests" >&2
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "docker daemon is not available" >&2
  exit 1
fi

if grep -q 'CHANGE_ME_OPENAI_API_KEY\|CHANGE_ME_ANTHROPIC_API_KEY\|CHANGE_ME_OPENAI_MODEL_ID\|CHANGE_ME_ANTHROPIC_MODEL_ID' .env; then
  echo "provider smoke test requires real OpenAI and Anthropic credentials in .env" >&2
  exit 1
fi

cleanup() {
  docker compose down -v >/dev/null 2>&1 || true
}
trap cleanup EXIT

ALP_EXECUTOR_MODE=provider docker compose up --build -d postgres agent-b agent-a

for _ in $(seq 1 60); do
  if curl -fsS http://127.0.0.1:8000/readyz >/dev/null 2>&1 && curl -fsS http://127.0.0.1:8080/readyz >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

RESPONSE="$(curl -fsS -X POST http://127.0.0.1:8000/demo/pipeline/sync -H 'content-type: application/json' -d '{"prompt":"Generate three infrastructure startup ideas for AI agent teams","context":"Use ALP delegation as part of the workflow."}')"
printf '%s' "$RESPONSE" | python3 -c 'import json,sys; body=json.load(sys.stdin); assert body["result"]["status"]=="success"; print("provider top idea:", body["top_idea"]["id"])'
