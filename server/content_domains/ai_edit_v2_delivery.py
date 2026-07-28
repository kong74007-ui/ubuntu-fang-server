"""Lease-fenced, restart-safe delivery into private COS and user assets."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import time
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
          worker_id: str | None = None, lease_seconds: int = 180) -> None:
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
    billing.refund_failure(job_id, now, points_client=billing.points, db_path=db_path)


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
        existing = conn.execute(
            "SELECT * FROM edit_v2_delivery_intents WHERE job_id=?", (job["id"],)
        ).fetchone()
    if existing is not None:
        return dict(existing)
    size, digest = _file_identity(output_path)
    quality_json = _strict_json(report.as_dict())
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
                       source_sha256,quality_json,actual_cost,status,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?, 'prepared',?,?)""",
                (job["id"], job["owner"], f"ai-edit-v2:{job['id']}:delivery",
                 key, size, digest, quality_json, actual_cost, now, now),
            )
            row = conn.execute(
                "SELECT * FROM edit_v2_delivery_intents WHERE job_id=?", (job["id"],)
            ).fetchone()
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    if (
        row["owner"] != job["owner"] or row["cos_key"] != key
        or int(row["actual_cost"]) != actual_cost or row["quality_json"] != quality_json
    ):
        raise DeliveryError("delivery_intent_conflict")
    return dict(row)


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
        row = conn.execute("SELECT id,username FROM video_assets WHERE job_id=?", (payload["job_id"],)).fetchone()
        if row is not None:
            if row["username"] != payload["username"]:
                raise DeliveryError("asset_owner_conflict")
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
                        now_fn: Callable[[], int]) -> dict[str, Any]:
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
        job["id"], int(intent["actual_cost"]), now, points_client=billing.points,
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
            cos_api: Any = None, asset_db_path: str | None = None) -> dict[str, Any]:
    """Persist intent before upload; reconcile HEAD/settlement/outbox on replay."""
    if not isinstance(report, QualityReport) or not report.passed:
        raise DeliveryError("quality_not_passed")
    if isinstance(actual_cost, bool) or not isinstance(actual_cost, int) or actual_cost < 0:
        raise DeliveryError("actual_cost_invalid")
    clock = now_fn or (lambda: int(time.time()))
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
        _fail(job_id, "storage_verification_failed", int(clock()), db_path, worker_id, lease_seconds)
        raise
    except Exception as exc:
        _fail(job_id, "storage_upload_failed", int(clock()), db_path, worker_id, lease_seconds)
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
        _settle_and_enqueue(_load(job_id, db_path), intent, int(clock()), db_path, worker_id, clock)
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
