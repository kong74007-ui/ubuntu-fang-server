# -*- coding: utf-8 -*-
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from content_domains import ai_edit, ai_edit_store


def valid_payload(job_id=51, username="fang"):
    return {
        "_job_id": job_id,
        "_username": username,
        "source_video_asset_id": 7,
        "style": "knowledge_dynamic",
        "ratio": "9:16",
        "captions": True,
    }


def fixture_for(name):
    fixtures = {
        "_source_context": {
            "source_type": "video",
            "url": "https://cos.example/source.mp4",
            "duration_ms": 30_000,
        },
        "_transcribe": {
            "text": "你好黄雀",
            "sentences": [],
            "words": [],
            "duration_ms": 30_000,
        },
        "_plan": {
            "version": "1.0",
            "ratio": "9:16",
            "output": {"width": 1080, "height": 1920},
            "segments": [
                {
                    "start_ms": 0,
                    "end_ms": 30_000,
                    "source_start_ms": 0,
                    "source_end_ms": 30_000,
                }
            ],
            "captions": [],
            "overlays": [],
            "broll": [],
        },
        "_resolve_assets": {
            "source_type": "video",
            "source_url": "https://cos.example/source.mp4",
            "materials": {},
        },
        "_render": {
            "provider_job_id": "render-51",
            "url": "https://provider.example/out.mp4",
        },
        "_transfer": "edit-output/fang/51.mp4",
        "_verify": 30.0,
    }
    return fixtures[name]


class AiEditPipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = pathlib.Path(self.tmp.name) / "ai_edit.db"
        self.env = mock.patch.dict(os.environ, {"AI_EDIT_DB": str(self.db)})
        self.env.start()
        self.cos_url = mock.patch(
            "content_domains.ai_edit.cos.object_url",
            return_value="https://cos.example/final.mp4",
        )
        self.cos_url.start()
        ai_edit_store.create_edit_job(
            self.db, 51, "fang", "knowledge_dynamic", "shotstack", 30
        )

    def tearDown(self):
        self.cos_url.stop()
        self.env.stop()
        self.tmp.cleanup()

    @mock.patch.multiple(
        "content_domains.ai_edit",
        _source_context=mock.DEFAULT,
        _transcribe=mock.DEFAULT,
        _plan=mock.DEFAULT,
        _resolve_assets=mock.DEFAULT,
        _render=mock.DEFAULT,
        _transfer=mock.DEFAULT,
        _verify=mock.DEFAULT,
    )
    def test_pipeline_orders_external_apis(self, **deps):
        events = []
        for name in (
            "_source_context",
            "_transcribe",
            "_plan",
            "_resolve_assets",
            "_render",
            "_transfer",
            "_verify",
        ):
            deps[name].side_effect = (
                lambda *args, _name=name, **kwargs: events.append(_name)
                or fixture_for(_name)
            )
        result = ai_edit.run_ai_edit(valid_payload())
        self.assertEqual(
            [
                "_source_context",
                "_transcribe",
                "_plan",
                "_resolve_assets",
                "_render",
                "_transfer",
                "_verify",
            ],
            events,
        )
        self.assertEqual("done", result["status"])
        self.assertEqual("edit-output/fang/51.mp4", result["video_file"])

    @mock.patch.multiple(
        "content_domains.ai_edit",
        _source_context=mock.DEFAULT,
        _transcribe=mock.DEFAULT,
        _plan=mock.DEFAULT,
        _resolve_assets=mock.DEFAULT,
        _render=mock.DEFAULT,
        _transfer=mock.DEFAULT,
        _verify=mock.DEFAULT,
    )
    def test_existing_provider_id_queries_instead_of_resubmitting(self, **deps):
        ai_edit_store.set_provider_job(
            self.db, 51, "render-existing", "rendering"
        )
        for name in deps:
            deps[name].return_value = fixture_for(name)
        ai_edit.run_ai_edit(valid_payload())
        self.assertEqual(
            "render-existing",
            deps["_render"].call_args.kwargs["existing_provider_job_id"],
        )

    def test_media_verification_requires_audio_and_duration(self):
        media = {
            "Format": {"Duration": "20.0"},
            "Stream": {"Video": [{"Width": "1080", "Height": "1920"}]},
        }
        with self.assertRaisesRegex(RuntimeError, "音轨"):
            ai_edit.verify_media(
                media, expected_duration_ms=20_000, require_audio=True
            )
        media["Stream"]["Audio"] = [{"CodecName": "aac"}]
        self.assertEqual(
            20.0,
            ai_edit.verify_media(
                media, expected_duration_ms=20_000, require_audio=True
            ),
        )
        media["Format"]["Duration"] = "18.9"
        with self.assertRaisesRegex(RuntimeError, "时长"):
            ai_edit.verify_media(
                media, expected_duration_ms=20_000, require_audio=True
            )

    @mock.patch("content_domains.ai_edit._source_context")
    def test_failure_is_categorized_and_truncated_in_store(self, source):
        source.side_effect = RuntimeError("sensitive provider detail " + "x" * 1000)
        with self.assertRaisesRegex(RuntimeError, "源素材"):
            ai_edit.run_ai_edit(valid_payload())
        row = ai_edit_store.get_owned_job(self.db, "fang", 51)
        self.assertEqual("source", row["error_code"])
        self.assertLessEqual(len(row["error_detail"]), 500)

    def test_requires_worker_identity(self):
        with self.assertRaisesRegex(ValueError, "身份"):
            ai_edit.run_ai_edit({"style": "knowledge_dynamic"})


if __name__ == "__main__":
    unittest.main()
