from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .transcript import TextTimeline


@dataclass(frozen=True, slots=True)
class SceneCandidate:
    id: str
    start_ms: int
    end_ms: int
    caption_ids: tuple[str, ...]
    authoritative_text: str
    protected_fact_ids: tuple[str, ...]
    available_material_ids: tuple[str, ...]
    speaker_available: bool


def _compiled_scene_spans(
    groups: list[list[Mapping[str, Any]]],
    duration_ms: int,
) -> list[int]:
    starts = [0] + [int(group[0]["start_ms"]) for group in groups[1:]]
    ends = [int(group[0]["start_ms"]) for group in groups[1:]] + [duration_ms]
    return [end - start for start, end in zip(starts, ends, strict=True)]


def _partition_group_starts(
    captions: list[Mapping[str, Any]],
    *,
    duration_ms: int,
    budget_ms: int,
    max_scenes: int,
) -> list[int] | None:
    positions = [0] + [int(item["start_ms"]) for item in captions[1:]] + [duration_ms]
    group_starts = [0]
    position_index = 0
    scene_count = 0
    last_index = len(positions) - 1
    while position_index < last_index:
        next_index = position_index + 1
        while next_index < last_index and positions[next_index + 1] - positions[position_index] <= budget_ms:
            next_index += 1
        if positions[next_index] - positions[position_index] > budget_ms:
            return None
        position_index = next_index
        scene_count += 1
        if scene_count > max_scenes:
            return None
        if position_index < last_index:
            group_starts.append(position_index)
    return group_starts


def _natural_caption_groups(
    captions: list[Mapping[str, Any]],
    *,
    duration_ms: int,
    budget_ms: int,
    max_scenes: int,
) -> list[list[Mapping[str, Any]]]:
    target_ms = max(2500, math.ceil(duration_ms / max_scenes))
    groups: list[list[Mapping[str, Any]]] = []
    current: list[Mapping[str, Any]] = []
    for caption in captions:
        if current and int(caption["end_ms"]) - int(current[0]["start_ms"]) > budget_ms:
            groups.append(current)
            current = []
        current.append(caption)
        if int(current[-1]["end_ms"]) - int(current[0]["start_ms"]) >= target_ms:
            groups.append(current)
            current = []
    if current:
        previous_start = 0 if len(groups) <= 1 else int(groups[-1][0]["start_ms"])
        if groups and int(current[-1]["end_ms"]) - int(current[0]["start_ms"]) < 1400 and duration_ms - previous_start <= budget_ms:
            groups[-1].extend(current)
        else:
            groups.append(current)
    return groups


def _groups_from_starts(
    captions: list[Mapping[str, Any]],
    group_starts: list[int],
) -> list[list[Mapping[str, Any]]]:
    ends = group_starts[1:] + [len(captions)]
    return [captions[start:end] for start, end in zip(group_starts, ends, strict=True)]


def _ensure_minimum_scene_groups(
    groups: list[list[Mapping[str, Any]]],
    *,
    duration_ms: int,
    budget_ms: int,
    min_scenes: int,
) -> list[list[Mapping[str, Any]]]:
    while len(groups) < min_scenes:
        candidates: list[tuple[tuple[int, int, int, int], int, int]] = []
        for group_index, group in enumerate(groups):
            if len(group) < 2:
                continue
            scene_start = 0 if group_index == 0 else int(group[0]["start_ms"])
            scene_end = duration_ms if group_index == len(groups) - 1 else int(groups[group_index + 1][0]["start_ms"])
            for split_index in range(1, len(group)):
                boundary = int(group[split_index]["start_ms"])
                left = boundary - scene_start
                right = scene_end - boundary
                if left <= budget_ms and right <= budget_ms:
                    candidates.append(((max(left, right), abs(left - right), group_index, split_index), group_index, split_index))
        if not candidates:
            raise ValueError("director_scene_partition_invalid")
        _, group_index, split_index = min(candidates)
        group = groups[group_index]
        groups[group_index:group_index + 1] = [group[:split_index], group[split_index:]]
    return groups


def _scene_duration_budget(
    captions: list[Mapping[str, Any]],
    *,
    duration_ms: int | None = None,
    max_scenes: int = 12,
) -> int:
    if not captions or max_scenes < 1:
        raise ValueError("director_captions_missing")
    starts = [int(item["start_ms"]) for item in captions]
    ends = [int(item["end_ms"]) for item in captions]
    duration = ends[-1] if duration_ms is None else duration_ms
    if isinstance(duration, bool) or not isinstance(duration, int) or duration < ends[-1]:
        raise ValueError("director_duration_invalid")
    baseline_budget_ms = max(8000, max(end - start for start, end in zip(starts, ends, strict=True)))
    lower = baseline_budget_ms
    upper = max(lower, duration)
    while lower < upper:
        candidate = (lower + upper) // 2
        if _partition_group_starts(captions, duration_ms=duration, budget_ms=candidate, max_scenes=max_scenes) is not None:
            upper = candidate
        else:
            lower = candidate + 1
    return lower


def _build_caption_groups(
    captions: list[Mapping[str, Any]],
    *,
    duration_ms: int,
    max_scenes: int,
) -> list[list[Mapping[str, Any]]]:
    budget_ms = _scene_duration_budget(captions, duration_ms=duration_ms, max_scenes=max_scenes)
    groups = _natural_caption_groups(captions, duration_ms=duration_ms, budget_ms=budget_ms, max_scenes=max_scenes)
    if len(groups) > max_scenes or max(_compiled_scene_spans(groups, duration_ms)) > budget_ms:
        group_starts = _partition_group_starts(captions, duration_ms=duration_ms, budget_ms=budget_ms, max_scenes=max_scenes)
        if group_starts is None:
            raise ValueError("director_scene_partition_invalid")
        groups = _groups_from_starts(captions, group_starts)
    min_scenes = 3 if duration_ms >= 12000 and len(captions) >= 3 else 1
    groups = _ensure_minimum_scene_groups(groups, duration_ms=duration_ms, budget_ms=budget_ms, min_scenes=min_scenes)
    if len(groups) > max_scenes or max(_compiled_scene_spans(groups, duration_ms)) > budget_ms:
        raise ValueError("director_scene_partition_invalid")
    return groups


def _material_id(material: Any) -> str:
    value = material.get("material_id") if isinstance(material, Mapping) else getattr(material, "material_id", None)
    if not isinstance(value, str) or not value:
        raise ValueError("director_material_identity_invalid")
    return value


def build_scene_candidates(
    timeline: TextTimeline,
    materials: Sequence[Any],
    *,
    ratio: str,
    input_type: str,
    max_scenes: int = 12,
) -> tuple[SceneCandidate, ...]:
    if not isinstance(timeline, TextTimeline) or not timeline.captions:
        raise ValueError("director_captions_missing")
    if ratio not in {"16:9", "9:16", "auto"}:
        raise ValueError("director_ratio_invalid")
    material_ids = tuple(sorted(_material_id(item) for item in materials))
    if len(material_ids) != len(set(material_ids)):
        raise ValueError("director_material_identity_invalid")
    captions = [
        {"id": item.id, "text": item.text, "start_ms": item.start_ms, "end_ms": item.end_ms}
        for item in timeline.captions
    ]
    groups = _build_caption_groups(captions, duration_ms=timeline.duration_ms, max_scenes=max_scenes)
    starts = [0] + [int(group[0]["start_ms"]) for group in groups[1:]]
    ends = starts[1:] + [timeline.duration_ms]
    speaker_available = input_type in {"platform_talking_head", "uploaded_video"}
    result: list[SceneCandidate] = []
    for index, (group, start_ms, end_ms) in enumerate(zip(groups, starts, ends, strict=True), 1):
        protected = tuple(
            segment.id
            for segment in timeline.source_segments
            if segment.protected and segment.start_ms < end_ms and segment.end_ms > start_ms
        )
        result.append(SceneCandidate(
            id=f"candidate_{index:02d}",
            start_ms=start_ms,
            end_ms=end_ms,
            caption_ids=tuple(str(item["id"]) for item in group),
            authoritative_text="".join(str(item["text"]) for item in group),
            protected_fact_ids=protected,
            available_material_ids=material_ids,
            speaker_available=speaker_available,
        ))
    return tuple(result)
