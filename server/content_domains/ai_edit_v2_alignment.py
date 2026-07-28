"""Deterministic alignment that keeps platform source text authoritative."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Any


MIN_ALIGNMENT_COVERAGE = 0.85
_CHINESE_NUMERALS = frozenset("零〇一二两三四五六七八九十百千万亿兆点")


class AlignmentError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


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


def _group_tokens(
    chars: list[tuple[str, int, int | None, int | None]],
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


def _original_tokens(original: str) -> list[_Token]:
    return _group_tokens([(char, index, None, None) for index, char in enumerate(original)])


def _asr_chars(words: list[dict[str, Any]]) -> tuple[list[tuple[str, int, int, int]], int]:
    chars: list[tuple[str, int, int, int]] = []
    previous_start = -1
    previous_end = -1
    char_index = 0
    for word in words:
        try:
            text = str(word["text"])
            start_ms = int(word["start_ms"])
            end_ms = int(word["end_ms"])
        except (KeyError, TypeError, ValueError):
            raise AlignmentError("alignment_low_coverage")
        if (
            not text
            or start_ms < 0
            or end_ms < start_ms
            or start_ms < previous_start
            or end_ms < previous_end
        ):
            raise AlignmentError("alignment_low_coverage")
        width = max(1, end_ms - start_ms)
        for offset, char in enumerate(text):
            char_start = start_ms + round(width * offset / len(text))
            char_end = start_ms + round(width * (offset + 1) / len(text))
            chars.append((char, char_index, char_start, char_end))
            char_index += 1
        previous_start = start_ms
        previous_end = end_ms
    return chars, max((item[3] for item in chars), default=0)


def _align_tokens(original: list[_Token], recognized: list[_Token]) -> list[tuple[int | None, int | None, bool]]:
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
            oi, ai = row - 1, column - 1
            pairs.append((oi, ai, original[oi].normalized == recognized[ai].normalized))
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
    pairs: list[tuple[int | None, int | None, bool]],
    original: list[_Token],
    recognized: list[_Token],
) -> set[int]:
    supported = {index for index, pair in enumerate(pairs) if pair[2]}
    for index, (oi, ai, matched) in enumerate(pairs):
        if matched or oi is None or ai is None or index == 0 or index == len(pairs) - 1:
            continue
        previous = pairs[index - 1]
        following = pairs[index + 1]
        if (
            previous[2]
            and following[2]
            and len(original[oi].text) <= 2
            and len(recognized[ai].text) <= 2
        ):
            supported.add(index)
    return supported


def _fill_centers(centers: list[float | None], duration_ms: int) -> list[float]:
    known = [index for index, center in enumerate(centers) if center is not None]
    if not known:
        raise AlignmentError("alignment_low_coverage")
    for index, center in enumerate(centers):
        if center is not None:
            continue
        previous = max((known_index for known_index in known if known_index < index), default=None)
        following = min((known_index for known_index in known if known_index > index), default=None)
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


def align_platform_text(original: str, asr_words: list[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(original, str) or not original or not isinstance(asr_words, list):
        raise AlignmentError("alignment_low_coverage")
    original_tokens = _original_tokens(original)
    asr_char_data, duration_ms = _asr_chars(asr_words)
    recognized_tokens = _group_tokens(asr_char_data)
    if not original_tokens or not recognized_tokens or duration_ms <= 0:
        raise AlignmentError("alignment_low_coverage")

    pairs = _align_tokens(original_tokens, recognized_tokens)
    supported = _supported_pairs(pairs, original_tokens, recognized_tokens)
    denominator = sum(len(token.text) for token in original_tokens)
    numerator = sum(
        len(original_tokens[pairs[index][0]].text)
        for index in supported
        if pairs[index][0] is not None
    )
    coverage = numerator / denominator if denominator else 0.0
    if coverage < MIN_ALIGNMENT_COVERAGE:
        raise AlignmentError("alignment_low_coverage")

    centers: list[float | None] = [None] * len(original)
    anchors = []
    for pair_index in sorted(supported):
        original_index, recognized_index, _ = pairs[pair_index]
        if original_index is None or recognized_index is None:
            continue
        source = original_tokens[original_index]
        timing = recognized_tokens[recognized_index]
        start_ms = int(timing.start_ms or 0)
        end_ms = int(timing.end_ms or start_ms)
        width = max(0, end_ms - start_ms)
        for offset, char_index in enumerate(source.char_indexes):
            centers[char_index] = start_ms + width * (offset + 0.5) / len(source.char_indexes)
        anchors.append(
            {
                "original_start": source.char_indexes[0],
                "original_end": source.char_indexes[-1] + 1,
                "start_ms": start_ms,
                "end_ms": end_ms,
            }
        )

    filled = _fill_centers(centers, duration_ms)
    boundaries = [0]
    boundaries.extend(round((left + right) / 2) for left, right in zip(filled, filled[1:]))
    boundaries.append(max(duration_ms, boundaries[-1]))
    aligned_words = [
        {
            "start_ms": boundaries[index],
            "end_ms": boundaries[index + 1],
            "text": char,
        }
        for index, char in enumerate(original)
    ]
    monotonic = all(
        left["start_ms"] <= left["end_ms"] <= right["end_ms"]
        for left, right in zip(aligned_words, aligned_words[1:])
    )
    if not monotonic:
        raise AlignmentError("alignment_low_coverage")
    return {
        "aligned_words": aligned_words,
        "coverage": round(coverage, 4),
        "monotonic": True,
        "anchors": anchors,
    }


def validate_punctuation_only(original: str, candidate: str) -> str:
    def factual_characters(value: str) -> str:
        return "".join(char for char in value if not _ignored(char))

    if factual_characters(original) != factual_characters(candidate):
        raise ValueError("外部转录修复不得改变正文")
    return candidate


def _validate_external_timeline(words: list[Any], sentences: list[Any]) -> None:
    for records in (words, sentences):
        previous_start = previous_end = -1
        for item in records:
            if not isinstance(item, dict):
                raise AlignmentError("asr_timeline_invalid")
            text = item.get("text")
            start_ms = item.get("start_ms")
            end_ms = item.get("end_ms")
            if (
                not isinstance(text, str) or not text
                or not isinstance(start_ms, int) or isinstance(start_ms, bool)
                or not isinstance(end_ms, int) or isinstance(end_ms, bool)
                or start_ms < 0 or end_ms < start_ms
                or start_ms < previous_start or end_ms < previous_end
            ):
                raise AlignmentError("asr_timeline_invalid")
            previous_start, previous_end = start_ms, end_ms


def build_text_timeline(source_type: str, original_text: str | None, asr_result: Any) -> dict[str, Any]:
    """Select the sole allowed text source while retaining ASR timing."""

    if not isinstance(asr_result, dict):
        raise AlignmentError("asr_result_invalid")
    words = asr_result.get("words")
    sentences = asr_result.get("sentences")
    if not isinstance(words, list) or not isinstance(sentences, list) or not words or not sentences:
        raise AlignmentError("asr_result_invalid")

    if source_type == "platform_video":
        if not isinstance(original_text, str) or not original_text:
            raise AlignmentError("platform_text_missing")
        aligned = align_platform_text(original_text, words)
        return {
            "source_type": source_type,
            "text": original_text,
            "words": aligned["aligned_words"],
            "sentences": sentences,
            "coverage": aligned["coverage"],
        }

    if source_type in {"external_video", "external_audio"}:
        _validate_external_timeline(words, sentences)
        raw_text = asr_result.get("raw_text")
        if not isinstance(raw_text, str) or not raw_text:
            raw_text = "".join(
                item.get("text", "") for item in sentences if isinstance(item, dict)
            )
        cleaned_text = asr_result.get("cleaned_text", raw_text)
        if not isinstance(raw_text, str) or not raw_text or not isinstance(cleaned_text, str) or not cleaned_text:
            raise AlignmentError("external_text_invalid")
        try:
            text = validate_punctuation_only(raw_text, cleaned_text)
        except ValueError as exc:
            raise AlignmentError("external_text_changed") from exc
        return {
            "source_type": source_type,
            "text": text,
            "words": words,
            "sentences": sentences,
        }
    raise AlignmentError("source_type_invalid")
