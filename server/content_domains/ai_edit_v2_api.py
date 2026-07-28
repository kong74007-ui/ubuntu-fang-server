"""Thin authenticated HTTP adapter for the isolated AI editing V2 domain."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
import urllib.parse
import uuid
from contextlib import closing
from typing import Any

from . import ai_edit_v2_store as store
from . import ai_edit_v2_cos as cos
from . import ai_edit_v2_billing as billing
from . import ai_edit_v2_pipeline as pipeline
from . import ai_edit_v2_media as media
from . import ai_edit_v2_feature as feature
from . import ai_edit_v2_shotstack as shotstack
from . import points
from .ai_edit_v2_providers.base import ProviderError, RetryableProviderError
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
_WEBHOOK_PATH = API_PREFIX + "webhooks/shotstack"
_WEBHOOK_MAX_BYTES = 64 * 1024
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


def _quote_public(quote: dict[str, Any]) -> dict[str, Any]:
    """Expose the stable price contract while retaining Phase A field aliases."""

    minimum = int(quote["min_points"])
    maximum = int(quote["max_points"])
    return {
        **quote,
        "minimum_points": minimum,
        "maximum_points": maximum,
        "held_points": maximum,
    }


def _stored_quote_public(owner: str, quote_id: str) -> dict[str, Any]:
    with closing(store.open_store(store._db_path())) as conn:
        row = conn.execute(
            "SELECT * FROM edit_v2_quotes WHERE id=? AND owner=?", (quote_id, owner)
        ).fetchone()
    if row is None:
        raise ValueError("quote_not_found")
    return _quote_public(
        {
            "id": row["id"],
            "min_points": int(row["min_points"]),
            "max_points": int(row["max_points"]),
            "breakdown": json.loads(row["breakdown_json"]),
            "price_version": row["price_version"],
            "expires_at": int(row["expires_at"]),
        }
    )


def _public_capability() -> dict[str, Any]:
    state = feature.capability()
    components = state.get("stable_components") or {}
    stable_ready = bool(state.get("stable_runtime_ready"))
    return {
        "feature": "ai_edit_v2",
        "enabled": bool(state.get("enabled")),
        "runtime_ready": bool(state.get("runtime_ready")),
        "accepts_submissions": bool(state.get("accepts_submissions")),
        "phase": "stable_v1",
        "reason": state.get("reason"),
        "stable_workflow": {
            "transcription": bool(components.get("dashscope")),
            "content_direction": bool(components.get("dashscope")),
            "generated_images": bool(components.get("openai_image")),
            "optional_audio": bool(components.get("elevenlabs")),
            "composition": bool(components.get("shotstack")),
            "private_delivery": bool(components.get("cos")),
            "ready": stable_ready,
        },
        "disabled_features": [
            "advanced_motion_graphics",
            "ai_video_generation",
            "free_code_rendering",
        ],
        "version": EDIT_PLAN_VERSION,
        "creation_modes": sorted(CREATION_MODES),
        "aspect_ratios": sorted(ASPECT_RATIOS),
        "material_window_limit": MAX_MATERIALS_PER_WINDOW,
    }


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
        metadata = {}
        if row["kind"] in {"video", "audio"}:
            metadata = media.probe_media(
                cos.presign_get(row["cos_key"], expires=300), media_type=row["kind"]
            )
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
                       SET mime_type=?,size_bytes=?,etag=?,duration_ms=?,width=?,height=?,
                           status='ready',updated_at=?
                       WHERE id=? AND status='uploading'""",
                    (actual_type, actual_size, verified.get("etag"),
                     metadata.get("duration_ms"), metadata.get("width"), metadata.get("height"),
                     now, current["id"]),
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
        for key in (
            "creation_mode", "brief", "language", "aspect_ratio", "target_duration_ms",
            "input_mode",
        )
    }
    if client_draft.get("original_text") is not None:
        canonical["original_text"] = client_draft["original_text"]
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
        if idempotency_key.startswith("retry:"):
            raise ValueError("idempotency_key_reserved")
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
        return _send(handler, 409, {"code": exc.code, "detail": exc.code})
    except points.AuthPointsError as exc:
        return _send(handler, 402 if exc.status == 402 else 502, {"detail": exc.detail})
    except (TypeError, ValueError) as exc:
        return _send(handler, 400, {"detail": str(exc)})


def _validate_quote_request(handler: Any, owner: str) -> bool:
    try:
        body = _read_body(handler)
        draft, _bindings = canonicalize_job_draft(owner, body.get("draft"))
        quote = billing.create_quote(owner, draft, _now(), uuid_factory=_new_uuid)
        return _send(handler, 201, {"quote": _quote_public(quote)})
    except (TypeError, ValueError) as exc:
        return _send(handler, 400, {"detail": str(exc)})


def _collect_degradations(checkpoints: Any) -> list[str]:
    found: list[str] = []
    allowed = {
        "image_generation_degraded",
        "music_generation_degraded",
        "sfx_generation_degraded",
    }

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "degradations" and isinstance(item, list):
                    for code in item:
                        if (
                            isinstance(code, str)
                            and code in allowed
                            and code not in found
                        ):
                            found.append(code)
                elif key == "material_resolution_status" and item in allowed:
                    if item not in found:
                        found.append(item)
                else:
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(checkpoints)
    return found


def _quality_public(intent: Any, checkpoints: Any, status: str) -> dict[str, Any]:
    value: dict[str, Any] | None = None
    if intent is not None:
        try:
            parsed = json.loads(intent["quality_json"])
            value = parsed if isinstance(parsed, dict) else None
        except (TypeError, ValueError):
            value = None
    if value is None:
        def find_qc(item: Any) -> dict[str, Any] | None:
            if isinstance(item, dict):
                qc = item.get("qc")
                if isinstance(qc, dict):
                    return qc
                for child in item.values():
                    match = find_qc(child)
                    if match is not None:
                        return match
            elif isinstance(item, list):
                for child in item:
                    match = find_qc(child)
                    if match is not None:
                        return match
            return None

        value = find_qc(checkpoints)
    if value is None:
        return {
            "status": "pending" if status not in FAILURE_STATES else "failed",
            "passed": False,
            "summary": "等待质检" if status not in FAILURE_STATES else "未交付合格成片",
            "failing_layers": [],
        }
    passed = value.get("passed") is True
    public_layers = {
        "probe", "decode_video", "decode_audio", "frames", "captions",
        "materials", "transcript", "audio", "video", "assembly",
    }
    layers = [
        item for item in value.get("failing_layers", []) if item in public_layers
    ]
    return {
        "status": "passed" if passed else "failed",
        "passed": passed,
        "summary": "成片已通过质检" if passed else "成片未通过质检",
        "failing_layers": layers,
    }


def _get_job(handler: Any, owner: str, job_id: str) -> bool:
    with closing(store.open_store(store._db_path())) as conn:
        row = conn.execute(
            "SELECT * FROM edit_v2_jobs WHERE id=? AND owner=?", (job_id, owner)
        ).fetchone()
        if row is None:
            return _send(handler, 404, {"detail": "任务不存在"})
        bill = conn.execute(
            "SELECT status,amount,response_json FROM edit_v2_billing WHERE job_id=? AND operation='hold'",
            (job_id,),
        ).fetchone()
        quote = conn.execute(
            "SELECT min_points,max_points,expires_at FROM edit_v2_quotes WHERE id=? AND owner=?",
            (row["quote_id"], owner),
        ).fetchone()
        intent = conn.execute(
            "SELECT quality_json FROM edit_v2_delivery_intents WHERE job_id=? AND owner=?",
            (job_id, owner),
        ).fetchone()
        outbox = conn.execute(
            "SELECT asset_id,status FROM edit_v2_delivery_outbox WHERE job_id=? AND owner=?",
            (job_id, owner),
        ).fetchone()
    try:
        checkpoints = json.loads(row["checkpoint_json"] or "[]")
    except (TypeError, ValueError):
        checkpoints = []
    now = _now()
    timing = pipeline.timing_status(job_id, now, db_path=store._db_path())
    terminal = row["status"] == "completed" or row["status"] in FAILURE_STATES
    elapsed = max(0, (int(row["updated_at"]) if terminal else now) - int(row["created_at"]))
    settlement: dict[str, Any] = {}
    if bill and bill["response_json"]:
        try:
            parsed = json.loads(bill["response_json"])
            settlement = parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            settlement = {}
    held = int(bill["amount"]) if bill else 0
    actual = settlement.get("actual_points")
    if bill and bill["status"] == "refunded":
        actual = 0
    refunded = settlement.get("refunded_points")
    billing_public = {
        "status": bill["status"] if bill else None,
        "held_points": held,
        "actual_charge_points": int(actual) if isinstance(actual, int) and not isinstance(actual, bool) else None,
        "refunded_difference_points": int(refunded) if isinstance(refunded, int) and not isinstance(refunded, bool) else 0,
    }
    output = None
    if row["status"] == "completed" and row["output_cos_key"] and outbox and outbox["asset_id"] is not None:
        play_url = cos.presign_get(row["output_cos_key"], expires=300)
        download_url = cos.presign_get(row["output_cos_key"], expires=300)
        asset_id = int(outbox["asset_id"])
        output = {
            "play_url": play_url,
            "download_url": download_url,
            "expires_in": 300,
            "asset_id": asset_id,
            "asset_url": f"assets.html?cat=video&asset={asset_id}",
        }
    job_public = {
        "id": row["id"],
        "status": row["status"],
        "stage": row["status"],
        "predecessor_job_id": row["predecessor_job_id"],
        "output_available": output is not None,
        "created_at": int(row["created_at"]),
        "updated_at": int(row["updated_at"]),
    }
    payload = {
        "job": job_public,
        "stage": row["status"],
        "elapsed_seconds": elapsed,
        "estimated_remaining_seconds": int(timing["remaining_seconds"]),
        "timing": timing,
        "degradations": _collect_degradations(checkpoints),
        "quality": _quality_public(intent, checkpoints, row["status"]),
        "billing": billing_public,
        "quote": {
            "minimum_points": int(quote["min_points"]),
            "maximum_points": int(quote["max_points"]),
            "held_points": held,
            "expires_at": int(quote["expires_at"]),
        } if quote is not None else None,
        "output": output,
    }
    return _send(handler, 200, payload)


def _retry_job(handler: Any, owner: str, job_id: str) -> bool:
    try:
        body = _read_body(handler)
        client_key = str(body.get("idempotency_key") or "").strip()
        if not client_key or len(client_key) > 160:
            raise ValueError("重试请求必须提供有效幂等键")
        retry_key = f"retry:{job_id}"
        with closing(store.open_store(store._db_path())) as conn:
            old = conn.execute(
                "SELECT * FROM edit_v2_jobs WHERE id=? AND owner=?", (job_id, owner)
            ).fetchone()
            old_bindings = conn.execute(
                """SELECT material_id,purpose FROM edit_v2_job_materials
                   WHERE job_id=? ORDER BY material_id,purpose""",
                (job_id,),
            ).fetchall()
            existing_successor = conn.execute(
                """SELECT * FROM edit_v2_jobs
                   WHERE owner=? AND predecessor_job_id=?""",
                (owner, job_id),
            ).fetchone()
            successor_bindings = (
                conn.execute(
                    """SELECT material_id,purpose FROM edit_v2_job_materials
                       WHERE job_id=? ORDER BY material_id,purpose""",
                    (existing_successor["id"],),
                ).fetchall()
                if existing_successor is not None
                else []
            )
        if old is None:
            return _send(handler, 404, {"detail": "任务不存在"})
        if old["status"] not in FAILURE_STATES:
            return _send(handler, 409, {"detail": "仅终态失败任务允许重试"})
        now = _now()
        if existing_successor is not None:
            payload = json.loads(existing_successor["payload_json"])
            bindings = [
                {"material_id": int(row["material_id"]), "purpose": row["purpose"]}
                for row in successor_bindings
            ]
            result = billing.precharge_and_create_job(
                owner,
                payload,
                existing_successor["quote_id"],
                existing_successor["idempotency_key"],
                now,
                points_client=_points_client,
                uuid_factory=_new_uuid,
                material_bindings=bindings,
                predecessor_job_id=job_id,
            )
            successor = result["job"]
            return _send(
                handler,
                201,
                {
                    "job_id": successor["id"],
                    "predecessor_job_id": job_id,
                    "status": successor["status"],
                    "quote": _stored_quote_public(owner, successor["quote_id"]),
                    "held_points": result["held_points"],
                },
            )
        payload = json.loads(old["payload_json"])
        draft, bindings = canonicalize_job_draft(owner, payload.get("draft"))
        expected_bindings = sorted(
            (int(row["material_id"]), str(row["purpose"])) for row in old_bindings
        )
        current_bindings = sorted(
            (int(binding["material_id"]), str(binding["purpose"]))
            for binding in bindings
        )
        if current_bindings != expected_bindings:
            raise ValueError("retry_material_bindings_mismatch")
        payload = {"draft": draft}
        quote_id = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"ai-edit-v2:{owner}:{retry_key}:quote")
        )
        quote = billing.create_quote(
            owner, draft, now, uuid_factory=lambda: quote_id
        )
        result = billing.precharge_and_create_job(
            owner,
            payload,
            quote["id"],
            retry_key,
            now,
            points_client=_points_client,
            uuid_factory=_new_uuid,
            material_bindings=bindings,
            predecessor_job_id=job_id,
        )
        successor = result["job"]
        return _send(
            handler,
            201,
            {
                "job_id": successor["id"],
                "predecessor_job_id": job_id,
                "status": successor["status"],
                "quote": _quote_public(quote),
                "held_points": result["held_points"],
            },
        )
    except ValueError as exc:
        return _send(handler, 400, {"detail": str(exc)})
    except billing.PrechargePending as exc:
        return _send(
            handler,
            202,
            {
                "job_id": exc.job_id,
                "predecessor_job_id": job_id,
                "status": "billing_pending",
                "billing_status": "pending",
                "held_points": exc.held_points,
                "retry_after_seconds": 3,
            },
        )
    except billing.BillingError as exc:
        return _send(handler, 409, {"code": exc.code, "detail": exc.code})
    except points.AuthPointsError as exc:
        return _send(handler, 402 if exc.status == 402 else 502, {"detail": exc.detail})


def _header(handler: Any, name: str) -> str:
    headers = getattr(handler, "headers", {}) or {}
    try:
        return str(headers.get(name) or headers.get(name.lower()) or "")
    except Exception:
        return ""


def _shotstack_webhook(handler: Any, query: str) -> bool:
    try:
        declared = _header(handler, "Content-Length")
        if not declared:
            return _send(handler, 411, {"detail": "回调请求体长度缺失"})
        size = int(declared)
        if size < 2 or size > _WEBHOOK_MAX_BYTES:
            return _send(handler, 413, {"detail": "回调请求体大小无效"})
        params = urllib.parse.parse_qs(query, keep_blank_values=True, strict_parsing=True)
        if set(params) != {"attempt_id", "token"} or any(len(values) != 1 for values in params.values()):
            return _send(handler, 401, {"detail": "回调鉴权失败"})
        try:
            attempt_id = int(params["attempt_id"][0])
        except (TypeError, ValueError):
            attempt_id = 0
        supplied_token = str(params["token"][0])
        with closing(store.open_store(store._db_path())) as conn:
            binding = conn.execute(
                """SELECT a.job_id,a.provider_task_id,a.provider_reference,j.status
                   FROM edit_v2_stage_attempts a
                   JOIN edit_v2_jobs j ON j.id=a.job_id
                   WHERE a.id=? AND a.stage='rendering' AND j.status='rendering'""",
                (attempt_id,),
            ).fetchone()
        if binding is None or not binding["provider_reference"]:
            hmac.compare_digest(supplied_token, "0" * 64)
            return _send(handler, 401, {"detail": "回调鉴权失败"})
        client = shotstack.ShotstackClient(
            job_id=binding["job_id"], attempt_id=attempt_id, db_path=store._db_path()
        )
        expected_token = client.callback_token(binding["provider_reference"])
        if not hmac.compare_digest(supplied_token, expected_token):
            return _send(handler, 401, {"detail": "回调鉴权失败"})
        event = _read_body(handler)
        if set(event) != {"id", "status"}:
            raise ValueError("回调结构无效")
        task_id = event.get("id")
        status = event.get("status")
        if (
            not isinstance(task_id, str) or not 1 <= len(task_id) <= 200
            or not isinstance(status, str) or not 1 <= len(status) <= 64
        ):
            raise ValueError("回调结构无效")
        if binding["provider_task_id"] and binding["provider_task_id"] != task_id:
            return _send(handler, 401, {"detail": "回调鉴权失败"})
        result = shotstack.reconcile_webhook(
            binding["job_id"],
            event,
            client,
            callback_attempt_id=attempt_id,
            callback_token=supplied_token,
            received_at=_now(),
            db_path=store._db_path(),
        )
        return _send(handler, 202, {"accepted": True, "duplicate": result is None})
    except (TypeError, ValueError):
        return _send(handler, 400, {"detail": "回调请求无效"})
    except RetryableProviderError:
        return _send(handler, 503, {"detail": "状态确认稍后重试"})
    except ProviderError:
        return _send(handler, 409, {"detail": "状态确认失败"})
    except Exception:
        return _send(handler, 502, {"detail": "状态确认失败"})


def dispatch(
    handler: Any,
    method: str,
    path: str,
    user: dict[str, Any] | None,
) -> bool:
    """Dispatch a V2 route and return False only when the prefix is unrelated."""
    parsed = urllib.parse.urlsplit(path)
    route_path = parsed.path
    if not route_path.startswith(API_PREFIX):
        return False
    if route_path == _WEBHOOK_PATH:
        if method != "POST":
            return _send(handler, 405, {"detail": "method not allowed"})
        return _shotstack_webhook(handler, parsed.query)
    path = route_path
    if not user or not _owner(user):
        return _send(handler, 401, {"detail": "未登录"})
    owner = _owner(user)

    if method == "GET" and path == API_PREFIX + "capabilities":
        return _send(handler, 200, _public_capability())
    if method != "GET":
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
    if method == "POST" and path in {API_PREFIX + "quote", API_PREFIX + "quotes"}:
        return _validate_quote_request(handler, owner)
    job_match = _JOB_RE.fullmatch(path)
    if method == "GET" and job_match:
        return _get_job(handler, owner, job_match.group(1))
    retry_match = _JOB_RETRY_RE.fullmatch(path)
    if method == "POST" and retry_match:
        return _retry_job(handler, owner, retry_match.group(1))
    return _send(handler, 404, {"detail": "not found"})
