from __future__ import annotations

import hashlib
import unittest
from types import SimpleNamespace

from server.content_domains.ai_edit_v3.providers.asr import (
    AsrSentence,
    AsrWord,
    NormalizedTranscript,
)
from server.content_domains.ai_edit_v3.transcript import (
    MIN_ALIGNMENT_COVERAGE,
    TranscriptError,
    align_authoritative_text,
    build_text_timeline,
    normalize_external_punctuation,
    validate_punctuation_only,
)


class TranscriptContractTests(unittest.TestCase):
    def test_external_cleanup_may_change_punctuation_but_not_price(self) -> None:
        cleaned = normalize_external_punctuation("价格是298元今天下单")
        validate_punctuation_only("价格是298元今天下单", cleaned)
        self.assertEqual(cleaned, "价格是298元，今天下单。")
        with self.assertRaisesRegex(TranscriptError, "external_text_changed"):
            validate_punctuation_only("价格是298元", "价格是299元。")

    def test_external_cleanup_rejects_any_factual_change(self) -> None:
        source = "这款产品不含糖今天下单"
        for changed in (
            "这款产品含糖，今天下单。",
            "这款产品不含糖，今天立刻下单。",
            "今天下单，这款产品不含糖。",
            "这款商品不含糖，今天下单。",
        ):
            with self.subTest(changed=changed):
                with self.assertRaisesRegex(TranscriptError, "external_text_changed"):
                    validate_punctuation_only(source, changed)

    def test_authoritative_alignment_keeps_brand_and_price(self) -> None:
        words = (
            AsrWord("果", 0, 100, 0.99),
            AsrWord("燃", 100, 200, 0.85),
            AsrWord("畅", 200, 300, 0.99),
            AsrWord("通", 300, 400, 0.99),
            AsrWord("价格", 400, 600, 0.99),
            AsrWord("二百九十八", 600, 900, 0.95),
            AsrWord("元", 900, 1000, 0.99),
        )

        result = align_authoritative_text("果然畅通价格298元", words)

        self.assertGreaterEqual(result.coverage, MIN_ALIGNMENT_COVERAGE)
        self.assertEqual("".join(word.text for word in result.words), "果然畅通价格298元")
        self.assertTrue(result.monotonic)

    def test_alignment_rejects_low_coverage_and_non_monotonic_words(self) -> None:
        with self.assertRaisesRegex(TranscriptError, "alignment_low_coverage"):
            align_authoritative_text(
                "果然畅通价格298元",
                (AsrWord("完全无关", 0, 500, None),),
            )
        with self.assertRaisesRegex(TranscriptError, "alignment_timeline_invalid"):
            align_authoritative_text(
                "你好",
                (
                    AsrWord("你", 100, 200, None),
                    AsrWord("好", 50, 250, None),
                ),
            )

    def test_platform_timeline_uses_original_text_and_millisecond_captions(self) -> None:
        source = SimpleNamespace(
            input_type="platform_talking_head",
            authoritative_text="果然畅通价格298元。今天下单！",
            media=SimpleNamespace(duration_ms=1600),
        )
        asr = NormalizedTranscript(
            language="zh-CN",
            duration_ms=1620,
            words=(
                AsrWord("果", 0, 100, None),
                AsrWord("燃", 100, 200, None),
                AsrWord("畅", 200, 300, None),
                AsrWord("通", 300, 400, None),
                AsrWord("价格", 400, 600, None),
                AsrWord("二百九十八", 600, 900, None),
                AsrWord("元", 900, 1000, None),
                AsrWord("今天", 1000, 1200, None),
                AsrWord("下单", 1200, 1500, None),
            ),
            sentences=(
                AsrSentence("果燃畅通价格二百九十八元", 0, 1000),
                AsrSentence("今天下单", 1000, 1500),
            ),
            provider_task_id="asr-1",
            raw_text="果燃畅通价格二百九十八元今天下单",
        )

        timeline = build_text_timeline(source, asr)

        self.assertEqual(
            "".join(caption.text for caption in timeline.captions),
            source.authoritative_text,
        )
        self.assertEqual(
            timeline.authoritative_text_sha256,
            hashlib.sha256(source.authoritative_text.encode("utf-8")).hexdigest(),
        )
        self.assertEqual(timeline.duration_ms, 1600)
        self.assertTrue(all(isinstance(item.start_ms, int) for item in timeline.captions))
        self.assertTrue(all(item.start_ms <= item.end_ms for item in timeline.captions))
        self.assertTrue(all(segment.text for segment in timeline.source_segments))

    def test_external_timeline_only_changes_punctuation(self) -> None:
        source = SimpleNamespace(
            input_type="uploaded_video",
            authoritative_text=None,
            media=SimpleNamespace(duration_ms=800),
        )
        asr = NormalizedTranscript(
            language="zh-CN",
            duration_ms=800,
            words=(
                AsrWord("价格是298元", 0, 500, None),
                AsrWord("今天下单", 500, 800, None),
            ),
            sentences=(AsrSentence("价格是298元今天下单", 0, 800),),
            provider_task_id=None,
            raw_text="价格是298元今天下单",
        )

        timeline = build_text_timeline(source, asr)

        text = "".join(caption.text for caption in timeline.captions)
        self.assertEqual(text, "价格是298元，今天下单。")
        validate_punctuation_only(asr.raw_text, text)
        self.assertIsNone(timeline.authoritative_text_sha256)


if __name__ == "__main__":
    unittest.main()
