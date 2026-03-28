from __future__ import annotations

import asyncio
import logging
from collections import defaultdict, deque
from collections.abc import Awaitable
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from time import perf_counter
from typing import Protocol

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from .crypto import sha256_hex, sign_protocol_object, verify_protocol_object, verify_status_headers
from .errors import ALPAuthError, ALPExecutionError, ALPValidationError
from .store import TaskStore
from .trust import TrustStore
from .types import AuthBlock, CostTracking, ExecutionTrace, ResultContract, ResultError, TaskEnvelope, TaskReceipt, TraceStep
from .validator import validate_callback_url, validate_output_against_schema, validate_result_contract, validate_task_envelope

LOGGER = logging.getLogger("alp.server")
CALLBACK_SCHEDULE_SECONDS = [5, 30, 120, 600, 1800]


class TaskExecutor(Protocol):
    async def can_handle(self, task: TaskEnvelope) -> bool:
        ...

    async def execute(self, task: TaskEnvelope) -> dict:
        ...


class _RateLimiter:
    def __init__(self) -> None:
        self._events: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, issuer: str, limit_per_minute: int) -> bool:
        now = perf_counter()
        window = self._events[issuer]
        while window and now - window[0] > 60:
            window.popleft()
        if len(window) >= limit_per_minute:
            return False
        window.append(now)
        return True


class ALPServer:
    def __init__(
        self,
        agent_id: str,
        trust_store: TrustStore,
        store: TaskStore,
        executor: TaskExecutor,
        *,
        key_id: str,
        private_key: str | bytes,
        local_sync_cap_ms: int = 300000,
        public_base_url: str | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.trust_store = trust_store
        self.store = store
        self.executor = executor
        self.key_id = key_id
        self.private_key = private_key
        self.local_sync_cap_ms = local_sync_cap_ms
        self.public_base_url = public_base_url.rstrip("/") if public_base_url else None
        self.rate_limiter = _RateLimiter()
        self._callback_loop_task: asyncio.Task | None = None
        self._callback_worker_started = False
        self._metrics: dict[str, int] = defaultdict(int)

    def create_app(self) -> FastAPI:
        @asynccontextmanager
        async def lifespan(_: FastAPI):
            await self.store.start()
            self._callback_loop_task = asyncio.create_task(self._callback_loop())
            self._callback_worker_started = True
            try:
                yield
            finally:
                if self._callback_loop_task:
                    self._callback_loop_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await self._callback_loop_task
                self._callback_worker_started = False
                await self.store.close()

        app = FastAPI(title=f"ALP Receiver {self.agent_id}", lifespan=lifespan)

        @app.post("/alp/v1/tasks")
        async def submit_task(request: Request) -> Response:
            try:
                payload = await request.json()
                return await self._handle_submit(payload, self._base_url(request))
            except HTTPException:
                raise
            except ALPAuthError as exc:
                raise HTTPException(status_code=401, detail=str(exc)) from exc
            except ALPValidationError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

        @app.get("/alp/v1/tasks/{task_id}")
        async def get_task(task_id: str, request: Request) -> Response:
            try:
                issuer = self._verify_status_request(task_id, request)
            except ALPAuthError as exc:
                raise HTTPException(status_code=401, detail=str(exc)) from exc
            task, result = await self.store.status_snapshot(issuer, task_id)
            if not task:
                raise HTTPException(status_code=404, detail="task not found")
            if result:
                return JSONResponse(content=result, status_code=200)
            return JSONResponse(
                content=TaskReceipt(
                    task_id=task_id,
                    state="accepted",
                    accepted_at=task["_store"]["accepted_at"],
                    status_url=self._status_url(self._base_url(request), task_id),
                ).model_dump(mode="json"),
                status_code=202,
            )

        @app.get("/healthz")
        async def healthz() -> Response:
            return JSONResponse(content={"status": "ok", "agent_id": self.agent_id}, status_code=200)

        @app.get("/readyz")
        async def readyz() -> Response:
            ready = await self.store.is_ready() and self._callback_worker_started
            payload = {
                "status": "ready" if ready else "not_ready",
                "agent_id": self.agent_id,
                "store_ready": await self.store.is_ready(),
                "callback_worker_started": self._callback_worker_started,
            }
            return JSONResponse(content=payload, status_code=200 if ready else 503)

        @app.get("/metrics")
        async def metrics() -> Response:
            lines = [
                "# HELP alp_requests_total Total ALP task submissions seen by this server.",
                "# TYPE alp_requests_total counter",
                f"alp_requests_total {self._metrics['alp_requests_total']}",
                "# HELP alp_validation_failures_total Total validation failures.",
                "# TYPE alp_validation_failures_total counter",
                f"alp_validation_failures_total {self._metrics['alp_validation_failures_total']}",
                "# HELP alp_execution_failures_total Total execution failures.",
                "# TYPE alp_execution_failures_total counter",
                f"alp_execution_failures_total {self._metrics['alp_execution_failures_total']}",
                "# HELP alp_callback_failures_total Total callback delivery failures after retries.",
                "# TYPE alp_callback_failures_total counter",
                f"alp_callback_failures_total {self._metrics['alp_callback_failures_total']}",
            ]
            return PlainTextResponse("\n".join(lines) + "\n")

        return app

    def _verify_status_request(self, task_id: str, request: Request) -> str:
        headers = {key: value for key, value in request.headers.items() if key.lower().startswith("x-alp-")}
        issuer = headers.get("x-alp-issuer")
        if not issuer:
            raise ALPAuthError("missing X-ALP-Issuer header")
        public_key = self.trust_store.get_public_key(issuer, headers.get("x-alp-key-id", ""))
        normalized_headers = {
            "X-ALP-Issuer": headers["x-alp-issuer"],
            "X-ALP-Key-Id": headers["x-alp-key-id"],
            "X-ALP-Issued-At": headers["x-alp-issued-at"],
            "X-ALP-Expires-At": headers["x-alp-expires-at"],
            "X-ALP-Nonce": headers["x-alp-nonce"],
            "X-ALP-Signature": headers["x-alp-signature"],
        }
        return verify_status_headers(f"/alp/v1/tasks/{task_id}", normalized_headers, public_key)

    async def _handle_submit(self, payload: dict, base_url: str) -> Response:
        self._metrics["alp_requests_total"] += 1
        validate_task_envelope(payload)
        task = TaskEnvelope.model_validate(payload)
        if task.recipient != self.agent_id:
            self._metrics["alp_validation_failures_total"] += 1
            raise ALPValidationError(f"recipient mismatch: expected {self.agent_id}")

        public_key = self.trust_store.get_public_key(task.issuer, task.auth.key_id)
        verify_protocol_object(payload, public_key)
        self.trust_store.validate_task_type(task.issuer, task.task_type)
        if not self.rate_limiter.allow(task.issuer, self.trust_store.require_peer(task.issuer).requests_per_minute):
            raise HTTPException(status_code=429, detail="rate limit exceeded")

        payload_hash = sha256_hex(self._idempotent_payload(payload))
        nonce_status = await self.store.reserve_nonce(task.issuer, task.auth.nonce, payload_hash)
        if nonce_status == "conflict":
            self._metrics["alp_validation_failures_total"] += 1
            raise ALPAuthError("nonce replay detected with different payload")

        insert = await self.store.create_task(task.issuer, task.task_id, payload_hash, payload)
        if insert.state == "conflict":
            raise HTTPException(status_code=409, detail="task_id already exists with different payload")
        if insert.state == "duplicate":
            if insert.result:
                return JSONResponse(content=insert.result, status_code=200)
            return JSONResponse(
                content=TaskReceipt(
                    task_id=task.task_id,
                    state="duplicate",
                    accepted_at=insert.task["_store"]["accepted_at"] if insert.task else datetime.now(UTC).isoformat(),
                    status_url=self._status_url(base_url, task.task_id),
                ).model_dump(mode="json"),
                status_code=202,
            )

        if not await self.executor.can_handle(task):
            self._metrics["alp_validation_failures_total"] += 1
            raise ALPValidationError(f"executor cannot handle task_type {task.task_type}")

        if task.response_mode.mode == "sync":
            sync_timeout_ms = min(
                task.constraints.max_runtime_ms,
                task.response_mode.sync_timeout_ms or self.local_sync_cap_ms,
                self.local_sync_cap_ms,
            )
            try:
                result = await asyncio.wait_for(self._execute_task(task), timeout=sync_timeout_ms / 1000)
            except TimeoutError:
                result = await self._execution_failure(task, "REMOTE_TIMEOUT", "sync execution timed out", retriable=True)
            await self.store.save_result(task.issuer, task.task_id, result)
            return JSONResponse(content=result, status_code=200)

        callback_url = str(task.response_mode.callback.url)
        validate_callback_url(callback_url, self.trust_store.callback_allowlist(task.issuer))
        await self.store.enqueue_callback(
            task.issuer,
            task.task_id,
            callback_url,
            task.response_mode.callback.timeout_ms,
            delay_seconds=0,
            attempt=0,
        )
        asyncio.create_task(self._run_async_execution(task))
        receipt = TaskReceipt(
            task_id=task.task_id,
            state="accepted",
            accepted_at=datetime.now(UTC).isoformat(),
            status_url=self._status_url(base_url, task.task_id),
        )
        return JSONResponse(content=receipt.model_dump(mode="json"), status_code=202)

    async def _run_async_execution(self, task: TaskEnvelope) -> None:
        result = await self._execute_task(task)
        await self.store.save_result(task.issuer, task.task_id, result)

    async def _execute_task(self, task: TaskEnvelope) -> dict:
        received_at = datetime.now(UTC).isoformat()
        started_at = datetime.now(UTC).isoformat()
        trace_steps = [TraceStep(index=0, kind="validate", status="success", duration_ms=0)]
        start = perf_counter()
        try:
            output = await self.executor.execute(task)
            validate_output_against_schema(output, task.expected_output_schema)
            completed_at = datetime.now(UTC).isoformat()
            trace_steps.append(
                TraceStep(index=1, kind="tool_call", status="success", duration_ms=max(1, int((perf_counter() - start) * 1000)))
            )
            trace_steps.append(TraceStep(index=2, kind="finalize", status="success", duration_ms=0))
            result = ResultContract(
                task_id=task.task_id,
                issuer=self.agent_id,
                status="success",
                output=output,
                confidence=max(task.constraints.min_confidence, 0.8),
                cost=CostTracking(),
                trace=ExecutionTrace(
                    worker_id=self.agent_id,
                    received_at=received_at,
                    started_at=started_at,
                    completed_at=completed_at,
                    attempts=1,
                    steps=trace_steps,
                ),
                error=None,
                auth=AuthBlock(key_id=self.key_id),
            ).model_dump(mode="json", exclude_none=True)
        except ALPExecutionError as exc:
            self._metrics["alp_execution_failures_total"] += 1
            result = await self._execution_failure(
                task,
                exc.code,
                str(exc),
                retriable=exc.retriable,
                details=exc.details,
                started_at=started_at,
                received_at=received_at,
                trace_kind="tool_call",
                duration_ms=max(1, int((perf_counter() - start) * 1000)),
            )
        except ALPValidationError as exc:
            self._metrics["alp_execution_failures_total"] += 1
            result = await self._execution_failure(
                task,
                "OUTPUT_SCHEMA_MISMATCH",
                str(exc),
                retriable=False,
                details={},
                started_at=started_at,
                received_at=received_at,
                trace_kind="repair",
                duration_ms=max(1, int((perf_counter() - start) * 1000)),
            )
        signed = sign_protocol_object(result, self.private_key)
        validate_result_contract(signed)
        LOGGER.info(
            "alp_execute_complete",
            extra={
                "task_id": task.task_id,
                "issuer": task.issuer,
                "recipient": task.recipient,
                "task_type": task.task_type,
                "result_status": signed["status"],
                "latency_ms": max(1, int((perf_counter() - start) * 1000)),
            },
        )
        return signed

    async def _execution_failure(
        self,
        task: TaskEnvelope,
        code: str,
        message: str,
        *,
        retriable: bool,
        details: dict | None = None,
        started_at: str | None = None,
        received_at: str | None = None,
        trace_kind: str = "tool_call",
        duration_ms: int = 1,
    ) -> dict:
        completed_at = datetime.now(UTC).isoformat()
        result = ResultContract(
            task_id=task.task_id,
            issuer=self.agent_id,
            status="failure",
            output=None,
            confidence=0,
            cost=CostTracking(),
            trace=ExecutionTrace(
                worker_id=self.agent_id,
                received_at=received_at or completed_at,
                started_at=started_at or completed_at,
                completed_at=completed_at,
                attempts=1,
                steps=[
                    TraceStep(index=0, kind="validate", status="success", duration_ms=0),
                    TraceStep(index=1, kind=trace_kind, status="failure", duration_ms=duration_ms),
                ],
            ),
            error=ResultError(code=code, message=message, retriable=retriable, details=details or {}),
            auth=AuthBlock(key_id=self.key_id),
        ).model_dump(mode="json", exclude_none=True)
        return result

    async def _callback_loop(self) -> None:
        while True:
            await asyncio.sleep(1)
            due = await self.store.due_callbacks()
            for attempt in due:
                result = await self.store.get_result(attempt.issuer, attempt.task_id)
                if not result:
                    continue
                try:
                    async with httpx.AsyncClient(timeout=attempt.timeout_ms / 1000) as client:
                        response = await client.post(attempt.url, json=result)
                    response.raise_for_status()
                    await self.store.mark_callback_delivered(attempt.callback_id)
                except Exception as exc:  # noqa: BLE001
                    next_attempt_index = attempt.attempt + 1
                    if next_attempt_index >= len(CALLBACK_SCHEDULE_SECONDS):
                        self._metrics["alp_callback_failures_total"] += 1
                        await self.store.fail_callback(attempt.callback_id, error=str(exc))
                    else:
                        await self.store.reschedule_callback(
                            attempt.callback_id,
                            delay_seconds=CALLBACK_SCHEDULE_SECONDS[next_attempt_index],
                            error=str(exc),
                        )

    @staticmethod
    def _idempotent_payload(payload: dict) -> dict:
        clone = dict(payload)
        auth = dict(clone.get("auth", {}))
        auth["issued_at"] = ""
        auth["expires_at"] = ""
        auth["nonce"] = ""
        auth["signature"] = ""
        clone["auth"] = auth
        return clone

    def _base_url(self, request: Request) -> str:
        return self.public_base_url or str(request.base_url).rstrip("/")

    def _status_url(self, base_url: str, task_id: str) -> str:
        return f"{base_url}/alp/v1/tasks/{task_id}"
