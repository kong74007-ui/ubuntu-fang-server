import copy
import unittest

from server.content_domains.ai_edit_v3.contracts import (
    ALLOWED_TRANSITIONS,
    MEDIA_STATES,
    RECONCILIATION_STATES,
    TERMINAL_STATES,
    ContractError,
    canonical_json,
    normalize_job_request,
    request_fingerprint,
)

INPUT_CASES = {
    "platform_talking_head": {
        "source_asset_id": "video_123",
        "ratio": "auto",
    },
    "uploaded_video": {
        "source_upload_id": "upload_video_123",
        "ratio": "auto",
    },
    "existing_audio": {
        "source_asset_id": "audio_123",
        "ratio": "16:9",
    },
    "uploaded_audio": {
        "source_upload_id": "upload_audio_123",
        "ratio": "9:16",
    },
    "script_to_audio_video": {
        "tts_input": {
            "text": "可信的准确文本",
            "voice_id": "voice_123",
        },
        "ratio": "16:9",
    },
}
CREATION_CASES = {
    "ai_auto": {},
    "style_prompt": {"style_prompt": "克制、可信"},
    "template_reference": {"template_id": "tpl_published_both_01"},
}


def valid_request(**overrides):
    body = {
        "input_type": "uploaded_video",
        "source_upload_id": "up-1",
        "ratio": "auto",
        "creation_mode": "ai_auto",
        "material_asset_ids": [],
    }
    body.update(overrides)
    return body


def matrix_request(input_type, creation_mode):
    return {
        "input_type": input_type,
        **copy.deepcopy(INPUT_CASES[input_type]),
        "creation_mode": creation_mode,
        **copy.deepcopy(CREATION_CASES[creation_mode]),
        "material_asset_ids": ["image_01", "image_02"],
    }


class RequestContractTests(unittest.TestCase):
    def test_all_five_inputs_accept_all_three_creation_modes(self):
        for input_type in INPUT_CASES:
            for creation_mode in CREATION_CASES:
                with self.subTest(
                    input_type=input_type,
                    creation_mode=creation_mode,
                ):
                    body = matrix_request(input_type, creation_mode)
                    self.assertEqual(normalize_job_request(body), body)

    def test_every_input_creation_combination_rejects_present_unused_source(self):
        source_fields = ("source_asset_id", "source_upload_id", "tts_input")
        for input_type in INPUT_CASES:
            expected = next(
                field
                for field in source_fields
                if field in INPUT_CASES[input_type]
            )
            conflicting = next(
                field for field in source_fields if field != expected
            )
            for creation_mode in CREATION_CASES:
                with self.subTest(
                    input_type=input_type,
                    creation_mode=creation_mode,
                ):
                    body = matrix_request(input_type, creation_mode)
                    body[conflicting] = None
                    with self.assertRaisesRegex(
                        ContractError,
                        "input_discriminator_conflict",
                    ):
                        normalize_job_request(body)

    def test_every_input_creation_combination_rejects_present_unused_mode_field(
        self,
    ):
        conflicts = {
            "ai_auto": ("style_prompt", None),
            "style_prompt": ("template_id", None),
            "template_reference": ("style_prompt", None),
        }
        for input_type in INPUT_CASES:
            for creation_mode in CREATION_CASES:
                with self.subTest(
                    input_type=input_type,
                    creation_mode=creation_mode,
                ):
                    body = matrix_request(input_type, creation_mode)
                    field, value = conflicts[creation_mode]
                    body[field] = value
                    with self.assertRaisesRegex(
                        ContractError,
                        "creation_mode_conflict",
                    ):
                        normalize_job_request(body)

    def test_existing_audio_defaults_ratio_and_preserves_request_values(self):
        body = {
            "input_type": "existing_audio",
            "source_asset_id": "audio-1",
            "creation_mode": "style_prompt",
            "style_prompt": "  克制可信  ",
            "material_asset_ids": ["image-1"],
        }

        normalized = normalize_job_request(body)

        self.assertEqual(
            normalized,
            {
                "input_type": "existing_audio",
                "source_asset_id": "audio-1",
                "ratio": "16:9",
                "creation_mode": "style_prompt",
                "style_prompt": "  克制可信  ",
                "material_asset_ids": ["image-1"],
            },
        )

    def test_normalized_request_deep_copies_nested_caller_values(self):
        body = matrix_request("script_to_audio_video", "ai_auto")

        normalized = normalize_job_request(body)
        body["tts_input"]["text"] = "mutated"
        body["material_asset_ids"].append("image_03")

        self.assertEqual(normalized["tts_input"]["text"], "可信的准确文本")
        self.assertEqual(
            normalized["material_asset_ids"],
            ["image_01", "image_02"],
        )

    def test_uploaded_video_rejects_even_null_unused_sources(self):
        body = valid_request(source_asset_id=None)

        with self.assertRaisesRegex(
            ContractError, "input_discriminator_conflict"
        ):
            normalize_job_request(body)

    def test_creation_mode_fields_are_mutually_exclusive(self):
        body = valid_request(
            creation_mode="ai_auto",
            style_prompt="not allowed",
        )

        with self.assertRaisesRegex(ContractError, "creation_mode_conflict"):
            normalize_job_request(body)

    def test_contract_error_preserves_machine_fields_and_readable_message(self):
        error = ContractError("bad_value", "request.ratio", "ratio is invalid")

        self.assertEqual(error.error_code, "bad_value")
        self.assertEqual(error.field_path, "request.ratio")
        self.assertEqual(
            str(error),
            "bad_value at request.ratio: ratio is invalid",
        )

    def test_video_and_audio_ratio_rules_are_strict(self):
        invalid = (
            valid_request(ratio="16:9"),
            matrix_request("existing_audio", "ai_auto") | {"ratio": "auto"},
            matrix_request("uploaded_audio", "ai_auto") | {"ratio": "1:1"},
        )

        for body in invalid:
            with self.subTest(body=body):
                with self.assertRaisesRegex(ContractError, "ratio_invalid"):
                    normalize_job_request(body)

    def test_client_authority_and_render_fields_are_rejected(self):
        forbidden = (
            "authoritative_text",
            "cos_key",
            "model",
            "renderer",
            "render_component",
            "output_path",
            "template_version",
            "template_published",
            "template_ratios",
        )

        for field in forbidden:
            with self.subTest(field=field):
                with self.assertRaisesRegex(
                    ContractError,
                    "request_authority_field_forbidden",
                ):
                    normalize_job_request(valid_request(**{field: None}))

    def test_unknown_root_and_nested_tts_fields_are_rejected(self):
        with self.assertRaisesRegex(
            ContractError,
            "request_unknown_field",
        ):
            normalize_job_request(valid_request(surprise=True))

        body = matrix_request("script_to_audio_video", "ai_auto")
        body["tts_input"]["provider_voice"] = "forbidden"
        with self.assertRaisesRegex(
            ContractError,
            "request_unknown_field",
        ):
            normalize_job_request(body)

    def test_non_string_root_nested_and_material_values_raise_contract_errors(
        self,
    ):
        bodies = []
        root_key = valid_request()
        root_key[1] = "forbidden"
        bodies.append(root_key)
        nested_key = matrix_request("script_to_audio_video", "ai_auto")
        nested_key["tts_input"][1] = "forbidden"
        bodies.append(nested_key)
        bodies.append(valid_request(material_asset_ids=[{"id": "image_01"}]))

        for body in bodies:
            with self.subTest(body=body):
                with self.assertRaises(ContractError):
                    normalize_job_request(body)

    def test_tts_requires_nonempty_text_and_voice(self):
        cases = (
            {"voice_id": "voice_123"},
            {"text": "准确文本"},
            {"text": "", "voice_id": "voice_123"},
            {"text": "准确文本", "voice_id": ""},
        )
        for tts_input in cases:
            with self.subTest(tts_input=tts_input):
                body = matrix_request("script_to_audio_video", "ai_auto")
                body["tts_input"] = tts_input
                with self.assertRaisesRegex(
                    ContractError,
                    "tts_input_invalid",
                ):
                    normalize_job_request(body)

    def test_unpublished_template_reference_marker_is_rejected(self):
        body = matrix_request("existing_audio", "template_reference")
        body["template_id"] = "draft:tpl_product"

        with self.assertRaisesRegex(
            ContractError,
            "template_reference_unpublished",
        ):
            normalize_job_request(body)

    def test_style_prompt_must_have_between_one_and_one_thousand_characters(self):
        for style_prompt in ("", " " * 4, "x" * 1001):
            with self.subTest(length=len(style_prompt)):
                body = matrix_request("uploaded_video", "style_prompt")
                body["style_prompt"] = style_prompt
                with self.assertRaisesRegex(
                    ContractError,
                    "style_prompt_invalid",
                ):
                    normalize_job_request(body)

    def test_material_ids_are_unique_and_limited_to_ten(self):
        invalid_lists = (
            ["image_01", "image_01"],
            [f"image_{index:02d}" for index in range(11)],
        )
        for material_ids in invalid_lists:
            with self.subTest(material_ids=material_ids):
                with self.assertRaisesRegex(
                    ContractError,
                    "material_asset_ids_invalid",
                ):
                    normalize_job_request(
                        valid_request(material_asset_ids=material_ids)
                    )

    def test_control_characters_are_rejected_recursively(self):
        bodies = (
            valid_request(source_upload_id="upload\n123"),
            matrix_request("uploaded_video", "style_prompt")
            | {"style_prompt": "hidden\u0000instruction"},
            matrix_request("script_to_audio_video", "ai_auto")
            | {
                "tts_input": {
                    "text": "unsafe\u001ftext",
                    "voice_id": "voice_123",
                }
            },
        )
        for body in bodies:
            with self.subTest(body=body):
                with self.assertRaisesRegex(
                    ContractError,
                    "control_character_forbidden",
                ):
                    normalize_job_request(body)

    def test_lone_surrogates_are_rejected_as_contract_errors(self):
        body = matrix_request("uploaded_video", "style_prompt")
        body["style_prompt"] = "unsafe\ud800text"

        with self.assertRaisesRegex(
            ContractError,
            "unicode_scalar_invalid",
        ):
            normalize_job_request(body)


class CanonicalRequestTests(unittest.TestCase):
    def test_canonical_json_is_compact_sorted_utf8_without_ascii_escaping(self):
        self.assertEqual(
            canonical_json({"z": 1, "a": "汉"}),
            '{"a":"汉","z":1}'.encode("utf-8"),
        )

    def test_request_fingerprint_is_lowercase_sha256_of_canonical_json(self):
        self.assertEqual(
            request_fingerprint({"z": 1, "a": "汉"}),
            "21cb284025d6c31264c451f5183f6ef24b9951dc2f18db237c1192ba90ffbd90",
        )

    def test_canonical_json_rejects_nonfinite_numbers_and_control_characters(
        self,
    ):
        for value in (
            {"value": float("nan")},
            {"value": float("inf")},
            {"value": float("-inf")},
            {"value": "unsafe\u0000text"},
        ):
            with self.subTest(value=value):
                with self.assertRaises(ContractError):
                    canonical_json(value)

    def test_canonical_json_and_fingerprint_reject_lone_surrogates_stably(self):
        value = {"text": "unsafe\ud800text"}
        for function in (canonical_json, request_fingerprint):
            with self.subTest(function=function.__name__):
                with self.assertRaisesRegex(
                    ContractError,
                    "unicode_scalar_invalid",
                ):
                    function(value)


class StateContractTests(unittest.TestCase):
    def test_frozen_state_sets_and_transitions_match_the_phase_a_contract(self):
        self.assertEqual(
            TERMINAL_STATES,
            frozenset({"completed", "refunded", "prehold_absent"}),
        )
        self.assertEqual(
            RECONCILIATION_STATES,
            frozenset(
                {
                    "billing_reconciling",
                    "failed_reconciliation_pending",
                    "asset_decision_reconciling",
                    "failed_asset_decision_pending",
                }
            ),
        )
        self.assertEqual(
            MEDIA_STATES,
            (
                "queued",
                "generating_voice",
                "normalizing",
                "transcribing",
                "aligning",
                "planning",
                "resolving_materials",
                "generating_images",
                "generating_audio",
                "mixing_audio",
                "compiling",
                "rendering",
                "quality_checking",
                "repair_planning",
                "staging_delivery",
            ),
        )
        self.assertEqual(
            ALLOWED_TRANSITIONS,
            {
                "created_draft": {"preholding"},
                "preholding": {
                    "queued",
                    "prehold_absent",
                    "billing_reconciling",
                },
                "queued": {"generating_voice", "failed"},
                "generating_voice": {"normalizing", "failed"},
                "normalizing": {"transcribing", "failed"},
                "transcribing": {"aligning", "failed"},
                "aligning": {"planning", "failed"},
                "planning": {"resolving_materials", "failed"},
                "resolving_materials": {"generating_images", "failed"},
                "generating_images": {"generating_audio", "failed"},
                "generating_audio": {"mixing_audio", "failed"},
                "mixing_audio": {"compiling", "failed"},
                "compiling": {"rendering", "failed"},
                "rendering": {"quality_checking", "failed"},
                "quality_checking": {
                    "repair_planning",
                    "staging_delivery",
                    "failed",
                },
                "repair_planning": {"compiling", "failed"},
                "staging_delivery": {"settling", "failed"},
                "settling": {"publishing", "billing_reconciling"},
                "publishing": {
                    "completed",
                    "failed",
                    "asset_decision_reconciling",
                },
                "asset_decision_reconciling": {
                    "completed",
                    "failed",
                    "publishing",
                    "failed_asset_decision_pending",
                },
                "failed_asset_decision_pending": {"completed", "failed"},
                "failed": {"refund_pending"},
                "refund_pending": {"refunded", "billing_reconciling"},
                "billing_reconciling": {
                    "queued",
                    "prehold_absent",
                    "publishing",
                    "settling",
                    "refunded",
                    "refund_pending",
                    "failed_reconciliation_pending",
                },
                "failed_reconciliation_pending": {
                    "prehold_absent",
                    "refund_pending",
                    "refunded",
                },
                "completed": set(),
                "refunded": set(),
                "prehold_absent": set(),
            },
        )

    def test_terminal_states_cannot_reopen_and_reconciliation_is_nonterminal(self):
        for state in TERMINAL_STATES:
            self.assertEqual(ALLOWED_TRANSITIONS[state], set())
        self.assertTrue(RECONCILIATION_STATES.isdisjoint(TERMINAL_STATES))
        for state in RECONCILIATION_STATES:
            self.assertTrue(ALLOWED_TRANSITIONS[state])


if __name__ == "__main__":
    unittest.main()
