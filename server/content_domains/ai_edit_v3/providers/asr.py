from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from .base import ProviderResult


class AsrResultError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class AsrWord:
    text: str
    start_ms: int
    end_ms: int
    confidence: float | None


@dataclass(frozen=True)
class AsrSentence:
    text: str
    start_ms: int
    end_ms: int


@dataclass(frozen=True)
class NormalizedTranscript:
    language: str
    duration_ms: int
    words: tuple[AsrWord, ...]
    sentences: tuple[AsrSentence, ...]
    provider_task_id: str | None
    raw_text: str


class AsrProvider(Protocol):
    def submit(
        self,
        source: Mapping[str, Any],
        *,
        idempotency_key: str,
        deadline_at: float,
    ) -> ProviderResult: ...

    def query(self, request_id: str, *, deadline_at: float) -> ProviderResult: ...


def _integer(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AsrResultError("asr_timeline_invalid")
    return value


def _text(value: Any) -> str:
    if not isinstance(value, str) or not value or any(ord(char) < 0x20 for char in value):
        raise AsrResultError("asr_timeline_invalid")
    return value


def _confidence(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AsrResultError("asr_confidence_invalid")
    result = float(value)
    if not math.isfinite(result) or not 0 <= result <= 1:
        raise AsrResultError("asr_confidence_invalid")
    return result


def _normalize_words(raw_words: Any) -> tuple[AsrWord, ...]:
    if not isinstance(raw_words, list) or not raw_words:
        raise AsrResultError("asr_timeline_invalid")
    words: list[AsrWord] = []
    previous_end = 0
    for raw in raw_words:
        if not isinstance(raw, Mapping):
            raise AsrResultError("asr_timeline_invalid")
        start_ms = _integer(raw.get("start_ms"))
        end_ms = _integer(raw.get("end_ms"))
        if start_ms < 0 or end_ms <= start_ms or (words and start_ms < previous_end):
            raise AsrResultError("asr_timeline_invalid")
        words.append(
            AsrWord(
                _text(raw.get("text")),
                start_ms,
                end_ms,
                _confidence(raw.get("confidence")),
            )
        )
        previous_end = end_ms
    return tuple(words)


def _normalize_sentences(raw_sentences: Any, words: tuple[AsrWord, ...]) -> tuple[AsrSentence, ...]:
    if raw_sentences is None:
        return (AsrSentence("".join(word.text for word in words), words[0].start_ms, words[-1].end_ms),)
    if not isinstance(raw_sentences, list) or not raw_sentences:
        raise AsrResultError("asr_timeline_invalid")
    sentences: list[AsrSentence] = []
    previous_end = 0
    for raw in raw_sentences:
        if not isinstance(raw, Mapping):
            raise AsrResultError("asr_timeline_invalid")
        start_ms = _integer(raw.get("start_ms"))
        end_ms = _integer(raw.get("end_ms"))
        if start_ms < 0 or end_ms <= start_ms or (sentences and start_ms < previous_end):
            raise AsrResultError("asr_timeline_invalid")
        sentences.append(AsrSentence(_text(raw.get("text")), start_ms, end_ms))
        previous_end = end_ms
    return tuple(sentences)


def normalize_asr_result(payload: Mapping[str, Any]) -> NormalizedTranscript:
    if not isinstance(payload, Mapping):
        raise AsrResultError("asr_result_invalid")
    words = _normalize_words(payload.get("words"))
    sentences = _normalize_sentences(payload.get("sentences"), words)
    raw_duration = payload.get("duration_ms", max(words[-1].end_ms, sentences[-1].end_ms))
    duration_ms = _integer(raw_duration)
    if duration_ms < max(words[-1].end_ms, sentences[-1].end_ms):
        raise AsrResultError("asr_duration_invalid")
    language = payload.get("language", "zh-CN")
    if not isinstance(language, str) or not language.startswith("zh"):
        raise AsrResultError("asr_language_invalid")
    provider_task_id = payload.get("provider_task_id")
    if provider_task_id is not None and (
        not isinstance(provider_task_id, str) or not provider_task_id.strip()
    ):
        raise AsrResultError("asr_provider_task_invalid")
    return NormalizedTranscript(
        language=language,
        duration_ms=duration_ms,
        words=words,
        sentences=sentences,
        provider_task_id=provider_task_id,
        raw_text="".join(sentence.text for sentence in sentences),
    )
