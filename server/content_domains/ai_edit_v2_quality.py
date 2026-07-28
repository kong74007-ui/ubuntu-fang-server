"""Fail-closed, locally auditable quality gates for AI Edit V2 outputs."""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class QualityReport:
    passed: bool
    error_codes: tuple[str, ...]
    failing_layers: tuple[str, ...]
    repairable: bool
    terminal: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "error_codes": list(self.error_codes),
            "failing_layers": list(self.failing_layers),
            "repairable": self.repairable,
            "terminal": self.terminal,
        }


_CHECKS = (
    "probe", "decode_video", "decode_audio", "frames", "captions",
    "materials", "transcript", "audio",
)
_TERMINAL_CODES = frozenset({
    "inspection_incomplete", "video_unplayable", "required_material_missing",
    "caption_source_mismatch", "caption_facts_mismatch",
})
_ANALYZER_CAPABILITIES = (
    "captions_ocr", "glyphs", "materials", "transcript_facts", "audio",
)
_CHECK_CAPABILITIES = {
    "captions": ("captions_ocr", "glyphs"),
    "materials": ("materials",),
    "transcript": ("transcript_facts",),
    "audio": ("audio",),
}


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


def _json_object(raw: bytes | str) -> dict[str, Any]:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "strict")
    value = json.loads(raw or "{}", parse_constant=_reject_constant)
    if not isinstance(value, dict):
        raise ValueError("quality evidence invalid")
    return value


def _finite(value: Any, *, minimum: float | None = None,
            maximum: float | None = None, integer: bool = False) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("quality number invalid")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("quality number non-finite")
    if minimum is not None and number < minimum:
        raise ValueError("quality number below range")
    if maximum is not None and number > maximum:
        raise ValueError("quality number above range")
    if integer:
        if not number.is_integer():
            raise ValueError("quality integer invalid")
        return int(number)
    return number


class LocalQualityRunner:
    """Collect technical evidence with ffprobe/ffmpeg and semantic evidence from
    the frozen render plan.  No shell or imaginary helper executable is used.
    """

    def __init__(self, process_runner: Callable[..., Any] = subprocess.run, *,
                 analyzer: Callable[..., Any] | None = None,
                 binary_finder: Callable[[str], str | None] = shutil.which) -> None:
        self.process_runner = process_runner
        self.analyzer = analyzer
        self.binary_finder = binary_finder

    def readiness_errors(self) -> list[str]:
        errors = [name for name in ("ffprobe", "ffmpeg") if self.binary_finder(name) is None]
        capabilities_fn = (
            getattr(self.analyzer, "capabilities", None)
            if callable(self.analyzer) else None
        )
        try:
            capabilities = capabilities_fn() if callable(capabilities_fn) else {}
        except Exception:
            capabilities = {}
        if not isinstance(capabilities, dict):
            capabilities = {}
        errors.extend(
            f"final_media_analyzer_{name}"
            for name in _ANALYZER_CAPABILITIES
            if capabilities.get(name) is not True
        )
        return errors

    @staticmethod
    def _expected(plan: dict[str, Any], check: str) -> dict[str, Any]:
        if check == "captions":
            return {"caption_plan": plan.get("caption_plan"),
                    "text_timeline": plan.get("text_timeline")}
        if check == "materials":
            return {"required_asset_ids": sorted(_required_ids(plan)),
                    "materials": plan.get("materials")}
        if check == "transcript":
            return {"text_timeline": plan.get("text_timeline")}
        if check == "audio":
            return {"audio_plan": plan.get("audio_plan")}
        raise ValueError("semantic quality check unsupported")

    def _analyze(self, check: str, path: str, plan: dict[str, Any]) -> dict[str, Any]:
        if self.analyzer is None:
            raise RuntimeError("final media analyzer unavailable")
        capabilities_fn = getattr(self.analyzer, "capabilities", None)
        capabilities = capabilities_fn() if callable(capabilities_fn) else {}
        if not isinstance(capabilities, dict) or any(
            capabilities.get(name) is not True for name in _CHECK_CAPABILITIES[check]
        ):
            raise RuntimeError("final media analyzer capability unavailable")
        value = self.analyzer(check, path=path, expected=self._expected(plan, check))
        if not isinstance(value, dict):
            raise ValueError("final media analysis invalid")
        return value

    def _run(self, command: list[str], timeout: int) -> Any:
        return self.process_runner(
            command, check=False, timeout=timeout,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )

    def __call__(self, check: str, *, path: str,
                 resolved_plan: dict[str, Any]) -> dict[str, Any]:
        if check == "probe":
            result = self._run([
                "ffprobe", "-v", "error", "-show_streams", "-show_format",
                "-of", "json", os.fspath(path),
            ], 30)
            if int(getattr(result, "returncode", 1)) != 0:
                raise RuntimeError("ffprobe failed")
            payload = _json_object(getattr(result, "stdout", b"") or b"")
            streams = payload.get("streams")
            if not isinstance(streams, list):
                raise ValueError("ffprobe streams invalid")
            video = next((x for x in streams if isinstance(x, dict) and x.get("codec_type") == "video"), None)
            audio = next((x for x in streams if isinstance(x, dict) and x.get("codec_type") == "audio"), None)
            rotation: Any = 0
            if video:
                tags = video.get("tags")
                if isinstance(tags, dict) and "rotate" in tags:
                    rotation = float(tags["rotate"])
                for item in video.get("side_data_list") or []:
                    if isinstance(item, dict) and "rotation" in item:
                        rotation = float(item["rotation"])
            fmt = payload.get("format")
            if not isinstance(fmt, dict) or "duration" not in fmt:
                raise ValueError("ffprobe duration missing")
            duration = float(fmt["duration"])
            if not math.isfinite(duration) or duration <= 0:
                raise ValueError("ffprobe duration invalid")
            return {
                "video": video is not None, "audio": audio is not None,
                "width": video.get("width") if video else None,
                "height": video.get("height") if video else None,
                "rotation": rotation, "duration_ms": round(duration * 1000),
            }
        if check in {"decode_video", "decode_audio"}:
            selector = "0:v:0" if check == "decode_video" else "0:a:0"
            result = self._run([
                "ffmpeg", "-v", "error", "-i", os.fspath(path),
                "-map", selector, "-f", "null", "-",
            ], 600)
            return {"decodable": int(getattr(result, "returncode", 1)) == 0}
        if check == "frames":
            result = self._run([
                "ffmpeg", "-v", "info", "-i", os.fspath(path), "-vf",
                "blackdetect=d=0.25:pix_th=0.10,freezedetect=n=-60dB:d=0.5",
                "-an", "-f", "null", "-",
            ], 600)
            if int(getattr(result, "returncode", 1)) != 0:
                raise RuntimeError("frame inspection failed")
            raw = getattr(result, "stderr", b"") or b""
            text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
            target = resolved_plan.get("target_duration_ms", resolved_plan.get("duration_ms"))
            duration = _finite(target, minimum=1) / 1000
            black = sum(float(x) for x in re.findall(r"black_duration:([0-9.]+)", text))
            blank = sum(float(x) for x in re.findall(r"freeze_duration:([0-9.]+)", text))
            return {"black_ratio": black / duration, "blank_ratio": blank / duration}
        if check == "audio":
            # The actual file must pass an audio analysis invocation.  Track-level
            # dialogue/BGM/SFX balance remains render-plan evidence because the
            # flattened master cannot recover those sources independently.
            result = self._run([
                "ffmpeg", "-v", "info", "-i", os.fspath(path), "-vn",
                "-af", "silencedetect=n=-50dB:d=0.2,ebur128=peak=true",
                "-f", "null", "-",
            ], 600)
            if int(getattr(result, "returncode", 1)) != 0:
                raise RuntimeError("audio inspection failed")
            return self._analyze("audio", path, resolved_plan)
        if check in {"captions", "materials", "transcript"}:
            return self._analyze(check, path, resolved_plan)
        raise ValueError("quality check unsupported")


def _required_ids(plan: dict[str, Any]) -> set[str]:
    direct = plan.get("required_materials") or []
    result = {
        str(item.get("asset_id")) for item in direct
        if isinstance(item, dict) and item.get("asset_id")
    }
    materials = plan.get("materials") or {}
    values = materials.values() if isinstance(materials, dict) else (
        materials if isinstance(materials, (list, tuple)) else ()
    )
    for item in values:
        if isinstance(item, dict) and item.get("required") and item.get("asset_id"):
            result.add(str(item["asset_id"]))
    return result


def inspect_output(path: str, resolved_plan: dict[str, Any],
                   runner: Callable[..., dict[str, Any]]) -> QualityReport:
    """Inspect the final file. Missing, malformed, or non-finite evidence fails closed."""
    evidence: dict[str, dict[str, Any]] = {}
    codes: list[str] = []
    layers: list[str] = []

    def issue(code: str, layer: str) -> None:
        if code not in codes:
            codes.append(code)
        if layer not in layers:
            layers.append(layer)

    for check in _CHECKS:
        try:
            value = runner(check, path=path, resolved_plan=resolved_plan)
            if not isinstance(value, dict):
                raise TypeError("inspection evidence must be a mapping")
            # Strict serialization also catches non-finite values nested in lists.
            evidence[check] = _json_object(json.dumps(value, allow_nan=False, ensure_ascii=False))
        except Exception:
            issue("inspection_incomplete", check)

    probe = evidence.get("probe", {})
    try:
        if probe.get("video") is not True or probe.get("audio") is not True:
            raise ValueError("streams invalid")
        width = _finite(probe.get("width"), minimum=1, maximum=16384, integer=True)
        height = _finite(probe.get("height"), minimum=1, maximum=16384, integer=True)
        rotation = _finite(probe.get("rotation"), minimum=-3600, maximum=3600, integer=True) % 360
        duration_ms = _finite(probe.get("duration_ms"), minimum=1, maximum=86_400_000, integer=True)
    except (TypeError, ValueError):
        issue("inspection_incomplete", "probe")
        width = height = duration_ms = None
        rotation = None
    if probe.get("video") is not True or evidence.get("decode_video", {}).get("decodable") is not True:
        issue("video_unplayable", "video")
    if probe.get("audio") is not True or evidence.get("decode_audio", {}).get("decodable") is not True:
        issue("audio_unplayable", "audio")
    ratio = resolved_plan.get("aspect_ratio")
    expected = (1920, 1080) if ratio == "16:9" else (1080, 1920) if ratio == "9:16" else None
    if expected is None or (width, height) != expected:
        issue("output_dimensions_invalid", "video")
    if rotation != 0:
        issue("output_rotation_invalid", "video")
    try:
        target = _finite(
            resolved_plan.get("target_duration_ms", resolved_plan.get("duration_ms")),
            minimum=1, maximum=86_400_000, integer=True,
        )
        tolerance = max(500, target * 2 // 100)
        if duration_ms is None or abs(duration_ms - target) > tolerance:
            issue("output_duration_mismatch", "assembly")
    except (TypeError, ValueError):
        issue("inspection_incomplete", "assembly")

    frames = evidence.get("frames", {})
    try:
        black = _finite(frames.get("black_ratio"), minimum=0, maximum=1)
        blank = _finite(frames.get("blank_ratio"), minimum=0, maximum=1)
        if black > 0.05:
            issue("black_frames_detected", "video")
        if blank > 0.02:
            issue("blank_frames_detected", "video")
    except (TypeError, ValueError):
        issue("inspection_incomplete", "frames")

    captions = evidence.get("captions", {})
    caption_bad = False
    try:
        safe = captions.get("safe_area")
        tofu = _finite(captions.get("tofu_count"), minimum=0, maximum=1_000_000, integer=True)
        missing = captions.get("missing_glyphs")
        if safe not in {True, False} or not isinstance(missing, list) or not all(isinstance(x, str) for x in missing):
            raise ValueError("caption evidence invalid")
        if safe is not True:
            issue("caption_out_of_safe_area", "captions"); caption_bad = True
        if tofu > 0:
            issue("caption_tofu_detected", "captions"); caption_bad = True
        if missing:
            issue("caption_glyph_missing", "captions"); caption_bad = True
    except (TypeError, ValueError):
        issue("inspection_incomplete", "captions")
    if caption_bad and "caption_invalid" not in codes:
        codes.insert(0, "caption_invalid")

    try:
        covered_values = evidence.get("materials", {}).get("covered_asset_ids")
        if not isinstance(covered_values, list) or not all(isinstance(x, (str, int)) and not isinstance(x, bool) for x in covered_values):
            raise ValueError("material evidence invalid")
        covered = {str(value) for value in covered_values}
        if not _required_ids(resolved_plan).issubset(covered):
            issue("required_material_missing", "materials")
    except (TypeError, ValueError):
        issue("inspection_incomplete", "materials")
    transcript = evidence.get("transcript", {})
    if transcript.get("source_matches") not in {True, False} or transcript.get("facts_match") not in {True, False}:
        issue("inspection_incomplete", "transcript")
    else:
        if transcript["source_matches"] is not True:
            issue("caption_source_mismatch", "transcript")
        if transcript["facts_match"] is not True:
            issue("caption_facts_mismatch", "transcript")

    audio = evidence.get("audio", {})
    try:
        silence = _finite(audio.get("silence_ratio"), minimum=0, maximum=1)
        peak = _finite(audio.get("true_peak_dbfs"), minimum=-200, maximum=20)
        bgm = _finite(audio.get("dialogue_to_bgm_db"), minimum=-200, maximum=200)
        sfx = _finite(audio.get("dialogue_to_sfx_db"), minimum=-200, maximum=200)
        if silence > 0.10:
            issue("audio_silence_detected", "audio")
        if peak > -0.1:
            issue("audio_clipping_detected", "audio")
        if bgm < 6.0:
            issue("dialogue_bgm_imbalance", "audio")
        if sfx < 6.0:
            issue("dialogue_sfx_imbalance", "audio")
    except (TypeError, ValueError):
        issue("inspection_incomplete", "audio")

    terminal = any(code in _TERMINAL_CODES for code in codes)
    return QualityReport(
        passed=not codes, error_codes=tuple(codes), failing_layers=tuple(layers),
        repairable=bool(codes) and not terminal, terminal=terminal,
    )
