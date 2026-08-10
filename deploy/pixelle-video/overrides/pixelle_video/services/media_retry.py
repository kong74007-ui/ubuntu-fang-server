from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")
logger = logging.getLogger(__name__)


async def retry_async(
    operation: Callable[[], Awaitable[T]],
    *,
    operation_name: str,
    max_attempts: int = 3,
    initial_delay: float = 2.0,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """Retry an async provider operation with bounded exponential backoff."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    if initial_delay < 0:
        raise ValueError("initial_delay cannot be negative")

    delay = initial_delay
    for attempt in range(1, max_attempts + 1):
        try:
            return await operation()
        except Exception as exc:
            if attempt == max_attempts:
                logger.error(
                    f"{operation_name} failed after {max_attempts} attempts: {exc}"
                )
                raise
            logger.warning(
                f"{operation_name} failed on attempt {attempt}/{max_attempts}: "
                f"{exc}; retrying in {delay:.1f}s"
            )
            await sleep(delay)
            delay *= 2

    raise RuntimeError("unreachable retry state")
