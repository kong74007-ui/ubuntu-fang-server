from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = (
    ROOT
    / "deploy"
    / "pixelle-video"
    / "overrides"
    / "pixelle_video"
    / "services"
    / "caption_cues.py"
)


def load_module():
    spec = importlib.util.spec_from_file_location("pixelle_caption_cues_test", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class CaptionSplitterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_module()

    @classmethod
    def tearDownClass(cls):
        sys.modules.pop("pixelle_caption_cues_test", None)

    def assert_lossless_and_single_line(self, text: str):
        cues = self.module.split_caption_text(text)
        self.assertEqual(text, "".join(cues))
        self.assertTrue(cues)
        self.assertTrue(
            all(self.module.display_units(cue) <= self.module.MAX_CAPTION_UNITS for cue in cues),
            cues,
        )
        return cues

    def test_preserves_chinese_text_and_limits_display_width(self):
        text = "所以轩和堂做这件事，并不是为了追风口，是为了让门店效果可验证。"
        cues = self.assert_lossless_and_single_line(text)
        self.assertGreater(len(cues), 1)
        self.assertTrue(cues[0].endswith("，"), cues)

    def test_handles_unpunctuated_mixed_text(self):
        self.assert_lossless_and_single_line(
            "AI培训2026帮助门店建立standard workflow持续增长"
        )

    def test_keeps_short_caption_as_one_cue(self):
        self.assertEqual(["效果可验证。"], self.module.split_caption_text("效果可验证。"))

    def test_prefers_sentence_boundary_before_clause_boundary(self):
        text = "第一句话很短。第二句话也很短，但是后面还有补充。"
        cues = self.assert_lossless_and_single_line(text)
        self.assertEqual("第一句话很短。", cues[0])

    def test_rejects_invalid_width(self):
        with self.assertRaises(ValueError):
            self.module.split_caption_text("测试", max_units=0)

    def test_rejects_more_than_twenty_cues(self):
        with self.assertRaises(ValueError):
            self.module.split_caption_text("一" * 300)


class CaptionTimelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = load_module()

    @classmethod
    def tearDownClass(cls):
        sys.modules.pop("pixelle_caption_cues_test", None)

    def test_real_audio_durations_define_cue_boundaries(self):
        cues = [
            {"text": "第一句，", "audio_path": "first.mp3"},
            {"text": "第二句。", "audio_path": "second.mp3"},
        ]

        timed = self.module.build_caption_timeline(cues, [1.25, 2.5])

        self.assertEqual(
            [(cue["start_time"], cue["end_time"], cue["duration"]) for cue in timed],
            [(0.0, 1.25, 1.25), (1.25, 3.75, 2.5)],
        )
        self.assertEqual(3.75, self.module.caption_timeline_duration(timed))

    def test_video_slices_advance_instead_of_restarting(self):
        timed = self.module.build_caption_timeline(
            [
                {"text": "第一句，", "audio_path": "first.mp3"},
                {"text": "第二句。", "audio_path": "second.mp3"},
            ],
            [1.25, 2.5],
        )

        self.assertEqual(
            [(0.0, 1.25), (1.25, 2.5)],
            self.module.caption_video_slices(timed),
        )

    def test_rejects_missing_or_non_positive_real_duration(self):
        cues = [{"text": "第一句。", "audio_path": "first.mp3"}]
        for durations in ([], [0], [-1], [None]):
            with self.subTest(durations=durations), self.assertRaises(ValueError):
                self.module.build_caption_timeline(cues, durations)


if __name__ == "__main__":
    unittest.main()
