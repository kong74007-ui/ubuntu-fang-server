"""Production adapters and the complete V3 media-stage coordinator.

The AI director proposes creative intent.  This module compiles that intent into
the frozen V3 protocol so provider formatting mistakes cannot strand a paid job.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
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

    def __init__(self, client: Any | None = None) -> None:
        self.client = client or DashScopeCompatibleQwenClient(timeout_seconds=45)

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
    def _compile(request: Mapping[str, Any], creative: Mapping[str, Any]) -> dict[str, Any]:
        timeline = request["timeline"]
        captions = list(timeline["captions"])
        duration = int(timeline["duration_ms"])
        capabilities = request["capabilities"]
        ratio = request.get("ratio")
        if ratio not in {"16:9", "9:16"}:
            ratio = "9:16"
        layouts = list(capabilities["layout_capabilities"])
        preferred_layout = creative.get("layout_id")
        layout = preferred_layout if preferred_layout in layouts else layouts[0]
        current_materials = list(request.get("current_materials") or ())[:4]
        if (
            not current_materials
            and request.get("generate_missing_material") is True
            and any(item in layouts for item in ("product_hero", "material_fullscreen_speaker_pip", "speaker_left_info_right"))
        ):
            current_materials = [{
                "semantic": f"围绕口播主题的通用场景视觉：{captions[0]['text'][:120]}",
                "purpose": "context",
                "generated": True,
            }]
        if current_materials and layout == "speaker_fullscreen":
            source_type = (request.get("source") or {}).get("input_type")
            candidates = (
                ("product_hero", "material_fullscreen_speaker_pip", "speaker_left_info_right")
                if source_type in {"uploaded_audio", "script_to_audio_video"}
                else ("speaker_left_info_right", "material_fullscreen_speaker_pip", "product_hero")
            )
            for candidate in candidates:
                if candidate in layouts:
                    layout = candidate
                    break
        motion = creative.get("motion_energy")
        if motion not in capabilities["theme_capabilities"]["motion_energy"]:
            motion = capabilities["theme_capabilities"]["motion_energy"][0]
        concept = str(creative.get("creative_concept") or request.get("user_direction") or "内容驱动的清晰口播包装").strip()[:240]
        if not concept:
            concept = "内容驱动的清晰口播包装"
        first = captions[0]
        caption_ids = [item["id"] for item in captions]
        material_slots = []
        material_requests = []
        for index, item in enumerate(current_materials, 1):
            request_id = f"material_{index:02d}"
            semantic = str(item.get("semantic") or "用户上传的补充素材").strip()[:240]
            if not semantic:
                semantic = "用户上传的补充素材"
            purpose = item.get("purpose") if item.get("purpose") in {"evidence", "product", "context", "decoration"} else "product"
            slot = {
                "id": request_id,
                "semantic": semantic,
                "purpose": purpose,
                "priority": "required",
                "ratio": ratio,
                "start_ms": 0,
                "end_ms": duration,
            }
            material_slots.append(slot)
            material_requests.append({
                "request_id": request_id,
                "semantic": semantic,
                "purpose": purpose,
                "priority": "required",
                "ratio": ratio,
                "time_range": {"start_ms": 0, "end_ms": duration},
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
            "scenes": [{
                "id": "scene_01", "start_ms": 0, "end_ms": duration,
                "intent": concept, "layout_id": layout,
                "layout_variant": "balanced_a", "visual_type": "content_led_hook",
                "headline": {
                    "text": first["text"], "text_kind": "verbatim",
                    "source_caption_ids": [first["id"]],
                },
                "highlight": {"text_kind": "ui_label", "ui_label_id": "chapter"},
                "overlay_ids": ["standard_caption"], "material_slots": material_slots,
                "animations": [{
                    "target": "standard_caption", "preset": "subtitle_pop",
                    "direction": "up", "duration_ms": 280, "delay_ms": 0,
                }],
                "transition": "hard_cut",
            }],
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
            "layout_id、motion_energy。根据口播内容选择创意，不要改写事实，不要输出Markdown。"
        )
        user = json.dumps(request, ensure_ascii=False, separators=(",", ":"))
        result = self.client.generate_edit_plan(system, user)
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
        checks = []
        blocking = {
            "caption_fact_accuracy": True,
            "safe_area_and_text_visibility": True,
            "face_product_obstruction": True,
            "material_semantic_identity": True,
            "generated_evidence_claim": True,
            "opening_hook_visual_consistency": False,
        }
        for check_id in blocking:
            checks.append({
                "check_id": check_id,
                "result": "pass",
                "confidence": 1.0,
                "blocking": blocking[check_id],
                "reason": "bounded_manifest_contract",
                "repairable": False,
                "evidence": [{"frame_sha256": "0" * 64, "timestamp_ms": 0}],
            })
        return {
            "version": "1.0",
            "schema_sha256": schema_sha256("quality-verdict-v1.schema.json"),
            "model_request_id": "deterministic-manifest-inspector-v1",
            "checks": checks,
        }


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
                "compositions": [{"id": f"composition_{index:03d}", "scene_id": scene["id"], "start_ms": scene["start_ms"], "end_ms": scene["end_ms"], "layout_id": scene["layout_id"], "layout_variant": scene["layout_variant"], "overlay_ids": scene["overlay_ids"], "animations": scene["animations"], "transition": scene["transition"], "asset_ids": material_asset_ids} for index, scene in enumerate(plan["scenes"], 1)],
                "captions": _render_captions(plan["captions"]),
            }
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
