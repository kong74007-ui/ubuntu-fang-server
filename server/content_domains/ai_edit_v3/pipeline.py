"""Crash-safe, fenced one-stage execution for AI Edit V3."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .billing import list_due_billing_intents, reconcile_unknown_intent
from .contracts import ALLOWED_TRANSITIONS, MEDIA_STATES, RECONCILIATION_STATES, LeaseClaim
from .delivery import list_due_publish_intents, reconcile_asset_decision
from .runtime import LeaseHeartbeat, RuntimeDependencies, StageContext, StageOutcome
from .store import LeaseLost


@dataclass(frozen=True, slots=True)
class JobRunResult:
    job_id: str
    state: str
    status: str
    error_code: str | None = None


class _StageFailure(RuntimeError):
    def __init__(self, error_code: str):
        self.error_code = error_code
        super().__init__(error_code)


def _now_ms(runtime: RuntimeDependencies) -> int:
    value = runtime.clock.now()
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError("pipeline_clock_invalid")
    return int(value * 1000)


def _checkpoint_payload(outcome: StageOutcome) -> dict[str, Any]:
    provider = outcome.provider_result
    provider_evidence = None
    if provider is not None:
        provider_evidence = {
            "provider": provider.provider,
            "capability": provider.capability,
            "request_id_present": provider.request_id is not None,
            "usage": dict(provider.usage),
            "elapsed_ms": provider.elapsed_ms,
        }
    return {
        "next_state": outcome.next_state,
        "checkpoint": dict(outcome.checkpoint),
        "provider_evidence": provider_evidence,
    }


def _checkpoint_next_state(row: Mapping[str, Any]) -> str:
    payload = json.loads(row["output_json"])
    if not isinstance(payload, dict) or not isinstance(payload.get("next_state"), str):
        raise ValueError("pipeline_checkpoint_invalid")
    return payload["next_state"]


def run_job(
    claim: LeaseClaim,
    runtime: RuntimeDependencies,
    *,
    db_path: Path | None = None,
    stop_event: object | None = None,
    lease_seconds: int = 30,
    queue_timeout_ms: int | None = None,
) -> JobRunResult:
    """Execute at most one fenced media-state transition."""

    if not isinstance(runtime, RuntimeDependencies):
        raise TypeError("pipeline_runtime_invalid")
    store = runtime.store
    if (
        queue_timeout_ms is not None
        and (
            isinstance(queue_timeout_ms, bool)
            or not isinstance(queue_timeout_ms, int)
            or queue_timeout_ms <= 0
        )
    ):
        raise ValueError("pipeline_queue_timeout_invalid")
    if db_path is not None and Path(db_path).resolve() != store.db_path.resolve():
        raise ValueError("pipeline_store_mismatch")
    now_ms = _now_ms(runtime)
    job = store.get_job_for_claim(claim, now_ms, environment=store.environment)
    state = job["state"]

    if state == "failed":
        if not store.transition_leased(
            claim, {"failed"}, "refund_pending", now_ms, lease_seconds=lease_seconds
        ):
            raise LeaseLost("lease_lost", "fenced failed transition was rejected")
        store.release_lease(claim, now_ms)
        return JobRunResult(claim.job_id, "refund_pending", "transitioned")
    if state not in MEDIA_STATES:
        store.release_lease(claim, now_ms)
        return JobRunResult(claim.job_id, state, "no_media_work")

    terminated = False
    attempt: dict[str, Any] | None = None
    heartbeat = LeaseHeartbeat(
        claim, lease_seconds, runtime.clock, store.renew_lease
    )

    def terminate_once() -> None:
        nonlocal terminated
        if not terminated:
            runtime.process_supervisor.terminate_job(claim.job_id)
            terminated = True

    def assert_active() -> None:
        heartbeat.assert_active()
        current_ms = _now_ms(runtime)
        if stop_event is not None and stop_event.is_set():
            raise RuntimeError("pipeline_stopped")
        deadline = job.get("processing_deadline_at")
        if deadline is not None and current_ms >= deadline:
            raise RuntimeError("pipeline_deadline_exceeded")
        if (
            state == "queued"
            and queue_timeout_ms is not None
            and job.get("queued_at") is not None
            and current_ms >= job["queued_at"] + queue_timeout_ms
        ):
            raise RuntimeError("pipeline_queue_timeout")
        if not store.lease_owned(claim, current_ms):
            raise LeaseLost("lease_lost", "lease ownership was lost")

    input_sha256 = job["request_sha256"]
    try:
        assert_active()
    except LeaseLost:
        terminate_once()
        raise
    except RuntimeError as exc:
        terminate_once()
        error_code = str(exc)
        current_ms = _now_ms(runtime)
        if not store.lease_owned(claim, current_ms):
            raise LeaseLost("lease_lost", "lease ownership was lost") from exc
        if error_code == "pipeline_stopped":
            store.release_lease(claim, current_ms)
            return JobRunResult(claim.job_id, state, "interrupted", error_code)
        attempt = store.start_stage_attempt(
            claim, state, input_sha256, current_ms
        )
        store.finish_stage_attempt(
            claim,
            attempt["id"],
            "failed",
            current_ms,
            error_code=error_code,
        )
        if not store.transition_leased(
            claim, {state}, "failed", current_ms, lease_seconds=lease_seconds
        ):
            raise LeaseLost("lease_lost", "fenced interruption failure was rejected")
        store.release_lease(claim, current_ms)
        return JobRunResult(claim.job_id, "failed", "failed", error_code)
    checkpoint = store.get_checkpoint_for_claim(
        claim, state, input_sha256, now_ms
    )
    if checkpoint is not None:
        next_state = _checkpoint_next_state(checkpoint)
        if not store.transition_leased(
            claim, {state}, next_state, now_ms, lease_seconds=lease_seconds
        ):
            raise LeaseLost("lease_lost", "fenced checkpoint replay was rejected")
        store.release_lease(claim, now_ms)
        return JobRunResult(claim.job_id, next_state, "checkpoint_replayed")

    attempt = store.start_stage_attempt(claim, state, input_sha256, now_ms)
    handler = runtime.stage_handlers.get(state)
    if handler is None:
        store.finish_stage_attempt(
            claim,
            attempt["id"],
            "failed",
            now_ms,
            error_code="capability_unavailable",
        )
        if not store.transition_leased(
            claim, {state}, "failed", now_ms, lease_seconds=lease_seconds
        ):
            raise LeaseLost("lease_lost", "fenced capability failure was rejected")
        store.release_lease(claim, now_ms)
        return JobRunResult(
            claim.job_id, "failed", "failed", "capability_unavailable"
        )

    context = StageContext(
        claim,
        attempt["id"],
        attempt["id"],
        job["processing_deadline_at"] / 1000,
        assert_active,
    )
    heartbeat.start()
    attempt_finished = False
    try:
        outcome = handler(job, context)
        if not isinstance(outcome, StageOutcome):
            raise ValueError("pipeline_stage_outcome_invalid")
        if outcome.checkpoint_input_sha256 != input_sha256:
            raise ValueError("pipeline_checkpoint_input_mismatch")
        if outcome.next_state not in ALLOWED_TRANSITIONS[state]:
            raise _StageFailure("invalid_stage_transition")
        assert_active()
        store.save_checkpoint(
            claim,
            attempt["id"],
            input_sha256,
            _checkpoint_payload(outcome),
            _now_ms(runtime),
        )
        status = "skipped" if outcome.checkpoint.get("skipped") is True else "completed"
        store.finish_stage_attempt(
            claim, attempt["id"], status, _now_ms(runtime)
        )
        attempt_finished = True
        if not store.transition_leased(
            claim,
            {state},
            outcome.next_state,
            _now_ms(runtime),
            lease_seconds=lease_seconds,
        ):
            raise LeaseLost("lease_lost", "fenced stage transition was rejected")
        store.release_lease(claim, _now_ms(runtime))
        return JobRunResult(claim.job_id, outcome.next_state, status)
    except LeaseLost:
        heartbeat.close()
        terminate_once()
        current_ms = _now_ms(runtime)
        if store.lease_owned(claim, current_ms):
            store.close_running_attempts(claim, current_ms)
            store.release_lease(claim, current_ms)
        raise
    except Exception as exc:
        heartbeat.close()
        terminate_once()
        current_ms = _now_ms(runtime)
        error_code = (
            exc.error_code if isinstance(exc, _StageFailure) else "stage_failed"
        )
        if store.lease_owned(claim, current_ms):
            if not attempt_finished:
                store.finish_stage_attempt(
                    claim,
                    attempt["id"],
                    "failed",
                    current_ms,
                    error_code=error_code,
                    error={"type": type(exc).__name__},
                )
            store.transition_leased(
                claim, {state}, "failed", current_ms, lease_seconds=lease_seconds
            )
            store.release_lease(claim, current_ms)
        return JobRunResult(claim.job_id, "failed", "failed", error_code)
    finally:
        heartbeat.close()


def run_reconciliation_pass(
    runtime: RuntimeDependencies,
    *,
    worker_id: str = "ai-edit-v3-reconciler",
    lease_seconds: int = 30,
    limit: int = 100,
) -> dict[str, int]:
    """Process billing then asset authority decisions with fenced claims."""

    now_ms = _now_ms(runtime)
    counts = {"billing": 0, "assets": 0}
    for intent in list_due_billing_intents(now=now_ms, store=runtime.store, limit=limit):
        claim = runtime.store.claim_job(
            intent.job_id,
            worker_id,
            lease_seconds,
            now_ms,
            expected_states=RECONCILIATION_STATES,
        )
        if claim is None:
            continue
        job = runtime.store.get_job_for_claim(claim, now_ms)
        outcome = reconcile_unknown_intent(
            intent.intent_id,
            claim=claim,
            ledger=runtime.points,
            now=now_ms,
            store=runtime.store,
        )
        if runtime.store.transition_leased(
            claim,
            {job["state"]},
            outcome.next_state,
            now_ms,
            lease_seconds=lease_seconds,
        ):
            counts["billing"] += 1
        runtime.store.release_lease(claim, now_ms)

    for row in list_due_publish_intents(now=now_ms, store=runtime.store, limit=limit):
        claim = runtime.store.claim_job(
            row["job_id"],
            worker_id,
            lease_seconds,
            now_ms,
            expected_states=RECONCILIATION_STATES,
        )
        if claim is None:
            continue
        job = runtime.store.get_job_for_claim(claim, now_ms)
        progress = reconcile_asset_decision(
            claim,
            now=now_ms,
            store=runtime.store,
            publisher=runtime.assets,
        )
        if runtime.store.transition_leased(
            claim,
            {job["state"]},
            progress.next_state,
            now_ms,
            lease_seconds=lease_seconds,
        ):
            counts["assets"] += 1
        runtime.store.release_lease(claim, now_ms)
    return counts


__all__ = ("JobRunResult", "run_job", "run_reconciliation_pass")
