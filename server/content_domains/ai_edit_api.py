# -*- coding: utf-8 -*-
"""Versioned HTTP surface and lifecycle hooks for the one-click AI editor."""

import cgi
import json
import pathlib
import re
import time
import urllib.parse
from contextlib import closing


def _core():
    from . import core
    return core


LEGACY_API_PREFIX = "/api/gen/ai-edit/"


def _canonical_path(path):
    """Keep the public legacy namespace isolated while retaining /api/v1 aliases."""
    if not path.startswith(LEGACY_API_PREFIX):
        return path
    suffix = path[len(LEGACY_API_PREFIX):]
    if suffix == "styles":
        return "/api/v1/edit-styles"
    if suffix == "assets" or suffix.startswith("assets/"):
        return "/api/v1/edit-" + suffix
    if suffix == "jobs" or suffix.startswith("jobs/"):
        return "/api/v1/edit-" + suffix
    return path


def init_db():
    from . import ai_edit_store
    ai_edit_store.init_db()


def job_completed(kind, job_id, result):
    if kind != "ai_edit":
        return
    try:
        from . import ai_edit_store
        ai_edit_store.mark_done(job_id, result)
        ai_edit_store.capture_hold(job_id)
    except Exception as exc:
        print("[ai-edit] 完成元数据写入失败 job=%s: %s" % (job_id, str(exc)[:180]), flush=True)


def job_failed(kind, job_id, error):
    if kind != "ai_edit":
        return
    try:
        from . import ai_edit, ai_edit_store
        ai_edit_store.release_hold(job_id)
        ai_edit_store.mark_failed(job_id, error, canceled=isinstance(error, ai_edit.EditCancelled))
    except Exception:
        pass


def _best_effort_reject_and_cleanup(core, ai_edit_store, job_id, username, cost, reason, error):
    """Keep the paid-job refund marker authoritative; editor metadata is secondary."""
    try:
        core._reject_pending_job(job_id, username, cost, reason)
    except Exception as cleanup_error:
        print("[ai-edit] paid-job cleanup failed job=%s: %s" % (
            job_id, str(cleanup_error)[:180]), flush=True)
    for cleanup in (
            lambda: ai_edit_store.release_hold(job_id),
            lambda: ai_edit_store.mark_failed(job_id, error)):
        try:
            cleanup()
        except Exception as cleanup_error:
            print("[ai-edit] metadata cleanup failed job=%s: %s" % (
                job_id, str(cleanup_error)[:180]), flush=True)


def prepare_submission(handler, kind, job_id, username, payload, cost, video_domain, route, idem_key):
    """Create editor metadata and the ordinary pending video asset.

    Returns True only when an AI-editor failure response has already been sent.
    Other video kinds preserve their existing exception behavior.
    """
    if kind not in {"video", "tryon", "xiaole_video", "cinematic", "ai_edit"}:
        return False
    core = _core()
    try:
        if kind == "ai_edit":
            from . import ai_edit_store
            ai_edit_store.create_job(job_id, username, payload, cost)
        video_domain.record_video_pending_asset(job_id, username, payload)
        return False
    except Exception as exc:
        if kind != "ai_edit":
            raise
        _best_effort_reject_and_cleanup(
            core, ai_edit_store, job_id, username, cost,
            "剪辑任务元数据创建失败", exc)
        try:
            core._idempotency_abort(username, route, idem_key)
        except Exception as cleanup_error:
            print("[ai-edit] idempotency cleanup failed job=%s: %s" % (
                job_id, str(cleanup_error)[:180]), flush=True)
        handler._send(500, {"detail": "剪辑任务创建失败，点数已释放"})
        return True


def queue_rejected(kind, job_id, error):
    if kind != "ai_edit":
        return
    from . import ai_edit_store
    ai_edit_store.release_hold(job_id)
    ai_edit_store.mark_failed(job_id, error)


def decorate_submission(kind, response):
    if kind == "ai_edit":
        response["billing_state"] = "HELD"


def submission_status(handler, kind):
    return 202 if kind == "ai_edit" and getattr(handler, "_versioned_edit_job", False) else 200


def _send_file_range(handler, path, content_type, filename="material"):
    path = pathlib.Path(path)
    total = path.stat().st_size
    start, end, status = 0, max(0, total - 1), 200
    raw_range = str(handler.headers.get("Range") or "").strip()
    if raw_range.startswith("bytes="):
        try:
            left, right = raw_range[6:].split("-", 1)
            start = int(left or 0)
            end = int(right) if right else end
            end = min(end, total - 1)
            if start < 0 or start > end:
                raise ValueError
            status = 206
        except Exception:
            handler.send_response(416)
            handler.send_header("Content-Range", "bytes */%d" % total)
            handler.end_headers()
            return
    length = max(0, end - start + 1)
    handler.send_response(status)
    handler.send_header("Content-Type", content_type or "application/octet-stream")
    handler.send_header("Content-Length", str(length))
    handler.send_header("Accept-Ranges", "bytes")
    handler.send_header("Cache-Control", "private, no-store")
    handler.send_header("Content-Disposition", "inline; filename*=UTF-8''%s" % urllib.parse.quote(filename))
    if status == 206:
        handler.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, total))
    handler.end_headers()
    with path.open("rb") as source:
        source.seek(start)
        remaining = length
        while remaining > 0:
            chunk = source.read(min(65536, remaining))
            if not chunk:
                break
            handler.wfile.write(chunk)
            remaining -= len(chunk)


def handle_post(handler, path, points_domain, video_domain):
    """Return ``(handled, possibly_rewritten_path)``."""
    path = _canonical_path(path)
    core = _core()
    if path == "/api/v1/edit-assets":
        user = core.verify(handler._token())
        if not user:
            handler._send(401, {"detail": "未登录"})
            return True, path
        from . import ai_edit_store
        try:
            length = int(handler.headers.get("Content-Length") or 0)
            if length <= 0:
                raise ValueError("上传文件为空")
            if length > ai_edit_store.VIDEO_MAX_BYTES + 2 * 1024 * 1024:
                raise ValueError("上传文件超过服务器限制")
            content_type = str(handler.headers.get("Content-Type") or "")
            if not content_type.lower().startswith("multipart/form-data"):
                raise ValueError("请使用文件上传格式")
            form = cgi.FieldStorage(
                fp=handler.rfile, headers=handler.headers,
                environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": content_type,
                         "CONTENT_LENGTH": str(length)}, keep_blank_values=True)
            field = form["file"] if "file" in form else None
            if isinstance(field, list):
                field = field[0] if field else None
            if field is None or not getattr(field, "file", None):
                raise ValueError("请选择上传文件")
            material = ai_edit_store.save_material(
                user["username"], field.file, getattr(field, "filename", ""),
                getattr(field, "type", ""), form.getfirst("usage", "auto"))
            handler._send(201, {"ok": True, "material": material})
        except ValueError as exc:
            handler._send(400, {"detail": str(exc)[:220]})
        except Exception as exc:
            handler._send(500, {"detail": "素材上传失败：" + str(exc)[:180]})
        return True, path

    match = re.fullmatch(r"/api/v1/edit-assets/(\d+)/(usage|delete)", path)
    if match:
        user = core.verify(handler._token())
        if not user:
            handler._send(401, {"detail": "未登录"})
            return True, path
        from . import ai_edit_store
        try:
            material_id, action = int(match.group(1)), match.group(2)
            if action == "delete":
                item = ai_edit_store.delete_material(material_id, user["username"])
            else:
                body = handler._json_body_strict()
                item = ai_edit_store.update_material_usage(material_id, user["username"], body.get("usage"))
            handler._send(200, {"ok": True, "material": item})
        except LookupError as exc:
            handler._send(404, {"detail": str(exc)})
        except ValueError as exc:
            handler._send(400, {"detail": str(exc)[:220]})
        except Exception as exc:
            handler._send(500, {"detail": str(exc)[:180]})
        return True, path

    match = re.fullmatch(r"/api/v1/edit-jobs/(\d+)/(cancel|retry)", path)
    if match:
        _handle_job_action(handler, int(match.group(1)), match.group(2), points_domain, video_domain)
        return True, path
    if path == "/api/v1/edit-jobs":
        handler._versioned_edit_job = True
        return False, "/api/gen/ai_edit"
    return False, path


def _handle_job_action(handler, job_id, action, points_domain, video_domain):
    core = _core()
    user = core.verify(handler._token())
    if not user:
        return handler._send(401, {"detail": "未登录"})
    from . import ai_edit, ai_edit_store, jobs_store
    with closing(core.jdb()) as db:
        row = db.execute(
            "SELECT * FROM jobs WHERE id=? AND username=? AND kind='ai_edit'",
            (job_id, user["username"])).fetchone()
    if not row:
        return handler._send(404, {"detail": "剪辑任务不存在"})
    if action == "cancel":
        if row["status"] in {"done", "error", "failed"}:
            return handler._send(409, {"detail": "任务已经结束，不能取消"})
        try:
            ai_edit_store.request_cancel(job_id, user["username"])
            if row["status"] == "pending" and core._set_terminal(
                    job_id, "error", error="用户取消", from_states=("pending",)):
                core._mark_video_asset_failed(job_id, "ai_edit", "用户取消")
                core._refund_once(job_id, user["username"], row["cost"])
                ai_edit_store.release_hold(job_id)
                ai_edit_store.mark_failed(job_id, "用户取消", canceled=True)
            return handler._send(202, {"ok": True, "job": ai_edit_store.public_job(job_id, user["username"])})
        except LookupError as exc:
            return handler._send(404, {"detail": str(exc)})

    if row["status"] not in {"error", "failed"}:
        return handler._send(409, {"detail": "只有失败任务可以重试"})
    retry_route = "/api/gen/ai-edit/jobs/%d/retry" % job_id
    request_body = {"action": "retry", "job_id": int(job_id)}
    try:
        idem_key = core._idempotency_key(handler.headers.get("Idempotency-Key"))
        if not idem_key:
            raise ValueError("重试任务必须提供 Idempotency-Key")
    except ValueError as exc:
        return handler._send(400, {"detail": str(exc)[:220]})

    with core._submission_lock:
        idem_state, idem_response = core._idempotency_begin(
            user["username"], retry_route, idem_key, request_body)
        if idem_state == "replay":
            return handler._send(202, idem_response)
        if idem_state == "conflict":
            return handler._send(409, {
                "detail": "同一个 Idempotency-Key 不能用于不同请求",
                "code": "idempotency_conflict"})
        if idem_state == "processing":
            return handler._send(409, {
                "detail": "相同重试请求正在受理，请稍后查询",
                "code": "idempotency_in_progress", "retry_after_ms": 1000})

        try:
            payload = ai_edit.validate_ai_edit_payload(
                json.loads(row["payload"] or "{}"), user["username"])
            payload["_retry_from_job_id"] = int(job_id)
            cost = points_domain.cost_of("ai_edit", payload)
            limit_hit = core._user_video_submit_limit("ai_edit", payload, user["username"], cost)
            if limit_hit:
                core._idempotency_abort(user["username"], retry_route, idem_key)
                return handler._send(429, limit_hit)
            new_id, points_left = jobs_store.create_paid_job(
                core.jdb, points_domain.deduct_points, points_domain.refund_points,
                "ai_edit", user["username"], cost, payload, core.SERVICE_OWNER)
        except points_domain.AuthPointsError as exc:
            core._idempotency_abort(user["username"], retry_route, idem_key)
            return handler._send(exc.status if exc.status in (402, 403) else 502,
                                 {"detail": exc.detail, "need": 30})
        except jobs_store.PaidJobInsertError as exc:
            core._idempotency_abort(user["username"], retry_route, idem_key)
            return handler._send(500, {
                "detail": {"refunded": "重试任务创建失败，点数已退回",
                           "queued": "重试任务创建失败，退款正在自动重试"}.get(
                               exc.compensation, "重试任务创建失败，退款需人工核对"),
                "compensation": exc.compensation,
                "submission_ref": exc.submission_ref})
        except ValueError as exc:
            core._idempotency_abort(user["username"], retry_route, idem_key)
            return handler._send(400, {"detail": str(exc)[:220]})
        except Exception as exc:
            core._idempotency_abort(user["username"], retry_route, idem_key)
            return handler._send(500, {"detail": "重试任务创建失败：" + str(exc)[:160]})

        try:
            ai_edit_store.create_job(new_id, user["username"], payload, cost)
            video_domain.record_video_pending_asset(new_id, user["username"], payload)
        except Exception as exc:
            _best_effort_reject_and_cleanup(
                core, ai_edit_store, new_id, user["username"], cost,
                "重试任务元数据创建失败", exc)
            core._idempotency_abort(user["username"], retry_route, idem_key)
            return handler._send(500, {
                "detail": "重试任务创建失败，退款正在自动处理",
                "job_id": new_id})

        try:
            queued = core.enqueue_job(new_id, "ai_edit", "ai_edit")
        except Exception as exc:
            _best_effort_reject_and_cleanup(
                core, ai_edit_store, new_id, user["username"], cost,
                "重试任务入队失败", exc)
            core._idempotency_abort(user["username"], retry_route, idem_key)
            return handler._send(500, {
                "detail": "重试任务入队失败，退款正在自动处理",
                "job_id": new_id})
        if not queued:
            queue_error = "任务队列已满"
            _best_effort_reject_and_cleanup(
                core, ai_edit_store, new_id, user["username"], cost,
                queue_error, queue_error)
            core._idempotency_abort(user["username"], retry_route, idem_key)
            return handler._send(429, {
                "detail": "任务队列已满，点数已释放",
                "code": "queue_full", "retry_after_ms": 4000})

        response = {"job_id": new_id, "cost": cost, "points_left": points_left,
                    "billing_state": "HELD", "retried_from": job_id}
        core._idempotency_complete(user["username"], retry_route, idem_key, response)
        return handler._send(202, response)


def handle_get(handler, path):
    path = _canonical_path(path)
    core = _core()
    if path == "/api/v1/edit-styles":
        user = core.verify(handler._token())
        if not user:
            handler._send(401, {"detail": "未登录"})
        else:
            from . import ai_edit
            handler._send(200, {"items": ai_edit.list_styles(), "cost": core.COST.get("ai_edit", 30),
                                "resolution": "1080p", "ratio": "9:16"})
        return True
    if path == "/api/v1/edit-assets":
        user = core.verify(handler._token())
        if not user:
            handler._send(401, {"detail": "未登录"})
            return True
        from . import ai_edit_store
        query = urllib.parse.parse_qs(urllib.parse.urlparse(handler.path).query)
        try:
            items = ai_edit_store.list_materials(
                user["username"], limit=int((query.get("limit") or ["200"])[0]))
            handler._send(200, {"items": items, "count": len(items),
                                "recommended": ai_edit_store.JOB_MATERIAL_RECOMMENDED,
                                "soft_limit": ai_edit_store.JOB_MATERIAL_SOFT_LIMIT,
                                "hard_limit": ai_edit_store.JOB_MATERIAL_HARD_LIMIT})
        except Exception as exc:
            handler._send(400, {"detail": str(exc)[:180]})
        return True

    if path == "/api/v1/edit-jobs":
        user = core.verify(handler._token())
        if not user:
            handler._send(401, {"detail": "未登录"})
            return True
        from . import ai_edit_store
        query = urllib.parse.parse_qs(urllib.parse.urlparse(handler.path).query)
        try:
            page = int((query.get("page") or ["1"])[0])
            page_size = int((query.get("page_size") or ["10"])[0])
            handler._send(200, ai_edit_store.list_jobs(
                user["username"], page=page, page_size=page_size))
        except (TypeError, ValueError):
            handler._send(400, {"detail": "分页参数无效"})
        except Exception as exc:
            handler._send(500, {"detail": str(exc)[:180]})
        return True

    match = re.fullmatch(r"/api/v1/edit-assets/(\d+)/content", path)
    if match:
        user = core.verify(handler._token())
        if not user:
            handler._send(401, {"detail": "未登录"})
            return True
        from . import ai_edit_store
        try:
            file_path, row = ai_edit_store.material_path(int(match.group(1)), user["username"])
            _send_file_range(handler, file_path, row.get("content_type"), row.get("filename") or file_path.name)
        except LookupError as exc:
            handler._send(404, {"detail": str(exc)})
        return True

    match = re.fullmatch(r"/api/v1/edit-jobs/(\d+)(?:/(timeline|result|events))?", path)
    if match:
        _handle_job_read(handler, int(match.group(1)), match.group(2) or "")
        return True
    return False


def _handle_job_read(handler, job_id, view):
    core = _core()
    user = core.verify(handler._token())
    if not user:
        return handler._send(401, {"detail": "未登录"})
    from . import ai_edit_store
    with closing(core.jdb()) as db:
        row = db.execute(
            "SELECT * FROM jobs WHERE id=? AND username=? AND kind='ai_edit'",
            (job_id, user["username"])).fetchone()
    if not row:
        return handler._send(404, {"detail": "剪辑任务不存在"})
    try:
        payload = json.loads(row["payload"] or "{}")
    except Exception:
        payload = {}
    ai_edit_store.ensure_legacy_job(job_id, user["username"], payload, row["cost"])
    try:
        result = json.loads(row["result"] or "{}") if row["result"] else {}
        if row["status"] == "done":
            ai_edit_store.mark_done(job_id, result)
            ai_edit_store.capture_hold(job_id)
        elif row["status"] in {"error", "failed"}:
            ai_edit_store.release_hold(job_id)
            error = row["error"] or "剪辑失败"
            stored = ai_edit_store.get_job(job_id, user["username"])
            canceled = bool(stored and stored.get("status") == "canceled") or "取消" in error
            ai_edit_store.mark_failed(job_id, error, canceled=canceled)
        item = ai_edit_store.public_job(
            job_id, user["username"], include_timeline=view in {"timeline", "result"})
        item["legacy_status"], item["refunded"] = row["status"], bool(row["refunded"])
        if view == "timeline":
            return handler._send(200, {"job_id": job_id, "timeline": item.get("timeline") or {}})
        if view == "result":
            if row["status"] != "done":
                return handler._send(409, {"detail": "剪辑尚未完成", "job": item})
            return handler._send(200, {"job_id": job_id, "result": item.get("result") or {},
                                       "timeline": item.get("timeline") or {}})
        if view == "events":
            return _send_events(handler, job_id, user["username"])
        return handler._send(200, item)
    except LookupError as exc:
        return handler._send(404, {"detail": str(exc)})
    except (BrokenPipeError, ConnectionResetError):
        return None
    except Exception as exc:
        return handler._send(500, {"detail": str(exc)[:180]})


def _send_events(handler, job_id, username):
    from . import ai_edit_store
    handler.send_response(200)
    handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
    handler.send_header("Cache-Control", "no-cache, no-transform")
    handler.send_header("Connection", "keep-alive")
    handler.send_header("X-Accel-Buffering", "no")
    handler.end_headers()
    previous = None
    for _ in range(25):
        current = ai_edit_store.public_job(job_id, username)
        encoded = json.dumps(current, ensure_ascii=False, separators=(",", ":"))
        if encoded != previous:
            handler.wfile.write(("event: progress\ndata: %s\n\n" % encoded).encode("utf-8"))
            handler.wfile.flush()
            previous = encoded
        if current.get("status") in {"done", "error", "failed", "canceled"}:
            break
        time.sleep(1)


def handle_delete(handler, path):
    path = _canonical_path(path)
    match = re.fullmatch(r"/api/v1/edit-assets/(\d+)", path)
    if not match:
        return False
    core = _core()
    user = core.verify(handler._token())
    if not user:
        handler._send(401, {"detail": "未登录"})
        return True
    from . import ai_edit_store
    try:
        item = ai_edit_store.delete_material(int(match.group(1)), user["username"])
        handler._send(200, {"ok": True, "material": item})
    except LookupError as exc:
        handler._send(404, {"detail": str(exc)})
    except Exception as exc:
        handler._send(500, {"detail": str(exc)[:180]})
    return True
