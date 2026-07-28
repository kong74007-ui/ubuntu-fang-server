"""Verified private-COS delivery and atomic AI Edit V2 completion."""

from __future__ import annotations

import hashlib
import json
import os
import time
from contextlib import closing
from typing import Any

from . import ai_edit_v2_billing as billing
from . import ai_edit_v2_cos as cos
from . import ai_edit_v2_store as store
from .ai_edit_v2_quality import QualityReport


class DeliveryError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _load(job_id: str, db_path: str | None) -> dict[str, Any]:
    with closing(store.open_store(store._db_path(db_path))) as conn:
        row = conn.execute("SELECT * FROM edit_v2_jobs WHERE id=?", (job_id,)).fetchone()
    if row is None:
        raise DeliveryError("job_not_found")
    return dict(row)


def _result(job_id: str, db_path: str | None) -> dict[str, Any]:
    with closing(store.open_store(store._db_path(db_path))) as conn:
        job = conn.execute("SELECT * FROM edit_v2_jobs WHERE id=?", (job_id,)).fetchone()
        asset = conn.execute(
            "SELECT * FROM edit_v2_render_artifacts WHERE job_id=? AND kind='delivery' AND version=1",
            (job_id,),
        ).fetchone()
        bill = conn.execute(
            "SELECT response_json FROM edit_v2_billing WHERE job_id=? AND operation='hold'",
            (job_id,),
        ).fetchone()
    if job is None or job["status"] != "completed" or asset is None:
        raise DeliveryError("delivery_incomplete")
    return {
        "job_id": job_id,
        "state": "completed",
        "cos_key": job["output_cos_key"],
        "asset_id": int(asset["id"]),
        "settlement": json.loads(bill["response_json"] or "{}"),
    }


def _fail(job_id: str, code: str, now: int, db_path: str | None) -> None:
    job = _load(job_id, db_path)
    if job["status"] == "storage_failed":
        raise DeliveryError("storage_failed")
    if job["status"] == "completed":
        return
    if not store.transition(
        job_id, job["status"], "storage_failed", {"error_code": code}, now,
        db_path=db_path,
    ):
        raise DeliveryError("delivery_state_conflict")
    billing.refund_failure(job_id, now, points_client=billing.points, db_path=db_path)


def deliver(
    job_id: str,
    output_path: str,
    report: QualityReport,
    actual_cost: int,
    db_path: str | None = None,
) -> dict[str, Any]:
    if not isinstance(report, QualityReport) or not report.passed:
        raise DeliveryError("quality_not_passed")
    job = _load(job_id, db_path)
    if job["status"] == "completed":
        return _result(job_id, db_path)
    if job["status"] == "storage_failed":
        raise DeliveryError("storage_failed")
    if job["status"] not in {"quality_check", "settling"}:
        raise DeliveryError("delivery_state_conflict")
    try:
        size = os.path.getsize(output_path)
        if size <= 0:
            raise OSError("empty output")
        owner_hash = hashlib.sha256(job["owner"].encode("utf-8")).hexdigest()[:16]
        key = f"ai-edit-v2/{owner_hash}/{job_id}/delivery/final.mp4"
        upload = cos.put_file(output_path, key, "video/mp4", private=True) or {}
        metadata = cos.head_object(key)
        upload_etag = str(upload.get("ETag") or upload.get("etag") or "").strip('"')
        if (
            int(metadata.get("content_length") or -1) != size
            or metadata.get("content_type") != "video/mp4"
            or not metadata.get("etag")
            or (upload_etag and str(metadata["etag"]).strip('"') != upload_etag)
        ):
            raise DeliveryError("storage_verification_failed")
    except DeliveryError:
        _fail(job_id, "storage_verification_failed", int(time.time()), db_path)
        raise
    except Exception as exc:
        _fail(job_id, "storage_upload_failed", int(time.time()), db_path)
        raise DeliveryError("storage_upload_failed") from exc

    now = int(time.time())
    if job["status"] == "quality_check":
        store.transition(
            job_id, "quality_check", "settling",
            {"quality_report": report.as_dict(), "verified_cos_key": key}, now,
            db_path=db_path,
        )

    def finalize(conn, settlement):
        current = conn.execute("SELECT status,checkpoint_json FROM edit_v2_jobs WHERE id=?", (job_id,)).fetchone()
        if current is None or current["status"] not in {"settling", "completed"}:
            raise DeliveryError("delivery_state_conflict")
        conn.execute(
            """INSERT OR IGNORE INTO edit_v2_render_artifacts(
                   job_id,kind,version,cos_key,validation_json,cleanup_status,created_at
               ) VALUES(?,'delivery',1,?,?,'retained',?)""",
            (job_id, key, json.dumps(report.as_dict(), ensure_ascii=False), now),
        )
        if current["status"] != "completed":
            checkpoints = json.loads(current["checkpoint_json"] or "[]")
            checkpoints.append({
                "version": len(checkpoints) + 1, "state": "completed", "at": now,
                "data": {"cos_key": key, "actual_cost": int(actual_cost)},
            })
            changed = conn.execute(
                """UPDATE edit_v2_jobs SET status='completed',output_cos_key=?,
                       checkpoint_json=?,error_code=NULL,lease_owner=NULL,lease_until=NULL,
                       updated_at=? WHERE id=? AND status='settling'""",
                (key, json.dumps(checkpoints, ensure_ascii=False, separators=(",", ":")), now, job_id),
            ).rowcount
            if changed != 1:
                raise DeliveryError("delivery_state_conflict")

    try:
        billing.settle_success(
            job_id, int(actual_cost), now, points_client=billing.points,
            db_path=db_path, finalize=finalize,
        )
    except billing.BillingError as exc:
        if exc.code != "billing_operation_in_progress":
            raise
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            current = _load(job_id, db_path)
            if current["status"] == "completed":
                return _result(job_id, db_path)
            time.sleep(0.01)
        raise DeliveryError("delivery_in_progress") from exc
    return _result(job_id, db_path)
