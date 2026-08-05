import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts.ai_edit_v3_fault_matrix import (
    FaultHarnessUnavailable,
    FaultVerdict,
    assert_authoritative_convergence,
    assert_production_build_fault_isolated,
    assert_fault_hooks_production_safe,
    build_fault_harness,
    enumerate_fault_points,
    main,
    run_fault_case,
)


class FaultMatrixTests(unittest.TestCase):
    def test_publish_response_loss_converges_once_without_refund(self) -> None:
        verdict = FaultVerdict(
            final_state="completed",
            confirmed_preheld_points=64,
            refunded_points=0,
            visible_asset_count=1,
            provider_submit_count=1,
            billing_request_count=1,
            storage_upload_count=1,
            publication_request_count=1,
            persistent_write_count=1,
            publication_winner="publish_won",
        )

        assert_authoritative_convergence(verdict)

    def test_matrix_covers_transition_sides_billing_publication_lease_and_sandbox(self) -> None:
        cases = enumerate_fault_points()
        ids = [case.case_id for case in cases]
        transitions = {
            case.fault_point for case in cases if case.category == "persistent_transition"
        }
        for transition in transitions:
            self.assertIn(f"kill_before_{transition}", ids)
            self.assertIn(f"kill_after_{transition}", ids)
        required = {
            "billing_predebit_response_lost",
            "billing_delta_refund_five_minute_outage",
            "billing_full_refund_rejected",
            "publication_commit_publish_response_lost",
            "publication_cancel_publish_response_lost",
            "publication_query_decision_response_lost",
            "lease_two_worker_competition",
            "lease_expiry_reclaim",
            "stale_fence_provider_result_write",
            "stale_fence_billing_intent_write",
            "stale_fence_delivery_intent_write",
            "chromium_oom",
            "ffmpeg_child_leak",
            "network_attempt",
            "path_traversal",
            "symlink_escape",
            "hardlink_escape",
            "device_file",
            "toctou_swap",
            "image_bomb",
            "environment_secret_read",
            "sibling_job_read",
            "systemd_property_injection",
            "systemd_unit_injection",
        }
        self.assertTrue(required.issubset(ids))
        self.assertEqual(len(ids), len(set(ids)))

    def test_cli_run_executes_every_declared_local_fake_case(self) -> None:
        output = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), redirect_stdout(output):
            result = main(["run", "--environment", "local-fake", "--strict"])
        report = json.loads(output.getvalue())

        self.assertEqual(result, 0)
        self.assertTrue(report["passed"])
        self.assertEqual(
            report["executed_case_ids"],
            [case.case_id for case in enumerate_fault_points()],
        )
        self.assertEqual(report["failures"], [])

    def test_delta_refund_is_idempotent_and_keeps_published_asset(self) -> None:
        case = next(
            case for case in enumerate_fault_points()
            if case.case_id == "billing_delta_refund_response_lost"
        )
        verdict = run_fault_case(case, build_fault_harness("local-fake"))

        assert_authoritative_convergence(verdict)
        self.assertEqual(verdict.final_state, "completed")
        self.assertEqual(verdict.confirmed_preheld_points, 64)
        self.assertEqual(verdict.charged_points, 48)
        self.assertEqual(verdict.refunded_points, 16)
        self.assertEqual(verdict.refund_kind, "delta")
        self.assertEqual(verdict.visible_asset_count, 1)

    def test_crashable_cancel_and_refund_transitions_follow_their_real_paths(self) -> None:
        cases = {case.case_id: case for case in enumerate_fault_points()}
        expected = {
            "kill_after_publication_cancel_publish": ("refunded", "cancel_won", 64),
            "kill_after_delta_refund_request": ("completed", "publish_won", 16),
            "kill_after_full_refund_request": ("refunded", "cancel_won", 64),
        }
        for case_id, final in expected.items():
            with self.subTest(case_id=case_id):
                verdict = run_fault_case(cases[case_id], build_fault_harness("local-fake"))
                self.assertEqual(
                    (verdict.final_state, verdict.publication_winner, verdict.refunded_points),
                    final,
                )
                self.assertEqual(verdict.target_attempt_count, 2)
                self.assertEqual(verdict.target_effect_count, 1)

    def test_settlement_is_authoritative_before_any_successful_publication(self) -> None:
        cases = {case.case_id: case for case in enumerate_fault_points()}
        harness = build_fault_harness("local-fake")
        before_settlement = run_fault_case(cases["kill_before_settlement_bound"], harness)
        before_publication = run_fault_case(
            cases["kill_before_publication_register_generation"], harness,
        )
        delta = run_fault_case(cases["kill_after_delta_refund_request"], harness)

        self.assertNotIn("publication_register_generation", before_settlement.pre_crash_effect_order)
        self.assertIn("persist:settlement_bound", before_publication.pre_crash_effect_order)
        self.assertLess(
            before_publication.effect_order.index("persist:settlement_bound"),
            before_publication.effect_order.index("publication_register_generation"),
        )
        self.assertLess(
            delta.effect_order.index("delta_refund_request"),
            delta.effect_order.index("persist:settlement_bound"),
        )
        self.assertLess(
            delta.effect_order.index("persist:settlement_bound"),
            delta.effect_order.index("publication_register_generation"),
        )

    def test_rejection_stops_forbidden_downstream_and_attempts_named_operation(self) -> None:
        cases = {case.case_id: case for case in enumerate_fault_points()}
        provider = run_fault_case(cases["provider_submit_rejected"], build_fault_harness("local-fake"))
        delta = run_fault_case(cases["billing_delta_refund_rejected"], build_fault_harness("local-fake"))

        self.assertEqual(provider.storage_upload_count, 0)
        self.assertEqual((provider.target_operation, provider.target_attempt_count, provider.target_effect_count),
                         ("provider_submit", 1, 0))
        self.assertEqual((delta.target_operation, delta.target_attempt_count, delta.target_effect_count),
                         ("delta_refund_request", 1, 0))
        self.assertEqual(delta.publication_winner, None)

    def test_cli_failing_case_uses_real_runner_and_exits_one(self) -> None:
        target = enumerate_fault_points()[0].case_id
        output = io.StringIO()
        with patch.dict(
            os.environ,
            {"AI_EDIT_V3_FAULT_FORCE_FAILURE": target},
            clear=True,
        ), redirect_stdout(output):
            result = main(["run", "--environment", "local-fake", "--strict"])
        report = json.loads(output.getvalue())

        self.assertEqual(result, 1)
        self.assertFalse(report["passed"])
        self.assertEqual(report["failures"], [f"{target}:AssertionError"])

    def test_test_harness_and_production_hooks_fail_closed_before_mutation(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(
                FaultHarnessUnavailable,
                "test_fault_authorization_missing_or_mismatched",
            ):
                build_fault_harness("test")
        with patch.dict(os.environ, {
            "AI_EDIT_V3_FAULT_AUTHORIZATION_REF": "approved-test-only",
            "AI_EDIT_V3_ENVIRONMENT": "test",
            "AI_EDIT_V3_DEPLOYED_SHA": "a" * 40,
            "AI_EDIT_V3_EXPECTED_TEST_SHA": "a" * 40,
        }, clear=True):
            with self.assertRaisesRegex(
                FaultHarnessUnavailable,
                "not_enabled_before_task_7",
            ):
                build_fault_harness("test")
        with self.assertRaisesRegex(AssertionError, "production_fault_hook_enabled"):
            assert_fault_hooks_production_safe({
                "environment": "production",
                "fault_hooks_enabled": True,
                "fault_module": None,
            })
        assert_production_build_fault_isolated(Path(__file__).resolve().parents[1])
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(main([
                "check-production-isolation", "--project-root",
                str(Path(__file__).resolve().parents[1]),
            ]), 0)
        self.assertTrue(json.loads(output.getvalue())["passed"])

    def test_matrix_fixture_cannot_be_silently_reduced(self) -> None:
        import scripts.ai_edit_v3_fault_matrix as module

        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory) / "fault-matrix.json"
            fixture.write_text(json.dumps({
                "version": "1.0",
                "persistent_transitions": ["only_one"],
                "standalone_faults": [],
            }), encoding="utf-8")
            with patch.object(module, "_fixture_path", return_value=fixture):
                with self.assertRaisesRegex(ValueError, "fault_transition_set_not_frozen"):
                    enumerate_fault_points()

    def test_production_isolation_scans_server_entrypoints_and_deployment_files(self) -> None:
        for relative in (
            "server/app.py", "deploy/worker.service", "deploy/start.sh",
            "deploy/start.ps1", "infra/main.tf", "infra/production.tfvars",
            "deploy/runtime.env", "deploy/.env", "deploy/worker.conf",
        ):
            with self.subTest(relative=relative), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / relative
                source.parent.mkdir(parents=True)
                source.write_text("AI_EDIT_V3_FAULT_AUTHORIZATION_REF=unsafe\n", encoding="utf-8")
                with self.assertRaisesRegex(AssertionError, "production_fault_hook_imported"):
                    assert_production_build_fault_isolated(root)

    def test_convergence_rejects_contradictory_terminal_states(self) -> None:
        base = {
            "final_state": "completed",
            "confirmed_preheld_points": 64,
            "refunded_points": 0,
            "visible_asset_count": 1,
            "provider_submit_count": 1,
            "billing_request_count": 1,
            "storage_upload_count": 1,
            "publication_request_count": 1,
            "persistent_write_count": 1,
            "publication_winner": "publish_won",
        }
        malformed = (
            ({**base, "confirmed_preheld_points": 0}, "completed_without_confirmed_prehold"),
            ({**base, "final_state": "prehold_absent", "confirmed_preheld_points": 0,
              "visible_asset_count": 0, "publication_winner": None}, "prehold_absent_has_money"),
            ({**base, "final_state": "failed_asset_decision_pending", "visible_asset_count": 0},
             "asset_pending_has_publication_winner"),
            ({**base, "final_state": "failed_reconciliation_pending", "visible_asset_count": 0},
             "reconciliation_pending_publication_invalid"),
        )
        for payload, error in malformed:
            with self.subTest(error=error):
                with self.assertRaisesRegex(AssertionError, error):
                    assert_authoritative_convergence(FaultVerdict(**payload))

    def test_convergence_rejects_money_visibility_duplicates_and_isolation_leaks(self) -> None:
        base = {
            "final_state": "completed",
            "confirmed_preheld_points": 64,
            "refunded_points": 0,
            "visible_asset_count": 1,
            "provider_submit_count": 1,
            "billing_request_count": 1,
            "storage_upload_count": 1,
            "publication_request_count": 1,
            "persistent_write_count": 1,
            "publication_winner": "publish_won",
        }
        mutations = (
            ("provider_submit_count", 2, "duplicate_provider_submit"),
            ("billing_request_count", 2, "duplicate_billing_request"),
            ("storage_upload_count", 2, "duplicate_storage_upload"),
            ("publication_request_count", 2, "duplicate_publication_request"),
            ("persistent_write_count", 2, "duplicate_persistent_write"),
            ("visible_asset_count", 2, "duplicate_visible_asset"),
            ("refunded_points", 65, "refund_exceeds_confirmed_prehold"),
            ("cross_job_read_count", 1, "isolation_violation"),
            ("leaked_child_count", 1, "isolation_violation"),
            ("forbidden_network_count", 1, "isolation_violation"),
            ("secret_read_count", 1, "isolation_violation"),
            ("permanent_running", True, "permanent_running_stage"),
        )
        for field, value, error in mutations:
            with self.subTest(field=field):
                with self.assertRaisesRegex(AssertionError, error):
                    assert_authoritative_convergence(FaultVerdict(**{**base, field: value}))


if __name__ == "__main__":
    unittest.main()
