# alp-typescript

TypeScript reference runtime for ALP v1.

## Included here

| Part | Purpose |
| --- | --- |
| Runtime types | Task, receipt, result, retry, and trust definitions |
| Validation | Ajv-based schema checks plus ALP output-schema subset rules |
| Signing | Canonical JSON plus Ed25519 signing and verification |
| Sender client | Submit and wait helpers over `fetch` |
| Receiver server | Fastify server with sync, callback, readiness, and metrics routes |
| Stores | Postgres for deployable runtime, file store for development fallback |
| Reference receiver | `examples/scoring-agent.ts` |

## Main files

1. [`src/client.ts`](./src/client.ts)
2. [`src/server.ts`](./src/server.ts)
3. [`src/store.ts`](./src/store.ts)
4. [`examples/scoring-agent.ts`](./examples/scoring-agent.ts)

## Quick commands

```bash
npm install
npm test
npm run build
npm run start:agent-b
```
