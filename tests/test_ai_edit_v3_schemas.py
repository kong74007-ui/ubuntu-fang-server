import copy
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from server.content_domains.ai_edit_v3.contracts import (
    ContractError,
    parse_strict_json,
    schema_sha256,
    validate_edit_plan,
    validate_quality_verdict,
    validate_render_manifest,
)


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_ROOT = ROOT / "server" / "content_domains" / "ai_edit_v3" / "schemas"
FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "ai_edit_v3"
SCHEMA_NAMES = (
    "edit-plan-2.0.schema.json",
    "render-manifest-v1.schema.json",
    "quality-verdict-v1.schema.json",
)
EXPECTED_ROOT_FIELDS = {
    "edit-plan-2.0.schema.json": {
        "version",
        "duration_ms",
        "ratio",
        "creative_concept",
        "theme",
        "narrative_arc",
        "captions",
        "source_segments",
        "scenes",
        "materials",
        "audio_cues",
    },
    "render-manifest-v1.schema.json": {
        "version",
        "schema_sha256",
        "renderer_environment",
        "output_spec",
        "duration_ms",
        "edit_plan_sha256",
        "registry_sha256",
        "theme",
        "seed",
        "source_video",
        "source_segments",
        "master_audio",
        "assets",
        "compositions",
        "captions",
    },
}


def load_schema(name):
    return json.loads((SCHEMA_ROOT / name).read_text(encoding="utf-8"))


def load_fixture(name):
    return json.loads((FIXTURE_ROOT / name).read_text(encoding="utf-8"))


def assert_all_object_nodes_closed(test_case, node, path="$"):
    if not isinstance(node, dict):
        return
    if node.get("type") == "object" or "properties" in node:
        test_case.assertIs(
            node.get("additionalProperties"),
            False,
            f"object schema remains open at {path}",
        )
    for key, value in node.items():
        if isinstance(value, dict):
            assert_all_object_nodes_closed(test_case, value, f"{path}.{key}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                assert_all_object_nodes_closed(
                    test_case,
                    item,
                    f"{path}.{key}[{index}]",
                )


def valid_timeline():
    return {
        "duration_ms": 4000,
        "accurate_captions": [
            {
                "id": "caption_01",
                "start_ms": 0,
                "end_ms": 2000,
                "text": "真实方法",
                "protected_terms": ["真实"],
            },
            {
                "id": "caption_02",
                "start_ms": 2000,
                "end_ms": 4000,
                "text": "提升效率",
                "protected_terms": ["提升效率"],
            },
        ],
        "layout_capabilities": [
            "speaker_fullscreen",
            "speaker_right_evidence_left",
        ],
        "overlay_capabilities": ["headline_block", "evidence_label"],
        "animation_capabilities": ["fade", "slide"],
        "transition_capabilities": ["hard_cut", "soft_wipe"],
        "theme_capabilities": {
            "palette_id": ["midnight_gold"],
            "typography_id": ["editorial_sans"],
            "density": ["balanced"],
            "motion_energy": ["medium"],
            "image_fit": ["cover"],
        },
    }


class SchemaMetaTests(unittest.TestCase):
    def test_all_schemas_are_draft_2020_12_and_closed_recursively(self):
        for name in SCHEMA_NAMES:
            with self.subTest(name=name):
                schema = load_schema(name)
                self.assertEqual(
                    schema["$schema"],
                    "https://json-schema.org/draft/2020-12/schema",
                )
                Draft202012Validator.check_schema(schema)
                assert_all_object_nodes_closed(self, schema)

    def test_edit_plan_and_render_manifest_have_exact_frozen_root_fields(self):
        for name, expected in EXPECTED_ROOT_FIELDS.items():
            with self.subTest(name=name):
                schema = load_schema(name)
                self.assertEqual(set(schema["properties"]), expected)
                self.assertEqual(set(schema["required"]), expected)

    def test_valid_fixtures_conform_to_their_schemas(self):
        for name in SCHEMA_NAMES:
            with self.subTest(name=name):
                schema = load_schema(name)
                fixture = load_fixture(
                    "valid-" + name.replace(".schema", "")
                )
                Draft202012Validator(schema).validate(fixture)

    def test_manifest_and_quality_fixtures_record_the_schema_hash(self):
        for name in (
            "render-manifest-v1.schema.json",
            "quality-verdict-v1.schema.json",
        ):
            with self.subTest(name=name):
                fixture = load_fixture(
                    "valid-" + name.replace(".schema", "")
                )
                self.assertEqual(fixture["schema_sha256"], schema_sha256(name))

    def test_nested_unknown_fields_and_boolean_integers_are_rejected(self):
        cases = []
        plan = load_fixture("valid-edit-plan-2.0.json")
        plan["scenes"][0]["animations"][0]["javascript"] = "alert(1)"
        cases.append(("edit-plan-2.0.schema.json", plan))
        plan = load_fixture("valid-edit-plan-2.0.json")
        plan["duration_ms"] = True
        cases.append(("edit-plan-2.0.schema.json", plan))
        manifest = load_fixture("valid-render-manifest-v1.json")
        manifest["renderer_environment"]["api_key"] = "secret"
        cases.append(("render-manifest-v1.schema.json", manifest))
        manifest = load_fixture("valid-render-manifest-v1.json")
        manifest["seed"] = True
        cases.append(("render-manifest-v1.schema.json", manifest))
        verdict = load_fixture("valid-quality-verdict-v1.json")
        verdict["checks"][0]["evidence"][0]["timestamp_ms"] = True
        cases.append(("quality-verdict-v1.schema.json", verdict))

        for name, value in cases:
            with self.subTest(name=name):
                self.assertFalse(
                    Draft202012Validator(load_schema(name)).is_valid(value)
                )

    def test_schema_limits_and_forbidden_machine_fields_are_executable(self):
        edit_schema = load_schema("edit-plan-2.0.schema.json")
        self.assertEqual(edit_schema["properties"]["scenes"]["maxItems"], 120)
        self.assertEqual(edit_schema["properties"]["captions"]["maxItems"], 2000)
        self.assertEqual(
            edit_schema["properties"]["source_segments"]["maxItems"],
            240,
        )
        render_schema = load_schema("render-manifest-v1.schema.json")
        quality_schema = load_schema("quality-verdict-v1.schema.json")
        self.assertEqual(quality_schema["properties"]["checks"]["maxItems"], 64)
        self.assertEqual(
            quality_schema["$defs"]["passCheck"]["properties"]["evidence"][
                "maxItems"
            ],
            8,
        )

        manifest = load_fixture("valid-render-manifest-v1.json")
        for field in ("output_path", "external_url", "script", "api_key"):
            with self.subTest(manifest_field=field):
                candidate = copy.deepcopy(manifest)
                candidate[field] = None
                self.assertFalse(
                    Draft202012Validator(render_schema).is_valid(candidate)
                )

        verdict = load_fixture("valid-quality-verdict-v1.json")
        for field in ("repair_prompt", "repair_result"):
            with self.subTest(verdict_field=field):
                candidate = copy.deepcopy(verdict)
                candidate[field] = None
                self.assertFalse(
                    Draft202012Validator(quality_schema).is_valid(candidate)
                )


class StrictJsonParserTests(unittest.TestCase):
    def parse(self, raw, **overrides):
        limits = {
            "max_bytes": 1024,
            "max_depth": 8,
            "max_items": 16,
            "max_string_chars": 32,
        }
        limits.update(overrides)
        return parse_strict_json(raw, **limits)

    def test_accepts_one_bounded_unicode_root(self):
        self.assertEqual(self.parse('{"text":"真实","ok":true}'), {
            "text": "真实",
            "ok": True,
        })

    def test_rejects_duplicate_keys(self):
        with self.assertRaisesRegex(ContractError, "json_duplicate_key"):
            self.parse('{"a":1,"a":2}')

    def test_rejects_multiple_roots_and_trailing_content(self):
        for raw in ('{"a":1} {"b":2}', '{"a":1} trailing'):
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(
                    ContractError,
                    "json_trailing_content",
                ):
                    self.parse(raw)

    def test_rejects_nonfinite_numbers(self):
        for token in ("NaN", "Infinity", "-Infinity", "1e999"):
            with self.subTest(token=token):
                with self.assertRaisesRegex(
                    ContractError,
                    "json_nonfinite_number",
                ):
                    self.parse(f'{{"value":{token}}}')

    def test_rejects_byte_depth_item_string_and_control_limits(self):
        cases = (
            ('{"x":"12345"}', {"max_bytes": 8}, "json_bytes_exceeded"),
            ("[[[0]]]", {"max_depth": 2}, "json_depth_exceeded"),
            ("[1,2,3]", {"max_items": 2}, "json_items_exceeded"),
            ('{"x":"12345"}', {"max_string_chars": 4}, "json_string_exceeded"),
            ('{"x":"\\u0000"}', {}, "control_character_forbidden"),
        )
        for raw, overrides, code in cases:
            with self.subTest(code=code):
                with self.assertRaisesRegex(ContractError, code):
                    self.parse(raw, **overrides)


class EditPlanValidatorTests(unittest.TestCase):
    def setUp(self):
        self.plan = load_fixture("valid-edit-plan-2.0.json")
        self.timeline = valid_timeline()

    def test_valid_edit_plan_is_normalized_without_mutating_the_input(self):
        original = copy.deepcopy(self.plan)

        result = validate_edit_plan(self.plan, timeline=self.timeline)

        self.assertEqual(result, original)
        self.assertEqual(self.plan, original)

    def test_rejects_unknown_component_and_broken_timeline(self):
        self.plan["scenes"][0]["layout_id"] = "freeform_canvas"
        with self.assertRaisesRegex(
            ContractError,
            "director_capability_unknown",
        ):
            validate_edit_plan(self.plan, timeline=self.timeline)

        self.plan = load_fixture("valid-edit-plan-2.0.json")
        self.plan["scenes"][1]["start_ms"] = 2100
        with self.assertRaisesRegex(
            ContractError,
            "scene_timeline_invalid",
        ):
            validate_edit_plan(self.plan, timeline=self.timeline)

    def test_rejects_missing_references_and_unresolved_required_material(self):
        self.plan["scenes"][0]["headline"]["source_caption_ids"] = [
            "caption_missing"
        ]
        with self.assertRaisesRegex(
            ContractError,
            "director_reference_unknown",
        ):
            validate_edit_plan(self.plan, timeline=self.timeline)

        self.plan = load_fixture("valid-edit-plan-2.0.json")
        self.plan["materials"] = []
        with self.assertRaisesRegex(
            ContractError,
            "required_material_unresolved",
        ):
            validate_edit_plan(self.plan, timeline=self.timeline)

    def test_rejects_visible_fact_text_tampering(self):
        self.plan["scenes"][0]["headline"]["text"] = "虚构承诺"
        with self.assertRaisesRegex(
            ContractError,
            "visible_text_inaccurate",
        ):
            validate_edit_plan(self.plan, timeline=self.timeline)

        self.plan = load_fixture("valid-edit-plan-2.0.json")
        self.plan["scenes"][1]["highlight"]["text"] = "更好"
        with self.assertRaisesRegex(
            ContractError,
            "visible_text_protected_fact_changed",
        ):
            validate_edit_plan(self.plan, timeline=self.timeline)

    def test_rejects_authoritative_caption_changes_and_segment_discontinuity(self):
        self.plan["captions"][0]["text"] = "篡改方法"
        with self.assertRaisesRegex(
            ContractError,
            "accurate_text_changed",
        ):
            validate_edit_plan(self.plan, timeline=self.timeline)

        self.plan = load_fixture("valid-edit-plan-2.0.json")
        self.plan["source_segments"][0]["output_start_ms"] = 1
        with self.assertRaisesRegex(
            ContractError,
            "source_segment_timeline_invalid",
        ):
            validate_edit_plan(self.plan, timeline=self.timeline)

    def test_rejects_duplicate_ids_and_out_of_bounds_cues(self):
        self.plan["scenes"][1]["id"] = self.plan["scenes"][0]["id"]
        with self.assertRaisesRegex(ContractError, "director_id_duplicate"):
            validate_edit_plan(self.plan, timeline=self.timeline)

        self.plan = load_fixture("valid-edit-plan-2.0.json")
        self.plan["audio_cues"][0]["end_ms"] = 4001
        with self.assertRaises(ContractError):
            validate_edit_plan(self.plan, timeline=self.timeline)

    def test_rejects_control_characters_even_for_preparsed_objects(self):
        self.plan["creative_concept"] = "unsafe\u0000concept"
        with self.assertRaisesRegex(
            ContractError,
            "control_character_forbidden",
        ):
            validate_edit_plan(self.plan, timeline=self.timeline)


class RenderManifestValidatorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.sandbox = Path(self.temporary.name)
        (self.sandbox / "media").mkdir()
        (self.sandbox / "media" / "source.mp4").write_bytes(b"video")
        (self.sandbox / "media" / "master.wav").write_bytes(b"audio")
        (self.sandbox / "media" / "image.png").write_bytes(b"image")
        self.manifest = load_fixture("valid-render-manifest-v1.json")

    def test_valid_manifest_checks_every_declared_local_file(self):
        original = copy.deepcopy(self.manifest)

        result = validate_render_manifest(
            self.manifest,
            sandbox_root=self.sandbox,
        )

        self.assertEqual(result, original)
        self.assertEqual(self.manifest, original)

    def test_rejects_absolute_parent_and_windows_media_paths(self):
        for path in (
            str((self.sandbox / "media" / "source.mp4").resolve()),
            "../source.mp4",
            "media\\source.mp4",
        ):
            with self.subTest(path=path):
                manifest = copy.deepcopy(self.manifest)
                manifest["source_video"]["path"] = path
                with self.assertRaisesRegex(
                    ContractError,
                    "render_path_invalid",
                ):
                    validate_render_manifest(
                        manifest,
                        sandbox_root=self.sandbox,
                    )

    def test_rejects_symlink(self):
        link = self.sandbox / "media" / "source-link.mp4"
        try:
            os.symlink(
                self.sandbox / "media" / "source.mp4",
                link,
            )
            relative_path = "media/source-link.mp4"
        except OSError:
            link = self.sandbox / "media" / "source-link"
            created = subprocess.run(
                [
                    "cmd",
                    "/c",
                    "mklink",
                    "/J",
                    str(link),
                    str(self.sandbox / "media"),
                ],
                capture_output=True,
                check=False,
            )
            if created.returncode != 0:
                self.fail("neither symlink nor junction was available")
            relative_path = "media/source-link/source.mp4"
        manifest = copy.deepcopy(self.manifest)
        manifest["source_video"]["path"] = relative_path
        with self.assertRaisesRegex(ContractError, "render_file_not_regular"):
            validate_render_manifest(manifest, sandbox_root=self.sandbox)

    def test_rejects_hardlink(self):
        link = self.sandbox / "media" / "source-hardlink.mp4"
        os.link(self.sandbox / "media" / "source.mp4", link)
        manifest = copy.deepcopy(self.manifest)
        manifest["source_video"]["path"] = "media/source-hardlink.mp4"

        with self.assertRaisesRegex(ContractError, "render_file_not_regular"):
            validate_render_manifest(manifest, sandbox_root=self.sandbox)

    def test_rejects_hash_mismatch(self):
        manifest = copy.deepcopy(self.manifest)
        manifest["assets"][0]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ContractError, "render_hash_mismatch"):
            validate_render_manifest(manifest, sandbox_root=self.sandbox)

    def test_rejects_unknown_capability_and_reference(self):
        self.manifest["compositions"][0]["layout_id"] = "freeform_canvas"
        with self.assertRaisesRegex(
            ContractError,
            "render_capability_unknown",
        ):
            validate_render_manifest(
                self.manifest,
                sandbox_root=self.sandbox,
            )

        self.manifest = load_fixture("valid-render-manifest-v1.json")
        self.manifest["compositions"][0]["animations"][0][
            "target"
        ] = "overlay_missing"
        with self.assertRaisesRegex(
            ContractError,
            "render_reference_unknown",
        ):
            validate_render_manifest(
                self.manifest,
                sandbox_root=self.sandbox,
            )

        self.manifest = load_fixture("valid-render-manifest-v1.json")
        self.manifest["compositions"][0]["asset_ids"] = ["asset_missing"]
        with self.assertRaisesRegex(
            ContractError,
            "render_reference_unknown",
        ):
            validate_render_manifest(
                self.manifest,
                sandbox_root=self.sandbox,
            )

    def test_rejects_ratio_dimension_duration_and_source_mapping_mismatch(self):
        cases = []
        manifest = copy.deepcopy(self.manifest)
        manifest["output_spec"]["width"] = 1080
        cases.append(manifest)
        manifest = copy.deepcopy(self.manifest)
        manifest["master_audio"]["duration_ms"] = 3999
        cases.append(manifest)
        manifest = copy.deepcopy(self.manifest)
        manifest["source_segments"][0]["output_start_ms"] = 1
        cases.append(manifest)

        for manifest in cases:
            with self.subTest(manifest=manifest):
                with self.assertRaises(ContractError):
                    validate_render_manifest(
                        manifest,
                        sandbox_root=self.sandbox,
                    )


class QualityVerdictValidatorTests(unittest.TestCase):
    def setUp(self):
        self.verdict = load_fixture("valid-quality-verdict-v1.json")

    def test_valid_quality_verdict_is_accepted_without_mutation(self):
        original = copy.deepcopy(self.verdict)
        self.assertEqual(validate_quality_verdict(self.verdict), original)
        self.assertEqual(self.verdict, original)

    def test_rejects_unknown_check_and_duplicate_check_ids(self):
        self.verdict["checks"][0]["check_id"] = "invented_check"
        with self.assertRaisesRegex(
            ContractError,
            "quality_check_unknown",
        ):
            validate_quality_verdict(self.verdict)

        self.verdict = load_fixture("valid-quality-verdict-v1.json")
        self.verdict["checks"].append(copy.deepcopy(self.verdict["checks"][0]))
        with self.assertRaisesRegex(
            ContractError,
            "quality_check_duplicate",
        ):
            validate_quality_verdict(self.verdict)

    def test_rejects_pass_without_evidence_and_keeps_blocking_unknown_nonpass(
        self,
    ):
        self.verdict["checks"][0]["evidence"] = []
        with self.assertRaisesRegex(
            ContractError,
            "quality_schema_invalid",
        ):
            validate_quality_verdict(self.verdict)

        self.verdict = load_fixture("valid-quality-verdict-v1.json")
        self.verdict["checks"][0]["result"] = "unknown"
        result = validate_quality_verdict(self.verdict)
        self.assertEqual(result["checks"][0]["result"], "unknown")
        self.assertNotEqual(result["checks"][0]["result"], "pass")

    def test_rejects_nonfinite_confidence_and_nested_unknown_fields(self):
        self.verdict["checks"][0]["confidence"] = float("nan")
        with self.assertRaisesRegex(
            ContractError,
            "quality_confidence_invalid",
        ):
            validate_quality_verdict(self.verdict)

        self.verdict = load_fixture("valid-quality-verdict-v1.json")
        self.verdict["checks"][0]["evidence"][0]["url"] = "https://evil"
        with self.assertRaisesRegex(
            ContractError,
            "quality_schema_invalid",
        ):
            validate_quality_verdict(self.verdict)

    def test_rejects_control_characters_and_evidence_overflow(self):
        self.verdict["checks"][0]["reason"] = "unsafe\u0000reason"
        with self.assertRaisesRegex(
            ContractError,
            "control_character_forbidden",
        ):
            validate_quality_verdict(self.verdict)

        self.verdict = load_fixture("valid-quality-verdict-v1.json")
        evidence = self.verdict["checks"][0]["evidence"][0]
        self.verdict["checks"][0]["evidence"] = [
            copy.deepcopy(evidence) for _ in range(9)
        ]
        with self.assertRaisesRegex(
            ContractError,
            "quality_schema_invalid",
        ):
            validate_quality_verdict(self.verdict)
