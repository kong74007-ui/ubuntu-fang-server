from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


BLOCKING_CHECKS = (
    "stream_contract",
    "decoded_media",
    "monotonic_pts",
    "av_duration_sync",
    "integrated_loudness",
    "true_peak",
    "duplicate_dialogue",
    "abnormal_silence",
    "lip_audio_sync",
    "plan_hash",
    "schema_hash",
    "manifest_hash",
    "required_materials_resolved",
    "material_ownership",
    "prohibited_source_absent",
    "fact_traceability",
    "hook_accuracy",
    "unique_master_audio",
    "range_get_206",
)


@dataclass(frozen=True)
class CaseEvidence:
    checks: Mapping[str, bool | None]
    metrics: Mapping[str, int | float | None]
    analyzers: Mapping[str, Mapping[str, Any]]
    output_sha256: str | None
    lip_sync_applicable: bool | None


@dataclass(frozen=True)
class MachineVerdict:
    passed: bool
    blockers: tuple[str, ...]


@dataclass(frozen=True)
class MachineSummary:
    passed: bool
    total: int
    passed_count: int
    failed_count: int
    blockers: tuple[str, ...]


@dataclass(frozen=True)
class OutputProbe:
    checks: Mapping[str, bool]
    metrics: Mapping[str, int | float | None]
    errors: tuple[str, ...]
    output_sha256: str | None


def load_quality_evidence(path: Path) -> CaseEvidence:
    evidence_path = Path(path)
    try:
        payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("quality_evidence_json_invalid") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("quality_evidence_json_invalid")
    checks = payload.get("checks")
    metrics = payload.get("metrics")
    output_sha256 = payload.get("output_sha256")
    artifact_names = payload.get("analyzer_artifacts")
    if (
        not isinstance(checks, Mapping)
        or not isinstance(metrics, Mapping)
        or not isinstance(artifact_names, Mapping)
        or not re.fullmatch(r"[0-9a-f]{64}", str(output_sha256 or ""))
        or any(value is not True and value is not False and value is not None for value in checks.values())
        or any(
            value is not None and (
                isinstance(value, bool) or not isinstance(value, (int, float))
            )
            for value in metrics.values()
        )
    ):
        raise ValueError("quality_evidence_json_invalid")
    loaded_metrics = dict(metrics)
    analyzers: dict[str, Mapping[str, Any]] = {}
    lip_sync_applicable: bool | None = None
    for role in ("duplicate_dialogue", "lip_audio_sync"):
        filename = artifact_names.get(role)
        if (
            not isinstance(filename, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,126}\.json", filename)
        ):
            raise ValueError(f"analyzer_artifact_invalid:{role}")
        try:
            evidence_parent = evidence_path.parent.resolve(strict=True)
            artifact_path = (evidence_parent / filename).resolve(strict=True)
        except OSError as exc:
            raise ValueError(f"analyzer_artifact_invalid:{role}") from exc
        if artifact_path.parent != evidence_parent:
            raise ValueError(f"analyzer_artifact_escape:{role}")
        artifact, artifact_sha256 = _load_analyzer_artifact(artifact_path)
        if artifact.get("output_sha256") != output_sha256:
            raise ValueError(f"analyzer_output_sha_mismatch:{role}")
        analyzer_id = artifact.get("analyzer_id")
        version = artifact.get("analyzer_version")
        analyzer_metrics = artifact.get("metrics")
        if not isinstance(analyzer_metrics, Mapping):
            raise ValueError(f"analyzer_metrics_invalid:{role}")
        if role == "duplicate_dialogue":
            if (analyzer_id, version) != ("dialogue-fingerprint", "1.0.0"):
                raise ValueError("analyzer_identity_invalid:duplicate_dialogue")
            loaded_metrics["duplicate_dialogue_count"] = analyzer_metrics.get(
                "duplicate_dialogue_count"
            )
        else:
            applicable = artifact.get("applicable")
            if applicable is True:
                if (analyzer_id, version) != ("talking-head-av-sync", "1.0.0"):
                    raise ValueError("analyzer_identity_invalid:lip_audio_sync")
                loaded_metrics["lip_audio_offset_ms"] = analyzer_metrics.get(
                    "lip_audio_offset_ms"
                )
                lip_sync_applicable = True
            elif applicable is False:
                if (
                    (analyzer_id, version) != ("talking-head-presence", "1.0.0")
                    or analyzer_metrics.get("talking_head_present") is not False
                ):
                    raise ValueError("analyzer_identity_invalid:lip_audio_sync_na")
                loaded_metrics.pop("lip_audio_offset_ms", None)
                lip_sync_applicable = False
            else:
                raise ValueError("lip_sync_applicability_invalid")
        analyzers[role] = {
            "name": analyzer_id,
            "version": version,
            "evidence_sha256": artifact_sha256,
            "output_sha256": output_sha256,
            "verified": True,
        }
    return CaseEvidence(
        checks=dict(checks),
        metrics=loaded_metrics,
        analyzers=analyzers,
        output_sha256=str(output_sha256),
        lip_sync_applicable=lip_sync_applicable,
    )


def _load_analyzer_artifact(path: Path) -> tuple[Mapping[str, Any], str]:
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("analyzer_artifact_invalid") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("analyzer_artifact_invalid")
    return payload, hashlib.sha256(raw).hexdigest()


def verify_quality_evidence(evidence: CaseEvidence) -> MachineVerdict:
    derived = _metric_checks(evidence)
    blockers: list[str] = []
    for name in BLOCKING_CHECKS:
        value = derived.get(name) if name in derived else evidence.checks.get(name)
        if value is None:
            blockers.append(f"quality_evidence_missing:{name}")
        elif value is not True:
            blockers.append(f"quality_evidence_failed:{name}")
    return MachineVerdict(passed=not blockers, blockers=tuple(blockers))


def _metric_checks(evidence: CaseEvidence) -> dict[str, bool | None]:
    metrics = evidence.metrics
    def finite(name: str) -> float | None:
        value = metrics.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        number = float(value)
        return number if math.isfinite(number) else float("nan")

    lufs = finite("integrated_lufs")
    peak = finite("true_peak_dbtp")
    duplicate_raw = metrics.get("duplicate_dialogue_count")
    silence_raw = metrics.get("abnormal_silence_count")
    duplicate = (
        duplicate_raw
        if isinstance(duplicate_raw, int) and not isinstance(duplicate_raw, bool) and duplicate_raw >= 0
        else float("nan") if duplicate_raw is not None else None
    )
    silence = (
        silence_raw
        if isinstance(silence_raw, int) and not isinstance(silence_raw, bool) and silence_raw >= 0
        else float("nan") if silence_raw is not None else None
    )
    lip = finite("lip_audio_offset_ms")
    duplicate_binding = _analyzer_binding(evidence, "duplicate_dialogue")
    lip_binding = _analyzer_binding(evidence, "lip_audio_sync")
    if evidence.lip_sync_applicable is False:
        lip_result = lip_binding
    elif evidence.lip_sync_applicable is True:
        lip_result = (
            None if lip is None or lip_binding is None
            else abs(lip) <= 80 and lip_binding
        )
    else:
        lip_result = None
    return {
        "integrated_loudness": None if lufs is None else -18 <= lufs <= -14,
        "true_peak": None if peak is None else peak <= -1,
        "duplicate_dialogue": (
            None if duplicate is None or duplicate_binding is None
            else duplicate == 0 and duplicate_binding
        ),
        "abnormal_silence": None if silence is None else silence == 0,
        "lip_audio_sync": lip_result,
    }


def _analyzer_binding(evidence: CaseEvidence, name: str) -> bool | None:
    binding = evidence.analyzers.get(name)
    if binding is None:
        return None
    if not isinstance(binding, Mapping):
        return False
    expected_name = {
        "duplicate_dialogue": "dialogue-fingerprint",
        "lip_audio_sync": (
            "talking-head-av-sync"
            if evidence.lip_sync_applicable is True
            else "talking-head-presence"
        ),
    }[name]
    return bool(
        binding.get("name") == expected_name
        and binding.get("version") == "1.0.0"
        and binding.get("verified") is True
        and re.fullmatch(r"[0-9a-f]{64}", str(binding.get("evidence_sha256", "")))
        and re.fullmatch(r"[0-9a-f]{64}", str(binding.get("output_sha256", "")))
        and binding.get("output_sha256") == evidence.output_sha256
    )


def aggregate_machine_verdicts(verdicts: Sequence[MachineVerdict]) -> MachineSummary:
    blockers = tuple(
        f"case_{index:03d}:{blocker}"
        for index, verdict in enumerate(verdicts, start=1)
        for blocker in verdict.blockers
    )
    passed_count = sum(verdict.passed for verdict in verdicts)
    return MachineSummary(
        passed=bool(verdicts) and passed_count == len(verdicts),
        total=len(verdicts),
        passed_count=passed_count,
        failed_count=len(verdicts) - passed_count,
        blockers=blockers,
    )


def probe_final_output(
    path: Path,
    *,
    process_runner: Callable[..., Any] = subprocess.run,
    timeout_seconds: int = 900,
) -> OutputProbe:
    """Probe an acceptance output without allowing a partial probe to pass.

    This is deliberately limited to objective container, stream and loudness
    evidence. Semantic, ownership and lip-sync evidence is supplied by the
    corresponding analyzers and is still required by ``verify_quality_evidence``.
    """
    media = Path(path)
    checks = {
        "stream_contract": False,
        "decoded_media": False,
        "monotonic_pts": False,
        "av_duration_sync": False,
        "integrated_loudness": False,
        "true_peak": False,
        "abnormal_silence": False,
    }
    metrics: dict[str, int | float | None] = {
        "av_duration_difference_ms": None,
        "frame_duration_ms": None,
        "integrated_lufs": None,
        "true_peak_dbtp": None,
        "abnormal_silence_count": None,
    }
    errors: list[str] = []
    if not media.is_file():
        return OutputProbe(checks, metrics, ("media_missing",), None)
    media = media.resolve(strict=True)
    output_sha256 = _sha256(media)

    probe_command = [
        "ffprobe", "-v", "error", "-show_streams",
        "-show_entries",
        "stream=index,codec_type,codec_name,pix_fmt,width,height,r_frame_rate,sample_rate,channels,duration,start_time",
        "-of", "json", os.fspath(media),
    ]
    packet_command = [
        "ffprobe", "-v", "error", "-show_packets",
        "-show_entries", "packet=stream_index,pts_time,dts_time",
        "-of", "json", os.fspath(media),
    ]
    frame_command = [
        "ffprobe", "-v", "error", "-show_frames",
        "-show_entries", "frame=stream_index,best_effort_timestamp_time",
        "-of", "json", os.fspath(media),
    ]
    decode_command = [
        "ffmpeg", "-v", "error", "-xerror", "-err_detect", "explode", "-nostdin",
        "-i", os.fspath(media),
        "-map", "0:v:0", "-map", "0:a:0", "-f", "null", os.devnull,
    ]
    loudnorm_command = [
        "ffmpeg", "-hide_banner", "-nostdin", "-v", "info", "-i", os.fspath(media),
        "-vn", "-af",
        "silencedetect=n=-50dB:d=0.5,loudnorm=I=-16:TP=-1:LRA=11:print_format=json",
        "-f", "null", os.devnull,
    ]

    probe_result = _run(process_runner, probe_command, timeout_seconds)
    if probe_result is None or probe_result.returncode != 0:
        return OutputProbe(checks, metrics, ("probe_failed",), output_sha256)
    try:
        payload = json.loads(_text(probe_result.stdout))
        streams = payload["streams"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return OutputProbe(checks, metrics, ("probe_json_invalid",), output_sha256)
    if not isinstance(streams, list) or any(not isinstance(item, Mapping) for item in streams):
        return OutputProbe(checks, metrics, ("probe_json_invalid",), output_sha256)

    videos = [stream for stream in streams if stream.get("codec_type") == "video"]
    audios = [stream for stream in streams if stream.get("codec_type") == "audio"]
    video = videos[0] if len(videos) == 1 else {}
    audio = audios[0] if len(audios) == 1 else {}
    fps = _fraction(video.get("r_frame_rate"))
    checks["stream_contract"] = bool(
        len(videos) == 1
        and len(audios) == 1
        and video.get("codec_name") == "h264"
        and video.get("pix_fmt") == "yuv420p"
        and (video.get("width"), video.get("height")) in {(1920, 1080), (1080, 1920)}
        and fps == 30
        and audio.get("codec_name") == "aac"
        and _integer(audio.get("sample_rate")) == 48_000
        and _integer(audio.get("channels")) == 2
    )

    video_duration = _seconds(video.get("duration"))
    audio_duration = _seconds(audio.get("duration"))
    if fps and video_duration is not None and audio_duration is not None:
        frame_ms = 1000 / fps
        difference_ms = abs(video_duration - audio_duration) * 1000
        metrics["frame_duration_ms"] = round(frame_ms, 3)
        metrics["av_duration_difference_ms"] = round(difference_ms)
        checks["av_duration_sync"] = difference_ms <= min(frame_ms, 40) + 1e-6
    else:
        errors.append("duration_evidence_missing")

    packet_ok = False
    packet_result = _run(process_runner, packet_command, timeout_seconds)
    if packet_result is not None and packet_result.returncode == 0:
        try:
            packets = json.loads(_text(packet_result.stdout))["packets"]
            packet_ok = _monotonic_timestamps(
                packets, videos, audios, timestamp_key="dts_time"
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            errors.append("packet_json_invalid")
    else:
        errors.append("packet_probe_failed")

    frame_ok = False
    frame_result = _run(process_runner, frame_command, timeout_seconds)
    if frame_result is not None and frame_result.returncode == 0:
        try:
            frames = json.loads(_text(frame_result.stdout))["frames"]
            frame_ok = _monotonic_timestamps(
                frames, videos, audios, timestamp_key="best_effort_timestamp_time"
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            errors.append("frame_json_invalid")
    else:
        errors.append("frame_probe_failed")
    checks["monotonic_pts"] = packet_ok and frame_ok

    decode_result = _run(process_runner, decode_command, timeout_seconds)
    checks["decoded_media"] = decode_result is not None and decode_result.returncode == 0
    if not checks["decoded_media"]:
        errors.append("decode_failed")

    loudnorm_result = _run(process_runner, loudnorm_command, timeout_seconds)
    if loudnorm_result is not None and loudnorm_result.returncode == 0:
        loudness = _loudnorm_metrics(_text(loudnorm_result.stderr))
        if loudness is not None:
            integrated_lufs, true_peak_dbtp = loudness
            metrics["integrated_lufs"] = integrated_lufs
            metrics["true_peak_dbtp"] = true_peak_dbtp
            abnormal_silence_count = sum(
                float(value) * 1000 >= 500
                for value in re.findall(
                    r"silence_duration:\s*([0-9.]+)", _text(loudnorm_result.stderr)
                )
            )
            metrics["abnormal_silence_count"] = abnormal_silence_count
            checks["integrated_loudness"] = -18 <= integrated_lufs <= -14
            checks["true_peak"] = true_peak_dbtp <= -1
            checks["abnormal_silence"] = abnormal_silence_count == 0
        else:
            errors.append("loudnorm_json_invalid")
    else:
        errors.append("loudnorm_failed")
    return OutputProbe(checks, metrics, tuple(errors), output_sha256)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(runner: Callable[..., Any], command: list[str], timeout_seconds: int) -> Any | None:
    try:
        return runner(
            command,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
            shell=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None


def _text(value: str | bytes) -> str:
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value


def _integer(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _seconds(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _fraction(value: Any) -> float | None:
    try:
        numerator, denominator = str(value).split("/", 1)
        result = float(numerator) / float(denominator)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    return result if math.isfinite(result) and result > 0 else None


def _monotonic_timestamps(
    records: Sequence[Mapping[str, Any]],
    videos: Sequence[Mapping[str, Any]],
    audios: Sequence[Mapping[str, Any]],
    *,
    timestamp_key: str,
) -> bool:
    if len(videos) != 1 or len(audios) != 1:
        return False
    try:
        required = {int(videos[0]["index"]), int(audios[0]["index"])}
        previous: dict[int, float] = {}
        counts = {index: 0 for index in required}
        for record in records:
            if not isinstance(record, Mapping):
                return False
            index = int(record["stream_index"])
            if index not in required:
                continue
            timestamp = float(record[timestamp_key])
            if (
                not math.isfinite(timestamp)
                or (index in previous and timestamp <= previous[index])
            ):
                return False
            previous[index] = timestamp
            counts[index] += 1
    except (KeyError, TypeError, ValueError):
        return False
    return all(counts.values())


def _loudnorm_metrics(stderr: str) -> tuple[float, float] | None:
    for candidate in reversed(re.findall(r"\{[^{}]*\}", stderr, flags=re.S)):
        try:
            payload = json.loads(candidate)
            integrated = float(payload["input_i"])
            peak = float(payload["input_tp"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if math.isfinite(integrated) and math.isfinite(peak):
            return integrated, peak
    return None
