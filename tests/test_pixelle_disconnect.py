from __future__ import annotations

import asyncio
import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_disconnect_module():
    path = ROOT / "deploy/pixelle-video/overrides/api/disconnect.py"
    spec = importlib.util.spec_from_file_location("pixelle_disconnect", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PixelleDisconnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_operation_returns_result(self):
        disconnect = load_disconnect_module()

        class Request:
            async def is_disconnected(self):
                return False

        async def operation():
            return "image-result"

        result = await disconnect.await_with_disconnect(
            operation(),
            Request(),
            check_interval=0,
        )

        self.assertEqual("image-result", result)

    async def test_disconnected_client_cancels_provider_wait(self):
        disconnect = load_disconnect_module()
        cancelled = asyncio.Event()

        class Request:
            async def is_disconnected(self):
                return True

        async def operation():
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        with self.assertRaises(disconnect.ClientDisconnectedError):
            await disconnect.await_with_disconnect(
                operation(),
                Request(),
                check_interval=0,
            )

        self.assertTrue(cancelled.is_set())


if __name__ == "__main__":
    unittest.main()
