"""DashScope Fun-ASR adapter with a provider-neutral result boundary."""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from .base import ProviderError, ProviderResult, RetryableProviderError, UnknownSubmissionError


_BASE_URL = "https://dashscope.aliyuncs.com/api/v1"
_ASR_PATH = "/services/audio/asr/transcription"


class DashScopeClient:
    """Submit and retrieve Fun-ASR tasks without exposing provider responses."""

    def __init__(
        self,
        *,
        http_request: Callable[[str, str, dict[str, str], bytes | None, int], dict[str, Any]] | None = None,
        timeout_seconds: int = 30,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self._http_request = http_request or self._stdlib_request
        self._timeout_seconds = int(timeout_seconds)
        self._clock_ms = clock_ms or (lambda: round(time.monotonic() * 1000))

    def submit_asr(self, cos_url: str, reference: str) -> ProviderResult:
        if not isinstance(cos_url, str) or not cos_url.startswith(("http://", "https://")):
            raise ProviderError("dashscope_asr_url_invalid")
        if not isinstance(reference, str) or not reference.strip():
            raise ProviderError("dashscope_asr_reference_invalid")

        started_at = self._clock_ms()
        body = json.dumps(
            {
                "model": "fun-asr",
                "input": {"file_urls": [cos_url]},
                "parameters": {"channel_id": [0]},
            }, ensure_ascii=False,
        ).encode("utf-8")
        try:
            response = self._request(
                "POST",
                f"{self._base_url()}{_ASR_PATH}",
                self._authorization_headers({
                    "Content-Type": "application/json",
                    "X-DashScope-Async": "enable",
                }),
                body,
            )
        except (TimeoutError, socket.timeout, RetryableProviderError) as exc:
            raise UnknownSubmissionError("dashscope_asr_submission_unknown") from exc

        request_id, output = self._output(response)
        task_id = output.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise ProviderError("dashscope_asr_response_invalid")
        return ProviderResult(
            provider="dashscope",
            capability="asr",
            request_id=request_id,
            payload={
                "provider_task_id": task_id,
                "reference": reference,
                "status": self._status(output),
            },
            cost_units=0,
            elapsed_ms=max(0, self._clock_ms() - started_at),
        )

    def query_asr(self, provider_task_id: str) -> ProviderResult:
        if not isinstance(provider_task_id, str) or not provider_task_id:
            raise ProviderError("dashscope_asr_task_id_invalid")
        started_at = self._clock_ms()
        response = self._request(
            "GET",
            f"{self._base_url()}/tasks/{provider_task_id}",
            self._authorization_headers({}),
            None,
        )
        request_id, output = self._output(response)
        status = self._status(output)
        task_id = output.get("task_id")
        if task_id != provider_task_id:
            raise ProviderError("dashscope_asr_response_invalid")
        payload: dict[str, Any] = {"provider_task_id": task_id, "status": status}
        if status == "succeeded":
            transcript_url = self._transcript_url(output)
            transcript = self._request("GET", transcript_url, {}, None)
            payload.update(_normalize_transcript(transcript, provider_task_id))
        elif status in {"failed", "cancelled", "canceled", "unknown"}:
            raise ProviderError("dashscope_asr_provider_failed")
        elif status not in {"pending", "queued", "running", "processing"}:
            raise ProviderError("dashscope_asr_response_invalid")
        return ProviderResult(
            provider="dashscope",
            capability="asr",
            request_id=request_id,
            payload=payload,
            cost_units=0,
            elapsed_ms=max(0, self._clock_ms() - started_at),
        )

    def _base_url(self) -> str:
        return os.environ.get("DASHSCOPE_BASE_URL", _BASE_URL).rstrip("/")

    @staticmethod
    def _status(output: dict[str, Any]) -> str:
        status = output.get("task_status")
        if not isinstance(status, str) or not status:
            raise ProviderError("dashscope_asr_response_invalid")
        return status.lower()

    @staticmethod
    def _output(response: Any) -> tuple[str, dict[str, Any]]:
        if not isinstance(response, dict):
            raise ProviderError("dashscope_asr_response_invalid")
        request_id = response.get("request_id")
        output = response.get("output")
        if not isinstance(request_id, str) or not request_id or not isinstance(output, dict):
            raise ProviderError("dashscope_asr_response_invalid")
        return request_id, output

    @staticmethod
    def _transcript_url(output: dict[str, Any]) -> str:
        results = output.get("results")
        if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
            raise ProviderError("dashscope_asr_response_invalid")
        result = results[0]
        if str(result.get("subtask_status", "")).upper() != "SUCCEEDED":
            raise ProviderError("dashscope_asr_provider_failed")
        transcript_url = result.get("transcription_url")
        if not isinstance(transcript_url, str) or not transcript_url.startswith(("http://", "https://")):
            raise ProviderError("dashscope_asr_response_invalid")
        return transcript_url

    @staticmethod
    def _authorization_headers(headers: dict[str, str]) -> dict[str, str]:
        api_key = os.environ.get("DASHSCOPE_API_KEY", "")
        if not api_key:
            raise ProviderError("dashscope_not_configured")
        return {"Authorization": f"Bearer {api_key}", **headers}

    def _request(
        self, method: str, url: str, headers: dict[str, str], body: bytes | None
    ) -> dict[str, Any]:
        try:
            return self._http_request(method, url, headers, body, self._timeout_seconds)
        except (TimeoutError, socket.timeout):
            raise
        except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
            status = getattr(exc, "code", None)
            if status is None or status == 408 or status == 429 or int(status) >= 500:
                raise RetryableProviderError("dashscope_asr_unavailable") from exc
            raise ProviderError("dashscope_asr_request_rejected") from exc

    @staticmethod
    def _stdlib_request(
        method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: int
    ) -> dict[str, Any]:
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            parsed = json.loads(response.read().decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ProviderError("dashscope_asr_response_invalid")
        return parsed


def _normalize_transcript(transcript: Any, provider_task_id: str) -> dict[str, Any]:
    if not isinstance(transcript, dict):
        raise ProviderError("dashscope_asr_result_invalid")
    properties = transcript.get("properties")
    transcripts = transcript.get("transcripts")
    if not isinstance(properties, dict) or not isinstance(transcripts, list) or len(transcripts) != 1:
        raise ProviderError("dashscope_asr_result_invalid")
    try:
        duration_ms = _positive_int(properties["original_duration_in_milliseconds"])
        source = transcripts[0]
        sentences_data = source["sentences"]
        if not isinstance(source, dict) or not isinstance(sentences_data, list) or not sentences_data:
            raise ValueError
        sentences: list[dict[str, Any]] = []
        words: list[dict[str, Any]] = []
        previous_start = previous_end = -1
        for sentence in sentences_data:
            sentence_start = _nonnegative_int(sentence["begin_time"])
            sentence_end = _nonnegative_int(sentence["end_time"])
            sentence_text = sentence["text"]
            sentence_words = sentence["words"]
            if (
                not isinstance(sentence_text, str) or not sentence_text
                or sentence_end < sentence_start or sentence_start < previous_start
                or sentence_end < previous_end or not isinstance(sentence_words, list) or not sentence_words
            ):
                raise ValueError
            sentences.append({"text": sentence_text, "start_ms": sentence_start, "end_ms": sentence_end})
            for word in sentence_words:
                text = word["text"]
                start_ms = _nonnegative_int(word["begin_time"])
                end_ms = _nonnegative_int(word["end_time"])
                if (
                    not isinstance(text, str) or not text or end_ms < start_ms
                    or start_ms < previous_start or end_ms < previous_end
                ):
                    raise ValueError
                words.append({"text": text, "start_ms": start_ms, "end_ms": end_ms})
                previous_start, previous_end = start_ms, end_ms
    except (KeyError, TypeError, ValueError):
        raise ProviderError("dashscope_asr_result_invalid")
    if not words or words[-1]["end_ms"] > duration_ms:
        raise ProviderError("dashscope_asr_result_invalid")
    return {
        "language": "zh-CN",
        "duration_ms": duration_ms,
        "words": words,
        "sentences": sentences,
        "raw_text": "".join(sentence["text"] for sentence in sentences),
        "provider_task_id": provider_task_id,
    }


def _nonnegative_int(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError
    result = int(value)
    if result < 0:
        raise ValueError
    return result


def _positive_int(value: Any) -> int:
    result = _nonnegative_int(value)
    if result <= 0:
        raise ValueError
    return result
