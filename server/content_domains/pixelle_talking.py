# -*- coding: utf-8 -*-
import base64
import binascii
import hashlib
import hmac
import ipaddress
import json
import os
import pathlib
import re
import tempfile
import threading
import time
from collections import OrderedDict

from . import video
from .core import OUT_DIR, _resolve_out_file


TALKING_CLIP_CONCURRENCY = 2
_TALKING_CLIP_SLOTS = threading.BoundedSemaphore(TALKING_CLIP_CONCURRENCY)
IMAGE_ASSET_CACHE_MAX = 256
IMAGE_ASSET_CACHE_TTL_SECONDS = 6 * 60 * 60
_IMAGE_ASSET_CACHE = OrderedDict()
_IMAGE_UPLOADS = {}
_IMAGE_CACHE_LOCK = threading.Lock()

_MAX_INPUT_BYTES = 35 * 1024 * 1024
_IMAGE_MIME_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
_AUDIO_MIME_EXTENSIONS = {
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/mp4": ".m4a",
    "audio/m4a": ".m4a",
    "audio/x-m4a": ".m4a",
}


class InternalTalkingAuthError(RuntimeError):
    pass


class TalkingPayloadError(ValueError):
    pass


class _ImageUpload:
    def __init__(self):
        self.event = threading.Event()
        self.asset_id = None
        self.error = None


def validate_internal_token(provided, expected):
    provided = str(provided or "")
    expected = str(expected or "")
    if not expected or not provided or not hmac.compare_digest(provided, expected):
        raise InternalTalkingAuthError("invalid internal Pixelle token")


def validate_loopback_address(address):
    try:
        if ipaddress.ip_address(str(address or "")).is_loopback:
            return
    except ValueError:
        pass
    raise InternalTalkingAuthError("internal Pixelle route is loopback-only")


def _decode_data_url(value, field, mime_extensions):
    if not isinstance(value, str):
        raise TalkingPayloadError("%s must be a base64 data URL" % field)
    match = re.fullmatch(r"data:([^;,]+);base64,(.*)", value.strip(), re.IGNORECASE | re.DOTALL)
    if not match:
        raise TalkingPayloadError("%s must be a base64 data URL" % field)
    mime = match.group(1).lower()
    if mime not in mime_extensions:
        raise TalkingPayloadError("%s has unsupported media type" % field)
    try:
        data = base64.b64decode(match.group(2), validate=True)
    except (binascii.Error, ValueError):
        raise TalkingPayloadError("%s contains invalid base64" % field)
    if not data:
        raise TalkingPayloadError("%s is empty" % field)
    if len(data) > _MAX_INPUT_BYTES:
        raise TalkingPayloadError("%s exceeds the 35 MiB limit" % field)
    return data, mime_extensions[mime]


def _validated_payload(payload):
    if not isinstance(payload, dict):
        raise TalkingPayloadError("request body must be a JSON object")
    request_id = str(payload.get("request_id") or "").strip()
    if not request_id or len(request_id) > 200:
        raise TalkingPayloadError("request_id is required and must be at most 200 characters")

    image_data, image_suffix = _decode_data_url(
        payload.get("image_data"), "image_data", _IMAGE_MIME_EXTENSIONS)
    audio_data, audio_suffix = _decode_data_url(
        payload.get("audio_data"), "audio_data", _AUDIO_MIME_EXTENSIONS)

    image_sha256 = str(payload.get("image_sha256") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", image_sha256):
        raise TalkingPayloadError("image_sha256 must be 64 lowercase hexadecimal characters")
    actual_sha256 = hashlib.sha256(image_data).hexdigest()
    if not hmac.compare_digest(actual_sha256, image_sha256):
        raise TalkingPayloadError("image_sha256 does not match image_data")

    resolution = str(payload.get("resolution") or "1080p").strip().lower()
    ratio = str(payload.get("ratio") or "9:16").strip()
    motion = str(payload.get("motion") or "medium").strip().lower()
    if resolution not in video.VALID_VIDEO_RESOLUTIONS:
        raise TalkingPayloadError("resolution must be 720p or 1080p")
    if ratio not in video.VALID_VIDEO_RATIOS:
        raise TalkingPayloadError("ratio is not supported")
    if motion not in video.VALID_VIDEO_MOTIONS:
        raise TalkingPayloadError("motion must be low, medium, or high")
    return {
        "request_id": request_id,
        "image_data": image_data,
        "image_suffix": image_suffix,
        "audio_data": audio_data,
        "audio_suffix": audio_suffix,
        "image_sha256": image_sha256,
        "resolution": resolution,
        "ratio": ratio,
        "motion": motion,
    }


def _write_private_temp(data, suffix):
    pathlib.Path(OUT_DIR).mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(
        prefix=".pixelle-talking-", suffix=suffix, dir=str(OUT_DIR))
    path = pathlib.Path(name)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        else:
            os.chmod(path, 0o600)
        with os.fdopen(fd, "wb") as output:
            fd = -1
            output.write(data)
        return path
    except Exception:
        if fd >= 0:
            os.close(fd)
        path.unlink(missing_ok=True)
        raise


def _new_private_temp(suffix):
    pathlib.Path(OUT_DIR).mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(
        prefix=".pixelle-talking-", suffix=suffix, dir=str(OUT_DIR))
    path = pathlib.Path(name)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        else:
            os.chmod(path, 0o600)
    finally:
        os.close(fd)
    return path


def _relative_temp_name(path):
    return path.relative_to(pathlib.Path(OUT_DIR)).as_posix()


def _prune_image_cache_locked(now):
    stale = [key for key, (_asset_id, expires_at) in _IMAGE_ASSET_CACHE.items()
             if expires_at <= now]
    for key in stale:
        _IMAGE_ASSET_CACHE.pop(key, None)


def _get_cached_image_asset(image_sha256):
    now = time.monotonic()
    with _IMAGE_CACHE_LOCK:
        _prune_image_cache_locked(now)
        entry = _IMAGE_ASSET_CACHE.get(image_sha256)
        if entry is None:
            return None
        _IMAGE_ASSET_CACHE.move_to_end(image_sha256)
        return entry[0]


def _cache_image_asset(image_sha256, image_asset_id):
    now = time.monotonic()
    with _IMAGE_CACHE_LOCK:
        _prune_image_cache_locked(now)
        _IMAGE_ASSET_CACHE[image_sha256] = (
            image_asset_id, now + IMAGE_ASSET_CACHE_TTL_SECONDS)
        _IMAGE_ASSET_CACHE.move_to_end(image_sha256)
        while len(_IMAGE_ASSET_CACHE) > IMAGE_ASSET_CACHE_MAX:
            _IMAGE_ASSET_CACHE.popitem(last=False)


def _evict_image_asset(image_sha256, image_asset_id):
    with _IMAGE_CACHE_LOCK:
        entry = _IMAGE_ASSET_CACHE.get(image_sha256)
        if entry is not None and entry[0] == image_asset_id:
            _IMAGE_ASSET_CACHE.pop(image_sha256, None)


def _resolve_image_asset(image_sha256, image_path):
    cached = _get_cached_image_asset(image_sha256)
    if cached:
        return cached

    with _IMAGE_CACHE_LOCK:
        _prune_image_cache_locked(time.monotonic())
        entry = _IMAGE_ASSET_CACHE.get(image_sha256)
        if entry is not None:
            _IMAGE_ASSET_CACHE.move_to_end(image_sha256)
            return entry[0]
        upload = _IMAGE_UPLOADS.get(image_sha256)
        owner = upload is None
        if owner:
            upload = _ImageUpload()
            _IMAGE_UPLOADS[image_sha256] = upload

    if not owner:
        upload.event.wait()
        if upload.error is not None:
            raise upload.error
        return upload.asset_id

    try:
        asset_id = str(video.upload_heygen_image_asset(
            _relative_temp_name(image_path)) or "").strip()
        if not asset_id:
            raise RuntimeError("provider image upload omitted asset ID")
        _cache_image_asset(image_sha256, asset_id)
        upload.asset_id = asset_id
        return asset_id
    except Exception as error:
        upload.error = error
        raise
    finally:
        with _IMAGE_CACHE_LOCK:
            if _IMAGE_UPLOADS.get(image_sha256) is upload:
                _IMAGE_UPLOADS.pop(image_sha256, None)
            upload.event.set()


def _generate_with_image_asset(validated, image_path, audio_path, image_asset_id):
    return video.generate_heygen_video(
        _relative_temp_name(image_path),
        _relative_temp_name(audio_path),
        validated["resolution"],
        validated["ratio"],
        validated["motion"],
        image_asset_id=image_asset_id,
        internal=True,
    )


def _result_image_asset(result):
    effective_asset_id = str(result.get("image_asset_id") or "").strip()
    if not effective_asset_id:
        if result.get("video_id"):
            raise video.HeyGenBilledError(
                "provider result omitted image_asset_id after video creation")
        raise RuntimeError("provider result omitted image_asset_id")
    return effective_asset_id


def _prepare_provider_media(image_path, audio_path):
    derivatives = []
    provider_image = image_path
    provider_audio = audio_path
    try:
        if image_path.suffix.lower() not in video.HEYGEN_IMAGE_EXTS:
            provider_image = _new_private_temp(".jpg")
            derivatives.append(provider_image)
            provider_image = video._ensure_heygen_image_jpg(
                image_path, output_path=provider_image)
        if audio_path.suffix.lower() != ".mp3":
            provider_audio = _new_private_temp(".mp3")
            derivatives.append(provider_audio)
            provider_audio = video._ensure_heygen_audio_mp3(
                audio_path, output_path=provider_audio)
        return provider_image, provider_audio, derivatives
    except Exception:
        for path in derivatives:
            path.unlink(missing_ok=True)
        raise


def generate_clip(payload):
    validated = _validated_payload(payload)
    image_path = _write_private_temp(
        validated["image_data"], validated["image_suffix"])
    audio_path = None
    derivatives = []
    try:
        audio_path = _write_private_temp(
            validated["audio_data"], validated["audio_suffix"])
        provider_image, provider_audio, derivatives = _prepare_provider_media(
            image_path, audio_path)
        image_sha256 = validated["image_sha256"]
        image_asset_id = _resolve_image_asset(image_sha256, provider_image)
        try:
            with _TALKING_CLIP_SLOTS:
                result = _generate_with_image_asset(
                    validated, provider_image, provider_audio, image_asset_id)
        except video.HeyGenBilledError:
            raise
        except Exception:
            _evict_image_asset(image_sha256, image_asset_id)
            raise
        effective_asset_id = _result_image_asset(result)
        _cache_image_asset(image_sha256, effective_asset_id)
        return result
    finally:
        for path in derivatives:
            path.unlink(missing_ok=True)
        if audio_path is not None:
            audio_path.unlink(missing_ok=True)
        image_path.unlink(missing_ok=True)


def classify_error(error):
    if isinstance(error, InternalTalkingAuthError):
        return {
            "code": "internal_auth",
            "detail": "internal authentication failed",
            "retryable": False,
            "billed": False,
        }
    if isinstance(error, (TalkingPayloadError, ValueError, json.JSONDecodeError)):
        return {
            "code": "invalid_request",
            "detail": "invalid talking clip request",
            "retryable": False,
            "billed": False,
        }
    if isinstance(error, video.HeyGenBilledError):
        return {
            "code": "heygen_billed",
            "detail": "provider video was created but delivery failed",
            "retryable": False,
            "billed": True,
        }
    return {
        "code": "talking_bridge_error",
        "detail": "talking clip generation failed",
        "retryable": True,
        "billed": False,
    }


def error_status(error):
    if isinstance(error, InternalTalkingAuthError):
        return 401
    if isinstance(error, (TalkingPayloadError, ValueError, json.JSONDecodeError)):
        return 400
    return 502


def resolve_video_path(result):
    path = _resolve_out_file(result.get("video_file"))
    if path is None:
        if result.get("video_id"):
            raise video.HeyGenBilledError(
                "completed provider video is missing from local output")
        raise RuntimeError("provider result did not include a readable video file")
    return path


def _safe_provider_header(value, name):
    value = str(value or "").strip()
    if not value or "\r" in value or "\n" in value:
        raise video.HeyGenBilledError("provider result omitted %s" % name)
    return value


def _safe_log_request_id(value):
    value = re.sub(r"[^A-Za-z0-9._:-]", "_", str(value or "-"))[:80]
    return value or "-"


def _redacted_diagnostic(error):
    detail = str(error).replace("\r", " ").replace("\n", " ")
    detail = re.sub(r"https?://[^\s]+", "<url>", detail, flags=re.IGNORECASE)
    detail = re.sub(r"\b[A-Za-z]:\\[^\s]+", "<path>", detail)
    detail = re.sub(r"(?<![A-Za-z0-9])/(?:[^/\s]+/)+[^\s]*", "<path>", detail)
    detail = re.sub(r"\b[0-9A-Za-z_-]{48,}\b", "<redacted>", detail)
    return detail[:400]


def _log_failure(error, request_id):
    print("[pixelle-talking] request_id=%s error=%s detail=%s" % (
        _safe_log_request_id(request_id), error.__class__.__name__,
        _redacted_diagnostic(error)), flush=True)


def _resolve_result_artifact(value):
    return _resolve_out_file(value)


def _cleanup_result_artifacts(result, video_path=None):
    paths = []
    if video_path is not None:
        paths.append(pathlib.Path(video_path))
    if isinstance(result, dict):
        for key in ("video_file", "image_file"):
            path = _resolve_result_artifact(result.get(key))
            if path is not None:
                paths.append(pathlib.Path(path))
    seen = set()
    for path in paths:
        marker = str(path)
        if marker in seen:
            continue
        seen.add(marker)
        try:
            path.unlink(missing_ok=True)
        except OSError as error:
            _log_failure(error, "cleanup")


def handle_http_request(handler):
    result = None
    video_path = None
    request_id = "-"
    try:
        validate_loopback_address(handler.client_address[0])
        validate_internal_token(
            handler.headers.get("X-HQ-Pixelle-Token"),
            os.environ.get("PIXELLE_TALKING_INTERNAL_TOKEN"),
        )
        payload = handler._json_body_strict()
        if isinstance(payload, dict):
            request_id = payload.get("request_id") or "-"
        result = generate_clip(payload)
        video_path = resolve_video_path(result)
        provider_video_id = _safe_provider_header(result.get("video_id"), "video_id")
        image_asset_id = _safe_provider_header(
            result.get("image_asset_id"), "image_asset_id")
    except Exception as error:
        _log_failure(error, request_id)
        _cleanup_result_artifacts(result, video_path)
        return handler._send(error_status(error), classify_error(error))
    try:
        handler.send_response(200)
        handler.send_header("Content-Type", "video/mp4")
        handler.send_header("Content-Length", str(video_path.stat().st_size))
        handler.send_header("X-Provider-Video-Id", provider_video_id)
        handler.send_header("X-Provider-Image-Asset-Id", image_asset_id)
        handler.end_headers()
        with video_path.open("rb") as source:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                handler.wfile.write(chunk)
    except Exception as error:
        _log_failure(error, request_id)
        raise
    finally:
        _cleanup_result_artifacts(result, video_path)
