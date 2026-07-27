# -*- coding: utf-8 -*-
"""AI 智能剪辑的持久化状态、素材关系与计费预占。"""
import os
import pathlib
import sqlite3
import time
from contextlib import closing


DEFAULT_DB = pathlib.Path(__file__).resolve().parents[1] / "ai_edit.db"

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS edit_jobs(
        job_id INTEGER PRIMARY KEY, username TEXT NOT NULL, style TEXT NOT NULL,
        renderer TEXT NOT NULL, stage TEXT NOT NULL DEFAULT 'created',
        provider_job_id TEXT, provider_status TEXT, edit_plan_json TEXT,
        provider_cost REAL, output_cos_key TEXT, error_code TEXT, error_detail TEXT,
        created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS edit_materials(
        id TEXT PRIMARY KEY, username TEXT NOT NULL, kind TEXT NOT NULL,
        role TEXT NOT NULL, origin TEXT NOT NULL DEFAULT 'uploaded',
        cos_key TEXT NOT NULL, content_type TEXT,
        size_bytes INTEGER, status TEXT NOT NULL DEFAULT 'pending',
        created_at INTEGER, updated_at INTEGER)""",
    """CREATE TABLE IF NOT EXISTS edit_job_assets(
        job_id INTEGER NOT NULL, material_id TEXT NOT NULL, role TEXT NOT NULL,
        PRIMARY KEY(job_id, material_id, role))""",
    """CREATE TABLE IF NOT EXISTS billing_holds(
        job_id INTEGER PRIMARY KEY, username TEXT NOT NULL, points INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'held', created_at INTEGER, updated_at INTEGER)""",
    "CREATE INDEX IF NOT EXISTS idx_edit_jobs_username_created "
    "ON edit_jobs(username, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_edit_materials_username_created "
    "ON edit_materials(username, created_at DESC)",
)

_MIGRATION_COLUMNS = {
    "edit_jobs": {
        "username": "TEXT",
        "style": "TEXT",
        "renderer": "TEXT",
        "stage": "TEXT NOT NULL DEFAULT 'created'",
        "provider_job_id": "TEXT",
        "provider_status": "TEXT",
        "edit_plan_json": "TEXT",
        "provider_cost": "REAL",
        "output_cos_key": "TEXT",
        "error_code": "TEXT",
        "error_detail": "TEXT",
        "created_at": "INTEGER",
        "updated_at": "INTEGER",
    },
    "edit_materials": {
        "username": "TEXT",
        "kind": "TEXT",
        "role": "TEXT",
        "origin": "TEXT NOT NULL DEFAULT 'uploaded'",
        "cos_key": "TEXT",
        "content_type": "TEXT",
        "size_bytes": "INTEGER",
        "status": "TEXT NOT NULL DEFAULT 'pending'",
        "created_at": "INTEGER",
        "updated_at": "INTEGER",
    },
    "edit_job_assets": {
        "job_id": "INTEGER",
        "material_id": "TEXT",
        "role": "TEXT",
    },
    "billing_holds": {
        "username": "TEXT",
        "points": "INTEGER",
        "status": "TEXT NOT NULL DEFAULT 'held'",
        "created_at": "INTEGER",
        "updated_at": "INTEGER",
    },
}


def _path(path=None):
    return pathlib.Path(path or os.environ.get("AI_EDIT_DB") or DEFAULT_DB)


def _connect(path):
    connection = sqlite3.connect(str(path), timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def _row_dict(row):
    return dict(row) if row is not None else None


def _add_missing_columns(connection, table, definitions):
    existing = {
        row[1] for row in connection.execute("PRAGMA table_info(%s)" % table)
    }
    for name, definition in definitions.items():
        if name not in existing:
            connection.execute(
                "ALTER TABLE %s ADD COLUMN %s %s" % (table, name, definition)
            )


def init_db(path=None):
    db_path = _path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(_connect(db_path)) as connection:
        for statement in SCHEMA[:4]:
            connection.execute(statement)
        for table, definitions in _MIGRATION_COLUMNS.items():
            _add_missing_columns(connection, table, definitions)
        for statement in SCHEMA[4:]:
            connection.execute(statement)
        connection.commit()
    return db_path


def create_edit_job(path, job_id, username, style, renderer, points):
    db_path = init_db(path)
    now = int(time.time())
    with closing(_connect(db_path)) as connection:
        connection.execute(
            """INSERT INTO edit_jobs(
                job_id,username,style,renderer,stage,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?)""",
            (int(job_id), str(username), str(style), str(renderer), "created", now, now),
        )
        connection.execute(
            """INSERT INTO billing_holds(
                job_id,username,points,status,created_at,updated_at
            ) VALUES(?,?,?,?,?,?)""",
            (int(job_id), str(username), int(points), "held", now, now),
        )
        connection.commit()
    return get_owned_job(db_path, username, job_id)


def get_owned_job(path, username, job_id):
    db_path = init_db(path)
    with closing(_connect(db_path)) as connection:
        row = connection.execute(
            "SELECT * FROM edit_jobs WHERE job_id=? AND username=?",
            (int(job_id), str(username)),
        ).fetchone()
    return _row_dict(row)


def get_job(path, job_id):
    db_path = init_db(path)
    with closing(_connect(db_path)) as connection:
        row = connection.execute(
            "SELECT * FROM edit_jobs WHERE job_id=?", (int(job_id),)
        ).fetchone()
    return _row_dict(row)


def get_job_by_provider_id(path, provider_job_id):
    db_path = init_db(path)
    with closing(_connect(db_path)) as connection:
        row = connection.execute(
            "SELECT * FROM edit_jobs WHERE provider_job_id=?",
            (str(provider_job_id),),
        ).fetchone()
    return _row_dict(row)


def update_stage(path, job_id, stage, error_code=None, error_detail=None):
    db_path = init_db(path)
    with closing(_connect(db_path)) as connection:
        cursor = connection.execute(
            """UPDATE edit_jobs
               SET stage=?,error_code=?,error_detail=?,updated_at=? WHERE job_id=?""",
            (
                str(stage),
                str(error_code)[:80] if error_code is not None else None,
                str(error_detail)[:500] if error_detail is not None else None,
                int(time.time()),
                int(job_id),
            ),
        )
        connection.commit()
        return cursor.rowcount == 1


def set_provider_job(path, job_id, provider_job_id, provider_status):
    db_path = init_db(path)
    provider_job_id = str(provider_job_id or "")
    with closing(_connect(db_path)) as connection:
        row = connection.execute(
            "SELECT provider_job_id FROM edit_jobs WHERE job_id=?", (int(job_id),)
        ).fetchone()
        if row is None:
            return False
        existing = str(row["provider_job_id"] or "")
        if existing and existing != provider_job_id:
            raise ValueError("剪辑任务已绑定其他供应商任务")
        cursor = connection.execute(
            """UPDATE edit_jobs SET provider_job_id=?,provider_status=?,updated_at=?
               WHERE job_id=?""",
            (provider_job_id, str(provider_status or ""), int(time.time()), int(job_id)),
        )
        connection.commit()
        return cursor.rowcount == 1


def create_material(
    path,
    material_id,
    username,
    kind,
    role,
    origin,
    cos_key,
    content_type,
    size_bytes,
):
    db_path = init_db(path)
    now = int(time.time())
    with closing(_connect(db_path)) as connection:
        connection.execute(
            """INSERT INTO edit_materials(
                id,username,kind,role,origin,cos_key,content_type,size_bytes,
                status,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                str(material_id),
                str(username),
                str(kind),
                str(role),
                str(origin),
                str(cos_key),
                str(content_type or ""),
                int(size_bytes) if size_bytes is not None else None,
                "pending",
                now,
                now,
            ),
        )
        row = connection.execute(
            "SELECT * FROM edit_materials WHERE id=?", (str(material_id),)
        ).fetchone()
        connection.commit()
    return _row_dict(row)


def get_owned_material(path, username, material_id):
    db_path = init_db(path)
    with closing(_connect(db_path)) as connection:
        row = connection.execute(
            "SELECT * FROM edit_materials WHERE id=? AND username=?",
            (str(material_id), str(username)),
        ).fetchone()
    return _row_dict(row)


def list_owned_materials(path, username, kinds=("image", "video"), limit=50):
    db_path = init_db(path)
    allowed = tuple(str(kind) for kind in kinds if str(kind))
    if not allowed:
        return []
    limit = max(1, min(100, int(limit or 50)))
    placeholders = ",".join("?" for _ in allowed)
    with closing(_connect(db_path)) as connection:
        rows = connection.execute(
            """SELECT * FROM edit_materials
               WHERE username=? AND status='ready' AND kind IN (%s)
               ORDER BY updated_at DESC,id DESC LIMIT ?""" % placeholders,
            (str(username),) + allowed + (limit,),
        ).fetchall()
    return [_row_dict(row) for row in rows]


def complete_material(path, material_id, username, actual_size):
    db_path = init_db(path)
    with closing(_connect(db_path)) as connection:
        cursor = connection.execute(
            """UPDATE edit_materials SET status='ready',size_bytes=?,updated_at=?
               WHERE id=? AND username=? AND status='pending'
                 AND (size_bytes IS NULL OR size_bytes=?)""",
            (
                int(actual_size),
                int(time.time()),
                str(material_id),
                str(username),
                int(actual_size),
            ),
        )
        connection.commit()
        return cursor.rowcount == 1


def attach_material(path, job_id, material_id, role):
    db_path = init_db(path)
    with closing(_connect(db_path)) as connection:
        cursor = connection.execute(
            """INSERT OR IGNORE INTO edit_job_assets(job_id,material_id,role)
               SELECT j.job_id,m.id,?
               FROM edit_jobs j JOIN edit_materials m ON m.id=?
               WHERE j.job_id=? AND j.username=m.username AND m.status='ready'""",
            (str(role), str(material_id), int(job_id)),
        )
        connection.commit()
        return cursor.rowcount == 1


def _finish_hold(path, job_id, status):
    db_path = init_db(path)
    with closing(_connect(db_path)) as connection:
        cursor = connection.execute(
            """UPDATE billing_holds SET status=?,updated_at=?
               WHERE job_id=? AND status='held'""",
            (str(status), int(time.time()), int(job_id)),
        )
        connection.commit()
        return cursor.rowcount == 1


def confirm_hold(path, job_id):
    return _finish_hold(path, job_id, "confirmed")


def release_hold(path, job_id):
    return _finish_hold(path, job_id, "released")


def safe_finish_hold(path, job_id, succeeded):
    try:
        return confirm_hold(path, job_id) if succeeded else release_hold(path, job_id)
    except Exception:
        return False


def requeue_orphaned_provider_job(path, jobs_db, job_id):
    """有供应商任务号的孤儿任务恢复为 pending；不触碰计费 hold。"""
    detail = get_job(path, job_id)
    if not detail or not detail.get("provider_job_id"):
        return False
    with closing(_connect(pathlib.Path(jobs_db))) as connection:
        cursor = connection.execute(
            "UPDATE jobs SET status='pending',updated_at=? WHERE id=? AND status='running'",
            (int(time.time()), int(job_id)),
        )
        connection.commit()
    if cursor.rowcount != 1:
        return False
    try:
        update_stage(path, job_id, "recovering_render")
    except Exception:
        pass
    return True
