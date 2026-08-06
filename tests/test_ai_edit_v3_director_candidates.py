from __future__ import annotations

import unittest

from server.content_domains.ai_edit_v3.director_candidates import (
    _scene_duration_budget,
    _scene_rhythm_minimum,
    build_scene_candidates,
)
from server.content_domains.ai_edit_v3.materials import MaterialDescriptor
from server.content_domains.ai_edit_v3.transcript import (
    Caption,
    SourceSegment,
    TextTimeline,
)


def _timeline(*, duration_ms: int = 12_000, count: int = 6) -> TextTimeline:
    width = duration_ms // count
    captions = tuple(
        Caption(
            id=f"caption_{index:03d}",
            text=f"第{index}句。",
            start_ms=(index - 1) * width,
            end_ms=index * width if index < count else duration_ms,
        )
        for index in range(1, count + 1)
    )
    segments = tuple(
        SourceSegment(
            id=f"fact_{index:03d}",
            start_ms=caption.start_ms,
            end_ms=caption.end_ms,
            protected=index in {2, 5},
            text=caption.text,
        )
        for index, caption in enumerate(captions, 1)
    )
    return TextTimeline(duration_ms, captions, segments, "a" * 64, 1.0)


def _materials() -> tuple[MaterialDescriptor, ...]:
    return (
        MaterialDescriptor("material_02", "产品", "context", "portrait", ("9:16",), (), "b" * 64),
        MaterialDescriptor("material_01", "门店", "context", "portrait", ("9:16",), (), "a" * 64),
    )


class SceneCandidateTests(unittest.TestCase):
    def test_candidates_cover_timeline_without_overlap_at_caption_boundaries(self) -> None:
        timeline = _timeline()
        candidates = build_scene_candidates(
            timeline,
            _materials(),
            ratio="9:16",
            input_type="platform_talking_head",
        )

        self.assertGreaterEqual(len(candidates), 3)
        self.assertLessEqual(len(candidates), 12)
        self.assertEqual(0, candidates[0].start_ms)
        self.assertEqual(timeline.duration_ms, candidates[-1].end_ms)
        self.assertTrue(all(left.end_ms == right.start_ms for left, right in zip(candidates, candidates[1:])))
        boundaries = {0, timeline.duration_ms, *(caption.start_ms for caption in timeline.captions)}
        self.assertTrue(all(item.start_ms in boundaries and item.end_ms in boundaries for item in candidates))

    def test_candidate_metadata_is_authoritative_and_deterministic(self) -> None:
        timeline = _timeline()
        first = build_scene_candidates(
            timeline,
            _materials(),
            ratio="9:16",
            input_type="platform_talking_head",
        )
        second = build_scene_candidates(
            timeline,
            tuple(reversed(_materials())),
            ratio="9:16",
            input_type="platform_talking_head",
        )

        self.assertEqual(first, second)
        self.assertEqual(tuple(f"candidate_{index:02d}" for index in range(1, len(first) + 1)), tuple(item.id for item in first))
        for candidate in first:
            captions = [item for item in timeline.captions if item.id in candidate.caption_ids]
            self.assertEqual("".join(item.text for item in captions), candidate.authoritative_text)
            self.assertEqual(("material_01", "material_02"), candidate.available_material_ids)
            self.assertTrue(candidate.speaker_available)
        protected = {fact_id for item in first for fact_id in item.protected_fact_ids}
        self.assertEqual({"fact_002", "fact_005"}, protected)

    def test_audio_input_has_no_speaker_and_scene_count_is_capped(self) -> None:
        candidates = build_scene_candidates(
            _timeline(duration_ms=60_000, count=30),
            (),
            ratio="16:9",
            input_type="uploaded_audio",
            max_scenes=12,
        )

        self.assertLessEqual(len(candidates), 12)
        self.assertTrue(all(not item.speaker_available for item in candidates))

    def test_caller_scene_cap_below_three_overrides_the_rhythm_target(self) -> None:
        timeline = _timeline(duration_ms=18_000, count=6)

        candidates = build_scene_candidates(
            timeline,
            (),
            ratio="16:9",
            input_type="uploaded_audio",
            max_scenes=2,
        )

        self.assertLessEqual(len(candidates), 2)
        self.assertTrue(all(
            item.end_ms - item.start_ms >= 500 for item in candidates
        ))

    def test_sub_500ms_tail_is_merged_into_a_contract_valid_scene(self) -> None:
        timeline = TextTimeline(
            9_000,
            (
                Caption("caption_001", "Authoritative statement.", 0, 8_500),
                Caption("caption_002", "Short tail.", 8_600, 8_950),
            ),
            (
                SourceSegment(
                    "fact_001",
                    0,
                    9_000,
                    False,
                    "Authoritative statement. Short tail.",
                ),
            ),
            "a" * 64,
            1.0,
        )

        first = build_scene_candidates(
            timeline,
            (),
            ratio="9:16",
            input_type="uploaded_video",
        )
        second = build_scene_candidates(
            timeline,
            (),
            ratio="9:16",
            input_type="uploaded_video",
        )

        self.assertEqual(first, second)
        self.assertEqual([(0, 9_000)], [(item.start_ms, item.end_ms) for item in first])
        self.assertEqual(
            ("caption_001", "caption_002"),
            first[0].caption_ids,
        )
        self.assertTrue(
            all(item.end_ms - item.start_ms >= 500 for item in first)
        )

    def test_minimum_duration_is_part_of_the_shared_scene_budget(self) -> None:
        ranges = (
            (30, 784),
            (880, 3_937),
            (4_341, 12_779),
            (12_847, 21_283),
            (21_854, 22_009),
            (22_013, 22_139),
        )
        captions = tuple(
            Caption(
                f"caption_{index:03d}",
                f"Caption {index}",
                start_ms,
                end_ms,
            )
            for index, (start_ms, end_ms) in enumerate(ranges, 1)
        )
        timeline = TextTimeline(22_255, captions, (), "a" * 64, 1.0)
        raw_captions = [
            {
                "id": item.id,
                "text": item.text,
                "start_ms": item.start_ms,
                "end_ms": item.end_ms,
            }
            for item in captions
        ]

        budget = _scene_duration_budget(raw_captions, duration_ms=22_255)
        candidates = build_scene_candidates(
            timeline,
            (),
            ratio="9:16",
            input_type="uploaded_video",
        )
        spans = [item.end_ms - item.start_ms for item in candidates]

        self.assertEqual(9_408, budget)
        self.assertGreaterEqual(min(spans), 500)
        self.assertLessEqual(max(spans), budget)

    def test_impossible_three_scene_rhythm_degrades_to_two_valid_scenes(self) -> None:
        captions = (
            Caption("caption_001", "One", 0, 8_500),
            Caption("caption_002", "Two", 8_600, 13_500),
            Caption("caption_003", "Three", 13_600, 13_950),
        )
        raw_captions = [
            {
                "id": item.id,
                "text": item.text,
                "start_ms": item.start_ms,
                "end_ms": item.end_ms,
            }
            for item in captions
        ]
        timeline = TextTimeline(14_000, captions, (), "a" * 64, 1.0)

        self.assertEqual(
            1,
            _scene_rhythm_minimum(
                raw_captions,
                duration_ms=14_000,
                max_scenes=12,
            ),
        )
        candidates = build_scene_candidates(
            timeline,
            (),
            ratio="9:16",
            input_type="uploaded_video",
        )

        self.assertEqual(
            [(0, 8_600), (8_600, 14_000)],
            [(item.start_ms, item.end_ms) for item in candidates],
        )

    def test_rejects_materials_without_frozen_identity(self) -> None:
        with self.assertRaisesRegex(ValueError, "director_material_identity_invalid"):
            build_scene_candidates(
                _timeline(),
                ({"semantic": ["missing id"]},),
                ratio="9:16",
                input_type="uploaded_video",
            )


if __name__ == "__main__":
    unittest.main()
