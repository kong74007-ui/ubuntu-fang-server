"""Thin HTTP dispatch boundary for AI Edit V3 Phase A."""

from __future__ import annotations

import re
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .contracts import ContractError, canonical_json, parse_strict_json
from .service import EditV3Service, ServiceError


_PREFIX = "/api/v3/edit"
_MAX_BODY_BYTES = 64 * 1024
_IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}\Z")
_UPLOAD_COMPLETE = re.compile(r"/api/v3/edit/uploads/([^/]+)/complete\Z")
_JOB_DETAIL = re.compile(r"/api/v3/edit/jobs/([^/]+)\Z")
_JOB_PLAN = re.compile(r"/api/v3/edit/jobs/([^/]+)/plan\Z")
_JOB_RESULT = re.compile(r"/api/v3/edit/jobs/([^/]+)/result\Z")
_JOB_RETRY = re.compile(r"/api/v3/edit/jobs/([^/]+)/retry\Z")


_ERROR_DEFINITIONS: dict[str, tuple[int, str, str, bool]] = {}


def _define_errors(
    codes: tuple[str, ...],
    status: int,
    message: str,
    stage: str,
    retryable: bool = False,
) -> None:
    for code in codes:
        _ERROR_DEFINITIONS[code] = (status, message, stage, retryable)


_define_errors(("authentication_required",), 401, "请先登录后再操作", "authentication")
_define_errors(("not_found", "quote_not_found", "template_not_found"), 404, "未找到对应资源", "request")
_define_errors(("method_not_allowed",), 405, "当前接口不支持该请求方法", "request")
_define_errors(("request_too_large",), 413, "请求内容超过大小限制", "request")
_define_errors(
    (
        "invalid_json",
        "control_character_forbidden",
        "identifier_invalid",
        "request_invalid",
        "request_type_invalid",
        "request_unknown_field",
        "request_authority_field_forbidden",
        "job_query_invalid",
        "idempotency_key_invalid",
        "input_type_invalid",
        "input_discriminator_conflict",
        "creation_mode_invalid",
        "creation_mode_conflict",
        "ratio_invalid",
        "style_prompt_invalid",
        "tts_input_invalid",
        "material_asset_ids_invalid",
        "job_cursor_invalid",
        "unicode_scalar_invalid",
    ),
    400,
    "请求参数无效，请检查后重试",
    "request",
)
_define_errors(
    (
        "input_audio_invalid",
        "input_declared_mime_invalid",
        "input_declared_size_invalid",
        "input_duration_invalid",
        "input_image_dimensions_invalid",
        "input_image_size_exceeded",
        "input_image_type_unsupported",
        "input_media_kind_mismatch",
        "input_object_metadata_invalid",
        "input_probe_invalid",
        "input_probe_metadata_mismatch",
        "input_source_invalid",
        "input_upload_total_exceeded",
        "input_upload_type_invalid",
        "input_video_invalid",
        "material_source_invalid",
        "object_key_invalid",
    ),
    400,
    "输入素材不符合要求，请检查文件后重试",
    "input",
)
_define_errors(
    (
        "upload_expired",
        "upload_identity_conflict",
        "upload_not_completable",
        "quote_authority_mismatch",
        "quote_conflict",
        "quote_expired",
        "quote_request_mismatch",
        "quote_template_mismatch",
        "idempotency_conflict",
        "material_upload_invalid",
        "retry_not_allowed",
        "retry_source_invalid",
        "billing_intent_conflict",
        "billing_intent_missing",
        "job_material_binding_conflict",
        "retry_predecessor_not_found",
        "quote_environment_mismatch",
        "template_ambiguous",
        "template_ratio_unsupported",
        "template_unpublished",
        "template_reference_unpublished",
        "pricing_ceiling_exceeded",
        "job_cursor_scope_mismatch",
        "quote_pricing_missing",
        "quote_template_unpublished",
        "job_material_not_found",
        "material_identity_conflict",
        "material_upload_metadata_mismatch",
    ),
    409,
    "请求与已冻结的数据冲突，请刷新后重试",
    "job",
)
_define_errors(
    ("plan_not_ready", "result_not_ready"),
    409,
    "任务结果尚未准备完成",
    "job",
)
_define_errors(
    (
        "feature_disabled",
        "capability_unavailable",
        "quote_capability_unavailable",
        "upload_capability_unavailable",
        "retry_capability_unavailable",
    ),
    503,
    "当前功能暂不可用",
    "capability",
)
_define_errors(
    (
        "platform_assets_unavailable",
        "audio_assets_unavailable",
        "voices_unavailable",
        "templates_unavailable",
        "pricing_unavailable",
        "pricing_ambiguous",
        "pricing_parameters_invalid",
        "pricing_parts_invalid",
        "pricing_part_invalid",
        "pricing_integer_invalid",
        "pricing_rate_invalid",
        "pricing_quantity_invalid",
        "pricing_overflow",
        "template_ratios_invalid",
        "quote_expiry_overflow",
    ),
    503,
    "依赖能力暂不可用，请稍后重试",
    "capability",
    True,
)
_define_errors(
    ("capacity_unavailable",),
    503,
    "当前处理容量不足，请稍后重试",
    "capacity",
    True,
)
_define_errors(
    (
        "identity_unavailable",
        "input_inspection_unavailable",
        "job_storage_failed",
        "material_storage_failed",
        "plan_storage_invalid",
        "quote_storage_invalid",
        "quote_identity_invalid",
        "result_storage_invalid",
        "service_unavailable",
        "upload_presign_unavailable",
        "upload_storage_failed",
        "billing_create_invalid",
    ),
    503,
    "服务暂不可用，请稍后重试",
    "storage",
    True,
)
_define_errors(("internal_error",), 500, "服务处理失败，请稍后重试", "internal", True)
del _define_errors


def _header(handler: Any, name: str) -> str | None:
    headers = getattr(handler, "headers", None)
    if headers is None:
        return None
    value = headers.get(name)
    if value is not None:
        return value
    lowered = name.lower()
    for key, candidate in headers.items():
        if isinstance(key, str) and key.lower() == lowered:
            return candidate
    return None


def _send(
    handler: Any,
    status: int,
    payload: Mapping[str, Any],
    *,
    retry_after: int | None = None,
) -> None:
    body = canonical_json(payload)
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    if retry_after is not None:
        handler.send_header("Retry-After", str(retry_after))
    handler.end_headers()
    handler.wfile.write(body)


def _error_payload(error_code: str, _message: str, _status: int) -> dict[str, Any]:
    if error_code not in _ERROR_DEFINITIONS:
        error_code = "internal_error"
    _mapped_status, message, stage, retryable = _ERROR_DEFINITIONS[error_code]
    return {
        "error_code": error_code,
        "message": message,
        "stage": stage,
        "retryable": retryable,
    }


def _safe_service_error(error: ServiceError) -> tuple[int, dict[str, Any], int | None]:
    code = error.error_code
    if not isinstance(code, str) or code not in _ERROR_DEFINITIONS:
        code = "internal_error"
    status, _message, _stage, retryable = _ERROR_DEFINITIONS[code]
    retry_after = error.retry_after
    if (
        not retryable
        or retry_after is not None
        and (
            isinstance(retry_after, bool)
            or not isinstance(retry_after, int)
            or not 1 <= retry_after <= 3_600
        )
    ):
        retry_after = None
    payload = _error_payload(code, "", status)
    if retry_after is not None:
        payload["retry_after"] = retry_after
    return status, payload, retry_after


def _owner(user: Any) -> str:
    if isinstance(user, Mapping):
        for name in ("owner", "username", "user_id", "id"):
            value = user.get(name)
            if isinstance(value, str) and value and value == value.strip():
                return value
    raise ServiceError(
        "authentication_required", "authentication is required", status=401
    )


def _read_json(handler: Any) -> dict[str, Any]:
    raw_length = _header(handler, "Content-Length")
    try:
        length = int(raw_length) if raw_length is not None else -1
    except (TypeError, ValueError) as exc:
        raise ServiceError("invalid_json", "request body length is invalid") from exc
    if length < 0:
        raise ServiceError("invalid_json", "request body length is invalid")
    if length > _MAX_BODY_BYTES:
        raise ServiceError("request_too_large", "request body is too large", status=413)
    raw = handler.rfile.read(length)
    if not isinstance(raw, bytes) or len(raw) != length:
        raise ServiceError("invalid_json", "request body is incomplete")
    try:
        value = parse_strict_json(
            raw,
            max_bytes=_MAX_BODY_BYTES,
            max_depth=16,
            max_items=512,
            max_string_chars=4_000,
        )
    except ContractError as exc:
        raise ServiceError("invalid_json", "request JSON is invalid") from exc
    if not isinstance(value, dict):
        raise ServiceError("invalid_json", "request JSON must be an object")
    return value


def _idempotency_key(handler: Any) -> str:
    value = _header(handler, "Idempotency-Key")
    if (
        not isinstance(value, str)
        or _IDEMPOTENCY_KEY.fullmatch(value) is None
        or value.startswith("retry:")
    ):
        raise ServiceError(
            "idempotency_key_invalid", "Idempotency-Key is invalid"
        )
    return value


def _request_now(service: EditV3Service) -> int:
    clock = getattr(service, "now", None)
    value = clock() if callable(clock) else int(time.time() * 1000)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ServiceError("service_unavailable", "request clock is unavailable", status=503)
    return value


def _route(path: str) -> tuple[str, tuple[str, ...], frozenset[str]] | None:
    static = {
        f"{_PREFIX}/capabilities": ("capabilities", (), frozenset({"GET"})),
        f"{_PREFIX}/platform-assets": ("platform-assets", (), frozenset({"GET"})),
        f"{_PREFIX}/audio-assets": ("audio-assets", (), frozenset({"GET"})),
        f"{_PREFIX}/voices": ("voices", (), frozenset({"GET"})),
        f"{_PREFIX}/templates": ("templates", (), frozenset({"GET"})),
        f"{_PREFIX}/uploads": ("uploads", (), frozenset({"POST"})),
        f"{_PREFIX}/materials": ("materials", (), frozenset({"POST"})),
        f"{_PREFIX}/quote": ("quote", (), frozenset({"POST"})),
        f"{_PREFIX}/jobs": ("jobs", (), frozenset({"GET", "POST"})),
    }
    if path in static:
        return static[path]
    for pattern, name, methods in (
        (_UPLOAD_COMPLETE, "upload-complete", frozenset({"POST"})),
        (_JOB_PLAN, "job-plan", frozenset({"GET"})),
        (_JOB_RESULT, "job-result", frozenset({"GET"})),
        (_JOB_RETRY, "job-retry", frozenset({"POST"})),
        (_JOB_DETAIL, "job-detail", frozenset({"GET"})),
    ):
        match = pattern.fullmatch(path)
        if match is not None:
            return name, match.groups(), methods
    return None


def _job_query(query: str) -> tuple[str | None, int]:
    try:
        values = parse_qs(query, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise ServiceError("job_query_invalid", "job query is invalid") from exc
    if set(values) - {"cursor", "limit"} or any(len(items) != 1 for items in values.values()):
        raise ServiceError("job_query_invalid", "job query is invalid")
    cursor = values.get("cursor", [None])[0]
    if cursor == "" or cursor is not None and len(cursor) > 1_024:
        raise ServiceError("job_query_invalid", "job query is invalid")
    raw_limit = values.get("limit", ["20"])[0]
    try:
        limit = int(raw_limit)
    except (TypeError, ValueError) as exc:
        raise ServiceError("job_query_invalid", "job query is invalid") from exc
    if not 1 <= limit <= 100:
        raise ServiceError("job_query_invalid", "job query is invalid")
    return cursor, limit


def dispatch(
    handler: Any,
    method: str,
    path: str,
    user: dict[str, Any] | None,
    *,
    service: EditV3Service | None = None,
) -> bool:
    """Dispatch one V3 request and send exactly one sanitized response."""

    if not isinstance(path, str):
        return False
    parsed = urlsplit(path)
    if parsed.path != _PREFIX and not parsed.path.startswith(f"{_PREFIX}/"):
        return False
    route = _route(parsed.path)
    if route is None:
        _send(handler, 404, _error_payload("not_found", "route was not found", 404))
        return True

    name, arguments, allowed_methods = route
    normalized_method = method.upper() if isinstance(method, str) else ""
    if normalized_method not in allowed_methods:
        _send(
            handler,
            405,
            _error_payload("method_not_allowed", "method is not allowed", 405),
        )
        return True

    try:
        owner = _owner(user)
        if service is None:
            raise ServiceError(
                "service_unavailable", "AI Edit V3 service is unavailable", status=503
            )
        if name == "capabilities":
            result = service.get_capabilities(owner)
            status = 200
        elif name == "platform-assets":
            result = service.list_platform_assets(owner)
            status = 200
        elif name == "audio-assets":
            result = service.list_audio_assets(owner)
            status = 200
        elif name == "voices":
            result = service.list_voices(owner)
            status = 200
        elif name == "templates":
            result = service.list_templates(owner)
            status = 200
        elif name == "uploads":
            result = service.create_upload(owner, _read_json(handler), now=_request_now(service))
            status = 201
        elif name == "upload-complete":
            body = _read_json(handler)
            if body:
                raise ServiceError("request_invalid", "complete request must be empty")
            result = service.complete_upload(owner, arguments[0], now=_request_now(service))
            status = 200
        elif name == "materials":
            body = _read_json(handler)
            if set(body) != {"upload_id"}:
                raise ServiceError("request_invalid", "material request fields are invalid")
            result = service.create_material(owner, body["upload_id"], now=_request_now(service))
            status = 201
        elif name == "quote":
            result = service.quote(owner, _read_json(handler), now=_request_now(service))
            status = 201
        elif name == "jobs" and normalized_method == "POST":
            body = _read_json(handler)
            if "quote_id" not in body:
                raise ServiceError("request_invalid", "quote_id is required")
            request = dict(body)
            quote_id = request.pop("quote_id")
            result = service.create_job(
                owner,
                request,
                quote_id,
                _idempotency_key(handler),
                now=_request_now(service),
            )
            status = 202
        elif name == "jobs":
            cursor, limit = _job_query(parsed.query)
            result = service.list_jobs(owner, cursor=cursor, limit=limit)
            status = 200
        elif name == "job-detail":
            result = service.get_job(owner, arguments[0])
            status = 200
        elif name == "job-plan":
            result = service.get_plan(owner, arguments[0])
            status = 200
        elif name == "job-result":
            result = service.get_result(owner, arguments[0])
            status = 200
        elif name == "job-retry":
            body = _read_json(handler)
            if body:
                raise ServiceError("request_invalid", "retry request must be empty")
            result = service.retry_job(
                owner,
                arguments[0],
                _idempotency_key(handler),
                now=_request_now(service),
            )
            status = 202
        else:  # pragma: no cover - exhaustive route table guard
            raise ServiceError("not_found", "route was not found", status=404)
        if not isinstance(result, Mapping):
            raise ServiceError("service_unavailable", "service response is invalid", status=503)
        _send(handler, status, result)
    except ServiceError as exc:
        status, payload, retry_after = _safe_service_error(exc)
        _send(handler, status, payload, retry_after=retry_after)
    except Exception:
        _send(
            handler,
            500,
            _error_payload("internal_error", "request failed safely", 500),
        )
    return True
