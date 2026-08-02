from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Literal, Mapping

from .base import DefinitiveNotAccepted, ProviderResult, SecretValue, SubmissionUnknown


QWEN_MODEL = "qwen3.7-max-2026-06-08"
WORKSPACE_ID_RE = re.compile(r"\A[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
MAX_REQUEST_BYTES = 512 * 1024


class DashScopeConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class CapabilityResult:
    available: bool
    provider: str
    model: str
    detail_code: str


def _extract_content(response: Mapping[str, Any]) -> str:
    output = response.get("output")
    if not isinstance(output, Mapping):
        return ""
    choices = output.get("choices")
    if not isinstance(choices, list):
        return ""
    chunks: list[str] = []
    for choice in choices:
        if not isinstance(choice, Mapping):
            continue
        message = choice.get("message")
        if not isinstance(message, Mapping):
            continue
        content = message.get("content")
        if isinstance(content, str):
            chunks.append(content)
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, Mapping) and isinstance(item.get("text"), str):
                    chunks.append(item["text"])
    return "".join(chunks)


class DashScopeMultimodalClient:
    def __init__(self, *, api_key: SecretValue, workspace_id: str, http: Any) -> None:
        if not isinstance(api_key, SecretValue):
            raise DashScopeConfigurationError("api_key_invalid")
        if WORKSPACE_ID_RE.fullmatch(workspace_id) is None:
            raise DashScopeConfigurationError("workspace_id_invalid")
        if http is None or not callable(getattr(http, "post", None)):
            raise DashScopeConfigurationError("http_client_invalid")
        self._api_key = api_key
        self._http = http
        self._endpoint = (
            f"https://{workspace_id}.cn-beijing.maas.aliyuncs.com/api/v1/services/"
            "aigc/multimodal-generation/generation"
        )

    def _request(
        self,
        request: Mapping[str, Any],
        *,
        capability: str,
        purpose: str,
        idempotency_key: str,
        deadline_at: float,
    ) -> ProviderResult:
        if time.time() >= deadline_at:
            raise TimeoutError("provider_deadline_exceeded")
        if (
            not isinstance(idempotency_key, str)
            or not idempotency_key
            or any(ord(char) < 0x20 for char in idempotency_key)
        ):
            raise ValueError("provider_idempotency_key_invalid")
        body = {
            "model": QWEN_MODEL,
            "input": dict(request),
            "purpose": purpose,
            "parameters": {
                "enable_thinking": True,
                "result_format": "message",
            },
        }
        encoded = json.dumps(
            body,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(encoded) > MAX_REQUEST_BYTES:
            raise ValueError("provider_request_too_large")
        started = time.monotonic()
        try:
            response = self._http.post(
                url=self._endpoint,
                json=body,
                headers={
                    "Authorization": f"Bearer {self._api_key.value}",
                    "Idempotency-Key": idempotency_key,
                    "Content-Type": "application/json",
                },
                deadline_at=deadline_at,
            )
        except Exception as exc:
            if bool(getattr(exc, "body_sent", False)):
                raise SubmissionUnknown("dashscope_submission_unknown") from None
            raise DefinitiveNotAccepted("dashscope_not_accepted") from None
        if not isinstance(response, Mapping):
            raise ValueError("dashscope_response_invalid")
        request_id = response.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("dashscope_request_id_missing")
        raw_usage = response.get("usage", {})
        if not isinstance(raw_usage, Mapping):
            raw_usage = {}
        usage = {
            str(key): value
            for key, value in raw_usage.items()
            if isinstance(key, str)
            and not isinstance(value, bool)
            and isinstance(value, (int, float))
            and value >= 0
        }
        return ProviderResult(
            provider="dashscope",
            capability=capability,
            request_id=request_id,
            payload={"content": _extract_content(response)},
            usage=usage,
            elapsed_ms=max(0, round((time.monotonic() - started) * 1000)),
        )

    def preflight(self, *, deadline_at: float) -> CapabilityResult:
        self._request(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [{"text": "仅返回一个空 JSON 对象。"}],
                    }
                ]
            },
            capability="preflight",
            purpose="preflight",
            idempotency_key="ai-edit-v3-preflight",
            deadline_at=deadline_at,
        )
        return CapabilityResult(True, "dashscope", QWEN_MODEL, "ok")

    def generate_plan(
        self,
        request: Mapping[str, Any],
        *,
        purpose: Literal["initial", "repair"],
        idempotency_key: str,
        deadline_at: float,
    ) -> ProviderResult:
        if purpose not in {"initial", "repair"}:
            raise ValueError("director_purpose_invalid")
        return self._request(
            request,
            capability="director",
            purpose=purpose,
            idempotency_key=idempotency_key,
            deadline_at=deadline_at,
        )

    def analyze_images(
        self,
        request: Mapping[str, Any],
        *,
        idempotency_key: str,
        deadline_at: float,
    ) -> ProviderResult:
        return self._request(
            request,
            capability="material_analysis",
            purpose="analysis",
            idempotency_key=idempotency_key,
            deadline_at=deadline_at,
        )
