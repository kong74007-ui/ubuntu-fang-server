import json
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from server.content_domains import ai_edit_v2_store as store
from server.content_domains.ai_edit_v2_providers.base import UnknownSubmissionError
from server.content_domains.ai_edit_v2_shotstack import (
    RenderGraphError,
    ShotstackClient,
    build_render_graph,
    reconcile_webhook,
)


FONT_URL = "https://fonts.example.invalid/noto-sans-sc.woff2"
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

    def tearDown(self):
        self.env.stop()
        self.temp_dir.cleanup()

    def _client(self, request):
        return ShotstackClient(
            job_id=self.job["id"], attempt_id=self.attempt_id,
            db_path=self.db_path, http_request=request, clock_ms=lambda: 50,
        )

    def test_submit_persists_task_and_attempt_atomically_then_replays_without_post(self):
        calls = []

        def request(method, url, headers, body, timeout):
            calls.append((method, url, headers, body, timeout))
            return {"success": True, "response": {"id": "render-123", "status": "queued"}}

        client = self._client(request)
        first = client.submit({"timeline": {}, "output": {}}, "job:render:1")
        second = client.submit({"timeline": {}, "output": {}}, "job:render:1")

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

    def test_submit_timeout_reconciles_by_reference_without_second_post(self):
        calls = []

        def request(method, url, headers, body, timeout):
            calls.append((method, url, headers, body))
            if method == "POST":
                raise TimeoutError("unknown submission")
            return {"success": True, "response": [{"id": "render-123", "status": "queued", "reference": "job:render:1"}]}

        result = self._client(request).submit({"timeline": {}, "output": {}}, "job:render:1")

        self.assertEqual(result.payload["provider_task_id"], "render-123")
        self.assertEqual([call[0] for call in calls], ["POST", "GET"])

    def test_unknown_submit_timeout_never_blindly_reposts(self):
        calls = []

        def request(method, url, headers, body, timeout):
            calls.append(method)
            if method == "POST":
                raise TimeoutError("unknown submission")
            return {"success": True, "response": []}

        with self.assertRaises(UnknownSubmissionError):
            self._client(request).submit({"timeline": {}, "output": {}}, "job:render:1")
        self.assertEqual(calls, ["POST", "GET"])

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
        first = reconcile_webhook(self.job["id"], forged, client, received_at=200, db_path=self.db_path)
        duplicate = reconcile_webhook(self.job["id"], forged, client, received_at=201, db_path=self.db_path)

        self.assertEqual(first.payload["status"], "succeeded")
        self.assertEqual(first.payload["output_url"], fixture["response"]["url"])
        self.assertIsNone(duplicate)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "GET")
        self.assertEqual(calls[0][2]["x-api-key"], "secret-test-key")


if __name__ == "__main__":
    unittest.main()
