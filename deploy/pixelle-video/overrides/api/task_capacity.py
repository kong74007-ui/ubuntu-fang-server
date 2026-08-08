from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional, Type


class TaskQueueFullError(RuntimeError):
    code = "task_queue_full"

    def __init__(self) -> None:
        super().__init__("video task queue is full")


class TaskCapacityReservation:
    def __init__(self, limiter: "TaskCapacityLimiter") -> None:
        self._limiter = limiter
        self._execution_acquired = False
        self._released = False

    async def __aenter__(self) -> None:
        try:
            await self._limiter._execution_slots.acquire()
            self._execution_acquired = True
        except BaseException:
            await self.release()
            raise

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        traceback: object,
    ) -> None:
        await self.release()

    async def release(self) -> None:
        async with self._limiter._admission_lock:
            if self._released:
                return
            if self._execution_acquired:
                self._limiter._execution_slots.release()
            self._limiter._release_locked()
            self._released = True


class TaskCapacityLimiter:
    def __init__(self, *, max_running: int, max_waiting: int) -> None:
        if isinstance(max_running, bool) or max_running < 1:
            raise ValueError("max_running must be a positive integer")
        if isinstance(max_waiting, bool) or max_waiting < 0:
            raise ValueError("max_waiting must be a non-negative integer")

        self._execution_slots = asyncio.Semaphore(max_running)
        self._admission_lock = asyncio.Lock()
        self._capacity = max_running + max_waiting
        self._admitted = 0

    @property
    def admitted(self) -> int:
        return self._admitted

    async def _admit(self) -> None:
        async with self._admission_lock:
            if self._admitted >= self._capacity:
                raise TaskQueueFullError()
            self._admitted += 1

    def _release_locked(self) -> None:
        self._admitted -= 1

    async def reserve(self) -> TaskCapacityReservation:
        await self._admit()
        return TaskCapacityReservation(self)

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        reservation = await self.reserve()
        async with reservation:
            yield


video_task_capacity = TaskCapacityLimiter(max_running=1, max_waiting=20)
