import unittest

from server.content_domains import ai_edit_v2_alignment as alignment
from server.content_domains import ai_edit_v2_asr as asr


def timed_chars(text, step=180):
    return [
        {
            "start_ms": index * step,
            "end_ms": (index + 1) * step,
            "text": char,
            "confidence": 0.98,
        }
        for index, char in enumerate(text)
    ]


class AlignmentTests(unittest.TestCase):
    def test_original_brand_text_wins_over_asr_homophone_with_monotonic_timing(self):
        result = alignment.align_platform_text(
            "黄雀引擎2", timed_chars("黄鹊引擎二")
        )

        self.assertEqual("".join(word["text"] for word in result["aligned_words"]), "黄雀引擎2")
        self.assertGreaterEqual(result["coverage"], 0.85)
        self.assertTrue(result["monotonic"])
        self.assertTrue(
            all(
                left["start_ms"] <= left["end_ms"] <= right["end_ms"]
                for left, right in zip(result["aligned_words"], result["aligned_words"][1:])
            )
        )

    def test_original_numbers_and_prices_are_never_replaced_by_asr(self):
        original = "现在价格499元、赠送1000积分马上领取"
        recognized = "现在价格四百九十九元赠送100积分马上领取"

        result = alignment.align_platform_text(original, timed_chars(recognized))
        rendered = "".join(word["text"] for word in result["aligned_words"])

        self.assertEqual(rendered, original)
        self.assertIn("499元", rendered)
        self.assertIn("1000积分", rendered)
        self.assertGreaterEqual(result["coverage"], 0.85)

    def test_low_coverage_or_backward_asr_timestamps_fail_closed(self):
        cases = (
            ("黄雀传媒招商方案", timed_chars("今天天气非常不错")),
            (
                "黄雀传媒",
                [
                    {"start_ms": 100, "end_ms": 200, "text": "黄", "confidence": 1},
                    {"start_ms": 50, "end_ms": 90, "text": "雀", "confidence": 1},
                    *timed_chars("传媒")[2:],
                ],
            ),
        )

        for original, words in cases:
            with self.subTest(original=original), self.assertRaises(alignment.AlignmentError) as caught:
                alignment.align_platform_text(original, words)
            self.assertEqual(caught.exception.code, "alignment_low_coverage")

    def test_external_transcript_may_change_only_punctuation_and_breaks(self):
        self.assertEqual(
            alignment.validate_punctuation_only("你好世界", "你好，世界！"),
            "你好，世界！",
        )
        with self.assertRaisesRegex(ValueError, "不得改变正文"):
            alignment.validate_punctuation_only("你好世界", "您好，世界！")


class FakeAsrClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.submissions = []
        self.queries = []

    def submit(self, cos_key):
        self.submissions.append(cos_key)
        return "task-new"

    def get(self, task_id):
        self.queries.append(task_id)
        return self.responses.pop(0)


class Clock:
    def __init__(self, values):
        self.values = iter(values)

    def __call__(self):
        return next(self.values)


class AsrTests(unittest.TestCase):
    def test_existing_provider_task_is_polled_without_resubmission(self):
        client = FakeAsrClient(
            [
                {"status": "running"},
                {
                    "status": "succeeded",
                    "result": {
                        "language": "zh-CN",
                        "duration_ms": 360,
                        "sentences": [{"start_ms": 0, "end_ms": 360, "text": "黄雀"}],
                        "words": timed_chars("黄雀"),
                    },
                },
            ]
        )

        result = asr.transcribe(
            "ai-edit-v2/owner/task/source.mp4",
            client,
            deadline_at=100,
            provider_task_id="task-existing",
            now_fn=Clock([1, 2]),
            sleep_fn=lambda _: None,
        )

        self.assertEqual(client.submissions, [])
        self.assertEqual(client.queries, ["task-existing", "task-existing"])
        self.assertEqual(result["words"][0]["text"], "黄")

    def test_new_transcription_submits_once_and_returns_frozen_shape(self):
        client = FakeAsrClient(
            [
                {
                    "status": "succeeded",
                    "result": {
                        "language": "zh-CN",
                        "duration_ms": 360,
                        "sentences": [{"start_ms": 0, "end_ms": 360, "text": "黄雀"}],
                        "words": timed_chars("黄雀"),
                    },
                }
            ]
        )

        result = asr.transcribe(
            "ai-edit-v2/owner/task/source.mp4",
            client,
            deadline_at=100,
            now_fn=lambda: 1,
            sleep_fn=lambda _: None,
        )

        self.assertEqual(client.submissions, ["ai-edit-v2/owner/task/source.mp4"])
        self.assertEqual(
            set(result), {"language", "duration_ms", "sentences", "words", "provider_task_id"}
        )
        self.assertEqual(result["provider_task_id"], "task-new")

    def test_timeout_and_provider_failure_have_stable_codes(self):
        timeout_client = FakeAsrClient([{"status": "running"}])
        with self.assertRaises(asr.AsrError) as timeout:
            asr.transcribe(
                "source",
                timeout_client,
                deadline_at=5,
                provider_task_id="task",
                now_fn=lambda: 5,
                sleep_fn=lambda _: None,
            )
        self.assertEqual(timeout.exception.code, "asr_timeout")

        failed_client = FakeAsrClient([{"status": "failed", "error": "provider detail"}])
        with self.assertRaises(asr.AsrError) as failed:
            asr.transcribe(
                "source",
                failed_client,
                deadline_at=10,
                provider_task_id="task",
                now_fn=lambda: 1,
                sleep_fn=lambda _: None,
            )
        self.assertEqual(failed.exception.code, "asr_provider_failed")

    def test_result_with_backward_word_start_is_rejected(self):
        client = FakeAsrClient(
            [
                {
                    "status": "succeeded",
                    "result": {
                        "language": "zh-CN",
                        "duration_ms": 300,
                        "sentences": [{"start_ms": 0, "end_ms": 300, "text": "黄雀"}],
                        "words": [
                            {"start_ms": 100, "end_ms": 180, "text": "黄"},
                            {"start_ms": 50, "end_ms": 240, "text": "雀"},
                        ],
                    },
                }
            ]
        )

        with self.assertRaises(asr.AsrError) as caught:
            asr.transcribe(
                "source",
                client,
                deadline_at=10,
                provider_task_id="task",
                now_fn=lambda: 1,
                sleep_fn=lambda _: None,
            )

        self.assertEqual(caught.exception.code, "asr_result_invalid")


if __name__ == "__main__":
    unittest.main()
