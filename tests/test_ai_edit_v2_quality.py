import json
import unittest

from server.content_domains.ai_edit_v2_quality import LocalQualityRunner, inspect_output


PLAN = {
    "aspect_ratio": "16:9",
    "duration_ms": 10_000,
    "target_duration_ms": 10_000,
    "required_materials": [{"asset_id": "required-product"}],
    "text_timeline": {"text": "黄雀套餐价格100元"},
}


def passing_evidence():
    return {
        "probe": {"video": True, "audio": True, "width": 1920, "height": 1080,
                  "rotation": 0, "duration_ms": 10_080},
        "decode_video": {"decodable": True},
        "decode_audio": {"decodable": True},
        "frames": {"black_ratio": 0.0, "blank_ratio": 0.0},
        "captions": {"safe_area": True, "tofu_count": 0, "missing_glyphs": []},
        "materials": {"covered_asset_ids": ["required-product"]},
        "transcript": {"source_matches": True, "facts_match": True},
        "audio": {"silence_ratio": 0.0, "true_peak_dbfs": -1.0,
                  "dialogue_to_bgm_db": 8.0, "dialogue_to_sfx_db": 10.0},
    }


class EvidenceRunner:
    def __init__(self, evidence):
        self.evidence = evidence
        self.calls = []

    def __call__(self, check, *, path, resolved_plan):
        self.calls.append(check)
        return self.evidence[check]


class QualityTests(unittest.TestCase):
    def test_local_runner_uses_only_real_ffprobe_and_ffmpeg_commands(self):
        evidence = passing_evidence()
        plan = {**PLAN, "quality_analysis": {
            key: evidence[key] for key in ("captions", "materials", "transcript", "audio")
        }}
        calls = []

        class Result:
            def __init__(self, payload=None, stderr=b"", returncode=0):
                self.returncode = returncode
                self.stdout = json.dumps(payload).encode() if payload is not None else b""
                self.stderr = stderr

        def process_runner(command, **kwargs):
            calls.append(command)
            self.assertNotIn("resolved_plan", kwargs)
            if command[0] == "ffprobe":
                return Result({
                    "format": {"duration": "10.08"},
                    "streams": [
                        {"codec_type": "video", "width": 1920, "height": 1080,
                         "tags": {"rotate": "0"}},
                        {"codec_type": "audio"},
                    ],
                })
            if command[0] == "ffmpeg":
                return Result(stderr=b"")
            self.fail(f"unexpected executable: {command[0]}")

        report = inspect_output("final.mp4", plan, LocalQualityRunner(process_runner))

        self.assertTrue(report.passed, report.error_codes)
        self.assertTrue(any(isinstance(command, list) and command[0] == "ffprobe" for command in calls))
        self.assertGreaterEqual(sum(
            isinstance(command, list) and command[0] == "ffmpeg" for command in calls
        ), 3)
        self.assertFalse(any(command[0] == "ai-edit-v2-quality-inspect" for command in calls))

    def test_nan_and_infinity_evidence_fail_closed(self):
        for invalid in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(invalid=invalid):
                evidence = passing_evidence()
                evidence["frames"]["black_ratio"] = invalid
                report = inspect_output("final.mp4", PLAN, EvidenceRunner(evidence))
                self.assertFalse(report.passed)
                self.assertIn("inspection_incomplete", report.error_codes)
                self.assertTrue(report.terminal)

    def test_all_hard_gates_pass_with_complete_evidence(self):
        runner = EvidenceRunner(passing_evidence())
        report = inspect_output("final.mp4", PLAN, runner)

        self.assertTrue(report.passed)
        self.assertEqual(report.error_codes, ())
        self.assertEqual(set(runner.calls), {
            "probe", "decode_video", "decode_audio", "frames", "captions",
            "materials", "transcript", "audio",
        })

    def test_caption_tofu_or_out_of_bounds_fails_quality(self):
        evidence = passing_evidence()
        evidence["captions"] = {
            "safe_area": False, "tofu_count": 2, "missing_glyphs": ["雀"]
        }
        report = inspect_output("bad-caption.mp4", PLAN, EvidenceRunner(evidence))

        self.assertFalse(report.passed)
        self.assertIn("caption_invalid", report.error_codes)
        self.assertIn("caption_out_of_safe_area", report.error_codes)
        self.assertIn("caption_tofu_detected", report.error_codes)
        self.assertIn("caption_glyph_missing", report.error_codes)
        self.assertTrue(report.repairable)
        self.assertEqual(report.failing_layers, ("captions",))

    def test_unplayable_video_is_terminal(self):
        evidence = passing_evidence()
        evidence["decode_video"] = {"decodable": False}
        report = inspect_output("broken.mp4", PLAN, EvidenceRunner(evidence))

        self.assertFalse(report.passed)
        self.assertIn("video_unplayable", report.error_codes)
        self.assertTrue(report.terminal)
        self.assertFalse(report.repairable)

    def test_content_and_audio_checks_emit_stable_codes(self):
        evidence = passing_evidence()
        evidence["materials"] = {"covered_asset_ids": []}
        evidence["transcript"] = {"source_matches": False, "facts_match": False}
        evidence["audio"] = {"silence_ratio": 0.4, "true_peak_dbfs": 0.2,
                             "dialogue_to_bgm_db": 1.0, "dialogue_to_sfx_db": 2.0}
        report = inspect_output("bad.mp4", PLAN, EvidenceRunner(evidence))

        self.assertEqual(set(report.error_codes), {
            "required_material_missing", "caption_source_mismatch",
            "caption_facts_mismatch", "audio_silence_detected",
            "audio_clipping_detected", "dialogue_bgm_imbalance",
            "dialogue_sfx_imbalance",
        })
        self.assertTrue(report.terminal)

    def test_dimensions_rotation_duration_and_frames_are_hard_gates(self):
        evidence = passing_evidence()
        evidence["probe"].update({"width": 1080, "height": 1920, "rotation": 90,
                                  "duration_ms": 11_000})
        evidence["frames"] = {"black_ratio": 0.2, "blank_ratio": 0.1}
        report = inspect_output("wrong.mp4", PLAN, EvidenceRunner(evidence))

        self.assertEqual(set(report.error_codes), {
            "output_dimensions_invalid", "output_rotation_invalid",
            "output_duration_mismatch", "black_frames_detected",
            "blank_frames_detected",
        })
        self.assertTrue(report.repairable)


if __name__ == "__main__":
    unittest.main()
