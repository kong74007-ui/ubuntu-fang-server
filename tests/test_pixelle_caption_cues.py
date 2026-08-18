from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TALKING_PATCH_PATH = (
    ROOT
    / "deploy"
    / "pixelle-video"
    / "patches"
    / "0011-render-talking-material-scenes.patch"
)
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

    def test_accepts_more_than_twenty_cues_for_long_scenes(self):
        cues = self.assert_lossless_and_single_line("一" * 300)
        self.assertEqual(len(cues), 22)

    def test_greedily_packs_short_english_and_cjk_fragments(self):
        for text in (
            "a " * 21,
            "一，" * 21,
            "一。" * 21,
            " ".join(["word"] * 50),
        ):
            with self.subTest(text=text):
                cues = self.assert_lossless_and_single_line(text)
                self.assertLessEqual(len(cues), 20)

    def test_one_hundred_cue_boundary_is_enforced_after_packing(self):
        accepted = self.module.split_caption_text("一，" * 700)
        self.assertEqual(len(accepted), 100)
        with self.assertRaises(ValueError):
            self.module.split_caption_text("一，" * 701)

    def test_nested_cue_width_contract_handles_cjk_and_ascii_boundaries(self):
        self.assertEqual("一" * 14, self.module.validate_caption_cue_text("一" * 14))
        self.assertEqual("a" * 28, self.module.validate_caption_cue_text("a" * 28))
        for text in ("一" * 15, "a" * 29):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.module.validate_caption_cue_text(text)


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

    def test_continuous_audio_is_shared_across_display_cues(self):
        cues = [{"text": "第一句，"}, {"text": "第二句更长一些。"}]

        timed = self.module.build_proportional_caption_timeline(cues, 4.5)

        self.assertEqual(0.0, timed[0]["start_time"])
        self.assertEqual(4.5, timed[-1]["end_time"])
        self.assertAlmostEqual(4.5, sum(cue["duration"] for cue in timed))
        self.assertGreater(timed[1]["duration"], timed[0]["duration"])

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

    def test_reports_padding_when_later_cue_would_start_after_video_eof(self):
        timed = self.module.build_caption_timeline(
            [{"text": "first"}, {"text": "second"}],
            [6.0, 5.0],
        )
        self.assertGreater(timed[1]["start_time"], 5.0)
        self.assertEqual(
            6.0,
            self.module.required_video_padding(
                5.0,
                self.module.caption_timeline_duration(timed),
            ),
        )


class TalkingMaterialPatchContractTests(unittest.TestCase):
    def test_talking_path_reuses_existing_cue_audio_without_second_tts(self):
        patch = TALKING_PATCH_PATH.read_text(encoding="utf-8")

        self.assertIn("build_talking_windows(", patch)
        self.assertIn("[cue.audio_path for cue in window_cues]", patch)
        self.assertIn("concat_audios(", patch)
        self.assertIn("await talking_client.generate(", patch)
        self.assertNotIn("_generate_talking_audio", patch)
        self.assertNotIn("tts(**talking", patch)

    def test_ordinary_visual_is_generated_before_optional_talking_replacement(self):
        patch = TALKING_PATCH_PATH.read_text(encoding="utf-8")

        self.assertIn("+            await self._prepare_talking_visuals(", patch)
        self.assertNotIn("-                await self._step_generate_media", patch)
        self.assertIn("except TalkingClipError as error:", patch)
        self.assertIn('frame.visual_source = "ordinary"', patch)
        self.assertIn("frame.talking_warning", patch)

    def test_talking_visuals_follow_existing_cue_timeline_and_parent_audio(self):
        patch = TALKING_PATCH_PATH.read_text(encoding="utf-8")

        self.assertIn("cue.start_time - window_start_time", patch)
        self.assertIn("cue.duration", patch)
        self.assertIn("frame.talking_cue_video_paths", patch)
        added_lines = "\n".join(
            line for line in patch.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        )
        self.assertNotIn("cue_frame.audio_path =", added_lines)
        self.assertNotIn("cue_frame.audio_path = talking_video", patch)

    def test_talking_windows_check_cancellation_and_clean_temporary_artifacts(self):
        patch = TALKING_PATCH_PATH.read_text(encoding="utf-8")

        self.assertIn("asyncio.current_task()", patch)
        self.assertIn("task.cancelling()", patch)
        self.assertIn("except asyncio.CancelledError:", patch)
        self.assertIn("temporary_paths", patch)
        self.assertIn("unlink(missing_ok=True)", patch)
        self.assertNotIn("MIN_TALKING_WINDOW", patch)
        self.assertNotIn("MAX_TALKING_WINDOW", patch)

    def test_talking_setup_failure_falls_back_and_single_cue_restores_ordinary_media(self):
        patch = TALKING_PATCH_PATH.read_text(encoding="utf-8")

        self.assertIn('frame.talking_warning = "talking_client_unavailable"', patch)
        self.assertIn("frame.talking_original_video_path = frame.video_path", patch)
        self.assertIn("frame.video_path = frame.talking_original_video_path", patch)
        self.assertIn("frame.media_type = frame.talking_original_media_type", patch)


if __name__ == "__main__":
    unittest.main()
