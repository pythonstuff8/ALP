from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


@lru_cache(maxsize=None)
def load_schema(name: str) -> dict:
    path = _repo_root() / "schemas" / name
    return json.loads(path.read_text())


TASK_ENVELOPE_SCHEMA = load_schema("task-envelope.v1.json")
RESULT_CONTRACT_SCHEMA = load_schema("result-contract.v1.json")
TASK_RECEIPT_SCHEMA = load_schema("task-receipt.v1.json")

