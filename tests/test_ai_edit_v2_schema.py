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
        "style_system": {},
        "scenes": [
            {
                "id": "scene_01",
                "start_ms": 0,
                "end_ms": 44_920,
                "intent": "介绍新品价值",
                "layout": "speaker_product_split",
                "visual_type": "product_hook",
                "headline": "新品为什么值得关注？",
                "material_slots": ["slot_01"],
                "transition": "cut",
            }
        ],
        "caption_plan": {"source": "text_timeline", "style": "clean"},
        "audio_plan": {
            "speech_policy": "preserve_source",
            "music_policy": "duck_under_speech",
            "sfx_policy": "semantic_only",
        },
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
        for forbidden_key in (
            "url",
            "cos_key",
            "provider",
            "api_key",
            "html",
            "code",
            "tracks",
            "shotstack",
            "subtitle_text",
            "transcript",
        ):
            with self.subTest(key=forbidden_key), self.assertRaisesRegex(
                ValueError, "禁止字段"
            ):
                schema.validate_edit_plan(
                    valid_plan(style_system={"nested": [{forbidden_key: "secret"}]})
                )

    def test_edit_plan_rejects_scene_overlap_and_duration_mismatch(self):
        invalid_scenes = (
            [
                {
                    **valid_plan()["scenes"][0],
                    "end_ms": 30_000,
                },
                {
                    **valid_plan()["scenes"][0],
                    "id": "scene_02",
                    "start_ms": 29_999,
                    "end_ms": 44_920,
                },
            ],
            [{**valid_plan()["scenes"][0], "end_ms": 44_919}],
            [{**valid_plan()["scenes"][0], "start_ms": 1, "end_ms": 44_920}],
        )

        for scenes in invalid_scenes:
            with self.subTest(scenes=scenes), self.assertRaises(ValueError):
                schema.validate_edit_plan(valid_plan(scenes=scenes))

    def test_edit_plan_rejects_unknown_scene_components(self):
        for field in ("layout", "visual_type", "transition"):
            scene = {**valid_plan()["scenes"][0], field: "experimental_component"}
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, field):
                schema.validate_edit_plan(valid_plan(scenes=[scene]))

    def test_edit_plan_rejects_unpublished_scene_component_fields(self):
        for field in ("shader", "motion_graphics", "render_component"):
            scene = {**valid_plan()["scenes"][0], field: "beta_component"}
            with self.subTest(field=field), self.assertRaises(ValueError):
                schema.validate_edit_plan(valid_plan(scenes=[scene]))

    def test_edit_plan_rejects_provider_fields_with_decorated_names(self):
        for forbidden_key in (
            "source_url",
            "sourceUrl",
            "cos_path",
            "cosPath",
            "shotstack_template_id",
            "shotstackTemplateId",
            "provider_name",
            "providerName",
            "render_code",
            "renderEngine",
            "codePayload",
        ):
            with self.subTest(key=forbidden_key), self.assertRaisesRegex(ValueError, "禁止字段"):
                schema.validate_edit_plan(
                    valid_plan(style_system={"nested": {forbidden_key: "secret"}})
                )

    def test_edit_plan_rejects_material_and_render_top_level_sections(self):
        for field in ("materials", "delivery", "overlays", "tracks", "render_plan"):
            plan = valid_plan()
            plan[field] = []
            with self.subTest(field=field), self.assertRaises(ValueError):
                schema.validate_edit_plan(plan)

    def test_edit_plan_style_system_only_references_stable_family_or_template(self):
        invalid_style_systems = (
            {"component_family": "editorial_business", "palette": "#fff"},
            {"template_id": "business_diagnostic", "component_family": "editorial_business"},
            {
                "template_id": "business_diagnostic",
                "template_version": "1.0",
                "component_family": "experimental_family",
            },
        )
        for style_system in invalid_style_systems:
            with self.subTest(style_system=style_system), self.assertRaises(ValueError):
                schema.validate_edit_plan(valid_plan(style_system=style_system))

    def test_edit_plan_rejects_caption_body_and_unknown_audio_policy(self):
        with self.assertRaisesRegex(ValueError, "字幕正文"):
            schema.validate_edit_plan(
                valid_plan(caption_plan={"source": "text_timeline", "style": "clean", "text": "改写"})
            )
        with self.assertRaisesRegex(ValueError, "music_policy"):
            schema.validate_edit_plan(
                valid_plan(
                    audio_plan={
                        "speech_policy": "preserve_source",
                        "music_policy": "invent_new_policy",
                        "sfx_policy": "semantic_only",
                    }
                )
            )

    def test_edit_plan_rejects_executable_or_provider_specific_string_values(self):
        forbidden_values = (
            "https://evil.example.invalid/payload",
            "<script>alert(document.cookie)</script>",
            "javascript:fetch('/secrets')",
            "DROP TABLE customer_jobs",
            "SELECT * FROM private_tokens",
            "provider=shotstack",
            "render_engine=remotion",
            "cos://private-bucket/object",
            "database_url=mysql://user:pass@host/db",
            "api_key=secret-value",
            "数据库表名：private_tokens",
            "COS路径：private-bucket/object",
            "JavaScript代码：const token = 1",
            "evil.example.com/payload",
            "evil.example.xyz/payload",
            "EVIL.EXAMPLE.XYZ",
            "host.tech",
            "HOST.TECH",
            "127.0.0.1:8080/private",
            "const x=1;",
            "import os",
            "print(secret)",
            "provider_id=internal",
            "render=true",
            "db_host=127.0.0.1",
            "database_name=private",
        )
        for value in forbidden_values:
            scene = {**valid_plan()["scenes"][0], "intent": value}
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "字符串"):
                schema.validate_edit_plan(valid_plan(scenes=[scene]))

    def test_edit_plan_allows_ordinary_database_semantics(self):
        scene = {
            **valid_plan()["scenes"][0],
            "intent": "介绍产品数据库能力与业务价值",
            "headline": "供应商：本地农户。This is clear. Keep it concise.",
        }
        plan = valid_plan(scenes=[scene])

        self.assertIs(schema.validate_edit_plan(plan), plan)

    def test_edit_plan_allows_ordinary_period_text_without_spaces(self):
        for value in ("clear.Keep", "version.final", "release.note", "status.ok"):
            scene = {**valid_plan()["scenes"][0], "intent": value}
            plan = valid_plan(scenes=[scene])

            with self.subTest(value=value):
                self.assertIs(schema.validate_edit_plan(plan), plan)

    def test_material_slot_ids_use_a_strict_bounded_format(self):
        invalid_slots = (
            "slot 01",
            "../slot_01",
            "https://evil.example/slot",
            "slot_<script>",
            "slot_" + "x" * 65,
            "material_01",
        )
        for slot in invalid_slots:
            scene = {**valid_plan()["scenes"][0], "material_slots": [slot]}
            with self.subTest(slot=slot), self.assertRaisesRegex(ValueError, "槽位ID"):
                schema.validate_edit_plan(valid_plan(scenes=[scene]))

    def test_model_generated_strings_have_reasonable_length(self):
        scene = {**valid_plan()["scenes"][0], "headline": "长" * 501}

        with self.assertRaisesRegex(ValueError, "字符串"):
            schema.validate_edit_plan(valid_plan(scenes=[scene]))

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
