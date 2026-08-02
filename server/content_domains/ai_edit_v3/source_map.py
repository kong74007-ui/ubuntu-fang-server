from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Sequence

from .transcript import SourceSegment, TextTimeline


class SourceMapError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Pause:
    start_ms: int
    end_ms: int
    duration_ms: int


@dataclass(frozen=True)
class CandidateSegment:
    id: str
    start_ms: int
    end_ms: int
    duration_ms: int
    protected: bool
    text: str
    pause_after_ms: int


def _validate_source_segments(segments: Sequence[SourceSegment]) -> None:
    if not segments:
        raise SourceMapError("source_timeline_empty")
    previous_end: int | None = None
    ids: set[str] = set()
    for segment in segments:
        if (
            not segment.id
            or segment.id in ids
            or segment.start_ms < 0
            or segment.end_ms <= segment.start_ms
            or (previous_end is not None and segment.start_ms != previous_end)
        ):
            raise SourceMapError("source_timeline_invalid")
        ids.add(segment.id)
        previous_end = segment.end_ms


def build_candidate_segments(
    timeline: TextTimeline, pauses: Sequence[Pause]
) -> tuple[CandidateSegment, ...]:
    segments = tuple(timeline.source_segments)
    _validate_source_segments(segments)
    for pause in pauses:
        if (
            pause.start_ms < 0
            or pause.end_ms <= pause.start_ms
            or pause.duration_ms != pause.end_ms - pause.start_ms
        ):
            raise SourceMapError("pause_invalid")
    candidates: list[CandidateSegment] = []
    for segment in segments:
        pause_after = max(
            (
                pause.duration_ms
                for pause in pauses
                if pause.start_ms <= segment.end_ms <= pause.end_ms
            ),
            default=0,
        )
        candidates.append(
            CandidateSegment(
                id=segment.id,
                start_ms=segment.start_ms,
                end_ms=segment.end_ms,
                duration_ms=segment.end_ms - segment.start_ms,
                protected=segment.protected,
                text=segment.text,
                pause_after_ms=pause_after,
            )
        )
    return tuple(candidates)


def compile_keep_decisions(
    timeline: TextTimeline, requested_ids: Sequence[str]
) -> tuple[SourceSegment, ...]:
    segments = tuple(timeline.source_segments)
    _validate_source_segments(segments)
    if not requested_ids:
        raise SourceMapError("source_selection_empty")
    if len(set(requested_ids)) != len(requested_ids):
        raise SourceMapError("source_segment_duplicate")
    by_id = {segment.id: segment for segment in segments}
    try:
        selected = tuple(by_id[segment_id] for segment_id in requested_ids)
    except KeyError as exc:
        raise SourceMapError("source_segment_unknown") from exc
    starts = [segment.start_ms for segment in selected]
    if starts != sorted(starts):
        raise SourceMapError("source_order_invalid")
    protected_ids = {segment.id for segment in segments if segment.protected}
    if not protected_ids.issubset(requested_ids):
        raise SourceMapError("protected_segment_missing")

    output_cursor = 0
    mapped: list[SourceSegment] = []
    for segment in selected:
        duration_ms = segment.end_ms - segment.start_ms
        mapped.append(
            replace(
                segment,
                output_start_ms=output_cursor,
                output_end_ms=output_cursor + duration_ms,
            )
        )
        output_cursor += duration_ms
    return tuple(mapped)


def map_source_ms_to_output_ms(
    segments: Sequence[SourceSegment], source_ms: int
) -> int:
    if isinstance(source_ms, bool) or not isinstance(source_ms, int) or source_ms < 0:
        raise SourceMapError("source_position_invalid")
    for index, segment in enumerate(segments):
        output_start = segment.output_start_ms
        output_end = segment.output_end_ms
        if output_start is None or output_end is None:
            raise SourceMapError("source_map_uncompiled")
        is_last = index == len(segments) - 1
        if segment.start_ms <= source_ms < segment.end_ms or (
            is_last and source_ms == segment.end_ms
        ):
            mapped = output_start + (source_ms - segment.start_ms)
            return min(mapped, output_end)
    raise SourceMapError("source_position_cut")
