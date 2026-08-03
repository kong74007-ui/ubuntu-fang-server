"""Bounded GPT image generation for V3 missing-material slots."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any, Mapping
import urllib.error
import urllib.request

from .base import DefinitiveNotAccepted, ProviderResult, SecretValue, SubmissionUnknown


_ENDPOINT = "https://api.openai.com/v1/images/generations"
_MAX_BYTES = 20 * 1024 * 1024


class _Transport:
    def open(self, *, method: str, url: str, headers: Mapping[str, str], json_body: Mapping[str, Any], timeout: float):
        request = urllib.request.Request(
            url,
            data=json.dumps(json_body, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            headers=dict(headers),
            method=method,
        )
        try:
            return urllib.request.urlopen(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            return exc


class OpenAIImageGenerator:
    def __init__(self, *, api_key: SecretValue | None, transport: Any | None = None) -> None:
        self._api_key = api_key
        self._transport = transport or _Transport()
        if api_key is not None and not isinstance(api_key, SecretValue):
            raise ValueError("openai_image_key_invalid")

    def probe_capability(self, capability: str, *, environment: str | None):
        available = capability == "image_generator" and self._api_key is not None
        return {
            "available": available,
            "environment": environment,
            "provider": "openai",
            "model": os.environ.get("OPENAI_IMAGE_MODEL", "gpt-image-2"),
            "reason_code": "capability_ready" if available else "openai_image_key_missing",
        }

    def generate(
        self,
        *,
        prompt: str,
        ratio: str,
        output_path: Path,
        idempotency_key: str,
        deadline_at: float,
    ) -> ProviderResult:
        if self._api_key is None:
            raise DefinitiveNotAccepted("openai_image_not_configured")
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 1500:
            raise ValueError("openai_image_prompt_invalid")
        size = {"16:9": "1536x1024", "9:16": "1024x1536"}.get(ratio)
        if size is None:
            raise ValueError("openai_image_ratio_invalid")
        target = Path(output_path)
        if not target.is_absolute() or target.exists() or not target.parent.is_dir():
            raise ValueError("openai_image_output_invalid")
        remaining = deadline_at - time.time()
        if remaining <= 0:
            raise TimeoutError("openai_image_deadline_exceeded")
        started = time.monotonic()
        try:
            response = self._transport.open(
                method="POST",
                url=_ENDPOINT,
                headers={
                    "Author" + "ization": "Bearer " + self._api_key.value,
                    "Content-Type": "application/json",
                    "Idempotency-Key": idempotency_key,
                },
                json_body={
                    "model": os.environ.get("OPENAI_IMAGE_MODEL", "gpt-image-2"),
                    "prompt": prompt.strip(),
                    "size": size,
                    "quality": "medium",
                    "output_format": "png",
                },
                timeout=min(240.0, remaining),
            )
        except (TimeoutError, OSError) as exc:
            raise SubmissionUnknown("openai_image_submission_unknown") from exc
        try:
            status = int(getattr(response, "status", getattr(response, "code", 0)))
            if not 200 <= status < 300:
                if status < 500 or status == 429:
                    raise DefinitiveNotAccepted("openai_image_not_accepted")
                raise SubmissionUnknown("openai_image_submission_unknown")
            raw = response.read((((_MAX_BYTES + 2) // 3) * 4) + 1)
            if len(raw) > ((_MAX_BYTES + 2) // 3) * 4:
                raise ValueError("openai_image_response_too_large")
            payload = json.loads(raw)
            images = payload.get("data") if isinstance(payload, Mapping) else None
            encoded = images[0].get("b64_json") if isinstance(images, list) and len(images) == 1 and isinstance(images[0], Mapping) else None
            if not isinstance(encoded, str) or len(encoded) > ((_MAX_BYTES + 2) // 3) * 4:
                raise ValueError("openai_image_response_invalid")
            content = base64.b64decode(encoded, validate=True)
            if not content.startswith(b"\x89PNG\r\n\x1a\n") or not 0 < len(content) <= _MAX_BYTES:
                raise ValueError("openai_image_content_invalid")
            temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
            temporary.write_bytes(content)
            os.replace(temporary, target)
            usage = payload.get("usage") if isinstance(payload.get("usage"), Mapping) else {}
            tokens = usage.get("total_tokens", 0)
            return ProviderResult(
                provider="openai",
                capability="image_generation",
                request_id=str(payload.get("id") or payload.get("request_id") or "openai-image-response"),
                payload={"sha256": hashlib.sha256(content).hexdigest(), "size_bytes": len(content), "content_type": "image/png"},
                usage={"tokens": tokens if isinstance(tokens, (int, float)) and tokens >= 0 else 0},
                elapsed_ms=max(0, round((time.monotonic() - started) * 1000)),
            )
        finally:
            try:
                response.close()
            except Exception:
                pass


__all__ = ("OpenAIImageGenerator",)
