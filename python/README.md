# alp python 🐍

Python reference implementation for ALP v1.

## Included here

| Part | Purpose |
| --- | --- |
| Protocol models | Pydantic objects for tasks, receipts, and results |
| Validation | JSON Schema checks plus ALP output schema subset rules |
| Signing | Deterministic canonical JSON plus Ed25519 signing and verification |
| Sender client | Async submit and wait helpers |
| Receiver server | FastAPI app with sync and callback handling |
| Demo store | SQLite backed task state for local runs |

## Main files

1. [`src/alp/client.py`](src/alp/client.py)
2. [`src/alp/server.py`](src/alp/server.py)
3. [`src/alp/store.py`](src/alp/store.py)
4. [`examples/research_agent.py`](examples/research_agent.py)

## Quick commands

```bash
python3 -m pip install -e '.[dev]'
python3 -m pytest tests
python3 examples/research_agent.py
```

