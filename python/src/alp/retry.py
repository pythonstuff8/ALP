from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

from .types import RetryPolicy

T = TypeVar("T")


async def with_retry(
    operation: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy,
    should_retry: Callable[[Exception], bool],
) -> T:
    delay_ms = policy.base_delay_ms
    last_error: Exception | None = None
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return await operation()
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt >= policy.max_attempts or not should_retry(exc):
                raise
            jitter = delay_ms * policy.jitter_ratio
            wait_ms = max(0, delay_ms + random.uniform(-jitter, jitter))
            await asyncio.sleep(wait_ms / 1000)
            delay_ms = min(delay_ms * 2, policy.max_delay_ms)
    assert last_error is not None
    raise last_error

