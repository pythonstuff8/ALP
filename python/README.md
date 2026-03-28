# alp-python

Python reference runtime for ALP v1.

## Included here

| Part | Purpose |
| --- | --- |
| Protocol models | Pydantic models for tasks, receipts, results, and retries |
| Validation | JSON Schema validation plus ALP output-schema subset checks |
| Signing | Canonical JSON plus Ed25519 signing and verification |
| Sender client | Async submit and wait helpers with result verification |
| Receiver server | FastAPI server with sync, callback, readiness, and metrics routes |
| Stores | Postgres for deployable runtime, SQLite for local tests and development |
| Reference sender | `examples/research_agent.py` |

## Main files

1. [`src/alp/client.py`](./src/alp/client.py)
2. [`src/alp/server.py`](./src/alp/server.py)
3. [`src/alp/store.py`](./src/alp/store.py)
4. [`examples/research_agent.py`](./examples/research_agent.py)

## Quick commands

```bash
python3 -m pip install -e '.[dev]'
python3 -m pytest tests
python3 examples/research_agent.py
```
