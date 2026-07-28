"""Lease-fenced, restart-safe delivery into private COS and user assets."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import time
import uuid
from contextlib import closing
from typing import Any, Callable

from . import ai_edit_v2_billing as billing
from . import ai_edit_v2_cos as cos
from . import ai_edit_v2_store as store
from .ai_edit_v2_quality import QualityReport


class DeliveryError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _strict_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


def _quality_report_from_intent(intent: dict[str, Any]) -> QualityReport:
    try:
        value = json.loads(intent["quality_json"], parse_constant=_reject_constant)
        if not isinstance(value, dict) or set(value) != {
            "passed", "error_codes", "failing_layers", "repairable", "terminal"
        }:
            raise ValueError("quality report shape invalid")
        if value["passed"] is not True or value["repairable"] is not False or value["terminal"] is not False:
            raise ValueError("quality report is not a durable pass")
        if not isinstance(value["error_codes"], list) or value["error_codes"]:
            raise ValueError("quality errors invalid")
        if not isinstance(value["failing_layers"], list) or value["failing_layers"]:
            raise ValueError("quality layers invalid")
        report = QualityReport(True, (), (), False, False)
        if _strict_json(report.as_dict()) != intent["quality_json"]:
            raise ValueError("quality report not canonical")
        return report
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise DeliveryError("delivery_quality_report_invalid") from exc


_INTENT_STATUSES = frozenset({"prepared", "uploaded", "asset_pending", "completed"})


def _intent_digest(value: dict[str, Any]) -> str:
    canonical = {
        key: value[key] for key in (
            "job_id", "owner", "idempotency_key", "cos_key",
            "source_size_bytes", "source_sha256", "actual_cost", "quality_json",
        )
    }
    return hashlib.sha256(_strict_json(canonical).encode("utf-8")).hexdigest()


def load_validated_intent(job_id: str, *, db_path: str | None = None) -> dict[str, Any]:
    """Load an intent only when every immutable field is canonical for its job."""
    with closing(store.open_store(store._db_path(db_path))) as conn:
        row = conn.execute(
            """SELECT d.*,j.owner AS job_owner,b.amount AS held_points
               FROM edit_v2_delivery_intents d
               JOIN edit_v2_jobs j ON j.id=d.job_id
               JOIN edit_v2_billing b ON b.job_id=j.id AND b.operation='hold'
               WHERE d.job_id=?""", (job_id,),
        ).fetchone()
    if row is None:
        raise DeliveryError("delivery_intent_missing")
    intent = dict(row)
    owner_hash = hashlib.sha256(intent["job_owner"].encode("utf-8")).hexdigest()[:16]
    expected_key = f"ai-edit-v2/{owner_hash}/{job_id}/delivery/final.mp4"
    try:
        _quality_report_from_intent(intent)
        valid = (
            intent["job_id"] == job_id
            and intent["owner"] == intent["job_owner"]
            and intent["idempotency_key"] == f"ai-edit-v2:{job_id}:delivery"
            and intent["cos_key"] == expected_key
            and isinstance(intent["source_size_bytes"], int)
            and not isinstance(intent["source_size_bytes"], bool)
            and intent["source_size_bytes"] > 0
            and isinstance(intent["source_sha256"], str)
            and len(intent["source_sha256"]) == 64
            and all(char in "0123456789abcdef" for char in intent["source_sha256"])
            and isinstance(intent["actual_cost"], int)
            and not isinstance(intent["actual_cost"], bool)
            and 0 <= intent["actual_cost"] <= int(intent["held_points"])
            and intent["status"] in _INTENT_STATUSES
            and isinstance(intent.get("canonical_digest"), str)
            and intent["canonical_digest"] == _intent_digest(intent)
        )
    except (KeyError, TypeError, ValueError, DeliveryError):
        valid = False
    if not valid:
        raise DeliveryError("delivery_intent_conflict")
    intent.pop("job_owner", None)
    intent.pop("held_points", None)
    return intent


def _load(job_id: str, db_path: str | None) -> dict[str, Any]:
    with closing(store.open_store(store._db_path(db_path))) as conn:
        row = conn.execute("SELECT * FROM edit_v2_jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise DeliveryError("job_not_found")
    return dict(row)


def _assert_lease(job_id: str, worker_id: str | None, now: int,
                  db_path: str | None) -> None:
    if worker_id and not store.lease_owned(job_id, worker_id, now, db_path=db_path):
        raise DeliveryError("delivery_lease_lost")


def _result(job_id: str, db_path: str | None) -> dict[str, Any]:
    with closing(store.open_store(store._db_path(db_path))) as conn:
        job = conn.execute("SELECT * FROM edit_v2_jobs WHERE id=?", (job_id,)).fetchone()
        outbox = conn.execute(
            "SELECT * FROM edit_v2_delivery_outbox WHERE job_id=?", (job_id,)
        ).fetchone()
        bill = conn.execute(
            "SELECT response_json FROM edit_v2_billing WHERE job_id=? AND operation='hold'",
            (job_id,),
        ).fetchone()
    if job is None or job["status"] != "completed" or outbox is None or outbox["status"] != "delivered":
        raise DeliveryError("delivery_incomplete")
    return {
        "job_id": job_id, "state": "completed", "cos_key": job["output_cos_key"],
        "asset_id": int(outbox["asset_id"]),
        "settlement": json.loads(bill["response_json"] or "{}"),
    }


def _fail(job_id: str, code: str, now: int, db_path: str | None,
          worker_id: str | None = None, lease_seconds: int = 180,
          points_client: Any = None) -> None:
    job = _load(job_id, db_path)
    if job["status"] == "storage_failed":
        raise DeliveryError("storage_failed")
    if job["status"] == "completed":
        return
    changed = store.transition_leased(
        job_id, job["status"], "storage_failed", {"error_code": code}, now,
        worker_id=worker_id, lease_seconds=lease_seconds, db_path=db_path,
    ) if worker_id else store.transition(
        job_id, job["status"], "storage_failed", {"error_code": code}, now,
        db_path=db_path,
    )
    if not changed:
        raise DeliveryError("delivery_state_conflict")
    billing.refund_failure(job_id, now, points_client=points_client or billing.points, db_path=db_path)


def _file_identity(path: str) -> tuple[int, str]:
    size = os.path.getsize(path)
    if size <= 0:
        raise OSError("empty output")
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return size, digest.hexdigest()


def _prepare_intent(job: dict[str, Any], output_path: str, report: QualityReport,
                    actual_cost: int, now: int, db_path: str | None,
                    worker_id: str | None) -> dict[str, Any]:
    owner_hash = hashlib.sha256(job["owner"].encode("utf-8")).hexdigest()[:16]
    key = f"ai-edit-v2/{owner_hash}/{job['id']}/delivery/final.mp4"
    with closing(store.open_store(store._db_path(db_path))) as conn:
        exists = conn.execute(
            "SELECT 1 FROM edit_v2_delivery_intents WHERE job_id=?", (job["id"],)
        ).fetchone() is not None
    size, digest = _file_identity(output_path) if output_path else (None, None)
    quality_json = _strict_json(report.as_dict())
    if exists:
        intent = load_validated_intent(job["id"], db_path=db_path)
        if (
            intent["owner"] != job["owner"] or int(intent["actual_cost"]) != actual_cost
            or intent["quality_json"] != quality_json
            or (size is not None and (
                int(intent["source_size_bytes"]) != size
                or intent["source_sha256"] != digest
            ))
        ):
            raise DeliveryError("delivery_intent_conflict")
        return intent
    if size is None or digest is None:
        raise DeliveryError("delivery_intent_missing")
    candidate = {
        "job_id": job["id"], "owner": job["owner"],
        "idempotency_key": f"ai-edit-v2:{job['id']}:delivery", "cos_key": key,
        "source_size_bytes": size, "source_sha256": digest,
        "quality_json": quality_json, "actual_cost": actual_cost,
    }
    canonical_digest = _intent_digest(candidate)
    with closing(store.open_store(store._db_path(db_path))) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            if worker_id and conn.execute(
                "SELECT 1 FROM edit_v2_jobs WHERE id=? AND lease_owner=? AND lease_until>?",
                (job["id"], worker_id, now),
            ).fetchone() is None:
                raise DeliveryError("delivery_lease_lost")
            conn.execute(
                """INSERT OR IGNORE INTO edit_v2_delivery_intents(
                       job_id,owner,idempotency_key,cos_key,source_size_bytes,
                       source_sha256,quality_json,actual_cost,canonical_digest,
                       status,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?, 'prepared',?,?)""",
                (job["id"], job["owner"], f"ai-edit-v2:{job['id']}:delivery",
                 key, size, digest, quality_json, actual_cost, canonical_digest, now, now),
            )
            row = conn.execute(
                "SELECT * FROM edit_v2_delivery_intents WHERE job_id=?", (job["id"],)
            ).fetchone()
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    return load_validated_intent(job["id"], db_path=db_path)


def _verified_head(intent: dict[str, Any], cos_api: Any) -> dict[str, Any] | None:
    try:
        metadata = cos_api.head_object(intent["cos_key"])
    except Exception:
        return None
    if (
        int(metadata.get("content_length") or -1) != int(intent["source_size_bytes"])
        or metadata.get("content_type") != "video/mp4" or not metadata.get("etag")
    ):
        return None
    return metadata


def _mark_uploaded(job_id: str, metadata: dict[str, Any], now: int,
                   db_path: str | None, worker_id: str | None) -> None:
    with closing(store.open_store(store._db_path(db_path))) as conn:
        query = """UPDATE edit_v2_delivery_intents SET status='uploaded',etag=?,updated_at=?
                   WHERE job_id=?"""
        params: list[Any] = [str(metadata["etag"]).strip('"'), now, job_id]
        if worker_id:
            query += " AND EXISTS(SELECT 1 FROM edit_v2_jobs j WHERE j.id=? AND j.lease_owner=? AND j.lease_until>?)"
            params.extend([job_id, worker_id, now])
        if conn.execute(query, params).rowcount != 1:
            raise DeliveryError("delivery_lease_lost" if worker_id else "delivery_state_conflict")


def _asset_db_path(path: str | None = None) -> str:
    if path:
        return path
    return os.environ.get("AI_EDIT_V2_ASSET_DB") or os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "audio_assets.db"
    )


def _write_user_asset(payload: dict[str, Any], asset_db_path: str | None) -> int:
    conn = sqlite3.connect(_asset_db_path(asset_db_path), timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """SELECT id,job_id,username,mode,video_file,ratio,status
               FROM video_assets WHERE job_id=?""", (payload["job_id"],)
        ).fetchone()
        if row is not None:
            expected = {
                "job_id": payload["job_id"], "username": payload["username"],
                "mode": "ai_edit_v2", "video_file": payload["video_file"],
                "ratio": payload["ratio"], "status": "done",
            }
            if any(str(row[key]) != str(value) for key, value in expected.items()):
                raise DeliveryError("asset_idempotency_conflict")
            conn.commit()
            return int(row["id"])
        cursor = conn.execute(
            """INSERT INTO video_assets(
                   job_id,username,mode,video_file,video_url,resolution,ratio,
                   phase,status,created_at,updated_at
               ) VALUES(?,?,?, ?,NULL,?,?, 'completed','done',?,?)""",
            (payload["job_id"], payload["username"], "ai_edit_v2",
             payload["video_file"], "1080p", payload["ratio"],
             payload["created_at"], payload["created_at"]),
        )
        conn.commit()
        return int(cursor.lastrowid)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _settle_and_enqueue(job: dict[str, Any], intent: dict[str, Any], now: int,
                        db_path: str | None, worker_id: str | None,
                        now_fn: Callable[[], int], points_client: Any) -> dict[str, Any]:
    try:
        payload = json.loads(job.get("payload_json") or "{}")
    except (TypeError, ValueError):
        payload = {}
    draft = payload.get("draft") if isinstance(payload, dict) else {}
    ratio = draft.get("aspect_ratio") if isinstance(draft, dict) else None
    if ratio not in {"16:9", "9:16"}:
        ratio = "16:9"
    asset_payload = {
        "job_id": job["id"], "username": job["owner"],
        "video_file": intent["cos_key"], "ratio": ratio, "created_at": now,
    }

    def finalize(conn: sqlite3.Connection, _settlement: dict[str, Any]) -> None:
        lease_now = int(now_fn())
        if worker_id and conn.execute(
            "SELECT 1 FROM edit_v2_jobs WHERE id=? AND lease_owner=? AND lease_until>? AND status='settling'",
            (job["id"], worker_id, lease_now),
        ).fetchone() is None:
            raise DeliveryError("delivery_lease_lost")
        conn.execute(
            """INSERT OR IGNORE INTO edit_v2_render_artifacts(
                   job_id,kind,version,cos_key,validation_json,cleanup_status,created_at
               ) VALUES(?,'delivery_internal',1,?,?,'retained',?)""",
            (job["id"], intent["cos_key"], intent["quality_json"], now),
        )
        conn.execute(
            """INSERT OR IGNORE INTO edit_v2_delivery_outbox(
                   job_id,owner,payload_json,status,created_at,updated_at
               ) VALUES(?,?,?,'pending',?,?)""",
            (job["id"], job["owner"], _strict_json(asset_payload), now, now),
        )
        conn.execute(
            "UPDATE edit_v2_delivery_intents SET status='asset_pending',updated_at=? WHERE job_id=?",
            (now, job["id"]),
        )

    return billing.settle_success(
        job["id"], int(intent["actual_cost"]), now, points_client=points_client,
        db_path=db_path, finalize=finalize,
    )


def _dispatch_and_complete(job_id: str, now: int, db_path: str | None,
                           asset_db_path: str | None, worker_id: str | None,
                           now_fn: Callable[[], int]) -> None:
    _assert_lease(job_id, worker_id, now, db_path)
    with closing(store.open_store(store._db_path(db_path))) as conn:
        row = conn.execute("SELECT * FROM edit_v2_delivery_outbox WHERE job_id=?", (job_id,)).fetchone()
    if row is None:
        raise DeliveryError("delivery_outbox_missing")
    payload = json.loads(row["payload_json"])
    asset_id = int(row["asset_id"]) if row["asset_id"] is not None else _write_user_asset(payload, asset_db_path)
    now = int(now_fn())
    _assert_lease(job_id, worker_id, now, db_path)
    with closing(store.open_store(store._db_path(db_path))) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            current = conn.execute(
                "SELECT status,checkpoint_json FROM edit_v2_jobs WHERE id=?", (job_id,)
            ).fetchone()
            if worker_id and conn.execute(
                "SELECT 1 FROM edit_v2_jobs WHERE id=? AND lease_owner=? AND lease_until>?",
                (job_id, worker_id, now),
            ).fetchone() is None:
                raise DeliveryError("delivery_lease_lost")
            conn.execute(
                "UPDATE edit_v2_delivery_outbox SET status='delivered',asset_id=?,updated_at=? WHERE job_id=?",
                (asset_id, now, job_id),
            )
            conn.execute(
                "UPDATE edit_v2_delivery_intents SET status='completed',updated_at=? WHERE job_id=?",
                (now, job_id),
            )
            if current["status"] != "completed":
                if current["status"] != "settling":
                    raise DeliveryError("delivery_state_conflict")
                checkpoints = json.loads(current["checkpoint_json"] or "[]")
                checkpoints.append({"version": len(checkpoints) + 1, "state": "completed", "at": now,
                                    "data": {"asset_id": asset_id}})
                query = """UPDATE edit_v2_jobs SET status='completed',output_cos_key=(
                               SELECT cos_key FROM edit_v2_delivery_intents WHERE job_id=?),
                               checkpoint_json=?,error_code=NULL,lease_owner=NULL,lease_until=NULL,updated_at=?
                           WHERE id=? AND status='settling'"""
                params: list[Any] = [job_id, _strict_json(checkpoints), now, job_id]
                if worker_id:
                    query += " AND lease_owner=? AND lease_until>?"
                    params.extend([worker_id, now])
                if conn.execute(query, params).rowcount != 1:
                    raise DeliveryError("delivery_lease_lost" if worker_id else "delivery_state_conflict")
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def deliver(job_id: str, output_path: str, report: QualityReport, actual_cost: int,
            db_path: str | None = None, *, worker_id: str | None = None,
            lease_seconds: int = 180, now_fn: Callable[[], int] | None = None,
            cos_api: Any = None, asset_db_path: str | None = None,
            points_client: Any = None) -> dict[str, Any]:
    """Persist intent before upload; reconcile HEAD/settlement/outbox on replay."""
    if not isinstance(report, QualityReport) or not report.passed:
        raise DeliveryError("quality_not_passed")
    if isinstance(actual_cost, bool) or not isinstance(actual_cost, int) or actual_cost < 0:
        raise DeliveryError("actual_cost_invalid")
    clock = now_fn or (lambda: int(time.time()))
    points_api = points_client or billing.points
    now = int(clock())
    job = _load(job_id, db_path)
    if job["status"] == "completed":
        return _result(job_id, db_path)
    if job["status"] == "storage_failed":
        raise DeliveryError("storage_failed")
    if job["status"] not in {"quality_check", "settling"}:
        raise DeliveryError("delivery_state_conflict")
    _assert_lease(job_id, worker_id, now, db_path)
    intent = _prepare_intent(job, output_path, report, actual_cost, now, db_path, worker_id)
    cos_client = cos_api or cos
    try:
        metadata = _verified_head(intent, cos_client)
        if metadata is None:
            _assert_lease(job_id, worker_id, int(clock()), db_path)
            upload = cos_client.put_file(output_path, intent["cos_key"], "video/mp4", private=True) or {}
            _assert_lease(job_id, worker_id, int(clock()), db_path)
            metadata = _verified_head(intent, cos_client)
            upload_etag = str(upload.get("ETag") or upload.get("etag") or "").strip('"')
            if metadata is None or (upload_etag and str(metadata["etag"]).strip('"') != upload_etag):
                raise DeliveryError("storage_verification_failed")
        _mark_uploaded(job_id, metadata, int(clock()), db_path, worker_id)
    except DeliveryError as exc:
        if exc.code == "delivery_lease_lost":
            raise
        _fail(job_id, "storage_verification_failed", int(clock()), db_path, worker_id, lease_seconds, points_api)
        raise
    except Exception as exc:
        _fail(job_id, "storage_upload_failed", int(clock()), db_path, worker_id, lease_seconds, points_api)
        raise DeliveryError("storage_upload_failed") from exc

    now = int(clock())
    _assert_lease(job_id, worker_id, now, db_path)
    current = _load(job_id, db_path)
    if current["status"] == "quality_check":
        changed = store.transition_leased(
            job_id, "quality_check", "settling",
            {"quality_report": report.as_dict(), "verified_cos_key": intent["cos_key"]}, now,
            worker_id=worker_id, lease_seconds=lease_seconds, db_path=db_path,
        ) if worker_id else store.transition(
            job_id, "quality_check", "settling",
            {"quality_report": report.as_dict(), "verified_cos_key": intent["cos_key"]}, now,
            db_path=db_path,
        )
        if not changed:
            raise DeliveryError("delivery_state_conflict")
    try:
        _assert_lease(job_id, worker_id, int(clock()), db_path)
        _settle_and_enqueue(_load(job_id, db_path), intent, int(clock()), db_path, worker_id, clock, points_api)
    except billing.BillingError as exc:
        if exc.code == "billing_operation_in_progress":
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                current = _load(job_id, db_path)
                if current["status"] == "completed":
                    return _result(job_id, db_path)
                with closing(store.open_store(store._db_path(db_path))) as conn:
                    bill = conn.execute(
                        "SELECT status FROM edit_v2_billing WHERE job_id=? AND operation='hold'",
                        (job_id,),
                    ).fetchone()
                if bill is not None and bill["status"] == "settled":
                    _dispatch_and_complete(job_id, int(clock()), db_path, asset_db_path, worker_id, clock)
                    return _result(job_id, db_path)
                time.sleep(0.01)
            raise DeliveryError("delivery_in_progress") from exc
        raise
    _dispatch_and_complete(job_id, int(clock()), db_path, asset_db_path, worker_id, clock)
    return _result(job_id, db_path)


def resume_delivery(job_id: str, *, db_path: str | None = None,
                    worker_id: str | None = None, lease_seconds: int = 180,
                    now_fn: Callable[[], int] | None = None, cos_api: Any = None,
                    asset_db_path: str | None = None,
                    points_client: Any = None) -> dict[str, Any]:
    """Resume settling solely from the persisted, canonical delivery intent."""
    intent = load_validated_intent(job_id, db_path=db_path)
    report = _quality_report_from_intent(intent)
    return deliver(
        job_id, "", report, int(intent["actual_cost"]), db_path=db_path,
        worker_id=worker_id, lease_seconds=lease_seconds, now_fn=now_fn,
        cos_api=cos_api, asset_db_path=asset_db_path, points_client=points_client,
    )


def _outbox_due(job_id: str, now: int, db_path: str | None) -> bool:
    with closing(store.open_store(store._db_path(db_path))) as conn:
        row = conn.execute(
            "SELECT status,retry_at,dead_letter_at FROM edit_v2_delivery_outbox WHERE job_id=?",
            (job_id,),
        ).fetchone()
    return row is None or (
        row["status"] == "pending" and row["dead_letter_at"] is None
        and (row["retry_at"] is None or int(row["retry_at"]) <= now)
    )


def _record_outbox_failure(job_id: str, exc: Exception, lease_owner: str, now: int,
                           db_path: str | None) -> None:
    code = getattr(exc, "code", None) or "delivery_reconcile_failed"
    with closing(store.open_store(store._db_path(db_path))) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                """SELECT o.attempt_count
                   FROM edit_v2_delivery_outbox o
                   JOIN edit_v2_jobs j ON j.id=o.job_id
                   WHERE o.job_id=? AND o.status='pending'
                     AND j.lease_owner=? AND j.lease_until>?""",
                (job_id, lease_owner, now),
            ).fetchone()
            if row is not None:
                attempts = int(row["attempt_count"] or 0) + 1
                dead_at = now if attempts >= 3 else None
                retry_at = None if dead_at is not None else now + min(3600, 2 ** attempts)
                conn.execute(
                    """UPDATE edit_v2_delivery_outbox
                       SET status=?,attempt_count=?,error_code=?,retry_at=?,dead_letter_at=?,updated_at=?
                       WHERE job_id=? AND status='pending' AND attempt_count=?
                         AND EXISTS (
                             SELECT 1 FROM edit_v2_jobs j
                             WHERE j.id=edit_v2_delivery_outbox.job_id
                               AND j.lease_owner=? AND j.lease_until>?
                         )""",
                    ("dead_letter" if dead_at is not None else "pending", attempts,
                     str(code), retry_at, dead_at, now, job_id,
                     attempts - 1, lease_owner, now),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def reconcile_pending_deliveries(now: int | None = None, *, db_path: str | None = None,
                                 lease_seconds: int = 180, cos_api: Any = None,
                                 asset_db_path: str | None = None,
                                 worker_id: str | None = None,
                                 points_client: Any = None) -> int:
    """Claim and resume every expired/unleased settling job with a durable intent."""
    now = int(time.time()) if now is None else int(now)
    with closing(store.open_store(store._db_path(db_path))) as conn:
        rows = conn.execute(
            """SELECT j.id FROM edit_v2_jobs j
               JOIN edit_v2_delivery_intents d ON d.job_id=j.id
               LEFT JOIN edit_v2_delivery_outbox o ON o.job_id=j.id
               WHERE j.status='settling' AND (j.lease_until IS NULL OR j.lease_until<=?)
                 AND (o.id IS NULL OR (
                     o.status='pending' AND o.dead_letter_at IS NULL
                     AND (o.retry_at IS NULL OR o.retry_at<=?)
                 ))
               ORDER BY j.created_at,j.id""", (now, now),
        ).fetchall()
    completed = 0
    for row in rows:
        owner = worker_id or f"delivery-reconcile:{uuid.uuid4().hex}"
        claimed = store.claim_job(
            row["id"], owner, lease_seconds, now, db_path=db_path,
        )
        if claimed is None:
            continue
        if not _outbox_due(row["id"], now, db_path):
            store.release_job_lease(row["id"], owner, db_path=db_path)
            continue
        try:
            result = resume_delivery(
                row["id"], db_path=db_path, worker_id=owner,
                lease_seconds=lease_seconds, now_fn=lambda: now,
                cos_api=cos_api, asset_db_path=asset_db_path,
                points_client=points_client,
            )
            if result.get("state") == "completed":
                completed += 1
        except Exception as exc:
            try:
                _record_outbox_failure(row["id"], exc, owner, now, db_path)
            except Exception:
                pass
            try:
                store.release_job_lease(row["id"], owner, db_path=db_path)
            except Exception:
                pass
            continue
    return completed
