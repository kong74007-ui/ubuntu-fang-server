# -*- coding: utf-8 -*-
import pathlib
import sys
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from content_domains.renderers import shotstack


def plan_fixture():
    return {
        "version": "1.0",
        "ratio": "9:16",
        "output": {"width": 1080, "height": 1920},
        "segments": [
            {
                "start_ms": 0,
                "end_ms": 5000,
                "source_start_ms": 1000,
                "source_end_ms": 6000,
            }
        ],
        "captions": [],
        "overlays": [
            {
                "type": "claim_card",
                "start_ms": 500,
                "end_ms": 2500,
                "text": "三个关键步骤",
            }
        ],
        "broll": [
            {"asset_id": "scene-1", "start_ms": 2500, "end_ms": 4500}
        ],
    }


def assets_fixture():
    return {
        "source_type": "video",
        "source_url": "https://cos.example/source.mp4",
        "captions_url": "https://cos.example/captions.vtt",
        "materials": {
            "scene-1": {
                "kind": "image",
                "url": "https://cos.example/scene.jpg",
            }
        },
    }


def edit_fixture():
    return {
        "timeline": {"tracks": []},
        "output": {"format": "mp4", "size": {"width": 1080, "height": 1920}},
    }


class ShotstackRendererTests(unittest.TestCase):
    def setUp(self):
        self.renderer = shotstack.ShotstackRenderer(
            api_key="configured-for-test",
            base="https://api.shotstack.io/edit/stage",
        )

    def test_builds_vertical_timeline_with_video_caption_and_card(self):
        edit = self.renderer.build_timeline(
            plan_fixture(),
            assets_fixture(),
            "https://fang.example/api/v1/edit/webhooks/shotstack",
        )
        self.assertEqual("mp4", edit["output"]["format"])
        self.assertEqual(
            {"width": 1080, "height": 1920}, edit["output"]["size"]
        )
        clips = [
            clip
            for track in edit["timeline"]["tracks"]
            for clip in track["clips"]
        ]
        types = [clip["asset"]["type"] for clip in clips]
        self.assertIn("video", types)
        self.assertIn("rich-caption", types)
        self.assertIn("html", types)
        source = next(clip for clip in clips if clip["asset"]["type"] == "video")
        self.assertEqual(1.0, source["asset"]["trim"])
        self.assertEqual(5.0, source["length"])

    @mock.patch("content_domains.renderers.shotstack._request_json")
    def test_submit_and_query_use_stage_api(self, request):
        request.side_effect = [
            {"success": True, "response": {"id": "render-1", "status": "queued"}},
            {
                "success": True,
                "response": {
                    "id": "render-1",
                    "status": "done",
                    "url": "https://cdn.example/out.mp4",
                },
            },
        ]
        render_id = self.renderer.submit(edit_fixture())
        status = self.renderer.get_status(render_id)
        self.assertEqual("render-1", render_id)
        self.assertEqual("succeeded", status["status"])
        self.assertEqual(
            "https://api.shotstack.io/edit/stage/render",
            request.call_args_list[0].args[1],
        )
        self.assertEqual(
            "https://api.shotstack.io/edit/stage/render/render-1",
            request.call_args_list[1].args[1],
        )

    def test_rejects_non_https_assets_and_callback(self):
        assets = assets_fixture()
        assets["source_url"] = "http://example.com/source.mp4"
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            self.renderer.build_timeline(
                plan_fixture(), assets, "https://fang.example/callback"
            )
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            self.renderer.build_timeline(
                plan_fixture(), assets_fixture(), "http://fang.example/callback"
            )

    def test_server_owned_card_escapes_model_text(self):
        plan = plan_fixture()
        plan["overlays"][0]["text"] = "<b>不是HTML</b>"
        edit = self.renderer.build_timeline(
            plan, assets_fixture(), "https://fang.example/callback"
        )
        clips = [
            clip
            for track in edit["timeline"]["tracks"]
            for clip in track["clips"]
            if clip["asset"]["type"] == "html"
        ]
        self.assertIn("&lt;b&gt;不是HTML&lt;/b&gt;", clips[0]["asset"]["html"])
        self.assertNotIn("<b>不是HTML</b>", clips[0]["asset"]["html"])

    @mock.patch("content_domains.renderers.shotstack.time.sleep")
    def test_wait_polls_existing_job_and_heartbeats(self, sleep):
        self.renderer.get_status = mock.Mock(
            side_effect=[
                {"status": "queued", "url": None, "error": None},
                {
                    "status": "succeeded",
                    "url": "https://cdn.example/out.mp4",
                    "error": None,
                },
            ]
        )
        heartbeat = mock.Mock()
        result = self.renderer.wait("render-existing", heartbeat, timeout=30)
        self.assertEqual("https://cdn.example/out.mp4", result["url"])
        self.assertEqual(
            [mock.call("rendering"), mock.call("rendering")], heartbeat.call_args_list
        )
        self.assertEqual([mock.call(5)], sleep.call_args_list)

    def test_normalizes_all_documented_provider_statuses(self):
        expected = {
            "queued": "queued",
            "fetching": "rendering",
            "rendering": "rendering",
            "saving": "rendering",
            "done": "succeeded",
            "failed": "failed",
        }
        self.assertEqual(
            expected,
            {raw: shotstack.normalize_status(raw) for raw in expected},
        )


if __name__ == "__main__":
    unittest.main()
