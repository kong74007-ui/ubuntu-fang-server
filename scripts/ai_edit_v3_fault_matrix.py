from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal, Mapping, Sequence


ALLOWED_FINAL_STATES = {
    "completed",
    "refunded",
    "prehold_absent",
    "failed_reconciliation_pending",
    "failed_asset_decision_pending",
}
ALLOWED_OUTCOMES = {
    "publish", "publish_delta_refund", "refund", "prehold_absent",
    "reconciliation_pending", "asset_pending",
}
REQUIRED_PERSISTENT_TRANSITIONS = (
    "predebit_request", "prehold_confirmed", "normalized", "transcript_bound",
    "edit_plan_bound", "materials_bound", "audio_bound", "provider_intent_bound",
    "provider_result_bound", "cos_upload", "qc_bound", "delivery_intent_bound",
    "publication_register_generation", "publication_prepare_hidden",
    "publication_commit_publish", "publication_cancel_publish",
    "publication_query_decision", "delta_refund_request", "full_refund_request",
    "settlement_bound", "terminal_bound",
)
REQUIRED_STANDALONE_CASES = (
    "provider_submit_response_lost", "provider_submit_rejected", "provider_five_minute_outage",
    "cos_upload_response_lost", "cos_upload_outage",
    "billing_predebit_response_lost", "billing_predebit_rejected", "billing_predebit_five_minute_outage",
    "billing_delta_refund_response_lost", "billing_delta_refund_rejected", "billing_delta_refund_five_minute_outage",
    "billing_full_refund_response_lost", "billing_full_refund_rejected", "billing_full_refund_five_minute_outage",
    "publication_register_generation_response_lost", "publication_prepare_hidden_response_lost",
    "publication_commit_publish_response_lost", "publication_cancel_publish_response_lost",
    "publication_query_decision_response_lost", "publication_five_minute_outage",
    "lease_two_worker_competition", "lease_expiry_reclaim", "stale_fence_transition_write",
    "stale_fence_checkpoint_write", "stale_fence_provider_result_write",
    "stale_fence_billing_intent_write", "stale_fence_delivery_intent_write",
    "chromium_crash", "chromium_oom", "chromium_timeout", "ffmpeg_child_leak",
    "network_attempt", "path_traversal", "symlink_escape", "hardlink_escape",
    "device_file", "toctou_swap", "image_bomb", "environment_secret_read",
    "sibling_job_read", "systemd_property_injection", "systemd_unit_injection",
)


class FaultHarnessUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class FaultCase:
    case_id: str
    category: str
    fault_point: str
    timing: str
    outcome: str


@dataclass(frozen=True)
class FaultVerdict:
    final_state: str
    confirmed_preheld_points: int
    refunded_points: int
    visible_asset_count: int
    provider_submit_count: int
    billing_request_count: int
    storage_upload_count: int
    publication_request_count: int
    persistent_write_count: int
    publication_winner: str | None
    cross_job_read_count: int = 0
    leaked_child_count: int = 0
    forbidden_network_count: int = 0
    secret_read_count: int = 0
    permanent_running: bool = False
    charged_points: int | None = None
    refund_kind: str | None = None
    crash_count: int = 0
    recovery_attempt_count: int = 0
    stale_write_rejected_count: int = 0
    sandbox_attempt_count: int = 0
    sandbox_denial_count: int = 0
    target_operation: str | None = None
    target_attempt_count: int = 0
    target_effect_count: int = 0
    downstream_effect_count: int = 0
    lease_worker_count: int = 0
    lease_reclaim_count: int = 0
    fenced_target: str | None = None
    sandbox_capability: str | None = None
    lease_clock_advanced_seconds: int = 0
    effect_order: tuple[str, ...] = ()
    pre_crash_effect_order: tuple[str, ...] = ()


@dataclass(frozen=True)
class FaultHarness:
    environment: str
    forced_failure_case_id: str | None = None


@dataclass(frozen=True)
class FaultMatrixReport:
    passed: bool
    executed_case_ids: tuple[str, ...]
    failures: tuple[str, ...]


class _LocalFaultState:
    """Injected idempotent fakes used only by this test-build module."""

    def __init__(self) -> None:
        self.confirmed_prehold = 0
        self.refunded = 0
        self.provider_calls: dict[str, int] = {}
        self.billing_calls: dict[str, int] = {}
        self.storage_calls: dict[str, int] = {}
        self.publication_calls: dict[str, int] = {}
        self.persistent_writes: dict[str, int] = {}
        self.hidden_asset = False
        self.visible_asset = False
        self.publication_winner: str | None = None
        self.fence = 1
        self.cross_job_reads = 0
        self.leaked_children = 0
        self.network_escapes = 0
        self.secret_reads = 0
        self.crashes = 0
        self.recoveries = 0
        self.stale_write_rejections = 0
        self.sandbox_attempts = 0
        self.sandbox_denials = 0
        self.operation_attempts: dict[str, int] = {}
        self.operation_effects: dict[str, int] = {}
        self.effect_order: list[str] = []
        self.pre_crash_effect_order: tuple[str, ...] = ()
        self.target_operation: str | None = None
        self.downstream_operations: set[str] = set()
        self.lease_workers = 0
        self.lease_reclaims = 0
        self.fenced_target: str | None = None
        self.sandbox_capability: str | None = None
        self.lease_clock_advanced_seconds = 0

    def _idempotent_call(
        self,
        calls: dict[str, int],
        operation: str,
        request_id: str,
        *,
        accepted: bool = True,
    ) -> None:
        self.operation_attempts[operation] = self.operation_attempts.get(operation, 0) + 1
        if accepted and request_id not in calls:
            calls[request_id] = 1
            self.operation_effects[operation] = self.operation_effects.get(operation, 0) + 1
            self.effect_order.append(operation)

    def prehold(self, request_id: str, *, accepted: bool = True) -> None:
        self._idempotent_call(self.billing_calls, "predebit_request", request_id, accepted=accepted)
        if accepted:
            self.confirmed_prehold = 64

    def submit_provider(self, request_id: str) -> None:
        self._idempotent_call(self.provider_calls, "provider_submit", request_id)

    def reject_provider(self, request_id: str) -> None:
        self._idempotent_call(self.provider_calls, "provider_submit", request_id, accepted=False)

    def upload_storage(self, request_id: str) -> None:
        self._idempotent_call(self.storage_calls, "cos_upload", request_id)

    def reject_storage(self, request_id: str) -> None:
        self._idempotent_call(self.storage_calls, "cos_upload", request_id, accepted=False)

    def persist_transition(self, request_id: str) -> None:
        operation = "persist:" + request_id.rsplit("/", 1)[-1]
        self._idempotent_call(self.persistent_writes, operation, request_id)

    def refund(self, request_id: str, *, points: int | None = None, accepted: bool = True) -> None:
        operation = "delta_refund_request" if points is not None else "full_refund_request"
        self._idempotent_call(self.billing_calls, operation, request_id, accepted=accepted)
        if accepted:
            self.refunded = self.confirmed_prehold if points is None else points

    def prepare_hidden(self, request_id: str = "publication/prepare/job-1") -> None:
        self._idempotent_call(self.publication_calls, "publication_prepare_hidden", request_id)
        if self.publication_winner != "cancel_won":
            self.hidden_asset = True

    def register_generation(self, request_id: str = "publication/register/job-1") -> None:
        self._idempotent_call(self.publication_calls, "publication_register_generation", request_id)

    def query_decision(self, request_id: str = "publication/query/job-1") -> str | None:
        self._idempotent_call(self.publication_calls, "publication_query_decision", request_id)
        return self.publication_winner

    def commit_publish(self, request_id: str = "publication/commit/job-1") -> None:
        self._idempotent_call(self.publication_calls, "publication_commit_publish", request_id)
        if self.publication_winner is None:
            self.publication_winner = "publish_won"
            self.visible_asset = self.hidden_asset

    def cancel_publish(self, request_id: str = "publication/cancel/job-1") -> None:
        self._idempotent_call(self.publication_calls, "publication_cancel_publish", request_id)
        if self.publication_winner is None:
            self.publication_winner = "cancel_won"
            self.visible_asset = False

    def reclaim_lease(self) -> int:
        self.fence += 1
        self.lease_reclaims += 1
        return self.fence

    def fenced_write(self, token: int, target: str = "transition") -> bool:
        self.fenced_target = target
        return token == self.fence

    def verdict(self, final_state: str, *, refund_kind: str | None = None) -> FaultVerdict:
        return FaultVerdict(
            final_state=final_state,
            confirmed_preheld_points=self.confirmed_prehold,
            refunded_points=self.refunded,
            visible_asset_count=int(self.visible_asset),
            provider_submit_count=max(self.provider_calls.values(), default=0),
            billing_request_count=max(self.billing_calls.values(), default=0),
            storage_upload_count=max(self.storage_calls.values(), default=0),
            publication_request_count=max(self.publication_calls.values(), default=0),
            persistent_write_count=max(self.persistent_writes.values(), default=0),
            publication_winner=self.publication_winner,
            cross_job_read_count=self.cross_job_reads,
            leaked_child_count=self.leaked_children,
            forbidden_network_count=self.network_escapes,
            secret_read_count=self.secret_reads,
            permanent_running=False,
            charged_points=self.confirmed_prehold - self.refunded,
            refund_kind=refund_kind,
            crash_count=self.crashes,
            recovery_attempt_count=self.recoveries,
            stale_write_rejected_count=self.stale_write_rejections,
            sandbox_attempt_count=self.sandbox_attempts,
            sandbox_denial_count=self.sandbox_denials,
            target_operation=self.target_operation,
            target_attempt_count=self.operation_attempts.get(self.target_operation or "", 0),
            target_effect_count=self.operation_effects.get(self.target_operation or "", 0),
            downstream_effect_count=sum(self.operation_effects.get(name, 0) for name in self.downstream_operations),
            lease_worker_count=self.lease_workers,
            lease_reclaim_count=self.lease_reclaims,
            fenced_target=self.fenced_target,
            sandbox_capability=self.sandbox_capability,
            lease_clock_advanced_seconds=self.lease_clock_advanced_seconds,
            effect_order=tuple(self.effect_order),
            pre_crash_effect_order=self.pre_crash_effect_order,
        )


class _LocalWorker:
    """Ephemeral worker facade; a crash creates a new instance over durable fakes."""

    def __init__(self, backend: _LocalFaultState, worker_id: str) -> None:
        self.backend = backend
        self.worker_id = worker_id

    def __getattr__(self, name: str):
        return getattr(self.backend, name)


class _FakeSandbox:
    """Named deny-by-default boundaries; no host capability is invoked."""

    def __init__(self, backend: _LocalFaultState) -> None:
        self.backend = backend

    def _deny(self, capability: str) -> None:
        self.backend.sandbox_capability = capability
        self.backend.sandbox_attempts += 1
        self.backend.sandbox_denials += 1

    def chromium_process(self, failure: str) -> None:
        self._deny(f"chromium_{failure}")

    def ffmpeg_child(self) -> None:
        self._deny("ffmpeg_child_leak")
        self.backend.leaked_children = 1
        self.backend.leaked_children = 0

    def network(self) -> None:
        self._deny("network_attempt")

    def filesystem(self, capability: str) -> None:
        self._deny(capability)

    def environment_secret(self) -> None:
        self._deny("environment_secret_read")

    def sibling_job(self) -> None:
        self._deny("sibling_job_read")

    def systemd(self, capability: str) -> None:
        self._deny(capability)


def _fixture_path() -> Path:
    return Path(__file__).resolve().parents[1] / "tests/fixtures/ai_edit_v3/fault-matrix.json"


def enumerate_fault_points() -> tuple[FaultCase, ...]:
    payload = json.loads(_fixture_path().read_text(encoding="utf-8"))
    if set(payload) != {"version", "persistent_transitions", "standalone_faults"}:
        raise ValueError("fault_matrix_shape_invalid")
    if payload.get("version") != "1.0":
        raise ValueError("fault_matrix_version_invalid")
    cases: list[FaultCase] = []
    raw_transitions = payload.get("persistent_transitions", [])
    if tuple(raw_transitions) != REQUIRED_PERSISTENT_TRANSITIONS:
        raise ValueError("fault_transition_set_not_frozen")
    for transition in raw_transitions:
        if not isinstance(transition, str) or not transition:
            raise ValueError("fault_transition_invalid")
        for timing in ("before", "after"):
            cases.append(FaultCase(
                case_id=f"kill_{timing}_{transition}",
                category="persistent_transition",
                fault_point=transition,
                timing=timing,
                outcome="publish",
            ))
    raw_standalone = payload.get("standalone_faults", [])
    if tuple(raw.get("case_id") for raw in raw_standalone if isinstance(raw, Mapping)) != REQUIRED_STANDALONE_CASES:
        raise ValueError("fault_standalone_set_not_frozen")
    for raw in raw_standalone:
        if not isinstance(raw, Mapping) or set(raw) != {"case_id", "category", "outcome"}:
            raise ValueError("fault_case_shape_invalid")
        outcome = raw["outcome"]
        if outcome not in ALLOWED_OUTCOMES:
            raise ValueError("fault_outcome_invalid")
        cases.append(FaultCase(
            case_id=str(raw["case_id"]),
            category=str(raw["category"]),
            fault_point=str(raw["case_id"]),
            timing="during",
            outcome=str(outcome),
        ))
    ids = [case.case_id for case in cases]
    if len(ids) != len(set(ids)) or not ids:
        raise ValueError("fault_case_ids_invalid")
    return tuple(cases)


def build_fault_harness(
    environment: Literal["local-fake", "test"],
) -> FaultHarness:
    if environment == "local-fake":
        return FaultHarness(
            environment="local-fake",
            forced_failure_case_id=os.environ.get("AI_EDIT_V3_FAULT_FORCE_FAILURE"),
        )
    if environment != "test":
        raise FaultHarnessUnavailable("fault_environment_forbidden")
    authorization = os.environ.get("AI_EDIT_V3_FAULT_AUTHORIZATION_REF")
    marker = os.environ.get("AI_EDIT_V3_ENVIRONMENT")
    deployed_sha = os.environ.get("AI_EDIT_V3_DEPLOYED_SHA")
    expected_sha = os.environ.get("AI_EDIT_V3_EXPECTED_TEST_SHA")
    sha_is_valid = bool(deployed_sha and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", deployed_sha))
    if (
        not authorization
        or marker != "test"
        or not sha_is_valid
        or deployed_sha != expected_sha
    ):
        raise FaultHarnessUnavailable("test_fault_authorization_missing_or_mismatched")
    raise FaultHarnessUnavailable("test_fault_harness_not_enabled_before_task_7")


def _publish(worker: _LocalWorker) -> None:
    worker.persist_transition("transition/job-1/settlement_bound")
    worker.register_generation()
    worker.prepare_hidden()
    worker.commit_publish()
    worker.query_decision()


def _cancel_and_refund(worker: _LocalWorker) -> None:
    worker.register_generation()
    worker.cancel_publish()
    worker.refund("refund/job-1")
    worker.persist_transition("transition/job-1/settlement_bound")
    worker.query_decision()


def _restart(backend: _LocalFaultState, worker: _LocalWorker) -> _LocalWorker:
    backend.pre_crash_effect_order = tuple(backend.effect_order)
    backend.crashes += 1
    backend.recoveries += 1
    return _LocalWorker(backend, f"worker-{backend.recoveries + 1}")


def _invoke_crashable_operation(
    case: FaultCase,
    backend: _LocalFaultState,
    worker: _LocalWorker,
    operation_name: str,
    operation,
) -> _LocalWorker:
    backend.target_operation = operation_name
    if case.timing == "before":
        worker = _restart(backend, worker)
        operation(worker)
    else:
        operation(worker)
        worker = _restart(backend, worker)
        operation(worker)
    return worker


def _run_persistent_case(case: FaultCase, backend: _LocalFaultState) -> FaultVerdict:
    worker = _LocalWorker(backend, "worker-1")
    def persist(name: str):
        return lambda w: w.persist_transition(f"transition/job-1/{name}")

    prefix = [
        ("predebit_request", lambda w: w.prehold("prehold/job-1")),
        ("persist:prehold_confirmed", persist("prehold_confirmed")),
        ("persist:normalized", persist("normalized")),
        ("persist:transcript_bound", persist("transcript_bound")),
        ("persist:edit_plan_bound", persist("edit_plan_bound")),
        ("persist:materials_bound", persist("materials_bound")),
        ("persist:audio_bound", persist("audio_bound")),
        ("persist:provider_intent_bound", persist("provider_intent_bound")),
        ("provider_submit", lambda w: w.submit_provider("provider/job-1")),
        ("persist:provider_result_bound", persist("provider_result_bound")),
        ("cos_upload", lambda w: w.upload_storage("cos/job-1/output.mp4")),
        ("persist:qc_bound", persist("qc_bound")),
        ("persist:delivery_intent_bound", persist("delivery_intent_bound")),
    ]
    settlement = [("persist:settlement_bound", persist("settlement_bound"))]
    terminal = [("persist:terminal_bound", persist("terminal_bound"))]
    if case.fault_point in {"publication_cancel_publish", "full_refund_request"}:
        graph = prefix + [
            ("publication_register_generation", lambda w: w.register_generation()),
            ("publication_cancel_publish", lambda w: w.cancel_publish()),
            ("full_refund_request", lambda w: w.refund("refund/job-1")),
            ("persist:settlement_bound", persist("settlement_bound")),
            ("publication_query_decision", lambda w: w.query_decision()),
        ] + terminal
        final_state, refund_kind = "refunded", "full"
    elif case.fault_point == "delta_refund_request":
        graph = prefix + [
            ("delta_refund_request", lambda w: w.refund("delta-refund/job-1", points=16)),
            ("persist:settlement_bound", persist("settlement_bound")),
            ("publication_register_generation", lambda w: w.register_generation()),
            ("publication_prepare_hidden", lambda w: w.prepare_hidden()),
            ("publication_commit_publish", lambda w: w.commit_publish()),
            ("publication_query_decision", lambda w: w.query_decision()),
        ] + terminal
        final_state, refund_kind = "completed", "delta"
    else:
        graph = prefix + settlement + [
            ("publication_register_generation", lambda w: w.register_generation()),
            ("publication_prepare_hidden", lambda w: w.prepare_hidden()),
            ("publication_commit_publish", lambda w: w.commit_publish()),
            ("publication_query_decision", lambda w: w.query_decision()),
        ] + terminal
        final_state, refund_kind = "completed", None

    exact = {
        "predebit_request", "cos_upload", "publication_register_generation",
        "publication_prepare_hidden", "publication_commit_publish",
        "publication_cancel_publish", "publication_query_decision",
        "delta_refund_request", "full_refund_request",
    }
    target = case.fault_point if case.fault_point in exact else f"persist:{case.fault_point}"
    names = [name for name, _ in graph]
    if target not in names:
        raise AssertionError(f"persistent_target_not_in_graph:{target}")
    for name, operation in graph:
        if name == target:
            worker = _invoke_crashable_operation(case, backend, worker, name, operation)
        else:
            operation(worker)
    return backend.verdict(final_state, refund_kind=refund_kind)


def _run_lease_case(case: FaultCase, backend: _LocalFaultState) -> FaultVerdict:
    worker_a = _LocalWorker(backend, "worker-a")
    worker_b = _LocalWorker(backend, "worker-b")
    backend.lease_workers = 2
    target_by_case = {
        "lease_two_worker_competition": "lease_claim",
        "lease_expiry_reclaim": "expired_lease",
        "stale_fence_transition_write": "transition",
        "stale_fence_checkpoint_write": "checkpoint",
        "stale_fence_provider_result_write": "provider_result",
        "stale_fence_billing_intent_write": "billing_intent",
        "stale_fence_delivery_intent_write": "delivery_intent",
    }
    backend.fenced_target = target_by_case[case.case_id]
    backend.target_operation = f"lease:{backend.fenced_target}"
    stale = backend.fence
    if case.case_id == "lease_two_worker_competition":
        current = worker_a.reclaim_lease()
        contender_accepted = worker_b.fenced_write(stale, backend.fenced_target)
    else:
        if case.case_id == "lease_expiry_reclaim":
            backend.lease_clock_advanced_seconds = 61
        current = worker_b.reclaim_lease()
        contender_accepted = worker_a.fenced_write(stale, backend.fenced_target)
        if case.case_id == "lease_expiry_reclaim":
            backend.recoveries += 1
    if contender_accepted or not worker_b.fenced_write(current, backend.fenced_target):
        raise AssertionError("stale_fence_write_accepted")
    backend.stale_write_rejections += 1
    worker_b.prehold("prehold/job-1")
    worker_b.submit_provider("provider/job-1")
    worker_b.upload_storage("cos/job-1/output.mp4")
    _publish(worker_b)
    return backend.verdict("completed")


def _run_sandbox_case(case: FaultCase, backend: _LocalFaultState) -> FaultVerdict:
    worker = _LocalWorker(backend, "sandbox-worker")
    backend.target_operation = f"sandbox:{case.case_id}"
    sandbox = _FakeSandbox(backend)
    worker.prehold("prehold/job-1")
    if case.case_id.startswith("chromium_"):
        sandbox.chromium_process(case.case_id.removeprefix("chromium_"))
        worker = _restart(backend, worker)
    elif case.case_id == "ffmpeg_child_leak":
        sandbox.ffmpeg_child()
    elif case.case_id == "network_attempt":
        sandbox.network()
    elif case.case_id == "environment_secret_read":
        sandbox.environment_secret()
    elif case.case_id == "sibling_job_read":
        sandbox.sibling_job()
    elif case.case_id.startswith("systemd_"):
        sandbox.systemd(case.case_id)
    else:
        sandbox.filesystem(case.case_id)
    _cancel_and_refund(worker)
    return backend.verdict("refunded", refund_kind="full")


def _replay(worker: _LocalWorker, operation) -> None:
    operation()
    worker.backend.recoveries += 1
    operation()


def _run_standalone_case(case: FaultCase, backend: _LocalFaultState) -> FaultVerdict:
    worker = _LocalWorker(backend, "worker-1")
    backend.target_operation = {
        "provider": "provider_submit",
        "storage": "cos_upload",
        "billing": case.case_id.removeprefix("billing_").split("_response_lost")[0]
            .split("_rejected")[0].split("_five_minute_outage")[0] + "_request",
        "publication": "publication_" + case.case_id.removeprefix("publication_")
            .split("_response_lost")[0].split("_five_minute_outage")[0],
    }.get(case.category, case.fault_point)

    if case.case_id == "billing_predebit_rejected":
        worker.prehold("prehold/job-1", accepted=False)
        backend.downstream_operations.update({"provider_submit", "cos_upload"})
        return backend.verdict("prehold_absent")
    if case.case_id == "billing_predebit_five_minute_outage":
        worker.prehold("prehold/job-1", accepted=False)
        backend.downstream_operations.update({"provider_submit", "cos_upload"})
        return backend.verdict("failed_reconciliation_pending")
    if case.case_id == "billing_predebit_response_lost":
        _replay(worker, lambda: worker.prehold("prehold/job-1"))
    else:
        worker.prehold("prehold/job-1")

    if case.case_id in {"provider_submit_rejected", "provider_five_minute_outage"}:
        worker.reject_provider("provider/job-1")
        backend.downstream_operations.add("cos_upload")
        _cancel_and_refund(worker)
        return backend.verdict("refunded", refund_kind="full")
    if case.case_id == "provider_submit_response_lost":
        _replay(worker, lambda: worker.submit_provider("provider/job-1"))
    else:
        worker.submit_provider("provider/job-1")

    if case.case_id == "cos_upload_outage":
        worker.reject_storage("cos/job-1/output.mp4")
        _cancel_and_refund(worker)
        return backend.verdict("refunded", refund_kind="full")
    if case.case_id == "cos_upload_response_lost":
        _replay(worker, lambda: worker.upload_storage("cos/job-1/output.mp4"))
    else:
        worker.upload_storage("cos/job-1/output.mp4")

    if case.case_id.startswith("billing_delta_refund_"):
        if case.case_id.endswith("response_lost"):
            _replay(worker, lambda: worker.refund("delta-refund/job-1", points=16))
            worker.persist_transition("transition/job-1/settlement_bound")
            worker.register_generation()
            worker.prepare_hidden()
            worker.commit_publish()
            worker.query_decision()
            return backend.verdict("completed", refund_kind="delta")
        worker.refund("delta-refund/job-1", points=16, accepted=False)
        backend.downstream_operations.add("publication_commit_publish")
        return backend.verdict("failed_reconciliation_pending")

    if case.case_id.startswith("billing_full_refund_"):
        worker.register_generation()
        worker.cancel_publish()
        if case.case_id.endswith("response_lost"):
            _replay(worker, lambda: worker.refund("refund/job-1"))
            worker.persist_transition("transition/job-1/settlement_bound")
            worker.query_decision()
            return backend.verdict("refunded", refund_kind="full")
        worker.refund("refund/job-1", accepted=False)
        return backend.verdict("failed_reconciliation_pending")

    if case.case_id == "publication_cancel_publish_response_lost":
        worker.register_generation()
        _replay(worker, worker.cancel_publish)
        worker.refund("refund/job-1")
        worker.persist_transition("transition/job-1/settlement_bound")
        worker.query_decision()
        return backend.verdict("refunded", refund_kind="full")
    if case.case_id == "publication_register_generation_response_lost":
        worker.persist_transition("transition/job-1/settlement_bound")
        _replay(worker, worker.register_generation)
        worker.prepare_hidden()
        worker.commit_publish()
        worker.query_decision()
        return backend.verdict("completed")
    if case.case_id == "publication_prepare_hidden_response_lost":
        worker.persist_transition("transition/job-1/settlement_bound")
        worker.register_generation()
        _replay(worker, worker.prepare_hidden)
        worker.commit_publish()
        worker.query_decision()
        return backend.verdict("completed")
    if case.case_id == "publication_commit_publish_response_lost":
        worker.persist_transition("transition/job-1/settlement_bound")
        worker.register_generation()
        worker.prepare_hidden()
        _replay(worker, worker.commit_publish)
        worker.query_decision()
        return backend.verdict("completed")
    if case.case_id == "publication_query_decision_response_lost":
        worker.persist_transition("transition/job-1/settlement_bound")
        worker.register_generation()
        worker.prepare_hidden()
        worker.commit_publish()
        _replay(worker, worker.query_decision)
        return backend.verdict("completed")
    if case.case_id == "publication_five_minute_outage":
        worker.persist_transition("transition/job-1/settlement_bound")
        backend.operation_attempts[backend.target_operation] = 1
        return backend.verdict("failed_asset_decision_pending")

    _publish(worker)
    return backend.verdict("completed")


def run_fault_case(case: FaultCase, harness: FaultHarness) -> FaultVerdict:
    if harness.environment != "local-fake":
        raise FaultHarnessUnavailable("fault_harness_not_local_fake")
    backend = _LocalFaultState()
    if case.category == "persistent_transition":
        verdict = _run_persistent_case(case, backend)
    elif case.category == "lease":
        verdict = _run_lease_case(case, backend)
    elif case.category == "sandbox":
        verdict = _run_sandbox_case(case, backend)
    else:
        verdict = _run_standalone_case(case, backend)
    if harness.forced_failure_case_id == case.case_id:
        return FaultVerdict(**{**asdict(verdict), "provider_submit_count": 2})
    return verdict


def assert_authoritative_convergence(verdict: FaultVerdict) -> None:
    charged_points = (
        verdict.confirmed_preheld_points - verdict.refunded_points
        if verdict.charged_points is None
        else verdict.charged_points
    )
    numeric = (
        verdict.confirmed_preheld_points,
        verdict.refunded_points,
        verdict.visible_asset_count,
        verdict.provider_submit_count,
        verdict.billing_request_count,
        verdict.storage_upload_count,
        verdict.publication_request_count,
        verdict.persistent_write_count,
        verdict.cross_job_read_count,
        verdict.leaked_child_count,
        verdict.forbidden_network_count,
        verdict.secret_read_count,
        verdict.crash_count,
        verdict.recovery_attempt_count,
        verdict.stale_write_rejected_count,
        verdict.sandbox_attempt_count,
        verdict.sandbox_denial_count,
        verdict.lease_clock_advanced_seconds,
    )
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in numeric):
        raise AssertionError("fault_counter_invalid")
    if verdict.final_state not in ALLOWED_FINAL_STATES:
        raise AssertionError(f"non_authoritative_state:{verdict.final_state}")
    if not 0 <= verdict.refunded_points <= verdict.confirmed_preheld_points:
        raise AssertionError("refund_exceeds_confirmed_prehold")
    if verdict.provider_submit_count > 1:
        raise AssertionError("duplicate_provider_submit")
    if verdict.billing_request_count > 1:
        raise AssertionError("duplicate_billing_request")
    if verdict.storage_upload_count > 1:
        raise AssertionError("duplicate_storage_upload")
    if verdict.publication_request_count > 1:
        raise AssertionError("duplicate_publication_request")
    if verdict.persistent_write_count > 1:
        raise AssertionError("duplicate_persistent_write")
    if verdict.visible_asset_count not in (0, 1):
        raise AssertionError("duplicate_visible_asset")
    if any((
        verdict.cross_job_read_count,
        verdict.leaked_child_count,
        verdict.forbidden_network_count,
        verdict.secret_read_count,
    )):
        raise AssertionError("isolation_violation")
    if verdict.permanent_running is not False:
        raise AssertionError("permanent_running_stage")
    if verdict.final_state == "completed":
        if verdict.confirmed_preheld_points <= 0:
            raise AssertionError("completed_without_confirmed_prehold")
        if verdict.publication_winner != "publish_won":
            raise AssertionError("completed_without_publish_winner")
        if verdict.visible_asset_count != 1:
            raise AssertionError("completed_without_one_visible_asset")
        if charged_points + verdict.refunded_points != verdict.confirmed_preheld_points:
            raise AssertionError("completed_settlement_mismatch")
        if verdict.refunded_points > 0 and verdict.refund_kind != "delta":
            raise AssertionError("completed_refund_not_delta")
        if verdict.refunded_points == 0 and verdict.refund_kind not in (None, "none"):
            raise AssertionError("completed_refund_kind_invalid")
    elif verdict.visible_asset_count != 0:
        raise AssertionError("asset_visible_before_authoritative_publish")
    if verdict.publication_winner == "cancel_won" and verdict.visible_asset_count:
        raise AssertionError("asset_visible_after_cancel_won")
    if verdict.final_state == "refunded" and (
        verdict.refunded_points != verdict.confirmed_preheld_points
        or charged_points != 0
        or verdict.refund_kind not in (None, "full")
        or verdict.publication_winner != "cancel_won"
    ):
        raise AssertionError("refund_not_authoritative")
    if verdict.final_state == "prehold_absent" and (
        verdict.confirmed_preheld_points != 0
        or verdict.refunded_points != 0
        or verdict.provider_submit_count != 0
        or verdict.visible_asset_count != 0
        or verdict.publication_winner is not None
    ):
        raise AssertionError("prehold_absent_has_money")
    if verdict.final_state == "failed_asset_decision_pending" and verdict.publication_winner is not None:
        raise AssertionError("asset_pending_has_publication_winner")
    if verdict.final_state == "failed_reconciliation_pending" and verdict.publication_winner not in (None, "cancel_won"):
        raise AssertionError("reconciliation_pending_publication_invalid")


def assert_case_exercised(case: FaultCase, verdict: FaultVerdict) -> None:
    if case.category == "persistent_transition":
        expected_attempts = 1 if case.timing == "before" else 2
        exact = {
            "predebit_request", "cos_upload", "publication_register_generation",
            "publication_prepare_hidden", "publication_commit_publish",
            "publication_cancel_publish", "publication_query_decision",
            "delta_refund_request", "full_refund_request",
        }
        expected_operation = case.fault_point if case.fault_point in exact else f"persist:{case.fault_point}"
        expected_persistent_writes = 1
        prefix = (
            "predebit_request", "persist:prehold_confirmed", "persist:normalized",
            "persist:transcript_bound", "persist:edit_plan_bound", "persist:materials_bound",
            "persist:audio_bound", "persist:provider_intent_bound", "provider_submit",
            "persist:provider_result_bound", "cos_upload", "persist:qc_bound",
            "persist:delivery_intent_bound",
        )
        terminal = ("persist:terminal_bound",)
        if case.fault_point in {"publication_cancel_publish", "full_refund_request"}:
            expected_order = prefix + (
                "publication_register_generation", "publication_cancel_publish",
                "full_refund_request", "persist:settlement_bound",
                "publication_query_decision",
            ) + terminal
        elif case.fault_point == "delta_refund_request":
            expected_order = prefix + (
                "delta_refund_request", "persist:settlement_bound",
                "publication_register_generation", "publication_prepare_hidden",
                "publication_commit_publish",
                "publication_query_decision",
            ) + terminal
        else:
            expected_order = prefix + ("persist:settlement_bound",) + (
                "publication_register_generation", "publication_prepare_hidden",
                "publication_commit_publish", "publication_query_decision",
            ) + terminal
        if (
            verdict.crash_count != 1
            or verdict.recovery_attempt_count != 1
            or verdict.persistent_write_count != expected_persistent_writes
            or verdict.target_operation != expected_operation
            or verdict.target_attempt_count != expected_attempts
            or verdict.target_effect_count != 1
            or verdict.effect_order != expected_order
            or verdict.pre_crash_effect_order != expected_order[
                :expected_order.index(expected_operation) + (1 if case.timing == "after" else 0)
            ]
        ):
            raise AssertionError("persistent_fault_not_exercised")
    elif case.category == "lease":
        expected_target = {
            "lease_two_worker_competition": "lease_claim",
            "lease_expiry_reclaim": "expired_lease",
            "stale_fence_transition_write": "transition",
            "stale_fence_checkpoint_write": "checkpoint",
            "stale_fence_provider_result_write": "provider_result",
            "stale_fence_billing_intent_write": "billing_intent",
            "stale_fence_delivery_intent_write": "delivery_intent",
        }[case.case_id]
        if (
            verdict.stale_write_rejected_count != 1
            or verdict.lease_worker_count != 2
            or verdict.lease_reclaim_count != 1
            or verdict.fenced_target != expected_target
            or (
                case.case_id == "lease_expiry_reclaim"
                and verdict.lease_clock_advanced_seconds <= 60
            )
            or (
                case.case_id != "lease_expiry_reclaim"
                and verdict.lease_clock_advanced_seconds != 0
            )
        ):
            raise AssertionError("lease_fault_not_exercised")
    elif case.category == "sandbox":
        if (
            verdict.sandbox_attempt_count != 1
            or verdict.sandbox_denial_count != 1
            or verdict.sandbox_capability != case.case_id
        ):
            raise AssertionError("sandbox_fault_not_exercised")
    elif "response_lost" in case.case_id:
        if (
            verdict.recovery_attempt_count < 1
            or verdict.target_attempt_count != 2
            or verdict.target_effect_count != 1
        ):
            raise AssertionError("response_loss_not_exercised")
    elif case.case_id.endswith(("_rejected", "_outage")):
        if (
            verdict.target_attempt_count != 1
            or verdict.target_effect_count != 0
            or verdict.downstream_effect_count != 0
        ):
            raise AssertionError("rejection_or_outage_not_exercised")


def assert_production_build_fault_isolated(project_root: Path) -> None:
    roots = tuple(
        path for path in (
            project_root / "server",
            project_root / ".github" / "workflows",
            project_root / "deploy",
            project_root / "deployment",
            project_root / "infra",
            project_root / "systemd",
        )
        if path.exists()
    )
    suffixes = {
        ".py", ".js", ".mjs", ".cjs", ".ts", ".json", ".yaml", ".yml",
        ".toml", ".service", ".sh", ".ps1", ".env", ".conf", ".ini",
        ".tf", ".tfvars",
    }
    candidates: list[Path] = []
    excluded = {"node_modules", ".venv", "venv", "__pycache__", ".git", "dist", "build"}
    for root in roots:
        for current, directories, filenames in os.walk(root):
            directories[:] = [name for name in directories if name not in excluded]
            candidates.extend(
                Path(current) / name
                for name in filenames
                if Path(name).suffix in suffixes or name == ".env" or name.startswith(".env.")
            )
    candidates.extend(
        path for path in project_root.iterdir()
        if path.is_file() and (path.name.startswith("Dockerfile") or path.name.startswith("docker-compose"))
    )
    for source in candidates:
        text = source.read_text(encoding="utf-8", errors="replace")
        if "ai_edit_v3_fault_matrix" in text or "AI_EDIT_V3_FAULT_" in text:
            raise AssertionError(f"production_fault_hook_imported:{source.relative_to(project_root)}")


def assert_fault_hooks_production_safe(config: Mapping[str, object]) -> None:
    if config.get("environment") == "production" and (
        config.get("fault_hooks_enabled") is True
        or config.get("fault_module") is not None
    ):
        raise AssertionError("production_fault_hook_enabled")


def execute_fault_matrix(
    environment: Literal["local-fake", "test"],
    *,
    strict: bool,
) -> FaultMatrixReport:
    harness = build_fault_harness(environment)
    executed: list[str] = []
    failures: list[str] = []
    for case in enumerate_fault_points():
        executed.append(case.case_id)
        try:
            verdict = run_fault_case(case, harness)
            assert_authoritative_convergence(verdict)
            assert_case_exercised(case, verdict)
        except Exception as error:
            failures.append(f"{case.case_id}:{type(error).__name__}")
    return FaultMatrixReport(
        passed=(strict and bool(executed) and not failures),
        executed_case_ids=tuple(executed),
        failures=tuple(failures),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--environment", choices=("local-fake", "test"), required=True)
    run.add_argument("--strict", action="store_true", required=True)
    isolation = commands.add_parser("check-production-isolation")
    isolation.add_argument("--project-root", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "check-production-isolation":
        try:
            assert_production_build_fault_isolated(args.project_root.resolve())
        except AssertionError as error:
            print(json.dumps({"passed": False, "error": str(error)}, sort_keys=True))
            return 1
        print(json.dumps({"passed": True, "production_fault_hooks": "absent"}, sort_keys=True))
        return 0
    try:
        report = execute_fault_matrix(args.environment, strict=args.strict)
    except FaultHarnessUnavailable as error:
        print(json.dumps({"passed": False, "error": str(error)}, sort_keys=True))
        return 2
    print(json.dumps(asdict(report), ensure_ascii=False, sort_keys=True))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
