# -*- coding: utf-8 -*-
"""阿里百炼 Fun-ASR 非实时音视频转写。"""
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request


POST_URL = "https://dashscope.aliyuncs.com/api/v1/services/audio/asr/transcription"
TASK_URL = "https://dashscope.aliyuncs.com/api/v1/tasks/{}"
API_KEY = os.environ.get("DASHSCOPE_API_KEY", "").strip()
MODEL = os.environ.get("DASHSCOPE_ASR_MODEL", "fun-asr").strip() or "fun-asr"
MAX_RESPONSE_BYTES = 32 * 1024 * 1024


def _json_request(method, url, payload=None, headers=None, timeout=60):
    body = None
    request_headers = {"Accept": "application/json"}
    request_headers.update(headers or {})
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(
        str(url), data=body, headers=request_headers, method=str(method).upper()
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read(4096).decode("utf-8", errors="replace")
            message = (json.loads(detail).get("message") or "")[:240]
        except Exception:
            message = ""
        raise RuntimeError("阿里语音识别请求失败%s" % (("：" + message) if message else ""))
    except OSError as exc:
        raise RuntimeError("阿里语音识别网络异常：%s" % str(exc)[:160])
    if len(raw) > MAX_RESPONSE_BYTES:
        raise RuntimeError("阿里语音识别响应过大")
    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("阿里语音识别返回格式错误")
    if not isinstance(result, dict):
        raise RuntimeError("阿里语音识别返回格式错误")
    return result


def _https_url(value, label):
    value = str(value or "").strip()
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ValueError("%s必须使用HTTPS地址" % label)
    return value


def _timestamp(value):
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _normalize_result(raw, task_id):
    transcripts = raw.get("transcripts") if isinstance(raw, dict) else None
    if not isinstance(transcripts, list):
        transcripts = []
    texts = []
    sentences = []
    words = []
    duration_ms = 0
    for transcript in transcripts:
        if not isinstance(transcript, dict):
            continue
        text = str(transcript.get("text") or "").strip()
        if text:
            texts.append(text)
        transcript_sentences = transcript.get("sentences") or []
        if not isinstance(transcript_sentences, list):
            continue
        for item in transcript_sentences:
            if not isinstance(item, dict):
                continue
            sentence = {
                "begin_time": _timestamp(item.get("begin_time")),
                "end_time": _timestamp(item.get("end_time")),
                "text": str(item.get("text") or ""),
            }
            duration_ms = max(duration_ms, sentence["end_time"])
            sentences.append(sentence)
            item_words = item.get("words") or []
            if not isinstance(item_words, list):
                continue
            for word in item_words:
                if not isinstance(word, dict):
                    continue
                normalized = {
                    "begin_time": _timestamp(word.get("begin_time")),
                    "end_time": _timestamp(word.get("end_time")),
                    "text": str(word.get("text") or ""),
                }
                duration_ms = max(duration_ms, normalized["end_time"])
                words.append(normalized)
    return {
        "text": "\n".join(texts).strip(),
        "sentences": sentences,
        "words": words,
        "duration_ms": duration_ms,
        "provider_task_id": str(task_id),
    }


def transcribe(file_url, heartbeat=None, timeout=900):
    file_url = _https_url(file_url, "待识别文件")
    if not API_KEY:
        raise RuntimeError("DASHSCOPE_API_KEY 未配置")
    try:
        timeout = max(1, int(timeout))
    except (TypeError, ValueError):
        raise ValueError("语音识别超时时间无效")
    headers = {
        "Authorization": "Bearer " + API_KEY,
        "Content-Type": "application/json",
        "X-DashScope-Async": "enable",
    }
    submitted = _json_request(
        "POST",
        POST_URL,
        {
            "model": MODEL,
            "input": {"file_urls": [file_url]},
            "parameters": {"channel_id": [0]},
        },
        headers,
    )
    output = submitted.get("output") or {}
    task_id = str(output.get("task_id") or "").strip()
    if not task_id:
        raise RuntimeError("阿里语音识别未返回任务ID")

    deadline = time.monotonic() + timeout
    delays = (2, 3, 5, 8)
    attempt = 0
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("阿里语音识别等待超时")
        delay = delays[attempt] if attempt < len(delays) else 10
        if delay >= remaining:
            raise TimeoutError("阿里语音识别等待超时")
        time.sleep(delay)
        attempt += 1
        current = _json_request(
            "GET", TASK_URL.format(urllib.parse.quote(task_id, safe="")), None, headers
        )
        if heartbeat:
            heartbeat("transcribing")
        output = current.get("output") or {}
        status = str(output.get("task_status") or "").upper()
        if status in {"PENDING", "RUNNING", "QUEUED"}:
            continue
        if status != "SUCCEEDED":
            message = str(output.get("message") or current.get("message") or status)
            raise RuntimeError("阿里语音识别失败：%s" % message[:240])
        results = output.get("results") or []
        result = next(
            (
                item
                for item in results
                if isinstance(item, dict)
                and str(item.get("subtask_status") or "SUCCEEDED").upper()
                == "SUCCEEDED"
                and item.get("transcription_url")
            ),
            None,
        )
        if result is None:
            raise RuntimeError("阿里语音识别未返回转写结果")
        result_url = _https_url(result["transcription_url"], "转写结果")
        raw_result = _json_request("GET", result_url)
        return _normalize_result(raw_result, task_id)
