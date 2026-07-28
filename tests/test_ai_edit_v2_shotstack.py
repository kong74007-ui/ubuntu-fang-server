import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
from contextlib import closing
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from server.content_domains import ai_edit_v2_store as store
from server.content_domains.ai_edit_v2_providers.base import (
    ProviderError,
    RetryableProviderError,
    UnknownSubmissionError,
)
from server.content_domains.ai_edit_v2_shotstack import (
    RenderGraphError,
    ShotstackClient,
    build_render_graph,
    reconcile_webhook,
)


FONT_URL = "https://shotstack-assets.s3-ap-southeast-2.amazonaws.com/fonts/NotoSansSC-Regular.otf"
SIGNED_ASSETS = {
    "private/main.mp4": "https://cos.example.invalid/main.mp4?signature=short",
    "private/broll.png": "https://cos.example.invalid/broll.png?signature=short",
    "private/master.m4a": "https://cos.example.invalid/master.m4a?signature=short",
}
RESOLVED_PLAN = {
    "version": "2.0",
    "duration_ms": 4_000,
    "aspect_ratio": "16:9",
    "text_timeline": {
        "alignment_status": "aligned",
        "sentences": [
            {"text": "所有店都关门了", "start_ms": 0, "end_ms": 1_840},
            {"text": "现在找到新办法", "start_ms": 1_840, "end_ms": 4_000},
        ],
    },
    "scenes": [
        {
            "id": "scene_1", "start_ms": 0, "end_ms": 1_840,
            "headline": "真实问题", "material_slots": [], "transition": "cut",
        },
        {
            "id": "scene_2", "start_ms": 1_840, "end_ms": 4_000,
            "headline": "解决办法", "material_slots": ["slot_solution"],
            "transition": "fade",
        },
    ],
    "materials": {
        "slot_solution": {
            "asset_id": "asset-1", "kind": "image", "cos_key": "private/broll.png"
        }
    },
    "primary_video": {"cos_key": "private/main.mp4"},
    "mastered_audio": {"cos_key": "private/master.m4a", "source": "mix_audio"},
}


def _components(graph, kind):
    return [item for item in graph["components"] if item["type"] == kind]


class RenderGraphTests(unittest.TestCase):
    def test_render_graph_uses_exact_aligned_caption_timestamps_and_noto_font(self):
        graph = build_render_graph(RESOLVED_PLAN, SIGNED_ASSETS, FONT_URL)

        caption = next(
            item for item in _components(graph, "basic_caption")
            if item["text"] == "所有店都关门了"
        )
        self.assertEqual(caption["start"], 0.0)
        self.assertEqual(caption["length"], 1.84)
        self.assertEqual(caption["font_url"], FONT_URL)
        self.assertNotIn("auto_caption", json.dumps(graph).lower())

    def test_render_graph_maps_only_the_stable_component_allowlist(self):
        graph = build_render_graph(RESOLVED_PLAN, SIGNED_ASSETS, FONT_URL)

        self.assertEqual(
            {item["type"] for item in graph["components"]},
            {"basic_caption", "basic_card", "broll_image", "broll_video", "standard_transition", "audio_bed"},
        )

    def test_render_graph_rejects_free_code_and_advanced_components(self):
        for component in (
            {"type": "free_code_mg", "code": "return <Widget />"},
            {"type": "advanced_chart", "data": [1, 2]},
        ):
            with self.subTest(component=component["type"]):
                plan = {**RESOLVED_PLAN, "components": [component]}
                with self.assertRaises(RenderGraphError):
                    build_render_graph(plan, SIGNED_ASSETS, FONT_URL)

    def test_audio_bed_accepts_only_the_single_mix_audio_master(self):
        graph = build_render_graph(RESOLVED_PLAN, SIGNED_ASSETS, FONT_URL)

        self.assertEqual(
            _components(graph, "audio_bed"),
            [{
                "type": "audio_bed", "start": 0.0, "length": 4.0,
                "src": SIGNED_ASSETS["private/master.m4a"],
            }],
        )
        plan = {**RESOLVED_PLAN, "mastered_audio": {"cos_key": "private/master.m4a", "source": "bgm"}}
        with self.assertRaises(RenderGraphError):
            build_render_graph(plan, SIGNED_ASSETS, FONT_URL)

    def test_missing_short_lived_asset_url_is_rejected(self):
        with self.assertRaises(RenderGraphError):
            build_render_graph(RESOLVED_PLAN, {}, FONT_URL)

    def test_noto_font_is_a_fixed_versioned_allowlist_not_a_name_substring(self):
        with self.assertRaises(RenderGraphError):
            build_render_graph(
                RESOLVED_PLAN,
                SIGNED_ASSETS,
                "https://evil.invalid/notorious-noto-malware.ttf",
            )


class ShotstackClientTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "ai_edit_v2.db")
        self.env = patch.dict(
            os.environ,
            {"AI_EDIT_V2_DB": self.db_path, "SHOTSTACK_API_KEY": "secret-test-key"},
        )
        self.env.start()
        store.init_db(self.db_path)
        self.job = store.create_job(
            "owner-a", {"creation_mode": "natural_brief"}, "quote-1", "request-1", 100,
            db_path=self.db_path,
        )
        self.attempt_id = store.record_stage_attempt(
            self.job["id"], "rendering", 1, "running", 101, db_path=self.db_path
        )
        self.graph = build_render_graph(RESOLVED_PLAN, SIGNED_ASSETS, FONT_URL)

    def tearDown(self):
        self.env.stop()
        self.temp_dir.cleanup()

    def _client(self, request):
        return ShotstackClient(
            job_id=self.job["id"], attempt_id=self.attempt_id,
            db_path=self.db_path, http_request=request, clock_ms=lambda: 50,
            callback_base_url="https://app.example.invalid/api/v2/edit/webhooks/shotstack",
            callback_secret="callback-test-secret",
        )

    @staticmethod
    def _callback_identity(body):
        callback = json.loads(body)["callback"]
        query = parse_qs(urlparse(callback).query)
        return int(query["attempt_id"][0]), query["token"][0]

    def test_submit_compiles_official_edit_api_json_before_transport(self):
        bodies = []

        def request(method, url, headers, body, timeout):
            bodies.append(json.loads(body))
            return {"success": True, "message": "Created", "response": {"id": "render-123"}}

        self._client(request).submit(self.graph, "job:render:1")

        payload = bodies[0]
        self.assertEqual(set(payload), {"timeline", "output", "callback"})
        self.assertEqual(payload["output"], {
            "format": "mp4", "resolution": "1080", "aspectRatio": "16:9", "fps": 30,
        })
        self.assertEqual(payload["timeline"]["fonts"], [{"src": FONT_URL}])
        self.assertNotIn("soundtrack", payload["timeline"])
        clips = [clip for track in payload["timeline"]["tracks"] for clip in track["clips"]]
        self.assertTrue(clips)
        self.assertTrue(all(set(clip) >= {"asset", "start", "length"} for clip in clips))
        self.assertTrue(all(clip["asset"]["type"] in {"rich-text", "image", "video", "audio"} for clip in clips))
        self.assertNotIn("reference", payload)
        self.assertNotIn("components", payload)

    def test_submit_validates_callback_https_before_claiming_reference(self):
        calls = []
        client = ShotstackClient(
            job_id=self.job["id"], attempt_id=self.attempt_id,
            db_path=self.db_path, http_request=lambda *args: calls.append(args),
            callback_base_url="http://app.example.invalid/webhooks/shotstack",
            callback_secret="callback-test-secret",
        )

        with self.assertRaisesRegex(ProviderError, "shotstack_callback_not_configured"):
            client.submit(self.graph, "job:render:1")

        self.assertEqual(calls, [])
        self.assertIsNone(
            store.find_stage_submission(self.attempt_id, db_path=self.db_path)[
                "provider_reference"
            ]
        )

    def test_submit_compiles_json_before_claiming_reference(self):
        invalid_graph = {**self.graph, "duration_ms": 0}

        with self.assertRaisesRegex(ProviderError, "shotstack_render_graph_invalid"):
            self._client(lambda *args: self.fail("transport must not run")).submit(
                invalid_graph, "job:render:1"
            )

        self.assertIsNone(
            store.find_stage_submission(self.attempt_id, db_path=self.db_path)[
                "provider_reference"
            ]
        )

    def test_deterministic_4xx_releases_claim_for_retry(self):
        calls = []
        reject = True

        def request(method, url, headers, body, timeout):
            nonlocal reject
            calls.append(method)
            if reject:
                raise urllib.error.HTTPError(url, 400, "Bad Request", None, None)
            return {
                "success": True,
                "response": {"id": "render-123", "status": "queued"},
            }

        client = self._client(request)
        with self.assertRaisesRegex(
            RetryableProviderError, "shotstack_request_rejected"
        ):
            client.submit(self.graph, "job:render:1")
        self.assertIsNone(
            store.find_stage_submission(self.attempt_id, db_path=self.db_path)[
                "provider_reference"
            ]
        )

        reject = False
        result = client.submit(self.graph, "job:render:1")

        self.assertEqual(result.payload["provider_task_id"], "render-123")
        self.assertEqual(calls, ["POST", "POST"])

    def test_mastered_audio_mutes_every_video_and_is_the_only_audible_track(self):
        bodies = []

        def request(method, url, headers, body, timeout):
            bodies.append(json.loads(body))
            return {"success": True, "message": "Created", "response": {"id": "render-123"}}

        self._client(request).submit(self.graph, "job:render:1")

        assets = [
            clip["asset"] for track in bodies[0]["timeline"]["tracks"]
            for clip in track["clips"]
        ]
        videos = [asset for asset in assets if asset["type"] == "video"]
        audible = [asset for asset in assets if asset["type"] in {"video", "audio"} and asset.get("volume", 1) > 0]
        self.assertTrue(videos)
        self.assertTrue(all(asset["volume"] == 0 for asset in videos))
        self.assertEqual(len(audible), 1)
        self.assertEqual(audible[0], {
            "type": "audio", "src": SIGNED_ASSETS["private/master.m4a"], "volume": 1,
        })

    def test_submit_persists_task_and_attempt_atomically_then_replays_without_post(self):
        calls = []

        def request(method, url, headers, body, timeout):
            calls.append((method, url, headers, body, timeout))
            return {"success": True, "response": {"id": "render-123", "status": "queued"}}

        client = self._client(request)
        first = client.submit(self.graph, "job:render:1")
        second = client.submit(self.graph, "job:render:1")

        self.assertEqual(first.payload["provider_task_id"], "render-123")
        self.assertEqual(second.payload["provider_task_id"], "render-123")
        self.assertEqual([call[0] for call in calls], ["POST", "GET"])
        with closing(sqlite3.connect(self.db_path)) as conn:
            attempt = conn.execute(
                "SELECT provider_task_id FROM edit_v2_stage_attempts WHERE id=?", (self.attempt_id,)
            ).fetchone()
            provider = conn.execute(
                "SELECT provider_task_id,reference FROM edit_v2_provider_jobs WHERE job_id=?",
                (self.job["id"],),
            ).fetchone()
        self.assertEqual(attempt[0], "render-123")
        self.assertEqual(provider, ("render-123", "job:render:1"))

    def test_submit_timeout_freezes_unknown_and_never_uses_reference_get_or_reposts(self):
        calls = []

        def request(method, url, headers, body, timeout):
            calls.append((method, url, headers, body))
            raise TimeoutError("unknown submission")

        client = self._client(request)
        with self.assertRaises(UnknownSubmissionError):
            client.submit(self.graph, "job:render:1")
        with self.assertRaises(UnknownSubmissionError):
            client.submit(self.graph, "job:render:1")

        self.assertEqual([call[0] for call in calls], ["POST"])
        self.assertTrue(all("?reference=" not in call[1] for call in calls))

    def test_unknown_submit_timeout_never_blindly_reposts(self):
        calls = []

        def request(method, url, headers, body, timeout):
            calls.append(method)
            if method == "POST":
                raise TimeoutError("unknown submission")
            return {"success": True, "response": []}

        with self.assertRaises(UnknownSubmissionError):
            self._client(request).submit(self.graph, "job:render:1")
        self.assertEqual(calls, ["POST"])

    def test_callback_after_lost_post_response_binds_task_then_reconciles_by_id(self):
        calls = []
        submitted_body = None

        def request(method, url, headers, body, timeout):
            nonlocal submitted_body
            calls.append((method, url))
            if method == "POST":
                submitted_body = body
                raise TimeoutError("response lost")
            return {"success": True, "message": "OK", "response": {
                "id": "render-123", "status": "done", "url": "https://cdn.example.invalid/render.mp4",
            }}

        client = self._client(request)
        with self.assertRaises(UnknownSubmissionError):
            client.submit(self.graph, "job:render:1")
        callback_attempt, callback_token = self._callback_identity(submitted_body)
        result = reconcile_webhook(
            self.job["id"], {"id": "render-123", "status": "done"}, client,
            callback_attempt_id=callback_attempt, callback_token=callback_token,
            received_at=200, db_path=self.db_path,
        )

        self.assertEqual(result.payload["status"], "succeeded")
        self.assertEqual(calls, [
            ("POST", "https://api.shotstack.io/edit/stage/render"),
            ("GET", "https://api.shotstack.io/edit/stage/render/render-123"),
        ])
        with closing(sqlite3.connect(self.db_path)) as conn:
            bound = conn.execute(
                "SELECT provider_task_id FROM edit_v2_stage_attempts WHERE id=?", (self.attempt_id,)
            ).fetchone()[0]
        self.assertEqual(bound, "render-123")

    def test_webhook_is_only_a_deduplicated_wakeup_and_query_is_authoritative(self):
        fixture = json.loads(
            (Path(__file__).parent / "fixtures/ai_edit_v2/provider_responses/shotstack_render_success.json").read_text(encoding="utf-8")
        )
        calls = []

        def request(method, url, headers, body, timeout):
            calls.append((method, url, headers))
            return fixture

        client = self._client(request)
        store.bind_provider_submission(
            attempt_id=self.attempt_id,
            job_id=self.job["id"],
            provider="shotstack",
            capability="render",
            provider_task_id="render-123",
            reference="job:render:1",
            status="pending",
            now=199,
            db_path=self.db_path,
        )
        forged = {"id": "render-123", "status": "failed", "url": "https://evil.invalid/forged.mp4"}
        token = client.callback_token("job:render:1")
        first = reconcile_webhook(
            self.job["id"], forged, client,
            callback_attempt_id=self.attempt_id, callback_token=token,
            received_at=200, db_path=self.db_path,
        )
        duplicate = reconcile_webhook(
            self.job["id"], forged, client,
            callback_attempt_id=self.attempt_id, callback_token=token,
            received_at=201, db_path=self.db_path,
        )

        self.assertEqual(first.payload["status"], "succeeded")
        self.assertEqual(first.payload["output_url"], fixture["response"]["url"])
        self.assertIsNone(duplicate)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "GET")
        self.assertEqual(calls[0][2]["x-api-key"], "secret-test-key")
        with closing(sqlite3.connect(self.db_path)) as conn:
            status = conn.execute(
                "SELECT normalized_status FROM edit_v2_provider_events"
            ).fetchone()[0]
        self.assertEqual(status, "processed")

    def test_failed_provider_get_releases_pending_webhook_for_retry(self):
        attempts = 0

        def request(method, url, headers, body, timeout):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise TimeoutError("provider query timeout")
            return {"success": True, "message": "OK", "response": {
                "id": "render-123", "status": "done", "url": "https://cdn.example.invalid/render.mp4",
            }}

        client = self._client(request)
        store.bind_provider_submission(
            attempt_id=self.attempt_id, job_id=self.job["id"], provider="shotstack",
            capability="render", provider_task_id="render-123", reference="job:render:1",
            status="pending", now=199, db_path=self.db_path,
        )
        event = {"id": "render-123", "status": "done"}
        token = client.callback_token("job:render:1")
        kwargs = {
            "callback_attempt_id": self.attempt_id, "callback_token": token,
            "received_at": 200, "db_path": self.db_path,
        }
        with self.assertRaises(UnknownSubmissionError):
            reconcile_webhook(self.job["id"], event, client, **kwargs)
        result = reconcile_webhook(self.job["id"], event, client, **kwargs)

        self.assertEqual(result.payload["status"], "succeeded")
        self.assertEqual(attempts, 2)
        with closing(sqlite3.connect(self.db_path)) as conn:
            statuses = conn.execute(
                "SELECT normalized_status FROM edit_v2_provider_events"
            ).fetchall()
        self.assertEqual(statuses, [("processed",)])

    def test_webhook_reclaims_expired_lease_left_by_crashed_process(self):
        calls = []

        def request(method, url, headers, body, timeout):
            calls.append(method)
            return {"success": True, "message": "OK", "response": {
                "id": "render-123", "status": "done",
                "url": "https://cdn.example.invalid/render.mp4",
            }}

        client = self._client(request)
        store.bind_provider_submission(
            attempt_id=self.attempt_id, job_id=self.job["id"], provider="shotstack",
            capability="render", provider_task_id="render-123", reference="job:render:1",
            status="pending", now=199, db_path=self.db_path,
        )
        event = {"id": "render-123", "status": "done"}
        canonical = json.dumps(
            event, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        store.claim_provider_event(
            self.job["id"], "shotstack", "render-123", fingerprint, 200,
            lease_owner="crashed-owner", lease_seconds=30, now=200,
            db_path=self.db_path,
        )

        result = reconcile_webhook(
            self.job["id"], event, client,
            callback_attempt_id=self.attempt_id,
            callback_token=client.callback_token("job:render:1"),
            received_at=230,
            db_path=self.db_path,
        )

        self.assertEqual(result.payload["status"], "succeeded")
        self.assertEqual(calls, ["GET"])
        with closing(sqlite3.connect(self.db_path)) as conn:
            event_row = conn.execute(
                """SELECT normalized_status,lease_owner,lease_until
                   FROM edit_v2_provider_events WHERE fingerprint=?""",
                (fingerprint,),
            ).fetchone()
        self.assertEqual(event_row, ("processed", None, None))

    def test_pending_duplicate_webhook_is_retryable_and_recovers_after_first_get_fails(self):
        first_get_entered = threading.Event()
        release_first_get = threading.Event()
        attempts_lock = threading.Lock()
        attempts = 0

        def request(method, url, headers, body, timeout):
            nonlocal attempts
            with attempts_lock:
                attempts += 1
                current_attempt = attempts
            if current_attempt == 1:
                first_get_entered.set()
                self.assertTrue(release_first_get.wait(5))
                raise TimeoutError("provider query timeout")
            return {"success": True, "message": "OK", "response": {
                "id": "render-123", "status": "done",
                "url": "https://cdn.example.invalid/render.mp4",
            }}

        client = self._client(request)
        store.bind_provider_submission(
            attempt_id=self.attempt_id, job_id=self.job["id"], provider="shotstack",
            capability="render", provider_task_id="render-123", reference="job:render:1",
            status="pending", now=199, db_path=self.db_path,
        )
        event = {"id": "render-123", "status": "done"}
        token = client.callback_token("job:render:1")
        kwargs = {
            "callback_attempt_id": self.attempt_id, "callback_token": token,
            "received_at": 200, "db_path": self.db_path,
        }
        first_error = []

        def first_delivery():
            try:
                reconcile_webhook(self.job["id"], event, client, **kwargs)
            except ProviderError as exc:
                first_error.append(exc)

        worker = threading.Thread(target=first_delivery)
        worker.start()
        self.assertTrue(first_get_entered.wait(5))
        try:
            with self.assertRaisesRegex(
                RetryableProviderError, "shotstack_webhook_in_progress"
            ):
                reconcile_webhook(self.job["id"], event, client, **kwargs)
        finally:
            release_first_get.set()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(first_error), 1)
        self.assertIsInstance(first_error[0], UnknownSubmissionError)

        recovered = reconcile_webhook(self.job["id"], event, client, **kwargs)

        self.assertEqual(recovered.payload["status"], "succeeded")
        self.assertEqual(attempts, 2)


if __name__ == "__main__":
    unittest.main()
