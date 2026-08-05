import json
import tempfile
import unittest
from pathlib import Path
from contextlib import redirect_stdout
from io import StringIO

from scripts.ai_edit_v3_acceptance import main, validate_matrix


ROOT = Path(__file__).resolve().parents[1]
FROZEN_MATRIX = ROOT / "tests/fixtures/ai_edit_v3/acceptance-20.json"


class AcceptanceMatrixTests(unittest.TestCase):
    def test_matrix_reports_the_single_missing_input_mode_pair(self) -> None:
        inputs = (
            "platform_talking_head",
            "uploaded_video",
            "existing_audio",
            "uploaded_audio",
            "script_to_audio_video",
        )
        modes = ("ai_auto", "style_prompt", "template_reference")
        omitted = ("uploaded_audio", "template_reference")
        cases = [
            {
                "case_id": f"case_{index:02d}",
                "input_type": input_type,
                "creation_mode": mode,
            }
            for index, (input_type, mode) in enumerate(
                (
                    (input_type, mode)
                    for input_type in inputs
                    for mode in modes
                    if (input_type, mode) != omitted
                ),
                start=1,
            )
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "matrix.json"
            path.write_text(json.dumps({"cases": cases}), encoding="utf-8")
            report = validate_matrix(path)

        self.assertFalse(report.passed)
        self.assertEqual(report.missing_pairs, (omitted,))

    def test_frozen_matrix_has_twenty_authorized_balanced_cases(self) -> None:
        report = validate_matrix(FROZEN_MATRIX)
        document = json.loads(FROZEN_MATRIX.read_text(encoding="utf-8"))

        self.assertTrue(report.passed, report.errors)
        self.assertEqual(20, report.case_count)
        video_inputs = {"platform_talking_head", "uploaded_video"}
        self.assertEqual(10, sum(
            case["input_type"] in video_inputs for case in document["cases"]
        ))
        self.assertEqual(10, sum(
            case["input_type"] not in video_inputs for case in document["cases"]
        ))
        templates = [case for case in document["cases"] if case["creation_mode"] == "template_reference"]
        self.assertGreaterEqual(sum(case["ratio"] == "16:9" for case in templates), 3)
        self.assertGreaterEqual(sum(case["ratio"] == "9:16" for case in templates), 3)
        self.assertEqual(4, len({case["template_id"] for case in templates}))

    def test_validator_rejects_absolute_query_or_secret_like_authority(self) -> None:
        document = json.loads(FROZEN_MATRIX.read_text(encoding="utf-8"))
        for value in (
            "C:/private/source.mp4",
            "https://example.invalid/a.mp4?token=secret",
            "sk-proj-this-must-never-be-stored",
        ):
            mutated = json.loads(json.dumps(document))
            mutated["cases"][0]["source"]["alias"] = value
            with self.subTest(value=value), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "matrix.json"
                path.write_text(json.dumps(mutated), encoding="utf-8")
                report = validate_matrix(path)
                self.assertFalse(report.passed)
                self.assertIn("case_01:source.alias:unsafe", report.errors)

    def test_validator_rejects_root_secret_and_broken_authorization_chain(self) -> None:
        document = json.loads(FROZEN_MATRIX.read_text(encoding="utf-8"))
        mutations = (
            ("root", "sk-proj-topsecretvalue"),
            ("source", "different-approval"),
            ("material", "different-approval"),
        )
        for target, value in mutations:
            mutated = json.loads(json.dumps(document))
            if target == "root":
                mutated["authorization_ref"] = value
            elif target == "source":
                mutated["cases"][0]["source"]["authorization_ref"] = value
            else:
                mutated["cases"][1]["materials"][0]["authorization_ref"] = value
            with self.subTest(target=target), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "acceptance-20.json"
                schema = FROZEN_MATRIX.with_name("acceptance-20.schema.json")
                path.write_text(json.dumps(mutated), encoding="utf-8")
                path.with_name(schema.name).write_text(schema.read_text(encoding="utf-8"), encoding="utf-8")
                report = validate_matrix(path)
                self.assertFalse(report.passed)
                self.assertTrue(any("authorization" in error for error in report.errors))

    def test_validator_binds_input_types_and_risk_tags_to_media_facts(self) -> None:
        document = json.loads(FROZEN_MATRIX.read_text(encoding="utf-8"))
        mutations = []
        wrong_media = json.loads(json.dumps(document))
        wrong_media["cases"][0]["source"]["media_type"] = "audio/mpeg"
        mutations.append((wrong_media, "case_01:source.media_type:input_mismatch"))
        no_complete_materials = json.loads(json.dumps(document))
        no_complete_materials["cases"][1]["materials"] = []
        mutations.append((no_complete_materials, "case_02:risk_tags:complete_images_unproven"))
        no_mismatch = json.loads(json.dumps(document))
        no_mismatch["cases"][3]["materials"][0]["intrinsic_ratio"] = "9:16"
        mutations.append((no_mismatch, "case_04:risk_tags:ratio_mismatch_unproven"))
        no_identity_fact = json.loads(json.dumps(document))
        no_identity_fact["cases"][2]["materials"][0]["person_role"] = "reference_person"
        mutations.append((no_identity_fact, "case_03:risk_tags:unrelated_person_material_unproven"))
        cross_owner = json.loads(json.dumps(document))
        cross_owner["cases"][1]["materials"][0]["owner_alias"] = "acceptance_owner_other"
        mutations.append((cross_owner, "case_02:materials[1].owner_alias:chain_mismatch"))
        not_talking_head = json.loads(json.dumps(document))
        not_talking_head["cases"][0]["talking_head_kind"] = None
        mutations.append((not_talking_head, "case_01:talking_head_kind:required"))
        for mutated, expected in mutations:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / "acceptance-20.json"
                schema = FROZEN_MATRIX.with_name("acceptance-20.schema.json")
                path.write_text(json.dumps(mutated), encoding="utf-8")
                path.with_name(schema.name).write_text(schema.read_text(encoding="utf-8"), encoding="utf-8")
                report = validate_matrix(path)
                self.assertFalse(report.passed)
                self.assertIn(expected, report.errors)

    def test_validator_fails_closed_when_schema_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "acceptance-20.json"
            path.write_text(FROZEN_MATRIX.read_text(encoding="utf-8"), encoding="utf-8")
            report = validate_matrix(path)
        self.assertFalse(report.passed)
        self.assertIn("schema:missing", report.errors)

    def test_validator_fails_closed_when_schema_is_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "acceptance-20.json"
            path.write_text(FROZEN_MATRIX.read_text(encoding="utf-8"), encoding="utf-8")
            path.with_name("acceptance-20.schema.json").write_text("{not-json", encoding="utf-8")
            report = validate_matrix(path)
        self.assertFalse(report.passed)
        self.assertIn("schema:invalid", report.errors)

    def test_validate_cli_returns_nonzero_for_an_incomplete_matrix(self) -> None:
        document = json.loads(FROZEN_MATRIX.read_text(encoding="utf-8"))
        document["cases"].pop()
        output = StringIO()
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "acceptance-20.json"
            schema = FROZEN_MATRIX.with_name("acceptance-20.schema.json")
            path.write_text(json.dumps(document), encoding="utf-8")
            path.with_name(schema.name).write_text(schema.read_text(encoding="utf-8"), encoding="utf-8")
            with redirect_stdout(output):
                exit_code = main(["validate", "--matrix", str(path)])
        self.assertEqual(1, exit_code)
        self.assertIn("invalid matrix", output.getvalue())

    def test_validate_cli_prints_exact_success_for_the_frozen_matrix(self) -> None:
        output = StringIO()
        with redirect_stdout(output):
            exit_code = main(["validate", "--matrix", str(FROZEN_MATRIX)])
        self.assertEqual(0, exit_code)
        self.assertEqual("20 cases; 15/15 input-mode pairs; valid\n", output.getvalue())


if __name__ == "__main__":
    unittest.main()
