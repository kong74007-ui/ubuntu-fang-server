import json
import unittest
import urllib.error

from server.content_domains.ai_edit_v2_providers.base import (
    ProviderError,
    RetryableProviderError,
)
from server.content_domains.ai_edit_v2_providers.openai_image import OpenAIImageProvider


JOB = "123e4567-e89b-12d3-a456-426614174000"
SLOT = {
    "id": "slot_product_1",
    "semantic_query": "Clean product still life",
    "ratio": "16:9",
    "dimensions": {"width": 1920, "height": 1080},
    "time_range": {"start_ms": 0, "end_ms": 2_000},
    "required": False,
}


class FakeCos:
    def __init__(self, events, *, head=None):
        self.events = events
        self.head = head or {
            "content_length": 8,
            "content_type": "image/png",
            "etag": "etag-1",
        }

    def put_bytes(self, content, cos_key, content_type, private=True):
        self.events.append(("put", cos_key, content_type, private, content))

    def head_object(self, cos_key):
        self.events.append(("head", cos_key))
        return dict(self.head)


class FakeAssetStore:
    def __init__(self, events):
        self.events = events
        self.saved = None

    def find_generated_material(self, owner, job_id, idempotency_key, **kwargs):
        self.events.append(("find", owner, job_id, idempotency_key))
        return self.saved

    def create_generated_material(self, **fields):
        self.events.append(("create", fields))
        self.saved = {"id": 77, **fields}
        return dict(self.saved)


class OpenAIImageProviderTests(unittest.TestCase):
    def _provider(self, response=None, downloaded=None, *, head=None):
        events = []
        response = response or {
            "id": "img-request-1",
            "data": [{"url": "https://provider.example/transient/image.png?sig=secret"}],
            "usage": {"total_tokens": 12},
        }
        downloaded = downloaded or {"content": b"pngbytes", "content_type": "image/png"}

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
        self.assertEqual(operation_order, ["find", "request", "download", "put", "head", "create"])
        saved = store.saved
        self.assertNotIn("url", json.dumps(saved).lower())
        self.assertEqual(saved["owner"], "user-a")
        self.assertEqual(saved["job_id"], JOB)
        self.assertEqual(saved["size_bytes"], 8)
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
        self.assertEqual([event[0] for event in events], ["find"])

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

    def test_head_verification_must_match_uploaded_object_before_asset_record(self):
        provider, store, events = self._provider(
            head={"content_length": 7, "content_type": "image/png", "etag": "etag-1"}
        )

        with self.assertRaisesRegex(ProviderError, "image_cos_verification_failed"):
            provider.generate(SLOT, "bad-head")

        self.assertIsNone(store.saved)
        self.assertNotIn("create", [event[0] for event in events])

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
        }

        with self.assertRaisesRegex(ProviderError, "image_asset_scope_invalid"):
            provider.generate(SLOT, "cross-scope")

        self.assertEqual([event[0] for event in events], ["find"])


if __name__ == "__main__":
    unittest.main()
