from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, Mapping, Sequence

from .director import ValidatedPlan, extract_single_json
from .providers.base import ProviderResult, SubmissionUnknown
from .contracts import request_fingerprint


_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_WORD_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]{1,8}", re.IGNORECASE)
_PRIVATE_LOCATION_RE = re.compile(
    r"https?://|(?:^|[?&\s])(?:q-sign-[a-z0-9-]+|x-cos-[a-z0-9-]+|signature|credential|security-token)\s*=",
    re.IGNORECASE,
)


class MaterialError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class MaterialDescriptor:
    material_id: str
    semantic: tuple[str, ...]
    subject_type: str
    composition: str
    supported_ratios: tuple[str, ...]
    risk_labels: tuple[str, ...]
    sha256: str


@dataclass(frozen=True)
class ResolvedMaterial:
    slot_id: str
    source: Literal["current_upload", "generated", "omitted_optional"] | None
    material_id: str | None
    cos_key: str | None
    match_score: float | None
    reason: str
    status: str
    score_components: tuple[tuple[str, float], ...] = ()


@dataclass(frozen=True)
class ResolutionDraft:
    slots: Mapping[str, ResolvedMaterial]

    def __post_init__(self) -> None:
        object.__setattr__(self, "slots", MappingProxyType(dict(self.slots)))


def bind_scene_materials(
    plan: Mapping[str, Any],
    current_items: Sequence[Mapping[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Freeze visual-program materials by scene and semantic slot.

    Only files already bound to the current request are candidates.  A file may
    be reused only for the same semantic requirement; unresolved optional slots
    are recorded but never sent to the image provider.
    """

    if plan.get("visual_program_version") != "1.0":
        raise MaterialError("scene_material_plan_invalid")
    if len(current_items) > 10:
        raise MaterialError("material_asset_ids_invalid")
    by_id: dict[str, dict[str, Any]] = {}
    for raw in current_items:
        item = dict(raw)
        material_id = item.get("material_id")
        metadata = item.get("metadata")
        if (
            not isinstance(material_id, str)
            or not material_id
            or material_id in by_id
            or not isinstance(metadata, Mapping)
        ):
            raise MaterialError("scene_material_upload_invalid")
        item["metadata"] = dict(metadata)
        by_id[material_id] = item

    bound: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    omitted: list[dict[str, Any]] = []
    reused_semantics: dict[str, frozenset[str]] = {}
    scenes = plan.get("scenes")
    if not isinstance(scenes, list):
        raise MaterialError("scene_material_plan_invalid")
    for scene in scenes:
        if not isinstance(scene, Mapping) or not isinstance(scene.get("id"), str):
            raise MaterialError("scene_material_plan_invalid")
        slots = scene.get("material_slots")
        if not isinstance(slots, list) or len(slots) > 4:
            raise MaterialError("scene_material_slots_invalid")
        for raw_slot in slots:
            if not isinstance(raw_slot, Mapping):
                raise MaterialError("scene_material_slot_invalid")
            request_id, semantic, required, purpose, ratio = _slot_values(raw_slot)
            layout_slot_id = raw_slot.get("layout_slot_id", request_id)
            if not isinstance(layout_slot_id, str) or not layout_slot_id:
                raise MaterialError("scene_material_slot_invalid")
            selected = by_id.get(request_id)
            if selected is None:
                ranked: list[tuple[float, str, dict[str, Any]]] = []
                for material_id, candidate in by_id.items():
                    metadata = candidate["metadata"]
                    candidate_semantic = _semantic_tokens(metadata.get("semantic", ()))
                    overlap = semantic & candidate_semantic
                    previous = reused_semantics.get(material_id)
                    if not overlap or (previous is not None and previous != semantic):
                        continue
                    ranked.append((-(len(overlap) / max(1, len(semantic))), material_id, candidate))
                if ranked:
                    selected = sorted(ranked, key=lambda row: (row[0], row[1]))[0][2]
            if selected is not None:
                material_id = str(selected["material_id"])
                previous = reused_semantics.get(material_id)
                if previous is not None and previous != semantic:
                    raise MaterialError("scene_material_cross_semantic_reuse")
                reused_semantics[material_id] = semantic
                bound.append({
                    **{key: value for key, value in selected.items() if key != "metadata"},
                    "scene_id": scene["id"],
                    "slot_id": layout_slot_id,
                    "request_id": request_id,
                    "semantic": str(raw_slot.get("semantic")),
                    "purpose": purpose,
                    "priority": "required" if required else "optional",
                    "ratio": ratio,
                    "source": "current_upload",
                    "reason": "current_task_semantic_match",
                })
                continue
            pending = {
                "scene_id": scene["id"],
                "slot_id": layout_slot_id,
                "request_id": request_id,
                "semantic": str(raw_slot.get("semantic")),
                "purpose": purpose,
                "priority": "required" if required else "optional",
                "ratio": ratio,
                "reason": "no_qualified_current_upload",
            }
            if required:
                unresolved.append(pending)
            else:
                omitted.append({**pending, "source": "omitted_optional", "status": "omitted_optional"})
    if len(unresolved) > 6:
        raise MaterialError("generated_material_limit_exceeded")
    return {"items": bound, "unresolved": unresolved, "omitted": omitted}


def validate_generated_material_review(
    raw: Any,
    *,
    required: bool,
) -> dict[str, Any]:
    """Accept only an independent, auditable visual review result."""

    if not isinstance(raw, Mapping) or set(raw) != {"result", "reason", "evidence"}:
        raise MaterialError("generated_material_review_invalid")
    result = raw.get("result")
    reason = raw.get("reason")
    evidence = raw.get("evidence")
    normalized_reason = reason.strip() if isinstance(reason, str) else None
    if (
        result not in {"pass", "fail"}
        or not normalized_reason
        or len(normalized_reason) > 500
        or _PRIVATE_LOCATION_RE.search(normalized_reason) is not None
        or not isinstance(evidence, list)
        or not evidence
        or len(evidence) > 8
        or any(not isinstance(item, Mapping) for item in evidence)
    ):
        raise MaterialError("generated_material_review_invalid")
    allowed_forbidden = {
        "person",
        "face",
        "wrong_product",
        "wrong_store",
        "fabricated_real_world_evidence",
    }
    normalized_evidence: list[dict[str, Any]] = []
    for item in evidence:
        if set(item) != {"semantic_match", "forbidden_subjects"}:
            raise MaterialError("generated_material_review_invalid")
        semantic_match_value = item.get("semantic_match")
        values = item.get("forbidden_subjects")
        if (
            not isinstance(semantic_match_value, bool)
            or not isinstance(values, list)
            or len(values) > len(allowed_forbidden)
            or any(not isinstance(value, str) or value not in allowed_forbidden for value in values)
            or len(set(values)) != len(values)
        ):
            raise MaterialError("generated_material_review_invalid")
        normalized_evidence.append({
            "semantic_match": semantic_match_value,
            "forbidden_subjects": list(values),
        })
    semantic_match = any(item["semantic_match"] for item in normalized_evidence)
    forbidden = [
        value
        for item in normalized_evidence
        for value in item["forbidden_subjects"]
    ]
    passed = result == "pass" and semantic_match and not forbidden
    normalized = {
        "result": "pass" if passed else "fail",
        "reason": normalized_reason,
        "evidence": normalized_evidence,
    }
    if required and not passed:
        raise MaterialError("generated_required_material_review_failed")
    return normalized


def _semantic_tokens(value: Any) -> frozenset[str]:
    values = value if isinstance(value, (list, tuple)) else (value,)
    tokens: set[str] = set()
    for item in values:
        if not isinstance(item, str):
            raise MaterialError("material_semantic_invalid")
        lowered = item.casefold().strip()
        if lowered:
            tokens.add(lowered)
            tokens.update(match.group(0) for match in _WORD_RE.finditer(lowered))
    return frozenset(tokens)


def _slot_values(slot: Mapping[str, Any]) -> tuple[str, frozenset[str], bool, str, str]:
    slot_id = slot.get("id")
    if not isinstance(slot_id, str) or not slot_id:
        raise MaterialError("material_slot_invalid")
    semantics = _semantic_tokens(slot.get("semantic"))
    required = slot.get("required") is True or slot.get("priority") == "required"
    purpose = slot.get("purpose", "context")
    ratio = slot.get("ratio", "auto")
    if not isinstance(purpose, str) or not isinstance(ratio, str):
        raise MaterialError("material_slot_invalid")
    return slot_id, semantics, required, purpose, ratio


def resolve_uploaded_materials(
    plan: ValidatedPlan | Any,
    descriptors: Sequence[MaterialDescriptor],
) -> ResolutionDraft:
    descriptor_ids = [item.material_id for item in descriptors]
    if len(descriptor_ids) != len(set(descriptor_ids)):
        raise MaterialError("material_descriptor_duplicate")
    slots: dict[str, ResolvedMaterial] = {}
    for slot in plan.material_slots:
        slot_id, required_semantic, required, purpose, ratio = _slot_values(slot)
        candidates: list[tuple[float, str, MaterialDescriptor, tuple[tuple[str, float], ...]]] = []
        for descriptor in descriptors:
            semantic = _semantic_tokens(descriptor.semantic)
            intersection = required_semantic & semantic
            if not intersection or descriptor.risk_labels:
                continue
            semantic_score = len(intersection) / max(1, len(required_semantic))
            subject_score = 0.15 if descriptor.subject_type == purpose else 0.0
            ratio_score = 0.10 if ratio == "auto" or ratio in descriptor.supported_ratios else 0.0
            score = round(min(1.0, 0.75 * semantic_score + subject_score + ratio_score), 4)
            components = (
                ("semantic", round(semantic_score, 4)),
                ("subject", subject_score),
                ("ratio", ratio_score),
            )
            candidates.append((-score, descriptor.material_id, descriptor, components))
        if candidates:
            _, _, match, components = sorted(candidates)[0]
            score = -sorted(candidates)[0][0]
            slots[slot_id] = ResolvedMaterial(
                slot_id,
                "current_upload",
                match.material_id,
                None,
                score,
                "semantic_current_task_match",
                "resolved",
                components,
            )
        else:
            slots[slot_id] = ResolvedMaterial(
                slot_id,
                None if required else "omitted_optional",
                None,
                None,
                None,
                "no_relevant_current_image",
                "generation_required" if required else "omitted_optional",
            )
    return ResolutionDraft(slots)


def _job_material_ids(job: Mapping[str, Any]) -> tuple[str, tuple[str, ...]]:
    owner = job.get("owner_id", job.get("owner"))
    values = job.get("material_asset_ids")
    if values is None:
        normalized = job.get("normalized_request_json")
        if isinstance(normalized, Mapping):
            values = normalized.get("material_asset_ids", [])
        else:
            values = []
    if not isinstance(owner, str) or not owner:
        raise MaterialError("material_owner_invalid")
    if (
        not isinstance(values, (list, tuple))
        or len(values) > 10
        or any(not isinstance(item, str) or not item for item in values)
        or len(set(values)) != len(values)
    ):
        raise MaterialError("material_asset_ids_invalid")
    return owner, tuple(values)


def _provider_descriptors(result: Any) -> Sequence[Mapping[str, Any]]:
    if isinstance(result, ProviderResult):
        content = result.payload.get("content")
        payload = extract_single_json(content) if isinstance(content, (str, bytes)) else result.payload
    elif isinstance(result, Mapping):
        payload = result
    else:
        payload = getattr(result, "payload", None)
    descriptors = payload.get("descriptors") if isinstance(payload, Mapping) else None
    if not isinstance(descriptors, (list, tuple)):
        raise MaterialError("material_analysis_invalid")
    return descriptors


def _normalize_descriptor(raw: Mapping[str, Any]) -> MaterialDescriptor:
    try:
        material_id = raw["material_id"]
        semantic = raw["semantic"]
        subject_type = raw["subject_type"]
        composition = raw["composition"]
        ratios = raw["supported_ratios"]
        risks = raw["risk_labels"]
        sha256 = raw["sha256"]
    except (KeyError, TypeError) as exc:
        raise MaterialError("material_analysis_invalid") from exc
    if (
        not isinstance(material_id, str)
        or not material_id
        or not isinstance(semantic, (list, tuple))
        or not semantic
        or any(not isinstance(item, str) or not item for item in semantic)
        or not isinstance(subject_type, str)
        or not subject_type
        or not isinstance(composition, str)
        or not composition
        or not isinstance(ratios, (list, tuple))
        or any(item not in {"16:9", "9:16", "1:1"} for item in ratios)
        or not isinstance(risks, (list, tuple))
        or any(not isinstance(item, str) for item in risks)
        or not isinstance(sha256, str)
        or _SHA256_RE.fullmatch(sha256) is None
    ):
        raise MaterialError("material_analysis_invalid")
    return MaterialDescriptor(
        material_id,
        tuple(semantic),
        subject_type,
        composition,
        tuple(ratios),
        tuple(risks),
        sha256,
    )


def analyze_current_images(
    job: Mapping[str, Any], context: Any, provider: Any
) -> tuple[MaterialDescriptor, ...]:
    owner, material_ids = _job_material_ids(job)
    if not material_ids:
        return ()
    repository = getattr(context, "material_repository", None)
    if repository is None or not callable(getattr(repository, "get_images_for_owner", None)):
        raise MaterialError("material_repository_unavailable")
    records = repository.get_images_for_owner(owner, material_ids)
    if not isinstance(records, (list, tuple)) or len(records) != len(material_ids):
        raise MaterialError("material_scope_invalid")
    by_id: dict[str, Mapping[str, Any]] = {}
    for record in records:
        if not isinstance(record, Mapping):
            raise MaterialError("material_scope_invalid")
        asset_id = record.get("asset_id")
        if (
            asset_id not in material_ids
            or asset_id in by_id
            or record.get("owner_id") != owner
            or record.get("media_type") != "image"
            or record.get("status") != "completed"
        ):
            raise MaterialError("material_scope_invalid")
        width = record.get("thumbnail_width")
        height = record.get("thumbnail_height")
        if (
            isinstance(width, bool)
            or not isinstance(width, int)
            or width < 1
            or width > 768
            or isinstance(height, bool)
            or not isinstance(height, int)
            or height < 1
            or height > 768
            or not isinstance(record.get("thumbnail"), str)
            or not isinstance(record.get("sha256"), str)
            or _SHA256_RE.fullmatch(record["sha256"]) is None
        ):
            raise MaterialError("material_thumbnail_invalid")
        by_id[asset_id] = record
    if set(by_id) != set(material_ids):
        raise MaterialError("material_scope_invalid")

    descriptors: list[MaterialDescriptor] = []
    for batch_index in range(0, len(material_ids), 5):
        batch_ids = material_ids[batch_index : batch_index + 5]
        request_images = [
            {
                "material_id": asset_id,
                "thumbnail": by_id[asset_id]["thumbnail"],
                "thumbnail_width": by_id[asset_id]["thumbnail_width"],
                "thumbnail_height": by_id[asset_id]["thumbnail_height"],
                "sha256": by_id[asset_id]["sha256"],
            }
            for asset_id in batch_ids
        ]
        result = provider.analyze_images(
            {"images": request_images, "output": "material-descriptor-v1"},
            idempotency_key=f"ai-edit-v3:{context.job_id}:materials:{batch_index // 5}",
            deadline_at=context.deadline_at,
        )
        normalized = tuple(_normalize_descriptor(item) for item in _provider_descriptors(result))
        if {item.material_id for item in normalized} != set(batch_ids):
            raise MaterialError("material_analysis_scope_invalid")
        for item in normalized:
            if item.sha256 != by_id[item.material_id]["sha256"]:
                raise MaterialError("material_analysis_identity_invalid")
        descriptors.extend(normalized)
    return tuple(sorted(descriptors, key=lambda item: material_ids.index(item.material_id)))


def _result_field(result: Any, field: str) -> Any:
    if hasattr(result, field):
        return getattr(result, field)
    payload = getattr(result, "payload", None)
    if isinstance(payload, Mapping):
        return payload.get(field)
    if isinstance(result, Mapping):
        return result.get(field)
    return None


def _safe_generation_semantic(value: Any, purpose: str) -> tuple[str, ...]:
    values = value if isinstance(value, (list, tuple)) else (value,)
    if any(not isinstance(item, str) or not item.strip() for item in values):
        raise MaterialError("generated_image_semantic_invalid")
    normalized = tuple(item.strip() for item in values)
    lowered = " ".join(normalized).casefold()
    forbidden = (
        "http://",
        "https://",
        "file://",
        "ignore previous",
        "system prompt",
        "api key",
        "password",
        "真实客户",
        "销售业绩",
        "产品功效",
        "证明",
    )
    if any(token in lowered for token in forbidden):
        raise MaterialError("generated_image_semantic_unsafe")
    if purpose == "product" or any(token in lowered for token in ("brand", "品牌", "包装", "product package")):
        raise MaterialError("generated_product_fidelity_forbidden")
    return normalized


def _generation_result(
    result: Any,
    *,
    job_id: str,
    slot_id: str,
    environment: str,
) -> tuple[str, str, str]:
    request_id = _result_field(result, "request_id")
    asset_id = _result_field(result, "asset_id")
    cos_key = _result_field(result, "cos_key")
    decoded = _result_field(result, "decoded")
    width = _result_field(result, "width")
    height = _result_field(result, "height")
    prefix = f"{environment}/ai-edit-v3/jobs/{job_id}/generated/"
    if (
        not isinstance(request_id, str)
        or not request_id
        or not isinstance(asset_id, str)
        or not asset_id
        or not isinstance(cos_key, str)
        or not cos_key.startswith(prefix)
        or not cos_key.endswith((".webp", ".png", ".jpg", ".jpeg"))
        or any(token in cos_key for token in ("?", "#", "://", "..", "\\"))
        or decoded is not True
        or isinstance(width, bool)
        or not isinstance(width, int)
        or width < 256
        or width > 4096
        or isinstance(height, bool)
        or not isinstance(height, int)
        or height < 256
        or height > 4096
    ):
        raise MaterialError("generated_image_scope_invalid")
    return request_id, asset_id, cos_key


def generate_required_materials(
    job: Mapping[str, Any],
    plan: ValidatedPlan | Any,
    draft: ResolutionDraft,
    provider: Any,
    context: Any,
) -> dict[str, ResolvedMaterial]:
    job_id = job.get("id", job.get("job_id"))
    if not isinstance(job_id, str) or not job_id:
        raise MaterialError("material_job_invalid")
    environment = getattr(context, "environment", "test")
    if environment not in {"test", "production"}:
        raise MaterialError("material_environment_invalid")
    plan_slots = {slot["id"]: slot for slot in plan.material_slots}
    if set(draft.slots) - set(plan_slots):
        raise MaterialError("material_slot_unknown")
    theme_value = getattr(plan, "value", {}).get("theme", {})
    if not isinstance(theme_value, Mapping):
        raise MaterialError("material_theme_invalid")
    theme = {
        key: value
        for key, value in theme_value.items()
        if key in {"palette_id", "typography_id", "density", "motion_energy", "image_fit"}
        and isinstance(value, str)
    }
    resolved = dict(draft.slots)
    for slot_id, current in draft.slots.items():
        if current.status != "generation_required":
            continue
        slot = plan_slots[slot_id]
        purpose = slot.get("purpose", "context")
        ratio = slot.get("ratio", "auto")
        if not isinstance(purpose, str) or not isinstance(ratio, str):
            raise MaterialError("generated_image_slot_invalid")
        semantic = _safe_generation_semantic(slot.get("semantic"), purpose)
        request = {
            "slot_id": slot_id,
            "semantic": list(semantic),
            "purpose": purpose,
            "ratio": ratio,
            "theme": theme,
            "fact_boundary": "generic_visual_only_no_real_customer_product_proof_or_branded_packaging",
        }
        operation_key = f"ai-edit-v3:{job_id}:image:{slot_id}"
        provider_tasks = getattr(context, "provider_tasks", None)
        existing = None
        if provider_tasks is not None:
            existing = provider_tasks.record_intent(
                operation_key=operation_key,
                request_sha256=request_fingerprint(request),
                provider="site_image_generation",
                capability="image_generation",
                context=context,
            )
        external_id = existing.get("external_id") if isinstance(existing, Mapping) else None
        if isinstance(external_id, str) and external_id:
            result = provider.query(external_id, deadline_at=context.deadline_at)
        else:
            try:
                result = provider.submit(
                    request,
                    idempotency_key=operation_key,
                    deadline_at=context.deadline_at,
                )
            except SubmissionUnknown as exc:
                if provider_tasks is not None and callable(getattr(provider_tasks, "mark_unknown", None)):
                    provider_tasks.mark_unknown(operation_key, reason_code=exc.reason_code, context=context)
                raise
        request_id, asset_id, cos_key = _generation_result(
            result,
            job_id=job_id,
            slot_id=slot_id,
            environment=environment,
        )
        if provider_tasks is not None and not external_id:
            provider_tasks.bind_result(
                operation_key=operation_key,
                external_id=request_id,
                result={
                    "request_id": request_id,
                    "asset_id": asset_id,
                    "cos_key": cos_key,
                    "status": "completed",
                },
                context=context,
            )
        resolved[slot_id] = ResolvedMaterial(
            slot_id=slot_id,
            source="generated",
            material_id=asset_id,
            cos_key=cos_key,
            match_score=None,
            reason="required_slot_generated",
            status="resolved",
        )
    return resolved
