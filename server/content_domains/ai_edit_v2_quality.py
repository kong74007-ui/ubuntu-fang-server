"""Fail-closed hard quality gates for AI Edit V2 final outputs."""

from __future__ import annotations

import json
import os
import re
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


def _completed_payload(result: Any) -> dict[str, Any]:
    if int(getattr(result, "returncode", 1)) != 0:
        raise RuntimeError("quality command failed")
    raw = getattr(result, "stdout", b"") or b""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    value = json.loads(raw or "{}")
    if not isinstance(value, dict):
        raise ValueError("quality evidence invalid")
    return value


def _command_evidence(
    check: str, path: str, resolved_plan: dict[str, Any], runner: Callable[..., Any]
) -> dict[str, Any]:
    if check == "probe":
        result = runner(
            ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", os.fspath(path)],
            check=False, timeout=30, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        payload = _completed_payload(result)
        streams = payload.get("streams") or []
        video = next((item for item in streams if item.get("codec_type") == "video"), None)
        audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
        tags = video.get("tags") if isinstance(video, dict) else {}
        side_data = video.get("side_data_list") if isinstance(video, dict) else []
        rotation = (tags or {}).get("rotate", 0)
        for item in side_data or []:
            if "rotation" in item:
                rotation = item["rotation"]
        return {
            "video": video is not None, "audio": audio is not None,
            "width": video.get("width") if video else None,
            "height": video.get("height") if video else None,
            "rotation": rotation,
            "duration_ms": round(float((payload.get("format") or {}).get("duration")) * 1000),
        }
    if check in {"decode_video", "decode_audio"}:
        selector = "0:v:0" if check == "decode_video" else "0:a:0"
        result = runner(
            ["ffmpeg", "-v", "error", "-i", os.fspath(path), "-map", selector, "-f", "null", "-"],
            check=False, timeout=600, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        return {"decodable": int(getattr(result, "returncode", 1)) == 0}
    if check == "frames":
        result = runner(
            ["ffmpeg", "-v", "info", "-i", os.fspath(path), "-vf",
             "blackdetect=d=0.25:pix_th=0.10,freezedetect=n=-60dB:d=0.5", "-an", "-f", "null", "-"],
            check=False, timeout=600, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if int(getattr(result, "returncode", 1)) != 0:
            raise RuntimeError("frame inspection failed")
        raw = getattr(result, "stderr", b"") or b""
        text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        duration = max(1, int(resolved_plan.get("duration_ms") or 1)) / 1000
        black = sum(float(value) for value in re.findall(r"black_duration:([0-9.]+)", text))
        blank = sum(float(value) for value in re.findall(r"freeze_duration:([0-9.]+)", text))
        return {"black_ratio": black / duration, "blank_ratio": blank / duration}
    result = runner(
        ["ai-edit-v2-quality-inspect", check, os.fspath(path)],
        check=False, timeout=600, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        resolved_plan=resolved_plan,
    )
    return _completed_payload(result)


def _run_evidence(check: str, path: str, plan: dict[str, Any], runner: Callable[..., Any]) -> dict[str, Any]:
    try:
        direct = runner(check, path=path, resolved_plan=plan)
        if isinstance(direct, dict):
            return direct
    except (TypeError, AttributeError):
        pass
    return _command_evidence(check, path, plan, runner)


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


def inspect_output(
    path: str,
    resolved_plan: dict[str, Any],
    runner: Callable[..., dict[str, Any]],
) -> QualityReport:
    """Inspect one final output using a deterministic, injectable evidence runner.

    The runner is invoked once for each named hard gate and must return a mapping.
    Missing or malformed evidence fails closed instead of silently approving output.
    """

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
            value = _run_evidence(check, path, resolved_plan, runner)
            if not isinstance(value, dict):
                raise TypeError("inspection evidence must be a mapping")
            evidence[check] = value
        except Exception:
            issue("inspection_incomplete", check)

    probe = evidence.get("probe", {})
    if not probe.get("video"):
        issue("video_unplayable", "video")
    if not probe.get("audio"):
        issue("audio_unplayable", "audio")
    if evidence.get("decode_video", {}).get("decodable") is not True:
        issue("video_unplayable", "video")
    if evidence.get("decode_audio", {}).get("decodable") is not True:
        issue("audio_unplayable", "audio")

    ratio = resolved_plan.get("aspect_ratio")
    expected = (1920, 1080) if ratio == "16:9" else (1080, 1920) if ratio == "9:16" else None
    actual = (probe.get("width"), probe.get("height"))
    if expected is None or actual != expected:
        issue("output_dimensions_invalid", "video")
    try:
        rotation = int(probe.get("rotation", 0) or 0) % 360
    except (TypeError, ValueError):
        rotation = -1
    if rotation != 0:
        issue("output_rotation_invalid", "video")
    target_ms = resolved_plan.get("target_duration_ms") or resolved_plan.get("duration_ms")
    try:
        tolerance = max(500, int(target_ms) * 2 // 100)
        if abs(int(probe.get("duration_ms")) - int(target_ms)) > tolerance:
            issue("output_duration_mismatch", "assembly")
    except (TypeError, ValueError):
        issue("output_duration_mismatch", "assembly")

    frames = evidence.get("frames", {})
    try:
        if float(frames.get("black_ratio", 1.0)) > 0.05:
            issue("black_frames_detected", "video")
        if float(frames.get("blank_ratio", 1.0)) > 0.02:
            issue("blank_frames_detected", "video")
    except (TypeError, ValueError):
        issue("inspection_incomplete", "frames")

    captions = evidence.get("captions", {})
    caption_bad = False
    if captions.get("safe_area") is not True:
        issue("caption_out_of_safe_area", "captions")
        caption_bad = True
    if int(captions.get("tofu_count", 1) or 0) > 0:
        issue("caption_tofu_detected", "captions")
        caption_bad = True
    if captions.get("missing_glyphs"):
        issue("caption_glyph_missing", "captions")
        caption_bad = True
    if caption_bad:
        codes.insert(0, "caption_invalid")

    covered = {str(value) for value in evidence.get("materials", {}).get("covered_asset_ids", [])}
    if not _required_ids(resolved_plan).issubset(covered):
        issue("required_material_missing", "materials")
    transcript = evidence.get("transcript", {})
    if transcript.get("source_matches") is not True:
        issue("caption_source_mismatch", "transcript")
    if transcript.get("facts_match") is not True:
        issue("caption_facts_mismatch", "transcript")

    audio = evidence.get("audio", {})
    try:
        if float(audio.get("silence_ratio", 1.0)) > 0.10:
            issue("audio_silence_detected", "audio")
        if float(audio.get("true_peak_dbfs", 1.0)) > -0.1:
            issue("audio_clipping_detected", "audio")
        if float(audio.get("dialogue_to_bgm_db", -99)) < 6.0:
            issue("dialogue_bgm_imbalance", "audio")
        if float(audio.get("dialogue_to_sfx_db", -99)) < 6.0:
            issue("dialogue_sfx_imbalance", "audio")
    except (TypeError, ValueError):
        issue("inspection_incomplete", "audio")

    terminal = any(code in _TERMINAL_CODES for code in codes)
    return QualityReport(
        passed=not codes,
        error_codes=tuple(codes),
        failing_layers=tuple(layers),
        repairable=bool(codes) and not terminal,
        terminal=terminal,
    )
