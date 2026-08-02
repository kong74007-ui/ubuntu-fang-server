"""Crash-safe, fenced one-stage execution for AI Edit V3."""

from __future__ import annotations

import hashlib
import json
import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .billing import (
    BillingError,
    list_due_billing_intents,
    process_pending_intent,
    reconcile_unknown_intent,
    request_delta_refund,
)
from .contracts import (
    ALLOWED_TRANSITIONS,
    MEDIA_STATES,
    LeaseClaim,
    request_fingerprint,
)
from .delivery import (
    advance_publish,
    create_publish_intent,
    list_due_publish_intents,
    reconcile_asset_decision,
)
from .providers import SubmissionUnknown
from .runtime import LeaseHeartbeat, RuntimeDependencies, StageContext, StageOutcome
from .store import LeaseLost, StoreConfigurationError, StoreConflictError


LOG = logging.getLogger("ai-edit-v3")


@dataclass(frozen=True, slots=True)
class JobRunResult:
    job_id: str
    state: str
    status: str
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class StageResult:
    next_stage: str


_PHASE_B_STAGES = (
    "generating_voice",
    "normalizing",
    "transcribing",
    "aligning",
    "planning",
    "resolving_materials",
    "generating_images",
)


def run_source_and_director_stages(
    claim: Any,
    runtime: Any,
    *,
    db_path: Path,
) -> StageResult:
    """Deterministic administrative runner; production transitions stay in run_job."""

    if not isinstance(db_path, Path):
        raise TypeError("phase_b_db_path_invalid")
    for stage_name in _PHASE_B_STAGES:
        runtime.run_phase_b_stage(stage_name, claim, db_path)
    return StageResult(next_stage="generating_audio")


class _StageFailure(RuntimeError):
    def __init__(self, error_code: str):
        self.error_code = error_code
        super().__init__(error_code)


def _query_historical_publish_authority(
    claim: LeaseClaim,
    runtime: RuntimeDependencies,
    publish_generation: int,
    now_ms: int,
) -> dict[str, Any]:
    """Query only the frozen authority key of an unresolved older generation."""

    authority = runtime.store.get_historical_publish_authority_for_claim(
        claim, publish_generation, now_ms
    )
    try:
        raw_decision = runtime.assets.query_decision(
            "ai_edit_v3",
            claim.job_id,
            authority["query"]["external_idempotency_key"],
        )
        if raw_decision is None:
            evidence = {
                "asset_id": None,
                "current_generation": publish_generation,
                "status": "accepted",
            }
        else:
            evidence = {
                "asset_id": getattr(raw_decision, "asset_id", None),
                "current_generation": getattr(
                    raw_decision, "current_generation", None
                ),
                "status": getattr(raw_decision, "status", None),
            }
    except SubmissionUnknown as exc:
        evidence = {"outcome": "unknown", "reason_code": exc.reason_code}
    except Exception:
        evidence = {"outcome": "unknown", "reason_code": "ambiguous_exception"}
    return runtime.store.record_historical_publish_authority(
        claim,
        publish_generation,
        evidence,
        now_ms=now_ms,
    )


def _billing_expected_states(operation: str, status: str) -> frozenset[str]:
    if status in {"pending", "retryable_absent"}:
        return {
            "pre_debit": frozenset({"created_draft", "preholding"}),
            "refund_delta": frozenset({"settling", "refund_pending"}),
            "refund_full": frozenset({"refund_pending"}),
        }[operation]
    if status in {"unknown", "reconciliation_pending"}:
        resume_states = {
            "pre_debit": {"preholding"},
            "refund_delta": {"settling", "refund_pending"},
            "refund_full": {"refund_pending"},
        }[operation]
        return frozenset(
            (*resume_states, "billing_reconciling", "failed_reconciliation_pending")
        )
    raise ValueError("billing_intent_status_invalid")


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


def _stage_input_sha256(job: Mapping[str, Any], stage: str) -> str:
    request_sha256 = job["request_sha256"]
    repair_generation = job["repair_count"]
    if repair_generation == 0:
        return request_sha256
    return request_fingerprint(
        {
            "contract": "ai-edit-v3-stage-input-v1",
            "request_sha256": request_sha256,
            "stage": stage,
            "repair_generation": repair_generation,
            "repair_budget_granted_at": job["repair_budget_granted_at"],
        }
    )


def _safe_identifier(value: object) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]


def _log_reconciliation_error(
    operation: str, identifier: object, exc: Exception
) -> None:
    LOG.error(
        "[ai-edit-v3] reconciliation intent deferred "
        "operation=%s identifier=%s error_type=%s",
        operation,
        _safe_identifier(identifier),
        type(exc).__name__,
    )


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


def _checkpoint_stage_data(row: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = json.loads(row["output_json"])
    checkpoint = payload.get("checkpoint") if isinstance(payload, dict) else None
    if not isinstance(checkpoint, dict):
        raise _StageFailure("pipeline_checkpoint_invalid")
    return checkpoint


def _staging_delivery_values(
    checkpoint: Mapping[str, Any],
    *,
    confirmed_preheld_total: int,
    environment: str,
) -> tuple[int, str, str]:
    actual_charge = checkpoint.get("actual_charge")
    metadata_sha256 = checkpoint.get("metadata_sha256")
    object_key = checkpoint.get("delivery_object_key")
    if (
        isinstance(actual_charge, bool)
        or not isinstance(actual_charge, int)
        or actual_charge < 0
        or actual_charge > confirmed_preheld_total
        or not isinstance(metadata_sha256, str)
        or len(metadata_sha256) != 64
        or any(character not in "0123456789abcdef" for character in metadata_sha256)
        or not isinstance(object_key, str)
        or not object_key
        or object_key != object_key.strip()
        or len(object_key) > 1_024
        or not object_key.startswith(f"{environment}/ai-edit-v3/")
        or ".." in object_key
        or "\\" in object_key
        or "?" in object_key
        or "#" in object_key
        or "://" in object_key
        or any(
            ord(character) < 0x20
            or 0x7F <= ord(character) <= 0x9F
            or 0xD800 <= ord(character) <= 0xDFFF
            for character in object_key
        )
    ):
        raise _StageFailure("staging_delivery_checkpoint_invalid")
    return actual_charge, metadata_sha256, object_key


def _prepare_publication(
    claim: LeaseClaim,
    runtime: RuntimeDependencies,
    values: tuple[int, str, str],
    now_ms: int,
) -> None:
    _actual_charge, metadata_sha256, object_key = values
    runtime.store.freeze_delivery_object_key(claim, object_key, now_ms)
    create_publish_intent(
        claim,
        metadata_sha256=metadata_sha256,
        now=now_ms,
        store=runtime.store,
    )


def _request_settlement(
    claim: LeaseClaim,
    runtime: RuntimeDependencies,
    values: tuple[int, str, str],
    now_ms: int,
    lease_seconds: int,
) -> str:
    actual_charge, _metadata_sha256, _object_key = values
    outcome = request_delta_refund(
        claim,
        actual_charge=actual_charge,
        now=now_ms,
        store=runtime.store,
    )
    job = runtime.store.get_job_for_claim(claim, now_ms)
    if job["state"] != outcome.next_state:
        if outcome.next_state not in ALLOWED_TRANSITIONS[job["state"]]:
            raise _StageFailure("settlement_state_transition_invalid")
        if not runtime.store.transition_leased(
            claim,
            {job["state"]},
            outcome.next_state,
            now_ms,
            lease_seconds=lease_seconds,
        ):
            raise LeaseLost("lease_lost", "fenced settlement transition was rejected")
    return outcome.next_state


def _settlement_safety_pending(
    claim: LeaseClaim,
    runtime: RuntimeDependencies,
    now_ms: int,
    error: Exception,
) -> JobRunResult:
    job = runtime.store.get_job_for_claim(claim, now_ms)
    error_code = getattr(error, "error_code", None)
    if not isinstance(error_code, str) or not error_code:
        error_code = str(error) if isinstance(error, ValueError) else "settlement_failed"
    if runtime.store.lease_owned(claim, now_ms):
        runtime.store.release_lease(claim, now_ms)
    return JobRunResult(
        claim.job_id,
        job["state"],
        "safety_pending",
        error_code,
    )


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
        refund = store.freeze_failed_full_refund(claim, now_ms=now_ms)
        result_state = refund["job"]["state"]
        if store.lease_owned(claim, now_ms):
            store.release_lease(claim, now_ms)
        return JobRunResult(claim.job_id, result_state, "transitioned")
    if state == "settling":
        checkpoint = store.get_checkpoint_for_claim(
            claim,
            "staging_delivery",
            _stage_input_sha256(job, "staging_delivery"),
            now_ms,
        )
        if checkpoint is None:
            store.release_lease(claim, now_ms)
            return JobRunResult(
                claim.job_id,
                "settling",
                "safety_pending",
                "actual_charge_unavailable",
            )
        try:
            values = _staging_delivery_values(
                _checkpoint_stage_data(checkpoint),
                confirmed_preheld_total=job["confirmed_preheld_total"],
                environment=store.environment,
            )
            _prepare_publication(claim, runtime, values, now_ms)
            result_state = _request_settlement(
                claim, runtime, values, now_ms, lease_seconds
            )
        except LeaseLost:
            raise
        except Exception as exc:
            return _settlement_safety_pending(claim, runtime, now_ms, exc)
        store.release_lease(claim, now_ms)
        return JobRunResult(
            claim.job_id, result_state, "settlement_requested"
        )
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

    input_sha256 = _stage_input_sha256(job, state)
    if job.get("processing_deadline_at") is None:
        terminate_once()
        current_ms = _now_ms(runtime)
        if not store.lease_owned(claim, current_ms):
            raise LeaseLost("lease_lost", "lease ownership was lost")
        attempt = store.start_stage_attempt(
            claim, state, input_sha256, current_ms
        )
        store.finish_stage_attempt(
            claim,
            attempt["id"],
            "failed",
            current_ms,
            error_code="processing_deadline_missing",
        )
        if not store.transition_leased(
            claim, {state}, "failed", current_ms, lease_seconds=lease_seconds
        ):
            raise LeaseLost("lease_lost", "fenced deadline failure was rejected")
        store.release_lease(claim, current_ms)
        return JobRunResult(
            claim.job_id,
            "failed",
            "failed",
            "processing_deadline_missing",
        )
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
        delivery_values = None
        if state == "staging_delivery":
            delivery_values = _staging_delivery_values(
                _checkpoint_stage_data(checkpoint),
                confirmed_preheld_total=job["confirmed_preheld_total"],
                environment=store.environment,
            )
            _prepare_publication(claim, runtime, delivery_values, now_ms)
        if not store.transition_leased(
            claim, {state}, next_state, now_ms, lease_seconds=lease_seconds
        ):
            raise LeaseLost("lease_lost", "fenced checkpoint replay was rejected")
        if delivery_values is not None:
            try:
                next_state = _request_settlement(
                    claim, runtime, delivery_values, now_ms, lease_seconds
                )
            except LeaseLost:
                raise
            except Exception as exc:
                return _settlement_safety_pending(
                    claim, runtime, _now_ms(runtime), exc
                )
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
        stage_job = dict(job)
        stage_job["stage_input_sha256"] = input_sha256
        outcome = handler(stage_job, context)
        if not isinstance(outcome, StageOutcome):
            raise ValueError("pipeline_stage_outcome_invalid")
        if outcome.checkpoint_input_sha256 != input_sha256:
            raise ValueError("pipeline_checkpoint_input_mismatch")
        if outcome.next_state not in ALLOWED_TRANSITIONS[state]:
            raise _StageFailure("invalid_stage_transition")
        delivery_values = None
        if state == "staging_delivery":
            delivery_values = _staging_delivery_values(
                outcome.checkpoint,
                confirmed_preheld_total=job["confirmed_preheld_total"],
                environment=store.environment,
            )
        assert_active()
        store.save_checkpoint(
            claim,
            attempt["id"],
            input_sha256,
            _checkpoint_payload(outcome),
            _now_ms(runtime),
        )
        if delivery_values is not None:
            _prepare_publication(
                claim, runtime, delivery_values, _now_ms(runtime)
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
        result_state = outcome.next_state
        if delivery_values is not None:
            try:
                result_state = _request_settlement(
                    claim,
                    runtime,
                    delivery_values,
                    _now_ms(runtime),
                    lease_seconds,
                )
            except LeaseLost:
                raise
            except Exception as exc:
                return _settlement_safety_pending(
                    claim, runtime, _now_ms(runtime), exc
                )
        store.release_lease(claim, _now_ms(runtime))
        return JobRunResult(claim.job_id, result_state, status)
    except LeaseLost:
        heartbeat.close()
        terminate_once()
        current_ms = _now_ms(runtime)
        if store.lease_owned(claim, current_ms):
            store.close_running_attempts(claim, current_ms)
            store.release_lease(claim, current_ms)
        raise
    except BillingError as exc:
        heartbeat.close()
        current_ms = _now_ms(runtime)
        current_state = "settling" if attempt_finished else state
        if store.lease_owned(claim, current_ms):
            store.release_lease(claim, current_ms)
        return JobRunResult(
            claim.job_id,
            current_state,
            "safety_pending",
            exc.error_code,
        )
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
    allow_new_work: bool = True,
) -> dict[str, int]:
    """Process billing then asset authority decisions with fenced claims."""

    if type(allow_new_work) is not bool:
        raise ValueError("reconciliation_new_work_mode_invalid")
    now_ms = _now_ms(runtime)
    counts = {"billing": 0, "assets": 0}
    for intent in list_due_billing_intents(now=now_ms, store=runtime.store, limit=limit):
        if not allow_new_work and intent.status not in {
            "unknown",
            "reconciliation_pending",
        }:
            continue
        claim = runtime.store.claim_job(
            intent.job_id,
            worker_id,
            lease_seconds,
            now_ms,
            expected_states=_billing_expected_states(
                intent.operation, intent.status
            ),
        )
        if claim is None:
            continue
        try:
            if intent.status in {"pending", "retryable_absent"}:
                outcome = process_pending_intent(
                    intent.intent_id,
                    claim=claim,
                    ledger=runtime.points,
                    now=now_ms,
                    store=runtime.store,
                )
            else:
                outcome = reconcile_unknown_intent(
                    intent.intent_id,
                    claim=claim,
                    ledger=runtime.points,
                    now=now_ms,
                    store=runtime.store,
                )
            job = runtime.store.get_job_for_claim(claim, now_ms)
            if job["state"] != outcome.next_state:
                if not runtime.store.transition_leased(
                    claim,
                    {job["state"]},
                    outcome.next_state,
                    now_ms,
                    lease_seconds=lease_seconds,
                ):
                    raise LeaseLost(
                        "lease_lost", "fenced billing transition was rejected"
                    )
            counts["billing"] += 1
        except (BillingError, LeaseLost, StoreConflictError) as exc:
            _log_reconciliation_error(
                intent.operation, intent.intent_id, exc
            )
        finally:
            if runtime.store.lease_owned(claim, now_ms):
                runtime.store.release_lease(claim, now_ms)

    seen_publish_jobs: set[str] = set()
    for row in list_due_publish_intents(now=now_ms, store=runtime.store, limit=limit):
        if not allow_new_work and row["status"] != "unknown":
            continue
        if row["job_id"] in seen_publish_jobs:
            continue
        seen_publish_jobs.add(row["job_id"])
        claim = runtime.store.claim_job(
            row["job_id"],
            worker_id,
            lease_seconds,
            now_ms,
            expected_states={
                "publishing",
                "asset_decision_reconciling",
                "failed_asset_decision_pending",
            },
        )
        if claim is None:
            continue
        try:
            if not allow_new_work:
                _query_historical_publish_authority(
                    claim,
                    runtime,
                    row["publish_generation"],
                    now_ms,
                )
                counts["assets"] += 1
                continue
            job = runtime.store.get_job_for_claim(claim, now_ms)
            if job["state"] == "publishing" and row["status"] == "pending":
                progress = advance_publish(
                    claim,
                    metadata_sha256=row["metadata_sha256"],
                    now=now_ms,
                    store=runtime.store,
                    publisher=runtime.assets,
                )
            else:
                create_publish_intent(
                    claim,
                    metadata_sha256=row["metadata_sha256"],
                    now=now_ms,
                    store=runtime.store,
                )
                progress = reconcile_asset_decision(
                    claim,
                    now=now_ms,
                    store=runtime.store,
                    publisher=runtime.assets,
                )
            current = runtime.store.get_job_for_claim(claim, now_ms)
            if current["state"] != progress.next_state:
                if progress.next_state not in ALLOWED_TRANSITIONS[current["state"]]:
                    raise ValueError("publication_state_transition_invalid")
                if not runtime.store.transition_leased(
                    claim,
                    {current["state"]},
                    progress.next_state,
                    now_ms,
                    lease_seconds=lease_seconds,
                ):
                    raise LeaseLost(
                        "lease_lost", "fenced publication transition was rejected"
                    )
            counts["assets"] += 1
        except (
            LeaseLost,
            StoreConfigurationError,
            StoreConflictError,
            ValueError,
        ) as exc:
            _log_reconciliation_error(
                row["operation"], row.get("id", row["job_id"]), exc
            )
        finally:
            if runtime.store.lease_owned(claim, now_ms):
                runtime.store.release_lease(claim, now_ms)

    ready_jobs = (
        runtime.store.list_publication_ready_jobs(now_ms, limit=limit)
        if allow_new_work
        else ()
    )
    for ready in ready_jobs:
        if ready["job_id"] in seen_publish_jobs:
            continue
        seen_publish_jobs.add(ready["job_id"])
        claim = runtime.store.claim_job(
            ready["job_id"],
            worker_id,
            lease_seconds,
            now_ms,
            expected_states={ready["state"]},
        )
        if claim is None:
            continue
        try:
            job = runtime.store.get_job_for_claim(claim, now_ms)
            checkpoint = runtime.store.get_checkpoint_for_claim(
                claim,
                "staging_delivery",
                _stage_input_sha256(job, "staging_delivery"),
                now_ms,
            )
            if checkpoint is None:
                raise ValueError("staging_delivery_checkpoint_missing")
            values = _staging_delivery_values(
                _checkpoint_stage_data(checkpoint),
                confirmed_preheld_total=job["confirmed_preheld_total"],
                environment=runtime.store.environment,
            )
            _prepare_publication(claim, runtime, values, now_ms)
            current_state = job["state"]
            if current_state == "settling":
                current_state = _request_settlement(
                    claim, runtime, values, now_ms, lease_seconds
                )
            if current_state == "publishing":
                progress = advance_publish(
                    claim,
                    metadata_sha256=values[1],
                    now=now_ms,
                    store=runtime.store,
                    publisher=runtime.assets,
                )
                current = runtime.store.get_job_for_claim(claim, now_ms)
                if current["state"] != progress.next_state:
                    if progress.next_state not in ALLOWED_TRANSITIONS[current["state"]]:
                        raise ValueError("publication_state_transition_invalid")
                    if not runtime.store.transition_leased(
                        claim,
                        {current["state"]},
                        progress.next_state,
                        now_ms,
                        lease_seconds=lease_seconds,
                    ):
                        raise LeaseLost(
                            "lease_lost",
                            "fenced publication transition was rejected",
                        )
                counts["assets"] += 1
        except (BillingError, LeaseLost, StoreConflictError, ValueError) as exc:
            _log_reconciliation_error(
                "publication_ready", ready["job_id"], exc
            )
        finally:
            if runtime.store.lease_owned(claim, now_ms):
                runtime.store.release_lease(claim, now_ms)
    return counts


__all__ = ("JobRunResult", "run_job", "run_reconciliation_pass")
