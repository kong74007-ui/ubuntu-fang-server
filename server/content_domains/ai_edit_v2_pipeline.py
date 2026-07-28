"""Leased, checkpointed orchestration and time budgets for AI Edit V2."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from contextlib import closing
from dataclasses import dataclass, field
from typing import Any, Callable

from . import ai_edit_v2_billing as billing
from . import ai_edit_v2_store as store
from . import points
from .ai_edit_v2_schema import FAILURE_STATES, STATE_TRANSITIONS, TERMINAL_STATES
from .ai_edit_v2_providers.base import RetryableProviderError


Handler = Callable[[dict[str, Any], dict[str, Any]], "StageResult"]


@dataclass(frozen=True)
class StageResult:
    next_state: str
    checkpoint: dict[str, Any] = field(default_factory=dict)
    provider_task_id: str | None = None
    error_code: str | None = None


class PipelineError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class RetryableStageError(PipelineError):
    """A provider stage may be retried twice with its durable identity intact."""


_FAILURE_BY_STAGE = {
    "queued": "validation_failed",
    "normalizing": "validation_failed",
    "transcribing": "transcription_failed",
    "aligning_transcript": "transcription_failed",
    "directing": "director_failed",
    "resolving_assets": "asset_failed",
    "generating_assets": "asset_failed",
    "designing_audio": "asset_failed",
    "routing_render": "render_failed",
    "rendering": "render_failed",
    "assembling": "render_failed",
    "quality_check": "quality_failed",
    "repairing": "quality_failed",
    "settling": "settlement_failed",
    "storing": "storage_failed",
}


def _normal_budget() -> int:
    value = os.environ.get("AI_EDIT_V2_NORMAL_TIMEOUT_SECONDS") or os.environ.get(
        "AI_EDIT_V2_NORMAL_BUDGET_SECONDS", "2700"
    )
    return max(60, int(value))


def _repair_budget() -> int:
    value = os.environ.get("AI_EDIT_V2_REPAIR_TIMEOUT_SECONDS") or os.environ.get(
        "AI_EDIT_V2_REPAIR_BUDGET_SECONDS", "900"
    )
    return max(0, int(value))


def _load_job(job_id: str, db_path: str | None = None) -> dict[str, Any]:
    with closing(store.open_store(store._db_path(db_path))) as conn:
        row = conn.execute("SELECT * FROM edit_v2_jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise PipelineError("job_not_found")
    return dict(row)


def _checkpoints(job: dict[str, Any]) -> list[dict[str, Any]]:
    value = json.loads(job.get("checkpoint_json") or "[]")
    return value if isinstance(value, list) else []


def _state_started(checkpoints: list[dict[str, Any]], state: str) -> int | None:
    for item in checkpoints:
        if item.get("state") == state:
            return int(item["at"])
    return None


def timing_status(job_id: str, now: int | None = None, *, db_path: str | None = None) -> dict[str, Any]:
    now = int(time.time()) if now is None else int(now)
    job = _load_job(job_id, db_path)
    checkpoints = _checkpoints(job)
    processing_start = _state_started(checkpoints, "normalizing")
    repair_start = _state_started(checkpoints, "repairing")
    terminal = job["status"] in TERMINAL_STATES
    end = min(now, int(job["updated_at"])) if terminal else now
    if processing_start is None:
        queue_seconds = max(0, end - int(job["created_at"]))
        processing_seconds = 0
        repair_seconds = 0
        remaining = _normal_budget()
    else:
        queue_seconds = max(0, processing_start - int(job["created_at"]))
        normal_end = repair_start if repair_start is not None else end
        processing_seconds = max(0, normal_end - processing_start)
        repair_seconds = max(0, end - repair_start) if repair_start is not None else 0
        if repair_start is not None:
            remaining = max(0, processing_start + _normal_budget() + _repair_budget() - end)
        else:
            remaining = max(0, processing_start + _normal_budget() - end)
    return {
        "queue_seconds": queue_seconds,
        "processing_seconds": processing_seconds,
        "repair_seconds": repair_seconds,
        "remaining_seconds": remaining,
        "current_stage": job["status"],
    }


def _latest_attempt(job_id: str, stage: str, db_path: str | None = None):
    with closing(store.open_store(store._db_path(db_path))) as conn:
        return conn.execute(
            """SELECT * FROM edit_v2_stage_attempts
               WHERE job_id=? AND stage=? ORDER BY attempt DESC LIMIT 1""",
            (job_id, stage),
        ).fetchone()


def _finish_attempt(
    attempt_id: int, result: StageResult, now: int, db_path: str | None = None,
    *, lease_owner: str | None = None,
) -> None:
    with closing(store.open_store(store._db_path(db_path))) as conn:
        conn.execute("BEGIN IMMEDIATE")
        changed = conn.execute(
            """UPDATE edit_v2_stage_attempts
               SET status=?,provider_task_id=COALESCE(?,provider_task_id),
                   output_summary_json=?,error_code=?,finished_at=?
               WHERE id=? AND (? IS NULL OR EXISTS(
                   SELECT 1 FROM edit_v2_jobs j
                   WHERE j.id=edit_v2_stage_attempts.job_id
                     AND j.lease_owner=? AND j.lease_until>?
               ))""",
            (
                "failed" if result.next_state in FAILURE_STATES else "completed",
                result.provider_task_id,
                json.dumps(result.checkpoint, ensure_ascii=False),
                result.error_code,
                now,
                attempt_id,
                lease_owner,
                lease_owner,
                now,
            ),
        ).rowcount
        if changed != 1:
            conn.rollback()
            raise PipelineError("job_lease_lost" if lease_owner else "stage_attempt_missing")
        conn.commit()


def _save_provider_task_id(
    attempt_id: int, provider_task_id: str, db_path: str | None = None,
    *, lease_owner: str | None = None, now: int | None = None,
) -> None:
    provider_task_id = str(provider_task_id or "").strip()
    if not provider_task_id:
        raise PipelineError("provider_task_id_invalid")
    with closing(store.open_store(store._db_path(db_path))) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                """SELECT a.provider_task_id,a.input_summary_json,a.job_id,
                          j.lease_owner,j.lease_until
                   FROM edit_v2_stage_attempts a
                   JOIN edit_v2_jobs j ON j.id=a.job_id WHERE a.id=?""",
                (attempt_id,),
            ).fetchone()
            if row is None or row["provider_task_id"] not in {None, provider_task_id}:
                raise PipelineError("provider_task_id_conflict")
            if lease_owner and (
                row["lease_owner"] != lease_owner
                or row["lease_until"] is None
                or int(row["lease_until"]) <= int(now or time.time())
            ):
                raise PipelineError("job_lease_lost")
            conn.execute(
                "UPDATE edit_v2_stage_attempts SET provider_task_id=? WHERE id=?",
                (provider_task_id, attempt_id),
            )
            summary = json.loads(row["input_summary_json"] or "{}")
            if not isinstance(summary, dict):
                raise PipelineError("submission_intent_invalid")
            intent = summary.get("submission_intent")
            if intent is not None:
                if not isinstance(intent, dict):
                    raise PipelineError("submission_intent_invalid")
                summary["submission_intent"] = {**intent, "status": "provider_bound"}
                conn.execute(
                    "UPDATE edit_v2_stage_attempts SET input_summary_json=? WHERE id=?",
                    (json.dumps(summary, ensure_ascii=False), attempt_id),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def _submission_intent(attempt: Any) -> dict[str, Any] | None:
    try:
        summary = json.loads(attempt["input_summary_json"] or "{}")
        intent = summary.get("submission_intent")
    except (TypeError, ValueError, KeyError):
        raise PipelineError("submission_intent_invalid")
    if intent is None:
        return None
    if not isinstance(intent, dict):
        raise PipelineError("submission_intent_invalid")
    return intent


def _claim_submission_intent(
    attempt_id: int,
    provider: str,
    capability: str,
    reference: str,
    db_path: str | None = None,
) -> bool:
    if (
        not isinstance(provider, str) or not provider
        or not isinstance(capability, str) or not capability
        or not isinstance(reference, str) or not reference
    ):
        raise PipelineError("submission_intent_invalid")
    with closing(store.open_store(store._db_path(db_path))) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT provider_task_id,input_summary_json FROM edit_v2_stage_attempts WHERE id=?",
                (attempt_id,),
            ).fetchone()
            if row is None:
                raise PipelineError("submission_intent_invalid")
            try:
                summary = json.loads(row["input_summary_json"] or "{}")
            except (TypeError, ValueError):
                raise PipelineError("submission_intent_invalid")
            if not isinstance(summary, dict):
                raise PipelineError("submission_intent_invalid")
            existing = summary.get("submission_intent")
            identity = {"provider": provider, "capability": capability, "reference": reference}
            if row["provider_task_id"] is not None:
                conn.commit()
                return False
            if existing is not None:
                if (
                    not isinstance(existing, dict)
                    or {key: existing.get(key) for key in identity} != identity
                ):
                    raise PipelineError("submission_intent_conflict")
                conn.commit()
                return False
            summary["submission_intent"] = {**identity, "status": "pending"}
            conn.execute(
                "UPDATE edit_v2_stage_attempts SET input_summary_json=? WHERE id=?",
                (json.dumps(summary, ensure_ascii=False), attempt_id),
            )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise


def _mark_submission_unknown(
    attempt_id: int, reference: str, db_path: str | None = None
) -> None:
    with closing(store.open_store(store._db_path(db_path))) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT provider_task_id,input_summary_json FROM edit_v2_stage_attempts WHERE id=?",
                (attempt_id,),
            ).fetchone()
            if row is None:
                raise PipelineError("submission_intent_invalid")
            summary = json.loads(row["input_summary_json"] or "{}")
            intent = summary.get("submission_intent") if isinstance(summary, dict) else None
            identity = {"provider": "dashscope", "capability": "asr", "reference": reference}
            if (
                not isinstance(intent, dict)
                or {key: intent.get(key) for key in identity} != identity
            ):
                raise PipelineError("submission_intent_conflict")
            if row["provider_task_id"] is None and intent.get("status") == "pending":
                summary["submission_intent"] = {**intent, "status": "unknown"}
                conn.execute(
                    "UPDATE edit_v2_stage_attempts SET input_summary_json=? WHERE id=?",
                    (json.dumps(summary, ensure_ascii=False), attempt_id),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def _repairable(job_id: str, checkpoints: list[dict[str, Any]], db_path: str | None = None) -> bool:
    qc = None
    for item in reversed(checkpoints):
        data = item.get("data") if isinstance(item, dict) else None
        if isinstance(data, dict) and isinstance(data.get("qc"), dict):
            qc = data["qc"]
            break
    if not qc or qc.get("passed") is not False or qc.get("repairable") is not True or not qc.get("issues"):
        return False
    with closing(store.open_store(store._db_path(db_path))) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM edit_v2_render_artifacts WHERE job_id=?", (job_id,)
        ).fetchone()[0]
    return int(count) > 0


def _transition_result(
    job_id: str,
    expected: str,
    result: StageResult,
    now: int,
    *,
    db_path: str | None,
    points_client: Any,
) -> StageResult:
    if result.next_state == "repairing" and not _repairable(job_id, _checkpoints(_load_job(job_id, db_path)), db_path):
        result = StageResult("quality_failed", {"reason": "repair_not_eligible"}, error_code="repair_not_eligible")
    if not store.transition(
        job_id, expected, result.next_state,
        {**result.checkpoint, **({"error_code": result.error_code} if result.error_code else {})},
        now, db_path=db_path,
    ):
        raise PipelineError("stage_compare_and_swap_failed")
    if result.next_state in FAILURE_STATES:
        billing.refund_failure(job_id, now, points_client=points_client, db_path=db_path)
    return result


def _budget_result(
    job: dict[str, Any], expected: str, now: int, db_path: str | None
) -> StageResult | None:
    checkpoints = _checkpoints(job)
    processing_start = _state_started(checkpoints, "normalizing")
    if processing_start is None:
        return None
    if expected == "repairing" and now > processing_start + _normal_budget() + _repair_budget():
        return StageResult(
            "quality_failed", {"budget": "repair"}, error_code="repair_budget_exceeded"
        )
    if expected != "repairing" and now > processing_start + _normal_budget():
        if expected == "quality_check" and _repairable(job["id"], checkpoints, db_path):
            return StageResult("repairing", {"reason": "qc_repair_extension"})
        return StageResult(
            _FAILURE_BY_STAGE.get(expected, "quality_failed"),
            {"budget": "normal"},
            error_code="normal_budget_exceeded",
        )
    return None


def run_stage(
    job_id: str,
    expected_state: str,
    *,
    handlers: dict[str, Handler] | None = None,
    now: int | None = None,
    db_path: str | None = None,
    points_client: Any = points,
) -> StageResult:
    now = int(time.time()) if now is None else int(now)
    job = _load_job(job_id, db_path)
    if job["status"] != expected_state:
        raise PipelineError("stage_compare_and_swap_failed")
    if expected_state in TERMINAL_STATES:
        raise PipelineError("terminal_job")

    budget = _budget_result(job, expected_state, now, db_path)
    if budget is not None:
        return _transition_result(
            job_id, expected_state, budget, now,
            db_path=db_path, points_client=points_client,
        )
    if expected_state == "queued":
        result = StageResult("normalizing", {"processing_started_at": now})
        return _transition_result(
            job_id, expected_state, result, now,
            db_path=db_path, points_client=points_client,
        )

    handler = (handlers or {}).get(expected_state)
    if handler is None:
        result = StageResult(
            _FAILURE_BY_STAGE.get(expected_state, "quality_failed"),
            {"stage": expected_state},
            error_code="stage_handler_missing",
        )
        return _transition_result(
            job_id, expected_state, result, now,
            db_path=db_path, points_client=points_client,
        )

    attempt = _latest_attempt(job_id, expected_state, db_path)
    if attempt is None:
        attempt_no = 1
        attempt_id = store.record_stage_attempt(
            job_id, expected_state, attempt_no, "running", now, db_path=db_path
        )
        provider_task_id = None
        submission_intent = None
    else:
        attempt_id = int(attempt["id"])
        provider_task_id = attempt["provider_task_id"]
        submission_intent = _submission_intent(attempt)
    context = {
        "provider_task_id": provider_task_id,
        "save_provider_task_id": lambda value: _save_provider_task_id(
            attempt_id, value, db_path
        ),
        "submission_intent": submission_intent,
        "save_submission_intent": lambda provider, capability, reference: _claim_submission_intent(
            attempt_id, provider, capability, reference, db_path
        ),
        "mark_submission_unknown": lambda reference: _mark_submission_unknown(
            attempt_id, reference, db_path
        ),
        "checkpoint": _checkpoints(job),
        "deadline_at": (
            (_state_started(_checkpoints(job), "normalizing") or now) + _normal_budget()
        ),
    }
    try:
        result = handler(job, context)
        if not isinstance(result, StageResult):
            raise PipelineError("stage_result_invalid")
        if result.next_state not in FAILURE_STATES and result.next_state not in STATE_TRANSITIONS.get(expected_state, ()):
            raise PipelineError("stage_result_invalid")
    except Exception as exc:
        code = exc.code if isinstance(exc, PipelineError) else "stage_execution_failed"
        result = StageResult(
            _FAILURE_BY_STAGE.get(expected_state, "quality_failed"),
            {"stage": expected_state},
            error_code=code,
        )
    _finish_attempt(attempt_id, result, now, db_path)
    return _transition_result(
        job_id, expected_state, result, now,
        db_path=db_path, points_client=points_client,
    )


def _stable_input(job: dict[str, Any], stage: str, previous: dict[str, Any]) -> dict[str, Any]:
    try:
        payload = json.loads(job.get("payload_json") or "{}")
    except (TypeError, ValueError):
        raise PipelineError("job_payload_invalid")
    if not isinstance(payload, dict):
        raise PipelineError("job_payload_invalid")
    return {"stage": stage, "payload": payload, "previous": previous}


def _fingerprint(value: dict[str, Any]) -> str:
    serialized = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _completed_pipeline_outputs(job_id: str, db_path: str | None) -> dict[str, dict[str, Any]]:
    with closing(store.open_store(store._db_path(db_path))) as conn:
        rows = conn.execute(
            """SELECT stage,output_json FROM edit_v2_pipeline_checkpoints
               WHERE job_id=? AND status='completed'""",
            (job_id,),
        ).fetchall()
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            output = json.loads(row["output_json"] or "{}")
        except (TypeError, ValueError):
            continue
        if isinstance(output, dict):
            result[row["stage"]] = output
    return result


def _previous_output(job_id: str, stage: str, db_path: str | None) -> dict[str, Any]:
    from . import ai_edit_v2_runtime as runtime

    completed = _completed_pipeline_outputs(job_id, db_path)
    index = runtime.STABLE_STAGE_SEQUENCE.index(stage)
    for previous_stage in reversed(runtime.STABLE_STAGE_SEQUENCE[:index]):
        if previous_stage in completed:
            return completed[previous_stage]
    return {}


def _artifact_reusable(
    dependencies: Any, stage: str, output: dict[str, Any]
) -> bool:
    from . import ai_edit_v2_runtime as runtime

    verifier = runtime.option(dependencies, "verify_artifact")
    try:
        valid, _code = runtime.validate_stage_output(
            stage, output, verifier if callable(verifier) else None
        )
        return valid
    except Exception:
        return False


def _ensure_stable_attempt(
    job_id: str,
    state: str,
    input_fingerprint: str,
    now: int,
    db_path: str | None,
    lease_owner: str | None = None,
) -> int:
    attempt = _latest_attempt(job_id, state, db_path)
    if attempt is not None:
        try:
            summary = json.loads(attempt["input_summary_json"] or "{}")
        except (TypeError, ValueError):
            summary = {}
        if summary.get("pipeline_input_fingerprint") == input_fingerprint:
            return int(attempt["id"])
        attempt_no = int(attempt["attempt"]) + 1
    else:
        attempt_no = 1
    return store.record_stage_attempt(
        job_id,
        state,
        attempt_no,
        "running",
        int(now),
        input_summary={"pipeline_input_fingerprint": input_fingerprint},
        lease_owner=lease_owner,
        db_path=db_path,
    )


class _LeaseHeartbeat:
    def __init__(
        self,
        job_id: str,
        worker_id: str,
        lease_seconds: int,
        db_path: str | None,
        now_fn: Callable[[], int],
    ) -> None:
        self.job_id = job_id
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.db_path = db_path
        self.now_fn = now_fn
        self.finished = threading.Event()
        self.lost = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        interval = max(0.1, self.lease_seconds / 3)
        while not self.finished.wait(interval):
            if not store.renew_lease(
                self.job_id,
                self.worker_id,
                self.lease_seconds,
                int(self.now_fn()),
                db_path=self.db_path,
            ):
                self.lost.set()
                return

    def assert_active(self) -> None:
        if self.lost.is_set():
            raise PipelineError("job_lease_lost")
        if not store.lease_owned(
            self.job_id, self.worker_id, int(self.now_fn()), db_path=self.db_path
        ):
            self.lost.set()
            raise PipelineError("job_lease_lost")

    def __enter__(self) -> "_LeaseHeartbeat":
        self.thread.start()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.finished.set()
        self.thread.join(timeout=2)


def _stable_failure(
    job_id: str,
    state: str,
    code: str,
    now: int,
    *,
    worker_id: str,
    lease_seconds: int,
    db_path: str | None,
    points_client: Any,
) -> dict[str, Any]:
    target = _FAILURE_BY_STAGE.get(state, "quality_failed")
    if not store.transition_leased(
        job_id,
        state,
        target,
        {"stage": state, "error_code": code},
        now,
        worker_id=worker_id,
        lease_seconds=lease_seconds,
        db_path=db_path,
    ):
        raise PipelineError("stage_compare_and_swap_failed")
    billing.refund_failure(job_id, now, points_client=points_client, db_path=db_path)
    return {"job_id": job_id, "state": target, "error_code": code}


def run_job(
    job_id: str, dependencies: Any, db_path: str | None = None
) -> dict[str, Any]:
    """Run the stable pipeline through the Task 8 quality-check boundary.

    Durable fingerprints guard checkpoint reuse.  A provider identity saved before a
    crash is sent only to a reconciler; the submit handler is never called first.
    """

    from . import ai_edit_v2_runtime as runtime

    now_fn = runtime.option(dependencies, "now", lambda: int(time.time()))
    if not callable(now_fn):
        raise PipelineError("pipeline_clock_invalid")
    lease_seconds = max(1, int(runtime.option(dependencies, "lease_seconds", 180)))
    preclaimed_owner = runtime.option(dependencies, "lease_owner")
    worker_base = str(runtime.option(dependencies, "worker_id", "run-job"))
    worker_id = str(preclaimed_owner) if preclaimed_owner else f"{worker_base}:{uuid.uuid4().hex}"
    now = int(now_fn())
    job = _load_job(job_id, db_path)
    if job["status"] in TERMINAL_STATES or job["status"] == "quality_check":
        return {"job_id": job_id, "state": runtime.public_state(job["status"])}
    claimed = (
        job
        if preclaimed_owner
        and store.lease_owned(job_id, worker_id, now, db_path=db_path)
        else store.claim_job(job_id, worker_id, lease_seconds, now, db_path=db_path)
    )
    if claimed is None:
        current = _load_job(job_id, db_path)
        return {
            "job_id": job_id,
            "state": runtime.public_state(current["status"]),
            "claimed": False,
        }
    points_client = runtime.option(dependencies, "points_client", points)

    with _LeaseHeartbeat(job_id, worker_id, lease_seconds, db_path, now_fn) as heartbeat:
        while True:
            heartbeat.assert_active()
            now = int(now_fn())
            job = _load_job(job_id, db_path)
            state = str(job["status"])
            if state == "quality_check" or state in TERMINAL_STATES:
                store.release_job_lease(job_id, worker_id, db_path=db_path)
                return {"job_id": job_id, "state": runtime.public_state(state)}
            if state == "queued":
                if not store.transition_leased(
                    job_id,
                    "queued",
                    "normalizing",
                    {"processing_started_at": now},
                    now,
                    worker_id=worker_id,
                    lease_seconds=lease_seconds,
                    db_path=db_path,
                ):
                    raise PipelineError("stage_compare_and_swap_failed")
                continue
            if state in {"designing_audio", "routing_render"}:
                target = "routing_render" if state == "designing_audio" else "rendering"
                if not store.transition_leased(
                    job_id,
                    state,
                    target,
                    {"aggregated_stage": "generating_media"},
                    now,
                    worker_id=worker_id,
                    lease_seconds=lease_seconds,
                    db_path=db_path,
                ):
                    raise PipelineError("stage_compare_and_swap_failed")
                continue
            stage = runtime.STATE_TO_STAGE.get(state)
            if stage is None:
                store.release_job_lease(job_id, worker_id, db_path=db_path)
                return {"job_id": job_id, "state": runtime.public_state(state)}

            checkpoints = _checkpoints(job)
            started = _state_started(checkpoints, "normalizing") or now
            deadline_at = started + _normal_budget()
            if now > deadline_at:
                return _stable_failure(
                    job_id,
                    state,
                    "normal_budget_exceeded",
                    now,
                    worker_id=worker_id,
                    lease_seconds=lease_seconds,
                    db_path=db_path,
                    points_client=points_client,
                )

            stage_input = _stable_input(
                job, stage, _previous_output(job_id, stage, db_path)
            )
            input_fingerprint = _fingerprint(stage_input)
            checkpoint = store.prepare_pipeline_checkpoint(
                job_id, stage, input_fingerprint, now,
                lease_owner=worker_id, db_path=db_path
            )
            output: dict[str, Any] | None = None
            if checkpoint["status"] == "completed":
                try:
                    saved = json.loads(checkpoint["output_json"] or "{}")
                except (TypeError, ValueError):
                    saved = None
                if isinstance(saved, dict) and _artifact_reusable(
                    dependencies, stage, saved
                ):
                    output = saved
                else:
                    store.invalidate_pipeline_checkpoint(
                        int(checkpoint["id"]), now,
                        lease_owner=worker_id, db_path=db_path
                    )
                    checkpoint["status"] = "running"

            attempt_id = _ensure_stable_attempt(
                job_id, state, input_fingerprint, now, db_path, worker_id
            )
            if output is None:
                handler = runtime.dependency_callable(dependencies, "handlers", stage)
                if handler is None:
                    return _stable_failure(
                        job_id,
                        state,
                        "stage_handler_missing",
                        now,
                        worker_id=worker_id,
                        lease_seconds=lease_seconds,
                        db_path=db_path,
                        points_client=points_client,
                    )
                provider_stage = stage in runtime.PROVIDER_STAGES
                max_calls = 3 if provider_stage else 1
                last_code = "stage_execution_failed"
                remaining_calls = (
                    max(0, max_calls - int(checkpoint["attempt_count"]))
                    if provider_stage
                    else 1
                )
                if remaining_calls == 0:
                    last_code = "provider_retry_exhausted"
                for _ in range(remaining_calls):
                    heartbeat.assert_active()
                    checkpoint = store.increment_pipeline_attempt(
                        int(checkpoint["id"]), int(now_fn()),
                        lease_owner=worker_id, db_path=db_path
                    )
                    context = {
                        "attempt_id": attempt_id,
                        "input_fingerprint": input_fingerprint,
                        "provider_task_id": checkpoint.get("provider_task_id"),
                        "provider_reference": checkpoint.get("provider_reference"),
                        "deadline_at": deadline_at,
                        "assert_active": heartbeat.assert_active,
                        "renew_lease": lambda: store.renew_lease(
                            job_id,
                            worker_id,
                            lease_seconds,
                            int(now_fn()),
                            db_path=db_path,
                        ),
                        "save_provider_task_id": lambda value: (
                            heartbeat.assert_active(),
                            _save_provider_task_id(
                                attempt_id, value, db_path,
                                lease_owner=worker_id, now=int(now_fn())
                            ),
                            store.save_pipeline_provider_identity(
                                int(checkpoint["id"]),
                                provider_task_id=value,
                                lease_owner=worker_id,
                                now=int(now_fn()),
                                db_path=db_path,
                            ),
                        )[-1],
                        "save_provider_reference": lambda value: (
                            heartbeat.assert_active(),
                            store.save_pipeline_provider_identity(
                                int(checkpoint["id"]),
                                provider_reference=value,
                                lease_owner=worker_id,
                                now=int(now_fn()),
                                db_path=db_path,
                            ),
                        )[-1],
                    }
                    has_identity = bool(
                        context["provider_task_id"] or context["provider_reference"]
                    )
                    callback = handler
                    if has_identity:
                        callback = runtime.dependency_callable(
                            dependencies, "reconcilers", stage
                        )
                        if callback is None:
                            last_code = "provider_reconciliation_required"
                            break
                    try:
                        heartbeat.assert_active()
                        value = callback(job, context, stage_input)
                        heartbeat.assert_active()
                        if int(now_fn()) > deadline_at:
                            last_code = "normal_budget_exceeded"
                            break
                        if isinstance(value, StageResult):
                            value = value.checkpoint
                        if not isinstance(value, dict):
                            raise PipelineError("stage_result_invalid")
                        verifier = runtime.option(dependencies, "verify_artifact")
                        valid, validation_code = runtime.validate_stage_output(
                            stage,
                            value,
                            verifier if callable(verifier) else None,
                        )
                        if not valid:
                            last_code = validation_code or "stage_output_invalid"
                            break
                        output = value
                        break
                    except (RetryableStageError, RetryableProviderError) as exc:
                        last_code = getattr(exc, "code", None) or str(exc) or "provider_unavailable"
                        continue
                    except PipelineError as exc:
                        if exc.code == "job_lease_lost":
                            raise
                        last_code = exc.code
                        break
                    except Exception as exc:
                        last_code = getattr(exc, "code", None) or "stage_execution_failed"
                        break
                if output is None:
                    return _stable_failure(
                        job_id,
                        state,
                        last_code,
                        int(now_fn()),
                        worker_id=worker_id,
                        lease_seconds=lease_seconds,
                        db_path=db_path,
                        points_client=points_client,
                    )
                store.complete_pipeline_checkpoint(
                    int(checkpoint["id"]), output, int(now_fn()),
                    lease_owner=worker_id, db_path=db_path
                )
                heartbeat.assert_active()
                _finish_attempt(
                    attempt_id,
                    StageResult(runtime.STAGE_TO_NEXT_STATE[stage], output),
                    int(now_fn()),
                    db_path,
                    lease_owner=worker_id,
                )

            target = runtime.STAGE_TO_NEXT_STATE[stage]
            heartbeat.assert_active()
            if int(now_fn()) > deadline_at:
                return _stable_failure(
                    job_id,
                    state,
                    "normal_budget_exceeded",
                    int(now_fn()),
                    worker_id=worker_id,
                    lease_seconds=lease_seconds,
                    db_path=db_path,
                    points_client=points_client,
                )
            if not store.transition_leased(
                job_id,
                state,
                target,
                {"stage": stage, "input_fingerprint": input_fingerprint, "output": output},
                int(now_fn()),
                worker_id=worker_id,
                lease_seconds=lease_seconds,
                db_path=db_path,
            ):
                raise PipelineError("stage_compare_and_swap_failed")


def reconcile_terminal_refunds(
    now: int | None = None,
    *,
    db_path: str | None = None,
    points_client: Any = points,
) -> int:
    now = int(time.time()) if now is None else int(now)
    placeholders = ",".join("?" for _ in FAILURE_STATES)
    with closing(store.open_store(store._db_path(db_path))) as conn:
        rows = conn.execute(
            f"""SELECT b.job_id FROM edit_v2_billing b
                 JOIN edit_v2_jobs j ON j.id=b.job_id
                 WHERE b.operation='hold' AND b.status='held'
                   AND j.status IN ({placeholders})""",
            tuple(FAILURE_STATES),
        ).fetchall()
    recovered = 0
    for row in rows:
        billing.refund_failure(
            row["job_id"], now, points_client=points_client, db_path=db_path
        )
        recovered += 1
    return recovered
