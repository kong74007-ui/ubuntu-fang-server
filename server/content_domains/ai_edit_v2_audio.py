"""Deterministic audio cue planning, degradation, and final mastering."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable

from .ai_edit_v2_providers.base import ProviderError


class AudioError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


_PROTECTED_TEXT = re.compile(
    r"(?:\d|品牌|产品|商品|价格|价钱|售价|元|块|折|￥|¥|RMB|USD)", re.IGNORECASE
)
_CUE_PRIORITY = {"camera_cut": 1, "semantic_turn": 2, "emphasis": 3}
BGM_MIX_VOLUME = 0.18
SFX_MIX_VOLUME = 0.04


def build_audio_plan(edit_plan: dict[str, Any], text_timeline: dict[str, Any]) -> dict[str, Any]:
    """Build only semantic cues, suppressing cues over protected speech facts."""

    policy = edit_plan.get("audio_plan") or {}
    duration_ms = int(edit_plan.get("duration_ms") or text_timeline.get("duration_ms") or 0)
    music_policy = policy.get("music_policy", "duck_under_speech")
    sfx_policy = policy.get("sfx_policy", "semantic_only")
    bgm = None
    if music_policy != "none":
        bgm = {
            "prompt": "Restrained instrumental background music, no vocals and no lyrics",
            "duration_ms": duration_ms,
            "force_instrumental": True,
            "duck_under_speech": True,
        }

    cues: list[dict[str, Any]] = []
    if sfx_policy != "none":
        for scene in list(edit_plan.get("scenes") or [])[1:]:
            at_ms = int(scene.get("start_ms", 0))
            transition = str(scene.get("transition", ""))
            intent = str(scene.get("intent", "")) + " " + str(scene.get("headline", ""))
            if re.search(r"重点|强调|关键|important|emphasis", intent, re.IGNORECASE):
                kind, prompt, cue_duration = "emphasis", "subtle emphasis accent", 500
            elif transition in {"fade", "dissolve", "wipe"}:
                kind, prompt, cue_duration = "semantic_turn", "soft semantic transition", 500
            else:
                kind, prompt, cue_duration = "camera_cut", "soft camera cut", 500
            cues.append(
                {
                    "kind": kind,
                    "prompt": prompt,
                    "at_ms": at_ms,
                    "duration_ms": cue_duration,
                    "required": False,
                }
            )

    cues.sort(key=lambda cue: cue["at_ms"])
    merged: list[dict[str, Any]] = []
    for cue in cues:
        if merged and cue["at_ms"] - merged[-1]["at_ms"] < 300:
            if _CUE_PRIORITY[cue["kind"]] > _CUE_PRIORITY[merged[-1]["kind"]]:
                merged[-1] = cue
            continue
        merged.append(cue)

    protected = _protected_ranges(text_timeline)
    safe_cues = [
        cue
        for cue in merged
        if not any(
            cue["at_ms"] < item["end_ms"]
            and cue["at_ms"] + cue["duration_ms"] > item["start_ms"]
            for item in protected
        )
    ]
    return {"bgm": bgm, "sfx": safe_cues, "degradations": []}


def _protected_ranges(text_timeline: dict[str, Any]) -> list[dict[str, int]]:
    ranges: list[dict[str, int]] = []
    verified_ranges = text_timeline.get("protected_ranges") or []
    for value in verified_ranges:
        try:
            start_ms, end_ms = int(value["start_ms"]), int(value["end_ms"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= start_ms < end_ms:
            ranges.append({"start_ms": start_ms, "end_ms": end_ms})
    for word in text_timeline.get("words") or []:
        # Upstream entity ranges are authoritative. If they are absent, protect all
        # speech conservatively because an unknown proper noun may be a real brand or
        # product; otherwise retain the number/price keyword safety net.
        if verified_ranges and not _PROTECTED_TEXT.search(str(word.get("text", ""))):
            continue
        try:
            start_ms, end_ms = int(word["start_ms"]), int(word["end_ms"])
        except (KeyError, TypeError, ValueError):
            continue
        if 0 <= start_ms < end_ms:
            ranges.append({"start_ms": start_ms, "end_ms": end_ms})
    return ranges


def generate_audio_assets(
    job_id: str, audio_plan: dict[str, Any], provider: Any
) -> dict[str, Any]:
    """Generate optional audio with explicit, item-level degradation records."""

    result: dict[str, Any] = {"bgm": None, "sfx": [], "degradations": []}
    bgm = audio_plan.get("bgm")
    if bgm:
        try:
            generated = provider.generate_music(
                bgm["prompt"], int(bgm["duration_ms"]), f"{job_id}:music"
            )
            result["bgm"] = {**bgm, **generated.payload, "provider_result": generated}
        except ProviderError:
            result["degradations"].append("music_generation_degraded")

    for index, cue in enumerate(audio_plan.get("sfx") or []):
        try:
            generated = provider.generate_sfx(
                cue["prompt"], int(cue["duration_ms"]), f"{job_id}:sfx:{index}"
            )
            result["sfx"].append({**cue, **generated.payload, "provider_result": generated})
        except ProviderError:
            if cue.get("required"):
                raise
            if "sfx_generation_degraded" not in result["degradations"]:
                result["degradations"].append("sfx_generation_degraded")
    return result


def mix_audio(
    video_path: str,
    voice_path: str,
    bgm_path: str | None,
    sfx: list[dict[str, Any]],
    output_path: str,
    runner: Callable[..., Any],
) -> str:
    """Duck music below dialogue and apply EBU-style loudness in two passes."""

    del video_path  # Voice is an explicit normalized input; video is retained by API contract.
    if not voice_path or not Path(voice_path).is_file():
        raise AudioError("audio_voice_missing")
    inputs = ["-i", voice_path]
    bgm_index: int | None = None
    if bgm_path:
        bgm_index = 1
        inputs.extend(["-i", bgm_path])
    sfx_indices: list[tuple[int, dict[str, Any]]] = []
    for cue in sfx:
        path = cue.get("path")
        if not path:
            continue
        index = 1 + (1 if bgm_path else 0) + len(sfx_indices)
        sfx_indices.append((index, cue))
        inputs.extend(["-i", str(path)])

    pre_master, mix_label = _mix_filter(bgm_index, sfx_indices)
    measurement_filter = (
        pre_master
        + f";[{mix_label}]loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json[master]"
    )
    first_command = [
        "ffmpeg", "-hide_banner", "-nostdin", "-y", *inputs,
        "-filter_complex", measurement_filter, "-map", "[master]", "-f", "null", "NUL",
    ]
    measured = _run_ffmpeg(first_command, runner)
    stats = _parse_loudness(measured)
    applied_filter = (
        pre_master
        + f";[{mix_label}]loudnorm=I=-16:TP=-1.5:LRA=11:"
        + f"measured_I={stats['input_i']}:measured_TP={stats['input_tp']}:"
        + f"measured_LRA={stats['input_lra']}:measured_thresh={stats['input_thresh']}:"
        + f"offset={stats['target_offset']}:linear=true:print_format=summary[master]"
    )
    second_command = [
        "ffmpeg", "-hide_banner", "-nostdin", "-y", *inputs,
        "-filter_complex", applied_filter, "-map", "[master]",
        "-c:a", "aac", "-b:a", "192k", output_path,
    ]
    _run_ffmpeg(second_command, runner)
    output = Path(output_path)
    if not output.is_file() or output.stat().st_size <= 0:
        raise AudioError("audio_mix_empty_output")
    return output_path


def _mix_filter(
    bgm_index: int | None, sfx_indices: list[tuple[int, dict[str, Any]]]
) -> tuple[str, str]:
    filters: list[str] = []
    labels = ["[0:a]"]
    if bgm_index is not None:
        filters.append(
            f"[{bgm_index}:a][0:a]sidechaincompress=threshold=0.03:ratio=8:"
            f"attack=20:release=300[ducked_raw];[ducked_raw]volume={BGM_MIX_VOLUME:.2f}[ducked]"
        )
        labels.append("[ducked]")
    for number, (input_index, cue) in enumerate(sfx_indices):
        delay = max(0, int(cue.get("at_ms", 0)))
        duration_seconds = max(500, int(cue.get("duration_ms", 500))) / 1000
        label = f"sfx{number}"
        filters.append(
            f"[{input_index}:a]atrim=duration={duration_seconds:g},"
            f"asetpts=PTS-STARTPTS,adelay={delay}|{delay},"
            f"volume={SFX_MIX_VOLUME:.2f}[{label}]"
        )
        labels.append(f"[{label}]")
    if len(labels) == 1:
        filters.append("[0:a]anull[premaster]")
    else:
        filters.append(
            "".join(labels)
            + f"amix=inputs={len(labels)}:duration=longest:normalize=0[premaster]"
        )
    return ";".join(filters), "premaster"


def _run_ffmpeg(command: list[str], runner: Callable[..., Any]) -> bytes:
    try:
        completed = runner(command, capture_output=True, timeout=600)
    except subprocess.TimeoutExpired as exc:
        raise AudioError("audio_mix_timeout") from exc
    except Exception as exc:
        raise AudioError("audio_mix_failed") from exc
    stderr = completed.stderr or b""
    if isinstance(stderr, str):
        stderr = stderr.encode("utf-8", errors="replace")
    lowered = stderr.lower()
    if b"clipping detected" in lowered:
        raise AudioError("audio_mix_clipping")
    if int(getattr(completed, "returncode", 1)) != 0:
        if b"clipping" in lowered or b"true peak" in lowered:
            raise AudioError("audio_mix_clipping")
        if any(
            marker in lowered
            for marker in (b"matches no streams", b"stream specifier", b"no audio stream")
        ):
            raise AudioError("audio_voice_missing")
        raise AudioError("audio_mix_failed")
    return stderr


def _parse_loudness(stderr: bytes) -> dict[str, str]:
    text = stderr.decode("utf-8", errors="replace")
    for match in reversed(list(re.finditer(r"\{[^{}]+\}", text, re.DOTALL))):
        try:
            value = json.loads(match.group(0))
        except json.JSONDecodeError:
            continue
        required = {"input_i", "input_tp", "input_lra", "input_thresh", "target_offset"}
        if required.issubset(value):
            return {key: str(value[key]) for key in required}
    raise AudioError("audio_loudness_measurement_invalid")
