from __future__ import annotations

import asyncio
import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_capacity_module():
    path = ROOT / "deploy" / "pixelle-video" / "overrides" / "api" / "task_capacity.py"
    spec = importlib.util.spec_from_file_location("pixelle_task_capacity", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PixelleTaskCapacityTests(unittest.IsolatedAsyncioTestCase):
    async def test_reservation_rejects_overflow_before_execution_starts(self):
        capacity = load_capacity_module()
        limiter = capacity.TaskCapacityLimiter(max_running=1, max_waiting=0)

        reservation = await limiter.reserve()
        self.assertEqual(1, limiter.admitted)
        with self.assertRaises(capacity.TaskQueueFullError):
            await limiter.reserve()

        async with reservation:
            self.assertEqual(1, limiter.admitted)
        self.assertEqual(0, limiter.admitted)

    async def test_only_one_task_executes_at_a_time(self):
        capacity = load_capacity_module()
        limiter = capacity.TaskCapacityLimiter(max_running=1, max_waiting=20)
        first_entered = asyncio.Event()
        release_first = asyncio.Event()
        second_entered = asyncio.Event()

        async def first():
            async with limiter.slot():
                first_entered.set()
                await release_first.wait()

        async def second():
            async with limiter.slot():
                second_entered.set()

        first_task = asyncio.create_task(first())
        await asyncio.wait_for(first_entered.wait(), timeout=1)
        second_task = asyncio.create_task(second())
        await asyncio.sleep(0)
        self.assertFalse(second_entered.is_set())

        release_first.set()
        await asyncio.wait_for(asyncio.gather(first_task, second_task), timeout=1)
        self.assertTrue(second_entered.is_set())
        self.assertEqual(0, limiter.admitted)

    async def test_twenty_waiting_tasks_are_admitted_and_next_is_rejected(self):
        capacity = load_capacity_module()
        limiter = capacity.TaskCapacityLimiter(max_running=1, max_waiting=20)
        release = asyncio.Event()
        entered = asyncio.Event()

        async def hold():
            async with limiter.slot():
                entered.set()
                await release.wait()

        tasks = [asyncio.create_task(hold()) for _ in range(21)]
        await asyncio.wait_for(entered.wait(), timeout=1)
        for _ in range(50):
            if limiter.admitted == 21:
                break
            await asyncio.sleep(0)
        self.assertEqual(21, limiter.admitted)

        with self.assertRaises(capacity.TaskQueueFullError):
            async with limiter.slot():
                self.fail("overflow task must not execute")

        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)
        self.assertEqual(0, limiter.admitted)

    async def test_cancelling_waiter_releases_admission_capacity(self):
        capacity = load_capacity_module()
        limiter = capacity.TaskCapacityLimiter(max_running=1, max_waiting=1)
        release = asyncio.Event()
        entered = asyncio.Event()

        async def hold():
            async with limiter.slot():
                entered.set()
                await release.wait()

        running = asyncio.create_task(hold())
        await asyncio.wait_for(entered.wait(), timeout=1)
        waiting = asyncio.create_task(hold())
        for _ in range(20):
            if limiter.admitted == 2:
                break
            await asyncio.sleep(0)
        self.assertEqual(2, limiter.admitted)

        waiting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiting
        self.assertEqual(1, limiter.admitted)

        replacement = asyncio.create_task(hold())
        for _ in range(20):
            if limiter.admitted == 2:
                break
            await asyncio.sleep(0)
        self.assertEqual(2, limiter.admitted)

        replacement.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await replacement
        release.set()
        await running
        self.assertEqual(0, limiter.admitted)

    async def test_cancelled_release_can_be_retried_without_leaking_capacity(self):
        capacity = load_capacity_module()
        limiter = capacity.TaskCapacityLimiter(max_running=1, max_waiting=0)
        reservation = await limiter.reserve()
        await reservation.__aenter__()

        await limiter._admission_lock.acquire()
        release_task = asyncio.create_task(reservation.release())
        await asyncio.sleep(0)
        release_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await release_task
        limiter._admission_lock.release()

        self.assertEqual(1, limiter.admitted)
        await reservation.release()
        self.assertEqual(0, limiter.admitted)


if __name__ == "__main__":
    unittest.main()
