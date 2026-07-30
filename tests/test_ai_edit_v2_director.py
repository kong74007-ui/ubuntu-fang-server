import copy
import json
import unittest

from server.content_domains.ai_edit_v2_director import DirectorError, generate_edit_plan
from server.content_domains.ai_edit_v2_providers.base import ProviderError, ProviderResult


VALID_PLAN = {
    "version": "2.0",
    "creation_mode": "natural_brief",
    "duration_ms": 1800,
    "target_duration_ms": 1800,
    "aspect_ratio": "16:9",
    "language": "zh-CN",
    "style_system": {"component_family": "editorial_business"},
    "scenes": [
        {
            "id": "scene_01",
            "start_ms": 0,
            "end_ms": 1800,
            "intent": "突出价格信息",
            "layout": "speaker_focus",
            "visual_type": "talking_head",
            "headline": "价格信息一目了然",
            "material_slots": [],
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

CONTEXT = {
    "creation_mode": "natural_brief",
    "text_timeline": {
        "source_type": "external_video",
        "text": "品牌价格是29元",
        "words": [
            {"text": "品牌", "start_ms": 0, "end_ms": 300},
            {"text": "价格", "start_ms": 300, "end_ms": 600},
            {"text": "是", "start_ms": 600, "end_ms": 900},
            {"text": "29", "start_ms": 900, "end_ms": 1350},
            {"text": "元", "start_ms": 1350, "end_ms": 1800},
        ],
        "sentences": [{"text": "品牌价格是29元", "start_ms": 0, "end_ms": 1800}],
    },
    "style_text": "清晰、克制的商业诊断风格",
    "aspect_ratio": "16:9",
    "target_duration_ms": 1800,
    "api_key": "must-not-leak",
}


class FakeQwen:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def generate_edit_plan(self, system_prompt, user_prompt):
        self.calls.append((system_prompt, user_prompt))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return ProviderResult(
            provider="dashscope",
            capability="director",
            request_id=f"request-{len(self.calls)}",
            payload={"content": response},
            cost_units=1,
            elapsed_ms=1,
        )


class DirectorTests(unittest.TestCase):
    def test_director_normalizes_only_safe_structural_fields_before_validation(self):
        structurally_wrong = copy.deepcopy(VALID_PLAN)
        structurally_wrong["style_system"] = "editorial_business"
        structurally_wrong["scenes"][0]["id"] = ""
        client = FakeQwen([json.dumps(structurally_wrong, ensure_ascii=False)])

        plan = generate_edit_plan(CONTEXT, client)

        self.assertEqual(plan["style_system"], {"component_family": "editorial_business"})
        self.assertEqual(plan["scenes"][0]["id"], "scene_01")

    def test_director_fills_blank_headline_from_scene_intent_without_retry(self):
        response = copy.deepcopy(VALID_PLAN)
        response["scenes"][0]["intent"] = "  解释价格构成  "
        response["scenes"][0]["headline"] = "   "
        client = FakeQwen([json.dumps(response, ensure_ascii=False)] * 3)

        plan = generate_edit_plan(CONTEXT, client)

        self.assertEqual(plan["scenes"][0]["headline"], "解释价格构成")
        self.assertEqual(len(client.calls), 1)

    def test_director_fills_missing_or_none_headline_from_scene_intent(self):
        for headline in (None, "missing"):
            with self.subTest(headline=headline):
                response = copy.deepcopy(VALID_PLAN)
                response["scenes"][0]["intent"] = "解释价格构成"
                if headline == "missing":
                    response["scenes"][0].pop("headline")
                else:
                    response["scenes"][0]["headline"] = headline
                client = FakeQwen([json.dumps(response, ensure_ascii=False)] * 3)

                plan = generate_edit_plan(CONTEXT, client)

                self.assertEqual(plan["scenes"][0]["headline"], "解释价格构成")
                self.assertEqual(len(client.calls), 1)

    def test_director_initial_request_contains_a_context_valid_output_example(self):
        class ExampleEchoQwen:
            def generate_edit_plan(self, _system_prompt, user_prompt):
                request = json.loads(user_prompt)
                return ProviderResult(
                    provider="dashscope",
                    capability="director",
                    request_id="example-echo",
                    payload={"content": json.dumps(request["output_example"], ensure_ascii=False)},
                    cost_units=1,
                    elapsed_ms=1,
                )

        plan = generate_edit_plan(CONTEXT, ExampleEchoQwen())

        self.assertEqual(plan["duration_ms"], 1800)
        self.assertEqual(plan["aspect_ratio"], "16:9")
        self.assertEqual(plan["scenes"][0]["id"], "scene_01")

    def test_final_schema_failure_exposes_only_a_safe_validation_detail(self):
        invalid = copy.deepcopy(VALID_PLAN)
        invalid["style_system"] = "unknown-family"
        client = FakeQwen([json.dumps(invalid, ensure_ascii=False)] * 3)

        with self.assertRaises(DirectorError) as caught:
            generate_edit_plan(CONTEXT, client)

        self.assertEqual(caught.exception.code, "director_schema_invalid")
        self.assertEqual(caught.exception.detail, "style_system必须是对象")

    def test_director_returns_semantic_plan_without_provider_or_render_fields(self):
        client = FakeQwen([json.dumps(VALID_PLAN, ensure_ascii=False)])

        plan = generate_edit_plan(CONTEXT, client)

        serialized = json.dumps(plan, ensure_ascii=False).lower()
        self.assertEqual(plan["version"], "2.0")
        for forbidden in ("tracks", "cos_key", "url", "shotstack", "code"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, serialized)
        self.assertNotIn("must-not-leak", client.calls[0][0] + client.calls[0][1])

    def test_director_system_prompt_forbids_transcript_rewrite_and_provider_fields(self):
        client = FakeQwen([json.dumps(VALID_PLAN, ensure_ascii=False)])

        generate_edit_plan(CONTEXT, client)

        system_prompt = client.calls[0][0]
        self.assertIn("不得改写字幕正文", system_prompt)
        for forbidden in ("COS", "URL", "Shotstack", "tracks", "代码", "数据库", "JavaScript"):
            with self.subTest(forbidden=forbidden):
                self.assertIn(forbidden, system_prompt)

    def test_director_whitelists_timeline_fields_before_sending_context(self):
        context = copy.deepcopy(CONTEXT)
        context["text_timeline"]["api_key"] = "nested-secret"
        context["text_timeline"]["words"][0]["provider_metadata"] = "nested-secret"
        client = FakeQwen([json.dumps(VALID_PLAN, ensure_ascii=False)])

        generate_edit_plan(context, client)

        sent_prompts = client.calls[0][0] + client.calls[0][1]
        self.assertNotIn("nested-secret", sent_prompts)
        self.assertNotIn("provider_metadata", sent_prompts)

    def test_director_repairs_caption_body_instead_of_accepting_rewrite(self):
        rewritten = copy.deepcopy(VALID_PLAN)
        rewritten["caption_plan"]["text"] = "品牌价格是39元"
        client = FakeQwen(
            [json.dumps(rewritten, ensure_ascii=False), json.dumps(VALID_PLAN, ensure_ascii=False)]
        )

        plan = generate_edit_plan(CONTEXT, client)

        self.assertEqual(plan, VALID_PLAN)
        self.assertEqual(len(client.calls), 2)
        repair_prompt = client.calls[1][1]
        self.assertIn("schema_errors", repair_prompt)
        self.assertIn("previous_response", repair_prompt)
        self.assertNotIn("must-not-leak", repair_prompt)

    def test_director_prompts_keep_the_exact_contract_and_original_request_during_repair(self):
        client = FakeQwen(["{}", json.dumps(VALID_PLAN, ensure_ascii=False)])

        plan = generate_edit_plan(CONTEXT, client)

        self.assertEqual(plan, VALID_PLAN)
        initial_request = json.loads(client.calls[0][1])
        contract = initial_request["output_contract"]
        self.assertEqual(contract["top_level_fields"], list(VALID_PLAN))
        self.assertEqual(contract["scene_fields"], list(VALID_PLAN["scenes"][0]))
        self.assertEqual(contract["caption_plan_fields"], ["source", "style"])
        self.assertEqual(
            contract["audio_plan_fields"],
            ["speech_policy", "music_policy", "sfx_policy"],
        )
        repair_request = json.loads(client.calls[1][1])
        self.assertEqual(repair_request["original_request"], initial_request)
        self.assertIn("schema_errors", repair_request)
        self.assertIn("previous_response", repair_request)

    def test_director_enforces_published_template_visual_and_sound_policy(self):
        context = {
            **CONTEXT,
            "creation_mode": "platform_template",
            "template_id": "business_diagnostic",
            "template_version": "1.0",
        }
        context.pop("style_text")
        correct = copy.deepcopy(VALID_PLAN)
        correct["creation_mode"] = "platform_template"
        correct["style_system"] = {
            "template_id": "business_diagnostic",
            "template_version": "1.0",
            "component_family": "editorial_business",
        }
        wrong = copy.deepcopy(correct)
        wrong["style_system"]["component_family"] = "documentary_modern"
        wrong["audio_plan"]["sfx_policy"] = "none"
        client = FakeQwen(
            [json.dumps(wrong, ensure_ascii=False), json.dumps(correct, ensure_ascii=False)]
        )

        plan = generate_edit_plan(context, client)

        self.assertEqual(plan, correct)
        self.assertEqual(len(client.calls), 2)

    def test_platform_template_rejects_style_only_missing_id_or_unknown_version(self):
        unknown_version = {
            **CONTEXT,
            "creation_mode": "platform_template",
            "template_id": "business_diagnostic",
            "template_version": "99.0",
        }
        unknown_version.pop("style_text")
        missing_version = {
            **CONTEXT,
            "creation_mode": "platform_template",
            "template_id": "business_diagnostic",
        }
        missing_version.pop("style_text")
        invalid_contexts = (
            {**CONTEXT, "creation_mode": "platform_template"},
            unknown_version,
            missing_version,
        )

        for context in invalid_contexts:
            with self.subTest(context=context), self.assertRaises(DirectorError) as caught:
                generate_edit_plan(context, FakeQwen([json.dumps(VALID_PLAN)]))
            self.assertEqual(caught.exception.code, "director_context_invalid")

    def test_repair_prompt_redacts_credentials_and_limits_previous_response(self):
        secret = "test-dashscope-key-should-never-repeat"
        invalid = json.dumps(
            {
                "api_key": secret,
                "access_token": "token-value-should-never-repeat",
                "padding": "x" * 20_000,
            }
        )
        client = FakeQwen([invalid, json.dumps(VALID_PLAN, ensure_ascii=False)])

        plan = generate_edit_plan(CONTEXT, client)

        self.assertEqual(plan, VALID_PLAN)
        repair_prompt = client.calls[1][1]
        self.assertNotIn(secret, repair_prompt)
        self.assertNotIn("token-value-should-never-repeat", repair_prompt)
        self.assertNotIn("api_key", repair_prompt)
        self.assertNotIn("access_token", repair_prompt)
        self.assertIn("[REDACTED]", repair_prompt)
        self.assertLessEqual(len(json.loads(repair_prompt)["previous_response"]), 8_000)

    def test_repair_prompt_redacts_malformed_assignments_and_bare_tokens(self):
        secrets = (
            "plain-secret-123",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signaturepart123",
            "eyJhbGciOiJIUzI1NiJ9.e30.signaturepart123",
            "AKIAIOSFODNN7EXAMPLE",
            "LTAI5tQexample12345678",
            "ghp_abcdefghijklmnopqrstuvwxyz123456",
            "xoxb-1234567890-abcdefghijklmnop",
        )
        invalid = (
            '{"api_key" : "plain-secret-123", '
            '"jwt":"eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signaturepart123", '
            '"short_jwt":"eyJhbGciOiJIUzI1NiJ9.e30.signaturepart123", '
            '"aws":"AKIAIOSFODNN7EXAMPLE", "aliyun":"LTAI5tQexample12345678", '
            '"github":"ghp_abcdefghijklmnopqrstuvwxyz123456", '
            '"slack":"xoxb-1234567890-abcdefghijklmnop", '
            '"note":"ordinary short text"'
        )
        client = FakeQwen([invalid, json.dumps(VALID_PLAN, ensure_ascii=False)])

        plan = generate_edit_plan(CONTEXT, client)

        self.assertEqual(plan, VALID_PLAN)
        repair_prompt = client.calls[1][1]
        for secret in secrets:
            with self.subTest(secret=secret):
                self.assertNotIn(secret, repair_prompt)
        self.assertIn("ordinary short text", repair_prompt)

    def test_repair_prompt_unclosed_credential_stops_before_ordinary_text(self):
        secret = "plain-secret-123"
        invalid = '{"api_key": "plain-secret-123 ordinary short text\nmore context'
        client = FakeQwen([invalid, json.dumps(VALID_PLAN, ensure_ascii=False)])

        plan = generate_edit_plan(CONTEXT, client)

        self.assertEqual(plan, VALID_PLAN)
        repair_prompt = client.calls[1][1]
        self.assertNotIn(secret, repair_prompt)
        self.assertIn("ordinary short text", repair_prompt)
        self.assertIn("more context", repair_prompt)

    def test_repair_prompt_redacts_punctuation_credentials_until_safe_delimiter(self):
        cases = (
            ("api_key=p@ssw0rd ordinary text", "[REDACTED_CREDENTIAL] ordinary text"),
            ('password="abcd$efgh", ordinary text', "[REDACTED_CREDENTIAL], ordinary text"),
            ("token='key!/%value') ordinary text", "[REDACTED_CREDENTIAL]) ordinary text"),
            (
                'secret="unclosed!/%value ordinary text\nmore context',
                "[REDACTED_CREDENTIAL] ordinary text\nmore context",
            ),
        )

        for invalid, expected in cases:
            with self.subTest(invalid=invalid):
                client = FakeQwen([invalid, json.dumps(VALID_PLAN, ensure_ascii=False)])

                plan = generate_edit_plan(CONTEXT, client)

                self.assertEqual(plan, VALID_PLAN)
                repair_prompt = json.loads(client.calls[1][1])
                self.assertEqual(repair_prompt["previous_response"], expected)

    def test_director_stops_after_two_schema_repairs(self):
        client = FakeQwen(["{}", "{}", "{}", json.dumps(VALID_PLAN)])

        with self.assertRaises(DirectorError) as caught:
            generate_edit_plan(CONTEXT, client, max_repairs=2)

        self.assertEqual(caught.exception.code, "director_schema_invalid")
        self.assertEqual(len(client.calls), 3)

    def test_each_repair_uses_only_the_immediately_previous_response(self):
        client = FakeQwen(["{}", None, json.dumps(VALID_PLAN, ensure_ascii=False)])

        plan = generate_edit_plan(CONTEXT, client)

        self.assertEqual(plan, VALID_PLAN)
        second_repair = json.loads(client.calls[2][1])
        self.assertEqual(second_repair["previous_response"], "")

    def test_director_maps_provider_failure_to_stable_error(self):
        client = FakeQwen([ProviderError("dashscope_director_unavailable")])

        with self.assertRaises(DirectorError) as caught:
            generate_edit_plan(CONTEXT, client)

        self.assertEqual(caught.exception.code, "director_provider_failed")


if __name__ == "__main__":
    unittest.main()
