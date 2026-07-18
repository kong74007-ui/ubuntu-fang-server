"""One-click packaging for generated Digital IP talking-head assets.

The source picture and audio are intentionally left untouched.  This domain
only builds a deterministic HyperFrames composition around the source video,
submits it to the existing HeyGen account, and stores the rendered MP4 as a
normal video asset.
"""

import hashlib
import html
import json
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

from . import video
from .core import adb, _out_path, _resolve_out_file, public_url


AI_EDIT_MAX_SECONDS = max(15, int(os.environ.get("AI_EDIT_MAX_SECONDS", "180") or 180))
AI_EDIT_RENDER_TIMEOUT = max(120, int(os.environ.get("AI_EDIT_RENDER_TIMEOUT", "1800") or 1800))
EDIT_STYLES = {
    "auto": {"label": "AI 自动推荐", "accent": "#f1bd54", "accent2": "#f8df9e"},
    "content_first": {"label": "信息科普", "accent": "#38bdf8", "accent2": "#a5f3fc"},
    "product_seeding": {"label": "产品种草", "accent": "#fb7185", "accent2": "#fbcfe8"},
    "promo_fast": {"label": "快节奏促销", "accent": "#fb923c", "accent2": "#fde047"},
    "brand_premium": {"label": "高级品牌", "accent": "#d4a84f", "accent2": "#f8e7b0"},
    "review_compare": {"label": "测评对比", "accent": "#8b5cf6", "accent2": "#c4b5fd"},
    "lifestyle": {"label": "生活方式", "accent": "#34d399", "accent2": "#bbf7d0"},
}


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
    return {
        "source_video_asset_id": item["id"],
        "style_id": style_id,
        "mode": "ai_edit",
        "ratio": "9:16",
        "resolution": "1080p",
        "text": item.get("text") or "",
    }


def _probe_media(path):
    try:
        raw = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries",
                "format=duration:stream=codec_type,width,height",
                "-of", "json", str(path),
            ],
            check=True,
            timeout=30,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
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


def _prepare_source(source, output, duration):
    """Normalize to H.264/AAC and force dense keyframes for exact frame seeks."""
    command = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(source),
        "-map", "0:v:0", "-map", "0:a:0?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-g", "30", "-keyint_min", "30", "-sc_threshold", "0",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart",
        str(output),
    ]
    try:
        subprocess.run(
            command,
            check=True,
            timeout=max(180, min(900, int(duration * 6 + 120))),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("服务器未安装 FFmpeg，暂时无法一键剪辑") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("口播视频预处理超时，请换一个更短的视频") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b"").decode("utf-8", "replace")[:240]
        raise RuntimeError("口播视频预处理失败" + ("：" + detail if detail else "")) from exc
    if not output.is_file() or output.stat().st_size <= 0:
        raise RuntimeError("口播视频预处理失败")


def _text_cards(text, duration):
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
    if not cleaned:
        cleaned = "数字化 IP 口播"
    raw = [x.strip(" ，。！？；,.!?;") for x in re.split(r"(?<=[。！？!?；;])|(?<=，)", cleaned)]
    raw = [x for x in raw if x]
    chunks = []
    for part in raw:
        if chunks and len(chunks[-1]) + len(part) <= 30:
            chunks[-1] += part
        else:
            chunks.append(part[:42])
    if not chunks:
        chunks = [cleaned[:42]]
    max_cards = max(1, min(5, int(duration // 4) or 1))
    if len(chunks) > max_cards:
        if max_cards == 1:
            chunks = [chunks[0]]
        else:
            indexes = [round(i * (len(chunks) - 1) / (max_cards - 1)) for i in range(max_cards)]
            chunks = [chunks[i] for i in indexes]
    return chunks


def _caption_cues(text, duration):
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip() or "数字化 IP 口播"
    pieces = []
    for sentence in re.split(r"(?<=[。！？!?；;，,])", cleaned):
        sentence = sentence.strip()
        while len(sentence) > 18:
            pieces.append(sentence[:18])
            sentence = sentence[18:]
        if sentence:
            pieces.append(sentence)
    pieces = pieces[:36] or [cleaned[:18]]
    weights = [max(4, len(piece.strip("，。！？!?；;"))) for piece in pieces]
    total = float(sum(weights))
    cursor = 0.0
    cues = []
    for index, (piece, weight) in enumerate(zip(pieces, weights)):
        end = duration if index == len(pieces) - 1 else min(duration, cursor + duration * weight / total)
        cues.append((cursor, max(0.01, end - cursor), piece))
        cursor = end
    return cues


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


def build_hyperframes_html(text, duration, source_width, source_height, style_id="auto"):
    """Build a seek-safe 1080x1920 composition with timed direct-root clips."""
    duration = max(1.0, float(duration))
    style_id = _resolve_style(style_id if style_id in EDIT_STYLES else "auto", text)
    style = EDIT_STYLES[style_id]
    cards = _text_cards(text, duration)
    segment = duration / len(cards)
    card_html = []
    for index, card in enumerate(cards):
        start = min(max(0.0, index * segment + min(0.7, segment * 0.16)), max(0.0, duration - 0.7))
        card_duration = min(max(0.7, duration - start), max(1.4, min(3.4, segment * 0.72)))
        side = "left" if index % 2 == 0 else "right"
        card_html.append(
            '<section id="topic-%d" class="clip topic %s" data-start="%.3f" '
            'data-duration="%.3f" data-track-index="2">'
            '<div class="topic-no">%02d</div><div class="topic-copy">%s</div></section>'
            % (index + 1, side, start, card_duration, index + 1, html.escape(card))
        )
    caption_html = []
    for index, (start, cue_duration, cue) in enumerate(_caption_cues(text, duration)):
        caption_html.append(
            '<div id="caption-%d" class="clip caption" data-start="%.3f" '
            'data-duration="%.3f" data-track-index="5"><span>%s</span></div>'
            % (index + 1, start, min(cue_duration, duration - start), html.escape(cue))
        )
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
    #source-video{position:absolute;inset:0;width:1080px;height:1920px;object-fit:cover;background:#070b13}
    .clip{position:absolute;z-index:2}
    .shade{inset:0;background:linear-gradient(180deg,rgba(2,7,15,.38) 0%,transparent 24%,transparent 68%,rgba(2,7,15,.72) 100%);pointer-events:none}
    .brand{top:72px;left:64px;right:64px;display:flex;align-items:center;justify-content:space-between;font-size:25px;font-weight:800;letter-spacing:2px;text-shadow:0 3px 14px rgba(0,0,0,.5)}
    .brand-mark{display:flex;align-items:center;gap:13px}.brand-dot{width:15px;height:15px;border-radius:50%;background:var(--accent);box-shadow:0 0 26px var(--accent)}
    .brand-mode{padding:10px 18px;border:1px solid rgba(255,255,255,.26);border-radius:999px;background:rgba(7,11,19,.38);font-size:18px;font-weight:600;color:rgba(255,255,255,.78);letter-spacing:0}
    .topic{left:64px;right:64px;bottom:250px;min-height:188px;display:flex;align-items:center;gap:22px;padding:30px 34px;border:1px solid rgba(255,255,255,.2);border-radius:32px;background:linear-gradient(135deg,rgba(10,15,26,.92),rgba(24,28,39,.78));box-shadow:0 24px 70px rgba(0,0,0,.38);backdrop-filter:blur(18px)}
    .topic.right{flex-direction:row-reverse;text-align:right}.topic-no{flex:none;width:72px;height:72px;display:grid;place-items:center;border-radius:22px;background:linear-gradient(135deg,var(--accent2),var(--accent));color:#10131a;font:900 25px/1 monospace;box-shadow:0 12px 32px rgba(0,0,0,.28)}
    .topic-copy{min-width:0;overflow-wrap:anywhere;word-break:break-all;font-size:43px;line-height:1.35;font-weight:850;letter-spacing:.5px;text-shadow:0 3px 16px rgba(0,0,0,.45)}
    .caption{left:60px;right:60px;bottom:88px;text-align:center;font-size:43px;line-height:1.4;font-weight:900;letter-spacing:1px;text-shadow:0 4px 12px #000,0 2px 4px #000}.caption span{padding:9px 15px;background:rgba(0,0,0,.56);box-decoration-break:clone;-webkit-box-decoration-break:clone;border-radius:11px}.caption span:first-letter{color:var(--accent2)}
    .rail{left:64px;right:64px;bottom:54px;height:7px;border-radius:999px;background:rgba(255,255,255,.18);overflow:hidden}.rail:after{content:"";display:block;width:100%;height:100%;background:linear-gradient(90deg,var(--accent2),var(--accent))}
    .style-content_first .topic{left:42px;right:210px;border-left:12px solid var(--accent);border-radius:12px}.style-content_first .topic.right{left:210px;right:42px}
    .style-product_seeding .topic{left:72px;right:72px;border-radius:44px;background:linear-gradient(135deg,rgba(55,18,35,.92),rgba(20,12,27,.88))}.style-product_seeding .topic-no{border-radius:50%}
    .style-promo_fast .topic{left:34px;right:34px;bottom:270px;min-height:154px;border:5px solid var(--accent);border-radius:8px;transform:rotate(-1deg)}.style-promo_fast .topic.right{transform:rotate(1deg)}
    .style-brand_premium .topic{left:96px;right:96px;padding:38px 42px;border-radius:2px;border-color:rgba(212,168,79,.58);background:rgba(4,7,12,.86)}.style-brand_premium .topic-no{border-radius:2px}
    .style-review_compare .topic{left:42px;right:42px;border-top:8px solid var(--accent);border-radius:18px}.style-review_compare .topic-no{border-radius:9px}
    .style-lifestyle .topic{left:62px;right:62px;border-radius:46px;background:linear-gradient(135deg,rgba(8,42,34,.88),rgba(8,18,25,.82))}.style-lifestyle .topic-copy{font-size:40px;font-weight:760}
  </style>
</head>
<body>
  <div id="root" class="style-__STYLE__" data-composition-id="ai-edit-main" data-no-timeline data-start="0" data-width="1080" data-height="1920" data-fps="30" data-duration="__DURATION__">
    <video id="source-video" src="input-video.mp4" data-start="0" data-duration="__DURATION__" data-track-index="0" muted playsinline></video>
    <audio id="source-audio" src="input-video.mp4" data-start="0" data-duration="__DURATION__" data-track-index="10" data-volume="1"></audio>
    <div id="shade" class="clip shade" data-start="0" data-duration="__DURATION__" data-track-index="1"></div>
    <div id="brand" class="clip brand" data-start="0" data-duration="__DURATION__" data-track-index="3"><div class="brand-mark"><span class="brand-dot"></span><span>黄雀 AI · 数字化 IP</span></div><span class="brand-mode">__STYLE_LABEL__ · __ORIENTATION__</span></div>
    __CARDS__
    __CAPTIONS__
    <div id="rail" class="clip rail" data-start="0" data-duration="__DURATION__" data-track-index="4"></div>
  </div>
</body>
</html>
"""
    return (page.replace("__DURATION__", "%.3f" % duration)
                .replace("__STYLE__", style_id)
                .replace("__STYLE_LABEL__", html.escape(style["label"]))
                .replace("__ACCENT__", style["accent"])
                .replace("__ACCENT2__", style["accent2"])
                .replace("__ORIENTATION__", html.escape(orientation))
                .replace("__CARDS__", "\n    ".join(card_html))
                .replace("__CAPTIONS__", "\n    ".join(caption_html)))


def _write_project_zip(zip_path, html_text, video_path, font_path=None):
    fixed_time = (2026, 1, 1, 0, 0, 0)
    font_path = pathlib.Path(font_path) if font_path else _font_path()
    with zipfile.ZipFile(str(zip_path), "w") as archive:
        page = zipfile.ZipInfo("index.html", fixed_time)
        page.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(page, html_text.encode("utf-8"))
        media = zipfile.ZipInfo("input-video.mp4", fixed_time)
        media.compress_type = zipfile.ZIP_STORED
        media.file_size = video_path.stat().st_size
        with archive.open(media, "w") as target, video_path.open("rb") as source:
            shutil.copyfileobj(source, target, length=1024 * 1024)
        font = zipfile.ZipInfo("assets/noto-sans-sc-700.woff2", fixed_time)
        font.compress_type = zipfile.ZIP_STORED
        font.file_size = font_path.stat().st_size
        with archive.open(font, "w") as target, font_path.open("rb") as source:
            shutil.copyfileobj(source, target, length=256 * 1024)


def _idempotent_json(method, path, body, key, what, tries=4):
    encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    last = None
    for attempt in range(tries):
        try:
            return video._heygen_request_json(
                method,
                path,
                encoded,
                {"Content-Type": "application/json", "Idempotency-Key": key},
                timeout=180,
                direct=True,
            )
        except video.HeyGenRateLimited as exc:
            last = exc
        except video.HeyGenNetworkError as exc:
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
        "POST",
        "/assets/direct-uploads",
        {
            "filename": "huangque-ai-edit-%s.zip" % job_id,
            "content_type": "application/zip",
            "size_bytes": zip_path.stat().st_size,
            "checksum_sha256": digest,
        },
        "hq-ai-edit-upload-%s" % job_id,
        "创建剪辑工程上传",
    )
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
        "POST",
        "/assets/%s/complete" % asset_id,
        {"checksum_sha256": digest},
        "hq-ai-edit-complete-%s" % job_id,
        "确认剪辑工程上传",
    )
    completed = complete.get("data") or {}
    if str(completed.get("asset_id") or asset_id) != asset_id:
        raise RuntimeError("剪辑工程上传确认失败")
    return asset_id


def _submit_render(asset_id, job_id, title):
    data = _idempotent_json(
        "POST",
        "/hyperframes/renders",
        {
            "project": {"type": "asset_id", "asset_id": asset_id},
            "fps": 30,
            "quality": "standard",
            "format": "mp4",
            "resolution": "1080p",
            "aspect_ratio": "9:16",
            "composition": "index.html",
            "title": title[:160],
            "callback_id": "huangque-ai-edit-%s" % job_id,
        },
        "hq-ai-edit-render-%s" % job_id,
        "提交一键剪辑",
    )
    render_id = str((data.get("data") or {}).get("render_id") or "").strip()
    if not render_id:
        raise RuntimeError("一键剪辑未返回渲染任务编号")
    return render_id


def _wait_render(render_id):
    deadline = time.time() + AI_EDIT_RENDER_TIMEOUT
    last_status = ""
    while time.time() < deadline:
        try:
            data = video._heygen_request_json(
                "GET",
                "/hyperframes/renders/%s" % render_id,
                timeout=90,
                direct=True,
            )
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
            check=True,
            timeout=max(120, min(900, int(duration * 4 + 90))),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("一键剪辑成片完整解码校验失败") from exc


def gen_ai_edit(payload):
    username = str(payload.get("_username") or "").strip()
    job_id = int(payload.get("_job_id") or 0)
    if not username or not job_id:
        raise ValueError("一键剪辑任务缺少账号信息")
    item = _source_asset(payload.get("source_video_asset_id"), username)
    video.update_video_asset_phase(job_id, "validating", status="running")
    duration, width, height = _probe_media(item["source_path"])
    source_text = str(item.get("text") or payload.get("text") or "数字化 IP 口播").strip()
    requested_style = str(payload.get("style_id") or "auto")
    resolved_style = _resolve_style(requested_style, source_text)
    title = (source_text[:36] or "数字化 IP 口播") + " · 一键剪辑"

    with tempfile.TemporaryDirectory(prefix="hq-ai-edit-%s-" % job_id) as temp_dir:
        work = pathlib.Path(temp_dir)
        normalized = work / "input-video.mp4"
        project_zip = work / "project.zip"
        video.update_video_asset_phase(job_id, "composing", status="running")
        _prepare_source(item["source_path"], normalized, duration)
        normalized_duration, normalized_width, normalized_height = _probe_media(normalized)
        page = build_hyperframes_html(
            source_text, normalized_duration, normalized_width, normalized_height, requested_style
        )
        _write_project_zip(project_zip, page, normalized)
        video.update_video_asset_phase(job_id, "uploading", status="running")
        asset_id = _upload_project(project_zip, job_id)
        video.update_video_asset_phase(job_id, "rendering", status="running")
        with video.heygen_slot("一键剪辑"):
            render_id = _submit_render(asset_id, job_id, title)
            video.update_video_asset_phase(
                job_id, "rendering", status="running", provider_video_id=render_id, model="hyperframes"
            )
            render = _wait_render(render_id)

    video.update_video_asset_phase(job_id, "downloading", status="running")
    output_file = video._download_video_file_direct(render.get("video_url"), prefix="ai_edit")
    output_path = _resolve_out_file(output_file)
    if not output_path:
        raise RuntimeError("一键剪辑成片保存失败")
    video.update_video_asset_phase(job_id, "qc", status="running")
    output_duration, output_width, output_height = _probe_media(output_path)
    if output_width != 1080 or output_height != 1920:
        raise RuntimeError("一键剪辑成片尺寸校验失败")
    if abs(output_duration - duration) > max(1.2, duration * 0.04):
        raise RuntimeError("一键剪辑成片时长校验失败")
    _decode_check(output_path, output_duration)
    return {
        "mode": "ai_edit",
        "video_file": output_file,
        "video_url": public_url(output_file, "video/mp4", private=True),
        "text": source_text,
        "resolution": "1080p",
        "ratio": "9:16",
        "phase": "done",
        "status": "done",
        "provider_video_id": render_id,
        "model": "hyperframes",
        "duration": output_duration,
        "source_video_asset_id": item["id"],
        "style_id": resolved_style,
    }


HANDLERS = {"ai_edit": gen_ai_edit}
