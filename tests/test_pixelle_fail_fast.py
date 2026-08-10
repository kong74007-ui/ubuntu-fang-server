from __future__ import annotations

import asyncio
import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_override(relative_path: str, module_name: str):
    path = ROOT / "deploy" / "pixelle-video" / "overrides" / relative_path
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PixelleFailFastTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_failure_cancels_running_and_waiting_frames(self):
        fail_fast = load_override(
            "pixelle_video/services/fail_fast.py", "pixelle_fail_fast"
        )
        retry = load_override(
            "pixelle_video/services/media_retry.py", "pixelle_media_retry_fail_fast"
        )
        semaphore = fail_fast.FailFastSemaphore(2)
        retry_budget = retry.RetryBudget(max_retries=10)
        provider_calls: list[int] = []
        provider_started = asyncio.Event()
        running_cancelled = asyncio.Event()
        waiting_cancelled = asyncio.Event()

        async def process_frame(index: int):
            try:
                async with semaphore:
                    if index == 0:
                        await provider_started.wait()
                        raise RuntimeError("frame 1 failed")

                    async def provider():
                        provider_calls.append(index)
                        provider_started.set()
                        await asyncio.Event().wait()

                    return await retry.retry_async(
                        provider,
                        operation_name=f"frame {index + 1}",
                        max_attempts=4,
                        retry_budget=retry_budget,
                        initial_delay=0,
                    )
            except asyncio.CancelledError:
                if index == 1:
                    running_cancelled.set()
                if index == 2:
                    waiting_cancelled.set()
                raise

        with self.assertRaisesRegex(RuntimeError, "frame 1 failed"):
            await fail_fast.gather_cancel_on_error(
                *(process_frame(index) for index in range(3))
            )

        self.assertEqual([1], provider_calls)
        self.assertTrue(running_cancelled.is_set())
        self.assertTrue(waiting_cancelled.is_set())
        self.assertEqual(0, retry_budget.used_retries)


if __name__ == "__main__":
    unittest.main()
