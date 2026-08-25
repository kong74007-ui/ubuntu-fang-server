"""Strict client for task-owned material-library inputs."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import subprocess
import threading
from pathlib import Path
from urllib.parse import urlsplit

import httpx


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
TEMPLATE_SIZE_RE = re.compile(r"(?:^|/)([1-9][0-9]*)x([1-9][0-9]*)(?:/|$)")
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


def _validate_selection(
    selected: list, scene_ids: list[str], requested_orientation: str
) -> list[dict]:
    if not isinstance(selected, list) or any(not isinstance(item, dict) for item in selected):
        raise MaterialLibraryClientError("material library returned an invalid selection")
    if len(selected) != len(scene_ids):
        raise MaterialLibraryClientError("material library returned an incomplete selection")
    by_scene = {str(item.get("scene_id") or ""): item for item in selected}
    if len(by_scene) != len(selected) or set(by_scene) != set(scene_ids):
        raise MaterialLibraryClientError("material library scene binding is invalid")
    sha_values = [str(item.get("sha256") or "").lower() for item in selected]
    if any(not SHA256_RE.fullmatch(value) for value in sha_values):
        raise MaterialLibraryClientError("material library SHA binding is invalid")
    if len(set(sha_values)) != len(selected):
        raise MaterialLibraryClientError("material library returned duplicate assets")
    for scene_id in scene_ids:
        item = by_scene[scene_id]
        media_type = str(item.get("media_type") or "")
        declared_match = str(item.get("orientation_match") or "")
        if scene_id == "bgm":
            if media_type != "bgm":
                raise MaterialLibraryClientError("material library BGM binding is invalid")
            if declared_match != "not_applicable":
                raise MaterialLibraryClientError("material library BGM orientation binding is invalid")
        elif media_type not in {"image", "video"}:
            raise MaterialLibraryClientError("material library visual binding is invalid")
        else:
            orientation = str(item.get("orientation") or "")
            if orientation not in {"portrait", "landscape", "square", "unknown"}:
                raise MaterialLibraryClientError("material library asset orientation is invalid")
            expected_match = (
                "same" if orientation in {requested_orientation, "unknown"}
                else "fallback"
            )
            if declared_match not in {"same", "fallback"} or declared_match != expected_match:
                raise MaterialLibraryClientError("material library orientation binding is invalid")
    return [dict(by_scene[scene_id]) for scene_id in scene_ids]


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


def _canvas_orientation(frame_template: str, media_width: int, media_height: int) -> str:
    match = TEMPLATE_SIZE_RE.search(str(frame_template or "").replace("\\", "/"))
    if match:
        return _orientation(int(match.group(1)), int(match.group(2)))
    return _orientation(media_width, media_height)


def _selection_http_error(error: httpx.HTTPStatusError) -> MaterialLibraryClientError:
    code = ""
    detail = ""
    try:
        payload = error.response.json()
        if isinstance(payload, dict):
            code = str(payload.get("error") or "")
            detail = str(payload.get("detail") or "")
    except ValueError:
        pass
    if error.response.status_code == 409 and code == "material_shortage":
        return MaterialLibraryClientError(
            "平台素材库中没有足够的不重复素材，请减少分镜或补充素材"
        )
    safe = " ".join(detail.split())[:240]
    return MaterialLibraryClientError(
        "material library rejected selection" + (f": {safe}" if safe else "")
    )


def _run_managed_process(
    command: list[str], output: str, timeout_seconds: float,
    cancel_event: threading.Event | None,
) -> None:
    from pixelle_video.services.video_concat import run_cancellable_process

    run_cancellable_process(
        command, output, timeout_seconds, cancel_event
    )


def _adapt_fallback_media(
    path: str, item: dict, width: int, height: int,
    cancel_event: threading.Event | None = None,
) -> str:
    if item.get("orientation_match") != "fallback":
        return path
    if width <= 0 or height <= 0:
        raise MaterialLibraryClientError("material adaptation dimensions are invalid")
    media_type = str(item.get("media_type") or "")
    if media_type not in {"image", "video"}:
        return path
    source = Path(path)
    suffix = ".jpg" if media_type == "image" else ".mp4"
    output = source.with_name(source.stem + "_fit" + suffix)
    graph = (
        f"[0:v]split=2[bgsrc][fgsrc];"
        f"[bgsrc]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},boxblur=20:5[bg];"
        f"[fgsrc]scale={width}:{height}:force_original_aspect_ratio=decrease[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[out]"
    )
    command = [
        "ffmpeg", "-y", "-v", "error", "-i", str(source),
        "-filter_complex", graph, "-map", "[out]",
    ]
    if media_type == "image":
        command.extend(["-frames:v", "1", str(output)])
    else:
        command.extend([
            "-an", "-c:v", "libx264", "-preset", "fast", "-crf", "20",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(output),
        ])
    try:
        _run_managed_process(command, str(output), 180, cancel_event)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        output.unlink(missing_ok=True)
        raise MaterialLibraryClientError("素材比例自动适配失败") from exc
    if not output.is_file() or output.stat().st_size <= 0:
        raise MaterialLibraryClientError("素材比例自动适配失败")
    return str(output)


async def _adapt_fallback_media_cancellable(
    path: str, item: dict, width: int, height: int
) -> str:
    cancel_event = threading.Event()
    worker = asyncio.create_task(asyncio.to_thread(
        _adapt_fallback_media,
        path,
        item,
        width,
        height,
        cancel_event,
    ))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        cancel_event.set()
        try:
            await asyncio.shield(worker)
        except (MaterialLibraryClientError, RuntimeError):
            pass
        source = Path(path)
        suffix = ".jpg" if item.get("media_type") == "image" else ".mp4"
        source.with_name(source.stem + "_fit" + suffix).unlink(missing_ok=True)
        raise


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
    frame_template: str,
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
                    "orientation": _canvas_orientation(frame_template, width, height),
                    "seed": task_id,
                },
            )
            response.raise_for_status()
            payload = response.json()
            canvas_orientation = _canvas_orientation(
                frame_template, width, height
            )
            selected = _validate_selection(
                payload.get("materials") or [],
                [scene["scene_id"] for scene in scenes],
                canvas_orientation,
            )
            downloaded = []
            for item in selected:
                path = await _download(client, base, headers, item, target_dir)
                path = await _adapt_fallback_media_cancellable(
                    path, item, width, height
                )
                downloaded.append({**item, "path": path})
    except httpx.HTTPStatusError as exc:
        raise _selection_http_error(exc) from exc
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
            "orientation_match",
        )}
        for item in downloaded
    ]
    return {"visuals": visuals, "bgm_path": bgm["path"], "manifest": manifest}


async def probe_library_capacity(scene_count: int, orientation: str) -> dict:
    if isinstance(scene_count, bool) or not isinstance(scene_count, int) or not 1 <= scene_count <= 20:
        raise MaterialLibraryClientError("material library probe scene_count is invalid")
    if orientation not in {"portrait", "landscape", "square"}:
        raise MaterialLibraryClientError("material library probe orientation is invalid")
    base, token = _settings()
    scene_ids = [f"scene_{index + 1:02d}" for index in range(scene_count)] + ["bgm"]
    scenes = [
        {"scene_id": scene_id, "query": "库存预检", "media_type": "bgm" if scene_id == "bgm" else "visual"}
        for scene_id in scene_ids
    ]
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=3, read=10, write=5, pool=3), trust_env=False
        ) as client:
            response = await client.post(
                f"{base}/v1/select",
                headers={"Authorization": f"Bearer {token}"},
                json={"scenes": scenes, "orientation": orientation, "seed": "capacity-probe"},
            )
            response.raise_for_status()
            selected = _validate_selection(
                response.json().get("materials") or [], scene_ids, orientation
            )
    except (httpx.HTTPError, ValueError) as exc:
        raise MaterialLibraryClientError(f"material library capacity probe failed: {exc}") from exc
    return {"ready": True, "scene_count": scene_count, "selected_count": len(selected)}


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
