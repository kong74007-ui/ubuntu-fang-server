"""Talking material configuration and cue window helpers."""

from __future__ import annotations

import re


AVATAR_ASSET_RE = re.compile(r"^avatar_[0-9a-f]{32}$")
DEFAULT_TALKING_RATIO = 0.3
MIN_TALKING_RATIO = 0.1
MAX_TALKING_RATIO = 0.5
MAX_WINDOW_DURATION = 6.0


def _default_disabled_config() -> dict:
    return {
        "enabled": False,
        "ratio": DEFAULT_TALKING_RATIO,
        "default_avatar_asset_id": "",
        "scenes": [],
    }


def _scene_id(value: object) -> str:
    return str(value or "")


def _avatar_id(value: object) -> str:
    return str(value or "")


def normalize_talking_material(raw: dict | None, scene_ids: list[str]) -> dict:
    value = dict(raw or {})
    if not value.get("enabled"):
        return _default_disabled_config()

    ratio = float(value.get("ratio", DEFAULT_TALKING_RATIO))
    if not MIN_TALKING_RATIO <= ratio <= MAX_TALKING_RATIO:
        raise ValueError("talking ratio must be between 0.1 and 0.5")

    default_avatar_asset_id = _avatar_id(value.get("default_avatar_asset_id"))
    if not AVATAR_ASSET_RE.fullmatch(default_avatar_asset_id):
        raise ValueError("default avatar asset is required")

    allowed_scene_ids = set(scene_ids)
    scenes: list[dict] = []
    for item in value.get("scenes") or []:
        scene_id = _scene_id(item.get("scene_id"))
        if scene_id not in allowed_scene_ids:
            raise ValueError("unknown scene_id")
        avatar_asset_id = _avatar_id(item.get("avatar_asset_id"))
        if avatar_asset_id and not AVATAR_ASSET_RE.fullmatch(avatar_asset_id):
            raise ValueError("invalid avatar asset id")
        scenes.append(
            {
                "scene_id": scene_id,
                "enabled": bool(item.get("enabled")),
                "avatar_asset_id": avatar_asset_id,
            }
        )

    return {
        "enabled": True,
        "ratio": ratio,
        "default_avatar_asset_id": default_avatar_asset_id,
        "scenes": scenes,
    }


def recommend_scene_ids(scenes: list[dict], ratio: float) -> list[str]:
    if not scenes:
        return []

    target = max(1, round(len(scenes) * float(ratio)))
    target = min(target, len(scenes))

    center = (len(scenes) - 1) / 2.0
    selected_indices: list[int] = [0]
    if len(scenes) > 1:
        selected_indices.append(len(scenes) - 1)

    def is_adjacent_to_selected_interior(index: int) -> bool:
        return any(abs(index - selected_index) == 1 for selected_index in selected_indices if 0 < selected_index < len(scenes) - 1)

    interior = sorted(
        range(1, len(scenes) - 1),
        key=lambda index: (abs(index - center), index),
    )

    for index in interior:
        if len(selected_indices) >= target:
            break
        if is_adjacent_to_selected_interior(index):
            continue
        selected_indices.append(index)

    if len(selected_indices) < target:
        for index in interior:
            if len(selected_indices) >= target:
                break
            if index in selected_indices:
                continue
            selected_indices.append(index)

    selected = [_scene_id(scenes[index].get("scene_id")) for index in selected_indices[:target]]

    return selected


def _cue_duration(cue: dict) -> float:
    duration = cue.get("duration")
    if not isinstance(duration, (int, float)) or float(duration) <= 0:
        raise ValueError("cue duration must be positive")
    return float(duration)


def _window(cue_start: int, cue_end: int, duration: float) -> dict:
    return {
        "cue_start": cue_start,
        "cue_end": cue_end,
        "duration": round(duration, 10),
    }


def build_talking_windows(cues: list[dict], enabled: bool) -> list[dict]:
    if not enabled or not cues:
        return []

    windows: list[dict] = []
    window_start = 0
    accumulated = 0.0

    for index, cue in enumerate(cues):
        duration = _cue_duration(cue)
        if accumulated and accumulated + duration > MAX_WINDOW_DURATION:
            windows.append(_window(window_start, index, accumulated))
            window_start = index
            accumulated = 0.0
        accumulated += duration

    if accumulated <= 0:
        return windows

    windows.append(_window(window_start, len(cues), accumulated))
    return windows
