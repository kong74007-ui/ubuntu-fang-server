# -*- coding: utf-8 -*-
"""Private metadata store for the one-click AI editor.

The shared ``content_jobs.db`` remains the source of truth for worker queues and
legacy job history.  This database only adds editor-specific material,
timeline, progress and billing-hold metadata, so older jobs keep working.
"""

import hashlib
import json
import mimetypes
import os
import pathlib
import re
import sqlite3
import subprocess
import time
import uuid
from contextlib import closing


BASE = pathlib.Path(__file__).resolve().parents[1]
OUT_DIR = pathlib.Path(os.environ.get("CONTENT_OUT", str(BASE / "content_out")))
EDIT_DB = pathlib.Path(os.environ.get("AI_EDIT_DB", str(BASE / "ai_edit.db")))
MATERIAL_DIR = pathlib.Path(
    os.environ.get("AI_EDIT_MATERIAL_DIR", str(OUT_DIR / "ai_edit_materials"))
)

JOB_MATERIAL_RECOMMENDED = 20
JOB_MATERIAL_SOFT_LIMIT = 30
JOB_MATERIAL_HARD_LIMIT = 50
LIBRARY_MATERIAL_LIMIT = max(50, int(os.environ.get("AI_EDIT_LIBRARY_LIMIT", "200") or 200))
IMAGE_MAX_BYTES = max(1024 * 1024, int(os.environ.get("AI_EDIT_IMAGE_MAX_MB", "25") or 25) * 1024 * 1024)
VIDEO_MAX_BYTES = max(5 * 1024 * 1024, int(os.environ.get("AI_EDIT_VIDEO_MAX_MB", "200") or 200) * 1024 * 1024)
VIDEO_MAX_SECONDS = max(30, int(os.environ.get("AI_EDIT_MATERIAL_MAX_SECONDS", "600") or 600))

USAGES = {"must_use", "auto", "exclude"}
CONTENT_TYPES = {
    "image/jpeg": ("image", ".jpg"),
    "image/png": ("image", ".png"),
    "image/webp": ("image", ".webp"),
    "video/mp4": ("video", ".mp4"),
    "video/quicktime": ("video", ".mov"),
}


def _db():
    EDIT_DB.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(EDIT_DB), timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c


def init_db():
    MATERIAL_DIR.mkdir(parents=True, exist_ok=True)
    with closing(_db()) as c:
        c.execute("""CREATE TABLE IF NOT EXISTS edit_materials(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            kind TEXT NOT NULL,
            usage TEXT NOT NULL DEFAULT 'auto',
            filename TEXT NOT NULL,
            content_type TEXT NOT NULL,
            local_file TEXT NOT NULL UNIQUE,
            size_bytes INTEGER NOT NULL,
            width INTEGER,
            height INTEGER,
            duration REAL,
            sha256 TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'ready',
            analysis_json TEXT,
            deleted INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )""")
        material_columns = {row[1] for row in c.execute("PRAGMA table_info(edit_materials)").fetchall()}
        if "cos_key" not in material_columns:
            c.execute("ALTER TABLE edit_materials ADD COLUMN cos_key TEXT")
        if "storage" not in material_columns:
            c.execute("ALTER TABLE edit_materials ADD COLUMN storage TEXT NOT NULL DEFAULT 'local'")
        c.execute("CREATE INDEX IF NOT EXISTS idx_edit_materials_user ON edit_materials(username, deleted, id DESC)")
        c.execute("""CREATE TABLE IF NOT EXISTS edit_jobs(
            job_id INTEGER PRIMARY KEY,
            username TEXT NOT NULL,
            source_video_asset_id INTEGER NOT NULL,
            style_id TEXT NOT NULL,
            product_facts_json TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            stage TEXT NOT NULL DEFAULT 'queued',
            progress INTEGER NOT NULL DEFAULT 5,
            message TEXT,
            billing_state TEXT NOT NULL DEFAULT 'HELD',
            timeline_json TEXT,
            result_json TEXT,
            error TEXT,
            cancel_requested INTEGER NOT NULL DEFAULT 0,
            eta_seconds INTEGER,
            stage_timings_json TEXT,
            provider_usage_json TEXT,
            cost_breakdown_json TEXT,
            style_version TEXT NOT NULL DEFAULT '1',
            prompt_version TEXT NOT NULL DEFAULT '2',
            timeline_version TEXT NOT NULL DEFAULT '2.0',
            result_version INTEGER NOT NULL DEFAULT 1,
            warning_codes_json TEXT,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )""")
        job_columns = {row[1] for row in c.execute("PRAGMA table_info(edit_jobs)").fetchall()}
        job_migrations = {
            "eta_seconds": "INTEGER",
            "stage_timings_json": "TEXT",
            "provider_usage_json": "TEXT",
            "cost_breakdown_json": "TEXT",
            "style_version": "TEXT NOT NULL DEFAULT '1'",
            "prompt_version": "TEXT NOT NULL DEFAULT '2'",
            "timeline_version": "TEXT NOT NULL DEFAULT '2.0'",
            "result_version": "INTEGER NOT NULL DEFAULT 1",
            "warning_codes_json": "TEXT",
        }
        for column, definition in job_migrations.items():
            if column not in job_columns:
                c.execute("ALTER TABLE edit_jobs ADD COLUMN %s %s" % (column, definition))
        c.execute("CREATE INDEX IF NOT EXISTS idx_edit_jobs_user ON edit_jobs(username, job_id DESC)")
        c.execute("""CREATE TABLE IF NOT EXISTS edit_job_assets(
            job_id INTEGER NOT NULL,
            material_id INTEGER NOT NULL,
            usage TEXT NOT NULL DEFAULT 'auto',
            ordinal INTEGER NOT NULL DEFAULT 0,
            analysis_json TEXT,
            evidence_text TEXT,
            PRIMARY KEY(job_id, material_id),
            FOREIGN KEY(job_id) REFERENCES edit_jobs(job_id) ON DELETE CASCADE,
            FOREIGN KEY(material_id) REFERENCES edit_materials(id)
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS billing_holds(
            job_id INTEGER PRIMARY KEY,
            username TEXT NOT NULL,
            amount INTEGER NOT NULL,
            state TEXT NOT NULL DEFAULT 'HELD',
            transaction_key TEXT NOT NULL UNIQUE,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            captured_at INTEGER,
            released_at INTEGER,
            FOREIGN KEY(job_id) REFERENCES edit_jobs(job_id) ON DELETE CASCADE
        )""")
        c.commit()


def normalize_usage(value):
    usage = str(value or "auto").strip().lower()
    if usage not in USAGES:
        raise ValueError("素材用途必须是必用、自动或排除")
    return usage


def normalize_product_facts(value):
    raw = value if isinstance(value, dict) else {}
    limits = {"name": 80, "category": 80, "spec": 240, "claims": 600}
    result = {}
    for key, limit in limits.items():
        text = re.sub(r"\s+", " ", str(raw.get(key) or "")).strip()
        if text:
            result[key] = text[:limit]
    return result


def _safe_filename(value):
    name = pathlib.Path(str(value or "material")).name
    name = re.sub(r"[\x00-\x1f\\/:*?\"<>|]+", "_", name).strip(" ._")
    return (name or "material")[:120]


def _content_type(filename, declared):
    declared = str(declared or "").split(";", 1)[0].strip().lower()
    guessed = (mimetypes.guess_type(str(filename or ""))[0] or "").lower()
    content_type = declared if declared in CONTENT_TYPES else guessed
    if content_type not in CONTENT_TYPES:
        raise ValueError("仅支持 JPG、PNG、WEBP、MP4 和 MOV 素材")
    return content_type, CONTENT_TYPES[content_type]


def _probe(path, kind):
    try:
        raw = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration:stream=codec_type,width,height", "-of", "json", str(path)],
            check=True, timeout=45, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ).stdout.decode("utf-8", "replace")
        data = json.loads(raw or "{}")
        streams = data.get("streams") or []
        visual = next((item for item in streams if item.get("codec_type") == "video"), {})
        width = int(visual.get("width") or 0)
        height = int(visual.get("height") or 0)
        duration = float((data.get("format") or {}).get("duration") or 0)
    except Exception as exc:
        raise ValueError("无法读取素材，请重新导出后上传") from exc
    if width <= 0 or height <= 0:
        raise ValueError("素材没有有效画面")
    if kind == "video" and duration <= 0:
        raise ValueError("视频素材时长无效")
    if kind == "video" and duration > VIDEO_MAX_SECONDS + 0.05:
        raise ValueError("单个视频素材最长支持 %d 秒" % VIDEO_MAX_SECONDS)
    return width, height, max(0.0, duration)


def _relative_to_out(path):
    return pathlib.Path(path).resolve().relative_to(OUT_DIR.resolve()).as_posix()


def save_material(username, fileobj, filename, declared_type="", usage="auto"):
    username = str(username or "").strip()
    if not username:
        raise ValueError("未登录")
    usage = normalize_usage(usage)
    filename = _safe_filename(filename)
    content_type, (kind, extension) = _content_type(filename, declared_type)
    init_db()
    with closing(_db()) as c:
        count = c.execute(
            "SELECT COUNT(*) AS n FROM edit_materials WHERE username=? AND deleted=0", (username,)
        ).fetchone()["n"]
    if int(count or 0) >= LIBRARY_MATERIAL_LIMIT:
        raise ValueError("素材库已达到 %d 个，请删除不用的素材后再上传" % LIBRARY_MATERIAL_LIMIT)

    user_dir = MATERIAL_DIR / hashlib.sha256(username.encode("utf-8")).hexdigest()[:16]
    user_dir.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    target = user_dir / (token + extension)
    temporary = user_dir / (token + ".part")
    limit = IMAGE_MAX_BYTES if kind == "image" else VIDEO_MAX_BYTES
    digest = hashlib.sha256()
    size = 0
    try:
        with temporary.open("wb") as output:
            while True:
                chunk = fileobj.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise ValueError("图片最大 %dMB，视频最大 %dMB" %
                                     (IMAGE_MAX_BYTES // 1024 // 1024, VIDEO_MAX_BYTES // 1024 // 1024))
                digest.update(chunk)
                output.write(chunk)
        if size <= 0:
            raise ValueError("上传文件为空")
        temporary.replace(target)
        width, height, duration = _probe(target, kind)
        now = int(time.time())
        rel = _relative_to_out(target)
        cos_key, storage = None, "local"
        try:
            from . import cos
            if cos.enabled():
                cos.upload(target, rel, content_type, private=True)
                cos_key, storage = rel, "cos"
        except Exception as exc:
            print("[ai-edit] material COS archive fallback: %s" % str(exc)[:180], flush=True)
        with closing(_db()) as c:
            cur = c.execute(
                """INSERT INTO edit_materials(
                    username,kind,usage,filename,content_type,local_file,size_bytes,width,height,duration,
                    sha256,status,created_at,updated_at,cos_key,storage
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (username, kind, usage, filename, content_type, rel, size, width, height, duration,
                 digest.hexdigest(), "ready", now, now, cos_key, storage),
            )
            c.commit()
            material_id = cur.lastrowid
    except Exception:
        for path in (temporary, target):
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
        raise
    return get_material(material_id, username)


def _material_public(row):
    item = dict(row)
    item.pop("local_file", None)
    item.pop("analysis_json", None)
    item["usage"] = normalize_usage(item.get("usage"))
    item["content_url"] = "/api/v1/edit-assets/%d/content" % int(item["id"])
    item["duration"] = round(float(item.get("duration") or 0), 3)
    return item


def list_materials(username, include_excluded=True, limit=200):
    init_db()
    limit = max(1, min(int(limit or 200), LIBRARY_MATERIAL_LIMIT))
    where = "username=? AND deleted=0"
    params = [username]
    if not include_excluded:
        where += " AND usage!='exclude'"
    with closing(_db()) as c:
        rows = c.execute(
            "SELECT * FROM edit_materials WHERE %s ORDER BY id DESC LIMIT ?" % where,
            params + [limit],
        ).fetchall()
    return [_material_public(row) for row in rows]


def get_material(material_id, username=None, include_deleted=False):
    init_db()
    try:
        material_id = int(material_id)
    except (TypeError, ValueError):
        raise LookupError("素材不存在")
    where = ["id=?"]
    params = [material_id]
    if username is not None:
        where.append("username=?")
        params.append(username)
    if not include_deleted:
        where.append("deleted=0")
    with closing(_db()) as c:
        row = c.execute("SELECT * FROM edit_materials WHERE " + " AND ".join(where), params).fetchone()
    if not row:
        raise LookupError("素材不存在")
    return _material_public(row)


def material_path(material_id, username):
    init_db()
    with closing(_db()) as c:
        row = c.execute(
            "SELECT * FROM edit_materials WHERE id=? AND username=? AND deleted=0",
            (int(material_id), username),
        ).fetchone()
    if not row:
        raise LookupError("素材不存在")
    path = (OUT_DIR / str(row["local_file"])).resolve()
    try:
        path.relative_to(OUT_DIR.resolve())
    except Exception as exc:
        raise LookupError("素材不存在") from exc
    if not path.is_file():
        raise LookupError("素材文件已不存在")
    return path, dict(row)


def update_material_usage(material_id, username, usage):
    usage = normalize_usage(usage)
    init_db()
    with closing(_db()) as c:
        cur = c.execute(
            "UPDATE edit_materials SET usage=?,updated_at=? WHERE id=? AND username=? AND deleted=0",
            (usage, int(time.time()), int(material_id), username),
        )
        c.commit()
    if cur.rowcount != 1:
        raise LookupError("素材不存在")
    return get_material(material_id, username)


def delete_material(material_id, username):
    path, row = material_path(material_id, username)
    now = int(time.time())
    with closing(_db()) as c:
        cur = c.execute(
            "UPDATE edit_materials SET deleted=1,status='deleted',updated_at=? "
            "WHERE id=? AND username=? AND deleted=0",
            (now, int(material_id), username),
        )
        c.commit()
    if cur.rowcount != 1:
        raise LookupError("素材不存在")
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass
    return _material_public(row)


def _normalize_material_refs(raw, username):
    refs = raw if isinstance(raw, list) else []
    if len(refs) > JOB_MATERIAL_HARD_LIMIT:
        raise ValueError("每次剪辑最多选择 %d 个辅助素材" % JOB_MATERIAL_HARD_LIMIT)
    result = []
    seen = set()
    for index, value in enumerate(refs):
        if isinstance(value, dict):
            material_id = value.get("id")
            usage = normalize_usage(value.get("usage") or "auto")
        else:
            material_id = value
            usage = "auto"
        try:
            material_id = int(material_id)
        except (TypeError, ValueError):
            raise ValueError("辅助素材编号无效")
        if material_id in seen:
            continue
        seen.add(material_id)
        item = get_material(material_id, username)
        result.append({"id": material_id, "usage": usage or item.get("usage") or "auto", "ordinal": index})
    return result


def validate_material_refs(raw, username):
    """Validate ownership/limits before points are held and return canonical refs."""
    return _normalize_material_refs(raw, username)


def create_job(job_id, username, payload, amount):
    init_db()
    refs = _normalize_material_refs(payload.get("materials"), username)
    facts = normalize_product_facts(payload.get("product_facts"))
    now = int(time.time())
    with closing(_db()) as c:
        c.execute("BEGIN IMMEDIATE")
        c.execute(
            """INSERT OR IGNORE INTO edit_jobs(
                job_id,username,source_video_asset_id,style_id,product_facts_json,status,stage,
                progress,message,billing_state,created_at,updated_at
            ) VALUES(?,?,?,?,?,'pending','queued',5,'任务已进入队列','HELD',?,?)""",
            (int(job_id), username, int(payload["source_video_asset_id"]), payload.get("style_id") or "auto",
             json.dumps(facts, ensure_ascii=False), now, now),
        )
        for ref in refs:
            c.execute(
                "INSERT OR IGNORE INTO edit_job_assets(job_id,material_id,usage,ordinal) VALUES(?,?,?,?)",
                (int(job_id), ref["id"], ref["usage"], ref["ordinal"]),
            )
        c.execute(
            """INSERT OR IGNORE INTO billing_holds(
                job_id,username,amount,state,transaction_key,created_at,updated_at
            ) VALUES(?,?,?,'HELD',?,?,?)""",
            (int(job_id), username, int(amount or 0), "ai-edit:%d" % int(job_id), now, now),
        )
        c.commit()
    return public_job(job_id, username)


def ensure_legacy_job(job_id, username, payload, amount=30):
    init_db()
    with closing(_db()) as c:
        row = c.execute("SELECT job_id FROM edit_jobs WHERE job_id=? AND username=?", (int(job_id), username)).fetchone()
    if row:
        return
    try:
        create_job(job_id, username, payload, amount)
    except Exception:
        pass


def job_materials(job_id, username):
    init_db()
    with closing(_db()) as c:
        rows = c.execute(
            """SELECT m.*,ja.usage AS job_usage,ja.ordinal,ja.analysis_json AS job_analysis_json,
                      ja.evidence_text
               FROM edit_job_assets ja JOIN edit_materials m ON m.id=ja.material_id
               JOIN edit_jobs j ON j.job_id=ja.job_id
               WHERE ja.job_id=? AND j.username=? AND m.deleted=0
               ORDER BY CASE ja.usage WHEN 'must_use' THEN 0 WHEN 'auto' THEN 1 ELSE 2 END, ja.ordinal, m.id""",
            (int(job_id), username),
        ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["usage"] = item.pop("job_usage")
        for key in ("analysis_json", "job_analysis_json"):
            try:
                item[key] = json.loads(item.get(key) or "{}")
            except Exception:
                item[key] = {}
        result.append(item)
    return result


def set_material_analysis(job_id, material_id, analysis, evidence=""):
    init_db()
    encoded = json.dumps(analysis if isinstance(analysis, dict) else {}, ensure_ascii=False)
    with closing(_db()) as c:
        c.execute(
            "UPDATE edit_job_assets SET analysis_json=?,evidence_text=? WHERE job_id=? AND material_id=?",
            (encoded, str(evidence or "")[:1200], int(job_id), int(material_id)),
        )
        c.execute(
            "UPDATE edit_materials SET analysis_json=?,updated_at=? WHERE id=?",
            (encoded, int(time.time()), int(material_id)),
        )
        c.commit()


def update_stage(job_id, stage, progress, message="", status="running"):
    init_db()
    now = int(time.time())
    with closing(_db()) as c:
        c.execute("BEGIN IMMEDIATE")
        row = c.execute(
            "SELECT stage,stage_timings_json,created_at FROM edit_jobs WHERE job_id=?", (int(job_id),)
        ).fetchone()
        timings = {}
        if row:
            try:
                timings = json.loads(row["stage_timings_json"] or "{}")
            except Exception:
                timings = {}
            previous = str(row["stage"] or "queued")
            if previous != str(stage):
                timings.setdefault(previous, {}).setdefault("started_at", int(row["created_at"] or now))
                timings[previous]["ended_at"] = now
            timings.setdefault(str(stage), {}).setdefault("started_at", now)
        progress_value = max(0, min(100, int(progress)))
        elapsed = max(1, now - int(row["created_at"] or now)) if row else 1
        eta = 0 if progress_value >= 100 else min(7200, int(elapsed * (100 - progress_value) / max(1, progress_value - 5)))
        c.execute(
            """UPDATE edit_jobs SET stage=?,progress=?,message=?,status=?,eta_seconds=?,
                       stage_timings_json=?,updated_at=? WHERE job_id=?""",
            (str(stage), progress_value, str(message or "")[:240], str(status), eta,
             json.dumps(timings, ensure_ascii=False), now, int(job_id)),
        )
        c.commit()


def set_usage(job_id, provider_usage=None, cost_breakdown=None, warnings=None):
    init_db()
    with closing(_db()) as c:
        c.execute(
            """UPDATE edit_jobs SET provider_usage_json=?,cost_breakdown_json=?,
                       warning_codes_json=?,updated_at=? WHERE job_id=?""",
            (json.dumps(provider_usage or {}, ensure_ascii=False),
             json.dumps(cost_breakdown or {}, ensure_ascii=False),
             json.dumps(warnings or [], ensure_ascii=False), int(time.time()), int(job_id)),
        )
        c.commit()


def set_timeline(job_id, timeline):
    init_db()
    encoded = json.dumps(timeline if isinstance(timeline, dict) else {}, ensure_ascii=False)
    with closing(_db()) as c:
        c.execute("UPDATE edit_jobs SET timeline_json=?,updated_at=? WHERE job_id=?",
                  (encoded, int(time.time()), int(job_id)))
        c.commit()


def request_cancel(job_id, username):
    init_db()
    with closing(_db()) as c:
        cur = c.execute(
            "UPDATE edit_jobs SET cancel_requested=1,message='正在取消',updated_at=? WHERE job_id=? AND username=?",
            (int(time.time()), int(job_id), username),
        )
        c.commit()
    if cur.rowcount != 1:
        raise LookupError("剪辑任务不存在")


def cancel_requested(job_id):
    init_db()
    with closing(_db()) as c:
        row = c.execute("SELECT cancel_requested FROM edit_jobs WHERE job_id=?", (int(job_id),)).fetchone()
    return bool(row and row["cancel_requested"])


def capture_hold(job_id):
    init_db()
    now = int(time.time())
    with closing(_db()) as c:
        c.execute("BEGIN IMMEDIATE")
        cur = c.execute(
            "UPDATE billing_holds SET state='CAPTURED',captured_at=?,updated_at=? WHERE job_id=? AND state='HELD'",
            (now, now, int(job_id)),
        )
        c.execute(
            "UPDATE edit_jobs SET billing_state='CAPTURED',updated_at=? WHERE job_id=? AND billing_state='HELD'",
            (now, int(job_id)),
        )
        c.commit()
    return cur.rowcount == 1


def _finish_timings(connection, job_id, terminal_stage, now):
    row = connection.execute(
        "SELECT stage,stage_timings_json,created_at FROM edit_jobs WHERE job_id=?", (int(job_id),)
    ).fetchone()
    if not row:
        return "{}"
    try:
        timings = json.loads(row["stage_timings_json"] or "{}")
    except Exception:
        timings = {}
    previous = str(row["stage"] or "queued")
    timings.setdefault(previous, {}).setdefault("started_at", int(row["created_at"] or now))
    timings[previous]["ended_at"] = now
    timings[str(terminal_stage)] = {"started_at": now, "ended_at": now}
    return json.dumps(timings, ensure_ascii=False)


def release_hold(job_id):
    init_db()
    now = int(time.time())
    with closing(_db()) as c:
        c.execute("BEGIN IMMEDIATE")
        cur = c.execute(
            "UPDATE billing_holds SET state='RELEASED',released_at=?,updated_at=? WHERE job_id=? AND state='HELD'",
            (now, now, int(job_id)),
        )
        c.execute(
            "UPDATE edit_jobs SET billing_state='RELEASED',updated_at=? WHERE job_id=? AND billing_state='HELD'",
            (now, int(job_id)),
        )
        c.commit()
    return cur.rowcount == 1


def mark_done(job_id, result):
    init_db()
    now = int(time.time())
    with closing(_db()) as c:
        timings = _finish_timings(c, job_id, "done", now)
        c.execute(
            """UPDATE edit_jobs SET status='done',stage='done',progress=100,eta_seconds=0,message='剪辑完成',
                       result_json=?,stage_timings_json=?,error=NULL,updated_at=? WHERE job_id=?""",
            (json.dumps(result or {}, ensure_ascii=False), timings, now, int(job_id)),
        )
        c.commit()


def mark_failed(job_id, error, canceled=False):
    init_db()
    now = int(time.time())
    stage = "canceled" if canceled else "failed"
    status = "canceled" if canceled else "error"
    message = "已取消，点数已释放" if canceled else "剪辑失败，点数已释放"
    with closing(_db()) as c:
        timings = _finish_timings(c, job_id, stage, now)
        c.execute(
            """UPDATE edit_jobs SET status=?,stage=?,progress=100,eta_seconds=0,message=?,error=?,
                       stage_timings_json=?,updated_at=? WHERE job_id=?""",
            (status, stage, message, str(error or "")[:500], timings, now, int(job_id)),
        )
        c.commit()


def public_job(job_id, username, include_timeline=False):
    init_db()
    with closing(_db()) as c:
        row = c.execute("SELECT * FROM edit_jobs WHERE job_id=? AND username=?", (int(job_id), username)).fetchone()
        hold = c.execute("SELECT amount,state FROM billing_holds WHERE job_id=?", (int(job_id),)).fetchone()
    if not row:
        raise LookupError("剪辑任务不存在")
    item = dict(row)
    for key in ("product_facts_json", "result_json", "stage_timings_json",
                "provider_usage_json", "cost_breakdown_json", "warning_codes_json"):
        target = key[:-5]
        try:
            fallback = "[]" if key == "warning_codes_json" else "{}"
            item[target] = json.loads(item.pop(key) or fallback)
        except Exception:
            item[target] = [] if key == "warning_codes_json" else {}
    raw_timeline = item.pop("timeline_json", None)
    if include_timeline:
        try:
            item["timeline"] = json.loads(raw_timeline or "{}")
        except Exception:
            item["timeline"] = {}
    item["cancel_requested"] = bool(item.get("cancel_requested"))
    item["billing"] = {"amount": int(hold["amount"] or 0), "state": hold["state"]} if hold else {
        "amount": 0, "state": item.get("billing_state") or "NONE"
    }
    item["materials"] = [
        {k: value for k, value in material.items() if k not in {"local_file", "analysis_json", "job_analysis_json"}}
        for material in job_materials(job_id, username)
    ]
    return item
