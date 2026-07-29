import json
import os
import struct
import tempfile
import threading
import unittest
import urllib.error
import zlib
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from unittest.mock import patch

from server.content_domains.ai_edit_v2_providers.base import (
    ProviderError,
    RetryableProviderError,
)
from server.content_domains.ai_edit_v2_providers.openai_image import OpenAIImageProvider
from server.content_domains import ai_edit_v2_store as store


JOB = "123e4567-e89b-12d3-a456-426614174000"
ACCEPTANCE_ENV = "AI_EDIT_V2_OPENAI_IMAGE_IDEMPOTENCY_ACCEPTED"
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

    def find_generated_material(self, owner, job_id, idempotency_key, **kwargs):
        self.events.append(("find", owner, job_id, idempotency_key))
        return None if self.saved is None else dict(self.saved)

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
            "generation_state": "pre_submit",
            "generation_lease_owner": fields["lease_owner"],
            "generation_request_digest": fields["request_digest"],
        }
        return {"claimed": True, "reason": "claimed", "material": dict(self.saved)}

    def mark_generated_material_submitting(self, **fields):
        self.events.append(("submitting", fields))
        self.saved["generation_state"] = "submitting"
        return True

    def mark_generated_material_provider_confirmed(self, **fields):
        self.events.append(("confirmed", fields))
        self.saved["generation_state"] = "provider_confirmed"
        self.saved["generation_provider_request_id"] = fields["provider_request_id"]
        return True

    def mark_generated_material_recoverable(self, **fields):
        self.events.append(("recoverable", fields))
        self.saved["generation_state"] = fields["state"]
        self.saved["generation_retry_at"] = fields["retry_at"]
        self.saved["generation_lease_owner"] = None
        return True

    def complete_generated_material(self, **fields):
        self.events.append(("complete", fields))
        self.saved = {**self.saved, **fields, "status": "ready"}
        self.saved["generation_state"] = "ready"
        return dict(self.saved)

    def fail_generated_material(self, owner, job_id, idempotency_key, **kwargs):
        self.events.append(("fail", owner, job_id, idempotency_key))
        if self.saved is not None and self.saved["status"] == "pending":
            self.saved["status"] = "failed"
            self.saved["generation_state"] = "terminal_failed"
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
            now_seconds=lambda: 100,
            worker_id="worker-test",
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
        self.assertEqual(
            operation_order,
            ["reserve", "submitting", "request", "confirmed", "download", "put", "head", "complete"],
        )
        saved = store.saved
        self.assertNotIn("url", json.dumps(saved).lower())
        self.assertEqual(saved["owner"], "user-a")
        self.assertEqual(saved["job_id"], JOB)
        self.assertEqual(saved["size_bytes"], len(png_bytes()))
        self.assertEqual(saved["mime_type"], "image/png")
        self.assertEqual(saved["etag"], "etag-1")

        request = events[2]
        self.assertEqual(request[1:3], ("POST", "https://api.openai.com/v1/images/generations"))
        self.assertEqual(request[3]["Idempotency-Key"], "job-attempt-1")
        self.assertEqual(request[4]["model"], "gpt-image-2")
        self.assertEqual(request[4]["size"], "1536x1024")

    def test_live_generation_uses_no_replay_policy_without_acceptance_flag(self):
        with patch.dict(os.environ, {ACCEPTANCE_ENV: "0"}, clear=True):
            provider, _, events = self._provider()

            provider.generate(SLOT, "no-provider-idempotency-required")

        self.assertIn("request", [event[0] for event in events])

    def test_constructor_rejects_direct_acceptance_override(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(TypeError):
                OpenAIImageProvider(
                    owner="user-a",
                    job_id=JOB,
                    acceptance_probe_passed=True,
                )

    def test_ready_asset_replays_without_external_resubmission(self):
        provider, store, events = self._provider()
        first = provider.generate(SLOT, "ready-before-gate")
        events.clear()

        blocked_provider = OpenAIImageProvider(
            owner="user-a",
            job_id=JOB,
            api_key="test-key",
            asset_store=store,
            http_request=lambda *args: self.fail(
                "ready replay must not call the provider"
            ),
        )
        replay = blocked_provider.generate(SLOT, "ready-before-gate")

        self.assertEqual(replay.payload, first.payload)
        self.assertEqual([event[0] for event in events], ["reserve"])

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
        self.assertEqual(store.saved["generation_state"], "terminal_failed")

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
        self.assertEqual(store.saved["generation_state"], "terminal_failed")
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

    def test_unknown_submission_is_terminal_and_restart_never_resubmits(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "ai-edit-v2.db")
            store.init_db(db_path)
            job = store.create_job(
                "user-a", {"brief": "image"}, "quote-1", "job-key", 1,
                db_path=db_path,
            )
            attempts = []
            events = []
            cos = FakeCos(events)

            def uncertain_request(method, url, headers, body, timeout):
                attempts.append((headers["Idempotency-Key"], body))
                raise TimeoutError("connection lost after send")

            first = OpenAIImageProvider(
                owner="user-a", job_id=job["id"], api_key="test-key",
                cos_api=cos, asset_store=store, http_request=uncertain_request,
                downloader=lambda *args: None, now_seconds=lambda: 100,
                worker_id="worker-first", db_path=db_path,
            )
            with self.assertRaisesRegex(
                ProviderError, "openai_image_submission_unknown"
            ) as caught:
                first.generate(SLOT, "same-provider-key")
            self.assertNotIsInstance(caught.exception, RetryableProviderError)

            persisted = store.find_generated_material(
                "user-a", job["id"], "same-provider-key", db_path=db_path
            )
            self.assertEqual(persisted["generation_state"], "terminal_failed")

            def replay_request(method, url, headers, body, timeout):
                attempts.append((headers["Idempotency-Key"], body))
                return {
                    "id": "request-replayed",
                    "data": [{"url": "https://provider.example/image.png"}],
                }

            second = OpenAIImageProvider(
                owner="user-a", job_id=job["id"], api_key="test-key",
                cos_api=cos, asset_store=store, http_request=replay_request,
                downloader=lambda *args: {
                    "content": png_bytes(), "content_type": "image/png"
                },
                now_seconds=lambda: 131, worker_id="worker-second", db_path=db_path,
            )
            with self.assertRaisesRegex(
                ProviderError, "openai_image_generation_failed"
            ):
                second.generate(SLOT, "same-provider-key")

            self.assertEqual(len(attempts), 1)
            with store._connection(db_path) as conn:
                logical_rows = conn.execute(
                    "SELECT COUNT(*) FROM edit_v2_materials WHERE source='gpt_image'"
                ).fetchone()[0]
            self.assertEqual(logical_rows, 1)

    def test_process_exit_after_submit_is_terminal_after_lease_expiry(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "ai-edit-v2.db")
            store.init_db(db_path)
            job = store.create_job(
                "user-a", {"brief": "image"}, "quote-1", "job-key", 1,
                db_path=db_path,
            )
            attempts = []

            def process_exit(method, url, headers, body, timeout):
                attempts.append((headers["Idempotency-Key"], body))
                raise SystemExit("worker exited after submit")

            first = OpenAIImageProvider(
                owner="user-a", job_id=job["id"], api_key="test-key",
                asset_store=store, http_request=process_exit,
                now_seconds=lambda: 100, worker_id="worker-first", db_path=db_path,
            )
            with self.assertRaises(SystemExit):
                first.generate(SLOT, "crash-key")

            second = OpenAIImageProvider(
                owner="user-a", job_id=job["id"], api_key="test-key",
                asset_store=store,
                http_request=lambda *args: self.fail("restart must not resubmit"),
                now_seconds=lambda: 281, worker_id="worker-second", db_path=db_path,
            )
            with self.assertRaisesRegex(
                ProviderError, "openai_image_generation_failed"
            ):
                second.generate(SLOT, "crash-key")

            self.assertEqual(len(attempts), 1)
            persisted = store.find_generated_material(
                "user-a", job["id"], "crash-key", db_path=db_path
            )
            self.assertEqual(persisted["generation_state"], "terminal_failed")

    def test_ambiguous_http_failures_are_terminal_and_never_retryable(self):
        for status in (408, 500, 503):
            with self.subTest(status=status):
                provider, store, events = self._provider()
                provider.http_request = lambda *args, code=status: (
                    _ for _ in ()
                ).throw(urllib.error.HTTPError(args[1], code, "ambiguous", {}, None))

                with self.assertRaisesRegex(
                    ProviderError, "openai_image_submission_unknown"
                ) as caught:
                    provider.generate(SLOT, f"ambiguous-{status}")

                self.assertNotIsInstance(caught.exception, RetryableProviderError)
                self.assertEqual(store.saved["generation_state"], "terminal_failed")
                self.assertEqual([event[0] for event in events].count("request"), 0)

    def test_retryable_response_is_recoverable_after_backoff(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "ai-edit-v2.db")
            store.init_db(db_path)
            job = store.create_job(
                "user-a", {"brief": "image"}, "quote-1", "job-key", 1,
                db_path=db_path,
            )
            attempts = []
            events = []
            cos = FakeCos(events)

            def rate_limited(method, url, headers, body, timeout):
                attempts.append((headers["Idempotency-Key"], body))
                raise urllib.error.HTTPError(url, 429, "rate limited", {}, None)

            first = OpenAIImageProvider(
                owner="user-a", job_id=job["id"], api_key="test-key",
                cos_api=cos, asset_store=store, http_request=rate_limited,
                now_seconds=lambda: 100, worker_id="worker-first", db_path=db_path,
            )
            with self.assertRaises(RetryableProviderError):
                first.generate(SLOT, "rate-limit-key")

            persisted = store.find_generated_material(
                "user-a", job["id"], "rate-limit-key", db_path=db_path
            )
            self.assertEqual(persisted["generation_state"], "rate_limited")

            during_backoff = OpenAIImageProvider(
                owner="user-a", job_id=job["id"], api_key="test-key",
                cos_api=cos, asset_store=store,
                http_request=lambda *args: self.fail("backoff must not resubmit"),
                now_seconds=lambda: 129, worker_id="worker-too-soon", db_path=db_path,
            )
            with self.assertRaisesRegex(
                RetryableProviderError, "openai_image_retry_backoff"
            ):
                during_backoff.generate(SLOT, "rate-limit-key")

            def succeeds(method, url, headers, body, timeout):
                attempts.append((headers["Idempotency-Key"], body))
                return {
                    "id": "request-after-backoff",
                    "data": [{"url": "https://provider.example/image.png"}],
                }

            second = OpenAIImageProvider(
                owner="user-a", job_id=job["id"], api_key="test-key",
                cos_api=cos, asset_store=store, http_request=succeeds,
                downloader=lambda *args: {
                    "content": png_bytes(), "content_type": "image/png"
                },
                now_seconds=lambda: 131, worker_id="worker-second", db_path=db_path,
            )
            second.generate(SLOT, "rate-limit-key")

            self.assertEqual(len(attempts), 2)
            self.assertEqual(attempts[0], attempts[1])

    def test_terminal_4xx_is_not_resubmitted_on_restart(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = os.path.join(temp_dir, "ai-edit-v2.db")
            store.init_db(db_path)
            job = store.create_job(
                "user-a", {"brief": "image"}, "quote-1", "job-key", 1,
                db_path=db_path,
            )
            calls = 0

            def rejected(method, url, headers, body, timeout):
                nonlocal calls
                calls += 1
                raise urllib.error.HTTPError(url, 400, "bad request", {}, None)

            first = OpenAIImageProvider(
                owner="user-a", job_id=job["id"], api_key="test-key",
                asset_store=store, http_request=rejected,
                now_seconds=lambda: 100, worker_id="worker-first", db_path=db_path,
            )
            with self.assertRaisesRegex(ProviderError, "openai_image_request_rejected"):
                first.generate(SLOT, "terminal-key")

            second = OpenAIImageProvider(
                owner="user-a", job_id=job["id"], api_key="test-key",
                asset_store=store, http_request=rejected,
                now_seconds=lambda: 1000, worker_id="worker-second", db_path=db_path,
            )
            with self.assertRaisesRegex(ProviderError, "openai_image_generation_failed"):
                second.generate(SLOT, "terminal-key")

            self.assertEqual(calls, 1)

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
