"""Strict client for task-owned material-library inputs."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

import httpx


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_ASSET_BYTES = 512 * 1024 * 1024
CONTENT_SUFFIXES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/mp4": ".m4a",
}


class MaterialLibraryClientError(RuntimeError):
    pass


def _settings() -> tuple[str, str]:
    base = os.environ.get(
        "PIXELLE_MATERIAL_LIBRARY_URL", "http://127.0.0.1:8111"
    ).strip().rstrip("/")
    token = os.environ.get("PIXELLE_MATERIAL_LIBRARY_TOKEN", "").strip()
    parsed = urlsplit(base)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise MaterialLibraryClientError("material library URL must be loopback HTTP")
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
        raise MaterialLibraryClientError("material library URL is invalid")
    if not token:
        raise MaterialLibraryClientError("material library token is missing")
    return base, token


def _orientation(width: int, height: int) -> str:
    if width == height:
        return "square"
    return "portrait" if height > width else "landscape"


async def _download(
    client: httpx.AsyncClient,
    base: str,
    headers: dict[str, str],
    item: dict,
    target_dir: Path,
) -> str:
    sha256 = str(item.get("sha256") or "").lower()
    media_type = str(item.get("media_type") or "")
    if not SHA256_RE.fullmatch(sha256) or media_type not in {"image", "video", "bgm"}:
        raise MaterialLibraryClientError("material library returned an invalid asset")
    async with client.stream("GET", f"{base}/v1/assets/{sha256}", headers=headers) as response:
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
        suffix = CONTENT_SUFFIXES.get(content_type)
        if not suffix:
            raise MaterialLibraryClientError("material library returned an unsupported content type")
        target = target_dir / f"{sha256}{suffix}"
        temporary = target.with_suffix(target.suffix + ".part")
        digest = hashlib.sha256()
        total = 0
        try:
            with temporary.open("wb") as handle:
                async for chunk in response.aiter_bytes(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_ASSET_BYTES:
                        raise MaterialLibraryClientError("material library asset is too large")
                    digest.update(chunk)
                    handle.write(chunk)
            if not total or digest.hexdigest() != sha256:
                raise MaterialLibraryClientError("material library asset checksum mismatch")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    return str(target)


async def prepare_library_materials(
    narrations: list[str],
    *,
    task_id: str,
    task_dir: str,
    width: int,
    height: int,
) -> dict:
    if not narrations or len(narrations) > 20:
        raise MaterialLibraryClientError("material library requires 1-20 scenes")
    base, token = _settings()
    headers = {"Authorization": f"Bearer {token}"}
    scenes = [
        {
            "scene_id": f"scene_{index + 1:02d}",
            "query": narration,
            "purpose": "文案成片分镜",
            "media_type": "visual",
        }
        for index, narration in enumerate(narrations)
    ]
    scenes.append({
        "scene_id": "bgm",
        "query": "适合作为整条视频背景音乐",
        "purpose": "背景音乐",
        "media_type": "bgm",
    })
    target_dir = Path(task_dir).resolve() / "library_materials"
    target_dir.mkdir(parents=True, exist_ok=True)
    timeout = httpx.Timeout(connect=5, read=180, write=30, pool=10)
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            response = await client.post(
                f"{base}/v1/select",
                headers=headers,
                json={
                    "scenes": scenes,
                    "orientation": _orientation(width, height),
                    "seed": task_id,
                },
            )
            response.raise_for_status()
            payload = response.json()
            selected = payload.get("materials") or []
            if len(selected) != len(scenes):
                raise MaterialLibraryClientError("material library returned an incomplete selection")
            if len({item.get("sha256") for item in selected}) != len(selected):
                raise MaterialLibraryClientError("material library returned duplicate assets")
            downloaded = []
            for item in selected:
                path = await _download(client, base, headers, item, target_dir)
                downloaded.append({**item, "path": path})
    except (httpx.HTTPError, ValueError) as exc:
        raise MaterialLibraryClientError(f"material library request failed: {exc}") from exc

    visuals = [item for item in downloaded if item.get("scene_id") != "bgm"]
    bgm = next((item for item in downloaded if item.get("scene_id") == "bgm"), None)
    if len(visuals) != len(narrations) or not bgm:
        raise MaterialLibraryClientError("material library selection is incomplete")
    manifest = [
        {key: item.get(key) for key in (
            "scene_id", "record_id", "sha256", "name", "media_type",
            "orientation", "duration_seconds", "match_level", "match_score",
        )}
        for item in downloaded
    ]
    return {"visuals": visuals, "bgm_path": bgm["path"], "manifest": manifest}


async def check_library_health() -> dict:
    base, token = _settings()
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=3, read=5, write=5, pool=3),
            trust_env=False,
        ) as client:
            response = await client.get(
                f"{base}/v1/ping",
                headers={"Authorization": f"Bearer {token}"},
            )
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise MaterialLibraryClientError(f"material library health check failed: {exc}") from exc
    if payload.get("ok") is not True or int(payload.get("records") or 0) < 1:
        raise MaterialLibraryClientError("material library is not ready")
    return {"ready": True, "records": int(payload["records"])}
