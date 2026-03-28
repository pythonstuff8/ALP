from __future__ import annotations

import logging
from typing import Any

import httpx

from .crypto import sign_protocol_object, sign_status_request
from .errors import (
    ALPAuthError,
    ALPRemoteExecutionError,
    ALPTimeoutError,
    ALPTransportError,
    ALPValidationError,
)
from .retry import with_retry
from .trust import TrustStore
from .types import PeerConfig, ResultContract, RetryPolicy, TaskEnvelope, TaskReceipt
from .validator import (
    validate_expected_output_schema,
    validate_output_against_schema,
    validate_result_contract,
    validate_task_envelope,
    validate_task_receipt,
)

LOGGER = logging.getLogger("alp.client")


class ALPClient:
    def __init__(
        self,
        issuer: str,
        key_id: str,
        private_key: str | bytes,
        *,
        trust_store: TrustStore | None = None,
        default_timeout_s: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.issuer = issuer
        self.key_id = key_id
        self.private_key = private_key
        self.trust_store = trust_store or TrustStore()
        self.default_timeout_s = default_timeout_s
        self.transport = transport

    def validate_task(self, task: TaskEnvelope | dict[str, Any]) -> None:
        payload = task.model_dump(mode="json", exclude_none=True) if isinstance(task, TaskEnvelope) else task
        validate_task_envelope(payload)

    def validate_result(self, result: ResultContract | dict[str, Any], expected_output_schema: dict[str, Any]) -> None:
        payload = result.model_dump(mode="json", exclude_none=True) if isinstance(result, ResultContract) else result
        validate_result_contract(payload)
        if payload["status"] == "success":
            validate_output_against_schema(payload.get("output"), expected_output_schema)

    async def submit(
        self,
        peer: PeerConfig,
        task: TaskEnvelope,
        *,
        retry: RetryPolicy | None = None,
    ) -> TaskReceipt | ResultContract:
        if task.issuer != self.issuer:
            raise ALPValidationError("task issuer does not match client issuer")
        if task.recipient != peer.agent_id:
            raise ALPValidationError("task recipient does not match selected peer")
        validate_expected_output_schema(task.expected_output_schema)
        signed_task = sign_protocol_object(
            {
                **task.model_dump(mode="json", exclude_none=True),
                "auth": {
                    **task.auth.model_dump(mode="json", exclude_none=True),
                    "key_id": self.key_id,
                },
            },
            self.private_key,
        )
        validate_task_envelope(signed_task)

        policy = retry or RetryPolicy()

        async def operation() -> TaskReceipt | ResultContract:
            timeout = self.default_timeout_s
            async with httpx.AsyncClient(timeout=timeout, transport=self.transport) as client:
                try:
                    response = await client.post(f"{peer.base_url}/alp/v1/tasks", json=signed_task)
                except httpx.TimeoutException as exc:
                    raise ALPTimeoutError("submit timed out") from exc
                except httpx.HTTPError as exc:
                    raise ALPTransportError(str(exc)) from exc
            if response.status_code == 200:
                payload = response.json()
                validate_result_contract(payload)
                public_key = self.trust_store.get_public_key(payload["issuer"], payload["auth"]["key_id"])
                from .crypto import verify_protocol_object

                verify_protocol_object(payload, public_key)
                validate_output_against_schema(payload["output"], task.expected_output_schema)
                if payload["status"] == "failure":
                    error = payload["error"]
                    raise ALPRemoteExecutionError(error["code"], error["message"], error["retriable"])
                return ResultContract.model_validate(payload)
            if response.status_code == 202:
                payload = response.json()
                validate_task_receipt(payload)
                return TaskReceipt.model_validate(payload)
            if response.status_code in {401, 403}:
                raise ALPAuthError(response.text)
            if response.status_code in {409, 422}:
                raise ALPValidationError(response.text)
            if response.status_code == 429:
                raise ALPTransportError("rate limited")
            raise ALPTransportError(f"unexpected status {response.status_code}: {response.text}")

        def should_retry(exc: Exception) -> bool:
            if isinstance(exc, ALPTransportError):
                return not isinstance(exc, ALPValidationError | ALPAuthError)
            return False

        result = await with_retry(operation, policy=policy, should_retry=should_retry)
        LOGGER.info("alp_submit_complete", extra={"task_id": task.task_id, "recipient": peer.agent_id})
        return result

    async def wait(
        self,
        peer: PeerConfig,
        task_id: str,
        *,
        timeout_s: float = 60.0,
        poll_interval_s: float = 2.0,
    ) -> ResultContract:
        import asyncio
        import time

        deadline = time.monotonic() + timeout_s
        path = f"/alp/v1/tasks/{task_id}"
        while time.monotonic() < deadline:
            headers = sign_status_request(path, self.issuer, self.key_id, self.private_key)
            async with httpx.AsyncClient(timeout=self.default_timeout_s, transport=self.transport) as client:
                try:
                    response = await client.get(f"{peer.base_url}{path}", headers=headers)
                except httpx.TimeoutException as exc:
                    raise ALPTimeoutError("status check timed out") from exc
                except httpx.HTTPError as exc:
                    raise ALPTransportError(str(exc)) from exc
            if response.status_code == 200:
                payload = response.json()
                validate_result_contract(payload)
                public_key = self.trust_store.get_public_key(payload["issuer"], payload["auth"]["key_id"])
                from .crypto import verify_protocol_object

                verify_protocol_object(payload, public_key)
                return ResultContract.model_validate(payload)
            if response.status_code == 202:
                await asyncio.sleep(poll_interval_s)
                continue
            if response.status_code == 404:
                await asyncio.sleep(poll_interval_s)
                continue
            raise ALPTransportError(f"unexpected status {response.status_code}: {response.text}")
        raise ALPTimeoutError("timed out waiting for terminal result")
