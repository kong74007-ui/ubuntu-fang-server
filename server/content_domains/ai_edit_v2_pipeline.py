"""Leased, checkpointed orchestration and time budgets for AI Edit V2."""

from __future__ import annotations

import json
import os
import time
from contextlib import closing
from dataclasses import dataclass, field
from typing import Any, Callable

from . import ai_edit_v2_billing as billing
from . import ai_edit_v2_store as store
from . import points
from .ai_edit_v2_schema import FAILURE_STATES, STATE_TRANSITIONS, TERMINAL_STATES


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
    attempt_id: int, result: StageResult, now: int, db_path: str | None = None
) -> None:
    with closing(store.open_store(store._db_path(db_path))) as conn:
        conn.execute(
            """UPDATE edit_v2_stage_attempts
               SET status=?,provider_task_id=COALESCE(?,provider_task_id),
                   output_summary_json=?,error_code=?,finished_at=?
               WHERE id=?""",
            (
                "failed" if result.next_state in FAILURE_STATES else "completed",
                result.provider_task_id,
                json.dumps(result.checkpoint, ensure_ascii=False),
                result.error_code,
                now,
                attempt_id,
            ),
        )


def _save_provider_task_id(
    attempt_id: int, provider_task_id: str, db_path: str | None = None
) -> None:
    provider_task_id = str(provider_task_id or "").strip()
    if not provider_task_id:
        raise PipelineError("provider_task_id_invalid")
    with closing(store.open_store(store._db_path(db_path))) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT provider_task_id,input_summary_json FROM edit_v2_stage_attempts WHERE id=?",
                (attempt_id,),
            ).fetchone()
            if row is None or row["provider_task_id"] not in {None, provider_task_id}:
                raise PipelineError("provider_task_id_conflict")
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
