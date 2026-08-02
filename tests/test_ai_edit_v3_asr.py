from __future__ import annotations

import unittest

from server.content_domains.ai_edit_v3.providers.asr import (
    AsrResultError,
    AsrSentence,
    AsrWord,
    normalize_asr_result,
)


class AsrContractTests(unittest.TestCase):
    def test_normalizes_one_word(self) -> None:
        result = normalize_asr_result(
            {"words": [{"text": "你", "start_ms": 0, "end_ms": 100}]}
        )
        self.assertEqual(result.words[0], AsrWord("你", 0, 100, None))

    def test_rejects_empty_negative_overlapping_and_backward_timestamps(self) -> None:
        invalid_words = (
            [],
            [{"text": "", "start_ms": 0, "end_ms": 100}],
            [{"text": "你", "start_ms": -1, "end_ms": 100}],
            [{"text": "你", "start_ms": 0, "end_ms": 0}],
            [
                {"text": "你", "start_ms": 0, "end_ms": 100},
                {"text": "好", "start_ms": 99, "end_ms": 200},
            ],
            [{"text": "你", "start_ms": True, "end_ms": 100}],
        )
        for words in invalid_words:
            with self.subTest(words=words), self.assertRaisesRegex(
                AsrResultError, "asr_timeline_invalid"
            ):
                normalize_asr_result({"words": words})

    def test_normalizes_sentences_language_duration_and_confidence(self) -> None:
        result = normalize_asr_result(
            {
                "language": "zh-CN",
                "duration_ms": 200,
                "words": [
                    {"text": "你", "start_ms": 0, "end_ms": 100, "confidence": 0.9},
                    {"text": "好", "start_ms": 100, "end_ms": 200, "confidence": 1},
                ],
                "sentences": [{"text": "你好", "start_ms": 0, "end_ms": 200}],
            }
        )
        self.assertEqual(result.language, "zh-CN")
        self.assertEqual(result.duration_ms, 200)
        self.assertEqual(result.sentences, (AsrSentence("你好", 0, 200),))
        self.assertEqual(result.words[0].confidence, 0.9)


if __name__ == "__main__":
    unittest.main()
