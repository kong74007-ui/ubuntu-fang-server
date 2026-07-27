# -*- coding: utf-8 -*-
"""AI 剪辑素材解析、缺图生成和逐字时间戳字幕。"""
import mimetypes
import os
import re
import uuid

from . import ai_edit_store, cos


MAX_GENERATED_IMAGES = 8
MAX_GENERATED_IMAGE_BYTES = 25 * 1024 * 1024
IMAGE_PROVIDER = os.environ.get("AI_EDIT_IMAGE_PROVIDER", "openai").strip() or "openai"


def _milliseconds(value):
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _vtt_time(milliseconds):
    milliseconds = _milliseconds(milliseconds)
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return "%02d:%02d:%02d.%03d" % (hours, minutes, seconds, millis)


def _safe_vtt_text(value):
    return str(value or "").replace("-->", "→").replace("\r", " ").replace("\n", " ")


def words_to_vtt(words):
    cues = []
    current = []
    cue_start = None
    cue_end = None

    def flush():
        nonlocal current, cue_start, cue_end
        if current and cue_start is not None and cue_end is not None:
            cues.append((cue_start, cue_end, "".join(current)))
        current = []
        cue_start = None
        cue_end = None

    for item in words or []:
        if not isinstance(item, dict):
            continue
        text = _safe_vtt_text(item.get("text"))
        start = _milliseconds(item.get("begin_time"))
        end = _milliseconds(item.get("end_time"))
        if not text or end <= start:
            continue
        would_text = "".join(current) + text
        would_duration = end - (cue_start if cue_start is not None else start)
        if current and (len(would_text) > 14 or would_duration > 2500):
            flush()
        if cue_start is None:
            cue_start = start
        cue_end = end
        current.append(text)
    flush()

    lines = ["WEBVTT", ""]
    for index, (start, end, text) in enumerate(cues, 1):
        lines.extend(
            [
                str(index),
                "%s --> %s" % (_vtt_time(start), _vtt_time(end)),
                text,
                "",
            ]
        )
    return "\n".join(lines)


def _key_component(value):
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", str(value or "").strip())
    return cleaned.strip("_")[:80] or "user"


def _generate_image(prompt, job_id, username, role):
    """调用现有生图能力，并把结果复制到 AI 剪辑专属私有 COS 键。"""
    from . import image
    from .core import _out_path

    result = image.HANDLERS["image"](
        {
            "prompt": str(prompt or "")[:2000],
            "provider": IMAGE_PROVIDER,
            "quality": "std",
            "ratio": "9:16",
            "count": 1,
        }
    )
    source_file = str((result or {}).get("file") or "")
    if not source_file:
        raise RuntimeError("缺失素材生图未返回文件")
    source_path = _out_path(source_file)
    size_bytes = source_path.stat().st_size
    if size_bytes <= 0 or size_bytes > MAX_GENERATED_IMAGE_BYTES:
        raise RuntimeError("缺失素材生图文件大小异常")
    content_type = mimetypes.guess_type(source_file)[0] or "image/png"
    suffix = source_path.suffix.lower() if source_path.suffix else ".png"
    key = "edit/{}/{}/generated/{}{}".format(
        _key_component(username),
        int(job_id),
        uuid.uuid4().hex,
        suffix,
    )
    url = cos.put_bytes(
        source_path.read_bytes(), key, content_type, private=True
    )
    return {
        "url": url,
        "cos_key": key,
        "content_type": content_type,
        "size_bytes": size_bytes,
        "role": role,
    }


def _material_requests(plan):
    raw = plan.get("material_requests") if isinstance(plan, dict) else []
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("素材生成请求格式错误")
    requests = []
    seen = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("素材生成请求格式错误")
        role = str(item.get("role") or "").strip()[:40]
        if not role or role.startswith("_") or role in seen:
            if role in seen:
                continue
            raise ValueError("素材角色无效")
        seen.add(role)
        requests.append(
            {
                "role": role,
                "kind": str(item.get("kind") or "image")[:24],
                "prompt": str(item.get("prompt") or "")[:2000],
            }
        )
    return requests


def resolve_materials(job_id, username, plan, attached_materials, heartbeat):
    username = str(username or "").strip()
    if not username or not int(job_id):
        raise ValueError("剪辑任务身份无效")
    result = {}
    ordered = sorted(
        [item for item in (attached_materials or []) if isinstance(item, dict)],
        key=lambda item: 0 if item.get("origin") == "uploaded" else 1,
    )
    for item in ordered:
        role = str(item.get("role") or "").strip()
        if role and role not in result:
            result[role] = dict(item)

    requests = _material_requests(plan or {})
    missing = [item for item in requests if item["role"] not in result]
    if len(missing) > MAX_GENERATED_IMAGES:
        raise ValueError("单个剪辑任务最多自动生成8张图片")

    db_path = ai_edit_store.init_db()
    for item in missing:
        if item["kind"] != "image" or not item["prompt"]:
            raise ValueError("缺失素材必须提供图片提示词")
        if heartbeat:
            heartbeat("generating_assets")
        generated = _generate_image(
            item["prompt"], int(job_id), username, item["role"]
        )
        material_id = "generated-" + uuid.uuid4().hex
        size_bytes = int(generated.get("size_bytes") or 0)
        ai_edit_store.create_material(
            db_path,
            material_id,
            username,
            "image",
            item["role"],
            "generated",
            generated["cos_key"],
            generated.get("content_type") or "image/png",
            size_bytes,
        )
        if not ai_edit_store.complete_material(
            db_path, material_id, username, size_bytes
        ):
            raise RuntimeError("生成素材入库失败")
        if not ai_edit_store.attach_material(
            db_path, int(job_id), material_id, item["role"]
        ):
            raise RuntimeError("生成素材关联失败")
        result[item["role"]] = {
            "id": material_id,
            "role": item["role"],
            "kind": "image",
            "origin": "generated",
            "cos_key": generated["cos_key"],
            "url": generated.get("url"),
        }

    words = (plan or {}).get("_words") or []
    if words:
        vtt = words_to_vtt(words)
        key = "edit/{}/{}/captions.vtt".format(
            _key_component(username), int(job_id)
        )
        url = cos.put_bytes(
            vtt.encode("utf-8"), key, "text/vtt; charset=utf-8", private=True
        )
        result["_captions"] = {"cos_key": key, "url": url}
    return result
