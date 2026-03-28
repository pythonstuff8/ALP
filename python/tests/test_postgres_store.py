from __future__ import annotations

import os
import uuid

import pytest

from alp.store import PostgresTaskStore


pytestmark = pytest.mark.asyncio


def postgres_database_url() -> str | None:
    return os.getenv("TEST_DATABASE_URL")


@pytest.mark.skipif(not postgres_database_url(), reason="TEST_DATABASE_URL is not configured")
async def test_postgres_store_roundtrip() -> None:
    database_url = postgres_database_url()
    assert database_url
    store = PostgresTaskStore(database_url)
    issuer = f"sender-{uuid.uuid4().hex[:8]}"
    task_id = f"tsk_{uuid.uuid4().hex[:24]}"
    nonce = f"nonce_{uuid.uuid4().hex[:12]}"

    task_payload = {
        "protocol_version": "alp.v1",
        "task_id": task_id,
        "task_type": "com.acme.echo.v1",
        "issuer": issuer,
        "recipient": "receiver-b",
        "created_at": "2026-03-28T14:00:00Z",
        "objective": "Echo a string",
        "inputs": {"message": "hello"},
        "constraints": {
            "deadline_at": "2026-03-28T14:05:00Z",
            "max_runtime_ms": 5000,
            "max_cost_usd": 0.5,
            "min_confidence": 0.5,
            "quality_tier": "standard",
        },
        "expected_output_schema": {
            "schema_dialect": "alp.output-schema.v1",
            "schema": {
                "type": "object",
                "properties": {"echo": {"type": "string"}},
                "required": ["echo"],
                "additionalProperties": False,
            },
        },
        "response_mode": {"mode": "sync", "sync_timeout_ms": 5000},
        "auth": {
            "alg": "Ed25519",
            "key_id": "sender-key",
            "issued_at": "2026-03-28T14:00:00Z",
            "expires_at": "2026-03-28T14:05:00Z",
            "nonce": nonce,
            "signature": "test-signature",
        },
    }
    result_payload = {
        "protocol_version": "alp.v1",
        "task_id": task_id,
        "issuer": "receiver-b",
        "status": "success",
        "output": {"echo": "hello"},
        "confidence": 0.9,
        "cost": {"currency": "USD", "total_usd": 0, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
        "trace": {
            "worker_id": "receiver-b",
            "received_at": "2026-03-28T14:00:00Z",
            "started_at": "2026-03-28T14:00:00Z",
            "completed_at": "2026-03-28T14:00:01Z",
            "attempts": 1,
            "steps": [{"index": 0, "kind": "validate", "status": "success", "duration_ms": 1}],
        },
        "auth": {
            "alg": "Ed25519",
            "key_id": "receiver-key",
            "issued_at": "2026-03-28T14:00:00Z",
            "expires_at": "2026-03-28T14:05:00Z",
            "nonce": "result-nonce",
            "signature": "result-signature",
        },
    }

    await store.start()
    try:
        assert await store.reserve_nonce(issuer, nonce, "hash-1") == "new"
        assert await store.reserve_nonce(issuer, nonce, "hash-1") == "duplicate"
        assert await store.reserve_nonce(issuer, nonce, "hash-2") == "conflict"

        created = await store.create_task(issuer, task_id, "hash-1", task_payload)
        assert created.state == "created"

        duplicate = await store.create_task(issuer, task_id, "hash-1", task_payload)
        assert duplicate.state == "duplicate"

        await store.save_result(issuer, task_id, result_payload)
        task, result = await store.status_snapshot(issuer, task_id)
        assert task
        assert result
        assert result["output"] == {"echo": "hello"}
        assert task["_store"]["status"] == "success"
    finally:
        await store.close()
