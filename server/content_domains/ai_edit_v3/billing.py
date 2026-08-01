"""Crash-safe pricing and billing orchestration for AI Edit V3."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, Protocol

from .contracts import (
    LeaseClaim,
    canonical_json,
    normalize_job_request,
    parse_strict_json,
    request_fingerprint,
)
from .store import StoreConflictError, V3Store


INT64_MAX = (1 << 63) - 1
QUOTE_TTL_MS = 900_000
UNKNOWN_TIMEOUT_MS = 300_000
PROCESSING_DEADLINE_MS = 2_700_000

PART_NAMES = (
    "base_task",
    "duration_tier",
    "tts_ceiling",
    "qwen_ceiling",
    "image_ceiling",
    "bgm_sfx_ceiling",
    "render_complexity",
    "one_repair_reserve",
)


class BillingError(RuntimeError):
    """Stable billing failure safe to expose through the application boundary."""

    def __init__(self, error_code: str, message: str):
        self.error_code = error_code
        self.message = message
        super().__init__(f"{error_code}: {message}")


class InjectedCommitFailure(RuntimeError):
    """Test-only crash seam raised from inside the local transaction."""


@dataclass(frozen=True, slots=True)
class LedgerTransaction:
    transaction_key: str
    operation: Literal["deduct", "refund"]
    owner: str
    amount: int
    points_after: int
    created_at: int


@dataclass(frozen=True, slots=True)
class LedgerResult:
    accepted: bool
    transaction: LedgerTransaction | None
    error_code: str | None


class PointsLedger(Protocol):
    def deduct(
        self, owner: str, amount: int, transaction_key: str, reason: str
    ) -> LedgerResult: ...

    def refund(
        self, owner: str, amount: int, transaction_key: str, reason: str
    ) -> LedgerResult: ...

    def query_transaction(
        self, owner: str, transaction_key: str
    ) -> LedgerTransaction | None: ...


@dataclass(frozen=True, slots=True)
class QuoteBreakdown:
    quote_id: str
    environment: str
    owner_id: str
    normalized_request: Mapping[str, Any]
    request_sha256: str
    pricing_version: str
    template_id: str | None
    template_version: str | None
    parts: Mapping[str, Mapping[str, Any]]
    min_points: int
    max_points: int
    expires_at: int
    created_at: int

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)


@dataclass(frozen=True, slots=True)
class BillingIntentDraft:
    intent_id: str
    environment: str
    owner_id: str
    job_id: str
    operation: Literal["pre_debit", "refund_delta", "refund_full"]
    external_idempotency_key: str
    request_sha256: str
    refund_target_total: int
    request_amount: int
    status: str
    first_unknown_at: int | None
    last_checked_at: int | None
    created_at: int
    updated_at: int
    completed_at: int | None

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)


@dataclass(frozen=True, slots=True)
class BillingOutcome:
    job_id: str
    intent: BillingIntentDraft
    next_state: str
    confirmed: bool
    error_code: str | None = None

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)


def _error(error_code: str, message: str) -> BillingError:
    return BillingError(error_code, message)


def _require_identifier(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _error(f"{name}_invalid", f"{name} must be a nonblank string")
    return value


def _require_int64(name: str, value: Any, *, nonnegative: bool = True) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _error("pricing_integer_invalid", f"{name} must be an integer")
    if value < (-INT64_MAX - 1) or value > INT64_MAX:
        raise _error("pricing_overflow", f"{name} is outside signed int64")
    if nonnegative and value < 0:
        raise _error("pricing_integer_invalid", f"{name} must be non-negative")
    return value


def _require_now(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= INT64_MAX:
        raise _error("billing_time_invalid", "now must be a non-negative int64 epoch millisecond")
    return value


def _checked_add(left: int, right: int, error_code: str) -> int:
    if left > INT64_MAX - right:
        raise _error(error_code, "signed int64 addition overflow")
    return left + right


def _checked_multiply(left: int, right: int) -> int:
    if left and right > INT64_MAX // left:
        raise _error("pricing_overflow", "signed int64 multiplication overflow")
    return left * right


def _parse_json_object(raw: str, *, error_code: str) -> dict[str, Any]:
    try:
        value = parse_strict_json(
            raw,
            max_bytes=262_144,
            max_depth=16,
            max_items=4_096,
            max_string_chars=65_536,
        )
    except Exception as exc:
        raise _error(error_code, "persisted JSON is invalid") from exc
    if not isinstance(value, dict):
        raise _error(error_code, "persisted JSON must be an object")
    return value


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    return value


def _published_pricing(store: V3Store) -> tuple[dict[str, Any], dict[str, Any]]:
    rows = store.list_published_pricing_versions()
    if not rows:
        raise _error("pricing_unavailable", "no published pricing version exists")
    if len(rows) != 1:
        raise _error("pricing_ambiguous", "published pricing version is ambiguous")
    row = rows[0]
    parameters = _parse_json_object(
        row["parameters_json"], error_code="pricing_parameters_invalid"
    )
    canonical = canonical_json(parameters)
    if hashlib.sha256(canonical).hexdigest() != row["parameters_sha256"]:
        raise _error("pricing_parameters_invalid", "pricing parameters hash mismatches")
    if canonical.decode("utf-8") != row["parameters_json"]:
        raise _error("pricing_parameters_invalid", "pricing parameters are not canonical")
    return row, parameters


def _template_for_request(
    store: V3Store, normalized_request: Mapping[str, Any]
) -> tuple[str | None, str | None]:
    if normalized_request["creation_mode"] != "template_reference":
        return None, None
    template_id = normalized_request["template_id"]
    rows = store.list_template_versions(template_id)
    published = [row for row in rows if row["status"] == "published"]
    if not rows:
        raise _error("template_not_found", "template reference does not exist")
    if not published:
        raise _error("template_unpublished", "template reference is not published")
    if len(published) != 1:
        raise _error("template_ambiguous", "published template version is ambiguous")
    row = published[0]
    try:
        ratios = parse_strict_json(
            row["supported_ratios_json"],
            max_bytes=4_096,
            max_depth=4,
            max_items=16,
            max_string_chars=32,
        )
    except Exception as exc:
        raise _error("template_ratios_invalid", "template ratios are invalid") from exc
    if (
        not isinstance(ratios, list)
        or not ratios
        or any(ratio not in {"16:9", "9:16"} for ratio in ratios)
        or len(ratios) != len(set(ratios))
    ):
        raise _error("template_ratios_invalid", "template ratios are invalid")
    requested_ratio = normalized_request["ratio"]
    supported = set(ratios)
    ratio_ok = (
        requested_ratio in supported
        if requested_ratio != "auto"
        else supported == {"16:9", "9:16"}
    )
    if not ratio_ok:
        raise _error("template_ratio_unsupported", "template does not support request ratio")
    return template_id, row["version"]


def _calculate_parts(
    parameters: Mapping[str, Any], normalized_request: Mapping[str, Any]
) -> tuple[dict[str, dict[str, Any]], int, int]:
    if set(parameters) != {"parts"} or not isinstance(parameters.get("parts"), Mapping):
        raise _error("pricing_parameters_invalid", "pricing requires exactly one parts object")
    specifications = parameters["parts"]
    if set(specifications) != set(PART_NAMES):
        raise _error("pricing_parts_invalid", "pricing must contain exactly eight named parts")

    parts: dict[str, dict[str, Any]] = {}
    min_total = 0
    max_total = 0
    for name in PART_NAMES:
        specification = specifications[name]
        expected_keys = {"ceiling_quantity", "min_rate", "max_rate"}
        if name == "tts_ceiling":
            expected_keys.add("unit_size")
        if not isinstance(specification, Mapping) or set(specification) != expected_keys:
            raise _error("pricing_part_invalid", f"{name} has invalid pricing keys")
        ceiling = _require_int64(f"{name}.ceiling_quantity", specification["ceiling_quantity"])
        min_rate = _require_int64(f"{name}.min_rate", specification["min_rate"])
        max_rate = _require_int64(f"{name}.max_rate", specification["max_rate"])
        if min_rate > max_rate:
            raise _error("pricing_rate_invalid", f"{name} min rate exceeds max rate")
        if name in {"base_task", "one_repair_reserve"} and ceiling != 1:
            raise _error("pricing_quantity_invalid", f"{name} quantity must be one")
        quantity = ceiling
        quantity_source = "published_ceiling"
        if name == "tts_ceiling":
            unit_size = _require_int64("tts_ceiling.unit_size", specification["unit_size"])
            if unit_size == 0:
                raise _error("pricing_quantity_invalid", "TTS unit size must be positive")
            if normalized_request["input_type"] != "script_to_audio_video":
                quantity = 0
                quantity_source = "not_requested"
            else:
                text_length = len(normalized_request["tts_input"]["text"])
                quantity = (text_length + unit_size - 1) // unit_size
                quantity_source = "tts_text_units"
                if quantity > ceiling:
                    raise _error("pricing_ceiling_exceeded", "TTS request exceeds published ceiling")
        minimum = _checked_multiply(quantity, min_rate)
        maximum = _checked_multiply(quantity, max_rate)
        min_total = _checked_add(min_total, minimum, "pricing_overflow")
        max_total = _checked_add(max_total, maximum, "pricing_overflow")
        evidence = {
            "quantity": quantity,
            "quantity_source": quantity_source,
            "min_rate": min_rate,
            "max_rate": max_rate,
            "min_points": minimum,
            "max_points": maximum,
        }
        if name == "tts_ceiling":
            evidence["unit_size"] = specification["unit_size"]
            evidence["ceiling_quantity"] = ceiling
        parts[name] = evidence
    return parts, min_total, max_total


def create_quote(
    owner_id: str,
    request: Mapping[str, Any],
    *,
    now: int,
    store: V3Store,
    quote_id: str | None = None,
) -> QuoteBreakdown:
    """Create and persist a fifteen-minute quote from one published version."""

    _require_identifier("owner_id", owner_id)
    now = _require_now(now)
    normalized = normalize_job_request(request)
    pricing, parameters = _published_pricing(store)
    template_id, template_version = _template_for_request(store, normalized)
    parts, minimum, maximum = _calculate_parts(parameters, normalized)
    expires_at = _checked_add(now, QUOTE_TTL_MS, "quote_expiry_overflow")
    quote_id = uuid.uuid4().hex if quote_id is None else _require_identifier("quote_id", quote_id)
    row = store.insert_quote(
        owner_id,
        quote_id,
        normalized,
        pricing_version=pricing["version"],
        min_points=minimum,
        max_points=maximum,
        breakdown={"parts": parts, "min_points": minimum, "max_points": maximum},
        expires_at=expires_at,
        created_at=now,
        template_id=template_id,
        template_version=template_version,
    )
    if row is None:
        raise _error("quote_conflict", "quote identifier raced with another owner")
    return QuoteBreakdown(
        quote_id=row["quote_id"],
        environment=row["environment"],
        owner_id=row["owner_id"],
        normalized_request=_freeze(normalized),
        request_sha256=row["request_sha256"],
        pricing_version=row["pricing_version"],
        template_id=row["template_id"],
        template_version=row["template_version"],
        parts=_freeze(parts),
        min_points=row["min_points"],
        max_points=row["max_points"],
        expires_at=row["expires_at"],
        created_at=row["created_at"],
    )


def _intent_from_row(row: Mapping[str, Any]) -> BillingIntentDraft:
    return BillingIntentDraft(
        intent_id=row["id"],
        environment=row["environment"],
        owner_id=row["owner_id"],
        job_id=row["job_id"],
        operation=row["operation"],
        external_idempotency_key=row["external_idempotency_key"],
        request_sha256=row["request_sha256"],
        refund_target_total=row["refund_target_total"],
        request_amount=row["request_amount"],
        status=row["status"],
        first_unknown_at=row["first_unknown_at"],
        last_checked_at=row["last_checked_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        completed_at=row["completed_at"],
    )


def create_job_with_predebit(
    owner_id: str,
    request: Mapping[str, Any],
    quote_id: str,
    idempotency_key: str,
    *,
    now: int,
    store: V3Store,
    environment: str | None = None,
    failpoint: str | None = None,
) -> BillingOutcome:
    """Atomically persist a created job and immutable pre-debit outbox row."""

    _require_identifier("owner_id", owner_id)
    _require_identifier("quote_id", quote_id)
    _require_identifier("idempotency_key", idempotency_key)
    now = _require_now(now)
    normalized = normalize_job_request(request)
    environment = store.environment if environment is None else environment
    if environment != store.environment:
        raise _error(
            "quote_environment_mismatch",
            "quote and job must use the configured V3 environment",
        )
    if failpoint not in {None, "after_job_before_intent"}:
        raise _error("billing_failpoint_invalid", "unknown billing failpoint")
    failure = (
        InjectedCommitFailure("injected failure after job insert")
        if failpoint == "after_job_before_intent"
        else None
    )
    try:
        result = store.create_job_with_predebit(
            owner_id,
            uuid.uuid4().hex,
            quote_id,
            idempotency_key,
            normalized,
            now_ms=now,
            intent_id=uuid.uuid4().hex,
            fail_after_job=failure,
            environment=environment,
        )
    except StoreConflictError as exc:
        raise _error(exc.error_code, exc.message) from exc
    return BillingOutcome(
        job_id=result["job"]["job_id"],
        intent=_intent_from_row(result["intent"]),
        next_state=result["job"]["state"],
        confirmed=result["intent"]["status"] == "completed",
    )


def _outcome(
    result: Mapping[str, Mapping[str, Any]],
    *,
    error_code: str | None = None,
) -> BillingOutcome:
    intent_row = result["intent"]
    intent = _intent_from_row(intent_row)
    context = (intent_row["reason"], intent_row["resume_state"])
    allowed_contexts = {
        "pre_debit": {("prehold", "preholding")},
        "refund_delta": {
            ("settlement", "settling"),
            ("refund", "refund_pending"),
        },
        "refund_full": {("refund", "refund_pending")},
    }
    if context not in allowed_contexts.get(intent.operation, set()):
        raise _error(
            "billing_context_conflict",
            "billing intent has an invalid durable recovery context",
        )
    next_state = result["job"]["state"]
    if intent.status == "completed" and intent.operation == "refund_delta":
        if context == ("refund", "refund_pending"):
            if next_state not in {
                "refund_pending",
                "billing_reconciling",
                "failed_reconciliation_pending",
            }:
                raise _error(
                    "billing_context_conflict",
                    "refund recovery context conflicts with the current job state",
                )
            next_state = "refund_pending"
        else:
            next_state = "publishing"
    elif intent.status == "completed" and intent.operation == "refund_full":
        next_state = "refunded"
    return BillingOutcome(
        job_id=intent.job_id,
        intent=intent,
        next_state=next_state,
        confirmed=intent.status == "completed",
        error_code=error_code,
    )


def _is_prehold_admission_timeout(
    result: Mapping[str, Mapping[str, Any]],
) -> bool:
    intent = result["intent"]
    job = result["job"]
    if not (
        intent["operation"] == "pre_debit"
        and intent["status"] == "reconciliation_pending"
        and intent["reason"] == "prehold"
        and intent["resume_state"] == "preholding"
        and job["state"] == "failed_reconciliation_pending"
        and job["reconciliation_reason"] == "prehold"
        and job["resume_state"] == "preholding"
    ):
        return False
    evidence_raw = intent["authority_evidence_json"]
    if not isinstance(evidence_raw, str):
        raise _error(
            "prehold_admission_evidence_invalid",
            "expired prehold admission is missing durable evidence",
        )
    evidence = _parse_json_object(
        evidence_raw,
        error_code="prehold_admission_evidence_invalid",
    )
    created_at = job["created_at"]
    if not (
        isinstance(created_at, int)
        and not isinstance(created_at, bool)
        and 0 <= created_at <= INT64_MAX - UNKNOWN_TIMEOUT_MS
        and intent["first_unknown_at"] == created_at
        and set(evidence)
        == {"admission_deadline_at", "observed_at", "transmission"}
        and evidence["admission_deadline_at"] == created_at + UNKNOWN_TIMEOUT_MS
        and isinstance(evidence["observed_at"], int)
        and not isinstance(evidence["observed_at"], bool)
        and evidence["admission_deadline_at"] <= evidence["observed_at"] <= INT64_MAX
        and evidence["transmission"] == "not_started"
    ):
        raise _error(
            "prehold_admission_evidence_invalid",
            "expired prehold admission evidence is inconsistent",
        )
    return True


def _transaction_evidence(transaction: LedgerTransaction) -> dict[str, Any]:
    return {
        "transaction_key": transaction.transaction_key,
        "operation": transaction.operation,
        "owner": transaction.owner,
        "amount": transaction.amount,
        "points_after": transaction.points_after,
        "created_at": transaction.created_at,
    }


def _validate_authority(
    transaction: Any,
    intent: BillingIntentDraft,
) -> tuple[bool, dict[str, Any]]:
    if not isinstance(transaction, LedgerTransaction):
        return False, {"authoritative": True, "conflict": "malformed_transaction"}
    integer_fields = (transaction.amount, transaction.points_after, transaction.created_at)
    if any(
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < (-INT64_MAX - 1)
        or value > INT64_MAX
        for value in integer_fields
    ) or transaction.amount < 0 or transaction.created_at < 0:
        return False, {"authoritative": True, "conflict": "invalid_integer"}
    expected_operation = "deduct" if intent.operation == "pre_debit" else "refund"
    evidence = _transaction_evidence(transaction)
    evidence["authoritative"] = True
    valid = (
        transaction.owner == intent.owner_id
        and transaction.transaction_key == intent.external_idempotency_key
        and transaction.operation == expected_operation
        and transaction.amount == intent.request_amount
    )
    if not valid:
        evidence["conflict"] = "identity_or_amount_mismatch"
    return valid, evidence


def _confirm_transaction(
    intent: BillingIntentDraft,
    transaction: LedgerTransaction,
    evidence: Mapping[str, Any],
    *,
    claim: LeaseClaim,
    now: int,
    store: V3Store,
) -> BillingOutcome:
    try:
        if intent.operation == "pre_debit":
            current = store.get_billing_for_claim(intent.intent_id, claim, now)
            job_created_at = current["job"]["created_at"]
            if transaction.created_at < job_created_at:
                return _mark_unknown(
                    intent.intent_id,
                    claim,
                    now=now,
                    store=store,
                    evidence={
                        **dict(evidence),
                        "conflict": "authority_before_job",
                    },
                    error_code="billing_authority_conflict",
                )
            refund_route = (
                transaction.created_at - job_created_at >= UNKNOWN_TIMEOUT_MS
                or current["job"]["state"] == "failed_reconciliation_pending"
            )
            deadline = None
            if not refund_route:
                try:
                    deadline = _checked_add(
                        transaction.created_at,
                        PROCESSING_DEADLINE_MS,
                        "processing_deadline_overflow",
                    )
                except BillingError as exc:
                    return _mark_unknown(
                        intent.intent_id,
                        claim,
                        now=now,
                        store=store,
                        evidence={
                            **dict(evidence),
                            "conflict": "processing_deadline_overflow",
                        },
                        error_code=exc.error_code,
                    )
            result = store.confirm_predebit(
                intent.intent_id,
                claim,
                authority_created_at=transaction.created_at,
                processing_deadline_at=deadline,
                authority_evidence=evidence,
                now_ms=now,
            )
        else:
            result = store.confirm_refund(
                intent.intent_id,
                claim,
                authority_evidence=evidence,
                now_ms=now,
            )
    except StoreConflictError as exc:
        raise _error(exc.error_code, exc.message) from exc
    return _outcome(result)


def _unresolved_outcome(
    result: Mapping[str, Mapping[str, Any]],
    *,
    claim: LeaseClaim,
    now: int,
    store: V3Store,
    error_code: str,
) -> BillingOutcome:
    if result["job"]["state"] == "failed_reconciliation_pending":
        return _outcome(result, error_code=error_code)
    first_unknown_at = result["intent"]["first_unknown_at"]
    if first_unknown_at is None:
        raise _error(
            "billing_unknown_time_missing",
            "unknown billing intent has no durable first-unknown time",
        )
    if now - first_unknown_at >= UNKNOWN_TIMEOUT_MS:
        result = store.timeout_billing_reconciliation(
            result["intent"]["id"], claim, now
        )
    return _outcome(result, error_code=error_code)


def _mark_unknown(
    intent_id: str,
    claim: LeaseClaim,
    *,
    now: int,
    store: V3Store,
    evidence: Mapping[str, Any],
    error_code: str,
) -> BillingOutcome:
    result = store.mark_billing_reconciling(intent_id, claim, now)
    result = store.record_billing_unknown(
        intent_id,
        claim,
        authority_evidence=evidence,
        now_ms=now,
    )
    return _unresolved_outcome(
        result,
        claim=claim,
        now=now,
        store=store,
        error_code=error_code,
    )


def process_pending_intent(
    intent_id: str,
    *,
    claim: LeaseClaim,
    ledger: PointsLedger,
    now: int,
    store: V3Store,
    failpoint: str | None = None,
) -> BillingOutcome:
    """Submit a never-transmitted intent, or query one that may have transmitted."""

    now = _require_now(now)
    allowed_failpoints = {
        None,
        "after_admission_timeout_commit",
        "after_intent_commit_before_ledger",
        "after_external_effect_before_local_confirmation",
        "after_local_confirmation",
    }
    if failpoint not in allowed_failpoints:
        raise _error("billing_failpoint_invalid", "unknown billing failpoint")
    current = store.get_billing_for_claim(intent_id, claim, now)
    if _is_prehold_admission_timeout(current):
        if failpoint == "after_admission_timeout_commit":
            raise InjectedCommitFailure(
                "injected crash after prehold admission timeout commit"
            )
        return _outcome(current, error_code="prehold_admission_timeout")
    if current["intent"]["status"] == "completed":
        return _outcome(current)
    if current["intent"]["status"] not in {"pending", "retryable_absent"}:
        return reconcile_unknown_intent(
            intent_id,
            claim=claim,
            ledger=ledger,
            now=now,
            store=store,
        )
    current = store.begin_billing_transmission(intent_id, claim, now)
    if _is_prehold_admission_timeout(current):
        if failpoint == "after_admission_timeout_commit":
            raise InjectedCommitFailure(
                "injected crash after prehold admission timeout commit"
            )
        return _outcome(current, error_code="prehold_admission_timeout")
    intent = _intent_from_row(current["intent"])
    if failpoint == "after_intent_commit_before_ledger":
        raise InjectedCommitFailure("injected crash before external ledger call")
    try:
        if intent.operation == "pre_debit":
            result = ledger.deduct(
                intent.owner_id,
                intent.request_amount,
                intent.external_idempotency_key,
                "AI Edit V3 pre-debit",
            )
        else:
            result = ledger.refund(
                intent.owner_id,
                intent.request_amount,
                intent.external_idempotency_key,
                "AI Edit V3 cumulative refund",
            )
    except Exception:
        return _mark_unknown(
            intent_id,
            claim,
            now=now,
            store=store,
            evidence={"transport": "unknown", "authoritative": False},
            error_code="billing_transport_unknown",
        )
    if failpoint == "after_external_effect_before_local_confirmation":
        raise InjectedCommitFailure("injected crash after external ledger effect")
    if (
        not isinstance(result, LedgerResult)
        or type(result.accepted) is not bool
        or (
            result.error_code is not None
            and (not isinstance(result.error_code, str) or not result.error_code)
        )
        or not result.accepted
        or result.transaction is None
    ):
        return _mark_unknown(
            intent_id,
            claim,
            now=now,
            store=store,
            evidence={"response": "unconfirmed", "authoritative": False},
            error_code="billing_response_unknown",
        )
    valid, evidence = _validate_authority(result.transaction, intent)
    if not valid:
        return _mark_unknown(
            intent_id,
            claim,
            now=now,
            store=store,
            evidence=evidence,
            error_code="billing_authority_conflict",
        )
    outcome = _confirm_transaction(
        intent,
        result.transaction,
        evidence,
        claim=claim,
        now=now,
        store=store,
    )
    if failpoint == "after_local_confirmation":
        raise InjectedCommitFailure("injected crash after local billing confirmation")
    return outcome


def reconcile_unknown_intent(
    intent_id: str,
    *,
    claim: LeaseClaim,
    ledger: PointsLedger,
    now: int,
    store: V3Store,
) -> BillingOutcome:
    """Use only authoritative lookup once an intent may have reached the ledger."""

    now = _require_now(now)
    current = store.get_billing_for_claim(intent_id, claim, now)
    intent = _intent_from_row(current["intent"])
    if intent.status == "completed":
        return _outcome(current)
    if intent.status not in {"unknown", "reconciliation_pending"}:
        raise _error(
            "billing_intent_not_unknown",
            "only a potentially transmitted intent can be reconciled",
        )
    try:
        transaction = ledger.query_transaction(
            intent.owner_id, intent.external_idempotency_key
        )
    except Exception:
        return _mark_unknown(
            intent_id,
            claim,
            now=now,
            store=store,
            evidence={"query_transport": "unknown", "authoritative": False},
            error_code="billing_query_unknown",
        )
    if transaction is None:
        result = store.mark_billing_authority_absent(
            intent_id, claim, now_ms=now
        )
        return _outcome(result, error_code="billing_authority_absent")
    valid, evidence = _validate_authority(transaction, intent)
    if not valid:
        return _mark_unknown(
            intent_id,
            claim,
            now=now,
            store=store,
            evidence=evidence,
            error_code="billing_authority_conflict",
        )
    return _confirm_transaction(
        intent,
        transaction,
        evidence,
        claim=claim,
        now=now,
        store=store,
    )


def list_due_billing_intents(
    *,
    now: int,
    store: V3Store,
    limit: int = 100,
) -> tuple[BillingIntentDraft, ...]:
    now = _require_now(now)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= INT64_MAX:
        raise _error("billing_limit_invalid", "limit must be a positive int64")
    return tuple(
        _intent_from_row(row)
        for row in store.list_due_billing_intents(now, limit=limit)
    )


def request_delta_refund(
    claim: LeaseClaim,
    *,
    actual_charge: int,
    now: int,
    store: V3Store,
) -> BillingOutcome:
    """Persist the difference between confirmed prehold and actual charge."""

    now = _require_now(now)
    job = store.get_job_billing_for_claim(claim, now)
    preheld = job["confirmed_preheld_total"]
    if (
        isinstance(actual_charge, bool)
        or not isinstance(actual_charge, int)
        or actual_charge < 0
        or actual_charge > preheld
        or actual_charge > INT64_MAX
    ):
        raise _error(
            "actual_charge_invalid",
            "actual charge must be an integer within the confirmed prehold",
        )
    target = preheld - actual_charge
    try:
        result = store.create_refund_intent(
            claim,
            "refund_delta",
            target,
            intent_id=uuid.uuid4().hex,
            now_ms=now,
        )
    except StoreConflictError as exc:
        raise _error(exc.error_code, exc.message) from exc
    return _outcome(result)


def request_full_refund(
    claim: LeaseClaim,
    *,
    now: int,
    store: V3Store,
) -> BillingOutcome:
    """Persist a cumulative full-refund target without double-refunding delta."""

    now = _require_now(now)
    job = store.get_job_billing_for_claim(claim, now)
    try:
        result = store.create_refund_intent(
            claim,
            "refund_full",
            job["confirmed_preheld_total"],
            intent_id=uuid.uuid4().hex,
            now_ms=now,
        )
    except StoreConflictError as exc:
        raise _error(exc.error_code, exc.message) from exc
    return _outcome(result)
