import json
import hashlib
import shutil
import tempfile
import unittest
from pathlib import Path

from server.content_domains.ai_edit_v3.acceptance_verify import (
    BLOCKING_CHECKS,
    CaseEvidence,
    MachineVerdict,
    aggregate_machine_verdicts,
    load_quality_evidence,
    probe_final_output,
    verify_quality_evidence,
)
from scripts.ai_edit_v3_acceptance import main


METRIC_CHECKS = {
    "integrated_loudness",
    "true_peak",
    "duplicate_dialogue",
    "abnormal_silence",
    "lip_audio_sync",
}


def passing_checks() -> dict[str, bool]:
    return {name: True for name in BLOCKING_CHECKS if name not in METRIC_CHECKS}


def passing_metrics() -> dict[str, int | float]:
    return {
        "integrated_lufs": -16.0,
        "true_peak_dbtp": -1.2,
        "duplicate_dialogue_count": 0,
        "abnormal_silence_count": 0,
        "lip_audio_offset_ms": 60,
    }


DEFAULT_OUTPUT_SHA256 = "a" * 64


def passing_analyzers(output_sha256: str = DEFAULT_OUTPUT_SHA256) -> dict[str, dict[str, str]]:
    return {
        "duplicate_dialogue": {
            "name": "dialogue-fingerprint",
            "version": "1.0.0",
            "evidence_sha256": "b" * 64,
            "output_sha256": output_sha256,
            "verified": True,
        },
        "lip_audio_sync": {
            "name": "talking-head-av-sync",
            "version": "1.0.0",
            "evidence_sha256": "c" * 64,
            "output_sha256": output_sha256,
            "verified": True,
        },
    }


def quality_evidence(
    *,
    checks: dict[str, bool | None],
    metrics: dict[str, int | float],
    output_sha256: str = DEFAULT_OUTPUT_SHA256,
) -> CaseEvidence:
    return CaseEvidence(
        checks=checks,
        metrics=metrics,
        analyzers=passing_analyzers(output_sha256),
        output_sha256=output_sha256,
        lip_sync_applicable=True,
    )


def write_analyzer_artifacts(
    directory: Path,
    output_sha256: str,
    *,
    lip_sync_applicable: bool = True,
) -> dict[str, str]:
    duplicate_name = "duplicate-dialogue.json"
    lip_name = "lip-sync.json"
    (directory / duplicate_name).write_text(json.dumps({
        "analyzer_id": "dialogue-fingerprint",
        "analyzer_version": "1.0.0",
        "output_sha256": output_sha256,
        "metrics": {"duplicate_dialogue_count": 0},
    }), encoding="utf-8")
    lip_payload = {
        "analyzer_id": (
            "talking-head-av-sync" if lip_sync_applicable else "talking-head-presence"
        ),
        "analyzer_version": "1.0.0",
        "output_sha256": output_sha256,
        "applicable": lip_sync_applicable,
        "metrics": (
            {"lip_audio_offset_ms": 60}
            if lip_sync_applicable
            else {"talking_head_present": False}
        ),
    }
    (directory / lip_name).write_text(json.dumps(lip_payload), encoding="utf-8")
    return {"duplicate_dialogue": duplicate_name, "lip_audio_sync": lip_name}


class Result:
    def __init__(self, *, stdout: str = "", stderr: str = "", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def good_probe_payload() -> dict:
    return {
        "streams": [
            {
                "index": 0,
                "codec_type": "video",
                "codec_name": "h264",
                "pix_fmt": "yuv420p",
                "width": 1080,
                "height": 1920,
                "r_frame_rate": "30/1",
                "duration": "2.000",
            },
            {
                "index": 1,
                "codec_type": "audio",
                "codec_name": "aac",
                "sample_rate": "48000",
                "channels": 2,
                "duration": "2.020",
            },
        ]
    }


def packet_payload(*, regress: bool = False) -> dict:
    video = [0.0, 0.033333, 0.066667]
    if regress:
        video[-1] = 0.02
    return {
        "packets": [
            *({"stream_index": 0, "pts_time": str(value), "dts_time": str(value)} for value in video),
            *({"stream_index": 1, "pts_time": str(value), "dts_time": str(value)} for value in (0.0, 0.021333)),
        ]
    }


def frame_payload(*, regress: bool = False) -> dict:
    video = [0.0, 0.033333, 0.066667]
    if regress:
        video[-1] = 0.02
    return {
        "frames": [
            *(
                {"stream_index": 0, "best_effort_timestamp_time": str(value)}
                for value in video
            ),
            *(
                {"stream_index": 1, "best_effort_timestamp_time": str(value)}
                for value in (0.0, 0.021333)
            ),
        ]
    }


class AcceptanceVerifyTests(unittest.TestCase):
    def test_missing_material_ownership_evidence_fails_closed(self) -> None:
        checks = passing_checks()
        checks["material_ownership"] = None

        verdict = verify_quality_evidence(quality_evidence(checks=checks, metrics=passing_metrics()))

        self.assertFalse(verdict.passed)
        self.assertEqual(
            verdict.blockers,
            ("quality_evidence_missing:material_ownership",),
        )

    def test_every_required_check_is_fail_closed_and_unknown_checks_do_not_replace_it(self) -> None:
        for missing in set(BLOCKING_CHECKS) - METRIC_CHECKS:
            with self.subTest(missing=missing):
                checks = passing_checks()
                checks.pop(missing)
                checks["unknown"] = True
                verdict = verify_quality_evidence(
                    quality_evidence(checks=checks, metrics=passing_metrics())
                )
                self.assertIn(f"quality_evidence_missing:{missing}", verdict.blockers)

    def test_metric_json_is_parsed_and_thresholds_are_not_replaced_by_claimed_booleans(self) -> None:
        cases = {
            "integrated_loudness": ("integrated_lufs", -18.01),
            "true_peak": ("true_peak_dbtp", -0.99),
            "duplicate_dialogue": ("duplicate_dialogue_count", 1),
            "abnormal_silence": ("abnormal_silence_count", 1),
            "lip_audio_sync": ("lip_audio_offset_ms", 80.01),
        }
        for check, (metric, value) in cases.items():
            with self.subTest(check=check):
                checks = passing_checks()
                checks[check] = True
                metrics = passing_metrics()
                metrics[metric] = value
                verdict = verify_quality_evidence(quality_evidence(checks=checks, metrics=metrics))
                self.assertIn(f"quality_evidence_failed:{check}", verdict.blockers)

    def test_missing_or_non_finite_metric_fails_closed(self) -> None:
        for missing in passing_metrics():
            with self.subTest(missing=missing):
                metrics = passing_metrics()
                metrics.pop(missing)
                verdict = verify_quality_evidence(
                    quality_evidence(checks=passing_checks(), metrics=metrics)
                )
                expected_check = {
                    "integrated_lufs": "integrated_loudness",
                    "true_peak_dbtp": "true_peak",
                    "duplicate_dialogue_count": "duplicate_dialogue",
                    "abnormal_silence_count": "abnormal_silence",
                    "lip_audio_offset_ms": "lip_audio_sync",
                }[missing]
                self.assertIn(f"quality_evidence_missing:{expected_check}", verdict.blockers)

        metrics = passing_metrics()
        metrics["integrated_lufs"] = float("nan")
        verdict = verify_quality_evidence(quality_evidence(checks=passing_checks(), metrics=metrics))
        self.assertIn("quality_evidence_failed:integrated_loudness", verdict.blockers)

    def test_probe_uses_argument_lists_and_accepts_exact_output_contract(self) -> None:
        calls: list[tuple[list[str], dict]] = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            if command[0] == "ffprobe" and "-show_packets" in command:
                return Result(stdout=json.dumps(packet_payload()))
            if command[0] == "ffprobe" and "-show_frames" in command:
                return Result(stdout=json.dumps(frame_payload()))
            if command[0] == "ffprobe":
                return Result(stdout=json.dumps(good_probe_payload()))
            if "-af" in command:
                return Result(stderr='{"input_i":"-16.1","input_tp":"-1.2"}')
            return Result()

        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "good.mp4"
            media.write_bytes(b"fixture")
            probe = probe_final_output(media, process_runner=runner)

        self.assertTrue(probe.checks["stream_contract"])
        self.assertTrue(probe.checks["decoded_media"])
        self.assertTrue(probe.checks["monotonic_pts"])
        self.assertTrue(probe.checks["av_duration_sync"])
        self.assertTrue(probe.checks["integrated_loudness"])
        self.assertTrue(probe.checks["true_peak"])
        self.assertEqual(probe.metrics["av_duration_difference_ms"], 20)
        self.assertEqual(
            [call[0][0] for call in calls],
            ["ffprobe", "ffprobe", "ffprobe", "ffmpeg", "ffmpeg"],
        )
        self.assertTrue(all(isinstance(call[0], list) for call in calls))
        self.assertTrue(all(call[1].get("shell") is False for call in calls))
        self.assertTrue(all(str(media.resolve()) in call[0] for call in calls))
        decode = calls[3][0]
        self.assertIn("-xerror", decode)
        self.assertEqual(decode[decode.index("-err_detect") + 1], "explode")

    def test_bad_contract_decode_pts_and_av_drift_are_independent_blockers(self) -> None:
        payload = good_probe_payload()
        payload["streams"][0].update({"width": 1280, "height": 720, "pix_fmt": "yuv444p"})
        payload["streams"][1]["duration"] = "2.200"

        def runner(command, **_kwargs):
            if command[0] == "ffprobe" and "-show_packets" in command:
                return Result(stdout=json.dumps(packet_payload(regress=True)))
            if command[0] == "ffprobe" and "-show_frames" in command:
                return Result(stdout=json.dumps(frame_payload(regress=True)))
            if command[0] == "ffprobe":
                return Result(stdout=json.dumps(payload))
            if "-af" in command:
                return Result(stderr='{"input_i":"-19.0","input_tp":"-0.5"}')
            return Result(returncode=1, stderr="decode failed")

        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "bad.mp4"
            media.write_bytes(b"fixture")
            probe = probe_final_output(media, process_runner=runner)

        self.assertEqual(
            {name for name, passed in probe.checks.items() if not passed},
            {
                "stream_contract", "decoded_media", "monotonic_pts", "av_duration_sync",
                "integrated_loudness", "true_peak",
            },
        )

    def test_missing_loudnorm_json_is_not_treated_as_audio_quality_success(self) -> None:
        def runner(command, **_kwargs):
            if command[0] == "ffprobe" and "-show_packets" in command:
                return Result(stdout=json.dumps(packet_payload()))
            if command[0] == "ffprobe" and "-show_frames" in command:
                return Result(stdout=json.dumps(frame_payload()))
            if command[0] == "ffprobe":
                return Result(stdout=json.dumps(good_probe_payload()))
            if "-af" in command:
                return Result(stderr="analysis completed without metrics")
            return Result()

        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "missing-metrics.mp4"
            media.write_bytes(b"fixture")
            probe = probe_final_output(media, process_runner=runner)

        self.assertFalse(probe.checks["integrated_loudness"])
        self.assertFalse(probe.checks["true_peak"])
        self.assertIn("loudnorm_json_invalid", probe.errors)

    def test_boolean_or_negative_analyzer_metrics_cannot_pass_as_numeric_zero(self) -> None:
        for metric in ("duplicate_dialogue_count", "abnormal_silence_count"):
            for invalid in (False, -1, 0.5):
                with self.subTest(metric=metric, invalid=invalid):
                    metrics = passing_metrics()
                    metrics[metric] = invalid
                    verdict = verify_quality_evidence(
                        quality_evidence(checks=passing_checks(), metrics=metrics)
                    )
                    check = (
                        "duplicate_dialogue"
                        if metric == "duplicate_dialogue_count"
                        else "abnormal_silence"
                    )
                    self.assertIn(f"quality_evidence_failed:{check}", verdict.blockers)

    def test_duplicate_and_lip_metrics_require_versioned_output_bound_analyzers(self) -> None:
        for analyzer, check in (
            ("duplicate_dialogue", "duplicate_dialogue"),
            ("lip_audio_sync", "lip_audio_sync"),
        ):
            with self.subTest(analyzer=analyzer):
                evidence = quality_evidence(
                    checks=passing_checks(),
                    metrics=passing_metrics(),
                )
                broken = {name: dict(value) for name, value in evidence.analyzers.items()}
                broken[analyzer]["output_sha256"] = "d" * 64
                verdict = verify_quality_evidence(CaseEvidence(
                    checks=evidence.checks,
                    metrics=evidence.metrics,
                    analyzers=broken,
                    output_sha256=evidence.output_sha256,
                    lip_sync_applicable=True,
                ))
                self.assertIn(f"quality_evidence_failed:{check}", verdict.blockers)

    def test_loader_hashes_real_analyzer_artifacts_and_supports_audited_lip_na(self) -> None:
        with tempfile.TemporaryDirectory() as directory_text:
            directory = Path(directory_text)
            artifacts = write_analyzer_artifacts(
                directory,
                DEFAULT_OUTPUT_SHA256,
                lip_sync_applicable=False,
            )
            evidence_path = directory / "quality.json"
            evidence_path.write_text(json.dumps({
                "checks": passing_checks(),
                "metrics": passing_metrics(),
                "analyzer_artifacts": artifacts,
                "output_sha256": DEFAULT_OUTPUT_SHA256,
            }), encoding="utf-8")

            evidence = load_quality_evidence(evidence_path)
            verdict = verify_quality_evidence(evidence)

            self.assertFalse(evidence.lip_sync_applicable)
            self.assertNotIn("lip_audio_offset_ms", evidence.metrics)
            self.assertTrue(verdict.passed, verdict.blockers)
            self.assertNotEqual(
                evidence.analyzers["duplicate_dialogue"]["evidence_sha256"],
                "b" * 64,
            )

            duplicate_path = directory / artifacts["duplicate_dialogue"]
            duplicate = json.loads(duplicate_path.read_text(encoding="utf-8"))
            duplicate["analyzer_version"] = "9.9.9"
            duplicate_path.write_text(json.dumps(duplicate), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "analyzer_identity_invalid"):
                load_quality_evidence(evidence_path)

    def test_ffmpeg_silence_detection_overrides_claimed_clean_metric(self) -> None:
        def runner(command, **_kwargs):
            if command[0] == "ffprobe" and "-show_packets" in command:
                return Result(stdout=json.dumps(packet_payload()))
            if command[0] == "ffprobe" and "-show_frames" in command:
                return Result(stdout=json.dumps(frame_payload()))
            if command[0] == "ffprobe":
                return Result(stdout=json.dumps(good_probe_payload()))
            if "-af" in command:
                return Result(stderr=(
                    "silence_duration: 0.700\n"
                    '{"input_i":"-16.1","input_tp":"-1.2"}'
                ))
            return Result()

        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "silence.mp4"
            media.write_bytes(b"fixture")
            probe = probe_final_output(media, process_runner=runner)

        self.assertFalse(probe.checks["abnormal_silence"])
        self.assertEqual(probe.metrics["abnormal_silence_count"], 1)

    def test_probe_failure_is_evidence_not_an_exception_or_implicit_pass(self) -> None:
        def runner(_command, **_kwargs):
            return Result(returncode=1, stderr="invalid media")

        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "broken.mp4"
            media.write_bytes(b"broken")
            probe = probe_final_output(media, process_runner=runner)

        self.assertFalse(any(probe.checks.values()))
        self.assertIn("probe_failed", probe.errors)

    def test_aggregate_requires_every_case_and_preserves_case_blockers(self) -> None:
        summary = aggregate_machine_verdicts(
            [
                MachineVerdict(True, ()),
                MachineVerdict(False, ("quality_evidence_failed:decoded_media",)),
            ]
        )

        self.assertFalse(summary.passed)
        self.assertEqual(summary.total, 2)
        self.assertEqual(summary.passed_count, 1)
        self.assertEqual(summary.failed_count, 1)
        self.assertEqual(summary.blockers, ("case_002:quality_evidence_failed:decoded_media",))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
    def test_declared_local_mp4_fixtures_produce_exact_good_and_bad_verdicts(self) -> None:
        fixture_dir = Path(__file__).parent / "fixtures" / "ai_edit_v3" / "acceptance-media"
        manifest = json.loads((fixture_dir / "manifest.json").read_text(encoding="utf-8"))

        probes = {}
        for name, details in manifest["fixtures"].items():
            fixture = fixture_dir / details["filename"]
            self.assertEqual(hashlib.sha256(fixture.read_bytes()).hexdigest(), details["sha256"])
            probes[name] = probe_final_output(fixture)
        good_checks = {**passing_checks(), **probes["known_good"].checks}
        good = verify_quality_evidence(
            quality_evidence(
                checks=good_checks,
                metrics={**passing_metrics(), **probes["known_good"].metrics},
                output_sha256=str(probes["known_good"].output_sha256),
            )
        )
        bad = verify_quality_evidence(
            quality_evidence(
                checks={**passing_checks(), **probes["known_bad"].checks},
                metrics={**passing_metrics(), **probes["known_bad"].metrics},
                output_sha256=str(probes["known_bad"].output_sha256),
            )
        )

        self.assertTrue(good.passed, good.blockers)
        self.assertEqual(
            bad.blockers,
            (
                "quality_evidence_failed:stream_contract",
                "quality_evidence_failed:av_duration_sync",
                "quality_evidence_failed:integrated_loudness",
            ),
        )

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
    def test_machine_verify_cli_parses_metric_json_and_returns_quality_failure_code(self) -> None:
        fixture_dir = Path(__file__).parent / "fixtures" / "ai_edit_v3" / "acceptance-media"
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory) / "quality.json"
            good_sha = hashlib.sha256((fixture_dir / "known-good.mp4").read_bytes()).hexdigest()
            analyzer_artifacts = write_analyzer_artifacts(Path(directory), good_sha)
            evidence.write_text(
                json.dumps({
                    "checks": passing_checks(),
                    "metrics": passing_metrics(),
                    "analyzer_artifacts": analyzer_artifacts,
                    "output_sha256": good_sha,
                }),
                encoding="utf-8",
            )
            self.assertEqual(
                main([
                    "machine-verify", "--media", str(fixture_dir / "known-good.mp4"),
                    "--evidence", str(evidence),
                ]),
                0,
            )
            bad_sha = hashlib.sha256((fixture_dir / "known-bad.mp4").read_bytes()).hexdigest()
            analyzer_artifacts = write_analyzer_artifacts(Path(directory), bad_sha)
            evidence.write_text(
                json.dumps({
                    "checks": passing_checks(),
                    "metrics": passing_metrics(),
                    "analyzer_artifacts": analyzer_artifacts,
                    "output_sha256": bad_sha,
                }),
                encoding="utf-8",
            )
            self.assertEqual(
                main([
                    "machine-verify", "--media", str(fixture_dir / "known-bad.mp4"),
                    "--evidence", str(evidence),
                ]),
                3,
            )

            evidence.write_text("not-json", encoding="utf-8")
            self.assertEqual(
                main([
                    "machine-verify", "--media", str(fixture_dir / "known-good.mp4"),
                    "--evidence", str(evidence),
                ]),
                4,
            )


if __name__ == "__main__":
    unittest.main()
