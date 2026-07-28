import unittest

from server.content_domains import ai_edit_v2_alignment as alignment
from server.content_domains import ai_edit_v2_asr as asr
from server.content_domains.ai_edit_v2_providers.base import ProviderResult, UnknownSubmissionError


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
    def test_platform_text_uses_original_words_with_asr_times(self):
        asr_result = {
            "words": timed_chars("品牌价格是29元"),
            "sentences": [{"text": "品牌价格是29元", "start_ms": 0, "end_ms": 1080}],
        }

        result = alignment.build_text_timeline("platform_video", "品牌价格是29元", asr_result)

        self.assertEqual("".join(word["text"] for word in result["words"]), "品牌价格是29元")
        self.assertTrue(
            all(
                left["start_ms"] <= left["end_ms"] <= right["end_ms"]
                for left, right in zip(result["words"], result["words"][1:])
            )
        )

    def test_platform_sentences_never_expose_conflicting_asr_words_or_numbers(self):
        original = "官方价格29元，今天截止。"
        asr_result = {
            "words": timed_chars("官方价格二十九元今天截止"),
            "sentences": [
                {"text": "官方价格二十九元", "start_ms": 0, "end_ms": 900},
                {"text": "今天截止", "start_ms": 900, "end_ms": 1800},
            ],
        }

        result = alignment.build_text_timeline("platform_video", original, asr_result)

        rendered_sentences = "".join(sentence["text"] for sentence in result["sentences"])
        self.assertEqual(rendered_sentences, original)
        self.assertNotIn("二十九", rendered_sentences)
        self.assertNotIn("官方价格二十九元", str(result))

    def test_external_cleanup_rejects_word_change(self):
        changed_meaning_fixture = {
            "words": timed_chars("你好世界"),
            "sentences": [{"text": "你好世界", "start_ms": 0, "end_ms": 720}],
            "cleaned_text": "您好，世界！",
        }

        with self.assertRaises(alignment.AlignmentError) as caught:
            alignment.build_text_timeline("external_video", None, changed_meaning_fixture)

        self.assertEqual(caught.exception.code, "external_text_changed")

    def test_external_cleanup_rejects_injected_raw_and_cleaned_text(self):
        asr_result = {
            "words": timed_chars("你好世界"),
            "sentences": [{"text": "你好世界", "start_ms": 0, "end_ms": 720}],
            "raw_text": "您好朋友",
            "cleaned_text": "您好，朋友！",
        }

        with self.assertRaises(alignment.AlignmentError) as caught:
            alignment.build_text_timeline("external_video", None, asr_result)

        self.assertEqual(caught.exception.code, "external_text_changed")

    def test_external_timeline_rejects_empty_or_backward_words(self):
        invalid_result = {
            "words": [
                {"text": "你好", "start_ms": 100, "end_ms": 200},
                {"text": "", "start_ms": 50, "end_ms": 90},
            ],
            "sentences": [{"text": "你好", "start_ms": 0, "end_ms": 200}],
        }

        with self.assertRaises(alignment.AlignmentError) as caught:
            alignment.build_text_timeline("external_video", None, invalid_result)

        self.assertEqual(caught.exception.code, "asr_timeline_invalid")

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
    def test_unknown_submission_is_persisted_and_restart_never_resubmits(self):
        events = []
        case = self

        class UnknownClient:
            def submit_asr(self, cos_url, reference):
                case.assertEqual(events, [("intent", "job-17:transcribing:1")])
                raise UnknownSubmissionError("connection lost after send")

        client = UnknownClient()
        with self.assertRaises(UnknownSubmissionError):
            asr.transcribe(
                "https://media.example.invalid/source.mp4", client, deadline_at=100,
                reference="job-17:transcribing:1",
                save_submission_intent=lambda reference: events.append(("intent", reference)),
                mark_submission_unknown=lambda reference: events.append(("unknown", reference)),
                now_fn=lambda: 1, sleep_fn=lambda _: None,
            )
        self.assertEqual(events, [
            ("intent", "job-17:transcribing:1"),
            ("unknown", "job-17:transcribing:1"),
        ])

        with self.assertRaises(UnknownSubmissionError):
            asr.transcribe(
                "https://media.example.invalid/source.mp4", client, deadline_at=100,
                reference="job-17:transcribing:1",
                submission_intent={"reference": "job-17:transcribing:1", "status": "unknown"},
                now_fn=lambda: 1, sleep_fn=lambda _: None,
            )

    def test_dashscope_task_id_is_saved_before_the_first_poll(self):
        saved_task_ids = []

        class ProviderClient:
            def submit_asr(self, cos_url, reference):
                return ProviderResult(
                    provider="dashscope", capability="asr", request_id="submit-1",
                    payload={"provider_task_id": "task-new", "reference": reference, "status": "pending"},
                    cost_units=0, elapsed_ms=1,
                )

            def query_asr(self, task_id):
                if saved_task_ids != ["task-new"]:
                    raise AssertionError("provider task was not persisted before polling")
                return ProviderResult(
                    provider="dashscope", capability="asr", request_id="query-1",
                    payload={
                        "provider_task_id": task_id,
                        "status": "succeeded",
                        "language": "zh-CN",
                        "duration_ms": 360,
                        "sentences": [{"start_ms": 0, "end_ms": 360, "text": "黄雀"}],
                        "words": timed_chars("黄雀"),
                    },
                    cost_units=0, elapsed_ms=1,
                )

        result = asr.transcribe(
            "https://media.example.invalid/source.mp4",
            ProviderClient(),
            deadline_at=100,
            reference="job-17",
            save_provider_task_id=saved_task_ids.append,
            now_fn=lambda: 1,
            sleep_fn=lambda _: None,
        )

        self.assertEqual(saved_task_ids, ["task-new"])
        self.assertEqual(result["provider_task_id"], "task-new")

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
