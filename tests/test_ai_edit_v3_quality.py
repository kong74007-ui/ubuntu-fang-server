from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import time
import unittest

from server.content_domains.ai_edit_v3.media import FinalMux
from server.content_domains.ai_edit_v3.quality import run_blocking_quality


ROOT = Path(__file__).resolve().parents[1]


class _Inspector:
    def __init__(self, verdict):
        self.verdict = verdict
        self.calls = 0

    def inspect(self, **_kwargs):
        self.calls += 1
        return copy.deepcopy(self.verdict)


class BlockingQualityTests(unittest.TestCase):
    def setUp(self):
        self.verdict = json.loads((ROOT / "tests/fixtures/ai_edit_v3/valid-quality-verdict-v1.json").read_text(encoding="utf-8"))
        self.mux = FinalMux("final.mp4", "1" * 64, 4000, "h264", "aac", 1920, 1080, 30, 1, 48000, 2, {
            "decode_ok": True, "video_start_ms": 0, "audio_start_ms": 0,
            "black_max_ms": 0, "freeze_max_ms": 0, "speech_silence_max_ms": 0,
            "true_peak_dbfs": -1.1, "integrated_lufs": -16.0, "audio_fingerprint_unique": True,
        })
        self.manifest = {
            "duration_ms": 4000, "output_spec": {"width": 1920, "height": 1080, "fps_num": 30, "fps_den": 1},
            "captions": [{"text": "准确文案", "start_ms": 0, "end_ms": 4000}],
            "assets": [{"id": "asset_1", "sha256": "2" * 64, "provenance": {"owner": "owner_1", "task_id": "job_1"}}],
        }
        self.render_report = {"status": "done", "output": {"silent": True}, "audit": {"audio_elements": 0, "audible_video_elements": 0}}
        self.owner = {"owner": "owner_1", "job_id": "job_1", "asset_hashes": {"asset_1": "2" * 64}}

    def test_valid_technical_content_and_visual_evidence_pass(self):
        report = run_blocking_quality(self.mux, self.manifest, self.render_report, owner_evidence=self.owner, visual_inspector=_Inspector(self.verdict), deadline_at=time.time() + 10)
        self.assertTrue(report.passed)
        self.assertEqual(len(report.findings), 12)
        self.assertEqual(report.repairable_ids, ())
        self.assertRegex(report.report_sha256, r"^[0-9a-f]{64}$")

    def test_cross_owner_material_and_unknown_visual_verdict_fail_closed(self):
        self.owner["owner"] = "other_owner"
        visual = next(item for item in self.verdict["checks"] if item["check_id"] == "safe_area_and_text_visibility")
        visual["result"] = "unknown"
        visual["evidence"] = []
        report = run_blocking_quality(self.mux, self.manifest, self.render_report, owner_evidence=self.owner, visual_inspector=_Inspector(self.verdict), deadline_at=time.time() + 10)
        self.assertFalse(report.passed)
        failed = {finding.check_id for finding in report.findings if finding.status != "pass"}
        self.assertIn("material_provenance", failed)
        self.assertIn("safe_area_and_text_visibility", failed)

    def test_visual_inspector_is_called_once_and_invalid_output_cannot_be_repaired_by_reprompt(self):
        inspector = _Inspector({"bad": True})
        report = run_blocking_quality(self.mux, self.manifest, self.render_report, owner_evidence=self.owner, visual_inspector=inspector, deadline_at=time.time() + 10)
        self.assertFalse(report.passed)
        self.assertEqual(inspector.calls, 1)


if __name__ == "__main__":
    unittest.main()
