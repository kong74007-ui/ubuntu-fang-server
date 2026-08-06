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
                "choices": [{"message": {"content": '{"result":"pass","reason":"ok","evidence":[{"semantic_match":true,"forbidden_subjects":[]}]}'}}],
                "usage": {},
            }

        client = DashScopeCompatibleQwenClient(http_request=recorded)
        with patch.dict(os.environ, {
            "DASHSCOPE_API_KEY": "test-key",
            "DASHSCOPE_QWEN_MODEL": "qwen3.7-max",
        }, clear=False):
            os.environ.pop("DASHSCOPE_QWEN_VL_MODEL", None)
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
        self.assertEqual("qwen3.7-max-2026-06-08", body["model"])
        self.assertEqual({"type": "json_object"}, body["response_format"])
        system_prompt = body["messages"][0]["content"]
        content = body["messages"][1]["content"]
        self.assertEqual("image_url", content[1]["type"])
        self.assertEqual(
            "https://private.example/image.png?q-signature=secret",
            content[1]["image_url"]["url"],
        )
        review_request = json.loads(content[0]["text"])
        self.assertEqual("material-review-v1", review_request["contract_version"])
        self.assertEqual(
            "abstract airflow diagram",
            review_request["requested_semantic"],
        )
        self.assertEqual(
            ["person", "face"],
            review_request["forbidden_subjects_to_detect"],
        )
        self.assertNotIn("output_contract", review_request)
        self.assertNotIn("forbidden_subjects", review_request)
        self.assertIn("exact root keys result, reason, and evidence", system_prompt)
        self.assertIn("actually visible in the image", system_prompt)
        self.assertIn("must be [] when none are detected", system_prompt)
        self.assertIn("Never copy the candidate list", system_prompt)
        self.assertIn("generic or non-branded", system_prompt)
        self.assertIn("identifiable branded product or real store", system_prompt)
        self.assertIn(
            '"semantic_match":true,"forbidden_subjects":[]',
            system_prompt,
        )
        self.assertNotIn(
            '"forbidden_subjects":["person","face"]',
            system_prompt,
        )
        self.assertNotIn("https://", json.dumps(result.payload, ensure_ascii=False))

    def test_material_descriptor_batch_uses_only_aliases_pixels_and_inline_jpegs(self):
        bodies = []

        def recorded(method, url, headers, body, timeout):
            bodies.append(json.loads(body.decode("utf-8")))
            return {
                "id": "material-descriptor-1",
                "choices": [{"message": {"content": json.dumps({
                    "descriptors": [
                        {
                            "upload_alias": "upload_01",
                            "semantic": "绿色产品包装正面实拍",
                            "subject_type": "product",
                            "composition": "centered close-up",
                            "supported_ratios": ["9:16", "1:1"],
                            "risk_labels": [],
                        },
                        {
                            "upload_alias": "upload_02",
                            "semantic": "蓝色门店前台实拍",
                            "subject_type": "store",
                            "composition": "wide interior",
                            "supported_ratios": ["16:9", "9:16"],
                            "risk_labels": [],
                        },
                    ],
                }, ensure_ascii=False)}}],
                "usage": {},
            }

        client = DashScopeCompatibleQwenClient(http_request=recorded)
        with patch.dict(os.environ, {
            "DASHSCOPE_API_KEY": "test-key",
            "DASHSCOPE_QWEN_MODEL": "qwen3.7-max",
        }, clear=False):
            os.environ.pop("DASHSCOPE_QWEN_VL_MODEL", None)
            result = client.describe_images(
                {
                    "images": [
                        {
                            "upload_alias": "upload_01",
                            "width": 512,
                            "height": 384,
                            "data_url": "data:image/jpeg;base64,/9j/AA==",
                        },
                        {
                            "upload_alias": "upload_02",
                            "width": 384,
                            "height": 512,
                            "data_url": "data:image/jpeg;base64,/9j/BB==",
                        },
                    ],
                    "output_contract": "material-descriptors-v1",
                },
                deadline_at=10_000_000_000.0,
            )

        body = bodies[0]
        self.assertEqual("qwen3.7-max-2026-06-08", body["model"])
        self.assertEqual({"type": "json_object"}, body["response_format"])
        system_prompt = body["messages"][0]["content"]
        self.assertIn("only allowed top-level key is descriptors", system_prompt)
        self.assertIn("Never return the key output_contract", system_prompt)
        content = body["messages"][1]["content"]
        self.assertEqual(["text", "image_url", "image_url"], [item["type"] for item in content])
        prompt = content[0]["text"]
        self.assertIn("upload_01", prompt)
        self.assertIn("upload_02", prompt)
        self.assertNotIn("data:image", prompt)
        self.assertNotIn("material-real", prompt)
        self.assertNotIn('"output_contract"', prompt)
        self.assertIn('"descriptors"', prompt)
        self.assertIn("only top-level key", prompt)
        self.assertIn("one to three unique values", prompt)
        self.assertEqual(
            ["data:image/jpeg;base64,/9j/AA==", "data:image/jpeg;base64,/9j/BB=="],
            [item["image_url"]["url"] for item in content[1:]],
        )
        self.assertNotIn("data:image", json.dumps(result.payload, ensure_ascii=False))

    def test_vision_calls_honor_only_explicit_vl_model_override(self):
        bodies = []

        def recorded(method, url, headers, body, timeout):
            bodies.append(json.loads(body.decode("utf-8")))
            return {
                "id": "vision-model-override",
                "choices": [{"message": {"content": "{}"}}],
                "usage": {},
            }

        client = DashScopeCompatibleQwenClient(http_request=recorded)
        with patch.dict(os.environ, {
            "DASHSCOPE_API_KEY": "test-key",
            "DASHSCOPE_QWEN_MODEL": "generic-text-model",
            "DASHSCOPE_QWEN_VL_MODEL": "explicit-vl-model",
        }, clear=False):
            client.inspect_image(
                {
                    "image_url": "https://private.example/image.png",
                    "semantic": "product image",
                    "forbidden_subjects": [],
                    "source_metadata": {},
                    "output_contract": "material-review-v1",
                },
                deadline_at=10_000_000_000.0,
            )
            client.describe_images(
                {
                    "images": [{
                        "upload_alias": "upload_01",
                        "width": 32,
                        "height": 32,
                        "data_url": "data:image/jpeg;base64,/9j/AA==",
                    }],
                    "output_contract": "material-descriptors-v1",
                },
                deadline_at=10_000_000_000.0,
            )

        self.assertEqual(
            ["explicit-vl-model", "explicit-vl-model"],
            [body["model"] for body in bodies],
        )


if __name__ == "__main__":
    unittest.main()
