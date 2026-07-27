"""Media probing and normalization for the AI editing V2 pipeline."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable


MAX_DURATION_MS = 600_000
PROBE_TIMEOUT_SECONDS = 30
NORMALIZE_TIMEOUT_SECONDS = 600


class MediaError(RuntimeError):
    def __init__(self, code: str, detail: str | None = None):
        self.code = code
        self.detail = detail or code
        super().__init__(self.detail)


def _run_probe(path: str, runner: Callable[..., Any]) -> dict[str, Any]:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        os.fspath(path),
    ]
    try:
        result = runner(
            command,
            check=False,
            timeout=PROBE_TIMEOUT_SECONDS,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise MediaError("ffprobe_missing") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaError("ffprobe_timeout") from exc
    except Exception as exc:
        raise MediaError("media_probe_failed") from exc
    if getattr(result, "returncode", 1) != 0:
        raise MediaError("media_probe_failed")
    try:
        raw = result.stdout.decode("utf-8", "replace") if isinstance(result.stdout, bytes) else result.stdout
        payload = json.loads(raw or "{}")
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise MediaError("media_probe_failed") from exc
    if not isinstance(payload, dict):
        raise MediaError("media_probe_failed")
    return payload


def _fps(value: Any) -> float | None:
    try:
        rate = float(Fraction(str(value)))
        return round(rate, 3) if rate > 0 else None
    except (ValueError, ZeroDivisionError):
        return None


def probe_media(
    path: str,
    runner: Callable[..., Any] = subprocess.run,
    *,
    media_type: str | None = None,
) -> dict[str, Any]:
    payload = _run_probe(path, runner)
    try:
        duration_ms = round(float((payload.get("format") or {}).get("duration")) * 1000)
    except (TypeError, ValueError):
        raise MediaError("media_probe_failed")
    if duration_ms <= 0 or duration_ms > MAX_DURATION_MS:
        raise MediaError("media_invalid_duration")

    streams = payload.get("streams")
    if not isinstance(streams, list):
        raise MediaError("media_probe_failed")
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    if media_type == "video" and video is None:
        raise MediaError("media_type_mismatch")
    if media_type == "audio" and audio is None:
        raise MediaError("media_type_mismatch")
    if media_type not in (None, "video", "audio"):
        raise MediaError("media_type_mismatch")
    if media_type is None and video is None and audio is None:
        raise MediaError("media_type_mismatch")

    try:
        metadata = {
            "duration_ms": duration_ms,
            "width": int(video.get("width") or 0) if video else None,
            "height": int(video.get("height") or 0) if video else None,
            "fps": _fps(video.get("r_frame_rate")) if video else None,
            "video_codec": (str(video.get("codec_name") or "").lower() or None) if video else None,
            "audio_codec": (str(audio.get("codec_name") or "").lower() or None) if audio else None,
            "sample_rate": int(audio.get("sample_rate") or 0) if audio else None,
            "channels": int(audio.get("channels") or 0) if audio else None,
            "container": str((payload.get("format") or {}).get("format_name") or ""),
        }
    except (TypeError, ValueError):
        raise MediaError("media_probe_failed")
    if video and (
        metadata["width"] <= 0
        or metadata["height"] <= 0
        or not metadata["fps"]
        or not metadata["video_codec"]
    ):
        raise MediaError("media_probe_failed")
    if audio and (
        metadata["sample_rate"] <= 0
        or metadata["channels"] <= 0
        or not metadata["audio_codec"]
    ):
        raise MediaError("media_probe_failed")
    if not metadata["container"]:
        raise MediaError("media_probe_failed")
    return metadata


def needs_normalization(metadata: dict[str, Any], media_type: str) -> bool:
    if media_type == "video":
        video_ok = metadata.get("video_codec") == "h264" and abs((metadata.get("fps") or 0) - 30) < 0.01
        audio_ok = metadata.get("audio_codec") in (None, "aac") and metadata.get("sample_rate") in (None, 48_000)
        return not (video_ok and audio_ok)
    if media_type == "audio":
        return not (
            metadata.get("audio_codec") == "aac"
            and metadata.get("sample_rate") == 48_000
        )
    raise MediaError("media_type_mismatch")


def _normalization_command(source: str, destination: str, media_type: str) -> list[str]:
    base = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", os.fspath(source)]
    if media_type == "video":
        return base + [
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-r",
            "30",
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-movflags",
            "+faststart",
            os.fspath(destination),
        ]
    if media_type == "audio":
        return base + [
            "-vn",
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-f",
            "ipod",
            os.fspath(destination),
        ]
    raise MediaError("media_type_mismatch")


def normalize_media(
    source: str,
    destination: str,
    media_type: str,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    before = probe_media(source, runner=runner, media_type=media_type)
    command = _normalization_command(source, destination, media_type)
    try:
        result = runner(
            command,
            check=False,
            timeout=NORMALIZE_TIMEOUT_SECONDS,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise MediaError("ffmpeg_missing") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaError("ffmpeg_timeout") from exc
    except Exception as exc:
        raise MediaError("ffmpeg_failed") from exc
    if getattr(result, "returncode", 1) != 0:
        raise MediaError("ffmpeg_failed")
    destination_path = Path(destination)
    if not destination_path.is_file() or destination_path.stat().st_size < 1:
        raise MediaError("normalized_output_missing")

    after = probe_media(destination, runner=runner, media_type=media_type)
    allowed_drift = max(500, round(before["duration_ms"] * 0.02))
    if abs(after["duration_ms"] - before["duration_ms"]) > allowed_drift:
        raise MediaError("duration_drift")
    after["path"] = os.fspath(destination)
    return after


def prepare_cos_media(
    source_cos_key: str,
    normalized_cos_key: str,
    media_type: str,
    *,
    cos_api: Any,
    runner: Callable[..., Any] = subprocess.run,
) -> dict[str, Any]:
    """Download, validate, optionally normalize, upload, and clean local files."""
    with tempfile.TemporaryDirectory(prefix="ai-edit-v2-") as temp_dir:
        suffix = ".mp4" if media_type == "video" else ".m4a"
        source_path = os.path.join(temp_dir, "source" + suffix)
        output_path = os.path.join(temp_dir, "normalized" + suffix)
        cos_api.download_file(source_cos_key, source_path)
        metadata = probe_media(source_path, runner=runner, media_type=media_type)
        if not needs_normalization(metadata, media_type):
            return {"cos_key": source_cos_key, "metadata": metadata, "normalized": False}
        normalized = normalize_media(source_path, output_path, media_type, runner=runner)
        content_type = "video/mp4" if media_type == "video" else "audio/mp4"
        cos_api.put_file(output_path, normalized_cos_key, content_type, private=True)
        normalized.pop("path", None)
        return {"cos_key": normalized_cos_key, "metadata": normalized, "normalized": True}
