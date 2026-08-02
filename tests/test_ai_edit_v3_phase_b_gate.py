from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.ai_edit_v3_phase_b_gate import validate_phase_b_cases


ROOT = Path(__file__).resolve().parents[1]


class PhaseBGateContractTests(unittest.TestCase):
    def test_gate_reports_the_exact_missing_required_outcome(self) -> None:
        payload = {
            "cases": [
                {"id": "c1", "outcome": "all_input_types", "input_type": "platform_talking_head", "creation_mode": "ai_open"},
                {"id": "c2", "outcome": "all_creation_modes", "input_type": "uploaded_video", "creation_mode": "style_prompt"},
                {"id": "c3", "outcome": "authoritative_text", "input_type": "existing_audio", "creation_mode": "platform_template"},
                {"id": "c4", "outcome": "punctuation_only", "input_type": "uploaded_audio", "creation_mode": "ai_open"},
                {"id": "c5", "outcome": "material_matching", "input_type": "script_to_audio_video", "creation_mode": "ai_open"},
                {"id": "c6", "outcome": "injection_rejected", "input_type": "platform_talking_head", "creation_mode": "ai_open"},
                {"id": "c7", "outcome": "invalid_model_rejected", "input_type": "platform_talking_head", "creation_mode": "ai_open"},
            ]
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cases.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            report = validate_phase_b_cases(path)

        self.assertFalse(report.passed)
        self.assertEqual(report.missing, ("required_material_failure",))

    def test_complete_fixture_passes_and_duplicate_ids_fail(self) -> None:
        fixture = ROOT / "tests" / "fixtures" / "ai_edit_v3" / "phase-b-cases.json"
        report = validate_phase_b_cases(fixture)
        self.assertTrue(report.passed)
        self.assertEqual(report.missing, ())
        payload = json.loads(fixture.read_text(encoding="utf-8"))
        payload["cases"].append(dict(payload["cases"][0]))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "duplicate.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "phase_b_case_id_duplicate"):
                validate_phase_b_cases(path)


if __name__ == "__main__":
    unittest.main()
