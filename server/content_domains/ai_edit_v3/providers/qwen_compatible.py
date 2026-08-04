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

    def generate_edit_plan(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        timeout_seconds: int | None = None,
    ) -> ProviderResult:
        return self._generate(
            system_prompt,
            user_prompt,
            timeout_seconds=timeout_seconds,
            strict_json=False,
        )

    def generate_director_decision(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        timeout_seconds: int | None = None,
    ) -> ProviderResult:
        return self._generate(
            system_prompt,
            user_prompt,
            timeout_seconds=timeout_seconds,
            strict_json=True,
        )

    def inspect_image(
        self,
        request: dict[str, Any],
        *,
        deadline_at: float,
    ) -> ProviderResult:
        """Review one short-lived private image URL without returning that URL."""

        image_url = request.get("image_url") if isinstance(request, dict) else None
        semantic = request.get("semantic") if isinstance(request, dict) else None
        forbidden = request.get("forbidden_subjects") if isinstance(request, dict) else None
        metadata = request.get("source_metadata") if isinstance(request, dict) else None
        if (
            not isinstance(image_url, str)
            or not image_url.startswith("https://")
            or not isinstance(semantic, str)
            or not semantic.strip()
            or not isinstance(forbidden, list)
            or any(not isinstance(item, str) or not item for item in forbidden)
            or not isinstance(metadata, dict)
            or request.get("output_contract") != "material-review-v1"
        ):
            raise ProviderError("dashscope_material_review_request_invalid")
        remaining = int(deadline_at - time.time())
        if remaining < 1:
            raise TimeoutError("material_review_deadline_exceeded")
        api_key = os.environ.get("DASHSCOPE_API_KEY", "")
        if not api_key:
            raise ProviderError("dashscope_not_configured")
        prompt = json.dumps(
            {
                "semantic": semantic.strip(),
                "forbidden_subjects": forbidden,
                "source_metadata": metadata,
                "output_contract": {
                    "result": "pass|fail",
                    "reason": "plain text without URL or credentials",
                    "evidence": [{
                        "semantic_match": "boolean",
                        "forbidden_subjects": forbidden,
                    }],
                },
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        body = json.dumps(
            {
                "model": os.environ.get("DASHSCOPE_QWEN_VL_MODEL", os.environ.get("DASHSCOPE_QWEN_MODEL", _MODEL)),
                "messages": [
                    {
                        "role": "system",
                        "content": "Review the image independently. Return only the exact JSON contract; never echo URLs, credentials, or signed parameters.",
                    },
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": image_url}},
                        ],
                    },
                ],
                "response_format": {"type": "json_object"},
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        started_at = self._clock_ms()
        try:
            response = self._http_request(
                "POST",
                os.environ.get("DASHSCOPE_QWEN_COMPATIBLE_URL", _ENDPOINT),
                {
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                body,
                min(self._timeout_seconds, remaining),
            )
        except (TimeoutError, socket.timeout) as exc:
            raise RetryableProviderError("dashscope_material_review_unavailable") from exc
        except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
            status = getattr(exc, "code", None)
            if status is None or status in {408, 429} or int(status) >= 500:
                raise RetryableProviderError("dashscope_material_review_unavailable") from exc
            raise ProviderError("dashscope_material_review_rejected") from exc
        request_id, content, tokens = self._normalize_response(response)
        return ProviderResult(
            provider="dashscope",
            capability="material_review",
            request_id=request_id,
            payload={"content": content},
            cost_units=tokens,
            elapsed_ms=max(0, self._clock_ms() - started_at),
        )

    def _generate(
        self,
        system_prompt: str,
        user_prompt: str,
        *,
        timeout_seconds: int | None,
        strict_json: bool,
    ) -> ProviderResult:
        if not isinstance(system_prompt, str) or not system_prompt.strip():
            raise ProviderError("dashscope_director_prompt_invalid")
        if not isinstance(user_prompt, str) or not user_prompt.strip():
            raise ProviderError("dashscope_director_prompt_invalid")

        api_key = os.environ.get("DASHSCOPE_API_KEY", "")
        if not api_key:
            raise ProviderError("dashscope_not_configured")
        started_at = self._clock_ms()
        request_body = {
            "model": os.environ.get("DASHSCOPE_QWEN_MODEL", _MODEL),
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        if strict_json:
            request_body["response_format"] = {"type": "json_object"}
        body = json.dumps(
            request_body,
            ensure_ascii=False,
        ).encode("utf-8")
        request_timeout = self._timeout_seconds
        if timeout_seconds is not None:
            if (
                isinstance(timeout_seconds, bool)
                or not isinstance(timeout_seconds, int)
                or timeout_seconds < 1
            ):
                raise ValueError("dashscope_timeout_invalid")
            request_timeout = min(request_timeout, timeout_seconds)
        try:
            response = self._http_request(
                "POST",
                os.environ.get("DASHSCOPE_QWEN_COMPATIBLE_URL", _ENDPOINT),
                {
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                body,
                request_timeout,
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
