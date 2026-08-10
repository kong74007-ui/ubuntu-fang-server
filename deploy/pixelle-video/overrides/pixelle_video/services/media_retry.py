from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")
logger = logging.getLogger(__name__)


class RetryBudgetExceededError(RuntimeError):
    """Raised when a task has no remaining additional media retries."""


class RetryBudget:
    """Concurrency-safe shared budget for additional retry attempts."""

    def __init__(self, max_retries: int) -> None:
        if max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        self.max_retries = max_retries
        self.used_retries = 0
        self._lock = asyncio.Lock()

    async def reserve_retry(self) -> bool:
        async with self._lock:
            if self.used_retries >= self.max_retries:
                return False
            self.used_retries += 1
            return True


async def retry_async(
    operation: Callable[[], Awaitable[T]],
    *,
    operation_name: str,
    max_attempts: int = 3,
    attempt_timeout: float | None = None,
    retry_budget: RetryBudget | None = None,
    initial_delay: float = 2.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """Retry an async provider operation with bounded exponential backoff."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    if attempt_timeout is not None and attempt_timeout <= 0:
        raise ValueError("attempt_timeout must be positive")
    if initial_delay < 0:
        raise ValueError("initial_delay cannot be negative")

    delay = initial_delay
    for attempt in range(1, max_attempts + 1):
        try:
            pending = operation()
            if attempt_timeout is None:
                return await pending
            return await asyncio.wait_for(pending, timeout=attempt_timeout)
        except Exception as exc:
            if attempt == max_attempts:
                logger.error(
                    f"{operation_name} failed after {max_attempts} attempts: {exc}"
                )
                raise
            if retry_budget is not None and not await retry_budget.reserve_retry():
                logger.error(
                    f"{operation_name} cannot retry: task retry budget exhausted "
                    f"({retry_budget.used_retries}/{retry_budget.max_retries})"
                )
                raise RetryBudgetExceededError(
                    f"{operation_name} retry budget exhausted after "
                    f"{retry_budget.used_retries} additional retries"
                ) from exc
            logger.warning(
                f"{operation_name} failed on attempt {attempt}/{max_attempts}: "
                f"{exc}; retrying in {delay:.1f}s"
            )
            await sleep(delay)
            delay *= 2

    raise RuntimeError("unreachable retry state")
