# -*- coding: utf-8 -*-
import pathlib
import sys
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from content_domains import ai_edit_styles, edit_plan


def valid_plan():
    return {
        "version": "1.0",
        "ratio": "9:16",
        "segments": [
            {
                "start_ms": 0,
                "end_ms": 30_000,
                "source_start_ms": 0,
                "source_end_ms": 30_000,
            }
        ],
        "captions": [{"start_ms": 0, "end_ms": 900, "text": "你好黄雀"}],
        "overlays": [],
        "broll": [],
    }


class EditPlanTests(unittest.TestCase):
    def test_exposes_three_distinct_styles(self):
        styles = ai_edit_styles.list_styles()
        self.assertEqual(
            ["knowledge_dynamic", "product_story", "story_broll"],
            [item["id"] for item in styles],
        )
        self.assertEqual(3, len({item["director_rules"] for item in styles}))

    def test_submit_requires_exactly_one_source(self):
        with self.assertRaisesRegex(ValueError, "素材来源"):
            edit_plan.validate_submit_payload({"style": "knowledge_dynamic"})
        cleaned = edit_plan.validate_submit_payload(
            {
                "source_video_asset_id": 7,
                "style": "knowledge_dynamic",
                "ratio": "9:16",
                "captions": True,
            }
        )
        self.assertEqual(7, cleaned["source_video_asset_id"])
        with self.assertRaisesRegex(ValueError, "只能选择一个"):
            edit_plan.validate_submit_payload(
                {
                    "source_video_asset_id": 7,
                    "source_audio_asset_id": 8,
                    "style": "story_broll",
                    "ratio": "9:16",
                }
            )

    def test_audio_source_requires_story_style(self):
        with self.assertRaisesRegex(ValueError, "音频"):
            edit_plan.validate_submit_payload(
                {
                    "source_audio_asset_id": 8,
                    "style": "knowledge_dynamic",
                    "ratio": "9:16",
                }
            )
        cleaned = edit_plan.validate_submit_payload(
            {
                "source_audio_asset_id": 8,
                "style": "story_broll",
                "ratio": "16:9",
            }
        )
        self.assertTrue(cleaned["auto_assets"])

    def test_rejects_unknown_style_ratio_and_non_positive_source_id(self):
        for field, value in (("style", "unknown"), ("ratio", "4:5")):
            body = {
                "source_video_asset_id": 7,
                "style": "knowledge_dynamic",
                "ratio": "9:16",
            }
            body[field] = value
            with self.assertRaises(ValueError):
                edit_plan.validate_submit_payload(body)
        with self.assertRaisesRegex(ValueError, "素材"):
            edit_plan.validate_submit_payload(
                {
                    "source_video_asset_id": 0,
                    "style": "knowledge_dynamic",
                    "ratio": "9:16",
                }
            )

    def test_rejects_plan_outside_source_duration(self):
        plan = valid_plan()
        plan["segments"][0]["end_ms"] = 31_000
        with self.assertRaisesRegex(ValueError, "源视频时长"):
            edit_plan.validate_edit_plan(plan, 30_000, set())

    def test_rejects_unknown_asset_and_script_text(self):
        plan = valid_plan()
        plan["broll"] = [
            {"asset_id": "other-user", "start_ms": 0, "end_ms": 1000}
        ]
        with self.assertRaisesRegex(ValueError, "素材"):
            edit_plan.validate_edit_plan(plan, 30_000, {"mine"})
        plan = valid_plan()
        plan["captions"][0]["text"] = "<script>alert(1)</script>"
        with self.assertRaisesRegex(ValueError, "非法"):
            edit_plan.validate_edit_plan(plan, 30_000, set())

    def test_derives_output_dimensions_and_does_not_trust_model_dimensions(self):
        plan = valid_plan()
        plan["output"] = {"width": 1, "height": 1}
        cleaned = edit_plan.validate_edit_plan(plan, 30_000, set())
        self.assertEqual({"width": 1080, "height": 1920}, cleaned["output"])

    def test_rejects_excessive_duration_and_collection_sizes(self):
        with self.assertRaisesRegex(ValueError, "10分钟"):
            edit_plan.validate_edit_plan(valid_plan(), 600_001, set())
        plan = valid_plan()
        plan["overlays"] = [
            {"start_ms": 0, "end_ms": 10, "text": str(index)}
            for index in range(81)
        ]
        with self.assertRaisesRegex(ValueError, "叠加"):
            edit_plan.validate_edit_plan(plan, 30_000, set())


if __name__ == "__main__":
    unittest.main()
