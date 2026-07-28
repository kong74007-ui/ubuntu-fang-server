import json
import os
import struct
import tempfile
import threading
import unittest
import urllib.error
import zlib
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

from server.content_domains.ai_edit_v2_providers.base import (
    ProviderError,
    RetryableProviderError,
)
from server.content_domains.ai_edit_v2_providers.openai_image import OpenAIImageProvider
from server.content_domains import ai_edit_v2_store as store


JOB = "123e4567-e89b-12d3-a456-426614174000"
SLOT = {
    "id": "slot_product_1",
    "semantic_query": "Clean product still life",
    "ratio": "16:9",
    "dimensions": {"width": 1920, "height": 1080},
    "time_range": {"start_ms": 0, "end_ms": 2_000},
    "required": False,
}


def png_bytes(width=1536, height=1024):
    def chunk(kind, data):
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    raw = (b"\x00" + b"\x00" * width) * height
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


class FakeCos:
    def __init__(self, events, *, head=None):
        self.events = events
        self.head = head
        self.objects = {}

    def put_bytes(self, content, cos_key, content_type, private=True):
        self.events.append(("put", cos_key, content_type, private, content))
        self.objects[cos_key] = (content, content_type)

    def head_object(self, cos_key):
        self.events.append(("head", cos_key))
        if self.head is not None:
            return dict(self.head)
        content, content_type = self.objects[cos_key]
        return {
            "content_length": len(content),
            "content_type": content_type,
            "etag": "etag-1",
        }


class FakeAssetStore:
    def __init__(self, events):
        self.events = events
        self.saved = None

    def reserve_generated_material(self, **fields):
        self.events.append(("reserve", fields))
        if self.saved is not None:
            return {"claimed": False, "material": dict(self.saved)}
        self.saved = {
            "id": 77,
            "owner": fields["owner"],
            "job_id": fields["job_id"],
            "cos_key": fields["cos_key"],
            "status": "pending",
        }
        return {"claimed": True, "material": dict(self.saved)}

    def complete_generated_material(self, **fields):
        self.events.append(("complete", fields))
        self.saved = {**self.saved, **fields, "status": "ready"}
        return dict(self.saved)

    def fail_generated_material(self, owner, job_id, idempotency_key, **kwargs):
        self.events.append(("fail", owner, job_id, idempotency_key))
        if self.saved is not None and self.saved["status"] == "pending":
            self.saved["status"] = "failed"
            return True
        return False


class OpenAIImageProviderTests(unittest.TestCase):
    def _provider(self, response=None, downloaded=None, *, head=None):
        events = []
        response = response or {
            "id": "img-request-1",
            "data": [{"url": "https://provider.example/transient/image.png?sig=secret"}],
            "usage": {"total_tokens": 12},
        }
        downloaded = downloaded or {"content": png_bytes(), "content_type": "image/png"}

        def request(method, url, headers, body, timeout):
            events.append(("request", method, url, headers, json.loads(body), timeout))
            return response

        def download(url, max_bytes, allowed_content_types, timeout):
            events.append(("download", url, max_bytes, allowed_content_types, timeout))
            return downloaded

        store = FakeAssetStore(events)
        provider = OpenAIImageProvider(
            owner="user-a",
            job_id=JOB,
            api_key="test-key",
            cos_api=FakeCos(events, head=head),
            asset_store=store,
            http_request=request,
            downloader=download,
            clock_ms=iter((100, 125)).__next__,
        )
        return provider, store, events

    def test_generate_downloads_then_stores_private_cos_and_returns_only_internal_record(self):
        provider, store, events = self._provider()

        result = provider.generate(SLOT, "job-attempt-1")

        self.assertEqual(result.provider, "openai")
        self.assertEqual(result.capability, "image_generation")
        self.assertEqual(result.request_id, "img-request-1")
        self.assertEqual(result.cost_units, 12)
        self.assertEqual(result.elapsed_ms, 25)
        self.assertEqual(result.payload["asset_id"], 77)
        self.assertRegex(
            result.payload["cos_key"],
            rf"^ai-edit-v2/[0-9a-f]{{16}}/{JOB}/generated/[0-9a-f-]+\.png$",
        )
        serialized = json.dumps(result.payload).lower()
        self.assertNotIn("provider.example", serialized)
        self.assertNotIn("url", serialized)
        operation_order = [event[0] for event in events]
        self.assertEqual(operation_order, ["reserve", "request", "download", "put", "head", "complete"])
        saved = store.saved
        self.assertNotIn("url", json.dumps(saved).lower())
        self.assertEqual(saved["owner"], "user-a")
        self.assertEqual(saved["job_id"], JOB)
        self.assertEqual(saved["size_bytes"], len(png_bytes()))
        self.assertEqual(saved["mime_type"], "image/png")
        self.assertEqual(saved["etag"], "etag-1")

        request = events[1]
        self.assertEqual(request[1:3], ("POST", "https://api.openai.com/v1/images/generations"))
        self.assertEqual(request[3]["Idempotency-Key"], "job-attempt-1")
        self.assertEqual(request[4]["model"], "gpt-image-2")
        self.assertEqual(request[4]["size"], "1536x1024")

    def test_retry_returns_existing_asset_without_external_resubmission(self):
        provider, store, events = self._provider()
        first = provider.generate(SLOT, "same-key")

        events.clear()
        second = provider.generate(SLOT, "same-key")

        self.assertEqual(second.payload, first.payload)
        self.assertEqual([event[0] for event in events], ["reserve"])

    def test_accepts_transport_request_id_when_images_body_has_no_id(self):
        provider, _, _ = self._provider(
            response={
                "_request_id": "request-from-header",
                "created": 123,
                "data": [{"url": "https://provider.example/image.png"}],
            }
        )

        result = provider.generate(SLOT, "header-request-id")

        self.assertEqual(result.request_id, "request-from-header")

    def test_rejects_download_over_size_limit_before_cos_write(self):
        provider, _, events = self._provider(
            downloaded={"content": b"x" * 11, "content_type": "image/png"}
        )
        provider.max_download_bytes = 10

        with self.assertRaisesRegex(ProviderError, "image_download_too_large"):
            provider.generate(SLOT, "too-large")

        self.assertNotIn("put", [event[0] for event in events])

    def test_definitive_request_rejection_is_not_marked_retryable(self):
        provider, _, _ = self._provider()
        provider.http_request = lambda *args: (_ for _ in ()).throw(
            urllib.error.HTTPError(args[1], 400, "bad request", {}, None)
        )

        with self.assertRaises(ProviderError) as caught:
            provider.generate(SLOT, "rejected")

        self.assertNotIsInstance(caught.exception, RetryableProviderError)
        self.assertEqual(str(caught.exception), "openai_image_request_rejected")

    def test_rate_limit_is_marked_retryable(self):
        provider, _, _ = self._provider()
        provider.http_request = lambda *args: (_ for _ in ()).throw(
            urllib.error.HTTPError(args[1], 429, "rate limited", {}, None)
        )

        with self.assertRaises(RetryableProviderError) as caught:
            provider.generate(SLOT, "rate-limited")

        self.assertEqual(str(caught.exception), "openai_image_unavailable")

    def test_rejects_non_image_download_before_cos_write(self):
        provider, _, events = self._provider(
            downloaded={"content": b"not-image", "content_type": "text/html"}
        )

        with self.assertRaisesRegex(ProviderError, "image_content_type_invalid"):
            provider.generate(SLOT, "wrong-type")

        self.assertNotIn("put", [event[0] for event in events])

    def test_rejects_mime_spoofed_bytes_before_cos_write(self):
        provider, store, events = self._provider(
            downloaded={"content": b"pngbytes", "content_type": "image/png"}
        )

        with self.assertRaisesRegex(ProviderError, "image_content_invalid"):
            provider.generate(SLOT, "mime-spoof")

        self.assertNotIn("put", [event[0] for event in events])
        self.assertEqual(store.saved["status"], "failed")

    def test_rejects_truncated_png_before_cos_write(self):
        provider, _, events = self._provider(
            downloaded={"content": png_bytes()[:-8], "content_type": "image/png"}
        )

        with self.assertRaisesRegex(ProviderError, "image_content_invalid"):
            provider.generate(SLOT, "truncated")

        self.assertNotIn("put", [event[0] for event in events])

    def test_rejects_actual_dimensions_that_do_not_match_requested_slot(self):
        provider, _, events = self._provider(
            downloaded={
                "content": png_bytes(width=1024, height=1536),
                "content_type": "image/png",
            }
        )

        with self.assertRaisesRegex(ProviderError, "image_dimensions_invalid"):
            provider.generate(SLOT, "wrong-dimensions")

        self.assertNotIn("put", [event[0] for event in events])

    def test_rejects_oversized_base64_before_attempting_decode(self):
        provider, _, events = self._provider(
            response={
                "id": "oversized-base64",
                "data": [{"b64_json": "!" * 20}],
            }
        )
        provider.max_download_bytes = 10

        with self.assertRaisesRegex(ProviderError, "image_download_too_large"):
            provider.generate(SLOT, "oversized-base64")

        self.assertNotIn("put", [event[0] for event in events])

    def test_head_verification_must_match_uploaded_object_before_asset_record(self):
        provider, store, events = self._provider(
            head={"content_length": 7, "content_type": "image/png", "etag": "etag-1"}
        )

        with self.assertRaisesRegex(ProviderError, "image_cos_verification_failed"):
            provider.generate(SLOT, "bad-head")

        self.assertEqual(store.saved["status"], "failed")
        self.assertNotIn("complete", [event[0] for event in events])

    def test_rejects_cross_scope_existing_asset(self):
        provider, store, events = self._provider()
        store.saved = {
            "id": 88,
            "owner": "user-b",
            "job_id": JOB,
            "cos_key": f"ai-edit-v2/0123456789abcdef/{JOB}/generated/x.png",
            "width": 1536,
            "height": 1024,
            "mime_type": "image/png",
            "size_bytes": 8,
            "status": "ready",
        }

        with self.assertRaisesRegex(ProviderError, "image_asset_scope_invalid"):
            provider.generate(SLOT, "cross-scope")

        self.assertEqual([event[0] for event in events], ["reserve"])

    def test_concurrent_workers_submit_the_same_idempotency_key_once(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "ai-edit-v2.db")
            store.init_db(db_path)
            job = store.create_job(
                "user-a", {"brief": "image"}, "quote-1", "job-key", 1,
                db_path=db_path,
            )
            events = []
            cos = FakeCos(events)
            start = threading.Barrier(2)
            request_started = threading.Event()
            release_request = threading.Event()
            call_lock = threading.Lock()
            http_calls = 0

            def request(method, url, headers, body, timeout):
                nonlocal http_calls
                with call_lock:
                    http_calls += 1
                request_started.set()
                release_request.wait(timeout=5)
                return {
                    "id": "request-winner",
                    "data": [{"url": "https://provider.example/image.png"}],
                }

            def download(url, max_bytes, allowed_content_types, timeout):
                return {"content": png_bytes(), "content_type": "image/png"}

            def run_worker():
                provider = OpenAIImageProvider(
                    owner="user-a",
                    job_id=job["id"],
                    api_key="test-key",
                    cos_api=cos,
                    asset_store=store,
                    http_request=request,
                    downloader=download,
                    db_path=db_path,
                )
                start.wait(timeout=5)
                return provider.generate(SLOT, "same-concurrent-key")

            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(run_worker) for _ in range(2)]
                try:
                    self.assertTrue(request_started.wait(timeout=5))
                    done, _ = wait(futures, timeout=5, return_when=FIRST_COMPLETED)
                    self.assertEqual(len(done), 1)
                    with self.assertRaisesRegex(
                        ProviderError, "openai_image_generation_in_progress"
                    ):
                        next(iter(done)).result()
                finally:
                    release_request.set()
                results = []
                for future in futures:
                    try:
                        results.append(future.result(timeout=5))
                    except ProviderError:
                        pass

            self.assertEqual(http_calls, 1)
            self.assertEqual(len(results), 1)


if __name__ == "__main__":
    unittest.main()
