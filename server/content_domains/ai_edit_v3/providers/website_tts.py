"""Owner-scoped adapter for the website's existing CosyVoice audio generator."""

from __future__ import annotations

import hashlib
import os
import shutil
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .base import DefinitiveNotAccepted, ProviderResult, SubmissionUnknown


class WebsiteCosyVoiceTts:
    def __init__(
        self,
        *,
        generate_audio: Callable[[Mapping[str, Any]], Mapping[str, Any]],
        resolve_output: Callable[[str], Path | None],
        configured: bool,
    ) -> None:
        if not callable(generate_audio) or not callable(resolve_output):
            raise ValueError("website_tts_dependency_invalid")
        if not isinstance(configured, bool):
            raise ValueError("website_tts_configuration_invalid")
        self._generate_audio = generate_audio
        self._resolve_output = resolve_output
        self._configured = configured

    def probe_capability(self, capability: str, *, environment: str | None):
        return {
            "available": capability == "tts" and self._configured,
            "environment": environment,
            "provider": "website-cosyvoice",
            "reason_code": (
                "capability_ready"
                if capability == "tts" and self._configured
                else "website_tts_not_configured"
            ),
        }

    def generate(
        self,
        *,
        owner: str,
        text: str,
        voice_id: str,
        output_path: Path,
        idempotency_key: str,
        deadline_at: float,
    ) -> ProviderResult:
        if not self._configured:
            raise DefinitiveNotAccepted("website_tts_not_configured")
        if (
            not isinstance(owner, str)
            or not owner.strip()
            or not isinstance(text, str)
            or not text.strip()
            or len(text) > 1200
            or not isinstance(voice_id, str)
            or not voice_id.strip()
            or not isinstance(idempotency_key, str)
            or not idempotency_key.strip()
            or not isinstance(output_path, Path)
        ):
            raise DefinitiveNotAccepted("website_tts_request_invalid")
        if (
            isinstance(deadline_at, bool)
            or not isinstance(deadline_at, (int, float))
            or deadline_at <= time.time()
        ):
            raise DefinitiveNotAccepted("website_tts_deadline_exceeded")
        started = time.monotonic()
        try:
            result = self._generate_audio({
                "_username": owner,
                "text": text,
                "voice": voice_id,
                "speed": "normal",
                "pitch": 0,
                "volume": 0,
            })
        except Exception as exc:
            raise SubmissionUnknown("website_tts_submission_unknown") from exc
        try:
            if not isinstance(result, Mapping) or result.get("type") != "audio":
                raise ValueError("website_tts_result_invalid")
            relative = result.get("file")
            if not isinstance(relative, str) or not relative:
                raise ValueError("website_tts_result_invalid")
            generated = self._resolve_output(relative)
            if generated is None or not Path(generated).is_file():
                raise ValueError("website_tts_output_missing")
            target = output_path.resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(
                f".{target.name}.{os.getpid()}.{time.monotonic_ns()}.tmp"
            )
            try:
                shutil.copyfile(Path(generated), temporary)
                if temporary.stat().st_size <= 0:
                    raise ValueError("website_tts_output_empty")
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
            size = target.stat().st_size
            if size <= 0:
                raise ValueError("website_tts_output_empty")
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
        except Exception as exc:
            raise SubmissionUnknown("website_tts_output_unknown") from exc
        request_id = "website-tts-" + hashlib.sha256(
            idempotency_key.encode("utf-8")
        ).hexdigest()[:32]
        return ProviderResult(
            provider="website-cosyvoice",
            capability="tts",
            request_id=request_id,
            payload={
                "sha256": digest,
                "size_bytes": size,
                "mime_type": "audio/mpeg",
                "characters": len(text),
            },
            usage={"characters": len(text)},
            elapsed_ms=max(0, round((time.monotonic() - started) * 1000)),
        )


__all__ = ("WebsiteCosyVoiceTts",)
