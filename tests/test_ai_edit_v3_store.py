from __future__ import annotations

import base64
import inspect
import os
import sqlite3
import stat
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from server.content_domains.ai_edit_v3 import store as store_module
from server.content_domains.ai_edit_v3.store import (
    StoreConflictError,
    StoreMigrationError,
    StoreConfigurationError,
    V3Store,
    assert_isolated_db,
    init_db,
    open_store,
    resolve_db_path,
)
from server.content_domains.ai_edit_v3.contracts import canonical_json, request_fingerprint


EXPECTED_TABLE_COLUMNS = {
    "edit_v3_schema_meta": (
        "id", "version", "migration_sha256", "created_at", "updated_at",
    ),
    "edit_v3_jobs": (
        "job_id", "environment", "owner_id", "state", "normalized_request_json",
        "request_sha256", "quote_id", "predecessor_job_id", "idempotency_key",
        "worker_id", "fencing_token", "lease_until", "queued_at", "processing_deadline_at",
        "repair_count", "repair_budget_granted_at", "reconciliation_reason", "resume_state",
        "confirmed_preheld_total", "confirmed_refunded_total",
        "delivery_object_key", "asset_id", "result_json", "error_code", "error_json",
        "created_at", "updated_at",
    ),
    "edit_v3_stage_attempts": (
        "id", "job_id", "stage", "attempt", "worker_id", "fencing_token", "status",
        "input_sha256", "started_at", "finished_at", "error_code", "error_json",
    ),
    "edit_v3_checkpoints": (
        "id", "job_id", "stage", "version", "stage_attempt_id", "input_sha256",
        "output_json", "output_sha256", "fencing_token", "created_at",
    ),
    "edit_v3_uploads": (
        "upload_id", "environment", "owner_id", "upload_type", "object_key",
        "declared_mime", "declared_size", "observed_mime", "observed_size", "observed_etag",
        "sha256", "duration_ms", "width", "height", "probe_json", "status", "expires_at",
        "completed_at", "created_at", "updated_at",
    ),
    "edit_v3_materials": (
        "material_id", "environment", "owner_id", "upload_id", "source_kind",
        "source_job_id", "cos_key", "mime_type", "size_bytes", "sha256", "metadata_json",
        "created_at",
    ),
    "edit_v3_job_materials": (
        "job_id", "material_id", "purpose", "ordinal", "created_at",
    ),
    "edit_v3_quotes": (
        "quote_id", "environment", "owner_id", "normalized_request_json",
        "request_sha256", "pricing_version", "template_id", "template_version", "min_points",
        "max_points", "breakdown_json", "expires_at", "created_at",
    ),
    "edit_v3_pricing_versions": (
        "version", "status", "parameters_json", "parameters_sha256", "created_at",
        "published_at", "retired_at",
    ),
    "edit_v3_template_versions": (
        "template_id", "version", "status", "preview_cos_key", "supported_ratios_json",
        "capability_contract_json", "sha256", "created_at", "published_at",
    ),
    "edit_v3_model_calls": (
        "id", "job_id", "stage_attempt_id", "provider", "model", "purpose",
        "prompt_version", "request_schema_sha256", "response_schema_sha256", "request_id",
        "redacted_final_output_json", "validation_json", "usage_json", "elapsed_ms",
        "created_at",
    ),
    "edit_v3_provider_tasks": (
        "id", "job_id", "stage", "stage_attempt_id", "provider", "capability",
        "operation_key", "request_sha256", "external_id", "status", "fencing_token",
        "first_unknown_at", "last_checked_at", "result_json", "created_at", "updated_at",
    ),
    "edit_v3_provider_usage": (
        "id", "job_id", "provider", "capability", "request_id", "usage_json",
        "cost_units", "created_at",
    ),
    "edit_v3_plans": (
        "id", "job_id", "version", "model_call_id", "raw_final_output_json",
        "normalized_plan_json", "plan_sha256", "schema_sha256", "created_at",
    ),
    "edit_v3_render_manifests": (
        "id", "job_id", "attempt", "plan_id", "manifest_json", "manifest_sha256",
        "schema_sha256", "registry_sha256", "renderer_environment_sha256", "created_at",
    ),
    "edit_v3_renders": (
        "id", "job_id", "attempt", "manifest_id", "status", "artifact_cos_key",
        "artifact_sha256", "evidence_json", "performance_json", "log_summary", "cost_units",
        "started_at", "finished_at",
    ),
    "edit_v3_quality_reports": (
        "id", "job_id", "attempt", "render_id", "verdict_json",
        "verdict_sha256", "schema_sha256", "evidence_json", "status", "repairable",
        "created_at",
    ),
    "edit_v3_billing_intents": (
        "id", "environment", "owner_id", "job_id", "operation",
        "external_idempotency_key", "request_sha256", "refund_target_total",
        "request_amount", "status", "first_unknown_at", "last_checked_at",
        "authority_evidence_json", "reason", "resume_state", "created_at", "updated_at",
        "completed_at",
    ),
    "edit_v3_publish_intents": (
        "id", "job_id", "publish_generation", "operation", "external_idempotency_key",
        "object_key", "metadata_sha256", "expected_decision", "status", "fencing_token",
        "first_unknown_at", "last_decision_json", "last_decision_at", "asset_id", "created_at",
        "updated_at",
    ),
}

EXPECTED_FOREIGN_KEYS = {
    "edit_v3_jobs": {
        ("predecessor_job_id", "edit_v3_jobs", "job_id", "RESTRICT"),
        ("quote_id", "edit_v3_quotes", "quote_id", "RESTRICT"),
    },
    "edit_v3_stage_attempts": {("job_id", "edit_v3_jobs", "job_id", "RESTRICT")},
    "edit_v3_checkpoints": {
        ("job_id", "edit_v3_jobs", "job_id", "RESTRICT"),
        ("stage_attempt_id", "edit_v3_stage_attempts", "id", "RESTRICT"),
    },
    "edit_v3_materials": {
        ("upload_id", "edit_v3_uploads", "upload_id", "RESTRICT"),
        ("source_job_id", "edit_v3_jobs", "job_id", "RESTRICT"),
    },
    "edit_v3_job_materials": {
        ("job_id", "edit_v3_jobs", "job_id", "RESTRICT"),
        ("material_id", "edit_v3_materials", "material_id", "RESTRICT"),
    },
    "edit_v3_quotes": {
        ("pricing_version", "edit_v3_pricing_versions", "version", "RESTRICT"),
        ("template_id", "edit_v3_template_versions", "template_id", "RESTRICT"),
        ("template_version", "edit_v3_template_versions", "version", "RESTRICT"),
    },
    "edit_v3_model_calls": {
        ("job_id", "edit_v3_jobs", "job_id", "RESTRICT"),
        ("stage_attempt_id", "edit_v3_stage_attempts", "id", "RESTRICT"),
    },
    "edit_v3_provider_tasks": {
        ("job_id", "edit_v3_jobs", "job_id", "RESTRICT"),
        ("stage_attempt_id", "edit_v3_stage_attempts", "id", "RESTRICT"),
    },
    "edit_v3_provider_usage": {("job_id", "edit_v3_jobs", "job_id", "RESTRICT")},
    "edit_v3_plans": {
        ("job_id", "edit_v3_jobs", "job_id", "RESTRICT"),
        ("model_call_id", "edit_v3_model_calls", "id", "RESTRICT"),
    },
    "edit_v3_render_manifests": {
        ("job_id", "edit_v3_jobs", "job_id", "RESTRICT"),
        ("plan_id", "edit_v3_plans", "id", "RESTRICT"),
    },
    "edit_v3_renders": {
        ("job_id", "edit_v3_jobs", "job_id", "RESTRICT"),
        ("manifest_id", "edit_v3_render_manifests", "id", "RESTRICT"),
    },
    "edit_v3_quality_reports": {
        ("job_id", "edit_v3_jobs", "job_id", "RESTRICT"),
        ("render_id", "edit_v3_renders", "id", "RESTRICT"),
    },
    "edit_v3_billing_intents": {("job_id", "edit_v3_jobs", "job_id", "RESTRICT")},
    "edit_v3_publish_intents": {("job_id", "edit_v3_jobs", "job_id", "RESTRICT")},
}

EXPECTED_DECLARED_INDEXES = {
    "edit_v3_billing_intents_due_idx": (False, ("status", "first_unknown_at", "id")),
    "edit_v3_jobs_claim_idx": (False, ("state", "lease_until", "queued_at", "job_id")),
    "edit_v3_jobs_owner_created_idx": (False, ("environment", "owner_id", "created_at", "job_id")),
    "edit_v3_materials_owner_created_idx": (False, ("environment", "owner_id", "created_at", "material_id")),
    "edit_v3_model_calls_request_idx": (True, ("provider", "request_id")),
    "edit_v3_pricing_one_published_idx": (True, ("status",)),
    "edit_v3_provider_tasks_due_idx": (False, ("status", "first_unknown_at", "id")),
    "edit_v3_provider_tasks_external_idx": (True, ("provider", "external_id")),
    "edit_v3_publish_intents_due_idx": (False, ("status", "first_unknown_at", "id")),
    "edit_v3_quotes_owner_created_idx": (False, ("environment", "owner_id", "created_at", "quote_id")),
    "edit_v3_stage_attempts_one_running_idx": (True, ("job_id",)),
    "edit_v3_template_one_published_idx": (True, ("template_id",)),
    "edit_v3_uploads_owner_created_idx": (False, ("environment", "owner_id", "created_at", "upload_id")),
}


EXPECTED_MIGRATION_SHA256 = (
    "af2f53b9c37aea2ed40cf0ffabef8389541405ff566a30a41216ed299d634f46"
)


class V3StorePathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def assert_code(self, expected, callable_, *args, **kwargs):
        with self.assertRaises(StoreConfigurationError) as caught:
            callable_(*args, **kwargs)
        self.assertEqual(caught.exception.error_code, expected)

    def test_explicit_v3_path_is_required_and_must_be_absolute(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assert_code("v3_db_path_required", resolve_db_path)
        for value in ("", "   "):
            with self.subTest(value=value):
                self.assert_code("v3_db_path_required", resolve_db_path, value)
        for value in (
            "relative/ai_edit_v3.db",
            "C:ai_edit_v3.db",
        ):
            with self.subTest(value=value):
                self.assert_code(
                    "v3_db_path_not_absolute",
                    resolve_db_path,
                    value,
                )

    def test_unc_and_network_syntax_is_rejected_on_every_platform(self):
        for value in (
            r"\\server\share\ai_edit_v3.db",
            "//server/share/ai_edit_v3.db",
        ):
            with self.subTest(value=value):
                self.assert_code("v3_db_path_network", resolve_db_path, value)

    def test_environment_path_is_resolved_without_creating_the_database(self):
        target = self.root / "ai_edit_v3.db"
        with mock.patch.dict(
            os.environ,
            {"AI_EDIT_V3_DB_PATH": os.fspath(target)},
            clear=True,
        ):
            self.assertEqual(resolve_db_path(), target)
        self.assertFalse(target.exists())

    def test_same_normalized_path_is_rejected_before_sqlite_is_called(self):
        path = self.root / "shared.db"
        with mock.patch.object(sqlite3, "connect") as connect:
            self.assert_code(
                "v2_v3_db_same_file",
                assert_isolated_db,
                path,
                path,
            )
        connect.assert_not_called()
        self.assertFalse(path.exists())

    def test_hardlink_alias_to_v2_is_rejected_without_opening_v2(self):
        v2 = self.root / "ai_edit_v2.db"
        v2.write_bytes(b"v2 must never be opened")
        hardlink = self.root / "ai_edit_v3.db"
        os.link(v2, hardlink)

        with mock.patch.object(sqlite3, "connect") as connect:
            self.assert_code(
                "v2_v3_db_same_file",
                assert_isolated_db,
                hardlink,
                v2,
            )
        connect.assert_not_called()

    def test_symlink_alias_to_v2_is_rejected_when_supported(self):
        v2 = self.root / "ai_edit_v2.db"
        v2.write_bytes(b"v2 must never be opened")
        alias = self.root / "ai_edit_v3.db"
        try:
            alias.symlink_to(v2)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")

        self.assert_code(
            "v3_db_path_reparse",
            assert_isolated_db,
            alias,
            v2,
        )

    def test_leaf_and_parent_reparse_evidence_is_not_lost_during_resolution(self):
        parent = self.root / "parent"
        parent.mkdir()
        leaf = parent / "ai_edit_v3.db"
        leaf.touch()
        real_lstat = Path.lstat
        reparse = SimpleNamespace(
            st_mode=stat.S_IFREG,
            st_file_attributes=getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400),
        )

        def leaf_reparse(path):
            return reparse if path == leaf else real_lstat(path)

        with mock.patch.object(Path, "lstat", leaf_reparse):
            self.assert_code("v3_db_path_reparse", resolve_db_path, leaf)

        leaf.unlink()

        def parent_reparse(path):
            return reparse if path == parent else real_lstat(path)

        with mock.patch.object(Path, "lstat", parent_reparse):
            self.assert_code(
                "v3_db_path_reparse",
                resolve_db_path,
                parent / "new.db",
            )

    def test_v2_parent_reparse_evidence_fails_closed(self):
        v3 = self.root / "ai_edit_v3.db"
        parent = self.root / "v2-parent"
        parent.mkdir()
        v2 = parent / "ai_edit_v2.db"
        v2.touch()
        real_lstat = Path.lstat
        reparse = SimpleNamespace(
            st_mode=stat.S_IFDIR,
            st_file_attributes=getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400),
        )

        def parent_reparse(path):
            return reparse if path == parent else real_lstat(path)

        with mock.patch.object(Path, "lstat", parent_reparse):
            self.assert_code("v2_db_path_reparse", assert_isolated_db, v3, v2)

    def test_parent_directory_alias_is_compared_before_creation(self):
        real = self.root / "real"
        real.mkdir()
        alias = self.root / "alias"
        try:
            alias.symlink_to(real, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"directory symlinks unavailable: {exc}")

        v3 = alias / "shared.db"
        v2 = real / "shared.db"
        self.assert_code(
            "v3_db_path_reparse",
            assert_isolated_db,
            v3,
            v2,
        )
        self.assertFalse(v3.exists())

    def test_v2_identity_with_missing_parent_fails_closed(self):
        v3 = self.root / "ai_edit_v3.db"
        v2 = self.root / "missing" / "ai_edit_v2.db"
        self.assert_code(
            "v2_db_identity_unknown",
            assert_isolated_db,
            v3,
            v2,
        )
        self.assertFalse(v3.exists())

    def test_network_filesystems_are_rejected_before_db_creation(self):
        v2 = self.root / "ai_edit_v2.db"
        remote_types = (
            "nfs",
            "nfs4",
            "cifs",
            "smb3",
            "fuse.sshfs",
            "cosfs",
            "fuse.s3fs",
            "gcsfuse",
            "fuse.rclone",
            "9p",
            "ceph",
            "glusterfs",
        )
        for fs_type in remote_types:
            target = self.root / f"{fs_type.replace('.', '-')}.db"
            with self.subTest(fs_type=fs_type):
                with mock.patch.object(
                    store_module,
                    "_filesystem_type_for_path",
                    return_value=fs_type,
                ):
                    self.assert_code(
                        "v3_db_network_filesystem",
                        init_db,
                        target,
                        v2_db_path=v2,
                    )
                self.assertFalse(target.exists())

    def test_unknown_filesystem_identity_fails_closed_before_creation(self):
        target = self.root / "ai_edit_v3.db"
        v2 = self.root / "ai_edit_v2.db"
        with mock.patch.object(
            store_module,
            "_filesystem_type_for_path",
            return_value=None,
        ):
            self.assert_code(
                "v3_db_filesystem_unknown",
                init_db,
                target,
                v2_db_path=v2,
            )
        self.assertFalse(target.exists())

    def test_v2_path_is_required_when_no_explicit_or_configured_value_exists(self):
        target = self.root / "ai_edit_v3.db"
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assert_code("v2_db_path_required", init_db, target)
        self.assertFalse(target.exists())

    def test_configured_v2_path_is_compared_without_opening_it(self):
        v2 = self.root / "ai_edit_v2.db"
        v2.write_bytes(b"not a sqlite database and must stay unopened")
        v3 = self.root / "ai_edit_v3.db"
        with mock.patch.dict(
            os.environ,
            {"AI_EDIT_V2_DB": os.fspath(v2)},
            clear=True,
        ):
            with mock.patch.object(
                store_module,
                "open_store",
                side_effect=RuntimeError("stop after isolation"),
            ) as open_v3:
                with self.assertRaisesRegex(RuntimeError, "stop after isolation"):
                    init_db(v3)
        open_v3.assert_called_once_with(v3)
        self.assertEqual(v2.read_bytes(), b"not a sqlite database and must stay unopened")


class V3StoreSchemaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.db = self.root / "ai_edit_v3.db"
        self.v2 = self.root / "ai_edit_v2.db"
        self.v2.write_bytes(b"V2 identity marker; never open")

    def initialize(self):
        init_db(self.db, v2_db_path=self.v2)

    def raw_connection(self):
        connection = sqlite3.connect(self.db, isolation_level=None)
        self.addCleanup(connection.close)
        return connection

    def write_schema_meta(self, version, migration_sha256):
        connection = self.raw_connection()
        connection.execute(
            """CREATE TABLE edit_v3_schema_meta(
                id INTEGER PRIMARY KEY CHECK(id = 1),
                version INTEGER NOT NULL,
                migration_sha256 TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )"""
        )
        connection.execute(
            "INSERT INTO edit_v3_schema_meta VALUES(1,?,?,1,1)",
            (version, migration_sha256),
        )

    def test_schema_v1_has_exact_frozen_table_column_fk_and_index_manifest(self):
        self.initialize()
        connection = open_store(self.db)
        self.addCleanup(connection.close)

        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
            if not row["name"].startswith("sqlite_")
        }
        self.assertEqual(tables, set(EXPECTED_TABLE_COLUMNS))
        self.assertFalse(any(name.startswith("edit_v2_") for name in tables))
        for table, expected_columns in EXPECTED_TABLE_COLUMNS.items():
            with self.subTest(table=table):
                actual_columns = tuple(
                    row["name"] for row in connection.execute(f"PRAGMA table_info({table})")
                )
                self.assertEqual(actual_columns, expected_columns)
                actual_foreign_keys = {
                    (row["from"], row["table"], row["to"], row["on_delete"])
                    for row in connection.execute(f"PRAGMA foreign_key_list({table})")
                }
                self.assertEqual(
                    actual_foreign_keys,
                    EXPECTED_FOREIGN_KEYS.get(table, set()),
                )

        declared = {
            row["name"]: row["sql"]
            for row in connection.execute(
                """SELECT name,sql FROM sqlite_master
                   WHERE type='index' AND name LIKE 'edit_v3_%' AND sql IS NOT NULL"""
            )
        }
        self.assertEqual(set(declared), set(EXPECTED_DECLARED_INDEXES))
        for index_name, (expected_unique, expected_columns) in EXPECTED_DECLARED_INDEXES.items():
            with self.subTest(index=index_name):
                table_name = connection.execute(
                    "SELECT tbl_name FROM sqlite_master WHERE type='index' AND name=?",
                    (index_name,),
                ).fetchone()[0]
                index_row = next(
                    row
                    for row in connection.execute(f"PRAGMA index_list({table_name})")
                    if row["name"] == index_name
                )
                self.assertEqual(bool(index_row["unique"]), expected_unique)
                actual_columns = tuple(
                    row["name"]
                    for row in connection.execute(f"PRAGMA index_info({index_name})")
                )
                self.assertEqual(actual_columns, expected_columns)

        meta = connection.execute("SELECT * FROM edit_v3_schema_meta").fetchone()
        self.assertEqual(meta["id"], 1)
        self.assertEqual(meta["version"], 1)
        self.assertEqual(meta["migration_sha256"], EXPECTED_MIGRATION_SHA256)

    def test_every_connection_has_wal_foreign_keys_busy_timeout_and_mapping_rows(self):
        self.initialize()
        first = open_store(self.db)
        second = open_store(self.db)
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        for connection in (first, second):
            with self.subTest(connection=id(connection)):
                self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
                self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
                self.assertGreaterEqual(
                    connection.execute("PRAGMA busy_timeout").fetchone()[0],
                    10_000,
                )
                self.assertEqual(
                    connection.execute("SELECT 1 AS answer").fetchone()["answer"],
                    1,
                )

    def test_schema_enforces_refund_billing_state_and_foreign_key_constraints(self):
        self.initialize()
        connection = open_store(self.db)
        self.addCleanup(connection.close)
        sha = "a" * 64
        connection.execute(
            """INSERT INTO edit_v3_pricing_versions(
                   version,status,parameters_json,parameters_sha256,created_at
               ) VALUES(?,?,?,?,?)""",
            ("price-v1", "published", "{}", sha, 1),
        )
        connection.execute(
            """INSERT INTO edit_v3_quotes(
                   quote_id,environment,owner_id,normalized_request_json,request_sha256,
                   pricing_version,min_points,max_points,breakdown_json,
                   expires_at,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            ("quote-1", "test", "alice", "{}", sha, "price-v1", 5, 10, "{}", 100, 1),
        )
        job_values = (
            "job-1", "test", "alice", "created_draft", "{}", sha,
            "quote-1", "client-key", 5, 0, 1, 1,
        )
        connection.execute(
            """INSERT INTO edit_v3_jobs(
                   job_id,environment,owner_id,state,normalized_request_json,request_sha256,
                   quote_id,idempotency_key,confirmed_preheld_total,confirmed_refunded_total,
                   created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            job_values,
        )
        invalid_refund = list(job_values)
        invalid_refund[0] = "job-invalid-refund"
        invalid_refund[7] = "client-key-2"
        invalid_refund[9] = 6
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO edit_v3_jobs(
                       job_id,environment,owner_id,state,normalized_request_json,request_sha256,
                       quote_id,idempotency_key,confirmed_preheld_total,confirmed_refunded_total,
                       created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                invalid_refund,
            )
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO edit_v3_billing_intents(
                       id,environment,owner_id,job_id,operation,
                       external_idempotency_key,request_sha256,refund_target_total,
                       request_amount,status,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                ("bill-1", "test", "alice", "job-1", "settle", "external-1", sha, 0, 5, "pending", 1, 1),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO edit_v3_materials(
                       material_id,environment,owner_id,upload_id,source_kind,cos_key,
                       mime_type,size_bytes,sha256,metadata_json,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                ("mat-1", "test", "alice", "missing-upload", "uploaded", "test/key", "image/png", 1, sha, "{}", 1),
            )

    def test_stage_attempt_status_check_accepts_the_exact_frozen_v1_set(self):
        self.initialize()
        connection = open_store(self.db)
        self.addCleanup(connection.close)
        sha = "a" * 64
        connection.execute(
            """INSERT INTO edit_v3_pricing_versions(
                   version,status,parameters_json,parameters_sha256,created_at
               ) VALUES(?,?,?,?,?)""",
            ("price-v1", "published", "{}", sha, 1),
        )
        connection.execute(
            """INSERT INTO edit_v3_quotes(
                   quote_id,environment,owner_id,normalized_request_json,request_sha256,
                   pricing_version,min_points,max_points,breakdown_json,
                   expires_at,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            ("quote-1", "test", "alice", "{}", sha, "price-v1", 5, 10, "{}", 100, 1),
        )
        connection.execute(
            """INSERT INTO edit_v3_jobs(
                   job_id,environment,owner_id,state,normalized_request_json,request_sha256,
                   quote_id,idempotency_key,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            ("job-1", "test", "alice", "created_draft", "{}", sha, "quote-1", "key-1", 1, 1),
        )
        frozen_statuses = (
            "running",
            "completed",
            "failed",
            "skipped",
            "aborted_lease_lost",
        )
        self.assertEqual(
            tuple(store_module.SCHEMA_MANIFEST["stage_attempt_statuses"]),
            frozen_statuses,
        )
        for attempt, status in enumerate(frozen_statuses, start=1):
            connection.execute(
                """INSERT INTO edit_v3_stage_attempts(
                       id,job_id,stage,attempt,worker_id,fencing_token,status,
                       input_sha256,started_at,finished_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    f"attempt-{attempt}",
                    "job-1",
                    f"stage-{attempt}",
                    attempt,
                    "worker-1",
                    1,
                    status,
                    sha,
                    1,
                    None if status == "running" else 2,
                ),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO edit_v3_stage_attempts(
                       id,job_id,stage,attempt,worker_id,fencing_token,status,
                       input_sha256,started_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                ("attempt-invalid", "job-1", "stage-invalid", 6, "worker-1", 1, "cancelled", sha, 1),
            )

    def test_future_mismatched_and_metadata_less_schemas_fail_closed(self):
        cases = (
            (2, EXPECTED_MIGRATION_SHA256, "v3_schema_future_version"),
            (1, "f" * 64, "v3_schema_migration_sha_mismatch"),
        )
        for version, migration_sha, error_code in cases:
            with self.subTest(error_code=error_code):
                db = self.root / f"{error_code}.db"
                self.db = db
                self.write_schema_meta(version, migration_sha)
                with self.assertRaises(StoreMigrationError) as caught:
                    self.initialize()
                self.assertEqual(caught.exception.error_code, error_code)

        self.db = self.root / "metadata-less.db"
        connection = self.raw_connection()
        connection.execute("CREATE TABLE edit_v3_jobs(dummy TEXT)")
        with self.assertRaises(StoreMigrationError) as caught:
            self.initialize()
        self.assertEqual(caught.exception.error_code, "v3_schema_metadata_missing")
        remaining = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'edit_v3_%'"
            )
        }
        self.assertEqual(remaining, {"edit_v3_jobs"})

    def test_valid_meta_cannot_mask_a_partial_or_tampered_v1_schema(self):
        self.write_schema_meta(1, EXPECTED_MIGRATION_SHA256)
        with self.assertRaises(StoreMigrationError) as caught:
            self.initialize()
        self.assertEqual(caught.exception.error_code, "v3_schema_manifest_mismatch")

    def test_migration_rolls_back_all_ddl_and_metadata_on_failure(self):
        def fail_mid_migration(connection):
            connection.execute("CREATE TABLE edit_v3_atomic_probe(value TEXT)")
            raise RuntimeError("injected migration failure")

        with mock.patch.object(store_module, "_apply_schema_v1", side_effect=fail_mid_migration):
            with self.assertRaisesRegex(RuntimeError, "injected migration failure"):
                self.initialize()
        connection = self.raw_connection()
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'edit_v3_%'"
            )
        }
        self.assertEqual(tables, set())

    def test_post_open_identity_revalidation_rejects_a_path_swap(self):
        initial = sqlite3.connect(self.db)
        initial.close()
        original_identity = self.db.stat()

        def swap_before_return(path):
            moved = self.root / "original.db"
            os.replace(path, moved)
            connection = sqlite3.connect(path, isolation_level=None)
            connection.row_factory = sqlite3.Row
            return connection

        with mock.patch.object(store_module, "open_store", side_effect=swap_before_return):
            with self.assertRaises(StoreConfigurationError) as caught:
                self.initialize()
        self.assertEqual(caught.exception.error_code, "v3_db_identity_changed")
        self.assertNotEqual(
            (self.db.stat().st_dev, self.db.stat().st_ino),
            (original_identity.st_dev, original_identity.st_ino),
        )


class V3StoreMigrationRaceTests(unittest.TestCase):
    THREADS = 8
    ROUNDS = 50

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.v2 = self.root / "ai_edit_v2.db"
        self.v2.write_bytes(b"V2 identity marker; never open")

    def prepare_v0(self, path, journal_mode):
        if journal_mode is None:
            return
        connection = sqlite3.connect(path, isolation_level=None)
        try:
            actual = connection.execute(
                f"PRAGMA journal_mode={journal_mode}"
            ).fetchone()[0]
            self.assertEqual(actual.lower(), journal_mode.lower())
        finally:
            connection.close()

    def snapshot(self, path):
        connection = open_store(path)
        try:
            tables = tuple(
                row[0]
                for row in connection.execute(
                    """SELECT name FROM sqlite_master
                       WHERE type='table' AND name NOT LIKE 'sqlite_%'
                       ORDER BY name"""
                )
            )
            indexes = tuple(
                row[0]
                for row in connection.execute(
                    """SELECT name FROM sqlite_master
                       WHERE type='index' AND name LIKE 'edit_v3_%' AND sql IS NOT NULL
                       ORDER BY name"""
                )
            )
            meta = tuple(
                connection.execute(
                    "SELECT version,migration_sha256 FROM edit_v3_schema_meta"
                ).fetchone()
            )
            pragmas = (
                connection.execute("PRAGMA journal_mode").fetchone()[0],
                connection.execute("PRAGMA foreign_keys").fetchone()[0],
                connection.execute("PRAGMA busy_timeout").fetchone()[0],
            )
            return tables, indexes, meta, pragmas
        finally:
            connection.close()

    def run_mode(self, mode, journal_mode):
        expected_tables = tuple(sorted(EXPECTED_TABLE_COLUMNS))
        expected_indexes = tuple(sorted(EXPECTED_DECLARED_INDEXES))
        for round_number in range(self.ROUNDS):
            path = self.root / f"{mode}-{round_number}.db"
            self.prepare_v0(path, journal_mode)
            barrier = threading.Barrier(self.THREADS)
            snapshots = []
            failures = []
            lock = threading.Lock()

            def initialize_worker():
                try:
                    barrier.wait(timeout=10)
                    init_db(path, v2_db_path=self.v2)
                    result = self.snapshot(path)
                    with lock:
                        snapshots.append(result)
                except BaseException as exc:
                    with lock:
                        failures.append(exc)

            threads = [
                threading.Thread(target=initialize_worker, daemon=True)
                for _ in range(self.THREADS)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=20)
            self.assertFalse(
                any(thread.is_alive() for thread in threads),
                f"{mode} round {round_number} did not finish",
            )
            self.assertEqual(
                failures,
                [],
                f"{mode} round {round_number} failures: {failures!r}",
            )
            self.assertEqual(len(snapshots), self.THREADS)
            for tables, indexes, meta, pragmas in snapshots:
                self.assertEqual(tables, expected_tables)
                self.assertEqual(indexes, expected_indexes)
                self.assertEqual(meta, (1, EXPECTED_MIGRATION_SHA256))
                self.assertEqual(pragmas[0], "wal")
                self.assertEqual(pragmas[1], 1)
                self.assertGreaterEqual(pragmas[2], 10_000)
            self.assertTrue(all(snapshot == snapshots[0] for snapshot in snapshots))

    def test_fresh_database_8_threads_50_rounds(self):
        self.run_mode("fresh", None)

    def test_native_delete_v0_database_8_threads_50_rounds(self):
        self.run_mode("delete", "delete")

    def test_existing_wal_v0_database_8_threads_50_rounds(self):
        self.run_mode("wal", "wal")


class V3StorePrimitiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.db = self.root / "ai_edit_v3.db"
        self.v2 = self.root / "ai_edit_v2.db"
        self.v2.write_bytes(b"V2 identity marker; never open")
        self.store = V3Store(
            self.db,
            v2_db_path=self.v2,
            environment="test",
        )

    def seed_pricing_and_quote(self, owner="alice", quote_id="quote-1"):
        self.store.insert_pricing_version(
            "price-v1",
            {"base": 5, "per_minute": 2},
            status="published",
            created_at=1_000,
            published_at=1_001,
        )
        return self.store.insert_quote(
            owner,
            quote_id,
            {
                "input_type": "uploaded_video",
                "source_upload_id": "upload-1",
                "ratio": "auto",
                "creation_mode": "ai_auto",
                "material_asset_ids": [],
            },
            pricing_version="price-v1",
            min_points=5,
            max_points=9,
            breakdown={"base": 5, "variable_max": 4},
            expires_at=9_999,
            created_at=1_010,
        )

    def seed_job(self, job_id, owner, created_at, *, environment="test", quote_id="quote-1"):
        connection = open_store(self.db)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO edit_v3_jobs(
                       job_id,environment,owner_id,state,normalized_request_json,request_sha256,
                       quote_id,idempotency_key,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    job_id,
                    environment,
                    owner,
                    "created_draft",
                    '{"a":1,"z":2}',
                    request_fingerprint({"a": 1, "z": 2}),
                    quote_id,
                    f"key-{environment}-{owner}-{job_id}",
                    created_at,
                    created_at,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def test_pricing_and_quotes_are_canonical_immutable_and_owner_scoped(self):
        pricing = self.store.insert_pricing_version(
            "price-v1",
            {"z": 2, "a": "汉"},
            status="published",
            created_at=100,
            published_at=101,
        )
        self.assertEqual(pricing["parameters_json"], '{"a":"汉","z":2}')
        self.assertEqual(
            pricing["parameters_sha256"],
            request_fingerprint({"z": 2, "a": "汉"}),
        )
        self.assertEqual(self.store.get_published_pricing_version(), pricing)
        self.assertEqual(
            self.store.insert_pricing_version(
                "price-v1",
                {"a": "汉", "z": 2},
                status="published",
                created_at=100,
                published_at=101,
            ),
            pricing,
        )
        with self.assertRaises(StoreConflictError) as caught:
            self.store.insert_pricing_version(
                "price-v1",
                {"a": "changed"},
                status="published",
                created_at=100,
                published_at=101,
            )
        self.assertEqual(caught.exception.error_code, "immutable_identity_conflict")

        request = {"z": 2, "a": "汉"}
        quote = self.store.insert_quote(
            "alice",
            "quote-1",
            request,
            pricing_version="price-v1",
            min_points=1,
            max_points=3,
            breakdown={"variable": 2, "base": 1},
            expires_at=500,
            created_at=200,
        )
        self.assertEqual(quote["normalized_request_json"], '{"a":"汉","z":2}')
        self.assertEqual(quote["breakdown_json"], '{"base":1,"variable":2}')
        self.assertEqual(quote["request_sha256"], request_fingerprint(request))
        self.assertEqual(self.store.get_quote("alice", "quote-1"), quote)
        self.assertIsNone(self.store.get_quote("bob", "quote-1"))
        self.assertIsNone(
            self.store.get_quote("alice", "quote-1", environment="production")
        )
        replay = self.store.insert_quote(
            "alice",
            "quote-1",
            {"a": "汉", "z": 2},
            pricing_version="price-v1",
            min_points=1,
            max_points=3,
            breakdown={"base": 1, "variable": 2},
            expires_at=500,
            created_at=200,
        )
        self.assertEqual(replay, quote)
        self.assertIsNone(
            self.store.insert_quote(
                "bob",
                "quote-1",
                request,
                pricing_version="price-v1",
                min_points=1,
                max_points=3,
                breakdown={"base": 1, "variable": 2},
                expires_at=500,
                created_at=200,
            )
        )
        with self.assertRaises(StoreConflictError):
            self.store.insert_quote(
                "alice",
                "quote-1",
                request,
                pricing_version="price-v1",
                min_points=1,
                max_points=4,
                breakdown={"base": 1, "variable": 3},
                expires_at=500,
                created_at=200,
            )

    def test_upload_completion_and_materials_are_immutable_and_owner_bound(self):
        upload = self.store.insert_upload(
            "alice",
            "upload-1",
            upload_type="material_image",
            object_key="test/ai-edit-v3/alice/upload-1",
            declared_mime="image/png",
            declared_size=12,
            expires_at=5_000,
            created_at=1_000,
        )
        self.assertEqual(upload["status"], "pending")
        self.assertIsNone(
            self.store.insert_upload(
                "bob",
                "upload-1",
                upload_type="material_image",
                object_key="test/ai-edit-v3/bob/upload-1",
                declared_mime="image/png",
                declared_size=12,
                expires_at=5_000,
                created_at=1_000,
            )
        )
        self.assertEqual(
            self.store.insert_upload(
                "alice",
                "upload-1",
                upload_type="material_image",
                object_key="test/ai-edit-v3/alice/upload-1",
                declared_mime="image/png",
                declared_size=12,
                expires_at=5_000,
                created_at=1_000,
            ),
            upload,
        )
        completed = self.store.complete_upload(
            "alice",
            "upload-1",
            observed_mime="image/png",
            observed_size=12,
            observed_etag="etag-1",
            sha256="a" * 64,
            duration_ms=None,
            width=2,
            height=3,
            probe={"format": "png", "safe": True},
            completed_at=1_100,
        )
        self.assertEqual(completed["probe_json"], '{"format":"png","safe":true}')
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(
            self.store.complete_upload(
                "alice",
                "upload-1",
                observed_mime="image/png",
                observed_size=12,
                observed_etag="etag-1",
                sha256="a" * 64,
                duration_ms=None,
                width=2,
                height=3,
                probe={"safe": True, "format": "png"},
                completed_at=1_100,
            ),
            completed,
        )
        self.assertIsNone(
            self.store.complete_upload(
                "bob",
                "upload-1",
                observed_mime="image/png",
                observed_size=12,
                observed_etag="etag-1",
                sha256="a" * 64,
                duration_ms=None,
                width=2,
                height=3,
                probe={},
                completed_at=1_100,
            )
        )
        with self.assertRaises(StoreConflictError):
            self.store.complete_upload(
                "alice",
                "upload-1",
                observed_mime="image/png",
                observed_size=13,
                observed_etag="etag-2",
                sha256="b" * 64,
                duration_ms=None,
                width=2,
                height=3,
                probe={},
                completed_at=1_100,
            )

        material = self.store.insert_material(
            "alice",
            "material-1",
            source_kind="uploaded",
            cos_key="test/ai-edit-v3/alice/material-1.png",
            mime_type="image/png",
            size_bytes=12,
            sha256="a" * 64,
            metadata={"z": 2, "a": 1},
            created_at=1_200,
            upload_id="upload-1",
        )
        self.assertEqual(material["metadata_json"], '{"a":1,"z":2}')
        self.assertEqual(
            self.store.insert_material(
                "alice",
                "material-1",
                source_kind="uploaded",
                cos_key="test/ai-edit-v3/alice/material-1.png",
                mime_type="image/png",
                size_bytes=12,
                sha256="a" * 64,
                metadata={"a": 1, "z": 2},
                created_at=1_200,
                upload_id="upload-1",
            ),
            material,
        )
        self.assertIsNone(
            self.store.insert_material(
                "bob",
                "material-bob",
                source_kind="uploaded",
                cos_key="test/ai-edit-v3/bob/material.png",
                mime_type="image/png",
                size_bytes=12,
                sha256="a" * 64,
                metadata={},
                created_at=1_200,
                upload_id="upload-1",
            )
        )

    def test_job_material_binding_checks_owner_in_the_write_transaction(self):
        self.seed_pricing_and_quote()
        self.seed_job("job-1", "alice", 2_000)
        alice = self.store.insert_material(
            "alice",
            "material-1",
            source_kind="existing",
            cos_key="test/ai-edit-v3/alice/material-1.png",
            mime_type="image/png",
            size_bytes=12,
            sha256="a" * 64,
            metadata={},
            created_at=1_200,
        )
        bound = self.store.bind_job_materials(
            "alice",
            "job-1",
            [{"material_id": alice["material_id"], "purpose": "evidence", "ordinal": 0}],
            created_at=2_100,
        )
        self.assertEqual(len(bound), 1)
        self.assertEqual(
            self.store.bind_job_materials(
                "alice",
                "job-1",
                [{"material_id": "material-1", "purpose": "evidence", "ordinal": 0}],
                created_at=2_100,
            ),
            bound,
        )
        self.assertIsNone(
            self.store.bind_job_materials(
                "bob",
                "job-1",
                [{"material_id": "material-1", "purpose": "evidence", "ordinal": 0}],
                created_at=2_100,
            )
        )

    def test_owner_job_reads_use_stable_bound_keyset_pagination_without_offset(self):
        self.seed_pricing_and_quote()
        expected = (
            ("job-e", 300),
            ("job-d", 300),
            ("job-c", 200),
            ("job-b", 100),
            ("job-a", 100),
        )
        for job_id, created_at in reversed(expected):
            self.seed_job(job_id, "alice", created_at)
        self.seed_job("job-bob", "bob", 999)
        self.seed_job("job-prod", "alice", 999, environment="production")

        traced = []
        real_open_store = open_store

        def traced_open(path):
            connection = real_open_store(path)
            connection.set_trace_callback(traced.append)
            return connection

        seen = []
        cursor = None
        with mock.patch.object(store_module, "open_store", side_effect=traced_open):
            while True:
                page = self.store.list_jobs_for_owner(
                    "alice",
                    limit=2,
                    cursor=cursor,
                )
                seen.extend((item["job_id"], item["created_at"]) for item in page["items"])
                cursor = page["next_cursor"]
                if cursor is None:
                    break
        self.assertEqual(tuple(seen), expected)
        select_sql = "\n".join(statement for statement in traced if "edit_v3_jobs" in statement)
        self.assertNotIn(" OFFSET ", f" {select_sql.upper()} ")
        self.assertIn("environment", select_sql)
        self.assertIn("owner_id", select_sql)
        self.assertIn("created_at <", select_sql)
        self.assertIn("job_id <", select_sql)

        job = self.store.get_job_for_owner("alice", "job-c")
        self.assertEqual(job["normalized_request_json"], '{"a":1,"z":2}')
        self.assertIsNone(self.store.get_job_for_owner("bob", "job-c"))
        self.assertIsNone(
            self.store.get_job_for_owner(
                "alice",
                "job-c",
                environment="production",
            )
        )
        first = self.store.list_jobs_for_owner("alice", limit=1)
        with self.assertRaises(StoreConfigurationError) as caught:
            self.store.list_jobs_for_owner(
                "bob",
                limit=1,
                cursor=first["next_cursor"],
            )
        self.assertEqual(caught.exception.error_code, "job_cursor_scope_mismatch")
        duplicate_key_payload = (
            b'{"created_at":300,"environment":"test","job_id":"job-e",'
            b'"owner_id":"alice","owner_id":"alice"}'
        )
        duplicate_key_cursor = base64.urlsafe_b64encode(duplicate_key_payload).rstrip(
            b"="
        ).decode("ascii")
        with self.assertRaises(StoreConfigurationError) as caught:
            self.store.list_jobs_for_owner(
                "alice",
                limit=1,
                cursor=duplicate_key_cursor,
            )
        self.assertEqual(caught.exception.error_code, "job_cursor_invalid")

    def test_store_public_methods_accept_no_raw_sql_identifiers_or_fragments(self):
        forbidden = {"sql", "table", "table_name", "predicate", "where", "order_by"}
        for method_name in (
            "insert_pricing_version",
            "get_published_pricing_version",
            "insert_quote",
            "get_quote",
            "insert_upload",
            "complete_upload",
            "insert_material",
            "bind_job_materials",
            "get_job_for_owner",
            "list_jobs_for_owner",
        ):
            with self.subTest(method=method_name):
                parameters = set(inspect.signature(getattr(V3Store, method_name)).parameters)
                self.assertTrue(parameters.isdisjoint(forbidden))


if __name__ == "__main__":
    unittest.main()
