from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class AuthBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    alg: Literal["Ed25519"] = "Ed25519"
    key_id: str
    issued_at: str = ""
    expires_at: str = ""
    nonce: str = ""
    signature: str = ""


class CallbackConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: HttpUrl
    timeout_ms: int = Field(ge=100, le=30000)


class ResponseMode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["sync", "callback"]
    sync_timeout_ms: int | None = Field(default=None, ge=100, le=300000)
    callback: CallbackConfig | None = None


class TaskConstraints(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deadline_at: str
    max_runtime_ms: int = Field(ge=100, le=3600000)
    max_cost_usd: float = Field(ge=0, le=1000)
    min_confidence: float = Field(ge=0, le=1)
    quality_tier: Literal["draft", "standard", "high"]


class TaskEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol_version: Literal["alp.v1"] = "alp.v1"
    task_id: str
    task_type: str
    issuer: str
    recipient: str
    created_at: str
    objective: str
    inputs: dict[str, Any]
    constraints: TaskConstraints
    expected_output_schema: dict[str, Any]
    response_mode: ResponseMode
    auth: AuthBlock


class TaskReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol_version: Literal["alp.v1"] = "alp.v1"
    task_id: str
    state: Literal["accepted", "duplicate"]
    accepted_at: str
    status_url: str


class CostTracking(BaseModel):
    model_config = ConfigDict(extra="forbid")

    currency: Literal["USD"] = "USD"
    total_usd: float = Field(default=0, ge=0)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)


class TraceStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    index: int = Field(ge=0, le=999)
    kind: Literal["validate", "plan", "model_call", "tool_call", "repair", "finalize"]
    provider: str | None = None
    model: str | None = None
    status: Literal["success", "failure"]
    duration_ms: int = Field(ge=0, le=3600000)


class ExecutionTrace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worker_id: str
    received_at: str
    started_at: str
    completed_at: str
    attempts: int = Field(ge=1, le=10)
    steps: list[TraceStep]


class ResultError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: Literal[
        "VALIDATION_ERROR",
        "UNSUPPORTED_SCHEMA",
        "UNAUTHORIZED",
        "REPLAY_DETECTED",
        "TASK_ID_CONFLICT",
        "CAPACITY_EXCEEDED",
        "REMOTE_TIMEOUT",
        "EXECUTION_ERROR",
        "OUTPUT_SCHEMA_MISMATCH",
        "CALLBACK_DELIVERY_FAILED",
    ]
    message: str
    retriable: bool
    details: dict[str, Any] = Field(default_factory=dict)


class ResultContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    protocol_version: Literal["alp.v1"] = "alp.v1"
    task_id: str
    issuer: str
    status: Literal["success", "failure"]
    output: Any | None = None
    confidence: float = Field(ge=0, le=1)
    cost: CostTracking
    trace: ExecutionTrace
    error: ResultError | None = None
    auth: AuthBlock


class RetryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_attempts: int = Field(default=4, ge=1, le=10)
    base_delay_ms: int = Field(default=250, ge=10, le=10000)
    max_delay_ms: int = Field(default=2000, ge=10, le=30000)
    jitter_ratio: float = Field(default=0.2, ge=0, le=1)


class PeerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent_id: str
    base_url: str
    public_keys: dict[str, str]
    allowed_task_types: list[str] = Field(default_factory=list)
    max_schema_bytes: int = Field(default=32768, ge=1024, le=1048576)
    max_sync_timeout_ms: int = Field(default=300000, ge=100, le=300000)
    callback_domain_allowlist: list[str] = Field(default_factory=list)

