from __future__ import annotations

import base64
import io
import json
from pathlib import Path
import tempfile
import time
import unittest

from server.content_domains.ai_edit_v3.providers.base import SecretValue
from server.content_domains.ai_edit_v3.providers.openai_image import OpenAIImageGenerator


class Response(io.BytesIO):
    status = 200

    def __init__(self, value):
        super().__init__(json.dumps(value).encode("utf-8"))


class Transport:
    def open(self, **kwargs):
        self.request = kwargs
        png = b"\x89PNG\r\n\x1a\n" + b"test-image"
        return Response({"id": "img-request-1", "data": [{"b64_json": base64.b64encode(png).decode()}], "usage": {"total_tokens": 12}})


class OpenAIImageGeneratorTests(unittest.TestCase):
    def test_generates_bounded_png_without_exposing_secret(self):
        transport = Transport()
        provider = OpenAIImageGenerator(api_key=SecretValue("test-only-secret-token"), transport=transport)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "image.png"
            result = provider.generate(prompt="通用行业方法视觉", ratio="9:16", output_path=output, idempotency_key="job-image-1", deadline_at=time.time() + 60)
            self.assertTrue(output.read_bytes().startswith(b"\x89PNG"))
        self.assertEqual("gpt-image-2", transport.request["json_body"]["model"])
        self.assertEqual(12, result.usage["tokens"])
        self.assertNotIn("test-only-secret-token", repr(provider))


if __name__ == "__main__":
    unittest.main()
