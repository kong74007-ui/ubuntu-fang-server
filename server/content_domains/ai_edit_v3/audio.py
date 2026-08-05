"""Deterministic AI Edit V3 audio planning, generation and mastering."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
from typing import Any, Literal, Mapping, Protocol, Sequence

from .contracts import canonical_json
from .media import MediaProcessError, probe_media, run_media_process
from .providers.base import DefinitiveNotAccepted, ProviderResult, SubmissionUnknown
from .providers.elevenlabs import AudioGenerator, MusicGenerationRequest, SfxGenerationRequest
from .transcript import SourceSegment, TextTimeline


_ID = re.compile(r"[a-z0-9_]{1,64}\Z")
_PROTECTED_FACT = re.compile(r"\d|品牌|产品|价格|售价|元|折|型号|不能|不是|不含")
_SFX_ROLES = frozenset({"reversal", "number", "method", "transition", "cta"})
_MASTER_TRUE_PEAK_TARGET_DBTP = -1.5
_PROVIDER_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_AUDIO_USAGE_KEYS = frozenset({"credits", "characters"})


class AudioPlanError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class AudioGenerationError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class PrivateCos(Protocol):
    def put_file(
        self,
        source: Path,
        object_key: str,
        content_type: str,
        *,
        private: bool,
        if_absent: bool,
    ) -> Mapping[str, Any]: ...

    def download_file(self, key: str, destination: str | Path) -> str: ...


@dataclass(frozen=True, slots=True)
class TimeRange:
    start_ms: int
    end_ms: int


@dataclass(frozen=True, slots=True)
class VolumeFade:
    cue_id: str
    target: str
    start_ms: int
    end_ms: int
    from_db: float
    to_db: float


@dataclass(frozen=True, slots=True)
class AudioGenerationPlan:
    music: MusicGenerationRequest
    sfx: tuple[SfxGenerationRequest, ...]
    volume_fades: tuple[VolumeFade, ...]
    protected_ranges: tuple[TimeRange, ...]
    duration_ms: int


@dataclass(frozen=True, slots=True)
class GeneratedAudioAsset:
    cue_id: str
    kind: Literal["bgm", "sfx"]
    relative_path: str
    object_key: str
    sha256: str
    duration_ms: int
    sample_rate: int
    channels: int
    provider_request_id: str
    usage: Mapping[str, int | float]


@dataclass(frozen=True, slots=True)
class MasterAudio:
    relative_path: str
    sha256: str
    duration_ms: int
    sample_rate: Literal[48000]
    channels: Literal[2]
    integrated_lufs: float
    true_peak_dbtp: float
    audit: Mapping[str, Any]


def _integer(value: object, code: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise AudioPlanError(code)
    return value


def _db(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AudioPlanError("volume_fade_db_invalid")
    result = float(value)
    if not math.isfinite(result) or result < -60 or result > 0:
        raise AudioPlanError("volume_fade_db_invalid")
    return result


def _identifier(value: object, code: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise AudioPlanError(code)
    return value


def _ranges_overlap(left: TimeRange, right: TimeRange) -> bool:
    return left.start_ms < right.end_ms and right.start_ms < left.end_ms


def _protected_ranges(timeline: TextTimeline, duration_ms: int) -> tuple[TimeRange, ...]:
    ranges: list[TimeRange] = []
    for segment in timeline.source_segments:
        start = segment.output_start_ms
        end = segment.output_end_ms
        if start is None or end is None:
            raise AudioPlanError("source_map_uncompiled")
        if start < 0 or end <= start or end > duration_ms:
            raise AudioPlanError("source_map_invalid")
        if segment.protected or _PROTECTED_FACT.search(segment.text):
            ranges.append(TimeRange(start, end))
    ranges.sort(key=lambda item: (item.start_ms, item.end_ms))
    merged: list[TimeRange] = []
    for item in ranges:
        if merged and item.start_ms <= merged[-1].end_ms:
            merged[-1] = TimeRange(merged[-1].start_ms, max(merged[-1].end_ms, item.end_ms))
        else:
            merged.append(item)
    return tuple(merged)


def compile_audio_plan(
    edit_plan: Mapping[str, Any],
    timeline: TextTimeline,
) -> AudioGenerationPlan:
    if not isinstance(edit_plan, Mapping) or not isinstance(timeline, TextTimeline):
        raise AudioPlanError("audio_plan_input_invalid")
    duration_ms = _integer(edit_plan.get("duration_ms"), "audio_duration_invalid", minimum=3_000, maximum=600_000)
    cues = edit_plan.get("audio_cues", [])
    if not isinstance(cues, list) or len(cues) > 64:
        raise AudioPlanError("audio_cues_invalid")
    protected = _protected_ranges(timeline, duration_ms)
    ids: set[str] = set()
    sfx: list[SfxGenerationRequest] = []
    sfx_ids: set[str] = set()
    sfx_ranges: dict[str, TimeRange] = {}
    bgm_descriptions: list[str] = []
    raw_fades: list[Mapping[str, Any]] = []
    for raw in cues:
        if not isinstance(raw, Mapping):
            raise AudioPlanError("audio_cue_invalid")
        cue_id = _identifier(raw.get("id"), "audio_cue_id_invalid")
        if cue_id in ids:
            raise AudioPlanError("audio_cue_duplicate")
        ids.add(cue_id)
        cue_type = raw.get("type")
        priority = raw.get("priority")
        if priority not in {"required", "optional"}:
            raise AudioPlanError("audio_cue_priority_invalid")
        start = _integer(raw.get("start_ms"), "audio_cue_range_invalid", minimum=0, maximum=duration_ms - 1)
        end = _integer(raw.get("end_ms"), "audio_cue_range_invalid", minimum=1, maximum=duration_ms)
        if end <= start:
            raise AudioPlanError("audio_cue_range_invalid")
        description = raw.get("description")
        if not isinstance(description, str) or not description.strip() or len(description) > 240:
            raise AudioPlanError("audio_cue_description_invalid")
        if cue_type == "bgm":
            if bgm_descriptions:
                raise AudioPlanError("bgm_duplicate")
            if start != 0 or end != duration_ms:
                raise AudioPlanError("bgm_range_invalid")
            bgm_descriptions.append(description.strip())
        elif cue_type == "sfx":
            role = raw.get("role")
            if role not in _SFX_ROLES:
                raise AudioPlanError("sfx_role_invalid")
            cue_range = TimeRange(start, end)
            if any(_ranges_overlap(cue_range, item) for item in protected):
                raise AudioPlanError("sfx_protected_overlap")
            sfx_ids.add(cue_id)
            sfx_ranges[cue_id] = cue_range
            sfx.append(
                SfxGenerationRequest(
                    description.strip(),
                    end - start,
                    cue_id,
                    priority == "required",
                    start,
                    end,
                )
            )
        elif cue_type == "volume_fade":
            raw_fades.append(raw)
        else:
            raise AudioPlanError("audio_cue_type_invalid")

    fades: list[VolumeFade] = []
    by_target: dict[str, list[TimeRange]] = {}
    for raw in raw_fades:
        target = raw.get("target")
        if target != "bgm" and target not in sfx_ids:
            raise AudioPlanError("volume_fade_target_invalid")
        start = int(raw["start_ms"])
        end = int(raw["end_ms"])
        interval = TimeRange(start, end)
        if target != "bgm":
            target_range = sfx_ranges[str(target)]
            if start < target_range.start_ms or end > target_range.end_ms:
                raise AudioPlanError("volume_fade_target_range_invalid")
        if any(_ranges_overlap(interval, other) for other in by_target.setdefault(str(target), [])):
            raise AudioPlanError("volume_fade_overlap")
        by_target[str(target)].append(interval)
        fades.append(VolumeFade(str(raw["id"]), str(target), start, end, _db(raw.get("from_db")), _db(raw.get("to_db"))))

    concept = edit_plan.get("creative_concept")
    if not isinstance(concept, str) or not concept.strip():
        concept = "清晰、克制、适合中文口播"
    music_description = bgm_descriptions[0] if bgm_descriptions else concept.strip()
    prompt = f"Instrumental only, no vocals, no lyrics. {music_description}. Keep dialogue intelligible."
    return AudioGenerationPlan(
        music=MusicGenerationRequest(prompt[:1000], duration_ms),
        sfx=tuple(sfx),
        volume_fades=tuple(sorted(fades, key=lambda item: (item.start_ms, item.target, item.cue_id))),
        protected_ranges=protected,
        duration_ms=duration_ms,
    )


def _safe_job_id(job_id: str) -> str:
    if not isinstance(job_id, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", job_id) is None:
        raise AudioGenerationError("audio_job_id_invalid")
    return job_id


def _audio_probe(path: Path, expected_duration_ms: int) -> tuple[int, int, int, str]:
    try:
        probe = probe_media(path, timeout_seconds=30)
    except (MediaProcessError, ValueError) as exc:
        raise AudioGenerationError("generated_audio_invalid") from exc
    if probe.media_type != "audio" or abs(probe.duration_ms - expected_duration_ms) > 100:
        raise AudioGenerationError("generated_audio_invalid")
    stream = next((item for item in probe.streams if item.get("codec_type") == "audio"), None)
    if not isinstance(stream, Mapping):
        raise AudioGenerationError("generated_audio_invalid")
    try:
        sample_rate = int(stream.get("sample_rate"))
        channels = int(stream.get("channels"))
    except (TypeError, ValueError) as exc:
        raise AudioGenerationError("generated_audio_invalid") from exc
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return probe.duration_ms, sample_rate, channels, digest


def _normalize_generated_audio(
    source: Path,
    target: Path,
    *,
    expected_duration_ms: int,
    deadline_at: float,
) -> None:
    """Convert provider-specific audio bytes into a deterministic WAV asset."""
    seconds = expected_duration_ms / 1000
    _run(
        (
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-protocol_whitelist",
            "file,pipe",
            "-i",
            os.fspath(source),
            "-af",
            f"aresample=48000,aformat=sample_fmts=s16:channel_layouts=stereo,apad,atrim=duration={seconds:.3f}",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-c:a",
            "pcm_s16le",
            "-y",
            os.fspath(target),
        ),
        deadline_at,
    )


def _generate_one(
    *,
    kind: Literal["bgm", "sfx"],
    cue_id: str,
    request: MusicGenerationRequest | SfxGenerationRequest,
    generator: AudioGenerator,
    path: Path,
    key: str,
    deadline_at: float,
) -> ProviderResult:
    operation = f"ai-edit-v3:{key}:audio:{'bgm' if kind == 'bgm' else 'sfx:' + cue_id}"
    last: Exception | None = None
    for _attempt in range(2):
        if path.exists():
            path.unlink()
        try:
            if kind == "bgm":
                return generator.generate_music(request, output_path=path, idempotency_key=operation, deadline_at=deadline_at)  # type: ignore[arg-type]
            return generator.generate_sfx(request, output_path=path, idempotency_key=operation, deadline_at=deadline_at)  # type: ignore[arg-type]
        except SubmissionUnknown:
            raise
        except DefinitiveNotAccepted as exc:
            last = exc
            continue
        except (OSError, TimeoutError, ValueError) as exc:
            last = exc
            continue
    if isinstance(last, DefinitiveNotAccepted):
        raise last
    raise AudioGenerationError(f"{kind}_generation_failed") from last


def _validate_audio_receipt(
    value: object,
    *,
    cue_id: str,
    kind: Literal["bgm", "sfx"],
    object_key: str,
    model: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise AudioGenerationError("audio_receipt_invalid")
    exact_keys = {
        "request_id", "cue_id", "kind", "object_key", "sha256",
        "duration_ms", "sample_rate", "channels", "provider_request_id",
        "usage", "model",
    }
    if set(value) != exact_keys:
        raise AudioGenerationError("audio_receipt_invalid")
    request_id = value.get("request_id")
    provider_request_id = value.get("provider_request_id")
    usage = value.get("usage")
    duration_ms = value.get("duration_ms")
    sample_rate = value.get("sample_rate")
    channels = value.get("channels")
    digest = value.get("sha256")
    if (
        value.get("cue_id") != cue_id
        or value.get("kind") != kind
        or value.get("object_key") != object_key
        or value.get("model") != model
        or not isinstance(request_id, str)
        or _PROVIDER_IDENTIFIER.fullmatch(request_id) is None
        or provider_request_id != request_id
        or not isinstance(digest, str)
        or _SHA256.fullmatch(digest) is None
        or isinstance(duration_ms, bool)
        or not isinstance(duration_ms, int)
        or duration_ms <= 0
        or sample_rate != 48_000
        or isinstance(sample_rate, bool)
        or channels != 2
        or isinstance(channels, bool)
        or not isinstance(usage, Mapping)
        or not set(usage).issubset(_AUDIO_USAGE_KEYS)
    ):
        raise AudioGenerationError("audio_receipt_invalid")
    normalized_usage: dict[str, int | float] = {}
    for name, amount in usage.items():
        if (
            isinstance(amount, bool)
            or not isinstance(amount, (int, float))
            or not math.isfinite(amount)
            or amount < 0
        ):
            raise AudioGenerationError("audio_receipt_invalid")
        normalized_usage[str(name)] = amount
    return {
        "request_id": request_id,
        "cue_id": cue_id,
        "kind": kind,
        "object_key": object_key,
        "sha256": digest,
        "duration_ms": duration_ms,
        "sample_rate": sample_rate,
        "channels": channels,
        "provider_request_id": provider_request_id,
        "usage": normalized_usage,
        "model": model,
    }


def generate_task_audio(
    job_id: str,
    plan: AudioGenerationPlan,
    generator: AudioGenerator,
    cos: PrivateCos,
    output_root: Path,
    context: Any,
    *,
    provider_once: Any | None = None,
) -> tuple[GeneratedAudioAsset, ...]:
    job_id = _safe_job_id(job_id)
    if not isinstance(plan, AudioGenerationPlan):
        raise AudioGenerationError("audio_plan_invalid")
    if not callable(getattr(context, "assert_active", None)):
        raise AudioGenerationError("audio_context_invalid")
    deadline_at = getattr(context, "deadline_at", None)
    if isinstance(deadline_at, bool) or not isinstance(deadline_at, (int, float)) or deadline_at <= time.time():
        raise AudioGenerationError("audio_deadline_exceeded")
    root = Path(output_root).resolve()
    task_root = root / job_id / "audio"
    task_root.mkdir(parents=True, exist_ok=True)
    assets: list[GeneratedAudioAsset] = []

    requests: list[tuple[Literal["bgm", "sfx"], str, MusicGenerationRequest | SfxGenerationRequest, bool]] = [
        ("bgm", "bgm", plan.music, True),
        *(("sfx", request.cue_id, request, request.required) for request in plan.sfx),
    ]
    for kind, cue_id, request, required in requests:
        context.assert_active()
        provider_path = task_root / f".{cue_id}.provider-audio"
        path = task_root / f"{cue_id}.wav"
        operation_key = (
            f"ai-edit-v3:{job_id}:audio:bgm"
            if kind == "bgm"
            else f"ai-edit-v3:{job_id}:audio:sfx:{cue_id}"
        )
        request_document = {
            "version": "audio-generation-v1",
            "kind": kind,
            "cue_id": cue_id,
            "prompt": request.prompt,
            "duration_ms": request.duration_ms,
            "model": "music_v2" if kind == "bgm" else "eleven_text_to_sound_v2",
            **(
                {
                    "required": request.required,
                    "start_ms": request.start_ms,
                    "end_ms": request.end_ms,
                }
                if isinstance(request, SfxGenerationRequest)
                else {}
            ),
        }
        request_sha256 = hashlib.sha256(canonical_json(request_document)).hexdigest()
        generated_request_id = "generated-" + hashlib.sha256(
            f"{operation_key}:{request_sha256}".encode("utf-8")
        ).hexdigest()
        expected = plan.duration_ms if kind == "bgm" else request.duration_ms
        environment = getattr(cos, "environment", "test")
        if environment not in {"test", "production"}:
            raise AudioGenerationError("audio_environment_invalid")
        object_key = f"{environment}/ai-edit-v3/{job_id}/audio/{cue_id}.wav"

        def produce() -> Mapping[str, Any]:
            result = _generate_one(
                kind=kind,
                cue_id=cue_id,
                request=request,
                generator=generator,
                path=provider_path,
                key=job_id,
                deadline_at=float(deadline_at),
            )
            provider_sha = hashlib.sha256(provider_path.read_bytes()).hexdigest()
            payload_sha = result.payload.get("sha256")
            if payload_sha is not None and payload_sha != provider_sha:
                raise AudioGenerationError("generated_audio_hash_mismatch")
            _normalize_generated_audio(
                provider_path,
                path,
                expected_duration_ms=expected,
                deadline_at=float(deadline_at),
            )
            duration_ms, sample_rate, channels, digest = _audio_probe(path, expected)
            receipt = _validate_audio_receipt({
                "request_id": result.request_id or generated_request_id,
                "cue_id": cue_id,
                "kind": kind,
                "object_key": object_key,
                "sha256": digest,
                "duration_ms": duration_ms,
                "sample_rate": sample_rate,
                "channels": channels,
                "provider_request_id": result.request_id or generated_request_id,
                "usage": dict(result.usage),
                "model": request_document["model"],
            }, cue_id=cue_id, kind=kind, object_key=object_key, model=request_document["model"])
            cos.put_file(path, object_key, "audio/wav", private=True, if_absent=True)
            return receipt

        try:
            receipt = (
                provider_once(
                    provider="elevenlabs",
                    capability=kind,
                    operation_key=operation_key,
                    request_sha256=request_sha256,
                    call=produce,
                )
                if provider_once is not None
                else produce()
            )
            receipt = _validate_audio_receipt(
                receipt,
                cue_id=cue_id,
                kind=kind,
                object_key=object_key,
                model=request_document["model"],
            )
            def verified_local_asset() -> tuple[int, int, int, str] | None:
                if not path.is_file():
                    return None
                try:
                    probed = _audio_probe(path, expected)
                except AudioGenerationError:
                    return None
                if (
                    probed[3] != receipt["sha256"]
                    or probed[0] != receipt["duration_ms"]
                    or probed[1] != receipt["sample_rate"]
                    or probed[2] != receipt["channels"]
                ):
                    return None
                return probed

            verified = verified_local_asset()
            if verified is None:
                download = getattr(cos, "download_file", None)
                if not callable(download):
                    raise AudioGenerationError("audio_receipt_restore_unavailable")
                path.unlink(missing_ok=True)
                download(object_key, path)
                verified = verified_local_asset()
            if verified is None:
                path.unlink(missing_ok=True)
                raise AudioGenerationError("audio_receipt_asset_mismatch")
            duration_ms, sample_rate, channels, digest = verified
            assets.append(
                GeneratedAudioAsset(
                    cue_id=cue_id,
                    kind=kind,
                    relative_path=path.relative_to(root).as_posix(),
                    object_key=object_key,
                    sha256=digest,
                    duration_ms=duration_ms,
                    sample_rate=sample_rate,
                    channels=channels,
                    provider_request_id=str(receipt["provider_request_id"]),
                    usage=dict(receipt["usage"]),
                )
            )
        except DefinitiveNotAccepted as exc:
            if required:
                raise AudioGenerationError(f"{kind}_generation_failed") from exc
            if path.exists():
                path.unlink()
        except AudioGenerationError:
            raise
        finally:
            provider_path.unlink(missing_ok=True)
    context.assert_active()
    return tuple(assets)


def _validate_segments(source_segments: Sequence[SourceSegment], voice_duration_ms: int, target_duration_ms: int) -> None:
    if not source_segments:
        raise AudioGenerationError("source_segments_empty")
    prior_source = -1
    prior_output = 0
    total = 0
    for segment in source_segments:
        if (
            not isinstance(segment, SourceSegment)
            or segment.start_ms < prior_source
            or segment.end_ms <= segment.start_ms
            or segment.end_ms > voice_duration_ms + 40
            or segment.output_start_ms != prior_output
            or segment.output_end_ms is None
            or segment.output_end_ms <= segment.output_start_ms
            or segment.output_end_ms - segment.output_start_ms != segment.end_ms - segment.start_ms
        ):
            raise AudioGenerationError("source_segments_non_monotonic")
        prior_source = segment.end_ms
        prior_output = segment.output_end_ms
        total += segment.end_ms - segment.start_ms
    if abs(total - target_duration_ms) > 40:
        raise AudioGenerationError("source_duration_mismatch")


def _asset_path(asset: GeneratedAudioAsset, output_path: Path) -> Path:
    value = Path(asset.relative_path)
    if value.is_absolute():
        return value
    candidates = (output_path.parent / value, output_path.parent.parent / value, output_path.parent.parent.parent / value)
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return candidates[0].resolve()


def _loudnorm_json(stderr: bytes) -> Mapping[str, Any]:
    text = stderr.decode("utf-8", "replace")
    candidates = re.findall(r"\{[^{}]*\}", text, re.DOTALL)
    for raw in reversed(candidates):
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(value, Mapping) and "input_i" in value and "input_tp" in value:
            return value
    raise AudioGenerationError("loudnorm_json_invalid")


def _finite_measurement(payload: Mapping[str, Any], key: str) -> float:
    try:
        value = float(payload[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise AudioGenerationError("loudnorm_json_invalid") from exc
    if not math.isfinite(value):
        raise AudioGenerationError("loudnorm_json_invalid")
    return value


def _run(argv: Sequence[str], deadline_at: float, maximum: float = 120) -> tuple[bytes, bytes]:
    remaining = float(deadline_at) - time.time()
    if not math.isfinite(remaining) or remaining <= 0:
        raise AudioGenerationError("audio_process_timeout")
    try:
        result = run_media_process(argv, timeout_seconds=min(maximum, remaining), max_output_bytes=4 * 1024 * 1024)
    except TimeoutError as exc:
        raise AudioGenerationError("audio_process_timeout") from exc
    except OSError as exc:
        raise AudioGenerationError("audio_tool_missing") from exc
    if result.returncode != 0:
        raise AudioGenerationError("audio_process_failed")
    return result.stdout, result.stderr


def _fade_expression(
    fades: Sequence[VolumeFade],
    *,
    target: str,
    offset_ms: int,
    duration_ms: int,
) -> str | None:
    selected = [fade for fade in fades if fade.target == target]
    if not selected:
        return None
    ordered = sorted(selected, key=lambda item: (item.start_ms, item.end_ms))
    expression = f"pow(10,({ordered[-1].to_db:.3f})/20)"
    for fade in reversed(ordered):
        start_ms = fade.start_ms - offset_ms
        end_ms = fade.end_ms - offset_ms
        if start_ms < 0 or end_ms > duration_ms or end_ms <= start_ms:
            raise AudioGenerationError("volume_fade_target_range_invalid")
        start = start_ms / 1000
        end = end_ms / 1000
        span = end - start
        delta = fade.to_db - fade.from_db
        gain = (
            f"pow(10,({fade.from_db:.3f}+({delta:.3f})*"
            f"(t-{start:.3f})/{span:.3f})/20)"
        )
        before_gain = f"pow(10,({fade.from_db:.3f})/20)"
        expression = (
            f"if(lt(t,{start:.3f}),{before_gain},"
            f"if(between(t,{start:.3f},{end:.3f}),{gain},{expression}))"
        )
    return expression


def _build_mix_filter_graph(
    source_segments: Sequence[SourceSegment],
    plan: AudioGenerationPlan,
    ordered_sfx: Sequence[GeneratedAudioAsset],
) -> tuple[list[str], list[str]]:
    seconds = plan.duration_ms / 1000
    filters: list[str] = []
    voice_labels: list[str] = []
    for index, segment in enumerate(source_segments):
        label = f"voice_{index}"
        filters.append(
            f"[0:a]atrim=start={segment.start_ms / 1000:.3f}:"
            f"end={segment.end_ms / 1000:.3f},asetpts=PTS-STARTPTS[{label}]"
        )
        voice_labels.append(f"[{label}]")
    filters.append(
        f"{''.join(voice_labels)}concat=n={len(voice_labels)}:v=0:a=1,"
        "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
        f"apad,atrim=duration={seconds:.3f}[voice_base]"
    )
    voice_split_labels = ["[voice_mix]", "[voice_bgm]"] + [
        f"[voice_sfx_{offset}]" for offset in range(2, 2 + len(ordered_sfx))
    ]
    filters.append(
        f"[voice_base]asplit={len(voice_split_labels)}{''.join(voice_split_labels)}"
    )
    filters.append(
        "[1:a]aloop=loop=-1:size=2147483647,"
        f"atrim=duration={seconds:.3f},aresample=48000,"
        "aformat=sample_fmts=fltp:channel_layouts=stereo,volume=0.12[bgm_base]"
    )
    bgm_expression = _fade_expression(
        plan.volume_fades,
        target="bgm",
        offset_ms=0,
        duration_ms=plan.duration_ms,
    )
    filters.append(
        f"[bgm_base]volume=eval=frame:volume='{bgm_expression}'[bgm_automated]"
        if bgm_expression is not None
        else "[bgm_base]anull[bgm_automated]"
    )
    filters.append(
        "[bgm_automated][voice_bgm]"
        "sidechaincompress=threshold=0.015:ratio=12:attack=10:release=250[bgm_ducked]"
    )
    mix_labels = ["[voice_mix]", "[bgm_ducked]"]
    expected_sfx = {item.cue_id: item for item in plan.sfx}
    for offset, asset in enumerate(ordered_sfx, start=2):
        request = expected_sfx[asset.cue_id]
        base_label = f"sfx_{offset}_base"
        automated_label = f"sfx_{offset}_automated"
        delayed_label = f"sfx_{offset}_delayed"
        label = f"sfx_{offset}"
        filters.append(
            f"[{offset}:a]atrim=duration={request.duration_ms / 1000:.3f},"
            "asetpts=PTS-STARTPTS,aresample=48000,"
            "aformat=sample_fmts=fltp:channel_layouts=stereo,"
            f"volume=0.32[{base_label}]"
        )
        expression = _fade_expression(
            plan.volume_fades,
            target=asset.cue_id,
            offset_ms=request.start_ms,
            duration_ms=request.duration_ms,
        )
        filters.append(
            f"[{base_label}]volume=eval=frame:volume='{expression}'[{automated_label}]"
            if expression is not None
            else f"[{base_label}]anull[{automated_label}]"
        )
        filters.append(
            f"[{automated_label}]adelay={request.start_ms}|{request.start_ms}[{delayed_label}]"
        )
        filters.append(
            f"[{delayed_label}][voice_sfx_{offset}]"
            "sidechaincompress=threshold=0.020:ratio=8:attack=5:release=120"
            f"[{label}]"
        )
        mix_labels.append(f"[{label}]")
    return filters, mix_labels


def build_master_audio(
    voice_source: Path,
    source_segments: Sequence[SourceSegment],
    plan: AudioGenerationPlan,
    generated: Sequence[GeneratedAudioAsset],
    output_path: Path,
    *,
    deadline_at: float,
) -> MasterAudio:
    voice = Path(voice_source).resolve()
    output = Path(output_path).resolve()
    if not voice.is_file():
        raise AudioGenerationError("voice_source_missing")
    if output.exists():
        raise AudioGenerationError("master_output_exists")
    try:
        voice_probe = probe_media(voice, timeout_seconds=30)
    except (MediaProcessError, ValueError) as exc:
        raise AudioGenerationError("voice_source_invalid") from exc
    if voice_probe.media_type not in {"audio", "video"}:
        raise AudioGenerationError("voice_source_invalid")
    _validate_segments(source_segments, voice_probe.duration_ms, plan.duration_ms)
    bgm_assets = [item for item in generated if item.kind == "bgm"]
    if len(bgm_assets) != 1:
        raise AudioGenerationError("bgm_asset_missing" if not bgm_assets else "bgm_asset_duplicate")
    if any(item.kind not in {"bgm", "sfx"} for item in generated):
        raise AudioGenerationError("dialogue_input_duplicate")
    sfx_assets = {item.cue_id: item for item in generated if item.kind == "sfx"}
    if len(sfx_assets) != len([item for item in generated if item.kind == "sfx"]):
        raise AudioGenerationError("sfx_asset_duplicate")
    expected_sfx = {item.cue_id: item for item in plan.sfx}
    if not set(sfx_assets).issubset(expected_sfx):
        raise AudioGenerationError("sfx_asset_undeclared")

    bgm_path = _asset_path(bgm_assets[0], output)
    if not bgm_path.is_file() or hashlib.sha256(bgm_path.read_bytes()).hexdigest() != bgm_assets[0].sha256:
        raise AudioGenerationError("bgm_asset_invalid")
    ordered_sfx = sorted(sfx_assets.values(), key=lambda item: next(c for c in plan.sfx if c.cue_id == item.cue_id).cue_id)
    input_paths = [voice, bgm_path, *(_asset_path(item, output) for item in ordered_sfx)]
    if any(not item.is_file() for item in input_paths):
        raise AudioGenerationError("sfx_asset_invalid")

    output.parent.mkdir(parents=True, exist_ok=True)
    premaster = output.with_name(f".{output.name}.premaster.wav")
    normalized = output.with_name(f".{output.name}.normalized.wav")
    for item in (premaster, normalized):
        if item.exists():
            item.unlink()
    seconds = plan.duration_ms / 1000
    filters, mix_labels = _build_mix_filter_graph(
        source_segments,
        plan,
        ordered_sfx,
    )
    filters.append(f"{''.join(mix_labels)}amix=inputs={len(mix_labels)}:duration=longest:normalize=0,atrim=duration={seconds:.3f}[master]")
    command = ["ffmpeg", "-hide_banner", "-nostdin", "-protocol_whitelist", "file,pipe", "-i", os.fspath(voice), "-i", os.fspath(bgm_path)]
    for path in input_paths[2:]:
        command.extend(["-i", os.fspath(path)])
    command.extend(["-filter_complex", ";".join(filters), "-map", "[master]", "-ar", "48000", "-ac", "2", "-c:a", "pcm_s24le", "-y", os.fspath(premaster)])
    _run(command, deadline_at)

    _, analysis_stderr = _run(["ffmpeg", "-hide_banner", "-nostdin", "-protocol_whitelist", "file,pipe", "-i", os.fspath(premaster), "-af", f"loudnorm=I=-16:TP={_MASTER_TRUE_PEAK_TARGET_DBTP}:LRA=11:print_format=json", "-f", "null", os.devnull], deadline_at)
    measured = _loudnorm_json(analysis_stderr)
    measured_i = _finite_measurement(measured, "input_i")
    measured_tp = _finite_measurement(measured, "input_tp")
    measured_lra = _finite_measurement(measured, "input_lra")
    measured_thresh = _finite_measurement(measured, "input_thresh")
    offset = _finite_measurement(measured, "target_offset")
    loud_filter = (
        f"loudnorm=I=-16:TP={_MASTER_TRUE_PEAK_TARGET_DBTP}:LRA=11:linear=true:print_format=json:"
        f"measured_I={measured_i}:measured_TP={measured_tp}:measured_LRA={measured_lra}:"
        f"measured_thresh={measured_thresh}:offset={offset}"
    )
    _run(["ffmpeg", "-hide_banner", "-nostdin", "-protocol_whitelist", "file,pipe", "-i", os.fspath(premaster), "-af", loud_filter, "-ar", "48000", "-ac", "2", "-c:a", "pcm_s24le", "-y", os.fspath(normalized)], deadline_at)
    _, verify_stderr = _run(["ffmpeg", "-hide_banner", "-nostdin", "-protocol_whitelist", "file,pipe", "-i", os.fspath(normalized), "-af", f"loudnorm=I=-16:TP={_MASTER_TRUE_PEAK_TARGET_DBTP}:LRA=11:print_format=json", "-f", "null", os.devnull], deadline_at)
    verified = _loudnorm_json(verify_stderr)
    integrated = _finite_measurement(verified, "input_i")
    true_peak = _finite_measurement(verified, "input_tp")
    if not -18 <= integrated <= -14:
        raise AudioGenerationError("master_loudness_invalid")
    if true_peak > -1:
        raise AudioGenerationError("master_true_peak_invalid")
    final_probe = probe_media(normalized, timeout_seconds=30)
    stream = next((item for item in final_probe.streams if item.get("codec_type") == "audio"), {})
    if final_probe.media_type != "audio" or abs(final_probe.duration_ms - plan.duration_ms) > 40:
        raise AudioGenerationError("master_duration_invalid")
    if int(stream.get("sample_rate", 0)) != 48_000 or int(stream.get("channels", 0)) != 2:
        raise AudioGenerationError("master_format_invalid")
    os.replace(normalized, output)
    if premaster.exists():
        premaster.unlink()
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    return MasterAudio(
        relative_path=output.name,
        sha256=digest,
        duration_ms=final_probe.duration_ms,
        sample_rate=48_000,
        channels=2,
        integrated_lufs=integrated,
        true_peak_dbtp=true_peak,
        audit={
            "loudness_passes": 2,
            "true_peak_target_dbtp": _MASTER_TRUE_PEAK_TARGET_DBTP,
            "ducking_db_minimum": 12,
            "source_segment_count": len(source_segments),
            "sfx_count": len(ordered_sfx),
            "volume_fades": [fade.__dict__ if hasattr(fade, "__dict__") else {"cue_id": fade.cue_id, "target": fade.target, "start_ms": fade.start_ms, "end_ms": fade.end_ms, "from_db": fade.from_db, "to_db": fade.to_db} for fade in plan.volume_fades],
            "protocol_whitelist": "file,pipe",
        },
    )


__all__ = (
    "AudioGenerationError",
    "AudioGenerationPlan",
    "AudioPlanError",
    "GeneratedAudioAsset",
    "MasterAudio",
    "TimeRange",
    "VolumeFade",
    "build_master_audio",
    "compile_audio_plan",
    "generate_task_audio",
)
