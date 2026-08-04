from __future__ import annotations

import base64
import errno
import gc
import inspect
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
import weakref
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from server.content_domains.ai_edit_v3 import contracts as contracts_module
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
    "edit_v3_director_decisions": (
        "id", "job_id", "version", "model_call_id", "prompt_version",
        "raw_output_json", "normalized_decision_json", "decision_sha256",
        "schema_sha256", "candidates_sha256", "created_at",
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
    "edit_v3_director_decisions": {
        ("job_id", "edit_v3_jobs", "job_id", "RESTRICT"),
        ("model_call_id", "edit_v3_model_calls", "id", "RESTRICT"),
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
    "50dd6199f274f98e94083efaf720dc279600a56d004f8ddb9b5c14a4b9dfc73f"
)
V1_MIGRATION_SHA256 = "ac0f6a45cc1e97976dfbfea95f9112e6ccc38802a3a4899f1b8fd435b09d3387"


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
        v2.write_bytes(b"V2 identity marker; never open through SQLite")
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
        v2.write_bytes(b"V2 identity marker; never open through SQLite")
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
                sqlite3,
                "connect",
                side_effect=RuntimeError("stop after isolation"),
            ) as open_v3:
                with self.assertRaisesRegex(RuntimeError, "stop after isolation"):
                    init_db(v3)
        open_v3.assert_called_once()
        connect_target = open_v3.call_args.args[0]
        if sys.platform.startswith("linux"):
            prefix = "file:/proc/self/fd/"
            suffix = f"/{v3.name}?mode=rw&cache=private&vfs=unix"
            self.assertIsInstance(connect_target, str)
            self.assertTrue(connect_target.startswith(prefix), connect_target)
            self.assertTrue(connect_target.endswith(suffix), connect_target)
            descriptor = connect_target[len(prefix) : -len(suffix)]
            self.assertTrue(descriptor.isdecimal(), connect_target)
            self.assertIs(open_v3.call_args.kwargs.get("uri"), True)
        else:
            self.assertEqual(Path(connect_target), v3)
            self.assertNotIn("uri", open_v3.call_args.kwargs)
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
        connection = open_store(self.db, v2_db_path=self.v2)
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
        self.assertEqual(meta["version"], 2)
        self.assertEqual(meta["migration_sha256"], EXPECTED_MIGRATION_SHA256)

    def test_every_connection_has_wal_foreign_keys_busy_timeout_and_mapping_rows(self):
        self.initialize()
        first = open_store(self.db, v2_db_path=self.v2)
        second = open_store(self.db, v2_db_path=self.v2)
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
        connection = open_store(self.db, v2_db_path=self.v2)
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

    def test_material_source_authority_union_is_enforced_by_schema_v1(self):
        self.initialize()
        connection = open_store(self.db, v2_db_path=self.v2)
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
                   pricing_version,min_points,max_points,breakdown_json,expires_at,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            ("quote-1", "test", "alice", "{}", sha, "price-v1", 1, 2, "{}", 9, 1),
        )
        connection.execute(
            """INSERT INTO edit_v3_jobs(
                   job_id,environment,owner_id,state,normalized_request_json,request_sha256,
                   quote_id,idempotency_key,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            ("job-1", "test", "alice", "created_draft", "{}", sha, "quote-1", "key-1", 1, 1),
        )
        for index in range(12):
            connection.execute(
                """INSERT INTO edit_v3_uploads(
                       upload_id,environment,owner_id,upload_type,object_key,declared_mime,
                       declared_size,status,expires_at,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,'completed',?,?,?)""",
                (
                    f"upload-{index}", "test", "alice", "material_image",
                    f"test/authority-{index}.png", "image/png", 1, 9, 1, 1,
                ),
            )

        def insert(material_id, source_kind, upload_id, source_job_id):
            connection.execute(
                """INSERT INTO edit_v3_materials(
                       material_id,environment,owner_id,upload_id,source_kind,source_job_id,
                       cos_key,mime_type,size_bytes,sha256,metadata_json,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    material_id, "test", "alice", upload_id, source_kind, source_job_id,
                    f"test/{material_id}.png", "image/png", 1, sha, "{}", 1,
                ),
            )

        insert("valid-uploaded", "uploaded", "upload-0", None)
        insert("valid-generated", "generated", None, "job-1")
        illegal = (
            ("uploaded", None, None),
            ("uploaded", None, "job-1"),
            ("uploaded", "upload-1", "job-1"),
            ("generated", None, None),
            ("generated", "upload-2", None),
            ("generated", "upload-3", "job-1"),
            ("other", None, None),
            ("other", None, "job-1"),
            ("other", "upload-4", None),
            ("other", "upload-5", "job-1"),
        )
        for index, values in enumerate(illegal):
            with self.subTest(values=values):
                with self.assertRaises(sqlite3.IntegrityError):
                    insert(f"illegal-{index}", *values)

        ddl = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='edit_v3_materials'"
        ).fetchone()[0]
        normalized = " ".join(ddl.split())
        self.assertIn(
            "CHECK((source_kind='uploaded' AND upload_id IS NOT NULL AND source_job_id IS NULL) "
            "OR (source_kind='generated' AND upload_id IS NULL AND source_job_id IS NOT NULL))",
            normalized,
        )

    def test_material_source_union_corruption_is_rejected_on_reinitialization(self):
        self.initialize()
        connection = self.raw_connection()
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute(
            """INSERT INTO edit_v3_materials(
                   material_id,environment,owner_id,upload_id,source_kind,source_job_id,
                   cos_key,mime_type,size_bytes,sha256,metadata_json,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "corrupt-material", "test", "alice", None, "uploaded", None,
                "test/corrupt.png", "image/png", 1, "a" * 64, "{}", 1,
            ),
        )
        connection.close()
        with self.assertRaises(StoreMigrationError) as caught:
            self.initialize()
        self.assertEqual(caught.exception.error_code, "v3_integrity_check_failed")

    def test_stage_attempt_status_check_accepts_the_exact_frozen_v1_set(self):
        self.initialize()
        connection = open_store(self.db, v2_db_path=self.v2)
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
            (3, EXPECTED_MIGRATION_SHA256, "v3_schema_future_version"),
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
        self.write_schema_meta(1, V1_MIGRATION_SHA256)
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
        real_path_identity = store_module._path_identity
        observations = 0

        def changed_identity(path):
            nonlocal observations
            result = real_path_identity(path)
            observations += 1
            if observations == 2:
                return result[0], (result[1][0], result[1][1] + 1)
            return result

        with mock.patch.object(store_module, "_path_identity", side_effect=changed_identity):
            with self.assertRaises(StoreConfigurationError) as caught:
                self.initialize()
        self.assertEqual(caught.exception.error_code, "v3_db_identity_changed")
        self.assertEqual(
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
        connection = open_store(path, v2_db_path=self.v2)
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
                self.assertEqual(meta, (2, EXPECTED_MIGRATION_SHA256))
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

    def test_schema_v1_to_v2_migration_is_serialized_across_threads(self):
        path = self.root / "schema-v1.db"
        connection = sqlite3.connect(path, isolation_level=None)
        try:
            store_module._register_connection_functions(connection)
            connection.execute("BEGIN IMMEDIATE")
            for name in sorted(set(store_module._CREATE_TABLE_SQL) - {"edit_v3_director_decisions"}):
                connection.execute(store_module._CREATE_TABLE_SQL[name])
            for name in sorted(store_module._CREATE_INDEX_SQL):
                connection.execute(store_module._CREATE_INDEX_SQL[name])
            connection.execute(
                "INSERT INTO edit_v3_schema_meta VALUES(1,?,?,?,?)",
                (1, V1_MIGRATION_SHA256, 1, 1),
            )
            connection.commit()
        finally:
            connection.close()
        barrier = threading.Barrier(self.THREADS)
        failures = []
        lock = threading.Lock()
        def migrate():
            try:
                barrier.wait(timeout=10)
                init_db(path, v2_db_path=self.v2)
            except BaseException as exc:
                with lock:
                    failures.append(exc)
        threads = [threading.Thread(target=migrate) for _ in range(self.THREADS)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=20)
        self.assertEqual([], failures)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        final = sqlite3.connect(path)
        try:
            self.assertEqual((2, EXPECTED_MIGRATION_SHA256), final.execute("SELECT version,migration_sha256 FROM edit_v3_schema_meta").fetchone())
            self.assertIsNotNone(final.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='edit_v3_director_decisions'").fetchone())
        finally:
            final.close()


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
        connection = open_store(self.db, v2_db_path=self.v2)
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
        self.assertEqual(caught.exception.error_code, "idempotency_conflict")

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
            cos_key="test/ai-edit-v3/alice/upload-1",
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
                cos_key="test/ai-edit-v3/alice/upload-1",
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
            source_kind="generated",
            cos_key="test/ai-edit-v3/alice/material-1.png",
            mime_type="image/png",
            size_bytes=12,
            sha256="a" * 64,
            metadata={},
            created_at=1_200,
            source_job_id="job-1",
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
        real_open_store = store_module._open_store_ordered

        def traced_open(path, v2_path):
            resolved, connection = real_open_store(path, v2_path)
            connection.set_trace_callback(traced.append)
            return resolved, connection

        seen = []
        cursor = None
        with mock.patch.object(store_module, "_open_store_ordered", side_effect=traced_open):
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

    def test_every_owner_replay_and_authority_select_binds_scope_in_sql(self):
        quote = self.seed_pricing_and_quote()
        self.seed_job("job-1", "alice", 100)
        upload = self.store.insert_upload(
            "alice",
            "upload-scoped",
            upload_type="material_image",
            object_key="test/ai-edit-v3/alice/upload-scoped.png",
            declared_mime="image/png",
            declared_size=12,
            expires_at=5_000,
            created_at=1_000,
        )
        upload = self.store.complete_upload(
            "alice",
            upload["upload_id"],
            observed_mime="image/png",
            observed_size=12,
            observed_etag="etag",
            sha256="a" * 64,
            duration_ms=None,
            width=2,
            height=3,
            probe={},
            completed_at=1_100,
        )
        material = self.store.insert_material(
            "alice",
            "material-scoped",
            source_kind="uploaded",
            upload_id=upload["upload_id"],
            cos_key=upload["object_key"],
            mime_type=upload["observed_mime"],
            size_bytes=upload["observed_size"],
            sha256=upload["sha256"],
            metadata={},
            created_at=1_200,
        )
        self.store.bind_job_materials(
            "alice",
            "job-1",
            [{"material_id": material["material_id"], "purpose": "evidence", "ordinal": 0}],
            created_at=1_300,
        )
        authority_upload = self.store.insert_upload(
            "alice",
            "upload-authority-scoped",
            upload_type="material_image",
            object_key="test/ai-edit-v3/alice/upload-authority-scoped.png",
            declared_mime="image/png",
            declared_size=13,
            expires_at=5_000,
            created_at=1_000,
        )
        authority_upload = self.store.complete_upload(
            "alice",
            authority_upload["upload_id"],
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

        captured = []
        real_execute = store_module._GuardedConnection.execute

        def capture(connection, statement, parameters=()):
            captured.append((statement, tuple(parameters)))
            return real_execute(connection, statement, parameters)

        with mock.patch.object(store_module._GuardedConnection, "execute", capture):
            self.store.insert_quote(
                "alice",
                quote["quote_id"],
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
            self.store.insert_upload(
                "alice",
                upload["upload_id"],
                upload_type="material_image",
                object_key=upload["object_key"],
                declared_mime="image/png",
                declared_size=12,
                expires_at=5_000,
                created_at=1_000,
            )
            self.store.insert_material(
                "alice",
                material["material_id"],
                source_kind="uploaded",
                upload_id=upload["upload_id"],
                cos_key=upload["object_key"],
                mime_type=upload["observed_mime"],
                size_bytes=upload["observed_size"],
                sha256=upload["sha256"],
                metadata={},
                created_at=1_200,
            )
            self.store.insert_material(
                "alice",
                "material-upload-replay-new-id",
                source_kind="uploaded",
                upload_id=upload["upload_id"],
                cos_key=upload["object_key"],
                mime_type=upload["observed_mime"],
                size_bytes=upload["observed_size"],
                sha256=upload["sha256"],
                metadata={},
                created_at=1_200,
            )
            self.store.insert_material(
                "alice",
                "material-authority-scoped",
                source_kind="uploaded",
                upload_id=authority_upload["upload_id"],
                cos_key=authority_upload["object_key"],
                mime_type=authority_upload["observed_mime"],
                size_bytes=authority_upload["observed_size"],
                sha256=authority_upload["sha256"],
                metadata={},
                created_at=1_200,
            )
            self.store.insert_material(
                "alice",
                "material-source-job-scoped",
                source_kind="generated",
                source_job_id="job-1",
                cos_key="test/ai-edit-v3/alice/generated-scoped.png",
                mime_type="image/png",
                size_bytes=1,
                sha256="c" * 64,
                metadata={},
                created_at=1_200,
            )
            self.store.bind_job_materials(
                "alice",
                "job-1",
                [{"material_id": material["material_id"], "purpose": "evidence", "ordinal": 0}],
                created_at=1_300,
            )

        checks = (
            ("edit_v3_quotes", quote["quote_id"]),
            ("edit_v3_uploads", upload["upload_id"]),
            ("edit_v3_uploads", authority_upload["upload_id"]),
            ("edit_v3_materials", material["material_id"]),
            ("edit_v3_materials", upload["upload_id"]),
            ("edit_v3_jobs", "job-1"),
            ("edit_v3_job_materials", "job-1"),
        )
        for table, identity in checks:
            with self.subTest(table=table, identity=identity):
                selects = [
                    (statement, parameters)
                    for statement, parameters in captured
                    if statement.lstrip().upper().startswith("SELECT")
                    and table in statement
                    and identity in parameters
                ]
                self.assertTrue(selects)
                for statement, parameters in selects:
                    compact = "".join(statement.lower().split())
                    self.assertIn("environment=?", compact)
                    self.assertIn("owner_id=?", compact)
                    self.assertIn("test", parameters)
                    self.assertIn("alice", parameters)

    def test_unique_race_reread_preserves_exact_divergent_and_private_replay_semantics(self):
        quote = self.seed_pricing_and_quote()
        self.seed_job("job-race", "alice", 100)
        upload = self.store.insert_upload(
            "alice", "upload-race", upload_type="material_image",
            object_key="test/ai-edit-v3/alice/upload-race.png",
            declared_mime="image/png", declared_size=12,
            expires_at=5_000, created_at=1_000,
        )
        upload = self.store.complete_upload(
            "alice", upload["upload_id"], observed_mime="image/png",
            observed_size=12, observed_etag="etag", sha256="a" * 64,
            duration_ms=None, width=2, height=3, probe={}, completed_at=1_100,
        )
        material = self.store.insert_material(
            "alice", "material-race", source_kind="generated",
            source_job_id="job-race", cos_key="test/ai-edit-v3/alice/generated-race.png",
            mime_type="image/png", size_bytes=12,
            sha256="b" * 64, metadata={}, created_at=1_200,
        )

        class NoRow:
            @staticmethod
            def fetchone():
                return None

        def miss_first_select(table, identity_column, operation):
            real_execute = store_module._GuardedConnection.execute
            missed = False

            def execute(connection, statement, parameters=()):
                nonlocal missed
                compact = "".join(statement.lower().split())
                if (
                    not missed
                    and statement.lstrip().upper().startswith("SELECT")
                    and table in statement
                    and f"{identity_column}=?" in compact
                ):
                    missed = True
                    return NoRow()
                return real_execute(connection, statement, parameters)

            with mock.patch.object(store_module._GuardedConnection, "execute", execute):
                result = operation()
            self.assertTrue(missed)
            return result

        quote_args = dict(
            pricing_version="price-v1", min_points=5, max_points=9,
            breakdown={"base": 5, "variable_max": 4},
            expires_at=9_999, created_at=1_010,
        )
        request = {
            "input_type": "uploaded_video", "source_upload_id": "upload-1",
            "ratio": "auto", "creation_mode": "ai_auto", "material_asset_ids": [],
        }
        self.assertEqual(
            miss_first_select(
                "edit_v3_quotes", "quote_id",
                lambda: self.store.insert_quote("alice", quote["quote_id"], request, **quote_args),
            ),
            quote,
        )
        with self.assertRaises(StoreConflictError) as caught:
            miss_first_select(
                "edit_v3_quotes", "quote_id",
                lambda: self.store.insert_quote(
                    "alice", quote["quote_id"], request, **{**quote_args, "max_points": 10}
                ),
            )
        self.assertEqual(caught.exception.error_code, "idempotency_conflict")

        upload_args = dict(
            upload_type="material_image", object_key=upload["object_key"],
            declared_mime="image/png", declared_size=12,
            expires_at=5_000, created_at=1_000,
        )
        self.assertEqual(
            miss_first_select(
                "edit_v3_uploads", "upload_id",
                lambda: self.store.insert_upload("alice", upload["upload_id"], **upload_args),
            )["upload_id"],
            upload["upload_id"],
        )
        with self.assertRaises(StoreConflictError) as caught:
            miss_first_select(
                "edit_v3_uploads", "upload_id",
                lambda: self.store.insert_upload(
                    "alice", upload["upload_id"], **{**upload_args, "declared_size": 13}
                ),
            )
        self.assertEqual(caught.exception.error_code, "idempotency_conflict")

        material_args = dict(
            source_kind="generated", source_job_id="job-race",
            cos_key="test/ai-edit-v3/alice/generated-race.png", mime_type="image/png",
            size_bytes=12, sha256="b" * 64,
            metadata={}, created_at=1_200,
        )
        self.assertEqual(
            miss_first_select(
                "edit_v3_materials", "material_id",
                lambda: self.store.insert_material(
                    "alice", material["material_id"], **material_args
                ),
            ),
            material,
        )
        with self.assertRaises(StoreConflictError) as caught:
            miss_first_select(
                "edit_v3_materials", "material_id",
                lambda: self.store.insert_material(
                    "alice", material["material_id"],
                    **{**material_args, "metadata": {"changed": True}},
                ),
            )
        self.assertEqual(caught.exception.error_code, "idempotency_conflict")

        self.assertIsNone(
            self.store.insert_quote("bob", quote["quote_id"], request, **quote_args)
        )
        self.assertIsNone(
            self.store.insert_upload(
                "bob", upload["upload_id"],
                **{**upload_args, "object_key": "test/ai-edit-v3/bob/upload-race.png"},
            )
        )


class V3StoreReviewIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.v2 = self.root / "ai_edit_v2.db"
        connection = sqlite3.connect(self.v2, isolation_level=None)
        try:
            self.assertEqual(
                connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0].lower(),
                "delete",
            )
            connection.execute("CREATE TABLE v2_marker(value TEXT NOT NULL)")
            connection.execute("INSERT INTO v2_marker VALUES('unchanged')")
        finally:
            connection.close()

    def assert_code(self, expected, callable_, *args, **kwargs):
        with self.assertRaises(StoreConfigurationError) as caught:
            callable_(*args, **kwargs)
        self.assertEqual(caught.exception.error_code, expected)

    @staticmethod
    def _sidecars(path):
        return tuple(
            sidecar.exists()
            for sidecar in (
                Path(f"{path}-wal"),
                Path(f"{path}-shm"),
                Path(f"{path}-journal"),
            )
        )

    @staticmethod
    def _journal_mode(path):
        connection = sqlite3.connect(path, isolation_level=None)
        try:
            return connection.execute("PRAGMA journal_mode").fetchone()[0].lower()
        finally:
            connection.close()

    def test_assert_and_every_public_open_require_v2_identity_before_connect(self):
        v3 = self.root / "ai_edit_v3.db"
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assert_code("v2_db_path_required", assert_isolated_db, v3, None)
            with mock.patch.object(sqlite3, "connect") as connect:
                self.assert_code("v2_db_path_required", open_store, v3)
            connect.assert_not_called()
        self.assertFalse(v3.exists())

    def test_public_open_rejects_the_v2_file_without_mutating_it(self):
        before = self.v2.read_bytes()
        before_sidecars = self._sidecars(self.v2)
        self.assert_code(
            "v2_v3_db_same_file",
            open_store,
            self.v2,
            v2_db_path=self.v2,
        )
        self.assertEqual(self.v2.read_bytes(), before)
        self.assertEqual(self._journal_mode(self.v2), "delete")
        self.assertEqual(self._sidecars(self.v2), before_sidecars)

    def test_connection_bound_to_another_database_is_rejected_before_wal_or_schema(self):
        requested = self.root / "requested.db"
        other = self.root / "other.db"
        connection = sqlite3.connect(other, isolation_level=None)
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.close()
        other_before = other.read_bytes()
        real_connect = sqlite3.connect

        def wrong_connect(_requested, *args, **kwargs):
            return real_connect(other, isolation_level=None)

        with mock.patch.object(sqlite3, "connect", side_effect=wrong_connect):
            self.assert_code(
                "v3_db_main_handle_mismatch",
                init_db,
                requested,
                v2_db_path=self.v2,
            )

        self.assertTrue(requested.exists())
        self.assertEqual(requested.stat().st_size, 0)
        self.assertEqual(other.read_bytes(), other_before)
        self.assertEqual(self._journal_mode(other), "delete")
        self.assertEqual(self._sidecars(other), (False, False, False))

    def _assert_connect_boundary_hardlink_attack_is_rejected(self, *, restore):
        requested = self.root / ("aba.db" if restore else "swap.db")
        backup = requested.with_suffix(".original")
        v2_before = self.v2.read_bytes()
        real_connect = sqlite3.connect
        attack = {"attempted": False, "swapped": False}

        def attacking_connect(_requested, *args, **kwargs):
            attack["attempted"] = True
            os.replace(requested, backup)
            os.link(self.v2, requested)
            attack["swapped"] = True
            connection = real_connect(requested, isolation_level=None)
            if restore:
                requested.unlink()
                os.replace(backup, requested)
            return connection

        with mock.patch.object(sqlite3, "connect", side_effect=attacking_connect):
            with self.assertRaises(StoreConfigurationError) as caught:
                open_store(requested, v2_db_path=self.v2)
        self.assertIn(
            caught.exception.error_code,
            {"v3_db_identity_changed", "v3_db_main_handle_mismatch"},
        )
        self.assertTrue(attack["attempted"])
        self.assertEqual(self.v2.read_bytes(), v2_before)
        self.assertEqual(self._journal_mode(self.v2), "delete")
        self.assertEqual(self._sidecars(self.v2), (False, False, False))

    def test_hardlink_swap_at_connect_boundary_is_rejected_before_v2_mutation(self):
        self._assert_connect_boundary_hardlink_attack_is_rejected(restore=False)

    def test_aba_hardlink_swap_and_restore_is_rejected_by_actual_handle_identity(self):
        self._assert_connect_boundary_hardlink_attack_is_rejected(restore=True)


class _ReleaseCountingGuard:
    def __init__(self, inner):
        self.inner = inner
        self.release_calls = 0
        self.before_release = None

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def release(self):
        self.release_calls += 1
        if self.before_release is not None:
            self.before_release()
        self.inner.release()


class V3StoreNativeGuardReleaseTests(unittest.TestCase):
    def test_cleanup_owner_records_actions_deduplicates_and_retries_under_lock(self):
        events = []

        class RecordingLock:
            active = False

            def __enter__(self):
                self.active = True
                events.append("lock-enter")

            def __exit__(self, exc_type, exc, traceback):
                events.append("lock-exit")
                self.active = False

        lock = RecordingLock()

        class Resource:
            def close(self):
                self_test.assertTrue(lock.active)
                events.append("close")

        self_test = self
        resource = Resource()
        owner = store_module._CleanupOwner()
        owner.append(resource, "close", description="test resource close")
        owner.append(resource, "close", description="duplicate test resource close")

        self.assertEqual(
            owner.pending_actions,
            ((resource, "close", "test resource close"),),
        )
        with mock.patch.object(store_module, "_SQLITE_OPEN_LOCK", lock):
            owner.retry()

        self.assertEqual(owner.pending_resources, ())
        self.assertEqual(events, ["lock-enter", "close", "lock-exit"])

    def test_windows_bundle_retains_failed_and_pending_handles_for_retry(self):
        close_handle = mock.Mock(side_effect=(1, 0, 1, 1))
        kernel32 = SimpleNamespace(CloseHandle=close_handle)
        fake_ctypes = SimpleNamespace(
            WinDLL=mock.Mock(return_value=kernel32),
            WinError=lambda code: OSError(code, "injected CloseHandle failure"),
            get_last_error=mock.Mock(return_value=6),
            windll=SimpleNamespace(kernel32=kernel32),
            wintypes=SimpleNamespace(BOOL=bool, HANDLE=lambda value: value),
        )
        bundle = store_module._WindowsGuardBundle([11, 22, 33], (1, 2))

        with mock.patch.dict(store_module.sys.modules, {"ctypes": fake_ctypes}):
            with self.assertRaisesRegex(OSError, "CloseHandle failure"):
                bundle.release()
            self.assertEqual(bundle.handles, [11, 22])

            bundle.release()

        self.assertEqual(bundle.handles, [])
        self.assertEqual(
            [call.args[0] for call in close_handle.call_args_list],
            [33, 22, 22, 11],
        )

    def test_linux_bundle_retains_failed_and_pending_descriptors_for_retry(self):
        calls = []
        failed = False

        def close_descriptor(descriptor):
            nonlocal failed
            calls.append(descriptor)
            if descriptor == 11 and not failed:
                failed = True
                raise OSError("injected descriptor close failure")

        bundle = store_module._LinuxGuardBundle(
            11,
            22,
            (1, 2),
            ancestor_fds=[10, 11],
        )
        with mock.patch.object(store_module.os, "close", side_effect=close_descriptor):
            with self.assertRaisesRegex(OSError, "descriptor close failure"):
                bundle.release()
            self.assertEqual(bundle.leaf_fd, -1)
            self.assertEqual(bundle.parent_fd, 11)
            self.assertEqual(bundle.ancestor_fds, [10, 11])

            bundle.release()

        self.assertEqual(bundle.leaf_fd, -1)
        self.assertEqual(bundle.parent_fd, -1)
        self.assertEqual(bundle.ancestor_fds, [])
        self.assertEqual(calls, [22, 11, 11, 10])

    def test_linux_empty_bundle_preserves_explicit_empty_ancestors_and_is_idempotent(self):
        explicit_empty = store_module._LinuxGuardBundle(
            11,
            -1,
            (1, 2),
            ancestor_fds=[],
        )
        invalid_default = store_module._LinuxGuardBundle(-1, -1, (1, 2))

        with mock.patch.object(store_module.os, "close") as close_descriptor:
            explicit_empty.release()
            explicit_empty.release()
            invalid_default.release()
            invalid_default.release()

        self.assertEqual(explicit_empty.ancestor_fds, [])
        self.assertEqual(invalid_default.ancestor_fds, [])
        close_descriptor.assert_not_called()

    def test_linux_main_descriptor_uses_connection_native_handle_when_sqlite_reuses_fd(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp).resolve() / "requested.db"
            v2_path = Path(temp).resolve() / "v2.db"
            connection = sqlite3.connect(
                ":memory:",
                isolation_level=None,
                factory=store_module._GuardedConnection,
                check_same_thread=False,
            )
            v3_guard = SimpleNamespace(
                parent_fd=11,
                leaf_fd=12,
                ancestor_fds=[11],
                leaf_identity=(101, 202),
                release=mock.Mock(),
            )
            v2_guard = SimpleNamespace(
                parent_fd=13,
                leaf_fd=14,
                ancestor_fds=[13],
                leaf_identity=(303, 404),
            )
            with mock.patch.object(store_module.os, "name", "posix"):
                with mock.patch.object(store_module.sys, "platform", "linux"):
                    with mock.patch.object(
                        store_module,
                        "_open_linux_guard",
                        return_value=v3_guard,
                    ):
                        with mock.patch.object(sqlite3, "connect", return_value=connection):
                            with mock.patch.object(
                                store_module,
                                "_main_database_path",
                                return_value=target,
                            ):
                                with mock.patch.object(
                                    store_module.os,
                                    "fstat",
                                    return_value=SimpleNamespace(
                                        st_mode=stat.S_IFREG | 0o600,
                                        st_dev=101,
                                        st_ino=202,
                                    ),
                                ):
                                    with mock.patch.object(
                                        store_module,
                                        "_linux_sqlite_main_descriptor",
                                        return_value=20,
                                    ) as native_descriptor:
                                        verified = store_module._connect_with_verified_identity_under_lock(
                                            target,
                                            v2_path,
                                            v2_guard,
                                        )

            self.assertIs(verified, connection)
            native_descriptor.assert_called_once_with(connection, target)
            connection.close()
            v3_guard.release.assert_called_once_with()

    def test_linux_native_main_descriptor_cannot_alias_a_guard_descriptor(self):
        connection = sqlite3.connect(
            ":memory:",
            isolation_level=None,
            factory=store_module._GuardedConnection,
            check_same_thread=False,
        )
        v3_guard = SimpleNamespace(
            parent_fd=11,
            leaf_fd=12,
            ancestor_fds=[11],
            leaf_identity=(101, 202),
            release=mock.Mock(),
        )
        v2_guard = SimpleNamespace(
            parent_fd=13,
            leaf_fd=14,
            ancestor_fds=[13],
            leaf_identity=(303, 404),
        )
        with mock.patch.object(store_module.os, "name", "posix"):
            with mock.patch.object(store_module.sys, "platform", "linux"):
                with mock.patch.object(
                    store_module,
                    "_open_linux_guard",
                    return_value=v3_guard,
                ):
                    with mock.patch.object(sqlite3, "connect", return_value=connection):
                        with mock.patch.object(
                            store_module,
                            "_main_database_path",
                            return_value=Path("/tmp/v3.db"),
                        ):
                            with mock.patch.object(
                                store_module,
                                "_linux_sqlite_main_descriptor",
                                return_value=12,
                            ):
                                with self.assertRaises(StoreConfigurationError) as caught:
                                    store_module._connect_with_verified_identity_under_lock(
                                        Path("/tmp/v3.db"),
                                        Path("/tmp/v2.db"),
                                        v2_guard,
                                    )

        self.assertEqual(caught.exception.error_code, "v3_db_main_handle_mismatch")
        v3_guard.release.assert_called_once_with()

    def test_linux_native_probe_fails_closed_outside_supported_cpython_layout(self):
        with mock.patch.object(
            store_module.sys,
            "implementation",
            SimpleNamespace(name="pypy"),
        ):
            with self.assertRaises(StoreConfigurationError) as caught:
                store_module._linux_sqlite_main_descriptor(
                    object(),
                    Path("/tmp/v3.db"),
                )

        self.assertEqual(caught.exception.error_code, "v3_db_identity_unprovable")
        self.assertIsInstance(caught.exception.__cause__, RuntimeError)

    def test_linux_provider_preflight_precedes_v3_guard_side_effects(self):
        native_library = object()
        rejected = StoreConfigurationError(
            "v3_db_identity_unprovable",
            "injected sqlite3_open_v2 interposition",
        )
        with mock.patch.object(store_module.sys, "platform", "linux"):
            with mock.patch.object(
                store_module,
                "_linux_sqlite_native_library",
                return_value=native_library,
            ):
                with mock.patch.object(
                    store_module,
                    "_linux_sqlite_runtime_preflight",
                    side_effect=rejected,
                ) as preflight:
                    with mock.patch.object(
                        store_module,
                        "_open_linux_parent",
                    ) as open_parent:
                        with self.assertRaises(StoreConfigurationError) as caught:
                            store_module._open_linux_guard(
                                Path("/tmp/v3.db"),
                                Path("/tmp/v2.db"),
                                v2_identity=(1, 2),
                            )

        self.assertIs(caught.exception, rejected)
        preflight.assert_called_once_with(native_library)
        open_parent.assert_not_called()

    def test_python310_linux_proves_serialized_mode_with_verified_provider(self):
        native_threadsafe = mock.Mock(return_value=1)
        native_library = SimpleNamespace(sqlite3_threadsafe=native_threadsafe)
        with mock.patch.object(store_module.sqlite3, "threadsafety", 1):
            with mock.patch.object(store_module.sys, "platform", "linux"):
                with mock.patch.object(
                    store_module.sys,
                    "version_info",
                    (3, 10, 20, "final", 0),
                ):
                    with mock.patch.object(
                        store_module,
                        "_linux_sqlite_native_library",
                        return_value=native_library,
                    ) as load_library:
                        self.assertTrue(
                            store_module._serialized_sqlite_thread_safety_available()
                        )

        load_library.assert_called_once_with()
        native_threadsafe.assert_called_once_with()
        self.assertEqual(native_threadsafe.argtypes, ())

    def test_python310_linux_rejects_nonserialized_native_provider(self):
        for native_mode in (0, 2):
            with self.subTest(native_mode=native_mode):
                native_library = SimpleNamespace(
                    sqlite3_threadsafe=mock.Mock(return_value=native_mode)
                )
                with mock.patch.object(store_module.sqlite3, "threadsafety", 1):
                    with mock.patch.object(store_module.sys, "platform", "linux"):
                        with mock.patch.object(
                            store_module.sys,
                            "version_info",
                            (3, 10, 20, "final", 0),
                        ):
                            with mock.patch.object(
                                store_module,
                                "_linux_sqlite_native_library",
                                return_value=native_library,
                            ):
                                self.assertFalse(
                                    store_module._serialized_sqlite_thread_safety_available()
                                )

    def test_linux_ldd_provider_parser_requires_one_absolute_sqlite_dependency(self):
        provider = store_module._parse_linux_ldd_sqlite_provider(
            "\tlibsqlite3.so.0 => /usr/lib/libsqlite3.so.0 (0x1234)\n"
        )
        self.assertEqual(provider, Path("/usr/lib/libsqlite3.so.0"))

        for output in (
            "\tlibc.so.6 => /usr/lib/libc.so.6 (0x1234)\n",
            "\tlibsqlite3.so.0 => not found\n",
            "\tlibsqlite3.so.0 => /a/libsqlite3.so.0 (0x1)\n"
            "\tlibsqlite3.so.1 => /b/libsqlite3.so.1 (0x2)\n",
        ):
            with self.subTest(output=output):
                with self.assertRaises(StoreConfigurationError) as caught:
                    store_module._parse_linux_ldd_sqlite_provider(output)
                self.assertEqual(
                    caught.exception.error_code,
                    "v3_db_identity_unprovable",
                )

    def test_linux_readelf_parser_captures_every_undefined_sqlite_symbol(self):
        output = textwrap.dedent(
            """
            Symbol table '.dynsym' contains 5 entries:
               Num:    Value          Size Type    Bind   Vis      Ndx Name
                 1: 0000000000000000     0 FUNC    GLOBAL DEFAULT  UND sqlite3_open_v2
                 2: 0000000000000000     0 FUNC    GLOBAL DEFAULT  UND sqlite3_extended_errcode
                 3: 0000000000000000     0 FUNC    GLOBAL DEFAULT  UND sqlite3_prepare_v2@SQLITE_3.0
                 4: 0000000000000010     0 FUNC    GLOBAL DEFAULT   12 sqlite3_local_helper
                 5: 0000000000000000     0 FUNC    GLOBAL DEFAULT  UND PyErr_SetString
            """
        )

        self.assertEqual(
            store_module._parse_linux_readelf_sqlite_imports(output),
            (
                "sqlite3_extended_errcode",
                "sqlite3_open_v2",
                "sqlite3_prepare_v2",
            ),
        )

        for invalid in ("", "1: 0 0 FUNC GLOBAL DEFAULT UND PyErr_SetString\n"):
            with self.subTest(invalid=invalid):
                with self.assertRaises(StoreConfigurationError) as caught:
                    store_module._parse_linux_readelf_sqlite_imports(invalid)
                self.assertEqual(
                    caught.exception.error_code,
                    "v3_db_identity_unprovable",
                )

    @unittest.skipUnless(
        sys.platform.startswith("linux"),
        "real Unix VFS probe runs only on Linux",
    )
    def test_linux_real_native_probe_isolated_in_subprocess(self):
        source = textwrap.dedent(
            """
            import json
            import os
            import sqlite3
            import stat
            import sys
            import tempfile
            from pathlib import Path

            sys.path.insert(0, os.getcwd())
            from server.content_domains.ai_edit_v3 import store

            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp).resolve()
                v2 = root / "ai_edit_v2.db"
                sqlite3.connect(v2).close()
                connection = store.open_store(
                    root / "ai_edit_v3.db",
                    v2_db_path=v2,
                )
                try:
                    native_library = store._linux_sqlite_native_library()
                    provider_symbols = native_library._v3_sqlite_provider_symbols
                    assert "sqlite3_open_v2" in provider_symbols
                    assert "sqlite3_extended_errcode" in provider_symbols
                    main_path = store._main_database_path(connection)
                    descriptor = store._linux_sqlite_main_descriptor(
                        connection,
                        main_path,
                    )
                    metadata = os.fstat(descriptor)
                    guard = connection._identity_guard
                    assert stat.S_ISREG(metadata.st_mode)
                    assert (metadata.st_dev, metadata.st_ino) == guard.leaf_identity
                    print(json.dumps({"descriptor": descriptor, "ok": True}))
                finally:
                    connection.close()
            """
        )
        result = subprocess.run(
            [sys.executable, "-I", "-c", source],
            cwd=Path(__file__).resolve().parents[1],
            text=True,
            capture_output=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertTrue(json.loads(result.stdout)["ok"])

    @unittest.skipUnless(
        sys.platform.startswith("linux"),
        "SQLite interposition probe runs only on Linux",
    )
    def test_linux_sqlite_symbol_interposition_fails_before_sqlite_connect(self):
        compiler = shutil.which("cc")
        if not compiler:
            self.skipTest("C compiler unavailable for interposition probe")
        shim_sources = {
            "sqlite3_open_v2": """
                typedef int (*target_fn)(const char *, sqlite3 **, int, const char *);
                int sqlite3_open_v2(const char *filename, sqlite3 **database, int flags, const char *vfs) {
                    target_fn real_target = (target_fn)dlsym(RTLD_NEXT, "sqlite3_open_v2");
                    return real_target(filename, database, flags, vfs);
                }
            """,
            "sqlite3_extended_errcode": """
                typedef int (*target_fn)(sqlite3 *);
                int sqlite3_extended_errcode(sqlite3 *database) {
                    target_fn real_target = (target_fn)dlsym(RTLD_NEXT, "sqlite3_extended_errcode");
                    return real_target(database);
                }
            """,
        }
        for symbol, implementation in shim_sources.items():
            with self.subTest(symbol=symbol), tempfile.TemporaryDirectory() as temp:
                root = Path(temp).resolve()
                source_path = root / "sqlite_shim.c"
                shim_path = root / "sqlite_shim.so"
                source_path.write_text(
                    textwrap.dedent(
                        f"""
                        #define _GNU_SOURCE
                        #include <dlfcn.h>
                        typedef struct sqlite3 sqlite3;
                        {implementation}
                        """
                    ),
                    encoding="utf-8",
                )
                compile_result = subprocess.run(
                    [
                        compiler,
                        "-shared",
                        "-fPIC",
                        "-o",
                        os.fspath(shim_path),
                        os.fspath(source_path),
                        "-ldl",
                    ],
                    text=True,
                    capture_output=True,
                    timeout=30,
                )
                self.assertEqual(
                    compile_result.returncode,
                    0,
                    compile_result.stderr or compile_result.stdout,
                )
                v2 = root / "ai_edit_v2.db"
                sqlite3.connect(v2).close()
                child_source = textwrap.dedent(
                    """
                    import json
                    import os
                    import sys
                    from pathlib import Path

                    root = Path(sys.argv[1])
                    symbol = sys.argv[2]
                    os.environ.pop("LD_PRELOAD", None)
                    sys.path.insert(0, os.getcwd())
                    from server.content_domains.ai_edit_v3 import store

                    connect_called = False
                    def forbidden_connect(*args, **kwargs):
                        global connect_called
                        connect_called = True
                        raise AssertionError("sqlite3.connect ran before provider rejection")

                    store.sqlite3.connect = forbidden_connect
                    try:
                        store.open_store(
                            root / "ai_edit_v3.db",
                            v2_db_path=root / "ai_edit_v2.db",
                        )
                    except store.StoreConfigurationError as exc:
                        print(json.dumps({
                            "code": exc.error_code,
                            "connect_called": connect_called,
                            "preload_present": "LD_PRELOAD" in os.environ,
                        }))
                    else:
                        raise AssertionError(f"interposed {symbol} was accepted")
                    """
                )
                environment = os.environ.copy()
                environment["LD_PRELOAD"] = os.fspath(shim_path)
                result = subprocess.run(
                    [
                        sys.executable,
                        "-I",
                        "-c",
                        child_source,
                        os.fspath(root),
                        symbol,
                    ],
                    cwd=Path(__file__).resolve().parents[1],
                    env=environment,
                    text=True,
                    capture_output=True,
                    timeout=30,
                )

                self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
                payload = json.loads(result.stdout)
                self.assertEqual(payload["code"], "v3_db_identity_unprovable")
                self.assertFalse(payload["connect_called"])
                self.assertFalse(payload["preload_present"])

    def test_linux_native_main_descriptor_fstat_error_fails_closed(self):
        connection = sqlite3.connect(
            ":memory:",
            isolation_level=None,
            factory=store_module._GuardedConnection,
            check_same_thread=False,
        )
        v3_guard = SimpleNamespace(
            parent_fd=11,
            leaf_fd=12,
            ancestor_fds=[11],
            leaf_identity=(101, 202),
            release=mock.Mock(),
        )
        v2_guard = SimpleNamespace(
            parent_fd=13,
            leaf_fd=14,
            ancestor_fds=[13],
            leaf_identity=(303, 404),
        )
        with mock.patch.object(store_module.os, "name", "posix"):
            with mock.patch.object(store_module.sys, "platform", "linux"):
                with mock.patch.object(
                    store_module,
                    "_open_linux_guard",
                    return_value=v3_guard,
                ):
                    with mock.patch.object(sqlite3, "connect", return_value=connection):
                        with mock.patch.object(
                            store_module,
                            "_main_database_path",
                            return_value=Path("/tmp/v3.db"),
                        ):
                            with mock.patch.object(
                                store_module,
                                "_linux_sqlite_main_descriptor",
                                return_value=20,
                            ):
                                with mock.patch.object(
                                    store_module.os,
                                    "fstat",
                                    side_effect=OSError(
                                        errno.EIO,
                                        "injected descriptor I/O failure",
                                    ),
                                ):
                                    with self.assertRaises(StoreConfigurationError) as caught:
                                        store_module._connect_with_verified_identity_under_lock(
                                            Path("/tmp/v3.db"),
                                            Path("/tmp/v2.db"),
                                            v2_guard,
                                        )

        self.assertEqual(
            caught.exception.error_code,
            "v3_db_identity_unprovable",
        )
        self.assertIsInstance(caught.exception.__cause__, OSError)
        self.assertEqual(caught.exception.__cause__.errno, errno.EIO)
        v3_guard.release.assert_called_once_with()

    @unittest.skipUnless(os.name == "nt", "native Windows handle cleanup probe")
    def test_windows_main_identity_error_owns_failed_temporary_handle_for_retry(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp).resolve() / "requested.db"
            target.touch()
            connection = sqlite3.connect(
                ":memory:",
                isolation_level=None,
                factory=store_module._GuardedConnection,
                check_same_thread=False,
            )
            v3_guard = SimpleNamespace(
                leaf_identity=(1, 2),
                release=mock.Mock(),
            )
            v2_guard = SimpleNamespace(leaf_identity=(3, 4))
            close_handle = mock.Mock(side_effect=(0, 1))
            kernel32 = SimpleNamespace(CloseHandle=close_handle)
            fake_ctypes = SimpleNamespace(
                WinDLL=mock.Mock(return_value=kernel32),
                WinError=lambda code: OSError(code, "injected CloseHandle failure"),
                get_last_error=mock.Mock(return_value=6),
                wintypes=SimpleNamespace(BOOL=bool, HANDLE=lambda value: value),
            )

            with mock.patch.object(store_module.os, "name", "nt"):
                with mock.patch.object(
                    store_module,
                    "_open_windows_guard",
                    return_value=v3_guard,
                ):
                    with mock.patch.object(sqlite3, "connect", return_value=connection):
                        with mock.patch.object(
                            store_module,
                            "_main_database_path",
                            return_value=target,
                        ):
                            with mock.patch.object(store_module, "_same_path", return_value=True):
                                with mock.patch.object(
                                    store_module,
                                    "_windows_create_handle",
                                    return_value=99,
                                ):
                                    with mock.patch.object(
                                        store_module,
                                        "_windows_handle_identity",
                                        return_value=((9, 9), 0, 1),
                                    ):
                                        with mock.patch.dict(
                                            store_module.sys.modules,
                                            {"ctypes": fake_ctypes},
                                        ):
                                            with self.assertRaises(
                                                StoreConfigurationError
                                            ) as caught:
                                                store_module._connect_with_verified_identity_under_lock(
                                                    target,
                                                    Path(temp).resolve() / "v2.db",
                                                    v2_guard,
                                                )
                                            owner = caught.exception.cleanup_owner
                                            self.assertEqual(
                                                caught.exception.error_code,
                                                "v3_db_main_handle_mismatch",
                                            )
                                            self.assertIn(
                                                "CloseHandle failure",
                                                "\n".join(caught.exception.__notes__),
                                            )
                                            self.assertEqual(len(owner.pending_resources), 1)
                                            self.assertEqual(
                                                owner.pending_resources[0].handles,
                                                [99],
                                            )

                                            owner.retry()
                                            owner.retry()

            self.assertEqual(owner.pending_resources, ())
            self.assertEqual(
                [call.args[0] for call in close_handle.call_args_list],
                [99, 99],
            )
            v3_guard.release.assert_called_once()

    def test_platform_open_cleanup_does_not_mask_stable_identity_errors(self):
        windows_release_error = OSError("injected Windows cleanup failure")
        with mock.patch.object(
            store_module,
            "_open_windows_ancestor_handles",
            return_value=[11],
        ):
            with mock.patch.object(store_module, "_windows_create_handle", return_value=22):
                with mock.patch.object(
                    store_module,
                    "_windows_handle_identity",
                    return_value=((1, 2), 0, 2),
                ):
                    with mock.patch.object(
                        store_module._WindowsGuardBundle,
                        "release",
                        side_effect=(windows_release_error, None),
                    ) as release:
                        with self.assertRaises(StoreConfigurationError) as caught:
                            store_module._open_windows_guard(
                                Path("C:/v3.db"),
                                Path("C:/v2.db"),
                                v2_identity=(3, 4),
                            )
                        windows_owner = caught.exception.cleanup_owner
                        self.assertEqual(len(windows_owner.pending_resources), 1)
                        windows_owner.retry()
        self.assertEqual(caught.exception.error_code, "v3_db_identity_unprovable")
        self.assertEqual(windows_owner.pending_resources, ())
        self.assertEqual(release.call_count, 2)

        linux_release_error = OSError("injected Linux cleanup failure")
        metadata = SimpleNamespace(
            st_mode=stat.S_IFREG,
            st_nlink=2,
            st_dev=1,
            st_ino=2,
        )
        with mock.patch.object(store_module, "_open_linux_parent", return_value=11):
            with mock.patch.object(store_module.os, "open", return_value=22):
                with mock.patch.object(store_module.os, "fstat", return_value=metadata):
                    with mock.patch.object(
                        store_module._LinuxGuardBundle,
                        "release",
                        side_effect=(linux_release_error, None),
                    ) as release:
                        with self.assertRaises(StoreConfigurationError) as caught:
                            store_module._open_linux_guard(
                                Path("/tmp/v3.db"),
                                Path("/tmp/v2.db"),
                                v2_identity=(3, 4),
                            )
                        linux_owner = caught.exception.cleanup_owner
                        self.assertEqual(len(linux_owner.pending_resources), 1)
                        linux_owner.retry()
        self.assertEqual(caught.exception.error_code, "v3_db_identity_unprovable")
        self.assertEqual(linux_owner.pending_resources, ())
        self.assertEqual(release.call_count, 2)

    def test_v2_platform_error_mapping_preserves_cleanup_owner_and_note(self):
        native_error = OSError("injected native V2 open failure")
        guard = SimpleNamespace(release=mock.Mock())
        owner = store_module._CleanupOwner()
        owner.append(
            guard,
            "release",
            description="injected V2 guard release",
        )
        native_error.cleanup_owner = owner
        native_error.add_note("injected retained V2 cleanup")

        with mock.patch.object(store_module.os, "name", "nt"):
            with mock.patch.object(
                store_module,
                "_open_windows_v2_guard",
                side_effect=native_error,
            ):
                with self.assertRaises(StoreConfigurationError) as caught:
                    store_module._open_v2_handshake_guard(Path("C:/v2.db"))

        self.assertEqual(caught.exception.error_code, "v2_db_identity_unknown")
        self.assertIs(caught.exception.cleanup_owner, owner)
        self.assertIn(
            "injected retained V2 cleanup",
            "\n".join(caught.exception.__notes__),
        )

    def test_linux_parent_build_failure_owns_failed_cleanup_for_retry(self):
        metadata = SimpleNamespace(st_uid=123, st_mode=0o777)
        close_descriptor = mock.Mock(side_effect=(None, OSError("close 22 failed"), None))

        with mock.patch.object(store_module.os, "open", side_effect=(11, 22)):
            with mock.patch.object(store_module.os, "close", close_descriptor):
                with mock.patch.object(store_module.os, "fstat", return_value=metadata):
                    with mock.patch.object(
                        store_module.os,
                        "geteuid",
                        return_value=456,
                        create=True,
                    ):
                        with self.assertRaises(StoreConfigurationError) as caught:
                            store_module._open_linux_parent(Path("/tmp/v3.db"))
                        owner = caught.exception.cleanup_owner
                        self.assertEqual(
                            caught.exception.error_code,
                            "v3_db_identity_unprovable",
                        )
                        self.assertEqual(len(owner.pending_resources), 1)
                        self.assertEqual(owner.pending_resources[0].ancestor_fds, [22])
                        owner.retry()

        self.assertEqual(owner.pending_resources, ())
        self.assertEqual(
            [call.args[0] for call in close_descriptor.call_args_list],
            [11, 22, 22],
        )

    def test_verified_open_cleanup_failure_does_not_mask_stable_error(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            target = root / "requested.db"
            other = root / "other.db"
            sqlite3.connect(other).close()
            guard = SimpleNamespace(
                leaf_identity=(1, 2),
                parent_fd=11,
                release=mock.Mock(
                    side_effect=(OSError("injected V3 cleanup failure"), None)
                ),
            )
            v2_guard = SimpleNamespace(leaf_identity=(3, 4))

            def wrong_connect(_requested, *args, **kwargs):
                return sqlite3.Connection(other)

            guard_name = (
                "_open_windows_guard" if os.name == "nt" else "_open_linux_guard"
            )
            with mock.patch.object(store_module, guard_name, return_value=guard):
                with mock.patch.object(sqlite3, "connect", side_effect=wrong_connect):
                    with self.assertRaises(StoreConfigurationError) as caught:
                        store_module._connect_with_verified_identity_under_lock(
                            target,
                            root / "v2.db",
                            v2_guard,
                        )

        self.assertEqual(caught.exception.error_code, "v3_db_main_handle_mismatch")
        owner = caught.exception.cleanup_owner
        self.assertEqual(owner.pending_resources, (guard,))
        owner.retry()
        self.assertEqual(owner.pending_resources, ())
        self.assertEqual(guard.release.call_count, 2)

    def test_unverified_native_close_failure_does_not_release_raw_guard(self):
        cleanup_order = []
        close_failed = False

        def close_connection():
            nonlocal close_failed
            cleanup_order.append("connection")
            if not close_failed:
                close_failed = True
                raise OSError("injected raw connection close failure")

        def release_guard():
            cleanup_order.append("guard")

        connection = SimpleNamespace(
            close=mock.Mock(side_effect=close_connection),
        )
        guard = SimpleNamespace(
            leaf_identity=(1, 2),
            parent_fd=11,
            release=mock.Mock(side_effect=release_guard),
        )
        v2_guard = SimpleNamespace(leaf_identity=(3, 4))
        guard_name = "_open_windows_guard" if os.name == "nt" else "_open_linux_guard"

        with mock.patch.object(store_module, guard_name, return_value=guard):
            with mock.patch.object(sqlite3, "connect", return_value=connection):
                with self.assertRaises(StoreConfigurationError) as caught:
                    store_module._connect_with_verified_identity_under_lock(
                        Path("C:/requested.db"),
                        Path("C:/v2.db"),
                        v2_guard,
                    )

        self.assertEqual(caught.exception.error_code, "v3_db_main_handle_mismatch")
        connection.close.assert_called_once()
        guard.release.assert_not_called()
        owner = caught.exception.cleanup_owner
        self.assertEqual(owner.pending_resources, (connection, guard))

        owner.retry()
        owner.retry()

        self.assertEqual(owner.pending_resources, ())
        self.assertEqual(cleanup_order, ["connection", "connection", "guard"])

    def test_nested_cleanup_owner_retries_connection_then_v3_then_v2(self):
        cleanup_order = []
        close_attempts = 0

        def close_connection():
            nonlocal close_attempts
            close_attempts += 1
            cleanup_order.append("connection")
            if close_attempts <= 2:
                raise OSError(f"injected close failure {close_attempts}")

        connection = SimpleNamespace(close=mock.Mock(side_effect=close_connection))
        v3_guard = SimpleNamespace(
            leaf_identity=(1, 2),
            parent_fd=11,
            release=mock.Mock(side_effect=lambda: cleanup_order.append("v3")),
        )
        v2_guard = SimpleNamespace(
            leaf_identity=(3, 4),
            release=mock.Mock(side_effect=lambda: cleanup_order.append("v2")),
        )
        guard_name = "_open_windows_guard" if os.name == "nt" else "_open_linux_guard"

        with mock.patch.object(
            store_module,
            "_open_v2_handshake_guard",
            return_value=v2_guard,
        ):
            with mock.patch.object(store_module, "resolve_db_path", side_effect=lambda path: path):
                with mock.patch.object(store_module, "assert_isolated_db"):
                    with mock.patch.object(store_module, "_assert_local_filesystem"):
                        with mock.patch.object(
                            store_module,
                            "_path_identity",
                            return_value=(False, None),
                        ):
                            with mock.patch.object(
                                store_module,
                                guard_name,
                                return_value=v3_guard,
                            ):
                                with mock.patch.object(
                                    sqlite3,
                                    "connect",
                                    return_value=connection,
                                ):
                                    with self.assertRaises(StoreConfigurationError) as caught:
                                        store_module._open_store_ordered(
                                            Path("C:/v3.db"),
                                            Path("C:/v2.db"),
                                        )

        owner = caught.exception.cleanup_owner
        self.assertEqual(
            owner.pending_resources,
            (connection, v3_guard, v2_guard),
        )
        self.assertEqual(cleanup_order, ["connection"])

        with self.assertRaisesRegex(OSError, "close failure 2"):
            owner.retry()

        self.assertEqual(
            owner.pending_resources,
            (connection, v3_guard, v2_guard),
        )
        self.assertEqual(cleanup_order, ["connection", "connection"])
        v3_guard.release.assert_not_called()
        v2_guard.release.assert_not_called()

        owner.retry()

        self.assertEqual(owner.pending_resources, ())
        self.assertEqual(
            cleanup_order,
            ["connection", "connection", "connection", "v3", "v2"],
        )

    def test_v2_guard_cleanup_failure_does_not_mask_stable_open_error(self):
        original = StoreConfigurationError(
            "v3_injected_stable_error",
            "injected stable open failure",
        )
        cleanup_failed = False

        def release_guard():
            nonlocal cleanup_failed
            if not cleanup_failed:
                cleanup_failed = True
                raise OSError("injected V2 cleanup failure")

        guard = SimpleNamespace(
            release=mock.Mock(side_effect=release_guard),
        )
        with mock.patch.object(
            store_module,
            "_open_v2_handshake_guard",
            return_value=guard,
        ):
            with mock.patch.object(store_module, "resolve_db_path", side_effect=original):
                with self.assertRaises(StoreConfigurationError) as caught:
                    store_module._open_store_ordered(
                        Path("C:/v3.db"),
                        Path("C:/v2.db"),
                    )

        self.assertIs(caught.exception, original)
        guard.release.assert_called_once()
        owner = caught.exception.cleanup_owner
        self.assertEqual(owner.pending_resources, (guard,))
        owner.retry()
        self.assertEqual(owner.pending_resources, ())
        self.assertEqual(guard.release.call_count, 2)

    def test_success_path_v2_release_failure_closes_unreturned_v3_and_retries(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            target = root / "prepared-v3.db"
            v2 = root / "v2.db"
            release_error = OSError("injected successful-open V2 release failure")
            v2_guard = SimpleNamespace(
                release=mock.Mock(side_effect=(release_error, release_error, None)),
            )
            v3_guard = SimpleNamespace(release=mock.Mock())
            connection = sqlite3.connect(
                ":memory:",
                isolation_level=None,
                factory=store_module._GuardedConnection,
                check_same_thread=False,
            )
            connection._retain_identity_guard(v3_guard)

            with mock.patch.object(
                store_module,
                "_open_v2_handshake_guard",
                return_value=v2_guard,
            ):
                with mock.patch.object(store_module, "_assert_local_filesystem"):
                    with mock.patch.object(
                        store_module,
                        "_connect_with_verified_identity_under_lock",
                        return_value=connection,
                    ):
                        with mock.patch.object(store_module, "_negotiate_wal"):
                            with mock.patch.object(store_module, "_revalidate_open_identity"):
                                with self.assertRaises(OSError) as caught:
                                    store_module._open_store_ordered(target, v2)

        self.assertIs(caught.exception, release_error)
        owner = caught.exception.cleanup_owner
        self.assertEqual(owner.pending_resources, (v2_guard,))
        owner.retry()
        self.assertEqual(owner.pending_resources, ())
        self.assertEqual(v2_guard.release.call_count, 3)
        v3_guard.release.assert_called_once()
        self.assertIsNone(connection._identity_guard)
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")


class V3StoreHandshakeAndLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.v2 = self.root / "ai_edit_v2.db"
        connection = sqlite3.connect(self.v2, isolation_level=None)
        try:
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("CREATE TABLE v2_marker(value TEXT NOT NULL)")
            connection.execute("INSERT INTO v2_marker VALUES('unchanged')")
        finally:
            connection.close()

    @staticmethod
    def _sidecars(path):
        return tuple(
            Path(f"{path}{suffix}").exists()
            for suffix in ("-wal", "-shm", "-journal")
        )

    @staticmethod
    def _journal_mode(path):
        connection = sqlite3.connect(path, isolation_level=None)
        try:
            return connection.execute("PRAGMA journal_mode").fetchone()[0].lower()
        finally:
            connection.close()

    @staticmethod
    def _guard_name():
        return "_open_windows_guard" if os.name == "nt" else "_open_linux_guard"

    def _open_counted(self, target):
        guard_name = self._guard_name()
        real_guard = getattr(store_module, guard_name)
        holder = {}

        def counted(*args, **kwargs):
            proxy = _ReleaseCountingGuard(real_guard(*args, **kwargs))
            holder["guard"] = proxy
            return proxy

        with mock.patch.object(store_module, guard_name, side_effect=counted):
            connection = open_store(target, v2_db_path=self.v2)
        return connection, holder["guard"]

    def test_v2_swap_before_v3_native_guard_cannot_reach_sqlite_or_mutate_identity(self):
        target = self.root / "swap-before-guard.db"
        before_mode = self._journal_mode(self.v2)
        before_bytes = self.v2.read_bytes()
        before_mtime = self.v2.stat().st_mtime_ns
        before_sidecars = self._sidecars(self.v2)
        guard_name = self._guard_name()
        real_guard = getattr(store_module, guard_name)
        attack = {"attempted": False, "moved": False}

        def swap_then_guard(*args, **kwargs):
            attack["attempted"] = True
            os.replace(self.v2, target)
            attack["moved"] = True
            return real_guard(*args, **kwargs)

        def operation():
            connection = open_store(target, v2_db_path=self.v2)
            connection.close()

        real_connect = sqlite3.connect
        with mock.patch.object(store_module, guard_name, side_effect=swap_then_guard):
            with mock.patch.object(sqlite3, "connect", wraps=real_connect) as connect:
                with self.assertRaises((StoreConfigurationError, OSError)):
                    operation()
            connect.assert_not_called()

        self.assertTrue(attack["attempted"])
        survivor = self.v2 if self.v2.exists() else target
        self.assertEqual(survivor.read_bytes(), before_bytes)
        self.assertEqual(self._journal_mode(survivor), before_mode)
        self.assertEqual(survivor.stat().st_mtime_ns, before_mtime)
        self.assertEqual(self._sidecars(self.v2), before_sidecars)
        self.assertEqual(self._sidecars(target), (False, False, False))

    def test_v2_missing_at_guard_handshake_fails_before_v3_creation(self):
        missing_v2 = self.root / "missing-v2.db"
        target = self.root / "missing-handshake-v3.db"

        def operation():
            connection = open_store(target, v2_db_path=missing_v2)
            connection.close()

        with mock.patch.object(sqlite3, "connect", wraps=sqlite3.connect) as connect:
            with self.assertRaises(StoreConfigurationError) as caught:
                operation()
        self.assertEqual(caught.exception.error_code, "v2_db_identity_missing")
        connect.assert_not_called()
        self.assertFalse(target.exists())
        self.assertEqual(self._sidecars(target), (False, False, False))

    def test_missing_v2_parent_has_one_stable_code_and_native_cause_for_every_entrypoint(self):
        missing_v2 = self.root / "missing-parent" / "ai_edit_v2.db"
        entries = (
            ("open", lambda target: open_store(target, v2_db_path=missing_v2)),
            ("init", lambda target: init_db(target, v2_db_path=missing_v2)),
            ("store", lambda target: V3Store(target, v2_db_path=missing_v2)),
        )
        for label, operation in entries:
            with self.subTest(entry=label):
                target = self.root / f"missing-parent-{label}.db"
                with self.assertRaises(StoreConfigurationError) as caught:
                    operation(target)
                self.assertEqual(caught.exception.error_code, "v2_db_identity_unknown")
                self.assertIsInstance(caught.exception.__cause__, FileNotFoundError)
                self.assertFalse(target.exists())
                self.assertEqual(self._sidecars(target), (False, False, False))

    def test_v2_native_permission_failure_has_stable_code_and_preserves_cause(self):
        target = self.root / "permission-v3.db"
        native_error = PermissionError(errno.EACCES, "injected V2 native open denial")
        if os.name == "nt":
            seam = mock.patch.object(
                store_module,
                "_windows_create_handle",
                side_effect=native_error,
            )
        else:
            seam = mock.patch.object(store_module.os, "open", side_effect=native_error)
        with seam:
            with self.assertRaises(StoreConfigurationError) as caught:
                open_store(target, v2_db_path=self.v2)
        self.assertEqual(caught.exception.error_code, "v2_db_identity_unknown")
        self.assertIs(caught.exception.__cause__, native_error)
        self.assertFalse(target.exists())
        self.assertEqual(self._sidecars(target), (False, False, False))

    def test_unclosed_connection_gc_closes_sqlite_then_releases_guard_once(self):
        target = self.root / "gc.db"
        connection, guard = self._open_counted(target)
        cursor = connection.execute("SELECT 1")
        connection.retained_cursor = cursor
        del connection
        del cursor
        for _ in range(3):
            gc.collect()
        observed = guard.release_calls
        if observed == 0:
            guard.release()
        self.assertEqual(observed, 1)

    def test_cursor_only_retains_native_connection_and_guard_until_graph_dies(self):
        target = self.root / "cursor-retained.db"
        connection, guard = self._open_counted(target)
        cursor = connection.execute("SELECT 42")
        del connection
        for _ in range(3):
            gc.collect()

        self.assertEqual(cursor.fetchone()[0], 42)
        self.assertEqual(guard.release_calls, 0)
        retained_connection = cursor.connection
        cursor.close()
        retained_connection.close()
        self.assertEqual(guard.release_calls, 1)

    def test_connection_and_cursor_keep_native_sqlite_identity(self):
        target = self.root / "native-identity.db"
        connection, guard = self._open_counted(target)
        self.addCleanup(connection.close)
        self.assertIsInstance(connection, sqlite3.Connection)
        cursor = connection.execute("SELECT 42")
        self.assertIs(cursor.connection, connection)
        self.assertEqual(cursor.fetchone()[0], 42)
        connection.close()
        self.assertEqual(guard.release_calls, 1)

    def test_temporary_query_cursors_return_tracking_to_baseline_after_gc(self):
        target = self.root / "temporary-cursors.db"
        connection, _guard = self._open_counted(target)
        self.addCleanup(connection.close)
        gc.collect()
        baseline = len(connection._tracked_cursors)

        for _ in range(10_000):
            self.assertEqual(connection.execute("SELECT 42").fetchone()[0], 42)
        gc.collect()

        self.assertEqual(len(connection._tracked_cursors), baseline)

    def test_closed_dropped_cursors_from_every_documented_path_stay_bounded(self):
        target = self.root / "closed-cursors.db"
        connection, _guard = self._open_counted(target)
        self.addCleanup(connection.close)

        class CustomCursor(sqlite3.Cursor):
            pass

        builders = {
            "cursor": lambda: connection.cursor(),
            "execute": lambda: connection.execute("SELECT 42"),
            "executemany": lambda: connection.executemany(
                "INSERT INTO bounded_cursor_probe VALUES(?)",
                (),
            ),
            "executescript": lambda: connection.executescript("SELECT 42;"),
            "custom_factory": lambda: connection.cursor(factory=CustomCursor),
        }
        connection.execute("CREATE TEMP TABLE bounded_cursor_probe(value INTEGER)").close()
        gc.collect()
        baseline = len(connection._tracked_cursors)

        for path, build_cursor in builders.items():
            with self.subTest(path=path):
                for _ in range(10_000):
                    cursor = build_cursor()
                    cursor.close()
                    del cursor
                gc.collect()
                self.assertEqual(len(connection._tracked_cursors), baseline)

    @unittest.skipUnless(os.name == "nt", "Windows delete sharing is the native handle regression")
    def test_retained_live_cursor_stays_tracked_until_close_then_allows_unlink(self):
        target = self.root / "retained-live-cursor.db"
        connection, guard = self._open_counted(target)
        cursor = connection.execute("SELECT 42")

        gc.collect()
        tracked = tuple(connection._tracked_cursors.values())
        self.assertTrue(
            any(
                value is cursor
                or (isinstance(value, weakref.ReferenceType) and value() is cursor)
                for value in tracked
            )
        )

        connection.close()
        target.unlink()

        self.assertFalse(target.exists())
        self.assertEqual(guard.release_calls, 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            cursor.fetchone()

    def test_cross_thread_cursor_drop_and_gc_leave_no_stale_tracking(self):
        target = self.root / "cross-thread-cursor-gc.db"
        connection, _guard = self._open_counted(target)
        self.addCleanup(connection.close)
        gc.collect()
        baseline = len(connection._tracked_cursors)
        errors = []

        def worker(offset):
            try:
                for value in range(offset, offset + 1_000):
                    cursor = connection.cursor()
                    cursor.close()
                    del cursor
                    if value % 100 == 0:
                        gc.collect()
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(index * 1_000,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        gc.collect()

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len(connection._tracked_cursors), baseline)

    def test_native_backup_accepts_two_verified_connections(self):
        source, source_guard = self._open_counted(self.root / "backup-source.db")
        target, target_guard = self._open_counted(self.root / "backup-target.db")
        self.addCleanup(source.close)
        self.addCleanup(target.close)
        source.execute("CREATE TABLE backup_marker(value INTEGER NOT NULL)")
        source.execute("INSERT INTO backup_marker VALUES(42)")

        source.backup(target)

        self.assertEqual(
            target.execute("SELECT value FROM backup_marker").fetchone()[0],
            42,
        )
        source.close()
        target.close()
        self.assertEqual(source_guard.release_calls, 1)
        self.assertEqual(target_guard.release_calls, 1)

    def test_explicit_close_followed_by_gc_releases_guard_once(self):
        target = self.root / "explicit-close.db"
        connection, guard = self._open_counted(target)

        def assert_sqlite_closed_before_guard_release():
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")

        guard.before_release = assert_sqlite_closed_before_guard_release
        connection.close()
        guard.before_release = None
        del connection
        for _ in range(3):
            gc.collect()
        self.assertEqual(guard.release_calls, 1)

    def test_context_manager_preserves_native_commit_and_connection_lifetime(self):
        target = self.root / "context-native.db"
        connection, guard = self._open_counted(target)
        self.addCleanup(connection.close)
        with connection as entered:
            self.assertIs(entered, connection)
            connection.execute("CREATE TABLE committed(value INTEGER NOT NULL)")
            connection.execute("INSERT INTO committed VALUES(42)")

        self.assertEqual(guard.release_calls, 0)
        self.assertEqual(connection.execute("SELECT value FROM committed").fetchone()[0], 42)
        connection.close()
        self.assertEqual(guard.release_calls, 1)

    def test_v2_expected_identity_precedes_path_only_isolation_callback(self):
        target = self.root / "identity-sample-gap.db"
        replacement = self.root / "replacement-v2.db"
        replacement_connection = sqlite3.connect(replacement, isolation_level=None)
        try:
            replacement_connection.execute("PRAGMA journal_mode=DELETE")
            replacement_connection.execute("CREATE TABLE replacement(value TEXT NOT NULL)")
            replacement_connection.execute("INSERT INTO replacement VALUES('different')")
        finally:
            replacement_connection.close()

        original_before = (
            self.v2.read_bytes(),
            self.v2.stat().st_mtime_ns,
            self._journal_mode(self.v2),
            self._sidecars(self.v2),
        )
        replacement_before = (
            replacement.read_bytes(),
            replacement.stat().st_mtime_ns,
            self._journal_mode(replacement),
            self._sidecars(replacement),
        )
        real_assert_isolated = store_module.assert_isolated_db
        attack = {"performed": False}

        def assert_then_replace(v3_path, v2_path):
            real_assert_isolated(v3_path, v2_path)
            os.replace(self.v2, target)
            os.replace(replacement, self.v2)
            attack["performed"] = True

        with mock.patch.object(
            store_module,
            "assert_isolated_db",
            side_effect=assert_then_replace,
        ):
            with mock.patch.object(sqlite3, "connect", wraps=sqlite3.connect) as connect:
                with self.assertRaises(StoreConfigurationError) as caught:
                    connection = open_store(target, v2_db_path=self.v2)
                    connection.close()
        self.assertIn(
            caught.exception.error_code,
            {"v2_db_identity_changed", "v2_v3_db_same_file"},
        )
        self.assertTrue(attack["performed"])
        connect.assert_not_called()
        self.assertEqual(
            (
                target.read_bytes(),
                target.stat().st_mtime_ns,
                self._journal_mode(target),
                self._sidecars(target),
            ),
            original_before,
        )
        self.assertEqual(
            (
                self.v2.read_bytes(),
                self.v2.stat().st_mtime_ns,
                self._journal_mode(self.v2),
                self._sidecars(self.v2),
            ),
            replacement_before,
        )

    def test_first_v2_resolution_identity_is_the_handshake_credential(self):
        target = self.root / "first-v2-observation.db"
        replacement = self.root / "first-v2-replacement.db"
        sqlite3.connect(replacement).close()
        real_candidate_identity = store_module._candidate_identity
        attack = {"performed": False}

        def resolve_then_replace(path, *, role):
            result = real_candidate_identity(path, role=role)
            if role == "v2" and not attack["performed"]:
                os.replace(self.v2, target)
                os.replace(replacement, self.v2)
                attack["performed"] = True
            return result

        with mock.patch.object(
            store_module,
            "_candidate_identity",
            side_effect=resolve_then_replace,
        ):
            with mock.patch.object(sqlite3, "connect", wraps=sqlite3.connect) as connect:
                with self.assertRaises(StoreConfigurationError) as caught:
                    connection = open_store(target, v2_db_path=self.v2)
                    connection.close()
        self.assertIn(
            caught.exception.error_code,
            {"v2_db_identity_changed", "v2_v3_db_same_file"},
        )
        self.assertTrue(attack["performed"])
        connect.assert_not_called()

    def test_failed_verified_open_releases_v2_and_v3_guards_once_each(self):
        target = self.root / "failed-open.db"
        other = self.root / "other.db"
        sqlite3.connect(other).close()
        bundle_class = (
            store_module._WindowsGuardBundle
            if os.name == "nt"
            else store_module._LinuxGuardBundle
        )
        real_release = bundle_class.release
        releases = {}

        def tracked_release(bundle):
            identity = id(bundle)
            releases[identity] = releases.get(identity, 0) + 1
            return real_release(bundle)

        real_connect = sqlite3.connect

        def wrong_connect(_requested, *args, **kwargs):
            return real_connect(other, isolation_level=None)

        with mock.patch.object(bundle_class, "release", tracked_release):
            with mock.patch.object(sqlite3, "connect", side_effect=wrong_connect):
                with self.assertRaises(StoreConfigurationError):
                    open_store(target, v2_db_path=self.v2)
        self.assertEqual(len(releases), 2)
        self.assertEqual(set(releases.values()), {1})

    def test_cross_thread_close_closes_sqlite_before_releasing_guard_once(self):
        target = self.root / "cross-thread-close.db"
        ready = threading.Event()
        close_done = threading.Event()
        post_close_checked = threading.Event()
        cleanup = threading.Event()
        shared = {"close_errors": [], "post_close_errors": [], "release_errors": []}

        def creator():
            connection, guard = self._open_counted(target)
            shared["connection"] = connection
            shared["guard"] = guard
            ready.set()
            close_done.wait(5)
            try:
                connection.execute("SELECT 1")
            except Exception as exc:
                shared["post_close_errors"].append(exc)
            finally:
                post_close_checked.set()
            cleanup.wait(5)
            connection.close()

        creator_thread = threading.Thread(target=creator)
        creator_thread.start()
        self.assertTrue(ready.wait(5))
        connection = shared["connection"]
        guard = shared["guard"]

        def before_release():
            try:
                sqlite3.Connection.execute(connection, "SELECT 1")
            except Exception as exc:
                shared["release_errors"].append(exc)

        guard.before_release = before_release

        def closer():
            try:
                connection.close()
            except Exception as exc:
                shared["close_errors"].append(exc)
            finally:
                close_done.set()

        closer_thread = threading.Thread(target=closer)
        closer_thread.start()
        closer_thread.join(5)
        try:
            self.assertFalse(closer_thread.is_alive())
            self.assertTrue(post_close_checked.wait(5))
            self.assertEqual(shared["close_errors"], [])
            self.assertEqual(len(shared["release_errors"]), 1)
            self.assertIsInstance(shared["release_errors"][0], sqlite3.ProgrammingError)
            self.assertIn("closed", str(shared["release_errors"][0]).lower())
            self.assertEqual(len(shared["post_close_errors"]), 1)
            self.assertIn("closed", str(shared["post_close_errors"][0]).lower())
            self.assertEqual(guard.release_calls, 1)
            self.assertIsNone(connection._identity_guard)
        finally:
            cleanup.set()
            creator_thread.join(5)
        self.assertFalse(creator_thread.is_alive())

    def test_connection_cycle_collected_in_another_thread_closes_before_release(self):
        target = self.root / "cross-thread-gc.db"
        shared = {"events": []}

        def creator():
            connection, guard = self._open_counted(target)
            cycle = [connection]
            cycle.append(cycle)
            shared["cycle"] = cycle
            shared["connection_ref"] = weakref.ref(connection)
            shared["guard"] = guard
            guard.before_release = lambda: shared["events"].append("guard_release")

        creator_thread = threading.Thread(target=creator)
        creator_thread.start()
        creator_thread.join(5)
        self.assertFalse(creator_thread.is_alive())
        guard = shared["guard"]

        def collector():
            cycle = shared.pop("cycle")
            del cycle
            for _ in range(3):
                gc.collect()

        real_connection_api = sqlite3.Connection

        class ObservedConnectionAPI:
            @staticmethod
            def close(connection):
                result = real_connection_api.close(connection)
                shared["events"].append("sqlite_closed")
                return result

        with mock.patch.object(
            store_module.sqlite3,
            "Connection",
            ObservedConnectionAPI,
        ):
            collector_thread = threading.Thread(target=collector)
            collector_thread.start()
            collector_thread.join(5)
        self.assertFalse(collector_thread.is_alive())
        self.assertEqual(shared["events"][:2], ["sqlite_closed", "guard_release"])
        self.assertEqual(shared["events"].count("guard_release"), 1)
        self.assertEqual(guard.release_calls, 1)
        self.assertIsNone(shared["connection_ref"]())

    def test_base_close_failure_retains_guard_until_successful_retry(self):
        target = self.root / "base-close-failure.db"
        connection, guard = self._open_counted(target)
        self.addCleanup(connection.close)

        class FailingConnectionAPI:
            @staticmethod
            def close(_connection):
                raise sqlite3.OperationalError("injected base close failure")

        with mock.patch.object(store_module.sqlite3, "Connection", FailingConnectionAPI):
            with self.assertRaisesRegex(sqlite3.OperationalError, "injected base close"):
                connection.close()

        self.assertIs(connection._identity_guard, guard)
        self.assertEqual(guard.release_calls, 0)
        self.assertEqual(connection.execute("SELECT 42").fetchone()[0], 42)

        release_errors = []

        def before_release():
            try:
                sqlite3.Connection.execute(connection, "SELECT 1")
            except Exception as exc:
                release_errors.append(exc)

        guard.before_release = before_release
        connection.close()
        self.assertEqual(len(release_errors), 1)
        self.assertIn("closed", str(release_errors[0]).lower())
        self.assertEqual(guard.release_calls, 1)

    def test_close_finalizes_every_documented_cursor_path_with_base_cursor_close(self):
        target = self.root / "tracked-cursors.db"
        connection, guard = self._open_counted(target)
        connection.execute("CREATE TABLE tracked(value INTEGER)")

        class DefiantCursor(sqlite3.Cursor):
            def close(self):
                raise AssertionError("subclass close override must not run")

        cursors = []
        manual = connection.cursor()
        manual.execute("SELECT 1")
        cursors.append(manual)
        cursors.append(connection.execute("SELECT 2"))
        cursors.append(connection.executemany("INSERT INTO tracked VALUES(?)", ((1,), (2,))))
        cursors.append(connection.executescript("SELECT 3;"))
        custom = connection.cursor(factory=DefiantCursor)
        custom.execute("SELECT 4")
        cursors.append(custom)

        connection.close()

        self.assertEqual(guard.release_calls, 1)
        self.assertIsNone(connection._identity_guard)
        self.assertEqual(connection._tracked_cursors, {})
        for cursor in cursors:
            with self.subTest(cursor=type(cursor).__name__):
                with self.assertRaises(sqlite3.ProgrammingError):
                    cursor.fetchone()

    @unittest.skipUnless(os.name == "nt", "Windows delete sharing is the native handle regression")
    def test_close_releases_actual_windows_sqlite_handle_while_cursor_object_is_retained(self):
        target = self.root / "windows-live-cursor.db"
        connection, guard = self._open_counted(target)
        cursor = connection.execute("SELECT 42")

        connection.close()
        target.unlink()

        self.assertFalse(target.exists())
        self.assertEqual(guard.release_calls, 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            cursor.fetchone()

    def test_tracked_cursor_close_failure_retains_guard_and_retry_releases_once(self):
        target = self.root / "cursor-close-failure.db"
        connection, guard = self._open_counted(target)
        first_cursor = connection.execute("SELECT 41")
        cursor = connection.execute("SELECT 42")

        real_cursor_api = sqlite3.Cursor
        closed = set()
        failure_injected = False

        class FailingCursorAPI:
            @staticmethod
            def close(candidate):
                nonlocal failure_injected
                identity = id(candidate)
                if identity in closed:
                    raise AssertionError("successfully closed cursor was retried")
                if closed and not failure_injected:
                    failure_injected = True
                    raise sqlite3.OperationalError("injected cursor close failure")
                real_cursor_api.close(candidate)
                closed.add(identity)

        with mock.patch.object(store_module.sqlite3, "Cursor", FailingCursorAPI):
            with self.assertRaisesRegex(sqlite3.OperationalError, "cursor close failure"):
                connection.close()

        self.assertIs(connection._identity_guard, guard)
        self.assertEqual(guard.release_calls, 0)
        with self.assertRaises(sqlite3.ProgrammingError):
            first_cursor.fetchone()
        self.assertEqual(cursor.fetchone()[0], 42)

        connection.close()
        self.assertTrue(failure_injected)
        self.assertEqual(guard.release_calls, 1)
        self.assertIsNone(connection._identity_guard)

    def test_guard_release_failure_retains_guard_until_successful_retry(self):
        target = self.root / "guard-release-failure.db"
        connection, guard = self._open_counted(target)
        connection.execute("SELECT 42")

        def fail_release():
            raise OSError("injected guard release failure")

        guard.before_release = fail_release
        with self.assertRaisesRegex(OSError, "guard release failure"):
            connection.close()
        self.assertIs(connection._identity_guard, guard)

        guard.before_release = None
        connection.close()
        self.assertIsNone(connection._identity_guard)
        self.assertEqual(guard.release_calls, 2)

    def test_cross_thread_close_with_live_cursor_closes_before_one_release(self):
        target = self.root / "cross-thread-live-cursor.db"
        connection, guard = self._open_counted(target)
        cursor = connection.execute("SELECT 42")
        errors = []

        thread = threading.Thread(
            target=lambda: self._close_in_thread(connection, errors),
        )
        thread.start()
        thread.join(5)

        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(guard.release_calls, 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            cursor.fetchone()

    @staticmethod
    def _close_in_thread(connection, errors):
        try:
            connection.close()
        except Exception as exc:
            errors.append(exc)

    def test_v3_production_code_does_not_construct_cursors_outside_connection_api(self):
        package = Path(store_module.__file__).resolve().parent
        offenders = []
        for path in package.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            if "sqlite3.Cursor(" in text:
                offenders.append(path.name)
            if path.name != "store.py" and "sqlite3.connect(" in text:
                offenders.append(path.name)
        self.assertEqual(offenders, [])

    def test_verified_open_requires_serialized_sqlite_thread_safety(self):
        target = self.root / "thread-safety-unavailable.db"
        def operation():
            connection = open_store(target, v2_db_path=self.v2)
            connection.close()

        with mock.patch.object(store_module.sqlite3, "threadsafety", 1):
            with mock.patch.object(sqlite3, "connect", wraps=sqlite3.connect) as connect:
                with self.assertRaises(StoreConfigurationError) as caught:
                    operation()
        self.assertEqual(
            caught.exception.error_code,
            "v3_sqlite_thread_safety_unavailable",
        )
        connect.assert_not_called()
        self.assertFalse(target.exists())


class V3StoreOuterCredentialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.v2 = self.root / "ai_edit_v2.db"
        self.replacement = self.root / "replacement_v2.db"
        self._create_marker_db(self.v2, "original")
        self._create_marker_db(self.replacement, "replacement")

    @staticmethod
    def _create_marker_db(path, marker):
        connection = sqlite3.connect(path, isolation_level=None)
        try:
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("CREATE TABLE identity_marker(value TEXT NOT NULL)")
            connection.execute("INSERT INTO identity_marker VALUES(?)", (marker,))
        finally:
            connection.close()

    @staticmethod
    def _sidecars(path):
        return tuple(
            Path(f"{path}{suffix}").exists()
            for suffix in ("-wal", "-shm", "-journal")
        )

    def _snapshot(self, path):
        connection = sqlite3.connect(path, isolation_level=None)
        try:
            mode = connection.execute("PRAGMA journal_mode").fetchone()[0].lower()
            marker = connection.execute("SELECT value FROM identity_marker").fetchone()[0]
            integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            connection.close()
        return (
            path.read_bytes(),
            path.stat().st_mtime_ns,
            mode,
            self._sidecars(path),
            marker,
            integrity,
        )

    def _swap_v2_onto_v3(self, target, attack):
        os.replace(self.v2, target)
        os.replace(self.replacement, self.v2)
        attack["performed"] = True

    def _assert_databases_preserved(self, target, attack, original_before, replacement_before):
        original_path = target if attack["performed"] else self.v2
        replacement_path = self.v2 if attack["performed"] else self.replacement
        self.assertEqual(self._snapshot(original_path), original_before)
        self.assertEqual(self._snapshot(replacement_path), replacement_before)

    def test_direct_open_pins_v2_before_first_v3_resolution(self):
        target = self.root / "direct-open-v3.db"
        original_before = self._snapshot(self.v2)
        replacement_before = self._snapshot(self.replacement)
        real_resolve = store_module.resolve_db_path
        attack = {"performed": False}

        def resolve_then_swap(value=None):
            result = real_resolve(value)
            if not attack["performed"]:
                self._swap_v2_onto_v3(target, attack)
            return result

        with mock.patch.object(store_module, "resolve_db_path", side_effect=resolve_then_swap):
            with mock.patch.object(sqlite3, "connect", wraps=sqlite3.connect) as connect:
                with self.assertRaises((StoreConfigurationError, OSError)):
                    connection = open_store(target, v2_db_path=self.v2)
                    connection.close()
        connect.assert_not_called()
        self._assert_databases_preserved(
            target,
            attack,
            original_before,
            replacement_before,
        )

    def test_init_db_carries_first_v2_pin_across_path_identity_seam(self):
        target = self.root / "init-v3.db"
        original_before = self._snapshot(self.v2)
        replacement_before = self._snapshot(self.replacement)
        real_path_identity = store_module._path_identity
        attack = {"performed": False}

        def identity_then_swap(path):
            result = real_path_identity(path)
            if path == target and not attack["performed"]:
                self._swap_v2_onto_v3(target, attack)
            return result

        with mock.patch.object(store_module, "_path_identity", side_effect=identity_then_swap):
            with mock.patch.object(sqlite3, "connect", wraps=sqlite3.connect) as connect:
                with self.assertRaises(
                    (StoreConfigurationError, StoreMigrationError, OSError)
                ):
                    init_db(target, v2_db_path=self.v2)
        connect.assert_not_called()
        self._assert_databases_preserved(
            target,
            attack,
            original_before,
            replacement_before,
        )

    def test_store_connect_carries_first_v2_pin_across_path_identity_seam(self):
        target = self.root / "store-v3.db"
        store = V3Store(target, v2_db_path=self.v2, environment="test")
        original_before = self._snapshot(self.v2)
        replacement_before = self._snapshot(self.replacement)
        real_path_identity = store_module._path_identity
        attack = {"performed": False}

        def identity_then_swap(path):
            result = real_path_identity(path)
            if path == target and not attack["performed"]:
                self._swap_v2_onto_v3(target, attack)
            return result

        with mock.patch.object(store_module, "_path_identity", side_effect=identity_then_swap):
            with mock.patch.object(sqlite3, "connect", wraps=sqlite3.connect) as connect:
                with self.assertRaises((StoreConfigurationError, OSError)):
                    connection = store._connect()
                    connection.close()
        connect.assert_not_called()
        self._assert_databases_preserved(
            target,
            attack,
            original_before,
            replacement_before,
        )


class V3StoreNativeCreateRaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.v2 = self.root / "ai_edit_v2.db"
        self.v2.write_bytes(b"V2 identity marker; never open")

    @unittest.skipUnless(os.name == "nt", "native Windows race probe")
    def test_exact_winerror_80_retries_inside_guard_and_revalidates(self):
        import ctypes

        target = self.root / "race-80.db"
        real_create = store_module._windows_create_handle
        injected = {"done": False}

        def create_with_race(path, *, directory, create_new=False, writable=False):
            if Path(path) == target and create_new and not injected["done"]:
                injected["done"] = True
                target.touch()
                raise ctypes.WinError(80)
            return real_create(
                path,
                directory=directory,
                create_new=create_new,
                writable=writable,
            )

        with mock.patch.object(
            store_module,
            "_windows_create_handle",
            side_effect=create_with_race,
        ):
            connection = open_store(target, v2_db_path=self.v2)
        try:
            self.assertTrue(injected["done"])
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        finally:
            connection.close()

    @unittest.skipUnless(os.name == "nt", "native Windows race probe")
    def test_winerror_183_and_code_less_file_exists_never_retry(self):
        import ctypes

        errors = (
            ("winerror-183", ctypes.WinError(183)),
            ("code-less", FileExistsError(errno.EEXIST, "synthetic collision")),
        )
        real_create = store_module._windows_create_handle
        for label, injected_error in errors:
            target = self.root / f"{label}.db"

            def create_with_error(
                path,
                *,
                directory,
                create_new=False,
                writable=False,
                _target=target,
                _error=injected_error,
            ):
                if Path(path) == _target and create_new:
                    _target.touch()
                    raise _error
                return real_create(
                    path,
                    directory=directory,
                    create_new=create_new,
                    writable=writable,
                )

            with self.subTest(label=label):
                with mock.patch.object(
                    store_module,
                    "_windows_create_handle",
                    side_effect=create_with_error,
                ):
                    with self.assertRaises(OSError) as caught:
                        connection = open_store(target, v2_db_path=self.v2)
                        connection.close()
                self.assertIs(caught.exception, injected_error)
                self.assertEqual(target.stat().st_size, 0)
                self.assertEqual(
                    tuple(
                        Path(f"{target}{suffix}").exists()
                        for suffix in ("-wal", "-shm", "-journal")
                    ),
                    (False, False, False),
                )

    def test_windows_and_linux_create_races_require_exact_native_codes(self):
        import ctypes

        win80 = ctypes.WinError(80) if os.name == "nt" else OSError("win80")
        if os.name != "nt":
            win80.winerror = 80
        win183 = ctypes.WinError(183) if os.name == "nt" else OSError("win183")
        if os.name != "nt":
            win183.winerror = 183
        no_winerror = FileExistsError(errno.EEXIST, "no native Windows code")
        linux_exists = FileExistsError(errno.EEXIST, "exists")
        linux_other = FileExistsError(errno.EACCES, "not EEXIST")

        self.assertTrue(store_module._is_exact_create_race(win80, windows=True))
        self.assertFalse(store_module._is_exact_create_race(win183, windows=True))
        self.assertFalse(store_module._is_exact_create_race(no_winerror, windows=True))
        self.assertTrue(store_module._is_exact_create_race(linux_exists, windows=False))
        self.assertFalse(store_module._is_exact_create_race(linux_other, windows=False))


class V3StoreFilesystemClassificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.v2 = self.root / "ai_edit_v2.db"
        self.v2.write_bytes(b"V2 identity marker; never open")

    def assert_code(self, expected, callable_, *args, **kwargs):
        with self.assertRaises(StoreConfigurationError) as caught:
            callable_(*args, **kwargs)
        self.assertEqual(caught.exception.error_code, expected)

    def test_filesystem_types_use_an_explicit_local_remote_unknown_classification(self):
        for fs_type in (
            "ext2", "ext3", "ext4", "xfs", "btrfs", "tmpfs", "overlay",
            "overlayfs", "windows_fixed",
        ):
            with self.subTest(fs_type=fs_type):
                self.assertEqual(store_module._classify_filesystem_type(fs_type), "local")
        for fs_type in (
            "nfs", "nfs4", "cifs", "smb3", "lustre", "gpfs", "afs",
            "davfs", "davfs2", "beegfs", "ceph", "cephfs", "glusterfs",
            "9p", "webdav", "fuse.sshfs", "fuse.cosfs", "fuse.s3fs",
            "fuse.gcsfuse", "fuse.goofys", "fuse.juicefs", "fuse.rclone",
            "azureblob", "windows_remote",
        ):
            with self.subTest(fs_type=fs_type):
                self.assertEqual(store_module._classify_filesystem_type(fs_type), "remote")
        for fs_type in (None, "", "mysteryfs", "fuse.unknown", "windows_ramdisk"):
            with self.subTest(fs_type=fs_type):
                self.assertEqual(store_module._classify_filesystem_type(fs_type), "unknown")

    def test_unknown_and_unapproved_fuse_fail_before_any_database_side_effect(self):
        for fs_type in ("mysteryfs", "fuse.unknown"):
            target = self.root / f"{fs_type.replace('.', '-')}.db"
            with self.subTest(fs_type=fs_type):
                with mock.patch.object(
                    store_module,
                    "_filesystem_type_for_path",
                    return_value=fs_type,
                ):
                    self.assert_code(
                        "v3_db_filesystem_unknown",
                        init_db,
                        target,
                        v2_db_path=self.v2,
                    )
                self.assertFalse(target.exists())
                self.assertEqual(
                    tuple(Path(f"{target}{suffix}").exists() for suffix in ("-wal", "-shm", "-journal")),
                    (False, False, False),
                )

    def test_existing_single_file_mount_is_checked_in_addition_to_parent(self):
        target = self.root / "single-file-mount.db"
        target.touch()
        seen = []

        def classify(path):
            seen.append(Path(path))
            return "nfs" if Path(path) == target else "ext4"

        with mock.patch.object(store_module, "_filesystem_type_for_path", side_effect=classify):
            self.assert_code(
                "v3_db_network_filesystem",
                open_store,
                target,
                v2_db_path=self.v2,
            )
        self.assertIn(target, seen)
        self.assertIn(target.parent, seen)
        self.assertEqual(target.stat().st_size, 0)

    def test_mountinfo_uses_decoding_component_boundaries_and_topmost_last_mount(self):
        text = "\n".join(
            (
                "1 0 0:1 / / rw - ext4 /dev/root rw",
                "2 1 0:2 / /mnt/data rw - nfs server:/data rw",
                "3 1 0:3 / /mnt/with\\040space rw - xfs /dev/x rw",
                "4 1 0:4 / /stack rw - ext4 /dev/a rw",
                "5 1 0:5 / /stack rw - nfs server:/stack rw",
            )
        )
        self.assertEqual(
            store_module._parse_linux_mountinfo(text, Path("/stack/db.sqlite")),
            "nfs",
        )
        self.assertEqual(
            store_module._parse_linux_mountinfo(
                text,
                Path("/mnt/with space/db.sqlite"),
            ),
            "xfs",
        )
        self.assertEqual(
            store_module._parse_linux_mountinfo(text, Path("/mnt/data2/db.sqlite")),
            "ext4",
        )
        self.assertIsNone(store_module._parse_linux_mountinfo("malformed", self.root))


class V3StoreLiveIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.v2 = self.root / "ai_edit_v2.db"
        self.v2.write_bytes(b"V2 identity marker; never open")

    def initialize(self, name="ai_edit_v3.db"):
        path = self.root / name
        init_db(path, v2_db_path=self.v2)
        return path

    def connect(self, path):
        connection = open_store(path, v2_db_path=self.v2)
        self.addCleanup(connection.close)
        return connection

    @staticmethod
    def _seed_minimum(connection, *, job_id="job-1"):
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
                   pricing_version,min_points,max_points,breakdown_json,expires_at,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            ("quote-1", "test", "alice", "{}", sha, "price-v1", 1, 2, "{}", 9, 1),
        )
        connection.execute(
            """INSERT INTO edit_v3_jobs(
                   job_id,environment,owner_id,state,normalized_request_json,request_sha256,
                   quote_id,idempotency_key,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (job_id, "test", "alice", "created_draft", "{}", sha, "quote-1", "key-1", 1, 1),
        )

    def test_every_connection_registers_bounded_canonical_json_validation(self):
        path = self.initialize()
        connection = self.connect(path)
        accepted = (
            "{}",
            '{"a":1,"b":[true,null,"é"]}',
        )
        rejected = (
            "{",
            '{"a":1,"a":2}',
            '{"b":2, "a":1}',
            '{"a":"\\u0061"}',
            '{"a":"e\u0301"}',
            "NaN",
        )
        for value in accepted:
            with self.subTest(accepted=value):
                self.assertEqual(
                    connection.execute(
                        "SELECT edit_v3_is_canonical_json(?)",
                        (value,),
                    ).fetchone()[0],
                    1,
                )
        for value in rejected:
            with self.subTest(rejected=value):
                self.assertEqual(
                    connection.execute(
                        "SELECT edit_v3_is_canonical_json(?)",
                        (value,),
                    ).fetchone()[0],
                    0,
                )

    def test_json_corruption_is_rejected_on_reinitialization(self):
        corrupt_values = (
            ("malformed", "{"),
            ("duplicate", '{"a":1,"a":2}'),
            ("whitespace-order", '{"b":2, "a":1}'),
            ("escape", '{"a":"\\u0061"}'),
            ("non-nfc", '{"a":"e\u0301"}'),
        )
        for label, value in corrupt_values:
            with self.subTest(label=label):
                path = self.initialize(f"json-{label}.db")
                connection = self.connect(path)
                connection.execute(
                    """INSERT INTO edit_v3_pricing_versions(
                           version,status,parameters_json,parameters_sha256,created_at
                       ) VALUES(?,?,?,?,?)""",
                    ("price-v1", "published", "{}", "a" * 64, 1),
                )
                connection.execute("PRAGMA ignore_check_constraints=ON")
                connection.execute(
                    "UPDATE edit_v3_pricing_versions SET parameters_json=?",
                    (value,),
                )
                connection.close()
                with self.assertRaises(StoreMigrationError) as caught:
                    init_db(path, v2_db_path=self.v2)
                self.assertEqual(caught.exception.error_code, "v3_integrity_check_failed")

    def test_orphan_foreign_key_is_rejected_on_reinitialization(self):
        path = self.initialize("orphan.db")
        connection = self.connect(path)
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute(
            """INSERT INTO edit_v3_stage_attempts(
                   id,job_id,stage,attempt,worker_id,fencing_token,status,
                   input_sha256,started_at
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            ("attempt-1", "missing", "planning", 1, "worker", 1, "running", "a" * 64, 1),
        )
        connection.close()
        with self.assertRaises(StoreMigrationError) as caught:
            init_db(path, v2_db_path=self.v2)
        self.assertEqual(caught.exception.error_code, "v3_foreign_key_check_failed")

    def test_check_constraint_corruption_is_rejected_on_reinitialization(self):
        path = self.initialize("check.db")
        connection = self.connect(path)
        self._seed_minimum(connection)
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute("UPDATE edit_v3_jobs SET repair_count=2 WHERE job_id='job-1'")
        connection.close()
        with self.assertRaises(StoreMigrationError) as caught:
            init_db(path, v2_db_path=self.v2)
        self.assertEqual(caught.exception.error_code, "v3_integrity_check_failed")

    def test_unregistered_trigger_view_and_indexes_are_rejected(self):
        objects = (
            (
                "trigger",
                """CREATE TRIGGER evil_trigger AFTER INSERT ON edit_v3_uploads
                    BEGIN UPDATE edit_v3_uploads SET owner_id='attacker'
                    WHERE upload_id=NEW.upload_id; END""",
            ),
            ("view", "CREATE VIEW evil_view AS SELECT * FROM edit_v3_jobs"),
            ("index", "CREATE INDEX evil_index ON edit_v3_uploads(owner_id)"),
            (
                "unique-index",
                "CREATE UNIQUE INDEX evil_unique ON edit_v3_uploads(owner_id,upload_id)",
            ),
        )
        for label, statement in objects:
            with self.subTest(label=label):
                path = self.initialize(f"object-{label}.db")
                connection = self.connect(path)
                connection.execute(statement)
                connection.close()
                with self.assertRaises(StoreMigrationError) as caught:
                    init_db(path, v2_db_path=self.v2)
                self.assertEqual(
                    caught.exception.error_code,
                    "v3_schema_manifest_mismatch",
                )

    def test_strict_tables_reject_wrong_integer_storage_class_at_write_boundary(self):
        path = self.initialize("strict.db")
        connection = self.connect(path)
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                """INSERT INTO edit_v3_pricing_versions(
                       version,status,parameters_json,parameters_sha256,created_at
                   ) VALUES(?,?,?,?,?)""",
                ("price-v1", "draft", "{}", "a" * 64, "not-an-integer"),
            )


class V3StorePromotionReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.db = self.root / "ai_edit_v3.db"
        self.v2 = self.root / "ai_edit_v2.db"
        self.v2.write_bytes(b"V2 identity marker; never open")
        self.store = V3Store(self.db, v2_db_path=self.v2, environment="test")

    def _complete_upload(
        self,
        upload_id,
        *,
        upload_type="material_image",
        mime_type="image/png",
        object_key=None,
        size=12,
        sha="a" * 64,
    ):
        object_key = object_key or f"test/ai-edit-v3/alice/{upload_id}.png"
        self.store.insert_upload(
            "alice",
            upload_id,
            upload_type=upload_type,
            object_key=object_key,
            declared_mime=mime_type,
            declared_size=size,
            expires_at=5_000,
            created_at=1_000,
        )
        return self.store.complete_upload(
            "alice",
            upload_id,
            observed_mime=mime_type,
            observed_size=size,
            observed_etag="etag",
            sha256=sha,
            duration_ms=None,
            width=2,
            height=3,
            probe={"safe": True},
            completed_at=1_100,
        )

    def test_uploaded_material_uses_authoritative_completed_image_metadata(self):
        upload = self._complete_upload("upload-authority")
        with self.assertRaises(StoreConflictError) as caught:
            self.store.insert_material(
                "alice",
                "material-malicious",
                source_kind="uploaded",
                upload_id=upload["upload_id"],
                cos_key="test/ai-edit-v3/alice/other.mp4",
                mime_type="video/mp4",
                size_bytes=999,
                sha256="b" * 64,
                metadata={},
                created_at=1_200,
            )
        self.assertEqual(caught.exception.error_code, "material_upload_metadata_mismatch")

    def test_upload_promotion_replays_by_upload_id_even_with_new_material_id(self):
        upload = self._complete_upload("upload-replay")
        first = self.store.insert_material(
            "alice",
            "material-first",
            source_kind="uploaded",
            upload_id=upload["upload_id"],
            cos_key=upload["object_key"],
            mime_type=upload["observed_mime"],
            size_bytes=upload["observed_size"],
            sha256=upload["sha256"],
            metadata={"role": "evidence"},
            created_at=1_200,
        )
        replay = self.store.insert_material(
            "alice",
            "material-new-client-id",
            source_kind="uploaded",
            upload_id=upload["upload_id"],
            cos_key=upload["object_key"],
            mime_type=upload["observed_mime"],
            size_bytes=upload["observed_size"],
            sha256=upload["sha256"],
            metadata={"role": "evidence"},
            created_at=1_200,
        )
        self.assertEqual(replay["material_id"], first["material_id"])
        with self.assertRaises(StoreConflictError) as caught:
            self.store.insert_material(
                "alice",
                "material-third-id",
                source_kind="uploaded",
                upload_id=upload["upload_id"],
                cos_key=upload["object_key"],
                mime_type=upload["observed_mime"],
                size_bytes=upload["observed_size"],
                sha256=upload["sha256"],
                metadata={"role": "changed"},
                created_at=1_200,
            )
        self.assertEqual(caught.exception.error_code, "idempotency_conflict")

    def test_upload_identity_divergence_wins_before_new_authority_validation(self):
        upload = self._complete_upload("upload-divergent-replay")
        self.store.insert_material(
            "alice",
            "material-original",
            source_kind="uploaded",
            upload_id=upload["upload_id"],
            cos_key=upload["object_key"],
            mime_type=upload["observed_mime"],
            size_bytes=upload["observed_size"],
            sha256=upload["sha256"],
            metadata={"role": "evidence"},
            created_at=1_200,
        )

        with self.assertRaises(StoreConflictError) as caught:
            self.store.insert_material(
                "alice",
                "material-new-id",
                source_kind="uploaded",
                upload_id=upload["upload_id"],
                cos_key="test/ai-edit-v3/alice/divergent.png",
                mime_type=upload["observed_mime"],
                size_bytes=upload["observed_size"],
                sha256=upload["sha256"],
                metadata={"role": "evidence"},
                created_at=1_200,
            )
        self.assertEqual(caught.exception.error_code, "idempotency_conflict")

    def test_material_id_divergence_wins_before_missing_upload_lookup(self):
        upload = self._complete_upload("upload-material-id")
        self.store.insert_material(
            "alice",
            "material-stable-id",
            source_kind="uploaded",
            upload_id=upload["upload_id"],
            cos_key=upload["object_key"],
            mime_type=upload["observed_mime"],
            size_bytes=upload["observed_size"],
            sha256=upload["sha256"],
            metadata={},
            created_at=1_200,
        )

        with self.assertRaises(StoreConflictError) as caught:
            self.store.insert_material(
                "alice",
                "material-stable-id",
                source_kind="uploaded",
                upload_id="missing-upload",
                cos_key="test/ai-edit-v3/alice/missing.png",
                mime_type="image/png",
                size_bytes=12,
                sha256="a" * 64,
                metadata={},
                created_at=1_200,
            )
        self.assertEqual(caught.exception.error_code, "idempotency_conflict")

    def test_material_replay_privacy_and_genuinely_new_missing_authority(self):
        upload = self._complete_upload("upload-private-replay")
        material = self.store.insert_material(
            "alice",
            "material-private",
            source_kind="uploaded",
            upload_id=upload["upload_id"],
            cos_key=upload["object_key"],
            mime_type=upload["observed_mime"],
            size_bytes=upload["observed_size"],
            sha256=upload["sha256"],
            metadata={},
            created_at=1_200,
        )
        self.assertEqual(
            self.store.insert_material(
                "alice",
                "material-private",
                source_kind="uploaded",
                upload_id=upload["upload_id"],
                cos_key=upload["object_key"],
                mime_type=upload["observed_mime"],
                size_bytes=upload["observed_size"],
                sha256=upload["sha256"],
                metadata={},
                created_at=1_200,
            ),
            material,
        )
        self.assertIsNone(
            self.store.insert_material(
                "bob",
                "material-private",
                source_kind="uploaded",
                upload_id="missing-upload",
                cos_key="test/ai-edit-v3/bob/private.png",
                mime_type="image/png",
                size_bytes=12,
                sha256="b" * 64,
                metadata={},
                created_at=1_200,
            )
        )
        self.assertIsNone(
            self.store.insert_material(
                "alice",
                "material-genuinely-new",
                source_kind="uploaded",
                upload_id="missing-upload",
                cos_key="test/ai-edit-v3/alice/missing.png",
                mime_type="image/png",
                size_bytes=12,
                sha256="a" * 64,
                metadata={},
                created_at=1_200,
            )
        )

    def test_promotion_rejects_non_material_upload_mime_and_illegal_source_unions(self):
        video = self._complete_upload(
            "upload-video",
            upload_type="main_video",
            mime_type="video/mp4",
            object_key="test/ai-edit-v3/alice/upload-video.mp4",
        )
        with self.assertRaises(StoreConflictError) as caught:
            self.store.insert_material(
                "alice",
                "material-video",
                source_kind="uploaded",
                upload_id=video["upload_id"],
                cos_key=video["object_key"],
                mime_type=video["observed_mime"],
                size_bytes=video["observed_size"],
                sha256=video["sha256"],
                metadata={},
                created_at=1_200,
            )
        self.assertEqual(caught.exception.error_code, "material_upload_invalid")

        illegal = (
            {"source_kind": "uploaded", "upload_id": None, "source_job_id": None},
            {"source_kind": "uploaded", "upload_id": "upload-video", "source_job_id": "job-1"},
            {"source_kind": "generated", "upload_id": "upload-video", "source_job_id": None},
            {"source_kind": "generated", "upload_id": None, "source_job_id": None},
            {"source_kind": "existing", "upload_id": None, "source_job_id": None},
        )
        for index, union in enumerate(illegal):
            with self.subTest(union=union):
                with self.assertRaises(StoreConfigurationError) as caught:
                    self.store.insert_material(
                        "alice",
                        f"illegal-{index}",
                        cos_key=f"test/ai-edit-v3/alice/illegal-{index}.png",
                        mime_type="image/png",
                        size_bytes=1,
                        sha256="c" * 64,
                        metadata={},
                        created_at=1_200,
                        **union,
                    )
                self.assertEqual(caught.exception.error_code, "material_source_invalid")


class V3StoreReplayAndIntegerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.db = self.root / "ai_edit_v3.db"
        self.v2 = self.root / "ai_edit_v2.db"
        self.v2.write_bytes(b"V2 identity marker; never open")
        self.store = V3Store(self.db, v2_db_path=self.v2, environment="test")
        self.store.insert_pricing_version(
            "price-v1",
            {"base": 1},
            status="published",
            created_at=1,
            published_at=1,
        )

    def _insert_quote(self, quote_id="quote-1"):
        return self.store.insert_quote(
            "alice",
            quote_id,
            {"input_type": "uploaded_video"},
            pricing_version="price-v1",
            min_points=1,
            max_points=2,
            breakdown={"base": 1},
            expires_at=100,
            created_at=2,
        )

    def _insert_job(self, job_id="job-1"):
        self._insert_quote()
        connection = self.store._connect()
        try:
            connection.execute(
                """INSERT INTO edit_v3_jobs(
                       job_id,environment,owner_id,state,normalized_request_json,request_sha256,
                       quote_id,idempotency_key,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    job_id,
                    "test",
                    "alice",
                    "created_draft",
                    "{}",
                    request_fingerprint({}),
                    "quote-1",
                    f"key-{job_id}",
                    3,
                    3,
                ),
            )
        finally:
            connection.close()

    def _complete_image(self, upload_id):
        key = f"test/ai-edit-v3/alice/{upload_id}.png"
        self.store.insert_upload(
            "alice",
            upload_id,
            upload_type="material_image",
            object_key=key,
            declared_mime="image/png",
            declared_size=12,
            expires_at=100,
            created_at=4,
        )
        return self.store.complete_upload(
            "alice",
            upload_id,
            observed_mime="image/png",
            observed_size=12,
            observed_etag="etag",
            sha256="a" * 64,
            duration_ms=None,
            width=2,
            height=3,
            probe={},
            completed_at=5,
        )

    def assert_idempotency_conflict(self, callable_, *args, **kwargs):
        with self.assertRaises(StoreConflictError) as caught:
            callable_(*args, **kwargs)
        self.assertEqual(caught.exception.error_code, "idempotency_conflict")

    def test_divergent_immutable_replays_use_one_stable_conflict_code(self):
        self.assert_idempotency_conflict(
            self.store.insert_pricing_version,
            "price-v1",
            {"base": 2},
            status="published",
            created_at=1,
            published_at=1,
        )
        quote = self._insert_quote()
        self.assert_idempotency_conflict(
            self.store.insert_quote,
            "alice",
            quote["quote_id"],
            {"input_type": "uploaded_video", "changed": True},
            pricing_version="price-v1",
            min_points=1,
            max_points=2,
            breakdown={"base": 1},
            expires_at=100,
            created_at=2,
        )
        self.store.insert_upload(
            "alice",
            "upload-replay",
            upload_type="material_image",
            object_key="test/ai-edit-v3/alice/upload-replay.png",
            declared_mime="image/png",
            declared_size=12,
            expires_at=100,
            created_at=4,
        )
        self.assert_idempotency_conflict(
            self.store.insert_upload,
            "alice",
            "upload-replay",
            upload_type="material_image",
            object_key="test/ai-edit-v3/alice/upload-replay.png",
            declared_mime="image/png",
            declared_size=13,
            expires_at=100,
            created_at=4,
        )
        completed = self._complete_image("upload-completion-replay")
        self.assert_idempotency_conflict(
            self.store.complete_upload,
            "alice",
            completed["upload_id"],
            observed_mime="image/png",
            observed_size=13,
            observed_etag="changed",
            sha256="b" * 64,
            duration_ms=None,
            width=2,
            height=3,
            probe={},
            completed_at=5,
        )
        material = self.store.insert_material(
            "alice",
            "material-replay",
            source_kind="uploaded",
            upload_id=completed["upload_id"],
            cos_key=completed["object_key"],
            mime_type=completed["observed_mime"],
            size_bytes=completed["observed_size"],
            sha256=completed["sha256"],
            metadata={},
            created_at=6,
        )
        self.assert_idempotency_conflict(
            self.store.insert_material,
            "alice",
            material["material_id"],
            source_kind="uploaded",
            upload_id=completed["upload_id"],
            cos_key=completed["object_key"],
            mime_type=completed["observed_mime"],
            size_bytes=completed["observed_size"],
            sha256=completed["sha256"],
            metadata={"changed": True},
            created_at=6,
        )

        self._insert_job()
        generated = self.store.insert_material(
            "alice",
            "generated-1",
            source_kind="generated",
            source_job_id="job-1",
            cos_key="test/ai-edit-v3/alice/generated-1.png",
            mime_type="image/png",
            size_bytes=1,
            sha256="c" * 64,
            metadata={},
            created_at=7,
        )
        self.store.bind_job_materials(
            "alice",
            "job-1",
            [{"material_id": generated["material_id"], "purpose": "evidence", "ordinal": 0}],
            created_at=8,
        )
        self.assert_idempotency_conflict(
            self.store.bind_job_materials,
            "alice",
            "job-1",
            [{"material_id": generated["material_id"], "purpose": "evidence", "ordinal": 0}],
            created_at=9,
        )

    def test_unrelated_uniqueness_conflict_keeps_its_specific_error(self):
        with self.assertRaises(StoreConflictError) as caught:
            self.store.insert_pricing_version(
                "price-v2",
                {"base": 2},
                status="published",
                created_at=2,
                published_at=2,
            )
        self.assertEqual(caught.exception.error_code, "published_pricing_conflict")

    def test_every_frozen_integer_primitive_rejects_bool_float_and_string_before_sqlite(self):
        pending_ids = iter(f"pending-{index}" for index in range(20))

        def invalid_completion(field, value):
            upload_id = next(pending_ids)
            self.store.insert_upload(
                "alice",
                upload_id,
                upload_type="material_image",
                object_key=f"test/ai-edit-v3/alice/{upload_id}.png",
                declared_mime="image/png",
                declared_size=1,
                expires_at=100,
                created_at=1,
            )
            values = {
                "observed_mime": "image/png",
                "observed_size": 1,
                "observed_etag": "etag",
                "sha256": "a" * 64,
                "duration_ms": None,
                "width": 1,
                "height": 1,
                "probe": {},
                "completed_at": 2,
            }
            values[field] = value
            return self.store.complete_upload("alice", upload_id, **values)

        cases = (
            ("pricing.created_at", lambda: self.store.insert_pricing_version("bad-p1", {}, status="draft", created_at=True)),
            ("pricing.published_at", lambda: self.store.insert_pricing_version("bad-p2", {}, status="draft", created_at=1, published_at=1.5)),
            ("pricing.retired_at", lambda: self.store.insert_pricing_version("bad-p3", {}, status="draft", created_at=1, retired_at="1")),
            ("quote.min_points", lambda: self.store.insert_quote("alice", "bad-q1", {}, pricing_version="price-v1", min_points=True, max_points=2, breakdown={}, expires_at=3, created_at=1)),
            ("quote.max_points", lambda: self.store.insert_quote("alice", "bad-q2", {}, pricing_version="price-v1", min_points=1, max_points=2.5, breakdown={}, expires_at=3, created_at=1)),
            ("quote.expires_at", lambda: self.store.insert_quote("alice", "bad-q3", {}, pricing_version="price-v1", min_points=1, max_points=2, breakdown={}, expires_at="3", created_at=1)),
            ("quote.created_at", lambda: self.store.insert_quote("alice", "bad-q4", {}, pricing_version="price-v1", min_points=1, max_points=2, breakdown={}, expires_at=3, created_at=True)),
            ("upload.declared_size", lambda: self.store.insert_upload("alice", "bad-u1", upload_type="material_image", object_key="test/u1", declared_mime="image/png", declared_size=1.5, expires_at=3, created_at=1)),
            ("upload.expires_at", lambda: self.store.insert_upload("alice", "bad-u2", upload_type="material_image", object_key="test/u2", declared_mime="image/png", declared_size=1, expires_at="3", created_at=1)),
            ("upload.created_at", lambda: self.store.insert_upload("alice", "bad-u3", upload_type="material_image", object_key="test/u3", declared_mime="image/png", declared_size=1, expires_at=3, created_at=True)),
            ("completion.observed_size", lambda: invalid_completion("observed_size", True)),
            ("completion.duration_ms", lambda: invalid_completion("duration_ms", 1.5)),
            ("completion.width", lambda: invalid_completion("width", "1")),
            ("completion.height", lambda: invalid_completion("height", True)),
            ("completion.completed_at", lambda: invalid_completion("completed_at", 1.5)),
            ("material.size_bytes", lambda: self.store.insert_material("alice", "bad-m1", source_kind="generated", source_job_id=None, cos_key="test/m1", mime_type="image/png", size_bytes=True, sha256="a" * 64, metadata={}, created_at=1)),
            ("material.created_at", lambda: self.store.insert_material("alice", "bad-m2", source_kind="generated", source_job_id=None, cos_key="test/m2", mime_type="image/png", size_bytes=1, sha256="a" * 64, metadata={}, created_at="1")),
            ("binding.ordinal", lambda: self.store.bind_job_materials("alice", "missing", [{"material_id": "m", "purpose": "p", "ordinal": 1.5}], created_at=1)),
            ("binding.created_at", lambda: self.store.bind_job_materials("alice", "missing", [], created_at="1")),
            ("pagination.limit.float", lambda: self.store.list_jobs_for_owner("alice", limit=1.5)),
            ("pagination.limit.string", lambda: self.store.list_jobs_for_owner("alice", limit="1")),
        )
        for label, operation in cases:
            with self.subTest(label=label):
                with self.assertRaises(StoreConfigurationError) as caught:
                    operation()
                self.assertEqual(caught.exception.error_code, "integer_argument_invalid")

    def test_signed_int64_boundaries_are_accepted_and_one_beyond_is_rejected(self):
        minimum = -(2**63)
        maximum = 2**63 - 1
        self.assertEqual(
            self.store.insert_pricing_version(
                "int64-min",
                {},
                status="draft",
                created_at=minimum,
            )["created_at"],
            minimum,
        )
        self.assertEqual(
            self.store.insert_pricing_version(
                "int64-max",
                {},
                status="draft",
                created_at=maximum,
            )["created_at"],
            maximum,
        )

        huge_cursor = base64.urlsafe_b64encode(
            canonical_json(
                {
                    "created_at": 2**63,
                    "environment": "test",
                    "job_id": "job",
                    "owner_id": "alice",
                }
            )
        ).rstrip(b"=").decode("ascii")
        cases = (
            (
                "created_at",
                lambda: self.store.insert_pricing_version(
                    "too-small", {}, status="draft", created_at=minimum - 1
                ),
            ),
            (
                "max_points",
                lambda: self.store.insert_quote(
                    "alice", "too-large-quote", {}, pricing_version="price-v1",
                    min_points=1, max_points=maximum + 1, breakdown={},
                    expires_at=3, created_at=1,
                ),
            ),
            (
                "declared_size",
                lambda: self.store.insert_upload(
                    "alice", "too-large-upload", upload_type="material_image",
                    object_key="test/too-large-upload", declared_mime="image/png",
                    declared_size=maximum + 1, expires_at=3, created_at=1,
                ),
            ),
            (
                "width",
                lambda: self.store.complete_upload(
                    "alice", "missing", observed_mime="image/png", observed_size=1,
                    observed_etag="etag", sha256="a" * 64, duration_ms=None,
                    width=maximum + 1, height=1, probe={}, completed_at=1,
                ),
            ),
            (
                "size_bytes",
                lambda: self.store.insert_material(
                    "alice", "too-large-material", source_kind="generated",
                    source_job_id="missing", cos_key="test/too-large-material",
                    mime_type="image/png", size_bytes=maximum + 1,
                    sha256="a" * 64, metadata={}, created_at=1,
                ),
            ),
            (
                "ordinal",
                lambda: self.store.bind_job_materials(
                    "alice", "missing",
                    [{"material_id": "m", "purpose": "p", "ordinal": maximum + 1}],
                    created_at=1,
                ),
            ),
            ("limit", lambda: self.store.list_jobs_for_owner("alice", limit=maximum + 1)),
            (
                "created_at",
                lambda: self.store.list_jobs_for_owner(
                    "alice", limit=1, cursor=huge_cursor
                ),
            ),
        )
        for field, operation in cases:
            with self.subTest(field=field):
                with self.assertRaises(StoreConfigurationError) as caught:
                    operation()
                self.assertEqual(caught.exception.error_code, "integer_out_of_range")
                self.assertIn(field, caught.exception.message)


class _WalCursor:
    def __init__(self, value=None):
        self.value = value

    def fetchone(self):
        return self.value


class _ControlledClock:
    def __init__(self):
        self.value = 0.0

    def monotonic(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class _ControlledWalConnection:
    def __init__(self, clock, outcomes, *, journal_elapsed=0.0):
        self.clock = clock
        self.outcomes = list(outcomes)
        self.journal_elapsed = journal_elapsed
        self.busy_timeout = 0
        self.timeout_history = []
        self.wal_attempts = 0

    @staticmethod
    def _error(message, error_code):
        error = sqlite3.OperationalError(message)
        error.sqlite_errorcode = error_code
        return error

    def execute(self, statement):
        normalized = " ".join(statement.split()).lower()
        if normalized.startswith("pragma busy_timeout="):
            self.busy_timeout = int(normalized.rsplit("=", 1)[1])
            self.timeout_history.append(self.busy_timeout)
            return _WalCursor()
        if normalized == "pragma busy_timeout":
            return _WalCursor((self.busy_timeout,))
        if normalized == "pragma journal_mode":
            self.clock.value += self.journal_elapsed
            return _WalCursor(("delete",))
        if normalized == "pragma journal_mode=wal":
            self.wal_attempts += 1
            outcome = self.outcomes.pop(0)
            self.clock.value += outcome.get("elapsed", 0.0)
            if outcome["kind"] == "ok":
                return _WalCursor(("wal",))
            raise self._error(outcome.get("message", "database is locked"), outcome["code"])
        raise AssertionError(f"unexpected statement: {statement}")


class V3StoreWalBudgetTests(unittest.TestCase):
    def test_integer_nonbusy_sqlite_error_code_never_falls_back_to_message_text(self):
        error = sqlite3.OperationalError("database is locked")
        error.sqlite_errorcode = sqlite3.SQLITE_IOERR
        self.assertFalse(store_module._is_sqlite_busy_or_locked(error))

    def test_wal_lock_wait_and_backoff_share_one_ten_second_monotonic_budget(self):
        clock = _ControlledClock()
        connection = _ControlledWalConnection(
            clock,
            (
                {"kind": "busy", "code": sqlite3.SQLITE_BUSY, "elapsed": 9.8},
                {"kind": "busy", "code": sqlite3.SQLITE_BUSY, "elapsed": 0.195},
            ),
        )
        with self.assertRaises(sqlite3.OperationalError):
            store_module._negotiate_wal(
                connection,
                monotonic=clock.monotonic,
                sleep=clock.sleep,
                budget_seconds=10.0,
            )
        self.assertLessEqual(clock.value, 10.001)
        self.assertEqual(connection.wal_attempts, 2)
        self.assertEqual(connection.timeout_history[0], 10_000)
        self.assertLess(connection.timeout_history[-1], 1_000)

    def test_journal_probe_and_wal_write_each_use_only_the_remaining_budget(self):
        clock = _ControlledClock()
        connection = _ControlledWalConnection(
            clock,
            ({"kind": "ok", "elapsed": 0.1},),
            journal_elapsed=9.8,
        )
        store_module._negotiate_wal(
            connection,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            budget_seconds=10.0,
        )
        self.assertLessEqual(clock.value, 10.001)
        self.assertEqual(connection.timeout_history[0], 10_000)
        self.assertLess(connection.timeout_history[-2], 1_000)
        self.assertEqual(connection.timeout_history[-1], 10_000)

    def test_wal_retries_busy_then_restores_ten_second_timeout_on_success(self):
        clock = _ControlledClock()
        connection = _ControlledWalConnection(
            clock,
            (
                {"kind": "busy", "code": sqlite3.SQLITE_LOCKED, "elapsed": 0.1},
                {"kind": "ok", "elapsed": 0.1},
            ),
        )
        store_module._negotiate_wal(
            connection,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
            budget_seconds=10.0,
        )
        self.assertEqual(connection.wal_attempts, 2)
        self.assertGreaterEqual(connection.busy_timeout, 10_000)

    def test_wal_does_not_retry_nonbusy_operational_error(self):
        clock = _ControlledClock()
        connection = _ControlledWalConnection(
            clock,
            (
                {
                    "kind": "busy",
                    "code": sqlite3.SQLITE_IOERR,
                    "elapsed": 0.1,
                    "message": "database is locked",
                },
            ),
        )
        with self.assertRaises(sqlite3.OperationalError):
            store_module._negotiate_wal(
                connection,
                monotonic=clock.monotonic,
                sleep=clock.sleep,
                budget_seconds=10.0,
            )
        self.assertEqual(connection.wal_attempts, 1)


class V3LeaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        self.db = root / "ai_edit_v3.db"
        self.v2 = root / "ai_edit_v2.db"
        self.v2.write_bytes(b"V2 identity marker; never open")
        self.store = V3Store(self.db, v2_db_path=self.v2, environment="test")
        self.store.insert_pricing_version(
            "price-v1",
            {"base": 1},
            status="published",
            created_at=1,
            published_at=1,
        )
        self.store.insert_quote(
            "alice",
            "quote-1",
            {},
            pricing_version="price-v1",
            min_points=1,
            max_points=1,
            breakdown={"base": 1},
            expires_at=9_999_999,
            created_at=1,
        )

    def seed_job(
        self,
        job_id,
        state="queued",
        *,
        queued_at=1,
        processing_deadline_at=None,
    ):
        connection = self.store._connect()
        try:
            connection.execute(
                """INSERT INTO edit_v3_jobs(
                       job_id,environment,owner_id,state,normalized_request_json,
                       request_sha256,quote_id,idempotency_key,queued_at,
                       processing_deadline_at,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    job_id,
                    "test",
                    "alice",
                    state,
                    "{}",
                    request_fingerprint({}),
                    "quote-1",
                    f"key-{job_id}",
                    queued_at,
                    processing_deadline_at,
                    1,
                    1,
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def job_row(self, job_id):
        connection = self.store._connect()
        try:
            return dict(
                connection.execute(
                    "SELECT * FROM edit_v3_jobs WHERE job_id=?", (job_id,)
                ).fetchone()
            )
        finally:
            connection.close()

    def protected_snapshot(self, job_id):
        connection = self.store._connect()
        try:
            job = dict(
                connection.execute(
                    "SELECT * FROM edit_v3_jobs WHERE job_id=?", (job_id,)
                ).fetchone()
            )
            attempts = [
                dict(row)
                for row in connection.execute(
                    """SELECT * FROM edit_v3_stage_attempts
                       WHERE job_id=? ORDER BY stage,attempt""",
                    (job_id,),
                )
            ]
            checkpoints = [
                dict(row)
                for row in connection.execute(
                    """SELECT * FROM edit_v3_checkpoints
                       WHERE job_id=? ORDER BY stage,version""",
                    (job_id,),
                )
            ]
            providers = [
                dict(row)
                for row in connection.execute(
                    """SELECT * FROM edit_v3_provider_tasks
                       WHERE job_id=? ORDER BY operation_key""",
                    (job_id,),
                )
            ]
            return canonical_json(
                {
                    "attempt_count": len(attempts),
                    "attempts": attempts,
                    "checkpoint_count": len(checkpoints),
                    "checkpoints": checkpoints,
                    "job": job,
                    "provider_count": len(providers),
                    "providers": providers,
                }
            )
        finally:
            connection.close()

    def test_task5_public_contract_surface_is_present_and_immutable(self):
        self.assertTrue(hasattr(contracts_module, "LeaseClaim"))
        self.assertTrue(hasattr(contracts_module, "ALL_STATES"))
        self.assertTrue(hasattr(contracts_module, "QUEUE_CLAIMABLE_STATES"))

        claim = contracts_module.LeaseClaim("job-1", "worker-1", 7, 123_000)
        self.assertFalse(hasattr(claim, "__dict__"))
        with self.assertRaises((AttributeError, TypeError)):
            claim.fencing_token = 8

    def test_claim_expiry_equality_and_renewal_never_shortens(self):
        self.assertTrue(hasattr(self.store, "claim_next_job"))
        self.seed_job("job-1")
        claim = self.store.claim_next_job("worker-a", 10, 100_000)
        self.assertEqual(
            claim,
            contracts_module.LeaseClaim("job-1", "worker-a", 1, 110_000),
        )
        self.assertTrue(self.store.lease_owned(claim, 109_999))
        self.assertFalse(self.store.lease_owned(claim, 110_000))

        self.assertTrue(self.store.renew_lease(claim, 5, 101_000))
        self.assertEqual(self.job_row("job-1")["lease_until"], 110_000)
        self.assertTrue(self.store.renew_lease(claim, 20, 102_000))
        self.assertEqual(self.job_row("job-1")["lease_until"], 122_000)

    def test_claim_fencing_token_int64_boundary_is_typed_and_atomic(self):
        int64_max = (1 << 63) - 1
        self.seed_job("exhausted")
        connection = self.store._connect()
        try:
            connection.execute(
                "UPDATE edit_v3_jobs SET fencing_token=? WHERE job_id=?",
                (int64_max, "exhausted"),
            )
            connection.commit()
        finally:
            connection.close()
        before = self.protected_snapshot("exhausted")
        with self.assertRaises(StoreConflictError) as captured:
            self.store.claim_job(
                "exhausted",
                "worker-a",
                10,
                100_000,
                expected_states={"queued"},
            )
        self.assertEqual(captured.exception.error_code, "fencing_token_exhausted")
        self.assertEqual(self.protected_snapshot("exhausted"), before)

        self.seed_job("last-safe")
        connection = self.store._connect()
        try:
            connection.execute(
                "UPDATE edit_v3_jobs SET fencing_token=? WHERE job_id=?",
                (int64_max - 1, "last-safe"),
            )
            connection.commit()
        finally:
            connection.close()
        claim = self.store.claim_job(
            "last-safe",
            "worker-b",
            10,
            100_000,
            expected_states={"queued"},
        )
        self.assertEqual(claim.fencing_token, int64_max)

    def test_claim_next_is_deterministic_and_excludes_terminal_and_reconciliation(self):
        self.assertTrue(hasattr(self.store, "claim_next_job"))
        self.seed_job("terminal", "completed", queued_at=0)
        self.seed_job("reconcile", "billing_reconciling", queued_at=0)
        self.seed_job("job-b", queued_at=2)
        self.seed_job("job-a", queued_at=2)
        first = self.store.claim_next_job("worker-a", 10, 100_000)
        second = self.store.claim_next_job("worker-b", 10, 100_000)
        self.assertEqual(first.job_id, "job-a")
        self.assertEqual(second.job_id, "job-b")
        self.assertIsNone(self.store.claim_next_job("worker-c", 10, 100_000))

    def test_named_claim_filters_actual_state_and_never_claims_terminal(self):
        self.assertTrue(hasattr(self.store, "claim_job"))
        self.seed_job("reconcile", "billing_reconciling")
        self.seed_job("terminal", "refunded")
        self.assertIsNone(
            self.store.claim_job(
                "reconcile", "worker-a", 10, 100_000, expected_states={"settling"}
            )
        )
        claim = self.store.claim_job(
            "reconcile",
            "worker-a",
            10,
            100_000,
            expected_states={"billing_reconciling"},
        )
        self.assertEqual(claim.job_id, "reconcile")
        self.assertIsNone(
            self.store.claim_job(
                "terminal", "worker-b", 10, 100_000, expected_states={"refunded"}
            )
        )

    def test_expired_worker_cannot_write_after_reclaim(self):
        self.assertTrue(hasattr(self.store, "transition_leased"))
        self.seed_job("job-1")
        old = self.store.claim_next_job("worker-a", 10, 100_000)
        new = self.store.claim_next_job("worker-b", 10, 110_000)
        self.assertGreater(new.fencing_token, old.fencing_token)
        before = self.job_row("job-1")
        self.assertFalse(
            self.store.transition_leased(
                old,
                {"queued"},
                "generating_voice",
                110_001,
                lease_seconds=10,
            )
        )
        self.assertEqual(self.job_row("job-1"), before)
        self.assertTrue(
            self.store.transition_leased(
                new,
                {"queued"},
                "generating_voice",
                110_001,
                lease_seconds=10,
            )
        )

    def test_transition_preserves_deadline_and_grants_repair_budget_once(self):
        self.assertTrue(hasattr(self.store, "transition_leased"))
        self.seed_job(
            "job-1",
            "quality_checking",
            processing_deadline_at=5_000_000,
        )
        claim = self.store.claim_next_job("worker-a", 30, 100_000)
        self.assertTrue(
            self.store.transition_leased(
                claim,
                {"quality_checking"},
                "repair_planning",
                101_000,
                lease_seconds=30,
            )
        )
        first = self.job_row("job-1")
        self.assertEqual(first["repair_count"], 1)
        self.assertEqual(first["repair_budget_granted_at"], 101_000)
        self.assertEqual(first["processing_deadline_at"], 5_600_000)
        self.assertTrue(
            self.store.transition_leased(
                claim,
                {"quality_checking"},
                "repair_planning",
                102_000,
                lease_seconds=30,
            )
        )
        replay = self.job_row("job-1")
        self.assertEqual(replay["processing_deadline_at"], 5_600_000)
        self.assertEqual(replay["repair_budget_granted_at"], 101_000)

        connection = self.store._connect()
        try:
            connection.execute(
                "UPDATE edit_v3_jobs SET state='quality_checking' WHERE job_id='job-1'"
            )
            connection.commit()
        finally:
            connection.close()
        self.assertFalse(
            self.store.transition_leased(
                claim,
                {"quality_checking"},
                "repair_planning",
                103_000,
                lease_seconds=30,
            )
        )
        self.assertEqual(self.job_row("job-1")["processing_deadline_at"], 5_600_000)

    def test_repair_deadline_int64_boundary_is_typed_and_atomic(self):
        int64_max = (1 << 63) - 1
        self.seed_job(
            "overflow",
            "quality_checking",
            processing_deadline_at=int64_max - 599_999,
        )
        overflow = self.store.claim_job(
            "overflow",
            "worker-a",
            30,
            100_000,
            expected_states={"quality_checking"},
        )
        before = self.protected_snapshot("overflow")
        with self.assertRaises(StoreConflictError) as captured:
            self.store.transition_leased(
                overflow,
                {"quality_checking"},
                "repair_planning",
                101_000,
                lease_seconds=30,
            )
        self.assertEqual(
            captured.exception.error_code, "processing_deadline_overflow"
        )
        self.assertEqual(self.protected_snapshot("overflow"), before)

        self.seed_job(
            "last-safe",
            "quality_checking",
            processing_deadline_at=int64_max - 600_000,
        )
        last_safe = self.store.claim_job(
            "last-safe",
            "worker-b",
            30,
            100_000,
            expected_states={"quality_checking"},
        )
        self.assertTrue(
            self.store.transition_leased(
                last_safe,
                {"quality_checking"},
                "repair_planning",
                101_000,
                lease_seconds=30,
            )
        )
        self.assertEqual(
            self.job_row("last-safe")["processing_deadline_at"], int64_max
        )

    def test_invalid_edge_is_rejected_before_sql_and_terminal_clears_lease(self):
        self.assertTrue(hasattr(self.store, "transition_leased"))
        self.seed_job("job-1", "publishing", processing_deadline_at=9_000_000)
        claim = self.store.claim_job(
            "job-1", "worker-a", 30, 100_000, expected_states={"publishing"}
        )
        before = self.job_row("job-1")
        with self.assertRaises(StoreConfigurationError):
            self.store.transition_leased(
                claim,
                {"publishing"},
                "queued",
                101_000,
                lease_seconds=30,
            )
        self.assertEqual(self.job_row("job-1"), before)
        self.assertTrue(
            self.store.transition_leased(
                claim,
                {"publishing"},
                "completed",
                101_000,
                lease_seconds=30,
            )
        )
        row = self.job_row("job-1")
        self.assertEqual(row["state"], "completed")
        self.assertIsNone(row["worker_id"])
        self.assertIsNone(row["lease_until"])
        self.assertEqual(row["processing_deadline_at"], 9_000_000)

    def test_stage_attempt_checkpoint_replay_versions_and_skipped(self):
        self.assertTrue(hasattr(self.store, "start_stage_attempt"))
        self.seed_job("job-1")
        claim = self.store.claim_next_job("worker-a", 30, 100_000)
        attempt = self.store.start_stage_attempt(
            claim, "queued", "a" * 64, 101_000
        )
        self.assertEqual(attempt["attempt"], 1)
        self.assertEqual(
            self.store.start_stage_attempt(claim, "queued", "a" * 64, 102_000),
            attempt,
        )
        with self.assertRaises(StoreConflictError):
            self.store.start_stage_attempt(claim, "queued", "b" * 64, 102_000)

        checkpoint = self.store.save_checkpoint(
            claim,
            attempt["id"],
            "a" * 64,
            {"z": 2, "a": 1},
            103_000,
        )
        self.assertEqual(checkpoint["version"], 1)
        self.assertEqual(checkpoint["output_json"], '{"a":1,"z":2}')
        self.assertEqual(
            self.store.save_checkpoint(
                claim,
                attempt["id"],
                "a" * 64,
                {"a": 1, "z": 2},
                104_000,
            ),
            checkpoint,
        )
        with self.assertRaises(StoreConflictError):
            self.store.save_checkpoint(
                claim, attempt["id"], "a" * 64, {"a": 9}, 104_000
            )
        finished = self.store.finish_stage_attempt(
            claim, attempt["id"], "skipped", 105_000
        )
        self.assertEqual(finished["status"], "skipped")
        self.assertEqual(
            self.store.get_checkpoint_for_claim(claim, "queued", "a" * 64, 105_000),
            checkpoint,
        )

        self.assertTrue(self.store.release_lease(claim, 106_000))
        replacement = self.store.claim_next_job("worker-b", 30, 107_000)
        second_attempt = self.store.start_stage_attempt(
            replacement, "queued", "b" * 64, 108_000
        )
        second = self.store.save_checkpoint(
            replacement,
            second_attempt["id"],
            "b" * 64,
            {"version": 2},
            109_000,
        )
        self.assertEqual(second["version"], 2)

    def test_release_refuses_running_attempt_and_close_is_fenced(self):
        self.assertTrue(hasattr(self.store, "close_running_attempts"))
        self.seed_job("job-1")
        old = self.store.claim_next_job("worker-a", 10, 100_000)
        attempt = self.store.start_stage_attempt(old, "queued", "a" * 64, 101_000)
        with self.assertRaises(StoreConflictError):
            self.store.release_lease(old, 102_000)
        self.assertEqual(self.store.close_running_attempts(old, 102_000), 1)
        self.assertEqual(self.store.close_running_attempts(old, 102_000), 0)
        self.assertTrue(self.store.release_lease(old, 102_000))
        replacement = self.store.claim_next_job("worker-b", 10, 103_000)
        self.assertGreater(replacement.fencing_token, old.fencing_token)
        self.assertEqual(
            self.store.close_running_attempts(replacement, 104_000), 0
        )
        self.assertFalse(self.store.release_lease(old, 104_000))
        connection = self.store._connect()
        try:
            row = connection.execute(
                "SELECT status FROM edit_v3_stage_attempts WHERE id=?",
                (attempt["id"],),
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(row["status"], "aborted_lease_lost")

    def test_reclaim_update_zero_rolls_back_old_attempt_closure(self):
        self.assertTrue(hasattr(self.store, "start_stage_attempt"))
        self.seed_job("job-1")
        old = self.store.claim_next_job("worker-a", 10, 100_000)
        attempt = self.store.start_stage_attempt(old, "queued", "a" * 64, 101_000)
        connection = self.store._connect()
        try:
            connection.execute(
                """CREATE TRIGGER reject_worker_b BEFORE UPDATE OF worker_id
                   ON edit_v3_jobs WHEN NEW.worker_id='worker-b'
                   BEGIN SELECT RAISE(IGNORE); END"""
            )
            connection.commit()
        finally:
            connection.close()
        self.assertIsNone(self.store.claim_next_job("worker-b", 10, 110_000))
        row = self.job_row("job-1")
        self.assertEqual(row["worker_id"], "worker-a")
        self.assertEqual(row["fencing_token"], old.fencing_token)
        connection = self.store._connect()
        try:
            status = connection.execute(
                "SELECT status FROM edit_v3_stage_attempts WHERE id=?",
                (attempt["id"],),
            ).fetchone()["status"]
        finally:
            connection.close()
        self.assertEqual(status, "running")

    def test_provider_intent_is_immutable_and_new_token_recovers_result(self):
        self.assertTrue(hasattr(self.store, "record_provider_intent"))
        self.seed_job("job-1")
        old = self.store.claim_next_job("worker-a", 30, 100_000)
        attempt = self.store.start_stage_attempt(old, "queued", "a" * 64, 101_000)
        intent = self.store.record_provider_intent(
            old,
            "queued",
            attempt["id"],
            "provider-a",
            "render",
            "op-1",
            "b" * 64,
            102_000,
        )
        self.assertEqual(intent["status"], "intent_recorded")
        self.assertIsNone(intent["external_id"])
        self.assertEqual(
            self.store.record_provider_intent(
                old,
                "queued",
                attempt["id"],
                "provider-a",
                "render",
                "op-1",
                "b" * 64,
                103_000,
            ),
            intent,
        )
        with self.assertRaises(StoreConflictError):
            self.store.record_provider_intent(
                old,
                "queued",
                attempt["id"],
                "provider-a",
                "render",
                "op-1",
                "c" * 64,
                103_000,
            )
        self.store.finish_stage_attempt(old, attempt["id"], "completed", 104_000)
        self.assertTrue(self.store.release_lease(old, 105_000))
        new = self.store.claim_next_job("worker-b", 30, 106_000)
        self.assertEqual(
            self.store.get_provider_task_for_claim(new, "op-1", 107_000)["id"],
            intent["id"],
        )
        before = self.store.get_provider_task_for_claim(new, "op-1", 107_000)
        with self.assertRaises(store_module.LeaseLost):
            self.store.bind_provider_result(
                old,
                "op-1",
                "external-1",
                "done",
                {"z": 2, "a": 1},
                107_000,
            )
        self.assertEqual(
            self.store.get_provider_task_for_claim(new, "op-1", 107_000), before
        )
        bound = self.store.bind_provider_result(
            new,
            "op-1",
            "external-1",
            "done",
            {"z": 2, "a": 1},
            108_000,
        )
        self.assertEqual(bound["result_json"], '{"a":1,"z":2}')
        self.assertEqual(bound["fencing_token"], new.fencing_token)
        self.assertEqual(
            self.store.bind_provider_result(
                new,
                "op-1",
                "external-1",
                "done",
                {"a": 1, "z": 2},
                109_000,
            ),
            bound,
        )
        with self.assertRaises(StoreConflictError):
            self.store.bind_provider_result(
                new,
                "op-1",
                "external-2",
                "done",
                {"a": 1, "z": 2},
                109_000,
            )

    def test_closed_attempt_replays_existing_intent_but_rejects_new_intent(self):
        for index, status in enumerate(("completed", "failed", "skipped"), 1):
            with self.subTest(status=status):
                job_id = f"closed-{status}"
                operation_key = f"op-existing-{status}"
                self.seed_job(job_id, queued_at=index)
                claim = self.store.claim_job(
                    job_id,
                    f"worker-{index}",
                    30,
                    100_000,
                    expected_states={"queued"},
                )
                attempt = self.store.start_stage_attempt(
                    claim, "queued", f"{index}" * 64, 101_000
                )
                intent = self.store.record_provider_intent(
                    claim,
                    "queued",
                    attempt["id"],
                    "provider-a",
                    "render",
                    operation_key,
                    "a" * 64,
                    102_000,
                )
                if status == "skipped":
                    self.store.save_checkpoint(
                        claim,
                        attempt["id"],
                        f"{index}" * 64,
                        {"skipped": True},
                        103_000,
                    )
                self.store.finish_stage_attempt(
                    claim, attempt["id"], status, 104_000
                )
                self.assertEqual(
                    self.store.record_provider_intent(
                        claim,
                        "queued",
                        attempt["id"],
                        "provider-a",
                        "render",
                        operation_key,
                        "a" * 64,
                        105_000,
                    ),
                    intent,
                )
                before = self.protected_snapshot(job_id)
                with self.assertRaises(StoreConflictError) as captured:
                    self.store.record_provider_intent(
                        claim,
                        "queued",
                        attempt["id"],
                        "provider-a",
                        "render",
                        f"op-new-{status}",
                        "b" * 64,
                        105_000,
                    )
                self.assertEqual(
                    captured.exception.error_code, "provider_attempt_not_running"
                )
                self.assertEqual(self.protected_snapshot(job_id), before)

    def test_top_level_claim_has_one_winner_and_matches_store_surface(self):
        self.assertTrue(hasattr(store_module, "claim_next_job"))
        self.seed_job("job-1")
        barrier = threading.Barrier(2)
        results = []
        failures = []

        def worker(worker_id):
            try:
                barrier.wait()
                with mock.patch.dict(
                    os.environ, {"AI_EDIT_V2_DB": os.fspath(self.v2)}, clear=False
                ):
                    results.append(
                        store_module.claim_next_job(
                            worker_id, 10, 100_000, db_path=self.db
                        )
                    )
            except Exception as exc:  # pragma: no cover - diagnostic capture
                failures.append(exc)

        threads = [
            threading.Thread(target=worker, args=(f"worker-{index}",))
            for index in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertFalse(failures)
        self.assertEqual(sum(result is not None for result in results), 1)
        winner = next(result for result in results if result is not None)
        self.assertTrue(self.store.lease_owned(winner, 100_001))

    def test_protected_read_and_exact_replay_sql_are_fenced_in_one_statement(self):
        self.seed_job("job-1")
        claim = self.store.claim_next_job("worker-a", 30, 100_000)
        attempt = self.store.start_stage_attempt(
            claim, "queued", "a" * 64, 101_000
        )
        self.store.save_checkpoint(
            claim, attempt["id"], "a" * 64, {"ok": True}, 102_000
        )
        self.store.record_provider_intent(
            claim,
            "queued",
            attempt["id"],
            "provider-a",
            "render",
            "op-1",
            "b" * 64,
            102_000,
        )
        self.store.bind_provider_result(
            claim, "op-1", "external-1", "done", {"ok": True}, 103_000
        )

        class RecordingConnection:
            def __init__(self, connection):
                self.connection = connection
                self.sql = []

            def execute(self, statement, parameters=()):
                self.sql.append(" ".join(statement.lower().split()))
                return self.connection.execute(statement, parameters)

            def __getattr__(self, name):
                return getattr(self.connection, name)

        def capture(callable_):
            connection = self.store._connect()
            recording = RecordingConnection(connection)
            with mock.patch.object(self.store, "_connect", return_value=recording):
                callable_()
            return recording.sql

        checkpoint_sql = capture(
            lambda: self.store.get_checkpoint_for_claim(
                claim, "queued", "a" * 64, 104_000
            )
        )
        provider_get_sql = capture(
            lambda: self.store.get_provider_task_for_claim(claim, "op-1", 104_000)
        )
        provider_record_replay_sql = capture(
            lambda: self.store.record_provider_intent(
                claim,
                "queued",
                attempt["id"],
                "provider-a",
                "render",
                "op-1",
                "b" * 64,
                104_000,
            )
        )
        provider_bind_replay_sql = capture(
            lambda: self.store.bind_provider_result(
                claim,
                "op-1",
                "external-1",
                "done",
                {"ok": True},
                104_000,
            )
        )
        checkpoint_replay_sql = capture(
            lambda: self.store.save_checkpoint(
                claim, attempt["id"], "a" * 64, {"ok": True}, 104_000
            )
        )
        self.store.finish_stage_attempt(
            claim, attempt["id"], "completed", 104_000
        )
        finish_replay_sql = capture(
            lambda: self.store.finish_stage_attempt(
                claim, attempt["id"], "completed", 105_000
            )
        )
        for statements, table in (
            (checkpoint_sql, "edit_v3_checkpoints"),
            (provider_get_sql, "edit_v3_provider_tasks"),
            (provider_record_replay_sql, "edit_v3_provider_tasks"),
            (provider_bind_replay_sql, "edit_v3_provider_tasks"),
            (checkpoint_replay_sql, "edit_v3_checkpoints"),
            (checkpoint_replay_sql, "edit_v3_stage_attempts"),
            (finish_replay_sql, "edit_v3_stage_attempts"),
        ):
            protected = [statement for statement in statements if table in statement]
            self.assertTrue(protected)
            for statement in protected:
                self.assertIn("edit_v3_jobs", statement)
                self.assertIn("worker_id", statement)
                self.assertIn("fencing_token", statement)
                self.assertIn("lease_until", statement)

    def test_every_stale_leased_mutation_preserves_rows_json_and_sha(self):
        self.seed_job("job-1")
        old = self.store.claim_next_job("worker-a", 10, 100_000)
        attempt = self.store.start_stage_attempt(old, "queued", "a" * 64, 101_000)
        self.store.save_checkpoint(
            old, attempt["id"], "a" * 64, {"output": 1}, 102_000
        )
        self.store.record_provider_intent(
            old,
            "queued",
            attempt["id"],
            "provider-a",
            "render",
            "op-1",
            "b" * 64,
            103_000,
        )
        new = self.store.claim_next_job("worker-b", 10, 110_000)
        self.assertGreater(new.fencing_token, old.fencing_token)
        before = self.protected_snapshot("job-1")

        self.assertFalse(self.store.renew_lease(old, 10, 110_001))
        self.assertFalse(self.store.release_lease(old, 110_001))
        self.assertFalse(
            self.store.transition_leased(
                old,
                {"queued"},
                "generating_voice",
                110_001,
                lease_seconds=10,
            )
        )
        stale_calls = (
            lambda: self.store.start_stage_attempt(
                old, "normalizing", "c" * 64, 110_001
            ),
            lambda: self.store.finish_stage_attempt(
                old, attempt["id"], "completed", 110_001
            ),
            lambda: self.store.save_checkpoint(
                old, attempt["id"], "a" * 64, {"output": 1}, 110_001
            ),
            lambda: self.store.get_checkpoint_for_claim(
                old, "queued", "a" * 64, 110_001
            ),
            lambda: self.store.record_provider_intent(
                old,
                "queued",
                attempt["id"],
                "provider-a",
                "render",
                "op-2",
                "d" * 64,
                110_001,
            ),
            lambda: self.store.get_provider_task_for_claim(old, "op-1", 110_001),
            lambda: self.store.bind_provider_result(
                old,
                "op-1",
                "external-1",
                "done",
                {"ok": True},
                110_001,
            ),
            lambda: self.store.close_running_attempts(old, 110_001),
        )
        for stale_call in stale_calls:
            with self.subTest(call=repr(stale_call)):
                with self.assertRaises(store_module.LeaseLost):
                    stale_call()
                self.assertEqual(self.protected_snapshot("job-1"), before)

    def test_cross_job_stage_and_missing_attempt_writes_change_nothing(self):
        self.seed_job("job-1")
        self.seed_job("job-2")
        first = self.store.claim_job(
            "job-1", "worker-a", 30, 100_000, expected_states={"queued"}
        )
        second = self.store.claim_job(
            "job-2", "worker-b", 30, 100_000, expected_states={"queued"}
        )
        first_attempt = self.store.start_stage_attempt(
            first, "queued", "a" * 64, 101_000
        )
        second_attempt = self.store.start_stage_attempt(
            second, "queued", "b" * 64, 101_000
        )
        before_first = self.protected_snapshot("job-1")
        before_second = self.protected_snapshot("job-2")
        conflicts = (
            lambda: self.store.start_stage_attempt(
                first, "normalizing", "c" * 64, 102_000
            ),
            lambda: self.store.finish_stage_attempt(
                first, second_attempt["id"], "completed", 102_000
            ),
            lambda: self.store.save_checkpoint(
                first, second_attempt["id"], "b" * 64, {"wrong": True}, 102_000
            ),
            lambda: self.store.save_checkpoint(
                first, "missing-attempt", "a" * 64, {"wrong": True}, 102_000
            ),
            lambda: self.store.record_provider_intent(
                first,
                "queued",
                second_attempt["id"],
                "provider-a",
                "render",
                "cross-job-op",
                "d" * 64,
                102_000,
            ),
        )
        for conflict in conflicts:
            with self.subTest(call=repr(conflict)):
                with self.assertRaises(StoreConflictError):
                    conflict()
                self.assertEqual(self.protected_snapshot("job-1"), before_first)
                self.assertEqual(self.protected_snapshot("job-2"), before_second)
        self.assertEqual(first_attempt["status"], "running")

    def test_invalid_lease_stage_key_sha_and_timestamp_arguments_are_typed(self):
        self.seed_job("job-1")
        invalid_claim_calls = (
            lambda: self.store.claim_next_job("", 10, 100_000),
            lambda: self.store.claim_next_job("worker", 0, 100_000),
            lambda: self.store.claim_next_job("worker", True, 100_000),
            lambda: self.store.claim_next_job("worker", 10, True),
            lambda: self.store.claim_job(
                "job-1", "worker", 10, 100_000, expected_states=set()
            ),
            lambda: self.store.claim_job(
                "", "worker", 10, 100_000, expected_states={"queued"}
            ),
        )
        for invalid_call in invalid_claim_calls:
            with self.subTest(call=repr(invalid_call)):
                with self.assertRaises(StoreConfigurationError):
                    invalid_call()
        claim = self.store.claim_next_job("worker", 10, 100_000)
        for invalid_call in (
            lambda: self.store.start_stage_attempt(claim, "", "a" * 64, 101_000),
            lambda: self.store.start_stage_attempt(claim, "queued", "A" * 64, 101_000),
            lambda: self.store.start_stage_attempt(claim, "queued", "a", 101_000),
            lambda: self.store.start_stage_attempt(claim, "queued", "a" * 64, True),
            lambda: self.store.get_provider_task_for_claim(claim, "", 101_000),
            lambda: self.store.renew_lease(
                contracts_module.LeaseClaim("job-1", "worker", True, 110_000),
                10,
                101_000,
            ),
        ):
            with self.subTest(call=repr(invalid_call)):
                with self.assertRaises(StoreConfigurationError):
                    invalid_call()

    def test_crash_replay_sequence_is_immutable_at_every_commit_boundary(self):
        self.seed_job("job-1")
        first_claim = self.store.claim_next_job("worker-a", 10, 100_000)
        self.assertIsNone(self.store.claim_next_job("worker-b", 10, 100_001))
        claim = self.store.claim_next_job("worker-b", 30, 110_000)
        self.assertGreater(claim.fencing_token, first_claim.fencing_token)

        attempt = self.store.start_stage_attempt(
            claim, "queued", "a" * 64, 111_000
        )
        self.assertEqual(
            self.store.start_stage_attempt(claim, "queued", "a" * 64, 112_000),
            attempt,
        )
        intent = self.store.record_provider_intent(
            claim,
            "queued",
            attempt["id"],
            "provider-a",
            "render",
            "op-crash",
            "b" * 64,
            113_000,
        )
        self.assertEqual(
            self.store.record_provider_intent(
                claim,
                "queued",
                attempt["id"],
                "provider-a",
                "render",
                "op-crash",
                "b" * 64,
                114_000,
            ),
            intent,
        )
        bound = self.store.bind_provider_result(
            claim,
            "op-crash",
            "external-crash",
            "done",
            {"accepted": True},
            115_000,
        )
        self.assertEqual(
            self.store.bind_provider_result(
                claim,
                "op-crash",
                "external-crash",
                "done",
                {"accepted": True},
                116_000,
            ),
            bound,
        )
        checkpoint = self.store.save_checkpoint(
            claim, attempt["id"], "a" * 64, {"result": "ok"}, 117_000
        )
        self.assertEqual(
            self.store.save_checkpoint(
                claim, attempt["id"], "a" * 64, {"result": "ok"}, 118_000
            ),
            checkpoint,
        )
        finished = self.store.finish_stage_attempt(
            claim, attempt["id"], "completed", 119_000
        )
        self.assertEqual(
            self.store.finish_stage_attempt(
                claim, attempt["id"], "completed", 120_000
            ),
            finished,
        )
        self.assertTrue(
            self.store.transition_leased(
                claim,
                {"queued"},
                "generating_voice",
                121_000,
                lease_seconds=30,
            )
        )
        after_transition = self.protected_snapshot("job-1")
        self.assertFalse(
            self.store.transition_leased(
                claim,
                {"queued"},
                "generating_voice",
                122_000,
                lease_seconds=30,
            )
        )
        self.assertEqual(self.protected_snapshot("job-1"), after_transition)
        attempt_counts = self.store._read(
            lambda connection: tuple(
                connection.execute(
                    """SELECT count(*),
                              sum(CASE WHEN status='running' THEN 1 ELSE 0 END)
                       FROM edit_v3_stage_attempts WHERE job_id='job-1'"""
                ).fetchone()
            )
        )
        checkpoint_count = self.store._read(
            lambda connection: connection.execute(
                "SELECT count(*) FROM edit_v3_checkpoints WHERE job_id='job-1'"
            ).fetchone()[0]
        )
        provider_count = self.store._read(
            lambda connection: connection.execute(
                "SELECT count(*) FROM edit_v3_provider_tasks WHERE job_id='job-1'"
            ).fetchone()[0]
        )
        self.assertEqual(attempt_counts, (1, 0))
        self.assertEqual(checkpoint_count, 1)
        self.assertEqual(provider_count, 1)


if __name__ == "__main__":
    unittest.main()
