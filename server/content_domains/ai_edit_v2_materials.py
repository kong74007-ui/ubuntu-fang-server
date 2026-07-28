"""Resolve provider-neutral edit-plan slots to private internal assets."""

from __future__ import annotations

import copy
import hashlib
from typing import Any

from .ai_edit_v2_providers.base import ProviderResult


SOURCE_PRIORITY = ("current_upload", "user_history", "platform_public")
_SAFE_ASSET_FIELDS = {
    "asset_id",
    "cos_key",
    "kind",
    "width",
    "height",
    "content_type",
    "mime_type",
    "size_bytes",
    "etag",
}


class MaterialResolutionError(RuntimeError):
    def __init__(self, code: str, detail: str | None = None):
        self.code = code
        self.detail = detail or code
        super().__init__(self.detail)


def resolve_materials(
    job_id: str,
    plan: dict[str, Any],
    repositories: Any,
    image_provider: Any,
) -> dict[str, Any]:
    """Resolve every material slot without allowing provider URLs into the plan."""

    owner = _owner_for_job(repositories, job_id)
    slots = _slot_specs(plan)
    required_assets = _required_materials(repositories, job_id)
    required_ids = {
        str(item.get("asset_id"))
        for item in required_assets
        if item.get("asset_id") is not None
    }
    used_required: set[str] = set()
    records: list[dict[str, Any]] = []
    resolved: dict[str, dict[str, Any]] = {}
    degraded = False

    for slot in slots:
        source_candidates: dict[str, list[dict[str, Any]]] = {}
        for source in SOURCE_PRIORITY:
            candidates = _search(repositories, source, job_id, slot)
            if source == "current_upload":
                known = {str(item.get("asset_id")) for item in candidates}
                candidates.extend(
                    copy.deepcopy(item)
                    for item in required_assets
                    if str(item.get("asset_id")) not in known
                )
            source_candidates[source] = candidates

        qualified_by_source: dict[str, list[dict[str, Any]]] = {}
        invalid_required_ids: set[str] = set()
        qualified_required_ids: set[str] = set()
        protected_real_product_present = False
        seen_assets: set[str] = set()
        for source in SOURCE_PRIORITY:
            qualified: list[dict[str, Any]] = []
            for candidate in source_candidates[source]:
                asset_id = str(candidate.get("asset_id") or "")
                required = bool(candidate.get("required")) or asset_id in required_ids
                exclusion = _exclusion_code(
                    candidate,
                    source=source,
                    owner=owner,
                    job_id=job_id,
                    ratio=slot["ratio"],
                    seen_assets=seen_assets,
                )
                records.append(
                    _resolution_record(
                        slot,
                        source,
                        candidate,
                        required=required,
                        exclusion_code=exclusion or "not_selected",
                    )
                )
                if exclusion is None:
                    qualified.append({**candidate, "required": required})
                    if required and candidate.get("is_real_product"):
                        protected_real_product_present = True
                    if required:
                        qualified_required_ids.add(asset_id)
                elif required:
                    invalid_required_ids.add(asset_id)
            qualified_by_source[source] = qualified

        if invalid_required_ids - qualified_required_ids:
            _persist(
                repositories,
                job_id,
                records,
                status="failed",
                error_code="required_material_unavailable",
            )
            raise MaterialResolutionError("required_material_unavailable")
        chosen = _choose_candidate(qualified_by_source, used_required)
        if chosen is not None:
            source, candidate = chosen
            asset_id = str(candidate["asset_id"])
            if candidate.get("required"):
                used_required.add(asset_id)
            _mark_selected(records, slot["id"], source, asset_id)
            resolved[slot["id"]] = {
                **_safe_asset(candidate),
                "source": source,
                "required": bool(candidate.get("required")),
            }
            continue

        slot_required = bool(required_ids - used_required)
        if slot_required or protected_real_product_present:
            _persist(
                repositories,
                job_id,
                records,
                status="failed",
                error_code="required_material_unavailable",
            )
            raise MaterialResolutionError("required_material_unavailable")

        generation_slot = {**slot, "required": False}
        try:
            generated = image_provider.generate(
                generation_slot, f"{job_id}:material:{slot['id']}"
            )
            payload = _generated_payload(generated, owner=owner, job_id=job_id)
        except MaterialResolutionError as exc:
            _persist(
                repositories,
                job_id,
                records,
                status="failed",
                error_code=exc.code,
            )
            raise
        except Exception:
            degraded = True
            records.append(
                _resolution_record(
                    slot,
                    "gpt_image",
                    {},
                    required=False,
                    exclusion_code="image_generation_failed",
                )
            )
            continue
        records.append(
            _resolution_record(
                slot, "gpt_image", payload, required=False, exclusion_code=None
            )
        )
        resolved[slot["id"]] = {
            **_safe_asset(payload),
            "source": "gpt_image",
            "required": False,
        }

    unused = required_ids - used_required
    if unused:
        _persist(
            repositories,
            job_id,
            records,
            status="failed",
            error_code="required_material_unused",
        )
        raise MaterialResolutionError("required_material_unused")
    _persist(repositories, job_id, records, status="succeeded")

    result = copy.deepcopy(plan)
    result["materials"] = resolved
    result["material_resolution_status"] = (
        "image_generation_degraded" if degraded else "resolved"
    )
    return result


def _slot_specs(plan: dict[str, Any]) -> list[dict[str, Any]]:
    ratio = plan.get("aspect_ratio")
    if ratio == "16:9":
        dimensions = {"width": 1920, "height": 1080}
    elif ratio == "9:16":
        dimensions = {"width": 1080, "height": 1920}
    else:
        raise MaterialResolutionError("material_plan_invalid")
    slots: dict[str, dict[str, Any]] = {}
    scenes = plan.get("scenes")
    if not isinstance(scenes, list):
        raise MaterialResolutionError("material_plan_invalid")
    for scene in scenes:
        if not isinstance(scene, dict):
            raise MaterialResolutionError("material_plan_invalid")
        for slot_id in scene.get("material_slots", []):
            if not isinstance(slot_id, str) or not slot_id:
                raise MaterialResolutionError("material_plan_invalid")
            existing = slots.get(slot_id)
            if existing is None:
                slots[slot_id] = {
                    "id": slot_id,
                    "semantic_query": str(
                        scene.get("headline") or scene.get("intent") or slot_id
                    ),
                    "time_range": {
                        "start_ms": int(scene["start_ms"]),
                        "end_ms": int(scene["end_ms"]),
                    },
                    "ratio": ratio,
                    "dimensions": dict(dimensions),
                }
            else:
                existing["time_range"]["start_ms"] = min(
                    existing["time_range"]["start_ms"], int(scene["start_ms"])
                )
                existing["time_range"]["end_ms"] = max(
                    existing["time_range"]["end_ms"], int(scene["end_ms"])
                )
    return list(slots.values())


def _owner_for_job(repositories: Any, job_id: str) -> str:
    if hasattr(repositories, "owner_for_job"):
        owner = repositories.owner_for_job(job_id)
    elif isinstance(repositories, dict):
        owner = repositories.get("owner")
    else:
        owner = None
    if not isinstance(owner, str) or not owner:
        raise MaterialResolutionError("material_job_scope_invalid")
    return owner


def _required_materials(repositories: Any, job_id: str) -> list[dict[str, Any]]:
    if hasattr(repositories, "required_materials"):
        value = repositories.required_materials(job_id)
    elif isinstance(repositories, dict):
        value = repositories.get("required_materials", [])
        value = value(job_id) if callable(value) else value
    else:
        value = []
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise MaterialResolutionError("material_repository_invalid")
    return copy.deepcopy(value)


def _search(
    repositories: Any, source: str, job_id: str, slot: dict[str, Any]
) -> list[dict[str, Any]]:
    if hasattr(repositories, "search"):
        value = repositories.search(source, job_id, copy.deepcopy(slot))
    elif isinstance(repositories, dict):
        repository = repositories.get(source, [])
        if hasattr(repository, "search"):
            value = repository.search(job_id, copy.deepcopy(slot))
        elif callable(repository):
            value = repository(job_id, copy.deepcopy(slot))
        else:
            value = repository
    else:
        raise MaterialResolutionError("material_repository_invalid")
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise MaterialResolutionError("material_repository_invalid")
    return copy.deepcopy(value)


def _exclusion_code(
    candidate: dict[str, Any],
    *,
    source: str,
    owner: str,
    job_id: str,
    ratio: str,
    seen_assets: set[str],
) -> str | None:
    asset_id = str(candidate.get("asset_id") or "")
    cos_key = candidate.get("cos_key")
    if not asset_id or not isinstance(cos_key, str) or not cos_key:
        return "invalid_asset"
    if source == "current_upload" and candidate.get("job_id") != job_id:
        return "job_scope_mismatch"
    if source in {"current_upload", "user_history"} and candidate.get("owner") != owner:
        return "owner_scope_mismatch"
    if source in {"current_upload", "user_history"}:
        owner_hash = hashlib.sha256(owner.encode("utf-8")).hexdigest()[:16]
        expected_prefix = f"ai-edit-v2/{owner_hash}/"
        if source == "current_upload":
            expected_prefix += f"{job_id}/"
        if not cos_key.startswith(expected_prefix):
            return "cos_scope_mismatch"
    if asset_id in seen_assets or candidate.get("duplicate") or candidate.get("duplicate_of"):
        return "duplicate"
    seen_assets.add(asset_id)
    if candidate.get("blurred"):
        return "blurred"
    if candidate.get("relevant") is False:
        return "irrelevant"
    if not _ratio_matches(candidate, ratio):
        return "invalid_ratio"
    return None


def _ratio_matches(candidate: dict[str, Any], ratio: str) -> bool:
    declared = candidate.get("ratio") or candidate.get("aspect_ratio")
    if declared is not None:
        return declared == ratio
    try:
        width = int(candidate["width"])
        height = int(candidate["height"])
        expected = 16 / 9 if ratio == "16:9" else 9 / 16
        return width > 0 and height > 0 and abs(width / height - expected) <= 0.05
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return False


def _choose_candidate(
    by_source: dict[str, list[dict[str, Any]]], used_required: set[str]
) -> tuple[str, dict[str, Any]] | None:
    for source in SOURCE_PRIORITY:
        candidates = by_source[source]
        if not candidates:
            continue
        unused_required = [
            item
            for item in candidates
            if item.get("required") and str(item.get("asset_id")) not in used_required
        ]
        pool = unused_required or candidates
        return source, max(pool, key=lambda item: float(item.get("score") or 0))
    return None


def _resolution_record(
    slot: dict[str, Any],
    source: str,
    candidate: dict[str, Any],
    *,
    required: bool,
    exclusion_code: str | None,
) -> dict[str, Any]:
    score = candidate.get("score")
    return {
        "slot_id": slot["id"],
        "semantic_query": slot["semantic_query"],
        "time_range": copy.deepcopy(slot["time_range"]),
        "ratio": slot["ratio"],
        "dimensions": copy.deepcopy(slot["dimensions"]),
        "source": source,
        "asset_id": candidate.get("asset_id"),
        "cos_key": (
            None
            if exclusion_code in {
                "job_scope_mismatch",
                "owner_scope_mismatch",
                "cos_scope_mismatch",
            }
            else candidate.get("cos_key")
        ),
        "required": bool(required),
        "selected_score": float(score) if score is not None else None,
        "exclusion_code": exclusion_code,
    }


def _mark_selected(
    records: list[dict[str, Any]], slot_id: str, source: str, asset_id: str
) -> None:
    for record in reversed(records):
        if (
            record["slot_id"] == slot_id
            and record["source"] == source
            and str(record.get("asset_id")) == asset_id
        ):
            record["exclusion_code"] = None
            return


def _generated_payload(
    result: Any, *, owner: str, job_id: str
) -> dict[str, Any]:
    if not isinstance(result, ProviderResult) or result.capability != "image_generation":
        raise MaterialResolutionError("image_generation_result_invalid")
    payload = result.payload
    if not isinstance(payload, dict) or set(payload) - _SAFE_ASSET_FIELDS:
        raise MaterialResolutionError("image_generation_result_invalid")
    if payload.get("asset_id") is None or not isinstance(payload.get("cos_key"), str):
        raise MaterialResolutionError("image_generation_result_invalid")
    owner_hash = hashlib.sha256(owner.encode("utf-8")).hexdigest()[:16]
    expected_prefix = f"ai-edit-v2/{owner_hash}/{job_id}/generated/"
    if not payload["cos_key"].startswith(expected_prefix):
        raise MaterialResolutionError("image_generation_result_invalid")
    return copy.deepcopy(payload)


def _safe_asset(candidate: dict[str, Any]) -> dict[str, Any]:
    return {key: copy.deepcopy(value) for key, value in candidate.items() if key in _SAFE_ASSET_FIELDS}


def _persist(
    repositories: Any,
    job_id: str,
    records: list[dict[str, Any]],
    *,
    status: str,
    error_code: str | None = None,
) -> None:
    safe_records = copy.deepcopy(records)
    if hasattr(repositories, "save_resolution_records"):
        repositories.save_resolution_records(
            job_id, safe_records, status=status, error_code=error_code
        )
    elif isinstance(repositories, dict) and callable(repositories.get("save_resolution_records")):
        repositories["save_resolution_records"](
            job_id, safe_records, status=status, error_code=error_code
        )
