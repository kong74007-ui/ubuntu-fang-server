"""Lossless, display-width-aware caption segmentation."""

from __future__ import annotations

import json
from pathlib import Path


MAX_CAPTION_UNITS = 28
MAX_CAPTION_CUES = 100
_SENTENCE_BOUNDARIES = frozenset("。！？!?；;：:\n")
_CLAUSE_BOUNDARIES = frozenset("，,、 ")


def display_units(text: str) -> int:
    """Return a conservative single-line width for mixed CJK and ASCII text."""
    return sum(1 if ord(char) < 128 else 2 for char in text)


def validate_caption_cue_text(text: str) -> str:
    """Validate the authoritative one-line contract for caller-provided cues."""
    if not isinstance(text, str) or not text:
        raise ValueError("caption cue text must be a non-empty string")
    if display_units(text) > MAX_CAPTION_UNITS:
        raise ValueError("caption cue exceeds the single-line display width")
    return text


def _split_after_boundaries(text: str, boundaries: frozenset[str]) -> list[str]:
    parts: list[str] = []
    start = 0
    for index, char in enumerate(text):
        if char in boundaries:
            parts.append(text[start : index + 1])
            start = index + 1
    if start < len(text):
        parts.append(text[start:])
    return [part for part in parts if part]


def _hard_split(text: str, max_units: int) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    current_units = 0
    for char in text:
        char_units = display_units(char)
        if current and current_units + char_units > max_units:
            parts.append("".join(current))
            current = []
            current_units = 0
        if char_units > max_units:
            raise ValueError("max_units is too small for the input character width")
        current.append(char)
        current_units += char_units
    if current:
        parts.append("".join(current))
    return parts


def _pack_fragments(fragments: list[str], max_units: int) -> list[str]:
    """Greedily pack adjacent bounded fragments without changing their text."""
    packed: list[str] = []
    current = ""
    for fragment in fragments:
        if current and display_units(current + fragment) > max_units:
            packed.append(current)
            current = ""
        current += fragment
    if current:
        packed.append(current)
    return packed


def _split_clause(text: str, max_units: int) -> list[str]:
    if display_units(text) <= max_units:
        return [text]
    clauses = _split_after_boundaries(text, _CLAUSE_BOUNDARIES)
    if len(clauses) == 1:
        return _hard_split(text, max_units)
    bounded: list[str] = []
    for clause in clauses:
        if display_units(clause) <= max_units:
            bounded.append(clause)
        else:
            bounded.extend(_hard_split(clause, max_units))

    return _pack_fragments(bounded, max_units)


def split_caption_text(text: str, max_units: int = MAX_CAPTION_UNITS) -> list[str]:
    """Split text at semantic boundaries without dropping or reordering characters."""
    if not isinstance(text, str) or not text:
        raise ValueError("caption text must be a non-empty string")
    if not isinstance(max_units, int) or max_units < 2:
        raise ValueError("max_units must be an integer greater than or equal to 2")
    if display_units(text) <= max_units:
        return [text]

    sentences = _split_after_boundaries(text, _SENTENCE_BOUNDARIES)
    result: list[str] = []
    for sentence in sentences:
        result.extend(_split_clause(sentence, max_units))
    result = _pack_fragments(result, max_units)

    if "".join(result) != text:
        raise ValueError("caption segmentation changed the narration text")
    if any(display_units(part) > max_units for part in result):
        raise ValueError("caption segmentation exceeded the display width")
    if len(result) > MAX_CAPTION_CUES:
        raise ValueError("caption segmentation exceeded the cue limit")
    return result


def build_caption_timeline(cues: list[dict], durations: list[float]) -> list[dict]:
    """Attach cumulative timing from probed audio durations to ordered cues."""
    if not cues or len(cues) != len(durations):
        raise ValueError("caption cues and durations must be non-empty and have equal length")
    timed: list[dict] = []
    cursor = 0.0
    for cue, raw_duration in zip(cues, durations):
        if not isinstance(raw_duration, (int, float)) or raw_duration <= 0:
            raise ValueError("caption cue duration must be positive")
        duration = float(raw_duration)
        item = dict(cue)
        item["duration"] = duration
        item["start_time"] = cursor
        cursor += duration
        item["end_time"] = cursor
        timed.append(item)
    return timed


def build_proportional_caption_timeline(
    cues: list[dict], total_duration: float
) -> list[dict]:
    """Distribute one continuous narration track across display-only cues."""
    if not cues:
        raise ValueError("caption cues must not be empty")
    if not isinstance(total_duration, (int, float)) or total_duration <= 0:
        raise ValueError("continuous narration duration must be positive")
    weights = []
    for cue in cues:
        text = cue.get("text") if isinstance(cue, dict) else None
        validate_caption_cue_text(text)
        weights.append(max(1, display_units(text.strip())))
    total_weight = sum(weights)
    cursor = 0.0
    timed: list[dict] = []
    for index, (cue, weight) in enumerate(zip(cues, weights)):
        end_time = (
            float(total_duration)
            if index == len(cues) - 1
            else float(total_duration) * sum(weights[: index + 1]) / total_weight
        )
        item = dict(cue)
        item["start_time"] = cursor
        item["end_time"] = end_time
        item["duration"] = end_time - cursor
        timed.append(item)
        cursor = end_time
    return timed


def build_explicit_caption_timeline(
    cues: list[dict], total_duration: float, tolerance: float = 0.08
) -> list[dict]:
    """Validate caller-supplied speech timing for one continuous narration track."""
    if not cues:
        raise ValueError("caption cues must not be empty")
    if not isinstance(total_duration, (int, float)) or total_duration <= 0:
        raise ValueError("continuous narration duration must be positive")
    if not isinstance(tolerance, (int, float)) or tolerance < 0:
        raise ValueError("caption timing tolerance must not be negative")

    duration = float(total_duration)
    allowed_error = float(tolerance)
    timed: list[dict] = []
    cursor = 0.0
    for index, cue in enumerate(cues):
        text = cue.get("text") if isinstance(cue, dict) else None
        validate_caption_cue_text(text)
        raw_start = cue.get("start_time")
        raw_end = cue.get("end_time")
        if (
            not isinstance(raw_start, (int, float))
            or isinstance(raw_start, bool)
            or not isinstance(raw_end, (int, float))
            or isinstance(raw_end, bool)
        ):
            raise ValueError("caption cue timing must be numeric")
        start = float(raw_start)
        end = float(raw_end)
        if start < 0 or end <= start:
            raise ValueError("caption cue timing must be positive")
        if abs(start - cursor) > allowed_error:
            raise ValueError("caption cue timing must be continuous")
        if index < len(cues) - 1 and end > duration + allowed_error:
            raise ValueError("caption cue timing exceeds narration duration")
        if index == len(cues) - 1 and abs(end - duration) > allowed_error:
            raise ValueError("caption cue timing must cover narration duration")

        normalized_start = cursor
        normalized_end = duration if index == len(cues) - 1 else end
        if normalized_end <= normalized_start:
            raise ValueError("caption cue timing must advance")
        item = dict(cue)
        item["start_time"] = normalized_start
        item["end_time"] = normalized_end
        item["duration"] = normalized_end - normalized_start
        timed.append(item)
        cursor = normalized_end

    return timed


def _spoken_units(text: object) -> list[str]:
    return [char for char in str(text or "") if char.isalnum()]


def load_tts_word_timing(audio_path: str) -> list[dict]:
    """Consume Edge TTS word boundaries stored beside generated audio."""
    timing_path = Path(f"{audio_path}.timing.json")
    try:
        payload = json.loads(timing_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    finally:
        timing_path.unlink(missing_ok=True)
    words = payload.get("words") if isinstance(payload, dict) else None
    return list(words) if isinstance(words, list) else []


def build_word_aligned_caption_timeline(
    cues: list[dict], total_duration: float, words: list[dict]
) -> list[dict]:
    """Map display cue boundaries onto Edge TTS word-boundary timestamps."""
    if not cues or not isinstance(total_duration, (int, float)) or total_duration <= 0:
        raise ValueError("word-aligned caption timing requires cues and duration")

    source_units: list[str] = []
    cue_unit_counts: list[int] = []
    for cue in cues:
        text = cue.get("text") if isinstance(cue, dict) else None
        validate_caption_cue_text(text)
        units = _spoken_units(text)
        if not units:
            raise ValueError("caption cue has no spoken units")
        source_units.extend(units)
        cue_unit_counts.append(len(units))

    recognized_units: list[str] = []
    timed_units: list[dict] = []
    for word in words or []:
        text = word.get("text") if isinstance(word, dict) else None
        start = word.get("start_time") if isinstance(word, dict) else None
        end = word.get("end_time") if isinstance(word, dict) else None
        if (
            not isinstance(start, (int, float))
            or isinstance(start, bool)
            or not isinstance(end, (int, float))
            or isinstance(end, bool)
            or end <= start
        ):
            continue
        units = _spoken_units(text)
        if not units:
            continue
        span = float(end) - float(start)
        for index, unit in enumerate(units):
            recognized_units.append(unit)
            timed_units.append(
                {
                    "start_time": float(start) + span * index / len(units),
                    "end_time": float(start) + span * (index + 1) / len(units),
                }
            )

    if recognized_units != source_units or len(timed_units) != len(source_units):
        raise ValueError("TTS word timing does not match caption text")

    boundaries = [0.0]
    consumed = 0
    for count in cue_unit_counts[:-1]:
        consumed += count
        boundary = max(
            boundaries[-1],
            min(float(total_duration), timed_units[consumed]["start_time"]),
        )
        if boundary <= boundaries[-1]:
            raise ValueError("TTS word timing does not advance")
        boundaries.append(boundary)
    boundaries.append(float(total_duration))

    timed: list[dict] = []
    for index, cue in enumerate(cues):
        start = boundaries[index]
        end = boundaries[index + 1]
        if end <= start:
            raise ValueError("TTS word timing produced an empty caption cue")
        item = dict(cue)
        item["start_time"] = start
        item["end_time"] = end
        item["duration"] = end - start
        timed.append(item)
    return timed


def caption_timeline_duration(cues: list[dict]) -> float:
    if not cues:
        raise ValueError("caption timeline must not be empty")
    duration = cues[-1].get("end_time")
    if not isinstance(duration, (int, float)) or duration <= 0:
        raise ValueError("caption timeline is not timed")
    return float(duration)


def caption_video_slices(cues: list[dict]) -> list[tuple[float, float]]:
    """Return consecutive source-video offsets and durations for cue clips."""
    return [
        (float(cue["start_time"]), float(cue["duration"]))
        for cue in cues
    ]


def required_video_padding(source_duration: float, target_duration: float) -> float:
    """Return freeze-frame padding needed before cumulative cue slicing."""
    if not isinstance(source_duration, (int, float)) or source_duration <= 0:
        raise ValueError("source video duration must be positive")
    if not isinstance(target_duration, (int, float)) or target_duration <= 0:
        raise ValueError("target video duration must be positive")
    return max(0.0, float(target_duration) - float(source_duration))
