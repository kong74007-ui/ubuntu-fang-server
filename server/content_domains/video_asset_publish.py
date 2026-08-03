# -*- coding: utf-8 -*-
"""Authoritative publication arbitration for hidden AI Edit V3 assets."""

import hashlib
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Protocol


PublicationStatus = Literal[
    "accepted", "stale_generation", "publish_won", "cancel_won"
]


@dataclass(frozen=True)
class PublicationDecision:
    status: PublicationStatus
    current_generation: int
    asset_id: str | None


class AssetPublisher(Protocol):
    def register_generation(
        self,
        mode: str,
        source_job_id: str,
        generation: int,
        idempotency_key: str,
    ) -> PublicationDecision: ...

    def prepare_hidden(
        self,
        mode: str,
        source_job_id: str,
        owner: str,
        object_key: str,
        generation: int,
        idempotency_key: str,
    ) -> PublicationDecision: ...

    def commit_publish(
        self,
        mode: str,
        source_job_id: str,
        generation: int,
        idempotency_key: str,
    ) -> PublicationDecision: ...

    def cancel_publish(
        self,
        mode: str,
        source_job_id: str,
        generation: int,
        idempotency_key: str,
    ) -> PublicationDecision: ...

    def query_decision(
        self,
        mode: str,
        source_job_id: str,
        idempotency_key: str,
    ) -> PublicationDecision | None: ...


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _ensure_column(
    conn: sqlite3.Connection, table: str, column: str, declaration: str
) -> None:
    if column not in _column_names(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def init_schema(conn: sqlite3.Connection) -> None:
    """Apply the publication schema to the existing shared asset database."""
    _ensure_column(conn, "video_assets", "source_job_id", "TEXT")
    _ensure_column(conn, "video_assets", "publication_generation", "INTEGER")
    _ensure_column(conn, "video_assets", "published_at", "INTEGER")
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS uq_video_assets_ai_edit_v3_source_job
           ON video_assets(mode, source_job_id)
           WHERE mode='ai_edit_v3' AND source_job_id IS NOT NULL"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS video_asset_publications(
            mode TEXT NOT NULL,
            source_job_id TEXT NOT NULL,
            current_generation INTEGER NOT NULL,
            prepared_generation INTEGER,
            owner TEXT,
            object_key TEXT,
            verdict TEXT,
            asset_id TEXT,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            PRIMARY KEY(mode, source_job_id)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS video_asset_publication_ops(
            idempotency_key TEXT PRIMARY KEY,
            operation TEXT NOT NULL,
            mode TEXT NOT NULL,
            source_job_id TEXT NOT NULL,
            generation INTEGER,
            request_sha256 TEXT NOT NULL,
            response_json TEXT NOT NULL,
            created_at INTEGER NOT NULL
        )"""
    )


class AssetPublicationService:
    def __init__(self, connect: Callable[[], sqlite3.Connection]):
        self._connect = connect

    @staticmethod
    def _validate_mode(mode: str) -> None:
        if mode != "ai_edit_v3":
            raise ValueError("unsupported_publication_mode")

    @staticmethod
    def _request_sha(payload: dict[str, object]) -> str:
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _serialize(decision: PublicationDecision | None) -> str:
        if decision is None:
            return "null"
        return json.dumps(
            {
                "asset_id": decision.asset_id,
                "current_generation": decision.current_generation,
                "status": decision.status,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def _deserialize(response_json: str) -> PublicationDecision | None:
        value = json.loads(response_json)
        if value is None:
            return None
        return PublicationDecision(
            status=value["status"],
            current_generation=int(value["current_generation"]),
            asset_id=value["asset_id"],
        )

    @staticmethod
    def _publication_decision(
        row: sqlite3.Row | None,
    ) -> PublicationDecision | None:
        if row is None:
            return None
        status = row["verdict"] or "accepted"
        return PublicationDecision(
            status=status,
            current_generation=int(row["current_generation"]),
            asset_id=str(row["asset_id"]) if row["asset_id"] is not None else None,
        )

    @staticmethod
    def _load_publication(
        conn: sqlite3.Connection, mode: str, source_job_id: str
    ) -> sqlite3.Row | None:
        return conn.execute(
            """SELECT * FROM video_asset_publications
               WHERE mode=? AND source_job_id=?""",
            (mode, source_job_id),
        ).fetchone()

    @staticmethod
    def _load_replay(
        conn: sqlite3.Connection,
        *,
        idempotency_key: str,
        operation: str,
        mode: str,
        source_job_id: str,
        generation: int | None,
        request_sha256: str,
    ) -> PublicationDecision | None | object:
        row = conn.execute(
            """SELECT operation,mode,source_job_id,generation,
                      request_sha256,response_json
               FROM video_asset_publication_ops
               WHERE idempotency_key=?""",
            (idempotency_key,),
        ).fetchone()
        if row is None:
            return _NO_REPLAY
        expected = (
            operation,
            mode,
            source_job_id,
            generation,
            request_sha256,
        )
        actual = (
            row["operation"],
            row["mode"],
            row["source_job_id"],
            row["generation"],
            row["request_sha256"],
        )
        if actual != expected:
            raise ValueError("idempotency_conflict")
        return AssetPublicationService._deserialize(row["response_json"])

    @staticmethod
    def _record_operation(
        conn: sqlite3.Connection,
        *,
        idempotency_key: str,
        operation: str,
        mode: str,
        source_job_id: str,
        generation: int | None,
        request_sha256: str,
        decision: PublicationDecision | None,
    ) -> None:
        conn.execute(
            """INSERT INTO video_asset_publication_ops(
                   idempotency_key,operation,mode,source_job_id,generation,
                   request_sha256,response_json,created_at
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                idempotency_key,
                operation,
                mode,
                source_job_id,
                generation,
                request_sha256,
                AssetPublicationService._serialize(decision),
                int(time.time()),
            ),
        )

    def _run(
        self,
        *,
        operation: str,
        mode: str,
        source_job_id: str,
        generation: int | None,
        idempotency_key: str,
        request: dict[str, object],
        mutate: Callable[
            [sqlite3.Connection], PublicationDecision | None
        ],
    ) -> PublicationDecision | None:
        self._validate_mode(mode)
        if not str(idempotency_key or "").strip():
            raise ValueError("idempotency_key_required")
        request_sha256 = self._request_sha(request)
        conn = self._connect()
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            replay = self._load_replay(
                conn,
                idempotency_key=idempotency_key,
                operation=operation,
                mode=mode,
                source_job_id=source_job_id,
                generation=generation,
                request_sha256=request_sha256,
            )
            if replay is not _NO_REPLAY:
                if operation == "query_decision":
                    replay = mutate(conn)
                conn.commit()
                return replay
            decision = mutate(conn)
            self._record_operation(
                conn,
                idempotency_key=idempotency_key,
                operation=operation,
                mode=mode,
                source_job_id=source_job_id,
                generation=generation,
                request_sha256=request_sha256,
                decision=decision,
            )
            conn.commit()
            return decision
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def register_generation(
        self,
        mode: str,
        source_job_id: str,
        generation: int,
        idempotency_key: str,
    ) -> PublicationDecision:
        def mutate(conn: sqlite3.Connection) -> PublicationDecision:
            row = self._load_publication(conn, mode, source_job_id)
            if row is None:
                now = int(time.time())
                conn.execute(
                    """INSERT INTO video_asset_publications(
                           mode,source_job_id,current_generation,
                           created_at,updated_at
                       ) VALUES(?,?,?,?,?)""",
                    (mode, source_job_id, generation, now, now),
                )
            elif generation < int(row["current_generation"]):
                return PublicationDecision(
                    "stale_generation", int(row["current_generation"]), None
                )
            elif row["verdict"] is None and generation > int(
                row["current_generation"]
            ):
                conn.execute(
                    """UPDATE video_asset_publications
                       SET current_generation=?,prepared_generation=NULL,
                           owner=NULL,object_key=NULL,updated_at=?
                       WHERE mode=? AND source_job_id=?""",
                    (
                        generation,
                        int(time.time()),
                        mode,
                        source_job_id,
                    ),
                )
            return self._publication_decision(
                self._load_publication(conn, mode, source_job_id)
            )

        return self._run(
            operation="register_generation",
            mode=mode,
            source_job_id=source_job_id,
            generation=generation,
            idempotency_key=idempotency_key,
            request={
                "generation": generation,
                "mode": mode,
                "source_job_id": source_job_id,
            },
            mutate=mutate,
        )

    def prepare_hidden(
        self,
        mode: str,
        source_job_id: str,
        owner: str,
        object_key: str,
        generation: int,
        idempotency_key: str,
    ) -> PublicationDecision:
        def mutate(conn: sqlite3.Connection) -> PublicationDecision:
            row = self._load_publication(conn, mode, source_job_id)
            now = int(time.time())
            if row is None:
                conn.execute(
                    """INSERT INTO video_asset_publications(
                           mode,source_job_id,current_generation,
                           prepared_generation,owner,object_key,
                           created_at,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        mode,
                        source_job_id,
                        generation,
                        generation,
                        owner,
                        object_key,
                        now,
                        now,
                    ),
                )
            elif generation < int(row["current_generation"]):
                return PublicationDecision(
                    "stale_generation", int(row["current_generation"]), None
                )
            elif row["verdict"] is None:
                if (
                    generation == int(row["current_generation"])
                    and row["prepared_generation"] is not None
                    and generation == int(row["prepared_generation"])
                ):
                    if row["owner"] != owner or row["object_key"] != object_key:
                        raise ValueError("prepared_payload_conflict")
                    return self._publication_decision(row)
                conn.execute(
                    """UPDATE video_asset_publications
                       SET current_generation=?,prepared_generation=?,
                           owner=?,object_key=?,updated_at=?
                       WHERE mode=? AND source_job_id=?""",
                    (
                        generation,
                        generation,
                        owner,
                        object_key,
                        now,
                        mode,
                        source_job_id,
                    ),
                )
            return self._publication_decision(
                self._load_publication(conn, mode, source_job_id)
            )

        return self._run(
            operation="prepare_hidden",
            mode=mode,
            source_job_id=source_job_id,
            generation=generation,
            idempotency_key=idempotency_key,
            request={
                "generation": generation,
                "mode": mode,
                "object_key": object_key,
                "owner": owner,
                "source_job_id": source_job_id,
            },
            mutate=mutate,
        )

    def commit_publish(
        self,
        mode: str,
        source_job_id: str,
        generation: int,
        idempotency_key: str,
    ) -> PublicationDecision:
        def mutate(conn: sqlite3.Connection) -> PublicationDecision:
            row = self._load_publication(conn, mode, source_job_id)
            if row is None:
                return PublicationDecision("accepted", generation, None)
            if generation < int(row["current_generation"]):
                return PublicationDecision(
                    "stale_generation", int(row["current_generation"]), None
                )
            existing = self._publication_decision(row)
            if existing.status in {"publish_won", "cancel_won"}:
                return existing
            if (
                generation != int(row["current_generation"])
                or row["prepared_generation"] is None
                or generation != int(row["prepared_generation"])
                or row["owner"] is None
                or row["object_key"] is None
            ):
                return existing
            now = int(time.time())
            cursor = conn.execute(
                """INSERT INTO video_assets(
                       job_id,username,mode,video_file,phase,status,
                       created_at,updated_at,source_job_id,
                       publication_generation,published_at
                   ) VALUES(NULL,?,?,?,?,?,?,?,?,?,?)""",
                (
                    row["owner"],
                    mode,
                    row["object_key"],
                    "completed",
                    "done",
                    now,
                    now,
                    source_job_id,
                    generation,
                    now,
                ),
            )
            asset_id = str(cursor.lastrowid)
            conn.execute(
                """UPDATE video_asset_publications
                   SET verdict='publish_won',asset_id=?,updated_at=?
                   WHERE mode=? AND source_job_id=?""",
                (asset_id, now, mode, source_job_id),
            )
            return PublicationDecision(
                "publish_won", int(row["current_generation"]), asset_id
            )

        return self._run(
            operation="commit_publish",
            mode=mode,
            source_job_id=source_job_id,
            generation=generation,
            idempotency_key=idempotency_key,
            request={
                "generation": generation,
                "mode": mode,
                "source_job_id": source_job_id,
            },
            mutate=mutate,
        )

    def cancel_publish(
        self,
        mode: str,
        source_job_id: str,
        generation: int,
        idempotency_key: str,
    ) -> PublicationDecision:
        def mutate(conn: sqlite3.Connection) -> PublicationDecision:
            row = self._load_publication(conn, mode, source_job_id)
            now = int(time.time())
            if row is None:
                conn.execute(
                    """INSERT INTO video_asset_publications(
                           mode,source_job_id,current_generation,verdict,
                           created_at,updated_at
                       ) VALUES(?,?,?,'cancel_won',?,?)""",
                    (mode, source_job_id, generation, now, now),
                )
            elif generation < int(row["current_generation"]):
                return PublicationDecision(
                    "stale_generation", int(row["current_generation"]), None
                )
            elif row["verdict"] is None and generation == int(
                row["current_generation"]
            ):
                conn.execute(
                    """UPDATE video_asset_publications
                       SET verdict='cancel_won',updated_at=?
                       WHERE mode=? AND source_job_id=?""",
                    (now, mode, source_job_id),
                )
            return self._publication_decision(
                self._load_publication(conn, mode, source_job_id)
            )

        return self._run(
            operation="cancel_publish",
            mode=mode,
            source_job_id=source_job_id,
            generation=generation,
            idempotency_key=idempotency_key,
            request={
                "generation": generation,
                "mode": mode,
                "source_job_id": source_job_id,
            },
            mutate=mutate,
        )

    def query_decision(
        self,
        mode: str,
        source_job_id: str,
        idempotency_key: str,
    ) -> PublicationDecision | None:
        def mutate(conn: sqlite3.Connection) -> PublicationDecision | None:
            return self._publication_decision(
                self._load_publication(conn, mode, source_job_id)
            )

        return self._run(
            operation="query_decision",
            mode=mode,
            source_job_id=source_job_id,
            generation=None,
            idempotency_key=idempotency_key,
            request={"mode": mode, "source_job_id": source_job_id},
            mutate=mutate,
        )


_NO_REPLAY = object()


def build_sqlite_publisher(db_path: str | os.PathLike[str]) -> AssetPublicationService:
    """Build the shared publisher without leaking SQLite lifecycle into callers."""
    path = Path(db_path).resolve()

    def connect() -> sqlite3.Connection:
        connection = sqlite3.connect(os.fspath(path), timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    with connect() as connection:
        init_schema(connection)
        connection.commit()
    return AssetPublicationService(connect)
