"""Read the renderer-owned, release-locked visual capability catalog."""
from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .contracts import ContractError, parse_strict_json


_CATALOG_RELATIVE_PATH = Path("src/registry/visual-capabilities-v1.json")
_VERSION = "ai-edit-v3-visual-capabilities-v1"
_FIELDS = {
    "version",
    "layout_capabilities",
    "layout_variants",
    "overlay_capabilities",
    "overlay_variants",
    "overlay_animation_targets",
    "layout_animation_targets",
    "animation_capabilities",
    "transition_capabilities",
    "theme_capabilities",
    "theme_profile_ids",
    "identity_match_capability",
}
_THEME_FIELDS = {
    "palette_id", "typography_id", "density", "motion_energy", "image_fit",
}
_ID = re.compile(r"[a-z][a-z0-9_]*")


def load_visual_capability_catalog(renderer_root: Path) -> dict[str, Any]:
    path = Path(renderer_root).resolve() / _CATALOG_RELATIVE_PATH
    try:
        value = parse_strict_json(
            path.read_bytes(), max_bytes=128 * 1024, max_depth=12,
            max_items=5000, max_string_chars=128,
        )
    except (OSError, ContractError) as exc:
        raise ValueError("visual_capability_catalog_invalid") from exc
    return validate_visual_capability_catalog(value)


def validate_visual_capability_catalog(value: Any) -> dict[str, Any]:
    if (
        not isinstance(value, Mapping)
        or set(value) != _FIELDS
        or value.get("version") != _VERSION
    ):
        raise ValueError("visual_capability_catalog_invalid")
    layouts = _id_list(value.get("layout_capabilities"), require_nonempty=True)
    overlays = _id_list(value.get("overlay_capabilities"), require_nonempty=True)
    _id_list(value.get("animation_capabilities"), require_nonempty=True)
    transitions = _id_list(
        value.get("transition_capabilities"), require_nonempty=True,
    )
    _id_list(value.get("theme_profile_ids"), require_nonempty=True)
    if (
        value.get("identity_match_capability") is not False
        or "card_match_cut" in transitions
    ):
        raise ValueError("visual_capability_catalog_invalid")
    variants = _catalog_map(
        value.get("layout_variants"), layouts, require_nonempty=True,
    )
    if any("balanced_a" in items for items in variants.values()):
        raise ValueError("visual_capability_catalog_invalid")
    for catalog in (
        _catalog_map(value.get("layout_animation_targets"), layouts),
        _catalog_map(value.get("overlay_variants"), overlays),
        _catalog_map(value.get("overlay_animation_targets"), overlays),
    ):
        if any(catalog.values()):
            raise ValueError("visual_capability_catalog_invalid")
    themes = value.get("theme_capabilities")
    if not isinstance(themes, Mapping) or set(themes) != _THEME_FIELDS:
        raise ValueError("visual_capability_catalog_invalid")
    for items in themes.values():
        _id_list(items, require_nonempty=True)
    return copy.deepcopy(dict(value))


def _id_list(value: Any, *, require_nonempty: bool = False) -> list[str]:
    if (
        not isinstance(value, list)
        or (require_nonempty and not value)
        or any(not isinstance(item, str) or _ID.fullmatch(item) is None for item in value)
        or len(set(value)) != len(value)
    ):
        raise ValueError("visual_capability_catalog_invalid")
    return value


def _catalog_map(
    value: Any, ids: list[str], *, require_nonempty: bool = False,
) -> Mapping[str, list[str]]:
    if not isinstance(value, Mapping) or set(value) != set(ids):
        raise ValueError("visual_capability_catalog_invalid")
    for items in value.values():
        _id_list(items, require_nonempty=require_nonempty)
    return value


__all__ = (
    "load_visual_capability_catalog", "validate_visual_capability_catalog",
)
