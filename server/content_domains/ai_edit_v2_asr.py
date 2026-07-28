"""Provider-neutral fun-asr submission, polling, and result normalization."""

from __future__ import annotations

import time
from typing import Any, Callable

from .ai_edit_v2_providers.base import ProviderError, ProviderResult, UnknownSubmissionError


class AsrError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _integer(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError
    return int(value)


def _normalize_result(result: Any, provider_task_id: str) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise AsrError("asr_result_invalid")
    try:
        language = str(result.get("language") or "zh-CN")
        duration_ms = _integer(result["duration_ms"])
        sentences = result["sentences"]
        words = result["words"]
        if language != "zh-CN" or duration_ms <= 0 or not isinstance(sentences, list) or not isinstance(words, list):
            raise ValueError
        normalized_sentences = [
            {
                "start_ms": _integer(item["start_ms"]),
                "end_ms": _integer(item["end_ms"]),
                "text": str(item["text"]),
            }
            for item in sentences
        ]
        normalized_words = [
            {
                "start_ms": _integer(item["start_ms"]),
                "end_ms": _integer(item["end_ms"]),
                "text": str(item["text"]),
                "confidence": float(item.get("confidence", 0)),
            }
            for item in words
        ]
    except (KeyError, TypeError, ValueError):
        raise AsrError("asr_result_invalid")
    previous_start = -1
    previous_end = -1
    for item in normalized_words:
        if (
            not item["text"]
            or item["start_ms"] < 0
            or item["end_ms"] < item["start_ms"]
            or item["start_ms"] < previous_start
            or item["end_ms"] < previous_end
        ):
            raise AsrError("asr_result_invalid")
        previous_start, previous_end = item["start_ms"], item["end_ms"]
    return {
        "language": language,
        "duration_ms": duration_ms,
        "sentences": normalized_sentences,
        "words": normalized_words,
        "provider_task_id": provider_task_id,
    }


def transcribe(
    cos_key: str,
    client: Any,
    deadline_at: int,
    *,
    provider_task_id: str | None = None,
    reference: str | None = None,
    save_provider_task_id: Callable[[str], None] | None = None,
    now_fn: Callable[[], float] = time.time,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    task_id = provider_task_id
    if not task_id:
        try:
            if hasattr(client, "submit_asr"):
                submitted = client.submit_asr(cos_key, reference or cos_key)
                if not isinstance(submitted, ProviderResult):
                    raise AsrError("asr_submit_failed")
                task_id = submitted.payload.get("provider_task_id")
            else:
                submitted = client.submit(cos_key)
                task_id = submitted.get("task_id") if isinstance(submitted, dict) else submitted
        except UnknownSubmissionError:
            raise
        except ProviderError as exc:
            raise AsrError("asr_submit_failed") from exc
        except Exception as exc:
            raise AsrError("asr_submit_failed") from exc
        if not isinstance(task_id, str) or not task_id:
            raise AsrError("asr_submit_failed")
        if save_provider_task_id is not None:
            save_provider_task_id(task_id)

    while True:
        if now_fn() >= deadline_at:
            raise AsrError("asr_timeout")
        try:
            if hasattr(client, "query_asr"):
                queried = client.query_asr(task_id)
                if not isinstance(queried, ProviderResult):
                    raise AsrError("asr_result_invalid")
                response = queried.payload
            else:
                response = client.get(task_id)
        except ProviderError as exc:
            raise AsrError("asr_poll_failed") from exc
        except Exception as exc:
            raise AsrError("asr_poll_failed") from exc
        if not isinstance(response, dict):
            raise AsrError("asr_result_invalid")
        status = str(response.get("status") or "").lower()
        if status in {"succeeded", "completed", "success"}:
            return _normalize_result(response.get("result", response), task_id)
        if status in {"failed", "error", "cancelled", "canceled"}:
            raise AsrError("asr_provider_failed")
        if status not in {"queued", "pending", "running", "processing"}:
            raise AsrError("asr_result_invalid")
        sleep_fn(1.0)
