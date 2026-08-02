from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, Mapping, Sequence

from .director import ValidatedPlan, extract_single_json
from .providers.base import ProviderResult


_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_WORD_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]{1,8}", re.IGNORECASE)


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
