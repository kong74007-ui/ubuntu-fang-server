from __future__ import annotations

import copy
from dataclasses import replace
import hashlib
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
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        snapshot_path = Path(temporary.name) / "frame.png"
        snapshot_path.write_bytes(b"verified-render-frame")
        snapshot_sha256 = hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
        for check in self.verdict["checks"]:
            for evidence in check["evidence"]:
                evidence["frame_sha256"] = snapshot_sha256
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
        self.snapshots = ({
            "local_path": snapshot_path.resolve(),
            "frame_sha256": snapshot_sha256,
            "timestamp_ms": 1000,
            "size_bytes": snapshot_path.stat().st_size,
        },)

    def test_valid_technical_content_and_visual_evidence_pass(self):
        report = run_blocking_quality(self.mux, self.manifest, self.render_report, owner_evidence=self.owner, visual_inspector=_Inspector(self.verdict), snapshot_inputs=self.snapshots, deadline_at=time.time() + 10)
        self.assertTrue(report.passed)
        self.assertEqual(len(report.findings), 12)
        self.assertEqual(report.repairable_ids, ())
        self.assertRegex(report.report_sha256, r"^[0-9a-f]{64}$")

    def test_cross_owner_material_and_unknown_visual_verdict_fail_closed(self):
        self.owner["owner"] = "other_owner"
        visual = next(item for item in self.verdict["checks"] if item["check_id"] == "safe_area_and_text_visibility")
        visual["result"] = "unknown"
        visual["evidence"] = []
        report = run_blocking_quality(self.mux, self.manifest, self.render_report, owner_evidence=self.owner, visual_inspector=_Inspector(self.verdict), snapshot_inputs=self.snapshots, deadline_at=time.time() + 10)
        self.assertFalse(report.passed)
        failed = {finding.check_id for finding in report.findings if finding.status != "pass"}
        self.assertIn("material_provenance", failed)
        self.assertIn("safe_area_and_text_visibility", failed)

    def test_visual_inspector_is_called_once_and_invalid_output_cannot_be_repaired_by_reprompt(self):
        inspector = _Inspector({"bad": True})
        report = run_blocking_quality(self.mux, self.manifest, self.render_report, owner_evidence=self.owner, visual_inspector=inspector, snapshot_inputs=self.snapshots, deadline_at=time.time() + 10)
        self.assertFalse(report.passed)
        self.assertEqual(inspector.calls, 1)

    def test_visual_evidence_not_bound_to_verified_snapshot_fails_closed(self):
        self.verdict["checks"][0]["evidence"][0]["frame_sha256"] = "7" * 64
        report = run_blocking_quality(
            self.mux,
            self.manifest,
            self.render_report,
            owner_evidence=self.owner,
            visual_inspector=_Inspector(self.verdict),
            snapshot_inputs=self.snapshots,
            deadline_at=time.time() + 10,
        )
        self.assertFalse(report.passed)
        self.assertTrue(all(
            finding.status == "unknown"
            for finding in report.findings
            if finding.executor["kind"] == "visual_model"
        ))

    def test_snapshot_changed_during_visual_inspection_fails_closed(self):
        class MutatingInspector(_Inspector):
            def inspect(inner_self, **kwargs):
                kwargs["snapshots"][0]["local_path"].write_bytes(b"tampered")
                return super(MutatingInspector, inner_self).inspect(**kwargs)

        report = run_blocking_quality(
            self.mux,
            self.manifest,
            self.render_report,
            owner_evidence=self.owner,
            visual_inspector=MutatingInspector(self.verdict),
            snapshot_inputs=self.snapshots,
            deadline_at=time.time() + 10,
        )

        self.assertFalse(report.passed)
        self.assertTrue(all(
            finding.status == "unknown"
            for finding in report.findings
            if finding.executor["kind"] == "visual_model"
        ))

    def test_visual_inspector_cannot_rebind_verified_snapshot_descriptor(self):
        replacement_path = self.snapshots[0]["local_path"].with_name("other.png")
        replacement_path.write_bytes(b"different-render-frame")
        replacement_sha256 = hashlib.sha256(replacement_path.read_bytes()).hexdigest()

        class DescriptorMutatingInspector(_Inspector):
            def inspect(inner_self, **kwargs):
                kwargs["snapshots"][0]["local_path"] = replacement_path
                kwargs["snapshots"][0]["frame_sha256"] = replacement_sha256
                return super(DescriptorMutatingInspector, inner_self).inspect(**kwargs)

        report = run_blocking_quality(
            self.mux,
            self.manifest,
            self.render_report,
            owner_evidence=self.owner,
            visual_inspector=DescriptorMutatingInspector(self.verdict),
            snapshot_inputs=self.snapshots,
            deadline_at=time.time() + 10,
        )

        self.assertFalse(report.passed)
        self.assertTrue(all(
            finding.status == "unknown"
            for finding in report.findings
            if finding.executor["kind"] == "visual_model"
        ))

    def test_repair_is_forbidden_when_any_blocking_failure_is_not_repairable(self):
        safe_area = next(
            item for item in self.verdict["checks"]
            if item["check_id"] == "safe_area_and_text_visibility"
        )
        safe_area.update({"result": "fail", "repairable": True})
        caption = next(
            item for item in self.verdict["checks"]
            if item["check_id"] == "caption_fact_accuracy"
        )
        caption.update({"result": "unknown", "repairable": False, "evidence": []})

        report = run_blocking_quality(
            self.mux,
            self.manifest,
            self.render_report,
            owner_evidence=self.owner,
            visual_inspector=_Inspector(self.verdict),
            snapshot_inputs=self.snapshots,
            deadline_at=time.time() + 10,
        )

        self.assertFalse(report.passed)
        self.assertEqual(("safe_area_and_text_visibility",), report.repairable_ids)
        self.assertFalse(report.can_repair)

    def test_unsupported_technical_failure_never_enters_visual_repair(self):
        self.mux = replace(
            self.mux,
            audit={**self.mux.audit, "black_max_ms": 500},
        )
        report = run_blocking_quality(
            self.mux,
            self.manifest,
            self.render_report,
            owner_evidence=self.owner,
            visual_inspector=_Inspector(self.verdict),
            snapshot_inputs=self.snapshots,
            deadline_at=time.time() + 10,
        )

        self.assertFalse(report.passed)
        self.assertEqual((), report.repairable_ids)
        self.assertFalse(report.can_repair)

    def test_short_safe_area_failure_does_not_enter_unexecutable_repair(self):
        self.manifest["compositions"] = [
            {
                "id": "composition_001", "scene_id": "scene_01",
                "start_ms": 0, "end_ms": 2000,
            },
            {
                "id": "composition_002", "scene_id": "scene_02",
                "start_ms": 2000, "end_ms": 4000,
            },
        ]
        safe_area = next(
            item for item in self.verdict["checks"]
            if item["check_id"] == "safe_area_and_text_visibility"
        )
        safe_area.update({"result": "fail", "repairable": True})

        report = run_blocking_quality(
            self.mux,
            self.manifest,
            self.render_report,
            owner_evidence=self.owner,
            visual_inspector=_Inspector(self.verdict),
            snapshot_inputs=self.snapshots,
            deadline_at=time.time() + 10,
        )

        self.assertFalse(report.passed)
        self.assertFalse(report.can_repair)
        self.assertEqual((), report.repair_directives)

    def test_repair_directive_maps_executable_face_fallback_to_exact_scene(self):
        self.manifest["source_video"] = {"path": "media/source.mp4", "silent": True}
        self.manifest["assets"][0]["kind"] = "image"
        self.manifest["captions"] = [
            {"id": "caption_01", "text": "One", "start_ms": 0, "end_ms": 2000},
            {"id": "caption_02", "text": "Two", "start_ms": 2000, "end_ms": 4000},
        ]
        self.manifest["compositions"] = [
            {
                "id": "composition_001", "scene_id": "scene_01",
                "start_ms": 0, "end_ms": 2000,
                "layout_id": "product_hero", "asset_ids": ["asset_1"],
            },
            {
                "id": "composition_002", "scene_id": "scene_02",
                "start_ms": 2000, "end_ms": 4000,
                "layout_id": "speaker_fullscreen", "asset_ids": [],
            },
        ]
        face = next(
            item for item in self.verdict["checks"]
            if item["check_id"] == "face_product_obstruction"
        )
        face.update({"result": "fail", "repairable": True})

        report = run_blocking_quality(
            self.mux,
            self.manifest,
            self.render_report,
            owner_evidence=self.owner,
            visual_inspector=_Inspector(self.verdict),
            snapshot_inputs=self.snapshots,
            deadline_at=time.time() + 10,
        )

        self.assertTrue(report.can_repair)
        self.assertEqual(({
            "scene_id": "scene_01",
            "reason_code": "face_product_obstruction",
            "allowed_action": "speaker_fallback",
        },), tuple(dict(item) for item in report.repair_directives))
        from server.content_domains.ai_edit_v3.production import (
            _freeze_repair_instruction,
            _repair_render_manifest,
        )
        instruction = _freeze_repair_instruction(
            self.manifest,
            report.repair_directives,
        )
        repaired = _repair_render_manifest(self.manifest, instruction)
        self.assertEqual("speaker_fullscreen", repaired["compositions"][0]["layout_id"])

    def test_audio_only_scene_cannot_enter_speaker_fallback_repair(self):
        self.manifest["compositions"] = [{
            "id": "composition_001", "scene_id": "scene_01",
            "start_ms": 0, "end_ms": 4000,
            "layout_id": "product_hero", "asset_ids": ["asset_1"],
        }]
        face = next(
            item for item in self.verdict["checks"]
            if item["check_id"] == "face_product_obstruction"
        )
        face.update({"result": "fail", "repairable": True})

        report = run_blocking_quality(
            self.mux,
            self.manifest,
            self.render_report,
            owner_evidence=self.owner,
            visual_inspector=_Inspector(self.verdict),
            snapshot_inputs=self.snapshots,
            deadline_at=time.time() + 10,
        )

        self.assertFalse(report.can_repair)
        self.assertEqual((), report.repair_directives)

    def test_repair_is_forbidden_when_failure_cannot_map_to_a_scene(self):
        safe_area = next(
            item for item in self.verdict["checks"]
            if item["check_id"] == "safe_area_and_text_visibility"
        )
        safe_area.update({"result": "fail", "repairable": True})

        report = run_blocking_quality(
            self.mux,
            self.manifest,
            self.render_report,
            owner_evidence=self.owner,
            visual_inspector=_Inspector(self.verdict),
            snapshot_inputs=self.snapshots,
            deadline_at=time.time() + 10,
        )

        self.assertFalse(report.can_repair)
        self.assertEqual((), report.repair_directives)


if __name__ == "__main__":
    unittest.main()
