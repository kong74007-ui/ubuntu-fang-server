import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from scripts.ai_edit_v3_capacity import (
    HostCapacity,
    RunSummary,
    TaskMeasurement,
    aggregate_capacity,
    admit_predebit,
    main,
    validate_capacity,
    verify_capacity_fixture,
)


FIXTURE = Path(__file__).parent / "fixtures" / "ai_edit_v3" / "capacity-synthetic.json"


class CapacityTests(unittest.TestCase):
    def test_stress_profile_is_blocked_on_parallel_five_host(self) -> None:
        decision = validate_capacity(
            "stress-10",
            HostCapacity(8, 16, 80, 5, 2),
        )
        self.assertEqual(decision.status, "capacity_blocked")
        self.assertFalse(decision.may_lower_quality_or_sandbox)
        self.assertEqual(decision.reasons, (
            "vcpu<16", "ram_gib<32", "temp_gib<160",
            "pipeline_concurrency<10", "render_slots<4",
        ))

    def test_exact_profile_minimums_are_ready_and_never_relax_quality(self) -> None:
        for profile, host in (
            ("parallel-5", HostCapacity(8, 16, 80, 5, 2)),
            ("stress-10", HostCapacity(16, 32, 160, 10, 4)),
        ):
            with self.subTest(profile=profile):
                decision = validate_capacity(profile, host)
                self.assertEqual(decision.status, "ready")
                self.assertEqual(decision.reasons, ())
                self.assertFalse(decision.may_lower_quality_or_sandbox)
                self.assertTrue(decision.require_1080p)
                self.assertTrue(decision.require_sandbox)
                self.assertTrue(decision.require_full_qc)

    def test_predebit_admission_rejects_queue_over_50_or_temp_shortage(self) -> None:
        queue = admit_predebit(queue_depth=51, free_temp_gib=200, reserved_temp_gib=10)
        disk = admit_predebit(queue_depth=3, free_temp_gib=9, reserved_temp_gib=10)
        ready = admit_predebit(queue_depth=50, free_temp_gib=10, reserved_temp_gib=10)

        self.assertEqual(queue.status, "capacity_unavailable")
        self.assertGreater(queue.retry_after_seconds, 0)
        self.assertEqual(disk.status, "capacity_unavailable")
        self.assertGreater(disk.retry_after_seconds, 0)
        self.assertEqual(ready.status, "ready")
        self.assertEqual(ready.retry_after_seconds, 0)

    def test_aggregate_parallel_five_enforces_latency_and_all_metrics(self) -> None:
        tasks = tuple(
            TaskMeasurement(
                queue_wait_ms=index * 1_000,
                end_to_end_ms=value,
                stage_ms={"director": 10_000 + index, "render": 20_000 + index},
                cpu_peak_percent=60 + index,
                ram_peak_mib=2_000 + index,
                disk_peak_mib=3_000 + index,
                render_slot_occupancy=1 + (index % 2),
                backpressure_events=index % 2,
                timeout_events=0,
                sandbox_limit_events=0,
                crash_count=0,
                cross_lineage_reads=0,
                duplicate_calls=0,
                billing_corruptions=0,
            )
            for index, value in enumerate((1_000_000, 1_200_000, 1_300_000, 1_400_000, 2_600_000))
        )
        report = aggregate_capacity(RunSummary(profile="parallel-5", tasks=tasks))

        self.assertEqual(report.measured_numerator, 5)
        self.assertEqual(report.measured_denominator, 5)
        self.assertLessEqual(report.end_to_end_p50_ms, 25 * 60_000)
        self.assertLessEqual(report.end_to_end_p95_ms, 45 * 60_000)
        self.assertEqual(set(report.stage_latency_ms), {"director", "render"})
        self.assertEqual(report.stage_latency_ms["render"]["samples"], 5)
        self.assertEqual(report.cpu_peak_percent, 64)
        self.assertEqual(report.render_slot_occupancy_peak, 2)
        self.assertEqual(report.status, "ready")

    def test_stress_ten_blocks_any_safety_corruption_without_quality_relaxation(self) -> None:
        tasks = tuple(
            TaskMeasurement(
                queue_wait_ms=0, end_to_end_ms=1_000_000,
                stage_ms={"render": 100_000}, cpu_peak_percent=80,
                ram_peak_mib=4_000, disk_peak_mib=5_000,
                render_slot_occupancy=4, backpressure_events=0,
                timeout_events=0, sandbox_limit_events=0,
                crash_count=1 if index == 9 else 0,
                cross_lineage_reads=0, duplicate_calls=0, billing_corruptions=0,
            )
            for index in range(10)
        )
        report = aggregate_capacity(RunSummary(profile="stress-10", tasks=tasks))

        self.assertEqual(report.status, "capacity_blocked")
        self.assertIn("crash_count>0", report.reasons)
        self.assertFalse(report.may_lower_quality_or_sandbox)

    def test_parallel_latency_and_resource_limit_events_block_capacity(self) -> None:
        tasks = tuple(
            TaskMeasurement(
                queue_wait_ms=0, end_to_end_ms=2_800_000,
                stage_ms={"render": 2_000_000}, cpu_peak_percent=90,
                ram_peak_mib=6_000, disk_peak_mib=8_000,
                render_slot_occupancy=2, backpressure_events=1,
                timeout_events=1 if index == 0 else 0,
                sandbox_limit_events=1 if index == 1 else 0,
                crash_count=0, cross_lineage_reads=0,
                duplicate_calls=0, billing_corruptions=0,
            )
            for index in range(5)
        )
        report = aggregate_capacity(RunSummary(profile="parallel-5", tasks=tasks))

        self.assertEqual(report.status, "capacity_blocked")
        self.assertIn("end_to_end_p50_ms>1500000", report.reasons)
        self.assertIn("end_to_end_p95_ms>2700000", report.reasons)
        self.assertIn("timeout_events>0", report.reasons)
        self.assertIn("sandbox_limit_events>0", report.reasons)

    def test_verify_cli_reads_fixture_and_reports_measured_counts(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            result = main(["verify", "--fixture", str(FIXTURE)])
        report = json.loads(output.getvalue())

        self.assertEqual(result, 0)
        self.assertTrue(report["passed"])
        self.assertEqual(report["measured_numerator"], 5)
        self.assertEqual(report["measured_denominator"], 5)

    def test_verify_rejects_malformed_and_expected_status_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            malformed = Path(directory) / "malformed.json"
            mismatch = Path(directory) / "mismatch.json"
            malformed.write_text("{}", encoding="utf-8")
            payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
            payload["expected_status"] = "capacity_blocked"
            mismatch.write_text(json.dumps(payload), encoding="utf-8")

            self.assertEqual(main(["verify", "--fixture", str(malformed)]), 1)
            self.assertFalse(verify_capacity_fixture(mismatch).passed)
            self.assertEqual(main(["verify", "--fixture", str(mismatch)]), 1)

    def test_verify_rejects_empty_or_inconsistent_stage_measurements(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for label, mutate in (
                ("empty", lambda payload: [task.update({"stage_ms": {}}) for task in payload["tasks"]]),
                ("inconsistent", lambda payload: payload["tasks"][0]["stage_ms"].pop("render")),
            ):
                with self.subTest(label=label):
                    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
                    mutate(payload)
                    path = Path(directory) / f"{label}.json"
                    path.write_text(json.dumps(payload), encoding="utf-8")
                    self.assertEqual(main(["verify", "--fixture", str(path)]), 1)


if __name__ == "__main__":
    unittest.main()
