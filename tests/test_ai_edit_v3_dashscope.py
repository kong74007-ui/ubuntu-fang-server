from __future__ import annotations

import time
import unittest
from types import SimpleNamespace

from server.content_domains.ai_edit_v3.providers.base import (
    ProviderResult,
    SecretValue,
    SubmissionUnknown,
)
from server.content_domains.ai_edit_v3.providers.dashscope import (
    DashScopeConfigurationError,
    DashScopeMultimodalClient,
)


class FakeHttp:
    def __init__(self, response=None, error=None) -> None:
        self.requests: list[SimpleNamespace] = []
        self.response = response or {
            "request_id": "qwen-1",
            "output": {"choices": [{"message": {"content": [{"text": "{}"}]}}]},
            "usage": {"input_tokens": 10, "output_tokens": 2},
        }
        self.error = error

    def post(self, *, url: str, json: dict[str, object], headers: dict[str, str], deadline_at: float):
        self.requests.append(SimpleNamespace(url=url, json=json, headers=headers, deadline_at=deadline_at))
        if self.error is not None:
            raise self.error
        return self.response


class BodySentError(RuntimeError):
    body_sent = True


class DashScopeContractTests(unittest.TestCase):
    def test_client_freezes_workspace_endpoint_and_model(self) -> None:
        http = FakeHttp()
        client = DashScopeMultimodalClient(
            api_key=SecretValue("test-only"), workspace_id="ws-123", http=http,
        )

        result = client.generate_plan(
            {"input": "safe-test"},
            purpose="initial",
            idempotency_key="director-1",
            deadline_at=time.time() + 5,
        )

        sent = http.requests[0]
        self.assertEqual(sent.json["model"], "qwen3.7-max-2026-06-08")
        self.assertTrue(sent.json["parameters"]["enable_thinking"])
        self.assertEqual(
            sent.url,
            "https://ws-123.cn-beijing.maas.aliyuncs.com/api/v1/services/"
            "aigc/multimodal-generation/generation",
        )
        self.assertEqual(repr(SecretValue("secret")), "SecretValue([REDACTED])")
        self.assertIsInstance(result, ProviderResult)
        self.assertEqual(result.request_id, "qwen-1")

    def test_workspace_validation_is_exact_and_no_endpoint_override_exists(self) -> None:
        valid = ("a", "ws-123", "a" * 63, "0")
        invalid = ("-ws", "ws-", "ws_1", "ws.example", "WS", "中文", "a" * 64, "ws:443", "ws/path")
        for workspace in valid:
            with self.subTest(valid=workspace):
                DashScopeMultimodalClient(api_key=SecretValue("x"), workspace_id=workspace, http=FakeHttp())
        for workspace in invalid:
            with self.subTest(invalid=workspace):
                with self.assertRaisesRegex(DashScopeConfigurationError, "workspace_id_invalid"):
                    DashScopeMultimodalClient(api_key=SecretValue("x"), workspace_id=workspace, http=FakeHttp())
        with self.assertRaises(TypeError):
            DashScopeMultimodalClient(
                api_key=SecretValue("x"), workspace_id="ws", http=FakeHttp(), endpoint="https://evil"
            )

    def test_preflight_and_image_analysis_use_same_endpoint_without_user_data(self) -> None:
        http = FakeHttp()
        client = DashScopeMultimodalClient(api_key=SecretValue("x"), workspace_id="ws", http=http)

        capability = client.preflight(deadline_at=time.time() + 5)
        analysis = client.analyze_images(
            {"images": [{"asset_id": "img-1", "descriptor": "green package"}]},
            idempotency_key="material-1",
            deadline_at=time.time() + 5,
        )

        self.assertTrue(capability.available)
        self.assertIsInstance(analysis, ProviderResult)
        self.assertEqual(http.requests[0].url, http.requests[1].url)
        self.assertNotIn("green package", repr(http.requests[0].json))

    def test_request_size_deadline_and_unknown_submission_fail_closed(self) -> None:
        client = DashScopeMultimodalClient(api_key=SecretValue("x"), workspace_id="ws", http=FakeHttp())
        with self.assertRaisesRegex(ValueError, "provider_request_too_large"):
            client.generate_plan(
                {"input": "x" * 600_000}, purpose="initial", idempotency_key="large", deadline_at=time.time() + 5
            )
        with self.assertRaisesRegex(TimeoutError, "provider_deadline_exceeded"):
            client.generate_plan(
                {}, purpose="initial", idempotency_key="late", deadline_at=time.time() - 1
            )
        unknown = DashScopeMultimodalClient(
            api_key=SecretValue("super-secret"), workspace_id="ws", http=FakeHttp(error=BodySentError("leak"))
        )
        with self.assertRaisesRegex(SubmissionUnknown, "dashscope_submission_unknown") as raised:
            unknown.generate_plan(
                {"transcript": "private words"}, purpose="repair", idempotency_key="unknown", deadline_at=time.time() + 5
            )
        self.assertNotIn("super-secret", str(raised.exception))
        self.assertNotIn("private words", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
