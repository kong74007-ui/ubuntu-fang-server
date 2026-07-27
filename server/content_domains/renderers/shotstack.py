# -*- coding: utf-8 -*-
"""edit-plan v1 到 Shotstack Edit API 的隔离适配器。"""
import html
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from .base import RenderStatus


DEFAULT_BASE = "https://api.shotstack.io/edit/stage"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


def _request_json(method, url, payload, api_key):
    if not api_key:
        raise RuntimeError("SHOTSTACK_API_KEY 未配置")
    body = None
    headers = {"Accept": "application/json", "x-api-key": api_key}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        str(url), data=body, headers=headers, method=str(method).upper()
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        try:
            error = json.loads(exc.read(4096).decode("utf-8", errors="replace"))
            detail = str(error.get("message") or error.get("error") or "")[:240]
        except Exception:
            detail = ""
        raise RuntimeError("Shotstack请求失败%s" % (("：" + detail) if detail else ""))
    except OSError as exc:
        raise RuntimeError("Shotstack网络异常：%s" % str(exc)[:160])
    if len(raw) > MAX_RESPONSE_BYTES:
        raise RuntimeError("Shotstack响应过大")
    try:
        response = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("Shotstack返回格式错误")
    if not isinstance(response, dict):
        raise RuntimeError("Shotstack返回格式错误")
    return response


def normalize_status(value):
    status = str(value or "").strip().lower()
    if status == "queued":
        return RenderStatus.QUEUED
    if status in {"fetching", "rendering", "saving"}:
        return RenderStatus.RENDERING
    if status == "done":
        return RenderStatus.SUCCEEDED
    return RenderStatus.FAILED


def _https(value, label):
    value = str(value or "").strip()
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ValueError("%s必须使用HTTPS地址" % label)
    return value


def _seconds(milliseconds):
    return round(int(milliseconds) / 1000.0, 3)


def _card_html(text, card_type):
    labels = {
        "claim_card": "核心观点",
        "title_card": "本段重点",
        "evidence_card": "事实依据",
    }
    label = labels.get(str(card_type), "重点信息")
    return (
        '<div style="box-sizing:border-box;width:100%%;height:100%%;display:flex;'
        'align-items:center;justify-content:center;padding:42px;background:rgba(8,16,31,.82);'
        'border:2px solid #e9b949;border-radius:28px;color:#fff;font-family:Arial,sans-serif;">'
        '<div><div style="font-size:28px;color:#f5c451;margin-bottom:18px;">%s</div>'
        '<div style="font-size:52px;font-weight:700;line-height:1.25;">%s</div></div></div>'
    ) % (html.escape(label), html.escape(str(text or "")[:300]))


class ShotstackRenderer:
    def __init__(self, api_key=None, base=None):
        self.api_key = (
            api_key
            if api_key is not None
            else os.environ.get("SHOTSTACK_API_KEY", "").strip()
        )
        self.base = str(
            base or os.environ.get("SHOTSTACK_API_BASE", DEFAULT_BASE) or DEFAULT_BASE
        ).rstrip("/")
        _https(self.base, "Shotstack API")

    def build_timeline(self, plan, assets, callback_url):
        callback_url = _https(callback_url, "Shotstack回调")
        source_url = _https((assets or {}).get("source_url"), "源素材")
        source_type = str((assets or {}).get("source_type") or "video")
        if source_type not in {"video", "audio"}:
            raise ValueError("源素材类型无效")

        output = (plan or {}).get("output") or {}
        width = int(output.get("width") or 0)
        height = int(output.get("height") or 0)
        if width <= 0 or height <= 0:
            raise ValueError("输出尺寸无效")

        source_clips = []
        segments = (plan or {}).get("segments") or []
        if source_type == "video":
            for segment in segments:
                start_ms = int(segment["start_ms"])
                end_ms = int(segment["end_ms"])
                trim_ms = int(segment.get("source_start_ms", start_ms))
                source_clips.append(
                    {
                        "asset": {
                            "type": "video",
                            "src": source_url,
                            "trim": _seconds(trim_ms),
                            "volume": 1,
                        },
                        "start": _seconds(start_ms),
                        "length": _seconds(end_ms - start_ms),
                        "fit": "cover",
                    }
                )
        else:
            duration_ms = max(int(item["end_ms"]) for item in segments)
            source_clips.append(
                {
                    "asset": {"type": "audio", "src": source_url, "volume": 1},
                    "start": 0,
                    "length": _seconds(duration_ms),
                }
            )

        broll_clips = []
        materials = (assets or {}).get("materials") or {}
        for item in (plan or {}).get("broll") or []:
            material = materials.get(str(item.get("asset_id") or "")) or {}
            material_url = _https(material.get("url"), "补充素材")
            kind = str(material.get("kind") or "image")
            if kind not in {"image", "video"}:
                raise ValueError("补充素材类型无效")
            broll_clips.append(
                {
                    "asset": {"type": kind, "src": material_url},
                    "start": _seconds(item["start_ms"]),
                    "length": _seconds(item["end_ms"] - item["start_ms"]),
                    "fit": "cover",
                }
            )

        card_clips = []
        for item in (plan or {}).get("overlays") or []:
            card_clips.append(
                {
                    "asset": {
                        "type": "html",
                        "html": _card_html(item.get("text"), item.get("type")),
                        "width": min(width - 80, 960),
                        "height": 360,
                        "background": "transparent",
                    },
                    "start": _seconds(item["start_ms"]),
                    "length": _seconds(item["end_ms"] - item["start_ms"]),
                    "position": "center",
                }
            )

        tracks = []
        if card_clips:
            tracks.append({"clips": card_clips})
        captions_url = (assets or {}).get("captions_url")
        if captions_url:
            tracks.append(
                {
                    "clips": [
                        {
                            "asset": {
                                "type": "rich-caption",
                                "src": _https(captions_url, "字幕文件"),
                                "font": {
                                    "family": "Noto Sans SC",
                                    "size": 42,
                                    "color": "#FFFFFF",
                                },
                                "active": {"font": {"color": "#F5C451"}},
                                "animation": {"style": "highlight"},
                            },
                            "start": 0,
                            "length": "end",
                            "position": "bottom",
                            "offset": {"y": 0.12},
                        }
                    ]
                }
            )
        if broll_clips:
            tracks.append({"clips": broll_clips})
        tracks.append({"clips": source_clips})

        bgm_url = (assets or {}).get("bgm_url")
        if bgm_url:
            tracks.append(
                {
                    "clips": [
                        {
                            "asset": {
                                "type": "audio",
                                "src": _https(bgm_url, "背景音乐"),
                                "volume": 0.12,
                            },
                            "start": 0,
                            "length": "end",
                        }
                    ]
                }
            )
        return {
            "timeline": {"background": "#080F1F", "tracks": tracks},
            "output": {
                "format": "mp4",
                "size": {"width": width, "height": height},
            },
            "callback": callback_url,
        }

    def submit(self, edit):
        response = _request_json(
            "POST", self.base + "/render", edit, self.api_key
        )
        provider_job_id = str((response.get("response") or {}).get("id") or "")
        if not provider_job_id:
            raise RuntimeError("Shotstack未返回渲染任务ID")
        return provider_job_id

    def get_status(self, provider_job_id):
        provider_job_id = str(provider_job_id or "").strip()
        if not provider_job_id:
            raise ValueError("Shotstack渲染任务ID为空")
        response = _request_json(
            "GET",
            self.base + "/render/" + urllib.parse.quote(provider_job_id, safe=""),
            None,
            self.api_key,
        )
        raw = response.get("response") or {}
        return {
            "status": normalize_status(raw.get("status")),
            "url": raw.get("url"),
            "error": raw.get("error") or raw.get("message"),
        }

    def wait(self, provider_job_id, heartbeat, timeout=1200):
        deadline = time.monotonic() + int(timeout)
        while time.monotonic() < deadline:
            current = self.get_status(provider_job_id)
            if heartbeat:
                heartbeat("rendering")
            if current["status"] == RenderStatus.SUCCEEDED:
                return current
            if current["status"] == RenderStatus.FAILED:
                raise RuntimeError(
                    str(current.get("error") or "Shotstack渲染失败")[:240]
                )
            time.sleep(5)
        raise TimeoutError("Shotstack渲染等待超时")
