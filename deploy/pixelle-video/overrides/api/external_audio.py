from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path


MAX_AUDIO_BYTES = 20 * 1024 * 1024
AUDIO_TTL_SECONDS = 24 * 60 * 60
EXTERNAL_AUDIO_ROOT = Path(os.environ.get("PIXELLE_EXTERNAL_AUDIO_ROOT", "data/external_audio"))
ASSET_ID_RE = re.compile(r"^audio_[0-9a-f]{32}$")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_LOCK = threading.RLock()


class AudioTooLargeError(ValueError):
    pass


class AudioProbeError(RuntimeError):
    pass


class AudioLeaseError(RuntimeError):
    pass


def _root() -> Path:
    root = Path(EXTERNAL_AUDIO_ROOT)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def _validate_asset_id(asset_id: str) -> str:
    value = str(asset_id or "")
    if not ASSET_ID_RE.fullmatch(value):
        raise ValueError("invalid audio asset id")
    return value


def _paths(asset_id: str) -> tuple[Path, Path]:
    value = _validate_asset_id(asset_id)
    root = _root()
    return root / f"{value}.mp3", root / f"{value}.json"


def _is_mp3(content: bytes) -> bool:
    return content.startswith(b"ID3") or (
        len(content) >= 2 and content[0] == 0xFF and (content[1] & 0xE0) == 0xE0
    )


def _atomic_write(path: Path, content: bytes) -> None:
    part = path.with_name(f".{path.name}.{uuid.uuid4().hex}.part")
    try:
        with part.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(part, 0o600)
        os.replace(part, path)
    finally:
        part.unlink(missing_ok=True)


def _write_metadata(path: Path, payload: dict) -> None:
    content = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    _atomic_write(path, content)


def _read_metadata(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as exc:
        raise FileNotFoundError("audio asset not found") from exc
    if not isinstance(payload, dict):
        raise FileNotFoundError("audio asset not found")
    return payload


def _probe_duration(path: Path) -> float:
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", str(path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        duration = float(result.stdout.strip()) if result.returncode == 0 else 0.0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise AudioProbeError("audio validation failed") from exc
    if duration <= 0:
        raise AudioProbeError("audio validation failed")
    return duration


def store_audio_asset(content: bytes, content_type: str, request_id: str) -> dict:
    if content_type != "audio/mpeg":
        raise ValueError("content type must be audio/mpeg")
    if not isinstance(content, bytes) or not content:
        raise ValueError("audio body is empty")
    if len(content) > MAX_AUDIO_BYTES:
        raise AudioTooLargeError("audio body exceeds size limit")
    if not _is_mp3(content):
        raise ValueError("invalid MP3 data")
    if not REQUEST_ID_RE.fullmatch(str(request_id or "")):
        raise ValueError("invalid request id")

    cleanup_expired_audio_assets()
    asset_id = f"audio_{uuid.uuid4().hex}"
    audio_path, metadata_path = _paths(asset_id)
    created_at = time.time()
    with _LOCK:
        _atomic_write(audio_path, content)
        try:
            duration = _probe_duration(audio_path)
            metadata = {
                "asset_id": asset_id,
                "content_type": content_type,
                "size": len(content),
                "duration": duration,
                "request_id": request_id,
                "created_at": created_at,
                "lease_task_id": None,
            }
            _write_metadata(metadata_path, metadata)
        except Exception:
            audio_path.unlink(missing_ok=True)
            metadata_path.unlink(missing_ok=True)
            raise

    return {
        "asset_id": asset_id,
        "content_type": content_type,
        "size": len(content),
        "duration": duration,
    }


def _active_asset(asset_id: str, now: float | None = None) -> tuple[Path, Path, dict]:
    audio_path, metadata_path = _paths(asset_id)
    metadata = _read_metadata(metadata_path)
    current = time.time() if now is None else now
    if current - float(metadata.get("created_at", 0)) > AUDIO_TTL_SECONDS:
        raise FileNotFoundError("audio asset not found")
    if not audio_path.is_file():
        raise FileNotFoundError("audio asset not found")
    return audio_path, metadata_path, metadata


def resolve_audio_asset(asset_id: str) -> Path:
    with _LOCK:
        audio_path, _, _ = _active_asset(asset_id)
        return audio_path


def lease_audio_assets(asset_ids: list[str], task_id: str) -> list[Path]:
    if not TASK_ID_RE.fullmatch(str(task_id or "")):
        raise ValueError("invalid task id")
    if not asset_ids or len(asset_ids) > 20 or len(set(asset_ids)) != len(asset_ids):
        raise ValueError("invalid audio asset list")

    with _LOCK:
        resolved = [_active_asset(asset_id) for asset_id in asset_ids]
        if any(metadata.get("lease_task_id") for _, _, metadata in resolved):
            raise AudioLeaseError("audio asset is already leased")
        written = []
        try:
            for _, metadata_path, metadata in resolved:
                metadata["lease_task_id"] = task_id
                metadata["leased_at"] = time.time()
                _write_metadata(metadata_path, metadata)
                written.append((metadata_path, metadata))
        except Exception:
            for metadata_path, metadata in written:
                metadata["lease_task_id"] = None
                metadata.pop("leased_at", None)
                _write_metadata(metadata_path, metadata)
            raise
        return [audio_path for audio_path, _, _ in resolved]


def release_audio_assets(asset_ids: list[str]) -> int:
    removed = 0
    with _LOCK:
        for asset_id in dict.fromkeys(asset_ids or []):
            try:
                audio_path, metadata_path = _paths(asset_id)
            except ValueError:
                continue
            existed = audio_path.exists() or metadata_path.exists()
            audio_path.unlink(missing_ok=True)
            metadata_path.unlink(missing_ok=True)
            removed += int(existed)
    return removed


def cleanup_expired_audio_assets(now: float | None = None) -> int:
    current = time.time() if now is None else now
    removed = 0
    with _LOCK:
        for metadata_path in _root().glob("audio_*.json"):
            try:
                metadata = _read_metadata(metadata_path)
                created_at = float(metadata.get("created_at", 0))
                asset_id = metadata_path.stem
                if current - created_at <= AUDIO_TTL_SECONDS:
                    continue
                audio_path, _ = _paths(asset_id)
            except (FileNotFoundError, ValueError, TypeError):
                continue
            audio_path.unlink(missing_ok=True)
            metadata_path.unlink(missing_ok=True)
            removed += 1
    return removed


def public_voice_catalog() -> list[dict]:
    from pixelle_video.tts_voices import EDGE_TTS_VOICES

    return [
        {
            "id": str(voice["id"]),
            "name": str(voice.get("name") or voice.get("label") or voice["id"]),
            "gender": str(voice.get("gender") or "unknown"),
            "locale": str(voice.get("locale") or ""),
        }
        for voice in EDGE_TTS_VOICES
        if voice.get("id")
    ]
