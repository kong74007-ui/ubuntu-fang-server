"""Versioned quotes, maximum precharge, settlement, and full refunds."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import uuid
from contextlib import closing
from typing import Any, Callable

from . import ai_edit_v2_store as store
from . import points
from .ai_edit_v2_schema import TERMINAL_STATES, validate_job_draft


PRICE_VERSION = "ai-edit-v2-price-v1"
QUOTE_TTL_SECONDS = 15 * 60
_DEFAULT_PRICE_CONFIG = {
    "base_points": 20,
    "duration_block_ms": 30_000,
    "duration_points_per_block": 4,
    "creative_mode_points": {
        "natural_brief": 0,
        "platform_template": 5,
        "open_generation": 15,
    },
    "required_material_points": 1,
    "reference_pair_points": 1,
    "max_multiplier": 1.25,
    "generation_reserve_points": {
        "natural_brief": 4,
        "platform_template": 4,
        "open_generation": 10,
    },
}


class BillingError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class PrechargePending(BillingError):
    def __init__(self, job_id: str, held_points: int):
        super().__init__("billing_pending")
        self.job_id = job_id
        self.held_points = held_points


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _draft_hash(draft: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(draft).encode("utf-8")).hexdigest()


def default_price_config() -> dict[str, Any]:
    return json.loads(_canonical(_DEFAULT_PRICE_CONFIG))


def _validate_price_config(config: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(config, dict) or set(config) != set(_DEFAULT_PRICE_CONFIG):
        raise BillingError("price_config_invalid")
    clean = json.loads(_canonical(config))
    positive = (
        "base_points", "duration_block_ms", "duration_points_per_block",
        "required_material_points", "reference_pair_points",
    )
    if any(not isinstance(clean[key], int) or clean[key] <= 0 for key in positive):
        raise BillingError("price_config_invalid")
    if not isinstance(clean["max_multiplier"], (int, float)) or not 1 <= clean["max_multiplier"] <= 5:
        raise BillingError("price_config_invalid")
    modes = {"natural_brief", "platform_template", "open_generation"}
    for key in ("creative_mode_points", "generation_reserve_points"):
        values = clean[key]
        if not isinstance(values, dict) or set(values) != modes:
            raise BillingError("price_config_invalid")
        if any(not isinstance(value, int) or value < 0 for value in values.values()):
            raise BillingError("price_config_invalid")
    return clean


def _pricing_db_path(path: str | None = None) -> str:
    if path:
        return path
    return os.environ.get("AI_EDIT_V2_PRICING_DB") or os.environ.get("ADMIN_DB") or os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "admin_config.db"
    )


def _open_pricing(path: str | None = None) -> sqlite3.Connection:
    target = _pricing_db_path(path)
    parent = os.path.dirname(os.path.abspath(target))
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(target, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS ai_edit_v2_price_versions(
               version TEXT PRIMARY KEY,
               status TEXT NOT NULL CHECK(status IN ('draft','published')),
               config_json TEXT NOT NULL,
               created_by TEXT NOT NULL,
               created_at INTEGER NOT NULL,
               published_by TEXT,
               published_at INTEGER
           )"""
    )
    return conn


def _price_public(row: Any) -> dict[str, Any]:
    return {
        "version": row["version"],
        "status": row["status"],
        "config": json.loads(row["config_json"]),
        "created_by": row["created_by"],
        "created_at": int(row["created_at"]),
        "published_by": row["published_by"],
        "published_at": int(row["published_at"]) if row["published_at"] is not None else None,
    }


def create_price_draft(
    actor: str, version: str, config: dict[str, Any], now: int, *, pricing_db_path: str | None = None
) -> dict[str, Any]:
    version = str(version or "").strip()
    if not version or len(version) > 80:
        raise BillingError("price_version_invalid")
    clean = _validate_price_config(config)
    try:
        with closing(_open_pricing(pricing_db_path)) as conn:
            conn.execute(
                "INSERT INTO ai_edit_v2_price_versions(version,status,config_json,created_by,created_at) VALUES(?,?,?,?,?)",
                (version, "draft", _canonical(clean), actor, now),
            )
            row = conn.execute(
                "SELECT * FROM ai_edit_v2_price_versions WHERE version=?", (version,)
            ).fetchone()
    except sqlite3.IntegrityError as exc:
        raise BillingError("price_version_immutable") from exc
    return _price_public(row)


def list_price_versions(*, pricing_db_path: str | None = None) -> list[dict[str, Any]]:
    with closing(_open_pricing(pricing_db_path)) as conn:
        rows = conn.execute(
            "SELECT * FROM ai_edit_v2_price_versions ORDER BY created_at DESC, version DESC"
        ).fetchall()
    return [_price_public(row) for row in rows]


def publish_price_version(
    actor: str, version: str, confirmation: str, now: int, *, pricing_db_path: str | None = None
) -> dict[str, Any]:
    if confirmation != f"发布 {version}":
        raise BillingError("publish_confirmation_required")
    with closing(_open_pricing(pricing_db_path)) as conn:
        row = conn.execute(
            "SELECT * FROM ai_edit_v2_price_versions WHERE version=?", (version,)
        ).fetchone()
        if row is None:
            raise BillingError("price_version_not_found")
        if row["status"] != "draft":
            raise BillingError("price_version_immutable")
        changed = conn.execute(
            """UPDATE ai_edit_v2_price_versions
               SET status='published',published_by=?,published_at=?
               WHERE version=? AND status='draft'""",
            (actor, now, version),
        ).rowcount
        if changed != 1:
            raise BillingError("price_version_immutable")
        row = conn.execute(
            "SELECT * FROM ai_edit_v2_price_versions WHERE version=?", (version,)
        ).fetchone()
    return _price_public(row)


def _active_price(*, pricing_db_path: str | None = None) -> tuple[str, dict[str, Any]]:
    with closing(_open_pricing(pricing_db_path)) as conn:
        row = conn.execute(
            """SELECT * FROM ai_edit_v2_price_versions WHERE status='published'
               ORDER BY published_at DESC, version DESC LIMIT 1"""
        ).fetchone()
    if row is None:
        return PRICE_VERSION, default_price_config()
    return row["version"], _validate_price_config(json.loads(row["config_json"]))


def _breakdown(draft: dict[str, Any], config: dict[str, Any]) -> dict[str, int]:
    duration_ms = draft.get("target_duration_ms") or draft["main_input"]["duration_ms"]
    mode = draft["creation_mode"]
    result = {
        "base": config["base_points"],
        "duration": max(
            config["duration_points_per_block"],
            math.ceil(duration_ms / config["duration_block_ms"]) * config["duration_points_per_block"],
        ),
        "creative_mode": config["creative_mode_points"][mode],
        "required_materials": len(draft.get("required_materials") or []) * config["required_material_points"],
        "reference_analysis": math.ceil(len(draft.get("reference_materials") or []) / 2) * config["reference_pair_points"],
    }
    return result


def preview_price_config(config: dict[str, Any], draft: dict[str, Any]) -> dict[str, Any]:
    validate_job_draft(draft)
    clean = _validate_price_config(config)
    breakdown = _breakdown(draft, clean)
    minimum = sum(breakdown.values())
    maximum = max(
        minimum,
        math.ceil(minimum * clean["max_multiplier"])
        + clean["generation_reserve_points"][draft["creation_mode"]],
    )
    return {"min_points": minimum, "max_points": maximum, "breakdown": breakdown}


def _quote_public(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "min_points": int(row["min_points"]),
        "max_points": int(row["max_points"]),
        "breakdown": json.loads(row["breakdown_json"]),
        "price_version": row["price_version"],
        "expires_at": int(row["expires_at"]),
    }


def create_quote(
    owner: str,
    draft: dict[str, Any],
    now: int,
    *,
    uuid_factory: Callable[[], Any] = uuid.uuid4,
    db_path: str | None = None,
    pricing_db_path: str | None = None,
) -> dict[str, Any]:
    validate_job_draft(draft)
    price_version, config = _active_price(pricing_db_path=pricing_db_path)
    preview = preview_price_config(config, draft)
    breakdown = preview["breakdown"]
    minimum = preview["min_points"]
    maximum = preview["max_points"]
    quote_id = str(uuid_factory())
    expires_at = now + QUOTE_TTL_SECONDS
    draft_hash = _draft_hash(draft)
    try:
        with closing(store.open_store(store._db_path(db_path))) as conn:
            conn.execute(
                """INSERT INTO edit_v2_quotes(
                       id,owner,draft_hash,min_points,max_points,breakdown_json,
                       price_version,expires_at,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    quote_id,
                    owner,
                    draft_hash,
                    minimum,
                    maximum,
                    _canonical(breakdown),
                    price_version,
                    expires_at,
                    now,
                ),
            )
            row = conn.execute(
                "SELECT * FROM edit_v2_quotes WHERE id=?", (quote_id,)
            ).fetchone()
    except sqlite3.IntegrityError as exc:
        with closing(store.open_store(store._db_path(db_path))) as conn:
            row = conn.execute(
                "SELECT * FROM edit_v2_quotes WHERE id=?", (quote_id,)
            ).fetchone()
        if row is None or row["owner"] != owner or row["draft_hash"] != draft_hash:
            raise BillingError("quote_id_conflict") from exc
    return _quote_public(row)


def validate_quote(
    owner: str,
    draft: dict[str, Any],
    quote_id: str,
    now: int,
    *,
    db_path: str | None = None,
) -> dict[str, Any]:
    validate_job_draft(draft)
    with closing(store.open_store(store._db_path(db_path))) as conn:
        row = conn.execute("SELECT * FROM edit_v2_quotes WHERE id=?", (quote_id,)).fetchone()
    if row is None:
        raise BillingError("quote_not_found")
    if row["owner"] != owner:
        raise BillingError("quote_owner_mismatch")
    if row["draft_hash"] != _draft_hash(draft):
        raise BillingError("quote_draft_mismatch")
    if now > int(row["expires_at"]):
        raise BillingError("quote_expired")
    return _quote_public(row)


def _billing_row(job_id: str, db_path: str | None = None):
    with closing(store.open_store(store._db_path(db_path))) as conn:
        return conn.execute(
            "SELECT * FROM edit_v2_billing WHERE job_id=? AND operation='hold'", (job_id,)
        ).fetchone()


def _is_definitive_points_rejection(exc: Exception) -> bool:
    status = int(getattr(exc, "status", 0) or 0)
    return 400 <= status < 500


def _reject_precharge(row: Any, exc: Exception, now: int, db_path: str | None) -> None:
    rejection = {
        "status": int(getattr(exc, "status", 400) or 400),
        "detail": str(getattr(exc, "detail", str(exc))),
    }
    with closing(store.open_store(store._db_path(db_path))) as conn:
        changed = conn.execute(
            """UPDATE edit_v2_billing SET status='rejected',response_json=?,updated_at=?
               WHERE id=? AND status='pending'""",
            (_canonical(rejection), now, row["id"]),
        ).rowcount
        job = conn.execute(
            "SELECT status FROM edit_v2_jobs WHERE id=?", (row["job_id"],)
        ).fetchone()
    if changed and job is not None:
        store.transition(
            row["job_id"],
            job["status"],
            "validation_failed",
            {"reason": "precharge_rejected", "points_status": rejection["status"]},
            now,
            db_path=db_path,
        )


def _raise_rejected_precharge(row: Any) -> None:
    rejection = json.loads(row["response_json"] or "{}")
    raise points.AuthPointsError(
        int(rejection.get("status") or 402),
        str(rejection.get("detail") or "precharge rejected"),
    )


def _queue_held_job(job_id: str, now: int, db_path: str | None = None) -> dict[str, Any]:
    targets = {
        "created": "validating",
        "validating": "quoting",
        "quoting": "precharging",
        "precharging": "queued",
    }
    for _ in range(10):
        with closing(store.open_store(store._db_path(db_path))) as conn:
            row = conn.execute("SELECT * FROM edit_v2_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise BillingError("job_not_found")
        if row["status"] == "queued":
            return dict(row)
        expected = row["status"]
        if expected not in targets:
            raise BillingError("job_queue_state_conflict")
        target = targets[expected]
        if not store.transition(
            job_id, expected, target, {"synchronous": True}, now, db_path=db_path
        ):
            continue
    raise BillingError("job_queue_state_conflict")


def reconcile_pending_precharges(
    now: int,
    *,
    points_client: Any = points,
    db_path: str | None = None,
) -> int:
    with closing(store.open_store(store._db_path(db_path))) as conn:
        rows = conn.execute(
            """SELECT b.*,j.owner FROM edit_v2_billing b
               JOIN edit_v2_jobs j ON j.id=b.job_id
               WHERE b.operation='hold' AND b.status='pending'"""
        ).fetchall()
    recovered = 0
    for row in rows:
        try:
            points_after = points_client.deduct_points(
                row["owner"], int(row["amount"]), "ai-edit-v2 maximum precharge",
                transaction_key=row["transaction_key"],
            )
        except Exception as exc:
            if not _is_definitive_points_rejection(exc):
                # A provider timeout is an unknown result, not a failed hold. Keep
                # the durable pending operation and let the remaining rows reconcile.
                continue
            _reject_precharge(row, exc, now, db_path)
            continue
        try:
            with closing(store.open_store(store._db_path(db_path))) as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    job = conn.execute(
                        "SELECT status,error_code FROM edit_v2_jobs WHERE id=?",
                        (row["job_id"],),
                    ).fetchone()
                    if job is None:
                        raise BillingError("job_not_found")
                    must_refund = (
                        job["status"] in TERMINAL_STATES
                        or job["error_code"] == "duplicate_successor_quarantined"
                    )
                    target = "refund_pending" if must_refund else "held"
                    changed = conn.execute(
                        """UPDATE edit_v2_billing
                           SET status=?,response_json=?,updated_at=?
                           WHERE id=? AND status='pending'""",
                        (target, _canonical({"points_after": points_after}),
                         now, row["id"]),
                    ).rowcount
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
            if not changed:
                continue
            if target == "held":
                _queue_held_job(row["job_id"], now, db_path)
        except Exception:
            # Local row races must not prevent other durable pending holds from
            # replaying. Provider idempotency makes this row safe to retry.
            continue
        recovered += 1
    return recovered


def precharge_and_create_job(
    owner: str,
    payload: dict[str, Any],
    quote_id: str,
    idempotency_key: str,
    now: int,
    *,
    points_client: Any = points,
    uuid_factory: Callable[[], Any] = uuid.uuid4,
    db_path: str | None = None,
    material_bindings: list[dict[str, Any]] | None = None,
    predecessor_job_id: str | None = None,
) -> dict[str, Any]:
    draft = payload.get("draft") if isinstance(payload, dict) else None
    with closing(store.open_store(store._db_path(db_path))) as conn:
        existing = conn.execute(
            "SELECT id FROM edit_v2_jobs WHERE owner=? AND idempotency_key=?",
            (owner, idempotency_key),
        ).fetchone()
    quote = None
    if existing is None:
        quote = validate_quote(owner, draft, quote_id, now, db_path=db_path)
    try:
        job = store.create_job(
            owner,
            payload,
            quote_id,
            idempotency_key,
            now,
            uuid_factory=uuid_factory,
            db_path=db_path,
            material_bindings=material_bindings,
            predecessor_job_id=predecessor_job_id,
        )
    except ValueError as exc:
        if str(exc) == "idempotency_conflict":
            raise BillingError("idempotency_conflict") from exc
        raise
    if quote is None:
        with closing(store.open_store(store._db_path(db_path))) as conn:
            quote_row = conn.execute(
                "SELECT * FROM edit_v2_quotes WHERE id=? AND owner=?",
                (quote_id, owner),
            ).fetchone()
        if quote_row is None:
            raise BillingError("quote_not_found")
        quote = _quote_public(quote_row)
    transaction_key = f"ai-edit-v2:{job['id']}:hold"
    with closing(store.open_store(store._db_path(db_path))) as conn:
        conn.execute(
            """INSERT OR IGNORE INTO edit_v2_billing(
                   job_id,transaction_key,operation,amount,status,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?)""",
            (job["id"], transaction_key, "hold", quote["max_points"], "pending", now, now),
        )
    bill = _billing_row(job["id"], db_path)
    if bill["status"] == "rejected":
        _raise_rejected_precharge(bill)
    if bill["status"] == "pending":
        try:
            points_after = points_client.deduct_points(
                owner,
                int(bill["amount"]),
                "ai-edit-v2 maximum precharge",
                transaction_key=transaction_key,
            )
        except Exception as exc:
            if _is_definitive_points_rejection(exc):
                _reject_precharge(bill, exc, now, db_path)
                raise
            raise PrechargePending(job["id"], int(bill["amount"])) from exc
        with closing(store.open_store(store._db_path(db_path))) as conn:
            conn.execute(
                """UPDATE edit_v2_billing SET status='held',response_json=?,updated_at=?
                   WHERE id=? AND status='pending'""",
                (_canonical({"points_after": points_after}), now, bill["id"]),
            )
    job = _queue_held_job(job["id"], now, db_path)
    return {"job": job, "quote": quote, "held_points": int(bill["amount"])}


def settle_success(
    job_id: str,
    actual_points: int,
    now: int,
    *,
    points_client: Any = points,
    db_path: str | None = None,
    finalize: Callable[[sqlite3.Connection, dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    initial = _billing_row(job_id, db_path)
    if initial is None:
        raise BillingError("billing_not_found")
    if initial["status"] == "settled":
        return json.loads(initial["response_json"] or "{}")
    held = int(initial["amount"])
    try:
        actual = int(actual_points)
    except (TypeError, ValueError) as exc:
        raise BillingError("actual_points_invalid") from exc
    if actual < 0 or actual > held:
        raise BillingError("actual_points_invalid")
    bill = _claim_terminal_operation(job_id, "settling", now, db_path)
    if bill["status"] == "settled":
        return json.loads(bill["response_json"] or "{}")
    difference = held - actual
    transaction_key = f"ai-edit-v2:{job_id}:settlement"
    intent = {
        "operation": "settlement",
        "transaction_key": transaction_key,
        "held_points": held,
        "actual_points": actual,
        "refunded_points": difference,
        "provider_operation_status": "pending",
    }
    try:
        existing_intent = json.loads(bill["response_json"] or "{}")
    except (TypeError, ValueError):
        existing_intent = {}
    if existing_intent.get("operation") == "settlement":
        if (
            int(existing_intent.get("actual_points", -1)) != actual
            or existing_intent.get("transaction_key") != transaction_key
        ):
            raise BillingError("settlement_intent_conflict")
        intent = existing_intent
    else:
        with closing(store.open_store(store._db_path(db_path))) as conn:
            conn.execute(
                """UPDATE edit_v2_billing SET response_json=?,updated_at=?
                   WHERE id=? AND status='settling'""",
                (_canonical(intent), now, bill["id"]),
            )
    points_after = None
    if difference:
        points_after = points_client.refund_points(
            _job_owner(job_id, db_path),
            difference,
            "ai-edit-v2 settlement difference",
            transaction_key=transaction_key,
        )
    response = {"held_points": held, "actual_points": actual, "refunded_points": difference, "points_after": points_after}
    with closing(store.open_store(store._db_path(db_path))) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            current = conn.execute("SELECT * FROM edit_v2_billing WHERE id=?", (bill["id"],)).fetchone()
            if current["status"] == "settled":
                conn.commit()
                return json.loads(current["response_json"] or "{}")
            if current["status"] != "settling":
                raise BillingError("billing_operation_conflict")
            if finalize is not None:
                finalize(conn, response)
            conn.execute(
                """UPDATE edit_v2_billing SET status='settled',response_json=?,updated_at=?
                   WHERE id=? AND status='settling'""",
                (_canonical(response), now, bill["id"]),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return response


def reconcile_quarantined_settlement(
    job_id: str,
    now: int,
    *,
    points_client: Any = points,
    db_path: str | None = None,
) -> dict[str, Any]:
    """Recover an exact settlement, then refund only its remaining charge.

    The hold row stays ``settling`` until both idempotent provider operations are
    known. An absent/invalid intent is deliberately left untouched: guessing the
    charged amount could over-refund an already-applied settlement difference.
    """
    bill = _billing_row(job_id, db_path)
    if bill is None:
        raise BillingError("billing_not_found")
    if bill["status"] == "refunded":
        return json.loads(bill["response_json"] or "{}")
    if bill["status"] not in {"settling", "settled"}:
        raise BillingError("billing_not_settling")
    try:
        intent = json.loads(bill["response_json"] or "{}")
        held = int(intent["held_points"])
        actual = int(intent["actual_points"])
        difference = int(intent["refunded_points"])
    except (KeyError, TypeError, ValueError) as exc:
        raise BillingError("settlement_intent_missing") from exc
    settlement_key = f"ai-edit-v2:{job_id}:settlement"
    if (
        held != int(bill["amount"])
        or not 0 <= actual <= held
        or difference != held - actual
    ):
        raise BillingError("settlement_intent_invalid")

    phase = intent.get("provider_operation_status")
    if bill["status"] == "settling" and (
        intent.get("operation") != "settlement"
        or intent.get("transaction_key") != settlement_key
    ):
        raise BillingError("settlement_intent_invalid")
    if bill["status"] == "settling" and phase != "settlement_recovered":
        settlement_points_after = None
        if difference:
            settlement_points_after = points_client.refund_points(
                _job_owner(job_id, db_path),
                difference,
                "ai-edit-v2 settlement difference",
                transaction_key=settlement_key,
            )
        intent["provider_operation_status"] = "settlement_recovered"
        intent["settlement_points_after"] = settlement_points_after
        with closing(store.open_store(store._db_path(db_path))) as conn:
            conn.execute(
                """UPDATE edit_v2_billing SET response_json=?,updated_at=?
                   WHERE id=? AND status='settling'""",
                (_canonical(intent), now, bill["id"]),
            )

    points_after = intent.get("settlement_points_after")
    if actual:
        points_after = points_client.refund_points(
            _job_owner(job_id, db_path),
            actual,
            "ai-edit-v2 quarantined successor compensation",
            transaction_key=f"ai-edit-v2:{job_id}:failure-refund",
        )
    response = {
        "held_points": held,
        "actual_points": 0,
        "refunded_points": held,
        "points_after": points_after,
        "settlement_actual_points": actual,
        "settlement_refunded_points": difference,
    }
    with closing(store.open_store(store._db_path(db_path))) as conn:
        conn.execute(
            """UPDATE edit_v2_billing SET status='refunded',response_json=?,updated_at=?
               WHERE id=? AND status IN ('settling','settled')""",
            (_canonical(response), now, bill["id"]),
        )
    return response


def _claim_terminal_operation(
    job_id: str, target: str, now: int, db_path: str | None
) -> Any:
    final = "settled" if target == "settling" else "refunded"
    other = {"settling", "settled", "refunding", "refunded"} - {target, final}
    with closing(store.open_store(store._db_path(db_path))) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT * FROM edit_v2_billing WHERE job_id=? AND operation='hold'",
                (job_id,),
            ).fetchone()
            if row is None:
                raise BillingError("billing_not_found")
            if row["status"] == final:
                conn.commit()
                return row
            if row["status"] in other or (
                target == "settling" and row["status"] == "refund_pending"
            ):
                raise BillingError("billing_operation_conflict")
            if row["status"] == target:
                if int(row["updated_at"]) >= int(now):
                    raise BillingError("billing_operation_in_progress")
            elif row["status"] != "held" and not (
                target == "refunding" and row["status"] == "refund_pending"
            ):
                raise BillingError("billing_not_held")
            conn.execute(
                "UPDATE edit_v2_billing SET status=?,updated_at=? WHERE id=?",
                (target, int(now), row["id"]),
            )
            row = conn.execute("SELECT * FROM edit_v2_billing WHERE id=?", (row["id"],)).fetchone()
            conn.commit()
            return row
        except Exception:
            conn.rollback()
            raise


def _job_owner(job_id: str, db_path: str | None = None) -> str:
    with closing(store.open_store(store._db_path(db_path))) as conn:
        row = conn.execute("SELECT owner FROM edit_v2_jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise BillingError("job_not_found")
    return row["owner"]


def refund_failure(
    job_id: str,
    now: int,
    *,
    points_client: Any = points,
    db_path: str | None = None,
) -> dict[str, Any]:
    bill = _claim_terminal_operation(job_id, "refunding", now, db_path)
    if bill["status"] == "refunded":
        return json.loads(bill["response_json"] or "{}")
    held = int(bill["amount"])
    points_after = points_client.refund_points(
        _job_owner(job_id, db_path),
        held,
        "ai-edit-v2 failure full refund",
        transaction_key=f"ai-edit-v2:{job_id}:failure-refund",
    )
    response = {"held_points": held, "refunded_points": held, "points_after": points_after}
    with closing(store.open_store(store._db_path(db_path))) as conn:
        conn.execute(
            """UPDATE edit_v2_billing SET status='refunded',response_json=?,updated_at=?
               WHERE id=? AND status='refunding'""",
            (_canonical(response), now, bill["id"]),
        )
    return response
