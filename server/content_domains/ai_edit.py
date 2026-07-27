# -*- coding: utf-8 -*-
"""AI 智能剪辑七阶段 API-only 编排。"""
import os

from . import (
    ai_edit_assets,
    ai_edit_store,
    ali_asr,
    audio,
    cos,
    edit_plan,
    edit_planner,
    video,
)
from .renderers.shotstack import ShotstackRenderer


MAX_OUTPUT_BYTES = 2 * 1024 * 1024 * 1024
ERROR_LABELS = {
    "source": "源素材",
    "asr": "语音识别",
    "planner": "剪辑方案",
    "assets": "素材准备",
    "renderer": "云端渲染",
    "transfer": "成片转存",
    "verify": "成片校验",
}


def _duration_ms(media_info):
    try:
        return int(round(float((media_info.get("Format") or {}).get("Duration")) * 1000))
    except (AttributeError, TypeError, ValueError):
        return 0


def _fresh_object_url(cos_key):
    cos_key = str(cos_key or "").strip()
    if not cos_key:
        raise ValueError("源素材缺少COS对象键")
    return cos.object_url(cos_key, private=True)


def _source_context(payload, username):
    if payload.get("source_video_asset_id"):
        item = video.get_owned_video_asset(username, payload["source_video_asset_id"])
        if not item:
            raise ValueError("视频素材不存在或不属于当前账号")
        cos_key = item.get("video_file")
        source_type = "video"
    elif payload.get("source_audio_asset_id"):
        item = audio.get_owned_audio_asset(username, payload["source_audio_asset_id"])
        if not item:
            raise ValueError("音频素材不存在或不属于当前账号")
        cos_key = item.get("file")
        source_type = "audio"
    elif payload.get("source_upload_id"):
        cos_key = payload.get("_source_upload_cos_key")
        content_type = str(payload.get("_source_upload_content_type") or "")
        source_type = "video" if content_type.startswith("video/") else "audio"
        if not cos_key or source_type not in {"video", "audio"}:
            raise ValueError("上传素材尚未完成或不属于当前账号")
    else:
        raise ValueError("缺少源素材")
    media_info = cos.get_media_info(cos_key)
    duration_ms = _duration_ms(media_info)
    if duration_ms <= 0:
        raise RuntimeError("无法读取源素材时长")
    if duration_ms > edit_plan.MAX_DURATION_MS:
        raise ValueError("源素材最长支持10分钟")
    return {
        "source_type": source_type,
        "cos_key": cos_key,
        "url": _fresh_object_url(cos_key),
        "duration_ms": duration_ms,
        "media_info": media_info,
    }


def _transcribe(source, heartbeat):
    return ali_asr.transcribe(source["url"], heartbeat=heartbeat)


def _plan(transcript, payload, source, attached_materials):
    generated = edit_planner.generate_plan(
        transcript,
        payload["style"],
        source["duration_ms"],
        attached_materials,
    )
    generated["ratio"] = payload["ratio"]
    allowed_ids = {
        str(item.get("id"))
        for item in attached_materials
        if isinstance(item, dict) and item.get("id")
    }
    return edit_plan.validate_edit_plan(
        generated, source["duration_ms"], allowed_ids
    )


def _resolve_assets(job_id, username, plan, source, attached_materials, heartbeat):
    with_words = dict(plan)
    with_words["_words"] = list(plan.get("_words") or [])
    resolved = ai_edit_assets.resolve_materials(
        job_id, username, with_words, attached_materials, heartbeat
    )
    materials = {}
    for item in attached_materials:
        if not isinstance(item, dict) or not item.get("id") or not item.get("cos_key"):
            continue
        materials[str(item["id"])] = {
            "kind": item.get("kind") or "image",
            "url": _fresh_object_url(item["cos_key"]),
        }
    for item in resolved.values():
        if not isinstance(item, dict) or not item.get("id") or not item.get("cos_key"):
            continue
        materials[str(item["id"])] = {
            "kind": item.get("kind") or "image",
            "url": _fresh_object_url(item["cos_key"]),
        }
    captions = resolved.get("_captions") or {}
    return {
        "source_type": source["source_type"],
        "source_url": source["url"],
        "captions_url": captions.get("url"),
        "materials": materials,
    }


def _render(
    job_id,
    username,
    plan,
    assets,
    heartbeat,
    existing_provider_job_id=None,
):
    del username
    renderer = ShotstackRenderer()
    provider_job_id = str(existing_provider_job_id or "").strip()
    if not provider_job_id:
        callback_base = str(
            os.environ.get(
                "SHOTSTACK_CALLBACK_BASE", "https://fang.huangquechuanmei.com"
            )
        ).rstrip("/")
        edit = renderer.build_timeline(
            plan, assets, callback_base + "/api/v1/edit/webhooks/shotstack"
        )
        provider_job_id = renderer.submit(edit)
        if not ai_edit_store.set_provider_job(
            None, job_id, provider_job_id, "queued"
        ):
            raise RuntimeError("渲染任务状态写入失败")
    current = renderer.wait(provider_job_id, heartbeat)
    if not str(current.get("url") or "").startswith("https://"):
        raise RuntimeError("Shotstack未返回HTTPS成片地址")
    ai_edit_store.set_provider_job(
        None, job_id, provider_job_id, current.get("status") or "done"
    )
    return {"provider_job_id": provider_job_id, "url": current["url"]}


def _transfer(job_id, username, rendered):
    output_key = "edit-output/{}/{}.mp4".format(
        ai_edit_assets._key_component(username), int(job_id)
    )
    cos.transfer_remote(rendered["url"], output_key, MAX_OUTPUT_BYTES)
    return output_key


def verify_media(media_info, expected_duration_ms, require_audio=True):
    duration_ms = _duration_ms(media_info)
    if duration_ms <= 0:
        raise RuntimeError("成片缺少有效时长")
    stream = media_info.get("Stream") if isinstance(media_info, dict) else {}
    stream = stream if isinstance(stream, dict) else {}
    audio_streams = stream.get("Audio") or []
    if require_audio and not audio_streams:
        raise RuntimeError("成片缺少音轨")
    if abs(duration_ms - int(expected_duration_ms)) > 500:
        raise RuntimeError("成片时长与剪辑方案不一致")
    return duration_ms / 1000.0


def _verify(output_key, plan):
    media_info = cos.get_media_info(output_key)
    expected_duration_ms = max(
        int(item["end_ms"]) for item in (plan.get("segments") or [])
    )
    return verify_media(media_info, expected_duration_ms, require_audio=True)


def _stage_call(job_id, code, stage, function, *args, **kwargs):
    ai_edit_store.update_stage(None, job_id, stage)
    try:
        return function(*args, **kwargs)
    except Exception as exc:
        detail = str(exc)[:500]
        ai_edit_store.update_stage(None, job_id, "failed", code, detail)
        raise RuntimeError("%s失败：%s" % (ERROR_LABELS[code], detail[:240])) from exc


def run_ai_edit(payload):
    payload = dict(payload or {})
    username = str(payload.get("_username") or "").strip()
    try:
        job_id = int(payload.get("_job_id") or 0)
    except (TypeError, ValueError):
        job_id = 0
    if not username or job_id <= 0:
        raise ValueError("AI剪辑任务身份无效")
    detail = ai_edit_store.get_owned_job(None, username, job_id)
    if not detail:
        raise ValueError("AI剪辑详细任务不存在")

    attached_materials = [
        dict(item)
        for item in (payload.get("_attached_materials") or [])
        if isinstance(item, dict)
    ]

    def heartbeat(stage):
        ai_edit_store.update_stage(None, job_id, stage)

    source = _stage_call(
        job_id, "source", "resolving_source", _source_context, payload, username
    )
    transcript = _stage_call(
        job_id, "asr", "transcribing", _transcribe, source, heartbeat
    )
    plan = _stage_call(
        job_id,
        "planner",
        "planning",
        _plan,
        transcript,
        payload,
        source,
        attached_materials,
    )
    plan["_words"] = transcript.get("words") or []
    assets = _stage_call(
        job_id,
        "assets",
        "preparing_assets",
        _resolve_assets,
        job_id,
        username,
        plan,
        source,
        attached_materials,
        heartbeat,
    )
    rendered = _stage_call(
        job_id,
        "renderer",
        "rendering",
        _render,
        job_id,
        username,
        plan,
        assets,
        heartbeat,
        existing_provider_job_id=detail.get("provider_job_id"),
    )
    output_key = _stage_call(
        job_id, "transfer", "transferring", _transfer, job_id, username, rendered
    )
    duration = _stage_call(
        job_id, "verify", "verifying", _verify, output_key, plan
    )
    ai_edit_store.update_stage(None, job_id, "done")
    return {
        "mode": "ai_edit",
        "video_file": output_key,
        "video_url": cos.object_url(output_key, private=True),
        "text": str(transcript.get("text") or "")[:1000],
        "resolution": "1080p",
        "ratio": payload["ratio"],
        "phase": "done",
        "status": "done",
        "provider_video_id": rendered["provider_job_id"],
        "model": "shotstack",
        "duration": duration,
    }


HANDLERS = {"ai_edit": run_ai_edit}
