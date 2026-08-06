from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import asdict, dataclass, is_dataclass
from typing import Any, Mapping, Sequence

from . import contracts
from .contracts import ContractError
from .director_layout_policy import (
    MAX_REQUIRED_MATERIAL_SLOTS,
    MAX_TOTAL_MATERIAL_SLOTS,
    SCENE_STRUCTURE_POLICY,
    SPEAKER_VISIBILITY_POLICY,
    validate_layout_requirements,
)
from .providers.base import ProviderResult
from .overlay_catalog import validate_overlay_projection


_UNSAFE_TEXT = re.compile(r"(?:[a-z][a-z0-9+.-]*://|^[a-zA-Z]:[\\/]|^[/\\]|```|<script\b)", re.IGNORECASE)
_UNSAFE_REQUEST_KEY = re.compile(
    r"(?:api[_-]?key|secret|password|token|authorization|credential|provider|(?:^|_)(?:url|path|key)$)",
    re.IGNORECASE,
)
_UNSAFE_REQUEST_VALUE = re.compile(
    r"(?:[a-z][a-z0-9+.-]*://|^[a-zA-Z]:[\\/]|^[/\\]|\bBearer\s+\S+|\bsk-[A-Za-z0-9_-]{8,})",
    re.IGNORECASE,
)
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_SAFE_FIELD_PATH = re.compile(
    r"^\$(?:(?:\.[A-Za-z_][A-Za-z0-9_]*)|(?:\[\d+\]))*$"
)
_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{1,256}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_VISIBLE_TEXT_REFERENCE_PATH = re.compile(
    r"\$\.scene_directives\[(?P<scene_index>\d+)\]\.(?P<field>headline|highlight)"
    r"(?:\.[A-Za-z_][A-Za-z0-9_]*)?"
)
_MATERIAL_PURPOSE_PATH = re.compile(
    r"\$\.scene_directives\[(?P<scene_index>\d+)\]"
    r"\.material_slot_directives\[(?P<slot_index>\d+)\]\.purpose"
)


def _safe_field_path(value: Any) -> str:
    if (
        isinstance(value, str)
        and len(value) <= 512
        and _SAFE_FIELD_PATH.fullmatch(value) is not None
    ):
        return value
    return "$"


def _request_id_evidence(value: Any) -> dict[str, Any]:
    if not isinstance(value, str) or not value:
        return {}
    digest = hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()
    if (
        value == value.strip()
        and _SAFE_REQUEST_ID.fullmatch(value) is not None
        and _UNSAFE_REQUEST_VALUE.search(value) is None
    ):
        return {"request_id": value}
    return {"request_id_present": True, "request_id_sha256": digest}


def _safe_attempt_evidence(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    attempt = value.get("attempt")
    purpose = value.get("purpose")
    response_sha256 = value.get("response_sha256")
    validation_code = value.get("validation_code")
    if (
        type(attempt) is not int
        or attempt not in {1, 2}
        or purpose not in {"initial", "repair"}
        or not isinstance(response_sha256, str)
        or _SHA256.fullmatch(response_sha256) is None
        or not isinstance(validation_code, str)
        or _SAFE_CODE.fullmatch(validation_code) is None
    ):
        return None
    result = {
        "attempt": attempt,
        "purpose": purpose,
        **_request_id_evidence(value.get("request_id")),
        "response_sha256": response_sha256,
        "validation_code": validation_code,
        "field_path": _safe_field_path(value.get("field_path")),
    }
    if "request_id" not in result and "request_id_sha256" not in result:
        request_id_sha256 = value.get("request_id_sha256")
        if isinstance(request_id_sha256, str) and _SHA256.fullmatch(request_id_sha256):
            result["request_id_present"] = value.get("request_id_present") is True
            result["request_id_sha256"] = request_id_sha256
    return result


class DirectorDecisionError(ValueError):
    def __init__(
        self,
        code: str,
        path: str = "$",
        *,
        detail_code: str | None = None,
        attempts: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        self.code = code
        self.path = _safe_field_path(path)
        self.detail_code = (
            detail_code
            if isinstance(detail_code, str) and _SAFE_CODE.fullmatch(detail_code)
            else None
        )
        self.attempts = tuple(
            item
            for item in (
                _safe_attempt_evidence(value) for value in tuple(attempts)[:2]
            )
            if item is not None
        )
        self.attempt_count = len(self.attempts)
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


def _visible_text(value: Mapping[str, Any], candidate: Mapping[str, Any], path: str) -> str:
    references = value.get("source_caption_ids")
    caption_texts = candidate.get("caption_texts", ())
    if isinstance(caption_texts, (list, tuple)):
        try:
            index = {str(item[0]): str(item[1]) for item in caption_texts if isinstance(item, (list, tuple)) and len(item) == 2}
        except (TypeError, ValueError):
            index = {}
        if isinstance(references, list) and references and all(reference in index for reference in references):
            return "".join(index[reference] for reference in references)
    if list(references or ()) == list(candidate.get("caption_ids", ())):
        text = candidate.get("authoritative_text")
        if isinstance(text, str) and text:
            return text
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

    layout_sequence = _sequence(capabilities, "layout_capabilities")
    layouts = set(layout_sequence)
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

    binding_mode = capabilities.get("material_binding_mode")
    layout_requirements: Mapping[str, Any] | None = None
    if binding_mode is not None or "layout_requirements" in capabilities:
        if binding_mode != "semantic_slots_only":
            raise DirectorDecisionError(
                "director_capabilities_invalid", "$.capabilities.material_binding_mode"
            )
        try:
            layout_requirements = validate_layout_requirements(
                layout_sequence, capabilities.get("layout_requirements")
            )
        except ValueError:
            raise DirectorDecisionError(
                "director_capabilities_invalid", "$.capabilities.layout_requirements"
            ) from None
        if capabilities.get("max_required_material_slots") != MAX_REQUIRED_MATERIAL_SLOTS:
            raise DirectorDecisionError(
                "director_capabilities_invalid",
                "$.capabilities.max_required_material_slots",
            )
        if capabilities.get("max_total_material_slots") != MAX_TOTAL_MATERIAL_SLOTS:
            raise DirectorDecisionError(
                "director_capabilities_invalid",
                "$.capabilities.max_total_material_slots",
            )
        if capabilities.get("speaker_visibility_policy") != SPEAKER_VISIBILITY_POLICY:
            raise DirectorDecisionError(
                "director_capabilities_invalid",
                "$.capabilities.speaker_visibility_policy",
            )
        if capabilities.get("scene_structure_policy") != SCENE_STRUCTURE_POLICY:
            raise DirectorDecisionError(
                "director_capabilities_invalid",
                "$.capabilities.scene_structure_policy",
            )

    global_slot_ids: set[str] = set()
    required_slot_count = 0
    total_slot_count = 0
    signatures: list[tuple[str, str, tuple[str, ...]]] = []
    source_has_speaker = bool(
        candidate_records and candidate_records[0].get("speaker_available") is True
    )
    if any(
        (candidate.get("speaker_available") is True) != source_has_speaker
        for candidate in candidate_records
    ):
        raise DirectorDecisionError(
            "director_candidates_invalid", "$.scene_candidates"
        )
    hidden_speaker_ms = 0
    total_duration_ms = 0

    for index, (directive, candidate) in enumerate(zip(directives, candidate_records, strict=True)):
        path = f"$.scene_directives[{index}]"
        layout_id = directive["layout_id"]
        if layout_id not in layouts:
            raise DirectorDecisionError("director_layout_unknown", f"{path}.layout_id")
        layout_policy = layout_requirements.get(layout_id) if layout_requirements else None
        if layout_requirements is not None and not isinstance(layout_policy, Mapping):
            raise DirectorDecisionError(
                "director_capabilities_invalid", f"$.capabilities.layout_requirements.{layout_id}"
            )
        if (
            isinstance(layout_policy, Mapping)
            and layout_policy.get("speaker_required") is True
            and candidate.get("speaker_available") is not True
        ):
            raise DirectorDecisionError(
                "director_layout_source_incompatible", f"{path}.layout_id"
            )
        if (
            index == 0
            and source_has_speaker
            and SPEAKER_VISIBILITY_POLICY["opening_requires_speaker"] is True
            and isinstance(layout_policy, Mapping)
            and layout_policy.get("speaker_required") is not True
        ):
            raise DirectorDecisionError(
                "director_opening_speaker_required", f"{path}.layout_id"
            )
        start_ms = candidate.get("start_ms")
        end_ms = candidate.get("end_ms")
        if (
            type(start_ms) is not int
            or type(end_ms) is not int
            or start_ms < 0
            or end_ms <= start_ms
        ):
            raise DirectorDecisionError(
                "director_candidates_invalid", f"$.scene_candidates[{index}]"
            )
        scene_duration_ms = end_ms - start_ms
        if scene_duration_ms < 500 and directive["sound_events"]:
            raise DirectorDecisionError(
                "director_sound_event_timeline_invalid",
                f"{path}.sound_events",
            )
        total_duration_ms += scene_duration_ms
        if (
            source_has_speaker
            and isinstance(layout_policy, Mapping)
            and layout_policy.get("speaker_required") is not True
        ):
            hidden_speaker_ms += scene_duration_ms
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
            try:
                validate_overlay_projection(
                    capabilities,
                    component_id=overlay["component_id"], placement=overlay["placement"],
                    ratio=str(capabilities.get("output_ratio")),
                    text=_visible_text(directive[overlay["content_ref"]], candidate, f"{overlay_path}.content_ref"),
                )
            except ValueError as exc:
                raise DirectorDecisionError(str(exc), overlay_path) from None
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
        signatures.append((
            layout_id,
            directive["layout_variant"],
            tuple(sorted(
                str(overlay["component_id"])
                for overlay in directive["overlay_instances"]
            )),
        ))
        for animation_index, animation in enumerate(directive["animations"]):
            animation_path = f"{path}.animations[{animation_index}]"
            if animation["preset"] not in animations:
                raise DirectorDecisionError("director_animation_unknown", f"{animation_path}.preset")
            if animation["target_id"] not in instance_ids | public_targets:
                raise DirectorDecisionError("director_animation_target_unknown", f"{animation_path}.target_id")

        available = set(candidate.get("available_material_ids", ()))
        binding_ids: set[str] = set()
        slot_ids: set[str] = set()
        if binding_mode == "semantic_slots_only" and directive["material_bindings"]:
            raise DirectorDecisionError(
                "director_material_binding_forbidden", f"{path}.material_bindings"
            )
        for binding_index, binding in enumerate(directive["material_bindings"]):
            binding_path = f"{path}.material_bindings[{binding_index}]"
            if binding["material_id"] not in available:
                raise DirectorDecisionError("director_material_unknown", f"{binding_path}.material_id")
            if binding["slot_id"] in slot_ids:
                raise DirectorDecisionError("director_material_slot_duplicate", f"{binding_path}.slot_id")
            slot_ids.add(binding["slot_id"])
            binding_ids.add(binding["material_id"])
        semantic_slots = (
            layout_policy.get("semantic_slots", ())
            if isinstance(layout_policy, Mapping)
            else ()
        )
        semantic_slot_by_purpose = {
            item.get("purpose"): item
            for item in semantic_slots
            if isinstance(item, Mapping)
        }
        seen_layout_slots: set[str] = set()
        satisfied_required_layout_slots: set[str] = set()
        for slot_index, slot in enumerate(directive["material_slot_directives"]):
            slot_path = f"{path}.material_slot_directives[{slot_index}]"
            if slot["slot_id"] in slot_ids:
                raise DirectorDecisionError("director_material_slot_duplicate", f"{slot_path}.slot_id")
            if binding_mode == "semantic_slots_only":
                total_slot_count += 1
                if total_slot_count > MAX_TOTAL_MATERIAL_SLOTS:
                    raise DirectorDecisionError(
                        "director_material_slots_exceeded", slot_path
                    )
                if slot["slot_id"] in global_slot_ids:
                    raise DirectorDecisionError(
                        "director_material_slot_duplicate", f"{slot_path}.slot_id"
                    )
                policy_slot = semantic_slot_by_purpose.get(slot["purpose"])
                if not isinstance(policy_slot, Mapping):
                    raise DirectorDecisionError(
                        "director_material_purpose_invalid", f"{slot_path}.purpose"
                    )
                layout_slot_id = policy_slot.get("layout_slot_id")
                if not isinstance(layout_slot_id, str) or layout_slot_id in seen_layout_slots:
                    raise DirectorDecisionError(
                        "director_material_slot_duplicate", f"{slot_path}.purpose"
                    )
                seen_layout_slots.add(layout_slot_id)
                global_slot_ids.add(slot["slot_id"])
                if slot["priority"] == "required":
                    required_slot_count += 1
                    if required_slot_count > MAX_REQUIRED_MATERIAL_SLOTS:
                        raise DirectorDecisionError(
                            "director_required_material_limit_exceeded",
                            f"{slot_path}.priority",
                        )
                    if policy_slot.get("required_for_layout") is True:
                        satisfied_required_layout_slots.add(layout_slot_id)
            if slot["priority"] == "required" and not slot["semantic"].strip():
                raise DirectorDecisionError("director_material_semantic_missing", f"{slot_path}.semantic")
            slot_ids.add(slot["slot_id"])
        if len(slot_ids) > 4:
            raise DirectorDecisionError("director_material_slots_exceeded", path)
        if binding_mode == "semantic_slots_only":
            required_layout_slots = {
                str(item["layout_slot_id"])
                for item in semantic_slots
                if isinstance(item, Mapping) and item.get("required_for_layout") is True
            }
            if not required_layout_slots.issubset(satisfied_required_layout_slots):
                raise DirectorDecisionError(
                    "director_layout_material_missing",
                    f"{path}.material_slot_directives",
                )
    if (
        source_has_speaker
        and hidden_speaker_ms
        > int(total_duration_ms * SPEAKER_VISIBILITY_POLICY["max_hidden_ratio"])
    ):
        raise DirectorDecisionError(
            "director_speaker_visibility_exceeded", "$.scene_directives"
        )
    if (
        layout_requirements is not None
        and total_duration_ms >= SCENE_STRUCTURE_POLICY["min_duration_ms"]
        and len(signatures) >= SCENE_STRUCTURE_POLICY["min_scenes"]
    ):
        if len(set(signatures)) < SCENE_STRUCTURE_POLICY["minimum_distinct_signatures"]:
            raise DirectorDecisionError(
                "director_scene_structure_repetitive", "$.scene_directives"
            )
        run_length = 0
        previous = None
        for signature in signatures:
            run_length = run_length + 1 if signature == previous else 1
            if run_length > SCENE_STRUCTURE_POLICY["max_adjacent_identical"]:
                raise DirectorDecisionError(
                    "director_scene_structure_repetitive", "$.scene_directives"
                )
            previous = signature
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


def _provider_request_id(raw: Any) -> str | None:
    if isinstance(raw, ProviderResult):
        return raw.request_id
    request_id = getattr(raw, "request_id", None)
    return request_id if isinstance(request_id, str) else None


def _provider_response_sha256(raw: Any) -> str:
    value = raw
    if isinstance(raw, ProviderResult):
        value = raw.payload.get("content")
    elif hasattr(raw, "payload") and isinstance(raw.payload, Mapping):
        value = raw.payload.get("content")
    if isinstance(value, bytes):
        encoded = value
    elif isinstance(value, str):
        encoded = value.encode("utf-8", errors="replace")
    else:
        try:
            encoded = contracts.canonical_json(value)
        except (ContractError, TypeError, ValueError):
            encoded = repr(value).encode("utf-8", errors="replace")
    return hashlib.sha256(encoded).hexdigest()


def _with_hard_cut_transition(
    value: Mapping[str, Any],
    error: DirectorDecisionError,
    capabilities: Mapping[str, Any],
) -> dict[str, Any]:
    """Apply one narrow, frozen transition fallback after strict validation."""

    if error.code not in {
        "director_transition_unknown",
        "director_identity_transition_invalid",
    }:
        raise error
    match = re.fullmatch(
        r"\$\.scene_directives\[(\d+)\]\.transition", error.path
    )
    transitions = _sequence(capabilities, "transition_capabilities")
    if match is None or "hard_cut" not in transitions:
        raise error
    scene_index = int(match.group(1))
    recovered = copy.deepcopy(dict(value))
    directives = recovered.get("scene_directives")
    if not isinstance(directives, list) or scene_index >= len(directives):
        raise error
    directive = directives[scene_index]
    transition = directive.get("transition") if isinstance(directive, Mapping) else None
    if not isinstance(transition, str) or _SAFE_CODE.fullmatch(transition) is None:
        raise error
    if error.code == "director_transition_unknown" and transition in transitions:
        raise error
    if (
        error.code == "director_identity_transition_invalid"
        and transition != "card_match_cut"
    ):
        raise error
    directive["transition"] = "hard_cut"
    return recovered


def _material_purpose_context(
    value: Mapping[str, Any],
    error: DirectorDecisionError,
    capabilities: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], tuple[str, ...], tuple[str, ...]]:
    """Resolve one material-purpose target from frozen layout policy only."""

    match = _MATERIAL_PURPOSE_PATH.fullmatch(error.path)
    if error.code != "director_material_purpose_invalid" or match is None:
        raise error
    if capabilities.get("material_binding_mode") != "semantic_slots_only":
        raise error
    layout_sequence = _sequence(capabilities, "layout_capabilities")
    try:
        layout_requirements = validate_layout_requirements(
            layout_sequence, capabilities.get("layout_requirements")
        )
    except ValueError:
        raise DirectorDecisionError(
            "director_capabilities_invalid", "$.capabilities.layout_requirements"
        ) from None

    scene_index = int(match.group("scene_index"))
    slot_index = int(match.group("slot_index"))
    directives = value.get("scene_directives")
    if not isinstance(directives, list) or scene_index >= len(directives):
        raise error
    directive = directives[scene_index]
    if not isinstance(directive, Mapping):
        raise error
    slots = directive.get("material_slot_directives")
    if not isinstance(slots, list) or slot_index >= len(slots):
        raise error
    slot = slots[slot_index]
    if not isinstance(slot, Mapping):
        raise error
    layout_id = directive.get("layout_id")
    policy = layout_requirements.get(layout_id)
    semantic_slots = policy.get("semantic_slots") if isinstance(policy, Mapping) else None
    if not isinstance(semantic_slots, (list, tuple)):
        raise error
    allowed = tuple(
        item["purpose"]
        for item in semantic_slots
        if isinstance(item, Mapping) and isinstance(item.get("purpose"), str)
    )
    required = tuple(
        item["purpose"]
        for item in semantic_slots
        if isinstance(item, Mapping)
        and isinstance(item.get("purpose"), str)
        and item.get("required_for_layout") is True
    )
    if not allowed or len(set(allowed)) != len(allowed):
        raise DirectorDecisionError(
            "director_capabilities_invalid", "$.capabilities.layout_requirements"
        )
    return dict(directive), dict(slot), allowed, required


def _with_layout_compatible_material_purpose(
    value: Mapping[str, Any],
    error: DirectorDecisionError,
    capabilities: Mapping[str, Any],
) -> dict[str, Any]:
    """Canonicalize one unambiguous purpose without spending the repair call."""

    directive, slot, allowed, required = _material_purpose_context(
        value, error, capabilities
    )
    match = _MATERIAL_PURPOSE_PATH.fullmatch(error.path)
    if match is None:
        raise error
    scene_index = int(match.group("scene_index"))
    slot_index = int(match.group("slot_index"))
    slots = directive.get("material_slot_directives")
    if not isinstance(slots, list):
        raise error
    used = {
        item.get("purpose")
        for index, item in enumerate(slots)
        if index != slot_index
        and isinstance(item, Mapping)
        and isinstance(item.get("purpose"), str)
        and item.get("purpose") in allowed
    }
    choices = tuple(purpose for purpose in allowed if purpose not in used)
    if slot.get("priority") == "required":
        required_choices = tuple(purpose for purpose in choices if purpose in required)
        if len(required_choices) == 1:
            choices = required_choices
    if len(choices) != 1:
        raise error

    recovered = copy.deepcopy(dict(value))
    recovered["scene_directives"][scene_index]["material_slot_directives"][
        slot_index
    ]["purpose"] = choices[0]
    return recovered


def _apply_scoped_material_purpose_repair(
    repair_value: Mapping[str, Any],
    initial_value: Mapping[str, Any],
    error: DirectorDecisionError,
    capabilities: Mapping[str, Any],
) -> dict[str, Any]:
    """Take only the repaired purpose; keep every other initial field frozen."""

    try:
        contracts.validate_director_decision_schema(repair_value)
    except ContractError as exc:
        raise DirectorDecisionError(exc.error_code, exc.field_path) from None
    _reject_unsafe_text(repair_value)
    initial_directive, initial_slot, allowed, _ = _material_purpose_context(
        initial_value, error, capabilities
    )
    match = _MATERIAL_PURPOSE_PATH.fullmatch(error.path)
    if match is None:
        raise error
    scene_index = int(match.group("scene_index"))
    slot_index = int(match.group("slot_index"))
    repair_directives = repair_value.get("scene_directives")
    if not isinstance(repair_directives, list) or scene_index >= len(repair_directives):
        raise error
    repair_directive = repair_directives[scene_index]
    if not isinstance(repair_directive, Mapping):
        raise error
    repair_slots = repair_directive.get("material_slot_directives")
    if not isinstance(repair_slots, list) or slot_index >= len(repair_slots):
        raise error
    repair_slot = repair_slots[slot_index]
    if (
        not isinstance(repair_slot, Mapping)
        or repair_directive.get("scene_id") != initial_directive.get("scene_id")
        or repair_slot.get("slot_id") != initial_slot.get("slot_id")
        or repair_slot.get("purpose") not in allowed
    ):
        raise error

    recovered = copy.deepcopy(dict(initial_value))
    recovered["scene_directives"][scene_index]["material_slot_directives"][
        slot_index
    ]["purpose"] = repair_slot["purpose"]
    return recovered


def _speaker_layout_fallback(
    directive: Mapping[str, Any],
    *,
    scene_index: int,
    layout_sequence: Sequence[str],
    layout_requirements: Mapping[str, Any],
    layout_variants: Mapping[str, Any],
    layout_targets: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Return a speaker-visible directive without changing text or material meaning."""

    slots = directive.get("material_slot_directives")
    bindings = directive.get("material_bindings")
    if not isinstance(slots, list) or bindings != []:
        return None
    original_layout = directive.get("layout_id")
    product_like = original_layout in {
        "product_hero",
        "editorial_collage",
        "comparison_split",
    }
    preferred = (
        ["material_fullscreen_speaker_pip", "speaker_fullscreen"]
        if product_like
        else ["speaker_fullscreen", "speaker_left_info_right", "speaker_right_evidence_left"]
    )
    ordered_layouts = [
        layout_id
        for layout_id in (*preferred, *layout_sequence)
        if layout_id in layout_sequence
    ]
    seen: set[str] = set()
    for layout_id in ordered_layouts:
        if layout_id in seen:
            continue
        seen.add(layout_id)
        policy = layout_requirements.get(layout_id)
        variants = layout_variants.get(layout_id)
        if (
            not isinstance(policy, Mapping)
            or policy.get("speaker_required") is not True
            or not isinstance(variants, (list, tuple))
            or not variants
            or any(not isinstance(item, str) for item in variants)
        ):
            continue
        semantic_slots = policy.get("semantic_slots")
        if not isinstance(semantic_slots, (list, tuple)):
            continue
        by_purpose = {
            item.get("purpose"): item
            for item in semantic_slots
            if isinstance(item, Mapping) and isinstance(item.get("purpose"), str)
        }
        if any(
            not isinstance(slot, Mapping)
            or slot.get("purpose") not in by_purpose
            for slot in slots
        ):
            continue
        required_purposes = {
            purpose
            for purpose, item in by_purpose.items()
            if item.get("required_for_layout") is True
        }
        satisfied = {
            slot.get("purpose")
            for slot in slots
            if isinstance(slot, Mapping) and slot.get("priority") == "required"
        }
        if not required_purposes.issubset(satisfied):
            continue

        recovered = copy.deepcopy(dict(directive))
        recovered["layout_id"] = layout_id
        recovered["layout_variant"] = variants[scene_index % len(variants)]
        overlay_ids = {
            item.get("instance_id")
            for item in recovered.get("overlay_instances", ())
            if isinstance(item, Mapping) and isinstance(item.get("instance_id"), str)
        }
        public_targets = layout_targets.get(layout_id, ())
        if not isinstance(public_targets, (list, tuple)):
            return None
        allowed_targets = overlay_ids | {
            item for item in public_targets if isinstance(item, str)
        }
        recovered["animations"] = [
            animation
            for animation in recovered.get("animations", ())
            if isinstance(animation, Mapping)
            and animation.get("target_id") in allowed_targets
        ]
        return recovered
    return None


def _with_speaker_visibility_fallback(
    value: Mapping[str, Any],
    *,
    candidates: Sequence[Any],
    capabilities: Mapping[str, Any],
) -> dict[str, Any]:
    """Minimally expose the source speaker while preserving required semantics."""

    recovered = copy.deepcopy(dict(value))
    directives = recovered.get("scene_directives")
    candidate_records = tuple(_record(item) for item in candidates)
    if not isinstance(directives, list) or len(directives) != len(candidate_records):
        raise DirectorDecisionError(
            "director_speaker_visibility_exceeded", "$.scene_directives"
        )
    if not candidate_records or any(
        candidate.get("speaker_available") is not True
        for candidate in candidate_records
    ):
        raise DirectorDecisionError(
            "director_speaker_visibility_exceeded", "$.scene_directives"
        )
    layout_sequence = _sequence(capabilities, "layout_capabilities")
    try:
        layout_requirements = validate_layout_requirements(
            layout_sequence, capabilities.get("layout_requirements")
        )
    except ValueError:
        raise DirectorDecisionError(
            "director_capabilities_invalid", "$.capabilities.layout_requirements"
        ) from None
    layout_variants = capabilities.get("layout_variants")
    layout_targets = capabilities.get("layout_animation_targets")
    if not isinstance(layout_variants, Mapping) or not isinstance(layout_targets, Mapping):
        raise DirectorDecisionError("director_capabilities_invalid", "$.capabilities")

    total_duration_ms = sum(
        int(candidate["end_ms"]) - int(candidate["start_ms"])
        for candidate in candidate_records
    )
    hidden: list[tuple[int, int, dict[str, Any] | None]] = []
    for index, (directive, candidate) in enumerate(
        zip(directives, candidate_records, strict=True)
    ):
        policy = layout_requirements.get(directive.get("layout_id"))
        if isinstance(policy, Mapping) and policy.get("speaker_required") is not True:
            duration_ms = int(candidate["end_ms"]) - int(candidate["start_ms"])
            hidden.append((
                index,
                duration_ms,
                _speaker_layout_fallback(
                    directive,
                    scene_index=index,
                    layout_sequence=layout_sequence,
                    layout_requirements=layout_requirements,
                    layout_variants=layout_variants,
                    layout_targets=layout_targets,
                ),
            ))
    hidden_duration_ms = sum(duration for _, duration, _ in hidden)
    max_hidden_ms = int(
        total_duration_ms * SPEAKER_VISIBILITY_POLICY["max_hidden_ratio"]
    )
    required_reduction_ms = hidden_duration_ms - max_hidden_ms
    if required_reduction_ms <= 0:
        return recovered

    convertible = [item for item in hidden if item[2] is not None]
    best: tuple[tuple[int, int, tuple[int, ...]], tuple[int, ...]] | None = None
    for mask in range(1, 1 << len(convertible)):
        selected = tuple(
            item_index
            for item_index in range(len(convertible))
            if mask & (1 << item_index)
        )
        reduction_ms = sum(convertible[item_index][1] for item_index in selected)
        if reduction_ms < required_reduction_ms:
            continue
        scene_indices = tuple(convertible[item_index][0] for item_index in selected)
        key = (len(selected), reduction_ms - required_reduction_ms, scene_indices)
        if best is None or key < best[0]:
            best = (key, selected)
    if best is None:
        raise DirectorDecisionError(
            "director_speaker_visibility_exceeded", "$.scene_directives"
        )
    for item_index in best[1]:
        scene_index, _, fallback = convertible[item_index]
        if fallback is None:
            raise DirectorDecisionError(
                "director_speaker_visibility_exceeded", "$.scene_directives"
            )
        directives[scene_index] = fallback
    return recovered


def _validate_with_local_recoveries(
    value: Mapping[str, Any],
    *,
    candidates: Sequence[Any],
    capabilities: Mapping[str, Any],
) -> tuple[dict[str, Any], DirectorDecisionError | None]:
    """Apply at most one bounded normalization of each locally safe kind."""

    candidate_value: Mapping[str, Any] = value
    normalized_transition = False
    normalized_material_purpose = False
    while True:
        try:
            return (
                validate_director_decision(
                    candidate_value,
                    candidates=candidates,
                    capabilities=capabilities,
                ),
                None,
            )
        except DirectorDecisionError as error:
            if (
                not normalized_material_purpose
                and error.code == "director_material_purpose_invalid"
                and _MATERIAL_PURPOSE_PATH.fullmatch(error.path) is not None
            ):
                try:
                    candidate_value = _with_layout_compatible_material_purpose(
                        candidate_value, error, capabilities
                    )
                except DirectorDecisionError:
                    return copy.deepcopy(dict(candidate_value)), error
                normalized_material_purpose = True
                continue
            if (
                not normalized_transition
                and error.code in {
                    "director_transition_unknown",
                    "director_identity_transition_invalid",
                }
            ):
                try:
                    candidate_value = _with_hard_cut_transition(
                        candidate_value, error, capabilities
                    )
                except DirectorDecisionError:
                    return copy.deepcopy(dict(candidate_value)), error
                normalized_transition = True
                continue
            return copy.deepcopy(dict(candidate_value)), error


def _validate_generated_decision(
    value: Mapping[str, Any],
    *,
    candidates: Sequence[Any],
    capabilities: Mapping[str, Any],
    allow_speaker_fallback: bool,
) -> dict[str, Any]:
    candidate_value, error = _validate_with_local_recoveries(
        value, candidates=candidates, capabilities=capabilities
    )
    if error is None:
        return candidate_value
    if allow_speaker_fallback and error.code == "director_speaker_visibility_exceeded":
        candidate_value = _with_speaker_visibility_fallback(
            candidate_value,
            candidates=candidates,
            capabilities=capabilities,
        )
        return validate_director_decision(
            candidate_value, candidates=candidates, capabilities=capabilities
        )
    raise error


def _repair_expected_constraint(error: DirectorDecisionError) -> str:
    scene_constraints = {
        "director_scene_coverage_invalid": (
            "scene_directives_exact_candidate_order_and_count"
        ),
        "director_scene_structure_repetitive": (
            "scene_signatures_meet_distinct_and_adjacency_policy"
        ),
        "director_speaker_visibility_exceeded": (
            "speaker_hidden_duration_within_max_ratio"
        ),
    }
    if error.path == "$.scene_directives" and error.code in scene_constraints:
        return scene_constraints[error.code]
    if (
        error.code in {
            "director_transition_unknown",
            "director_identity_transition_invalid",
        }
        and re.fullmatch(
            r"\$\.scene_directives\[\d+\]\.transition", error.path
        )
        is not None
    ):
        return "transition_from_capabilities_transition_capabilities"
    if (
        error.code == "director_sound_event_timeline_invalid"
        and re.fullmatch(
            r"\$\.scene_directives\[\d+\]\.sound_events", error.path
        )
        is not None
    ):
        return "sound_events_empty_when_candidate_shorter_than_500ms"
    if (
        error.code == "director_material_purpose_invalid"
        and _MATERIAL_PURPOSE_PATH.fullmatch(error.path) is not None
    ):
        return "material_purpose_matches_selected_layout_semantic_slots"
    if (
        error.code == "director_decision_schema_invalid"
        and error.path == "$.scene_directives"
    ):
        return "scene_directives_array_matches_schema_and_candidates"
    if (
        error.code == "director_decision_schema_invalid"
        and _VISIBLE_TEXT_REFERENCE_PATH.fullmatch(error.path) is not None
    ):
        return "visible_text_reference_object_or_omit"
    return "follow_director_decision_schema_exactly"


def _without_visible_text_schema_regression(
    repair_value: Mapping[str, Any],
    initial_value: Mapping[str, Any],
    error: DirectorDecisionError,
) -> dict[str, Any]:
    """Restore one trusted visible-text field so all other repair rules can run."""

    match = _VISIBLE_TEXT_REFERENCE_PATH.fullmatch(error.path)
    if error.code != "director_decision_schema_invalid" or match is None:
        raise error
    scene_index = int(match.group("scene_index"))
    field = match.group("field")
    guarded = copy.deepcopy(dict(repair_value))
    guarded_directives = guarded.get("scene_directives")
    initial_directives = initial_value.get("scene_directives")
    if (
        not isinstance(guarded_directives, list)
        or not isinstance(initial_directives, list)
        or scene_index >= len(guarded_directives)
        or scene_index >= len(initial_directives)
        or not isinstance(guarded_directives[scene_index], Mapping)
        or not isinstance(initial_directives[scene_index], Mapping)
    ):
        raise error
    guarded_directive = dict(guarded_directives[scene_index])
    initial_directive = initial_directives[scene_index]
    if field in initial_directive:
        guarded_directive[field] = copy.deepcopy(initial_directive[field])
    else:
        guarded_directive.pop(field, None)
    guarded_directives[scene_index] = guarded_directive
    return guarded


def _visible_text_target_context(
    value: Mapping[str, Any],
    error: DirectorDecisionError,
    candidates: Sequence[Any],
) -> dict[str, Any]:
    """Expose only safe identity and enum data for one visible-text repair."""

    match = _VISIBLE_TEXT_REFERENCE_PATH.fullmatch(error.path)
    if error.code != "director_decision_schema_invalid" or match is None:
        raise error
    scene_index = int(match.group("scene_index"))
    field = match.group("field")
    directives = value.get("scene_directives")
    candidate_records = tuple(_record(item) for item in candidates)
    if (
        not isinstance(directives, list)
        or scene_index >= len(directives)
        or scene_index >= len(candidate_records)
        or not isinstance(directives[scene_index], Mapping)
    ):
        raise error
    directive = directives[scene_index]
    candidate = candidate_records[scene_index]
    scene_id = directive.get("scene_id")
    caption_ids = candidate.get("caption_ids")
    if (
        not isinstance(scene_id, str)
        or _SAFE_CODE.fullmatch(scene_id) is None
        or scene_id != candidate.get("id")
        or not isinstance(caption_ids, (list, tuple))
        or not caption_ids
        or any(
            not isinstance(item, str)
            or re.fullmatch(r"caption_[0-9]{3}", item) is None
            for item in caption_ids
        )
    ):
        raise error
    overlays = directive.get("overlay_instances")
    omission_allowed = isinstance(overlays, list) and not any(
        isinstance(item, Mapping) and item.get("content_ref") == field
        for item in overlays
    )
    return {
        "scene_id": scene_id,
        "field": field,
        "allowed_text_kinds": ["verbatim", "compressed"],
        "allowed_source_caption_ids": list(caption_ids),
        "omission_allowed": omission_allowed,
    }


def _apply_scoped_visible_text_repair(
    repair_value: Mapping[str, Any],
    initial_value: Mapping[str, Any],
    error: DirectorDecisionError,
    candidates: Sequence[Any],
) -> dict[str, Any]:
    """Take only one repaired visible-text field and freeze all other fields."""

    try:
        contracts.validate_director_decision_schema(repair_value)
    except ContractError as exc:
        raise DirectorDecisionError(exc.error_code, exc.field_path) from None
    _reject_unsafe_text(repair_value)
    target = _visible_text_target_context(initial_value, error, candidates)
    match = _VISIBLE_TEXT_REFERENCE_PATH.fullmatch(error.path)
    if match is None:
        raise error
    scene_index = int(match.group("scene_index"))
    field = match.group("field")
    repair_directives = repair_value.get("scene_directives")
    if (
        not isinstance(repair_directives, list)
        or scene_index >= len(repair_directives)
        or not isinstance(repair_directives[scene_index], Mapping)
        or repair_directives[scene_index].get("scene_id") != target["scene_id"]
    ):
        raise error
    repair_directive = repair_directives[scene_index]
    recovered = copy.deepcopy(dict(initial_value))
    recovered_directive = recovered["scene_directives"][scene_index]
    if field in repair_directive:
        recovered_directive[field] = copy.deepcopy(repair_directive[field])
    elif target["omission_allowed"] is True:
        recovered_directive.pop(field, None)
    else:
        raise error
    return recovered


def _repair_constraint_envelope(
    candidates: Sequence[Any], capabilities: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Project authoritative timing constraints without transcript or media data."""

    policy = capabilities.get("speaker_visibility_policy")
    if policy is None:
        return None
    if policy != SPEAKER_VISIBILITY_POLICY:
        raise DirectorDecisionError(
            "director_capabilities_invalid",
            "$.capabilities.speaker_visibility_policy",
        )
    bounds = []
    total_duration_ms = 0
    for index, candidate_value in enumerate(candidates):
        candidate = _record(candidate_value)
        scene_id = candidate.get("id")
        start_ms = candidate.get("start_ms")
        end_ms = candidate.get("end_ms")
        speaker_available = candidate.get("speaker_available")
        if (
            not isinstance(scene_id, str)
            or _SAFE_CODE.fullmatch(scene_id) is None
            or type(start_ms) is not int
            or type(end_ms) is not int
            or start_ms < 0
            or end_ms <= start_ms
            or type(speaker_available) is not bool
        ):
            raise DirectorDecisionError(
                "director_candidates_invalid", f"$.scene_candidates[{index}]"
            )
        duration_ms = end_ms - start_ms
        total_duration_ms += duration_ms
        bounds.append({
            "scene_id": scene_id,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "duration_ms": duration_ms,
            "speaker_available": speaker_available,
        })
    return {
        "speaker_visibility_policy": copy.deepcopy(dict(policy)),
        "scene_candidate_bounds": bounds,
        "total_duration_ms": total_duration_ms,
        "max_hidden_ms": int(
            total_duration_ms * SPEAKER_VISIBILITY_POLICY["max_hidden_ratio"]
        ),
    }


def _material_purpose_target_context(
    value: Mapping[str, Any],
    error: DirectorDecisionError,
    capabilities: Mapping[str, Any],
) -> dict[str, Any]:
    """Expose only safe enum/identity data required for one scoped repair."""

    directive, slot, allowed, required = _material_purpose_context(
        value, error, capabilities
    )
    scene_id = directive.get("scene_id")
    layout_id = directive.get("layout_id")
    slot_id = slot.get("slot_id")
    priority = slot.get("priority")
    if any(
        not isinstance(item, str) or _SAFE_CODE.fullmatch(item) is None
        for item in (scene_id, layout_id, slot_id, priority)
    ):
        raise error
    return {
        "scene_id": scene_id,
        "layout_id": layout_id,
        "slot_id": slot_id,
        "slot_priority": priority,
        "allowed_purposes": list(allowed),
        "required_purposes": list(required),
    }


def generate_director_decision(context: Any, provider: Any, *, max_repairs: int = 1) -> ValidatedDecision:
    if max_repairs != 1:
        raise ValueError("director_repair_budget_invalid")
    frozen_request = copy.deepcopy(dict(context.request))
    _reject_unsafe_request_keys(frozen_request)
    previous_sha = None
    last_error = DirectorDecisionError("director_decision_invalid")
    attempts: list[dict[str, Any]] = []
    initial_speaker_candidate: tuple[dict[str, Any], str | None, str, str] | None = None
    initial_material_candidate: tuple[dict[str, Any], DirectorDecisionError] | None = None
    initial_visible_text_candidate: tuple[
        dict[str, Any], DirectorDecisionError
    ] | None = None
    constraint_envelope = _repair_constraint_envelope(
        context.candidates, context.capabilities
    )
    for attempt in range(max_repairs + 1):
        if attempt == 0:
            request = frozen_request
        else:
            repair_request: dict[str, Any] = {
                "error_code": last_error.code,
                "field_path": last_error.path,
                "expected_constraint": _repair_expected_constraint(last_error),
            }
            if constraint_envelope is not None:
                repair_request["constraint_envelope"] = copy.deepcopy(
                    constraint_envelope
                )
            if initial_material_candidate is not None:
                initial_material_value, initial_material_error = (
                    initial_material_candidate
                )
                repair_request["target_context"] = _material_purpose_target_context(
                    initial_material_value,
                    initial_material_error,
                    context.capabilities,
                )
                repair_request["preserve_all_other_fields"] = True
            elif initial_visible_text_candidate is not None:
                initial_visible_value, initial_visible_error = (
                    initial_visible_text_candidate
                )
                repair_request["target_context"] = _visible_text_target_context(
                    initial_visible_value,
                    initial_visible_error,
                    context.candidates,
                )
                repair_request["preserve_all_other_fields"] = True
            request = {
                "frozen_request": frozen_request,
                "previous_response_sha256": previous_sha,
                "repair": repair_request,
            }
        raw = provider.generate_decision(
            request,
            purpose="initial" if attempt == 0 else "repair",
            idempotency_key=f"ai-edit-v3:{context.job_id}:director-decision:{attempt}",
            deadline_at=context.deadline_at,
        )
        request_id = _provider_request_id(raw)
        response_sha256 = _provider_response_sha256(raw)
        parsed_output: tuple[dict[str, Any], str | None, str, str] | None = None
        try:
            parsed_output = _provider_output(raw)
            value, request_id, raw_json, previous_sha = parsed_output
            response_sha256 = previous_sha
            scoped_visible_text_repair = False
            if attempt == max_repairs and initial_material_candidate is not None:
                initial_material_value, initial_material_error = (
                    initial_material_candidate
                )
                value = _apply_scoped_material_purpose_repair(
                    value,
                    initial_material_value,
                    initial_material_error,
                    context.capabilities,
                )
            elif (
                attempt == max_repairs
                and initial_visible_text_candidate is not None
            ):
                initial_visible_value, initial_visible_error = (
                    initial_visible_text_candidate
                )
                value = _apply_scoped_visible_text_repair(
                    value,
                    initial_visible_value,
                    initial_visible_error,
                    context.candidates,
                )
                scoped_visible_text_repair = True
            if scoped_visible_text_repair:
                normalized = validate_director_decision(
                    value,
                    candidates=context.candidates,
                    capabilities=context.capabilities,
                )
            else:
                normalized = _validate_generated_decision(
                    value,
                    candidates=context.candidates,
                    capabilities=context.capabilities,
                    allow_speaker_fallback=attempt == max_repairs,
                )
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
            if (
                attempt == 0
                and exc.code == "director_decision_schema_invalid"
                and _VISIBLE_TEXT_REFERENCE_PATH.fullmatch(exc.path) is not None
                and parsed_output is not None
            ):
                initial_visible_text_candidate = (
                    copy.deepcopy(parsed_output[0]),
                    exc,
                )
            elif (
                attempt == 0
                and exc.code == "director_material_purpose_invalid"
                and _MATERIAL_PURPOSE_PATH.fullmatch(exc.path) is not None
                and parsed_output is not None
            ):
                initial_material_candidate = (
                    copy.deepcopy(parsed_output[0]),
                    exc,
                )
            elif (
                attempt == 0
                and exc.code == "director_speaker_visibility_exceeded"
                and exc.path == "$.scene_directives"
                and parsed_output is not None
            ):
                value, request_id, raw_json, initial_sha = parsed_output
                initial_value, initial_error = _validate_with_local_recoveries(
                    value,
                    candidates=context.candidates,
                    capabilities=context.capabilities,
                )
                if (
                    initial_error is not None
                    and initial_error.code == "director_speaker_visibility_exceeded"
                    and initial_error.path == "$.scene_directives"
                ):
                    initial_speaker_candidate = (
                        initial_value,
                        request_id,
                        raw_json,
                        initial_sha,
                    )
            elif (
                attempt == max_repairs
                and initial_speaker_candidate is not None
                and parsed_output is not None
                and exc.code == "director_decision_schema_invalid"
                and _VISIBLE_TEXT_REFERENCE_PATH.fullmatch(exc.path) is not None
            ):
                initial_value, initial_request_id, initial_raw_json, initial_sha = (
                    initial_speaker_candidate
                )
                try:
                    repair_value = parsed_output[0]
                    _reject_unsafe_text(repair_value)
                    guarded_repair = _without_visible_text_schema_regression(
                        repair_value,
                        initial_value,
                        exc,
                    )
                    validate_director_decision(
                        guarded_repair,
                        candidates=context.candidates,
                        capabilities=context.capabilities,
                    )
                    normalized = _validate_generated_decision(
                        initial_value,
                        candidates=context.candidates,
                        capabilities=context.capabilities,
                        allow_speaker_fallback=True,
                    )
                except DirectorDecisionError:
                    pass
                else:
                    normalized_json = contracts.canonical_json(normalized)
                    candidate_json = contracts.canonical_json(
                        [_record(item) for item in context.candidates]
                    )
                    return ValidatedDecision(
                        normalized,
                        initial_request_id,
                        initial_raw_json,
                        initial_sha,
                        hashlib.sha256(normalized_json).hexdigest(),
                        contracts.schema_sha256("director-decision-v1.schema.json"),
                        hashlib.sha256(candidate_json).hexdigest(),
                    )
            elif (
                attempt == max_repairs
                and initial_speaker_candidate is not None
                and parsed_output is not None
                and exc.code == "director_speaker_visibility_exceeded"
                and exc.path == "$.scene_directives"
            ):
                initial_value, initial_request_id, initial_raw_json, initial_sha = (
                    initial_speaker_candidate
                )
                try:
                    _reject_unsafe_text(parsed_output[0])
                    normalized = _validate_generated_decision(
                        initial_value,
                        candidates=context.candidates,
                        capabilities=context.capabilities,
                        allow_speaker_fallback=True,
                    )
                except DirectorDecisionError:
                    pass
                else:
                    normalized_json = contracts.canonical_json(normalized)
                    candidate_json = contracts.canonical_json(
                        [_record(item) for item in context.candidates]
                    )
                    return ValidatedDecision(
                        normalized,
                        initial_request_id,
                        initial_raw_json,
                        initial_sha,
                        hashlib.sha256(normalized_json).hexdigest(),
                        contracts.schema_sha256("director-decision-v1.schema.json"),
                        hashlib.sha256(candidate_json).hexdigest(),
                    )
            last_error = exc
            previous_sha = response_sha256
            attempts.append({
                "attempt": attempt + 1,
                "purpose": "initial" if attempt == 0 else "repair",
                "request_id": request_id,
                "response_sha256": response_sha256,
                "validation_code": exc.code,
                "field_path": exc.path,
            })
    raise DirectorDecisionError(
        "director_decision_invalid",
        last_error.path,
        detail_code=last_error.code,
        attempts=attempts,
    )
