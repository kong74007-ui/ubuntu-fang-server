"""SQLite persistence, leases, and checkpoints for AI editing V2."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from typing import Any, Callable, Iterator

from .ai_edit_v2_schema import FAILURE_STATES, STATE_TRANSITIONS, TERMINAL_STATES


SCHEMA_VERSION = 2
DEFAULT_DB_NAME = "ai_edit_v2.db"
WORKER_STATES = tuple(
    state
    for state in STATE_TRANSITIONS
    if state
    not in {
        "created",
        "validating",
        "quoting",
        "precharging",
    }
)


def _db_path(db_path: str | None = None) -> str:
    return db_path or os.environ.get("AI_EDIT_V2_DB") or os.path.join(
        os.path.dirname(os.path.dirname(__file__)), DEFAULT_DB_NAME
    )


def open_store(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def _connection(db_path: str | None = None) -> Iterator[sqlite3.Connection]:
    conn = open_store(_db_path(db_path))
    try:
        yield conn
    finally:
        conn.close()


def init_db(db_path: str | None = None) -> None:
    path = _db_path(db_path)
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with _connection(path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS edit_v2_schema_meta(
                id INTEGER PRIMARY KEY CHECK(id = 1),
                version INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS edit_v2_jobs(
                id TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                quote_id TEXT NOT NULL,
                predecessor_job_id TEXT REFERENCES edit_v2_jobs(id),
                status TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                director_plan_json TEXT,
                checkpoint_json TEXT NOT NULL DEFAULT '[]',
                lease_owner TEXT,
                lease_until INTEGER,
                error_code TEXT,
                output_cos_key TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                UNIQUE(owner, idempotency_key)
            );

            CREATE TABLE IF NOT EXISTS edit_v2_materials(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                upload_id TEXT UNIQUE,
                owner TEXT NOT NULL,
                kind TEXT NOT NULL,
                purpose TEXT NOT NULL,
                reference_mode TEXT,
                semantic_label TEXT,
                source TEXT,
                cos_key TEXT NOT NULL,
                filename TEXT,
                declared_content_type TEXT,
                mime_type TEXT,
                etag TEXT,
                size_bytes INTEGER,
                duration_ms INTEGER,
                width INTEGER,
                height INTEGER,
                reference_analysis_json TEXT,
                status TEXT NOT NULL DEFAULT 'ready',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS edit_v2_job_materials(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL REFERENCES edit_v2_jobs(id) ON DELETE CASCADE,
                material_id INTEGER NOT NULL REFERENCES edit_v2_materials(id),
                purpose TEXT NOT NULL,
                exclusion_reason TEXT,
                created_at INTEGER NOT NULL,
                UNIQUE(job_id, material_id, purpose)
            );

            CREATE TABLE IF NOT EXISTS edit_v2_stage_attempts(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL REFERENCES edit_v2_jobs(id) ON DELETE CASCADE,
                stage TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                status TEXT NOT NULL,
                provider_task_id TEXT,
                input_summary_json TEXT,
                output_summary_json TEXT,
                error_code TEXT,
                started_at INTEGER NOT NULL,
                finished_at INTEGER,
                UNIQUE(job_id, stage, attempt)
            );

            CREATE TABLE IF NOT EXISTS edit_v2_provider_jobs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL REFERENCES edit_v2_jobs(id) ON DELETE CASCADE,
                provider TEXT NOT NULL,
                capability TEXT NOT NULL,
                provider_task_id TEXT NOT NULL,
                status TEXT NOT NULL,
                is_primary INTEGER NOT NULL DEFAULT 1,
                is_fallback INTEGER NOT NULL DEFAULT 0,
                input_summary_json TEXT,
                output_cos_key TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                UNIQUE(provider, provider_task_id)
            );

            CREATE TABLE IF NOT EXISTS edit_v2_provider_events(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL REFERENCES edit_v2_jobs(id) ON DELETE CASCADE,
                provider TEXT NOT NULL,
                provider_task_id TEXT NOT NULL,
                normalized_status TEXT NOT NULL,
                fingerprint TEXT NOT NULL UNIQUE,
                received_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS edit_v2_quotes(
                id TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                draft_hash TEXT NOT NULL,
                min_points INTEGER NOT NULL,
                max_points INTEGER NOT NULL,
                breakdown_json TEXT NOT NULL,
                price_version TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS edit_v2_billing(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL REFERENCES edit_v2_jobs(id) ON DELETE CASCADE,
                transaction_key TEXT NOT NULL UNIQUE,
                operation TEXT NOT NULL,
                amount INTEGER NOT NULL,
                status TEXT NOT NULL,
                response_json TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS edit_v2_render_artifacts(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id TEXT NOT NULL REFERENCES edit_v2_jobs(id) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                version INTEGER NOT NULL,
                cos_key TEXT NOT NULL,
                validation_json TEXT,
                cleanup_status TEXT,
                created_at INTEGER NOT NULL,
                UNIQUE(job_id, kind, version)
            );

            CREATE INDEX IF NOT EXISTS idx_edit_v2_jobs_claim
                ON edit_v2_jobs(status, lease_until, created_at);
            CREATE INDEX IF NOT EXISTS idx_edit_v2_materials_owner
                ON edit_v2_materials(owner, created_at);
            CREATE INDEX IF NOT EXISTS idx_edit_v2_attempts_job
                ON edit_v2_stage_attempts(job_id, stage, attempt);
            """
        )
        job_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(edit_v2_jobs)")
        }
        if "predecessor_job_id" not in job_columns:
            conn.execute(
                """ALTER TABLE edit_v2_jobs
                   ADD COLUMN predecessor_job_id TEXT REFERENCES edit_v2_jobs(id)"""
            )
        conn.execute(
            """INSERT INTO edit_v2_schema_meta(id,version,updated_at)
               VALUES(1,?,0)
               ON CONFLICT(id) DO UPDATE SET version=excluded.version""",
            (SCHEMA_VERSION,),
        )


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def create_job(
    owner: str,
    payload: dict[str, Any],
    quote_id: str,
    idempotency_key: str,
    now: int,
    *,
    uuid_factory: Callable[[], Any] = uuid.uuid4,
    db_path: str | None = None,
    material_bindings: list[dict[str, Any]] | None = None,
    predecessor_job_id: str | None = None,
) -> dict[str, Any]:
    payload_json = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    requested_bindings = sorted(
        (int(binding["material_id"]), str(binding["purpose"]))
        for binding in (material_bindings or [])
    )
    with _connection(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = conn.execute(
                "SELECT * FROM edit_v2_jobs WHERE owner=? AND idempotency_key=?",
                (owner, idempotency_key),
            ).fetchone()
            if existing is not None:
                existing_bindings = [
                    (int(row["material_id"]), str(row["purpose"]))
                    for row in conn.execute(
                        """SELECT material_id,purpose FROM edit_v2_job_materials
                           WHERE job_id=? ORDER BY material_id,purpose""",
                        (existing["id"],),
                    ).fetchall()
                ]
                existing_payload = json.dumps(
                    json.loads(existing["payload_json"]),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                if (
                    existing["quote_id"] != quote_id
                    or existing["predecessor_job_id"] != predecessor_job_id
                    or existing_payload != payload_json
                    or existing_bindings != requested_bindings
                ):
                    raise ValueError("idempotency_conflict")
                conn.commit()
                return dict(existing)
            job_id = str(uuid_factory())
            conn.execute(
                """INSERT INTO edit_v2_jobs(
                       id,owner,idempotency_key,quote_id,predecessor_job_id,
                       status,payload_json,checkpoint_json,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    job_id,
                    owner,
                    idempotency_key,
                    quote_id,
                    predecessor_job_id,
                    "created",
                    payload_json,
                    "[]",
                    now,
                    now,
                ),
            )
            for material_id, purpose in requested_bindings:
                material = conn.execute(
                    """SELECT purpose FROM edit_v2_materials
                       WHERE id=? AND owner=? AND status='ready'""",
                    (material_id, owner),
                ).fetchone()
                if material is None or material["purpose"] != purpose:
                    raise ValueError("material_not_available")
                conn.execute(
                    """INSERT INTO edit_v2_job_materials(
                           job_id,material_id,purpose,created_at
                       ) VALUES(?,?,?,?)""",
                    (job_id, material_id, purpose, now),
                )
            row = conn.execute(
                "SELECT * FROM edit_v2_jobs WHERE id=?", (job_id,)
            ).fetchone()
            conn.commit()
            return dict(row)
        except Exception:
            conn.rollback()
            raise


def bind_job_materials(
    job_id: str, owner: str, bindings: list[dict[str, Any]], now: int, *, db_path: str | None = None
) -> None:
    with _connection(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            job = conn.execute(
                "SELECT id FROM edit_v2_jobs WHERE id=? AND owner=?", (job_id, owner)
            ).fetchone()
            if job is None:
                raise ValueError("job_not_found")
            for binding in bindings:
                material_id = int(binding["material_id"])
                material = conn.execute(
                    "SELECT purpose FROM edit_v2_materials WHERE id=? AND owner=? AND status='ready'",
                    (material_id, owner),
                ).fetchone()
                if material is None or material["purpose"] != binding["purpose"]:
                    raise ValueError("material_not_available")
                conn.execute(
                    """INSERT OR IGNORE INTO edit_v2_job_materials(
                           job_id,material_id,purpose,created_at
                       ) VALUES(?,?,?,?)""",
                    (job_id, material_id, binding["purpose"], now),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def claim_next_job(
    worker_id: str,
    lease_seconds: int,
    now: int,
    *,
    db_path: str | None = None,
) -> dict[str, Any] | None:
    placeholders = ",".join("?" for _ in WORKER_STATES)
    with _connection(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                f"""SELECT * FROM edit_v2_jobs
                    WHERE status IN ({placeholders})
                      AND (lease_until IS NULL OR lease_until<=?)
                    ORDER BY created_at,id LIMIT 1""",
                (*WORKER_STATES, now),
            ).fetchone()
            if row is None:
                conn.commit()
                return None
            changed = conn.execute(
                """UPDATE edit_v2_jobs
                   SET lease_owner=?,lease_until=?,updated_at=?
                   WHERE id=? AND (lease_until IS NULL OR lease_until<=?)""",
                (worker_id, now + lease_seconds, now, row["id"], now),
            ).rowcount
            if changed != 1:
                conn.rollback()
                return None
            claimed = conn.execute(
                "SELECT * FROM edit_v2_jobs WHERE id=?", (row["id"],)
            ).fetchone()
            conn.commit()
            return _row_dict(claimed)
        except Exception:
            conn.rollback()
            raise


def renew_lease(
    job_id: str,
    worker_id: str,
    lease_seconds: int,
    now: int,
    *,
    db_path: str | None = None,
) -> bool:
    with _connection(db_path) as conn:
        changed = conn.execute(
            """UPDATE edit_v2_jobs
               SET lease_until=?,updated_at=?
               WHERE id=? AND lease_owner=? AND lease_until>?""",
            (now + lease_seconds, now, job_id, worker_id, now),
        ).rowcount
        return changed == 1


def transition(
    job_id: str,
    expected: str,
    target: str,
    checkpoint: dict[str, Any],
    now: int,
    *,
    db_path: str | None = None,
) -> bool:
    allowed = expected not in TERMINAL_STATES and (
        target in FAILURE_STATES or target in STATE_TRANSITIONS.get(expected, ())
    )
    if not allowed:
        return False
    with _connection(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT status,checkpoint_json FROM edit_v2_jobs WHERE id=?", (job_id,)
            ).fetchone()
            if row is None or row["status"] != expected:
                conn.rollback()
                return False
            checkpoints = json.loads(row["checkpoint_json"] or "[]")
            checkpoints.append(
                {
                    "version": len(checkpoints) + 1,
                    "state": target,
                    "at": now,
                    "data": checkpoint,
                }
            )
            changed = conn.execute(
                """UPDATE edit_v2_jobs
                   SET status=?,checkpoint_json=?,lease_owner=NULL,lease_until=NULL,updated_at=?
                   WHERE id=? AND status=?""",
                (
                    target,
                    json.dumps(checkpoints, ensure_ascii=False, separators=(",", ":")),
                    now,
                    job_id,
                    expected,
                ),
            ).rowcount
            if changed != 1:
                conn.rollback()
                return False
            if target in TERMINAL_STATES:
                conn.execute(
                    """UPDATE edit_v2_materials
                       SET reference_analysis_json=NULL,updated_at=?
                       WHERE reference_mode='style_only'
                         AND id IN (
                             SELECT material_id FROM edit_v2_job_materials WHERE job_id=?
                         )""",
                    (now, job_id),
                )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise


def record_stage_attempt(
    job_id: str,
    stage: str,
    attempt: int,
    status: str,
    started_at: int,
    *,
    finished_at: int | None = None,
    provider_task_id: str | None = None,
    input_summary: dict[str, Any] | None = None,
    output_summary: dict[str, Any] | None = None,
    error_code: str | None = None,
    db_path: str | None = None,
) -> int:
    with _connection(db_path) as conn:
        cursor = conn.execute(
            """INSERT INTO edit_v2_stage_attempts(
                   job_id,stage,attempt,status,provider_task_id,input_summary_json,
                   output_summary_json,error_code,started_at,finished_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                job_id,
                stage,
                attempt,
                status,
                provider_task_id,
                json.dumps(input_summary, ensure_ascii=False) if input_summary is not None else None,
                json.dumps(output_summary, ensure_ascii=False) if output_summary is not None else None,
                error_code,
                started_at,
                finished_at,
            ),
        )
        return int(cursor.lastrowid)


def record_provider_event(
    job_id: str,
    provider: str,
    provider_task_id: str,
    normalized_status: str,
    fingerprint: str,
    received_at: int,
    *,
    db_path: str | None = None,
) -> bool:
    with _connection(db_path) as conn:
        try:
            conn.execute(
                """INSERT INTO edit_v2_provider_events(
                       job_id,provider,provider_task_id,normalized_status,fingerprint,received_at
                   ) VALUES(?,?,?,?,?,?)""",
                (
                    job_id,
                    provider,
                    provider_task_id,
                    normalized_status,
                    fingerprint,
                    received_at,
                ),
            )
            return True
        except sqlite3.IntegrityError:
            return False
