"""Thin authenticated HTTP adapter for the isolated AI editing V2 domain."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import uuid
from contextlib import closing
from typing import Any

from . import ai_edit_v2_store as store
from . import ai_edit_v2_cos as cos
from . import ai_edit_v2_billing as billing
from . import ai_edit_v2_pipeline as pipeline
from . import ai_edit_v2_feature as feature
from . import points
from .ai_edit_v2_schema import (
    ASPECT_RATIOS,
    CREATION_MODES,
    EDIT_PLAN_VERSION,
    MAX_AUDIO_BYTES,
    MAX_IMAGE_BYTES,
    MAX_MAIN_VIDEO_BYTES,
    MAX_MATERIALS_PER_WINDOW,
    MAX_SUPPLEMENTARY_VIDEO_BYTES,
    REFERENCE_MODES,
    FAILURE_STATES,
    validate_job_draft,
)


API_PREFIX = "/api/v2/edit/"
_UPLOAD_RE = re.compile(r"^/api/v2/edit/uploads/([0-9a-f-]{36})/complete$")
_MATERIAL_RE = re.compile(r"^/api/v2/edit/materials/(\d+)$")
_JOB_RE = re.compile(r"^/api/v2/edit/jobs/([0-9a-f-]{36})$")
_JOB_RETRY_RE = re.compile(r"^/api/v2/edit/jobs/([0-9a-f-]{36})/retry$")
_CONTENT_TYPES = {
    "video": {"video/mp4", "video/quicktime"},
    "image": {"image/jpeg", "image/png", "image/webp"},
    "audio": {"audio/mpeg", "audio/mp4", "audio/x-m4a", "audio/wav", "audio/x-wav"},
}
_EXTENSIONS = {
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
}
_points_client = points


def _new_uuid() -> str:
    return str(uuid.uuid4())


def _now() -> int:
    return int(time.time())


def _send(handler: Any, status: int, payload: dict[str, Any]) -> bool:
    handler._send(status, payload)
    return True


def _owner(user: dict[str, Any]) -> str:
    return str(user.get("username") or "").strip()


def _owner_hash(owner: str) -> str:
    return hashlib.sha256(owner.encode("utf-8")).hexdigest()[:16]


def _material_public(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "kind": row["kind"],
        "purpose": row["purpose"],
        "reference_mode": row["reference_mode"],
        "filename": row["filename"],
        "content_type": row["mime_type"],
        "size_bytes": row["size_bytes"],
        "duration_ms": row["duration_ms"],
        "width": row["width"],
        "height": row["height"],
        "etag": row["etag"],
        "status": row["status"],
    }


def _read_body(handler: Any) -> dict[str, Any]:
    body = handler._json_body()
    if not isinstance(body, dict):
        raise ValueError("请求体必须是对象")
    return body


def _create_upload(handler: Any, owner: str) -> bool:
    try:
        body = _read_body(handler)
        kind = body.get("kind")
        purpose = body.get("purpose")
        content_type = str(body.get("content_type") or "").lower()
        reference_mode = body.get("reference_mode")
        if kind not in _CONTENT_TYPES:
            raise ValueError("素材类型不受支持")
        if purpose not in {"primary", "required", "reference"}:
            raise ValueError("素材用途不受支持")
        if content_type not in _CONTENT_TYPES[kind]:
            raise ValueError("素材Content-Type与类型不匹配")
        if purpose == "reference" and reference_mode not in REFERENCE_MODES:
            raise ValueError("参考素材必须选择参考模式")
        if purpose != "reference":
            reference_mode = None
        filename = os.path.basename(str(body.get("filename") or "upload"))[:180]
        upload_id = _new_uuid()
        object_key = "ai-edit-v2/{}/{}/uploads/source{}".format(
            _owner_hash(owner), upload_id, _EXTENSIONS[content_type]
        )
        now = _now()
        with closing(store.open_store(store._db_path())) as conn:
            conn.execute(
                """INSERT INTO edit_v2_materials(
                       upload_id,owner,kind,purpose,reference_mode,source,cos_key,
                       filename,declared_content_type,status,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    upload_id,
                    owner,
                    kind,
                    purpose,
                    reference_mode,
                    "user_upload",
                    object_key,
                    filename,
                    content_type,
                    "uploading",
                    now,
                    now,
                ),
            )
        upload_url = cos.presign_put(object_key, content_type, expires=900)
        return _send(
            handler,
            201,
            {"upload_id": upload_id, "upload_url": upload_url, "expires_in": 900},
        )
    except ValueError as exc:
        return _send(handler, 400, {"detail": str(exc)})
    except Exception:
        return _send(handler, 502, {"detail": "上传凭证创建失败"})


def _size_limit(row: Any) -> int:
    if row["kind"] == "video":
        return (
            MAX_MAIN_VIDEO_BYTES
            if row["purpose"] == "primary"
            else MAX_SUPPLEMENTARY_VIDEO_BYTES
        )
    if row["kind"] == "image":
        return MAX_IMAGE_BYTES
    return MAX_AUDIO_BYTES


def _complete_upload(handler: Any, owner: str, upload_id: str) -> bool:
    with closing(store.open_store(store._db_path())) as conn:
        row = conn.execute(
            "SELECT * FROM edit_v2_materials WHERE upload_id=? AND owner=?",
            (upload_id, owner),
        ).fetchone()
    if row is None:
        return _send(handler, 404, {"detail": "上传不存在"})
    if row["status"] == "ready":
        return _send(handler, 200, {"material": _material_public(row)})
    try:
        verified = cos.head_object(row["cos_key"])
        actual_type = str(verified.get("content_type") or "").split(";", 1)[0].lower()
        actual_size = int(verified.get("content_length") or 0)
        if actual_type != row["declared_content_type"]:
            raise ValueError("COS核验类型与上传声明不一致")
        if actual_size < 1 or actual_size > _size_limit(row):
            raise ValueError("COS核验文件容量超过限制")
        now = _now()
        with closing(store.open_store(store._db_path())) as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT * FROM edit_v2_materials WHERE upload_id=? AND owner=?",
                (upload_id, owner),
            ).fetchone()
            if current["status"] != "ready":
                conn.execute(
                    """UPDATE edit_v2_materials
                       SET mime_type=?,size_bytes=?,etag=?,status='ready',updated_at=?
                       WHERE id=? AND status='uploading'""",
                    (actual_type, actual_size, verified.get("etag"), now, current["id"]),
                )
            ready = conn.execute(
                "SELECT * FROM edit_v2_materials WHERE id=?", (current["id"],)
            ).fetchone()
            conn.commit()
        return _send(handler, 200, {"material": _material_public(ready)})
    except ValueError as exc:
        return _send(handler, 400, {"detail": str(exc)})
    except Exception:
        return _send(handler, 502, {"detail": "COS对象核验失败"})


def _get_material(handler: Any, owner: str, material_id: int) -> bool:
    with closing(store.open_store(store._db_path())) as conn:
        row = conn.execute(
            "SELECT * FROM edit_v2_materials WHERE id=? AND owner=? AND status='ready'",
            (material_id, owner),
        ).fetchone()
    if row is None:
        return _send(handler, 404, {"detail": "素材不存在"})
    return _send(handler, 200, {"material": _material_public(row)})


def _list_materials(handler: Any, owner: str) -> bool:
    with closing(store.open_store(store._db_path())) as conn:
        rows = conn.execute(
            """SELECT * FROM edit_v2_materials
               WHERE owner=? AND status='ready' ORDER BY created_at DESC,id DESC LIMIT 100""",
            (owner,),
        ).fetchall()
    return _send(handler, 200, {"items": [_material_public(row) for row in rows]})


def canonicalize_job_draft(owner: str, client_draft: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not isinstance(client_draft, dict):
        raise ValueError("draft must be an object")
    groups = [
        ("primary", [client_draft.get("main_input")]),
        ("required", client_draft.get("required_materials") or []),
        ("reference", client_draft.get("reference_materials") or []),
    ]
    requested: list[tuple[str, int, dict[str, Any]]] = []
    seen: set[int] = set()
    for purpose, items in groups:
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("material_not_available")
            try:
                material_id = int(item.get("asset_id"))
            except (TypeError, ValueError):
                raise ValueError("material_not_available") from None
            if material_id in seen:
                raise ValueError("material_reused_across_groups")
            seen.add(material_id)
            requested.append((purpose, material_id, item))
    if not requested:
        raise ValueError("material_not_available")
    placeholders = ",".join("?" for _ in requested)
    with closing(store.open_store(store._db_path())) as conn:
        rows = conn.execute(
            f"SELECT * FROM edit_v2_materials WHERE id IN ({placeholders}) AND owner=? AND status='ready'",
            (*[item[1] for item in requested], owner),
        ).fetchall()
    by_id = {int(row["id"]): row for row in rows}
    canonical_groups: dict[str, list[dict[str, Any]]] = {"primary": [], "required": [], "reference": []}
    bindings: list[dict[str, Any]] = []
    for purpose, material_id, client_item in requested:
        row = by_id.get(material_id)
        if row is None or row["purpose"] != purpose:
            raise ValueError("material_not_available")
        reference_mode = client_item.get("reference_mode") if purpose == "reference" else None
        if purpose == "reference" and reference_mode not in REFERENCE_MODES:
            raise ValueError("reference_mode_invalid")
        material = {
            "asset_id": str(material_id),
            "kind": row["kind"],
            "size_bytes": int(row["size_bytes"] or 0),
            "duration_ms": row["duration_ms"],
        }
        if reference_mode:
            material["reference_mode"] = reference_mode
        canonical_groups[purpose].append(material)
        bindings.append({"material_id": material_id, "purpose": purpose})
    canonical = {
        key: client_draft.get(key)
        for key in ("creation_mode", "brief", "language", "aspect_ratio", "target_duration_ms")
    }
    canonical["main_input"] = canonical_groups["primary"][0]
    canonical["required_materials"] = canonical_groups["required"]
    canonical["reference_materials"] = canonical_groups["reference"]
    validate_job_draft(canonical)
    return canonical, bindings


def _validate_job_request(handler: Any, owner: str) -> bool:
    try:
        body = _read_body(handler)
        draft, bindings = canonicalize_job_draft(owner, body.get("draft"))
        quote_id = str(body.get("quote_id") or "").strip()
        idempotency_key = str(body.get("idempotency_key") or "").strip()
        if not quote_id or not idempotency_key:
            raise ValueError("缺少报价或幂等键")
        result = billing.precharge_and_create_job(
            owner,
            {"draft": draft},
            quote_id,
            idempotency_key,
            _now(),
            points_client=_points_client,
            uuid_factory=_new_uuid,
            material_bindings=bindings,
        )
        return _send(
            handler,
            201,
            {
                "job_id": result["job"]["id"],
                "status": result["job"]["status"],
                "held_points": result["held_points"],
            },
        )
    except billing.PrechargePending as exc:
        return _send(
            handler,
            202,
            {
                "job_id": exc.job_id,
                "status": "billing_pending",
                "billing_status": "pending",
                "held_points": exc.held_points,
                "retry_after_seconds": 3,
            },
        )
    except billing.BillingError as exc:
        return _send(handler, 409, {"detail": exc.code})
    except points.AuthPointsError as exc:
        return _send(handler, 402 if exc.status == 402 else 502, {"detail": exc.detail})
    except (TypeError, ValueError) as exc:
        return _send(handler, 400, {"detail": str(exc)})


def _validate_quote_request(handler: Any, owner: str) -> bool:
    try:
        body = _read_body(handler)
        draft, _bindings = canonicalize_job_draft(owner, body.get("draft"))
        quote = billing.create_quote(owner, draft, _now(), uuid_factory=_new_uuid)
        return _send(handler, 201, {"quote": quote})
    except (TypeError, ValueError) as exc:
        return _send(handler, 400, {"detail": str(exc)})


def _get_job(handler: Any, owner: str, job_id: str) -> bool:
    with closing(store.open_store(store._db_path())) as conn:
        row = conn.execute(
            "SELECT * FROM edit_v2_jobs WHERE id=? AND owner=?", (job_id, owner)
        ).fetchone()
    if row is None:
        return _send(handler, 404, {"detail": "任务不存在"})
    with closing(store.open_store(store._db_path())) as conn:
        bill = conn.execute(
            "SELECT status,amount FROM edit_v2_billing WHERE job_id=? AND operation='hold'",
            (job_id,),
        ).fetchone()
    return _send(
        handler,
        200,
        {
            "job": {
                "id": row["id"],
                "status": row["status"],
                "quote_id": row["quote_id"],
                "output_available": bool(row["output_cos_key"]),
                "error_code": row["error_code"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            },
            "timing": pipeline.timing_status(job_id, _now(), db_path=store._db_path()),
            "billing": {
                "status": bill["status"] if bill else None,
                "held_points": int(bill["amount"]) if bill else 0,
            },
        },
    )


def _retry_job(handler: Any, owner: str, job_id: str) -> bool:
    try:
        body = _read_body(handler)
        client_key = str(body.get("idempotency_key") or "").strip()
        if not client_key or len(client_key) > 160:
            raise ValueError("重试请求必须提供有效幂等键")
        with closing(store.open_store(store._db_path())) as conn:
            old = conn.execute(
                "SELECT * FROM edit_v2_jobs WHERE id=? AND owner=?", (job_id, owner)
            ).fetchone()
        if old is None:
            return _send(handler, 404, {"detail": "任务不存在"})
        if old["status"] not in FAILURE_STATES:
            return _send(handler, 409, {"detail": "仅终态失败任务允许重试"})
        payload = json.loads(old["payload_json"])
        now = _now()
        quote = billing.create_quote(owner, payload["draft"], now, uuid_factory=_new_uuid)
        result = billing.precharge_and_create_job(
            owner,
            payload,
            quote["id"],
            f"retry:{job_id}:{client_key}",
            now,
            points_client=_points_client,
            uuid_factory=_new_uuid,
        )
        successor = result["job"]
        return _send(
            handler,
            201,
            {
                "job_id": successor["id"],
                "predecessor_job_id": job_id,
                "status": successor["status"],
            },
        )
    except ValueError as exc:
        return _send(handler, 400, {"detail": str(exc)})


def dispatch(
    handler: Any,
    method: str,
    path: str,
    user: dict[str, Any] | None,
) -> bool:
    """Dispatch a V2 route and return False only when the prefix is unrelated."""
    if not path.startswith(API_PREFIX):
        return False
    if path.startswith(API_PREFIX + "webhooks/"):
        return _send(handler, 503, {"detail": "webhook capability disabled"})
    if not user or not _owner(user):
        return _send(handler, 401, {"detail": "未登录"})
    owner = _owner(user)

    if method == "GET" and path == API_PREFIX + "capabilities":
        capability = feature.capability()
        return _send(
            handler,
            200,
            {
                **capability,
                "version": EDIT_PLAN_VERSION,
                "creation_modes": sorted(CREATION_MODES),
                "aspect_ratios": sorted(ASPECT_RATIOS),
                "material_window_limit": MAX_MATERIALS_PER_WINDOW,
            },
        )
    if method == "GET" and (path == API_PREFIX + "materials" or _MATERIAL_RE.fullmatch(path)):
        pass
    elif method == "GET" and _JOB_RE.fullmatch(path):
        pass
    else:
        rejection = feature.rejection()
        if rejection is not None:
            return _send(handler, rejection[0], rejection[1])
    if method == "GET" and path == API_PREFIX + "templates":
        return _send(handler, 200, {"items": []})
    if method == "GET" and path == API_PREFIX + "materials":
        return _list_materials(handler, owner)
    material_match = _MATERIAL_RE.fullmatch(path)
    if method == "GET" and material_match:
        return _get_material(handler, owner, int(material_match.group(1)))
    if method == "POST" and path == API_PREFIX + "uploads":
        return _create_upload(handler, owner)
    upload_match = _UPLOAD_RE.fullmatch(path)
    if method == "POST" and upload_match:
        return _complete_upload(handler, owner, upload_match.group(1))
    if method == "POST" and path == API_PREFIX + "jobs":
        return _validate_job_request(handler, owner)
    if method == "POST" and path == API_PREFIX + "quotes":
        return _validate_quote_request(handler, owner)
    job_match = _JOB_RE.fullmatch(path)
    if method == "GET" and job_match:
        return _get_job(handler, owner, job_match.group(1))
    retry_match = _JOB_RETRY_RE.fullmatch(path)
    if method == "POST" and retry_match:
        return _retry_job(handler, owner, retry_match.group(1))
    return _send(handler, 404, {"detail": "not found"})
