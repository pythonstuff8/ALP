from __future__ import annotations

import os
import secrets
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException

from alp.client import ALPClient
from alp.crypto import verify_protocol_object
from alp.trust import TrustStore, TrustedPeer
from alp.types import AuthBlock, PeerConfig, ResponseMode, TaskConstraints, TaskEnvelope, TaskReceipt
from alp.validator import validate_result_contract

APP = FastAPI(title="ALP Demo Agent A")
CALLBACK_RESULTS: dict[str, dict[str, Any]] = {}


def env(name: str, default: str | None = None) -> str:
    value = os.getenv(name, default)
    if value is None:
        raise RuntimeError(f"missing environment variable {name}")
    return value


AGENT_ID = env("ALP_AGENT_ID", "research-agent-a")
KEY_ID = env("ALP_KEY_ID", "agent-a-key")
PRIVATE_KEY = env("ALP_PRIVATE_KEY", "Y8JhlL_nD98k1DuGRt5tJPy5cZ96EOK2U5m6FNO9D_M")
PEER_AGENT_ID = env("ALP_PEER_AGENT_ID", "scoring-agent-b")
PEER_BASE_URL = env("ALP_PEER_BASE_URL", "http://localhost:8080")
PEER_PUBLIC_KEY = env("ALP_PEER_PUBLIC_KEY", "PEZsH-5otmmf_brSp2tQKPVOsSNxk7hu-G6KEuZC8-U")
CALLBACK_PUBLIC_URL = env("CALLBACK_PUBLIC_URL", "http://localhost:8000/callbacks/result")

trust_store = TrustStore()
trust_store.add_peer(
    TrustedPeer(
        agent_id=PEER_AGENT_ID,
        public_keys={"agent-b-key": PEER_PUBLIC_KEY},
        allowed_task_types=["com.acme.score_ideas.v1"],
    )
)
client = ALPClient(issuer=AGENT_ID, key_id=KEY_ID, private_key=PRIVATE_KEY, trust_store=trust_store)
peer = PeerConfig(agent_id=PEER_AGENT_ID, base_url=PEER_BASE_URL, public_keys={"agent-b-key": PEER_PUBLIC_KEY})

IDEA_SCORE_SCHEMA = {
    "schema_dialect": "alp.output-schema.v1",
    "schema": {
        "type": "object",
        "properties": {
            "ideas": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "market_score": {"type": "number", "minimum": 0, "maximum": 10},
                        "feasibility_score": {"type": "number", "minimum": 0, "maximum": 10},
                        "novelty_score": {"type": "number", "minimum": 0, "maximum": 10},
                        "top_risks": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                            "maxItems": 3,
                        },
                        "rationale": {"type": "string", "minLength": 1, "maxLength": 400},
                    },
                    "required": [
                        "id",
                        "market_score",
                        "feasibility_score",
                        "novelty_score",
                        "top_risks",
                        "rationale",
                    ],
                    "additionalProperties": False,
                },
                "minItems": 1,
            }
        },
        "required": ["ideas"],
        "additionalProperties": False,
    },
}


def sample_ideas() -> list[dict[str, str]]:
    return [
        {
            "id": "idea-1",
            "title": "MCP Reliability Auditor",
            "problem": "Teams ship MCP servers without standardized health checks or schema conformance testing.",
            "approach": "Run delegated audits against MCP endpoints and return scorecards with failure clusters.",
        },
        {
            "id": "idea-2",
            "title": "RAG Drift Watch",
            "problem": "RAG pipelines degrade as indexes and retrieval prompts drift over time.",
            "approach": "Monitor retrieval quality, benchmark golden questions, and recommend index refreshes.",
        },
        {
            "id": "idea-3",
            "title": "Agent Budget Router",
            "problem": "Multi-agent systems overspend on expensive models for routine subtasks.",
            "approach": "Route tasks to cheaper specialized agents while preserving output contracts and audit trails.",
        },
    ]


def build_task(ideas: list[dict[str, str]], *, callback: bool) -> TaskEnvelope:
    task_id = "tsk_" + secrets.token_urlsafe(18).replace("-", "_")
    response_mode = (
        ResponseMode(mode="callback", callback={"url": CALLBACK_PUBLIC_URL, "timeout_ms": 5000})
        if callback
        else ResponseMode(mode="sync", sync_timeout_ms=15000)
    )
    return TaskEnvelope(
        task_id=task_id,
        task_type="com.acme.score_ideas.v1",
        issuer=AGENT_ID,
        recipient=PEER_AGENT_ID,
        created_at="2026-03-28T14:00:00Z",
        objective="Score ideas against market, feasibility, and novelty",
        inputs={"ideas": ideas},
        constraints=TaskConstraints(
            deadline_at="2026-03-28T14:05:00Z",
            max_runtime_ms=20000,
            max_cost_usd=0.4,
            min_confidence=0.7,
            quality_tier="standard",
        ),
        expected_output_schema=IDEA_SCORE_SCHEMA,
        response_mode=response_mode,
        auth=AuthBlock(key_id=KEY_ID),
    )


@APP.post("/callbacks/result")
async def receive_result(payload: dict[str, Any]) -> dict[str, Any]:
    validate_result_contract(payload)
    public_key = trust_store.get_public_key(payload["issuer"], payload["auth"]["key_id"])
    verify_protocol_object(payload, public_key)
    CALLBACK_RESULTS[payload["task_id"]] = payload
    return {"accepted": True}


@APP.post("/demo/score-sync")
async def score_sync(body: dict[str, Any] | None = None) -> dict[str, Any]:
    ideas = (body or {}).get("ideas") or sample_ideas()
    task = build_task(ideas, callback=False)
    result = await client.submit(peer, task)
    if isinstance(result, TaskReceipt):
        raise HTTPException(status_code=500, detail="expected sync result")
    ranked = sorted(
        result.output["ideas"],
        key=lambda item: item["market_score"] + item["feasibility_score"] + item["novelty_score"],
        reverse=True,
    )
    return {"task_id": task.task_id, "result": result.model_dump(mode="json"), "top_idea": ranked[0]}


@APP.post("/demo/score-async")
async def score_async(body: dict[str, Any] | None = None) -> dict[str, Any]:
    ideas = (body or {}).get("ideas") or sample_ideas()
    task = build_task(ideas, callback=True)
    receipt = await client.submit(peer, task)
    return receipt.model_dump(mode="json") if isinstance(receipt, TaskReceipt) else receipt.model_dump(mode="json")


@APP.get("/demo/result/{task_id}")
async def get_callback_result(task_id: str) -> dict[str, Any]:
    result = CALLBACK_RESULTS.get(task_id)
    if not result:
        raise HTTPException(status_code=404, detail="result not received yet")
    return result


if __name__ == "__main__":
    uvicorn.run(APP, host="0.0.0.0", port=int(env("PORT", "8000")))

