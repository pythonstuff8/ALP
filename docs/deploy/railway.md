# Railway deployment

This repo deploys to Railway as three services:

1. Postgres
2. `agent-b` from [`typescript/Dockerfile`](../../typescript/Dockerfile)
3. `agent-a` from [`python/Dockerfile`](../../python/Dockerfile)

The deploy shape stays the same as local Docker Compose. Railway is just hosting the two containers and the database.

## Before you deploy

1. Generate a local `.env` with:

```bash
python3 scripts/generate_dev_keys.py
```

2. Replace the provider placeholders in `.env` if you want provider mode:

- `OPENAI_API_KEY`
- `OPENAI_MODEL_ID`
- `ANTHROPIC_API_KEY`
- `ANTHROPIC_MODEL_ID`

3. Decide the runtime mode:

- `mock` for transport-only verification
- `provider` for the full reference pipeline

## Create the Railway resources

### Postgres

Create a PostgreSQL service in Railway first. Copy the connection string and map it to `ALP_DATABASE_URL` for `agent-b`.

### Agent B

Create a service from this repo using:

- Dockerfile path: `typescript/Dockerfile`
- Port: `8080`

Set these environment variables:

- `ALP_ENV=production`
- `ALP_EXECUTOR_MODE=provider` or `mock`
- `ALP_AGENT_ID=scoring-agent-b`
- `ALP_KEY_ID=agent-b-key`
- `ALP_PRIVATE_KEY=<AGENT_B_PRIVATE_KEY>`
- `ALP_TRUST_ISSUER=research-agent-a`
- `ALP_TRUST_PUBLIC_KEY=<AGENT_A_PUBLIC_KEY>`
- `ALP_DATABASE_URL=<Railway Postgres URL>`
- `ALP_PUBLIC_BASE_URL=https://<agent-b-public-domain>`
- `ANTHROPIC_API_KEY=<real key when provider mode is used>`
- `ANTHROPIC_MODEL_ID=<tool-capable Claude model>`

### Agent A

Create a second service from this repo using:

- Dockerfile path: `python/Dockerfile`
- Port: `8000`

Set these environment variables:

- `ALP_ENV=production`
- `ALP_EXECUTOR_MODE=provider` or `mock`
- `ALP_AGENT_ID=research-agent-a`
- `ALP_KEY_ID=agent-a-key`
- `ALP_PRIVATE_KEY=<AGENT_A_PRIVATE_KEY>`
- `ALP_PEER_AGENT_ID=scoring-agent-b`
- `ALP_PEER_BASE_URL=https://<agent-b-public-domain>`
- `ALP_PEER_PUBLIC_KEY=<AGENT_B_PUBLIC_KEY>`
- `CALLBACK_PUBLIC_URL=https://<agent-a-public-domain>/callbacks/result`
- `OPENAI_API_KEY=<real key when provider mode is used>`
- `OPENAI_MODEL_ID=<structured-output capable model>`

## Smoke test after deploy

Health checks:

- `GET https://<agent-b-public-domain>/readyz`
- `GET https://<agent-a-public-domain>/readyz`

Sync pipeline:

```bash
curl -sS -X POST https://<agent-a-public-domain>/demo/pipeline/sync \
  -H 'content-type: application/json' \
  -d '{"prompt":"Find the next MCP or RAG infrastructure startup idea","context":"Prefer ideas that can be delegated and scored."}'
```

Async pipeline:

```bash
curl -sS -X POST https://<agent-a-public-domain>/demo/pipeline/async \
  -H 'content-type: application/json' \
  -d '{"prompt":"Generate AI infrastructure ideas for developer tools","context":"Focus on products that benefit from specialist agents."}'
```

Then poll the returned `result_url`.

## Operational notes

- `agent-b` is the only service that requires Postgres.
- If provider mode is enabled and a provider key is missing, readiness stays red and the process fails fast.
- Callback failures do not lose the final result. Agent A can still fetch the terminal result from Agent B by task ID.
- Use `GET /metrics` on either service for a small Prometheus-compatible metrics surface.
