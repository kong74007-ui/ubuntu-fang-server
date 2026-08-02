from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Any, Mapping

from .providers import SubmissionUnknown


class SourceError(ValueError):
    pass


@dataclass(frozen=True)
class PreparedSource:
    input_type: str
    authoritative_text: str | None
    media: Any
    source_asset_id: str | None
    source_upload_id: str | None
    provider_request_id: str | None
    source_fingerprint: str


def _fingerprint(
    *,
    input_type: str,
    authoritative_text: str | None,
    media: Any,
    source_asset_id: str | None,
    source_upload_id: str | None,
) -> str:
    payload = {
        "authoritative_text_sha256": (
            None
            if authoritative_text is None
            else sha256(authoritative_text.encode("utf-8")).hexdigest()
        ),
        "input_type": input_type,
        "media_sha256": str(getattr(media, "sha256", "")),
        "source_asset_id": source_asset_id,
        "source_upload_id": source_upload_id,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _prepare_existing_source(job: Mapping[str, Any], deps: Any, context: Any) -> PreparedSource:
    input_type = str(job["input_type"])
    owner = str(job["owner"])
    repository_name, identifier_field = {
        "platform_talking_head": ("platform_assets", "source_asset_id"),
        "uploaded_video": ("uploads", "source_upload_id"),
        "existing_audio": ("audio_assets", "source_asset_id"),
        "uploaded_audio": ("uploads", "source_upload_id"),
    }[input_type]
    identifier = str(job[identifier_field])
    repository = getattr(deps, repository_name)
    record = repository.get_for_owner(owner, identifier)
    if not isinstance(record, Mapping) or record.get("status") != "completed":
        raise SourceError("source_not_found")
    expected_identifier = record.get("asset_id" if identifier_field == "source_asset_id" else "upload_id")
    if expected_identifier != identifier:
        raise SourceError("source_not_found")
    authoritative_text: str | None = None
    if input_type == "platform_talking_head":
        raw_text = record.get("authoritative_text")
        if not isinstance(raw_text, str) or not raw_text.strip():
            raise SourceError("authoritative_text_missing")
        authoritative_text = raw_text
    media = deps.media.prepare(
        dict(record),
        input_type=input_type,
        deadline_at=context.deadline_at,
    )
    source_asset_id = identifier if identifier_field == "source_asset_id" else None
    source_upload_id = identifier if identifier_field == "source_upload_id" else None
    return PreparedSource(
        input_type=input_type,
        authoritative_text=authoritative_text,
        media=media,
        source_asset_id=source_asset_id,
        source_upload_id=source_upload_id,
        provider_request_id=None,
        source_fingerprint=_fingerprint(
            input_type=input_type,
            authoritative_text=authoritative_text,
            media=media,
            source_asset_id=source_asset_id,
            source_upload_id=source_upload_id,
        ),
    )


def _tts_result_value(result: Any, name: str) -> Any:
    if hasattr(result, name):
        return getattr(result, name)
    payload = getattr(result, "payload", None)
    if isinstance(payload, Mapping):
        return payload.get(name)
    return None


def _safe_tts_result(result: Any, external_id: str) -> dict[str, Any]:
    media = _tts_result_value(result, "media")
    media_sha256 = getattr(media, "sha256", None)
    if isinstance(media, Mapping):
        media_sha256 = media.get("sha256")
    timestamps = _tts_result_value(result, "timestamps")
    timestamp_count = len(timestamps) if isinstance(timestamps, (list, tuple)) else 0
    return {
        "external_id": external_id,
        "media_sha256": media_sha256 if isinstance(media_sha256, str) else None,
        "status": "completed",
        "timestamp_count": timestamp_count,
    }


def _prepare_tts_source(job: Mapping[str, Any], deps: Any, context: Any) -> PreparedSource:
    owner = str(job["owner"])
    voice_id = str(job["voice_id"])
    voice = deps.voices.get_active_for_owner(owner, voice_id)
    if voice is None:
        raise SourceError("voice_not_found")
    text = str(job["authoritative_text"])
    if not text.strip():
        raise SourceError("authoritative_text_missing")
    operation_key = f"ai-edit-v3:{job['id']}:tts"
    request_payload = {
        "owner": owner,
        "text_sha256": sha256(text.encode("utf-8")).hexdigest(),
        "voice_id": voice_id,
    }
    request_sha256 = sha256(
        json.dumps(
            request_payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    existing = deps.provider_tasks.record_intent(
        operation_key=operation_key,
        request_sha256=request_sha256,
        provider="website_tts",
        capability="tts",
        context=context,
    )
    external_id = existing.get("external_id") if isinstance(existing, Mapping) else None
    if isinstance(external_id, str) and external_id:
        result = deps.tts.query(external_id, deadline_at=context.deadline_at)
    else:
        try:
            result = deps.tts.submit(
                owner=owner,
                text=text,
                voice_id=voice_id,
                idempotency_key=operation_key,
                deadline_at=context.deadline_at,
            )
        except SubmissionUnknown as exc:
            deps.provider_tasks.mark_unknown(
                operation_key,
                reason_code=exc.reason_code,
                context=context,
            )
            raise
        external_id = _tts_result_value(result, "request_id")
        if not isinstance(external_id, str) or not external_id:
            raise SourceError("tts_request_id_missing")
        deps.provider_tasks.bind_result(
            operation_key=operation_key,
            external_id=external_id,
            result=_safe_tts_result(result, external_id),
            context=context,
        )
    media = _tts_result_value(result, "media")
    if media is None:
        prepare_tts = getattr(getattr(deps, "media", None), "prepare_tts", None)
        if not callable(prepare_tts):
            raise SourceError("tts_media_missing")
        media = prepare_tts(result, input_type="script_to_audio_video", deadline_at=context.deadline_at)
    return PreparedSource(
        input_type="script_to_audio_video",
        authoritative_text=text,
        media=media,
        source_asset_id=None,
        source_upload_id=None,
        provider_request_id=external_id,
        source_fingerprint=_fingerprint(
            input_type="script_to_audio_video",
            authoritative_text=text,
            media=media,
            source_asset_id=None,
            source_upload_id=None,
        ),
    )


def _normalize_stage_job(job: Mapping[str, Any]) -> dict[str, Any]:
    if "input_type" in job:
        return dict(job)
    raw_request = job.get("normalized_request_json")
    if isinstance(raw_request, str):
        try:
            request = json.loads(raw_request)
        except json.JSONDecodeError as exc:
            raise SourceError("normalized_request_invalid") from exc
    elif isinstance(raw_request, Mapping):
        request = dict(raw_request)
    else:
        raise SourceError("normalized_request_invalid")
    if not isinstance(request, Mapping):
        raise SourceError("normalized_request_invalid")
    normalized = dict(request)
    normalized["id"] = job.get("id", job.get("job_id"))
    normalized["owner"] = job.get("owner", job.get("owner_id"))
    tts_input = normalized.get("tts_input")
    if normalized.get("input_type") == "script_to_audio_video":
        if not isinstance(tts_input, Mapping):
            raise SourceError("tts_input_invalid")
        normalized["authoritative_text"] = tts_input.get("text")
        normalized["voice_id"] = tts_input.get("voice_id")
    if not isinstance(normalized.get("id"), str) or not normalized["id"]:
        raise SourceError("job_id_invalid")
    if not isinstance(normalized.get("owner"), str) or not normalized["owner"]:
        raise SourceError("owner_invalid")
    return normalized


def prepare_source(job: Mapping[str, Any], deps: Any, context: Any) -> PreparedSource:
    job = _normalize_stage_job(job)
    input_type = str(job["input_type"])
    if input_type in {
        "platform_talking_head",
        "uploaded_video",
        "existing_audio",
        "uploaded_audio",
    }:
        return _prepare_existing_source(job, deps, context)
    if input_type != "script_to_audio_video":
        raise SourceError("input_type_invalid")
    return _prepare_tts_source(job, deps, context)
