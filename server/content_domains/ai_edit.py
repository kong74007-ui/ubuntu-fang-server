# -*- coding: utf-8 -*-
"""Content-driven one-click editor for generated Digital IP videos."""

import base64
import math
import hashlib
import html
import json
import mimetypes
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from contextlib import closing
from fractions import Fraction

from . import ai_edit_store as store
from . import video
from .core import adb, _out_path, _resolve_out_file, public_url


AI_EDIT_MAX_SECONDS = max(15, int(os.environ.get("AI_EDIT_MAX_SECONDS", "180") or 180))
AI_EDIT_RENDER_TIMEOUT = max(120, int(os.environ.get("AI_EDIT_RENDER_TIMEOUT", "1800") or 1800))
AI_EDIT_GENERATE_FALLBACK = os.environ.get("AI_EDIT_GENERATE_FALLBACK", "1").strip().lower() not in {
    "0", "false", "no", "off",
}
DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "").strip()
QWEN_BASE = os.environ.get(
    "AI_EDIT_QWEN_BASE", "https://dashscope.aliyuncs.com/compatible-mode/v1"
).rstrip("/")
QWEN_VISION_MODEL = os.environ.get("AI_EDIT_VISION_MODEL", "qwen3-vl-flash").strip()
QWEN_DIRECTOR_MODEL = os.environ.get("AI_EDIT_DIRECTOR_MODEL", "qwen3.6-flash").strip()

EDIT_STYLES = {
    "auto": {"label": "AI 自动推荐", "desc": "根据内容和素材自动选择节奏", "accent": "#f1bd54", "accent2": "#f8df9e"},
    "content_first": {"label": "信息科普", "desc": "清晰层级和知识卡片", "accent": "#38bdf8", "accent2": "#a5f3fc"},
    "product_seeding": {"label": "产品种草", "desc": "突出产品证据和使用场景", "accent": "#fb7185", "accent2": "#fbcfe8"},
    "promo_fast": {"label": "快节奏促销", "desc": "优惠信息和行动提示", "accent": "#fb923c", "accent2": "#fde047"},
    "brand_premium": {"label": "高级品牌", "desc": "克制留白和高级配色", "accent": "#d4a84f", "accent2": "#f8e7b0"},
    "review_compare": {"label": "测评对比", "desc": "对比结构和结论卡片", "accent": "#8b5cf6", "accent2": "#c4b5fd"},
    "lifestyle": {"label": "生活方式", "desc": "柔和氛围和自然节奏", "accent": "#34d399", "accent2": "#bbf7d0"},
}
LAYOUTS = {"talking_full", "split_product", "product_full", "picture_in_picture", "title_focus"}
MOTIONS = {"none", "slow_push", "quick_zoom", "float_left"}
TRANSITIONS = {"cut", "crossfade", "wipe"}
STYLE_MOTION = {
    "content_first": ("float_left", "wipe"), "product_seeding": ("slow_push", "crossfade"),
    "promo_fast": ("quick_zoom", "cut"), "brand_premium": ("slow_push", "crossfade"),
    "review_compare": ("float_left", "wipe"), "lifestyle": ("slow_push", "crossfade"),
}
STYLE_PROFILES = {
    "content_first": {"scene_seconds": 6.2, "material_density": 0.55, "enter": 0.34, "y": 26,
                      "layouts": ("title_focus", "split_product", "talking_full")},
    "product_seeding": {"scene_seconds": 4.4, "material_density": 0.82, "enter": 0.3, "y": 34,
                        "layouts": ("split_product", "product_full", "picture_in_picture")},
    "promo_fast": {"scene_seconds": 3.0, "material_density": 0.9, "enter": 0.18, "y": 52,
                   "layouts": ("product_full", "split_product", "title_focus")},
    "brand_premium": {"scene_seconds": 6.8, "material_density": 0.45, "enter": 0.52, "y": 18,
                      "layouts": ("title_focus", "talking_full", "picture_in_picture")},
    "review_compare": {"scene_seconds": 5.0, "material_density": 0.72, "enter": 0.3, "y": 30,
                       "layouts": ("split_product", "picture_in_picture", "talking_full")},
    "lifestyle": {"scene_seconds": 5.8, "material_density": 0.62, "enter": 0.46, "y": 22,
                  "layouts": ("picture_in_picture", "talking_full", "product_full")},
}


class EditCancelled(RuntimeError):
    pass


def list_styles():
    return [
        {"id": key, "label": value["label"], "description": value["desc"]}
        for key, value in EDIT_STYLES.items()
    ]


def _font_path():
    configured = str(os.environ.get("AI_EDIT_FONT_PATH") or "").strip()
    candidates = [
        pathlib.Path(configured) if configured else None,
        pathlib.Path(__file__).resolve().parents[2] / "site" / "assets" / "fonts" / "noto-sans-sc-700.woff2",
        pathlib.Path("/var/www/html/assets/fonts/noto-sans-sc-700.woff2"),
    ]
    for candidate in candidates:
        if candidate and candidate.is_file():
            return candidate
    raise RuntimeError("一键剪辑字体文件缺失，请联系管理员")


def _gsap_path():
    configured = str(os.environ.get("AI_EDIT_GSAP_PATH") or "").strip()
    candidates = [
        pathlib.Path(configured) if configured else None,
        pathlib.Path(__file__).resolve().parent / "vendor" / "gsap.min.js",
    ]
    for candidate in candidates:
        if candidate and candidate.is_file():
            return candidate
    raise RuntimeError("一键剪辑动画运行库缺失，请联系管理员")


def _source_asset(asset_id, username):
    try:
        asset_id = int(asset_id)
    except (TypeError, ValueError):
        raise ValueError("请选择一个口播视频资产")
    if asset_id <= 0:
        raise ValueError("请选择一个口播视频资产")
    with closing(adb()) as c:
        row = c.execute(
            """SELECT id, job_id, username, mode, video_file, video_url, text,
                      resolution, ratio, status, created_at
               FROM video_assets
               WHERE id=? AND username=? AND status!='deleted'""",
            (asset_id, username),
        ).fetchone()
    if not row:
        raise ValueError("所选口播视频不存在或不属于当前账号")
    item = dict(row)
    if item.get("mode") not in {"text", "audio"}:
        raise ValueError("一键剪辑目前只支持数字化 IP 口播视频")
    if str(item.get("status") or "").lower() not in {"done", "completed", "ready"}:
        raise ValueError("所选口播视频还没有生成完成")
    source_file = _resolve_out_file(item.get("video_file"))
    if not source_file:
        raise ValueError("所选口播视频原文件已不存在，请重新生成后再剪辑")
    item["source_path"] = source_file
    return item


def validate_ai_edit_payload(payload, username):
    if not isinstance(payload, dict):
        raise ValueError("请求体不是合法 JSON")
    item = _source_asset(payload.get("source_video_asset_id"), username)
    style_id = str(payload.get("style_id") or "auto").strip().lower()
    if style_id not in EDIT_STYLES:
        raise ValueError("请选择有效的剪辑风格")
    refs = store.validate_material_refs(payload.get("materials"), username)
    facts = store.normalize_product_facts(payload.get("product_facts"))
    return {
        "source_video_asset_id": item["id"],
        "style_id": style_id,
        "materials": refs,
        "product_facts": facts,
        "mode": "ai_edit",
        "ratio": "9:16",
        "resolution": "1080p",
        "text": item.get("text") or "",
    }


def _probe_media(path):
    try:
        raw = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration:stream=codec_type,width,height", "-of", "json", str(path)],
            check=True, timeout=30, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ).stdout.decode("utf-8", "replace")
        data = json.loads(raw or "{}")
        duration = float((data.get("format") or {}).get("duration") or 0)
        streams = data.get("streams") or []
        stream = next((s for s in streams if s.get("codec_type") == "video"), {})
        width, height = int(stream.get("width") or 0), int(stream.get("height") or 0)
    except Exception as exc:
        raise ValueError("无法读取口播视频，请重新生成后再试") from exc
    if duration <= 0 or width <= 0 or height <= 0:
        raise ValueError("口播视频内容无效，请重新生成后再试")
    if not any(s.get("codec_type") == "audio" for s in streams):
        raise ValueError("口播视频缺少原声音轨，请重新生成后再试")
    if duration > AI_EDIT_MAX_SECONDS + 0.05:
        raise ValueError("当前一键剪辑最长支持 %d 秒视频" % AI_EDIT_MAX_SECONDS)
    return duration, width, height


def _stream_info(path):
    try:
        raw = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=start_time,duration:stream=codec_type,codec_name,pix_fmt,avg_frame_rate,r_frame_rate,start_time,duration,sample_rate,width,height",
             "-of", "json", str(path)],
            check=True, timeout=30, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ).stdout.decode("utf-8", "replace")
        data = json.loads(raw or "{}")
        streams = data.get("streams") or []
    except Exception:
        return {}
    visual = next((row for row in streams if row.get("codec_type") == "video"), {})
    audio = next((row for row in streams if row.get("codec_type") == "audio"), {})
    def rate(value):
        try:
            return float(Fraction(str(value or "0")))
        except Exception:
            return 0.0
    def number(value):
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0
    return {"video_codec": visual.get("codec_name"), "pix_fmt": visual.get("pix_fmt"),
            "audio_codec": audio.get("codec_name"), "sample_rate": int(audio.get("sample_rate") or 0),
            "fps": rate(visual.get("avg_frame_rate")), "r_fps": rate(visual.get("r_frame_rate")),
            "width": int(visual.get("width") or 0), "height": int(visual.get("height") or 0),
            "video_start": number(visual.get("start_time")),
            "audio_start": number(audio.get("start_time")),
            "video_duration": number(visual.get("duration")),
            "audio_duration": number(audio.get("duration")),
            "duration": number((data.get("format") or {}).get("duration")),
            "format_start": number((data.get("format") or {}).get("start_time"))}


def _prepare_source(source, output, duration):
    info = _stream_info(source)
    compatible = (
        info.get("video_codec") == "h264"
        and info.get("pix_fmt") in {"yuv420p", "yuvj420p"}
        and info.get("audio_codec") in {"aac", "mp3"}
        and abs(float(info.get("format_start") or 0)) <= 0.02
        and (not info.get("fps") or not info.get("r_fps")
             or abs(float(info["fps"]) - float(info["r_fps"])) <= 0.02)
    )
    if compatible:
        shutil.copy2(str(source), str(output))
        return "copied"
    command = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(source),
        "-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libx264", "-preset", "veryfast",
        "-crf", "20", "-g", "30", "-keyint_min", "30", "-sc_threshold", "0",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
        str(output),
    ]
    try:
        subprocess.run(command, check=True,
            timeout=max(180, min(900, int(duration * 6 + 120))),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except FileNotFoundError as exc:
        raise RuntimeError("服务器未安装 FFmpeg，暂时无法一键剪辑") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("口播视频预处理超时，请换一个更短的视频") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b"").decode("utf-8", "replace")[:240]
        raise RuntimeError("口播视频预处理失败" + ("：" + detail if detail else "")) from exc
    if not output.is_file() or output.stat().st_size <= 0:
        raise RuntimeError("口播视频预处理失败")
    return "normalized"


def _phase(job_id, stage, progress, message, video_phase=None):
    store.update_stage(job_id, stage, progress, message)
    try:
        video.update_video_asset_phase(job_id, video_phase or stage, status="running")
    except Exception:
        pass


def _ensure_not_cancelled(job_id):
    if store.cancel_requested(job_id):
        raise EditCancelled("用户已取消一键剪辑")


def _transcribe_source(source_path, known_text, duration, work, job_id):
    wav = work / "speech.wav"
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(source_path),
             "-vn", "-ar", "16000", "-ac", "1", str(wav)],
            check=True, timeout=max(120, int(duration * 3 + 60)),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except Exception as exc:
        raise RuntimeError("无法提取口播音频") from exc
    _ensure_not_cancelled(job_id)
    try:
        with video._whisper_sem:
            model = video._get_whisper_model()
            segment_iter, info = model.transcribe(
                str(wav), language="zh", vad_filter=True, word_timestamps=True)
            objects = list(segment_iter)
    except Exception as exc:
        raise RuntimeError("口播语音识别失败，请确认测试服务器已安装本地语音模型") from exc
    segments = [(float(item.start), float(item.end), str(item.text or "").strip())
                for item in objects if str(item.text or "").strip()]
    if not segments:
        raise RuntimeError("口播语音识别结果为空")
    words = []
    for segment in objects:
        for word in (getattr(segment, "words", None) or []):
            value = str(getattr(word, "word", "") or "").strip()
            if value:
                words.append((float(word.start), float(word.end), value))
    if known_text:
        try:
            words = video._retime_known_text(known_text, words, segments)
            segments = video._redistribute_known_text(known_text, segments)
        except Exception:
            pass
    if not words:
        words = video._segments_to_timed_words(segments)
    return {
        "provider": "faster-whisper",
        "model": str(getattr(video, "WHISPER_MODEL_NAME", "unknown")),
        "language": str(getattr(info, "language", "zh") or "zh"),
        "segments": [{"start": round(max(0.0, s), 3), "end": round(min(duration, e), 3), "text": t}
                     for s, e, t in segments if e > s],
        "words": [{"start": round(max(0.0, s), 3), "end": round(min(duration, e), 3), "text": t}
                  for s, e, t in words if e > s and s < duration],
    }


def _extract_json(value, what):
    text = str(value or "").strip().strip(chr(96)).strip()
    if text.lower().startswith("json"):
        text = text[4:].strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise RuntimeError("%s返回格式无效" % what)
    try:
        return json.loads(text[start:end + 1])
    except Exception as exc:
        raise RuntimeError("%s返回格式无效" % what) from exc


def _qwen_json(content, model, what, timeout=180):
    if not DASHSCOPE_API_KEY:
        raise RuntimeError("测试服务器未配置 Qwen 分析密钥")
    body = {"model": model, "messages": [{"role": "user", "content": content}],
            "temperature": 0.2, "enable_thinking": False,
            "response_format": {"type": "json_object"}}
    encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    last = None
    for attempt in range(3):
        request = urllib.request.Request(
            QWEN_BASE + "/chat/completions", data=encoded,
            headers={"Authorization": "Bearer " + DASHSCOPE_API_KEY,
                     "Content-Type": "application/json; charset=utf-8"},
            method="POST")
        try:
            with opener.open(request, timeout=timeout) as response:
                data = json.loads(response.read() or b"{}")
            message = (data.get("choices") or [{}])[0].get("message", {})
            return _extract_json(message.get("content"), what)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:240]
            last = RuntimeError("%s调用失败（HTTP %d）：%s" % (what, exc.code, detail))
            if exc.code not in {408, 409, 429, 500, 502, 503, 504}:
                break
        except Exception as exc:
            last = exc
        if attempt < 2:
            time.sleep(2 * (attempt + 1))
    raise RuntimeError("%s调用失败：%s" % (what, str(last)[:220]))


def _preview_image(material, work):
    source = pathlib.Path(material["source_path"])
    target = work / ("material-%s.jpg" % material["id"])
    if material.get("kind") == "video":
        command = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss",
                   "%.3f" % max(0.0, float(material.get("duration") or 0) * 0.45),
                   "-i", str(source), "-frames:v", "1", "-vf",
                   "scale='min(960,iw)':-2", "-q:v", "3", str(target)]
    else:
        command = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(source),
                   "-frames:v", "1", "-vf", "scale='min(960,iw)':-2", "-q:v", "3", str(target)]
    subprocess.run(command, check=True, timeout=60, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if not target.is_file() or target.stat().st_size <= 0:
        raise RuntimeError("辅助素材预览生成失败")
    return target


def _image_data_url(path):
    mime = mimetypes.guess_type(str(path))[0] or "image/jpeg"
    return "data:%s;base64,%s" % (mime, base64.b64encode(path.read_bytes()).decode("ascii"))


def _fallback_analysis(material):
    stem = pathlib.Path(str(material.get("filename") or "素材")).stem
    return {"summary": stem[:80] or "用户上传素材", "objects": [], "ocr": [],
            "keywords": [v for v in re.split(r"[\s_\-—]+", stem) if v][:8],
            "product_evidence": [], "quality": 60, "safe": True,
             "analysis_source": "local_metadata"}


def _material_relevance(material, transcript, product_facts):
    analysis = material.get("analysis") or {}
    evidence = " ".join(
        [str(analysis.get("summary") or ""), str(material.get("filename") or "")]
        + [str(value) for key in ("objects", "ocr", "keywords", "product_evidence")
           for value in (analysis.get(key) or [])]
    ).lower()
    context = " ".join(
        [str(value) for value in (product_facts or {}).values()]
        + [str(row.get("text") or "") for row in (transcript.get("segments") or [])]
    ).lower()
    tokens = {value for value in re.split(r"[\s，。！？、；：,.!?;:_\-]+", context) if len(value) >= 2}
    overlap = sum(1 for token in tokens if token in evidence)
    quality = max(0, min(100, int(analysis.get("quality") or 60)))
    source_bonus = 12 if material.get("source") == "uploaded" else 4
    return round(min(0.99, 0.28 + quality / 250.0 + min(0.28, overlap * 0.07) + source_bonus / 100.0), 3)


def _rank_materials(materials, transcript, product_facts):
    for material in materials:
        material.setdefault("source", "uploaded")
        material["match_score"] = _material_relevance(material, transcript, product_facts)
    source_order = {"uploaded": 0, "source_frame": 1, "reused": 2, "ai_generated": 3}
    return sorted(materials, key=lambda item: (
        item.get("usage") != "must_use", source_order.get(item.get("source"), 4),
        -float(item.get("match_score") or 0), int(item.get("ordinal") or 0)
    ))


def _analyze_materials(materials, work, job_id):
    usable = [item for item in materials if item.get("usage") != "exclude"]
    if not usable:
        return []
    previews = {}
    for material in usable:
        _ensure_not_cancelled(job_id)
        try:
            previews[int(material["id"])] = _preview_image(material, work)
        except Exception:
            previews[int(material["id"])] = None
    analyses = {}
    for material in usable:
        cached = material.get("job_analysis_json") or material.get("analysis_json") or {}
        if isinstance(cached, dict) and cached.get("summary"):
            analyses[int(material["id"])] = cached
    pending = [material for material in usable if int(material["id"]) not in analyses]
    for offset in range(0, len(pending), 6):
        batch = pending[offset:offset + 6]
        content = [{"type": "text", "text": (
            "你是短视频素材分析师。逐个分析素材预览，提取可被编导引用的客观证据。"
            "不要猜测品牌或功效。只输出 JSON：{\"items\":[{\"id\":数字,"
            "\"summary\":\"画面摘要\",\"objects\":[\"物体\"],\"ocr\":[\"画面文字\"],"
            "\"keywords\":[\"关键词\"],\"product_evidence\":[\"画面可见证据\"],"
            "\"quality\":0到100,\"safe\":true}]}。")}]
        batch_ids = {int(row["id"]) for row in batch}
        for material in batch:
            content.append({"type": "text", "text": "MATERIAL_ID=%s；类型=%s；文件名=%s；尺寸=%sx%s" % (
                material["id"], material.get("kind"), material.get("filename"),
                material.get("width"), material.get("height"))})
            preview = previews.get(int(material["id"]))
            if preview:
                content.append({"type": "image_url", "image_url": {"url": _image_data_url(preview)}})
        try:
            response = _qwen_json(content, QWEN_VISION_MODEL, "Qwen 素材分析")
            for item in response.get("items") or []:
                try:
                    material_id = int(item.get("id"))
                except Exception:
                    continue
                if material_id not in batch_ids:
                    continue
                analyses[material_id] = {
                    "summary": str(item.get("summary") or "")[:160],
                    "objects": [str(v)[:60] for v in (item.get("objects") or [])[:12]],
                    "ocr": [str(v)[:160] for v in (item.get("ocr") or [])[:16]],
                    "keywords": [str(v)[:60] for v in (item.get("keywords") or [])[:16]],
                    "product_evidence": [str(v)[:180] for v in (item.get("product_evidence") or [])[:12]],
                    "quality": max(0, min(100, int(item.get("quality") or 0))),
                    "safe": bool(item.get("safe", True)), "analysis_source": QWEN_VISION_MODEL}
        except Exception as exc:
            print("[ai-edit] material analysis fallback: %s" % str(exc)[:180], flush=True)
        for material in batch:
            material_id = int(material["id"])
            analysis = analyses.get(material_id) or _fallback_analysis(material)
            analyses[material_id] = analysis
            evidence = "；".join(analysis.get("product_evidence") or analysis.get("ocr") or [])
            store.set_material_analysis(job_id, material_id, analysis, evidence)
    result = []
    for material in usable:
        item = dict(material)
        item["analysis"] = analyses[int(material["id"])]
        item["preview_path"] = previews.get(int(material["id"]))
        result.append(item)
    return result


def _extract_source_frames(source_path, windows, work, limit):
    frames = []
    if limit <= 0 or not windows:
        return frames
    picks = sorted({min(len(windows) - 1, int((index + 0.5) * len(windows) / limit)) for index in range(limit)})
    for ordinal, window_index in enumerate(picks, 1):
        window = windows[window_index]
        source_time = (float(window["start"]) + float(window["end"])) / 2.0
        target = work / ("source-frame-%02d.jpg" % ordinal)
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", "%.3f" % source_time,
                 "-i", str(source_path), "-frames:v", "1", "-vf", "scale=1080:-2", "-q:v", "3", str(target)],
                check=True, timeout=60, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            width, height, _ = store._probe(target, "image")
        except Exception as exc:
            print("[ai-edit] source frame skipped: %s" % str(exc)[:160], flush=True)
            continue
        frames.append({
            "id": -1000 - ordinal, "kind": "image", "usage": "auto",
            "filename": "原视频截帧 %.1fs" % source_time, "source": "source_frame",
            "source_time": round(source_time, 3), "source_path": target,
            "width": width, "height": height, "duration": 0,
            "analysis": {"summary": "原口播视频真实画面截帧", "objects": ["口播人物"],
                         "ocr": [], "keywords": ["原视频", "人物"], "product_evidence": [],
                         "quality": 72, "safe": True, "analysis_source": "source_frame"},
        })
    return frames


def _generate_fallback_material(transcript, product_facts, visual_need="", ordinal=1):
    if not AI_EDIT_GENERATE_FALLBACK:
        return None
    last_error = None
    for attempt in range(2):
        try:
            from . import image as image_domain
            theme = product_facts.get("name") or product_facts.get("category") or "口播主题视觉"
            transcript_text = "".join(s.get("text") or "" for s in transcript.get("segments") or [])
            prompt = (
                "为竖屏短视频生成一张真实、干净、无文字、无商标的商业氛围辅助画面。"
                "只表现概念、生活场景或过渡氛围，不得生成或仿造产品包装、Logo、认证、规格、"
                "成分、用量和功效声明。主题：%s。当前镜头需要：%s。口播参考：%s。"
                "避免人物嘴型特写、畸形、水印和任何可读文字。"
            ) % (theme[:80], str(visual_need or "主题过渡")[:160], transcript_text[:260])
            result = image_domain.gen_image({
                "provider": "seedream", "variant": "std", "quality": "std", "ratio": "9:16",
                "count": 1, "prompt": prompt})
            path = _resolve_out_file(result.get("file"))
            if not path:
                raise RuntimeError("AI 补充视觉文件不存在")
            width, height, _ = store._probe(path, "image")
            return {"id": -2000 - int(ordinal), "kind": "image", "usage": "auto",
                    "filename": "AI 补充视觉 %02d.png" % int(ordinal), "source": "ai_generated",
                    "source_path": path, "generated_file": result.get("file"),
                    "generation_model": "seedream", "generation_prompt": prompt,
                    "width": width, "height": height, "duration": 0,
                    "analysis": {"summary": "根据口播主题生成的无文字氛围辅助画面",
                                 "objects": [], "ocr": [], "keywords": [theme[:40]],
                                 "product_evidence": [], "quality": 70, "safe": True,
                                 "analysis_source": "generated"}}
        except Exception as exc:
            last_error = exc
            if attempt == 0:
                time.sleep(1)
    if last_error:
        print("[ai-edit] fallback image skipped after retry: %s" % str(last_error)[:180], flush=True)
    return None


def _retry_timeline(retry_from_job_id, username):
    try:
        retry_from_job_id = int(retry_from_job_id or 0)
        if retry_from_job_id <= 0:
            return {}
        previous = store.public_job(retry_from_job_id, username, include_timeline=True)
        timeline = previous.get("timeline") or {}
        return timeline if isinstance(timeline, dict) else {}
    except Exception:
        return {}


def _reusable_generated_materials(timeline, limit):
    reusable = []
    for item in (timeline.get("materials") or []):
        if len(reusable) >= max(0, int(limit or 0)):
            break
        if item.get("source") not in {"ai_generated", "reused"}:
            continue
        generated_file = item.get("generated_file")
        path = _resolve_out_file(generated_file)
        if not path:
            continue
        try:
            width, height, _duration = store._probe(path, "image")
        except Exception:
            continue
        ordinal = len(reusable) + 1
        reusable.append({
            "id": -3000 - ordinal, "kind": "image", "usage": "auto",
            "filename": "复用 AI 视觉 %02d.png" % ordinal, "source": "reused",
            "source_path": path, "generated_file": generated_file,
            "generation_model": item.get("generation_model") or "seedream",
            "generation_prompt": item.get("generation_prompt") or "",
            "width": width, "height": height, "duration": 0,
            "analysis": item.get("analysis") or {
                "summary": "失败任务中已生成并通过校验的辅助视觉", "objects": [], "ocr": [],
                "keywords": [], "product_evidence": [], "quality": 70, "safe": True,
                "analysis_source": "retry_cache"},
        })
    return reusable


def _resolve_style(style_id, text):
    if style_id != "auto":
        return style_id
    content = str(text or "")
    rules = (
        ("promo_fast", ("限时", "优惠", "下单", "抢", "名额", "福利")),
        ("review_compare", ("对比", "测评", "区别", "优缺点", "实测")),
        ("product_seeding", ("产品", "好物", "推荐", "成分", "体验")),
        ("content_first", ("知识", "科普", "为什么", "方法", "步骤")),
        ("lifestyle", ("生活", "日常", "姐妹", "松弛", "氛围")),
    )
    for candidate, words in rules:
        if any(word in content for word in words):
            return candidate
    return "brand_premium"


def _window_text(words, start, end):
    return "".join(
        str(word.get("text") or "")
        for word in words
        if float(word.get("end") or 0) > start and float(word.get("start") or 0) < end
    ).strip()


def _beat_windows(transcript, duration, must_use_count=0, style_id="brand_premium"):
    words = transcript.get("words") or []
    profile = STYLE_PROFILES.get(style_id) or STYLE_PROFILES["brand_premium"]
    base_count = max(1, min(36, int(round(duration / profile["scene_seconds"])) or 1))
    count = max(base_count, min(store.JOB_MATERIAL_HARD_LIMIT, int(must_use_count or 0)))
    count = min(max(1, count), max(1, int(duration / 0.8)))
    windows = []
    for index in range(count):
        start = duration * index / count
        end = duration if index == count - 1 else duration * (index + 1) / count
        windows.append({
            "id": "scene-%02d" % (index + 1),
            "start": round(start, 3),
            "end": round(end, 3),
            "text": (_window_text(words, start, end) or "口播内容")[:220],
        })
    return windows


def _short_headline(text):
    cleaned = re.sub(r"\s+", "", str(text or "")).strip("，。！？、；：,.!?;:")
    return cleaned[:22] or "重点信息"


def _director_fallback(windows, materials, style_id="brand_premium"):
    usable = [item for item in materials if item.get("analysis", {}).get("safe", True)]
    ordered = (
        [item for item in usable if item.get("usage") == "must_use"]
        + [item for item in usable if item.get("usage") == "auto"]
    )
    profile = STYLE_PROFILES.get(style_id) or STYLE_PROFILES["brand_premium"]
    material_slots = max(len([row for row in ordered if row.get("usage") == "must_use"]),
                         int(round(len(windows) * profile["material_density"])))
    assignments = []
    for index, window in enumerate(windows):
        material = ordered[index] if index < min(len(ordered), material_slots) else None
        if material:
            layout = profile["layouts"][index % len(profile["layouts"])]
            if layout in {"talking_full", "title_focus"}:
                layout = "split_product"
        elif index in {0, len(windows) - 1}:
            layout = "title_focus"
        else:
            layout = "talking_full"
        assignments.append({
            "id": window["id"], "layout": layout,
            "material_id": material["id"] if material else None,
            "headline": _short_headline(window["text"]),
            "reason": "按口播时间段和素材优先级自动编排", "evidence": "",
        })
    return assignments


def _direct_timeline(windows, materials, product_facts, style_id, job_id):
    safe_materials = []
    for item in materials:
        analysis = item.get("analysis") or {}
        safe_materials.append({
            "id": item["id"], "usage": item.get("usage"), "kind": item.get("kind"),
            "source": item.get("source") or "uploaded", "match_score": item.get("match_score"),
            "summary": analysis.get("summary"), "objects": analysis.get("objects") or [],
            "ocr": analysis.get("ocr") or [], "keywords": analysis.get("keywords") or [],
            "product_evidence": analysis.get("product_evidence") or [],
            "quality": analysis.get("quality"), "safe": analysis.get("safe", True),
        })
    prompt = (
        "你是黄雀传媒短视频剪辑编导。为固定时间窗口分配画面，不得改变起止时间，"
        "不得虚构产品功效或素材中不存在的证据。must_use 素材必须优先出现。"
        "可选布局只有 talking_full、split_product、product_full、picture_in_picture、title_focus；"
        "motion 只有 none、slow_push、quick_zoom、float_left；"
        "transition 只有 cut、crossfade、wipe。"
        "product_full 只用于清晰且相关的素材；没有匹配素材时用 talking_full 或 title_focus。"
        "只输出 JSON：{\"assignments\":[{\"id\":\"scene-01\",\"layout\":\"talking_full\","
        "\"motion\":\"none\",\"transition\":\"cut\","
        "\"material_id\":数字或null,\"headline\":\"不超过22字\",\"reason\":\"编排原因\","
        "\"evidence\":\"使用的画面证据，不得编造\"}]}。\n"
        "风格=%s\n产品事实=%s\n时间窗口=%s\n素材分析=%s"
    ) % (
        style_id, json.dumps(product_facts, ensure_ascii=False),
        json.dumps(windows, ensure_ascii=False), json.dumps(safe_materials, ensure_ascii=False),
    )
    try:
        response = _qwen_json([{"type": "text", "text": prompt}], QWEN_DIRECTOR_MODEL, "Qwen 剪辑编导")
        assignments = response.get("assignments") or []
    except Exception as exc:
        print("[ai-edit] director fallback: %s" % str(exc)[:180], flush=True)
        assignments = _director_fallback(windows, materials, style_id)
    _ensure_not_cancelled(job_id)

    by_scene = {str(item.get("id")): item for item in assignments if isinstance(item, dict)}
    material_by_id = {int(item["id"]): item for item in materials}
    valid_ids = set(material_by_id)
    default_motion, default_transition = STYLE_MOTION.get(style_id, ("slow_push", "crossfade"))
    normalized = []
    for window in windows:
        raw = by_scene.get(window["id"]) or {}
        layout = str(raw.get("layout") or "talking_full")
        if layout not in LAYOUTS:
            layout = "talking_full"
        try:
            material_id = int(raw.get("material_id")) if raw.get("material_id") is not None else None
        except Exception:
            material_id = None
        if material_id not in valid_ids:
            material_id = None
        if material_id is None and layout in {"split_product", "product_full", "picture_in_picture"}:
            layout = "title_focus" if raw.get("headline") else "talking_full"
        selected = material_by_id.get(material_id) or {}
        motion = str(raw.get("motion") or default_motion)
        transition = str(raw.get("transition") or default_transition)
        if motion not in MOTIONS:
            motion = default_motion
        if transition not in TRANSITIONS:
            transition = default_transition
        normalized.append({
            **window, "layout": layout, "material_id": material_id,
            "asset_source": selected.get("source") if material_id is not None else "source_video",
            "match_score": selected.get("match_score") if material_id is not None else None,
            "motion": motion if material_id is not None else "none", "transition": transition,
            "headline": _short_headline(raw.get("headline") or window["text"]),
            "reason": str(raw.get("reason") or "")[:180],
            "evidence": str(raw.get("evidence") or "")[:220],
        })

    used = {scene.get("material_id") for scene in normalized}
    missing_must = [
        item for item in materials
        if item.get("usage") == "must_use" and int(item["id"]) not in used
    ]
    replaceable = [
        index for index, scene in enumerate(normalized)
        if scene.get("material_id") is None
        or material_by_id.get(scene.get("material_id"), {}).get("usage") != "must_use"
    ]
    for item, index in zip(missing_must, replaceable):
        normalized[index]["material_id"] = int(item["id"])
        normalized[index]["layout"] = "split_product" if index % 2 == 0 else "product_full"
        normalized[index]["asset_source"] = item.get("source") or "uploaded"
        normalized[index]["match_score"] = item.get("match_score")
        normalized[index]["motion"], normalized[index]["transition"] = default_motion, default_transition
        normalized[index]["reason"] = "用户标记为必用素材"
        normalized[index]["evidence"] = "；".join(
            (item.get("analysis") or {}).get("product_evidence")
            or (item.get("analysis") or {}).get("ocr") or []
        )[:220]
    if len(missing_must) > len(replaceable):
        raise RuntimeError("必用素材数量超过当前视频可容纳的画面段，请减少必用素材后重试")
    return normalized


def _caption_cues_from_words(words, duration, max_chars=15):
    cues, current, count = [], [], 0
    for word in words:
        value = re.sub(r"\s+", "", str(word.get("text") or ""))
        if not value:
            continue
        size = len(value)
        gap = float(word.get("start") or 0) - float(current[-1].get("end") or 0) if current else 0
        if current and (count + size > max_chars or gap > 0.8):
            cues.append({"start": float(current[0]["start"]), "end": float(current[-1]["end"]),
                         "text": "".join(item["value"] for item in current)})
            current, count = [], 0
        current.append({**word, "value": value})
        count += size
    if current:
        cues.append({"start": float(current[0]["start"]), "end": float(current[-1]["end"]),
                     "text": "".join(item["value"] for item in current)})
    result = []
    for index, cue in enumerate(cues):
        start = max(0.0, min(duration, cue["start"]))
        next_start = cues[index + 1]["start"] if index + 1 < len(cues) else duration
        end = min(duration, max(cue["end"], min(next_start, cue["end"] + 0.35)))
        if end > start:
            result.append({"start": round(start, 3), "end": round(end, 3), "text": cue["text"]})
    return result


def _validate_timeline(timeline):
    duration = float(timeline.get("duration") or 0)
    scenes = timeline.get("scenes") or []
    if duration <= 0 or not scenes:
        raise RuntimeError("剪辑时间轴为空")
    cursor = 0.0
    for index, scene in enumerate(scenes):
        start, end = float(scene.get("start") or 0), float(scene.get("end") or 0)
        if abs(start - cursor) > 0.06:
            raise RuntimeError("剪辑时间轴存在空隙或重叠")
        if end <= start:
            raise RuntimeError("剪辑时间轴片段时长无效")
        if scene.get("layout") not in LAYOUTS:
            raise RuntimeError("剪辑时间轴布局无效")
        if scene.get("motion", "none") not in MOTIONS:
            raise RuntimeError("剪辑时间轴动效无效")
        if scene.get("transition", "cut") not in TRANSITIONS:
            raise RuntimeError("剪辑时间轴转场无效")
        cursor = end
        scene["start"], scene["end"] = round(start, 3), round(end, 3)
        scene["duration"], scene["index"] = round(end - start, 3), index + 1
    if abs(cursor - duration) > 0.06:
        raise RuntimeError("剪辑时间轴没有覆盖完整口播")
    return timeline


def _legacy_timeline(text, duration):
    pieces = [p.strip() for p in re.split(r"(?<=[。！？!?])", str(text or "")) if p.strip()]
    pieces = pieces[:8] or ["数字化 IP 口播"]
    scenes = []
    for index, piece in enumerate(pieces):
        start = duration * index / len(pieces)
        end = duration if index == len(pieces) - 1 else duration * (index + 1) / len(pieces)
        scenes.append({
            "id": "scene-%02d" % (index + 1), "start": start, "end": end,
            "layout": "title_focus" if index in {0, len(pieces) - 1} else "talking_full",
            "material_id": None, "motion": "none", "transition": "crossfade",
            "headline": _short_headline(piece), "reason": "", "evidence": "",
        })
    return {
        "version": "2.0", "duration": duration, "scenes": scenes,
        "transcript": {"provider": "legacy", "words": [
            {"start": 0, "end": duration, "text": str(text or "数字化 IP 口播")}]},
    }


def _material_bundle(materials):
    bundle = {}
    for item in materials:
        source = pathlib.Path(item["source_path"])
        suffix = source.suffix.lower() or (".mp4" if item.get("kind") == "video" else ".jpg")
        name = "assets/material-%s%s" % (str(item["id"]).replace("-", "g"), suffix)
        bundle[int(item["id"])] = {"name": name, "path": source, "kind": item.get("kind")}
    return bundle


def build_hyperframes_html(
    text, duration, source_width, source_height, style_id="auto", timeline=None, material_files=None
):
    duration = max(1.0, float(duration))
    style_id = _resolve_style(style_id if style_id in EDIT_STYLES else "auto", text)
    style = EDIT_STYLES[style_id]
    profile = STYLE_PROFILES.get(style_id) or STYLE_PROFILES["brand_premium"]
    timeline = _validate_timeline(timeline or _legacy_timeline(text, duration))
    materials = material_files or {}
    scenes_html, animations = [], []
    for index, scene in enumerate(timeline["scenes"]):
        scene_no = index + 1
        start, end = float(scene["start"]), float(scene["end"])
        scene_duration = end - start
        layout = scene["layout"]
        material = materials.get(scene.get("material_id"))
        source_class = {
            "split_product": "source split-source",
            "product_full": "source source-pip",
            "picture_in_picture": "source source-pip",
        }.get(layout, "source source-full")
        scenes_html.append(
            '<video id="source-scene-%d" class="clip %s" src="input-video.mp4" '
            'data-start="%.3f" data-duration="%.3f" data-media-start="%.3f" '
            'data-track-index="0" muted playsinline></video>'
            % (scene_no, source_class, start, scene_duration, start)
        )
        if material:
            material_class = {
                "split_product": "material split-material",
                "product_full": "material material-full",
                "picture_in_picture": "material material-full",
            }.get(layout, "material material-full")
            if material["kind"] == "video":
                scenes_html.append(
                    '<video id="material-%d" class="clip %s" src="%s" data-start="%.3f" '
                    'data-duration="%.3f" data-track-index="1" data-media-start="0" muted playsinline></video>'
                    % (scene_no, material_class, html.escape(material["name"], quote=True), start, scene_duration)
                )
            else:
                scenes_html.append(
                    '<img id="material-%d" class="clip %s" src="%s" data-start="%.3f" '
                    'data-duration="%.3f" data-track-index="1" alt="">'
                    % (scene_no, material_class, html.escape(material["name"], quote=True), start, scene_duration)
                )
            motion = scene.get("motion") or "none"
            if motion == "slow_push":
                animations.append(
                    'tl.fromTo("#material-%d",{scale:1},{scale:1.07,duration:%.3f,ease:"none"},%.4f);'
                    % (scene_no, scene_duration, start))
            elif motion == "quick_zoom":
                animations.append(
                    'tl.fromTo("#material-%d",{scale:1.09},{scale:1,duration:.28,ease:"power2.out"},%.4f);'
                    % (scene_no, start))
            elif motion == "float_left":
                animations.append(
                    'tl.fromTo("#material-%d",{x:22},{x:-22,duration:%.3f,ease:"none"},%.4f);'
                    % (scene_no, scene_duration, start))
            if scene.get("transition") in {"crossfade", "wipe"}:
                animations.append(
                    'tl.fromTo("#material-%d",{opacity:0},{opacity:1,duration:.24,ease:"power1.out"},%.4f);'
                    % (scene_no, start))
        evidence = str(scene.get("evidence") or "").strip()
        evidence_html = '<div class="evidence">%s</div>' % html.escape(evidence[:90]) if evidence else ""
        scenes_html.append(
            '<section id="scene-shell-%d" class="clip scene-shell layout-%s" '
            'data-start="%.3f" data-duration="%.3f" data-track-index="2">'
            '<div id="scene-card-%d" class="scene-card"><span class="scene-index">%02d</span>'
            '<div class="scene-copy">%s</div>%s</div></section>'
            % (scene_no, layout, start, scene_duration, scene_no, scene_no,
               html.escape(str(scene.get("headline") or "重点信息")), evidence_html)
        )
        enter = round(start + min(0.12, scene_duration * 0.04), 4)
        leave = round(max(enter + 0.2, end - min(0.28, scene_duration * 0.12)), 4)
        animations.append(
            'tl.fromTo("#scene-card-%d",{opacity:0,y:%d,scale:.97},'
            '{opacity:1,y:0,scale:1,duration:%.2f,ease:"power2.out"},%.4f);'
            % (scene_no, profile["y"], profile["enter"], enter))
        if leave < end:
            animations.append(
                'tl.to("#scene-card-%d",{opacity:0,y:-18,duration:.24,ease:"power2.in"},%.4f);'
                % (scene_no, leave))
    captions = []
    words = (timeline.get("transcript") or {}).get("words") or []
    for index, cue in enumerate(_caption_cues_from_words(words, duration)):
        cue_duration = max(0.08, float(cue["end"]) - float(cue["start"]))
        captions.append(
            '<div id="caption-%d" class="clip caption" data-start="%.3f" data-duration="%.3f" '
            'data-track-index="5"><span id="caption-inner-%d" class="caption-inner">%s</span></div>'
            % (index + 1, cue["start"], cue_duration, index + 1, html.escape(cue["text"])))
        animations.append(
            'tl.fromTo("#caption-inner-%d",{opacity:0,y:12},'
            '{opacity:1,y:0,duration:.12,ease:"power1.out"},%.4f);'
            % (index + 1, float(cue["start"])))
    orientation = "竖屏原片" if source_height >= source_width else "横屏智能裁切"
    page = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=1080, height=1920">
  <title>黄雀 AI 一键剪辑</title>
  <style>
    @font-face{font-family:"Noto Sans SC";src:url("assets/noto-sans-sc-700.woff2") format("woff2");font-weight:300 900;font-style:normal;font-display:block}
    *{box-sizing:border-box}html,body{margin:0;width:1080px;height:1920px;overflow:hidden;background:#070b13}
    body{font-family:"Noto Sans SC",sans-serif;color:#fff}
    #root{--accent:__ACCENT__;--accent2:__ACCENT2__;position:relative;width:1080px;height:1920px;overflow:hidden;background:#070b13}
    .source,.material{position:absolute;object-fit:cover;background:#070b13}
    .source-full{inset:0;width:1080px;height:1920px}.split-source{left:0;top:0;width:594px;height:1920px}
    .split-material{right:0;top:0;width:486px;height:1920px}.material-full{inset:0;width:1080px;height:1920px;z-index:0}
    .source-pip{right:54px;top:98px;width:360px;height:520px;z-index:3;border:8px solid rgba(255,255,255,.92);border-radius:34px;box-shadow:0 24px 70px rgba(0,0,0,.45)}
    .clip{position:absolute}.shade{inset:0;z-index:4;background:linear-gradient(180deg,rgba(2,7,15,.38),transparent 26%,transparent 64%,rgba(2,7,15,.78));pointer-events:none}
    .brand{top:62px;left:58px;right:58px;z-index:8;display:flex;align-items:center;justify-content:space-between;font-size:24px;font-weight:850;letter-spacing:1.5px;text-shadow:0 3px 14px rgba(0,0,0,.6)}
    .brand-mark{display:flex;align-items:center;gap:13px}.brand-dot{width:15px;height:15px;border-radius:50%;background:var(--accent);box-shadow:0 0 26px var(--accent)}
    .brand-mode{padding:10px 18px;border:1px solid rgba(255,255,255,.28);border-radius:999px;background:rgba(7,11,19,.5);font-size:17px;color:rgba(255,255,255,.82)}
    .scene-shell{inset:0;z-index:7;display:flex;pointer-events:none}
    .scene-card{position:absolute;left:58px;right:58px;bottom:250px;min-height:172px;padding:30px 34px;border:1px solid rgba(255,255,255,.22);border-radius:30px;background:linear-gradient(135deg,rgba(8,13,24,.94),rgba(24,28,39,.82));box-shadow:0 24px 70px rgba(0,0,0,.42);display:grid;grid-template-columns:72px 1fr;gap:22px;align-items:center}
    .scene-index{width:72px;height:72px;display:grid;place-items:center;border-radius:20px;background:linear-gradient(135deg,var(--accent2),var(--accent));color:#10131a;font:900 25px/1 monospace}
    .scene-copy{min-width:0;font-size:43px;line-height:1.32;font-weight:900;overflow-wrap:anywhere;text-shadow:0 3px 16px rgba(0,0,0,.45)}
    .evidence{grid-column:2;color:rgba(255,255,255,.72);font-size:20px;line-height:1.35}
    .layout-split_product .scene-card{left:42px;right:520px;bottom:245px;grid-template-columns:58px 1fr;padding:26px}
    .layout-split_product .scene-index{width:58px;height:58px;border-radius:15px}.layout-split_product .scene-copy{font-size:34px}
    .layout-product_full .scene-card,.layout-picture_in_picture .scene-card{bottom:230px;background:rgba(5,9,17,.9)}
    .layout-title_focus .scene-card{top:700px;bottom:auto;min-height:270px;display:flex;flex-direction:column;justify-content:center;text-align:center;border-color:var(--accent);background:rgba(5,9,17,.86)}
    .layout-title_focus .scene-copy{font-size:58px}.layout-title_focus .scene-index{border-radius:50%}
    .caption{left:50px;right:50px;bottom:88px;z-index:12;text-align:center;font-size:42px;line-height:1.4;font-weight:900;text-shadow:0 4px 12px #000,0 2px 4px #000}
    .caption-inner{display:inline-block;padding:9px 15px;background:rgba(0,0,0,.58);border-radius:11px}
    .rail{left:58px;right:58px;bottom:52px;height:7px;z-index:10;border-radius:999px;background:linear-gradient(90deg,var(--accent2),var(--accent))}
    .style-content_first .scene-card{border-left:12px solid var(--accent);border-radius:12px}
    .style-product_seeding .scene-card{border-radius:44px;background:linear-gradient(135deg,rgba(55,18,35,.94),rgba(20,12,27,.9))}.style-product_seeding .scene-index{border-radius:50%}
    .style-promo_fast .scene-card{border:5px solid var(--accent);border-radius:8px}.style-promo_fast .scene-copy{font-size:46px}
    .style-brand_premium .scene-card{left:88px;right:88px;border-radius:2px;border-color:rgba(212,168,79,.7);background:rgba(4,7,12,.9)}.style-brand_premium .scene-index{border-radius:2px}
    .style-review_compare .scene-card{border-top:8px solid var(--accent);border-radius:18px}
    .style-lifestyle .scene-card{border-radius:46px;background:linear-gradient(135deg,rgba(8,42,34,.9),rgba(8,18,25,.85))}
  </style>
</head>
<body>
  <div id="root" class="style-__STYLE__" data-composition-id="ai-edit-main" data-start="0" data-width="1080" data-height="1920" data-fps="30" data-duration="__DURATION__">
    <audio id="source-audio" src="input-video.mp4" data-start="0" data-duration="__DURATION__" data-track-index="10" data-volume="1"></audio>
    __SCENES__
    <div id="shade" class="clip shade" data-start="0" data-duration="__DURATION__" data-track-index="6"></div>
    <div id="brand" class="clip brand" data-start="0" data-duration="__DURATION__" data-track-index="7"><div class="brand-mark"><span class="brand-dot"></span><span>黄雀 AI · 数字化 IP</span></div><span class="brand-mode">__STYLE_LABEL__ · __ORIENTATION__</span></div>
    __CAPTIONS__
    <div id="rail" class="clip rail" data-start="0" data-duration="__DURATION__" data-track-index="8"></div>
  </div>
  <script src="assets/gsap.min.js"></script>
  <script>
    const tl=gsap.timeline({paused:true});
    __ANIMATIONS__
    window.__timelines=window.__timelines||{};
    window.__timelines["ai-edit-main"]=tl;
  </script>
</body>
</html>
"""
    return (
        page.replace("__DURATION__", "%.3f" % duration)
        .replace("__STYLE__", style_id).replace("__STYLE_LABEL__", html.escape(style["label"]))
        .replace("__ACCENT__", style["accent"]).replace("__ACCENT2__", style["accent2"])
        .replace("__ORIENTATION__", html.escape(orientation))
        .replace("__SCENES__", "\n    ".join(scenes_html))
        .replace("__CAPTIONS__", "\n    ".join(captions))
        .replace("__ANIMATIONS__", "\n    ".join(animations))
    )


def _zip_file(archive, arcname, path, compress_type, fixed_time):
    info = zipfile.ZipInfo(arcname, fixed_time)
    info.compress_type = compress_type
    info.file_size = pathlib.Path(path).stat().st_size
    with archive.open(info, "w") as target, pathlib.Path(path).open("rb") as source:
        shutil.copyfileobj(source, target, length=1024 * 1024)


def _write_project_zip(
    zip_path, html_text, video_path, font_path=None, material_files=None, gsap_path=None
):
    fixed_time = (2026, 1, 1, 0, 0, 0)
    font_path = pathlib.Path(font_path) if font_path else _font_path()
    gsap_path = pathlib.Path(gsap_path) if gsap_path else _gsap_path()
    with zipfile.ZipFile(str(zip_path), "w") as archive:
        page = zipfile.ZipInfo("index.html", fixed_time)
        page.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(page, html_text.encode("utf-8"))
        _zip_file(archive, "input-video.mp4", video_path, zipfile.ZIP_STORED, fixed_time)
        _zip_file(archive, "assets/noto-sans-sc-700.woff2", font_path, zipfile.ZIP_STORED, fixed_time)
        _zip_file(archive, "assets/gsap.min.js", gsap_path, zipfile.ZIP_DEFLATED, fixed_time)
        for item in sorted((material_files or {}).values(), key=lambda row: row["name"]):
            _zip_file(archive, item["name"], item["path"], zipfile.ZIP_STORED, fixed_time)


def _preflight_project(html_text, timeline, material_files):
    ids = re.findall(r'\sid="([^"]+)"', html_text)
    duplicates = sorted({value for value in ids if ids.count(value) > 1})
    if duplicates:
        raise RuntimeError("剪辑工程存在重复元素编号：" + ",".join(duplicates[:5]))
    if "http://" in html_text or "https://" in html_text:
        raise RuntimeError("剪辑工程包含未冻结的网络资源")
    if 'window.__timelines["ai-edit-main"]' not in html_text:
        raise RuntimeError("剪辑工程缺少可定位动画时间轴")
    _validate_timeline(timeline)
    known_materials = {int(item.get("id")): item for item in (timeline.get("materials") or [])}
    for scene in timeline.get("scenes") or []:
        material_id = scene.get("material_id")
        if material_id is None:
            continue
        if int(material_id) not in material_files or int(material_id) not in known_materials:
            raise RuntimeError("剪辑时间轴引用了缺失素材")
        material = known_materials[int(material_id)]
        if material.get("source") == "ai_generated" and not material.get("generation_prompt"):
            raise RuntimeError("AI 生成素材缺少可追溯提示词")
    for item in material_files.values():
        if not pathlib.Path(item["path"]).is_file():
            raise RuntimeError("剪辑素材文件缺失")
    return {
        "engine": "hyperframes-contract", "unique_ids": len(ids),
        "scene_count": len(timeline.get("scenes") or []),
        "material_count": len(material_files), "passed": True,
    }


def _idempotent_json(method, path, body, key, what, tries=4):
    encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    last = None
    for attempt in range(tries):
        try:
            return video._heygen_request_json(
                method, path, encoded,
                {"Content-Type": "application/json", "Idempotency-Key": key},
                timeout=180, direct=True)
        except (video.HeyGenRateLimited, video.HeyGenNetworkError) as exc:
            last = exc
        except RuntimeError as exc:
            last = exc
            if "HTTP 409" not in str(exc):
                raise
        if attempt + 1 < tries:
            time.sleep(2 * (attempt + 1))
    raise RuntimeError("%s失败：%s" % (what, str(last)[:180]))


def _upload_project(zip_path, job_id):
    checksum = hashlib.sha256()
    with zip_path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            checksum.update(chunk)
    digest = checksum.hexdigest()
    create = _idempotent_json(
        "POST", "/assets/direct-uploads",
        {"filename": "huangque-ai-edit-%s.zip" % job_id, "content_type": "application/zip",
         "size_bytes": zip_path.stat().st_size, "checksum_sha256": digest},
        "hq-ai-edit-upload-%s" % job_id, "创建剪辑工程上传")
    info = create.get("data") or {}
    asset_id = str(info.get("asset_id") or "").strip()
    upload_url = str(info.get("upload_url") or "").strip()
    upload_headers = dict(info.get("upload_headers") or {})
    if not asset_id or not upload_url:
        raise RuntimeError("剪辑工程上传地址无效")
    upload_headers.setdefault("Content-Type", "application/zip")
    upload_headers.setdefault("Content-Length", str(zip_path.stat().st_size))
    last = None
    for attempt in range(3):
        try:
            with zip_path.open("rb") as source:
                request = urllib.request.Request(upload_url, data=source, headers=upload_headers, method="PUT")
                with video._heygen_direct_opener().open(request, timeout=600) as response:
                    response.read()
            last = None
            break
        except (OSError, urllib.error.HTTPError) as exc:
            last = exc
            if attempt < 2:
                time.sleep(2 * (attempt + 1))
    if last is not None:
        raise RuntimeError("上传剪辑工程失败：%s" % str(last)[:160])
    complete = _idempotent_json(
        "POST", "/assets/%s/complete" % asset_id, {"checksum_sha256": digest},
        "hq-ai-edit-complete-%s" % job_id, "确认剪辑工程上传")
    completed = complete.get("data") or {}
    if str(completed.get("asset_id") or asset_id) != asset_id:
        raise RuntimeError("剪辑工程上传确认失败")
    return asset_id


def _submit_render(asset_id, job_id, title):
    data = _idempotent_json(
        "POST", "/hyperframes/renders",
        {"project": {"type": "asset_id", "asset_id": asset_id}, "fps": 30,
         "quality": "standard", "format": "mp4", "resolution": "1080p",
         "aspect_ratio": "9:16", "composition": "index.html", "title": title[:160],
         "callback_id": "huangque-ai-edit-%s" % job_id},
        "hq-ai-edit-render-%s" % job_id, "提交一键剪辑")
    render_id = str((data.get("data") or {}).get("render_id") or "").strip()
    if not render_id:
        raise RuntimeError("一键剪辑未返回渲染任务编号")
    return render_id


def _wait_render(render_id, job_id=None):
    deadline = time.time() + AI_EDIT_RENDER_TIMEOUT
    last_status = ""
    while time.time() < deadline:
        if job_id:
            _ensure_not_cancelled(job_id)
        try:
            data = video._heygen_request_json(
                "GET", "/hyperframes/renders/%s" % render_id, timeout=90, direct=True)
        except video.HeyGenNetworkError:
            time.sleep(video.HEYGEN_POLL_INTERVAL)
            continue
        info = data.get("data") or {}
        status = str(info.get("status") or "").lower()
        if status != last_status:
            print("[ai-edit] render_id=%s status=%s" % (render_id, status), flush=True)
            last_status = status
        if status == "completed":
            if not info.get("video_url"):
                raise RuntimeError("一键剪辑已完成但没有返回成片地址")
            return info
        if status in {"failed", "error", "cancelled", "canceled"}:
            detail = str(info.get("failure_message") or "云端渲染失败")[:300]
            raise RuntimeError("一键剪辑失败：" + detail)
        time.sleep(video.HEYGEN_POLL_INTERVAL)
    raise TimeoutError("一键剪辑云端渲染超时")


def _decode_check(path, duration):
    try:
        subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", os.devnull],
            check=True, timeout=max(120, min(900, int(duration * 4 + 90))),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("一键剪辑成片完整解码校验失败") from exc


def _technical_qc(path, source_duration):
    info = _stream_info(path)
    problems = []
    if (info.get("width"), info.get("height")) != (1080, 1920):
        problems.append("尺寸不是 1080×1920")
    if info.get("video_codec") != "h264" or info.get("pix_fmt") != "yuv420p":
        problems.append("视频编码必须为 H.264 yuv420p")
    if info.get("audio_codec") != "aac":
        problems.append("音频编码必须为 AAC")
    if info.get("sample_rate") != 48000:
        problems.append("音频采样率必须为 48 kHz")
    if (abs(float(info.get("fps") or 0) - 30.0) > 0.05
            or abs(float(info.get("r_fps") or 0) - 30.0) > 0.05):
        problems.append("帧率必须为 30fps")
    output_duration = float(info.get("duration") or 0)
    if output_duration <= 0 or abs(output_duration - float(source_duration)) > (1.0 / 30.0 + 0.01):
        problems.append("成片时长与源视频偏差超过允许范围")
    video_start = float(info.get("video_start") or 0)
    audio_start = float(info.get("audio_start") or 0)
    format_start = float(info.get("format_start") or 0)
    if max(abs(video_start - audio_start), abs(video_start - format_start),
           abs(audio_start - format_start)) > 0.05:
        problems.append("新增音画起始偏差超过 50ms")
    video_duration = float(info.get("video_duration") or output_duration)
    audio_duration = float(info.get("audio_duration") or output_duration)
    if video_duration and audio_duration and abs(video_duration - audio_duration) > 0.1:
        problems.append("音视频流时长差超过 100ms")
    try:
        with open(path, "rb") as stream:
            head = stream.read(1024 * 1024)
        if head.find(b"moov") < 0 or (head.find(b"mdat") >= 0 and head.find(b"moov") > head.find(b"mdat")):
            problems.append("MP4 未启用 faststart")
    except Exception:
        problems.append("无法检查 MP4 faststart")
    if problems:
        raise RuntimeError("一键剪辑成片技术质检未通过：" + "；".join(problems))
    return {"passed": True, **info, "full_decode": True, "faststart": True}


def _extract_qc_frames(path, duration, work):
    frames = []
    for index, ratio in enumerate((0.15, 0.5, 0.85), 1):
        target = work / ("qc-%d.jpg" % index)
        subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-ss", "%.3f" % max(0.0, duration * ratio), "-i", str(path),
             "-frames:v", "1", "-vf", "scale=540:-2", "-q:v", "3", str(target)],
            check=True, timeout=60, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        frames.append(target)
    return frames


def _visual_qc(path, duration, product_facts, work):
    try:
        content = [{"type": "text", "text": (
            "检查竖屏短视频抽帧是否黑屏、严重遮挡、字幕不可读、人物异常裁切，"
            "以及画面是否与产品事实冲突。只输出 JSON：{\"pass\":true,\"issues\":[],"
            "\"subtitle_readable\":true,\"speaker_visible\":true,\"content_consistent\":true}。"
            "产品事实=%s") % json.dumps(product_facts, ensure_ascii=False)}]
        for frame in _extract_qc_frames(path, duration, work):
            content.append({"type": "image_url", "image_url": {"url": _image_data_url(frame)}})
        result = _qwen_json(content, QWEN_VISION_MODEL, "Qwen 成片质检")
    except Exception as exc:
        print("[ai-edit] visual qc unavailable: %s" % str(exc)[:180], flush=True)
        return {"passed": True, "model": None, "issues": ["视觉模型暂不可用，已通过技术质检"]}
    passed = bool(result.get("pass", True))
    issues = [str(value)[:160] for value in (result.get("issues") or [])[:10]]
    if not passed:
        raise RuntimeError("成片视觉质检未通过：" + "；".join(issues or ["画面异常"]))
    return {"passed": True, "model": QWEN_VISION_MODEL, "issues": issues,
            "subtitle_readable": bool(result.get("subtitle_readable", True)),
            "speaker_visible": bool(result.get("speaker_visible", True)),
            "content_consistent": bool(result.get("content_consistent", True))}


def _save_timeline(job_id, timeline):
    digest = hashlib.sha256(
        json.dumps(timeline, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:10]
    rel = "video/ai_edit_timeline_%d_%s.json" % (int(job_id), digest)
    path = _out_path(rel)
    path.write_text(json.dumps(timeline, ensure_ascii=False, indent=2), encoding="utf-8")
    return rel, public_url(rel, "application/json", private=True)


def _make_cover(video_path, job_id, duration):
    digest = hashlib.sha256((str(video_path) + str(duration)).encode("utf-8")).hexdigest()[:10]
    rel = "video/ai_edit_cover_%d_%s.jpg" % (int(job_id), digest)
    target = _out_path(rel)
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-ss", "%.3f" % min(max(0.1, duration * 0.12), max(0.1, duration - 0.1)),
         "-i", str(video_path), "-frames:v", "1", "-vf", "scale=540:-2",
         "-q:v", "3", str(target)],
        check=True, timeout=60, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return rel, public_url(rel, "image/jpeg", private=True)


def gen_ai_edit(payload):
    username = str(payload.get("_username") or "").strip()
    job_id = int(payload.get("_job_id") or 0)
    if not username or not job_id:
        raise ValueError("一键剪辑任务缺少账号信息")
    item = _source_asset(payload.get("source_video_asset_id"), username)
    _phase(job_id, "validating", 10, "校验口播视频和素材归属")
    duration, width, height = _probe_media(item["source_path"])
    _ensure_not_cancelled(job_id)
    source_text = str(item.get("text") or payload.get("text") or "数字化 IP 口播").strip()
    requested_style = str(payload.get("style_id") or "auto")
    resolved_style = _resolve_style(requested_style, source_text)
    product_facts = store.normalize_product_facts(payload.get("product_facts"))
    previous_timeline = _retry_timeline(payload.get("_retry_from_job_id"), username)
    title = (source_text[:36] or "数字化 IP 口播") + " · 一键剪辑"
    materials = store.job_materials(job_id, username)
    for material in materials:
        path, _row = store.material_path(material["id"], username)
        material["source_path"] = path

    with tempfile.TemporaryDirectory(prefix="hq-ai-edit-%s-" % job_id) as temp_dir:
        work = pathlib.Path(temp_dir)
        normalized = work / "input-video.mp4"
        project_zip = work / "project.zip"

        _phase(job_id, "normalizing", 16, "准备可精确定位的视频源", "composing")
        normalization = _prepare_source(item["source_path"], normalized, duration)
        normalized_duration, normalized_width, normalized_height = _probe_media(normalized)
        _ensure_not_cancelled(job_id)

        _phase(job_id, "transcribing", 27, "按真实语音生成逐词时间轴", "composing")
        cached_transcript = previous_timeline.get("transcript") or {}
        cached_duration = float(previous_timeline.get("duration") or 0)
        if (cached_transcript.get("words") and cached_duration > 0
                and abs(cached_duration - normalized_duration) <= 0.05):
            transcript = cached_transcript
            _phase(job_id, "transcribing", 31, "已复用失败任务的真实语音时间轴", "composing")
        else:
            transcript = _transcribe_source(
                normalized, source_text, normalized_duration, work, job_id)
        store.set_timeline(job_id, {
            "version": "2.0", "duration": round(normalized_duration, 3),
            "checkpoint_stage": "transcribed", "transcript": transcript,
        })

        _phase(job_id, "analyzing_assets", 42, "识别辅助素材内容、文字和产品证据", "composing")
        analyzed_materials = _rank_materials(
            _analyze_materials(materials, work, job_id), transcript, product_facts)
        must_use_count = sum(1 for material in analyzed_materials if material.get("usage") == "must_use")
        windows = _beat_windows(transcript, normalized_duration, must_use_count, resolved_style)
        profile = STYLE_PROFILES.get(resolved_style) or STYLE_PROFILES["brand_premium"]
        target_materials = min(len(windows), max(must_use_count, int(math.ceil(
            normalized_duration / 3.0 * profile["material_density"]))))
        relevant_uploads = sum(1 for material in analyzed_materials
                               if material.get("source") == "uploaded" and material.get("match_score", 0) >= 0.45)
        missing = max(0, target_materials - relevant_uploads)
        source_frames = []
        if missing:
            source_frame_limit = min(missing, max(1, min(4, int(math.ceil(normalized_duration / 30.0)))))
            source_frames = _extract_source_frames(normalized, windows, work, source_frame_limit)
            analyzed_materials.extend(source_frames)
            missing = max(0, target_materials - relevant_uploads - len(source_frames))
        reused_materials = _reusable_generated_materials(previous_timeline, missing)
        if reused_materials:
            analyzed_materials.extend(reused_materials)
            missing = max(0, missing - len(reused_materials))
        generated_limit = 6 if normalized_duration <= 20.0 else 10
        if missing and AI_EDIT_GENERATE_FALLBACK:
            generate_count = min(missing, generated_limit)
            generated_offset = len(reused_materials)
            for ordinal in range(generated_offset + 1, generated_offset + generate_count + 1):
                _ensure_not_cancelled(job_id)
                current = ordinal - generated_offset
                _phase(job_id, "generating_visual", 44 + int(8 * current / max(1, generate_count)),
                       "生成缺失的概念视觉 %d / %d" % (current, generate_count), "composing")
                assigned_before = relevant_uploads + len(source_frames) + len(reused_materials)
                window = windows[min(len(windows) - 1, assigned_before + current - 1)]
                generated = _generate_fallback_material(
                    transcript, product_facts, window.get("text") or "主题过渡", ordinal)
                if generated:
                    analyzed_materials.append(generated)
                    store.set_timeline(job_id, {
                        "version": "2.0", "duration": round(normalized_duration, 3),
                        "checkpoint_stage": "generating_assets", "transcript": transcript,
                        "materials": [
                            {"source": item.get("source"), "generated_file": item.get("generated_file"),
                             "generation_model": item.get("generation_model"),
                             "generation_prompt": item.get("generation_prompt"),
                             "analysis": item.get("analysis") or {}}
                            for item in analyzed_materials if item.get("generated_file")
                        ],
                    })
        analyzed_materials = _rank_materials(analyzed_materials, transcript, product_facts)
        _ensure_not_cancelled(job_id)
        _phase(job_id, "directing", 58, "AI 编导正在规划每个画面", "composing")
        scenes = _direct_timeline(
            windows, analyzed_materials, product_facts, resolved_style, job_id)
        timeline = _validate_timeline({
            "version": "2.0",
            "duration": round(normalized_duration, 3),
            "source": {"video_asset_id": item["id"], "width": normalized_width,
                       "height": normalized_height, "normalization": normalization},
            "style_id": resolved_style,
            "product_facts": product_facts,
            "transcript": transcript,
            "materials": [
                {"id": material["id"], "kind": material.get("kind"),
                  "usage": material.get("usage"), "filename": material.get("filename"),
                  "source": material.get("source") or "uploaded",
                  "source_time": material.get("source_time"),
                  "match_score": material.get("match_score"),
                  "generated_file": material.get("generated_file"),
                  "generation_model": material.get("generation_model"),
                  "generation_prompt": material.get("generation_prompt"),
                  "analysis": material.get("analysis") or {}}
                for material in analyzed_materials
            ],
            "scenes": scenes,
            "director": {"model": QWEN_DIRECTOR_MODEL},
        })
        store.set_timeline(job_id, timeline)
        _ensure_not_cancelled(job_id)

        assigned_ids = {
            int(scene["material_id"]) for scene in scenes
            if scene.get("material_id") is not None
        }
        assigned_materials = [
            material for material in analyzed_materials if int(material["id"]) in assigned_ids
        ]
        material_bundle = _material_bundle(assigned_materials)
        _phase(job_id, "composing", 72, "制作分屏、画中画、标题和逐词字幕")
        page = build_hyperframes_html(
            source_text, normalized_duration, normalized_width, normalized_height,
            requested_style, timeline=timeline, material_files=material_bundle)
        _phase(job_id, "preflight", 80, "检查时间轴覆盖、素材和动画定位", "composing")
        preflight = _preflight_project(page, timeline, material_bundle)
        timeline["preflight"] = preflight
        store.set_timeline(job_id, timeline)
        _write_project_zip(project_zip, page, normalized, material_files=material_bundle)
        _ensure_not_cancelled(job_id)

        _phase(job_id, "uploading", 85, "上传剪辑工程")
        asset_id = _upload_project(project_zip, job_id)
        _phase(job_id, "rendering", 90, "云端渲染 1080P 成片")
        with video.heygen_slot("一键剪辑"):
            render_id = _submit_render(asset_id, job_id, title)
            video.update_video_asset_phase(
                job_id, "rendering", status="running",
                provider_video_id=render_id, model="hyperframes")
            render = _wait_render(render_id, job_id=job_id)

        _phase(job_id, "downloading", 94, "保存渲染成片")
        output_file = video._download_video_file_direct(
            render.get("video_url"), prefix="ai_edit")
        output_path = _resolve_out_file(output_file)
        if not output_path:
            raise RuntimeError("一键剪辑成片保存失败")
        _phase(job_id, "qc", 97, "检查画面、字幕、尺寸、时长和完整解码")
        output_duration, output_width, output_height = _probe_media(output_path)
        _decode_check(output_path, output_duration)
        technical_qc = _technical_qc(output_path, normalized_duration)
        visual_qc = _visual_qc(output_path, output_duration, product_facts, work)

    _phase(job_id, "saving", 99, "保存封面、时间轴和来源清单")
    timeline["qc"] = {
        "technical": technical_qc,
        "visual": visual_qc,
    }
    timeline_file, timeline_url = _save_timeline(job_id, timeline)
    cover_file, cover_url = _make_cover(output_path, job_id, output_duration)
    store.set_timeline(job_id, timeline)
    material_breakdown = {"uploaded": 0, "source_frame": 0, "reused": 0, "ai_generated": 0}
    for material in assigned_materials:
        source_kind = str(material.get("source") or "uploaded")
        material_breakdown[source_kind] = material_breakdown.get(source_kind, 0) + 1
    store.set_usage(job_id, {
        "asr": {"provider": transcript.get("provider"), "audio_seconds": round(normalized_duration, 3)},
        "asset_analysis": {"model": QWEN_VISION_MODEL, "asset_count": len(materials)},
        "director": {"model": QWEN_DIRECTOR_MODEL, "scene_count": len(timeline.get("scenes") or [])},
        "image_generation": {"model": "seedream", "count": material_breakdown.get("ai_generated", 0)},
        "hyperframes": {"render_id": render_id, "output_seconds": round(output_duration, 3)},
    }, {"billable_points": 30, "provider_currency_cost": None})
    return {
        "mode": "ai_edit", "video_file": output_file,
        "video_url": public_url(output_file, "video/mp4", private=True),
        "image_file": cover_file, "image_url": cover_url,
        "timeline_file": timeline_file, "timeline_url": timeline_url,
        "timeline": timeline, "text": source_text, "resolution": "1080p", "ratio": "9:16",
        "phase": "done", "status": "done", "provider_video_id": render_id,
        "model": "hyperframes", "duration": output_duration,
        "size_bytes": output_path.stat().st_size, "result_version": 1,
        "source_video_asset_id": item["id"], "style_id": resolved_style,
        "material_count": len(assigned_materials), "material_breakdown": material_breakdown,
        "scene_count": len(timeline.get("scenes") or []), "asr_provider": transcript["provider"],
        "director_model": QWEN_DIRECTOR_MODEL, "qc": timeline["qc"],
    }


HANDLERS = {"ai_edit": gen_ai_edit}
