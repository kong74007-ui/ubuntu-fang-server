"""Qwen director transport for DashScope's OpenAI-compatible endpoint."""

from __future__ import annotations

import base64
import json
import os
import re
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
_MATERIAL_REVIEW_SYSTEM_PROMPT = " ".join((
    "Review the image independently and return exactly one JSON object with the exact root keys result, reason, and evidence; do not add a wrapper such as output_contract, schema, result_data, or data.",
    "The evidence value must be a non-empty array and every evidence item must contain exactly semantic_match (a boolean) and forbidden_subjects (an array of strings).",
    "Each evidence.forbidden_subjects value may contain only the subset of forbidden_subjects_to_detect that is actually visible in the image; it must be [] when none are detected.",
    "Never copy the candidate list into evidence.forbidden_subjects and never treat a category to inspect as a detected category.",
    "Set result to pass if and only if at least one evidence item has semantic_match=true and every evidence.forbidden_subjects array is empty; otherwise set result to fail.",
    "Use wrong_product or wrong_store when requested_semantic identifies a specific product or store but the image visibly depicts a different identifiable one, or when requested_semantic asks for a generic or non-branded illustration but the image contains an identifiable branded product or real store; uncertainty alone is not a match.",
    "Use fabricated_real_world_evidence only for an image visibly presented as authentic documentary proof of a factual claim, not for a clearly illustrative concept graphic.",
    "The following is a structure-only pass example, not a conclusion about the supplied image: {\"result\":\"pass\",\"reason\":\"semantic matched and no forbidden subject detected\",\"evidence\":[{\"semantic_match\":true,\"forbidden_subjects\":[]}]}",
    "The reason must be plain text and must never echo URLs, credentials, signed parameters, or private storage identifiers.",
))


class DashScopeCompatibleQwenClient:
    """Call the V3 Qwen model without routing it through the legacy endpoint."""

    def __init__(
        self,
        *,
        http_request: Callable[[str, str, dict[str, str], bytes | None, int], dict[str, Any]]
        | None = None,
        timeout_seconds: int = 45,
        clock_ms: Callable[[], int] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._http_request = http_request or self._stdlib_request
        self._timeout_seconds = int(timeout_seconds)
        self._clock_ms = clock_ms or (lambda: round(time.monotonic() * 1000))
        self._sleep = sleep or time.sleep

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
                "contract_version": "material-review-v1",
                "requested_semantic": semantic.strip(),
                "forbidden_subjects_to_detect": forbidden,
                "source_metadata": metadata,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        body = json.dumps(
            {
                "model": os.environ.get("DASHSCOPE_QWEN_VL_MODEL") or _MODEL,
                "messages": [
                    {
                        "role": "system",
                        "content": _MATERIAL_REVIEW_SYSTEM_PROMPT,
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

    def describe_images(
        self,
        request: dict[str, Any],
        *,
        deadline_at: float,
    ) -> ProviderResult:
        """Describe up to five sanitized JPEGs using public upload aliases only."""

        images = request.get("images") if isinstance(request, dict) else None
        if (
            not isinstance(images, list)
            or not images
            or len(images) > 5
            or request.get("output_contract") != "material-descriptors-v1"
        ):
            raise ProviderError("dashscope_material_descriptor_request_invalid")
        prompt_images: list[dict[str, Any]] = []
        image_parts: list[dict[str, Any]] = []
        aliases: set[str] = set()
        prefix = "data:image/jpeg;base64,"
        for image in images:
            if not isinstance(image, dict) or set(image) != {
                "upload_alias", "width", "height", "data_url",
            }:
                raise ProviderError("dashscope_material_descriptor_request_invalid")
            alias = image.get("upload_alias")
            width = image.get("width")
            height = image.get("height")
            data_url = image.get("data_url")
            if (
                not isinstance(alias, str)
                or re.fullmatch(r"upload_[0-9]{2}", alias) is None
                or alias in aliases
                or isinstance(width, bool)
                or not isinstance(width, int)
                or not 1 <= width <= 512
                or isinstance(height, bool)
                or not isinstance(height, int)
                or not 1 <= height <= 512
                or not isinstance(data_url, str)
                or not data_url.startswith(prefix)
            ):
                raise ProviderError("dashscope_material_descriptor_request_invalid")
            try:
                pixels = base64.b64decode(data_url[len(prefix):], validate=True)
            except (ValueError, TypeError) as exc:
                raise ProviderError("dashscope_material_descriptor_request_invalid") from exc
            if not pixels.startswith(b"\xff\xd8") or len(pixels) > 256 * 1024:
                raise ProviderError("dashscope_material_descriptor_request_invalid")
            aliases.add(alias)
            prompt_images.append({
                "upload_alias": alias,
                "width": width,
                "height": height,
                "image_order": len(prompt_images) + 1,
            })
            image_parts.append({"type": "image_url", "image_url": {"url": data_url}})

        remaining = int(deadline_at - time.time())
        if remaining < 1:
            raise TimeoutError("material_descriptor_deadline_exceeded")
        api_key = os.environ.get("DASHSCOPE_API_KEY", "")
        if not api_key:
            raise ProviderError("dashscope_not_configured")
        input_manifest = json.dumps(
            prompt_images,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        prompt = (
            "Analyze every attached image in image_order. Describe only visible products, places, "
            "objects, documents, graphics, people, and composition. Do not identify a person or "
            "infer sensitive attributes. Use one concise Chinese semantic string per image. "
            "Return JSON only. The response object must use descriptors as its only top-level key; "
            "never return output_contract, schema, task, or images as top-level keys. Return exactly "
            "one descriptor per input, preserve input order, and copy each upload_alias exactly. "
            "Every descriptor must contain exactly upload_alias, semantic, subject_type, composition, "
            "supported_ratios, and risk_labels. subject_type must be one of product, store, venue, "
            "document, object, environment, graphic, person, or other. supported_ratios must contain "
            "one to three unique values chosen only from 16:9, 9:16, and 1:1. risk_labels may contain "
            "only person, face, text, logo, sensitive, "
            "or uncertain. Required JSON shape: "
            '{"descriptors":[{"upload_alias":"upload_01","semantic":"简短中文语义",'
            '"subject_type":"object","composition":"居中近景","supported_ratios":["1:1"],'
            '"risk_labels":[]}]}. '
            f"Input manifest: {input_manifest}"
        )
        body = json.dumps(
            {
                "model": os.environ.get("DASHSCOPE_QWEN_VL_MODEL") or _MODEL,
                "messages": [
                    {
                        "role": "system",
                        "content": (
                            "Return JSON only; the only allowed top-level key is descriptors. "
                            "Never return the key output_contract or echo image bytes, URLs, paths, "
                            "credentials, hashes, or hidden identifiers."
                        ),
                    },
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": prompt}, *image_parts],
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
            raise RetryableProviderError("dashscope_material_descriptor_unavailable") from exc
        except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
            status = getattr(exc, "code", None)
            if status is None or status in {408, 429} or int(status) >= 500:
                raise RetryableProviderError("dashscope_material_descriptor_unavailable") from exc
            raise ProviderError("dashscope_material_descriptor_rejected") from exc
        request_id, content, tokens = self._normalize_response(response)
        return ProviderResult(
            provider="dashscope",
            capability="material_analysis",
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
        endpoint = os.environ.get("DASHSCOPE_QWEN_COMPATIBLE_URL", _ENDPOINT)
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        deadline_ms = started_at + request_timeout * 1000
        current_timeout = request_timeout
        response: dict[str, Any] | None = None
        last_error: BaseException | None = None
        for attempt in range(2):
            try:
                response = self._http_request(
                    "POST",
                    endpoint,
                    headers,
                    body,
                    current_timeout,
                )
                break
            except (TimeoutError, socket.timeout) as exc:
                last_error = exc
            except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
                last_error = exc
                status = getattr(exc, "code", None)
                if not (
                    status is None
                    or status in {408, 429}
                    or int(status) >= 500
                ):
                    raise ProviderError("dashscope_director_request_rejected") from exc

            if attempt == 1:
                raise RetryableProviderError("dashscope_director_unavailable") from last_error
            if deadline_ms - self._clock_ms() <= 1_000:
                raise RetryableProviderError("dashscope_director_unavailable") from last_error
            self._sleep(0.5)
            current_timeout = int((deadline_ms - self._clock_ms()) // 1000)
            if current_timeout < 1:
                raise RetryableProviderError("dashscope_director_unavailable") from last_error

        if response is None:
            raise RetryableProviderError("dashscope_director_unavailable")

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
