from __future__ import annotations

import unittest

from server.content_domains.ai_edit_v3.director_candidates import (
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
        MaterialDescriptor("material_02", ("产品",), "context", "portrait", ("9:16",), (), "b" * 64),
        MaterialDescriptor("material_01", ("门店",), "context", "portrait", ("9:16",), (), "a" * 64),
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
