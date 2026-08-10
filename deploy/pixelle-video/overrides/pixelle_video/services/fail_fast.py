from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import TypeVar


T = TypeVar("T")


class FailFastSemaphore:
    """Semaphore that closes before releasing a failed holder's slot."""

    def __init__(self, value: int) -> None:
        self._semaphore = asyncio.Semaphore(value)
        self._failed = asyncio.Event()

    async def __aenter__(self) -> FailFastSemaphore:
        await self._semaphore.acquire()
        if self._failed.is_set():
            self._semaphore.release()
            raise asyncio.CancelledError()
        return self

    async def __aexit__(self, exc_type, _exc, _traceback) -> bool:
        if exc_type is not None:
            self._failed.set()
        self._semaphore.release()
        return False


async def gather_cancel_on_error(*awaitables: Awaitable[T]) -> list[T]:
    """Gather results while cancelling and draining siblings on first failure."""
    tasks = [asyncio.ensure_future(awaitable) for awaitable in awaitables]
    try:
        return await asyncio.gather(*tasks)
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
