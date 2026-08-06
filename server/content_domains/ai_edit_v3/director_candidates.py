from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .transcript import TextTimeline


_MIN_SCENE_DURATION_MS = 500


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
    caption_texts: tuple[tuple[str, str], ...] = ()


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
    min_scenes: int = 1,
) -> list[int] | None:
    if min_scenes < 1 or min_scenes > max_scenes:
        raise ValueError("director_scene_partition_invalid")
    positions = [0] + [int(item["start_ms"]) for item in captions[1:]] + [duration_ms]
    last_index = len(positions) - 1
    reachable = [False] * len(positions)
    reachable[0] = True
    predecessor_layers: list[list[int]] = []

    for scene_count in range(1, max_scenes + 1):
        predecessors = [-1] * len(positions)
        eligible: deque[int] = deque()
        add_index = 0
        for target_index in range(1, len(positions)):
            earliest_position = positions[target_index] - budget_ms
            latest_position = positions[target_index] - _MIN_SCENE_DURATION_MS
            while (
                add_index < target_index
                and positions[add_index] <= latest_position
            ):
                if reachable[add_index]:
                    eligible.append(add_index)
                add_index += 1
            while eligible and positions[eligible[0]] < earliest_position:
                eligible.popleft()
            if eligible:
                predecessors[target_index] = eligible[-1]
        predecessor_layers.append(predecessors)
        if scene_count >= min_scenes and predecessors[last_index] >= 0:
            path = [last_index]
            current_index = last_index
            for layer_index in range(scene_count - 1, -1, -1):
                current_index = predecessor_layers[layer_index][current_index]
                if current_index < 0:
                    return None
                path.append(current_index)
            path.reverse()
            return path[:-1]
        reachable = [index >= 0 for index in predecessors]
    return None


def _scene_rhythm_minimum(
    captions: list[Mapping[str, Any]],
    *,
    duration_ms: int,
    max_scenes: int,
) -> int:
    desired = (
        3
        if max_scenes >= 3 and duration_ms >= 12_000 and len(captions) >= 3
        else 1
    )
    if desired == 1:
        return 1
    feasible = _partition_group_starts(
        captions,
        duration_ms=duration_ms,
        budget_ms=duration_ms,
        max_scenes=max_scenes,
        min_scenes=desired,
    )
    return desired if feasible is not None else 1


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
                if (
                    _MIN_SCENE_DURATION_MS <= left <= budget_ms
                    and _MIN_SCENE_DURATION_MS <= right <= budget_ms
                ):
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
    min_scenes = _scene_rhythm_minimum(
        captions,
        duration_ms=duration,
        max_scenes=max_scenes,
    )
    baseline_budget_ms = max(8000, max(end - start for start, end in zip(starts, ends, strict=True)))
    lower = baseline_budget_ms
    upper = max(lower, duration)
    while lower < upper:
        candidate = (lower + upper) // 2
        if _partition_group_starts(
            captions,
            duration_ms=duration,
            budget_ms=candidate,
            max_scenes=max_scenes,
            min_scenes=min_scenes,
        ) is not None:
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
    min_scenes = _scene_rhythm_minimum(
        captions,
        duration_ms=duration_ms,
        max_scenes=max_scenes,
    )
    groups = _natural_caption_groups(captions, duration_ms=duration_ms, budget_ms=budget_ms, max_scenes=max_scenes)
    natural_spans = _compiled_scene_spans(groups, duration_ms)
    if (
        len(groups) > max_scenes
        or len(groups) < min_scenes
        or min(natural_spans) < _MIN_SCENE_DURATION_MS
        or max(natural_spans) > budget_ms
    ):
        group_starts = _partition_group_starts(
            captions,
            duration_ms=duration_ms,
            budget_ms=budget_ms,
            max_scenes=max_scenes,
            min_scenes=min_scenes,
        )
        if group_starts is None:
            raise ValueError("director_scene_partition_invalid")
        groups = _groups_from_starts(captions, group_starts)
    try:
        groups = _ensure_minimum_scene_groups(
            groups,
            duration_ms=duration_ms,
            budget_ms=budget_ms,
            min_scenes=min_scenes,
        )
    except ValueError:
        group_starts = _partition_group_starts(
            captions,
            duration_ms=duration_ms,
            budget_ms=budget_ms,
            max_scenes=max_scenes,
            min_scenes=min_scenes,
        )
        if group_starts is None:
            raise
        groups = _groups_from_starts(captions, group_starts)
    spans = _compiled_scene_spans(groups, duration_ms)
    if (
        len(groups) > max_scenes
        or min(spans) < _MIN_SCENE_DURATION_MS
        or max(spans) > budget_ms
    ):
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
            caption_texts=tuple((str(item["id"]), str(item["text"])) for item in group),
        ))
    return tuple(result)
