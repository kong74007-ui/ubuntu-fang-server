"""Read and enforce the renderer-owned immutable overlay placement catalog."""
from __future__ import annotations

import copy
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .contracts import ContractError, parse_strict_json


_CATALOG_RELATIVE_PATH = Path("src/registry/overlays/overlay-placement-v1.json")
_RATIOS = {"16:9", "9:16"}
_PLACEMENTS = {"title_safe", "subtitle_safe", "left_panel", "right_panel", "center", "lower_third"}


def load_overlay_placement_catalog(renderer_root: Path) -> dict[str, Any]:
    path = Path(renderer_root).resolve() / _CATALOG_RELATIVE_PATH
    try:
        value = parse_strict_json(
            path.read_bytes(), max_bytes=512 * 1024, max_depth=12,
            max_items=5000, max_string_chars=128,
        )
    except (OSError, ContractError) as exc:
        raise ValueError("overlay_placement_catalog_invalid") from exc
    return validate_overlay_placement_catalog(value)


def validate_overlay_placement_catalog(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {"version", "entries"} or value.get("version") != "overlay-placement-v1":
        raise ValueError("overlay_placement_catalog_invalid")
    entries = value.get("entries")
    if not isinstance(entries, list) or not entries:
        raise ValueError("overlay_placement_catalog_invalid")
    expected_fields = {
        "component_id", "placement", "ratio", "max_chars", "max_lines",
        "font_size_steps", "line_height", "content_box", "host_box", "chrome",
    }
    seen: set[tuple[str, str, str]] = set()
    for entry in entries:
        if not isinstance(entry, Mapping) or set(entry) != expected_fields:
            raise ValueError("overlay_placement_catalog_invalid")
        component_id, placement, ratio = entry.get("component_id"), entry.get("placement"), entry.get("ratio")
        identity = (component_id, placement, ratio)
        if not isinstance(component_id, str) or re.fullmatch(r"[a-z][a-z0-9_]*", component_id) is None or placement not in _PLACEMENTS or ratio not in _RATIOS or identity in seen:
            raise ValueError("overlay_placement_catalog_invalid")
        seen.add(identity)
        if any(isinstance(entry.get(name), bool) or not isinstance(entry.get(name), int) for name in ("max_chars", "max_lines")) or not 1 <= entry["max_chars"] <= 480 or not 1 <= entry["max_lines"] <= 12:
            raise ValueError("overlay_placement_catalog_invalid")
        steps = entry.get("font_size_steps")
        if not isinstance(steps, list) or not steps or any(isinstance(item, bool) or not isinstance(item, int) or item < 20 for item in steps) or any(left <= right for left, right in zip(steps, steps[1:])):
            raise ValueError("overlay_placement_catalog_invalid")
        if isinstance(entry.get("line_height"), bool) or not isinstance(entry.get("line_height"), (int, float)) or not 1 <= entry["line_height"] <= 2:
            raise ValueError("overlay_placement_catalog_invalid")
        if not _valid_box(entry.get("content_box")) or not _valid_box(entry.get("host_box")):
            raise ValueError("overlay_placement_catalog_invalid")
        chrome = entry.get("chrome")
        if not isinstance(chrome, Mapping) or set(chrome) != {"width", "height", "content_height_factor"} or any(isinstance(item, bool) or not isinstance(item, (int, float)) or item < 0 for item in chrome.values()):
            raise ValueError("overlay_placement_catalog_invalid")
        if entry["content_box"]["width"] + chrome["width"] > entry["host_box"]["width"] or entry["content_box"]["height"] * chrome["content_height_factor"] + chrome["height"] > entry["host_box"]["height"]:
            raise ValueError("overlay_placement_catalog_invalid")
    return copy.deepcopy(dict(value))


def overlay_budget_index(capabilities: Mapping[str, Any]) -> dict[tuple[str, str, str], Mapping[str, Any]]:
    catalog = validate_overlay_placement_catalog(capabilities.get("overlay_placement_budgets"))
    return {(entry["component_id"], entry["placement"], entry["ratio"]): entry for entry in catalog["entries"]}


def validate_overlay_projection(
    capabilities: Mapping[str, Any], *, component_id: str, placement: str,
    ratio: str, text: str | None = None,
) -> Mapping[str, Any]:
    budget = overlay_budget_index(capabilities).get((component_id, placement, ratio))
    if budget is None:
        raise ValueError("director_overlay_placement_invalid")
    if text is not None and (not isinstance(text, str) or not text or len(text) > budget["max_chars"] or len(text.split("\n")) > budget["max_lines"]):
        raise ValueError("director_overlay_text_budget_exceeded")
    return budget


def _valid_box(value: Any) -> bool:
    return isinstance(value, Mapping) and set(value) == {"width", "height"} and all(
        not isinstance(item, bool) and isinstance(item, int) and item > 0
        for item in value.values()
    )


__all__ = (
    "load_overlay_placement_catalog", "overlay_budget_index",
    "validate_overlay_placement_catalog", "validate_overlay_projection",
)
