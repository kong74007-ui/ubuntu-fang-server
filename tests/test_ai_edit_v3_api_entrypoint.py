from __future__ import annotations

import importlib
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


class _FakeHttpServer:
    instances = []

    def __init__(self, address, handler):
        self.address = address
        self.handler = handler
        self.served = False
        type(self).instances.append(self)

    def serve_forever(self):
        self.served = True


class AiEditV3ApiEntrypointTests(unittest.TestCase):
    def tearDown(self):
        sys.modules.pop("server.ai_edit_v3_api", None)
        _FakeHttpServer.instances.clear()

    def test_binds_private_port_without_starting_generic_content_workers(self):
        self.assertTrue((Path(__file__).resolve().parents[1] / "server/ai_edit_v3_api.py").is_file())
        with (
            patch.dict(os.environ, {"AI_EDIT_V3_API_PORT": "18113"}, clear=False),
            patch("http.server.ThreadingHTTPServer", _FakeHttpServer),
        ):
            module = importlib.import_module("server.ai_edit_v3_api")
            with (
                patch.object(module.core, "init_db", side_effect=AssertionError("generic init")),
                patch.object(
                    module.core,
                    "start_job_workers",
                    side_effect=AssertionError("generic workers"),
                ),
                patch.object(
                    module.core,
                    "reclaim_orphaned_running",
                    side_effect=AssertionError("generic reclaim"),
                ),
            ):
                self.assertEqual(module.main(), 0)

        self.assertEqual(len(_FakeHttpServer.instances), 1)
        server = _FakeHttpServer.instances[0]
        self.assertEqual(server.address, ("127.0.0.1", 18113))
        self.assertIs(server.handler, module.core.H)
        self.assertTrue(server.served)


if __name__ == "__main__":
    unittest.main()
