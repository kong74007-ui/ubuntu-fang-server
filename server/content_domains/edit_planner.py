# -*- coding: utf-8 -*-
"""使用通义千问 JSON Mode 生成并校验 edit-plan v1。"""
import json
import os
import urllib.error
import urllib.request

from . import ai_edit_styles, edit_plan


CHAT_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
API_KEY = os.environ.get("DASHSCOPE_API_KEY", "").strip()
MODEL = os.environ.get("AI_EDIT_QWEN_MODEL", "qwen-plus").strip() or "qwen-plus"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


def _chat(request_body):
    if not API_KEY:
        raise RuntimeError("DASHSCOPE_API_KEY 未配置")
    body = json.dumps(request_body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        CHAT_URL,
        data=body,
        headers={
            "Authorization": "Bearer " + API_KEY,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        try:
            error = json.loads(exc.read(4096).decode("utf-8", errors="replace"))
            detail = str(error.get("message") or error.get("error") or "")[:240]
        except Exception:
            detail = ""
        raise RuntimeError("千问剪辑方案请求失败%s" % (("：" + detail) if detail else ""))
    except OSError as exc:
        raise RuntimeError("千问剪辑方案网络异常：%s" % str(exc)[:160])
    if len(raw) > MAX_RESPONSE_BYTES:
        raise RuntimeError("千问剪辑方案响应过大")
    try:
        response = json.loads(raw.decode("utf-8"))
        content = response["choices"][0]["message"]["content"]
        result = json.loads(content) if isinstance(content, str) else content
    except (KeyError, IndexError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("千问未返回合法JSON剪辑方案")
    if not isinstance(result, dict):
        raise RuntimeError("千问未返回合法JSON剪辑方案")
    return result


def _style(style_id):
    for item in ai_edit_styles.list_styles():
        if item["id"] == style_id:
            return item
    raise ValueError("不支持的剪辑风格")


def _safe_transcript(transcript):
    transcript = transcript if isinstance(transcript, dict) else {}
    safe = {"text": str(transcript.get("text") or "")[:100_000]}
    sentences = []
    for item in (transcript.get("sentences") or [])[:1000]:
        if not isinstance(item, dict):
            continue
        sentences.append(
            {
                "begin_time": int(item.get("begin_time") or 0),
                "end_time": int(item.get("end_time") or 0),
                "text": str(item.get("text") or "")[:1000],
            }
        )
    safe["sentences"] = sentences
    words = []
    for item in (transcript.get("words") or [])[:5000]:
        if not isinstance(item, dict):
            continue
        words.append(
            {
                "begin_time": int(item.get("begin_time") or 0),
                "end_time": int(item.get("end_time") or 0),
                "text": str(item.get("text") or "")[:80],
            }
        )
    safe["words"] = words
    return safe


def _safe_materials(material_catalog):
    safe = []
    seen = set()
    for item in (material_catalog or [])[:100]:
        if not isinstance(item, dict):
            continue
        material_id = str(item.get("id") or "")[:128]
        if not material_id or material_id in seen:
            continue
        seen.add(material_id)
        safe.append(
            {
                "id": material_id,
                "kind": str(item.get("kind") or "")[:24],
                "role": str(item.get("role") or "")[:40],
                "origin": str(item.get("origin") or "")[:24],
                "description": str(item.get("description") or "")[:300],
            }
        )
    return safe


def _system_prompt(style, source_duration_ms, allowed_ids):
    return """你是黄雀AI视频剪辑导演。只返回一个合法 JSON 对象，不得返回Markdown或代码。
JSON必须符合 edit-plan v1：
- version 固定为 \"1.0\"，ratio 只能是 9:16、16:9、1:1。
- segments/captions/overlays/broll 都是数组；每项必须有整数 start_ms、end_ms。
- 所有时间满足 0 <= start_ms < end_ms <= %(duration)d。
- segments 可带 source_start_ms/source_end_ms，范围同样不得越界。
- broll.asset_id 只能来自允许素材ID：%(assets)s。
- overlays 只允许 claim_card、title_card、evidence_card 布局；不得输出HTML。
- 不得编造口播没有表达的事实、功效或数据。
导演风格规则：%(rules)s
""" % {
        "duration": int(source_duration_ms),
        "assets": json.dumps(sorted(allowed_ids), ensure_ascii=False),
        "rules": style["director_rules"],
    }


def _request(system_prompt, context):
    return {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": json.dumps(context, ensure_ascii=False),
            },
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.3,
        "enable_thinking": False,
    }


def generate_plan(transcript, style, source_duration_ms, material_catalog):
    style_item = _style(str(style or ""))
    materials = _safe_materials(material_catalog)
    allowed_ids = {item["id"] for item in materials}
    system_prompt = _system_prompt(style_item, source_duration_ms, allowed_ids)
    context = {
        "style": style_item["id"],
        "source_duration_ms": int(source_duration_ms),
        "transcript": _safe_transcript(transcript),
        "materials": materials,
    }
    first = _chat(_request(system_prompt, context))
    try:
        return edit_plan.validate_edit_plan(first, source_duration_ms, allowed_ids)
    except ValueError as first_error:
        repair_context = {
            "instruction": "修复下列剪辑方案，并只返回完整的JSON对象。",
            "validation_error": str(first_error)[:500],
            "invalid_plan": first,
            "source_duration_ms": int(source_duration_ms),
            "allowed_material_ids": sorted(allowed_ids),
        }
        repaired = _chat(_request(system_prompt, repair_context))
        try:
            return edit_plan.validate_edit_plan(
                repaired, source_duration_ms, allowed_ids
            )
        except ValueError as second_error:
            raise RuntimeError("剪辑方案校验失败：%s" % str(second_error)[:240])
