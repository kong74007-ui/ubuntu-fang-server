"""Constrained Qwen director producing only provider-neutral edit plans."""

from __future__ import annotations

import json
import re
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
不得输出数据库字段、SQL、JavaScript/JS、脚本或任何可执行代码片段。
不得输出渲染指令、素材地址或具体画面坐标。
每个 scene 必须且只能使用已发布的稳定组件语义，并包含 id、start_ms、end_ms、intent、layout、visual_type、headline、material_slots、transition。
layout 可选：{layouts}。
visual_type 可选：{visual_types}。
transition 可选：{transitions}。
component_family 可选：{families}。
caption style 可选：{caption_styles}。
audio policy：speech_policy={speech}; music_policy 可选 {music}; sfx_policy 可选 {sfx}。
layout、visual_type 与 material_slots 必须语义一致。
speaker_focus 不得创建 material_slots；只有需要产品、门店、图表或 B-roll 补充画面时才创建素材槽位。
同一槽位 ID 不得跨语义不同的场景复用；不同语义的场景必须使用不同槽位 ID。
内容适合时改变连续场景的 layout，避免机械重复；内容不适合变化时优先保持稳定和人物主体清晰。
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
_MAX_REPAIR_RESPONSE_CHARS: Final = 8_000
_MAX_REPAIR_ERROR_CHARS: Final = 1_000
_OUTPUT_CONTRACT: Final = {
    "top_level_fields": [
        "version",
        "creation_mode",
        "duration_ms",
        "target_duration_ms",
        "aspect_ratio",
        "language",
        "style_system",
        "scenes",
        "caption_plan",
        "audio_plan",
    ],
    "scene_fields": [
        "id",
        "start_ms",
        "end_ms",
        "intent",
        "layout",
        "visual_type",
        "headline",
        "material_slots",
        "transition",
    ],
    "caption_plan_fields": ["source", "style"],
    "audio_plan_fields": ["speech_policy", "music_policy", "sfx_policy"],
    "field_types": {
        "version": "string",
        "creation_mode": "string",
        "duration_ms": "positive integer",
        "target_duration_ms": "positive integer",
        "aspect_ratio": "string",
        "language": "string",
        "style_system": "object",
        "scenes": "non-empty array",
        "caption_plan": "object",
        "audio_plan": "object",
        "scene.id": "non-empty string",
        "scene.start_ms": "non-negative integer",
        "scene.end_ms": "positive integer",
        "scene.material_slots": "array of slot IDs",
    },
    "rules": {
        "version": "2.0",
        "language": "zh-CN",
        "duration": "duration_ms and target_duration_ms must equal context.target_duration_ms",
        "scenes": "start at 0, remain contiguous without overlap, and end at duration_ms",
        "style_system": "use only component_family unless context contains a published template",
        "caption_source": "text_timeline",
        "material_slot_id": "slot_ followed by lowercase letters, digits, underscores, or hyphens",
        "scene_semantics": {
            "speaker_focus": "talking_head with no material slots",
            "speaker_product_split": "product_hook or b_roll with material slots",
            "full_bleed": "product_hook or b_roll with material slots",
            "split_screen": "product_hook or b_roll with material slots",
            "data_card": "text_card with no slots, or data_visualization with material slots",
            "slot_reuse": "a slot ID may appear in only one scene",
        },
    },
}
_SENSITIVE_KEY_RE: Final = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?(?:key|token)|token|secret|password|credential|authorization|cookie)"
)
_SENSITIVE_ASSIGNMENT_RE: Final = re.compile(
    r"(?i)[\"']?(?:api[_-]?key|access[_-]?(?:key|token)|token|secret|password|credential|"
    r"authorization|cookie)[\"']?\s*[:=]\s*(?:bearer\s+)?"
    r"[\"']?[^\s,;)}\]]+"
)
_BEARER_RE: Final = re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]{8,}")
_SECRET_TOKEN_RE: Final = re.compile(
    r"(?i)\b(?:"
    r"eyJ[a-z0-9_-]*\.[a-z0-9_-]+\.[a-z0-9_-]+|"
    r"(?:AKIA|ASIA)[A-Z0-9]{16}|"
    r"LTAI[A-Z0-9]{12,30}|"
    r"(?:gh[pousr]_[a-z0-9]{20,}|github_pat_[a-z0-9_]{20,})|"
    r"xox[baprs]-[a-z0-9-]{16,}|"
    r"(?:sk|ak)-[a-z0-9_-]{8,}|"
    r"(?:api[_-]?)?(?:key|token|secret)[_-][a-z0-9_-]{12,}"
    r")\b"
)


class DirectorError(RuntimeError):
    def __init__(self, code: str, detail: str | None = None):
        self.code = code
        self.detail = detail
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
    template_version = context.get("template_version")
    style_text = context.get("style_text")
    if creation_mode == "platform_template":
        if (
            not isinstance(template_id, str)
            or not template_id.strip()
            or not isinstance(template_version, str)
            or not template_version.strip()
            or style_text is not None
        ):
            raise DirectorError("director_context_invalid")
        try:
            safe["template"] = get_published_template(template_id, template_version)
        except TemplateError as exc:
            raise DirectorError("director_context_invalid") from exc
    elif (
        isinstance(style_text, str)
        and style_text.strip()
        and template_id is None
        and template_version is None
    ):
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


def _normalize_structural_fields(plan: Any) -> Any:
    """Repair only deterministic identifiers and wrapper shapes, never semantics."""

    if not isinstance(plan, dict):
        return plan
    normalized = dict(plan)
    style_system = normalized.get("style_system")
    if isinstance(style_system, str) and style_system in COMPONENT_FAMILIES:
        normalized["style_system"] = {"component_family": style_system}
    scenes = normalized.get("scenes")
    if isinstance(scenes, list):
        normalized_scenes = []
        for index, scene in enumerate(scenes):
            if not isinstance(scene, dict):
                normalized_scenes.append(scene)
                continue
            normalized_scene = dict(scene)
            scene_id = normalized_scene.get("id")
            if scene_id is None or (isinstance(scene_id, str) and not scene_id.strip()):
                normalized_scene["id"] = f"scene_{index + 1:02d}"
            normalized_scenes.append(normalized_scene)
        normalized["scenes"] = normalized_scenes
    return normalized


def _decode_and_validate(content: str, safe_context: dict[str, Any]) -> dict[str, Any]:
    try:
        plan = json.loads(content)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("响应不是合法JSON对象") from exc
    plan = _normalize_structural_fields(plan)
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
            "output_contract": _OUTPUT_CONTRACT,
            "output_example": _output_example(safe_context),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _output_example(safe_context: dict[str, Any]) -> dict[str, Any]:
    duration_ms = safe_context["target_duration_ms"]
    template = safe_context.get("template")
    if template is None:
        style_system = {"component_family": "editorial_business"}
        music_policy = "duck_under_speech"
        sfx_policy = "semantic_only"
    else:
        style_system = {
            "template_id": template["id"],
            "template_version": template["version"],
            "component_family": template["component_family"],
        }
        music_policy = template["sound_policy"]["music_policy"]
        sfx_policy = template["sound_policy"]["sfx_policy"]
    return {
        "version": "2.0",
        "creation_mode": safe_context["creation_mode"],
        "duration_ms": duration_ms,
        "target_duration_ms": duration_ms,
        "aspect_ratio": safe_context["aspect_ratio"],
        "language": "zh-CN",
        "style_system": style_system,
        "scenes": [{
            "id": "scene_01",
            "start_ms": 0,
            "end_ms": duration_ms,
            "intent": "概括本场景的表达目的",
            "layout": "speaker_focus",
            "visual_type": "talking_head",
            "headline": "非空中文重点标题",
            "material_slots": [],
            "transition": "cut",
        }],
        "caption_plan": {"source": "text_timeline", "style": "clean"},
        "audio_plan": {
            "speech_policy": "preserve_source",
            "music_policy": music_policy,
            "sfx_policy": sfx_policy,
        },
    }


def _redact_sensitive_json(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, child in value.items():
            if _SENSITIVE_KEY_RE.search(str(key)):
                redacted["[REDACTED_KEY]"] = "[REDACTED]"
            else:
                redacted[str(key)] = _redact_sensitive_json(child)
        return redacted
    if isinstance(value, list):
        return [_redact_sensitive_json(child) for child in value]
    return value


def _redact_sensitive_text(value: str) -> str:
    redacted = _SENSITIVE_ASSIGNMENT_RE.sub("[REDACTED_CREDENTIAL]", value)
    redacted = _BEARER_RE.sub("Bearer [REDACTED]", redacted)
    return _SECRET_TOKEN_RE.sub("[REDACTED]", redacted)


def _sanitize_previous_response(previous_response: str) -> str:
    try:
        parsed = json.loads(previous_response)
    except (json.JSONDecodeError, TypeError):
        sanitized = _redact_sensitive_text(previous_response)
    else:
        sanitized = json.dumps(
            _redact_sensitive_json(parsed),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        sanitized = _redact_sensitive_text(sanitized)
    return sanitized[:_MAX_REPAIR_RESPONSE_CHARS]


def _repair_prompt(error: ValueError, previous_response: str, original_request: str) -> str:
    return json.dumps(
        {
            "task": "repair_semantic_edit_plan_v2",
            "instruction": "只修复 schema_errors 指向的字段，其他已经合规的字段和值必须原样保留；返回完整 JSON 对象，所有字符串必填字段不得为空。",
            "original_request": json.loads(original_request),
            "schema_errors": _redact_sensitive_text(str(error))[:_MAX_REPAIR_ERROR_CHARS],
            "previous_response": _sanitize_previous_response(previous_response),
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
    original_request = _initial_prompt(safe_context)
    user_prompt = original_request
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
                detail = _redact_sensitive_text(str(exc))[:_MAX_REPAIR_ERROR_CHARS]
                raise DirectorError("director_schema_invalid", detail) from exc
            error = exc if isinstance(exc, ValueError) else ValueError(exc.code)
            user_prompt = _repair_prompt(error, content, original_request)
    raise DirectorError("director_schema_invalid")
