import json
import os
import unittest
from unittest.mock import patch

from server.content_domains import ai_edit_v2_quality as quality
from server.content_domains import ai_edit_v2_runtime as runtime
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


ANALYZER_CAPABILITIES = {
    "captions_ocr": True, "glyphs": True, "materials": True,
    "transcript_facts": True, "audio": True,
}


class EvidenceRunner:
    def __init__(self, evidence):
        self.evidence = evidence
        self.calls = []

    def __call__(self, check, *, path, resolved_plan):
        self.calls.append(check)
        return self.evidence[check]


class QualityTests(unittest.TestCase):
    def test_production_readiness_discovers_quality_binaries_from_current_path(self):
        def analyzer(*_args, **_kwargs): return {}
        analyzer.capabilities = lambda: dict(ANALYZER_CAPABILITIES)
        discovered = {"ffmpeg": "/usr/bin/ffmpeg", "ffprobe": "/usr/bin/ffprobe"}
        with patch.dict(os.environ, {"PATH": "/ci/system/bin"}, clear=True), \
             patch.object(quality.shutil, "which", side_effect=lambda name: discovered.get(name)):
            runner = LocalQualityRunner(lambda *_a, **_k: None, analyzer=analyzer)
            runtime.assert_production_ready({"readiness_errors": runner.readiness_errors})

    def test_bad_explicit_quality_binary_never_falls_back_to_path(self):
        def analyzer(*_args, **_kwargs): return {}
        analyzer.capabilities = lambda: dict(ANALYZER_CAPABILITIES)
        explicit = "/missing/quality-ffmpeg"
        with patch.dict(os.environ, {
            "AI_EDIT_V2_QUALITY_FFMPEG_BIN": explicit,
            "PATH": "/ci/system/bin",
        }, clear=True):
            runner = LocalQualityRunner(
                lambda *_a, **_k: None, analyzer=analyzer,
                binary_finder=lambda name: None if name == explicit else f"/usr/bin/{name}",
            )
            with self.assertRaisesRegex(RuntimeError, "ffmpeg"):
                runtime.assert_production_ready({"readiness_errors": runner.readiness_errors})

    def test_local_runner_uses_only_real_ffprobe_and_ffmpeg_commands(self):
        evidence = passing_evidence()
        plan = {**PLAN, "quality_analysis": {"captions": {"safe_area": False}}}
        analyzed = []

        def analyzer(check, *, path, expected):
            analyzed.append((check, path, expected))
            return evidence[check]
        analyzer.capabilities = lambda: dict(ANALYZER_CAPABILITIES)
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

        report = inspect_output(
            "final.mp4", plan,
            LocalQualityRunner(process_runner, analyzer=analyzer,
                               binary_finder=lambda _name: "fake-binary"),
        )

        self.assertTrue(report.passed, report.error_codes)
        self.assertTrue(any(isinstance(command, list) and command[0] == "ffprobe" for command in calls))
        self.assertGreaterEqual(sum(
            isinstance(command, list) and command[0] == "ffmpeg" for command in calls
        ), 3)
        self.assertFalse(any(command[0] == "ai-edit-v2-quality-inspect" for command in calls))
        self.assertEqual([item[0] for item in analyzed], ["captions", "materials", "transcript", "audio"])
        self.assertEqual(analyzed[0][1], "final.mp4")

    def test_local_runner_readiness_requires_binaries_and_final_media_analyzer(self):
        runner = LocalQualityRunner(lambda *_a, **_k: None,
                                    binary_finder=lambda name: None if name == "ffprobe" else name)
        self.assertIn("ffprobe", runner.readiness_errors())
        self.assertIn("final_media_analyzer_captions_ocr", runner.readiness_errors())

        class UnavailableAnalyzer:
            def __call__(self, *_a, **_k): return {}
            def capabilities(self): return {**ANALYZER_CAPABILITIES, "glyphs": False}

        runner = LocalQualityRunner(lambda *_a, **_k: None, analyzer=UnavailableAnalyzer(),
                                    binary_finder=lambda name: name)
        self.assertEqual(runner.readiness_errors(), ["final_media_analyzer_glyphs"])

    def test_callable_or_partial_analyzer_is_never_ready(self):
        bare = LocalQualityRunner(lambda *_a, **_k: None,
                                  analyzer=lambda *_a, **_k: {},
                                  binary_finder=lambda name: name)
        self.assertEqual(set(bare.readiness_errors()), {
            "final_media_analyzer_captions_ocr", "final_media_analyzer_glyphs",
            "final_media_analyzer_materials", "final_media_analyzer_transcript_facts",
            "final_media_analyzer_audio",
        })

        class Partial:
            def __call__(self, *_a, **_k): return {}
            def capabilities(self): return {"captions_ocr": True, "glyphs": True}

        partial = LocalQualityRunner(lambda *_a, **_k: None, analyzer=Partial(),
                                     binary_finder=lambda name: name)
        self.assertEqual(set(partial.readiness_errors()), {
            "final_media_analyzer_materials", "final_media_analyzer_transcript_facts",
            "final_media_analyzer_audio",
        })

    def test_non_callable_analyzer_with_all_capabilities_is_never_ready(self):
        class NonCallableAnalyzer:
            def capabilities(self): return dict(ANALYZER_CAPABILITIES)

        runner = LocalQualityRunner(
            lambda *_a, **_k: None,
            analyzer=NonCallableAnalyzer(),
            binary_finder=lambda name: name,
        )
        self.assertEqual(set(runner.readiness_errors()), {
            "final_media_analyzer_captions_ocr", "final_media_analyzer_glyphs",
            "final_media_analyzer_materials", "final_media_analyzer_transcript_facts",
            "final_media_analyzer_audio",
        })

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

    def test_caption_out_of_bounds_is_repairable(self):
        evidence = passing_evidence()
        evidence["captions"] = {
            "safe_area": False, "tofu_count": 0, "missing_glyphs": []
        }
        report = inspect_output("bad-caption.mp4", PLAN, EvidenceRunner(evidence))

        self.assertFalse(report.passed)
        self.assertIn("caption_invalid", report.error_codes)
        self.assertIn("caption_out_of_safe_area", report.error_codes)
        self.assertTrue(report.repairable)
        self.assertFalse(report.terminal)
        self.assertEqual(report.failing_layers, ("captions",))

    def test_caption_tofu_or_missing_glyph_is_terminal(self):
        cases = (
            ({"safe_area": True, "tofu_count": 2, "missing_glyphs": []},
             "caption_tofu_detected"),
            ({"safe_area": True, "tofu_count": 0, "missing_glyphs": ["雀"]},
             "caption_glyph_missing"),
        )
        for captions, expected_code in cases:
            with self.subTest(expected_code=expected_code):
                evidence = passing_evidence()
                evidence["captions"] = captions
                report = inspect_output(
                    "bad-glyph.mp4", PLAN, EvidenceRunner(evidence)
                )

                self.assertFalse(report.passed)
                self.assertIn("caption_invalid", report.error_codes)
                self.assertIn(expected_code, report.error_codes)
                self.assertTrue(report.terminal)
                self.assertFalse(report.repairable)
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
