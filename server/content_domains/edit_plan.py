# -*- coding: utf-8 -*-
"""AI 剪辑提交参数与 edit-plan v1 白名单校验。"""
import copy


STYLE_IDS = {"knowledge_dynamic", "product_story", "story_broll"}
RATIOS = {
    "9:16": (1080, 1920),
    "16:9": (1920, 1080),
    "1:1": (1080, 1080),
}
MAX_DURATION_MS = 10 * 60 * 1000
MAX_SEGMENTS = 120
MAX_CAPTIONS = 600
MAX_OVERLAYS = 80
MAX_BROLL = 80
FORBIDDEN_TEXT = ("<script", "javascript:", "data:text/html")
SOURCE_KEYS = (
    "source_video_asset_id",
    "source_audio_asset_id",
    "source_upload_id",
)


def _positive_int(value, label):
    if isinstance(value, bool):
        raise ValueError("%s必须是正整数" % label)
    try:
        cleaned = int(value)
    except (TypeError, ValueError):
        raise ValueError("%s必须是正整数" % label)
    if cleaned <= 0:
        raise ValueError("%s必须是正整数" % label)
    return cleaned


def validate_submit_payload(body):
    if not isinstance(body, dict):
        raise ValueError("提交参数格式错误")
    selected = [key for key in SOURCE_KEYS if body.get(key) not in (None, "")]
    if not selected:
        raise ValueError("请选择一个素材来源")
    if len(selected) != 1:
        raise ValueError("素材来源只能选择一个")

    style = str(body.get("style") or "")
    if style not in STYLE_IDS:
        raise ValueError("不支持的剪辑风格")
    ratio = str(body.get("ratio") or "9:16")
    if ratio not in RATIOS:
        raise ValueError("不支持的画面比例")

    source_key = selected[0]
    source_value = body[source_key]
    if source_key in {"source_video_asset_id", "source_audio_asset_id"}:
        source_value = _positive_int(source_value, "素材ID")
    else:
        source_value = str(source_value).strip()
        if not source_value or len(source_value) > 128:
            raise ValueError("上传素材ID无效")
    if source_key == "source_audio_asset_id" and style != "story_broll":
        raise ValueError("纯音频素材只支持故事画面风格")

    cleaned = {
        source_key: source_value,
        "style": style,
        "ratio": ratio,
        "captions": bool(body.get("captions", True)),
        "auto_assets": bool(body.get("auto_assets", True)),
    }
    if source_key == "source_audio_asset_id":
        cleaned["auto_assets"] = True
    material_ids = body.get("material_ids")
    if material_ids is not None:
        if not isinstance(material_ids, list) or len(material_ids) > 20:
            raise ValueError("附加素材数量无效")
        cleaned["material_ids"] = [str(item) for item in material_ids if str(item)]
    return cleaned


def _check_text(value):
    if value is None:
        return
    if not isinstance(value, str):
        raise ValueError("文本字段格式错误")
    lowered = value.lower()
    if any(marker in lowered for marker in FORBIDDEN_TEXT):
        raise ValueError("文本包含非法内容")


def _check_interval(item, source_duration_ms, label):
    if not isinstance(item, dict):
        raise ValueError("%s格式错误" % label)
    start = item.get("start_ms")
    end = item.get("end_ms")
    if isinstance(start, bool) or isinstance(end, bool):
        raise ValueError("%s时间格式错误" % label)
    if not isinstance(start, int) or not isinstance(end, int):
        raise ValueError("%s时间格式错误" % label)
    if not 0 <= start < end <= source_duration_ms:
        raise ValueError("%s超出源视频时长" % label)
    source_start = item.get("source_start_ms")
    source_end = item.get("source_end_ms")
    if source_start is not None or source_end is not None:
        if not isinstance(source_start, int) or not isinstance(source_end, int):
            raise ValueError("%s源片段时间格式错误" % label)
        if not 0 <= source_start < source_end <= source_duration_ms:
            raise ValueError("%s超出源视频时长" % label)


def _checked_collection(plan, name, limit, source_duration_ms, label):
    items = plan.get(name, [])
    if not isinstance(items, list):
        raise ValueError("%s必须是数组" % label)
    if len(items) > limit:
        raise ValueError("%s数量超过限制" % label)
    for item in items:
        _check_interval(item, source_duration_ms, label)
        _check_text(item.get("text"))
    return items


def validate_edit_plan(plan, source_duration_ms, allowed_assets):
    if not isinstance(plan, dict):
        raise ValueError("剪辑方案必须是JSON对象")
    source_duration_ms = _positive_int(source_duration_ms, "源视频时长")
    if source_duration_ms > MAX_DURATION_MS:
        raise ValueError("源视频最长支持10分钟")
    if str(plan.get("version") or "") != "1.0":
        raise ValueError("edit-plan版本必须为1.0")
    ratio = str(plan.get("ratio") or "9:16")
    if ratio not in RATIOS:
        raise ValueError("不支持的画面比例")

    cleaned = copy.deepcopy(plan)
    segments = _checked_collection(
        cleaned, "segments", MAX_SEGMENTS, source_duration_ms, "片段"
    )
    if not segments:
        raise ValueError("剪辑方案至少需要一个片段")
    _checked_collection(
        cleaned, "captions", MAX_CAPTIONS, source_duration_ms, "字幕"
    )
    _checked_collection(
        cleaned, "overlays", MAX_OVERLAYS, source_duration_ms, "叠加元素"
    )
    broll = _checked_collection(
        cleaned, "broll", MAX_BROLL, source_duration_ms, "补充素材"
    )
    allowed_assets = {str(item) for item in (allowed_assets or set())}
    for item in broll:
        asset_id = str(item.get("asset_id") or "")
        if not asset_id or asset_id not in allowed_assets:
            raise ValueError("剪辑方案引用了未授权素材")

    width, height = RATIOS[ratio]
    cleaned["ratio"] = ratio
    cleaned["output"] = {"width": width, "height": height}
    return cleaned
