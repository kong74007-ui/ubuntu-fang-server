# -*- coding: utf-8 -*-
import pathlib
import sys
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from content_domains import ali_asr


class AliAsrTests(unittest.TestCase):
    @mock.patch("content_domains.ali_asr.time.sleep")
    @mock.patch("content_domains.ali_asr._json_request")
    def test_async_transcription_returns_word_timestamps(self, request, sleep):
        request.side_effect = [
            {"output": {"task_id": "asr-1", "task_status": "PENDING"}},
            {"output": {"task_id": "asr-1", "task_status": "RUNNING"}},
            {
                "output": {
                    "task_id": "asr-1",
                    "task_status": "SUCCEEDED",
                    "results": [
                        {
                            "subtask_status": "SUCCEEDED",
                            "transcription_url": "https://result.example/asr.json",
                        }
                    ],
                }
            },
            {
                "transcripts": [
                    {
                        "text": "你好世界",
                        "sentences": [
                            {
                                "begin_time": 100,
                                "end_time": 900,
                                "text": "你好世界",
                                "words": [
                                    {
                                        "begin_time": 100,
                                        "end_time": 300,
                                        "text": "你",
                                    },
                                    {
                                        "begin_time": 300,
                                        "end_time": 500,
                                        "text": "好",
                                    },
                                ],
                            }
                        ],
                    }
                ]
            },
        ]
        heartbeat = mock.Mock()
        with mock.patch.object(ali_asr, "API_KEY", "configured-for-test"):
            result = ali_asr.transcribe(
                "https://cos.example/source.mp4", heartbeat=heartbeat, timeout=5
            )
        self.assertEqual("你好世界", result["text"])
        self.assertEqual(100, result["words"][0]["begin_time"])
        self.assertEqual("asr-1", result["provider_task_id"])
        self.assertEqual(900, result["duration_ms"])
        self.assertEqual([mock.call(2), mock.call(3)], sleep.call_args_list)
        self.assertEqual([mock.call("transcribing"), mock.call("transcribing")], heartbeat.call_args_list)

    def test_rejects_missing_key_and_non_https_url(self):
        with mock.patch.object(ali_asr, "API_KEY", ""):
            with self.assertRaisesRegex(RuntimeError, "DASHSCOPE_API_KEY"):
                ali_asr.transcribe("https://cos.example/source.mp4")
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            ali_asr.transcribe("http://example.com/source.mp4")

    @mock.patch("content_domains.ali_asr.time.sleep")
    @mock.patch("content_domains.ali_asr._json_request")
    def test_provider_failure_is_bounded_and_does_not_fetch_result(self, request, _sleep):
        request.side_effect = [
            {"output": {"task_id": "asr-2", "task_status": "PENDING"}},
            {
                "output": {
                    "task_id": "asr-2",
                    "task_status": "FAILED",
                    "message": "unsupported media",
                }
            },
        ]
        with mock.patch.object(ali_asr, "API_KEY", "configured-for-test"):
            with self.assertRaisesRegex(RuntimeError, "unsupported media"):
                ali_asr.transcribe("https://cos.example/source.mp4", timeout=5)
        self.assertEqual(2, request.call_count)

    @mock.patch("content_domains.ali_asr.urllib.request.urlopen")
    def test_http_helper_keeps_authorization_in_header(self, urlopen):
        response = mock.MagicMock()
        response.read.return_value = b'{"output":{"task_id":"asr-3"}}'
        response.__enter__.return_value = response
        urlopen.return_value = response
        result = ali_asr._json_request(
            "POST",
            ali_asr.POST_URL,
            {"model": "fun-asr"},
            {"Authorization": "Bearer configured-for-test"},
        )
        self.assertEqual("asr-3", result["output"]["task_id"])
        sent = urlopen.call_args.args[0]
        self.assertEqual("POST", sent.method)
        self.assertNotIn("configured-for-test", sent.full_url)


if __name__ == "__main__":
    unittest.main()
