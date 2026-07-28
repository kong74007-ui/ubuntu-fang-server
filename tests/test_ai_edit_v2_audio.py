import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from server.content_domains.ai_edit_v2_audio import (
    AudioError,
    build_audio_plan,
    generate_audio_assets,
    mix_audio,
)
from server.content_domains.ai_edit_v2_providers.base import ProviderError, ProviderResult


EDIT_PLAN = {
    "duration_ms": 8_000,
    "scenes": [
        {
            "id": "scene_1", "start_ms": 0, "end_ms": 2_000,
            "intent": "介绍问题", "headline": "开场", "transition": "none",
        },
        {
            "id": "scene_2", "start_ms": 2_000, "end_ms": 4_000,
            "intent": "重点解释价格", "headline": "重点", "transition": "cut",
        },
        {
            "id": "scene_3", "start_ms": 2_200, "end_ms": 6_000,
            "intent": "展示产品", "headline": "产品", "transition": "cut",
        },
        {
            "id": "scene_4", "start_ms": 6_000, "end_ms": 8_000,
            "intent": "结论", "headline": "结论", "transition": "fade",
        },
    ],
    "audio_plan": {
        "speech_policy": "preserve_source",
        "music_policy": "duck_under_speech",
        "sfx_policy": "semantic_only",
    },
}
TEXT_TIMELINE = {
    "duration_ms": 8_000,
    "words": [
        {"text": "品牌", "start_ms": 1_850, "end_ms": 2_050},
        {"text": "价格", "start_ms": 2_050, "end_ms": 2_400},
        {"text": "29元", "start_ms": 2_400, "end_ms": 2_800},
    ],
    "protected_ranges": [
        {"type": "brand", "start_ms": 1_850, "end_ms": 2_050},
        {"type": "price", "start_ms": 2_050, "end_ms": 2_800},
    ],
}


class AudioPlanTests(unittest.TestCase):
    def test_sfx_are_semantic_only_merged_under_300ms_and_avoid_protected_speech(self):
        plan = build_audio_plan(EDIT_PLAN, TEXT_TIMELINE)

        self.assertTrue(plan["bgm"]["force_instrumental"])
        self.assertTrue(all(cue["kind"] in {
            "semantic_turn", "camera_cut", "emphasis"
        } for cue in plan["sfx"]))
        self.assertTrue(all(
            right["at_ms"] - left["at_ms"] >= 300
            for left, right in zip(plan["sfx"], plan["sfx"][1:])
        ))
        self.assertFalse(any(
            cue["at_ms"] < protected["end_ms"]
            and cue["at_ms"] + cue["duration_ms"] > protected["start_ms"]
            for cue in plan["sfx"]
            for protected in TEXT_TIMELINE["protected_ranges"]
        ))
        self.assertEqual([cue["at_ms"] for cue in plan["sfx"]], [6_000])

    def test_number_words_are_automatically_protected_even_without_annotations(self):
        timeline = {
            "duration_ms": 8_000,
            "words": [{"text": "只要29元", "start_ms": 5_900, "end_ms": 6_200}],
        }
        plan = build_audio_plan(EDIT_PLAN, timeline)
        self.assertFalse(any(cue["at_ms"] == 6_000 for cue in plan["sfx"]))

    def test_none_policy_creates_no_generated_audio(self):
        edit_plan = {**EDIT_PLAN, "audio_plan": {
            "speech_policy": "preserve_source", "music_policy": "none", "sfx_policy": "none"
        }}
        plan = build_audio_plan(edit_plan, TEXT_TIMELINE)
        self.assertIsNone(plan["bgm"])
        self.assertEqual(plan["sfx"], [])


class FakeProvider:
    def __init__(self, music_error=None, failed_sfx=None):
        self.music_error = music_error
        self.failed_sfx = set(failed_sfx or ())

    def generate_music(self, prompt, duration_ms, idempotency_key):
        if self.music_error:
            raise self.music_error
        return ProviderResult("elevenlabs", "music", "m1", {"cos_key": "private/music.mp3"}, 1, 2)

    def generate_sfx(self, prompt, duration_ms, idempotency_key):
        if idempotency_key in self.failed_sfx:
            raise ProviderError("sfx failed")
        return ProviderResult("elevenlabs", "sfx", "s1", {"cos_key": idempotency_key + ".mp3"}, 1, 2)


class DegradationTests(unittest.TestCase):
    def test_bgm_and_individual_nonrequired_sfx_failures_degrade_explicitly(self):
        plan = {
            "bgm": {"prompt": "calm", "duration_ms": 1_000},
            "sfx": [
                {"prompt": "soft click", "duration_ms": 300, "required": False},
                {"prompt": "page turn", "duration_ms": 400, "required": False},
            ],
        }
        provider = FakeProvider(
            music_error=ProviderError("music failed"),
            failed_sfx={"job:sfx:0"},
        )
        result = generate_audio_assets("job", plan, provider)

        self.assertIsNone(result["bgm"])
        self.assertEqual(len(result["sfx"]), 1)
        self.assertEqual(
            result["degradations"],
            ["music_generation_degraded", "sfx_generation_degraded"],
        )

    def test_required_sfx_failure_is_not_silently_degraded(self):
        plan = {
            "bgm": None,
            "sfx": [{"prompt": "required", "duration_ms": 300, "required": True}],
        }
        with self.assertRaises(ProviderError):
            generate_audio_assets("job", plan, FakeProvider(failed_sfx={"job:sfx:0"}))


class Result:
    def __init__(self, returncode=0, stderr=b""):
        self.returncode = returncode
        self.stdout = b""
        self.stderr = stderr


class MixRunner:
    def __init__(self, output_path, second_error=None):
        self.output_path = output_path
        self.second_error = second_error
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if len(self.calls) == 1:
            measurement = {
                "input_i": "-18.2", "input_tp": "-2.1", "input_lra": "5.4",
                "input_thresh": "-28.2", "target_offset": "0.3",
            }
            return Result(stderr=("[Parsed_loudnorm]\n" + json.dumps(measurement)).encode())
        if self.second_error:
            if isinstance(self.second_error, BaseException):
                raise self.second_error
            return Result(returncode=1, stderr=self.second_error)
        Path(self.output_path).write_bytes(b"mixed-audio")
        return Result()


class MixAudioTests(unittest.TestCase):
    def test_dialogue_ducking_and_two_pass_loudness_use_argument_lists(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            voice = os.path.join(temp_dir, "voice.wav")
            Path(voice).write_bytes(b"voice")
            output = os.path.join(temp_dir, "master.m4a")
            runner = MixRunner(output)

            result = mix_audio(
                "video.mp4", voice, "bgm.mp3",
                [{"path": "hit.mp3", "at_ms": 1_000}], output, runner,
            )

            self.assertEqual(result, output)
            self.assertEqual(len(runner.calls), 2)
            for command, kwargs in runner.calls:
                self.assertIsInstance(command, list)
                self.assertEqual(command[0], "ffmpeg")
                self.assertNotIn("shell", kwargs)
            first_filter = runner.calls[0][0][runner.calls[0][0].index("-filter_complex") + 1]
            second_filter = runner.calls[1][0][runner.calls[1][0].index("-filter_complex") + 1]
            self.assertIn("sidechaincompress", first_filter)
            self.assertIn("loudnorm=I=-16", first_filter)
            self.assertIn("print_format=json", first_filter)
            self.assertIn("measured_I=-18.2", second_filter)
            self.assertIn("TP=-1.5", second_filter)

    def test_missing_voice_timeout_clipping_and_empty_output_have_stable_codes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = os.path.join(temp_dir, "master.m4a")
            cases = (
                ("missing.wav", MixRunner(output), "audio_voice_missing"),
                (None, MixRunner(output, subprocess.TimeoutExpired("ffmpeg", 600)), "audio_mix_timeout"),
                (None, MixRunner(output, b"clipping detected"), "audio_mix_clipping"),
            )
            for missing, runner, code in cases:
                voice = missing or os.path.join(temp_dir, "voice.wav")
                if missing is None:
                    Path(voice).write_bytes(b"voice")
                with self.subTest(code=code), self.assertRaises(AudioError) as caught:
                    mix_audio("video.mp4", voice, None, [], output, runner)
                self.assertEqual(caught.exception.code, code)


if __name__ == "__main__":
    unittest.main()
