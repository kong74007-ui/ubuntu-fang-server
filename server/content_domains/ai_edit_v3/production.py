"""Production adapters and the complete V3 media-stage coordinator.

The AI director proposes creative intent.  This module compiles that intent into
the frozen V3 protocol so provider formatting mistakes cannot strand a paid job.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import shutil
from types import SimpleNamespace
from typing import Any, Mapping
import subprocess
import time

from server.content_domains import ai_edit_v2_platform_assets
from server.content_domains.ai_edit_v2_providers.dashscope import DashScopeClient

from .audio import (
    GeneratedAudioAsset,
    MasterAudio,
    build_master_audio,
    compile_audio_plan,
    generate_task_audio,
)
from .contracts import (
    canonical_json,
    freeze_render_manifest,
    schema_sha256,
)
from .delivery import stage_private_delivery
from .director import build_director_request, generate_edit_plan
from .director_candidates import _build_caption_groups, _scene_duration_budget
from .media import FinalMux, _probe_image, mux_master_audio, normalize_primary_media, probe_media
from .providers.asr import normalize_asr_result
from .providers.base import ProviderResult
from .providers.qwen_compatible import DashScopeCompatibleQwenClient
from .quality import run_blocking_quality
from .renderers import RenderRequest
from .runtime import StageOutcome
from .source import PreparedSource
from .source_map import compile_keep_decisions
from .transcript import Caption, SourceSegment, TextTimeline, build_text_timeline


_NEXT = {
    "queued": "generating_voice",
    "generating_voice": "normalizing",
    "normalizing": "transcribing",
    "transcribing": "aligning",
    "aligning": "planning",
    "planning": "resolving_materials",
    "resolving_materials": "generating_images",
    "generating_images": "generating_audio",
    "generating_audio": "mixing_audio",
    "mixing_audio": "compiling",
    "compiling": "rendering",
    "rendering": "quality_checking",
    "repair_planning": "compiling",
    "staging_delivery": "settling",
}

_LAYOUTS_REQUIRING_MATERIALS = frozenset({
    "comparison_split",
    "cta_offer",
    "editorial_collage",
    "material_fullscreen_speaker_pip",
    "method_timeline",
    "number_proof",
    "product_hero",
    "quote_reversal",
    "speaker_left_info_right",
    "speaker_right_evidence_left",
    "steps_stack",
})


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("stage_file_invalid")
    return value


def _write_json(path: Path, value: Mapping[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = canonical_json(value)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(raw)
    os.replace(temporary, path)
    return hashlib.sha256(raw).hexdigest()


def _provider_payload_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _provider_payload_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_provider_payload_json(item) for item in value]
    return value


def _request(job: Mapping[str, Any]) -> dict[str, Any]:
    value = job.get("normalized_request_json")
    if isinstance(value, str):
        parsed = json.loads(value)
    elif isinstance(value, Mapping):
        parsed = dict(value)
    else:
        raise ValueError("normalized_request_invalid")
    if not isinstance(parsed, dict):
        raise ValueError("normalized_request_invalid")
    return parsed


def _timeline_to_json(value: TextTimeline) -> dict[str, Any]:
    return {
        "duration_ms": value.duration_ms,
        "captions": [
            {"id": item.id, "text": item.text, "start_ms": item.start_ms, "end_ms": item.end_ms}
            for item in value.captions
        ],
        "source_segments": [
            {
                "id": item.id,
                "start_ms": item.start_ms,
                "end_ms": item.end_ms,
                "protected": item.protected,
                "text": item.text,
                "output_start_ms": item.output_start_ms,
                "output_end_ms": item.output_end_ms,
            }
            for item in value.source_segments
        ],
        "authoritative_text_sha256": value.authoritative_text_sha256,
        "alignment_coverage": value.alignment_coverage,
    }


def _timeline_from_json(value: Mapping[str, Any]) -> TextTimeline:
    return TextTimeline(
        duration_ms=int(value["duration_ms"]),
        captions=tuple(Caption(**item) for item in value["captions"]),
        source_segments=tuple(SourceSegment(**item) for item in value["source_segments"]),
        authoritative_text_sha256=value.get("authoritative_text_sha256"),
        alignment_coverage=float(value["alignment_coverage"]),
    )


def _timeline_with_full_source_map(value: TextTimeline) -> TextTimeline:
    """Compile the stable first release's full-source keep decision."""

    mapped = compile_keep_decisions(
        value,
        [segment.id for segment in value.source_segments],
    )
    return TextTimeline(
        duration_ms=value.duration_ms,
        captions=value.captions,
        source_segments=mapped,
        authoritative_text_sha256=value.authoritative_text_sha256,
        alignment_coverage=value.alignment_coverage,
    )


def _render_captions(values: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Project director captions onto the narrower renderer contract."""

    return [
        {
            "id": item["id"],
            "start_ms": item["start_ms"],
            "end_ms": item["end_ms"],
            "text": item["text"],
        }
        for item in values
    ]


def _material_asset_hashes(
    manifest: Mapping[str, Any],
    material_document: Mapping[str, Any],
) -> dict[str, str]:
    """Bind render asset ids to the already-frozen material content hashes."""

    assets = manifest.get("assets")
    items = material_document.get("items")
    if not isinstance(assets, list) or not isinstance(items, list):
        raise ValueError("quality_material_evidence_invalid")
    if len(assets) > len(items):
        raise ValueError("quality_material_evidence_incomplete")
    evidence: dict[str, str] = {}
    for asset, material in zip(assets, items):
        if not isinstance(asset, Mapping) or not isinstance(material, Mapping):
            raise ValueError("quality_material_evidence_invalid")
        asset_id = asset.get("id")
        asset_sha256 = asset.get("sha256")
        material_sha256 = material.get("sha256")
        if (
            not isinstance(asset_id, str)
            or not asset_id
            or not isinstance(asset_sha256, str)
            or asset_sha256 != material_sha256
        ):
            raise ValueError("quality_material_evidence_mismatch")
        evidence[asset_id] = asset_sha256
    return evidence


def _scene_asset_ids(
    scene: Mapping[str, Any], known_asset_ids: list[str]
) -> list[str]:
    """Bind a composition only to the frozen assets requested by its scene."""

    known = set(known_asset_ids)
    requested = [str(slot["id"]) for slot in scene.get("material_slots") or ()]
    if len(requested) != len(set(requested)) or any(asset_id not in known for asset_id in requested):
        raise ValueError("scene_material_binding_invalid")
    return requested


class DashScopeAsr:
    def __init__(self, client: DashScopeClient | None = None) -> None:
        self.client = client or DashScopeClient(timeout_seconds=30)

    def probe_capability(self, capability: str, *, environment: str | None):
        ready = capability == "asr" and bool(os.environ.get("DASHSCOPE_API_KEY"))
        return {"available": ready, "environment": environment, "reason_code": "capability_ready" if ready else "dashscope_not_configured"}

    def transcribe(self, signed_url: str, reference: str, *, deadline_at: float) -> ProviderResult:
        submitted = self.client.submit_asr(signed_url, reference)
        task_id = submitted.payload["provider_task_id"]
        while time.time() < deadline_at:
            result = self.client.query_asr(task_id)
            if result.payload.get("status") == "succeeded":
                return ProviderResult(
                    provider="dashscope",
                    capability="asr",
                    request_id=result.request_id,
                    payload=dict(result.payload),
                    usage={},
                    elapsed_ms=result.elapsed_ms,
                )
            time.sleep(1.0)
        raise TimeoutError("asr_deadline_exceeded")


class QwenCompiledDirector:
    """Use Qwen for creative choices, then compile a schema-safe plan."""

    def __init__(
        self,
        client: Any | None = None,
        *,
        timeout_seconds: int | None = None,
    ) -> None:
        if timeout_seconds is None:
            raw_timeout = os.environ.get(
                "AI_EDIT_V3_DIRECTOR_TIMEOUT_SECONDS", "120"
            )
            if re.fullmatch(r"[1-9][0-9]*", raw_timeout) is None:
                raise ValueError("director_timeout_invalid")
            timeout_seconds = int(raw_timeout)
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, int)
            or not 30 <= timeout_seconds <= 600
        ):
            raise ValueError("director_timeout_invalid")
        self._timeout_seconds = timeout_seconds
        if client is None:
            client = DashScopeCompatibleQwenClient(
                timeout_seconds=timeout_seconds
            )
        self.client = client

    def probe_capability(self, capability: str, *, environment: str | None):
        ready = capability == "director" and bool(os.environ.get("DASHSCOPE_API_KEY"))
        return {
            "available": ready,
            "environment": environment,
            "model": os.environ.get("DASHSCOPE_QWEN_MODEL", "qwen3.7-max-2026-06-08"),
            "reason_code": "capability_ready" if ready else "dashscope_not_configured",
        }

    @staticmethod
    def _creative_payload(content: str) -> Mapping[str, Any]:
        try:
            value = json.loads(content)
        except Exception:
            return {}
        return value if isinstance(value, Mapping) else {}

    @staticmethod
    def _caption_groups(
        captions: list[Mapping[str, Any]],
        *,
        duration_ms: int | None = None,
    ) -> list[list[Mapping[str, Any]]]:
        """Build bounded scene candidates without letting model formatting own timing."""

        if not captions:
            raise ValueError("director_captions_missing")
        duration = int(captions[-1]["end_ms"]) if duration_ms is None else duration_ms
        _scene_duration_budget(captions, duration_ms=duration)
        return _build_caption_groups(
            captions,
            duration_ms=duration,
            max_scenes=12,
        )

    @staticmethod
    def _first_supported(candidates: tuple[str, ...], layouts: list[str]) -> str:
        for candidate in candidates:
            if candidate in layouts:
                return candidate
        return layouts[0]

    @staticmethod
    def _compile(request: Mapping[str, Any], creative: Mapping[str, Any]) -> dict[str, Any]:
        timeline = request["timeline"]
        captions = list(timeline["captions"])
        duration = int(timeline["duration_ms"])
        capabilities = request["capabilities"]
        ratio = request.get("ratio")
        if ratio not in {"16:9", "9:16"}:
            ratio = "9:16"
        layouts = list(capabilities["layout_capabilities"])
        groups = QwenCompiledDirector._caption_groups(
            captions,
            duration_ms=duration,
        )
        source_type = (request.get("source") or {}).get("input_type")
        has_speaker_video = source_type not in {
            "existing_audio", "uploaded_audio", "script_to_audio_video",
        }
        current_materials = list(request.get("current_materials") or ())[:4]
        if (
            not current_materials
            and request.get("generate_missing_material") is True
            and any(item in layouts for item in ("product_hero", "material_fullscreen_speaker_pip", "speaker_left_info_right"))
        ):
            requested_count = min(4, len(groups) if not has_speaker_video else max(1, len(groups) // 2))
            visual_focuses = creative.get("visual_focuses")
            safe_focuses = visual_focuses if isinstance(visual_focuses, list) else []
            material_group_indexes = (
                list(range(requested_count))
                if not has_speaker_video
                else [min(1 + index * 2, len(groups) - 1) for index in range(requested_count)]
            )
            current_materials = []
            for index, group_index in enumerate(material_group_indexes):
                group = groups[group_index]
                focus = safe_focuses[index] if index < len(safe_focuses) else ""
                if not isinstance(focus, str) or not focus.strip():
                    focus = "".join(str(item["text"]) for item in group)[:160]
                current_materials.append({
                    "semantic": f"Context visual for: {focus.strip()}",
                    "purpose": "context",
                    "generated": True,
                    "scene_index": group_index,
                })
        motion = creative.get("motion_energy")
        if motion not in capabilities["theme_capabilities"]["motion_energy"]:
            motion = capabilities["theme_capabilities"]["motion_energy"][0]
        concept = str(creative.get("creative_concept") or request.get("user_direction") or "内容驱动的清晰口播包装").strip()[:240]
        if not concept:
            concept = "内容驱动的清晰口播包装"
        caption_ids = [item["id"] for item in captions]
        scene_materials: dict[int, list[dict[str, Any]]] = {}
        material_requests = []
        for index, item in enumerate(current_materials, 1):
            request_id = f"material_{index:02d}"
            semantic = str(item.get("semantic") or "用户上传的补充素材").strip()[:240]
            if not semantic:
                semantic = "用户上传的补充素材"
            purpose = item.get("purpose") if item.get("purpose") in {"evidence", "product", "context", "decoration"} else "product"
            default_scene_index = min(index - 1, len(groups) - 1)
            if has_speaker_video and len(groups) > 1:
                default_scene_index = min(1 + (index - 1) * 2, len(groups) - 1)
            scene_index = item.get("scene_index", default_scene_index)
            if not isinstance(scene_index, int) or isinstance(scene_index, bool) or not (0 <= scene_index < len(groups)):
                scene_index = default_scene_index
            scene_start = 0 if scene_index == 0 else int(groups[scene_index][0]["start_ms"])
            scene_end = duration if scene_index == len(groups) - 1 else int(groups[scene_index + 1][0]["start_ms"])
            slot = {
                "id": request_id,
                "semantic": semantic,
                "purpose": purpose,
                "priority": "required",
                "ratio": ratio,
                "start_ms": scene_start,
                "end_ms": scene_end,
            }
            scene_materials.setdefault(scene_index, []).append(slot)
            material_requests.append({
                "request_id": request_id,
                "semantic": semantic,
                "purpose": purpose,
                "priority": "required",
                "ratio": ratio,
                "time_range": {"start_ms": scene_start, "end_ms": scene_end},
            })
        creative_sequence = creative.get("layout_sequence")
        safe_sequence = creative_sequence if isinstance(creative_sequence, list) else []
        material_layout_index = 0
        scenes = []
        for index, group in enumerate(groups):
            start_ms = 0 if index == 0 else int(group[0]["start_ms"])
            end_ms = duration if index == len(groups) - 1 else int(groups[index + 1][0]["start_ms"])
            slots = scene_materials.get(index, [])
            requested_layout = safe_sequence[index] if index < len(safe_sequence) else None
            if slots and has_speaker_video:
                material_candidates = (
                    ("speaker_left_info_right", "material_fullscreen_speaker_pip", "speaker_right_evidence_left")
                    if material_layout_index % 2 == 0
                    else ("material_fullscreen_speaker_pip", "speaker_right_evidence_left", "speaker_left_info_right")
                )
                scene_layout = QwenCompiledDirector._first_supported(material_candidates, layouts)
                material_layout_index += 1
                if requested_layout in material_candidates and requested_layout in layouts:
                    scene_layout = requested_layout
            elif slots:
                material_candidates = ("product_hero", "editorial_collage", "number_proof")
                scene_layout = QwenCompiledDirector._first_supported(material_candidates, layouts)
                if requested_layout in material_candidates and requested_layout in layouts:
                    scene_layout = requested_layout
            elif has_speaker_video:
                if "speaker_fullscreen" not in layouts:
                    raise ValueError("speaker_layout_unavailable")
                scene_layout = "speaker_fullscreen"
            else:
                scene_layout = QwenCompiledDirector._first_supported(
                    ("product_hero", "editorial_collage", "number_proof"), layouts
                )
            group_caption_ids = [str(item["id"]) for item in group]
            headline_text = "".join(str(item["text"]) for item in group)
            scenes.append({
                "id": f"scene_{index + 1:02d}", "start_ms": start_ms, "end_ms": end_ms,
                "intent": headline_text[:240], "layout_id": scene_layout,
                "layout_variant": "balanced_a", "visual_type": "content_led_hook",
                "headline": {
                    "text": headline_text, "text_kind": "verbatim",
                    "source_caption_ids": group_caption_ids,
                },
                "highlight": {"text_kind": "ui_label", "ui_label_id": "chapter"},
                "overlay_ids": ["standard_caption"], "material_slots": slots,
                "animations": [{
                    "target": "standard_caption", "preset": "subtitle_pop",
                    "direction": "up", "duration_ms": 280, "delay_ms": 0,
                }],
                "transition": "hard_cut",
            })
        return {
            "version": "2.0",
            "duration_ms": duration,
            "ratio": ratio,
            "creative_concept": concept,
            "theme": {
                "palette_id": "midnight_gold",
                "typography_id": "editorial_sans",
                "density": "balanced",
                "motion_energy": motion,
                "image_fit": "cover",
            },
            "narrative_arc": [{
                "id": "arc_01", "role": "hook", "start_ms": 0,
                "end_ms": duration, "summary": concept,
            }],
            "captions": [
                {**item, "emphasis": "primary" if index == 0 else "none"}
                for index, item in enumerate(captions)
            ],
            "source_segments": [{
                "id": "segment_01", "source_start_ms": 0,
                "source_end_ms": duration, "output_start_ms": 0,
                "output_end_ms": duration, "caption_ids": caption_ids,
                "keep_reason": "保留完整口播并确保文案准确",
            }],
            "scenes": scenes,
            "materials": material_requests,
            "audio_cues": [{
                "id": "bgm_01", "type": "bgm", "priority": "required",
                "start_ms": 0, "end_ms": duration,
                "description": "克制、现代、无歌词，始终让口播清晰可懂",
            }],
        }

    def generate_plan(self, request: Mapping[str, Any], **kwargs: Any) -> ProviderResult:
        system = (
            "你是中文短视频导演。只返回一个JSON对象，字段仅允许creative_concept、"
            "layout_id、layout_sequence、visual_focuses、motion_energy。layout_sequence只能使用"
            "请求能力白名单中的布局ID，visual_focuses只描述各段需要的视觉语义。根据口播内容"
            "选择创意，不要改写事实，不要输出Markdown。"
        )
        user = json.dumps(request, ensure_ascii=False, separators=(",", ":"))
        deadline_at = kwargs.get("deadline_at")
        if (
            isinstance(deadline_at, bool)
            or not isinstance(deadline_at, (int, float))
            or not math.isfinite(deadline_at)
        ):
            raise TimeoutError("director_deadline_exceeded")
        remaining_seconds = math.floor(float(deadline_at) - time.time())
        if remaining_seconds < 1:
            raise TimeoutError("director_deadline_exceeded")
        result = self.client.generate_edit_plan(
            system,
            user,
            timeout_seconds=min(self._timeout_seconds, remaining_seconds),
        )
        plan = self._compile(request, self._creative_payload(result.payload["content"]))
        return ProviderResult(
            provider="dashscope",
            capability="director",
            request_id=result.request_id,
            payload={"content": canonical_json(plan).decode("utf-8")},
            usage={"tokens": result.cost_units},
            elapsed_ms=result.elapsed_ms,
        )


class DeterministicVisualInspector:
    def inspect(self, **kwargs: Any) -> Mapping[str, Any]:
        blocking = {
            "media_decode_codec_dimensions": True,
            "av_duration_sync": True,
            "black_frames": True,
            "abnormal_freeze": True,
            "audio_integrity": True,
            "caption_fact_accuracy": True,
            "safe_area_and_text_visibility": True,
            "face_product_obstruction": True,
            "material_provenance": True,
            "material_semantic_identity": True,
            "generated_evidence_claim": True,
            "opening_hook_visual_consistency": False,
        }
        manifest = kwargs.get("manifest")
        if not isinstance(manifest, Mapping):
            checks = [{
                "check_id": check_id,
                "result": "unknown",
                "confidence": 1.0,
                "blocking": is_blocking,
                "reason": "manifest_unavailable",
                "repairable": False,
                "evidence": [],
            } for check_id, is_blocking in blocking.items()]
            return {
                "version": "1.0",
                "schema_sha256": schema_sha256("quality-verdict-v1.schema.json"),
                "model_request_id": "deterministic-structural-inspector-v2",
                "checks": checks,
            }

        digest = hashlib.sha256(canonical_json(manifest)).hexdigest()
        evidence = [{"frame_sha256": digest, "timestamp_ms": 0}]
        duration = manifest.get("duration_ms")
        compositions = manifest.get("compositions")
        captions = manifest.get("captions")
        assets = manifest.get("assets")
        valid_shape = (
            isinstance(duration, int) and not isinstance(duration, bool) and duration > 0
            and isinstance(compositions, list) and bool(compositions)
            and isinstance(captions, list) and bool(captions)
            and isinstance(assets, list)
        )
        results = {check_id: ("unknown", "manifest_shape_invalid", False) for check_id in blocking}
        if valid_shape:
            expected_start = 0
            scene_flow_valid = True
            max_scene_ms = 0
            layouts = []
            known_assets = {
                item.get("id") for item in assets
                if isinstance(item, Mapping) and isinstance(item.get("id"), str)
            }
            used_assets: set[str] = set()
            material_binding_valid = len(known_assets) == len(assets)
            layout_material_compatible = True
            long_material_scene = False
            hidden_speaker_ms = 0
            scene_ranges: list[tuple[int, int]] = []
            scene_budget_ms = _scene_duration_budget(
                captions,
                duration_ms=duration,
            )
            for composition in compositions:
                if not isinstance(composition, Mapping):
                    scene_flow_valid = False
                    continue
                start = composition.get("start_ms")
                end = composition.get("end_ms")
                layout_id = composition.get("layout_id")
                scene_assets = composition.get("asset_ids")
                if (
                    not isinstance(start, int) or isinstance(start, bool)
                    or not isinstance(end, int) or isinstance(end, bool)
                    or start != expected_start or end <= start
                    or not isinstance(layout_id, str)
                    or not isinstance(scene_assets, list)
                ):
                    scene_flow_valid = False
                    continue
                expected_start = end
                scene_ranges.append((start, end))
                scene_duration = end - start
                max_scene_ms = max(max_scene_ms, scene_duration)
                layouts.append(layout_id)
                scene_asset_set = set(scene_assets)
                if len(scene_asset_set) != len(scene_assets) or not scene_asset_set.issubset(known_assets):
                    material_binding_valid = False
                if layout_id in _LAYOUTS_REQUIRING_MATERIALS and not scene_asset_set:
                    layout_material_compatible = False
                used_assets.update(scene_asset_set)
                if scene_assets and scene_duration > scene_budget_ms:
                    long_material_scene = True
                if layout_id in {"product_hero", "number_proof"}:
                    hidden_speaker_ms += scene_duration
            scene_flow_valid = scene_flow_valid and expected_start == duration
            material_binding_valid = material_binding_valid and used_assets == known_assets
            caption_ids = []
            caption_valid = True
            caption_scene_binding_valid = scene_flow_valid
            for caption in captions:
                if not isinstance(caption, Mapping):
                    caption_valid = False
                    continue
                caption_id = caption.get("id")
                start = caption.get("start_ms")
                end = caption.get("end_ms")
                text = caption.get("text")
                if (
                    not isinstance(caption_id, str) or not caption_id
                    or not isinstance(start, int) or isinstance(start, bool)
                    or not isinstance(end, int) or isinstance(end, bool)
                    or start < 0 or end <= start or end > duration
                    or not isinstance(text, str) or not text.strip() or len(text) > 80
                ):
                    caption_valid = False
                elif sum(
                    scene_start <= start and end <= scene_end
                    for scene_start, scene_end in scene_ranges
                ) != 1:
                    caption_scene_binding_valid = False
                caption_ids.append(caption_id)
            caption_valid = caption_valid and len(caption_ids) == len(set(caption_ids))
            requires_scene_rhythm = duration >= 12000 and len(captions) >= 3
            scene_rhythm_valid = (
                scene_flow_valid
                and (
                    not requires_scene_rhythm
                    or (len(compositions) >= 3 and max_scene_ms <= scene_budget_ms)
                )
            )
            layout_varied = not requires_scene_rhythm or len(set(layouts)) >= 2
            source_video = isinstance(manifest.get("source_video"), Mapping)
            face_visible = (
                not source_video
                or (
                    hidden_speaker_ms <= int(duration * 0.4)
                    and bool(layouts)
                    and layouts[0].startswith("speaker_")
                )
            )
            opening_consistent = scene_rhythm_valid and layout_varied and (
                not source_video or (bool(layouts) and layouts[0].startswith("speaker_"))
            )
            material_identity = (
                material_binding_valid
                and layout_material_compatible
                and not long_material_scene
                and scene_rhythm_valid
            )
            material_identity_reason = (
                "material_layout_requires_bound_asset"
                if not layout_material_compatible
                else "materials_are_bound_to_bounded_requesting_scenes"
            )
            structural = {
                "caption_fact_accuracy": (
                    caption_valid and caption_scene_binding_valid,
                    "authoritative_captions_have_one_complete_scene_binding",
                ),
                "safe_area_and_text_visibility": (
                    caption_valid
                    and caption_scene_binding_valid
                    and scene_rhythm_valid
                    and layout_varied,
                    "captions_are_timed_per_bounded_varied_scene",
                ),
                "face_product_obstruction": (face_visible, "speaker_visibility_budget_valid"),
                "material_semantic_identity": (
                    material_identity,
                    material_identity_reason,
                ),
                "generated_evidence_claim": (
                    all(isinstance(item, Mapping) and item.get("kind") in {"image", "video"} for item in assets),
                    "generated_assets_are_visual_only",
                ),
                "opening_hook_visual_consistency": (
                    opening_consistent,
                    "opening_preserves_subject_and_scene_rhythm",
                ),
            }
            for check_id in blocking:
                if check_id in structural:
                    passed, reason = structural[check_id]
                    results[check_id] = (
                        "pass" if passed else "fail",
                        reason if passed else f"{reason}_failed",
                        not passed and check_id in {
                            "safe_area_and_text_visibility", "face_product_obstruction",
                            "material_semantic_identity", "opening_hook_visual_consistency",
                        },
                    )
                else:
                    results[check_id] = ("pass", "deferred_to_deterministic_media_check", False)

        checks = []
        for check_id, is_blocking in blocking.items():
            result, reason, repairable = results[check_id]
            checks.append({
                "check_id": check_id,
                "result": result,
                "confidence": 1.0,
                "blocking": is_blocking,
                "reason": reason,
                "repairable": repairable,
                "evidence": evidence if result != "unknown" else [],
            })
        return {
            "version": "1.0",
            "schema_sha256": schema_sha256("quality-verdict-v1.schema.json"),
            "model_request_id": "deterministic-structural-inspector-v2",
            "checks": checks,
        }


def _composition_split_boundaries(
    start_ms: int,
    end_ms: int,
    captions: list[Mapping[str, Any]],
    budget_ms: int,
) -> list[int]:
    positions = [start_ms] + sorted({
        int(item["start_ms"])
        for item in captions
        if start_ms < int(item["start_ms"]) < end_ms
    }) + [end_ms]
    boundaries = [start_ms]
    position_index = 0
    last_index = len(positions) - 1
    while position_index < last_index:
        next_index = position_index + 1
        while (
            next_index < last_index
            and positions[next_index + 1] - positions[position_index] <= budget_ms
        ):
            next_index += 1
        if positions[next_index] - positions[position_index] > budget_ms:
            raise ValueError("repair_manifest_caption_partition_invalid")
        position_index = next_index
        boundaries.append(positions[position_index])
    return boundaries


def _repair_render_manifest(
    manifest: Mapping[str, Any],
    repairable_ids: set[str] | frozenset[str],
) -> dict[str, Any]:
    """Apply bounded structural repairs instead of rerendering identical input."""

    repaired = copy.deepcopy(dict(manifest))
    compositions = repaired.get("compositions")
    if not isinstance(compositions, list) or not compositions:
        raise ValueError("repair_manifest_invalid")
    requested = frozenset(repairable_ids)
    supported_ids = {
        "safe_area_and_text_visibility",
        "face_product_obstruction",
        "material_semantic_identity",
        "opening_hook_visual_consistency",
    }
    if requested - supported_ids:
        raise ValueError("repair_manifest_unsupported")
    captions = list(repaired.get("captions") or ())
    scene_budget_ms = _scene_duration_budget(
        captions,
        duration_ms=repaired.get("duration_ms"),
    )
    if requested & (supported_ids - {"face_product_obstruction"}):
        bounded: list[dict[str, Any]] = []
        reserved_ids = {
            str(item.get("id"))
            for item in compositions
            if isinstance(item, Mapping)
            and isinstance(item.get("start_ms"), int)
            and not isinstance(item.get("start_ms"), bool)
            and isinstance(item.get("end_ms"), int)
            and not isinstance(item.get("end_ms"), bool)
            and int(item["end_ms"]) - int(item["start_ms"]) <= scene_budget_ms
        }
        for raw in compositions:
            if not isinstance(raw, Mapping):
                raise ValueError("repair_manifest_invalid")
            composition = copy.deepcopy(dict(raw))
            start = composition.get("start_ms")
            end = composition.get("end_ms")
            if (
                isinstance(start, bool)
                or not isinstance(start, int)
                or isinstance(end, bool)
                or not isinstance(end, int)
                or end <= start
            ):
                raise ValueError("repair_manifest_invalid")
            if end - start <= scene_budget_ms:
                bounded.append(composition)
                continue
            part = 1
            original_id = str(composition.get("id") or "composition")
            boundaries = _composition_split_boundaries(
                start,
                end,
                captions,
                scene_budget_ms,
            )
            for segment_start, segment_end in zip(
                boundaries,
                boundaries[1:],
            ):
                segment = copy.deepcopy(composition)
                identity = hashlib.sha256(
                    f"{original_id}:{part}".encode("utf-8")
                ).hexdigest()[:12]
                suffix = f"_r{part:02d}_{identity}"
                segment["id"] = f"{original_id[:64 - len(suffix)]}{suffix}"
                if segment["id"] in reserved_ids:
                    raise ValueError("repair_manifest_id_collision")
                reserved_ids.add(segment["id"])
                segment["start_ms"] = segment_start
                segment["end_ms"] = segment_end
                bounded.append(segment)
                part += 1
        repaired["compositions"] = bounded

    if (
        "opening_hook_visual_consistency" in requested
        and isinstance(repaired.get("source_video"), Mapping)
        and not str(repaired["compositions"][0].get("layout_id", "")).startswith("speaker_")
    ):
        repaired["compositions"][0]["layout_id"] = "speaker_fullscreen"
        repaired["compositions"][0]["asset_ids"] = []

    if "face_product_obstruction" in requested:
        for composition in repaired["compositions"]:
            if composition.get("layout_id") in {"product_hero", "number_proof"}:
                composition["layout_id"] = "speaker_fullscreen"
                composition["asset_ids"] = []

    if (
        "material_semantic_identity" in requested
        and isinstance(repaired.get("source_video"), Mapping)
    ):
        for composition in repaired["compositions"]:
            if (
                composition.get("layout_id") in _LAYOUTS_REQUIRING_MATERIALS
                and not composition.get("asset_ids")
            ):
                composition["layout_id"] = "speaker_fullscreen"
                composition["asset_ids"] = []

    if repaired == dict(manifest):
        raise ValueError("repair_manifest_unchanged")
    verdict = DeterministicVisualInspector().inspect(
        manifest=repaired,
        render_report={},
    )
    results = {
        item.get("check_id"): item.get("result")
        for item in verdict.get("checks", ())
        if isinstance(item, Mapping)
    }
    required_preflight = requested | {
        "caption_fact_accuracy",
        "safe_area_and_text_visibility",
        "face_product_obstruction",
        "material_semantic_identity",
        "generated_evidence_claim",
    }
    if any(results.get(check_id) != "pass" for check_id in required_preflight):
        raise ValueError("repair_manifest_unresolved")
    return repaired


class ProductionStageCoordinator:
    def __init__(
        self,
        *,
        store: Any,
        cos: Any,
        asr: DashScopeAsr,
        director: QwenCompiledDirector,
        audio_generator: Any,
        image_generator: Any,
        renderer: Any,
        work_root: Path,
        owner_hmac_secret: bytes,
        renderer_root: Path,
        visual_inspector: Any | None = None,
    ) -> None:
        self.store = store
        self.cos = cos
        self.asr = asr
        self.director = director
        self.audio_generator = audio_generator
        self.image_generator = image_generator
        self.renderer = renderer
        self.work_root = Path(work_root).resolve()
        self.owner_hmac_secret = owner_hmac_secret
        self.renderer_root = Path(renderer_root).resolve()
        self.visual_inspector = visual_inspector or DeterministicVisualInspector()

    def probe_capability(self, capability: str, *, environment: str | None):
        return {"available": True, "environment": environment, "reason_code": "capability_ready"}

    def _root(self, job_id: str) -> Path:
        safe = hashlib.sha256(job_id.encode("utf-8")).hexdigest()
        root = self.work_root / safe
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _owner_hmac(self, owner: str) -> str:
        return hmac.new(self.owner_hmac_secret, owner.encode("utf-8"), hashlib.sha256).hexdigest()[:24]

    def _source(self, job: Mapping[str, Any], context: Any) -> tuple[Path, str | None]:
        request = _request(job)
        owner = str(job["owner_id"])
        input_type = request["input_type"]
        root = self._root(str(job["job_id"]))
        if input_type == "platform_talking_head":
            row = ai_edit_v2_platform_assets._owned_row(owner, int(request["source_asset_id"]))
            if row is None or not ai_edit_v2_platform_assets._is_digital_ip_asset(row):
                raise ValueError("platform_source_not_found")
            return ai_edit_v2_platform_assets._source_path(row["video_file"]), ai_edit_v2_platform_assets._authoritative_text(row)
        if input_type in {"uploaded_video", "uploaded_audio"}:
            upload = self.store.get_upload_for_owner(owner, request["source_upload_id"], environment=self.store.environment)
            if upload is None or upload["status"] != "completed":
                raise ValueError("uploaded_source_not_found")
            extension = ".mp4" if input_type == "uploaded_video" else ".audio"
            destination = root / f"uploaded-source{extension}"
            if not destination.exists():
                self.cos.download_file(upload["object_key"], destination)
            return destination, None
        raise ValueError("input_type_not_implemented")

    @staticmethod
    def _capabilities(ratio: str) -> dict[str, Any]:
        layouts = [
            "speaker_fullscreen", "speaker_left_info_right", "speaker_right_evidence_left",
            "editorial_collage", "comparison_split", "steps_stack", "method_timeline",
            "number_proof", "quote_reversal", "cta_offer", "product_hero",
            "material_fullscreen_speaker_pip",
        ]
        return {
            "layout_capabilities": layouts,
            "overlay_capabilities": ["standard_caption", "headline_block", "info_card"],
            "animation_capabilities": ["fade", "slide", "scale", "subtitle_pop"],
            "transition_capabilities": ["hard_cut", "soft_wipe", "directional_slide"],
            "theme_capabilities": {
                "palette_id": ["midnight_gold"],
                "typography_id": ["editorial_sans"],
                "density": ["balanced"],
                "motion_energy": ["medium", "high", "low"],
                "image_fit": ["cover"],
            },
        }

    def _normalized(self, job_id: str) -> tuple[dict[str, Any], Path]:
        root = self._root(job_id)
        return _json(root / "normalized.json"), root

    def _bound_materials(self, job: Mapping[str, Any]) -> list[dict[str, Any]]:
        request = _request(job)
        material_ids = list(request.get("material_asset_ids") or ())
        resolved = self.store.resolve_request_uploads_for_owner(
            str(job["owner_id"]),
            source_upload_id=None,
            material_ids=material_ids,
            environment=self.store.environment,
        )
        if resolved is None:
            raise ValueError("job_materials_not_found")
        materials = list(resolved.get("materials") or ())
        if [item.get("material_id") for item in materials] != material_ids:
            raise ValueError("job_materials_authority_mismatch")
        return materials

    def _render_attempt(self, job: Mapping[str, Any]) -> int:
        return int(job.get("repair_count", 0)) + 1

    def _release_environment(self) -> dict[str, str]:
        lock_path = self.renderer_root / "renderer-release.lock.json"
        lock = _json(lock_path)
        release_sha = _sha(lock_path)
        version = lambda raw: str(raw).split(" version ", 1)[-1].split(" ", 1)[0].lstrip("v")
        return {
            "renderer_build_id": self.renderer.renderer_build_id,
            "code_sha256": hashlib.sha256(str(lock.get("git_commit", "")).encode()).hexdigest(),
            "package_lock_sha256": str(lock["package_lock_sha256"]),
            "release_sha256": release_sha,
            "node_version": version(lock["node"]["version"]),
            "chromium_version": version(lock["chromium"]["version"]),
            "ffmpeg_version": version(lock["ffmpeg"]["version"]),
            "ffprobe_version": version(lock["ffprobe"]["version"]),
            "locale": "C.UTF-8",
            "timezone": "UTC",
        }

    def _stage(self, name: str, job: Mapping[str, Any], context: Any) -> StageOutcome:
        job_id = str(job["job_id"])
        request = _request(job)
        root = self._root(job_id)
        input_sha = str(job["stage_input_sha256"])
        if name == "queued":
            return StageOutcome(
                _NEXT[name],
                {"admitted": True, "pipeline_version": "3.0"},
                input_sha,
            )
        if name == "generating_voice":
            if request["input_type"] == "script_to_audio_video":
                raise ValueError("script_to_audio_not_enabled")
            return StageOutcome(_NEXT[name], {"skipped": True, "reason": "source_has_voice"}, input_sha)
        if name == "normalizing":
            source, authoritative_text = self._source(job, context)
            normalized = normalize_primary_media(source, root / "media", input_type=request["input_type"], deadline_at=context.deadline_at)
            path = root / "media" / normalized.relative_path
            probe = probe_media(path)
            payload = {
                "input_type": request["input_type"], "relative_path": path.relative_to(root).as_posix(),
                "sha256": normalized.sha256, "duration_ms": normalized.duration_ms,
                "ratio": normalized.ratio or request["ratio"], "authoritative_text": authoritative_text,
                "media_type": probe.media_type, "width": probe.width, "height": probe.height,
            }
            digest = _write_json(root / "normalized.json", payload)
            return StageOutcome(_NEXT[name], {"normalized_sha256": digest, "duration_ms": normalized.duration_ms, "ratio": payload["ratio"]}, input_sha)
        if name == "transcribing":
            normalized, _ = self._normalized(job_id)
            media_path = root / normalized["relative_path"]
            owner_hmac = self._owner_hmac(str(job["owner_id"]))
            suffix = ".mp4" if normalized["media_type"] == "video" else ".flac"
            object_key = f"{self.store.environment}/ai-edit-v3/{owner_hmac}/{job_id}/working/source{suffix}"
            self.cos.put_file(media_path, object_key, "video/mp4" if suffix == ".mp4" else "audio/flac", private=True, if_absent=True)
            result = self.asr.transcribe(self.cos.presign_get(object_key, expires=300), job_id, deadline_at=context.deadline_at)
            payload = _provider_payload_json(result.payload)
            digest = _write_json(root / "asr.json", payload)
            return StageOutcome(_NEXT[name], {"asr_sha256": digest, "provider_task_id": payload.get("provider_task_id")}, input_sha, result)
        if name == "aligning":
            normalized, _ = self._normalized(job_id)
            asr = normalize_asr_result(_json(root / "asr.json"))
            media = SimpleNamespace(duration_ms=normalized["duration_ms"], sha256=normalized["sha256"])
            source = PreparedSource(
                input_type=normalized["input_type"], authoritative_text=normalized.get("authoritative_text"),
                media=media, source_asset_id=request.get("source_asset_id"),
                source_upload_id=request.get("source_upload_id"), provider_request_id=None,
                source_fingerprint=hashlib.sha256(canonical_json({"job_id": job_id, "media_sha256": normalized["sha256"]})).hexdigest(),
            )
            timeline = build_text_timeline(source, asr)
            digest = _write_json(root / "timeline.json", _timeline_to_json(timeline))
            return StageOutcome(_NEXT[name], {"timeline_sha256": digest, "caption_count": len(timeline.captions), "alignment_coverage": timeline.alignment_coverage}, input_sha)
        if name == "planning":
            normalized, _ = self._normalized(job_id)
            timeline = _timeline_from_json(_json(root / "timeline.json"))
            source = SimpleNamespace(
                input_type=normalized["input_type"], source_fingerprint=normalized["sha256"]
            )
            capabilities = self._capabilities(str(normalized["ratio"]))
            descriptors = []
            for material in self._bound_materials(job):
                metadata = json.loads(material.get("metadata_json") or "{}")
                descriptors.append(SimpleNamespace(
                    material_id=material["material_id"],
                    semantic=str(metadata.get("semantic") or "用户上传的产品、门店或招商素材"),
                    subject_type=str(metadata.get("subject_type") or "user_provided"),
                    composition=str(metadata.get("composition") or "original"),
                    supported_ratios=("16:9", "9:16", "1:1"),
                    risk_labels=(),
                    sha256=material["sha256"],
                ))
            director_request = build_director_request(source, timeline, (), descriptors, capabilities)
            director_request["ratio"] = normalized["ratio"]
            director_request["user_direction"] = request.get("style_prompt", request.get("creation_mode", "ai_auto"))
            director_request["generate_missing_material"] = not descriptors
            generated = generate_edit_plan(
                SimpleNamespace(request=director_request, timeline=timeline, capabilities=capabilities, job_id=job_id, deadline_at=context.deadline_at),
                self.director,
            )
            digest = _write_json(root / "plan.json", generated.value)
            save = getattr(self.store, "save_director_plan", None)
            if callable(save):
                save(context.claim, context.stage_attempt_id, generated, now_ms=int(time.time() * 1000))
            return StageOutcome(_NEXT[name], {"plan_sha256": digest, "provider_request_id": generated.provider_request_id}, input_sha)
        if name == "resolving_materials":
            frozen = []
            material_root = root / "materials"
            material_root.mkdir(parents=True, exist_ok=True)
            suffixes = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
            for index, material in enumerate(self._bound_materials(job), 1):
                mime = str(material["mime_type"])
                suffix = suffixes.get(mime)
                if suffix is None:
                    raise ValueError("job_material_type_invalid")
                destination = material_root / f"material-{index:02d}{suffix}"
                if not destination.exists():
                    self.cos.download_file(material["cos_key"], destination)
                digest = _sha(destination)
                if destination.stat().st_size != int(material["size_bytes"]) or digest != material["sha256"]:
                    raise ValueError("job_material_content_mismatch")
                frozen.append({
                    "material_id": material["material_id"],
                    "relative_path": destination.relative_to(root).as_posix(),
                    "mime_type": mime,
                    "size_bytes": destination.stat().st_size,
                    "sha256": digest,
                })
            digest = _write_json(root / "materials.json", {"items": frozen})
            return StageOutcome(
                _NEXT[name],
                {"materials_sha256": digest, "material_count": len(frozen)},
                input_sha,
            )
        if name == "generating_images":
            material_document = _json(root / "materials.json")
            items = list(material_document["items"])
            plan = _json(root / "plan.json")
            required = list(plan.get("materials") or ())
            provider_result = None
            for index, material_request in enumerate(required[len(items):], len(items) + 1):
                destination = root / "materials" / f"generated-{index:02d}.png"
                if not destination.exists():
                    provider_result = self.image_generator.generate(
                        prompt=(
                            f"为中文短视频生成一张无文字、无水印、无品牌标识的通用配图。"
                            f"主题：{material_request['semantic']}。"
                            "不得虚构客户、销量、价格、功效或产品包装。"
                            " Supplemental B-roll or graphic only. No presenter, no talking head, "
                            "no portrait, no recognizable person or face. No visible text, logo, or watermark."
                        ),
                        ratio=plan["ratio"],
                        output_path=destination,
                        idempotency_key=f"ai-edit-v3:{job_id}:image:{material_request['request_id']}",
                        deadline_at=context.deadline_at,
                    )
                remaining = context.deadline_at - time.time()
                if remaining <= 0:
                    raise TimeoutError("image_probe_deadline_exceeded")
                image = _probe_image(destination, timeout_seconds=min(30.0, remaining))
                digest = _sha(destination)
                object_key = (
                    f"{self.store.environment}/ai-edit-v3/{self._owner_hmac(str(job['owner_id']))}/"
                    f"{job_id}/materials/generated-{index:02d}.png"
                )
                self.cos.put_file(destination, object_key, "image/png", private=True, if_absent=True)
                items.append({
                    "material_id": f"generated_{index:02d}",
                    "relative_path": destination.relative_to(root).as_posix(),
                    "mime_type": "image/png",
                    "size_bytes": destination.stat().st_size,
                    "sha256": digest,
                    "width": image.width,
                    "height": image.height,
                    "source": "generated",
                    "object_key": object_key,
                })
            digest = _write_json(root / "materials.json", {"items": items})
            material_count = len(items)
            return StageOutcome(
                _NEXT[name],
                {
                    "skipped": len(required) <= len(material_document["items"]),
                    "reason": "all_director_slots_resolved_from_user_materials"
                    if required and len(required) <= len(material_document["items"])
                    else "missing_director_slots_generated" if required else "no_required_material_slots",
                    "material_count": material_count,
                    "materials_sha256": digest,
                },
                input_sha,
                provider_result,
            )
        if name == "generating_audio":
            plan = _json(root / "plan.json")
            timeline = _timeline_with_full_source_map(
                _timeline_from_json(_json(root / "timeline.json"))
            )
            audio_plan = compile_audio_plan(plan, timeline)
            generated = generate_task_audio(job_id, audio_plan, self.audio_generator, self.cos, root, context)
            values = [
                {
                    "cue_id": item.cue_id, "kind": item.kind, "relative_path": item.relative_path,
                    "object_key": item.object_key, "sha256": item.sha256, "duration_ms": item.duration_ms,
                    "sample_rate": item.sample_rate, "channels": item.channels,
                    "provider_request_id": item.provider_request_id, "usage": dict(item.usage),
                }
                for item in generated
            ]
            digest = _write_json(root / "generated-audio.json", {"items": values})
            return StageOutcome(_NEXT[name], {"audio_assets_sha256": digest, "audio_asset_count": len(values)}, input_sha)
        if name == "mixing_audio":
            normalized, _ = self._normalized(job_id)
            plan = _json(root / "plan.json")
            timeline = _timeline_with_full_source_map(
                _timeline_from_json(_json(root / "timeline.json"))
            )
            audio_plan = compile_audio_plan(plan, timeline)
            # The stable first release preserves the full source; align the master to it.
            mapped = (SourceSegment("segment_01", 0, int(plan["duration_ms"]), False, "full source", 0, int(plan["duration_ms"])),)
            generated = tuple(GeneratedAudioAsset(**item) for item in _json(root / "generated-audio.json")["items"])
            master_path = root / "master.wav"
            master_path.unlink(missing_ok=True)
            master = build_master_audio(root / normalized["relative_path"], mapped, audio_plan, generated, master_path, deadline_at=context.deadline_at)
            payload = {
                "relative_path": master_path.relative_to(root).as_posix(), "sha256": master.sha256,
                "duration_ms": master.duration_ms, "sample_rate": master.sample_rate,
                "channels": master.channels, "integrated_lufs": master.integrated_lufs,
                "true_peak_dbtp": master.true_peak_dbtp,
            }
            digest = _write_json(root / "master.json", payload)
            return StageOutcome(_NEXT[name], {"master_sha256": digest, "duration_ms": master.duration_ms}, input_sha)
        if name == "repair_planning":
            return StageOutcome(_NEXT[name], {"repair": "recompile_same_bounded_manifest"}, input_sha)
        if name == "compiling":
            normalized, _ = self._normalized(job_id)
            plan = _json(root / "plan.json")
            master = _json(root / "master.json")
            attempt = self._render_attempt(job)
            input_root = root / f"render-{attempt}" / "input"
            if input_root.exists():
                shutil.rmtree(input_root)
            (input_root / "media").mkdir(parents=True)
            master_source = root / master["relative_path"]
            master_target = input_root / "media" / "master.wav"
            shutil.copyfile(master_source, master_target)
            source_video = None
            segment_path = "media/master.wav"
            segment_sha = _sha(master_target)
            if normalized["media_type"] == "video":
                source_target = input_root / "media" / "source.mp4"
                command = ["ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", os.fspath(root / normalized["relative_path"]), "-map", "0:v:0", "-an", "-c:v", "copy", "-movflags", "+faststart", os.fspath(source_target)]
                completed = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=max(1, min(600, context.deadline_at - time.time())))
                if completed.returncode != 0:
                    raise ValueError("silent_source_failed")
                segment_path = "media/source.mp4"
                segment_sha = _sha(source_target)
                source_video = {
                    "path": segment_path, "sha256": segment_sha, "size_bytes": source_target.stat().st_size,
                    "silent": True, "duration_ms": normalized["duration_ms"],
                    "width": int(normalized["width"]), "height": int(normalized["height"]),
                }
            material_assets = []
            material_asset_ids = []
            material_items = list(_json(root / "materials.json")["items"])
            maximum_assets = min(4, len(plan.get("materials") or ()), len(material_items))
            for index, material in enumerate(material_items[:maximum_assets], 1):
                source = root / material["relative_path"]
                target = input_root / "media" / f"material-{index:02d}{source.suffix.lower()}"
                shutil.copyfile(source, target)
                asset_id = f"material_{index:02d}"
                material_asset_ids.append(asset_id)
                material_assets.append({
                    "id": asset_id,
                    "kind": "image",
                    "path": target.relative_to(input_root).as_posix(),
                    "sha256": _sha(target),
                    "size_bytes": target.stat().st_size,
                })
            ratio = plan["ratio"]
            width, height = ((1920, 1080) if ratio == "16:9" else (1080, 1920))
            manifest = {
                "version": "1.0", "schema_sha256": schema_sha256("render-manifest-v1.schema.json"),
                "renderer_environment": self._release_environment(),
                "output_spec": {"ratio": ratio, "width": width, "height": height, "fps_num": 30, "fps_den": 1, "video_codec": "h264", "pixel_format": "yuv420p", "audio_codec": "aac", "sample_rate": 48000, "channels": 2},
                "duration_ms": plan["duration_ms"], "edit_plan_sha256": hashlib.sha256(canonical_json(plan)).hexdigest(),
                "registry_sha256": self.renderer.registry_sha256.removeprefix("sha256:"),
                "theme": plan["theme"], "seed": int(hashlib.sha256(job_id.encode()).hexdigest()[:8], 16) % 2147483648,
                "source_video": source_video,
                "source_segments": [{"id": item["id"], "source_path": segment_path, "sha256": segment_sha, "source_start_ms": item["source_start_ms"], "source_end_ms": item["source_end_ms"], "output_start_ms": item["output_start_ms"], "output_end_ms": item["output_end_ms"]} for item in plan["source_segments"]],
                "master_audio": {"path": "media/master.wav", "sha256": _sha(master_target), "size_bytes": master_target.stat().st_size, "duration_ms": master["duration_ms"], "sample_rate": 48000, "channels": 2},
                "assets": material_assets,
                "compositions": [{"id": f"composition_{index:03d}", "scene_id": scene["id"], "start_ms": scene["start_ms"], "end_ms": scene["end_ms"], "layout_id": scene["layout_id"], "layout_variant": scene["layout_variant"], "overlay_ids": scene["overlay_ids"], "animations": scene["animations"], "transition": scene["transition"], "asset_ids": _scene_asset_ids(scene, material_asset_ids)} for index, scene in enumerate(plan["scenes"], 1)],
                "captions": _render_captions(plan["captions"]),
            }
            if attempt > 1:
                previous_quality = _json(root / f"quality-{attempt - 1}.json")
                repairable_ids = previous_quality.get("repairable_ids")
                if not isinstance(repairable_ids, list) or not all(
                    isinstance(item, str) for item in repairable_ids
                ):
                    raise ValueError("repair_quality_invalid")
                manifest = _repair_render_manifest(
                    manifest,
                    set(repairable_ids),
                )
            frozen = freeze_render_manifest(manifest, input_root / "render-manifest.json", sandbox_root=input_root)
            payload = {"attempt": attempt, "input_root": input_root.relative_to(root).as_posix(), "manifest_sha256": frozen.sha256}
            digest = _write_json(root / f"compile-{attempt}.json", payload)
            return StageOutcome(_NEXT[name], {"compile_sha256": digest, **payload}, input_sha)
        if name == "rendering":
            attempt = self._render_attempt(job)
            compiled = _json(root / f"compile-{attempt}.json")
            input_root = root / compiled["input_root"]
            output_root = root / f"render-{attempt}" / "output"
            if output_root.exists():
                shutil.rmtree(output_root)
            output_root.mkdir(parents=True)
            instance = f"r{hashlib.sha256(f'{job_id}:{attempt}'.encode()).hexdigest()[:40]}"
            context.assert_active()
            result = self.renderer.render(RenderRequest(instance, job_id, attempt, input_root / "render-manifest.json", input_root, output_root, compiled["manifest_sha256"], self.renderer.renderer_build_id, context.deadline_at))
            context.assert_active()
            payload = {"attempt": attempt, "output_root": output_root.relative_to(root).as_posix(), "silent_video_relpath": result.silent_video_relpath, "sha256": result.sha256, "report_relpath": result.report_relpath, "snapshots": list(result.snapshots), "performance": dict(result.performance)}
            digest = _write_json(root / f"render-{attempt}.json", payload)
            return StageOutcome(_NEXT[name], {"render_sha256": digest, "attempt": attempt}, input_sha)
        if name == "quality_checking":
            attempt = self._render_attempt(job)
            render = _json(root / f"render-{attempt}.json")
            output_root = root / render["output_root"]
            master = _json(root / "master.json")
            final_path = root / f"final-{attempt}.mp4"
            final_path.unlink(missing_ok=True)
            mux = mux_master_audio(output_root / render["silent_video_relpath"], root / master["relative_path"], final_path, duration_ms=int(master["duration_ms"]), deadline_at=context.deadline_at)
            manifest = _json(root / f"render-{attempt}" / "input" / "render-manifest.json")
            report = _json(output_root / render["report_relpath"])
            owner_evidence = {
                "owner": job["owner_id"],
                "job_id": job_id,
                "asset_hashes": _material_asset_hashes(
                    manifest,
                    _json(root / "materials.json"),
                ),
            }
            quality = run_blocking_quality(mux, manifest, report, owner_evidence=owner_evidence, visual_inspector=self.visual_inspector, deadline_at=context.deadline_at)
            payload = {"passed": quality.passed, "repairable_ids": list(quality.repairable_ids), "report_sha256": quality.report_sha256, "final_relpath": final_path.relative_to(root).as_posix(), "final": {"relative_path": mux.relative_path, "sha256": mux.sha256, "duration_ms": mux.duration_ms, "video_codec": mux.video_codec, "audio_codec": mux.audio_codec, "width": mux.width, "height": mux.height, "fps_num": mux.fps_num, "fps_den": mux.fps_den, "sample_rate": mux.sample_rate, "channels": mux.channels, "audit": dict(mux.audit)}}
            digest = _write_json(root / f"quality-{attempt}.json", payload)
            if quality.passed:
                next_state = "staging_delivery"
            elif int(job.get("repair_count", 0)) == 0 and quality.repairable_ids:
                next_state = "repair_planning"
            else:
                next_state = "failed"
            return StageOutcome(next_state, {"quality_sha256": digest, "passed": quality.passed, "repairable_ids": list(quality.repairable_ids)}, input_sha)
        if name == "staging_delivery":
            attempt = self._render_attempt(job)
            quality = _json(root / f"quality-{attempt}.json")
            final_data = quality["final"]
            mux = FinalMux(**final_data)
            staged = stage_private_delivery(str(job["owner_id"]), self._owner_hmac(str(job["owner_id"])), job_id, attempt, mux, source_path=root / quality["final_relpath"], environment=self.store.environment, cos=self.cos)
            return StageOutcome(_NEXT[name], {"delivery_object_key": staged.object_key, "metadata_sha256": quality["report_sha256"], "actual_charge": int(job["confirmed_preheld_total"]), "content_sha256": staged.sha256, "size_bytes": staged.size_bytes}, input_sha)
        raise ValueError("stage_not_implemented")

    def run_stage(self, name: str, job: Mapping[str, Any], context: Any) -> StageOutcome:
        return self._stage(name, job, context)


class CapabilityPlaceholder:
    def __init__(self, name: str, *, available: bool = True) -> None:
        self.name = name
        self.available = available

    def probe_capability(self, capability: str, *, environment: str | None):
        available = self.available and capability == self.name
        return {"available": available, "environment": environment, "reason_code": "capability_ready" if available else "capability_unavailable"}


__all__ = (
    "CapabilityPlaceholder",
    "DashScopeAsr",
    "DeterministicVisualInspector",
    "ProductionStageCoordinator",
    "QwenCompiledDirector",
)
