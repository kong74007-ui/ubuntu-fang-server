# -*- coding: utf-8 -*-
"""AI 智能剪辑 HTTP 路由；保持认证、扣点和核心 jobs 状态机一致。"""
import json
import os
import pathlib
import re
import time
import uuid
from contextlib import closing

from . import ai_edit_store, ai_edit_styles, audio, cos, edit_plan, video
from .renderers.shotstack import ShotstackRenderer


MAX_UPLOAD_BYTES = 1024 * 1024 * 1024
UPLOAD_TYPES = {
    "video/mp4": ("video", ".mp4"),
    "video/quicktime": ("video", ".mov"),
    "video/webm": ("video", ".webm"),
    "audio/mpeg": ("audio", ".mp3"),
    "audio/wav": ("audio", ".wav"),
    "audio/mp4": ("audio", ".m4a"),
}


def _core():
    from . import core
    return core


def _enabled():
    return str(os.environ.get("AI_EDIT_ENABLED", "0")).strip().lower() in {
        "1", "true", "yes", "on",
    }


def _disabled(handler):
    if _enabled():
        return False
    handler._send(503, {"detail": "AI智能剪辑暂未开放", "code": "ai_edit_disabled"})
    return True


def _auth(handler):
    user = _core().verify(handler._token())
    if not user:
        handler._send(401, {"detail": "未登录"})
        return None
    return user


def _safe_user_key(username):
    value = re.sub(r"[^a-zA-Z0-9_-]+", "-", str(username or "")).strip("-")
    return value[:64] or "user"


def _owned_source(username, payload):
    if payload.get("source_video_asset_id"):
        item = video.get_owned_video_asset(username, payload["source_video_asset_id"])
        if not item or not item.get("video_file"):
            return None
        return {"kind": "video", "asset": item}
    if payload.get("source_audio_asset_id"):
        item = audio.get_owned_audio_asset(username, payload["source_audio_asset_id"])
        if not item or not item.get("file"):
            return None
        return {"kind": "audio", "asset": item}
    item = ai_edit_store.get_owned_material(None, username, payload.get("source_upload_id"))
    if not item or item.get("status") != "ready" or item.get("kind") not in {"video", "audio"}:
        return None
    return {"kind": item["kind"], "material": item}


def _attached_materials(username, material_ids):
    items = []
    for material_id in material_ids or []:
        item = ai_edit_store.get_owned_material(None, username, material_id)
        if not item or item.get("status") != "ready" or item.get("kind") not in {"image", "video"}:
            raise LookupError("附加素材不存在或不属于当前账号")
        items.append(item)
    return items


def _create_upload(handler, user, body):
    content_type = str(body.get("content_type") or "").split(";", 1)[0].strip().lower()
    if content_type not in UPLOAD_TYPES:
        return handler._send(400, {"detail": "不支持的上传文件类型", "code": "upload_type_invalid"})
    try:
        size_bytes = int(body.get("size_bytes"))
    except (TypeError, ValueError):
        size_bytes = 0
    if not 0 < size_bytes <= MAX_UPLOAD_BYTES:
        return handler._send(400, {"detail": "上传文件大小必须在1字节到1GiB之间", "code": "upload_size_invalid"})
    upload_id = uuid.uuid4().hex
    kind, extension = UPLOAD_TYPES[content_type]
    cos_key = "edit-input/{}/{}{}".format(_safe_user_key(user["username"]), upload_id, extension)
    # 签名必须先成功，再记录 pending；失败不会留下永远无法完成的素材。
    signed = cos.create_presigned_put(cos_key, content_type, expires=900)
    ai_edit_store.create_material(
        None, upload_id, user["username"], kind, "source", "uploaded",
        cos_key, content_type, size_bytes,
    )
    if isinstance(signed, dict):
        put_url = signed.get("url")
        headers = signed.get("headers") or {"Content-Type": content_type}
    else:
        put_url = signed
        headers = {"Content-Type": content_type}
    return handler._send(200, {
        "upload_id": upload_id, "put_url": put_url, "headers": headers,
        "expires_in": 900, "max_size_bytes": MAX_UPLOAD_BYTES,
    })


def _complete_upload(handler, user, upload_id):
    material = ai_edit_store.get_owned_material(None, user["username"], upload_id)
    if not material:
        return handler._send(404, {"detail": "上传记录不存在"})
    if material.get("status") == "ready":
        return handler._send(200, {"upload": material})
    info = cos.head(material["cos_key"])
    actual = int(info.get("size_bytes") or 0)
    if actual != int(material.get("size_bytes") or 0):
        return handler._send(409, {"detail": "上传对象大小与声明不一致", "code": "upload_size_mismatch"})
    actual_type = str(info.get("content_type") or "").split(";", 1)[0].lower()
    if actual_type and actual_type != material.get("content_type"):
        return handler._send(409, {"detail": "上传对象类型与声明不一致", "code": "upload_type_mismatch"})
    if not ai_edit_store.complete_material(None, upload_id, user["username"], actual):
        return handler._send(409, {"detail": "上传状态已变化，请刷新后重试", "code": "upload_state_conflict"})
    return handler._send(200, {"upload": ai_edit_store.get_owned_material(None, user["username"], upload_id)})


def _create_job(handler, user, request_body):
    core = _core()
    idem_key = core._idempotency_key(handler.headers.get("Idempotency-Key"))
    if not idem_key:
        return handler._send(400, {"detail": "必须提供Idempotency-Key", "code": "idempotency_required"})
    try:
        payload = edit_plan.validate_submit_payload(request_body)
    except ValueError as exc:
        return handler._send(400, {"detail": str(exc)[:220], "code": "invalid_request"})
    source = _owned_source(user["username"], payload)
    if not source:
        return handler._send(404, {"detail": "源素材不存在或不属于当前账号"})
    try:
        attached = _attached_materials(user["username"], payload.get("material_ids"))
    except LookupError as exc:
        return handler._send(404, {"detail": str(exc)})
    if source.get("material"):
        payload["_source_upload_cos_key"] = source["material"]["cos_key"]
        payload["_source_upload_content_type"] = source["material"]["content_type"]
    payload["_attached_materials"] = attached
    payload["mode"] = "ai_edit"
    points_domain = core._domains()[1]
    cost = points_domain.cost_of("ai_edit", payload)
    endpoint = "/api/v1/edit/jobs"
    with core._submission_lock:
        state, response = core._idempotency_begin(user["username"], endpoint, idem_key, request_body)
        if state == "replay":
            return handler._send(200, response)
        if state == "conflict":
            return handler._send(409, {"detail": "同一个Idempotency-Key不能用于不同请求", "code": "idempotency_conflict"})
        if state == "processing":
            return handler._send(409, {"detail": "相同请求正在受理", "code": "idempotency_in_progress", "retry_after_ms": 1000})
        active = core._user_active_job_count(user["username"])
        if active >= core.MAX_USER_ACTIVE_JOBS:
            core._idempotency_abort(user["username"], endpoint, idem_key)
            return handler._send(429, {"detail": "当前任务数已达上限", "code": "active_job_cap", "active_jobs": active})
        try:
            points_left = points_domain.deduct_points(user["username"], cost, "job:ai_edit")
        except points_domain.AuthPointsError as exc:
            core._idempotency_abort(user["username"], endpoint, idem_key)
            return handler._send(402 if exc.status == 402 else 502, {"detail": exc.detail, "need": cost})
        job_id = None
        try:
            now = int(time.time())
            with closing(core.jdb()) as connection:
                cursor = connection.execute(
                    "INSERT INTO jobs(kind,username,cost,payload,created_at,updated_at,owner) VALUES(?,?,?,?,?,?,?)",
                    ("ai_edit", user["username"], cost, json.dumps(payload, ensure_ascii=False), now, now, core.SERVICE_OWNER),
                )
                connection.commit()
                job_id = cursor.lastrowid
            ai_edit_store.create_edit_job(None, job_id, user["username"], payload["style"], "shotstack", cost)
            if source.get("material"):
                ai_edit_store.attach_material(None, job_id, source["material"]["id"], "source")
            for item in attached:
                ai_edit_store.attach_material(None, job_id, item["id"], "broll")
            video.record_video_pending_asset(job_id, user["username"], payload)
            if not core.enqueue_job(job_id, "ai_edit", "ai_edit"):
                raise OverflowError("任务队列已满，请稍后再试")
        except Exception as exc:
            if job_id:
                core._reject_pending_job(job_id, user["username"], cost, str(exc))
                video.update_video_asset_phase(job_id, "failed", status="failed", error=str(exc)[:300])
                ai_edit_store.release_hold(None, job_id)
            else:
                points_domain.safe_refund_points(user["username"], cost, "job:ai_edit 提交回滚")
            core._idempotency_abort(user["username"], endpoint, idem_key)
            return handler._send(429 if isinstance(exc, OverflowError) else 500, {
                "detail": str(exc)[:220], "code": "queue_full" if isinstance(exc, OverflowError) else "submit_failed",
            })
    response = {"job_id": job_id, "cost": cost, "points_left": points_left}
    core._idempotency_complete(user["username"], endpoint, idem_key, response)
    return handler._send(200, response)


def _get_job(handler, user, job_id):
    core = _core()
    try:
        job_id = int(job_id)
    except (TypeError, ValueError):
        return handler._send(404, {"detail": "任务不存在"})
    with closing(core.jdb()) as connection:
        row = connection.execute(
            "SELECT * FROM jobs WHERE id=? AND username=? AND kind='ai_edit' AND COALESCE(deleted,0)=0",
            (job_id, user["username"]),
        ).fetchone()
    detail = ai_edit_store.get_owned_job(None, user["username"], job_id)
    if not row or not detail:
        return handler._send(404, {"detail": "任务不存在"})
    public = core._job_public_dict(row, detail.get("stage"))
    public["edit"] = {key: detail.get(key) for key in (
        "style", "renderer", "stage", "provider_status", "error_code", "error_detail",
    )}
    return handler._send(200, public)


def _webhook(handler, body):
    provider_job_id = str(body.get("id") or body.get("render_id") or "").strip()
    if not provider_job_id:
        return handler._send(400, {"detail": "缺少渲染任务ID"})
    detail = ai_edit_store.get_job_by_provider_id(None, provider_job_id)
    if not detail:
        return handler._send(200, {"ok": True, "ignored": True})
    current = ShotstackRenderer().get_status(provider_job_id)
    ai_edit_store.set_provider_job(None, detail["job_id"], provider_job_id, current.get("status") or "")
    return handler._send(200, {"ok": True})


def handle_post(handler):
    path = handler.path.split("?", 1)[0]
    if path == "/api/v1/edit/webhooks/shotstack":
        try:
            return_value = _webhook(handler, handler._json_body_strict())
        except Exception as exc:
            return_value = handler._send(502, {"detail": str(exc)[:220], "code": "provider_status_failed"})
        return bool(return_value or True)
    if not path.startswith("/api/v1/edit/"):
        return False
    if _disabled(handler):
        return True
    user = _auth(handler)
    if not user:
        return True
    try:
        body = handler._json_body_strict()
    except ValueError as exc:
        handler._send(400, {"detail": str(exc)[:220]})
        return True
    try:
        if path == "/api/v1/edit/uploads":
            _create_upload(handler, user, body)
            return True
        match = re.fullmatch(r"/api/v1/edit/uploads/([a-f0-9]{32})/complete", path)
        if match:
            _complete_upload(handler, user, match.group(1))
            return True
        if path == "/api/v1/edit/jobs":
            _create_job(handler, user, body)
            return True
    except Exception as exc:
        handler._send(400, {"detail": str(exc)[:220], "code": "edit_request_failed"})
        return True
    return False


def handle_get(handler):
    path = handler.path.split("?", 1)[0]
    if path != "/api/v1/edit/styles" and not re.fullmatch(r"/api/v1/edit/jobs/\d+", path):
        return False
    if _disabled(handler):
        return True
    user = _auth(handler)
    if not user:
        return True
    if path == "/api/v1/edit/styles":
        cost = _core()._domains()[1].cost_of("ai_edit", {})
        materials = ai_edit_store.list_owned_materials(None, user["username"])
        handler._send(200, {
            "styles": ai_edit_styles.list_styles(),
            "cost": cost,
            "materials": materials,
        })
        return True
    _get_job(handler, user, path.rsplit("/", 1)[-1])
    return True
