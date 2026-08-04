import copy
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jsonschema import Draft202012Validator

from server.content_domains.ai_edit_v3 import contracts as contracts_module
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
    "director-decision-v1.schema.json",
    "edit-plan-2.0.schema.json",
    "render-manifest-v1.schema.json",
    "quality-verdict-v1.schema.json",
)
QUALITY_BLOCKING = {
    "media_decode_codec_dimensions": True,
    "av_duration_sync": True,
    "black_frames": True,
    "abnormal_freeze": True,
    "audio_integrity": True,
    "caption_fact_accuracy": True,
    "safe_area_and_text_visibility": True,
    "face_product_obstruction": True,
    "material_provenance": True,
    "material_semantic_identity": True,
    "generated_evidence_claim": True,
    "opening_hook_visual_consistency": False,
}
EXPECTED_ROOT_FIELDS = {
    "director-decision-v1.schema.json": {
        "version",
        "creative_concept",
        "narrative_pattern",
        "theme_profile_id",
        "design_intent",
        "scene_directives",
        "audio_intent",
    },
    "edit-plan-2.0.schema.json": {
        "version",
        "visual_program_version",
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
    if (node.get("type") == "object" or "properties" in node) and not path.startswith("$.allOf"):
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
        "overlay_capabilities": ["headline_block", "info_card"],
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


def complete_quality_verdict():
    verdict = load_fixture("valid-quality-verdict-v1.json")
    prototype = verdict["checks"][0]
    verdict["checks"] = []
    for check_id, blocking in QUALITY_BLOCKING.items():
        check = copy.deepcopy(prototype)
        check["check_id"] = check_id
        check["blocking"] = blocking
        verdict["checks"].append(check)
    return verdict


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
                self.assertEqual(
                    set(schema["required"]),
                    expected - {"visual_program_version"},
                )

    def test_phase_a_schemas_freeze_layout_variant_and_theme_identity(self):
        edit_schema = load_schema("edit-plan-2.0.schema.json")
        render_schema = load_schema("render-manifest-v1.schema.json")

        for schema, owner in (
            (edit_schema, "scene"),
            (render_schema, "composition"),
        ):
            with self.subTest(schema=schema["title"]):
                self.assertEqual(
                    schema["$defs"][owner]["properties"][
                        "layout_variant"
                    ].get("const"),
                    "balanced_a",
                )
                self.assertEqual(
                    schema["$defs"]["theme"]["properties"][
                        "palette_id"
                    ].get("const"),
                    "midnight_gold",
                )
                self.assertEqual(
                    schema["$defs"]["theme"]["properties"][
                        "typography_id"
                    ].get("const"),
                    "editorial_sans",
                )

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

    def test_edit_plan_fixture_is_bound_to_the_frozen_schema_digest(self):
        fixture = load_fixture("valid-edit-plan-2.0.json")
        digest = schema_sha256("edit-plan-2.0.schema.json")

        self.assertEqual(
            digest,
            "1dfc64bdfe8bee1a37d2ceb8eb7d6f52f2c2e3df1f80be9919d42a788ec6627c",
        )
        Draft202012Validator(
            load_schema("edit-plan-2.0.schema.json")
        ).validate(fixture)

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

    def test_rejects_non_json_surrounding_whitespace(self):
        for raw in (
            '\u001c{"a":1}',
            '{"a":1}\u001c',
            '\u00a0{"a":1}',
            '{"a":1}\u00a0',
        ):
            with self.subTest(raw=ascii(raw)):
                with self.assertRaises(ContractError):
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

    def test_extreme_nesting_fails_with_depth_error_not_recursion_error(self):
        raw = "[" * 2000 + "0" + "]" * 2000

        with self.assertRaisesRegex(ContractError, "json_depth_exceeded"):
            self.parse(raw, max_bytes=10000, max_depth=24)

    def test_rejects_lone_surrogates_after_json_escape_decoding(self):
        with self.assertRaisesRegex(
            ContractError,
            "unicode_scalar_invalid",
        ):
            self.parse('{"text":"\\ud800"}')

    def test_rejects_python_string_lone_surrogate_stably(self):
        with self.assertRaisesRegex(
            ContractError,
            "unicode_scalar_invalid",
        ):
            self.parse('{"text":"\ud800"}')

    def test_rejects_integer_beyond_python_digit_limit_stably(self):
        raw = '{"value":' + ("9" * 5000) + "}"

        with self.assertRaisesRegex(ContractError, "json_invalid"):
            self.parse(raw, max_bytes=6000)


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

    def test_phase_a_compressed_text_rejects_every_substantive_rewrite(self):
        cases = (
            ("not effective", "effective"),
            ("price $100", "price 100"),
            ("别购买", "购买"),
            ("提升源于方法", "提升方法"),
            ("确保真实结果", "真实结果"),
            ("真的有效吗？", "真的有效"),
        )
        for source, compressed in cases:
            with self.subTest(source=source, compressed=compressed):
                plan = load_fixture("valid-edit-plan-2.0.json")
                timeline = valid_timeline()
                plan["captions"][1]["text"] = source
                plan["scenes"][1]["headline"]["text"] = source
                plan["scenes"][1]["highlight"]["text"] = compressed
                timeline["accurate_captions"][1]["text"] = source
                timeline["accurate_captions"][1]["protected_terms"] = []

                with self.assertRaisesRegex(
                    ContractError,
                    "visible_text_protected_fact_changed",
                ):
                    validate_edit_plan(plan, timeline=timeline)

    def test_phase_a_compressed_text_accepts_nfc_equivalent_caption_text(self):
        source = "Cafe\u0301"
        plan = load_fixture("valid-edit-plan-2.0.json")
        timeline = valid_timeline()
        plan["captions"][1]["text"] = source
        plan["scenes"][1]["headline"]["text"] = source
        plan["scenes"][1]["highlight"]["text"] = "Caf\u00e9"
        timeline["accurate_captions"][1]["text"] = source
        timeline["accurate_captions"][1]["protected_terms"] = []

        self.assertEqual(validate_edit_plan(plan, timeline=timeline), plan)

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

    def test_capability_inputs_cannot_enlarge_or_omit_frozen_registry(self):
        enlarged = copy.deepcopy(self.timeline)
        enlarged["layout_capabilities"].append("freeform_canvas")
        with self.assertRaisesRegex(
            ContractError,
            "timeline_capability_invalid",
        ):
            validate_edit_plan(self.plan, timeline=enlarged)

        missing_theme = copy.deepcopy(self.timeline)
        del missing_theme["theme_capabilities"]
        with self.assertRaisesRegex(
            ContractError,
            "timeline_capability_missing",
        ):
            validate_edit_plan(self.plan, timeline=missing_theme)

        enlarged_theme = copy.deepcopy(self.timeline)
        enlarged_theme["theme_capabilities"]["palette_id"].append(
            "model_invented_palette"
        )
        with self.assertRaisesRegex(
            ContractError,
            "timeline_capability_invalid",
        ):
            validate_edit_plan(self.plan, timeline=enlarged_theme)

    def test_primary_capability_lists_are_mandatory_and_null_fails_closed(self):
        fields = (
            "layout_capabilities",
            "overlay_capabilities",
            "animation_capabilities",
            "transition_capabilities",
        )
        for field in fields:
            with self.subTest(field=field, value="missing"):
                timeline = copy.deepcopy(self.timeline)
                del timeline[field]
                with self.assertRaisesRegex(
                    ContractError,
                    "timeline_capability_missing",
                ):
                    validate_edit_plan(self.plan, timeline=timeline)
            with self.subTest(field=field, value=None):
                timeline = copy.deepcopy(self.timeline)
                timeline[field] = None
                with self.assertRaisesRegex(
                    ContractError,
                    "timeline_capability_invalid",
                ):
                    validate_edit_plan(self.plan, timeline=timeline)

    def test_unhashable_capability_members_fail_with_contract_error(self):
        cases = (
            ("layout_capabilities", None),
            ("theme_capabilities", "palette_id"),
        )
        for field, nested in cases:
            with self.subTest(field=field, nested=nested):
                timeline = copy.deepcopy(self.timeline)
                if nested is None:
                    timeline[field] = [[]]
                else:
                    timeline[field][nested] = [[]]
                with self.assertRaisesRegex(
                    ContractError,
                    "timeline_capability_invalid",
                ):
                    validate_edit_plan(self.plan, timeline=timeline)

    def test_undocumented_capability_aliases_always_fail_closed(self):
        aliases = (
            ("layout_capabilities", "layout_ids"),
            ("overlay_capabilities", "overlay_ids"),
            ("animation_capabilities", "animation_ids"),
            ("transition_capabilities", "transition_ids"),
        )
        for primary, alternate in aliases:
            with self.subTest(primary=primary, shape="alternate_only"):
                timeline = copy.deepcopy(self.timeline)
                timeline[alternate] = timeline.pop(primary)
                with self.assertRaisesRegex(
                    ContractError,
                    "timeline_capability_invalid",
                ) as caught:
                    validate_edit_plan(self.plan, timeline=timeline)
                self.assertEqual(caught.exception.field_path, alternate)

            with self.subTest(primary=primary, shape="primary_and_alternate"):
                timeline = copy.deepcopy(self.timeline)
                timeline[alternate] = [[]]
                with self.assertRaisesRegex(
                    ContractError,
                    "timeline_capability_invalid",
                ) as caught:
                    validate_edit_plan(self.plan, timeline=timeline)
                self.assertEqual(caught.exception.field_path, alternate)

    def test_edit_plan_rejects_unpublished_layout_variant(self):
        self.plan["scenes"][0]["layout_variant"] = "balanced_b"

        with self.assertRaisesRegex(ContractError, "director_schema_invalid"):
            validate_edit_plan(self.plan, timeline=self.timeline)

    def test_material_requests_must_exactly_bind_slots_and_time_ranges(self):
        mutations = (
            ("semantic", "different semantic"),
            ("purpose", "product"),
            ("priority", "optional"),
            ("ratio", "9:16"),
            ("time_range", {"start_ms": 0, "end_ms": 1900}),
        )
        for field, value in mutations:
            with self.subTest(field=field):
                plan = load_fixture("valid-edit-plan-2.0.json")
                plan["materials"][0][field] = value
                with self.assertRaisesRegex(
                    ContractError,
                    "material_request_mismatch",
                ):
                    validate_edit_plan(plan, timeline=self.timeline)

        orphan = load_fixture("valid-edit-plan-2.0.json")
        extra = copy.deepcopy(orphan["materials"][0])
        extra["request_id"] = "slot_orphan"
        orphan["materials"].append(extra)
        with self.assertRaisesRegex(
            ContractError,
            "material_request_unbound",
        ):
            validate_edit_plan(orphan, timeline=self.timeline)

        out_of_bounds = load_fixture("valid-edit-plan-2.0.json")
        out_of_bounds["materials"][0]["time_range"]["end_ms"] = 4001
        with self.assertRaisesRegex(
            ContractError,
            "material_request_timeline_invalid",
        ):
            validate_edit_plan(out_of_bounds, timeline=self.timeline)

    def test_material_slot_ids_are_unique_within_one_scene(self):
        same_scene = load_fixture("valid-edit-plan-2.0.json")
        same_scene["scenes"][0]["material_slots"].append(
            copy.deepcopy(same_scene["scenes"][0]["material_slots"][0])
        )
        with self.assertRaisesRegex(
            ContractError,
            "director_id_duplicate",
        ) as caught:
            validate_edit_plan(same_scene, timeline=self.timeline)
        self.assertEqual(
            caught.exception.field_path,
            "scenes[0].material_slots[1].id",
        )

    def test_material_slot_ids_are_unique_across_scenes(self):
        cross_scene = load_fixture("valid-edit-plan-2.0.json")
        cross_scene["scenes"][1]["material_slots"].append(
            copy.deepcopy(cross_scene["scenes"][0]["material_slots"][0])
        )
        with self.assertRaisesRegex(
            ContractError,
            "director_id_duplicate",
        ) as caught:
            validate_edit_plan(cross_scene, timeline=self.timeline)
        self.assertEqual(
            caught.exception.field_path,
            "scenes[1].material_slots[0].id",
        )

    def test_rejects_lone_surrogates_in_preparsed_edit_plan(self):
        self.plan["creative_concept"] = "unsafe\ud800concept"
        with self.assertRaisesRegex(
            ContractError,
            "unicode_scalar_invalid",
        ):
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

    def audio_only_manifest(self):
        manifest = copy.deepcopy(self.manifest)
        manifest["source_video"] = None
        segment = manifest["source_segments"][0]
        segment["source_path"] = manifest["master_audio"]["path"]
        segment["sha256"] = manifest["master_audio"]["sha256"]
        segment["source_start_ms"] = segment["output_start_ms"]
        segment["source_end_ms"] = segment["output_end_ms"]
        return manifest

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
        manifest["source_segments"][0]["source_path"] = relative_path
        with self.assertRaisesRegex(ContractError, "render_file_not_regular"):
            validate_render_manifest(manifest, sandbox_root=self.sandbox)

    def test_rejects_hardlink(self):
        link = self.sandbox / "media" / "source-hardlink.mp4"
        os.link(self.sandbox / "media" / "source.mp4", link)
        manifest = copy.deepcopy(self.manifest)
        manifest["source_video"]["path"] = "media/source-hardlink.mp4"
        manifest["source_segments"][0][
            "source_path"
        ] = "media/source-hardlink.mp4"

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

    def test_source_segments_are_bound_to_source_video_and_its_duration(self):
        asset_bound = copy.deepcopy(self.manifest)
        segment = asset_bound["source_segments"][0]
        asset = asset_bound["assets"][0]
        segment["source_path"] = asset["path"]
        segment["sha256"] = asset["sha256"]
        with self.assertRaisesRegex(
            ContractError,
            "render_source_video_binding_invalid",
        ):
            validate_render_manifest(asset_bound, sandbox_root=self.sandbox)

        overrun = copy.deepcopy(self.manifest)
        overrun["source_segments"][0]["source_end_ms"] = 5001
        with self.assertRaisesRegex(
            ContractError,
            "render_source_video_binding_invalid",
        ):
            validate_render_manifest(overrun, sandbox_root=self.sandbox)

    def test_audio_only_modes_bind_identity_segments_to_master_audio(self):
        for input_type in (
            "existing_audio",
            "uploaded_audio",
            "script_to_audio_video",
        ):
            with self.subTest(input_type=input_type):
                manifest = self.audio_only_manifest()
                self.assertEqual(
                    validate_render_manifest(
                        manifest,
                        sandbox_root=self.sandbox,
                    ),
                    manifest,
                )

    def test_audio_only_segments_reject_arbitrary_nonidentity_and_overrun(self):
        arbitrary = self.audio_only_manifest()
        segment = arbitrary["source_segments"][0]
        segment["source_path"] = arbitrary["assets"][0]["path"]
        segment["sha256"] = arbitrary["assets"][0]["sha256"]
        with self.assertRaisesRegex(
            ContractError,
            "render_source_audio_binding_invalid",
        ):
            validate_render_manifest(arbitrary, sandbox_root=self.sandbox)

        nonidentity = self.audio_only_manifest()
        nonidentity["source_segments"][0]["source_start_ms"] = 1
        with self.assertRaisesRegex(
            ContractError,
            "render_source_mapping_invalid",
        ):
            validate_render_manifest(nonidentity, sandbox_root=self.sandbox)

        overrun = self.audio_only_manifest()
        overrun["source_segments"][0]["source_end_ms"] = 4001
        with self.assertRaisesRegex(
            ContractError,
            "render_source_audio_binding_invalid",
        ):
            validate_render_manifest(overrun, sandbox_root=self.sandbox)

    def test_manifest_rejects_unpublished_theme_and_layout_variant(self):
        mutations = (
            ("theme.palette_id", "model_palette"),
            ("theme.typography_id", "model_typography"),
            ("compositions[0].layout_variant", "balanced_b"),
        )
        for field, value in mutations:
            with self.subTest(field=field):
                manifest = copy.deepcopy(self.manifest)
                if field == "theme.palette_id":
                    manifest["theme"]["palette_id"] = value
                elif field == "theme.typography_id":
                    manifest["theme"]["typography_id"] = value
                else:
                    manifest["compositions"][0]["layout_variant"] = value
                with self.assertRaisesRegex(
                    ContractError,
                    "render_schema_invalid",
                ):
                    validate_render_manifest(
                        manifest,
                        sandbox_root=self.sandbox,
                    )

    def test_schema_validation_precedes_file_traversal_and_errors_are_stable(self):
        malformed = copy.deepcopy(self.manifest)
        malformed["assets"] = None
        with self.assertRaisesRegex(ContractError, "render_schema_invalid"):
            validate_render_manifest(malformed, sandbox_root=self.sandbox)

        directory = copy.deepcopy(self.manifest)
        directory["assets"][0]["path"] = "media"
        with self.assertRaisesRegex(
            ContractError,
            "render_file_not_regular",
        ):
            validate_render_manifest(directory, sandbox_root=self.sandbox)

        target = self.sandbox / "media" / "image.png"
        original_open = Path.open

        def denied_open(path, *args, **kwargs):
            if path == target:
                raise PermissionError("denied for test")
            return original_open(path, *args, **kwargs)

        with mock.patch.object(Path, "open", denied_open):
            with self.assertRaisesRegex(
                ContractError,
                "render_file_unreadable",
            ):
                validate_render_manifest(
                    copy.deepcopy(self.manifest),
                    sandbox_root=self.sandbox,
                )

    def test_detects_path_swap_between_metadata_check_and_open(self):
        target = self.sandbox / "media" / "source.mp4"
        replacement = self.sandbox / "media" / "replacement.mp4"
        replacement.write_bytes(b"swap!")
        original_open = Path.open
        swapped = False

        def swapping_open(path, *args, **kwargs):
            nonlocal swapped
            if path == target and not swapped:
                os.replace(replacement, target)
                swapped = True
            return original_open(path, *args, **kwargs)

        with mock.patch.object(Path, "open", swapping_open):
            with self.assertRaisesRegex(
                ContractError,
                "render_file_identity_changed",
            ):
                validate_render_manifest(
                    copy.deepcopy(self.manifest),
                    sandbox_root=self.sandbox,
                )

    def test_rejects_hardlink_created_during_first_file_read(self):
        target = self.sandbox / "media" / "source.mp4"
        hardlink = self.sandbox / "media" / "late-hardlink.mp4"
        original_open = Path.open

        class LinkingStream:
            def __init__(self, stream):
                self.stream = stream
                self.linked = False

            def read(self, *args, **kwargs):
                if not self.linked:
                    os.link(target, hardlink)
                    self.linked = True
                return self.stream.read(*args, **kwargs)

            def __getattr__(self, name):
                return getattr(self.stream, name)

        def linking_open(path, *args, **kwargs):
            stream = original_open(path, *args, **kwargs)
            if path == target:
                return LinkingStream(stream)
            return stream

        with mock.patch.object(Path, "open", linking_open):
            with self.assertRaisesRegex(
                ContractError,
                "render_file_not_regular",
            ):
                validate_render_manifest(
                    copy.deepcopy(self.manifest),
                    sandbox_root=self.sandbox,
                )

    def test_repeated_file_declarations_are_hashed_once(self):
        real_sha256 = contracts_module.hashlib.sha256
        file_hash_calls = 0

        def tracked_sha256(*args, **kwargs):
            nonlocal file_hash_calls
            if not args and not kwargs:
                file_hash_calls += 1
            return real_sha256(*args, **kwargs)

        with mock.patch.object(
            contracts_module.hashlib,
            "sha256",
            side_effect=tracked_sha256,
        ):
            validate_render_manifest(
                copy.deepcopy(self.manifest),
                sandbox_root=self.sandbox,
            )
        self.assertEqual(file_hash_calls, 3)

    def test_declared_size_mismatch_is_rejected_before_hashing_file_body(self):
        manifest = copy.deepcopy(self.manifest)
        manifest["assets"][0]["size_bytes"] = 4
        real_sha256 = contracts_module.hashlib.sha256
        file_hash_calls = 0

        def guarded_sha256(*args, **kwargs):
            nonlocal file_hash_calls
            if not args and not kwargs:
                file_hash_calls += 1
                if file_hash_calls >= 3:
                    raise AssertionError("oversized body was hashed")
            return real_sha256(*args, **kwargs)

        with mock.patch.object(
            contracts_module.hashlib,
            "sha256",
            side_effect=guarded_sha256,
        ):
            with self.assertRaisesRegex(
                ContractError,
                "render_size_mismatch",
            ):
                validate_render_manifest(
                    manifest,
                    sandbox_root=self.sandbox,
                )

    def test_rejects_lone_surrogates_in_preparsed_manifest(self):
        self.manifest["renderer_environment"]["node_version"] = "v\ud800"
        with self.assertRaisesRegex(
            ContractError,
            "unicode_scalar_invalid",
        ):
            validate_render_manifest(
                self.manifest,
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

    def test_quality_policy_mapping_is_immutable_and_validation_unchanged(self):
        policy = contracts_module._QUALITY_BLOCKING
        original = policy["caption_fact_accuracy"]
        try:
            with self.assertRaises(TypeError):
                policy["caption_fact_accuracy"] = not original
        finally:
            if policy["caption_fact_accuracy"] != original:
                policy["caption_fact_accuracy"] = original

        verdict = complete_quality_verdict()
        caption = next(
            check
            for check in verdict["checks"]
            if check["check_id"] == "caption_fact_accuracy"
        )
        caption["blocking"] = False
        with self.assertRaisesRegex(
            ContractError,
            "quality_blocking_mismatch",
        ):
            validate_quality_verdict(verdict)

    def test_unhashable_quality_check_id_fails_with_contract_error(self):
        self.verdict["checks"][0]["check_id"] = []

        with self.assertRaisesRegex(ContractError, "quality_check_unknown"):
            validate_quality_verdict(self.verdict)

    def test_requires_complete_frozen_check_set_and_blocking_classification(self):
        complete = complete_quality_verdict()
        self.assertEqual(validate_quality_verdict(complete), complete)

        missing = copy.deepcopy(complete)
        missing["checks"] = missing["checks"][:-1]
        with self.assertRaisesRegex(
            ContractError,
            "quality_check_missing",
        ) as caught:
            validate_quality_verdict(missing)
        self.assertEqual(caught.exception.field_path, "checks")

        misclassified = copy.deepcopy(complete)
        caption = next(
            check
            for check in misclassified["checks"]
            if check["check_id"] == "caption_fact_accuracy"
        )
        caption["blocking"] = False
        with self.assertRaisesRegex(
            ContractError,
            "quality_blocking_mismatch",
        ) as caught:
            validate_quality_verdict(misclassified)
        self.assertRegex(caught.exception.field_path, r"checks\[\d+\]\.blocking")

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

    def test_rejects_lone_surrogates_in_preparsed_quality_verdict(self):
        self.verdict["checks"][0]["reason"] = "unsafe\ud800reason"
        with self.assertRaisesRegex(
            ContractError,
            "unicode_scalar_invalid",
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
