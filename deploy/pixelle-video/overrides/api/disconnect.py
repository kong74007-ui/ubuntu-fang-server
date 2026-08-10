from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any, TypeVar


T = TypeVar("T")


class ClientDisconnectedError(RuntimeError):
    """Raised after cancelling work whose HTTP client has disconnected."""


async def _cancel_and_wait(task: asyncio.Task[Any]) -> None:
    if task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def await_with_disconnect(
    operation: Awaitable[T],
    request: Any,
    *,
    check_interval: float = 0.5,
) -> T:
    """Wait for an operation while cancelling it after the client disconnects."""

    task = asyncio.create_task(operation)
    try:
        while True:
            done, _ = await asyncio.wait(
                {task},
                timeout=check_interval,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if task in done:
                return task.result()
            if await request.is_disconnected():
                if task.done():
                    return task.result()
                await _cancel_and_wait(task)
                raise ClientDisconnectedError("client disconnected during image generation")
    except asyncio.CancelledError:
        await _cancel_and_wait(task)
        raise
    finally:
        await _cancel_and_wait(task)
