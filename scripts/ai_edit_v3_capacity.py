from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence


Profile = Literal["parallel-5", "stress-10"]


@dataclass(frozen=True)
class HostCapacity:
    vcpu: int
    ram_gib: int
    temp_gib: int
    pipeline_concurrency: int
    render_slots: int


@dataclass(frozen=True)
class CapacityDecision:
    status: Literal["ready", "capacity_blocked"]
    reasons: tuple[str, ...]
    may_lower_quality_or_sandbox: bool = False
    require_1080p: bool = True
    require_sandbox: bool = True
    require_full_qc: bool = True


@dataclass(frozen=True)
class AdmissionDecision:
    status: Literal["ready", "capacity_unavailable"]
    reason: str | None
    retry_after_seconds: int


@dataclass(frozen=True)
class TaskMeasurement:
    queue_wait_ms: int
    end_to_end_ms: int
    stage_ms: Mapping[str, int]
    cpu_peak_percent: int
    ram_peak_mib: int
    disk_peak_mib: int
    render_slot_occupancy: int
    backpressure_events: int
    timeout_events: int
    sandbox_limit_events: int
    crash_count: int
    cross_lineage_reads: int
    duplicate_calls: int
    billing_corruptions: int


@dataclass(frozen=True)
class RunSummary:
    profile: Profile
    tasks: tuple[TaskMeasurement, ...]


@dataclass(frozen=True)
class CapacityReport:
    status: Literal["ready", "capacity_blocked"]
    reasons: tuple[str, ...]
    measured_numerator: int
    measured_denominator: int
    queue_wait_p50_ms: int
    queue_wait_p95_ms: int
    end_to_end_p50_ms: int
    end_to_end_p95_ms: int
    stage_latency_ms: Mapping[str, Mapping[str, int]]
    cpu_peak_percent: int
    ram_peak_mib: int
    disk_peak_mib: int
    render_slot_occupancy_peak: int
    backpressure_events: int
    timeout_events: int
    sandbox_limit_events: int
    crash_count: int
    cross_lineage_reads: int
    duplicate_calls: int
    billing_corruptions: int
    may_lower_quality_or_sandbox: bool = False


@dataclass(frozen=True)
class CapacityFixtureReport:
    passed: bool
    profile: str
    expected_status: str
    observed_status: str
    measured_numerator: int
    measured_denominator: int


PROFILE_MINIMUMS = {
    "parallel-5": HostCapacity(8, 16, 80, 5, 2),
    "stress-10": HostCapacity(16, 32, 160, 10, 4),
}


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field}_invalid")
    return value


def read_host_capacity() -> HostCapacity:
    vcpu = os.cpu_count() or 1
    try:
        import psutil  # type: ignore[import-not-found]
        ram_gib = max(1, int(psutil.virtual_memory().total // (1024 ** 3)))
    except ImportError:
        ram_gib = max(1, int(os.environ.get("AI_EDIT_V3_HOST_RAM_GIB", "1")))
    temp_gib = max(0, int(shutil.disk_usage(tempfile.gettempdir()).free // (1024 ** 3)))
    return HostCapacity(
        vcpu=vcpu,
        ram_gib=ram_gib,
        temp_gib=temp_gib,
        pipeline_concurrency=int(os.environ.get("AI_EDIT_V3_PIPELINE_CONCURRENCY", "1")),
        render_slots=int(os.environ.get("AI_EDIT_V3_RENDER_SLOTS", "1")),
    )


def validate_capacity(profile: Profile, host: HostCapacity) -> CapacityDecision:
    if profile not in PROFILE_MINIMUMS:
        raise ValueError("capacity_profile_invalid")
    required = PROFILE_MINIMUMS[profile]
    reasons = tuple(
        f"{name}<{getattr(required, name)}"
        for name in (
            "vcpu", "ram_gib", "temp_gib", "pipeline_concurrency", "render_slots",
        )
        if getattr(host, name) < getattr(required, name)
    )
    return CapacityDecision(
        status="capacity_blocked" if reasons else "ready",
        reasons=reasons,
    )


def admit_predebit(
    *,
    queue_depth: int,
    free_temp_gib: int,
    reserved_temp_gib: int,
) -> AdmissionDecision:
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in (
        queue_depth, free_temp_gib, reserved_temp_gib,
    )):
        raise ValueError("capacity_admission_input_invalid")
    if queue_depth > 50:
        return AdmissionDecision("capacity_unavailable", "queue_depth>50", max(30, queue_depth * 2))
    if free_temp_gib < reserved_temp_gib:
        shortage = reserved_temp_gib - free_temp_gib
        return AdmissionDecision("capacity_unavailable", "reserved_temp_insufficient", max(30, shortage * 15))
    return AdmissionDecision("ready", None, 0)


def _percentile(values: Sequence[int], percentile: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _validate_measurement(task: TaskMeasurement) -> None:
    values = (
        task.queue_wait_ms, task.end_to_end_ms, task.cpu_peak_percent,
        task.ram_peak_mib, task.disk_peak_mib, task.render_slot_occupancy,
        task.backpressure_events, task.timeout_events, task.sandbox_limit_events,
        task.crash_count, task.cross_lineage_reads, task.duplicate_calls,
        task.billing_corruptions,
    )
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values):
        raise ValueError("capacity_measurement_invalid")
    if any(
        not isinstance(name, str) or not name
        or isinstance(value, bool) or not isinstance(value, int) or value < 0
        for name, value in task.stage_ms.items()
    ):
        raise ValueError("capacity_stage_measurement_invalid")


def aggregate_capacity(run: RunSummary) -> CapacityReport:
    if run.profile not in PROFILE_MINIMUMS or not run.tasks:
        raise ValueError("capacity_run_invalid")
    for task in run.tasks:
        _validate_measurement(task)
    stage_keys = frozenset(run.tasks[0].stage_ms)
    if not stage_keys:
        raise ValueError("capacity_stage_set_empty")
    if any(frozenset(task.stage_ms) != stage_keys for task in run.tasks[1:]):
        raise ValueError("capacity_stage_set_inconsistent")
    expected = 5 if run.profile == "parallel-5" else 10
    reasons: list[str] = []
    if len(run.tasks) != expected:
        reasons.append(f"measured_tasks!={expected}")
    queue_wait = [task.queue_wait_ms for task in run.tasks]
    end_to_end = [task.end_to_end_ms for task in run.tasks]
    e2e_p50 = _percentile(end_to_end, 0.50)
    e2e_p95 = _percentile(end_to_end, 0.95)
    if run.profile == "parallel-5":
        if e2e_p50 > 25 * 60_000:
            reasons.append("end_to_end_p50_ms>1500000")
        if e2e_p95 > 45 * 60_000:
            reasons.append("end_to_end_p95_ms>2700000")
    safety_fields = (
        ("crash_count", sum(task.crash_count for task in run.tasks)),
        ("cross_lineage_reads", sum(task.cross_lineage_reads for task in run.tasks)),
        ("duplicate_calls", sum(task.duplicate_calls for task in run.tasks)),
        ("billing_corruptions", sum(task.billing_corruptions for task in run.tasks)),
    )
    reasons.extend(f"{name}>0" for name, value in safety_fields if value > 0)
    timeout_events = sum(task.timeout_events for task in run.tasks)
    sandbox_limit_events = sum(task.sandbox_limit_events for task in run.tasks)
    if timeout_events:
        reasons.append("timeout_events>0")
    if sandbox_limit_events:
        reasons.append("sandbox_limit_events>0")
    stage_names = sorted({name for task in run.tasks for name in task.stage_ms})
    stage_latency = {
        name: {
            "p50_ms": _percentile([task.stage_ms[name] for task in run.tasks if name in task.stage_ms], 0.50),
            "p95_ms": _percentile([task.stage_ms[name] for task in run.tasks if name in task.stage_ms], 0.95),
            "samples": len(run.tasks),
        }
        for name in stage_names
    }
    return CapacityReport(
        status="capacity_blocked" if reasons else "ready",
        reasons=tuple(reasons),
        measured_numerator=len(run.tasks),
        measured_denominator=expected,
        queue_wait_p50_ms=_percentile(queue_wait, 0.50),
        queue_wait_p95_ms=_percentile(queue_wait, 0.95),
        end_to_end_p50_ms=e2e_p50,
        end_to_end_p95_ms=e2e_p95,
        stage_latency_ms=stage_latency,
        cpu_peak_percent=max(task.cpu_peak_percent for task in run.tasks),
        ram_peak_mib=max(task.ram_peak_mib for task in run.tasks),
        disk_peak_mib=max(task.disk_peak_mib for task in run.tasks),
        render_slot_occupancy_peak=max(task.render_slot_occupancy for task in run.tasks),
        backpressure_events=sum(task.backpressure_events for task in run.tasks),
        timeout_events=timeout_events,
        sandbox_limit_events=sandbox_limit_events,
        crash_count=dict(safety_fields)["crash_count"],
        cross_lineage_reads=dict(safety_fields)["cross_lineage_reads"],
        duplicate_calls=dict(safety_fields)["duplicate_calls"],
        billing_corruptions=dict(safety_fields)["billing_corruptions"],
    )


def verify_capacity_fixture(path: Path) -> CapacityFixtureReport:
    payload: Mapping[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    if set(payload) != {"version", "profile", "host", "expected_status", "measured", "tasks"}:
        raise ValueError("capacity_fixture_shape_invalid")
    if payload["version"] != "1.0":
        raise ValueError("capacity_fixture_version_invalid")
    profile = str(payload["profile"])
    if profile not in PROFILE_MINIMUMS:
        raise ValueError("capacity_profile_invalid")
    host_raw = payload["host"]
    if not isinstance(host_raw, Mapping) or set(host_raw) != {
        "vcpu", "ram_gib", "temp_gib", "pipeline_concurrency", "render_slots",
    }:
        raise ValueError("capacity_host_shape_invalid")
    host = HostCapacity(**{
        name: _positive_int(value, name) for name, value in host_raw.items()
    })
    measured = payload["measured"]
    if not isinstance(measured, Mapping) or set(measured) != {"numerator", "denominator"}:
        raise ValueError("capacity_measured_shape_invalid")
    numerator_raw = measured["numerator"]
    denominator_raw = measured["denominator"]
    if (
        isinstance(numerator_raw, bool) or not isinstance(numerator_raw, int) or numerator_raw < 0
        or isinstance(denominator_raw, bool) or not isinstance(denominator_raw, int) or denominator_raw <= 0
    ):
        raise ValueError("capacity_measured_count_invalid")
    numerator = numerator_raw
    denominator = denominator_raw
    expected_status = str(payload["expected_status"])
    if expected_status not in {"ready", "capacity_blocked"}:
        raise ValueError("capacity_expected_status_invalid")
    tasks_raw = payload["tasks"]
    if not isinstance(tasks_raw, list):
        raise ValueError("capacity_tasks_invalid")
    tasks: list[TaskMeasurement] = []
    required_task_fields = set(TaskMeasurement.__dataclass_fields__)
    for raw in tasks_raw:
        if not isinstance(raw, Mapping) or set(raw) != required_task_fields:
            raise ValueError("capacity_task_shape_invalid")
        if not isinstance(raw["stage_ms"], Mapping):
            raise ValueError("capacity_stage_measurement_invalid")
        tasks.append(TaskMeasurement(**raw))
    run_report = aggregate_capacity(RunSummary(profile=profile, tasks=tuple(tasks)))  # type: ignore[arg-type]
    host_status = validate_capacity(profile, host).status  # type: ignore[arg-type]
    observed = "ready" if host_status == "ready" and run_report.status == "ready" else "capacity_blocked"
    return CapacityFixtureReport(
        passed=(
            denominator > 0
            and 0 <= numerator <= denominator
            and numerator == run_report.measured_numerator
            and denominator == run_report.measured_denominator
            and observed == expected_status
        ),
        profile=profile,
        expected_status=expected_status,
        observed_status=observed,
        measured_numerator=numerator,
        measured_denominator=denominator,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--fixture", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = verify_capacity_fixture(args.fixture)
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return 1
    print(json.dumps(asdict(report), ensure_ascii=False, sort_keys=True))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
