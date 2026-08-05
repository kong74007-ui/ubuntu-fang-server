from __future__ import annotations

from array import array
import hashlib
import json
import math
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

from server.content_domains.ai_edit_v3.audio import (
    AudioGenerationError,
    GeneratedAudioAsset,
    build_master_audio,
    compile_audio_plan,
    generate_task_audio,
)
from server.content_domains.ai_edit_v3.production import invoke_provider_once
from server.content_domains.ai_edit_v3.providers.base import (
    DefinitiveNotAccepted,
    ProviderResult,
    SubmissionUnknown,
)
from server.content_domains.ai_edit_v3.contracts import LeaseClaim
from server.content_domains.ai_edit_v3.runtime import StageContext
from server.content_domains.ai_edit_v3.transcript import Caption, SourceSegment, TextTimeline


def _stereo_silence(path: Path, duration_ms: int) -> bytes:
    frames = 48_000 * duration_ms // 1_000
    with wave.open(str(path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(48_000)
        output.writeframes(b"\0\0\0\0" * frames)
    return path.read_bytes()


def _stereo_tone_with_silence(
    path: Path,
    *,
    duration_ms: int,
    frequency: float,
    amplitude: float,
    tone_until_ms: int | None = None,
) -> bytes:
    frames = 48_000 * duration_ms // 1_000
    tone_frames = frames if tone_until_ms is None else 48_000 * tone_until_ms // 1_000
    samples = array("h")
    for index in range(frames):
        value = (
            int(32_767 * amplitude * math.sin(2 * math.pi * frequency * index / 48_000))
            if index < tone_frames
            else 0
        )
        samples.extend((value, value))
    with wave.open(str(path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(48_000)
        output.writeframes(samples.tobytes())
    return path.read_bytes()


def _frequency_amplitude(path: Path, frequency: float, start_ms: int, end_ms: int) -> float:
    with wave.open(str(path), "rb") as source:
        channels = source.getnchannels()
        sample_width = source.getsampwidth()
        source.setpos(48_000 * start_ms // 1_000)
        raw = source.readframes(48_000 * (end_ms - start_ms) // 1_000)
    frame_width = channels * sample_width
    mono = [
        int.from_bytes(raw[offset:offset + sample_width], "little", signed=True)
        for offset in range(0, len(raw), frame_width)
    ]
    cosine = sum(
        value * math.cos(2 * math.pi * frequency * index / 48_000)
        for index, value in enumerate(mono)
    )
    sine = sum(
        value * math.sin(2 * math.pi * frequency * index / 48_000)
        for index, value in enumerate(mono)
    )
    return 2 * math.hypot(cosine, sine) / max(1, len(mono))


def _timeline() -> TextTimeline:
    return TextTimeline(
        duration_ms=4_000,
        captions=(Caption("caption_01", "方法分为三步", 0, 4_000),),
        source_segments=(
            SourceSegment(
                "segment_01", 0, 4_000, False, "方法分为三步", 0, 4_000
            ),
        ),
        authoritative_text_sha256="a" * 64,
        alignment_coverage=1.0,
    )


def _context(job_id: str) -> StageContext:
    return StageContext(
        LeaseClaim(job_id, "worker-1", 1, 99_999_999_999_999),
        "attempt-1",
        "stage-attempt-1",
        time.time() + 60,
        lambda: None,
    )


class _NoCallGenerator:
    def generate_music(self, *_args, **_kwargs):
        raise AssertionError("completed receipt must not resubmit ElevenLabs")

    def generate_sfx(self, *_args, **_kwargs):
        raise AssertionError("completed receipt must not resubmit ElevenLabs")


class _UnsafeReceiptGenerator:
    def generate_music(self, _request, *, output_path, **_kwargs):
        _stereo_silence(Path(output_path), 4_000)
        return ProviderResult(
            provider="elevenlabs",
            capability="music",
            request_id="https://provider.example/result?signature=secret",
            payload={},
            usage={"credits": 2},
            elapsed_ms=1,
        )

    def generate_sfx(self, *_args, **_kwargs):
        raise AssertionError("no sfx expected")


class _UnknownGenerator:
    def generate_sfx(self, *_args, **_kwargs):
        raise SubmissionUnknown("elevenlabs_submission_unknown")


class _NoRequestIdGenerator:
    def generate_music(self, request, *, output_path, **_kwargs):
        _stereo_silence(Path(output_path), request.duration_ms)
        return ProviderResult("elevenlabs", "music", None, {}, {}, 1)

    def generate_sfx(self, request, *, output_path, **_kwargs):
        _stereo_silence(Path(output_path), request.duration_ms)
        return ProviderResult("elevenlabs", "sfx", None, {}, {}, 1)


class _UploadCos:
    environment = "test"

    def __init__(self):
        self.objects = {}

    def put_file(self, source, key, *_args, **_kwargs):
        self.objects[key] = Path(source).read_bytes()
        return {"key": key}

    def download_file(self, key, destination):
        Path(destination).write_bytes(self.objects[key])
        return str(destination)


class _Cos:
    environment = "test"

    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects
        self.downloads: list[str] = []

    def put_file(self, *_args, **_kwargs):
        raise AssertionError("completed receipt must not upload again")

    def download_file(self, key: str, destination: str | Path) -> str:
        self.downloads.append(key)
        Path(destination).write_bytes(self.objects[key])
        return str(destination)


class SoundDirectionTests(unittest.TestCase):
    def test_missing_provider_request_id_fallback_is_stable_and_globally_unique(self):
        plan = compile_audio_plan(
            {
                "duration_ms": 4_000,
                "creative_concept": "restrained commercial explainer",
                "audio_cues": [],
            },
            _timeline(),
        )
        request_ids = []

        def provider_once(**kwargs):
            receipt = kwargs["call"]()
            request_ids.append(receipt["request_id"])
            return receipt

        with TemporaryDirectory() as folder:
            root = Path(folder)
            for job_id in ("job-one", "job-two"):
                generate_task_audio(
                    job_id,
                    plan,
                    _NoRequestIdGenerator(),
                    _UploadCos(),
                    root,
                    _context(job_id),
                    provider_once=provider_once,
                )

        self.assertEqual(2, len(set(request_ids)))
        self.assertTrue(all(item.startswith("generated-") for item in request_ids))

    def test_terminal_receipt_replay_rejects_changed_immutable_intent(self):
        class Store:
            def get_provider_task_for_claim(self, *_args):
                return {
                    "status": "completed",
                    "stage": "generating_audio",
                    "provider": "elevenlabs",
                    "capability": "bgm",
                    "request_sha256": "a" * 64,
                    "result_json": json.dumps({"request_id": "old-request"}),
                }

        with self.assertRaisesRegex(ValueError, "provider_intent_conflict"):
            invoke_provider_once(
                store=Store(),
                context=SimpleNamespace(claim=object(), stage_attempt_id="attempt-1"),
                stage="generating_audio",
                provider="elevenlabs",
                capability="bgm",
                operation_key="ai-edit-v3:job:audio:bgm",
                request_sha256="b" * 64,
                call=lambda: self.fail("changed intent must never replay or resubmit"),
                now_ms=123,
            )

    def test_rendered_waveform_applies_bgm_fade(self):
        plan = compile_audio_plan(
            {
                "duration_ms": 4_000,
                "creative_concept": "restrained commercial explainer",
                "audio_cues": [
                    {
                        "id": "fade_bgm",
                        "type": "volume_fade",
                        "priority": "required",
                        "target": "bgm",
                        "start_ms": 0,
                        "end_ms": 500,
                        "from_db": -60,
                        "to_db": 0,
                        "description": "fade in",
                    }
                ],
            },
            _timeline(),
        )
        with TemporaryDirectory() as folder:
            root = Path(folder)
            voice = root / "voice.wav"
            bgm = root / "bgm.wav"
            _stereo_silence(voice, 4_000)
            _stereo_tone_with_silence(
                bgm, duration_ms=4_000, frequency=220, amplitude=0.12
            )
            generated = (
                GeneratedAudioAsset(
                    "bgm", "bgm", str(bgm), "test/audio/bgm.wav",
                    hashlib.sha256(bgm.read_bytes()).hexdigest(), 4_000, 48_000, 2,
                    "music-request", {},
                ),
            )
            output = root / "master.wav"
            build_master_audio(
                voice,
                _timeline().source_segments,
                plan,
                generated,
                output,
                deadline_at=time.time() + 90,
            )
            early = _frequency_amplitude(output, 220, 50, 150)
            settled = _frequency_amplitude(output, 220, 1_000, 1_100)

        self.assertGreater(settled, early * 10)

    def test_unknown_submission_stays_fail_closed_and_is_never_optional_degradation(self):
        from server.content_domains.ai_edit_v3.audio import _generate_one

        plan = compile_audio_plan(
            {
                "duration_ms": 4_000,
                "creative_concept": "restrained commercial explainer",
                "audio_cues": [
                    {
                        "id": "sfx_optional",
                        "type": "sfx",
                        "priority": "optional",
                        "role": "cta",
                        "start_ms": 3_100,
                        "end_ms": 3_700,
                        "description": "soft close",
                    }
                ],
            },
            _timeline(),
        )
        with TemporaryDirectory() as folder:
            with self.assertRaisesRegex(
                SubmissionUnknown,
                "elevenlabs_submission_unknown",
            ):
                _generate_one(
                    kind="sfx",
                    cue_id="sfx_optional",
                    request=plan.sfx[0],
                    generator=_UnknownGenerator(),
                    path=Path(folder) / "provider-audio",
                    key="job",
                    deadline_at=time.time() + 30,
                )

    def test_rendered_waveform_ducks_bgm_and_sfx_under_dialogue(self):
        plan = compile_audio_plan(
            {
                "duration_ms": 4_000,
                "creative_concept": "restrained commercial explainer",
                "audio_cues": [
                    {
                        "id": "sfx_method",
                        "type": "sfx",
                        "priority": "required",
                        "role": "method",
                        "start_ms": 800,
                        "end_ms": 1_400,
                        "description": "step accent",
                    }
                ],
            },
            _timeline(),
        )
        with TemporaryDirectory() as folder:
            root = Path(folder)
            voice = root / "voice.wav"
            bgm = root / "bgm.wav"
            sfx = root / "sfx.wav"
            _stereo_tone_with_silence(
                voice,
                duration_ms=4_000,
                frequency=440,
                amplitude=0.30,
                tone_until_ms=1_100,
            )
            _stereo_tone_with_silence(
                bgm, duration_ms=4_000, frequency=220, amplitude=0.12
            )
            _stereo_tone_with_silence(
                sfx, duration_ms=600, frequency=880, amplitude=0.20
            )
            generated = (
                GeneratedAudioAsset(
                    "bgm", "bgm", str(bgm), "test/audio/bgm.wav",
                    hashlib.sha256(bgm.read_bytes()).hexdigest(), 4_000, 48_000, 2,
                    "music-request", {},
                ),
                GeneratedAudioAsset(
                    "sfx_method", "sfx", str(sfx), "test/audio/sfx.wav",
                    hashlib.sha256(sfx.read_bytes()).hexdigest(), 600, 48_000, 2,
                    "sfx-request", {},
                ),
            )
            output = root / "master.wav"
            build_master_audio(
                voice,
                _timeline().source_segments,
                plan,
                generated,
                output,
                deadline_at=time.time() + 90,
            )

            bgm_during_dialogue = _frequency_amplitude(output, 220, 500, 700)
            bgm_after_dialogue = _frequency_amplitude(output, 220, 2_500, 2_700)
            sfx_during_dialogue = _frequency_amplitude(output, 880, 900, 1_000)
            sfx_after_dialogue = _frequency_amplitude(output, 880, 1_250, 1_350)

        self.assertGreater(bgm_after_dialogue, bgm_during_dialogue * 1.5)
        self.assertGreater(sfx_after_dialogue, sfx_during_dialogue * 1.25)

    def test_definitive_provider_failure_is_persisted_and_replayed_without_resubmit(self):
        class Store:
            task = None

            def get_provider_task_for_claim(self, *_args):
                return self.task

            def record_provider_intent(self, *_args):
                self.task = {
                    "status": "intent_recorded",
                    "stage": _args[1],
                    "provider": _args[3],
                    "capability": _args[4],
                    "request_sha256": _args[6],
                }

            def claim_provider_submission(self, *_args):
                self.task = {**self.task, "status": "submitting"}
                return True

            def bind_provider_result(
                self, _claim, _operation_key, external_id, status, result, _now_ms
            ):
                self.task = {
                    **self.task,
                    "status": status,
                    "external_id": external_id,
                    "result_json": json.dumps(result),
                }

        store = Store()
        context = SimpleNamespace(claim=object(), stage_attempt_id="attempt-1")
        calls = []

        def rejected():
            calls.append("called")
            raise DefinitiveNotAccepted("elevenlabs_not_accepted")

        arguments = {
            "store": store,
            "context": context,
            "stage": "generating_audio",
            "provider": "elevenlabs",
            "capability": "bgm",
            "operation_key": "ai-edit-v3:job:audio:bgm",
            "request_sha256": "a" * 64,
            "now_ms": 123,
        }
        with self.assertRaisesRegex(
            DefinitiveNotAccepted,
            "elevenlabs_not_accepted",
        ):
            invoke_provider_once(call=rejected, **arguments)
        self.assertEqual("failed", store.task["status"])

        with self.assertRaisesRegex(
            DefinitiveNotAccepted,
            "elevenlabs_not_accepted",
        ):
            invoke_provider_once(
                call=lambda: self.fail("failed receipt must not resubmit"),
                **arguments,
            )
        self.assertEqual(["called"], calls)

    def test_unsafe_provider_fields_are_rejected_before_receipt_persistence(self):
        plan = compile_audio_plan(
            {
                "duration_ms": 4_000,
                "creative_concept": "restrained commercial explainer",
                "audio_cues": [],
            },
            _timeline(),
        )
        persisted = []

        def provider_once(**kwargs):
            receipt = kwargs["call"]()
            persisted.append(receipt)
            return receipt

        with TemporaryDirectory() as folder:
            with self.assertRaisesRegex(
                AudioGenerationError,
                "audio_receipt_invalid",
            ):
                generate_task_audio(
                    "job-unsafe-receipt",
                    plan,
                    _UnsafeReceiptGenerator(),
                    _Cos({}),
                    Path(folder),
                    _context("job-unsafe-receipt"),
                    provider_once=provider_once,
                )

        self.assertEqual([], persisted)

    def test_volume_automation_is_compiled_into_bgm_and_scene_local_sfx_filters(self):
        from server.content_domains.ai_edit_v3.audio import _build_mix_filter_graph

        plan = compile_audio_plan(
            {
                "duration_ms": 4_000,
                "creative_concept": "克制可信的商业讲解",
                "audio_cues": [
                    {
                        "id": "sfx_method",
                        "type": "sfx",
                        "priority": "required",
                        "role": "method",
                        "start_ms": 2_000,
                        "end_ms": 2_600,
                        "description": "清晰步骤提示",
                    },
                    {
                        "id": "fade_bgm",
                        "type": "volume_fade",
                        "priority": "required",
                        "target": "bgm",
                        "start_ms": 0,
                        "end_ms": 500,
                        "from_db": -60,
                        "to_db": -18,
                        "description": "淡入",
                    },
                    {
                        "id": "fade_sfx",
                        "type": "volume_fade",
                        "priority": "required",
                        "target": "sfx_method",
                        "start_ms": 2_000,
                        "end_ms": 2_300,
                        "from_db": -30,
                        "to_db": -6,
                        "description": "强调",
                    },
                ],
            },
            _timeline(),
        )
        sfx = GeneratedAudioAsset(
            "sfx_method",
            "sfx",
            "sfx.wav",
            "test/ai-edit-v3/job/audio/sfx.wav",
            "b" * 64,
            600,
            48_000,
            2,
            "request-sfx",
            {},
        )

        filters, _labels = _build_mix_filter_graph(
            _timeline().source_segments,
            plan,
            [sfx],
        )
        graph = ";".join(filters)

        self.assertIn("[bgm_base]volume=eval=frame", graph)
        self.assertIn("between(t,0.000,0.500)", graph)
        self.assertIn("pow(10,(-18.000)/20)", graph)
        self.assertIn("[sfx_2_base]volume=eval=frame", graph)
        self.assertIn("between(t,0.000,0.300)", graph)
        self.assertIn("[voice_base]asplit=3[voice_mix][voice_bgm][voice_sfx_2]", graph)
        self.assertIn("[sfx_2_automated]adelay=2000|2000[sfx_2_delayed]", graph)
        self.assertIn(
            "[sfx_2_delayed][voice_sfx_2]sidechaincompress",
            graph,
        )

    def test_completed_receipt_restores_private_cos_asset_without_provider_call(self):
        plan = compile_audio_plan(
            {
                "duration_ms": 4_000,
                "creative_concept": "克制可信的商业讲解",
                "audio_cues": [],
            },
            _timeline(),
        )
        with TemporaryDirectory() as folder:
            root = Path(folder)
            fixture = root / "fixture.wav"
            audio = _stereo_silence(fixture, 4_000)
            digest = hashlib.sha256(audio).hexdigest()
            object_key = "test/ai-edit-v3/job-audio/audio/bgm.wav"
            cos = _Cos({object_key: audio})
            receipt_calls = []
            stale = root / "job-audio" / "audio" / "bgm.wav"
            stale.parent.mkdir(parents=True)
            stale.write_bytes(b"stale-partial-download")

            def provider_once(**kwargs):
                receipt_calls.append(kwargs)
                return {
                    "request_id": "music-request-1",
                    "cue_id": "bgm",
                    "kind": "bgm",
                    "object_key": object_key,
                    "sha256": digest,
                    "duration_ms": 4_000,
                    "sample_rate": 48_000,
                    "channels": 2,
                    "provider_request_id": "music-request-1",
                    "usage": {"credits": 2},
                    "model": "music_v2",
                }

            assets = generate_task_audio(
                "job-audio",
                plan,
                _NoCallGenerator(),
                cos,
                root,
                _context("job-audio"),
                provider_once=provider_once,
            )

            restored = root / assets[0].relative_path
            self.assertEqual(audio, restored.read_bytes())
            self.assertEqual(digest, assets[0].sha256)
            self.assertEqual([object_key], cos.downloads)
            self.assertEqual(1, len(receipt_calls))
            self.assertEqual(
                "ai-edit-v3:job-audio:audio:bgm",
                receipt_calls[0]["operation_key"],
            )

    def test_generating_audio_wires_each_cue_through_real_store_receipt(self):
        completed = {
            "status": "completed",
            "stage": "generating_audio",
            "provider": "elevenlabs",
            "capability": "bgm",
            "request_sha256": "b" * 64,
            "result_json": json.dumps({"request_id": "receipt-request"}),
        }

        class Store:
            environment = "test"

            def get_provider_task_for_claim(self, *_args):
                return completed

            def record_provider_intent(self, *_args):
                raise AssertionError("completed receipt must not be recorded again")

            def claim_provider_submission(self, *_args):
                raise AssertionError("completed receipt must not be claimed again")

            def bind_provider_result(self, *_args):
                raise AssertionError("completed receipt must not be rebound")

        captured = {}
        with TemporaryDirectory() as folder:
            from server.content_domains.ai_edit_v3.production import (
                ProductionStageCoordinator,
            )

            coordinator = object.__new__(ProductionStageCoordinator)
            coordinator.work_root = Path(folder)
            coordinator.store = Store()
            coordinator.audio_generator = object()
            coordinator.cos = object()
            root = coordinator._root("job-audio-stage")
            root.mkdir(parents=True, exist_ok=True)
            (root / "plan.json").write_text(
                json.dumps(
                    {
                        "duration_ms": 4_000,
                        "creative_concept": "克制可信的商业讲解",
                        "audio_cues": [],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (root / "timeline.json").write_text(
                json.dumps(
                    {
                        "duration_ms": 4_000,
                        "captions": [
                            {
                                "id": "caption_01",
                                "text": "方法分为三步",
                                "start_ms": 0,
                                "end_ms": 4_000,
                            }
                        ],
                        "source_segments": [
                            {
                                "id": "segment_01",
                                "text": "方法分为三步",
                                "start_ms": 0,
                                "end_ms": 4_000,
                                "protected": False,
                                "output_start_ms": 0,
                                "output_end_ms": 4_000,
                            }
                        ],
                        "authoritative_text_sha256": "a" * 64,
                        "alignment_coverage": 1.0,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            def fake_generate(*_args, **kwargs):
                captured.update(kwargs)
                return ()

            context = SimpleNamespace(
                claim=object(),
                stage_attempt_id="stage-attempt-1",
                deadline_at=time.time() + 60,
            )
            with patch(
                "server.content_domains.ai_edit_v3.production.generate_task_audio",
                side_effect=fake_generate,
            ):
                coordinator._stage(
                    "generating_audio",
                    {
                        "job_id": "job-audio-stage",
                        "owner_id": "alice",
                        "stage_input_sha256": "0" * 64,
                        "normalized_request_json": '{"input_type":"uploaded_audio"}',
                    },
                    context,
                )

        provider_once = captured.get("provider_once")
        self.assertTrue(callable(provider_once))
        self.assertEqual(
            {"request_id": "receipt-request"},
            provider_once(
                provider="elevenlabs",
                capability="bgm",
                operation_key="ai-edit-v3:job-audio-stage:audio:bgm",
                request_sha256="b" * 64,
                call=lambda: self.fail("completed receipt must not call provider"),
            ),
        )


if __name__ == "__main__":
    unittest.main()
    build_master_audio,
