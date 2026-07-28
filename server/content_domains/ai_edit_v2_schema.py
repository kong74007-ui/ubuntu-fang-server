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
            all(isinstance(slot, str) and bool(slot.strip()) for slot in material_slots),
            f"{path}.material_slots只能包含非空槽位ID",
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
    return plan
