"""Constrained Qwen director producing only provider-neutral edit plans."""

from __future__ import annotations

import json
from typing import Any, Final

from .ai_edit_v2_providers.base import ProviderError, ProviderResult
from .ai_edit_v2_schema import (
    ASPECT_RATIOS,
    CAPTION_STYLES,
    COMPONENT_FAMILIES,
    CREATION_MODES,
    MUSIC_POLICIES,
    SCENE_LAYOUTS,
    SCENE_TRANSITIONS,
    SCENE_VISUAL_TYPES,
    SFX_POLICIES,
    SPEECH_POLICIES,
    validate_edit_plan,
)
from .ai_edit_v2_templates import TemplateError, get_published_template


_SYSTEM_PROMPT: Final = """你是 AI Edit V2 的受约束导演，只能返回一个 JSON 对象。
不得改写字幕正文；字幕正文始终且只能来自输入 text_timeline，输出 caption_plan 只能引用它。
不得输出 COS、URL、Shotstack、provider、api_key、tracks、HTML 或代码字段。
不得输出渲染指令、素材地址或具体画面坐标。
每个 scene 必须且只能使用已发布的稳定组件语义，并包含 id、start_ms、end_ms、intent、layout、visual_type、headline、material_slots、transition。
layout 可选：{layouts}。
visual_type 可选：{visual_types}。
transition 可选：{transitions}。
component_family 可选：{families}。
caption style 可选：{caption_styles}。
audio policy：speech_policy={speech}; music_policy 可选 {music}; sfx_policy 可选 {sfx}。
场景必须从 0 开始、首尾连续无重叠，最后一个场景必须结束于 duration_ms。
不要使用 Markdown 代码围栏。""".format(
    layouts=", ".join(sorted(SCENE_LAYOUTS)),
    visual_types=", ".join(sorted(SCENE_VISUAL_TYPES)),
    transitions=", ".join(sorted(SCENE_TRANSITIONS)),
    families=", ".join(sorted(COMPONENT_FAMILIES)),
    caption_styles=", ".join(sorted(CAPTION_STYLES)),
    speech=", ".join(sorted(SPEECH_POLICIES)),
    music=", ".join(sorted(MUSIC_POLICIES)),
    sfx=", ".join(sorted(SFX_POLICIES)),
)


class DirectorError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _positive_int(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise DirectorError("director_context_invalid")
    return value


def _sanitize_timeline_item(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise DirectorError("director_context_invalid")
    text = item.get("text")
    start_ms = item.get("start_ms")
    end_ms = item.get("end_ms")
    if not isinstance(text, str) or not text:
        raise DirectorError("director_context_invalid")
    if (
        not isinstance(start_ms, int)
        or isinstance(start_ms, bool)
        or start_ms < 0
        or not isinstance(end_ms, int)
        or isinstance(end_ms, bool)
        or end_ms < start_ms
    ):
        raise DirectorError("director_context_invalid")
    return {"text": text, "start_ms": start_ms, "end_ms": end_ms}


def _sanitize_timeline(timeline: Any) -> dict[str, Any]:
    if not isinstance(timeline, dict):
        raise DirectorError("director_context_invalid")
    text = timeline.get("text")
    words = timeline.get("words")
    sentences = timeline.get("sentences")
    if not isinstance(text, str) or not text:
        raise DirectorError("director_context_invalid")
    if not isinstance(words, list) or not words:
        raise DirectorError("director_context_invalid")
    if not isinstance(sentences, list) or not sentences:
        raise DirectorError("director_context_invalid")
    sanitized = {
        "text": text,
        "words": [_sanitize_timeline_item(item) for item in words],
        "sentences": [_sanitize_timeline_item(item) for item in sentences],
    }
    source_type = timeline.get("source_type")
    if isinstance(source_type, str) and source_type:
        sanitized["source_type"] = source_type
    coverage = timeline.get("coverage")
    if isinstance(coverage, (int, float)) and not isinstance(coverage, bool):
        sanitized["coverage"] = coverage
    return sanitized


def _safe_context(context: Any) -> dict[str, Any]:
    if not isinstance(context, dict):
        raise DirectorError("director_context_invalid")
    creation_mode = context.get("creation_mode")
    aspect_ratio = context.get("aspect_ratio")
    if creation_mode not in CREATION_MODES or aspect_ratio not in ASPECT_RATIOS:
        raise DirectorError("director_context_invalid")
    target_duration_ms = _positive_int(context.get("target_duration_ms"))
    timeline = _sanitize_timeline(context.get("text_timeline"))

    safe: dict[str, Any] = {
        "creation_mode": creation_mode,
        "text_timeline": timeline,
        "aspect_ratio": aspect_ratio,
        "target_duration_ms": target_duration_ms,
        "language": "zh-CN",
    }
    template_id = context.get("template_id")
    style_text = context.get("style_text")
    if template_id is not None:
        try:
            safe["template"] = get_published_template(template_id, context.get("template_version"))
        except TemplateError as exc:
            raise DirectorError("director_context_invalid") from exc
    elif isinstance(style_text, str) and style_text.strip():
        safe["style_text"] = style_text.strip()
    else:
        raise DirectorError("director_context_invalid")
    return safe


def _response_content(result: Any) -> str:
    if not isinstance(result, ProviderResult) or result.capability != "director":
        raise DirectorError("director_provider_failed")
    content = result.payload.get("content")
    if not isinstance(content, str) or not content.strip():
        raise DirectorError("director_schema_invalid")
    return content


def _decode_and_validate(content: str, safe_context: dict[str, Any]) -> dict[str, Any]:
    try:
        plan = json.loads(content)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("响应不是合法JSON对象") from exc
    validate_edit_plan(plan)
    if plan["creation_mode"] != safe_context["creation_mode"]:
        raise ValueError("creation_mode与请求不一致")
    if plan["aspect_ratio"] != safe_context["aspect_ratio"]:
        raise ValueError("aspect_ratio与请求不一致")
    if plan.get("target_duration_ms") != safe_context["target_duration_ms"]:
        raise ValueError("target_duration_ms与请求不一致")
    if plan["duration_ms"] != safe_context["target_duration_ms"]:
        raise ValueError("duration_ms与目标时长不一致")
    template = safe_context.get("template")
    if template is not None:
        expected_style = {
            "template_id": template["id"],
            "template_version": template["version"],
            "component_family": template["component_family"],
        }
        if plan["style_system"] != expected_style:
            raise ValueError("style_system与已发布模板不一致")
        sound_policy = template["sound_policy"]
        if plan["audio_plan"]["music_policy"] != sound_policy["music_policy"]:
            raise ValueError("audio_plan.music_policy与已发布模板不一致")
        if plan["audio_plan"]["sfx_policy"] != sound_policy["sfx_policy"]:
            raise ValueError("audio_plan.sfx_policy与已发布模板不一致")
    return plan


def _initial_prompt(safe_context: dict[str, Any]) -> str:
    return json.dumps(
        {
            "task": "generate_semantic_edit_plan_v2",
            "required_version": "2.0",
            "context": safe_context,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _repair_prompt(error: ValueError, previous_response: str) -> str:
    return json.dumps(
        {
            "schema_errors": str(error),
            "previous_response": previous_response,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def generate_edit_plan(context: dict[str, Any], client: Any, max_repairs: int = 2) -> dict[str, Any]:
    """Generate and validate a version 2.0 semantic edit plan with bounded repairs."""

    if not isinstance(max_repairs, int) or isinstance(max_repairs, bool) or max_repairs < 0:
        raise DirectorError("director_context_invalid")
    repair_limit = min(max_repairs, 2)
    safe_context = _safe_context(context)
    user_prompt = _initial_prompt(safe_context)
    for attempt in range(repair_limit + 1):
        content = ""
        try:
            result = client.generate_edit_plan(_SYSTEM_PROMPT, user_prompt)
        except ProviderError as exc:
            raise DirectorError("director_provider_failed") from exc
        except DirectorError:
            raise
        except Exception as exc:
            raise DirectorError("director_provider_failed") from exc
        try:
            content = _response_content(result)
            return _decode_and_validate(content, safe_context)
        except (ValueError, DirectorError) as exc:
            if isinstance(exc, DirectorError) and exc.code == "director_provider_failed":
                raise
            if attempt == repair_limit:
                raise DirectorError("director_schema_invalid") from exc
            error = exc if isinstance(exc, ValueError) else ValueError(exc.code)
            user_prompt = _repair_prompt(error, content)
    raise DirectorError("director_schema_invalid")
