from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import threading
import time
import uuid
from pathlib import Path


MAX_AVATAR_BYTES = 12 * 1024 * 1024
AVATAR_TTL_SECONDS = 24 * 60 * 60
CLEANUP_INTERVAL_SECONDS = 15 * 60
AVATAR_ROOT = Path(os.environ.get("PIXELLE_AVATAR_ROOT", "data/avatar_assets"))
ASSET_ID_RE = re.compile(r"^avatar_[0-9a-f]{32}$")
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
ALLOWED_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
}
_LOCK = threading.RLock()
_CLEANUP_TASK: asyncio.Task | None = None


class AvatarLeaseError(RuntimeError):
    pass


class AvatarTooLargeError(ValueError):
    pass


def _root() -> Path:
    root = Path(AVATAR_ROOT)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


def _validate_asset_id(asset_id: str) -> str:
    value = str(asset_id or "")
    if not ASSET_ID_RE.fullmatch(value):
        raise ValueError("invalid avatar asset id")
    return value


def _metadata_path(asset_id: str) -> Path:
    return _root() / f"{_validate_asset_id(asset_id)}.json"


def _image_path(asset_id: str, suffix: str) -> Path:
    if suffix not in ALLOWED_TYPES.values():
        raise ValueError("invalid avatar suffix")
    return _root() / f"{_validate_asset_id(asset_id)}{suffix}"


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
        raise FileNotFoundError("avatar asset not found") from exc
    if not isinstance(payload, dict):
        raise FileNotFoundError("avatar asset not found")
    return payload


def _matches_content_type(content: bytes, content_type: str) -> bool:
    if content_type == "image/png":
        return content.startswith(b"\x89PNG\r\n\x1a\n")
    if content_type == "image/jpeg":
        return len(content) >= 3 and content[:3] == b"\xff\xd8\xff"
    if content_type == "image/webp":
        return len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP"
    return False


def store_avatar_asset(content: bytes, content_type: str, request_id: str) -> dict:
    suffix = ALLOWED_TYPES.get(content_type)
    if suffix is None:
        raise ValueError("content type must be image/jpeg, image/png, or image/webp")
    if not isinstance(content, bytes) or not content:
        raise ValueError("avatar body is empty")
    if len(content) > MAX_AVATAR_BYTES:
        raise AvatarTooLargeError("avatar body exceeds size limit")
    if not _matches_content_type(content, content_type):
        raise ValueError("invalid image data")
    if not REQUEST_ID_RE.fullmatch(str(request_id or "")):
        raise ValueError("invalid request id")

    cleanup_expired_avatar_assets()
    asset_id = f"avatar_{uuid.uuid4().hex}"
    image_path = _image_path(asset_id, suffix)
    metadata_path = _metadata_path(asset_id)
    created_at = time.time()
    sha256 = hashlib.sha256(content).hexdigest()
    metadata = {
        "asset_id": asset_id,
        "content_type": content_type,
        "size": len(content),
        "sha256": sha256,
        "request_id": request_id,
        "created_at": created_at,
        "lease_task_id": None,
        "suffix": suffix,
    }

    with _LOCK:
        _atomic_write(image_path, content)
        try:
            _write_metadata(metadata_path, metadata)
        except Exception:
            image_path.unlink(missing_ok=True)
            metadata_path.unlink(missing_ok=True)
            raise

    return {
        "asset_id": asset_id,
        "content_type": content_type,
        "size": len(content),
        "sha256": sha256,
    }


def _active_asset(asset_id: str, now: float | None = None) -> tuple[Path, Path, dict]:
    metadata_path = _metadata_path(asset_id)
    metadata = _read_metadata(metadata_path)
    current = time.time() if now is None else now
    if current - float(metadata.get("created_at", 0)) > AVATAR_TTL_SECONDS:
        raise FileNotFoundError("avatar asset not found")
    image_path = _image_path(asset_id, str(metadata.get("suffix") or ""))
    if not image_path.is_file():
        raise FileNotFoundError("avatar asset not found")
    return image_path, metadata_path, metadata


def lease_avatar_assets(asset_ids: list[str], task_id: str) -> dict[str, Path]:
    if not TASK_ID_RE.fullmatch(str(task_id or "")):
        raise ValueError("invalid task id")
    if not asset_ids or len(asset_ids) > 400 or len(set(asset_ids)) != len(asset_ids):
        raise ValueError("invalid avatar asset list")

    with _LOCK:
        resolved = [(asset_id, *_active_asset(asset_id)) for asset_id in asset_ids]
        if any(metadata.get("lease_task_id") for _, _, _, metadata in resolved):
            raise AvatarLeaseError("avatar asset is already leased")
        written = []
        try:
            for _, _, metadata_path, metadata in resolved:
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
        return {asset_id: image_path for asset_id, image_path, _, _ in resolved}


def release_avatar_assets(asset_ids: list[str]) -> None:
    with _LOCK:
        for asset_id in dict.fromkeys(asset_ids or []):
            try:
                metadata_path = _metadata_path(asset_id)
            except ValueError:
                continue
            try:
                metadata = _read_metadata(metadata_path)
                image_path = _image_path(asset_id, str(metadata.get("suffix") or ""))
            except (FileNotFoundError, ValueError):
                image_path = None
            if image_path is not None:
                image_path.unlink(missing_ok=True)
            metadata_path.unlink(missing_ok=True)


def cleanup_expired_avatar_assets(now: float | None = None) -> int:
    current = time.time() if now is None else now
    removed = 0
    with _LOCK:
        for metadata_path in _root().glob("avatar_*.json"):
            try:
                metadata = _read_metadata(metadata_path)
                created_at = float(metadata.get("created_at", 0))
                if metadata.get("lease_task_id"):
                    continue
                if current - created_at <= AVATAR_TTL_SECONDS:
                    continue
                asset_id = metadata_path.stem
                image_path = _image_path(asset_id, str(metadata.get("suffix") or ""))
            except (FileNotFoundError, ValueError, TypeError):
                continue
            image_path.unlink(missing_ok=True)
            metadata_path.unlink(missing_ok=True)
            removed += 1
    return removed


def reclaim_stale_avatar_leases() -> int:
    reclaimed = 0
    with _LOCK:
        for metadata_path in _root().glob("avatar_*.json"):
            try:
                metadata = _read_metadata(metadata_path)
            except FileNotFoundError:
                continue
            if not metadata.get("lease_task_id"):
                continue
            metadata["lease_task_id"] = None
            metadata.pop("leased_at", None)
            _write_metadata(metadata_path, metadata)
            reclaimed += 1
    return reclaimed


async def _cleanup_loop() -> None:
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
        await asyncio.to_thread(cleanup_expired_avatar_assets)


async def start_cleanup_scheduler() -> None:
    global _CLEANUP_TASK
    reclaim_stale_avatar_leases()
    cleanup_expired_avatar_assets()
    if _CLEANUP_TASK is None or _CLEANUP_TASK.done():
        _CLEANUP_TASK = asyncio.create_task(_cleanup_loop())


async def stop_cleanup_scheduler() -> None:
    global _CLEANUP_TASK
    task = _CLEANUP_TASK
    _CLEANUP_TASK = None
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
