from __future__ import annotations

import unittest
from types import SimpleNamespace

from server.content_domains.ai_edit_v3.source_map import (
    Pause,
    SourceMapError,
    build_candidate_segments,
    compile_keep_decisions,
    map_source_ms_to_output_ms,
)
from server.content_domains.ai_edit_v3.transcript import SourceSegment


class SourceMapContractTests(unittest.TestCase):
    def _timeline(self):
        return SimpleNamespace(
            source_segments=(
                SourceSegment("segment_01", 0, 1000, True, "品牌价格298元。"),
                SourceSegment("segment_02", 1000, 2000, False, "嗯，这是过渡。"),
                SourceSegment("segment_03", 2000, 3500, True, "今天下单。"),
            )
        )

    def test_keep_decisions_cannot_reorder_segments(self) -> None:
        with self.assertRaisesRegex(SourceMapError, "source_order_invalid"):
            compile_keep_decisions(self._timeline(), ["segment_03", "segment_01"])

    def test_keep_decisions_are_whole_unique_known_segments(self) -> None:
        for requested, code in (
            (["segment_01", "segment_01", "segment_03"], "source_segment_duplicate"),
            (["segment_01", "missing", "segment_03"], "source_segment_unknown"),
            (["segment_01"], "protected_segment_missing"),
        ):
            with self.subTest(requested=requested):
                with self.assertRaisesRegex(SourceMapError, code):
                    compile_keep_decisions(self._timeline(), requested)

    def test_selected_segments_have_contiguous_output_without_time_stretch(self) -> None:
        selected = compile_keep_decisions(
            self._timeline(), ["segment_01", "segment_03"]
        )

        self.assertEqual(
            [(item.output_start_ms, item.output_end_ms) for item in selected],
            [(0, 1000), (1000, 2500)],
        )
        self.assertEqual(map_source_ms_to_output_ms(selected, 0), 0)
        self.assertEqual(map_source_ms_to_output_ms(selected, 999), 999)
        self.assertEqual(map_source_ms_to_output_ms(selected, 2000), 1000)
        self.assertEqual(map_source_ms_to_output_ms(selected, 3500), 2500)
        with self.assertRaisesRegex(SourceMapError, "source_position_cut"):
            map_source_ms_to_output_ms(selected, 1500)

    def test_invalid_source_timeline_overlap_or_gap_is_rejected(self) -> None:
        for second_start in (900, 1100):
            timeline = SimpleNamespace(
                source_segments=(
                    SourceSegment("segment_01", 0, 1000, False, "第一句。"),
                    SourceSegment("segment_02", second_start, 2000, False, "第二句。"),
                )
            )
            with self.subTest(second_start=second_start):
                with self.assertRaisesRegex(SourceMapError, "source_timeline_invalid"):
                    compile_keep_decisions(timeline, ["segment_01", "segment_02"])

    def test_candidates_follow_sentence_boundaries_and_pauses(self) -> None:
        candidates = build_candidate_segments(
            self._timeline(), (Pause(950, 1050, 100), Pause(3400, 3500, 100))
        )

        self.assertEqual([item.id for item in candidates], ["segment_01", "segment_02", "segment_03"])
        self.assertEqual(candidates[0].pause_after_ms, 100)
        self.assertEqual(candidates[-1].pause_after_ms, 100)
        self.assertTrue(candidates[0].protected)

    def test_mapping_is_monotonic_for_fixed_cases(self) -> None:
        selected = compile_keep_decisions(
            self._timeline(), ["segment_01", "segment_02", "segment_03"]
        )
        cases = ((0, 0), (500, 500), (1000, 1000), (1999, 1999), (2000, 2000), (3500, 3500))
        observed = []
        for source_ms, expected in cases:
            with self.subTest(source_ms=source_ms):
                actual = map_source_ms_to_output_ms(selected, source_ms)
                self.assertEqual(actual, expected)
                observed.append(actual)
        self.assertEqual(observed, sorted(observed))


if __name__ == "__main__":
    unittest.main()
