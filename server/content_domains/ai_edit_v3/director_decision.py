from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any, Mapping, Sequence

from . import contracts
from .contracts import ContractError
from .providers.base import ProviderResult


_UNSAFE_TEXT = re.compile(r"(?:[a-z][a-z0-9+.-]*://|^[a-zA-Z]:[\\/]|^[/\\]|```|<script\b)", re.IGNORECASE)
_UNSAFE_REQUEST_KEY = re.compile(
    r"(?:api[_-]?key|secret|password|token|authorization|credential|provider|(?:^|_)(?:url|path|key)$)",
    re.IGNORECASE,
)
_UNSAFE_REQUEST_VALUE = re.compile(
    r"(?:[a-z][a-z0-9+.-]*://|^[a-zA-Z]:[\\/]|^[/\\]|\bBearer\s+\S+|\bsk-[A-Za-z0-9_-]{8,})",
    re.IGNORECASE,
)


class DirectorDecisionError(ValueError):
    def __init__(self, code: str, path: str = "$") -> None:
        self.code = code
        self.path = path
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class ValidatedDecision:
    value: Mapping[str, Any]
    provider_request_id: str | None
    raw_output_json: str
    raw_output_sha256: str
    decision_sha256: str
    schema_sha256: str
    candidates_sha256: str
    prompt_version: str = "director-decision-v1"


def _record(value: Any) -> dict[str, Any]:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Mapping):
        return copy.deepcopy(dict(value))
    raise DirectorDecisionError("director_candidate_invalid")


def _sequence(capabilities: Mapping[str, Any], name: str) -> tuple[str, ...]:
    value = capabilities.get(name, ())
    if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) for item in value):
        raise DirectorDecisionError("director_capabilities_invalid", f"$.capabilities.{name}")
    return tuple(value)


def _reject_unsafe_text(value: Any, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            _reject_unsafe_text(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_unsafe_text(item, f"{path}[{index}]")
    elif isinstance(value, str) and _UNSAFE_TEXT.search(value.strip()):
        raise DirectorDecisionError("director_decision_unsafe_value", path)


def _reject_unsafe_request_keys(value: Any, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str) or _UNSAFE_REQUEST_KEY.search(key):
                raise DirectorDecisionError("director_request_unsafe", f"{path}.{key}")
            _reject_unsafe_request_keys(item, f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_unsafe_request_keys(item, f"{path}[{index}]")
    elif isinstance(value, str) and _UNSAFE_REQUEST_VALUE.search(value.strip()):
        raise DirectorDecisionError("director_request_unsafe", path)


def _validate_visible_text(value: Mapping[str, Any], candidate: Mapping[str, Any], path: str) -> None:
    references = value.get("source_caption_ids")
    allowed = tuple(candidate.get("caption_ids", ()))
    if not isinstance(references, list) or any(item not in allowed for item in references):
        raise DirectorDecisionError("director_text_reference_invalid", path)


def validate_director_decision(
    value: Any,
    *,
    candidates: Sequence[Any],
    capabilities: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        contracts.validate_director_decision_schema(value)
    except ContractError as exc:
        raise DirectorDecisionError(exc.error_code, exc.field_path) from None
    normalized = copy.deepcopy(dict(value))
    _reject_unsafe_text(normalized)
    candidate_records = tuple(_record(item) for item in candidates)
    candidate_ids = tuple(item.get("id") for item in candidate_records)
    directives = normalized["scene_directives"]
    directive_ids = tuple(item["scene_id"] for item in directives)
    if directive_ids != candidate_ids or len(set(directive_ids)) != len(directive_ids):
        raise DirectorDecisionError("director_scene_coverage_invalid", "$.scene_directives")

    layouts = set(_sequence(capabilities, "layout_capabilities"))
    overlays = set(_sequence(capabilities, "overlay_capabilities"))
    animations = set(_sequence(capabilities, "animation_capabilities"))
    transitions = set(_sequence(capabilities, "transition_capabilities"))
    themes = capabilities.get("theme_profile_ids")
    if themes is not None and normalized["theme_profile_id"] not in set(_sequence(capabilities, "theme_profile_ids")):
        raise DirectorDecisionError("director_theme_unknown", "$.theme_profile_id")
    layout_variants = capabilities.get("layout_variants", {})
    if not isinstance(layout_variants, Mapping):
        raise DirectorDecisionError("director_capabilities_invalid", "$.capabilities.layout_variants")
    overlay_variants = capabilities.get("overlay_variants", {})
    overlay_targets = capabilities.get("overlay_animation_targets", {})
    layout_targets = capabilities.get("layout_animation_targets", {})
    for name, catalog in (
        ("overlay_variants", overlay_variants),
        ("overlay_animation_targets", overlay_targets),
        ("layout_animation_targets", layout_targets),
    ):
        if not isinstance(catalog, Mapping):
            raise DirectorDecisionError("director_capabilities_invalid", f"$.capabilities.{name}")

    for index, (directive, candidate) in enumerate(zip(directives, candidate_records, strict=True)):
        path = f"$.scene_directives[{index}]"
        layout_id = directive["layout_id"]
        if layout_id not in layouts:
            raise DirectorDecisionError("director_layout_unknown", f"{path}.layout_id")
        variants = layout_variants.get(layout_id)
        if variants is not None and (
            not isinstance(variants, (list, tuple)) or directive["layout_variant"] not in variants
        ):
            raise DirectorDecisionError("director_layout_variant_unknown", f"{path}.layout_variant")
        if directive["transition"] not in transitions:
            raise DirectorDecisionError("director_transition_unknown", f"{path}.transition")
        if directive["transition"] == "card_match_cut" and capabilities.get("identity_match_capability") is not True:
            raise DirectorDecisionError("director_identity_transition_invalid", f"{path}.transition")

        visible_names = {name for name in ("headline", "highlight") if name in directive}
        for name in visible_names:
            _validate_visible_text(directive[name], candidate, f"{path}.{name}")
        instance_ids: set[str] = set()
        declared_layout_targets = layout_targets.get(layout_id, ())
        if not isinstance(declared_layout_targets, (list, tuple)) or any(
            not isinstance(item, str) for item in declared_layout_targets
        ):
            raise DirectorDecisionError("director_capabilities_invalid", "$.capabilities.layout_animation_targets")
        public_targets = set(declared_layout_targets)
        for overlay_index, overlay in enumerate(directive["overlay_instances"]):
            overlay_path = f"{path}.overlay_instances[{overlay_index}]"
            if overlay["component_id"] not in overlays:
                raise DirectorDecisionError("director_component_unknown", f"{overlay_path}.component_id")
            if overlay["content_ref"] not in visible_names:
                raise DirectorDecisionError("director_content_reference_invalid", f"{overlay_path}.content_ref")
            variants = overlay_variants.get(overlay["component_id"])
            if "variant" in overlay and (
                not isinstance(variants, (list, tuple)) or overlay["variant"] not in variants
            ):
                raise DirectorDecisionError("director_overlay_variant_unknown", f"{overlay_path}.variant")
            if overlay["instance_id"] in instance_ids:
                raise DirectorDecisionError("director_overlay_duplicate", f"{overlay_path}.instance_id")
            instance_ids.add(overlay["instance_id"])
            targets = overlay_targets.get(overlay["component_id"], ())
            if not isinstance(targets, (list, tuple)) or any(not isinstance(item, str) for item in targets):
                raise DirectorDecisionError("director_capabilities_invalid", "$.capabilities.overlay_animation_targets")
            public_targets.update(targets)
        for animation_index, animation in enumerate(directive["animations"]):
            animation_path = f"{path}.animations[{animation_index}]"
            if animation["preset"] not in animations:
                raise DirectorDecisionError("director_animation_unknown", f"{animation_path}.preset")
            if animation["target_id"] not in instance_ids | public_targets:
                raise DirectorDecisionError("director_animation_target_unknown", f"{animation_path}.target_id")

        available = set(candidate.get("available_material_ids", ()))
        binding_ids: set[str] = set()
        slot_ids: set[str] = set()
        for binding_index, binding in enumerate(directive["material_bindings"]):
            binding_path = f"{path}.material_bindings[{binding_index}]"
            if binding["material_id"] not in available:
                raise DirectorDecisionError("director_material_unknown", f"{binding_path}.material_id")
            if binding["slot_id"] in slot_ids:
                raise DirectorDecisionError("director_material_slot_duplicate", f"{binding_path}.slot_id")
            slot_ids.add(binding["slot_id"])
            binding_ids.add(binding["material_id"])
        for slot_index, slot in enumerate(directive["material_slot_directives"]):
            slot_path = f"{path}.material_slot_directives[{slot_index}]"
            if slot["slot_id"] in slot_ids:
                raise DirectorDecisionError("director_material_slot_duplicate", f"{slot_path}.slot_id")
            if slot["priority"] == "required" and not slot["semantic"].strip():
                raise DirectorDecisionError("director_material_semantic_missing", f"{slot_path}.semantic")
            slot_ids.add(slot["slot_id"])
        if len(slot_ids) > 4:
            raise DirectorDecisionError("director_material_slots_exceeded", path)
    contracts.canonical_json(normalized)
    return normalized


def _provider_output(raw: Any) -> tuple[dict[str, Any], str | None, str, str]:
    request_id = None
    if isinstance(raw, ProviderResult):
        request_id = raw.request_id
        raw = raw.payload.get("content")
    elif hasattr(raw, "payload") and isinstance(raw.payload, Mapping):
        request_id = getattr(raw, "request_id", None)
        raw = raw.payload.get("content")
    try:
        if isinstance(raw, Mapping):
            value = dict(raw)
        elif isinstance(raw, (str, bytes)):
            value = contracts.parse_strict_json(raw, max_bytes=512 * 1024, max_depth=24, max_items=5000, max_string_chars=4000)
        else:
            raise TypeError
    except (ContractError, TypeError):
        raise DirectorDecisionError("director_decision_json_invalid") from None
    if not isinstance(value, Mapping):
        raise DirectorDecisionError("director_decision_json_invalid")
    raw_json = contracts.canonical_json(value).decode("utf-8")
    return dict(value), request_id, raw_json, hashlib.sha256(raw_json.encode("utf-8")).hexdigest()


def generate_director_decision(context: Any, provider: Any, *, max_repairs: int = 1) -> ValidatedDecision:
    if max_repairs != 1:
        raise ValueError("director_repair_budget_invalid")
    frozen_request = copy.deepcopy(dict(context.request))
    _reject_unsafe_request_keys(frozen_request)
    previous_sha = None
    last_error = DirectorDecisionError("director_decision_invalid")
    for attempt in range(max_repairs + 1):
        request = frozen_request if attempt == 0 else {
            "frozen_request": frozen_request,
            "previous_response_sha256": previous_sha,
            "repair": {"error_code": last_error.code, "field_path": last_error.path},
        }
        raw = provider.generate_decision(
            request,
            purpose="initial" if attempt == 0 else "repair",
            idempotency_key=f"ai-edit-v3:{context.job_id}:director-decision:{attempt}",
            deadline_at=context.deadline_at,
        )
        try:
            value, request_id, raw_json, previous_sha = _provider_output(raw)
            normalized = validate_director_decision(value, candidates=context.candidates, capabilities=context.capabilities)
            normalized_json = contracts.canonical_json(normalized)
            candidate_json = contracts.canonical_json([_record(item) for item in context.candidates])
            return ValidatedDecision(
                normalized,
                request_id,
                raw_json,
                previous_sha,
                hashlib.sha256(normalized_json).hexdigest(),
                contracts.schema_sha256("director-decision-v1.schema.json"),
                hashlib.sha256(candidate_json).hexdigest(),
            )
        except DirectorDecisionError as exc:
            last_error = exc
            if previous_sha is None:
                previous_sha = hashlib.sha256(repr(raw).encode("utf-8")).hexdigest()
    raise DirectorDecisionError("director_decision_invalid", last_error.path)
