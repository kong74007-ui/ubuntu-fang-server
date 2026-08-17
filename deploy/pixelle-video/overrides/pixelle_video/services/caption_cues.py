"""Lossless, display-width-aware caption segmentation."""

from __future__ import annotations


MAX_CAPTION_UNITS = 28
MAX_CAPTION_CUES = 20
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
