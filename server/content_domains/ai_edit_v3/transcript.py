from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from typing import Sequence

from .providers.asr import AsrWord, NormalizedTranscript
from .source import PreparedSource


MIN_ALIGNMENT_COVERAGE = 0.85
_CHINESE_NUMERALS = frozenset("零〇一二两三四五六七八九十百千万亿兆点")
_CLAUSE_MARKERS = ("今天", "今日", "现在", "目前", "接下来", "同时", "但是", "所以")
_SENTENCE_END = frozenset("。！？!?")
_FACT_RE = re.compile(r"[0-9￥¥]|价格|元|折|品牌|产品|型号|不含|不是|不能")


class TranscriptError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Caption:
    id: str
    text: str
    start_ms: int
    end_ms: int


@dataclass(frozen=True)
class SourceSegment:
    id: str
    start_ms: int
    end_ms: int
    protected: bool
    text: str
    output_start_ms: int | None = None
    output_end_ms: int | None = None


@dataclass(frozen=True)
class AlignmentResult:
    words: tuple[AsrWord, ...]
    coverage: float
    monotonic: bool
    anchors: tuple[tuple[int, int, int, int], ...]


@dataclass(frozen=True)
class TextTimeline:
    duration_ms: int
    captions: tuple[Caption, ...]
    source_segments: tuple[SourceSegment, ...]
    authoritative_text_sha256: str | None
    alignment_coverage: float


@dataclass(frozen=True)
class _Token:
    text: str
    char_indexes: tuple[int, ...]
    start_ms: int | None = None
    end_ms: int | None = None

    @property
    def numeric(self) -> bool:
        return all(char.isdigit() or char in _CHINESE_NUMERALS for char in self.text)

    @property
    def normalized(self) -> str:
        return "<NUM>" if self.numeric else self.text.casefold()


def _ignored(char: str) -> bool:
    category = unicodedata.category(char)
    return char.isspace() or category.startswith("P") or category.startswith("Z")


def _without_punctuation(value: str) -> str:
    return "".join(char for char in value if not _ignored(char))


def normalize_external_punctuation(text: str) -> str:
    if not isinstance(text, str):
        raise TranscriptError("external_text_invalid")
    compact = _without_punctuation(text)
    if not compact:
        raise TranscriptError("external_text_invalid")
    for marker in _CLAUSE_MARKERS:
        position = compact.find(marker, 1)
        if position > 0:
            compact = compact[:position] + "，" + compact[position:]
            break
    return compact + "。"


def validate_punctuation_only(source: str, cleaned: str) -> None:
    if not isinstance(source, str) or not isinstance(cleaned, str):
        raise TranscriptError("external_text_changed")
    if _without_punctuation(source) != _without_punctuation(cleaned):
        raise TranscriptError("external_text_changed")


def _group_tokens(
    chars: Sequence[tuple[str, int, int | None, int | None]],
) -> list[_Token]:
    tokens: list[_Token] = []
    for char, index, start_ms, end_ms in chars:
        if _ignored(char):
            continue
        numeric = char.isdigit() or char in _CHINESE_NUMERALS
        if tokens and numeric and tokens[-1].numeric:
            previous = tokens[-1]
            tokens[-1] = _Token(
                previous.text + char,
                previous.char_indexes + (index,),
                previous.start_ms,
                end_ms,
            )
        else:
            tokens.append(_Token(char, (index,), start_ms, end_ms))
    return tokens


def _original_tokens(text: str) -> list[_Token]:
    return _group_tokens([(char, index, None, None) for index, char in enumerate(text)])


def _recognized_tokens(words: Sequence[AsrWord]) -> tuple[list[_Token], int]:
    chars: list[tuple[str, int, int, int]] = []
    previous_start = -1
    previous_end = -1
    char_index = 0
    for word in words:
        if (
            not isinstance(word.text, str)
            or not word.text
            or word.start_ms < 0
            or word.end_ms <= word.start_ms
            or word.start_ms < previous_start
            or word.end_ms < previous_end
        ):
            raise TranscriptError("alignment_timeline_invalid")
        width = word.end_ms - word.start_ms
        for offset, char in enumerate(word.text):
            start_ms = word.start_ms + round(width * offset / len(word.text))
            end_ms = word.start_ms + round(width * (offset + 1) / len(word.text))
            chars.append((char, char_index, start_ms, end_ms))
            char_index += 1
        previous_start = word.start_ms
        previous_end = word.end_ms
    return _group_tokens(chars), max((item[3] for item in chars), default=0)


def _align_tokens(
    original: Sequence[_Token], recognized: Sequence[_Token]
) -> list[tuple[int | None, int | None, bool]]:
    rows = len(original) + 1
    columns = len(recognized) + 1
    scores = [[0] * columns for _ in range(rows)]
    trace = [[""] * columns for _ in range(rows)]
    for row in range(1, rows):
        scores[row][0] = -2 * row
        trace[row][0] = "up"
    for column in range(1, columns):
        scores[0][column] = -2 * column
        trace[0][column] = "left"
    for row in range(1, rows):
        for column in range(1, columns):
            matched = original[row - 1].normalized == recognized[column - 1].normalized
            candidates = (
                (scores[row - 1][column - 1] + (3 if matched else -2), "diag"),
                (scores[row - 1][column] - 2, "up"),
                (scores[row][column - 1] - 2, "left"),
            )
            scores[row][column], trace[row][column] = max(
                candidates, key=lambda item: (item[0], item[1] == "diag")
            )
    pairs: list[tuple[int | None, int | None, bool]] = []
    row, column = len(original), len(recognized)
    while row or column:
        direction = trace[row][column]
        if direction == "diag":
            original_index, recognized_index = row - 1, column - 1
            pairs.append(
                (
                    original_index,
                    recognized_index,
                    original[original_index].normalized == recognized[recognized_index].normalized,
                )
            )
            row -= 1
            column -= 1
        elif direction == "up":
            pairs.append((row - 1, None, False))
            row -= 1
        else:
            pairs.append((None, column - 1, False))
            column -= 1
    pairs.reverse()
    return pairs


def _supported_pairs(
    pairs: Sequence[tuple[int | None, int | None, bool]],
    original: Sequence[_Token],
    recognized: Sequence[_Token],
) -> set[int]:
    supported = {index for index, pair in enumerate(pairs) if pair[2]}
    for index, (original_index, recognized_index, matched) in enumerate(pairs):
        if (
            matched
            or original_index is None
            or recognized_index is None
            or index == 0
            or index == len(pairs) - 1
        ):
            continue
        previous = pairs[index - 1]
        following = pairs[index + 1]
        if (
            previous[2]
            and following[2]
            and len(original[original_index].text) <= 2
            and len(recognized[recognized_index].text) <= 2
        ):
            supported.add(index)
    return supported


def _fill_centers(centers: list[float | None], duration_ms: int) -> list[float]:
    known = [index for index, center in enumerate(centers) if center is not None]
    if not known:
        raise TranscriptError("alignment_low_coverage")
    for index, center in enumerate(centers):
        if center is not None:
            continue
        previous = max((item for item in known if item < index), default=None)
        following = min((item for item in known if item > index), default=None)
        if previous is None:
            centers[index] = float(centers[following]) * (index + 1) / (following + 1)
        elif following is None:
            remaining = len(centers) - 1 - previous
            centers[index] = float(centers[previous]) + (
                max(duration_ms, int(centers[previous])) - float(centers[previous])
            ) * (index - previous) / max(1, remaining)
        else:
            centers[index] = float(centers[previous]) + (
                float(centers[following]) - float(centers[previous])
            ) * (index - previous) / (following - previous)
    result: list[float] = []
    for center in centers:
        result.append(max(result[-1] if result else 0.0, float(center)))
    return result


def align_authoritative_text(
    text: str, words: Sequence[AsrWord]
) -> AlignmentResult:
    if not isinstance(text, str) or not text or not words:
        raise TranscriptError("alignment_low_coverage")
    original = _original_tokens(text)
    recognized, duration_ms = _recognized_tokens(words)
    if not original or not recognized or duration_ms <= 0:
        raise TranscriptError("alignment_low_coverage")
    pairs = _align_tokens(original, recognized)
    supported = _supported_pairs(pairs, original, recognized)
    denominator = sum(len(token.text) for token in original)
    numerator = sum(
        len(original[pairs[index][0]].text)
        for index in supported
        if pairs[index][0] is not None
    )
    coverage = numerator / denominator if denominator else 0.0
    if coverage < MIN_ALIGNMENT_COVERAGE:
        raise TranscriptError("alignment_low_coverage")

    centers: list[float | None] = [None] * len(text)
    anchors: list[tuple[int, int, int, int]] = []
    for pair_index in sorted(supported):
        original_index, recognized_index, _ = pairs[pair_index]
        if original_index is None or recognized_index is None:
            continue
        source = original[original_index]
        timing = recognized[recognized_index]
        start_ms = int(timing.start_ms or 0)
        end_ms = int(timing.end_ms or start_ms)
        width = max(0, end_ms - start_ms)
        for offset, char_index in enumerate(source.char_indexes):
            centers[char_index] = start_ms + width * (offset + 0.5) / len(source.char_indexes)
        anchors.append((source.char_indexes[0], source.char_indexes[-1] + 1, start_ms, end_ms))

    filled = _fill_centers(centers, duration_ms)
    boundaries = [0]
    boundaries.extend(round((left + right) / 2) for left, right in zip(filled, filled[1:]))
    boundaries.append(max(duration_ms, boundaries[-1]))
    aligned = tuple(
        AsrWord(char, boundaries[index], boundaries[index + 1], None)
        for index, char in enumerate(text)
    )
    monotonic = all(
        left.start_ms <= left.end_ms <= right.end_ms
        for left, right in zip(aligned, aligned[1:])
    )
    if not monotonic:
        raise TranscriptError("alignment_timeline_invalid")
    return AlignmentResult(aligned, round(coverage, 4), True, tuple(anchors))


def _caption_groups(words: Sequence[AsrWord]) -> tuple[Caption, ...]:
    groups: list[list[AsrWord]] = []
    current: list[AsrWord] = []
    factual_count = 0
    for word in words:
        current.append(word)
        if not _ignored(word.text):
            factual_count += len(word.text)
        if any(char in _SENTENCE_END for char in word.text) or factual_count >= 24:
            groups.append(current)
            current = []
            factual_count = 0
    if current:
        groups.append(current)
    return tuple(
        Caption(
            id=f"caption_{index:03d}",
            text="".join(word.text for word in group),
            start_ms=group[0].start_ms,
            end_ms=group[-1].end_ms,
        )
        for index, group in enumerate(groups, 1)
    )


def _segments_from_captions(captions: Sequence[Caption]) -> tuple[SourceSegment, ...]:
    return tuple(
        SourceSegment(
            id=f"segment_{index:03d}",
            start_ms=caption.start_ms,
            end_ms=caption.end_ms,
            protected=bool(_FACT_RE.search(caption.text)),
            text=caption.text,
        )
        for index, caption in enumerate(captions, 1)
    )


def build_text_timeline(
    source: PreparedSource, asr: NormalizedTranscript
) -> TextTimeline:
    if not isinstance(asr, NormalizedTranscript) or not asr.words:
        raise TranscriptError("asr_result_invalid")
    authoritative = source.authoritative_text
    if source.input_type in {"platform_talking_head", "script_to_audio_video"}:
        if not isinstance(authoritative, str) or not authoritative:
            raise TranscriptError("authoritative_text_missing")
        text = authoritative
        authoritative_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    else:
        text = normalize_external_punctuation(asr.raw_text)
        validate_punctuation_only(asr.raw_text, text)
        authoritative_hash = None
    alignment = align_authoritative_text(text, asr.words)
    captions = _caption_groups(alignment.words)
    if not captions:
        raise TranscriptError("caption_timeline_empty")
    media_duration = getattr(source.media, "duration_ms", asr.duration_ms)
    duration_ms = int(media_duration)
    if captions[-1].end_ms > duration_ms:
        raise TranscriptError("caption_timeline_exceeds_media")
    return TextTimeline(
        duration_ms=duration_ms,
        captions=captions,
        source_segments=_segments_from_captions(captions),
        authoritative_text_sha256=authoritative_hash,
        alignment_coverage=alignment.coverage,
    )
