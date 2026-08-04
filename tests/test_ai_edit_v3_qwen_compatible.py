from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from server.content_domains.ai_edit_v3.providers.qwen_compatible import (
    DashScopeCompatibleQwenClient,
)


class DashScopeCompatibleQwenClientTests(unittest.TestCase):
    def test_uses_exact_v3_model_on_openai_compatible_endpoint(self):
        requests = []

        def recorded(method, url, headers, body, timeout):
            requests.append((method, url, headers, body, timeout))
            return {
                "id": "chatcmpl-v3-1",
                "choices": [{
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": '{"creative_concept":"先结论后方法"}',
                    },
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 21, "completion_tokens": 8, "total_tokens": 29},
            }

        with patch.dict(
            os.environ,
            {
                "DASHSCOPE_API_KEY": "test-key",
                "DASHSCOPE_QWEN_MODEL": "qwen3.7-max-2026-06-08",
            },
            clear=False,
        ):
            result = DashScopeCompatibleQwenClient(
                http_request=recorded, clock_ms=lambda: 100
            ).generate_edit_plan("system constraints", "safe context")

        request = requests[0]
        request_body = json.loads(request[3].decode("utf-8"))
        self.assertEqual(
            request[1],
            "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        )
        self.assertEqual(request[2]["Authorization"], "Bearer test-key")
        self.assertEqual(request_body["model"], "qwen3.7-max-2026-06-08")
        self.assertNotIn("response_format", request_body)
        self.assertEqual(
            request_body["messages"],
            [
                {"role": "system", "content": "system constraints"},
                {"role": "user", "content": "safe context"},
            ],
        )
        self.assertEqual(result.request_id, "chatcmpl-v3-1")
        self.assertEqual(result.cost_units, 29)
        self.assertEqual(
            result.payload["content"], '{"creative_concept":"先结论后方法"}'
        )

    def test_per_call_timeout_can_only_reduce_the_configured_timeout(self):
        timeouts = []

        def recorded(method, url, headers, body, timeout):
            timeouts.append(timeout)
            return {
                "id": "chatcmpl-v3-timeout",
                "choices": [{"message": {"content": "{}"}}],
                "usage": {},
            }

        client = DashScopeCompatibleQwenClient(
            http_request=recorded,
            timeout_seconds=120,
        )
        with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "test-key"}, clear=False):
            client.generate_edit_plan("system", "user", timeout_seconds=37)
            client.generate_edit_plan("system", "user", timeout_seconds=240)

        self.assertEqual(timeouts, [37, 120])

    def test_new_director_decision_method_requests_strict_json_without_changing_legacy(self):
        bodies = []
        def recorded(method, url, headers, body, timeout):
            bodies.append(json.loads(body.decode("utf-8")))
            return {"id": "decision-1", "choices": [{"message": {"content": "{}"}}], "usage": {}}
        client = DashScopeCompatibleQwenClient(http_request=recorded)
        with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "test-key"}, clear=False):
            client.generate_director_decision("system", "user")
        self.assertEqual({"type": "json_object"}, bodies[0]["response_format"])

    def test_material_review_uses_multimodal_image_and_strict_json(self):
        bodies = []

        def recorded(method, url, headers, body, timeout):
            bodies.append(json.loads(body.decode("utf-8")))
            return {
                "id": "material-review-1",
                "choices": [{"message": {"content": '{"result":"pass","reason":"ok","evidence":[]}'}}],
                "usage": {},
            }

        client = DashScopeCompatibleQwenClient(http_request=recorded)
        with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "test-key"}, clear=False):
            result = client.inspect_image(
                {
                    "image_url": "https://private.example/image.png?q-signature=secret",
                    "semantic": "abstract airflow diagram",
                    "forbidden_subjects": ["person", "face"],
                    "source_metadata": {"sha256": "a" * 64},
                    "output_contract": "material-review-v1",
                },
                deadline_at=10_000_000_000.0,
            )

        body = bodies[0]
        self.assertEqual({"type": "json_object"}, body["response_format"])
        content = body["messages"][1]["content"]
        self.assertEqual("image_url", content[1]["type"])
        self.assertEqual(
            "https://private.example/image.png?q-signature=secret",
            content[1]["image_url"]["url"],
        )
        self.assertNotIn("https://", json.dumps(result.payload, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
