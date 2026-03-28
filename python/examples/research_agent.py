from __future__ import annotations

import json
import os
import secrets
from collections import defaultdict
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from alp.client import ALPClient
from alp.crypto import verify_protocol_object
from alp.trust import TrustStore, TrustedPeer
from alp.types import AuthBlock, PeerConfig, ResponseMode, TaskConstraints, TaskEnvelope, TaskReceipt
from alp.validator import validate_output_against_schema, validate_result_contract

APP = FastAPI(title="ALP Demo Agent A")
CALLBACK_RESULTS: dict[str, dict[str, Any]] = {}
METRICS: dict[str, int] = defaultdict(int)


class PipelineRequest(BaseModel):
    prompt: str = Field(min_length=5, max_length=4000)
    context: str | None = Field(default=None, max_length=8000)


def env(name: str, default: str | None = None) -> str:
    value = os.getenv(name, default)
    if value is None:
        raise RuntimeError(f"missing environment variable {name}")
    return value


def env_optional(name: str) -> str | None:
    value = os.getenv(name)
    return value or None


ALP_ENV = env("ALP_ENV", "development")
EXECUTOR_MODE = env("ALP_EXECUTOR_MODE", "mock")
AGENT_ID = env("ALP_AGENT_ID", "research-agent-a")
KEY_ID = env("ALP_KEY_ID", "agent-a-key")
PRIVATE_KEY = env("ALP_PRIVATE_KEY", "CHANGE_ME_AGENT_A_PRIVATE_KEY")
PEER_AGENT_ID = env("ALP_PEER_AGENT_ID", "scoring-agent-b")
PEER_BASE_URL = env("ALP_PEER_BASE_URL", "http://localhost:8080")
PEER_PUBLIC_KEY = env("ALP_PEER_PUBLIC_KEY", "CHANGE_ME_AGENT_B_PUBLIC_KEY")
CALLBACK_PUBLIC_URL = env("CALLBACK_PUBLIC_URL", "http://localhost:8000/callbacks/result")
OPENAI_API_KEY = env_optional("OPENAI_API_KEY")
OPENAI_MODEL_ID = env_optional("OPENAI_MODEL_ID")

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

IDEA_GENERATION_SCHEMA = {
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
                        "title": {"type": "string", "minLength": 3, "maxLength": 120},
                        "problem": {"type": "string", "minLength": 20, "maxLength": 300},
                        "approach": {"type": "string", "minLength": 20, "maxLength": 300},
                    },
                    "required": ["id", "title", "problem", "approach"],
                    "additionalProperties": False,
                },
                "minItems": 3,
                "maxItems": 5,
            }
        },
        "required": ["ideas"],
        "additionalProperties": False,
    },
}

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


def validate_runtime_config() -> None:
    if ALP_ENV != "development":
        placeholders = {
            "ALP_PRIVATE_KEY": PRIVATE_KEY,
            "ALP_PEER_PUBLIC_KEY": PEER_PUBLIC_KEY,
            "CALLBACK_PUBLIC_URL": CALLBACK_PUBLIC_URL,
        }
        for name, value in placeholders.items():
            if value.startswith("CHANGE_ME"):
                raise RuntimeError(f"{name} must be configured outside development")
    if EXECUTOR_MODE == "provider":
        if not OPENAI_API_KEY or not OPENAI_MODEL_ID:
            raise RuntimeError("provider mode requires OPENAI_API_KEY and OPENAI_MODEL_ID")


def build_task(ideas: list[dict[str, str]], *, callback: bool) -> TaskEnvelope:
    task_id = "tsk_" + secrets.token_urlsafe(18).replace("-", "_")
    response_mode = (
        ResponseMode(mode="callback", callback={"url": CALLBACK_PUBLIC_URL, "timeout_ms": 5000})
        if callback
        else ResponseMode(mode="sync", sync_timeout_ms=20000)
    )
    return TaskEnvelope(
        task_id=task_id,
        task_type="com.acme.score_ideas.v1",
        issuer=AGENT_ID,
        recipient=PEER_AGENT_ID,
        created_at="2026-03-28T14:00:00Z",
        objective="Score generated product ideas against market, feasibility, and novelty",
        inputs={"ideas": ideas},
        constraints=TaskConstraints(
            deadline_at="2026-12-31T23:59:59Z",
            max_runtime_ms=30000,
            max_cost_usd=1.5,
            min_confidence=0.7,
            quality_tier="standard",
        ),
        expected_output_schema=IDEA_SCORE_SCHEMA,
        response_mode=response_mode,
        auth=AuthBlock(key_id=KEY_ID),
    )


async def generate_ideas(prompt: str, context: str | None) -> list[dict[str, str]]:
    if EXECUTOR_MODE == "provider":
        return await generate_ideas_openai(prompt, context)
    return generate_ideas_mock(prompt, context)


def generate_ideas_mock(prompt: str, context: str | None) -> list[dict[str, str]]:
    context_blurb = context or "No extra context provided."
    keywords = [token.strip(" ,.") for token in prompt.split() if len(token.strip(" ,.")) > 4][:3]
    seeds = keywords or ["agents", "mcp", "rag"]
    ideas = [
        {
            "id": f"idea-{index + 1}",
            "title": f"{seed.title()} Reliability Console",
            "problem": f"Teams lack a dependable way to measure {seed} performance drift and reliability in production. {context_blurb[:120]}",
            "approach": f"Use ALP delegation to benchmark {seed} workflows, persist scorecards, and route failures to specialized agents with clear contracts.",
        }
        for index, seed in enumerate(seeds)
    ]
    while len(ideas) < 3:
        ordinal = len(ideas) + 1
        ideas.append(
            {
                "id": f"idea-{ordinal}",
                "title": f"Agent Workflow Optimizer {ordinal}",
                "problem": "Operators struggle to compare which agent patterns actually reduce cost and latency without hurting output quality.",
                "approach": "Generate candidate workflows, score them through delegated specialists, and keep the best paths with signed result contracts.",
            }
        )
    payload = {"ideas": ideas[:5]}
    validate_output_against_schema(payload, IDEA_GENERATION_SCHEMA)
    return payload["ideas"]


async def generate_ideas_openai(prompt: str, context: str | None) -> list[dict[str, str]]:
    payload = {
        "model": OPENAI_MODEL_ID,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Generate product ideas as strict JSON only. Each idea must be plausible, specific, and useful for an AI infrastructure team."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Prompt:\n{prompt}\n\n"
                    f"Context:\n{context or 'No additional context.'}\n\n"
                    "Return 3 to 5 ideas. Keep them concrete enough to delegate for scoring."
                ),
            },
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": "idea_generation",
                "strict": True,
                "schema": IDEA_GENERATION_SCHEMA["schema"],
            },
        },
    }
    async with httpx.AsyncClient(timeout=45) as http:
        response = await http.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail=f"OpenAI request failed: {response.text}")
    body = response.json()
    try:
        message = body["choices"][0]["message"]
        if message.get("refusal"):
            raise ValueError(message["refusal"])
        parsed = json.loads(message["content"])
    except (KeyError, IndexError, TypeError, json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(status_code=502, detail=f"OpenAI response was not usable structured output: {exc}") from exc
    validate_output_against_schema(parsed, IDEA_GENERATION_SCHEMA)
    return parsed["ideas"]


def rank_scored_ideas(ideas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        ideas,
        key=lambda item: item["market_score"] + item["feasibility_score"] + item["novelty_score"],
        reverse=True,
    )


@APP.on_event("startup")
async def startup() -> None:
    validate_runtime_config()


@APP.post("/callbacks/result")
async def receive_result(payload: dict[str, Any]) -> dict[str, Any]:
    validate_result_contract(payload)
    public_key = trust_store.get_public_key(payload["issuer"], payload["auth"]["key_id"])
    verify_protocol_object(payload, public_key)
    CALLBACK_RESULTS[payload["task_id"]] = payload
    METRICS["callbacks_received_total"] += 1
    return {"accepted": True}


@APP.post("/demo/pipeline/sync")
async def pipeline_sync(body: PipelineRequest) -> dict[str, Any]:
    METRICS["pipeline_requests_total"] += 1
    ideas = await generate_ideas(body.prompt, body.context)
    METRICS["ideas_generated_total"] += len(ideas)
    task = build_task(ideas, callback=False)
    result = await client.submit(peer, task)
    if isinstance(result, TaskReceipt):
        raise HTTPException(status_code=500, detail="expected sync result")
    ranked = rank_scored_ideas(result.output["ideas"])
    return {
        "mode": EXECUTOR_MODE,
        "task_id": task.task_id,
        "ideas": ideas,
        "result": result.model_dump(mode="json"),
        "top_idea": ranked[0],
    }


@APP.post("/demo/pipeline/async")
async def pipeline_async(body: PipelineRequest) -> dict[str, Any]:
    METRICS["pipeline_requests_total"] += 1
    ideas = await generate_ideas(body.prompt, body.context)
    METRICS["ideas_generated_total"] += len(ideas)
    task = build_task(ideas, callback=True)
    result = await client.submit(peer, task)
    if isinstance(result, TaskReceipt):
        return {
            "mode": EXECUTOR_MODE,
            "task_id": task.task_id,
            "ideas": ideas,
            "receipt": result.model_dump(mode="json"),
            "result_url": f"/demo/result/{task.task_id}",
        }
    ranked = rank_scored_ideas(result.output["ideas"])
    return {
        "mode": EXECUTOR_MODE,
        "task_id": task.task_id,
        "ideas": ideas,
        "result": result.model_dump(mode="json"),
        "top_idea": ranked[0],
    }


@APP.get("/demo/result/{task_id}")
async def get_callback_result(task_id: str) -> dict[str, Any]:
    cached = CALLBACK_RESULTS.get(task_id)
    if cached:
        return cached
    try:
        result = await client.wait(peer, task_id, timeout_s=3, poll_interval_s=0.25, expected_output_schema=IDEA_SCORE_SCHEMA)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=404, detail=f"result not received yet: {exc}") from exc
    return result.model_dump(mode="json")


@APP.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "agent_id": AGENT_ID}


@APP.get("/readyz")
async def readyz() -> JSONResponse:
    provider_ready = EXECUTOR_MODE != "provider" or bool(OPENAI_API_KEY and OPENAI_MODEL_ID)
    payload = {
        "status": "ready" if provider_ready else "not_ready",
        "agent_id": AGENT_ID,
        "mode": EXECUTOR_MODE,
        "provider_ready": provider_ready,
        "peer_base_url": PEER_BASE_URL,
    }
    return JSONResponse(content=payload, status_code=200 if provider_ready else 503)


@APP.get("/metrics")
async def metrics() -> PlainTextResponse:
    lines = [
        "# HELP alp_sender_pipeline_requests_total Total pipeline requests handled by agent A.",
        "# TYPE alp_sender_pipeline_requests_total counter",
        f"alp_sender_pipeline_requests_total {METRICS['pipeline_requests_total']}",
        "# HELP alp_sender_ideas_generated_total Total ideas generated by agent A.",
        "# TYPE alp_sender_ideas_generated_total counter",
        f"alp_sender_ideas_generated_total {METRICS['ideas_generated_total']}",
        "# HELP alp_sender_callbacks_received_total Total callback results received by agent A.",
        "# TYPE alp_sender_callbacks_received_total counter",
        f"alp_sender_callbacks_received_total {METRICS['callbacks_received_total']}",
    ]
    return PlainTextResponse("\n".join(lines) + "\n")


if __name__ == "__main__":
    uvicorn.run(APP, host="0.0.0.0", port=int(env("PORT", "8000")))
