# -*- coding: utf-8 -*-
import json
import pathlib
import sys
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from content_domains import edit_planner


def transcript_fixture():
    return {
        "text": "大家好，今天介绍黄雀智能剪辑。",
        "sentences": [
            {"begin_time": 0, "end_time": 30_000, "text": "大家好，今天介绍黄雀智能剪辑。"}
        ],
        "words": [],
    }


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
        "captions": [],
        "overlays": [],
        "broll": [],
    }


class EditPlannerTests(unittest.TestCase):
    @mock.patch("content_domains.edit_planner._chat")
    def test_requests_json_mode_and_validates_result(self, chat):
        chat.return_value = valid_plan()
        plan = edit_planner.generate_plan(
            transcript_fixture(), "knowledge_dynamic", 30_000, []
        )
        request = chat.call_args.args[0]
        self.assertEqual({"type": "json_object"}, request["response_format"])
        self.assertNotIn("max_tokens", request)
        self.assertIn("JSON", request["messages"][0]["content"])
        self.assertEqual("1.0", plan["version"])

    @mock.patch("content_domains.edit_planner._chat")
    def test_repairs_invalid_plan_only_once(self, chat):
        chat.side_effect = [{"bad": True}, valid_plan()]
        edit_planner.generate_plan(
            transcript_fixture(), "knowledge_dynamic", 30_000, []
        )
        self.assertEqual(2, chat.call_count)
        repair_context = json.loads(chat.call_args_list[1].args[0]["messages"][1]["content"])
        self.assertEqual({"bad": True}, repair_context["invalid_plan"])
        self.assertTrue(repair_context["validation_error"])

    @mock.patch("content_domains.edit_planner._chat")
    def test_fails_after_second_invalid_plan(self, chat):
        chat.return_value = {"bad": True}
        with self.assertRaisesRegex(RuntimeError, "剪辑方案"):
            edit_planner.generate_plan(
                transcript_fixture(), "knowledge_dynamic", 30_000, []
            )
        self.assertEqual(2, chat.call_count)

    @mock.patch("content_domains.edit_planner._chat")
    def test_prompt_contains_only_allowed_material_projection(self, chat):
        plan = valid_plan()
        plan["broll"] = [
            {"asset_id": "mine", "start_ms": 1000, "end_ms": 2000}
        ]
        chat.return_value = plan
        edit_planner.generate_plan(
            transcript_fixture(),
            "product_story",
            30_000,
            [
                {
                    "id": "mine",
                    "kind": "image",
                    "role": "product",
                    "origin": "uploaded",
                    "description": "产品正面",
                    "signed_url": "https://cos.example/private?signature=secret",
                    "database_note": "must-not-leak",
                }
            ],
        )
        prompt = chat.call_args.args[0]["messages"][1]["content"]
        self.assertIn('"id": "mine"', prompt)
        self.assertNotIn("signature=secret", prompt)
        self.assertNotIn("must-not-leak", prompt)

    @mock.patch("content_domains.edit_planner.urllib.request.urlopen")
    def test_chat_parses_openai_compatible_content(self, urlopen):
        response = mock.MagicMock()
        response.read.return_value = json.dumps(
            {"choices": [{"message": {"content": json.dumps(valid_plan())}}]}
        ).encode("utf-8")
        response.__enter__.return_value = response
        urlopen.return_value = response
        with mock.patch.object(edit_planner, "API_KEY", "configured-for-test"):
            result = edit_planner._chat(
                {
                    "model": "qwen-plus",
                    "messages": [{"role": "system", "content": "JSON"}],
                    "response_format": {"type": "json_object"},
                }
            )
        self.assertEqual("1.0", result["version"])
        sent = json.loads(urlopen.call_args.args[0].data.decode("utf-8"))
        self.assertEqual({"type": "json_object"}, sent["response_format"])


if __name__ == "__main__":
    unittest.main()
