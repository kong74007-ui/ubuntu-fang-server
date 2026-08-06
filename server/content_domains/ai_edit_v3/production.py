"""Production adapters and the complete V3 media-stage coordinator.

The AI director proposes creative intent.  This module compiles that intent into
the frozen V3 protocol so provider formatting mistakes cannot strand a paid job.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
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
from .capability_catalog import load_visual_capability_catalog
from .contracts import (
    canonical_json,
    freeze_render_manifest,
    load_frozen_schema,
    schema_sha256,
)
from .delivery import stage_private_delivery
from .director import ValidatedPlan, build_director_request, generate_edit_plan
from .director_candidates import _build_caption_groups, _scene_duration_budget, build_scene_candidates
from .director_compiler import compile_edit_plan
from .director_decision import generate_director_decision
from .director_layout_policy import (
    MAX_REQUIRED_MATERIAL_SLOTS,
    MAX_TOTAL_MATERIAL_SLOTS,
    SCENE_STRUCTURE_POLICY,
    SPEAKER_VISIBILITY_POLICY,
    allowed_layout_ids,
    layout_shows_speaker,
    layout_requirements_for,
    required_material_layout_ids,
)
from .media import FinalMux, _probe_image, mux_master_audio, normalize_primary_media, probe_media
from .materials import MaterialError, bind_scene_materials, validate_generated_material_review
from .overlay_catalog import load_overlay_placement_catalog, overlay_budget_index
from .providers.asr import normalize_asr_result
from .providers.base import DefinitiveNotAccepted, ProviderResult
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

_MATERIAL_REVIEW_POLICY_VERSION = "material-review-policy-v2"


def _material_review_receipt_request(
    *,
    scene_id: str,
    slot_id: str,
    semantic: str,
    forbidden_subjects: tuple[str, ...] | list[str],
    cos_key: str,
    source_metadata: Mapping[str, Any],
) -> dict[str, Any]:
    """Version the reviewer policy inside the provider receipt identity."""

    return {
        "review_policy_version": _MATERIAL_REVIEW_POLICY_VERSION,
        "scene_id": scene_id,
        "slot_id": slot_id,
        "semantic": semantic,
        "forbidden_subjects": list(forbidden_subjects),
        "cos_key": cos_key,
        "source_metadata": dict(source_metadata),
    }


class MaterialDescriptorContractError(MaterialError):
    """A provider response arrived but failed the local descriptor contract."""


def visual_program_capabilities(capabilities: Mapping[str, Any]) -> dict[str, Any]:
    """Admit visual-v1 only after a versioned registry supplies real variants."""
    variants = capabilities.get("layout_variants") if isinstance(capabilities, Mapping) else None
    layouts = capabilities.get("layout_capabilities") if isinstance(capabilities, Mapping) else None
    if not isinstance(layouts, list) or not isinstance(variants, Mapping) or any(
        not isinstance(variants.get(layout), (list, tuple)) or not variants[layout]
        or any(not isinstance(item, str) or item == "balanced_a" for item in variants[layout])
        for layout in layouts
    ):
        raise ValueError("visual_program_capabilities_incomplete")
    required = ("overlay_variants", "overlay_animation_targets", "layout_animation_targets", "theme_profile_ids", "identity_match_capability", "overlay_placement_budgets", "output_ratio")
    if any(name not in capabilities for name in required):
        raise ValueError("visual_program_capabilities_incomplete")
    if capabilities.get("output_ratio") not in {"16:9", "9:16"}:
        raise ValueError("visual_program_capabilities_incomplete")
    if (
        capabilities.get("identity_match_capability") is not False
        or "card_match_cut" in capabilities.get("transition_capabilities", ())
    ):
        raise ValueError("visual_program_capabilities_incomplete")
    for name, expected in (
        ("layout_variants", set(layouts)),
        ("layout_animation_targets", set(layouts)),
        ("overlay_variants", set(capabilities.get("overlay_capabilities", ()))),
        ("overlay_animation_targets", set(capabilities.get("overlay_capabilities", ()))),
    ):
        catalog = capabilities.get(name)
        if not isinstance(catalog, Mapping) or set(catalog) != expected:
            raise ValueError("visual_program_capabilities_incomplete")
    if any(
        any(capabilities[name].values())
        for name in (
            "layout_animation_targets", "overlay_variants",
            "overlay_animation_targets",
        )
    ):
        raise ValueError("visual_program_capabilities_incomplete")
    try:
        budget_components = {identity[0] for identity in overlay_budget_index(capabilities)}
    except ValueError as exc:
        raise ValueError("visual_program_capabilities_incomplete") from exc
    if set(capabilities.get("overlay_capabilities", ())) != budget_components:
        raise ValueError("visual_program_capabilities_incomplete")
    return copy.deepcopy(dict(capabilities))


def director_prompt_capabilities(capabilities: Mapping[str, Any]) -> dict[str, Any]:
    """Project renderer capabilities into the compact choices Qwen can use."""

    ratio = capabilities.get("output_ratio")
    budget = capabilities.get("overlay_placement_budgets")
    entries = budget.get("entries") if isinstance(budget, Mapping) else None
    overlays = capabilities.get("overlay_capabilities")
    if ratio not in {"16:9", "9:16"} or not isinstance(entries, list) or not isinstance(overlays, list):
        raise ValueError("visual_program_capabilities_incomplete")
    placements: dict[str, list[dict[str, Any]]] = {
        str(component_id): [] for component_id in overlays
    }
    for entry in entries:
        if not isinstance(entry, Mapping) or entry.get("ratio") != ratio:
            continue
        component_id = entry.get("component_id")
        placement = entry.get("placement")
        max_chars = entry.get("max_chars")
        max_lines = entry.get("max_lines")
        if component_id not in placements:
            continue
        if (
            not isinstance(placement, str)
            or type(max_chars) is not int
            or max_chars < 1
            or type(max_lines) is not int
            or max_lines < 1
            or any(item["placement"] == placement for item in placements[component_id])
        ):
            raise ValueError("visual_program_capabilities_incomplete")
        placements[component_id].append({
            "placement": placement,
            "max_chars": max_chars,
            "max_lines": max_lines,
        })
    if any(not values for values in placements.values()):
        raise ValueError("visual_program_capabilities_incomplete")
    fields = (
        "version",
        "layout_capabilities",
        "layout_variants",
        "overlay_capabilities",
        "overlay_variants",
        "overlay_animation_targets",
        "layout_animation_targets",
        "animation_capabilities",
        "transition_capabilities",
        "theme_profile_ids",
        "identity_match_capability",
        "output_ratio",
        "layout_requirements",
        "material_binding_mode",
        "max_required_material_slots",
        "max_total_material_slots",
        "speaker_visibility_policy",
        "scene_structure_policy",
    )
    projected = {
        name: copy.deepcopy(capabilities[name])
        for name in fields
        if name in capabilities
    }
    projected["overlay_placements"] = placements
    return projected


_VARIATION_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_VARIATION_SEED = re.compile(r"^[0-9a-f]{16}$")
_THEME_PROFILES = {
    "editorial_clean": {"bg": "#f7f4ed", "surface": "#ffffff", "text": "#17212b", "accent": "#315b8a", "border": "rgba(49,91,138,.28)", "shadow": "0 18px 48px rgba(23,33,43,.14)", "texture": "none"},
    "commercial_energy": {"bg": "#10122a", "surface": "#1e2360", "text": "#ffffff", "accent": "#ff6b35", "border": "rgba(255,107,53,.52)", "shadow": "0 22px 64px rgba(255,107,53,.22)", "texture": "grain_subtle"},
    "premium_dark": {"bg": "#07111f", "surface": "#101d2f", "text": "#f7f5ef", "accent": "#d9a441", "border": "rgba(217,164,65,.42)", "shadow": "0 24px 80px rgba(0,0,0,.30)", "texture": "none"},
    "warm_lifestyle": {"bg": "#fff5e8", "surface": "#fffdfa", "text": "#49352c", "accent": "#b9603d", "border": "rgba(185,96,61,.30)", "shadow": "0 16px 44px rgba(106,65,45,.16)", "texture": "paper_subtle"},
}


def derive_variation_seed(
    request_sha256: str, director_decision_sha256: str, registry_sha256: str
) -> str:
    """Keep the visual choice seed string-safe and bound to frozen inputs."""
    values = (request_sha256, director_decision_sha256, registry_sha256)
    if any(not isinstance(value, str) or _VARIATION_SHA256.fullmatch(value) is None for value in values):
        raise ValueError("variation_seed_source_invalid")
    return hashlib.sha256("".join(values).encode("ascii")).hexdigest()[:16]


def _resolve_design_tokens(
    theme_profile_id: str, design_intent: Mapping[str, Any], variation_seed: str
) -> dict[str, str]:
    profile = _THEME_PROFILES.get(theme_profile_id)
    if profile is None or _VARIATION_SEED.fullmatch(variation_seed) is None:
        raise ValueError("visual_design_tokens_invalid")
    density = {"minimal": "airy", "balanced": "balanced", "dense": "dense"}.get(design_intent.get("density"))
    motion_distance = {"low": "18px", "medium": "36px", "high": "54px"}.get(design_intent.get("motion_energy"))
    image_fit = {"contain": "contain", "cover": "cover", "smart_crop": "cover"}.get(design_intent.get("image_fit"))
    if density is None or motion_distance is None or image_fit is None or design_intent.get("decoration_intensity") not in {"low", "medium", "high"}:
        raise ValueError("visual_design_tokens_invalid")
    left, right = int(variation_seed[:8], 16), int(variation_seed[8:], 16)
    if left | right == 0:
        right = 0x9E3779B9

    def next_uint32() -> int:
        nonlocal left, right
        left ^= (left << 13) & 0xFFFFFFFF; left &= 0xFFFFFFFF
        left ^= left >> 17; left &= 0xFFFFFFFF
        left ^= (left << 5) & 0xFFFFFFFF; left &= 0xFFFFFFFF
        right = (right + 0x9E3779B9) & 0xFFFFFFFF
        return (left ^ right) & 0xFFFFFFFF

    type_scale = ("0.960", "1.000", "1.040")[next_uint32() % 3]
    gap = {"airy": ("34px", "38px"), "balanced": ("24px", "28px"), "dense": ("16px", "20px")}[density][next_uint32() % 2]
    radius = ("18px", "22px", "26px")[next_uint32() % 3]
    return {
        "--hf-theme-profile": theme_profile_id, "--hf-bg": profile["bg"], "--hf-surface": profile["surface"],
        "--hf-text": profile["text"], "--hf-accent": profile["accent"], "--hf-font": '"Noto Sans SC", sans-serif',
        "--hf-type-scale": type_scale, "--hf-gap": gap, "--hf-radius": radius, "--hf-border": profile["border"],
        "--hf-shadow": profile["shadow"], "--hf-texture": profile["texture"], "--hf-density": density,
        "--hf-motion-distance": motion_distance, "--hf-image-fit": image_fit,
    }

_LAYOUTS_REQUIRING_MATERIALS = required_material_layout_ids()


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


def _record_material_rejection(
    root: Path,
    *,
    material_request: Mapping[str, Any],
    cos_key: str,
    source_metadata: Mapping[str, Any],
    review: Mapping[str, Any],
    cos: Any,
) -> None:
    path = root / "material-rejections.json"
    document = _json(path) if path.exists() else {"items": []}
    items = document.get("items")
    if not isinstance(items, list):
        raise MaterialError("material_rejection_audit_invalid")
    audit = {
        "scene_id": material_request["scene_id"],
        "slot_id": material_request["slot_id"],
        "request_id": material_request["request_id"],
        "semantic": material_request["semantic"],
        "reason": review["reason"],
        "evidence": review["evidence"],
        "cos_key": cos_key,
        "source_metadata": dict(source_metadata),
        "cleanup_status": "pending",
        "cleanup_required": True,
        "cleanup_attempt": {
            "attempt_count": 1,
            "last_error_code": None,
        },
    }
    items.append(audit)
    _write_json(path, document)
    try:
        delete = getattr(cos, "delete_object")
        delete(cos_key)
        audit["cleanup_status"] = "deleted"
        audit["cleanup_required"] = False
    except Exception:
        audit["cleanup_status"] = "cleanup_failed"
        audit["cleanup_attempt"]["last_error_code"] = "cos_delete_failed"
    _write_json(path, document)


def scan_material_cleanup_retries(root: Path) -> list[dict[str, Any]]:
    """Return a deterministic, secret-free list of task-private COS cleanup work."""

    path = root / "material-rejections.json"
    if not path.exists():
        return []
    document = _json(path)
    items = document.get("items")
    if not isinstance(items, list):
        raise MaterialError("material_rejection_audit_invalid")
    retries: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, Mapping):
            raise MaterialError("material_rejection_audit_invalid")
        if item.get("cleanup_status") != "cleanup_failed" or item.get("cleanup_required") is not True:
            continue
        attempt = item.get("cleanup_attempt")
        values = {
            "scene_id": item.get("scene_id"),
            "slot_id": item.get("slot_id"),
            "request_id": item.get("request_id"),
            "cos_key": item.get("cos_key"),
        }
        if (
            not isinstance(attempt, Mapping)
            or not isinstance(attempt.get("attempt_count"), int)
            or isinstance(attempt.get("attempt_count"), bool)
            or attempt.get("attempt_count") < 1
            or attempt.get("last_error_code") != "cos_delete_failed"
            or any(not isinstance(value, str) or not value for value in values.values())
        ):
            raise MaterialError("material_rejection_audit_invalid")
        retries.append({
            "audit_path": path.name,
            **values,
            "attempt_count": attempt["attempt_count"],
            "last_error_code": attempt["last_error_code"],
        })
    return sorted(
        retries,
        key=lambda item: (
            item["scene_id"], item["slot_id"], item["request_id"], item["cos_key"]
        ),
    )


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


def _verified_snapshot_inputs(
    output_root: Path,
    render_payload: Mapping[str, Any],
    render_report: Mapping[str, Any],
    *,
    duration_ms: int,
) -> tuple[dict[str, Any], ...]:
    """Rebind visual inspection to real renderer-owned PNG evidence."""

    if (
        isinstance(duration_ms, bool)
        or not isinstance(duration_ms, int)
        or not 1 <= duration_ms <= 600_000
        or not isinstance(render_payload, Mapping)
        or not isinstance(render_report, Mapping)
    ):
        raise ValueError("quality_snapshot_evidence_invalid")
    root = Path(output_root).resolve()
    snapshot_root = (root / "snapshots").resolve()
    render_items = render_payload.get("snapshots")
    report_items = render_report.get("snapshots")
    if (
        not isinstance(render_items, list)
        or not isinstance(report_items, list)
        or not 1 <= len(render_items) <= 6
        or len(report_items) != len(render_items)
        or any(not isinstance(item, str) for item in render_items)
        or len(set(render_items)) != len(render_items)
    ):
        raise ValueError("quality_snapshot_evidence_invalid")
    evidence: list[dict[str, Any]] = []
    for relative, item in zip(render_items, report_items):
        if not isinstance(item, Mapping) or set(item) != {
            "path", "size_bytes", "sha256", "timestamp_ms"
        }:
            raise ValueError("quality_snapshot_evidence_invalid")
        name = item.get("path")
        size = item.get("size_bytes")
        digest = item.get("sha256")
        timestamp_ms = item.get("timestamp_ms")
        timestamp_match = re.fullmatch(
            r"frame-\d+-at-(\d+(?:\.\d+)?)s\.png",
            name if isinstance(name, str) else "",
        )
        filename_timestamp_ms = (
            round(float(timestamp_match.group(1)) * 1000)
            if timestamp_match is not None else None
        )
        expected_relative = f"snapshots/{name}" if isinstance(name, str) else ""
        candidate = root / relative
        if (
            not isinstance(name, str)
            or Path(name).name != name
            or relative != expected_relative
            or isinstance(size, bool)
            or not isinstance(size, int)
            or not 1 <= size <= 32 * 1024 * 1024
            or not isinstance(digest, str)
            or _VARIATION_SHA256.fullmatch(digest) is None
            or isinstance(timestamp_ms, bool)
            or not isinstance(timestamp_ms, int)
            or not 0 <= timestamp_ms <= duration_ms
            or timestamp_ms != filename_timestamp_ms
            or candidate.is_symlink()
            or not candidate.is_file()
        ):
            raise ValueError("quality_snapshot_evidence_invalid")
        resolved = candidate.resolve()
        if resolved.parent != snapshot_root:
            raise ValueError("quality_snapshot_evidence_invalid")
        try:
            before = os.stat(resolved, follow_symlinks=False)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_size != size
                or before.st_size > 32 * 1024 * 1024
            ):
                raise ValueError("quality_snapshot_evidence_invalid")
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(
                os, "O_NOFOLLOW", 0
            )
            descriptor = os.open(os.fspath(resolved), flags)
            with os.fdopen(descriptor, "rb", closefd=True) as source:
                opened = os.fstat(source.fileno())
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or not os.path.samestat(before, opened)
                    or opened.st_size != size
                ):
                    raise ValueError("quality_snapshot_evidence_invalid")
                hasher = hashlib.sha256()
                total = 0
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > size or total > 32 * 1024 * 1024:
                        raise ValueError("quality_snapshot_evidence_invalid")
                    hasher.update(chunk)
        except OSError as exc:
            raise ValueError("quality_snapshot_evidence_invalid") from exc
        if total != size or hasher.hexdigest() != digest:
            raise ValueError("quality_snapshot_evidence_invalid")
        evidence.append({
            "local_path": resolved,
            "frame_sha256": digest,
            "timestamp_ms": timestamp_ms,
            "size_bytes": size,
        })
    return tuple(evidence)


def _scene_asset_ids(
    scene: Mapping[str, Any],
    known_asset_ids: list[str],
    scene_slot_asset_ids: Mapping[tuple[str, str], str] | None = None,
) -> list[str]:
    """Bind a composition only to the frozen assets requested by its scene."""

    known = set(known_asset_ids)
    if scene_slot_asset_ids is not None:
        scene_id = str(scene.get("id") or "")
        requested = []
        for slot in scene.get("material_slots") or ():
            slot_id = str(slot.get("layout_slot_id") or slot.get("id") or "")
            asset_id = scene_slot_asset_ids.get((scene_id, slot_id))
            if asset_id is None:
                if slot.get("priority") == "required":
                    raise ValueError("scene_material_binding_invalid")
                continue
            requested.append(asset_id)
    else:
        requested = [str(slot["id"]) for slot in scene.get("material_slots") or ()]
    if len(requested) != len(set(requested)) or any(asset_id not in known for asset_id in requested):
        raise ValueError("scene_material_binding_invalid")
    return requested


def _layout_slot_bindings(
    scene: Mapping[str, Any],
    known_asset_ids: list[str],
    scene_slot_asset_ids: Mapping[tuple[str, str], str] | None = None,
) -> list[dict[str, str]]:
    """Emit deterministic V2 semantic bindings from the scene's frozen slots."""

    layout_id = str(scene.get("layout_id") or "")
    consumed_slots = {
        "speaker_fullscreen": frozenset({"evidence"}),
        "speaker_left_info_right": frozenset({"evidence"}),
        "speaker_right_evidence_left": frozenset({"evidence"}),
        "material_fullscreen_speaker_pip": frozenset({"primary", "detail"}),
        "product_hero": frozenset({"primary", "detail"}),
        "editorial_collage": frozenset({"primary", "detail"}),
        "comparison_split": frozenset({"primary", "detail"}),
        "steps_stack": frozenset({"accent"}),
        "number_proof": frozenset({"evidence"}),
        "quote_reversal": frozenset({"evidence"}),
        "method_timeline": frozenset({"accent"}),
        "cta_offer": frozenset({"accent"}),
    }.get(layout_id)
    if consumed_slots is None:
        return []
    known = set(known_asset_ids)
    bindings: list[dict[str, str]] = []
    seen_slots: set[str] = set()
    for raw in scene.get("material_slots") or ():
        if not isinstance(raw, Mapping):
            raise ValueError("scene_layout_binding_invalid")
        original_asset_id = raw.get("id")
        purpose = raw.get("purpose")
        priority = raw.get("priority")
        explicit_slot = raw.get("layout_slot_id")
        if not isinstance(original_asset_id, str) or priority not in {"required", "optional"}:
            raise ValueError("scene_layout_binding_invalid")
        if explicit_slot is not None:
            if explicit_slot not in {"primary", "detail", "evidence", "accent", "steps"}:
                raise ValueError("scene_layout_binding_invalid")
            slot_id = explicit_slot
        elif purpose == "product":
            if priority != "required":
                raise ValueError("scene_layout_binding_invalid")
            slot_id = "primary"
        elif purpose == "evidence":
            slot_id = "evidence"
        elif purpose == "context":
            slot_id = "steps" if layout_id == "steps_stack" else "detail"
        elif purpose == "decoration":
            slot_id = "accent"
        else:
            raise ValueError("scene_layout_binding_invalid")
        lookup_slot_id = str(explicit_slot or original_asset_id)
        asset_id = (
            scene_slot_asset_ids.get((str(scene.get("id") or ""), lookup_slot_id))
            if scene_slot_asset_ids is not None
            else original_asset_id
        )
        if asset_id is None:
            if priority == "required":
                raise ValueError("scene_layout_binding_invalid")
            continue
        if asset_id not in known:
            raise ValueError("scene_layout_binding_invalid")
        if slot_id not in consumed_slots:
            if priority == "required":
                raise ValueError("scene_layout_binding_unconsumed")
            continue
        if slot_id in seen_slots:
            raise ValueError("scene_layout_binding_duplicate")
        seen_slots.add(slot_id)
        bindings.append({"slot_id": slot_id, "asset_id": asset_id})
    if layout_id in {"product_hero", "material_fullscreen_speaker_pip", "editorial_collage", "comparison_split"} and "primary" not in seen_slots:
        raise ValueError("scene_layout_required_slot_missing")
    if layout_id in {"speaker_left_info_right", "speaker_right_evidence_left"} and "evidence" not in seen_slots:
        raise ValueError("scene_layout_required_slot_missing")
    slot_order = {"primary": 0, "detail": 1, "evidence": 2, "accent": 3, "steps": 4}
    return sorted(bindings, key=lambda binding: slot_order[binding["slot_id"]])


def _validate_layout_source_requirements(
    scene: Mapping[str, Any], *, source_video: Mapping[str, Any] | None
) -> None:
    if str(scene.get("layout_id") or "") in {
        "speaker_fullscreen",
        "speaker_left_info_right",
        "speaker_right_evidence_left",
        "material_fullscreen_speaker_pip",
    } and not isinstance(source_video, Mapping):
        raise ValueError("scene_layout_required_source_missing")


def _validate_layout_authoritative_content(
    scene: Mapping[str, Any], *, captions: list[Mapping[str, Any]]
) -> None:
    if str(scene.get("layout_id") or "") not in {
        "number_proof",
        "quote_reversal",
        "method_timeline",
        "cta_offer",
    }:
        return
    start_ms = scene.get("start_ms")
    end_ms = scene.get("end_ms")
    if not isinstance(start_ms, int) or not isinstance(end_ms, int) or end_ms <= start_ms:
        raise ValueError("scene_layout_authoritative_content_missing")
    if not any(
        isinstance(caption, Mapping)
        and isinstance(caption.get("text"), str)
        and bool(caption["text"])
        and isinstance(caption.get("start_ms"), int)
        and isinstance(caption.get("end_ms"), int)
        and caption["start_ms"] < end_ms
        and caption["end_ms"] > start_ms
        for caption in captions
    ):
        raise ValueError("scene_layout_authoritative_content_missing")


_OVERLAY_UI_LABELS = {
    "chapter": "章节",
    "step": "步骤",
    "category": "分类",
    "evidence_marker": "证据",
    "cta_prompt": "行动",
}


def _freeze_overlay_authoritative_content(scene: Mapping[str, Any]) -> dict[str, Any]:
    """Project validated edit-plan facts into the renderer without markup or URLs."""
    if not isinstance(scene, Mapping):
        raise ValueError("scene_overlay_authoritative_content_invalid")
    frozen: dict[str, Any] = {}
    for reference in ("headline", "highlight"):
        value = scene.get(reference)
        if not isinstance(value, Mapping):
            raise ValueError("scene_overlay_authoritative_content_invalid")
        text_kind = value.get("text_kind")
        if text_kind == "ui_label":
            if set(value) != {"text_kind", "ui_label_id"} or value.get("ui_label_id") not in _OVERLAY_UI_LABELS:
                raise ValueError("scene_overlay_authoritative_content_invalid")
            frozen[reference] = {"text": _OVERLAY_UI_LABELS[str(value["ui_label_id"])], "source_caption_ids": []}
            continue
        if text_kind not in {"verbatim", "compressed"} or set(value) != {"text_kind", "text", "source_caption_ids"}:
            raise ValueError("scene_overlay_authoritative_content_invalid")
        text = value.get("text")
        source_caption_ids = value.get("source_caption_ids")
        if not isinstance(text, str) or not text or len(text) > 4000 or not isinstance(source_caption_ids, list) or not source_caption_ids or not all(isinstance(item, str) and re.fullmatch(r"[a-z0-9_]{1,64}", item) for item in source_caption_ids):
            raise ValueError("scene_overlay_authoritative_content_invalid")
        frozen[reference] = {"text": text, "source_caption_ids": list(source_caption_ids)}
    return frozen


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
    def _director_decision_system_prompt() -> str:
        schema = load_frozen_schema("director-decision-v1.schema.json")
        for metadata_key in ("$schema", "$id", "title"):
            schema.pop(metadata_key, None)
        schema["$defs"]["sceneDirective"]["properties"]["material_bindings"] = {
            "const": []
        }
        contract = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
        return "\n".join((
            "你是中文短视频导演。只返回一个 JSON 对象，不得输出 Markdown、解释、路径、URL、代码或提供商字段。",
            "输出必须严格满足下方 director-decision-v1 JSON Schema；additionalProperties=false 表示不得增加任何字段。",
            "顶层只能包含 version、creative_concept、narrative_pattern、theme_profile_id、design_intent、scene_directives、audio_intent；不得包装在 output_contract、schema、$schema、$defs、properties、task、result、data 或 decision 中，也不得返回 Schema 本身。",
            "scene_directives.length 必须等于 scene_candidates.length，且第 i 项必须对应第 i 个候选；scene_id 必须原样使用候选 ID，不得遗漏、重复或重排。",
            "每个场景的 layout_id 必须来自该 scene_candidate.allowed_layout_ids；layout_variant、component_id、preset、transition 和 theme_profile_id 只能从请求 capabilities 的对应白名单选择。",
            "headline/highlight 若存在，必须是只含 text_kind 与 source_caption_ids 两个键的 JSON 对象，例如 {\"text_kind\":\"compressed\",\"source_caption_ids\":[\"caption_001\"]}；这个例子只展示结构，实际 ID 必须使用当前 scene_candidate.caption_ids。若不使用就完全省略该字段，不得输出 null、字符串、数组或 text 字段，不得复制或改写字幕文字。",
            "overlay 的 content_ref 必须指向同场景已声明的 headline 或 highlight；placement 必须等于 capabilities.overlay_placements[component_id] 中某项的 placement，引用字幕拼接后的字符数和行数不得超过该项 max_chars 与 max_lines。",
            "overlay_variants 中对应列表为空时省略 variant；每个 animation.target_id 必须等于同场景 overlay instance_id，除非 capabilities 明确声明公开 target；没有 overlay 时 animations 必须为空。",
            "material_bindings 必须为空。只用 material_slot_directives 表达语义素材需求；slot_id 必须采用 candidate_XX_purpose 形式并在整条视频中唯一。",
            "素材槽的 purpose、priority 必须满足 capabilities.layout_requirements；标记 required_for_layout 的槽必须用 priority=required 提供，整条视频 required 槽不得超过 max_required_material_slots。",
            "整条视频的素材槽总数不得超过 max_total_material_slots。视频口播的第一场必须选择 speaker_required=true 的布局；把所有 speaker_required!=true 场景的(end_ms-start_ms)相加，必须不超过整片时长乘 speaker_visibility_policy.max_hidden_ratio。",
            "输出前必须按 scene_structure_policy 自检：当总时长和场景数达到门槛时，结构签名(layout_id、layout_variant、排序后的overlay组件集合)的不同数量必须不少于 minimum_distinct_signatures，任何连续相同签名的数量不得超过 max_adjacent_identical。",
            "current_materials 只是可优先匹配的安全语义摘要，不代表所有槽都已满足；若某素材槽意图复用其中一项，semantic 必须逐字复制该项 semantic，未匹配的 required 槽由后续素材解析器生成。所有必需数组即使为空也必须输出。",
            "自动生成的 required 素材槽必须能由非人物安全配图满足：semantic 不得要求人物、人脸、讲师、团队、客户、口播者、特定品牌、真实产品包装、真实门店或事实证据；优先描述抽象概念图、流程示意图或非人物环境素材。product/evidence 槽若没有 current_materials 可精确匹配，只能表达非品牌概念或明确的示意图；若叙事必须出现人物或特定真实主体，应改用显示原口播人物的布局，不得要求生成新人物素材。",
            "audio_intent.dialogue_priority 必须为 true；创意可以自由，但不得改变权威文案事实。修复请求出现时，根据 repair.error_code、repair.field_path 和 repair.expected_constraint 修正结构；scene_directives_exact_candidate_order_and_count 表示数量、顺序与ID必须完全对应候选；scene_signatures_meet_distinct_and_adjacency_policy 表示重新编排布局、变体或overlay组合以同时满足 minimum_distinct_signatures 与 max_adjacent_identical；speaker_hidden_duration_within_max_ratio 表示减少无人物布局时长。",
            "For a repair, transition_from_capabilities_transition_capabilities means every transition must be copied from capabilities.transition_capabilities. Recheck every global constraint after the requested repair, not only the named field. Speaker visibility must use each candidate's exact end_ms-start_ms and the server-provided max_hidden_ratio; never estimate it from text length.",
            "JSON Schema:",
            contract,
        ))

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
        current_materials = list(request.get("current_materials") or ())[:10]
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

    def generate_decision(self, request: Mapping[str, Any], **kwargs: Any) -> ProviderResult:
        """Use the same pinned Qwen transport, returning only director-decision JSON."""
        deadline_at = kwargs.get("deadline_at")
        if isinstance(deadline_at, bool) or not isinstance(deadline_at, (int, float)) or not math.isfinite(deadline_at):
            raise TimeoutError("director_deadline_exceeded")
        remaining_seconds = math.floor(float(deadline_at) - time.time())
        if remaining_seconds < 1:
            raise TimeoutError("director_deadline_exceeded")
        system = self._director_decision_system_prompt()
        result = self.client.generate_director_decision(
            system,
            json.dumps(request, ensure_ascii=False, separators=(",", ":")),
            timeout_seconds=min(self._timeout_seconds, remaining_seconds),
        )
        return ProviderResult(provider="dashscope", capability="director", request_id=result.request_id, payload={"content": result.payload["content"]}, usage={"tokens": result.cost_units}, elapsed_ms=result.elapsed_ms)


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

        snapshots = kwargs.get("snapshots")
        evidence = [
            {
                "frame_sha256": item["frame_sha256"],
                "timestamp_ms": item["timestamp_ms"],
            }
            for item in snapshots
            if isinstance(item, Mapping)
            and isinstance(item.get("frame_sha256"), str)
            and isinstance(item.get("timestamp_ms"), int)
            and not isinstance(item.get("timestamp_ms"), bool)
        ] if isinstance(snapshots, (list, tuple)) else []
        if snapshots is None:
            evidence = [{
                "frame_sha256": hashlib.sha256(canonical_json(manifest)).hexdigest(),
                "timestamp_ms": 0,
            }]
        duration = manifest.get("duration_ms")
        compositions = manifest.get("compositions")
        captions = manifest.get("captions")
        assets = manifest.get("assets")
        valid_shape = (
            isinstance(duration, int) and not isinstance(duration, bool) and duration > 0
            and isinstance(compositions, list) and bool(compositions)
            and isinstance(captions, list) and bool(captions)
            and isinstance(assets, list)
            and bool(evidence)
        )
        results = {check_id: ("unknown", "manifest_shape_invalid", False) for check_id in blocking}
        if valid_shape:
            expected_start = 0
            scene_flow_valid = True
            max_scene_ms = 0
            layouts = []
            structure_signatures: list[tuple[str, str, tuple[str, ...]]] = []
            structure_scene_ids: list[str] = []
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
                overlay_instances = composition.get("overlay_instances")
                component_ids = tuple(sorted(
                    str(instance.get("component_id"))
                    for instance in overlay_instances
                    if isinstance(instance, Mapping)
                    and isinstance(instance.get("component_id"), str)
                )) if isinstance(overlay_instances, list) else ()
                structure_signatures.append((
                    layout_id,
                    str(composition.get("layout_variant") or ""),
                    component_ids,
                ))
                structure_scene_ids.append(str(
                    composition.get("scene_id")
                    or composition.get("id")
                    or f"composition@{len(structure_scene_ids)}"
                ))
                scene_asset_set = set(scene_assets)
                if len(scene_asset_set) != len(scene_assets) or not scene_asset_set.issubset(known_assets):
                    material_binding_valid = False
                if layout_id in _LAYOUTS_REQUIRING_MATERIALS and not scene_asset_set:
                    layout_material_compatible = False
                used_assets.update(scene_asset_set)
                if scene_assets and scene_duration > scene_budget_ms:
                    long_material_scene = True
                if not layout_shows_speaker(layout_id):
                    hidden_speaker_ms += scene_duration
            scene_flow_valid = scene_flow_valid and expected_start == duration
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
            longest_identical_run = 0
            current_identical_run = 0
            previous_signature: tuple[str, str, tuple[str, ...]] | None = None
            logical_structure_signatures: list[tuple[str, str, tuple[str, ...]]] = []
            previous_scene_id: str | None = None
            for scene_id, signature in zip(structure_scene_ids, structure_signatures):
                if scene_id == previous_scene_id:
                    continue
                logical_structure_signatures.append(signature)
                previous_scene_id = scene_id
            for signature in logical_structure_signatures:
                current_identical_run = (
                    current_identical_run + 1
                    if signature == previous_signature else 1
                )
                longest_identical_run = max(
                    longest_identical_run,
                    current_identical_run,
                )
                previous_signature = signature
            structure_diverse = (
                not requires_scene_rhythm
                or (
                    len(set(logical_structure_signatures)) >= 2
                    and longest_identical_run <= 2
                )
            )
            structure_reason = (
                "adjacent_scene_structure_repetition"
                if longest_identical_run > 2
                else "scene_structure_diversity"
            )
            source_video = isinstance(manifest.get("source_video"), Mapping)
            face_visible = (
                not source_video
                or (
                    hidden_speaker_ms <= int(duration * 0.4)
                    and bool(layouts)
                    and layout_shows_speaker(layouts[0])
                )
            )
            opening_consistent = scene_rhythm_valid and structure_diverse and (
                not source_video or (bool(layouts) and layout_shows_speaker(layouts[0]))
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
                    and structure_diverse,
                    (
                        "captions_are_timed_per_bounded_varied_scene"
                        if structure_diverse else structure_reason
                    ),
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
                    structure_failure = (
                        check_id == "safe_area_and_text_visibility"
                        and reason in {
                            "adjacent_scene_structure_repetition",
                            "scene_structure_diversity",
                        }
                    )
                    results[check_id] = (
                        "pass" if passed else "fail",
                        reason if passed else f"{reason}_failed",
                        not passed and not structure_failure and check_id in {
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


class _MaterialReviewMapping(dict[str, Any]):
    """Mapping-compatible review carrying provider identity outside JSON."""

    def __init__(self, value: Mapping[str, Any], *, request_id: str) -> None:
        super().__init__(value)
        self.request_id = request_id


_MATERIAL_DESCRIPTOR_TEXT_RE = re.compile(
    r"(?:[\x00-\x1f\x7f-\x9f]|https?://|file://|data:image|q-sign-|x-cos-|security-token|(?<![A-Za-z0-9])sk[-_][A-Za-z0-9]|(?:signature|credential|api[_ -]?key|password)\s*[:=]\s*\S)",
    re.IGNORECASE,
)
_MATERIAL_SUBJECT_TYPES = frozenset({
    "product", "store", "venue", "document", "object", "environment",
    "graphic", "person", "other",
})
_MATERIAL_RISK_LABELS = frozenset({
    "person", "face", "text", "logo", "sensitive", "uncertain",
})
_MATERIAL_RATIOS = frozenset({"16:9", "9:16", "1:1"})


def _material_descriptor_input_sha256(
    owner_id: str,
    frozen: list[Mapping[str, Any]],
) -> str:
    if not isinstance(owner_id, str) or not owner_id:
        raise MaterialError("material_descriptor_input_invalid")
    identities: list[dict[str, Any]] = []
    for ordinal, item in enumerate(frozen, 1):
        if not isinstance(item, Mapping):
            raise MaterialError("material_descriptor_input_invalid")
        identity = {
            "ordinal": ordinal,
            "material_id": item.get("material_id"),
            "sha256": item.get("sha256"),
            "size_bytes": item.get("size_bytes"),
            "mime_type": item.get("mime_type"),
        }
        if (
            not isinstance(identity["material_id"], str)
            or not identity["material_id"]
            or not isinstance(identity["sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", identity["sha256"]) is None
            or isinstance(identity["size_bytes"], bool)
            or not isinstance(identity["size_bytes"], int)
            or identity["size_bytes"] < 1
            or identity["mime_type"] not in {"image/jpeg", "image/png", "image/webp"}
        ):
            raise MaterialError("material_descriptor_input_invalid")
        identities.append(identity)
    return hashlib.sha256(canonical_json({
        "contract": "ai-edit-v3-material-descriptor-input-v1",
        "owner_id": owner_id,
        "items": identities,
    })).hexdigest()


def _safe_descriptor_text(value: Any, *, max_chars: int) -> str:
    if not isinstance(value, str):
        raise MaterialError("material_descriptor_invalid")
    normalized = " ".join(value.split())
    if (
        not normalized
        or len(normalized) > max_chars
        or _MATERIAL_DESCRIPTOR_TEXT_RE.search(normalized) is not None
        or re.search(r"[0-9a-f]{64}", normalized, re.IGNORECASE) is not None
    ):
        raise MaterialError("material_descriptor_invalid")
    return normalized


def _material_descriptor_payload(
    raw: Any,
    *,
    expected_aliases: tuple[str, ...],
) -> dict[str, Any]:
    if isinstance(raw, ProviderResult) or (
        not isinstance(raw, Mapping)
        and all(hasattr(raw, field) for field in (
            "provider", "capability", "request_id", "payload",
        ))
    ):
        provider_payload = getattr(raw, "payload", None)
        if (
            getattr(raw, "provider", None) != "dashscope"
            or getattr(raw, "capability", None) != "material_analysis"
            or not isinstance(getattr(raw, "request_id", None), str)
            or not getattr(raw, "request_id", "")
            or not isinstance(provider_payload, Mapping)
        ):
            raise MaterialError("material_descriptor_invalid")
        content = provider_payload.get("content")
        if not isinstance(content, str):
            raise MaterialError("material_descriptor_invalid")
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise MaterialError("material_descriptor_invalid") from exc
    elif isinstance(raw, Mapping):
        payload = dict(raw)
    else:
        raise MaterialError("material_descriptor_invalid")
    if not isinstance(payload, Mapping):
        raise MaterialError("material_descriptor_invalid")
    if set(payload) != {"descriptors"} or not isinstance(payload["descriptors"], list):
        raise MaterialError("material_descriptor_invalid")
    by_alias: dict[str, dict[str, Any]] = {}
    for raw_descriptor in payload["descriptors"]:
        if not isinstance(raw_descriptor, Mapping) or set(raw_descriptor) != {
            "upload_alias", "semantic", "subject_type", "composition",
            "supported_ratios", "risk_labels",
        }:
            raise MaterialError("material_descriptor_invalid")
        alias = raw_descriptor.get("upload_alias")
        subject_type = raw_descriptor.get("subject_type")
        ratios = raw_descriptor.get("supported_ratios")
        risks = raw_descriptor.get("risk_labels")
        if (
            not isinstance(alias, str)
            or alias not in expected_aliases
            or alias in by_alias
            or not isinstance(subject_type, str)
            or subject_type not in _MATERIAL_SUBJECT_TYPES
            or not isinstance(ratios, list)
            or not ratios
            or len(ratios) > len(_MATERIAL_RATIOS)
            or any(
                not isinstance(item, str) or item not in _MATERIAL_RATIOS
                for item in ratios
            )
            or len(set(ratios)) != len(ratios)
            or not isinstance(risks, list)
            or len(risks) > len(_MATERIAL_RISK_LABELS)
            or any(
                not isinstance(item, str) or item not in _MATERIAL_RISK_LABELS
                for item in risks
            )
            or len(set(risks)) != len(risks)
        ):
            raise MaterialError("material_descriptor_invalid")
        by_alias[alias] = {
            "upload_alias": alias,
            "semantic": _safe_descriptor_text(raw_descriptor.get("semantic"), max_chars=300),
            "subject_type": subject_type,
            "composition": _safe_descriptor_text(raw_descriptor.get("composition"), max_chars=160),
            "supported_ratios": list(ratios),
            "risk_labels": list(risks),
        }
    if set(by_alias) != set(expected_aliases):
        raise MaterialError("material_descriptor_scope_invalid")
    return {"descriptors": [by_alias[alias] for alias in expected_aliases]}


class QwenMaterialReviewer(DeterministicVisualInspector):
    """Keep COS signing inside the Qwen-VL adapter and return only review JSON."""

    def __init__(self, *, cos: Any, client: Any) -> None:
        if not callable(getattr(client, "inspect_image", None)):
            raise ValueError("material_review_client_invalid")
        self.cos = cos
        self.client = client

    def inspect_material(
        self,
        *,
        cos_key: str,
        semantic: str,
        forbidden_subjects: tuple[str, ...] | list[str],
        source_metadata: Mapping[str, Any],
        deadline_at: float,
        **_: Any,
    ) -> Mapping[str, Any]:
        if (
            not isinstance(cos_key, str)
            or not cos_key
            or "://" in cos_key
            or ".." in cos_key
            or "\\" in cos_key
            or not isinstance(semantic, str)
            or not semantic.strip()
            or not isinstance(source_metadata, Mapping)
            or not isinstance(forbidden_subjects, (tuple, list))
            or any(not isinstance(item, str) or not item for item in forbidden_subjects)
        ):
            raise ValueError("material_review_request_invalid")
        presign_get = getattr(self.cos, "presign_get", None)
        if not callable(presign_get):
            raise ValueError("material_review_cos_invalid")
        signed_url = presign_get(cos_key, expires=300)
        if not isinstance(signed_url, str) or not signed_url.startswith("https://"):
            raise ValueError("material_review_signed_url_invalid")
        result = self.client.inspect_image(
            {
                "image_url": signed_url,
                "semantic": semantic.strip(),
                "forbidden_subjects": list(forbidden_subjects),
                "source_metadata": dict(source_metadata),
                "output_contract": "material-review-v1",
            },
            deadline_at=deadline_at,
        )
        request_id = getattr(result, "request_id", None)
        payload = getattr(result, "payload", None)
        if not isinstance(request_id, str) or not isinstance(payload, Mapping):
            raise ValueError("material_review_response_invalid")
        content = payload.get("content")
        if not isinstance(content, str) or not content or len(content.encode("utf-8")) > 16 * 1024:
            raise ValueError("material_review_response_invalid")
        try:
            review = json.loads(content)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError("material_review_response_invalid") from exc
        if not isinstance(review, Mapping):
            raise ValueError("material_review_response_invalid")
        normalized = validate_generated_material_review(review, required=False)
        return _MaterialReviewMapping(normalized, request_id=request_id)

    def describe_materials(
        self,
        images: list[Mapping[str, Any]],
        *,
        deadline_at: float,
    ) -> Mapping[str, Any]:
        describe_images = getattr(self.client, "describe_images", None)
        if not callable(describe_images) or not isinstance(images, list) or not 1 <= len(images) <= 5:
            raise ValueError("material_descriptor_client_invalid")
        provider_images: list[dict[str, Any]] = []
        aliases: list[str] = []
        for image in images:
            if not isinstance(image, Mapping) or set(image) != {
                "upload_alias", "width", "height", "jpeg_bytes",
            }:
                raise ValueError("material_descriptor_request_invalid")
            alias = image.get("upload_alias")
            width = image.get("width")
            height = image.get("height")
            pixels = image.get("jpeg_bytes")
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
                or not isinstance(pixels, bytes)
                or not pixels
                or len(pixels) > 256 * 1024
            ):
                raise ValueError("material_descriptor_request_invalid")
            aliases.append(alias)
            provider_images.append({
                "upload_alias": alias,
                "width": width,
                "height": height,
                "data_url": "data:image/jpeg;base64," + base64.b64encode(pixels).decode("ascii"),
            })
        result = describe_images(
            {
                "images": provider_images,
                "output_contract": "material-descriptors-v1",
            },
            deadline_at=deadline_at,
        )
        request_id = getattr(result, "request_id", None)
        if not isinstance(request_id, str) or not request_id:
            raise MaterialDescriptorContractError(
                "material_descriptor_response_invalid"
            )
        try:
            normalized = _material_descriptor_payload(
                result,
                expected_aliases=tuple(aliases),
            )
        except MaterialError as exc:
            raise MaterialDescriptorContractError(exc.code) from exc
        return _MaterialReviewMapping(normalized, request_id=request_id)


def invoke_provider_once(
    *,
    store: Any,
    context: Any,
    stage: str,
    provider: str,
    capability: str,
    operation_key: str,
    request_sha256: str,
    call: Any,
    now_ms: int,
) -> Mapping[str, Any]:
    """Use the real V3 provider receipt before making a replayable provider call."""

    if not callable(call):
        raise ValueError("provider_call_invalid")
    def frozen_result(task: Any, expected_status: str) -> Mapping[str, Any] | None:
        if not isinstance(task, Mapping) or task.get("status") != expected_status:
            return None
        raw = task.get("result_json", task.get("result"))
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError("provider_receipt_invalid") from exc
        if not isinstance(raw, Mapping):
            raise ValueError("provider_receipt_invalid")
        return dict(raw)

    def replay_terminal(task: Any) -> Mapping[str, Any] | None:
        completed = frozen_result(task, "completed")
        if completed is not None:
            return completed
        failed = frozen_result(task, "failed")
        if failed is not None:
            if set(failed) != {"request_id", "reason_code"}:
                raise ValueError("provider_receipt_invalid")
            reason_code = failed.get("reason_code")
            if not isinstance(reason_code, str):
                raise ValueError("provider_receipt_invalid")
            raise DefinitiveNotAccepted(reason_code)
        return None

    def validate_immutable_intent(task: Any) -> None:
        if task is None:
            return
        if not isinstance(task, Mapping):
            raise ValueError("provider_receipt_invalid")
        expected = {
            "stage": stage,
            "provider": provider,
            "capability": capability,
            "request_sha256": request_sha256,
        }
        if any(task.get(name) != value for name, value in expected.items()):
            raise ValueError("provider_intent_conflict")

    existing = store.get_provider_task_for_claim(
        context.claim,
        operation_key,
        now_ms,
    )
    validate_immutable_intent(existing)
    replay = replay_terminal(existing)
    if replay is not None:
        return replay
    if existing is not None and existing.get("status") != "intent_recorded":
        raise ValueError("provider_receipt_pending")
    if existing is None or existing.get("status") == "intent_recorded":
        store.record_provider_intent(
            context.claim,
            stage,
            context.stage_attempt_id,
            provider,
            capability,
            operation_key,
            request_sha256,
            now_ms,
        )
    if not store.claim_provider_submission(
        context.claim,
        stage,
        context.stage_attempt_id,
        provider,
        capability,
        operation_key,
        request_sha256,
        now_ms,
    ):
        existing = store.get_provider_task_for_claim(
            context.claim,
            operation_key,
            now_ms,
        )
        validate_immutable_intent(existing)
        replay = replay_terminal(existing)
        if replay is not None:
            return replay
        raise ValueError("provider_receipt_pending")

    try:
        result = call()
    except MaterialDescriptorContractError:
        if capability != "material_analysis":
            raise
        release = getattr(store, "release_material_analysis_submission", None)
        if not callable(release):
            raise ValueError("provider_validation_release_unavailable")
        release(
            context.claim,
            operation_key,
            now_ms,
        )
        raise
    except DefinitiveNotAccepted as exc:
        external_id = "failure-" + hashlib.sha256(
            f"{operation_key}:{exc.reason_code}".encode("utf-8")
        ).hexdigest()
        store.bind_provider_result(
            context.claim,
            operation_key,
            external_id,
            "failed",
            {"request_id": external_id, "reason_code": exc.reason_code},
            now_ms,
        )
        raise
    if isinstance(result, ProviderResult):
        external_id = result.request_id
        payload: Any = result.payload.get("content", result.payload)
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError as exc:
                raise ValueError("provider_result_invalid") from exc
    elif isinstance(result, Mapping):
        external_id = getattr(result, "request_id", result.get("request_id"))
        payload = dict(result)
    else:
        raise ValueError("provider_result_invalid")
    if not isinstance(external_id, str) or not external_id or not isinstance(payload, Mapping):
        raise ValueError("provider_result_invalid")
    frozen = dict(payload)
    store.bind_provider_result(
        context.claim,
        operation_key,
        external_id,
        "completed",
        frozen,
        now_ms,
    )
    return frozen


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
    repairable_ids: set[str] | frozenset[str] | Mapping[str, Any],
) -> dict[str, Any]:
    """Apply bounded structural repairs instead of rerendering identical input."""

    repaired = copy.deepcopy(dict(manifest))
    compositions = repaired.get("compositions")
    if not isinstance(compositions, list) or not compositions:
        raise ValueError("repair_manifest_invalid")
    supported_ids = {
        "safe_area_and_text_visibility",
        "face_product_obstruction",
        "material_semantic_identity",
        "opening_hook_visual_consistency",
    }
    strict_targets: dict[str, frozenset[str]] | None = None
    if isinstance(repairable_ids, Mapping):
        strict_targets = _validate_repair_instruction(manifest, repairable_ids)
        requested = frozenset(strict_targets)
    else:
        requested = frozenset(repairable_ids)
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
            if (
                strict_targets is not None
                and str(composition.get("scene_id") or "")
                not in strict_targets.get("safe_area_and_text_visibility", frozenset())
            ):
                bounded.append(composition)
                continue
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
        and (
            strict_targets is None
            or str(repaired["compositions"][0].get("scene_id") or "")
            in strict_targets.get("opening_hook_visual_consistency", frozenset())
        )
        and not str(repaired["compositions"][0].get("layout_id", "")).startswith("speaker_")
    ):
        _apply_speaker_fallback(repaired["compositions"][0])

    if "face_product_obstruction" in requested:
        for composition in repaired["compositions"]:
            if (
                strict_targets is not None
                and str(composition.get("scene_id") or "")
                not in strict_targets.get("face_product_obstruction", frozenset())
            ):
                continue
            if composition.get("layout_id") in {"product_hero", "number_proof"}:
                _apply_speaker_fallback(composition)

    if (
        "material_semantic_identity" in requested
        and isinstance(repaired.get("source_video"), Mapping)
    ):
        for composition in repaired["compositions"]:
            if strict_targets is not None:
                targeted = str(composition.get("scene_id") or "") in strict_targets.get(
                    "material_semantic_identity", frozenset()
                )
            else:
                targeted = (
                    composition.get("layout_id") in _LAYOUTS_REQUIRING_MATERIALS
                    and not composition.get("asset_ids")
                )
            if targeted:
                _apply_speaker_fallback(composition)

    if repaired == dict(manifest):
        raise ValueError("repair_manifest_unchanged")
    if strict_targets is not None:
        _assert_repair_scope(manifest, repaired, strict_targets)
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


def _apply_speaker_fallback(composition: dict[str, Any]) -> None:
    composition["layout_id"] = "speaker_fullscreen"
    composition["asset_ids"] = []
    if "layout_variant" in composition:
        composition["layout_variant"] = "clean_center"
    if "layout_slot_bindings" in composition:
        composition["layout_slot_bindings"] = []


_REPAIR_ACTION_BY_REASON = {
    "safe_area_and_text_visibility": "split_scene",
    "face_product_obstruction": "speaker_fallback",
    "material_semantic_identity": "speaker_fallback",
    "opening_hook_visual_consistency": "speaker_fallback",
}
_REPAIR_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _freeze_repair_instruction(
    manifest: Mapping[str, Any],
    directives: tuple[Mapping[str, Any], ...] | list[Mapping[str, Any]],
) -> dict[str, Any]:
    compositions = manifest.get("compositions")
    if not isinstance(compositions, list):
        raise ValueError("repair_instruction_invalid")
    scene_id_values = [
        item.get("scene_id") for item in compositions if isinstance(item, Mapping)
    ]
    if (
        len(scene_id_values) != len(compositions)
        or any(
            not isinstance(scene_id, str) or _REPAIR_ID.fullmatch(scene_id) is None
            for scene_id in scene_id_values
        )
        or len(set(scene_id_values)) != len(scene_id_values)
    ):
        raise ValueError("repair_instruction_invalid")
    scene_ids = set(scene_id_values)
    normalized: list[dict[str, str]] = []
    identities: set[tuple[str, str]] = set()
    if not isinstance(directives, (list, tuple)) or not directives:
        raise ValueError("repair_instruction_invalid")
    for item in directives:
        if not isinstance(item, Mapping) or set(item) != {
            "scene_id", "reason_code", "allowed_action"
        }:
            raise ValueError("repair_instruction_invalid")
        scene_id = item.get("scene_id")
        reason_code = item.get("reason_code")
        allowed_action = item.get("allowed_action")
        if (
            not isinstance(scene_id, str) or _REPAIR_ID.fullmatch(scene_id) is None
            or scene_id not in scene_ids
            or not isinstance(reason_code, str)
            or _REPAIR_ACTION_BY_REASON.get(reason_code) != allowed_action
            or (scene_id, reason_code) in identities
        ):
            raise ValueError("repair_instruction_invalid")
        identities.add((scene_id, reason_code))
        normalized.append({
            "scene_id": scene_id,
            "reason_code": reason_code,
            "allowed_action": allowed_action,
        })
    body = {
        "version": "1.0",
        "source_manifest_sha256": hashlib.sha256(canonical_json(manifest)).hexdigest(),
        "directives": sorted(
            normalized,
            key=lambda value: (value["scene_id"], value["reason_code"]),
        ),
    }
    return {
        **body,
        "instruction_sha256": hashlib.sha256(canonical_json(body)).hexdigest(),
    }


def _validate_repair_instruction(
    manifest: Mapping[str, Any],
    instruction: Mapping[str, Any],
) -> dict[str, frozenset[str]]:
    if set(instruction) != {
        "version", "source_manifest_sha256", "directives", "instruction_sha256"
    }:
        raise ValueError("repair_instruction_invalid")
    body = {key: copy.deepcopy(instruction[key]) for key in (
        "version", "source_manifest_sha256", "directives"
    )}
    actual_sha = hashlib.sha256(canonical_json(body)).hexdigest()
    if instruction.get("instruction_sha256") != actual_sha:
        raise ValueError("repair_instruction_sha_invalid")
    if (
        instruction.get("version") != "1.0"
        or instruction.get("source_manifest_sha256")
        != hashlib.sha256(canonical_json(manifest)).hexdigest()
    ):
        raise ValueError("repair_instruction_source_invalid")
    frozen = _freeze_repair_instruction(manifest, body["directives"])
    if frozen != dict(instruction):
        raise ValueError("repair_instruction_invalid")
    targets: dict[str, set[str]] = {}
    for item in body["directives"]:
        targets.setdefault(item["reason_code"], set()).add(item["scene_id"])
    return {reason: frozenset(scene_ids) for reason, scene_ids in targets.items()}


def _assert_repair_scope(
    original: Mapping[str, Any],
    repaired: Mapping[str, Any],
    targets: Mapping[str, frozenset[str]],
) -> None:
    if {
        key: value for key, value in original.items() if key != "compositions"
    } != {
        key: value for key, value in repaired.items() if key != "compositions"
    }:
        raise ValueError("repair_manifest_scope_invalid")
    target_scene_ids = frozenset(
        scene_id for scene_ids in targets.values() for scene_id in scene_ids
    )
    original_non_targets = [
        item for item in original.get("compositions", ())
        if isinstance(item, Mapping) and item.get("scene_id") not in target_scene_ids
    ]
    repaired_non_targets = [
        item for item in repaired.get("compositions", ())
        if isinstance(item, Mapping) and item.get("scene_id") not in target_scene_ids
    ]
    if original_non_targets != repaired_non_targets:
        raise ValueError("repair_manifest_scope_invalid")


def _quality_repair_payload(
    manifest: Mapping[str, Any],
    quality: Any,
) -> dict[str, Any]:
    if getattr(quality, "can_repair", False) is not True:
        return {
            "can_repair": False,
            "repair_instruction": None,
            "repair_instruction_sha256": None,
        }
    instruction = _freeze_repair_instruction(
        manifest,
        getattr(quality, "repair_directives", ()),
    )
    return {
        "can_repair": True,
        "repair_instruction": instruction,
        "repair_instruction_sha256": instruction["instruction_sha256"],
    }


def _repair_instruction_from_quality(
    manifest: Mapping[str, Any],
    quality_payload: Mapping[str, Any],
) -> dict[str, Any]:
    instruction = quality_payload.get("repair_instruction")
    if (
        quality_payload.get("can_repair") is not True
        or not isinstance(instruction, Mapping)
        or quality_payload.get("repair_instruction_sha256")
        != instruction.get("instruction_sha256")
    ):
        raise ValueError("repair_quality_invalid")
    _validate_repair_instruction(manifest, instruction)
    return copy.deepcopy(dict(instruction))


def _prepare_material_analysis_jpeg(
    source: Path,
    destination: Path,
    *,
    deadline_at: float,
) -> dict[str, int]:
    """Create a bounded, metadata-free JPEG for multimodal analysis."""

    source = Path(source).resolve(strict=True)
    destination = Path(destination).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    remaining = deadline_at - time.time()
    if remaining <= 0:
        raise TimeoutError("material_descriptor_deadline_exceeded")
    temporary = destination.with_name(f".{destination.stem}.{os.getpid()}.tmp.jpg")
    command = [
        "ffmpeg", "-y", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-protocol_whitelist", "file,pipe",
        "-i", os.fspath(source),
        "-map_metadata", "-1",
        "-frames:v", "1",
        "-an",
        "-vf", "scale=w='min(512,iw)':h='min(512,ih)':force_original_aspect_ratio=decrease",
        "-c:v", "mjpeg",
        "-q:v", "5",
        "-pix_fmt", "yuvj420p",
        os.fspath(temporary),
    ]
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=max(1.0, min(30.0, remaining)),
            check=False,
        )
    except FileNotFoundError as exc:
        raise MaterialError("material_descriptor_ffmpeg_missing") from exc
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError("material_descriptor_ffmpeg_timeout") from exc
    if completed.returncode != 0 or not temporary.is_file():
        temporary.unlink(missing_ok=True)
        raise MaterialError("material_descriptor_ffmpeg_failed")
    try:
        size_bytes = temporary.stat().st_size
        if size_bytes < 4 or size_bytes > 256 * 1024:
            raise MaterialError("material_descriptor_pixels_invalid")
        image = _probe_image(
            temporary,
            timeout_seconds=max(1.0, min(10.0, deadline_at - time.time())),
        )
        if not 1 <= image.width <= 512 or not 1 <= image.height <= 512:
            raise MaterialError("material_descriptor_pixels_invalid")
        os.replace(temporary, destination)
        return {"width": image.width, "height": image.height}
    finally:
        temporary.unlink(missing_ok=True)


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
        material_analyzer: Any | None = None,
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
        self.material_analyzer = material_analyzer or self.visual_inspector

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

    def _frozen_bound_materials(self, job: Mapping[str, Any]) -> list[dict[str, Any]]:
        root = self._root(str(job["job_id"]))
        material_root = root / "materials"
        material_root.mkdir(parents=True, exist_ok=True)
        suffixes = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
        frozen: list[dict[str, Any]] = []
        for index, material in enumerate(self._bound_materials(job), 1):
            mime = str(material.get("mime_type"))
            suffix = suffixes.get(mime)
            if suffix is None:
                raise ValueError("job_material_type_invalid")
            destination = material_root / f"material-{index:02d}{suffix}"
            if not destination.exists():
                self.cos.download_file(material["cos_key"], destination)
            digest = _sha(destination)
            if (
                destination.stat().st_size != int(material["size_bytes"])
                or digest != material["sha256"]
            ):
                raise ValueError("job_material_content_mismatch")
            try:
                metadata = json.loads(material.get("metadata_json") or "{}")
            except json.JSONDecodeError as exc:
                raise ValueError("job_material_metadata_invalid") from exc
            if not isinstance(metadata, Mapping):
                raise ValueError("job_material_metadata_invalid")
            frozen.append({
                "material_id": material["material_id"],
                "cos_key": material["cos_key"],
                "relative_path": destination.relative_to(root).as_posix(),
                "mime_type": mime,
                "size_bytes": destination.stat().st_size,
                "sha256": digest,
                "probe_metadata": dict(metadata),
            })
        return frozen

    @staticmethod
    def _validated_material_descriptor_document(
        document: Any,
        frozen: list[dict[str, Any]],
        *,
        input_sha256: str,
    ) -> list[dict[str, Any]]:
        if not isinstance(document, Mapping) or set(document) != {
            "contract", "version", "input_sha256", "items",
        }:
            raise MaterialError("material_descriptor_artifact_invalid")
        items = document.get("items")
        if (
            document.get("contract") != "ai-edit-v3-material-descriptors-v1"
            or document.get("version") != "1.0"
            or document.get("input_sha256") != input_sha256
            or not isinstance(items, list)
            or len(items) != len(frozen)
        ):
            raise MaterialError("material_descriptor_artifact_invalid")
        normalized: list[dict[str, Any]] = []
        for index, (raw, trusted) in enumerate(zip(items, frozen, strict=True), 1):
            if not isinstance(raw, Mapping) or set(raw) != {
                "upload_alias", "material_id", "sha256", "semantic", "subject_type",
                "composition", "supported_ratios", "risk_labels",
            }:
                raise MaterialError("material_descriptor_artifact_invalid")
            alias = f"upload_{index:02d}"
            if (
                raw.get("upload_alias") != alias
                or raw.get("material_id") != trusted["material_id"]
                or raw.get("sha256") != trusted["sha256"]
            ):
                raise MaterialError("material_descriptor_artifact_identity_invalid")
            safe = _material_descriptor_payload(
                {"descriptors": [{
                    key: copy.deepcopy(raw[key])
                    for key in (
                        "upload_alias", "semantic", "subject_type", "composition",
                        "supported_ratios", "risk_labels",
                    )
                }]},
                expected_aliases=(alias,),
            )["descriptors"][0]
            normalized.append({
                "upload_alias": alias,
                "material_id": trusted["material_id"],
                "sha256": trusted["sha256"],
                **safe,
            })
        return normalized

    def _material_descriptors(
        self,
        job: Mapping[str, Any],
        context: Any,
    ) -> list[dict[str, Any]]:
        job_id = str(job["job_id"])
        root = self._root(job_id)
        frozen = self._frozen_bound_materials(job)
        artifact = root / "material-descriptors.json"
        input_sha256 = _material_descriptor_input_sha256(
            str(job.get("owner_id") or ""), frozen,
        )
        if artifact.exists():
            return self._validated_material_descriptor_document(
                _json(artifact), frozen, input_sha256=input_sha256,
            )
        if not frozen:
            _write_json(artifact, {
                "contract": "ai-edit-v3-material-descriptors-v1",
                "version": "1.0",
                "input_sha256": input_sha256,
                "items": [],
            })
            return []
        analyzer = getattr(self, "material_analyzer", None)
        describe_materials = getattr(analyzer, "describe_materials", None)
        if not callable(describe_materials):
            raise MaterialError("material_descriptor_analyzer_unavailable")
        deadline_at = getattr(context, "deadline_at", None)
        if not isinstance(deadline_at, (int, float)):
            raise MaterialError("material_descriptor_deadline_invalid")
        items: list[dict[str, Any]] = []
        analysis_root = root / "material-analysis"
        for batch_start in range(0, len(frozen), 5):
            batch = frozen[batch_start : batch_start + 5]
            analysis_images: list[dict[str, Any]] = []
            aliases: list[str] = []
            for offset, trusted in enumerate(batch):
                item_index = batch_start + offset + 1
                alias = f"upload_{item_index:02d}"
                source = root / trusted["relative_path"]
                analysis_path = analysis_root / f"{alias}.jpg"
                dimensions = _prepare_material_analysis_jpeg(
                    source,
                    analysis_path,
                    deadline_at=float(deadline_at),
                )
                aliases.append(alias)
                analysis_images.append({
                    "upload_alias": alias,
                    "width": dimensions["width"],
                    "height": dimensions["height"],
                    "jpeg_bytes": analysis_path.read_bytes(),
                })

            def call_analyzer() -> Mapping[str, Any]:
                try:
                    raw = describe_materials(
                        analysis_images,
                        deadline_at=float(deadline_at),
                    )
                    _material_descriptor_payload(
                        raw,
                        expected_aliases=tuple(aliases),
                    )
                    return raw
                except MaterialDescriptorContractError:
                    raise
                except MaterialError as exc:
                    raise MaterialDescriptorContractError(exc.code) from exc

            receipt_request = {
                "aliases": aliases,
                "source_sha256": [item["sha256"] for item in batch],
                "analysis_sha256": [
                    hashlib.sha256(item["jpeg_bytes"]).hexdigest()
                    for item in analysis_images
                ],
                "dimensions": [
                    [item["width"], item["height"]]
                    for item in analysis_images
                ],
            }
            real_receipts = (
                hasattr(context, "claim")
                and hasattr(context, "stage_attempt_id")
                and callable(getattr(self.store, "record_provider_intent", None))
                and callable(getattr(self.store, "get_provider_task_for_claim", None))
                and callable(getattr(self.store, "claim_provider_submission", None))
                and callable(getattr(self.store, "bind_provider_result", None))
            )
            if real_receipts:
                raw_descriptors = invoke_provider_once(
                    store=self.store,
                    context=context,
                    stage="planning",
                    provider="dashscope",
                    capability="material_analysis",
                    operation_key=(
                        f"ai-edit-v3:{job_id}:material-analysis:{batch_start // 5}"
                    ),
                    request_sha256=hashlib.sha256(
                        canonical_json(receipt_request)
                    ).hexdigest(),
                    call=call_analyzer,
                    now_ms=round(time.time() * 1000),
                )
            else:
                raw_descriptors = call_analyzer()
            safe_batch = _material_descriptor_payload(
                raw_descriptors,
                expected_aliases=tuple(aliases),
            )["descriptors"]
            public_json = canonical_json({"descriptors": safe_batch}).decode("utf-8")
            for trusted in batch:
                for private_value in (
                    trusted["material_id"], trusted["sha256"], trusted["cos_key"],
                ):
                    if isinstance(private_value, str) and private_value and private_value in public_json:
                        raise MaterialError("material_descriptor_private_data_leak")
            for trusted, safe in zip(batch, safe_batch, strict=True):
                items.append({
                    "material_id": trusted["material_id"],
                    "sha256": trusted["sha256"],
                    **safe,
                })
        document = {
            "contract": "ai-edit-v3-material-descriptors-v1",
            "version": "1.0",
            "input_sha256": input_sha256,
            "items": items,
        }
        _write_json(artifact, document)
        return self._validated_material_descriptor_document(
            document, frozen, input_sha256=input_sha256,
        )

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
            for material in self._material_descriptors(job, context):
                descriptors.append(SimpleNamespace(
                    material_id=material["upload_alias"],
                    upload_alias=material["upload_alias"],
                    semantic=material["semantic"],
                    subject_type=material["subject_type"],
                    composition=material["composition"],
                    supported_ratios=tuple(material["supported_ratios"]),
                    risk_labels=tuple(material["risk_labels"]),
                ))
            director_request = build_director_request(source, timeline, (), descriptors, capabilities)
            director_request["ratio"] = normalized["ratio"]
            director_request["user_direction"] = request.get("style_prompt", request.get("creation_mode", "ai_auto"))
            director_request["generate_missing_material"] = not descriptors
            if os.environ.get("AI_EDIT_V3_VISUAL_PROGRAM_ENABLED", "0") == "1":
                candidates = build_scene_candidates(timeline, descriptors, ratio=str(normalized["ratio"]), input_type=normalized["input_type"])
                placement_catalog = load_overlay_placement_catalog(self.renderer_root)
                visual_capabilities = visual_program_capabilities({
                    **load_visual_capability_catalog(self.renderer_root),
                    "overlay_placement_budgets": placement_catalog,
                    "output_ratio": str(normalized["ratio"]),
                })
                layout_ids = list(visual_capabilities["layout_capabilities"])
                visual_capabilities["layout_requirements"] = layout_requirements_for(layout_ids)
                visual_capabilities["material_binding_mode"] = "semantic_slots_only"
                visual_capabilities["max_required_material_slots"] = MAX_REQUIRED_MATERIAL_SLOTS
                visual_capabilities["max_total_material_slots"] = MAX_TOTAL_MATERIAL_SLOTS
                visual_capabilities["speaker_visibility_policy"] = copy.deepcopy(
                    SPEAKER_VISIBILITY_POLICY
                )
                visual_capabilities["scene_structure_policy"] = copy.deepcopy(
                    SCENE_STRUCTURE_POLICY
                )
                prompt_candidates = []
                for candidate_index, item in enumerate(candidates):
                    candidate = (
                        item.__dict__ if hasattr(item, "__dict__")
                        else {slot: getattr(item, slot) for slot in item.__slots__}
                    )
                    candidate = copy.deepcopy(candidate)
                    candidate["available_material_ids"] = []
                    candidate["allowed_layout_ids"] = allowed_layout_ids(
                        layout_ids,
                        speaker_available=bool(candidate.get("speaker_available")),
                        require_speaker=(
                            candidate_index == 0
                            and candidate.get("speaker_available") is True
                            and SPEAKER_VISIBILITY_POLICY["opening_requires_speaker"] is True
                        ),
                    )
                    prompt_candidates.append(candidate)
                safe_materials = []
                for material in director_request.get("current_materials", ()):
                    if not isinstance(material, Mapping):
                        raise ValueError("director_material_descriptor_invalid")
                    safe_materials.append({
                        key: copy.deepcopy(material[key])
                        for key in (
                            "upload_alias", "semantic", "subject_type", "composition",
                            "supported_ratios", "risk_labels",
                        )
                        if key in material
                    })
                decision_request = {
                    **director_request,
                    "current_materials": safe_materials,
                    "scene_candidates": prompt_candidates,
                    "capabilities": director_prompt_capabilities(visual_capabilities),
                    "material_resolution_policy": "prefer_current_upload_then_generate_required",
                }
                decision_request.pop("generate_missing_material", None)
                decision = generate_director_decision(SimpleNamespace(request=decision_request, timeline=timeline, candidates=candidates, capabilities=visual_capabilities, job_id=job_id, deadline_at=context.deadline_at), self.director)
                variation_seed = derive_variation_seed(job.get("request_sha256"), decision.decision_sha256, self.renderer.registry_sha256.removeprefix("sha256:"))
                plan = compile_edit_plan(decision.value, candidates=candidates, timeline={"duration_ms": timeline.duration_ms, "captions": [{"id": item.id, "start_ms": item.start_ms, "end_ms": item.end_ms, "text": item.text} for item in timeline.captions], "ratio": normalized["ratio"]}, materials=descriptors, capabilities=visual_capabilities, variation_seed=int(variation_seed[:8], 16))
                _write_json(root / "visual-program.json", {"theme_profile_id": decision.value["theme_profile_id"], "design_intent": decision.value["design_intent"], "variation_seed": variation_seed})
                generated = ValidatedPlan(plan, provider_request_id=decision.provider_request_id, raw_output_sha256=decision.raw_output_sha256)
            else:
                generated = generate_edit_plan(
                    SimpleNamespace(request=director_request, timeline=timeline, capabilities=capabilities, job_id=job_id, deadline_at=context.deadline_at),
                    self.director,
                )
            digest = _write_json(root / "plan.json", generated.value)
            save = getattr(self.store, "save_director_plan", None)
            if callable(save):
                save(context.claim, context.stage_attempt_id, generated, now_ms=int(time.time() * 1000))
            return StageOutcome(
                _NEXT[name],
                {
                    "plan_sha256": digest,
                    "material_descriptors_sha256": _sha(root / "material-descriptors.json"),
                    "provider_request_id": generated.provider_request_id,
                },
                input_sha,
            )
        if name == "resolving_materials":
            trusted_materials = self._frozen_bound_materials(job)
            descriptor_path = root / "material-descriptors.json"
            if not descriptor_path.exists():
                raise MaterialError("material_descriptor_artifact_missing")
            descriptors = self._validated_material_descriptor_document(
                _json(descriptor_path),
                trusted_materials,
                input_sha256=_material_descriptor_input_sha256(
                    str(job.get("owner_id") or ""), trusted_materials,
                ),
            )
            frozen = []
            for material, descriptor in zip(trusted_materials, descriptors, strict=True):
                frozen.append({
                    "material_id": material["material_id"],
                    "upload_alias": descriptor["upload_alias"],
                    "relative_path": material["relative_path"],
                    "mime_type": material["mime_type"],
                    "size_bytes": material["size_bytes"],
                    "sha256": material["sha256"],
                    "metadata": {
                        key: copy.deepcopy(descriptor[key])
                        for key in (
                            "semantic", "subject_type", "composition",
                            "supported_ratios", "risk_labels",
                        )
                    },
                })
            plan_path = root / "plan.json"
            plan = _json(plan_path) if plan_path.exists() else {}
            material_document = (
                bind_scene_materials(plan, frozen)
                if plan.get("visual_program_version") == "1.0"
                else {"items": frozen}
            )
            digest = _write_json(root / "materials.json", material_document)
            return StageOutcome(
                _NEXT[name],
                {"materials_sha256": digest, "material_count": len(material_document["items"])},
                input_sha,
            )
        if name == "generating_images":
            material_document = _json(root / "materials.json")
            items = list(material_document["items"])
            initial_item_count = len(items)
            plan = _json(root / "plan.json")
            visual_program = plan.get("visual_program_version") == "1.0"
            plan_materials = list(plan.get("materials") or ())
            required = (
                list(material_document.get("unresolved") or ())
                if visual_program
                else plan_materials[initial_item_count:]
            )
            provider_result = None
            generated_count = sum(1 for item in items if item.get("source") == "generated")
            for material_request in required:
                if material_request.get("priority", "required") != "required":
                    continue
                generated_count += 1
                index = generated_count
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
                generated_item = {
                    "material_id": f"generated_{index:02d}",
                    "relative_path": destination.relative_to(root).as_posix(),
                    "mime_type": "image/png",
                    "size_bytes": destination.stat().st_size,
                    "sha256": digest,
                    "width": image.width,
                    "height": image.height,
                    "source": "generated",
                    "object_key": object_key,
                }
                if visual_program:
                    generated_item.update({
                        "scene_id": material_request["scene_id"],
                        "slot_id": material_request["slot_id"],
                        "request_id": material_request["request_id"],
                        "semantic": material_request["semantic"],
                        "purpose": material_request["purpose"],
                        "priority": material_request["priority"],
                        "ratio": material_request["ratio"],
                        "reason": "required_slot_generated",
                    })
                    source_metadata = {
                        "source": "generated",
                        "sha256": digest,
                        "mime_type": "image/png",
                        "width": image.width,
                        "height": image.height,
                        "provider_request_id": getattr(provider_result, "request_id", None),
                    }
                    inspect_material = getattr(self.visual_inspector, "inspect_material", None)
                    if not callable(inspect_material):
                        _record_material_rejection(
                            root,
                            material_request=material_request,
                            cos_key=object_key,
                            source_metadata=source_metadata,
                            review={
                                "result": "fail",
                                "reason": "reviewer_unavailable",
                                "evidence": [{"semantic_match": False, "forbidden_subjects": []}],
                            },
                            cos=self.cos,
                        )
                        raise MaterialError("generated_material_reviewer_unavailable")
                    def call_material_reviewer() -> Mapping[str, Any]:
                        return inspect_material(
                            scene_id=material_request["scene_id"],
                            slot_id=material_request["slot_id"],
                            semantic=material_request["semantic"],
                            forbidden_subjects=(
                                "person", "face", "wrong_product", "wrong_store",
                                "fabricated_real_world_evidence",
                            ),
                            cos_key=object_key,
                            source_metadata=source_metadata,
                            deadline_at=context.deadline_at,
                        )
                    try:
                        real_receipts = (
                            hasattr(context, "claim")
                            and hasattr(context, "stage_attempt_id")
                            and callable(getattr(self.store, "record_provider_intent", None))
                            and callable(getattr(self.store, "get_provider_task_for_claim", None))
                            and callable(getattr(self.store, "claim_provider_submission", None))
                            and callable(getattr(self.store, "bind_provider_result", None))
                        )
                        if real_receipts:
                            review_request = _material_review_receipt_request(
                                scene_id=material_request["scene_id"],
                                slot_id=material_request["slot_id"],
                                semantic=material_request["semantic"],
                                forbidden_subjects=(
                                    "person", "face", "wrong_product", "wrong_store",
                                    "fabricated_real_world_evidence",
                                ),
                                cos_key=object_key,
                                source_metadata=source_metadata,
                            )
                            review = invoke_provider_once(
                                store=self.store,
                                context=context,
                                stage="generating_images",
                                provider="dashscope",
                                capability="material_review",
                                operation_key=(
                                    f"ai-edit-v3:{job_id}:material-review:"
                                    f"{material_request['scene_id']}:{material_request['slot_id']}"
                                ),
                                request_sha256=hashlib.sha256(
                                    canonical_json(review_request)
                                ).hexdigest(),
                                call=call_material_reviewer,
                                now_ms=round(time.time() * 1000),
                            )
                        else:
                            review = call_material_reviewer()
                    except Exception:
                        _record_material_rejection(
                            root,
                            material_request=material_request,
                            cos_key=object_key,
                            source_metadata=source_metadata,
                            review={
                                "result": "fail",
                                "reason": "reviewer_failed",
                                "evidence": [{"semantic_match": False, "forbidden_subjects": []}],
                            },
                            cos=self.cos,
                        )
                        raise
                    try:
                        normalized_review = validate_generated_material_review(review, required=False)
                    except MaterialError:
                        _record_material_rejection(
                            root,
                            material_request=material_request,
                            cos_key=object_key,
                            source_metadata=source_metadata,
                            review={
                                "result": "fail",
                                "reason": "review_schema_invalid",
                                "evidence": [{"semantic_match": False, "forbidden_subjects": []}],
                            },
                            cos=self.cos,
                        )
                        raise
                    if normalized_review["result"] != "pass":
                        _record_material_rejection(
                            root,
                            material_request=material_request,
                            cos_key=object_key,
                            source_metadata=source_metadata,
                            review=normalized_review,
                            cos=self.cos,
                        )
                        raise MaterialError("generated_required_material_review_failed")
                    generated_item["visual_review"] = normalized_review
                items.append(generated_item)
            output_document = {"items": items}
            if visual_program:
                output_document.update({"unresolved": [], "omitted": list(material_document.get("omitted") or ())})
            digest = _write_json(root / "materials.json", output_document)
            material_count = len(items)
            if visual_program:
                skipped = not required
                reason = (
                    "all_director_slots_resolved_from_user_materials"
                    if not required and material_document["items"]
                    else "optional_director_slots_omitted"
                    if not required and material_document.get("omitted")
                    else "missing_director_slots_generated"
                    if required
                    else "no_required_material_slots"
                )
            else:
                skipped = len(plan_materials) <= initial_item_count
                reason = (
                    "all_director_slots_resolved_from_user_materials"
                    if plan_materials and len(plan_materials) <= initial_item_count
                    else "missing_director_slots_generated"
                    if plan_materials
                    else "no_required_material_slots"
                )
            return StageOutcome(
                _NEXT[name],
                {
                    "skipped": skipped,
                    "reason": reason,
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
            receipt_methods = (
                "record_provider_intent",
                "get_provider_task_for_claim",
                "claim_provider_submission",
                "bind_provider_result",
            )
            provider_once = None
            if hasattr(context, "claim") and hasattr(context, "stage_attempt_id") and all(
                callable(getattr(self.store, method, None)) for method in receipt_methods
            ):
                def provider_once(**kwargs: Any) -> Mapping[str, Any]:
                    return invoke_provider_once(
                        store=self.store,
                        context=context,
                        stage="generating_audio",
                        now_ms=round(time.time() * 1000),
                        **kwargs,
                    )
            generated = generate_task_audio(
                job_id,
                audio_plan,
                self.audio_generator,
                self.cos,
                root,
                context,
                provider_once=provider_once,
            )
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
            visual_program = plan.get("visual_program_version") == "1.0"
            scene_slot_asset_ids: dict[tuple[str, str], str] = {}
            if visual_program:
                requested_slots = [
                    (str(scene["id"]), str(slot.get("layout_slot_id") or slot["id"]))
                    for scene in plan["scenes"]
                    for slot in scene.get("material_slots") or ()
                ]
                material_by_slot: dict[tuple[str, str], Mapping[str, Any]] = {}
                for material in material_items:
                    key = (str(material.get("scene_id") or ""), str(material.get("slot_id") or ""))
                    if key in requested_slots:
                        if key in material_by_slot:
                            raise ValueError("scene_material_binding_duplicate")
                        material_by_slot[key] = material
                selected_materials = [material_by_slot[key] for key in requested_slots if key in material_by_slot]
            else:
                maximum_assets = min(4, len(plan.get("materials") or ()), len(material_items))
                selected_materials = material_items[:maximum_assets]
            for index, material in enumerate(selected_materials, 1):
                source = root / material["relative_path"]
                target = input_root / "media" / f"material-{index:02d}{source.suffix.lower()}"
                shutil.copyfile(source, target)
                asset_id = f"material_{index:02d}"
                material_asset_ids.append(asset_id)
                if visual_program:
                    scene_slot_asset_ids[(str(material["scene_id"]), str(material["slot_id"]))] = asset_id
                material_assets.append({
                    "id": asset_id,
                    "kind": "image",
                    "path": target.relative_to(input_root).as_posix(),
                    "sha256": _sha(target),
                    "size_bytes": target.stat().st_size,
                })
            ratio = plan["ratio"]
            width, height = ((1920, 1080) if ratio == "16:9" else (1080, 1920))
            if visual_program:
                for scene in plan["scenes"]:
                    _validate_layout_source_requirements(scene, source_video=source_video)
                    _validate_layout_authoritative_content(scene, captions=plan["captions"])
            manifest = {
                "version": "2.0" if visual_program else "1.0", "schema_sha256": schema_sha256("render-manifest-v2.schema.json" if visual_program else "render-manifest-v1.schema.json"),
                "renderer_environment": self._release_environment(),
                "output_spec": {"ratio": ratio, "width": width, "height": height, "fps_num": 30, "fps_den": 1, "video_codec": "h264", "pixel_format": "yuv420p", "audio_codec": "aac", "sample_rate": 48000, "channels": 2},
                "duration_ms": plan["duration_ms"], "edit_plan_sha256": hashlib.sha256(canonical_json(plan)).hexdigest(),
                "registry_sha256": self.renderer.registry_sha256.removeprefix("sha256:"),
                "theme": plan["theme"], "seed": int(hashlib.sha256(job_id.encode()).hexdigest()[:8], 16) % 2147483648,
                "source_video": source_video,
                "source_segments": [{"id": item["id"], "source_path": segment_path, "sha256": segment_sha, "source_start_ms": item["source_start_ms"], "source_end_ms": item["source_end_ms"], "output_start_ms": item["output_start_ms"], "output_end_ms": item["output_end_ms"]} for item in plan["source_segments"]],
                "master_audio": {"path": "media/master.wav", "sha256": _sha(master_target), "size_bytes": master_target.stat().st_size, "duration_ms": master["duration_ms"], "sample_rate": 48000, "channels": 2},
                "assets": material_assets,
                "compositions": [{"id": f"composition_{index:03d}", "scene_id": scene["id"], "start_ms": scene["start_ms"], "end_ms": scene["end_ms"], "layout_id": scene["layout_id"], "layout_variant": scene["layout_variant"], "overlay_ids": scene["overlay_ids"], **({"overlay_instances": scene["overlay_instances"], "layout_slot_bindings": _layout_slot_bindings(scene, material_asset_ids, scene_slot_asset_ids), "authoritative_content": _freeze_overlay_authoritative_content(scene)} if visual_program else {}), "animations": scene["animations"], "transition": scene["transition"], "asset_ids": _scene_asset_ids(scene, material_asset_ids, scene_slot_asset_ids if visual_program else None)} for index, scene in enumerate(plan["scenes"], 1)],
                "captions": _render_captions(plan["captions"]),
            }
            if visual_program:
                visual = _json(root / "visual-program.json")
                manifest.update({
                    "theme_profile_id": visual["theme_profile_id"],
                    "design_intent": visual["design_intent"],
                    "variation_seed": visual["variation_seed"],
                    "design_tokens": _resolve_design_tokens(visual["theme_profile_id"], visual["design_intent"], visual["variation_seed"]),
                })
            if attempt > 1:
                previous_quality = _json(root / f"quality-{attempt - 1}.json")
                repair_instruction = _repair_instruction_from_quality(
                    manifest,
                    previous_quality,
                )
                manifest = _repair_render_manifest(
                    manifest,
                    repair_instruction,
                )
            frozen = freeze_render_manifest(
                manifest, input_root / "render-manifest.json", sandbox_root=input_root,
                overlay_placement_catalog=(load_overlay_placement_catalog(self.renderer_root) if visual_program else None),
            )
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
            snapshot_inputs = _verified_snapshot_inputs(
                output_root,
                render,
                report,
                duration_ms=int(manifest["duration_ms"]),
            )
            quality = run_blocking_quality(
                mux,
                manifest,
                report,
                owner_evidence=owner_evidence,
                visual_inspector=self.visual_inspector,
                snapshot_inputs=snapshot_inputs,
                deadline_at=context.deadline_at,
            )
            repair_payload = _quality_repair_payload(manifest, quality)
            payload = {"passed": quality.passed, "repairable_ids": list(quality.repairable_ids), **repair_payload, "report_sha256": quality.report_sha256, "final_relpath": final_path.relative_to(root).as_posix(), "final": {"relative_path": mux.relative_path, "sha256": mux.sha256, "duration_ms": mux.duration_ms, "video_codec": mux.video_codec, "audio_codec": mux.audio_codec, "width": mux.width, "height": mux.height, "fps_num": mux.fps_num, "fps_den": mux.fps_den, "sample_rate": mux.sample_rate, "channels": mux.channels, "audit": dict(mux.audit)}}
            digest = _write_json(root / f"quality-{attempt}.json", payload)
            if quality.passed:
                next_state = "staging_delivery"
            elif int(job.get("repair_count", 0)) == 0 and quality.can_repair:
                next_state = "repair_planning"
            else:
                next_state = "failed"
            return StageOutcome(next_state, {"quality_sha256": digest, "passed": quality.passed, "repairable_ids": list(quality.repairable_ids), "can_repair": quality.can_repair, "repair_instruction_sha256": repair_payload["repair_instruction_sha256"]}, input_sha)
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
