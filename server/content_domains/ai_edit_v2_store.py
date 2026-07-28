"""SQLite persistence, leases, and checkpoints for AI editing V2."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from typing import Any, Callable, Iterator

from .ai_edit_v2_schema import FAILURE_STATES, STATE_TRANSITIONS, TERMINAL_STATES


SCHEMA_VERSION = 5
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
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=10000")
        deadline = time.monotonic() + 10
        while True:
            try:
                mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
                if mode != "wal":
                    mode = str(
                        conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
                    ).lower()
                if mode != "wal":
                    raise sqlite3.OperationalError("failed to enable WAL journal mode")
                break
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                    raise
                time.sleep(0.025)
        conn.execute("PRAGMA foreign_keys=ON")
        return conn
    except Exception:
        conn.close()
        raise


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
                generation_job_id TEXT,
                generation_idempotency_key TEXT,
                generation_request_digest TEXT,
                generation_state TEXT,
                generation_lease_owner TEXT,
                generation_lease_until INTEGER,
                generation_retry_at INTEGER,
                generation_provider_request_id TEXT,
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
                provider_reference TEXT,
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
                reference TEXT,
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
                received_at INTEGER NOT NULL,
                lease_owner TEXT,
                lease_until INTEGER
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
            CREATE UNIQUE INDEX IF NOT EXISTS idx_edit_v2_generated_idempotency
                ON edit_v2_materials(owner, semantic_label)
                WHERE source='gpt_image';
            CREATE INDEX IF NOT EXISTS idx_edit_v2_attempts_job
                ON edit_v2_stage_attempts(job_id, stage, attempt);
            """
        )
        conn.execute("BEGIN IMMEDIATE")
        try:
            job_columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(edit_v2_jobs)")
            }
            if "predecessor_job_id" not in job_columns:
                conn.execute(
                    """ALTER TABLE edit_v2_jobs
                       ADD COLUMN predecessor_job_id TEXT REFERENCES edit_v2_jobs(id)"""
                )
            material_columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(edit_v2_materials)")
            }
            provider_job_columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(edit_v2_provider_jobs)")
            }
            if "reference" not in provider_job_columns:
                conn.execute(
                    "ALTER TABLE edit_v2_provider_jobs ADD COLUMN reference TEXT"
                )
            conn.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_edit_v2_provider_reference
                   ON edit_v2_provider_jobs(provider, reference)
                   WHERE reference IS NOT NULL"""
            )
            attempt_columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(edit_v2_stage_attempts)")
            }
            if "provider_reference" not in attempt_columns:
                conn.execute(
                    "ALTER TABLE edit_v2_stage_attempts ADD COLUMN provider_reference TEXT"
                )
            conn.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_edit_v2_attempt_reference
                   ON edit_v2_stage_attempts(provider_reference)
                   WHERE provider_reference IS NOT NULL"""
            )
            provider_event_columns = {
                row["name"]
                for row in conn.execute(
                    "PRAGMA table_info(edit_v2_provider_events)"
                )
            }
            if "lease_owner" not in provider_event_columns:
                conn.execute(
                    "ALTER TABLE edit_v2_provider_events ADD COLUMN lease_owner TEXT"
                )
            if "lease_until" not in provider_event_columns:
                conn.execute(
                    "ALTER TABLE edit_v2_provider_events ADD COLUMN lease_until INTEGER"
                )
            generation_columns = {
                "generation_job_id": "TEXT",
                "generation_idempotency_key": "TEXT",
                "generation_request_digest": "TEXT",
                "generation_state": "TEXT",
                "generation_lease_owner": "TEXT",
                "generation_lease_until": "INTEGER",
                "generation_retry_at": "INTEGER",
                "generation_provider_request_id": "TEXT",
            }
            for column, column_type in generation_columns.items():
                if column not in material_columns:
                    conn.execute(
                        f"ALTER TABLE edit_v2_materials ADD COLUMN {column} {column_type}"
                    )
            conn.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_edit_v2_generated_request
                   ON edit_v2_materials(
                       owner,generation_job_id,generation_idempotency_key
                   ) WHERE source='gpt_image'"""
            )
            conn.execute(
                """UPDATE edit_v2_materials
                   SET generation_state=CASE status
                       WHEN 'ready' THEN 'ready'
                       WHEN 'pending' THEN 'unknown_submission'
                       WHEN 'failed' THEN 'terminal_failed'
                       ELSE 'legacy_reconciliation_required'
                   END
                   WHERE source='gpt_image' AND generation_state IS NULL"""
            )
            conn.execute(
                """INSERT INTO edit_v2_schema_meta(id,version,updated_at)
                   VALUES(1,?,0)
                   ON CONFLICT(id) DO UPDATE SET version=excluded.version""",
                (SCHEMA_VERSION,),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise


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


def _generation_label(job_id: str, idempotency_key: str) -> str:
    digest = hashlib.sha256(f"{job_id}\0{idempotency_key}".encode("utf-8")).hexdigest()
    return f"generation:{digest}"


def find_generated_material(
    owner: str,
    job_id: str,
    idempotency_key: str,
    *,
    db_path: str | None = None,
) -> dict[str, Any] | None:
    label = _generation_label(job_id, idempotency_key)
    with _connection(db_path) as conn:
        row = conn.execute(
            """SELECT * FROM edit_v2_materials
               WHERE owner=? AND source='gpt_image' AND semantic_label=?""",
            (owner, label),
        ).fetchone()
    if row is None:
        return None
    return {**dict(row), "job_id": row["generation_job_id"] or job_id}


def _generated_scope(
    owner: str, job_id: str, idempotency_key: str, cos_key: str
) -> str:
    owner_hash = hashlib.sha256(owner.encode("utf-8")).hexdigest()[:16]
    expected_prefix = f"ai-edit-v2/{owner_hash}/{job_id}/generated/"
    if not isinstance(cos_key, str) or not cos_key.startswith(expected_prefix):
        raise ValueError("generated_material_job_scope_invalid")
    return _generation_label(job_id, idempotency_key)


def reserve_generated_material(
    *,
    owner: str,
    job_id: str,
    idempotency_key: str,
    cos_key: str,
    request_digest: str,
    lease_owner: str,
    lease_seconds: int,
    now: int,
    db_path: str | None = None,
) -> dict[str, Any]:
    label = _generated_scope(owner, job_id, idempotency_key, cos_key)
    if not isinstance(request_digest, str) or not request_digest:
        raise ValueError("generated_request_digest_invalid")
    if not isinstance(lease_owner, str) or not lease_owner:
        raise ValueError("generated_lease_owner_invalid")
    if int(lease_seconds) <= 0:
        raise ValueError("generated_lease_seconds_invalid")
    now = int(now)
    lease_until = now + int(lease_seconds)
    with _connection(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            job = conn.execute(
                "SELECT id FROM edit_v2_jobs WHERE id=? AND owner=?", (job_id, owner)
            ).fetchone()
            if job is None:
                raise ValueError("generated_material_job_scope_invalid")
            row = conn.execute(
                """SELECT * FROM edit_v2_materials
                   WHERE owner=? AND source='gpt_image' AND semantic_label=?""",
                (owner, label),
            ).fetchone()
            claimed = row is None
            if claimed:
                cursor = conn.execute(
                    """INSERT INTO edit_v2_materials(
                           owner,kind,purpose,semantic_label,source,cos_key,status,
                           generation_job_id,generation_idempotency_key,
                           generation_request_digest,generation_state,
                           generation_lease_owner,generation_lease_until,
                           created_at,updated_at
                       ) VALUES(?,'image','generated',?,'gpt_image',?,'pending',
                                ?,?,?,'pre_submit',?,?,?,?)""",
                    (
                        owner,
                        label,
                        cos_key,
                        job_id,
                        idempotency_key,
                        request_digest,
                        lease_owner,
                        lease_until,
                        now,
                        now,
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM edit_v2_materials WHERE id=?", (cursor.lastrowid,)
                ).fetchone()
                reason = "claimed"
            else:
                if row["cos_key"] != cos_key:
                    raise ValueError("generated_material_idempotency_conflict")
                state = row["generation_state"]
                if row["status"] == "ready" or state == "ready":
                    if (
                        row["generation_request_digest"] is not None
                        and row["generation_request_digest"] != request_digest
                    ):
                        raise ValueError("generated_request_conflict")
                    reason = "ready"
                elif row["status"] == "failed" or state == "terminal_failed":
                    reason = "terminal_failed"
                elif row["generation_request_digest"] is None:
                    if state != "unknown_submission" or row["status"] != "pending":
                        raise ValueError(
                            "generated_legacy_material_requires_reconciliation"
                        )
                    changed = conn.execute(
                        """UPDATE edit_v2_materials
                           SET generation_job_id=?,generation_idempotency_key=?,
                               generation_request_digest=?,
                               generation_lease_owner=?,generation_lease_until=?,
                               generation_retry_at=NULL,updated_at=?
                           WHERE id=? AND status='pending'
                             AND generation_state='unknown_submission'
                             AND generation_request_digest IS NULL""",
                        (
                            job_id,
                            idempotency_key,
                            request_digest,
                            lease_owner,
                            lease_until,
                            now,
                            row["id"],
                        ),
                    ).rowcount
                    claimed = changed == 1
                    reason = "claimed" if claimed else "in_progress"
                    row = conn.execute(
                        "SELECT * FROM edit_v2_materials WHERE id=?", (row["id"],)
                    ).fetchone()
                elif row["generation_request_digest"] != request_digest:
                    raise ValueError("generated_request_conflict")
                elif (
                    row["generation_lease_owner"] is not None
                    and row["generation_lease_until"] is not None
                    and int(row["generation_lease_until"]) > now
                ):
                    reason = "in_progress"
                elif (
                    row["generation_retry_at"] is not None
                    and int(row["generation_retry_at"]) > now
                ):
                    reason = "retry_backoff"
                elif state in {
                    "pre_submit",
                    "submitting",
                    "unknown_submission",
                    "retryable",
                    "provider_confirmed",
                }:
                    changed = conn.execute(
                        """UPDATE edit_v2_materials
                           SET generation_lease_owner=?,generation_lease_until=?,
                               generation_retry_at=NULL,
                               generation_state=CASE
                                   WHEN generation_state='submitting'
                                   THEN 'unknown_submission'
                                   ELSE generation_state
                               END,
                               updated_at=?
                           WHERE id=? AND status='pending'
                             AND generation_request_digest=?
                             AND (generation_lease_until IS NULL
                                  OR generation_lease_until<=?)
                             AND (generation_retry_at IS NULL
                                  OR generation_retry_at<=?)""",
                        (
                            lease_owner,
                            lease_until,
                            now,
                            row["id"],
                            request_digest,
                            now,
                            now,
                        ),
                    ).rowcount
                    claimed = changed == 1
                    reason = "claimed" if claimed else "in_progress"
                    row = conn.execute(
                        "SELECT * FROM edit_v2_materials WHERE id=?", (row["id"],)
                    ).fetchone()
                else:
                    reason = "terminal_failed"
            conn.commit()
            return {
                "claimed": claimed,
                "reason": reason,
                "material": {**dict(row), "job_id": row["generation_job_id"] or job_id},
            }
        except Exception:
            conn.rollback()
            raise


def _update_generated_state(
    *,
    owner: str,
    job_id: str,
    idempotency_key: str,
    lease_owner: str,
    from_states: tuple[str, ...],
    to_state: str,
    now: int,
    db_path: str | None,
    release_lease: bool = False,
    retry_at: int | None = None,
    provider_request_id: str | None = None,
) -> bool:
    label = _generation_label(job_id, idempotency_key)
    placeholders = ",".join("?" for _ in from_states)
    lease_value = None if release_lease else lease_owner
    lease_until_sql = "NULL" if release_lease else "generation_lease_until"
    with _connection(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            changed = conn.execute(
                f"""UPDATE edit_v2_materials
                    SET generation_state=?,generation_lease_owner=?,
                        generation_lease_until={lease_until_sql},
                        generation_retry_at=?,
                        generation_provider_request_id=COALESCE(?,generation_provider_request_id),
                        updated_at=?
                    WHERE owner=? AND source='gpt_image' AND semantic_label=?
                      AND status='pending' AND generation_lease_owner=?
                      AND generation_state IN ({placeholders})""",
                (
                    to_state,
                    lease_value,
                    retry_at,
                    provider_request_id,
                    int(now),
                    owner,
                    label,
                    lease_owner,
                    *from_states,
                ),
            ).rowcount
            conn.commit()
            return changed == 1
        except Exception:
            conn.rollback()
            raise


def mark_generated_material_submitting(
    *,
    owner: str,
    job_id: str,
    idempotency_key: str,
    lease_owner: str,
    now: int,
    db_path: str | None = None,
) -> bool:
    return _update_generated_state(
        owner=owner,
        job_id=job_id,
        idempotency_key=idempotency_key,
        lease_owner=lease_owner,
        from_states=(
            "pre_submit",
            "submitting",
            "unknown_submission",
            "retryable",
            "provider_confirmed",
        ),
        to_state="submitting",
        now=now,
        db_path=db_path,
    )


def mark_generated_material_provider_confirmed(
    *,
    owner: str,
    job_id: str,
    idempotency_key: str,
    lease_owner: str,
    provider_request_id: str,
    now: int,
    db_path: str | None = None,
) -> bool:
    return _update_generated_state(
        owner=owner,
        job_id=job_id,
        idempotency_key=idempotency_key,
        lease_owner=lease_owner,
        from_states=("submitting",),
        to_state="provider_confirmed",
        provider_request_id=provider_request_id,
        now=now,
        db_path=db_path,
    )


def mark_generated_material_recoverable(
    *,
    owner: str,
    job_id: str,
    idempotency_key: str,
    lease_owner: str,
    state: str,
    retry_at: int,
    now: int,
    db_path: str | None = None,
) -> bool:
    allowed_from = {
        "unknown_submission": ("submitting",),
        "retryable": ("pre_submit", "submitting"),
        "provider_confirmed": ("provider_confirmed",),
    }
    if state not in allowed_from:
        raise ValueError("generated_recovery_state_invalid")
    if int(retry_at) < int(now):
        raise ValueError("generated_retry_at_invalid")
    return _update_generated_state(
        owner=owner,
        job_id=job_id,
        idempotency_key=idempotency_key,
        lease_owner=lease_owner,
        from_states=allowed_from[state],
        to_state=state,
        retry_at=int(retry_at),
        release_lease=True,
        now=now,
        db_path=db_path,
    )


def complete_generated_material(
    *,
    owner: str,
    job_id: str,
    idempotency_key: str,
    cos_key: str,
    mime_type: str,
    etag: str,
    size_bytes: int,
    width: int,
    height: int,
    lease_owner: str,
    now: int,
    db_path: str | None = None,
) -> dict[str, Any]:
    label = _generated_scope(owner, job_id, idempotency_key, cos_key)
    expected = {
        "cos_key": cos_key,
        "mime_type": mime_type,
        "etag": etag,
        "size_bytes": int(size_bytes),
        "width": int(width),
        "height": int(height),
    }
    with _connection(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                """SELECT * FROM edit_v2_materials
                   WHERE owner=? AND source='gpt_image' AND semantic_label=?""",
                (owner, label),
            ).fetchone()
            if row is None or row["status"] not in {"pending", "ready"}:
                raise ValueError("generated_material_not_pending")
            if row["status"] == "pending":
                if (
                    row["cos_key"] != cos_key
                    or row["generation_state"] != "provider_confirmed"
                    or row["generation_lease_owner"] != lease_owner
                ):
                    raise ValueError("generated_material_idempotency_conflict")
                conn.execute(
                    """UPDATE edit_v2_materials
                       SET mime_type=?,etag=?,size_bytes=?,width=?,height=?,
                           status='ready',generation_state='ready',
                           generation_lease_owner=NULL,generation_lease_until=NULL,
                           generation_retry_at=NULL,updated_at=?
                       WHERE id=? AND status='pending'
                         AND generation_state='provider_confirmed'
                         AND generation_lease_owner=?""",
                    (
                        mime_type,
                        etag,
                        int(size_bytes),
                        int(width),
                        int(height),
                        int(now),
                        row["id"],
                        lease_owner,
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM edit_v2_materials WHERE id=?", (row["id"],)
                ).fetchone()
            if any(row[key] != value for key, value in expected.items()):
                raise ValueError("generated_material_idempotency_conflict")
            conn.commit()
            return {**dict(row), "job_id": job_id}
        except Exception:
            conn.rollback()
            raise


def fail_generated_material(
    owner: str,
    job_id: str,
    idempotency_key: str,
    *,
    lease_owner: str,
    now: int,
    db_path: str | None = None,
) -> bool:
    label = _generation_label(job_id, idempotency_key)
    with _connection(db_path) as conn:
        changed = conn.execute(
            """UPDATE edit_v2_materials
               SET status='failed',generation_state='terminal_failed',
                   generation_lease_owner=NULL,generation_lease_until=NULL,
                   generation_retry_at=NULL,updated_at=?
               WHERE owner=? AND source='gpt_image' AND semantic_label=?
                 AND status='pending' AND generation_lease_owner=?
                 AND generation_state IN ('pre_submit','submitting')""",
            (int(now), owner, label, lease_owner),
        ).rowcount
        return changed == 1


def create_generated_material(
    *,
    owner: str,
    job_id: str,
    idempotency_key: str,
    cos_key: str,
    mime_type: str,
    etag: str,
    size_bytes: int,
    width: int,
    height: int,
    now: int,
    db_path: str | None = None,
) -> dict[str, Any]:
    reservation = reserve_generated_material(
        owner=owner,
        job_id=job_id,
        idempotency_key=idempotency_key,
        cos_key=cos_key,
        request_digest=hashlib.sha256(
            json.dumps(
                {
                    "cos_key": cos_key,
                    "mime_type": mime_type,
                    "etag": etag,
                    "size_bytes": int(size_bytes),
                    "width": int(width),
                    "height": int(height),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
        lease_owner="legacy-create",
        lease_seconds=60,
        now=now,
        db_path=db_path,
    )
    material = reservation["material"]
    if not reservation["claimed"] and material["status"] not in {"pending", "ready"}:
        raise ValueError("generated_material_not_pending")
    if material["status"] == "ready":
        return complete_generated_material(
            owner=owner,
            job_id=job_id,
            idempotency_key=idempotency_key,
            cos_key=cos_key,
            mime_type=mime_type,
            etag=etag,
            size_bytes=size_bytes,
            width=width,
            height=height,
            lease_owner="legacy-create",
            now=now,
            db_path=db_path,
        )
    if not reservation["claimed"]:
        raise ValueError("generated_material_not_pending")
    mark_generated_material_submitting(
        owner=owner,
        job_id=job_id,
        idempotency_key=idempotency_key,
        lease_owner="legacy-create",
        now=now,
        db_path=db_path,
    )
    mark_generated_material_provider_confirmed(
        owner=owner,
        job_id=job_id,
        idempotency_key=idempotency_key,
        lease_owner="legacy-create",
        provider_request_id="legacy-create",
        now=now,
        db_path=db_path,
    )
    return complete_generated_material(
        owner=owner,
        job_id=job_id,
        idempotency_key=idempotency_key,
        cos_key=cos_key,
        mime_type=mime_type,
        etag=etag,
        size_bytes=size_bytes,
        width=width,
        height=height,
        lease_owner="legacy-create",
        now=now,
        db_path=db_path,
    )


_RESOLUTION_RECORD_FIELDS = {
    "slot_id",
    "semantic_query",
    "time_range",
    "ratio",
    "dimensions",
    "source",
    "asset_id",
    "cos_key",
    "required",
    "selected_score",
    "exclusion_code",
}


def save_material_resolution_records(
    job_id: str,
    records: list[dict[str, Any]],
    now: int,
    *,
    status: str = "succeeded",
    error_code: str | None = None,
    attempt: int | None = None,
    db_path: str | None = None,
) -> int:
    if not isinstance(records, list) or any(
        not isinstance(record, dict) or set(record) != _RESOLUTION_RECORD_FIELDS
        for record in records
    ):
        raise ValueError("unsafe_resolution_record")
    serialized = json.dumps(
        {"material_resolutions": records},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    lowered = serialized.lower()
    if any(
        marker in lowered
        for marker in ('"url"', "provider_url", "signed_url", "://", "?signature=", "?sig=")
    ):
        raise ValueError("unsafe_resolution_record")
    if status not in {"succeeded", "failed"}:
        raise ValueError("material_resolution_status_invalid")
    if (status == "failed") != bool(error_code):
        raise ValueError("material_resolution_error_code_invalid")
    if attempt is not None and (
        not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1
    ):
        raise ValueError("material_resolution_attempt_invalid")
    fingerprint = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    input_summary = json.dumps(
        {"resolution_fingerprint": fingerprint}, separators=(",", ":")
    )
    with _connection(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            existing = None
            if attempt is None:
                latest = conn.execute(
                    """SELECT * FROM edit_v2_stage_attempts
                       WHERE job_id=? AND stage='resolving_assets'
                       ORDER BY attempt DESC LIMIT 1""",
                    (job_id,),
                ).fetchone()
                if latest is not None and latest["status"] == "succeeded":
                    existing = latest
                    attempt = int(latest["attempt"])
                else:
                    attempt = int(latest["attempt"]) + 1 if latest is not None else 1
            else:
                existing = conn.execute(
                    """SELECT * FROM edit_v2_stage_attempts
                       WHERE job_id=? AND stage='resolving_assets' AND attempt=?""",
                    (job_id, attempt),
                ).fetchone()
            if existing is not None:
                if existing["output_summary_json"] is None:
                    existing_input = json.loads(existing["input_summary_json"] or "{}")
                    existing_fingerprint = existing_input.get("resolution_fingerprint")
                    if existing_fingerprint not in (None, fingerprint):
                        raise ValueError("material_resolution_idempotency_conflict")
                    conn.execute(
                        """UPDATE edit_v2_stage_attempts
                           SET status=?,input_summary_json=?,output_summary_json=?,
                               finished_at=?,error_code=?
                           WHERE id=?""",
                        (
                            status,
                            input_summary,
                            serialized,
                            now,
                            error_code,
                            existing["id"],
                        ),
                    )
                    conn.commit()
                    return int(existing["id"])
                existing_json = json.dumps(
                    json.loads(existing["output_summary_json"] or "{}"),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                if (
                    existing_json != serialized
                    or existing["status"] != status
                    or existing["error_code"] != error_code
                ):
                    raise ValueError("material_resolution_idempotency_conflict")
                conn.commit()
                return int(existing["id"])
            cursor = conn.execute(
                """INSERT INTO edit_v2_stage_attempts(
                       job_id,stage,attempt,status,input_summary_json,
                       output_summary_json,error_code,started_at,finished_at
                   ) VALUES(?,'resolving_assets',?,?,?,?,?,?,?)""",
                (
                    job_id,
                    attempt,
                    status,
                    input_summary,
                    serialized,
                    error_code,
                    now,
                    now,
                ),
            )
            conn.commit()
            return int(cursor.lastrowid)
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


def claim_provider_event(
    job_id: str,
    provider: str,
    provider_task_id: str,
    fingerprint: str,
    received_at: int,
    *,
    lease_owner: str,
    lease_seconds: int,
    now: int,
    db_path: str | None = None,
) -> str:
    """Atomically classify a webhook fingerprint as claimed, pending, or processed."""

    if not isinstance(lease_owner, str) or not lease_owner.strip():
        raise ValueError("provider_event_lease_owner_invalid")
    if isinstance(lease_seconds, bool) or int(lease_seconds) <= 0:
        raise ValueError("provider_event_lease_invalid")
    lease_until = int(now) + int(lease_seconds)
    with _connection(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                """SELECT job_id,provider,provider_task_id,normalized_status,
                          lease_until
                   FROM edit_v2_provider_events
                   WHERE fingerprint=?""",
                (fingerprint,),
            ).fetchone()
            if row is not None:
                if (
                    row["job_id"] != job_id
                    or row["provider"] != provider
                    or row["provider_task_id"] != provider_task_id
                ):
                    raise ValueError("provider_event_identity_conflict")
                status = str(row["normalized_status"])
                if status not in {"pending", "processed"}:
                    raise ValueError("provider_event_status_invalid")
                if status == "pending" and (
                    row["lease_until"] is None
                    or int(row["lease_until"]) <= int(now)
                ):
                    changed = conn.execute(
                        """UPDATE edit_v2_provider_events
                           SET lease_owner=?,lease_until=?,received_at=?
                           WHERE fingerprint=? AND normalized_status='pending'
                             AND (lease_until IS NULL OR lease_until<=?)""",
                        (
                            lease_owner,
                            lease_until,
                            received_at,
                            fingerprint,
                            int(now),
                        ),
                    ).rowcount
                    if changed != 1:
                        raise ValueError("provider_event_lease_conflict")
                    conn.commit()
                    return "claimed"
                conn.commit()
                return status
            conn.execute(
                """INSERT INTO edit_v2_provider_events(
                       job_id,provider,provider_task_id,normalized_status,fingerprint,
                       received_at,lease_owner,lease_until
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    job_id,
                    provider,
                    provider_task_id,
                    "pending",
                    fingerprint,
                    received_at,
                    lease_owner,
                    lease_until,
                ),
            )
            conn.commit()
            return "claimed"
        except Exception:
            conn.rollback()
            raise


def mark_provider_event_processed(
    fingerprint: str, *, lease_owner: str, db_path: str | None = None
) -> bool:
    with _connection(db_path) as conn:
        changed = conn.execute(
            """UPDATE edit_v2_provider_events
               SET normalized_status='processed',lease_owner=NULL,lease_until=NULL
               WHERE fingerprint=? AND lease_owner=?
                 AND normalized_status='pending'""",
            (fingerprint, lease_owner),
        ).rowcount
    return changed == 1


def release_pending_provider_event(
    fingerprint: str, *, lease_owner: str, db_path: str | None = None
) -> bool:
    with _connection(db_path) as conn:
        changed = conn.execute(
            """DELETE FROM edit_v2_provider_events
               WHERE fingerprint=? AND lease_owner=?
                 AND normalized_status='pending'""",
            (fingerprint, lease_owner),
        ).rowcount
    return changed == 1


def bind_provider_submission(
    *,
    attempt_id: int,
    job_id: str,
    provider: str,
    capability: str,
    provider_task_id: str,
    reference: str,
    status: str,
    now: int,
    db_path: str | None = None,
) -> dict[str, Any]:
    """Atomically bind a remote task to its durable stage attempt and provider row."""

    values = (job_id, provider, capability, provider_task_id, reference, status)
    if not all(isinstance(value, str) and value.strip() for value in values):
        raise ValueError("provider_submission_invalid")
    with _connection(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            attempt = conn.execute(
                """SELECT job_id,provider_task_id,provider_reference
                   FROM edit_v2_stage_attempts WHERE id=?""",
                (attempt_id,),
            ).fetchone()
            if attempt is None or attempt["job_id"] != job_id:
                raise ValueError("provider_submission_attempt_invalid")
            if attempt["provider_task_id"] not in {None, provider_task_id}:
                raise ValueError("provider_submission_conflict")
            if attempt["provider_reference"] not in {None, reference}:
                raise ValueError("provider_submission_conflict")
            existing = conn.execute(
                """SELECT * FROM edit_v2_provider_jobs
                   WHERE provider=? AND (provider_task_id=? OR reference=?)""",
                (provider, provider_task_id, reference),
            ).fetchall()
            if any(
                row["job_id"] != job_id
                or row["provider_task_id"] != provider_task_id
                or row["reference"] != reference
                for row in existing
            ):
                raise ValueError("provider_submission_conflict")
            if existing:
                conn.execute(
                    """UPDATE edit_v2_provider_jobs
                       SET status=CASE
                               WHEN status IN ('succeeded','failed') THEN status
                               ELSE ?
                           END,
                           updated_at=?
                       WHERE provider=? AND provider_task_id=?""",
                    (status, now, provider, provider_task_id),
                )
            else:
                conn.execute(
                    """INSERT INTO edit_v2_provider_jobs(
                           job_id,provider,capability,provider_task_id,reference,status,
                           is_primary,is_fallback,created_at,updated_at
                       ) VALUES(?,?,?,?,?,?,1,0,?,?)""",
                    (
                        job_id,
                        provider,
                        capability,
                        provider_task_id,
                        reference,
                        status,
                        now,
                        now,
                    ),
                )
            conn.execute(
                """UPDATE edit_v2_stage_attempts
                   SET provider_task_id=?,provider_reference=? WHERE id=?""",
                (provider_task_id, reference, attempt_id),
            )
            row = conn.execute(
                """SELECT * FROM edit_v2_provider_jobs
                   WHERE provider=? AND provider_task_id=?""",
                (provider, provider_task_id),
            ).fetchone()
            conn.commit()
            return dict(row)
        except Exception:
            conn.rollback()
            raise


def find_provider_submission(
    provider: str,
    *,
    provider_task_id: str | None = None,
    reference: str | None = None,
    db_path: str | None = None,
) -> dict[str, Any] | None:
    if bool(provider_task_id) == bool(reference):
        raise ValueError("provider_submission_lookup_invalid")
    column, value = (
        ("provider_task_id", provider_task_id)
        if provider_task_id
        else ("reference", reference)
    )
    with _connection(db_path) as conn:
        row = conn.execute(
            f"SELECT * FROM edit_v2_provider_jobs WHERE provider=? AND {column}=?",
            (provider, value),
        ).fetchone()
    return _row_dict(row)


def claim_provider_submission_reference(
    *,
    attempt_id: int,
    job_id: str,
    reference: str,
    db_path: str | None = None,
) -> bool:
    """Reserve the idempotency reference before the first billable network call."""

    if not isinstance(reference, str) or not reference.strip():
        raise ValueError("provider_submission_reference_invalid")
    with _connection(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                """SELECT job_id,provider_reference FROM edit_v2_stage_attempts
                   WHERE id=?""",
                (attempt_id,),
            ).fetchone()
            if row is None or row["job_id"] != job_id:
                raise ValueError("provider_submission_attempt_invalid")
            if row["provider_reference"] is not None:
                if row["provider_reference"] != reference:
                    raise ValueError("provider_submission_conflict")
                conn.commit()
                return False
            changed = conn.execute(
                """UPDATE edit_v2_stage_attempts SET provider_reference=?
                   WHERE id=? AND provider_reference IS NULL""",
                (reference, attempt_id),
            ).rowcount
            conn.commit()
            return changed == 1
        except Exception:
            conn.rollback()
            raise


def release_provider_submission_reference(
    *,
    attempt_id: int,
    job_id: str,
    reference: str,
    db_path: str | None = None,
) -> bool:
    """CAS-release an unbound reference after a provider definitively rejects POST."""

    with _connection(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            changed = conn.execute(
                """UPDATE edit_v2_stage_attempts SET provider_reference=NULL
                   WHERE id=? AND job_id=? AND provider_reference=?
                     AND provider_task_id IS NULL""",
                (attempt_id, job_id, reference),
            ).rowcount
            conn.commit()
            return changed == 1
        except Exception:
            conn.rollback()
            raise


def find_stage_submission(
    attempt_id: int, *, db_path: str | None = None
) -> dict[str, Any] | None:
    with _connection(db_path) as conn:
        row = conn.execute(
            """SELECT job_id,provider_task_id,provider_reference
               FROM edit_v2_stage_attempts WHERE id=?""",
            (attempt_id,),
        ).fetchone()
    return _row_dict(row)
