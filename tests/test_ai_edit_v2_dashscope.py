import json
import os
import urllib.error
import unittest
from pathlib import Path
from unittest.mock import patch

from server.content_domains.ai_edit_v2_providers.base import UnknownSubmissionError
from server.content_domains.ai_edit_v2_providers.dashscope import DashScopeClient


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "ai_edit_v2" / "provider_responses" / "fun_asr_success.json"
QWEN_FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "ai_edit_v2"
    / "provider_responses"
    / "qwen_edit_plan_success.json"
)


class RecordedDashScope:
    def __init__(self, transcript):
        self.transcript = transcript
        self.requests = []

    def __call__(self, method, url, headers, body, timeout):
        self.requests.append((method, url, headers, body, timeout))
        if method == "POST":
            return {
                "request_id": "submit-request",
                "output": {"task_id": "asr-task-1", "task_status": "PENDING"},
            }
        if url.endswith("/tasks/asr-task-1"):
            return {
                "request_id": "query-request",
                "output": {
                    "task_id": "asr-task-1",
                    "task_status": "SUCCEEDED",
                    "results": [
                        {
                            "subtask_status": "SUCCEEDED",
                            "transcription_url": "https://result.example.invalid/asr-task-1.json",
                        }
                    ],
                },
            }
        return self.transcript


class DashScopeClientTests(unittest.TestCase):
    def test_qwen_director_reuses_dashscope_transport_and_normalizes_message(self):
        response = json.loads(QWEN_FIXTURE_PATH.read_text(encoding="utf-8"))
        requests = []

        def recorded(method, url, headers, body, timeout):
            requests.append((method, url, headers, body, timeout))
            return response

        with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "test-dashscope-key"}, clear=False):
            result = DashScopeClient(http_request=recorded, clock_ms=lambda: 100).generate_edit_plan(
                "system constraints", "safe context"
            )

        request_body = json.loads(requests[0][3].decode("utf-8"))
        self.assertEqual(result.provider, "dashscope")
        self.assertEqual(result.capability, "director")
        self.assertEqual(result.request_id, "qwen-request-1")
        self.assertTrue(result.payload["content"].startswith('{"version":"2.0"'))
        self.assertEqual(request_body["model"], "qwen-plus")
        self.assertEqual(
            request_body["input"]["messages"],
            [
                {"role": "system", "content": "system constraints"},
                {"role": "user", "content": "safe context"},
            ],
        )

    def test_submit_and_query_normalize_fun_asr_words_and_sentences(self):
        transcript = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        recorded = RecordedDashScope(transcript)
        with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "test-dashscope-key"}, clear=False):
            client = DashScopeClient(http_request=recorded, clock_ms=lambda: 100)
            submitted = client.submit_asr("https://media.example.invalid/source.mp4", "job-17")
            result = client.query_asr(submitted.payload["provider_task_id"])

        self.assertEqual(submitted.provider, "dashscope")
        self.assertEqual(submitted.payload, {
            "provider_task_id": "asr-task-1",
            "reference": "job-17",
            "status": "pending",
        })
        self.assertEqual(result.request_id, "query-request")
        self.assertEqual(result.payload["words"], [
            {"text": "品牌", "start_ms": 0, "end_ms": 300},
            {"text": "价格", "start_ms": 300, "end_ms": 600},
            {"text": "是", "start_ms": 600, "end_ms": 900},
            {"text": "29", "start_ms": 900, "end_ms": 1350},
            {"text": "元", "start_ms": 1350, "end_ms": 1800},
        ])
        self.assertEqual(result.payload["sentences"], [
            {"text": "品牌价格是29元", "start_ms": 0, "end_ms": 1800}
        ])
        self.assertEqual(recorded.requests[0][2]["Authorization"], "Bearer test-dashscope-key")
        self.assertEqual(recorded.requests[2][2], {})

    def test_submit_timeout_is_unknown_so_pipeline_can_reconcile_before_retrying(self):
        def timeout_request(method, url, headers, body, timeout):
            raise TimeoutError("submission timed out")

        with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "test-dashscope-key"}, clear=False):
            client = DashScopeClient(http_request=timeout_request)
            with self.assertRaises(UnknownSubmissionError):
                client.submit_asr("https://media.example.invalid/source.mp4", "job-17")

    def test_submit_network_or_server_failure_is_unknown_not_retryable(self):
        failures = (
            urllib.error.URLError("connection reset after send"),
            urllib.error.HTTPError(
                "https://dashscope.aliyuncs.com/api/v1/services/audio/asr/transcription",
                502,
                "bad gateway after accept",
                {},
                None,
            ),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__), patch.dict(
                os.environ, {"DASHSCOPE_API_KEY": "test-dashscope-key"}, clear=False
            ):
                client = DashScopeClient(
                    http_request=lambda method, url, headers, body, timeout: (_ for _ in ()).throw(failure)
                )
                with self.assertRaises(UnknownSubmissionError):
                    client.submit_asr("https://media.example.invalid/source.mp4", "job-17")


if __name__ == "__main__":
    unittest.main()
