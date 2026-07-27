# -*- coding: utf-8 -*-
"""Versioned HTTP surface and lifecycle hooks for the one-click AI editor."""

import cgi
import hashlib
import json
import pathlib
import re
import time
import urllib.parse
import uuid
from contextlib import closing


def _core():
    from . import core
    return core


LEGACY_API_PREFIX = "/api/gen/ai-edit/"
RETRY_IDEMPOTENCY_SCOPE = "/api/gen/ai-edit/jobs/retry"
RETRY_INITIALIZATION_WAIT_SECONDS = 2.0
RETRY_INITIALIZATION_LEASE_SECONDS = 10


def _retry_submission_identity(username, job_id, idem_key):
    seed = "%s\0%d\0%s" % (username, int(job_id), idem_key)
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
    # Negative ids do not advance SQLite's positive AUTOINCREMENT sequence and
    # give every process the same primary-key arbitration point for a retry.
    # Keep the numeric JSON id inside JavaScript's exact integer range because
    # the legacy page stores and interpolates it as a Number.
    successor_id = -(int(digest[:13], 16) + 1)
    return successor_id, "ai-edit-retry-" + digest, "ai-edit-retry-hold:" + digest


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


class PaidJobCompensationError(RuntimeError):
    pass


def _scannable_error_row(row, cost):
    return bool(row and row["status"] == "error" and (
        int(cost or 0) <= 0 or int(row["refunded"] or 0) in {1, 2}))


def _paid_job_row(core, job_id):
    with closing(core.jdb()) as db:
        return db.execute(
            "SELECT status,refunded FROM jobs WHERE id=?", (job_id,)).fetchone()


def _reject_paid_job(core, job_id, username, cost, reason):
    """Persist a scanner-visible terminal state before any local cleanup."""
    first_error = None
    try:
        if core._reject_pending_job(job_id, username, cost, reason):
            return
    except Exception as exc:
        first_error = exc
    try:
        row = _paid_job_row(core, job_id)
        if _scannable_error_row(row, cost):
            return
        if not row or row["status"] != "pending":
            raise RuntimeError("paid job is not rejectable: %s" % (
                row["status"] if row else "missing"))
        claimed = core.jobs_store.set_terminal(
            core.jdb, job_id, "error", error=reason, from_states=("pending",))
        if not claimed and not _scannable_error_row(_paid_job_row(core, job_id), cost):
            raise RuntimeError("paid job rejection CAS did not persist")
        if claimed:
            try:
                core._refund_once(job_id, username, cost)
            except Exception as refund_error:
                print("[ai-edit] refund confirmation deferred job=%s: %s" % (
                    job_id, str(refund_error)[:180]), flush=True)
        if _scannable_error_row(_paid_job_row(core, job_id), cost):
            return
        raise RuntimeError("paid job compensation is not scanner-visible")
    except Exception as exc:
        raise PaidJobCompensationError(
            "paid job compensation persistence failed: %s" % str(exc)[:180]) from (first_error or exc)


def _best_effort_editor_cleanup(ai_edit_store, job_id, error):
    for cleanup in (
            lambda: ai_edit_store.release_hold(job_id),
            lambda: ai_edit_store.mark_failed(job_id, error)):
        try:
            cleanup()
        except Exception as cleanup_error:
            print("[ai-edit] metadata cleanup failed job=%s: %s" % (
                job_id, str(cleanup_error)[:180]), flush=True)


def _reject_and_cleanup(core, ai_edit_store, job_id, username, cost, reason, error):
    _reject_paid_job(core, job_id, username, cost, reason)
    _best_effort_editor_cleanup(ai_edit_store, job_id, error)


def _best_effort_idempotency_abort(core, username, route, idem_key, job_id=0):
    try:
        core._idempotency_abort(username, route, idem_key)
        return True
    except Exception as cleanup_error:
        print("[ai-edit] idempotency cleanup failed job=%s: %s" % (
            job_id or "unknown", str(cleanup_error)[:180]), flush=True)
        return False


def _retry_successor_row(core, jobs_store, job_id, username, submission_ref):
    with closing(core.jdb()) as db:
        row = db.execute(
            "SELECT * FROM jobs WHERE id=?", (int(job_id),)).fetchone()
    if not row:
        return None
    try:
        payload = json.loads(row["payload"] or "{}")
    except Exception:
        payload = {}
    stored_ref = payload.pop("_submission_ref", None)
    submission_state = payload.pop("_submission_state", None)
    if row["kind"] != "ai_edit" or row["username"] != username or stored_ref != submission_ref:
        raise jobs_store.PaidJobConflictError("explicit job_id conflict")
    return row, payload, submission_state


def _retry_response(job_id, cost, predecessor_id, status, recovery_state,
                    billing_state="HELD"):
    return {
        "job_id": int(job_id), "status": str(status), "cost": int(cost or 0),
        # The points service result is not part of the durable successor row.
        # Returning null for every retry role keeps creator, DB loser and later
        # recovery responses identical without re-contacting Auth.
        "points_left": None, "billing_state": billing_state,
        "retried_from": int(predecessor_id), "recovery_state": recovery_state,
    }


def _complete_retry(handler, core, username, route, idem_key, response):
    winner = core._idempotency_complete(username, route, idem_key, response)
    return handler._send(202, winner or response)


def _compensate_retry_successor(handler, core, ai_edit_store, row, username, cost,
                                predecessor_id, route, idem_key, reason, error,
                                complete=True, expected_state=None):
    try:
        if expected_state is not None:
            if not core.jobs_store.reject_explicit_job_owner(
                    core.jdb, row["id"], expected_state, reason):
                return None
            try:
                core._refund_once(row["id"], username, cost)
            except Exception as refund_error:
                print("[ai-edit] refund confirmation deferred job=%s: %s" % (
                    row["id"], str(refund_error)[:180]), flush=True)
            _best_effort_editor_cleanup(ai_edit_store, row["id"], error)
        else:
            _reject_and_cleanup(
                core, ai_edit_store, row["id"], username, cost, reason, error)
    except Exception as cleanup_error:
        print("[ai-edit] %s" % cleanup_error, flush=True)
        try:
            current = core.jobs_store.explicit_job_state(core.jdb, row["id"])
            core.jobs_store.set_explicit_job_state(
                core.jdb, row["id"], "recovery",
                expected_states=(current[1],) if current else ())
        except Exception as state_error:
            print("[ai-edit] recovery marker failed job=%s: %s" % (
                row["id"], str(state_error)[:180]), flush=True)
        return handler._send(503, {
            "detail": "Retry compensation state could not be persisted",
            "code": "compensation_persistence_failed", "job_id": row["id"]})
    if not complete:
        response = _retry_response(
            row["id"], cost, predecessor_id, "error", "compensated",
            billing_state="RELEASED")
        core._idempotency_complete(username, route, idem_key, response)
        _best_effort_idempotency_abort(core, username, route, idem_key, row["id"])
        return handler._send(500, {
            "detail": "Retry successor initialization failed; points are being released",
            "job_id": row["id"]})
    return _complete_retry(
        handler, core, username, route, idem_key,
        _retry_response(
            row["id"], cost, predecessor_id, "error", "compensated",
            billing_state="RELEASED"))


def _recover_retry_successor(handler, core, ai_edit_store, video_domain, row, username,
                             payload, cost, predecessor_id, route, idem_key,
                             submission_state=None, initialization_token=None,
                             initial_request=False):
    job_id = int(row["id"])
    cost = int(row["cost"] or 0)
    if row["status"] in {"error", "failed"}:
        _best_effort_editor_cleanup(ai_edit_store, job_id, row["error"] or "retry compensated")
        return _complete_retry(
            handler, core, username, route, idem_key,
            _retry_response(
                job_id, cost, predecessor_id, row["status"], "compensated",
                billing_state="RELEASED"))
    if row["status"] in {"running", "done", "success"}:
        return _complete_retry(
            handler, core, username, route, idem_key,
            _retry_response(
                job_id, cost, predecessor_id, row["status"], "recovered",
                billing_state="CAPTURED" if row["status"] in {"done", "success"} else "HELD"))

    if submission_state == "ready":
        return _complete_retry(
            handler, core, username, route, idem_key,
            _retry_response(
                job_id, cost, predecessor_id, row["status"], "recovered"))

    initialization_token = initialization_token or uuid.uuid4().hex
    owned_state = "initializing:" + initialization_token

    def recover_latest():
        status, state, _updated_at = core.jobs_store.explicit_job_state(core.jdb, job_id)
        with closing(core.jdb()) as db:
            latest = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return _recover_retry_successor(
            handler, core, ai_edit_store, video_domain, latest, username,
            payload, cost, predecessor_id, route, idem_key,
            submission_state=state, initialization_token=initialization_token)

    if submission_state != owned_state and str(submission_state or "").startswith("initializing:"):
        deadline = time.time() + RETRY_INITIALIZATION_WAIT_SECONDS
        while time.time() < deadline:
            status, state, updated_at = core.jobs_store.explicit_job_state(core.jdb, job_id)
            if status in {"error", "failed", "running", "done", "success"} or state != submission_state:
                return recover_latest()
            time.sleep(0.02)
        if int(time.time()) - int(updated_at or 0) >= RETRY_INITIALIZATION_LEASE_SECONDS and core.jobs_store.set_explicit_job_state(
                core.jdb, job_id, owned_state, expected_states=(submission_state,)):
            submission_state = owned_state
        else:
            return handler._send(409, {
                "detail": "Retry successor initialization is still in progress",
                "code": "idempotency_in_progress", "job_id": job_id,
                "retry_after_ms": 500})
    elif submission_state != owned_state:
        if not core.jobs_store.set_explicit_job_state(
                core.jdb, job_id, owned_state, expected_states=(submission_state,)):
            return recover_latest()
        submission_state = owned_state

    def still_owner():
        current = core.jobs_store.explicit_job_state(core.jdb, job_id)
        return bool(current and current[0] == "pending" and current[1] == owned_state)

    def compensate_owned(reason, error):
        result = _compensate_retry_successor(
            handler, core, ai_edit_store, row, username, cost, predecessor_id,
            route, idem_key, reason, error, complete=not initial_request,
            expected_state=owned_state)
        return result if result is not None else recover_latest()

    # Renew the lease immediately before the local initialization side effects.
    if not core.jobs_store.set_explicit_job_state(
            core.jdb, job_id, owned_state, expected_states=(owned_state,)):
        return recover_latest()

    try:
        editor_job = ai_edit_store.public_job(job_id, username)
    except LookupError:
        editor_job = None
    if editor_job and (
            editor_job.get("status") in {"error", "failed", "canceled"}
            or editor_job.get("billing", {}).get("state") == "RELEASED"):
        if not still_owner():
            return recover_latest()
        return compensate_owned(
            "retry metadata was already rejected", "retry metadata was already rejected")

    try:
        ai_edit_store.create_job(job_id, username, payload, cost)
        video_domain.record_video_pending_asset(job_id, username, payload)
    except Exception as exc:
        if not still_owner():
            return recover_latest()
        return compensate_owned("retry metadata recovery failed", exc)

    # A slow metadata write may have crossed the lease deadline. A former
    # owner must never enqueue or compensate after another process took over.
    if not core.jobs_store.set_explicit_job_state(
            core.jdb, job_id, owned_state, expected_states=(owned_state,)):
        return recover_latest()

    try:
        queued = core.enqueue_job(job_id, "ai_edit", "ai_edit")
    except Exception as exc:
        if not still_owner():
            return recover_latest()
        return compensate_owned("retry queue recovery failed", exc)
    if not queued:
        if not still_owner():
            return recover_latest()
        return compensate_owned(
            "retry queue recovery was full", "retry queue recovery was full")
    if not core.jobs_store.set_explicit_job_state(
            core.jdb, job_id, "ready",
            expected_states=(owned_state,)):
        return recover_latest()
    return _complete_retry(
        handler, core, username, route, idem_key,
        _retry_response(
            job_id, cost, predecessor_id, row["status"], "recovered"))


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
        try:
            _reject_and_cleanup(
                core, ai_edit_store, job_id, username, cost,
                "剪辑任务元数据创建失败", exc)
        except PaidJobCompensationError as cleanup_error:
            print("[ai-edit] %s" % cleanup_error, flush=True)
            handler._send(503, {
                "detail": "任务补偿状态保存失败，请稍后重试或联系管理员",
                "code": "compensation_persistence_failed", "job_id": job_id})
            return True
        _best_effort_idempotency_abort(core, username, route, idem_key, job_id)
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

    match = re.fullmatch(r"/api/v1/edit-jobs/(-?\d+)/(cancel|retry)", path)
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
    retry_route = RETRY_IDEMPOTENCY_SCOPE
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
        successor_id, submission_ref, deduct_transaction_key = _retry_submission_identity(
            user["username"], job_id, idem_key)
        initialization_token = uuid.uuid4().hex
        initialization_state = "initializing:" + initialization_token

        try:
            existing = _retry_successor_row(
                core, jobs_store, successor_id, user["username"], submission_ref)
            if existing:
                existing_row, existing_payload, existing_state = existing
                return _recover_retry_successor(
                    handler, core, ai_edit_store, video_domain, existing_row,
                    user["username"], existing_payload, existing_row["cost"], job_id,
                    retry_route, idem_key, submission_state=existing_state,
                    initialization_token=initialization_token)
            payload = ai_edit.validate_ai_edit_payload(
                json.loads(row["payload"] or "{}"), user["username"])
            payload["_retry_from_job_id"] = int(job_id)
            cost = points_domain.cost_of("ai_edit", payload)
            limit_hit = core._user_video_submit_limit("ai_edit", payload, user["username"], cost)
            if limit_hit:
                # Another process may have inserted this exact submission after
                # our first lookup; its deterministic PK wins over the cap.
                existing = _retry_successor_row(
                    core, jobs_store, successor_id, user["username"], submission_ref)
                if existing:
                    existing_row, existing_payload, existing_state = existing
                    return _recover_retry_successor(
                        handler, core, ai_edit_store, video_domain, existing_row,
                        user["username"], existing_payload, existing_row["cost"], job_id,
                        retry_route, idem_key, submission_state=existing_state,
                        initialization_token=initialization_token)
                _best_effort_idempotency_abort(
                    core, user["username"], retry_route, idem_key, job_id)
                return handler._send(429, limit_hit)
            new_id, _points_left, created = jobs_store.create_paid_job(
                core.jdb, points_domain.deduct_points, points_domain.refund_points,
                "ai_edit", user["username"], cost, payload, core.SERVICE_OWNER,
                submission_ref=submission_ref,
                deduct_transaction_key=deduct_transaction_key,
                job_id=successor_id, return_created=True,
                submission_state=initialization_state)
            existing = _retry_successor_row(
                core, jobs_store, successor_id, user["username"], submission_ref)
            existing_row, existing_payload, existing_state = existing
            return _recover_retry_successor(
                handler, core, ai_edit_store, video_domain, existing_row,
                user["username"], existing_payload, existing_row["cost"], job_id,
                retry_route, idem_key, submission_state=existing_state,
                initialization_token=initialization_token,
                initial_request=created)
        except points_domain.AuthPointsError as exc:
            status = exc.status if exc.status in (402, 403, 409) else 502
            if status in (402, 403, 409):
                _best_effort_idempotency_abort(
                    core, user["username"], retry_route, idem_key, job_id)
                return handler._send(status, {"detail": exc.detail, "need": 30})
            return handler._send(502, {
                "detail": exc.detail, "code": "points_result_unknown",
                "retry_after_ms": 1000, "need": 30})
        except jobs_store.PaidJobInsertError as exc:
            if exc.job_id is None:
                _best_effort_idempotency_abort(
                    core, user["username"], retry_route, idem_key, job_id)
            return handler._send(500, {
                "detail": {"refunded": "重试任务创建失败，点数已退回",
                           "queued": "重试任务创建失败，退款正在自动重试"}.get(
                               exc.compensation, "重试任务创建失败，退款需人工核对"),
                "compensation": exc.compensation,
                "submission_ref": exc.submission_ref,
                "job_id": exc.job_id})
        except jobs_store.PaidJobConflictError:
            return handler._send(409, {
                "detail": "Retry submission identity conflicts with an existing job",
                "code": "job_identity_conflict"})
        except ValueError as exc:
            _best_effort_idempotency_abort(
                core, user["username"], retry_route, idem_key, job_id)
            return handler._send(400, {"detail": str(exc)[:220]})
        except Exception as exc:
            try:
                if _retry_successor_row(
                        core, jobs_store, successor_id, user["username"], submission_ref):
                    raise
            except jobs_store.PaidJobConflictError:
                raise
            _best_effort_idempotency_abort(
                core, user["username"], retry_route, idem_key, job_id)
            return handler._send(500, {"detail": "重试任务创建失败：" + str(exc)[:160]})

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

    match = re.fullmatch(r"/api/v1/edit-jobs/(-?\d+)(?:/(timeline|result|events))?", path)
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
