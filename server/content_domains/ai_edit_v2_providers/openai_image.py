"""OpenAI image generation with bounded download and COS-first persistence."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Callable

from .. import ai_edit_v2_cos, ai_edit_v2_store
from .base import ProviderError, ProviderResult, RetryableProviderError


_ENDPOINT = "https://api.openai.com/v1/images/generations"
_ALLOWED_CONTENT_TYPES = frozenset({"image/png", "image/jpeg", "image/webp"})
_MAX_IMAGE_BYTES = 15 * 1024 * 1024


class OpenAIImageProvider:
    def __init__(
        self,
        *,
        owner: str,
        job_id: str,
        api_key: str | None = None,
        cos_api: Any = ai_edit_v2_cos,
        asset_store: Any = ai_edit_v2_store,
        http_request: Callable[..., dict[str, Any]] | None = None,
        downloader: Callable[..., dict[str, Any]] | None = None,
        clock_ms: Callable[[], int] | None = None,
        timeout_seconds: int = 60,
        db_path: str | None = None,
        model: str | None = None,
    ) -> None:
        try:
            uuid.UUID(job_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("image_job_scope_invalid") from exc
        if not isinstance(owner, str) or not owner:
            raise ValueError("image_job_scope_invalid")
        self.owner = owner
        self.job_id = job_id
        self.api_key = api_key if api_key is not None else os.environ.get("OPENAI_API_KEY", "")
        self.cos_api = cos_api
        self.asset_store = asset_store
        self.http_request = http_request or self._stdlib_request
        self.downloader = downloader or self._stdlib_download
        self.clock_ms = clock_ms or (lambda: round(time.monotonic() * 1000))
        self.timeout_seconds = int(timeout_seconds)
        self.db_path = db_path
        self.model = model or os.environ.get("OPENAI_IMAGE_MODEL", "gpt-image-2")
        self.max_download_bytes = _MAX_IMAGE_BYTES

    def generate(self, slot: dict[str, Any], idempotency_key: str) -> ProviderResult:
        if not isinstance(idempotency_key, str) or not idempotency_key.strip() or len(idempotency_key) > 255:
            raise ProviderError("openai_image_idempotency_key_invalid")
        existing = self.asset_store.find_generated_material(
            self.owner,
            self.job_id,
            idempotency_key,
            db_path=self.db_path,
        )
        if existing is not None:
            payload = self._existing_payload(existing)
            return ProviderResult(
                provider="openai",
                capability="image_generation",
                request_id="idempotent-replay",
                payload=payload,
                cost_units=0,
                elapsed_ms=0,
            )
        if not self.api_key:
            raise ProviderError("openai_image_not_configured")
        prompt, size, width, height = self._request_fields(slot)
        started_at = self.clock_ms()
        body = json.dumps(
            {"model": self.model, "prompt": prompt, "size": size, "quality": "medium"},
            ensure_ascii=False,
        ).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Idempotency-Key": idempotency_key,
        }
        try:
            response = self.http_request(
                "POST", _ENDPOINT, headers, body, self.timeout_seconds
            )
        except urllib.error.HTTPError as exc:
            if exc.code in (408, 429) or exc.code >= 500:
                raise RetryableProviderError("openai_image_unavailable") from exc
            raise ProviderError("openai_image_request_rejected") from exc
        except (TimeoutError, urllib.error.URLError, OSError) as exc:
            raise RetryableProviderError("openai_image_unavailable") from exc
        request_id, output, cost_units = self._parse_response(response)
        image = self._download_output(output)
        content = image["content"]
        content_type = image["content_type"]
        suffix = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}[content_type]
        object_id = uuid.uuid5(uuid.UUID(self.job_id), idempotency_key)
        owner_hash = hashlib.sha256(self.owner.encode("utf-8")).hexdigest()[:16]
        cos_key = f"ai-edit-v2/{owner_hash}/{self.job_id}/generated/{object_id}{suffix}"
        self.cos_api.put_bytes(content, cos_key, content_type, private=True)
        head = self.cos_api.head_object(cos_key)
        if (
            not isinstance(head, dict)
            or head.get("content_length") != len(content)
            or str(head.get("content_type", "")).split(";", 1)[0].lower() != content_type
            or not head.get("etag")
        ):
            raise ProviderError("image_cos_verification_failed")
        material = self.asset_store.create_generated_material(
            owner=self.owner,
            job_id=self.job_id,
            idempotency_key=idempotency_key,
            cos_key=cos_key,
            mime_type=content_type,
            etag=str(head["etag"]),
            size_bytes=len(content),
            width=width,
            height=height,
            now=round(time.time()),
            db_path=self.db_path,
        )
        payload = self._existing_payload(material)
        return ProviderResult(
            provider="openai",
            capability="image_generation",
            request_id=request_id,
            payload=payload,
            cost_units=cost_units,
            elapsed_ms=max(0, self.clock_ms() - started_at),
        )

    def _existing_payload(self, material: Any) -> dict[str, Any]:
        row = dict(material)
        if row.get("owner") != self.owner or row.get("job_id") != self.job_id:
            raise ProviderError("image_asset_scope_invalid")
        cos_key = row.get("cos_key")
        owner_hash = hashlib.sha256(self.owner.encode("utf-8")).hexdigest()[:16]
        expected_prefix = f"ai-edit-v2/{owner_hash}/{self.job_id}/generated/"
        if not isinstance(cos_key, str) or not cos_key.startswith(expected_prefix):
            raise ProviderError("image_asset_scope_invalid")
        return {
            "asset_id": row["id"],
            "cos_key": cos_key,
            "width": row.get("width"),
            "height": row.get("height"),
            "content_type": row.get("mime_type"),
            "size_bytes": row.get("size_bytes"),
            "etag": row.get("etag"),
        }

    @staticmethod
    def _request_fields(slot: dict[str, Any]) -> tuple[str, str, int, int]:
        if not isinstance(slot, dict):
            raise ProviderError("openai_image_slot_invalid")
        prompt = slot.get("semantic_query")
        ratio = slot.get("ratio")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ProviderError("openai_image_slot_invalid")
        if ratio == "16:9":
            return prompt.strip(), "1536x1024", 1536, 1024
        if ratio == "9:16":
            return prompt.strip(), "1024x1536", 1024, 1536
        raise ProviderError("openai_image_slot_invalid")

    @staticmethod
    def _parse_response(response: Any) -> tuple[str, dict[str, Any], int]:
        if not isinstance(response, dict):
            raise ProviderError("openai_image_response_invalid")
        request_id = (
            response.get("id") or response.get("request_id") or response.get("_request_id")
        )
        data = response.get("data")
        usage = response.get("usage") or {}
        if (
            not isinstance(request_id, str)
            or not request_id
            or not isinstance(data, list)
            or len(data) != 1
            or not isinstance(data[0], dict)
            or not isinstance(usage, dict)
        ):
            raise ProviderError("openai_image_response_invalid")
        cost = usage.get("total_tokens", 0)
        if not isinstance(cost, int) or isinstance(cost, bool) or cost < 0:
            raise ProviderError("openai_image_response_invalid")
        output = data[0]
        if not isinstance(output.get("url"), str) and not isinstance(output.get("b64_json"), str):
            raise ProviderError("openai_image_response_invalid")
        return request_id, output, cost

    def _download_output(self, output: dict[str, Any]) -> dict[str, Any]:
        if isinstance(output.get("b64_json"), str):
            try:
                downloaded = {
                    "content": base64.b64decode(output["b64_json"], validate=True),
                    "content_type": "image/png",
                }
            except (ValueError, TypeError) as exc:
                raise ProviderError("openai_image_response_invalid") from exc
        else:
            downloaded = self.downloader(
                output["url"],
                self.max_download_bytes,
                _ALLOWED_CONTENT_TYPES,
                self.timeout_seconds,
            )
        if not isinstance(downloaded, dict):
            raise ProviderError("openai_image_download_invalid")
        content = downloaded.get("content")
        content_type = str(downloaded.get("content_type") or "").split(";", 1)[0].lower()
        if content_type not in _ALLOWED_CONTENT_TYPES:
            raise ProviderError("image_content_type_invalid")
        if not isinstance(content, bytes) or not content:
            raise ProviderError("openai_image_download_invalid")
        if len(content) > self.max_download_bytes:
            raise ProviderError("image_download_too_large")
        return {"content": content, "content_type": content_type}

    @staticmethod
    def _stdlib_request(method: str, url: str, headers: dict[str, str], body: bytes, timeout: int) -> dict[str, Any]:
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            parsed = json.loads(response.read().decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ProviderError("openai_image_response_invalid")
        request_id = response.headers.get("x-request-id")
        if request_id:
            parsed = {**parsed, "_request_id": request_id}
        return parsed

    @staticmethod
    def _stdlib_download(
        url: str, max_bytes: int, allowed_content_types: frozenset[str], timeout: int
    ) -> dict[str, Any]:
        request = urllib.request.Request(url, headers={"Accept": "image/*"}, method="GET")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0].lower()
            if content_type not in allowed_content_types:
                raise ProviderError("image_content_type_invalid")
            declared = response.headers.get("Content-Length")
            if declared is not None and int(declared) > max_bytes:
                raise ProviderError("image_download_too_large")
            content = response.read(max_bytes + 1)
        if len(content) > max_bytes:
            raise ProviderError("image_download_too_large")
        return {"content": content, "content_type": content_type}
