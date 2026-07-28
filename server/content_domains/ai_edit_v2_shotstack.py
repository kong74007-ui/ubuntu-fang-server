"""Audited Shotstack timeline construction, submission, and reconciliation."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from . import ai_edit_v2_store as store
from .ai_edit_v2_providers.base import (
    ProviderError,
    ProviderResult,
    RetryableProviderError,
    UnknownSubmissionError,
)
from .ai_edit_v2_schema import STABLE_RENDER_COMPONENTS, validate_render_graph


_DEFAULT_API_BASE = "https://api.shotstack.io/edit/stage"


class RenderGraphError(RuntimeError):
    pass


def build_render_graph(
    resolved_plan: dict[str, Any],
    signed_assets: dict[str, str],
    font_url: str,
) -> dict[str, Any]:
    """Build the complete audited graph immediately before provider submission."""

    try:
        duration_ms = _positive_int(resolved_plan["duration_ms"])
        aspect_ratio = resolved_plan["aspect_ratio"]
        timeline = resolved_plan["text_timeline"]
        if timeline.get("alignment_status") != "aligned":
            raise RenderGraphError("caption_timeline_not_aligned")
        if not isinstance(font_url, str) or "noto" not in font_url.lower():
            raise RenderGraphError("caption_font_invalid")
        _reject_unstable_components(resolved_plan.get("components", []))
        components: list[dict[str, Any]] = []

        primary = resolved_plan.get("primary_video")
        if primary:
            components.append(
                _asset_component(
                    "broll_video", primary, signed_assets, 0, duration_ms
                )
            )
        for sentence in timeline.get("sentences") or []:
            start_ms, end_ms = _time_range(sentence, duration_ms)
            text = sentence.get("text")
            if not isinstance(text, str) or not text.strip():
                raise RenderGraphError("caption_timeline_invalid")
            components.append(
                {
                    "type": "basic_caption",
                    "text": text,
                    "start": start_ms / 1000,
                    "length": (end_ms - start_ms) / 1000,
                    "font_url": font_url,
                }
            )
        materials = resolved_plan.get("materials") or {}
        if not isinstance(materials, dict):
            raise RenderGraphError("resolved_materials_invalid")
        for scene in resolved_plan.get("scenes") or []:
            start_ms, end_ms = _time_range(scene, duration_ms)
            headline = scene.get("headline")
            if isinstance(headline, str) and headline.strip():
                components.append(
                    {
                        "type": "basic_card",
                        "text": headline,
                        "start": start_ms / 1000,
                        "length": (end_ms - start_ms) / 1000,
                    }
                )
            transition = scene.get("transition")
            if transition in {"cut", "dissolve", "fade", "wipe"}:
                transition_ms = min(500, end_ms - start_ms)
                components.append(
                    {
                        "type": "standard_transition",
                        "name": transition,
                        "start": start_ms / 1000,
                        "length": transition_ms / 1000,
                    }
                )
            for slot_id in scene.get("material_slots") or []:
                material = materials.get(slot_id)
                if not isinstance(material, dict):
                    raise RenderGraphError("resolved_material_missing")
                kind = material.get("kind")
                component_type = {
                    "image": "broll_image",
                    "video": "broll_video",
                }.get(kind)
                if component_type is None:
                    raise RenderGraphError("resolved_material_kind_invalid")
                components.append(
                    _asset_component(
                        component_type, material, signed_assets, start_ms, end_ms
                    )
                )
        mastered = resolved_plan.get("mastered_audio")
        if mastered is not None:
            if not isinstance(mastered, dict) or mastered.get("source") != "mix_audio":
                raise RenderGraphError("audio_bed_not_mastered")
            components.append(
                _asset_component("audio_bed", mastered, signed_assets, 0, duration_ms)
            )
        graph = {
            "version": "1.0",
            "aspect_ratio": aspect_ratio,
            "duration_ms": duration_ms,
            "components": components,
            "output": {
                "format": "mp4",
                "resolution": "1080p",
                "video_codec": "h264",
                "audio_codec": "aac",
            },
        }
        validate_render_graph(graph)
        return graph
    except RenderGraphError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise RenderGraphError("render_graph_invalid") from exc


def _reject_unstable_components(components: Any) -> None:
    if not isinstance(components, list):
        raise RenderGraphError("render_components_invalid")
    for component in components:
        if not isinstance(component, dict) or component.get("type") not in STABLE_RENDER_COMPONENTS:
            raise RenderGraphError("advanced_render_component_forbidden")
        if {"code", "html", "script", "jsx"}.intersection(component):
            raise RenderGraphError("free_code_render_component_forbidden")


def _asset_component(
    kind: str,
    asset: dict[str, Any],
    signed_assets: dict[str, str],
    start_ms: int,
    end_ms: int,
) -> dict[str, Any]:
    cos_key = asset.get("cos_key")
    if not isinstance(cos_key, str) or not cos_key:
        raise RenderGraphError("render_asset_key_invalid")
    src = signed_assets.get(cos_key)
    if not isinstance(src, str) or not src.startswith(("http://", "https://")):
        raise RenderGraphError("render_asset_signature_missing")
    return {
        "type": kind,
        "start": start_ms / 1000,
        "length": (end_ms - start_ms) / 1000,
        "src": src,
    }


def _time_range(value: dict[str, Any], duration_ms: int) -> tuple[int, int]:
    start_ms = int(value["start_ms"])
    end_ms = int(value["end_ms"])
    if start_ms < 0 or end_ms <= start_ms or end_ms > duration_ms:
        raise RenderGraphError("render_timing_invalid")
    return start_ms, end_ms


def _positive_int(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError
    result = int(value)
    if result <= 0:
        raise ValueError
    return result


class ShotstackClient:
    def __init__(
        self,
        *,
        job_id: str,
        attempt_id: int,
        api_key: str | None = None,
        api_base: str | None = None,
        db_path: str | None = None,
        http_request: Callable[[str, str, dict[str, str], bytes | None, int], dict[str, Any]] | None = None,
        timeout_seconds: int = 30,
        clock_ms: Callable[[], int] | None = None,
        now_seconds: Callable[[], int] | None = None,
    ) -> None:
        self.job_id = job_id
        self.attempt_id = int(attempt_id)
        self.api_key = api_key if api_key is not None else os.environ.get("SHOTSTACK_API_KEY", "")
        self.api_base = (api_base or os.environ.get("SHOTSTACK_API_BASE") or _DEFAULT_API_BASE).rstrip("/")
        self.db_path = db_path
        self.http_request = http_request or self._stdlib_request
        self.timeout_seconds = int(timeout_seconds)
        self.clock_ms = clock_ms or (lambda: round(time.monotonic() * 1000))
        self.now_seconds = now_seconds or (lambda: round(time.time()))

    def submit(self, render_graph: dict[str, Any], reference: str) -> ProviderResult:
        if not isinstance(render_graph, dict):
            raise ProviderError("shotstack_render_graph_invalid")
        if not isinstance(reference, str) or not reference.strip():
            raise ProviderError("shotstack_reference_invalid")
        existing = store.find_provider_submission(
            "shotstack", reference=reference, db_path=self.db_path
        )
        if existing is not None:
            return self.reconcile(provider_task_id=existing["provider_task_id"])
        try:
            claimed = store.claim_provider_submission_reference(
                attempt_id=self.attempt_id,
                job_id=self.job_id,
                reference=reference,
                db_path=self.db_path,
            )
        except ValueError as exc:
            raise ProviderError("shotstack_reference_conflict") from exc
        if not claimed:
            durable = store.find_stage_submission(
                self.attempt_id, db_path=self.db_path
            )
            if durable and durable.get("provider_task_id"):
                return self.reconcile(
                    provider_task_id=durable["provider_task_id"]
                )
            try:
                return self.reconcile(reference=reference)
            except ProviderError as exc:
                raise UnknownSubmissionError("shotstack_submission_unknown") from exc
        started = self.clock_ms()
        body = json.dumps(
            {**render_graph, "reference": reference},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        try:
            response = self._request("POST", f"{self.api_base}/render", body)
        except (TimeoutError, socket.timeout, RetryableProviderError, urllib.error.URLError, OSError):
            try:
                return self.reconcile(reference=reference)
            except ProviderError as exc:
                raise UnknownSubmissionError("shotstack_submission_unknown") from exc
        task_id, status, output_url, request_id = self._parse_response(response)
        self._bind(task_id, reference, status)
        return self._result(task_id, reference, status, output_url, request_id, started)

    def reconcile(
        self,
        provider_task_id: str | None = None,
        reference: str | None = None,
    ) -> ProviderResult:
        if bool(provider_task_id) == bool(reference):
            raise ProviderError("shotstack_reconcile_identity_invalid")
        started = self.clock_ms()
        if provider_task_id:
            encoded = urllib.parse.quote(provider_task_id, safe="")
            url = f"{self.api_base}/render/{encoded}"
        else:
            encoded = urllib.parse.urlencode({"reference": reference})
            url = f"{self.api_base}/render?{encoded}"
        response = self._request("GET", url, None)
        task_id, status, output_url, request_id = self._parse_response(
            response, expected_task_id=provider_task_id, expected_reference=reference
        )
        bound = store.find_provider_submission(
            "shotstack", provider_task_id=task_id, db_path=self.db_path
        )
        durable_reference = reference or (bound or {}).get("reference")
        if not durable_reference:
            raise ProviderError("shotstack_reference_missing")
        self._bind(task_id, durable_reference, status)
        return self._result(
            task_id, durable_reference, status, output_url, request_id, started
        )

    def _bind(self, task_id: str, reference: str, status: str) -> None:
        try:
            store.bind_provider_submission(
                attempt_id=self.attempt_id,
                job_id=self.job_id,
                provider="shotstack",
                capability="render",
                provider_task_id=task_id,
                reference=reference,
                status=status,
                now=self.now_seconds(),
                db_path=self.db_path,
            )
        except ValueError as exc:
            raise UnknownSubmissionError("shotstack_submission_persistence_failed") from exc

    def _request(self, method: str, url: str, body: bytes | None) -> dict[str, Any]:
        if not self.api_key:
            raise ProviderError("shotstack_not_configured")
        try:
            return self.http_request(
                method,
                url,
                {"x-api-key": self.api_key, "Content-Type": "application/json"},
                body,
                self.timeout_seconds,
            )
        except (TimeoutError, socket.timeout):
            raise
        except urllib.error.HTTPError as exc:
            if exc.code in {408, 429} or exc.code >= 500:
                raise RetryableProviderError("shotstack_unavailable") from exc
            raise ProviderError("shotstack_request_rejected") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise RetryableProviderError("shotstack_unavailable") from exc

    @staticmethod
    def _parse_response(
        response: Any,
        *,
        expected_task_id: str | None = None,
        expected_reference: str | None = None,
    ) -> tuple[str, str, str | None, str]:
        if not isinstance(response, dict) or response.get("success") is not True:
            raise ProviderError("shotstack_response_invalid")
        value = response.get("response")
        if isinstance(value, list):
            matches = [
                item for item in value
                if isinstance(item, dict)
                and (expected_reference is None or item.get("reference") == expected_reference)
            ]
            if len(matches) != 1:
                raise ProviderError("shotstack_render_not_found")
            value = matches[0]
        if not isinstance(value, dict):
            raise ProviderError("shotstack_response_invalid")
        task_id = value.get("id")
        if not isinstance(task_id, str) or not task_id:
            raise ProviderError("shotstack_response_invalid")
        if expected_task_id is not None and task_id != expected_task_id:
            raise ProviderError("shotstack_response_identity_mismatch")
        raw_status = str(value.get("status") or "queued").lower()
        if raw_status in {"queued", "pending", "rendering", "processing", "fetching"}:
            status = "pending"
        elif raw_status in {"done", "completed", "succeeded"}:
            status = "succeeded"
        elif raw_status in {"failed", "cancelled", "canceled"}:
            status = "failed"
        else:
            raise ProviderError("shotstack_status_invalid")
        output_url = value.get("url")
        if status == "succeeded" and (
            not isinstance(output_url, str)
            or not output_url.startswith(("http://", "https://"))
        ):
            raise ProviderError("shotstack_output_invalid")
        if status != "succeeded":
            output_url = None
        request_id = response.get("request_id") or response.get("message") or task_id
        return task_id, status, output_url, str(request_id)

    def _result(
        self,
        task_id: str,
        reference: str,
        status: str,
        output_url: str | None,
        request_id: str,
        started: int,
    ) -> ProviderResult:
        payload: dict[str, Any] = {
            "provider_task_id": task_id,
            "reference": reference,
            "status": status,
        }
        if output_url is not None:
            payload["output_url"] = output_url
        return ProviderResult(
            provider="shotstack",
            capability="render",
            request_id=request_id,
            payload=payload,
            cost_units=0,
            elapsed_ms=max(0, self.clock_ms() - started),
        )

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
            raise ProviderError("shotstack_response_invalid")
        return parsed


def reconcile_webhook(
    job_id: str,
    event: dict[str, Any],
    client: ShotstackClient,
    *,
    received_at: int,
    db_path: str | None = None,
) -> ProviderResult | None:
    """Deduplicate a webhook wakeup; never trust its status or output URL."""

    if not isinstance(event, dict):
        raise ProviderError("shotstack_webhook_invalid")
    task_id = event.get("id")
    if not isinstance(task_id, str) or not task_id:
        raise ProviderError("shotstack_webhook_invalid")
    canonical = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    inserted = store.record_provider_event(
        job_id,
        "shotstack",
        task_id,
        "wake",
        fingerprint,
        received_at,
        db_path=db_path,
    )
    if not inserted:
        return None
    return client.reconcile(provider_task_id=task_id)
