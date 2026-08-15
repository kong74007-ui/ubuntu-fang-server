"""Talking material configuration and cue window helpers."""

from __future__ import annotations

import re


AVATAR_ASSET_RE = re.compile(r"^avatar_[0-9a-f]{32}$")
DEFAULT_TALKING_RATIO = 0.3
MIN_TALKING_RATIO = 0.1
MAX_TALKING_RATIO = 0.5
MIN_WINDOW_DURATION = 3.0
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

    ordered_indices = [0]
    if len(scenes) > 1:
        ordered_indices.append(len(scenes) - 1)

    center = (len(scenes) - 1) / 2.0
    interior = [
        index
        for index in range(1, len(scenes) - 1)
    ]
    interior.sort(key=lambda index: (abs(index - center), index))
    ordered_indices.extend(interior)

    selected: list[str] = []
    for index in ordered_indices:
        scene_id = _scene_id(scenes[index].get("scene_id"))
        if not scene_id or scene_id in selected:
            continue
        selected.append(scene_id)
        if len(selected) >= target:
            break

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
        accumulated += _cue_duration(cue)
        if accumulated >= MIN_WINDOW_DURATION:
            windows.append(_window(window_start, index + 1, accumulated))
            window_start = index + 1
            accumulated = 0.0

    if accumulated <= 0:
        return windows

    if windows and accumulated < MIN_WINDOW_DURATION:
        previous = windows[-1]
        if previous["duration"] < MIN_WINDOW_DURATION and previous["duration"] + accumulated <= MAX_WINDOW_DURATION:
            previous["cue_end"] = len(cues)
            previous["duration"] = round(previous["duration"] + accumulated, 10)
            return windows

    windows.append(_window(window_start, len(cues), accumulated))
    return windows
