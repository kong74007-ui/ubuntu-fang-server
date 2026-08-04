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
        self.assertEqual(request_body["response_format"], {"type": "json_object"})
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


if __name__ == "__main__":
    unittest.main()
