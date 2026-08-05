import unittest
import tempfile
import json
import os
from pathlib import Path
from unittest.mock import patch

from scripts.ai_edit_v3_acceptance import main

from server.content_domains.ai_edit_v3.acceptance_export import (
    CaseCheckpoint,
    collect_case_evidence,
    load_test_session,
    resume_or_create_case,
    terminal_result_code,
    verify_case_evidence,
    write_json_exclusive,
)


class FakeV3Api:
    def __init__(self) -> None:
        self.created_idempotency_keys: list[str] = []
        self.fetched_job_ids: list[str] = []

    def create_job(self, idempotency_key: str) -> str:
        self.created_idempotency_keys.append(idempotency_key)
        return "unexpected-new-job"

    def get_job(self, job_id: str) -> dict[str, str]:
        self.fetched_job_ids.append(job_id)
        return {"job_id": job_id, "status": "rendering"}


class EvidenceFakeApi:
    def __init__(self, response: dict) -> None:
        self.response = response
        self.operations: list[str] = []
        self.range_url: str | None = None

    def upload_source(self, case: dict) -> dict:
        self.operations.append("upload")
        return {"upload_id": f"upload-{case['case_id']}", "owner_alias": case["source"]["owner_alias"]}

    def quote(self, case: dict, upload: dict) -> dict:
        self.operations.append("quote")
        return dict(self.response["quote"])

    def create_job(self, idempotency_key: str) -> str:
        self.operations.append(f"create:{idempotency_key}")
        return str(self.response["job_id"])

    def get_job(self, job_id: str) -> dict:
        self.operations.append("poll")
        return {"job_id": job_id, "status": self.response["status"]}

    def get_result(self, job_id: str) -> dict:
        self.operations.append("result")
        return json.loads(json.dumps(self.response))

    def verify_range(self, playback_url: str) -> bool:
        self.operations.append("range")
        self.range_url = playback_url
        return True


class AcceptanceRunnerTests(unittest.TestCase):
    def test_restart_uses_persisted_job_and_idempotency_key(self) -> None:
        api = FakeV3Api()
        checkpoint = CaseCheckpoint(
            case_id="case_01",
            idempotency_key="acceptance/run-01/case-01",
            job_id="job-17",
        )

        resumed = resume_or_create_case(checkpoint, api)

        self.assertEqual(resumed.job_id, "job-17")
        self.assertEqual(resumed.idempotency_key, "acceptance/run-01/case-01")
        self.assertEqual(api.created_idempotency_keys, [])
        self.assertEqual(api.fetched_job_ids, ["job-17"])

    def test_run_cli_calls_execute_run_command_once(self) -> None:
        with patch("scripts.ai_edit_v3_acceptance.execute_run_command", return_value=0) as execute:
            exit_code = main([
                "run", "--environment", "local-fake", "--matrix", "matrix.json",
                "--run-id", "run-01", "--concurrency", "5", "--subset", "parallel-5",
            ])
        self.assertEqual(0, exit_code)
        execute.assert_called_once()
        args = execute.call_args.args[0]
        self.assertEqual("local-fake", args.environment)
        self.assertEqual(Path("matrix.json"), args.matrix)
        self.assertEqual("run-01", args.run_id)
        self.assertEqual(5, args.concurrency)
        self.assertEqual("parallel-5", args.subset)

    def test_run_cli_propagates_nonzero_exit(self) -> None:
        with patch("scripts.ai_edit_v3_acceptance.execute_run_command", return_value=4):
            exit_code = main([
                "run", "--environment", "local-fake", "--matrix", "matrix.json",
                "--run-id", "run-02", "--concurrency", "1",
            ])
        self.assertEqual(4, exit_code)

    def test_verify_cli_opens_named_report_and_runs_strict_verification(self) -> None:
        with patch("scripts.ai_edit_v3_acceptance.execute_verify_command", return_value=0) as execute:
            exit_code = main(["verify", "--report", "report.json", "--strict"])
        self.assertEqual(0, exit_code)
        execute.assert_called_once()
        args = execute.call_args.args[0]
        self.assertEqual(Path("report.json"), args.report)
        self.assertTrue(args.strict)

    def test_verify_cli_rejects_missing_or_invalid_evidence(self) -> None:
        from scripts.ai_edit_v3_acceptance import execute_verify_command

        with tempfile.TemporaryDirectory() as folder:
            missing = type("Args", (), {"report": Path(folder) / "missing.json", "strict": True})()
            invalid_path = Path(folder) / "invalid.json"
            invalid_path.write_text("{invalid", encoding="utf-8")
            invalid = type("Args", (), {"report": invalid_path, "strict": True})()
            self.assertEqual(4, execute_verify_command(missing))
            self.assertEqual(4, execute_verify_command(invalid))

    def test_collect_case_evidence_is_immutable_and_drops_signed_url(self) -> None:
        root = Path(__file__).resolve().parents[1]
        response = json.loads((
            root / "tests/fixtures/ai_edit_v3/acceptance-responses/completed.json"
        ).read_text(encoding="utf-8"))
        matrix = json.loads((
            root / "tests/fixtures/ai_edit_v3/acceptance-20.json"
        ).read_text(encoding="utf-8"))
        response["normalized_request_sha256"] = matrix["cases"][0]["source"]["sha256"]
        api = EvidenceFakeApi(response)
        with tempfile.TemporaryDirectory() as folder:
            case_dir = Path(folder) / "case_01"
            evidence = collect_case_evidence(api, matrix["cases"][0], case_dir)
            persisted = (case_dir / "evidence.json").read_text(encoding="utf-8")
            with self.assertRaises(FileExistsError):
                collect_case_evidence(api, matrix["cases"][0], case_dir)

        self.assertEqual("asset-local-01", evidence.asset_id)
        self.assertNotIn("playback_url", persisted)
        self.assertNotIn("token=fake-signed-value", persisted)
        self.assertEqual("https://playback.invalid/final.mp4?token=fake-signed-value", api.range_url)
        self.assertEqual(
            ["upload", "quote", "create:acceptance/local-fake/case_01", "poll", "result", "range"],
            api.operations[:6],
        )

    def test_collect_resumes_persisted_checkpoint_without_upload_quote_or_create(self) -> None:
        root = Path(__file__).resolve().parents[1]
        response = json.loads((
            root / "tests/fixtures/ai_edit_v3/acceptance-responses/completed.json"
        ).read_text(encoding="utf-8"))
        matrix = json.loads((
            root / "tests/fixtures/ai_edit_v3/acceptance-20.json"
        ).read_text(encoding="utf-8"))
        response["normalized_request_sha256"] = matrix["cases"][0]["source"]["sha256"]
        api = EvidenceFakeApi(response)
        with tempfile.TemporaryDirectory() as folder:
            case_dir = Path(folder) / "case_01"
            case_dir.mkdir()
            (case_dir / "checkpoint.json").write_text(json.dumps({
                "case_id": "case_01",
                "idempotency_key": "acceptance/local-fake/case_01",
                "job_id": "job-local-01",
                "normalized_request_sha256": matrix["cases"][0]["source"]["sha256"],
                "upload_id": "upload-case_01",
                "quote": response["quote"],
            }), encoding="utf-8")
            evidence = collect_case_evidence(api, matrix["cases"][0], case_dir)
        self.assertEqual("acceptance/local-fake/case_01", evidence.idempotency_key)
        self.assertEqual(["poll", "poll", "result", "range"], api.operations)

    def test_local_fake_run_writes_twenty_immutable_case_evidence_files(self) -> None:
        root = Path(__file__).resolve().parents[1]
        matrix = root / "tests/fixtures/ai_edit_v3/acceptance-20.json"
        with tempfile.TemporaryDirectory() as folder, patch.dict(
            os.environ, {"AI_EDIT_V3_ACCEPTANCE_OUTPUT_ROOT": folder}
        ):
            argv = [
                "run", "--environment", "local-fake", "--matrix", str(matrix),
                "--run-id", "run-local-01", "--concurrency", "5",
            ]
            self.assertEqual(0, main(argv))
            run_dir = Path(folder) / "run-local-01"
            report_text = (run_dir / "report.json").read_text(encoding="utf-8")
            report = json.loads(report_text)
            self.assertEqual(20, report["case_count"])
            self.assertEqual("completed", report["status"])
            self.assertEqual(20, len(list(run_dir.glob("case_*/evidence.json"))))
            self.assertNotIn("token=", report_text)
            self.assertEqual(0, main([
                "verify", "--report", str(run_dir / "report.json"), "--strict",
            ]))
            evidence_path = run_dir / "case_01/evidence.json"
            tampered = json.loads(evidence_path.read_text(encoding="utf-8"))
            tampered["status"] = "refunded"
            evidence_path.write_text(json.dumps(tampered), encoding="utf-8")
            self.assertEqual(4, main([
                "verify", "--report", str(run_dir / "report.json"), "--strict",
            ]))
            self.assertEqual(4, main(argv))

    def test_failed_local_fake_run_reports_and_verifies_exit_three(self) -> None:
        root = Path(__file__).resolve().parents[1]
        matrix = root / "tests/fixtures/ai_edit_v3/acceptance-20.json"
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
            "AI_EDIT_V3_ACCEPTANCE_OUTPUT_ROOT": folder,
            "AI_EDIT_V3_ACCEPTANCE_FAKE_RESPONSE": "refunded.json",
        }):
            argv = [
                "run", "--environment", "local-fake", "--matrix", str(matrix),
                "--run-id", "run-refunded-01", "--concurrency", "1",
                "--subset", "parallel-5",
            ]
            self.assertEqual(3, main(argv))
            report = Path(folder) / "run-refunded-01/report.json"
            self.assertEqual(3, main(["verify", "--report", str(report), "--strict"]))

    def test_test_environment_stops_before_real_api_construction(self) -> None:
        with patch("scripts.ai_edit_v3_acceptance.build_real_run_api") as build:
            exit_code = main([
                "run", "--environment", "test", "--matrix", "matrix.json",
                "--run-id", "run-test-01", "--concurrency", "1",
            ])
        self.assertEqual(2, exit_code)
        build.assert_not_called()

    def test_local_fake_run_resumes_after_partial_process_failure(self) -> None:
        import scripts.ai_edit_v3_acceptance as command

        root = Path(__file__).resolve().parents[1]
        matrix = root / "tests/fixtures/ai_edit_v3/acceptance-20.json"
        original = command.collect_case_evidence

        def crash_on_second(api, case, run_dir, *, run_id):
            if case["case_id"] == "case_02":
                raise OSError("simulated_process_loss")
            return original(api, case, run_dir, run_id=run_id)

        with tempfile.TemporaryDirectory() as folder, patch.dict(
            os.environ, {"AI_EDIT_V3_ACCEPTANCE_OUTPUT_ROOT": folder}
        ):
            argv = [
                "run", "--environment", "local-fake", "--matrix", str(matrix),
                "--run-id", "run-resume-01", "--concurrency", "1",
            ]
            with patch(
                "server.content_domains.ai_edit_v3.acceptance_export.collect_case_evidence",
                side_effect=crash_on_second,
            ):
                self.assertEqual(4, main(argv))
            run_dir = Path(folder) / "run-resume-01"
            self.assertTrue((run_dir / "case_01/evidence.json").exists())
            self.assertFalse((run_dir / "report.json").exists())
            self.assertEqual(0, main(argv))
            self.assertEqual(20, len(list(run_dir.glob("case_*/evidence.json"))))

    def test_session_is_injected_only_from_environment_or_prompt_and_redacted(self) -> None:
        prompted: list[str] = []
        from_environment = load_test_session(
            {"AI_EDIT_V3_TEST_SESSION": "session-from-env"},
            lambda prompt: prompted.append(prompt) or "unexpected",
        )
        self.assertEqual("session-from-env", from_environment.reveal())
        self.assertEqual([], prompted)
        self.assertNotIn("session-from-env", repr(from_environment))
        from_prompt = load_test_session(
            {}, lambda prompt: prompted.append(prompt) or "session-from-prompt"
        )
        self.assertEqual("session-from-prompt", from_prompt.reveal())
        self.assertEqual(1, len(prompted))
        self.assertNotIn("session-from-prompt", repr(from_prompt))

    def test_all_five_fake_terminal_response_fixtures_are_classified(self) -> None:
        root = Path(__file__).resolve().parents[1]
        expected = {
            "completed.json": 0,
            "refunded.json": 3,
            "prehold_absent.json": 3,
            "failed_reconciliation_pending.json": 3,
            "failed_asset_decision_pending.json": 3,
        }
        fixture_dir = root / "tests/fixtures/ai_edit_v3/acceptance-responses"
        for name, code in expected.items():
            with self.subTest(name=name):
                response = json.loads((fixture_dir / name).read_text(encoding="utf-8"))
                self.assertEqual(code, terminal_result_code(response))

    def test_nested_signed_provider_url_is_rejected_before_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "evidence.json"
            with self.assertRaises(ValueError):
                write_json_exclusive(path, {
                    "provider_usage": [{
                        "download_url": "https://cos.invalid/a?X-Amz-Signature=credential"
                    }]
                })
            self.assertFalse(path.exists())

    def test_atomic_writer_never_publishes_partial_final_file(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "evidence.json"
            with patch("server.content_domains.ai_edit_v3.acceptance_export.os.link", side_effect=OSError):
                with self.assertRaises(OSError):
                    write_json_exclusive(path, {"status": "completed"})
            self.assertFalse(path.exists())

    def test_failed_terminal_fixtures_use_common_collect_and_verify_path(self) -> None:
        root = Path(__file__).resolve().parents[1]
        matrix = json.loads((
            root / "tests/fixtures/ai_edit_v3/acceptance-20.json"
        ).read_text(encoding="utf-8"))
        fixture_dir = root / "tests/fixtures/ai_edit_v3/acceptance-responses"
        for name in (
            "refunded.json", "prehold_absent.json",
            "failed_reconciliation_pending.json", "failed_asset_decision_pending.json",
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as folder:
                response = json.loads((fixture_dir / name).read_text(encoding="utf-8"))
                response["normalized_request_sha256"] = matrix["cases"][0]["source"]["sha256"]
                api = EvidenceFakeApi(response)
                case_dir = Path(folder) / "case_01"
                evidence = collect_case_evidence(api, matrix["cases"][0], case_dir)
                self.assertEqual(3, terminal_result_code({"status": evidence.status, "quote": evidence.quote}))
                self.assertTrue(verify_case_evidence(case_dir, strict=True).passed)
                creates = [item for item in api.operations if item.startswith("create:")]
                self.assertEqual(["create:acceptance/local-fake/case_01"], creates)

    def test_strict_verifier_rejects_forged_refunded_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            case_dir = Path(folder)
            (case_dir / "evidence.json").write_text(json.dumps({
                "case_id": "wrong",
                "idempotency_key": "acceptance/other/wrong",
                "status": "refunded",
                "normalized_request_sha256": "garbage",
                "quote": {},
                "job_id": "",
                "stage_timings_ms": {},
                "material_decisions": [],
                "provider_usage": [],
                "audio_evidence": {},
                "qc": {},
                "settlement": {},
                "range_verified": False
            }), encoding="utf-8")
            verdict = verify_case_evidence(case_dir, strict=True)
        self.assertFalse(verdict.passed)
        self.assertIn("quote_invalid", verdict.errors)
        self.assertIn("settlement_invalid", verdict.errors)

    def test_completed_evidence_rejects_refunded_settlement_and_wrong_request_sha(self) -> None:
        root = Path(__file__).resolve().parents[1]
        response = json.loads((
            root / "tests/fixtures/ai_edit_v3/acceptance-responses/completed.json"
        ).read_text(encoding="utf-8"))
        matrix = json.loads((
            root / "tests/fixtures/ai_edit_v3/acceptance-20.json"
        ).read_text(encoding="utf-8"))
        response["normalized_request_sha256"] = matrix["cases"][0]["source"]["sha256"]
        with tempfile.TemporaryDirectory() as folder:
            case_dir = Path(folder) / "case_01"
            collect_case_evidence(EvidenceFakeApi(response), matrix["cases"][0], case_dir)
            path = case_dir / "evidence.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["settlement"] = {
                "state": "refunded", "charged_points": 0, "refunded_points": 30,
            }
            path.write_text(json.dumps(payload), encoding="utf-8")
            verdict = verify_case_evidence(case_dir, strict=True)
        self.assertFalse(verdict.passed)
        self.assertIn("status_settlement_mismatch", verdict.errors)

        wrong = json.loads(json.dumps(response))
        wrong["normalized_request_sha256"] = "9" * 64
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, "normalized_request_sha256_mismatch"):
                collect_case_evidence(
                    EvidenceFakeApi(wrong), matrix["cases"][0], Path(folder) / "case_01"
                )


if __name__ == "__main__":
    unittest.main()
