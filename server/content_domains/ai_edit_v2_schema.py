"""Pure contracts and validation for the isolated AI editing V2 domain."""

from __future__ import annotations

import re
from typing import Any, Final, TypedDict


EDIT_PLAN_VERSION: Final = "2.0"
CREATION_MODES: Final = frozenset(
    {"natural_brief", "platform_template", "open_generation"}
)
ASPECT_RATIOS: Final = frozenset({"9:16", "16:9"})
MATERIAL_PURPOSES: Final = frozenset({"primary", "required", "reference"})
REFERENCE_MODES: Final = frozenset({"direct_use", "style_only"})
SCENE_LAYOUTS: Final = frozenset(
    {"speaker_focus", "speaker_product_split", "full_bleed", "split_screen", "data_card"}
)
SCENE_VISUAL_TYPES: Final = frozenset(
    {"talking_head", "product_hook", "b_roll", "text_card", "data_visualization"}
)
SCENE_TRANSITIONS: Final = frozenset({"cut", "dissolve", "fade", "wipe"})
COMPONENT_FAMILIES: Final = frozenset(
    {"editorial_business", "documentary_modern", "energetic_social"}
)
CAPTION_STYLES: Final = frozenset({"clean", "word_highlight", "karaoke"})
SPEECH_POLICIES: Final = frozenset({"preserve_source"})
MUSIC_POLICIES: Final = frozenset({"none", "duck_under_speech"})
SFX_POLICIES: Final = frozenset({"none", "semantic_only"})
STABLE_RENDER_COMPONENTS: Final = frozenset(
    {
        "basic_caption",
        "basic_card",
        "broll_image",
        "broll_video",
        "standard_transition",
        "audio_bed",
    }
)
BUNDLED_NOTO_SANS_SC_URL: Final = (
    "https://shotstack-assets.s3-ap-southeast-2.amazonaws.com/"
    "fonts/NotoSansSC-Regular.otf"
)
MAX_MODEL_STRING_LENGTH: Final = 500
MATERIAL_SLOT_ID_RE: Final = re.compile(r"^slot_[a-z0-9][a-z0-9_-]{0,63}$")
_HOST_LABEL_PATTERN: Final = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
_HOST_SEMANTIC_LABEL_PATTERN: Final = r"(?:api|auth|cdn|host|mail|server|static)"
FORBIDDEN_MODEL_VALUE_PATTERNS: Final = (
    re.compile(
        r"(?i)(?:https?|ftp|file|javascript|cos|s3)://|\bwww\.|"
        r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)"
        r"(?::\d{1,5})?(?:/\S*)?"
    ),
    re.compile(
        rf"(?<![\w@-])(?:(?:{_HOST_LABEL_PATTERN}\.){{2,}}[A-Za-z]{{2,63}}|"
        rf"{_HOST_SEMANTIC_LABEL_PATTERN}\.[A-Za-z]{{2,63}})(?![A-Za-z0-9-])"
        r"(?::\d{1,5})?(?:/\S*)?|"
        rf"(?<![\w@-])(?:{_HOST_LABEL_PATTERN}\.)+[A-Za-z]{{2,63}}"
        r"(?![A-Za-z0-9-])(?::\d{1,5}(?:/\S*)?|/\S*)",
        re.IGNORECASE,
    ),
    re.compile(r"(?is)<\s*/?\s*[a-z][^>]*>"),
    re.compile(
        r"(?is)\b(?:select\s+.+?\s+from|insert\s+into|update\s+\w+\s+set|delete\s+from|"
        r"drop\s+(?:table|database)|alter\s+table|create\s+(?:table|database)|truncate\s+table)\b"
    ),
    re.compile(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|secret|password|credential|provider|"
        r"render(?:er)?(?:[_-](?:engine|id|url|path))?|cos(?:[_-](?:key|path|url))?|"
        r"database[_-](?:url|dsn|password|query|table)|db[_-](?:url|dsn|password|query|table)|"
        r"code[_-](?:payload|script))\s*[:=]"
    ),
    re.compile(r"(?i)\b(?:shotstack|dashscope|remotion)\b"),
    re.compile(
        r"(?i)(?:数据库(?:地址|连接串|密码|查询|表名)|COS(?:地址|路径|密钥)|"
        r"渲染(?:器|引擎)|(?:JavaScript|JS)?(?:代码|脚本))\s*[:：=]"
    ),
    re.compile(
        r"(?i)\b(?:provider(?:_[a-z0-9]+)+|render(?:_[a-z0-9]+)*|"
        r"db(?:_[a-z0-9]+)+|database(?:_[a-z0-9]+)+)\s*="
    ),
    re.compile(
        r"(?i)(?:```|\b(?:eval|function|fetch)\s*\(|\bdocument\.(?:cookie|write)|"
        r"\bwindow\.|\bos\.system\s*\(|\bsubprocess\.|\bconst\s+[a-z_$][\w$]*\s*=|"
        r"\b(?:from\s+[a-z_][\w.]*\s+)?import\s+[a-z_][\w.]*|\bprint\s*\()"
    ),
)

MAX_MATERIALS_PER_WINDOW: Final = 10
MAX_SOURCE_DURATION_MS: Final = 10 * 60 * 1000
MAX_MAIN_VIDEO_BYTES: Final = 500 * 1024 * 1024
MAX_SUPPLEMENTARY_VIDEO_BYTES: Final = 200 * 1024 * 1024
MAX_IMAGE_BYTES: Final = 15 * 1024 * 1024
MAX_AUDIO_BYTES: Final = 50 * 1024 * 1024
MAX_JOB_UPLOAD_BYTES: Final = 1024 * 1024 * 1024

NORMAL_STATES: Final = (
    "created",
    "validating",
    "quoting",
    "precharging",
    "queued",
    "normalizing",
    "transcribing",
    "aligning_transcript",
    "directing",
    "resolving_assets",
    "generating_assets",
    "designing_audio",
    "routing_render",
    "rendering",
    "assembling",
    "quality_check",
    "repairing",
    "settling",
    "storing",
    "completed",
)
FAILURE_STATES: Final = frozenset(
    {
        "validation_failed",
        "transcription_failed",
        "director_failed",
        "asset_failed",
        "render_failed",
        "quality_failed",
        "settlement_failed",
        "storage_failed",
    }
)
TERMINAL_STATES: Final = FAILURE_STATES | {"completed"}
STATE_TRANSITIONS: Final = {
    state: frozenset({NORMAL_STATES[index + 1]})
    for index, state in enumerate(NORMAL_STATES[:-1])
}
STATE_TRANSITIONS["quality_check"] = frozenset({"repairing", "settling"})
STATE_TRANSITIONS["repairing"] = frozenset({"settling", "quality_failed"})

FORBIDDEN_PLAN_KEYS: Final = frozenset(
    {
        "url",
        "cos_key",
        "provider",
        "api_key",
        "html",
        "code",
        "tracks",
        "shotstack",
        "subtitle_text",
        "transcript",
    }
)
FORBIDDEN_PLAN_KEY_TOKENS: Final = frozenset(
    {"url", "cos", "shotstack", "provider", "code", "html", "tracks", "render", "renderer"}
)
EDIT_PLAN_FIELDS: Final = frozenset(
    {
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
    }
)


class MaterialInput(TypedDict, total=False):
    asset_id: str
    kind: str
    size_bytes: int
    duration_ms: int
    reference_mode: str


class JobDraft(TypedDict, total=False):
    creation_mode: str
    brief: str
    language: str
    aspect_ratio: str
    target_duration_ms: int | None
    input_mode: str
    original_text: str
    main_input: MaterialInput
    required_materials: list[MaterialInput]
    reference_materials: list[MaterialInput]


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _positive_int(value: Any, field_name: str) -> int:
    _require(isinstance(value, int) and not isinstance(value, bool) and value > 0,
             f"{field_name}必须是正整数")
    return value


def _nonnegative_int(value: Any, field_name: str) -> int:
    _require(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0,
        f"{field_name}必须是非负整数",
    )
    return value


def _nonempty_string(value: Any, field_name: str) -> str:
    _require(isinstance(value, str) and bool(value.strip()), f"{field_name}不能为空")
    return value


def _validate_material(
    material: Any,
    *,
    purpose: str,
    is_main: bool = False,
) -> int:
    _require(isinstance(material, dict), "素材必须是对象")
    _require(isinstance(material.get("asset_id"), str) and material["asset_id"].strip(),
             "素材asset_id不能为空")
    kind = material.get("kind")
    _require(kind in {"video", "image", "audio"}, "素材类型不受支持")
    if is_main:
        _require(kind in {"video", "audio"}, "主输入只能是视频或音频")

    size_bytes = _positive_int(material.get("size_bytes"), "素材大小")
    if kind == "video":
        maximum = MAX_MAIN_VIDEO_BYTES if is_main else MAX_SUPPLEMENTARY_VIDEO_BYTES
        _require(size_bytes <= maximum,
                 "主视频最大500MB" if is_main else "补充视频单个最大200MB")
    elif kind == "image":
        _require(size_bytes <= MAX_IMAGE_BYTES, "图片单张最大15MB")
    else:
        _require(size_bytes <= MAX_AUDIO_BYTES, "音频单个最大50MB")

    if is_main:
        duration_ms = _positive_int(material.get("duration_ms"), "主输入时长")
        _require(duration_ms <= MAX_SOURCE_DURATION_MS, "主视频或主音频最长10分钟")

    if purpose == "reference":
        _require(material.get("reference_mode") in REFERENCE_MODES,
                 "参考模式必须是可直接使用或仅作风格参考")
    return size_bytes


def validate_job_draft(draft: dict[str, Any]) -> dict[str, Any]:
    """Validate user-controlled draft metadata without mutating it."""

    _require(isinstance(draft, dict), "任务草稿必须是对象")
    _require(draft.get("creation_mode") in CREATION_MODES, "创作入口不受支持")
    _require(draft.get("aspect_ratio") in ASPECT_RATIOS, "画面比例不受支持")
    _require(draft.get("language") == "zh-CN", "第一阶段只支持中文")
    input_mode = draft.get("input_mode")
    _require(input_mode in {"platform_video", "external_video", "audio_only"},
             "input_mode unsupported")
    if input_mode == "platform_video":
        _nonempty_string(draft.get("original_text"), "original_text")
    if draft.get("creation_mode") == "platform_template":
        _nonempty_string(draft.get("template_id"), "template_id")
        _nonempty_string(draft.get("template_version"), "template_version")
        from .ai_edit_v2_templates import get_published_template
        try:
            get_published_template(draft["template_id"], draft["template_version"])
        except Exception as exc:
            raise ValueError("template_not_published") from exc
    target_duration_ms = draft.get("target_duration_ms")
    if target_duration_ms is not None:
        _positive_int(target_duration_ms, "目标时长")

    required = draft.get("required_materials", [])
    references = draft.get("reference_materials", [])
    _require(isinstance(required, list), "必须使用素材必须是数组")
    _require(isinstance(references, list), "参考使用素材必须是数组")
    _require(
        len(required) <= MAX_MATERIALS_PER_WINDOW,
        "必须使用素材最多10个",
    )
    _require(
        len(references) <= MAX_MATERIALS_PER_WINDOW,
        "参考使用素材最多10个",
    )

    total_bytes = _validate_material(
        draft.get("main_input"), purpose="primary", is_main=True
    )
    main_kind = draft["main_input"].get("kind")
    if input_mode == "audio_only":
        _require(main_kind == "audio", "audio_only requires audio input")
    elif input_mode in {"platform_video", "external_video"}:
        _require(main_kind == "video", "video input_mode requires video input")
    total_bytes += sum(
        _validate_material(material, purpose="required") for material in required
    )
    total_bytes += sum(
        _validate_material(material, purpose="reference") for material in references
    )
    _require(total_bytes <= MAX_JOB_UPLOAD_BYTES, "单任务全部上传文件合计最大1GB")
    return draft


def _reject_forbidden_plan_keys(value: Any, path: str = "plan") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key)).lower()
            key_tokens = {token for token in re.split(r"[^a-z0-9]+", normalized) if token}
            if (
                normalized in FORBIDDEN_PLAN_KEYS
                or key_tokens.intersection(FORBIDDEN_PLAN_KEY_TOKENS)
                or "shotstack" in normalized
                or "provider" in normalized
            ):
                raise ValueError(f"中间协议禁止字段: {path}.{key}")
            _reject_forbidden_plan_keys(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_forbidden_plan_keys(child, f"{path}[{index}]")


def _reject_forbidden_plan_values(value: Any, path: str = "plan") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            _reject_forbidden_plan_values(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_forbidden_plan_values(child, f"{path}[{index}]")
    elif isinstance(value, str):
        _require(len(value) <= MAX_MODEL_STRING_LENGTH, f"模型字符串过长: {path}")
        _require(
            not any(pattern.search(value) for pattern in FORBIDDEN_MODEL_VALUE_PATTERNS),
            f"模型字符串包含禁止的地址、脚本、数据库或供应商模式: {path}",
        )


def _validate_scenes(scenes: Any, duration_ms: int) -> None:
    _require(isinstance(scenes, list) and bool(scenes), "剪辑方案scenes不能为空")
    previous_end = 0
    scene_ids: set[str] = set()
    for index, scene in enumerate(scenes):
        path = f"scenes[{index}]"
        _require(isinstance(scene, dict), f"{path}必须是对象")
        _require(
            set(scene)
            == {
                "id",
                "start_ms",
                "end_ms",
                "intent",
                "layout",
                "visual_type",
                "headline",
                "material_slots",
                "transition",
            },
            f"{path}只允许已发布的稳定字段",
        )
        scene_id = _nonempty_string(scene.get("id"), f"{path}.id")
        _require(scene_id not in scene_ids, f"{path}.id不能重复")
        scene_ids.add(scene_id)
        start_ms = _nonnegative_int(scene.get("start_ms"), f"{path}.start_ms")
        end_ms = _positive_int(scene.get("end_ms"), f"{path}.end_ms")
        _require(end_ms > start_ms, f"{path}时长必须为正")
        _require(start_ms == previous_end, f"{path}必须与前一场景连续且不得重叠")
        _require(end_ms <= duration_ms, f"{path}不得超过剪辑方案时长")
        previous_end = end_ms

        _nonempty_string(scene.get("intent"), f"{path}.intent")
        _nonempty_string(scene.get("headline"), f"{path}.headline")
        _require(scene.get("layout") in SCENE_LAYOUTS, f"{path}.layout不受支持")
        _require(
            scene.get("visual_type") in SCENE_VISUAL_TYPES,
            f"{path}.visual_type不受支持",
        )
        _require(
            scene.get("transition") in SCENE_TRANSITIONS,
            f"{path}.transition不受支持",
        )
        material_slots = scene.get("material_slots")
        _require(isinstance(material_slots, list), f"{path}.material_slots必须是数组")
        _require(
            all(isinstance(slot, str) and MATERIAL_SLOT_ID_RE.fullmatch(slot) for slot in material_slots),
            f"{path}.material_slots只能包含严格格式的槽位ID",
        )
        _require(len(material_slots) == len(set(material_slots)), f"{path}.material_slots不能重复")
    _require(previous_end == duration_ms, "场景总时长必须等于剪辑方案时长")


def _validate_caption_plan(caption_plan: Any) -> None:
    _require(isinstance(caption_plan, dict), "caption_plan必须是对象")
    text_fields = {"text", "body", "content", "lines", "segments"}
    _require(not text_fields.intersection(caption_plan), "caption_plan不得包含字幕正文")
    _require(
        set(caption_plan).issubset({"source", "style"}),
        "caption_plan只允许稳定字幕配置",
    )
    _require(caption_plan.get("source") == "text_timeline", "字幕必须引用text_timeline")
    _require(caption_plan.get("style") in CAPTION_STYLES, "caption_plan.style不受支持")


def _validate_audio_plan(audio_plan: Any) -> None:
    _require(isinstance(audio_plan, dict), "audio_plan必须是对象")
    _require(
        set(audio_plan) == {"speech_policy", "music_policy", "sfx_policy"},
        "audio_plan字段不完整",
    )
    _require(
        audio_plan.get("speech_policy") in SPEECH_POLICIES,
        "audio_plan.speech_policy不受支持",
    )
    _require(
        audio_plan.get("music_policy") in MUSIC_POLICIES,
        "audio_plan.music_policy不受支持",
    )
    _require(
        audio_plan.get("sfx_policy") in SFX_POLICIES,
        "audio_plan.sfx_policy不受支持",
    )


def validate_edit_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """Validate the provider-neutral edit-plan boundary."""

    _require(isinstance(plan, dict), "剪辑方案必须是对象")
    _require(plan.get("version") == EDIT_PLAN_VERSION, "剪辑方案版本必须为2.0")
    _require(plan.get("creation_mode") in CREATION_MODES, "剪辑方案创作入口不受支持")
    _require(plan.get("aspect_ratio") in ASPECT_RATIOS, "剪辑方案比例不受支持")
    _require(plan.get("language") == "zh-CN", "剪辑方案语言必须是中文")
    duration_ms = _positive_int(plan.get("duration_ms"), "剪辑方案时长")
    target_duration_ms = plan.get("target_duration_ms")
    if target_duration_ms is not None:
        _positive_int(target_duration_ms, "剪辑方案目标时长")
    _reject_forbidden_plan_keys(plan)
    _require(set(plan) == EDIT_PLAN_FIELDS, "剪辑方案只允许语义导演字段")
    style_system = plan.get("style_system")
    _require(isinstance(style_system, dict), "style_system必须是对象")
    _require(
        set(style_system)
        in (
            set(),
            {"component_family"},
            {"template_id", "template_version", "component_family"},
        ),
        "style_system只允许稳定组件族或已发布模板引用",
    )
    if "component_family" in style_system:
        _require(
            style_system["component_family"] in COMPONENT_FAMILIES,
            "style_system.component_family不受支持",
        )
    if "template_id" in style_system:
        _nonempty_string(style_system["template_id"], "style_system.template_id")
        _nonempty_string(style_system["template_version"], "style_system.template_version")
    _validate_scenes(plan.get("scenes"), duration_ms)
    _validate_caption_plan(plan.get("caption_plan"))
    _validate_audio_plan(plan.get("audio_plan"))
    _reject_forbidden_plan_values(plan)
    return plan


def validate_render_graph(graph: dict[str, Any]) -> dict[str, Any]:
    """Validate the audited stable-render boundary before provider submission."""

    _require(isinstance(graph, dict), "render_graph必须是对象")
    _require(
        set(graph) == {"version", "aspect_ratio", "duration_ms", "components", "output"},
        "render_graph只允许稳定字段",
    )
    _require(graph.get("version") == "1.0", "render_graph版本不受支持")
    _require(graph.get("aspect_ratio") in ASPECT_RATIOS, "render_graph比例不受支持")
    duration_ms = _positive_int(graph.get("duration_ms"), "render_graph时长")
    components = graph.get("components")
    _require(isinstance(components, list) and bool(components), "render_graph组件不能为空")
    allowed_fields = {
        "basic_caption": {"type", "text", "start", "length", "font_url"},
        "basic_card": {"type", "text", "start", "length"},
        "broll_image": {"type", "src", "start", "length"},
        "broll_video": {"type", "src", "start", "length"},
        "standard_transition": {"type", "name", "start", "length"},
        "audio_bed": {"type", "src", "start", "length"},
    }
    for index, component in enumerate(components):
        path = f"render_graph.components[{index}]"
        _require(isinstance(component, dict), f"{path}必须是对象")
        kind = component.get("type")
        _require(kind in STABLE_RENDER_COMPONENTS, f"{path}.type不受支持")
        _require(set(component) == allowed_fields[kind], f"{path}字段不受支持")
        start = component.get("start")
        length = component.get("length")
        _require(
            isinstance(start, (int, float)) and not isinstance(start, bool) and start >= 0,
            f"{path}.start无效",
        )
        _require(
            isinstance(length, (int, float)) and not isinstance(length, bool) and length > 0,
            f"{path}.length无效",
        )
        _require((start + length) * 1000 <= duration_ms + 1, f"{path}超出时长")
        if kind in {"basic_caption", "basic_card"}:
            _nonempty_string(component.get("text"), f"{path}.text")
        if kind in {"broll_image", "broll_video", "audio_bed"}:
            src = _nonempty_string(component.get("src"), f"{path}.src")
            _require(src.startswith(("http://", "https://")), f"{path}.src必须是短期地址")
        if kind == "basic_caption":
            font_url = _nonempty_string(component.get("font_url"), f"{path}.font_url")
            _require(
                font_url == BUNDLED_NOTO_SANS_SC_URL,
                f"{path}.font_url必须使用Noto Sans SC",
            )
        if kind == "standard_transition":
            _require(component.get("name") in SCENE_TRANSITIONS, f"{path}.name不受支持")
    _require(
        sum(component["type"] == "audio_bed" for component in components) <= 1,
        "render_graph只允许一个mastered audio_bed",
    )
    output = graph.get("output")
    _require(
        output == {"format": "mp4", "resolution": "1080p", "video_codec": "h264", "audio_codec": "aac"},
        "render_graph输出规格不受支持",
    )
    return graph
