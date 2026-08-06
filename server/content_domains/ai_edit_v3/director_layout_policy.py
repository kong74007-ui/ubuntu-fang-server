"""Frozen layout constraints shared by the V3 director prompt and validator."""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from typing import Any


MAX_REQUIRED_MATERIAL_SLOTS = 6
MAX_TOTAL_MATERIAL_SLOTS = 40

SPEAKER_VISIBILITY_POLICY = {
    "opening_requires_speaker": True,
    "max_hidden_ratio": 0.4,
}

SCENE_STRUCTURE_POLICY = {
    "min_duration_ms": 12000,
    "min_scenes": 3,
    "minimum_distinct_signatures": 2,
    "max_adjacent_identical": 2,
}


_POLICIES: dict[str, dict[str, Any]] = {
    "speaker_fullscreen": {
        "speaker_required": True,
        "semantic_slots": [
            {"layout_slot_id": "evidence", "purpose": "evidence", "required_for_layout": False},
        ],
    },
    "speaker_left_info_right": {
        "speaker_required": True,
        "semantic_slots": [
            {"layout_slot_id": "evidence", "purpose": "evidence", "required_for_layout": True},
        ],
    },
    "speaker_right_evidence_left": {
        "speaker_required": True,
        "semantic_slots": [
            {"layout_slot_id": "evidence", "purpose": "evidence", "required_for_layout": True},
        ],
    },
    "material_fullscreen_speaker_pip": {
        "speaker_required": True,
        "semantic_slots": [
            {"layout_slot_id": "primary", "purpose": "product", "required_for_layout": True},
            {"layout_slot_id": "detail", "purpose": "context", "required_for_layout": False},
        ],
    },
    "product_hero": {
        "speaker_required": False,
        "semantic_slots": [
            {"layout_slot_id": "primary", "purpose": "product", "required_for_layout": True},
            {"layout_slot_id": "detail", "purpose": "context", "required_for_layout": False},
        ],
    },
    "editorial_collage": {
        "speaker_required": False,
        "semantic_slots": [
            {"layout_slot_id": "primary", "purpose": "product", "required_for_layout": True},
            {"layout_slot_id": "detail", "purpose": "context", "required_for_layout": False},
        ],
    },
    "comparison_split": {
        "speaker_required": False,
        "semantic_slots": [
            {"layout_slot_id": "primary", "purpose": "product", "required_for_layout": True},
            {"layout_slot_id": "detail", "purpose": "context", "required_for_layout": False},
        ],
    },
    "number_proof": {
        "speaker_required": False,
        "semantic_slots": [
            {"layout_slot_id": "evidence", "purpose": "evidence", "required_for_layout": False},
        ],
    },
    "quote_reversal": {
        "speaker_required": False,
        "semantic_slots": [
            {"layout_slot_id": "evidence", "purpose": "evidence", "required_for_layout": False},
        ],
    },
    "steps_stack": {
        "speaker_required": False,
        "semantic_slots": [
            {"layout_slot_id": "accent", "purpose": "decoration", "required_for_layout": False},
        ],
    },
    "method_timeline": {
        "speaker_required": False,
        "semantic_slots": [
            {"layout_slot_id": "accent", "purpose": "decoration", "required_for_layout": False},
        ],
    },
    "cta_offer": {
        "speaker_required": False,
        "semantic_slots": [
            {"layout_slot_id": "accent", "purpose": "decoration", "required_for_layout": False},
        ],
    },
}


def layout_requirements_for(layout_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
    """Return a fresh, ordered policy projection for a renderer layout list."""

    if not isinstance(layout_ids, (list, tuple)) or not layout_ids:
        raise ValueError("director_layout_policy_invalid")
    result: dict[str, dict[str, Any]] = {}
    for layout_id in layout_ids:
        if not isinstance(layout_id, str) or layout_id not in _POLICIES or layout_id in result:
            raise ValueError("director_layout_policy_invalid")
        result[layout_id] = copy.deepcopy(_POLICIES[layout_id])
    return result


def validate_layout_requirements(
    layout_ids: Sequence[str], requirements: Mapping[str, Any]
) -> dict[str, dict[str, Any]]:
    expected = layout_requirements_for(layout_ids)
    if not isinstance(requirements, Mapping) or dict(requirements) != expected:
        raise ValueError("director_layout_policy_invalid")
    return copy.deepcopy(expected)


def allowed_layout_ids(
    layout_ids: Sequence[str],
    *,
    speaker_available: bool,
    require_speaker: bool = False,
) -> list[str]:
    requirements = layout_requirements_for(layout_ids)
    if require_speaker and not speaker_available:
        raise ValueError("director_layout_source_incompatible")
    allowed = [
        layout_id
        for layout_id, policy in requirements.items()
        if (speaker_available or policy["speaker_required"] is False)
        and (not require_speaker or policy["speaker_required"] is True)
    ]
    if not allowed:
        raise ValueError("director_layout_source_incompatible")
    return allowed


def required_material_layout_ids() -> frozenset[str]:
    return frozenset(
        layout_id
        for layout_id, policy in _POLICIES.items()
        if any(slot["required_for_layout"] for slot in policy["semantic_slots"])
    )


def speaker_layout_ids() -> frozenset[str]:
    return frozenset(
        layout_id
        for layout_id, policy in _POLICIES.items()
        if policy["speaker_required"] is True
    )


def layout_shows_speaker(layout_id: Any) -> bool:
    return isinstance(layout_id, str) and layout_id in speaker_layout_ids()


__all__ = (
    "MAX_REQUIRED_MATERIAL_SLOTS",
    "MAX_TOTAL_MATERIAL_SLOTS",
    "SCENE_STRUCTURE_POLICY",
    "SPEAKER_VISIBILITY_POLICY",
    "allowed_layout_ids",
    "layout_shows_speaker",
    "layout_requirements_for",
    "required_material_layout_ids",
    "speaker_layout_ids",
    "validate_layout_requirements",
)
