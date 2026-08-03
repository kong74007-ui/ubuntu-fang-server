"""Qwen director transport for DashScope's OpenAI-compatible endpoint."""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from server.content_domains.ai_edit_v2_providers.base import (
    ProviderError,
    ProviderResult,
    RetryableProviderError,
)


_ENDPOINT = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
_MODEL = "qwen3.7-max-2026-06-08"


class DashScopeCompatibleQwenClient:
    """Call the V3 Qwen model without routing it through the legacy endpoint."""

    def __init__(
        self,
        *,
        http_request: Callable[[str, str, dict[str, str], bytes | None, int], dict[str, Any]]
        | None = None,
        timeout_seconds: int = 45,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self._http_request = http_request or self._stdlib_request
        self._timeout_seconds = int(timeout_seconds)
        self._clock_ms = clock_ms or (lambda: round(time.monotonic() * 1000))

    def generate_edit_plan(self, system_prompt: str, user_prompt: str) -> ProviderResult:
        if not isinstance(system_prompt, str) or not system_prompt.strip():
            raise ProviderError("dashscope_director_prompt_invalid")
        if not isinstance(user_prompt, str) or not user_prompt.strip():
            raise ProviderError("dashscope_director_prompt_invalid")

        api_key = os.environ.get("DASHSCOPE_API_KEY", "")
        if not api_key:
            raise ProviderError("dashscope_not_configured")
        started_at = self._clock_ms()
        body = json.dumps(
            {
                "model": os.environ.get("DASHSCOPE_QWEN_MODEL", _MODEL),
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            },
            ensure_ascii=False,
        ).encode("utf-8")
        try:
            response = self._http_request(
                "POST",
                os.environ.get("DASHSCOPE_QWEN_COMPATIBLE_URL", _ENDPOINT),
                {
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                body,
                self._timeout_seconds,
            )
        except (TimeoutError, socket.timeout) as exc:
            raise RetryableProviderError("dashscope_director_unavailable") from exc
        except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
            status = getattr(exc, "code", None)
            if status is None or status in {408, 429} or int(status) >= 500:
                raise RetryableProviderError("dashscope_director_unavailable") from exc
            raise ProviderError("dashscope_director_request_rejected") from exc

        request_id, content, tokens = self._normalize_response(response)
        return ProviderResult(
            provider="dashscope",
            capability="director",
            request_id=request_id,
            payload={"content": content},
            cost_units=tokens,
            elapsed_ms=max(0, self._clock_ms() - started_at),
        )

    @staticmethod
    def _normalize_response(response: Any) -> tuple[str, str, int]:
        if not isinstance(response, dict):
            raise ProviderError("dashscope_director_response_invalid")
        request_id = response.get("id")
        choices = response.get("choices")
        usage = response.get("usage", {})
        if (
            not isinstance(request_id, str)
            or not request_id
            or not isinstance(choices, list)
            or len(choices) != 1
            or not isinstance(choices[0], dict)
            or not isinstance(usage, dict)
        ):
            raise ProviderError("dashscope_director_response_invalid")
        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise ProviderError("dashscope_director_response_invalid")
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise ProviderError("dashscope_director_response_invalid")
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
        if (
            isinstance(prompt_tokens, bool)
            or not isinstance(prompt_tokens, int)
            or prompt_tokens < 0
            or isinstance(completion_tokens, bool)
            or not isinstance(completion_tokens, int)
            or completion_tokens < 0
        ):
            raise ProviderError("dashscope_director_response_invalid")
        return request_id, content, prompt_tokens + completion_tokens

    @staticmethod
    def _stdlib_request(
        method: str,
        url: str,
        headers: dict[str, str],
        body: bytes | None,
        timeout: int,
    ) -> dict[str, Any]:
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            parsed = json.loads(response.read().decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ProviderError("dashscope_director_response_invalid")
        return parsed
