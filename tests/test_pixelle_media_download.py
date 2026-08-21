from __future__ import annotations

import asyncio
import importlib.util
import tempfile
import unittest
from pathlib import Path

import httpx


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = (
    ROOT
    / "deploy"
    / "pixelle-video"
    / "overrides"
    / "pixelle_video"
    / "services"
    / "media_download.py"
)


def load_module():
    spec = importlib.util.spec_from_file_location("pixelle_media_download", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeResponse:
    def __init__(self, content: bytes = b"image", status_code: int = 200):
        self.content = content
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("GET", "https://cdn.example.test/image.png")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("status failure", request=request, response=response)


class FakeClient:
    def __init__(self, outcomes):
        self.outcomes = outcomes
        self.calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def get(self, _url):
        outcome = self.outcomes[self.calls]
        self.calls += 1
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class PixelleMediaDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_transient_download_failures_retry_then_publish_atomically(self):
        module = load_module()
        request = httpx.Request("GET", "https://cdn.example.test/image.png?secret=hidden")
        client = FakeClient(
            [
                httpx.ConnectTimeout("", request=request),
                httpx.ReadTimeout("", request=request),
                FakeResponse(b"complete-image"),
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "frame.png"
            result = await module.download_with_retry(
                str(request.url),
                output,
                client_factory=lambda **_kwargs: client,
                sleep=lambda _delay: asyncio.sleep(0),
            )
            self.assertEqual(output, result)
            self.assertEqual(b"complete-image", output.read_bytes())
            self.assertEqual(3, client.calls)
            self.assertFalse(output.with_suffix(".png.part").exists())

    async def test_terminal_http_status_does_not_retry(self):
        module = load_module()
        client = FakeClient([FakeResponse(status_code=404)])
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(RuntimeError, "HTTP 404"):
                await module.download_with_retry(
                    "https://cdn.example.test/missing.png",
                    Path(tmp) / "frame.png",
                    client_factory=lambda **_kwargs: client,
                )
        self.assertEqual(1, client.calls)

    async def test_exhausted_timeout_has_nonempty_sanitized_error(self):
        module = load_module()
        request = httpx.Request("GET", "https://cdn.example.test/image.png?secret=hidden")
        client = FakeClient(
            [httpx.ReadTimeout(f"failed to read {request.url}", request=request)] * 3
        )
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError) as raised:
                await module.download_with_retry(
                    str(request.url),
                    Path(tmp) / "frame.png",
                    client_factory=lambda **_kwargs: client,
                    sleep=lambda _delay: asyncio.sleep(0),
                )
        message = str(raised.exception)
        self.assertIn("cdn.example.test", message)
        self.assertIn("ReadTimeout", message)
        self.assertNotIn(str(request.url), message)
        self.assertNotIn("secret=hidden", message)
        self.assertEqual(3, client.calls)

    async def test_retryable_http_errors_never_expose_signed_url(self):
        module = load_module()
        signed_url = (
            "https://cdn.example.test/image.png?"
            "X-Amz-Signature=TOPSECRET&token=hidden"
        )
        request = httpx.Request("GET", signed_url)
        for status in (429, 503):
            responses = [httpx.Response(status, request=request) for _ in range(3)]
            client = FakeClient(responses)
            with tempfile.TemporaryDirectory() as tmp, self.assertLogs(
                module.logger, level="WARNING"
            ) as logs:
                with self.assertRaises(RuntimeError) as raised:
                    await module.download_with_retry(
                        signed_url,
                        Path(tmp) / "frame.png",
                        client_factory=lambda **_kwargs: client,
                        sleep=lambda _delay: asyncio.sleep(0),
                    )
            combined = str(raised.exception) + "\n" + "\n".join(logs.output)
            self.assertIn(f"HTTP {status}", combined)
            self.assertIn("HTTPStatusError", combined)
            self.assertNotIn(signed_url, combined)
            self.assertNotIn("TOPSECRET", combined)
            self.assertNotIn("token=hidden", combined)
            self.assertEqual(3, client.calls)

    async def test_cancellation_is_not_retried(self):
        module = load_module()
        client = FakeClient([asyncio.CancelledError()])
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(asyncio.CancelledError):
                await module.download_with_retry(
                    "https://cdn.example.test/image.png",
                    Path(tmp) / "frame.png",
                    client_factory=lambda **_kwargs: client,
                )
        self.assertEqual(1, client.calls)


if __name__ == "__main__":
    unittest.main()
