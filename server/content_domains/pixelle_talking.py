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
import sqlite3
import stat
import threading
import time
import uuid
from collections import OrderedDict

from . import video
from .core import OUT_DIR, _resolve_out_file


TALKING_CLIP_CONCURRENCY = 2
_TALKING_CLIP_SLOTS = threading.BoundedSemaphore(TALKING_CLIP_CONCURRENCY)
IMAGE_ASSET_CACHE_MAX = 256
IMAGE_ASSET_CACHE_TTL_SECONDS = 6 * 60 * 60
DEFERRED_CLEANUP_MAX = 256
PRIVATE_DIR_NAME = ".pixelle-talking-private"
PRIVATE_SWEEP_MIN_AGE_SECONDS = 24 * 60 * 60
PRIVATE_SWEEP_BATCH = 64
PRIVATE_BACKLOG_MAX = 1024
PRIVATE_CLEANUP_RETRY_SECONDS = 60
PRIVATE_CLEANUP_DB_NAME = "cleanup.sqlite3"
_IMAGE_ASSET_CACHE = OrderedDict()
_IMAGE_UPLOADS = {}
_IMAGE_CACHE_LOCK = threading.Lock()
_DEFERRED_CLEANUP = OrderedDict()
_DEFERRED_CLEANUP_LOCK = threading.Lock()
_CLEANUP_PASS_LOCK = threading.Lock()

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


class TalkingBridgeBackpressureError(RuntimeError):
    pass


class CleanupJournalIntegrityError(RuntimeError):
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


def _private_dir():
    path = pathlib.Path(OUT_DIR) / PRIVATE_DIR_NAME
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


def _cleanup_db_path():
    return _private_dir() / PRIVATE_CLEANUP_DB_NAME


def _cleanup_db_metadata_is_link(metadata):
    reparse_point = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(
        reparse_point and
        getattr(metadata, "st_file_attributes", 0) & reparse_point)


def _validate_cleanup_db_metadata(metadata):
    if _cleanup_db_metadata_is_link(metadata):
        raise OSError("private cleanup journal must not be a link")
    if not stat.S_ISREG(metadata.st_mode):
        raise OSError("private cleanup journal must be a regular file")
    if getattr(metadata, "st_nlink", 1) != 1:
        raise OSError("private cleanup journal must have exactly one link")
    get_effective_uid = getattr(os, "geteuid", None)
    if get_effective_uid is not None and hasattr(metadata, "st_uid"):
        if metadata.st_uid != get_effective_uid():
            raise PermissionError("private cleanup journal has an unexpected owner")
    if os.name != "nt" and stat.S_IMODE(metadata.st_mode) != 0o600:
        raise PermissionError("private cleanup journal must have mode 0600")


def _cleanup_db_identity(metadata):
    return (
        getattr(metadata, "st_dev", None),
        getattr(metadata, "st_ino", None),
    )


def _validate_cleanup_db_identity(path, descriptor):
    try:
        descriptor_metadata = os.fstat(descriptor)
        _validate_cleanup_db_metadata(descriptor_metadata)
        path_metadata = path.lstat()
        _validate_cleanup_db_metadata(path_metadata)
        if (_cleanup_db_identity(descriptor_metadata) !=
                _cleanup_db_identity(path_metadata)):
            raise OSError("private cleanup journal identity changed")
    except Exception as error:
        raise CleanupJournalIntegrityError(
            "private cleanup journal identity changed") from error


def _secure_cleanup_db_descriptor(path):
    flags = os.O_RDWR | getattr(os, "O_BINARY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    created = False
    try:
        descriptor = os.open(
            str(path), flags | nofollow | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
    except FileExistsError:
        existing = path.lstat()
        _validate_cleanup_db_metadata(existing)
        descriptor = os.open(str(path), flags | nofollow)

    try:
        if created:
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            else:
                os.chmod(path, 0o600)
        metadata = os.fstat(descriptor)
        _validate_cleanup_db_metadata(metadata)
        path_metadata = path.lstat()
        _validate_cleanup_db_metadata(path_metadata)
        if (_cleanup_db_identity(metadata) !=
                _cleanup_db_identity(path_metadata)):
            raise OSError("private cleanup journal identity changed")
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _open_cleanup_db():
    path = _cleanup_db_path()
    descriptor = _secure_cleanup_db_descriptor(path)
    try:
        connection = sqlite3.connect(str(path), timeout=5)
        try:
            _validate_cleanup_db_identity(path, descriptor)
        except Exception:
            connection.close()
            raise
    finally:
        os.close(descriptor)
    try:
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS bridge_artifacts (
                path TEXT PRIMARY KEY,
                created_at REAL NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('active', 'pending')),
                cleanup_requested_at REAL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL NOT NULL DEFAULT 0
            )
        """)
        connection.execute("""
            CREATE INDEX IF NOT EXISTS bridge_artifacts_due
            ON bridge_artifacts(next_attempt_at, path)
        """)
        connection.commit()
        return connection
    except Exception:
        connection.close()
        raise


def _windows_cleanup_name_is_unsafe(name):
    if name.endswith((".", " ")) or ":" in name:
        return True
    stem = name.split(".", 1)[0].casefold()
    if stem in {"con", "prn", "aux", "nul", "clock$", "conin$", "conout$"}:
        return True
    return bool(re.fullmatch(r"(?:com|lpt)[1-9\u00b9\u00b2\u00b3]", stem))


def _allowed_cleanup_artifact_name(name, private_root):
    if not isinstance(name, str) or not name or "\x00" in name:
        return None
    if (pathlib.PurePosixPath(name).name != name or
            pathlib.PureWindowsPath(name).name != name):
        return None
    if _windows_cleanup_name_is_unsafe(name):
        return None
    normalized = name.casefold()
    cleanup_db_name = PRIVATE_CLEANUP_DB_NAME.casefold()
    if (normalized == cleanup_db_name or
            normalized.startswith(cleanup_db_name + "-")):
        return None
    try:
        candidate = private_root / name
        if candidate.resolve().parent != private_root:
            return None
    except (OSError, RuntimeError):
        return None
    return name


def _private_artifact_name(path):
    private_root = _private_dir().resolve()
    candidate = pathlib.Path(path)
    try:
        if candidate.parent.resolve() != private_root:
            return None
    except (OSError, RuntimeError):
        return None
    return _allowed_cleanup_artifact_name(candidate.name, private_root)


def _journal_register(path):
    name = _private_artifact_name(path)
    if name is None:
        raise ValueError("bridge artifacts must stay in the private namespace")
    connection = _open_cleanup_db()
    try:
        connection.execute("BEGIN IMMEDIATE")
        backlog = connection.execute(
            "SELECT COUNT(*) FROM bridge_artifacts").fetchone()[0]
        if backlog >= max(0, PRIVATE_BACKLOG_MAX):
            raise TalkingBridgeBackpressureError("private artifact backlog limit reached")
        created_at = time.time()
        connection.execute("""
            INSERT INTO bridge_artifacts(
                path, created_at, status, next_attempt_at
            ) VALUES (?, ?, 'active', ?)
        """, (
            name,
            created_at,
            created_at + max(0, PRIVATE_SWEEP_MIN_AGE_SECONDS),
        ))
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _journal_mark_pending(path):
    name = _private_artifact_name(path)
    if name is None:
        return False
    now = time.time()
    connection = _open_cleanup_db()
    try:
        connection.execute("""
            INSERT INTO bridge_artifacts(
                path, created_at, status, cleanup_requested_at, next_attempt_at
            ) VALUES (?, ?, 'pending', ?, 0)
            ON CONFLICT(path) DO UPDATE SET
                status='pending', cleanup_requested_at=excluded.cleanup_requested_at,
                next_attempt_at=0
        """, (name, now, now))
        connection.commit()
        return True
    finally:
        connection.close()


def _journal_forget(path):
    name = _private_artifact_name(path)
    if name is None:
        return
    connection = _open_cleanup_db()
    try:
        connection.execute("DELETE FROM bridge_artifacts WHERE path=?", (name,))
        connection.commit()
    finally:
        connection.close()


def _journal_note_failure(path):
    name = _private_artifact_name(path)
    if name is None:
        return
    now = time.time()
    connection = _open_cleanup_db()
    try:
        connection.execute("""
            UPDATE bridge_artifacts
            SET status='pending', attempts=attempts+1,
                next_attempt_at=?
            WHERE path=?
        """, (now + max(0, PRIVATE_CLEANUP_RETRY_SECONDS), name))
        connection.commit()
    finally:
        connection.close()


def _journal_cleanup_candidates(now):
    connection = _open_cleanup_db()
    try:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute("""
            SELECT path FROM bridge_artifacts
            WHERE next_attempt_at <= ?
            ORDER BY next_attempt_at, path
            LIMIT ?
        """, (
            now,
            max(0, PRIVATE_SWEEP_BATCH),
        )).fetchall()
        private_root = _private_dir().resolve()
        candidates = []
        invalid = []
        for row in rows:
            name = row[0]
            if _allowed_cleanup_artifact_name(name, private_root) is None:
                invalid.append((name,))
            else:
                candidates.append(name)
        if invalid:
            connection.executemany(
                "DELETE FROM bridge_artifacts WHERE path=?", invalid)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    if invalid:
        raise CleanupJournalIntegrityError(
            "private cleanup journal contains invalid artifact rows")
    return candidates


def _validate_cleanup_journal_integrity():
    connection = _open_cleanup_db()
    try:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            "SELECT path FROM bridge_artifacts").fetchall()
        private_root = _private_dir().resolve()
        invalid = [
            (row[0],) for row in rows
            if _allowed_cleanup_artifact_name(row[0], private_root) is None
        ]
        if invalid:
            connection.executemany(
                "DELETE FROM bridge_artifacts WHERE path=?", invalid)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    if invalid:
        raise CleanupJournalIntegrityError(
            "private cleanup journal contains invalid artifact rows")


def _private_backlog_size():
    connection = _open_cleanup_db()
    try:
        return connection.execute(
            "SELECT COUNT(*) FROM bridge_artifacts").fetchone()[0]
    finally:
        connection.close()


def _enforce_backlog_admission():
    try:
        backlog = _private_backlog_size()
    except TalkingBridgeBackpressureError:
        raise
    except Exception as error:
        raise TalkingBridgeBackpressureError(
            "private cleanup journal unavailable") from error
    if backlog >= max(0, PRIVATE_BACKLOG_MAX):
        raise TalkingBridgeBackpressureError("private artifact backlog limit reached")


def _allocate_private_file(suffix, kind):
    path = _private_dir() / ("%s-%s%s" % (kind, uuid.uuid4().hex, suffix))
    try:
        _journal_register(path)
    except TalkingBridgeBackpressureError:
        raise
    except Exception as error:
        raise TalkingBridgeBackpressureError(
            "private cleanup journal unavailable") from error
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    try:
        fd = os.open(str(path), flags, 0o600)
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        else:
            os.chmod(path, 0o600)
        return fd, path
    except Exception:
        try:
            _journal_forget(path)
        except Exception:
            pass
        raise


def _write_private_temp(data, suffix, kind="input"):
    fd, path = _allocate_private_file(suffix, kind)
    try:
        with os.fdopen(fd, "wb") as output:
            fd = -1
            output.write(data)
        return path
    except Exception:
        if fd >= 0:
            os.close(fd)
        _best_effort_cleanup([path], "artifact-create")
        raise


def _new_private_temp(suffix, kind="derivative"):
    fd, path = _allocate_private_file(suffix, kind)
    os.close(fd)
    return path


def _relative_temp_name(path):
    return path.relative_to(pathlib.Path(OUT_DIR)).as_posix()


def _remember_deferred_cleanup(path):
    marker = str(path)
    evicted = None
    with _DEFERRED_CLEANUP_LOCK:
        _DEFERRED_CLEANUP[marker] = pathlib.Path(path)
        _DEFERRED_CLEANUP.move_to_end(marker)
        while len(_DEFERRED_CLEANUP) > DEFERRED_CLEANUP_MAX:
            _marker, evicted = _DEFERRED_CLEANUP.popitem(last=False)
    return evicted


def _forget_deferred_cleanup(path):
    with _DEFERRED_CLEANUP_LOCK:
        _DEFERRED_CLEANUP.pop(str(path), None)


def _best_effort_log_failure(error, request_id):
    try:
        _log_failure(error, request_id)
    except Exception:
        pass


def _best_effort_cleanup(paths, request_id):
    seen = set()
    for value in paths:
        if value is None:
            continue
        path = pathlib.Path(value)
        marker = str(path)
        if marker in seen:
            continue
        seen.add(marker)
        try:
            _journal_mark_pending(path)
        except Exception as error:
            _best_effort_log_failure(error, request_id)
        try:
            path.unlink(missing_ok=True)
        except Exception as error:
            try:
                evicted = _remember_deferred_cleanup(path)
                if evicted is not None:
                    _best_effort_log_failure(
                        RuntimeError(
                            "deferred cleanup memory limit reached; "
                            "private sweep retains discovery"),
                        request_id,
                    )
            except Exception as registry_error:
                _best_effort_log_failure(registry_error, request_id)
            _best_effort_log_failure(error, request_id)
        else:
            try:
                _journal_forget(path)
            except Exception as error:
                _best_effort_log_failure(error, request_id)
            try:
                _forget_deferred_cleanup(path)
            except Exception:
                pass


def _retry_deferred_cleanup(request_id):
    return _sweep_private_cleanup(request_id)


def _sweep_private_cleanup(request_id):
    try:
        _validate_cleanup_journal_integrity()
    except CleanupJournalIntegrityError as error:
        _best_effort_log_failure(error, request_id)
        raise TalkingBridgeBackpressureError(
            "private cleanup journal integrity violation") from error
    except Exception as error:
        _best_effort_log_failure(error, request_id)
        return 0
    if not _CLEANUP_PASS_LOCK.acquire(blocking=False):
        return 0
    try:
        try:
            private_dir = _private_dir()
            candidates = _journal_cleanup_candidates(time.time())
        except CleanupJournalIntegrityError as error:
            _best_effort_log_failure(error, request_id)
            raise TalkingBridgeBackpressureError(
                "private cleanup journal integrity violation") from error
        except Exception as error:
            _best_effort_log_failure(error, request_id)
            return 0

        deleted = 0
        for name in candidates:
            path = private_dir / name
            try:
                path.unlink(missing_ok=True)
            except Exception as error:
                try:
                    _remember_deferred_cleanup(path)
                    _journal_note_failure(path)
                except Exception as registry_error:
                    _best_effort_log_failure(registry_error, request_id)
                _best_effort_log_failure(error, request_id)
            else:
                deleted += 1
                try:
                    _journal_forget(path)
                    _forget_deferred_cleanup(path)
                except Exception as error:
                    _best_effort_log_failure(error, request_id)
        return deleted
    finally:
        _CLEANUP_PASS_LOCK.release()


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


def _resolve_image_asset(image_sha256, image_path, request_id):
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
        derivatives = []
        with _TALKING_CLIP_SLOTS:
            provider_image, derivatives = _prepare_provider_image(
                image_path, request_id)
            try:
                asset_id = str(video.upload_heygen_image_asset(
                    _relative_temp_name(provider_image)) or "").strip()
            finally:
                _best_effort_cleanup(derivatives, request_id)
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


def _generate_with_image_asset(validated, image_path, audio_path, image_asset_id,
                               output_path):
    return video.generate_heygen_video(
        _relative_temp_name(image_path),
        _relative_temp_name(audio_path),
        validated["resolution"],
        validated["ratio"],
        validated["motion"],
        image_asset_id=image_asset_id,
        internal=True,
        internal_output_file=_relative_temp_name(output_path),
    )


def _result_image_asset(result):
    effective_asset_id = str(result.get("image_asset_id") or "").strip()
    if not effective_asset_id:
        if result.get("video_id"):
            raise video.HeyGenBilledError(
                "provider result omitted image_asset_id after video creation")
        raise RuntimeError("provider result omitted image_asset_id")
    return effective_asset_id


def _prepare_provider_image(image_path, request_id):
    derivatives = []
    provider_image = image_path
    try:
        if image_path.suffix.lower() not in video.HEYGEN_IMAGE_EXTS:
            provider_image = _new_private_temp(".jpg", "derivative-image")
            derivatives.append(provider_image)
            provider_image = video._ensure_heygen_image_jpg(
                image_path, output_path=provider_image)
        return provider_image, derivatives
    except Exception:
        _best_effort_cleanup(derivatives, request_id)
        raise


def _prepare_provider_audio(audio_path, request_id):
    derivatives = []
    provider_audio = audio_path
    try:
        if audio_path.suffix.lower() != ".mp3":
            provider_audio = _new_private_temp(".mp3", "derivative-audio")
            derivatives.append(provider_audio)
            provider_audio = video._ensure_heygen_audio_mp3(
                audio_path, output_path=provider_audio)
        return provider_audio, derivatives
    except Exception:
        _best_effort_cleanup(derivatives, request_id)
        raise


def generate_clip(payload):
    validated = _validated_payload(payload)
    request_id = validated["request_id"]
    _sweep_private_cleanup(request_id)
    _enforce_backlog_admission()
    image_path = _write_private_temp(
        validated["image_data"], validated["image_suffix"], "input-image")
    audio_path = None
    result = None
    try:
        audio_path = _write_private_temp(
            validated["audio_data"], validated["audio_suffix"], "input-audio")
        image_sha256 = validated["image_sha256"]
        image_asset_id = _resolve_image_asset(
            image_sha256, image_path, request_id)
        try:
            with _TALKING_CLIP_SLOTS:
                provider_audio, derivatives = _prepare_provider_audio(
                    audio_path, request_id)
                provider_output = None
                output_transferred = False
                try:
                    provider_output = _new_private_temp(
                        ".mp4", "result-video")
                    result = _generate_with_image_asset(
                        validated, image_path, provider_audio, image_asset_id,
                        provider_output)
                    result = _adopt_result_artifacts(result, request_id)
                    resolved_output = _resolve_result_artifact(
                        result.get("video_file"))
                    output_transferred = (
                        resolved_output is not None and
                        resolved_output.resolve() == provider_output.resolve()
                    )
                finally:
                    _best_effort_cleanup(derivatives, request_id)
                    if provider_output is not None and not output_transferred:
                        _best_effort_cleanup([provider_output], request_id)
        except video.HeyGenBilledError:
            raise
        except Exception:
            _evict_image_asset(image_sha256, image_asset_id)
            raise
        try:
            effective_asset_id = _result_image_asset(result)
            _cache_image_asset(image_sha256, effective_asset_id)
        except video.HeyGenBilledError:
            _cleanup_result_artifacts(result, request_id=request_id)
            raise
        except Exception as error:
            _cleanup_result_artifacts(result, request_id=request_id)
            if result.get("video_id"):
                raise video.HeyGenBilledError(
                    "post-generation result validation failed after video creation") from error
            raise
        return result
    finally:
        _best_effort_cleanup([audio_path, image_path], request_id)


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
    if isinstance(error, TalkingBridgeBackpressureError):
        return {
            "code": "talking_bridge_backpressure",
            "detail": "talking clip bridge is temporarily unavailable",
            "retryable": True,
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
    if isinstance(error, TalkingBridgeBackpressureError):
        return 503
    return 502


def resolve_video_path(result):
    path = _resolve_result_artifact(result.get("video_file"))
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


def _resolve_private_artifact(value):
    rel = str(value or "").replace("\\", "/").lstrip("/")
    if not rel.startswith(PRIVATE_DIR_NAME + "/"):
        return None
    try:
        path = (pathlib.Path(OUT_DIR) / rel).resolve()
        path.relative_to(_private_dir().resolve())
    except Exception:
        return None
    return path if path.is_file() else None


def _resolve_result_artifact(value):
    return _resolve_private_artifact(value) or _resolve_out_file(value)


def _adopt_result_artifacts(result, request_id):
    if not isinstance(result, dict):
        return result

    resolved = []
    for key, kind in (("video_file", "video"), ("image_file", "cover")):
        source = _resolve_result_artifact(result.get(key))
        if source is not None:
            resolved.append((key, kind, source))

    adopted = []
    try:
        private_root = _private_dir().resolve()
        for key, kind, source in resolved:
            source = source.resolve()
            if source.parent == private_root:
                destination = source
            else:
                suffix = source.suffix or (".mp4" if kind == "video" else ".bin")
                destination = _new_private_temp(suffix, "result-%s" % kind)
                os.replace(source, destination)
                adopted.append(destination)
            os.chmod(destination, 0o600)
            result[key] = _relative_temp_name(destination)
        result.pop("video_url", None)
        result.pop("image_url", None)
        return result
    except Exception as error:
        _best_effort_cleanup(
            adopted + [source for _key, _kind, source in resolved], request_id)
        if result.get("video_id"):
            raise video.HeyGenBilledError(
                "provider result artifact adoption failed after video creation") from error
        raise


def _cleanup_result_artifacts(result, video_path=None, request_id="cleanup"):
    paths = []
    if video_path is not None:
        paths.append(pathlib.Path(video_path))
    if isinstance(result, dict):
        for key in ("video_file", "image_file"):
            try:
                path = _resolve_result_artifact(result.get(key))
            except Exception as error:
                _best_effort_log_failure(error, request_id)
                continue
            if path is not None:
                paths.append(pathlib.Path(path))
    _best_effort_cleanup(paths, request_id)


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
        _cleanup_result_artifacts(result, video_path, request_id)
        _best_effort_log_failure(error, request_id)
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
        _best_effort_log_failure(error, request_id)
        raise
    finally:
        _cleanup_result_artifacts(result, video_path, request_id)
