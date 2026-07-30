"""Owner-scoped import of completed first-party talking-video assets."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any
from urllib.parse import quote

from . import ai_edit_v2_delivery as delivery
from . import ai_edit_v2_store as store


READY_STATUSES = {"done", "ready", "completed", "succeeded"}
TALKING_MODES = {"text", "audio"}


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _asset_db_path() -> str:
    return delivery._asset_db_path()


def _jobs_db_path() -> str:
    return os.environ.get("AI_EDIT_V2_JOB_DB") or os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "content_jobs.db"
    )


def _content_root() -> Path:
    configured = (
        os.environ.get("AI_EDIT_V2_PLATFORM_OUT")
        or os.environ.get("CONTENT_OUT")
        or os.path.join(os.path.dirname(os.path.dirname(__file__)), "content_out")
    )
    return Path(configured).resolve()


def _source_path(value: str) -> Path:
    root = _content_root()
    candidate = Path(str(value or ""))
    candidate = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("platform_asset_path_invalid") from exc
    if not candidate.is_file() or candidate.stat().st_size <= 0:
        raise ValueError("platform_asset_file_missing")
    return candidate


def _preview_url(value: str | None) -> str | None:
    rel = str(value or "").replace("\\", "/").lstrip("/")
    parts = rel.split("/")
    if (not rel or ":" in rel or "?" in rel or "#" in rel
            or any(part in ("", ".", "..") for part in parts)):
        return None
    return "/api/gen/file/" + quote(rel, safe="/")


def _authoritative_text(row: sqlite3.Row) -> str:
    text = str(row["text"] or "").strip()
    if text:
        return text
    job_id = row["job_id"]
    if job_id is None or not os.path.isfile(_jobs_db_path()):
        raise ValueError("platform_original_text_missing")
    with closing(_connect(_jobs_db_path())) as conn:
        job = conn.execute(
            "SELECT payload FROM jobs WHERE id=? AND username=?", (job_id, row["username"])
        ).fetchone()
    if job is None:
        raise ValueError("platform_original_text_missing")
    try:
        payload = json.loads(job["payload"] or "{}")
    except (TypeError, ValueError):
        payload = {}
    text = str((payload if isinstance(payload, dict) else {}).get("text")
               or (payload if isinstance(payload, dict) else {}).get("prompt") or "").strip()
    if not text:
        raise ValueError("platform_original_text_missing")
    return text


def _is_digital_ip_asset(
    row: sqlite3.Row,
    *,
    jobs_conn: sqlite3.Connection | None = None,
) -> bool:
    mode = str(row["mode"] or "").strip().lower()
    job_id = row["job_id"]
    owner = str(row["username"] or "").strip()
    provider_video_id = str(row["provider_video_id"] or "").strip()
    if mode not in TALKING_MODES or not job_id or not owner or not provider_video_id:
        return False
    try:
        _source_path(row["video_file"])
    except ValueError:
        return False
    jobs_db = _jobs_db_path()
    if not os.path.isfile(jobs_db):
        raise sqlite3.OperationalError("platform jobs database unavailable")
    if jobs_conn is None:
        with closing(_connect(jobs_db)) as conn:
            job = conn.execute(
                """SELECT kind,status,payload,result,COALESCE(deleted,0) AS deleted
                   FROM jobs WHERE id=? AND username=?""",
                (job_id, owner),
            ).fetchone()
    else:
        job = jobs_conn.execute(
            """SELECT kind,status,payload,result,COALESCE(deleted,0) AS deleted
               FROM jobs WHERE id=? AND username=?""",
            (job_id, owner),
        ).fetchone()
    if (
        job is None
        or str(job["kind"] or "").strip().lower() != "video"
        or str(job["status"] or "").strip().lower() != "done"
        or int(job["deleted"] or 0) != 0
    ):
        return False
    try:
        payload = json.loads(job["payload"] or "{}")
        result = json.loads(job["result"] or "{}")
    except (TypeError, ValueError):
        return False
    if not isinstance(payload, dict) or not isinstance(result, dict):
        return False
    payload_mode = str(
        payload.get("mode") or "text"
    ).strip().lower()
    result_mode = str(result.get("mode") or "").strip().lower()
    if (
        payload_mode != mode
        or result_mode != mode
        or str(result.get("type") or "").strip().lower() != "video"
        or str(result.get("status") or "").strip().lower() != "done"
    ):
        return False
    has_avatar = bool(str(payload.get("image_data") or payload.get("avatar_id") or "").strip())
    if not has_avatar:
        return False
    if mode == "text":
        return bool(str(payload.get("text") or "").strip() and str(payload.get("voice") or "").strip())
    return bool(str(payload.get("audio_data") or payload.get("audio_file") or "").strip())


def _owned_row(owner: str, asset_id: int) -> sqlite3.Row | None:
    with closing(_connect(_asset_db_path())) as conn:
        return conn.execute(
            """SELECT id,job_id,username,mode,video_file,provider_video_id,text,
                      ratio,status,created_at,updated_at
               FROM video_assets WHERE id=? AND username=?""",
            (int(asset_id), owner),
        ).fetchone()


def list_assets(owner: str, limit: int = 100) -> list[dict[str, Any]]:
    item_limit = max(1, min(100, int(limit)))
    items = []
    with (
        closing(_connect(_asset_db_path())) as conn,
        closing(_connect(_jobs_db_path())) as jobs_conn,
    ):
        rows = conn.execute(
            """SELECT id,job_id,username,mode,image_file,video_file,provider_video_id,
                      text,ratio,
                      status,created_at
               FROM video_assets
               WHERE username=? AND mode IN ('text','audio')
                  AND status IN ('done','ready','completed','succeeded')
                  AND video_file IS NOT NULL AND TRIM(video_file)!=''
               ORDER BY updated_at DESC,id DESC""",
            (owner,),
        )
        for row in rows:
            if not _is_digital_ip_asset(row, jobs_conn=jobs_conn):
                continue
            preview_url = _preview_url(row["video_file"])
            if preview_url is None:
                continue
            items.append({
                "id": int(row["id"]), "reference_id": str(row["id"]),
                "filename": os.path.basename(str(row["video_file"])),
                "summary": " ".join(str(row["text"] or "").split())[:120],
                "ratio": row["ratio"], "status": row["status"],
                "created_at": int(row["created_at"] or 0),
                "asset_type": "digital_ip",
                "preview_url": preview_url,
                "thumbnail_url": _preview_url(row["image_file"]),
            })
            if len(items) >= item_limit:
                break
    return items


def import_asset(
    owner: str,
    asset_id: int,
    *,
    cos_api: Any,
    probe_media: Any,
    db_path: str | None = None,
) -> dict[str, Any]:
    row = _owned_row(owner, asset_id)
    if row is None:
        raise LookupError("platform_asset_not_found")
    if not _is_digital_ip_asset(row):
        raise ValueError("platform_asset_not_digital_ip")
    if row["mode"] not in TALKING_MODES or row["status"] not in READY_STATUSES:
        raise ValueError("platform_asset_not_ready")
    original_text = _authoritative_text(row)
    source = _source_path(row["video_file"])
    with closing(store.open_store(store._db_path(db_path))) as conn:
        existing = conn.execute(
            """SELECT * FROM edit_v2_materials
               WHERE owner=? AND source='platform_video' AND platform_asset_id=?""",
            (owner, int(asset_id)),
        ).fetchone()
    if existing is not None:
        return dict(existing)

    owner_hash = hashlib.sha256(owner.encode("utf-8")).hexdigest()[:16]
    key = f"ai-edit-v2/{owner_hash}/platform/{int(asset_id)}/source.mp4"
    cos_api.put_file(str(source), key, "video/mp4", private=True)
    verified = cos_api.head_object(key)
    metadata = probe_media(cos_api.presign_get(key, expires=300), media_type="video")
    now = int(time.time())
    with closing(store.open_store(store._db_path(db_path))) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """INSERT OR IGNORE INTO edit_v2_materials(
                   owner,kind,purpose,source,platform_asset_id,cos_key,filename,
                   mime_type,etag,size_bytes,duration_ms,width,height,original_text,
                   status,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (owner, "video", "primary", "platform_video", int(asset_id), key,
             source.name, "video/mp4", verified.get("etag"),
             int(verified.get("content_length") or source.stat().st_size),
             metadata.get("duration_ms"), metadata.get("width"), metadata.get("height"),
             original_text, "ready", now, now),
        )
        material = conn.execute(
            """SELECT * FROM edit_v2_materials
               WHERE owner=? AND source='platform_video' AND platform_asset_id=?""",
            (owner, int(asset_id)),
        ).fetchone()
        conn.commit()
    if material is None:
        raise RuntimeError("platform_asset_import_failed")
    return dict(material)
