from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
import wave

from server.content_domains.ai_edit_v3.audio import (
    AudioGenerationError,
    AudioPlanError,
    GeneratedAudioAsset,
    build_master_audio,
    compile_audio_plan,
    generate_task_audio,
)
from server.content_domains.ai_edit_v3.contracts import LeaseClaim
from server.content_domains.ai_edit_v3.providers.base import DefinitiveNotAccepted, ProviderResult
from server.content_domains.ai_edit_v3.runtime import StageContext
from server.content_domains.ai_edit_v3.transcript import Caption, SourceSegment, TextTimeline


def _tone(path: Path, *, duration_ms: int, frequency: float = 440.0, amplitude: float = 0.18) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rate = 48_000
    frames = round(rate * duration_ms / 1000)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        for index in range(frames):
            sample = int(32767 * amplitude * math.sin(2 * math.pi * frequency * index / rate))
            output.writeframesraw(sample.to_bytes(2, "little", signed=True))


def _timeline() -> TextTimeline:
    return TextTimeline(
        duration_ms=4_000,
        captions=(
            Caption("caption_01", "品牌产品只要99元", 0, 1_500),
            Caption("caption_02", "方法分为三步", 1_500, 4_000),
        ),
        source_segments=(
            SourceSegment("segment_01", 0, 1_500, True, "品牌产品只要99元", 0, 1_500),
            SourceSegment("segment_02", 1_500, 4_000, False, "方法分为三步", 1_500, 4_000),
        ),
        authoritative_text_sha256="a" * 64,
        alignment_coverage=1.0,
    )


def _edit_plan(cues: list[dict] | None = None) -> dict:
    return {
        "duration_ms": 4_000,
        "creative_concept": "可信产品说明",
        "audio_cues": cues or [],
    }


class _Cos:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_file(self, source, object_key, content_type, *, private, if_absent):
        self.assertions = (content_type, private, if_absent)
        if if_absent and object_key in self.objects:
            raise FileExistsError(object_key)
        self.objects[object_key] = Path(source).read_bytes()
        return {"etag": hashlib.sha256(self.objects[object_key]).hexdigest()}


class _Generator:
    def __init__(self, *, fail_music: bool = False, fail_cues: set[str] | None = None) -> None:
        self.fail_music = fail_music
        self.fail_cues = fail_cues or set()
        self.calls: list[tuple[str, str]] = []

    @staticmethod
    def _result(path: Path, capability: str, request_id: str) -> ProviderResult:
        return ProviderResult(
            provider="elevenlabs",
            capability=capability,
            request_id=request_id,
            payload={"sha256": hashlib.sha256(path.read_bytes()).hexdigest()},
            usage={"credits": 1},
            elapsed_ms=1,
        )

    def generate_music(self, request, *, output_path, idempotency_key, deadline_at):
        self.calls.append(("bgm", idempotency_key))
        if self.fail_music:
            raise DefinitiveNotAccepted("music_rejected")
        _tone(Path(output_path), duration_ms=request.duration_ms, frequency=220)
        return self._result(Path(output_path), "music", "music-request")

    def generate_sfx(self, request, *, output_path, idempotency_key, deadline_at):
        self.calls.append((request.cue_id, idempotency_key))
        if request.cue_id in self.fail_cues:
            raise DefinitiveNotAccepted("sfx_rejected")
        _tone(Path(output_path), duration_ms=request.duration_ms, frequency=880, amplitude=0.08)
        return self._result(Path(output_path), "sfx", f"request-{request.cue_id}")


def _context(job_id: str) -> StageContext:
    return StageContext(
        LeaseClaim(job_id, "worker-1", 1, 99_999_999_999_999),
        "attempt-1",
        "stage-attempt-1",
        time.time() + 60,
        lambda: None,
    )


class AudioPlanTests(unittest.TestCase):
    def test_every_job_gets_exactly_one_instrumental_bgm(self):
        plan = compile_audio_plan(_edit_plan(), _timeline())
        self.assertEqual(plan.music.duration_ms, 4_000)
        self.assertIn("instrumental", plan.music.prompt.lower())
        self.assertEqual(plan.sfx, ())

    def test_declared_roles_and_flags_are_preserved(self):
        plan = compile_audio_plan(
            _edit_plan([
                {"id": "sfx_method", "type": "sfx", "priority": "required", "role": "method", "start_ms": 2_000, "end_ms": 2_600, "description": "清晰步骤提示"},
                {"id": "sfx_cta", "type": "sfx", "priority": "optional", "role": "cta", "start_ms": 3_200, "end_ms": 3_800, "description": "轻柔收束"},
            ]),
            _timeline(),
        )
        self.assertEqual([(cue.cue_id, cue.required) for cue in plan.sfx], [("sfx_method", True), ("sfx_cta", False)])

    def test_undeclared_or_unsupported_sfx_and_protected_overlap_fail(self):
        base = {"id": "sfx_bad", "type": "sfx", "priority": "required", "start_ms": 2_000, "end_ms": 2_600, "description": "提示"}
        with self.assertRaisesRegex(AudioPlanError, "sfx_role_invalid"):
            compile_audio_plan(_edit_plan([{**base, "role": "ambient"}]), _timeline())
        with self.assertRaisesRegex(AudioPlanError, "sfx_protected_overlap"):
            compile_audio_plan(_edit_plan([{**base, "role": "number", "start_ms": 500, "end_ms": 1_000}]), _timeline())

    def test_optional_protected_sfx_degrades_before_provider_generation(self):
        plan = compile_audio_plan(
            _edit_plan([
                {
                    "id": "sfx_protected_optional",
                    "type": "sfx",
                    "priority": "optional",
                    "role": "transition",
                    "start_ms": 500,
                    "end_ms": 1_000,
                    "description": "optional protected accent",
                },
                {
                    "id": "fade_protected_optional",
                    "type": "volume_fade",
                    "priority": "optional",
                    "target": "sfx_protected_optional",
                    "start_ms": 500,
                    "end_ms": 800,
                    "from_db": -18,
                    "to_db": -6,
                    "description": "optional omitted fade",
                },
                {
                    "id": "sfx_safe_optional",
                    "type": "sfx",
                    "priority": "optional",
                    "role": "method",
                    "start_ms": 1_500,
                    "end_ms": 2_000,
                    "description": "safe boundary accent",
                },
            ]),
            _timeline(),
        )

        self.assertEqual(
            ("sfx_protected_optional",), plan.omitted_optional_sfx
        )
        self.assertEqual(
            ["sfx_safe_optional"], [item.cue_id for item in plan.sfx]
        )
        self.assertEqual((), plan.volume_fades)

        with self.assertRaisesRegex(AudioPlanError, "volume_fade_target_invalid"):
            compile_audio_plan(
                _edit_plan([
                    {
                        "id": "sfx_protected_optional",
                        "type": "sfx",
                        "priority": "optional",
                        "role": "transition",
                        "start_ms": 500,
                        "end_ms": 1_000,
                        "description": "optional protected accent",
                    },
                    {
                        "id": "fade_protected_required",
                        "type": "volume_fade",
                        "priority": "required",
                        "target": "sfx_protected_optional",
                        "start_ms": 500,
                        "end_ms": 800,
                        "from_db": -18,
                        "to_db": -6,
                        "description": "required fade cannot dangle",
                    },
                ]),
                _timeline(),
            )

    def test_real_failure_shape_omits_only_the_conflicting_optional_sfx(self):
        timeline = TextTimeline(
            duration_ms=26_000,
            captions=(Caption("caption_01", "safe", 0, 26_000),),
            source_segments=(
                SourceSegment(
                    "segment_protected",
                    10_205,
                    16_460,
                    True,
                    "protected",
                    10_205,
                    16_460,
                ),
            ),
            authoritative_text_sha256="b" * 64,
            alignment_coverage=1.0,
        )
        cues = [
            {
                "id": cue_id,
                "type": "sfx",
                "priority": priority,
                "role": role,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "description": "bounded accent",
            }
            for cue_id, role, priority, start_ms, end_ms in (
                ("scene_01_sfx_01", "transition", "required", 0, 500),
                ("scene_02_sfx_01", "method", "optional", 6_545, 7_045),
                ("scene_03_sfx_01", "transition", "optional", 10_405, 10_905),
                ("scene_04_sfx_01", "method", "optional", 16_760, 17_260),
                ("scene_05_sfx_01", "cta", "required", 19_938, 20_438),
            )
        ]

        plan = compile_audio_plan(
            {
                "duration_ms": 26_000,
                "creative_concept": "bounded commercial edit",
                "audio_cues": cues,
            },
            timeline,
        )

        self.assertEqual(("scene_03_sfx_01",), plan.omitted_optional_sfx)
        self.assertEqual(
            [
                "scene_01_sfx_01",
                "scene_02_sfx_01",
                "scene_04_sfx_01",
                "scene_05_sfx_01",
            ],
            [item.cue_id for item in plan.sfx],
        )

    def test_volume_fades_are_strict_non_overlapping_and_target_declared_audio(self):
        cues = [
            {"id": "sfx_method", "type": "sfx", "priority": "optional", "role": "method", "start_ms": 2_000, "end_ms": 2_600, "description": "步骤"},
            {"id": "fade_01", "type": "volume_fade", "priority": "required", "target": "bgm", "start_ms": 0, "end_ms": 500, "from_db": -60, "to_db": -18, "description": "淡入"},
            {"id": "fade_02", "type": "volume_fade", "priority": "required", "target": "sfx_method", "start_ms": 2_000, "end_ms": 2_300, "from_db": -18, "to_db": -6, "description": "抬升"},
        ]
        plan = compile_audio_plan(_edit_plan(cues), _timeline())
        self.assertEqual([fade.target for fade in plan.volume_fades], ["bgm", "sfx_method"])
        with self.assertRaisesRegex(AudioPlanError, "volume_fade_overlap"):
            compile_audio_plan(_edit_plan(cues + [{**cues[1], "id": "fade_03", "start_ms": 400, "end_ms": 700}]), _timeline())
        with self.assertRaisesRegex(AudioPlanError, "volume_fade_target_invalid"):
            compile_audio_plan(_edit_plan([{**cues[1], "target": "not_declared"}]), _timeline())
        with self.assertRaisesRegex(AudioPlanError, "volume_fade_target_range_invalid"):
            compile_audio_plan(
                _edit_plan([
                    cues[0],
                    {**cues[2], "start_ms": 0, "end_ms": 300},
                ]),
                _timeline(),
            )


class AudioGenerationTests(unittest.TestCase):
    def test_generation_uses_task_private_keys_and_optional_sfx_can_degrade(self):
        plan = compile_audio_plan(_edit_plan([
            {"id": "sfx_optional", "type": "sfx", "priority": "optional", "role": "cta", "start_ms": 3_100, "end_ms": 3_700, "description": "轻柔收束"},
        ]), _timeline())
        generator = _Generator(fail_cues={"sfx_optional"})
        cos = _Cos()
        with TemporaryDirectory() as folder:
            assets = generate_task_audio("job-1", plan, generator, cos, Path(folder), _context("job-1"))
        self.assertEqual([asset.kind for asset in assets], ["bgm"])
        self.assertEqual(generator.calls[0], ("bgm", "ai-edit-v3:job-1:audio:bgm"))
        self.assertEqual(len(cos.objects), 1)
        self.assertTrue(next(iter(cos.objects)).endswith("/bgm.wav"))

    def test_required_generation_failure_is_terminal_and_bounded(self):
        plan = compile_audio_plan(_edit_plan(), _timeline())
        generator = _Generator(fail_music=True)
        with TemporaryDirectory() as folder:
            with self.assertRaisesRegex(AudioGenerationError, "bgm_generation_failed"):
                generate_task_audio("job-1", plan, generator, _Cos(), Path(folder), _context("job-1"))
        self.assertEqual(len(generator.calls), 2)


class MasterAudioTests(unittest.TestCase):
    def test_builds_48khz_stereo_loudness_master_with_exact_duration_and_sha(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            voice = root / "voice.wav"
            bgm = root / "bgm.wav"
            _tone(voice, duration_ms=4_000, frequency=440, amplitude=0.22)
            _tone(bgm, duration_ms=4_000, frequency=220, amplitude=0.08)
            generated = (GeneratedAudioAsset("bgm", "bgm", "bgm.wav", "test/ai-edit-v3/job/audio/bgm.wav", hashlib.sha256(bgm.read_bytes()).hexdigest(), 4_000, 48_000, 1, "req", {"credits": 1}),)
            plan = compile_audio_plan(_edit_plan(), _timeline())
            output = root / "master.wav"
            result = build_master_audio(voice, _timeline().source_segments, plan, generated, output, deadline_at=time.time() + 90)
            self.assertEqual((result.sample_rate, result.channels), (48_000, 2))
            self.assertLessEqual(abs(result.duration_ms - 4_000), 40)
            self.assertGreaterEqual(result.integrated_lufs, -18)
            self.assertLessEqual(result.integrated_lufs, -14)
            self.assertLessEqual(result.true_peak_dbtp, -1.25)
            self.assertEqual(result.audit["true_peak_target_dbtp"], -1.5)
            self.assertEqual(result.sha256, hashlib.sha256(output.read_bytes()).hexdigest())
            self.assertEqual(result.audit["loudness_passes"], 2)

    def test_rejects_missing_voice_non_monotonic_segments_and_duration_mismatch(self):
        plan = compile_audio_plan(_edit_plan(), _timeline())
        with TemporaryDirectory() as folder:
            root = Path(folder)
            with self.assertRaisesRegex(AudioGenerationError, "voice_source_missing"):
                build_master_audio(root / "missing.wav", _timeline().source_segments, plan, (), root / "master.wav", deadline_at=time.time() + 10)
            voice = root / "voice.wav"
            _tone(voice, duration_ms=4_000)
            reversed_segments = tuple(reversed(_timeline().source_segments))
            with self.assertRaisesRegex(AudioGenerationError, "source_segments_non_monotonic"):
                build_master_audio(voice, reversed_segments, plan, (), root / "master.wav", deadline_at=time.time() + 10)


if __name__ == "__main__":
    unittest.main()
