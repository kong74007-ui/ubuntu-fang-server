"""Crash-safe AI Edit V3 publication outbox and decision recovery."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol, runtime_checkable

from .contracts import ALL_STATES, LeaseClaim
from .providers import DefinitiveNotAccepted, SubmissionUnknown
from .store import LeaseLost, V3Store, is_valid_publish_asset_id


_MODE = "ai_edit_v3"
_DECISION_STATUSES = frozenset(
    {"accepted", "stale_generation", "publish_won", "cancel_won"}
)
_FINAL_DECISIONS = frozenset({"publish_won", "cancel_won"})
_ASSET_DECISION_TIMEOUT_MS = 300_000


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("publication_checkpoint_invalid")
        return MappingProxyType(
            {key: _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValueError("publication_checkpoint_invalid")


@dataclass(frozen=True, slots=True)
class PublicationProgress:
    next_state: str
    checkpoint: Mapping[str, Any]

    def __post_init__(self) -> None:
        if self.next_state not in ALL_STATES:
            raise ValueError("publication_next_state_invalid")
        if not isinstance(self.checkpoint, Mapping):
            raise ValueError("publication_checkpoint_invalid")
        object.__setattr__(self, "checkpoint", _freeze_json(self.checkpoint))


@runtime_checkable
class SharedAssetPublisher(Protocol):
    def register_generation(
        self,
        mode: str,
        source_job_id: str,
        generation: int,
        idempotency_key: str,
    ) -> object: ...

    def prepare_hidden(
        self,
        mode: str,
        source_job_id: str,
        owner: str,
        object_key: str,
        generation: int,
        idempotency_key: str,
    ) -> object: ...

    def commit_publish(
        self,
        mode: str,
        source_job_id: str,
        generation: int,
        idempotency_key: str,
    ) -> object: ...

    def cancel_publish(
        self,
        mode: str,
        source_job_id: str,
        generation: int,
        idempotency_key: str,
    ) -> object: ...

    def query_decision(
        self,
        mode: str,
        source_job_id: str,
        idempotency_key: str,
    ) -> object | None: ...


@dataclass(frozen=True, slots=True)
class _StepResult:
    outcome: str
    progress: PublicationProgress


def _require_now(now: Any) -> int:
    if isinstance(now, bool) or not isinstance(now, int) or now < 0:
        raise ValueError("publication_now_invalid")
    return now


def _require_metadata_sha256(value: Any) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError("publication_metadata_sha256_invalid")
    return value


def _row_map(context: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {row["operation"]: row for row in context["intents"]}


def _stored_evidence(row: Mapping[str, Any]) -> Mapping[str, Any] | None:
    raw = row.get("last_decision_json")
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, Mapping) else None


def _decision_values(value: object) -> dict[str, Any]:
    status = getattr(value, "status", None)
    generation = getattr(value, "current_generation", None)
    asset_id = getattr(value, "asset_id", None)
    if status not in _DECISION_STATUSES:
        raise ValueError("publication_decision_invalid")
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or generation < 0
        or generation > (1 << 63) - 1
    ):
        raise ValueError("publication_generation_invalid")
    if status == "publish_won":
        if not is_valid_publish_asset_id(asset_id):
            raise ValueError("publication_asset_id_invalid")
    elif asset_id is not None:
        raise ValueError("publication_asset_id_invalid")
    return {
        "asset_id": asset_id,
        "current_generation": generation,
        "status": status,
    }


def _unknown_progress(
    row: Mapping[str, Any], now: int, *, reason: str
) -> PublicationProgress:
    first_unknown_at = row.get("first_unknown_at")
    if isinstance(first_unknown_at, bool) or not isinstance(first_unknown_at, int):
        first_unknown_at = row.get("updated_at")
    if isinstance(first_unknown_at, bool) or not isinstance(first_unknown_at, int):
        first_unknown_at = now
    next_state = (
        "failed_asset_decision_pending"
        if now - first_unknown_at >= _ASSET_DECISION_TIMEOUT_MS
        else "asset_decision_reconciling"
    )
    return PublicationProgress(
        next_state,
        {
            "external_idempotency_key": row["external_idempotency_key"],
            "first_unknown_at": first_unknown_at,
            "operation": row["operation"],
            "outcome": reason,
            "publish_generation": row["publish_generation"],
        },
    )


def _timed_out_generation_progress(
    context: Mapping[str, Any], now: int
) -> PublicationProgress | None:
    anchors = tuple(
        row
        for row in context["intents"]
        if not isinstance(row.get("first_unknown_at"), bool)
        and isinstance(row.get("first_unknown_at"), int)
    )
    if not anchors:
        return None
    anchor = min(
        anchors,
        key=lambda row: (row["first_unknown_at"], row["id"]),
    )
    progress = _unknown_progress(anchor, now, reason="submission_timeout")
    if progress.next_state != "failed_asset_decision_pending":
        return None
    return progress


def _current_progress(context: Mapping[str, Any], **checkpoint: Any) -> PublicationProgress:
    return PublicationProgress(context["job"]["state"], checkpoint)


def _stored_final_progress(
    row: Mapping[str, Any], context: Mapping[str, Any]
) -> _StepResult | None:
    if row["status"] == "publish_won":
        asset_id = row.get("asset_id") or context["job"].get("asset_id")
        if not is_valid_publish_asset_id(asset_id):
            raise ValueError("publication_asset_id_invalid")
        return _StepResult(
            "publish_won",
            PublicationProgress(
                "completed",
                {
                    "asset_id": asset_id,
                    "operation": row["operation"],
                    "outcome": "publish_won",
                },
            ),
        )
    if row["status"] == "cancel_won":
        return _StepResult(
            "cancel_won",
            PublicationProgress(
                "failed",
                {"operation": row["operation"], "outcome": "cancel_won"},
            ),
        )
    return None


def _record_unknown(
    *,
    claim: LeaseClaim,
    row: Mapping[str, Any],
    now: int,
    store: V3Store,
    reason_code: str,
) -> _StepResult:
    try:
        persisted = store.record_publish_operation(
            claim,
            row["operation"],
            "unknown",
            {"outcome": "unknown", "reason_code": reason_code},
            now_ms=now,
        )
    except LeaseLost:
        return _StepResult(
            "stale",
            PublicationProgress(
                "asset_decision_reconciling",
                {"operation": row["operation"], "outcome": "stale_claim"},
            ),
        )
    return _StepResult(
        "unknown", _unknown_progress(persisted, now, reason=reason_code)
    )


def _invoke_operation(
    *,
    claim: LeaseClaim,
    row: Mapping[str, Any],
    context: Mapping[str, Any],
    now: int,
    store: V3Store,
    publisher: SharedAssetPublisher,
) -> _StepResult:
    operation = row["operation"]
    stored_final = _stored_final_progress(row, context)
    if stored_final is not None:
        if stored_final.outcome == "cancel_won":
            decision = _stored_evidence(row)
            if decision is None or decision.get("status") != "cancel_won":
                raise ValueError("publication_decision_invalid")
            store.record_cancel_winner_and_refund(
                claim,
                operation,
                decision,
                now_ms=now,
            )
        return stored_final
    if operation != "query_decision":
        timed_progress = _timed_out_generation_progress(context, now)
        if timed_progress is not None:
            return _StepResult("timed_out", timed_progress)
    outbound = store.begin_publish_operation(claim, operation, now_ms=now)
    key = outbound["external_idempotency_key"]
    generation = outbound["publish_generation"]
    job = context["job"]
    try:
        if operation == "register_generation":
            raw_decision = publisher.register_generation(
                _MODE, claim.job_id, generation, key
            )
        elif operation == "prepare_hidden":
            raw_decision = publisher.prepare_hidden(
                _MODE,
                claim.job_id,
                job["owner_id"],
                outbound["object_key"],
                generation,
                key,
            )
        elif operation == "commit_publish":
            raw_decision = publisher.commit_publish(
                _MODE, claim.job_id, generation, key
            )
        elif operation == "cancel_publish":
            raw_decision = publisher.cancel_publish(
                _MODE, claim.job_id, generation, key
            )
        else:
            raw_decision = publisher.query_decision(_MODE, claim.job_id, key)
        if raw_decision is None:
            if operation != "query_decision":
                raise ValueError("publication_decision_invalid")
            decision = {
                "asset_id": None,
                "current_generation": generation,
                "status": "accepted",
            }
        else:
            decision = _decision_values(raw_decision)
    except DefinitiveNotAccepted:
        persisted = store.record_publish_operation(
            claim,
            operation,
            "pending",
            {
                "outcome": "definitive_not_accepted",
                "reason_code": "definitive_not_accepted",
            },
            now_ms=now,
        )
        return _StepResult(
            "definitive_not_accepted",
            _current_progress(
                context,
                external_idempotency_key=persisted["external_idempotency_key"],
                operation=operation,
                outcome="definitive_not_accepted",
            ),
        )
    except SubmissionUnknown as exc:
        return _record_unknown(
            claim=claim,
            row=outbound,
            now=now,
            store=store,
            reason_code=exc.reason_code,
        )
    except Exception:
        return _record_unknown(
            claim=claim,
            row=outbound,
            now=now,
            store=store,
            reason_code="ambiguous_exception",
        )

    status = decision["status"]
    current_generation = decision["current_generation"]
    if status == "stale_generation" or (
        status == "accepted" and current_generation > claim.fencing_token
    ):
        return _StepResult(
            "stale",
            _current_progress(
                context,
                current_generation=current_generation,
                operation=operation,
                outcome="stale_generation",
            ),
        )
    if status == "accepted" and current_generation != claim.fencing_token:
        return _record_unknown(
            claim=claim,
            row=outbound,
            now=now,
            store=store,
            reason_code="invalid_generation_response",
        )
    if status == "publish_won":
        try:
            store.record_publish_winner(
                claim,
                operation,
                decision["asset_id"],
                decision,
                now_ms=now,
            )
        except LeaseLost:
            return _StepResult(
                "stale",
                _current_progress(context, operation=operation, outcome="stale_claim"),
            )
        return _StepResult(
            "publish_won",
            PublicationProgress(
                "completed",
                {
                    "asset_id": decision["asset_id"],
                    "operation": operation,
                    "outcome": "publish_won",
                },
            ),
        )
    if status == "cancel_won":
        try:
            result = store.record_cancel_winner_and_refund(
                claim,
                operation,
                decision,
                now_ms=now,
            )
        except LeaseLost:
            return _StepResult(
                "stale",
                _current_progress(context, operation=operation, outcome="stale_claim"),
            )
        return _StepResult(
            "cancel_won",
            PublicationProgress(
                "failed",
                {
                    "operation": operation,
                    "outcome": "cancel_won",
                    "refund_intent_id": result["intent"]["id"],
                    "refund_request_amount": result["intent"]["request_amount"],
                    "refund_target_total": result["intent"]["refund_target_total"],
                },
            ),
        )

    recoverable = operation in {
        "commit_publish",
        "cancel_publish",
        "query_decision",
    }
    persisted = store.record_publish_operation(
        claim,
        operation,
        "unknown" if recoverable else "accepted",
        decision,
        now_ms=now,
    )
    if recoverable:
        return _StepResult(
            "accepted_no_verdict" if operation == "query_decision" else "unknown",
            _unknown_progress(persisted, now, reason="accepted_no_verdict"),
        )
    return _StepResult(
        "accepted",
        _current_progress(context, operation=operation, outcome="accepted"),
    )


def create_publish_intent(
    claim: LeaseClaim,
    *,
    metadata_sha256: str,
    now: int,
    store: V3Store,
) -> tuple[dict[str, Any], ...]:
    now = _require_now(now)
    metadata_sha256 = _require_metadata_sha256(metadata_sha256)
    return store.create_publish_intents(
        claim, metadata_sha256, now_ms=now
    )


def register_current_generation(
    claim: LeaseClaim,
    *,
    metadata_sha256: str,
    now: int,
    store: V3Store,
    publisher: SharedAssetPublisher,
) -> PublicationProgress:
    create_publish_intent(
        claim, metadata_sha256=metadata_sha256, now=now, store=store
    )
    context = store.get_publish_context_for_claim(claim, now)
    row = _row_map(context)["register_generation"]
    if row["status"] == "accepted":
        return _current_progress(
            context, operation="register_generation", outcome="accepted"
        )
    if row["status"] in {"pending", "unknown"}:
        evidence = _stored_evidence(row)
        if not evidence or evidence.get("outcome") != "definitive_not_accepted":
            return _unknown_progress(row, now, reason="submission_pending")
    return _invoke_operation(
        claim=claim,
        row=row,
        context=context,
        now=now,
        store=store,
        publisher=publisher,
    ).progress


def prepare_hidden(
    claim: LeaseClaim,
    *,
    metadata_sha256: str,
    now: int,
    store: V3Store,
    publisher: SharedAssetPublisher,
) -> PublicationProgress:
    create_publish_intent(
        claim, metadata_sha256=metadata_sha256, now=now, store=store
    )
    context = store.get_publish_context_for_claim(claim, now)
    rows = _row_map(context)
    register = rows["register_generation"]
    if register["status"] != "accepted":
        return register_current_generation(
            claim,
            metadata_sha256=metadata_sha256,
            now=now,
            store=store,
            publisher=publisher,
        )
    row = rows["prepare_hidden"]
    if row["status"] == "accepted":
        return _current_progress(context, operation="prepare_hidden", outcome="accepted")
    if row["status"] in {"pending", "unknown"}:
        evidence = _stored_evidence(row)
        if not evidence or evidence.get("outcome") != "definitive_not_accepted":
            return _unknown_progress(row, now, reason="submission_pending")
    return _invoke_operation(
        claim=claim,
        row=row,
        context=context,
        now=now,
        store=store,
        publisher=publisher,
    ).progress


def advance_publish(
    claim: LeaseClaim,
    *,
    metadata_sha256: str,
    now: int,
    store: V3Store,
    publisher: SharedAssetPublisher,
) -> PublicationProgress:
    now = _require_now(now)
    create_publish_intent(
        claim, metadata_sha256=metadata_sha256, now=now, store=store
    )
    context = store.get_publish_context_for_claim(claim, now)
    rows = _row_map(context)
    for operation in ("register_generation", "prepare_hidden"):
        row = rows[operation]
        if row["status"] == "accepted":
            continue
        if row["status"] in {"pending", "unknown"}:
            evidence = _stored_evidence(row)
            if not evidence or evidence.get("outcome") != "definitive_not_accepted":
                return _unknown_progress(row, now, reason="submission_pending")
        result = _invoke_operation(
            claim=claim,
            row=row,
            context=context,
            now=now,
            store=store,
            publisher=publisher,
        )
        if result.outcome != "accepted":
            return result.progress
        context = store.get_publish_context_for_claim(claim, now)
        rows = _row_map(context)

    commit = rows["commit_publish"]
    if commit["status"] in {"pending", "unknown"}:
        evidence = _stored_evidence(commit)
        if not evidence or evidence.get("outcome") != "definitive_not_accepted":
            return _unknown_progress(commit, now, reason="submission_pending")
    return _invoke_operation(
        claim=claim,
        row=commit,
        context=context,
        now=now,
        store=store,
        publisher=publisher,
    ).progress


def request_cancel(
    claim: LeaseClaim,
    *,
    metadata_sha256: str,
    now: int,
    store: V3Store,
    publisher: SharedAssetPublisher,
) -> PublicationProgress:
    now = _require_now(now)
    create_publish_intent(
        claim, metadata_sha256=metadata_sha256, now=now, store=store
    )
    context = store.get_publish_context_for_claim(claim, now)
    rows = _row_map(context)
    register = rows["register_generation"]
    stored_final = _stored_final_progress(register, context)
    if stored_final is not None:
        return _invoke_operation(
            claim=claim,
            row=register,
            context=context,
            now=now,
            store=store,
            publisher=publisher,
        ).progress
    if register["status"] != "accepted":
        if register["status"] in {"pending", "unknown"}:
            evidence = _stored_evidence(register)
            if not evidence or evidence.get("outcome") != "definitive_not_accepted":
                return _unknown_progress(register, now, reason="submission_pending")
        registered = _invoke_operation(
            claim=claim,
            row=register,
            context=context,
            now=now,
            store=store,
            publisher=publisher,
        )
        if registered.outcome != "accepted":
            return registered.progress
        context = store.get_publish_context_for_claim(claim, now)
        rows = _row_map(context)

    cancel = rows["cancel_publish"]
    stored_final = _stored_final_progress(cancel, context)
    if stored_final is not None:
        return _invoke_operation(
            claim=claim,
            row=cancel,
            context=context,
            now=now,
            store=store,
            publisher=publisher,
        ).progress
    if cancel["status"] in {"pending", "unknown"}:
        evidence = _stored_evidence(cancel)
        if not evidence or evidence.get("outcome") != "definitive_not_accepted":
            return _unknown_progress(cancel, now, reason="submission_pending")
    return _invoke_operation(
        claim=claim,
        row=cancel,
        context=context,
        now=now,
        store=store,
        publisher=publisher,
    ).progress


def list_due_publish_intents(
    *,
    now: int,
    store: V3Store,
    limit: int = 100,
    cursor: tuple[int, str] | None = None,
) -> tuple[dict[str, Any], ...]:
    now = _require_now(now)
    try:
        return store.list_due_publish_intents(
            now, limit=limit, cursor=cursor
        )
    except ValueError:
        raise
    except Exception as exc:
        if getattr(exc, "error_code", None) in {
            "publish_limit_invalid",
            "publish_cursor_invalid",
        }:
            raise ValueError(exc.error_code) from exc
        raise


def reconcile_asset_decision(
    claim: LeaseClaim,
    *,
    now: int,
    store: V3Store,
    publisher: SharedAssetPublisher,
) -> PublicationProgress:
    now = _require_now(now)
    context = store.get_publish_context_for_claim(claim, now)
    rows = _row_map(context)
    if not rows:
        raise ValueError("publish_intent_missing")
    recoverable = sorted(
        (
            row
            for row in rows.values()
            if row["operation"] != "query_decision"
            and row["status"] in {"pending", "unknown"}
        ),
        key=lambda row: (
            row["first_unknown_at"]
            if row["first_unknown_at"] is not None
            else row["updated_at"],
            row["id"],
        ),
    )
    query_result = _invoke_operation(
        claim=claim,
        row=rows["query_decision"],
        context=context,
        now=now,
        store=store,
        publisher=publisher,
    )
    if query_result.outcome in _FINAL_DECISIONS or query_result.outcome == "stale":
        return query_result.progress
    if query_result.outcome in {"unknown", "definitive_not_accepted"}:
        context = store.get_publish_context_for_claim(claim, now)
        rows = _row_map(context)
        anchor = min(
            (
                row
                for row in rows.values()
                if not isinstance(row.get("first_unknown_at"), bool)
                and isinstance(row.get("first_unknown_at"), int)
            ),
            key=lambda row: (row["first_unknown_at"], row["id"]),
            default=rows["query_decision"],
        )
        return _unknown_progress(
            anchor,
            now,
            reason=query_result.progress.checkpoint["outcome"],
        )
    if not recoverable:
        return query_result.progress
    context = store.get_publish_context_for_claim(claim, now)
    resumed = _invoke_operation(
        claim=claim,
        row=recoverable[0],
        context=context,
        now=now,
        store=store,
        publisher=publisher,
    )
    return resumed.progress
