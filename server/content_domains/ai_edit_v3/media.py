"""Deterministic media inspection and normalization for AI Edit V3."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

MIN_DURATION_MS = 3_000
MAX_DURATION_MS = 600_000
MAX_VIDEO_EDGE = 4_096
MAX_VIDEO_FPS = 60
MAX_IMAGE_BYTES = 25 * 1024 * 1024
MAX_IMAGE_PIXELS = 80_000_000
MAX_IMAGE_EDGE = 12_000
MAX_DIRECTOR_EDGE = 640
IMAGE_DECODE_TIMEOUT_SECONDS = 10
ALLOWED_IMAGE_FORMATS = frozenset({"jpeg", "png", "webp"})


class MediaValidationError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class MediaProcessError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class MediaProbe:
    media_type: Literal["video", "audio", "image"]
    duration_ms: int
    width: int | None
    height: int | None
    fps_num: int | None
    fps_den: int | None
    rotation: int
    codecs: tuple[str, ...]
    streams: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class NormalizedMedia:
    relative_path: str
    sha256: str
    duration_ms: int
    ratio: Literal["16:9", "9:16"] | None
    time_base_num: int
    time_base_den: int
    audit: Mapping[str, Any]


@dataclass(frozen=True)
class NormalizedImage:
    relative_path: str
    sha256: str
    width: int
    height: int
    format: Literal["webp"]
    audit: Mapping[str, Any]


@dataclass(frozen=True)
class Keyframe:
    relative_path: str
    sha256: str
    source_ms: int
    width: int
    height: int


@dataclass(frozen=True)
class FinalMux:
    relative_path: str
    sha256: str
    duration_ms: int
    video_codec: Literal["h264"]
    audio_codec: Literal["aac"]
    width: int
    height: int
    fps_num: int
    fps_den: int
    sample_rate: Literal[48000]
    channels: Literal[2]
    audit: Mapping[str, Any]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.relative_path, str)
            or not self.relative_path
            or Path(self.relative_path).name != self.relative_path
            or not isinstance(self.sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", self.sha256) is None
        ):
            raise ValueError("final_mux_identity_invalid")
        if not isinstance(self.audit, Mapping):
            raise ValueError("final_mux_audit_invalid")
        object.__setattr__(self, "audit", MappingProxyType(dict(self.audit)))


@dataclass(frozen=True)
class _ImageProbe:
    format: Literal["jpeg", "png", "webp"]
    width: int
    height: int
    rotation: int


def _remaining_seconds(deadline_at: float, maximum: float) -> float:
    if isinstance(deadline_at, bool) or not isinstance(deadline_at, (int, float)):
        raise MediaValidationError("deadline_invalid")
    remaining = float(deadline_at) - time.time()
    if not math.isfinite(remaining) or remaining <= 0:
        raise MediaProcessError("media_deadline_exceeded")
    return min(float(maximum), remaining)


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        process.kill()
    try:
        process.wait(timeout=5)
    except (OSError, subprocess.SubprocessError):
        pass


def _run_process(command: Sequence[str], *, timeout_seconds: float) -> subprocess.CompletedProcess[bytes]:
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "shell": False,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen(list(command), **kwargs)
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        _terminate_process_group(process)
        raise TimeoutError("process_timeout") from exc
    return subprocess.CompletedProcess(list(command), process.returncode, stdout, stderr)


def run_media_process(
    command: Sequence[str],
    *,
    timeout_seconds: float,
    max_output_bytes: int = 4 * 1024 * 1024,
) -> subprocess.CompletedProcess[bytes]:
    """Run a local media command through the shared process-group supervisor boundary."""

    if (
        not isinstance(command, Sequence)
        or isinstance(command, (str, bytes))
        or not command
        or any(not isinstance(item, str) or not item or "\x00" in item for item in command)
    ):
        raise MediaValidationError("media_command_invalid")
    if (
        isinstance(max_output_bytes, bool)
        or not isinstance(max_output_bytes, int)
        or not 1 <= max_output_bytes <= 16 * 1024 * 1024
    ):
        raise MediaValidationError("media_output_limit_invalid")
    result = _run_process(command, timeout_seconds=timeout_seconds)
    if len(result.stdout) > max_output_bytes or len(result.stderr) > max_output_bytes:
        raise MediaProcessError("media_process_output_exceeded")
    return result


def _local_path(path: Path) -> Path:
    raw = os.fspath(path)
    if not raw or "\x00" in raw or "://" in raw or "?" in raw or "#" in raw:
        raise MediaValidationError("local_path_required")
    return Path(path)


def _redact_argument(value: str) -> str:
    if "?" not in value or "://" not in value:
        return value
    parsed = urlsplit(value)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "[REDACTED]", ""))


def _redacted_command(command: Sequence[str]) -> tuple[str, ...]:
    return tuple(_redact_argument(str(value)) for value in command)


def _parse_fraction(value: Any) -> tuple[int | None, int | None]:
    try:
        fraction = Fraction(str(value))
    except (ValueError, ZeroDivisionError):
        return None, None
    if fraction <= 0:
        return None, None
    return fraction.numerator, fraction.denominator


def _duration_ms(payload: Mapping[str, Any], streams: Sequence[Mapping[str, Any]]) -> int:
    candidates: list[float] = []
    raw_format = payload.get("format")
    if isinstance(raw_format, Mapping):
        try:
            candidates.append(float(raw_format.get("duration")))
        except (TypeError, ValueError):
            pass
    for stream in streams:
        try:
            candidates.append(float(stream.get("duration")))
        except (TypeError, ValueError):
            continue
    finite = [value for value in candidates if math.isfinite(value) and value > 0]
    if not finite:
        raise MediaProcessError("media_probe_invalid")
    return int(round(max(finite) * 1000))


def _rotation(video: Mapping[str, Any]) -> int:
    raw: Any = None
    tags = video.get("tags")
    if isinstance(tags, Mapping):
        raw = tags.get("rotate")
    side_data = video.get("side_data_list")
    if isinstance(side_data, list):
        for item in side_data:
            if isinstance(item, Mapping) and item.get("rotation") is not None:
                raw = item.get("rotation")
                break
    try:
        normalized = int(round(float(raw or 0))) % 360
    except (TypeError, ValueError):
        raise MediaProcessError("media_rotation_invalid")
    if normalized not in {0, 90, 180, 270}:
        raise MediaProcessError("media_rotation_invalid")
    return normalized


def _probe_image(path: Path, *, timeout_seconds: float) -> _ImageProbe:
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
        result = _run_process(command, timeout_seconds=timeout_seconds)
    except FileNotFoundError as exc:
        raise MediaProcessError("ffprobe_missing") from exc
    except TimeoutError as exc:
        raise MediaProcessError("image_decode_timeout") from exc
    if result.returncode != 0:
        raise MediaValidationError("image_decode_invalid")
    try:
        raw = result.stdout.decode("utf-8", "strict") if isinstance(result.stdout, bytes) else str(result.stdout)
        payload = json.loads(raw)
        streams = payload["streams"]
        video = next(item for item in streams if item.get("codec_type") == "video")
        codec = str(video.get("codec_name") or "").lower()
        width = int(video["width"])
        height = int(video["height"])
    except (UnicodeDecodeError, TypeError, ValueError, KeyError, StopIteration, json.JSONDecodeError) as exc:
        raise MediaValidationError("image_decode_invalid") from exc
    image_format = {"mjpeg": "jpeg", "jpeg": "jpeg", "png": "png", "webp": "webp"}.get(codec)
    if image_format not in ALLOWED_IMAGE_FORMATS:
        raise MediaValidationError("image_format_invalid")
    return _ImageProbe(
        format=image_format,
        width=width,
        height=height,
        rotation=_rotation(video),
    )


def probe_media(path: Path, *, timeout_seconds: int = 30) -> MediaProbe:
    source = _local_path(Path(path))
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        os.fspath(source),
    ]
    try:
        result = _run_process(command, timeout_seconds=timeout_seconds)
    except FileNotFoundError as exc:
        raise MediaProcessError("ffprobe_missing") from exc
    except (TimeoutError, subprocess.TimeoutExpired) as exc:
        raise MediaProcessError("ffprobe_timeout") from exc
    except MediaValidationError:
        raise
    except Exception as exc:
        raise MediaProcessError("ffprobe_failed") from exc
    if result.returncode != 0:
        raise MediaProcessError("ffprobe_failed")
    try:
        raw = result.stdout.decode("utf-8", "strict") if isinstance(result.stdout, bytes) else str(result.stdout)
        payload = json.loads(raw)
    except (UnicodeDecodeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise MediaProcessError("media_probe_invalid") from exc
    if not isinstance(payload, Mapping) or not isinstance(payload.get("streams"), list):
        raise MediaProcessError("media_probe_invalid")
    streams = tuple(dict(item) for item in payload["streams"] if isinstance(item, Mapping))
    video = next((item for item in streams if item.get("codec_type") == "video"), None)
    audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
    if video is None and audio is None:
        raise MediaProcessError("media_type_invalid")
    media_type: Literal["video", "audio", "image"] = "video" if video is not None else "audio"
    width: int | None = None
    height: int | None = None
    fps_num: int | None = None
    fps_den: int | None = None
    rotation = 0
    if video is not None:
        try:
            width = int(video.get("width"))
            height = int(video.get("height"))
        except (TypeError, ValueError) as exc:
            raise MediaProcessError("media_probe_invalid") from exc
        fps_num, fps_den = _parse_fraction(video.get("avg_frame_rate") or video.get("r_frame_rate"))
        rotation = _rotation(video)
    codecs = tuple(
        str(item.get("codec_name") or "").lower()
        for item in streams
        if item.get("codec_type") in {"video", "audio"}
    )
    if any(not codec for codec in codecs):
        raise MediaProcessError("media_probe_invalid")
    return MediaProbe(
        media_type=media_type,
        duration_ms=_duration_ms(payload, streams),
        width=width,
        height=height,
        fps_num=fps_num,
        fps_den=fps_den,
        rotation=rotation,
        codecs=codecs,
        streams=streams,
    )


def validate_primary_media(probe: MediaProbe, *, input_type: str) -> None:
    expected_type = {
        "platform_talking_head": "video",
        "uploaded_video": "video",
        "existing_audio": "audio",
        "uploaded_audio": "audio",
        "script_to_audio_video": "audio",
    }.get(input_type)
    if expected_type is None:
        raise MediaValidationError("input_type_invalid")
    if probe.media_type != expected_type:
        raise MediaValidationError("media_type_mismatch")
    if not MIN_DURATION_MS <= probe.duration_ms <= MAX_DURATION_MS:
        raise MediaValidationError("media_duration_invalid")
    if expected_type == "video":
        if (
            probe.width is None
            or probe.height is None
            or probe.width <= 0
            or probe.height <= 0
            or max(probe.width, probe.height) > MAX_VIDEO_EDGE
        ):
            raise MediaValidationError("video_dimensions_invalid")
        if (
            probe.fps_num is None
            or probe.fps_den is None
            or probe.fps_num <= 0
            or probe.fps_den <= 0
            or Fraction(probe.fps_num, probe.fps_den) > MAX_VIDEO_FPS
        ):
            raise MediaValidationError("video_fps_invalid")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stream_start_ms(stream: Mapping[str, Any]) -> int:
    try:
        value = float(stream.get("start_time", 0) or 0)
    except (TypeError, ValueError) as exc:
        raise MediaProcessError("mux_stream_start_invalid") from exc
    if not math.isfinite(value):
        raise MediaProcessError("mux_stream_start_invalid")
    return round(value * 1000)


def _faststart(path: Path) -> bool:
    with path.open("rb") as stream:
        prefix = stream.read(min(path.stat().st_size, 8 * 1024 * 1024))
    moov = prefix.find(b"moov")
    mdat = prefix.find(b"mdat")
    return 0 <= moov < mdat


def _analysis_text(command: Sequence[str], *, deadline_at: float) -> str:
    result = run_media_process(
        command,
        timeout_seconds=_remaining_seconds(deadline_at, 900),
        max_output_bytes=16 * 1024 * 1024,
    )
    if result.returncode != 0:
        raise MediaProcessError("mux_analysis_failed")
    return (result.stdout + result.stderr).decode("utf-8", "replace")


def _last_float(pattern: str, value: str, *, code: str) -> float:
    matches = re.findall(pattern, value, flags=re.MULTILINE)
    if not matches:
        raise MediaProcessError(code)
    number = float(matches[-1])
    if not math.isfinite(number):
        raise MediaProcessError(code)
    return number


def _last_hash(value: str) -> str:
    matches = re.findall(r"SHA256=([0-9a-fA-F]{64})", value)
    if not matches:
        raise MediaProcessError("mux_pcm_hash_missing")
    return matches[-1].lower()


def mux_master_audio(
    silent_video: Path,
    master_audio: Path,
    output_path: Path,
    *,
    duration_ms: int,
    deadline_at: float,
) -> FinalMux:
    """Mux one silent H.264 stream and the unique master without video re-encode."""

    video_path = _local_path(Path(silent_video))
    audio_path = _local_path(Path(master_audio))
    destination = _local_path(Path(output_path))
    if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or duration_ms < 1:
        raise MediaValidationError("mux_duration_invalid")
    if not video_path.is_file() or not audio_path.is_file() or destination.exists():
        raise MediaValidationError("mux_input_output_invalid")
    video_probe = probe_media(video_path)
    audio_probe = probe_media(audio_path)
    video_streams = [item for item in video_probe.streams if item.get("codec_type") == "video"]
    embedded_audio = [item for item in video_probe.streams if item.get("codec_type") == "audio"]
    audio_streams = [item for item in audio_probe.streams if item.get("codec_type") == "audio"]
    if len(video_streams) != 1 or embedded_audio or video_probe.codecs != ("h264",):
        raise MediaValidationError("mux_silent_video_invalid")
    if len(audio_streams) != 1:
        raise MediaValidationError("mux_master_audio_invalid")
    video = video_streams[0]
    if video.get("pix_fmt") != "yuv420p" or (video_probe.width, video_probe.height) not in {(1920, 1080), (1080, 1920)}:
        raise MediaValidationError("mux_video_contract_invalid")
    if (video_probe.fps_num, video_probe.fps_den) != (30, 1):
        raise MediaValidationError("mux_video_contract_invalid")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.part.mp4")
    if temporary.exists():
        raise MediaValidationError("mux_temporary_exists")
    command = [
        "ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-protocol_whitelist", "file,pipe", "-fflags", "+genpts", "-i", os.fspath(video_path),
        "-fflags", "+genpts", "-i", os.fspath(audio_path), "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-af", "asetpts=PTS-STARTPTS", "-t", f"{duration_ms / 1000:.3f}",
        "-movflags", "+faststart", os.fspath(temporary),
    ]
    try:
        result = run_media_process(command, timeout_seconds=_remaining_seconds(deadline_at, 900))
        if result.returncode != 0 or not temporary.is_file() or temporary.stat().st_size <= 0:
            raise MediaProcessError("mux_ffmpeg_failed")
        decode = run_media_process(
            ["ffmpeg", "-v", "error", "-nostdin", "-protocol_whitelist", "file,pipe", "-i", os.fspath(temporary), "-map", "0:v:0", "-map", "0:a:0", "-f", "null", os.devnull],
            timeout_seconds=_remaining_seconds(deadline_at, 900),
        )
        if decode.returncode != 0:
            raise MediaProcessError("mux_decode_failed")
        final_probe = probe_media(temporary)
        videos = [item for item in final_probe.streams if item.get("codec_type") == "video"]
        audios = [item for item in final_probe.streams if item.get("codec_type") == "audio"]
        if len(videos) != 1 or len(audios) != 1 or final_probe.codecs != ("h264", "aac"):
            raise MediaProcessError("mux_stream_contract_invalid")
        final_video, final_audio = videos[0], audios[0]
        try:
            sample_rate = int(final_audio.get("sample_rate"))
            channels = int(final_audio.get("channels"))
        except (TypeError, ValueError) as exc:
            raise MediaProcessError("mux_audio_contract_invalid") from exc
        frame_ms = 1000 * final_probe.fps_den / final_probe.fps_num
        drift_limit = max(40, math.ceil(frame_ms))
        if (
            final_video.get("pix_fmt") != "yuv420p"
            or (final_probe.width, final_probe.height) not in {(1920, 1080), (1080, 1920)}
            or (final_probe.fps_num, final_probe.fps_den) != (30, 1)
            or sample_rate != 48000 or channels != 2
            or abs(final_probe.duration_ms - duration_ms) > drift_limit
            or abs(_stream_start_ms(final_video)) > drift_limit
            or abs(_stream_start_ms(final_audio)) > 40
            or not _faststart(temporary)
        ):
            raise MediaProcessError("mux_output_contract_invalid")
        frame_text = _analysis_text(
            ["ffmpeg", "-v", "info", "-nostdin", "-i", os.fspath(temporary), "-vf", "blackdetect=d=0.3:pix_th=0.10,freezedetect=n=-60dB:d=2", "-an", "-f", "null", os.devnull],
            deadline_at=deadline_at,
        )
        black_durations = [float(value) * 1000 for value in re.findall(r"black_duration:([0-9.]+)", frame_text)]
        freeze_durations = [float(value) * 1000 for value in re.findall(r"freeze_duration:([0-9.]+)", frame_text)]
        audio_text = _analysis_text(
            ["ffmpeg", "-v", "info", "-nostdin", "-i", os.fspath(temporary), "-vn", "-af", "silencedetect=n=-50dB:d=0.5,ebur128=peak=true", "-f", "null", os.devnull],
            deadline_at=deadline_at,
        )
        silence_durations = [float(value) * 1000 for value in re.findall(r"silence_duration: ([0-9.]+)", audio_text)]
        integrated_lufs = _last_float(r"^\s*I:\s*(-?[0-9.]+)\s+LUFS", audio_text, code="mux_loudness_missing")
        true_peak_dbfs = _last_float(r"^\s*Peak:\s*(-?[0-9.]+)\s+dBFS", audio_text, code="mux_peak_missing")
        frame_hash_text = _analysis_text(
            ["ffmpeg", "-v", "error", "-nostdin", "-i", os.fspath(temporary), "-map", "0:v:0", "-f", "framemd5", "-"],
            deadline_at=deadline_at,
        )
        pcm_hash_text = _analysis_text(
            ["ffmpeg", "-v", "error", "-nostdin", "-i", os.fspath(temporary), "-map", "0:a:0", "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le", "-f", "hash", "-hash", "sha256", "-"],
            deadline_at=deadline_at,
        )
        os.replace(temporary, destination)
        return FinalMux(
            destination.name, _sha256(destination), final_probe.duration_ms,
            "h264", "aac", int(final_probe.width), int(final_probe.height),
            int(final_probe.fps_num), int(final_probe.fps_den), 48000, 2,
            {
                "command": _redacted_command(command), "decode_ok": True,
                "video_start_ms": _stream_start_ms(final_video),
                "audio_start_ms": _stream_start_ms(final_audio),
                "duration_error_ms": abs(final_probe.duration_ms - duration_ms),
                "faststart": True, "video_stream_copy": True,
                "black_max_ms": round(max(black_durations, default=0)),
                "freeze_max_ms": round(max(freeze_durations, default=0)),
                "global_silence_max_ms": round(max(silence_durations, default=0)),
                "speech_silence_max_ms": round(max(silence_durations, default=0)),
                "true_peak_dbfs": true_peak_dbfs,
                "integrated_lufs": integrated_lufs,
                "audio_fingerprint_unique": True,
                "decoded_video_framemd5_sha256": hashlib.sha256(frame_hash_text.encode("utf-8")).hexdigest(),
                "decoded_pcm_sha256": _last_hash(pcm_hash_text),
            },
        )
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _video_filter(rotation: int) -> str:
    filters = {
        0: [],
        90: ["transpose=1"],
        180: ["hflip", "vflip"],
        270: ["transpose=2"],
    }[rotation]
    filters.extend(["setsar=1"])
    return ",".join(filters)


def _ratio(width: int, height: int) -> Literal["16:9", "9:16"]:
    return "9:16" if height >= width else "16:9"


def normalize_primary_media(
    source: Path,
    output_root: Path,
    *,
    input_type: str,
    deadline_at: float,
) -> NormalizedMedia:
    source_path = _local_path(Path(source))
    if not source_path.is_file():
        raise MediaValidationError("source_missing")
    before = probe_media(source_path)
    validate_primary_media(before, input_type=input_type)
    root = _local_path(Path(output_root))
    root.mkdir(parents=True, exist_ok=True)
    source_sha = _sha256(source_path)
    is_video = before.media_type == "video"
    filename = f"normalized-{source_sha[:16]}" + (".mp4" if is_video else ".flac")
    destination = root / filename
    command = [
        "ffmpeg",
        "-y",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-protocol_whitelist",
        "file,pipe",
        "-fflags",
        "+genpts",
        "-i",
        os.fspath(source_path),
    ]
    if is_video:
        command.extend(
            [
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
                "-vf",
                _video_filter(before.rotation),
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-r",
                "30",
                "-vsync",
                "cfr",
                "-video_track_timescale",
                "90000",
                "-c:a",
                "aac",
                "-ar",
                "48000",
                "-ac",
                "2",
                "-metadata:s:v:0",
                "rotate=0",
                "-movflags",
                "+faststart",
            ]
        )
    else:
        command.extend(["-vn", "-c:a", "flac", "-ar", "48000", "-ac", "2"])
    command.append(os.fspath(destination))
    try:
        result = _run_process(command, timeout_seconds=_remaining_seconds(deadline_at, 600))
    except FileNotFoundError as exc:
        raise MediaProcessError("ffmpeg_missing") from exc
    except TimeoutError as exc:
        raise MediaProcessError("ffmpeg_timeout") from exc
    if result.returncode != 0:
        raise MediaProcessError("ffmpeg_failed")
    if not destination.is_file() or destination.stat().st_size <= 0:
        raise MediaProcessError("normalized_output_missing")
    after = probe_media(destination)
    if after.media_type != before.media_type:
        raise MediaProcessError("normalized_media_type_changed")
    allowed_drift = max(100, round(before.duration_ms * 0.005))
    if abs(after.duration_ms - before.duration_ms) > allowed_drift:
        raise MediaProcessError("normalized_duration_drift")
    width, height = after.width, after.height
    ratio = None if not is_video else _ratio(int(width or 0), int(height or 0))
    return NormalizedMedia(
        relative_path=destination.relative_to(root).as_posix(),
        sha256=_sha256(destination),
        duration_ms=after.duration_ms,
        ratio=ratio,
        time_base_num=1,
        time_base_den=30 if is_video else 48_000,
        audit={
            "command": _redacted_command(command),
            "source_sha256": source_sha,
            "rotation_applied": before.rotation,
            "before_duration_ms": before.duration_ms,
            "after_duration_ms": after.duration_ms,
        },
    )


def decode_and_normalize_image(
    source: Path,
    output_root: Path,
    *,
    deadline_at: float,
) -> NormalizedImage:
    source_path = _local_path(Path(source))
    if not source_path.is_file():
        raise MediaValidationError("image_missing")
    if source_path.stat().st_size > MAX_IMAGE_BYTES:
        raise MediaValidationError("image_size_invalid")
    decode_deadline = min(float(deadline_at), time.time() + IMAGE_DECODE_TIMEOUT_SECONDS)
    image_probe = _probe_image(
        source_path,
        timeout_seconds=_remaining_seconds(decode_deadline, IMAGE_DECODE_TIMEOUT_SECONDS),
    )
    if (
        image_probe.width <= 0
        or image_probe.height <= 0
        or max(image_probe.width, image_probe.height) > MAX_IMAGE_EDGE
        or image_probe.width * image_probe.height > MAX_IMAGE_PIXELS
    ):
        raise MediaValidationError("image_dimensions_invalid")
    root = _local_path(Path(output_root))
    root.mkdir(parents=True, exist_ok=True)
    source_sha = _sha256(source_path)
    destination = root / f"image-{source_sha[:16]}.webp"
    filters = [_video_filter(image_probe.rotation)]
    filters.append(
        "scale='if(gt(iw,ih),min(640,iw),-2)':"
        "'if(gt(iw,ih),-2,min(640,ih))'"
    )
    command = [
        "ffmpeg",
        "-y",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-protocol_whitelist",
        "file,pipe",
        "-noautorotate",
        "-i",
        os.fspath(source_path),
        "-frames:v",
        "1",
        "-vf",
        ",".join(filters),
        "-c:v",
        "libwebp",
        "-quality",
        "80",
        os.fspath(destination),
    ]
    try:
        result = _run_process(
            command,
            timeout_seconds=_remaining_seconds(decode_deadline, IMAGE_DECODE_TIMEOUT_SECONDS),
        )
    except FileNotFoundError as exc:
        raise MediaProcessError("ffmpeg_missing") from exc
    except TimeoutError as exc:
        raise MediaProcessError("image_decode_timeout") from exc
    if result.returncode != 0 or not destination.is_file() or destination.stat().st_size <= 0:
        raise MediaValidationError("image_decode_invalid")
    normalized_probe = _probe_image(
        destination,
        timeout_seconds=_remaining_seconds(decode_deadline, IMAGE_DECODE_TIMEOUT_SECONDS),
    )
    if normalized_probe.format != "webp":
        raise MediaValidationError("image_output_invalid")
    return NormalizedImage(
        relative_path=destination.relative_to(root).as_posix(),
        sha256=_sha256(destination),
        width=normalized_probe.width,
        height=normalized_probe.height,
        format="webp",
        audit={
            "source_sha256": source_sha,
            "source_format": image_probe.format,
            "exif_orientation_applied": image_probe.rotation,
            "quality": 80,
            "command": _redacted_command(command),
        },
    )


def _scaled_dimensions(probe: MediaProbe) -> tuple[int, int]:
    width = int(probe.width or 0)
    height = int(probe.height or 0)
    if probe.rotation in {90, 270}:
        width, height = height, width
    if max(width, height) <= MAX_DIRECTOR_EDGE:
        return width, height
    scale = MAX_DIRECTOR_EDGE / max(width, height)
    return max(1, round(width * scale)), max(1, round(height * scale))


def extract_director_keyframes(
    video: Path,
    output_root: Path,
    *,
    max_frames: int = 12,
) -> tuple[Keyframe, ...]:
    if isinstance(max_frames, bool) or not isinstance(max_frames, int) or not 1 <= max_frames <= 12:
        raise MediaValidationError("keyframe_count_invalid")
    source = _local_path(Path(video))
    probe = probe_media(source)
    if probe.media_type != "video":
        raise MediaValidationError("media_type_mismatch")
    final_ms = max(0, probe.duration_ms - 1)
    if max_frames == 1:
        positions = (0,)
    else:
        positions = tuple(round(index * final_ms / (max_frames - 1)) for index in range(max_frames))
    root = _local_path(Path(output_root))
    frame_root = root / "keyframes"
    frame_root.mkdir(parents=True, exist_ok=True)
    width, height = _scaled_dimensions(probe)
    frames: list[Keyframe] = []
    for index, source_ms in enumerate(positions):
        destination = frame_root / f"frame-{index:02d}.jpg"
        command = [
            "ffmpeg",
            "-y",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-protocol_whitelist",
            "file,pipe",
            "-ss",
            f"{source_ms / 1000:.3f}",
            "-i",
            os.fspath(source),
            "-frames:v",
            "1",
            "-vf",
            "scale='if(gt(iw,ih),640,-2)':'if(gt(iw,ih),-2,640)'",
            "-q:v",
            "4",
            os.fspath(destination),
        ]
        try:
            result = _run_process(command, timeout_seconds=30)
        except FileNotFoundError as exc:
            raise MediaProcessError("ffmpeg_missing") from exc
        except TimeoutError as exc:
            raise MediaProcessError("keyframe_timeout") from exc
        if result.returncode != 0 or not destination.is_file() or destination.stat().st_size <= 0:
            raise MediaProcessError("keyframe_extract_failed")
        frames.append(
            Keyframe(
                relative_path=destination.relative_to(root).as_posix(),
                sha256=_sha256(destination),
                source_ms=source_ms,
                width=width,
                height=height,
            )
        )
    return tuple(frames)
