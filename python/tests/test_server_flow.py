from __future__ import annotations

import httpx
import pytest

from alp.client import ALPClient
from alp.crypto import generate_keypair
from alp.server import ALPServer, TaskExecutor
from alp.store import SQLiteTaskStore
from alp.trust import TrustStore, TrustedPeer
from alp.types import AuthBlock, PeerConfig, ResponseMode, TaskConstraints, TaskEnvelope


class EchoExecutor(TaskExecutor):
    async def can_handle(self, task: TaskEnvelope) -> bool:
        return task.task_type == "com.acme.echo.v1"

    async def execute(self, task: TaskEnvelope) -> dict:
        return {"echo": task.inputs["message"]}


def build_task() -> TaskEnvelope:
    return TaskEnvelope(
        task_id="tsk_01JQ8Y0A6K4W8X7R9M1N2P3Q",
        task_type="com.acme.echo.v1",
        issuer="sender-a",
        recipient="receiver-b",
        created_at="2026-03-28T14:00:00Z",
        objective="Echo one string",
        inputs={"message": "hello"},
        constraints=TaskConstraints(
            deadline_at="2026-03-28T14:05:00Z",
            max_runtime_ms=5000,
            max_cost_usd=0.5,
            min_confidence=0.6,
            quality_tier="standard",
        ),
        expected_output_schema={
            "schema_dialect": "alp.output-schema.v1",
            "schema": {
                "type": "object",
                "properties": {"echo": {"type": "string"}},
                "required": ["echo"],
                "additionalProperties": False,
            },
        },
        response_mode=ResponseMode(mode="sync", sync_timeout_ms=5000),
        auth=AuthBlock(key_id="sender-key"),
    )


@pytest.mark.asyncio
async def test_sync_submit_and_deduplicate(tmp_path) -> None:
    sender_private, sender_public = generate_keypair()
    receiver_private, receiver_public = generate_keypair()

    receiver_trust = TrustStore()
    receiver_trust.add_peer(
        TrustedPeer(
            agent_id="sender-a",
            public_keys={"sender-key": sender_public},
            allowed_task_types=["com.acme.echo.v1"],
        )
    )
    server = ALPServer(
        agent_id="receiver-b",
        trust_store=receiver_trust,
        store=SQLiteTaskStore(str(tmp_path / "alp.db")),
        executor=EchoExecutor(),
        key_id="receiver-key",
        private_key=receiver_private,
    )
    app = server.create_app()
    transport = httpx.ASGITransport(app=app)

    client_trust = TrustStore()
    client_trust.add_peer(
        TrustedPeer(agent_id="receiver-b", public_keys={"receiver-key": receiver_public}, allowed_task_types=["com.acme.echo.v1"])
    )
    client = ALPClient(
        issuer="sender-a",
        key_id="sender-key",
        private_key=sender_private,
        trust_store=client_trust,
        transport=transport,
    )
    peer = PeerConfig(agent_id="receiver-b", base_url="http://testserver", public_keys={"receiver-key": receiver_public})

    result = await client.submit(peer, build_task())
    assert result.output == {"echo": "hello"}

    duplicate = await client.submit(peer, build_task())
    assert duplicate.output == {"echo": "hello"}

