"""Per-job ElevenLabs BGM and sound-effect generation for AI Edit V3."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import socket
import time
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Protocol, runtime_checkable
import urllib.error
import urllib.request

from .base import DefinitiveNotAccepted, ProviderResult, SecretValue, SubmissionUnknown


MUSIC_MODEL = "music_v2"
SFX_MODEL = "eleven_text_to_sound_v2"
_BASE_URL = "https://api.elevenlabs.io"
_AUDIO_TYPES = frozenset({"audio/mpeg", "audio/mp3", "audio/wav", "audio/x-wav", "audio/mp4"})
_MUSIC_MAX_BYTES = 64 * 1024 * 1024
_SFX_MAX_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class MusicGenerationRequest:
    prompt: str
    duration_ms: int


@dataclass(frozen=True, slots=True)
class SfxGenerationRequest:
    prompt: str
    duration_ms: int
    cue_id: str
    required: bool
    start_ms: int = 0
    end_ms: int = 0


@runtime_checkable
class AudioGenerator(Protocol):
    def generate_music(
        self,
        request: MusicGenerationRequest,
        *,
        output_path: Path,
        idempotency_key: str,
        deadline_at: float,
    ) -> ProviderResult: ...

    def generate_sfx(
        self,
        request: SfxGenerationRequest,
        *,
        output_path: Path,
        idempotency_key: str,
        deadline_at: float,
    ) -> ProviderResult: ...


class _UrllibTransport:
    def open(
        self,
        *,
        method: str,
        url: str,
        headers: Mapping[str, str],
        json_body: Mapping[str, Any],
        timeout: float,
    ) -> Any:
        encoded = json.dumps(
            json_body,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        request = urllib.request.Request(
            url=url,
            data=encoded,
            headers=dict(headers),
            method=method,
        )
        try:
            return urllib.request.urlopen(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            return exc


class ElevenLabsAudioGenerator:
    """Fixed-endpoint adapter with no internal retry or persistence authority."""

    def __init__(self, *, api_key: SecretValue | None, transport: Any | None = None) -> None:
        if api_key is not None and not isinstance(api_key, SecretValue):
            raise ValueError("elevenlabs_api_key_invalid")
        self._api_key = api_key
        self._transport = transport or _UrllibTransport()
        if not callable(getattr(self._transport, "open", None)):
            raise ValueError("elevenlabs_transport_invalid")

    def probe_capability(
        self,
        capability: str,
        *,
        environment: str | None,
    ) -> Mapping[str, object]:
        if capability != "audio_generator":
            return {"available": False, "reason_code": "capability_unknown"}
        if self._api_key is None:
            return {
                "available": False,
                "reason_code": "elevenlabs_api_key_missing",
            }
        return {
            "available": True,
            "environment": environment,
            "provider": "elevenlabs",
        }

    def generate_music(
        self,
        request: MusicGenerationRequest,
        *,
        output_path: Path,
        idempotency_key: str,
        deadline_at: float,
    ) -> ProviderResult:
        if not isinstance(request, MusicGenerationRequest):
            raise ValueError("elevenlabs_music_request_invalid")
        prompt = _prompt(request.prompt, maximum=1000)
        duration_ms = _duration(request.duration_ms, 3_000, 600_000)
        return self._generate(
            capability="music",
            endpoint="/v1/music",
            model=MUSIC_MODEL,
            body={
                "prompt": prompt,
                "music_length_ms": duration_ms,
                "model_id": MUSIC_MODEL,
                "force_instrumental": True,
            },
            output_path=output_path,
            idempotency_key=idempotency_key,
            deadline_at=deadline_at,
            maximum_bytes=_MUSIC_MAX_BYTES,
        )

    def generate_sfx(
        self,
        request: SfxGenerationRequest,
        *,
        output_path: Path,
        idempotency_key: str,
        deadline_at: float,
    ) -> ProviderResult:
        if not isinstance(request, SfxGenerationRequest):
            raise ValueError("elevenlabs_sfx_request_invalid")
        prompt = _prompt(request.prompt, maximum=500)
        duration_ms = _duration(request.duration_ms, 500, 30_000)
        _identifier(request.cue_id, "cue_id", maximum=64)
        if not isinstance(request.required, bool):
            raise ValueError("elevenlabs_sfx_required_invalid")
        return self._generate(
            capability="sfx",
            endpoint="/v1/sound-generation",
            model=SFX_MODEL,
            body={
                "text": prompt,
                "duration_seconds": duration_ms / 1000,
                "model_id": SFX_MODEL,
            },
            output_path=output_path,
            idempotency_key=idempotency_key,
            deadline_at=deadline_at,
            maximum_bytes=_SFX_MAX_BYTES,
        )

    def _generate(
        self,
        *,
        capability: Literal["music", "sfx"],
        endpoint: str,
        model: str,
        body: Mapping[str, Any],
        output_path: Path,
        idempotency_key: str,
        deadline_at: float,
        maximum_bytes: int,
    ) -> ProviderResult:
        if self._api_key is None:
            raise DefinitiveNotAccepted("elevenlabs_not_configured")
        key = _identifier(idempotency_key, "idempotency_key", maximum=200)
        target = _output_path(output_path)
        remaining = deadline_at - time.time()
        if not isinstance(deadline_at, (int, float)) or isinstance(deadline_at, bool) or remaining <= 0:
            raise TimeoutError("elevenlabs_deadline_exceeded")
        started = time.monotonic()
        try:
            response = self._transport.open(
                method="POST",
                url=_BASE_URL + endpoint,
                headers={
                    "Accept": "audio/mpeg",
                    "Content-Type": "application/json",
                    "xi-api-key": self._api_key.value,
                    "Idempotency-Key": key,
                },
                json_body=dict(body),
                timeout=min(120.0, max(0.1, remaining)),
            )
        except Exception as exc:
            if _definitively_not_sent(exc):
                raise DefinitiveNotAccepted("elevenlabs_not_accepted") from None
            raise SubmissionUnknown("elevenlabs_submission_unknown") from None

        try:
            status = int(getattr(response, "status", getattr(response, "code", 0)))
            headers = _headers(getattr(response, "headers", {}))
            if not 200 <= status < 300:
                accepted = headers.get("x-request-accepted", "").lower()
                if status == 429 or status < 500 or accepted == "false":
                    raise DefinitiveNotAccepted("elevenlabs_not_accepted")
                raise SubmissionUnknown("elevenlabs_submission_unknown")
            content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if content_type not in _AUDIO_TYPES:
                raise ValueError("elevenlabs_content_type_invalid")
            size, digest = _stream_audio(
                response,
                target=target,
                maximum_bytes=maximum_bytes,
            )
            usage = _usage(headers)
            return ProviderResult(
                provider="elevenlabs",
                capability=capability,
                request_id=headers.get("request-id") or headers.get("x-request-id") or None,
                payload={
                    "content_type": content_type,
                    "size_bytes": size,
                    "sha256": digest,
                    "model": model,
                },
                usage=usage,
                elapsed_ms=max(0, round((time.monotonic() - started) * 1000)),
            )
        finally:
            try:
                response.close()
            except Exception:
                pass


def _headers(raw: object) -> dict[str, str]:
    try:
        values = dict(raw)  # type: ignore[arg-type]
    except Exception:
        return {}
    return {str(key).lower(): str(value) for key, value in values.items()}


def _usage(headers: Mapping[str, str]) -> dict[str, int | float]:
    usage: dict[str, int | float] = {}
    for header, name in (
        ("x-usage-credits", "credits"),
        ("character-cost", "characters"),
    ):
        raw = headers.get(header)
        if raw is None:
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        if value >= 0:
            usage[name] = int(value) if value.is_integer() else value
    return usage


def _stream_audio(response: Any, *, target: Path, maximum_bytes: int) -> tuple[int, str]:
    temporary = target.parent / f".{target.name}.{os.getpid()}.{time.monotonic_ns()}.tmp"
    descriptor: int | None = None
    digest = hashlib.sha256()
    total = 0
    prefix = bytearray()
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = None
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                if not isinstance(chunk, (bytes, bytearray)):
                    raise ValueError("elevenlabs_audio_stream_invalid")
                total += len(chunk)
                if total > maximum_bytes:
                    raise ValueError("elevenlabs_audio_too_large")
                if len(prefix) < 64:
                    prefix.extend(chunk[: 64 - len(prefix)])
                digest.update(chunk)
                stream.write(chunk)
            if total == 0:
                raise ValueError("elevenlabs_audio_empty")
            lowered = bytes(prefix).lstrip().lower()
            if lowered.startswith((b"{", b"[", b"<html", b"<!doctype")):
                raise ValueError("elevenlabs_audio_body_invalid")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError as exc:
            raise ValueError("elevenlabs_output_exists") from exc
        temporary.unlink()
        return total, digest.hexdigest()
    finally:
        if descriptor is not None:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _output_path(value: Path) -> Path:
    if not isinstance(value, Path) or not value.is_absolute():
        raise ValueError("elevenlabs_output_path_invalid")
    parent = value.parent.resolve(strict=True)
    target = parent / value.name
    if target.exists() or not value.name or value.name in {".", ".."}:
        raise ValueError("elevenlabs_output_exists")
    return target


def _prompt(value: object, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise ValueError("elevenlabs_prompt_invalid")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum or _has_control(normalized):
        raise ValueError("elevenlabs_prompt_invalid")
    return normalized


def _duration(value: object, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError("elevenlabs_duration_invalid")
    return value


def _identifier(value: object, field: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or _has_control(value):
        raise ValueError(f"elevenlabs_{field}_invalid")
    return value


def _has_control(value: str) -> bool:
    return any(ord(character) < 0x20 or 0x7F <= ord(character) <= 0x9F for character in value)


def _definitively_not_sent(exc: BaseException) -> bool:
    if getattr(exc, "body_sent", None) is False:
        return True
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(reason, (socket.gaierror, ConnectionRefusedError)):
        return True
    return False
