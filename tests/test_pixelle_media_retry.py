from __future__ import annotations

import asyncio
import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_retry_module():
    path = (
        ROOT
        / "deploy"
        / "pixelle-video"
        / "overrides"
        / "pixelle_video"
        / "services"
        / "media_retry.py"
    )
    spec = importlib.util.spec_from_file_location("pixelle_media_retry", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PixelleMediaRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_image_generation_retries_twice_then_returns_success(self):
        retry = load_retry_module()
        attempts = 0
        delays: list[float] = []

        async def operation():
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise RuntimeError(f"temporary failure {attempts}")
            return "image-result"

        async def fake_sleep(delay: float):
            delays.append(delay)

        result = await retry.retry_async(
            operation,
            operation_name="image generation for frame 2",
            max_attempts=3,
            initial_delay=2.0,
            sleep=fake_sleep,
        )

        self.assertEqual("image-result", result)
        self.assertEqual(3, attempts)
        self.assertEqual([2.0, 4.0], delays)

    async def test_image_generation_raises_last_error_after_three_attempts(self):
        retry = load_retry_module()
        attempts = 0
        delays: list[float] = []

        async def operation():
            nonlocal attempts
            attempts += 1
            raise RuntimeError(f"failure {attempts}")

        async def fake_sleep(delay: float):
            delays.append(delay)

        with self.assertRaisesRegex(RuntimeError, "failure 3"):
            await retry.retry_async(
                operation,
                operation_name="image generation for frame 1",
                max_attempts=3,
                initial_delay=2.0,
                sleep=fake_sleep,
            )

        self.assertEqual(3, attempts)
        self.assertEqual([2.0, 4.0], delays)

    async def test_cancellation_is_not_retried(self):
        retry = load_retry_module()
        attempts = 0

        async def operation():
            nonlocal attempts
            attempts += 1
            raise asyncio.CancelledError()

        with self.assertRaises(asyncio.CancelledError):
            await retry.retry_async(
                operation,
                operation_name="image generation for frame 1",
                max_attempts=3,
                initial_delay=0,
            )

        self.assertEqual(1, attempts)


if __name__ == "__main__":
    unittest.main()
