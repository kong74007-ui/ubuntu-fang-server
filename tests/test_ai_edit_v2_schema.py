import unittest

from server.content_domains import ai_edit_v2_schema as schema


MB = 1024 * 1024


def valid_draft(**overrides):
    draft = {
        "creation_mode": "natural_brief",
        "brief": "为新品发布制作一条清晰、有节奏的中文短片",
        "language": "zh-CN",
        "aspect_ratio": "16:9",
        "target_duration_ms": 40_000,
        "main_input": {
            "asset_id": "main-video",
            "kind": "video",
            "size_bytes": 500 * MB,
            "duration_ms": 600_000,
        },
        "required_materials": [],
        "reference_materials": [],
    }
    draft.update(overrides)
    return draft


def valid_plan(**overrides):
    plan = {
        "version": "2.0",
        "creation_mode": "natural_brief",
        "duration_ms": 44_920,
        "target_duration_ms": 40_000,
        "aspect_ratio": "16:9",
        "language": "zh-CN",
        "editorial_decisions": [],
        "style_system": {},
        "scenes": [
            {
                "id": "scene_01",
                "start_ms": 0,
                "end_ms": 5_800,
                "intent": "介绍新品价值",
                "source_ranges": [{"start_ms": 0, "end_ms": 5_800}],
                "layout_intent": "speaker_with_product",
                "visual_type": "product_hook",
                "headline": "新品为什么值得关注？",
                "material_slots": ["slot_01"],
                "motion_graphics": [],
                "transition_intent": "hard_emphasis",
                "complexity": "standard",
            }
        ],
        "overlays": [],
        "materials": [
            {
                "slot_id": "slot_01",
                "start_ms": 1_200,
                "end_ms": 4_200,
                "semantic": "产品包装特写",
                "recommended_visual": "干净背景中的产品包装",
                "kind": "image_or_video",
                "required": False,
                "generation_allowed": True,
                "generation_intent": "写实产品摄影",
            }
        ],
        "caption_plan": {},
        "audio_plan": {},
        "delivery": {"resolution": "1080p", "format": "mp4"},
    }
    plan.update(overrides)
    return plan


class SchemaTests(unittest.TestCase):
    def test_rejects_more_than_ten_required_materials(self):
        draft = valid_draft(
            required_materials=[
                {"asset_id": str(i), "kind": "image", "size_bytes": MB}
                for i in range(11)
            ]
        )

        with self.assertRaisesRegex(ValueError, "必须使用.*10"):
            schema.validate_job_draft(draft)

    def test_rejects_more_than_ten_reference_materials(self):
        draft = valid_draft(
            reference_materials=[
                {
                    "asset_id": str(i),
                    "kind": "image",
                    "size_bytes": MB,
                    "reference_mode": "style_only",
                }
                for i in range(11)
            ]
        )

        with self.assertRaisesRegex(ValueError, "参考使用.*10"):
            schema.validate_job_draft(draft)

    def test_edit_plan_rejects_wrong_version_and_provider_fields(self):
        plan = valid_plan(
            version="1.0",
            scenes=[{"provider": "shotstack", "url": "https://example.invalid"}],
        )

        with self.assertRaises(ValueError):
            schema.validate_edit_plan(plan)

    def test_accepts_frozen_modes_ratios_language_and_capacity_boundaries(self):
        for creation_mode in (
            "natural_brief",
            "platform_template",
            "open_generation",
        ):
            for aspect_ratio in ("9:16", "16:9"):
                with self.subTest(mode=creation_mode, ratio=aspect_ratio):
                    draft = valid_draft(
                        creation_mode=creation_mode,
                        aspect_ratio=aspect_ratio,
                        required_materials=[
                            {
                                "asset_id": "required-video",
                                "kind": "video",
                                "size_bytes": 200 * MB,
                                "duration_ms": 600_000,
                            },
                            {
                                "asset_id": "required-image",
                                "kind": "image",
                                "size_bytes": 15 * MB,
                            },
                            {
                                "asset_id": "required-audio",
                                "kind": "audio",
                                "size_bytes": 50 * MB,
                                "duration_ms": 600_000,
                            },
                        ],
                        reference_materials=[
                            {
                                "asset_id": "style-reference",
                                "kind": "image",
                                "size_bytes": MB,
                                "reference_mode": "style_only",
                            }
                        ],
                    )
                    self.assertIs(schema.validate_job_draft(draft), draft)

    def test_accepts_optional_ai_decided_target_duration(self):
        draft = valid_draft(target_duration_ms=None)

        self.assertIs(schema.validate_job_draft(draft), draft)

    def test_rejects_unsupported_mode_ratio_or_language(self):
        invalid_drafts = (
            valid_draft(creation_mode="copy_reference"),
            valid_draft(aspect_ratio="1:1"),
            valid_draft(language="en-US"),
        )

        for draft in invalid_drafts:
            with self.subTest(draft=draft), self.assertRaises(ValueError):
                schema.validate_job_draft(draft)

    def test_rejects_non_positive_target_duration(self):
        with self.assertRaisesRegex(ValueError, "目标时长"):
            schema.validate_job_draft(valid_draft(target_duration_ms=0))

    def test_rejects_each_file_above_its_capacity(self):
        invalid_drafts = (
            valid_draft(
                main_input={
                    "asset_id": "main",
                    "kind": "video",
                    "size_bytes": 500 * MB + 1,
                    "duration_ms": 1_000,
                }
            ),
            valid_draft(
                required_materials=[
                    {
                        "asset_id": "extra-video",
                        "kind": "video",
                        "size_bytes": 200 * MB + 1,
                        "duration_ms": 1_000,
                    }
                ]
            ),
            valid_draft(
                required_materials=[
                    {"asset_id": "image", "kind": "image", "size_bytes": 15 * MB + 1}
                ]
            ),
            valid_draft(
                required_materials=[
                    {
                        "asset_id": "audio",
                        "kind": "audio",
                        "size_bytes": 50 * MB + 1,
                        "duration_ms": 1_000,
                    }
                ]
            ),
        )

        for draft in invalid_drafts:
            with self.subTest(draft=draft), self.assertRaises(ValueError):
                schema.validate_job_draft(draft)

    def test_rejects_source_over_ten_minutes(self):
        draft = valid_draft(
            main_input={
                "asset_id": "main",
                "kind": "audio",
                "size_bytes": MB,
                "duration_ms": 600_001,
            }
        )

        with self.assertRaisesRegex(ValueError, "10分钟"):
            schema.validate_job_draft(draft)

    def test_rejects_total_uploads_above_one_gibibyte(self):
        draft = valid_draft(
            main_input={
                "asset_id": "main",
                "kind": "video",
                "size_bytes": 500 * MB,
                "duration_ms": 1_000,
            },
            required_materials=[
                {
                    "asset_id": f"required-{i}",
                    "kind": "video",
                    "size_bytes": 200 * MB,
                    "duration_ms": 1_000,
                }
                for i in range(3)
            ],
        )

        with self.assertRaisesRegex(ValueError, "1GB"):
            schema.validate_job_draft(draft)

    def test_reference_material_requires_an_explicit_reference_mode(self):
        draft = valid_draft(
            reference_materials=[
                {"asset_id": "reference", "kind": "image", "size_bytes": MB}
            ]
        )

        with self.assertRaisesRegex(ValueError, "参考模式"):
            schema.validate_job_draft(draft)

    def test_accepts_provider_neutral_edit_plan(self):
        plan = valid_plan()

        self.assertIs(schema.validate_edit_plan(plan), plan)

    def test_edit_plan_recursively_rejects_each_boundary_field(self):
        for forbidden_key in ("url", "cos_key", "provider", "api_key", "html", "code"):
            with self.subTest(key=forbidden_key), self.assertRaisesRegex(
                ValueError, "禁止字段"
            ):
                schema.validate_edit_plan(
                    valid_plan(style_system={"nested": [{forbidden_key: "secret"}]})
                )

    def test_terminal_state_cannot_be_reopened(self):
        self.assertNotIn("completed", schema.STATE_TRANSITIONS)
        for state in schema.FAILURE_STATES:
            self.assertNotIn(state, schema.STATE_TRANSITIONS)

    def test_quality_check_can_skip_or_enter_repair(self):
        self.assertEqual(
            schema.STATE_TRANSITIONS["quality_check"],
            frozenset({"repairing", "settling"}),
        )


if __name__ == "__main__":
    unittest.main()
