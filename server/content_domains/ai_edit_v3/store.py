"""Isolated SQLite persistence boundary for AI Edit V3."""

from __future__ import annotations

import base64
import binascii
import errno
import hashlib
import math
import os
import re
import sqlite3
import stat
import sys
import threading
import time
import unicodedata
import uuid
import weakref
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path, PureWindowsPath
from typing import Any, Literal, NamedTuple, TypeVar
from urllib.parse import quote

from .contracts import (
    ALLOWED_TRANSITIONS,
    ALL_STATES,
    ContractError,
    LeaseClaim,
    MEDIA_STATES,
    QUEUE_CLAIMABLE_STATES,
    TERMINAL_STATES,
    canonical_json,
    parse_strict_json,
    request_fingerprint,
)


_LOCAL_FILESYSTEMS = frozenset(
    {
        "btrfs",
        "ext2",
        "ext3",
        "ext4",
        "overlay",
        "overlayfs",
        "tmpfs",
        "windows_fixed",
        "xfs",
    }
)
_REMOTE_FILESYSTEMS = frozenset(
    {
        "9p",
        "afs",
        "azureblob",
        "beegfs",
        "ceph",
        "cephfs",
        "cifs",
        "cosfs",
        "davfs",
        "davfs2",
        "fuse.cosfs",
        "fuse.gcsfuse",
        "fuse.goofys",
        "fuse.juicefs",
        "fuse.rclone",
        "fuse.s3fs",
        "fuse.sshfs",
        "gcsfuse",
        "gpfs",
        "glusterfs",
        "lustre",
        "nfs",
        "nfs4",
        "smb",
        "smb2",
        "smb3",
        "webdav",
        "windows_remote",
    }
)
_WINDOWS_DRIVE_RELATIVE = re.compile(r"^[A-Za-z]:[^\\/]")
_MOUNT_ESCAPE = re.compile(r"\\([0-7]{3})")
_SQLITE_OPEN_LOCK = threading.RLock()
_SQLITE_INT64_MAX = (1 << 63) - 1
_PREHOLD_ADMISSION_TIMEOUT_MS = 300_000
_PROCESSING_DEADLINE_MS = 2_700_000
_PUBLISH_OPERATIONS = (
    "register_generation",
    "prepare_hidden",
    "commit_publish",
    "cancel_publish",
    "query_decision",
)
_PUBLISH_KEY_SEGMENTS = {
    "register_generation": "register",
    "prepare_hidden": "prepare",
    "commit_publish": "commit",
    "cancel_publish": "cancel",
    "query_decision": "query",
}
_PUBLISH_EXPECTED_DECISIONS = {
    "register_generation": "accepted",
    "prepare_hidden": "accepted",
    "commit_publish": "publish_won",
    "cancel_publish": "cancel_won",
    "query_decision": "publish_won_or_cancel_won",
}
_PUBLISH_ASSET_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}\Z")
_PUBLISH_DECISION_KEYS = frozenset(
    {"asset_id", "current_generation", "status"}
)
_PUBLISH_DECISION_STATUSES = frozenset(
    {"accepted", "stale_generation", "publish_won", "cancel_won"}
)
_PUBLISH_SAFE_EVIDENCE_KEYS = frozenset({"outcome", "reason_code"})
_PUBLISH_SAFE_OUTCOMES = frozenset({"unknown", "definitive_not_accepted"})
_PUBLISH_REASON_CODE_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,127}\Z")
_PUBLISH_ACCEPTED_OPERATIONS = frozenset(
    {"register_generation", "prepare_hidden"}
)
_PUBLISH_AUTHORITY_STATES = frozenset(
    {"publishing", "asset_decision_reconciling", "failed_asset_decision_pending"}
)
SCHEMA_VERSION = 1
_STAGE_ATTEMPT_STATUSES = (
    "running",
    "completed",
    "failed",
    "skipped",
    "aborted_lease_lost",
)

_SCHEMA_TABLE_COLUMNS = {
    "edit_v3_schema_meta": (
        "id", "version", "migration_sha256", "created_at", "updated_at",
    ),
    "edit_v3_jobs": (
        "job_id", "environment", "owner_id", "state", "normalized_request_json",
        "request_sha256", "quote_id", "predecessor_job_id", "idempotency_key",
        "worker_id", "fencing_token", "lease_until", "queued_at", "processing_deadline_at",
        "repair_count", "repair_budget_granted_at", "reconciliation_reason", "resume_state",
        "confirmed_preheld_total", "confirmed_refunded_total", "delivery_object_key",
        "asset_id", "result_json", "error_code", "error_json", "created_at", "updated_at",
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
        "quote_id", "environment", "owner_id", "normalized_request_json", "request_sha256",
        "pricing_version", "template_id", "template_version", "min_points", "max_points",
        "breakdown_json", "expires_at", "created_at",
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
        "id", "job_id", "attempt", "render_id", "verdict_json", "verdict_sha256",
        "schema_sha256", "evidence_json", "status", "repairable", "created_at",
    ),
    "edit_v3_billing_intents": (
        "id", "environment", "owner_id", "job_id", "operation", "external_idempotency_key",
        "request_sha256", "refund_target_total", "request_amount", "status",
        "first_unknown_at", "last_checked_at", "authority_evidence_json", "reason",
        "resume_state", "created_at", "updated_at", "completed_at",
    ),
    "edit_v3_publish_intents": (
        "id", "job_id", "publish_generation", "operation", "external_idempotency_key",
        "object_key", "metadata_sha256", "expected_decision", "status", "fencing_token",
        "first_unknown_at", "last_decision_json", "last_decision_at", "asset_id", "created_at",
        "updated_at",
    ),
}

_SCHEMA_FOREIGN_KEYS = {
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

_SCHEMA_INDEXES = {
    "edit_v3_billing_intents_due_idx": (False, ("status", "first_unknown_at", "id")),
    "edit_v3_jobs_claim_idx": (False, ("state", "lease_until", "queued_at", "job_id")),
    "edit_v3_jobs_owner_created_idx": (
        False, ("environment", "owner_id", "created_at", "job_id"),
    ),
    "edit_v3_materials_owner_created_idx": (
        False, ("environment", "owner_id", "created_at", "material_id"),
    ),
    "edit_v3_model_calls_request_idx": (True, ("provider", "request_id")),
    "edit_v3_pricing_one_published_idx": (True, ("status",)),
    "edit_v3_provider_tasks_due_idx": (False, ("status", "first_unknown_at", "id")),
    "edit_v3_provider_tasks_external_idx": (True, ("provider", "external_id")),
    "edit_v3_publish_intents_due_idx": (False, ("status", "first_unknown_at", "id")),
    "edit_v3_quotes_owner_created_idx": (
        False, ("environment", "owner_id", "created_at", "quote_id"),
    ),
    "edit_v3_stage_attempts_one_running_idx": (True, ("job_id",)),
    "edit_v3_template_one_published_idx": (True, ("template_id",)),
    "edit_v3_uploads_owner_created_idx": (
        False, ("environment", "owner_id", "created_at", "upload_id"),
    ),
}

SCHEMA_MANIFEST = {
    "schema_version": SCHEMA_VERSION,
    "stage_attempt_statuses": list(_STAGE_ATTEMPT_STATUSES),
    "tables": {name: list(columns) for name, columns in _SCHEMA_TABLE_COLUMNS.items()},
    "foreign_keys": {
        name: [list(value) for value in sorted(values)]
        for name, values in sorted(_SCHEMA_FOREIGN_KEYS.items())
    },
    "indexes": {
        name: {"unique": unique, "columns": list(columns)}
        for name, (unique, columns) in sorted(_SCHEMA_INDEXES.items())
    },
}
_JOB_STATES_SQL = ",".join(f"'{state}'" for state in sorted(ALLOWED_TRANSITIONS))
_STAGE_ATTEMPT_STATUSES_SQL = ",".join(
    f"'{status}'" for status in _STAGE_ATTEMPT_STATUSES
)

_CREATE_TABLE_SQL = {
    "edit_v3_schema_meta": """
        CREATE TABLE edit_v3_schema_meta(
            id INTEGER PRIMARY KEY CHECK(id=1),
            version INTEGER NOT NULL,
            migration_sha256 TEXT NOT NULL CHECK(length(migration_sha256)=64 AND migration_sha256 NOT GLOB '*[^0-9a-f]*'),
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )
    """,
    "edit_v3_pricing_versions": """
        CREATE TABLE edit_v3_pricing_versions(
            version TEXT PRIMARY KEY CHECK(length(version)>0),
            status TEXT NOT NULL CHECK(status IN ('draft','published','retired')),
            parameters_json TEXT NOT NULL,
            parameters_sha256 TEXT NOT NULL CHECK(length(parameters_sha256)=64 AND parameters_sha256 NOT GLOB '*[^0-9a-f]*'),
            created_at INTEGER NOT NULL,
            published_at INTEGER,
            retired_at INTEGER
        )
    """,
    "edit_v3_template_versions": """
        CREATE TABLE edit_v3_template_versions(
            template_id TEXT NOT NULL CHECK(length(template_id)>0),
            version TEXT NOT NULL CHECK(length(version)>0),
            status TEXT NOT NULL CHECK(status IN ('draft','published','retired')),
            preview_cos_key TEXT NOT NULL CHECK(length(preview_cos_key)>0 AND instr(preview_cos_key,'://')=0),
            supported_ratios_json TEXT NOT NULL,
            capability_contract_json TEXT NOT NULL,
            sha256 TEXT NOT NULL CHECK(length(sha256)=64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
            created_at INTEGER NOT NULL,
            published_at INTEGER,
            PRIMARY KEY(template_id,version)
        )
    """,
    "edit_v3_quotes": """
        CREATE TABLE edit_v3_quotes(
            quote_id TEXT PRIMARY KEY CHECK(length(quote_id)>0),
            environment TEXT NOT NULL CHECK(environment IN ('test','production')),
            owner_id TEXT NOT NULL CHECK(length(owner_id)>0),
            normalized_request_json TEXT NOT NULL,
            request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64 AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
            pricing_version TEXT NOT NULL,
            template_id TEXT,
            template_version TEXT,
            min_points INTEGER NOT NULL CHECK(min_points>=0),
            max_points INTEGER NOT NULL CHECK(max_points>=min_points),
            breakdown_json TEXT NOT NULL,
            expires_at INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            CHECK((template_id IS NULL AND template_version IS NULL) OR (template_id IS NOT NULL AND template_version IS NOT NULL)),
            FOREIGN KEY(pricing_version) REFERENCES edit_v3_pricing_versions(version) ON DELETE RESTRICT,
            FOREIGN KEY(template_id,template_version) REFERENCES edit_v3_template_versions(template_id,version) ON DELETE RESTRICT
        )
    """,
    "edit_v3_jobs": f"""
        CREATE TABLE edit_v3_jobs(
            job_id TEXT PRIMARY KEY CHECK(length(job_id)>0),
            environment TEXT NOT NULL CHECK(environment IN ('test','production')),
            owner_id TEXT NOT NULL CHECK(length(owner_id)>0),
            state TEXT NOT NULL CHECK(state IN ({_JOB_STATES_SQL})),
            normalized_request_json TEXT NOT NULL,
            request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64 AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
            quote_id TEXT NOT NULL,
            predecessor_job_id TEXT,
            idempotency_key TEXT NOT NULL CHECK(length(idempotency_key)>0),
            worker_id TEXT,
            fencing_token INTEGER NOT NULL DEFAULT 0 CHECK(fencing_token>=0),
            lease_until INTEGER,
            queued_at INTEGER,
            processing_deadline_at INTEGER,
            repair_count INTEGER NOT NULL DEFAULT 0 CHECK(repair_count IN (0,1)),
            repair_budget_granted_at INTEGER,
            reconciliation_reason TEXT,
            resume_state TEXT,
            confirmed_preheld_total INTEGER NOT NULL DEFAULT 0 CHECK(confirmed_preheld_total>=0),
            confirmed_refunded_total INTEGER NOT NULL DEFAULT 0 CHECK(confirmed_refunded_total>=0 AND confirmed_refunded_total<=confirmed_preheld_total),
            delivery_object_key TEXT CHECK(delivery_object_key IS NULL OR (length(delivery_object_key)>0 AND instr(delivery_object_key,'://')=0)),
            asset_id TEXT,
            result_json TEXT,
            error_code TEXT,
            error_json TEXT,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            UNIQUE(environment,owner_id,idempotency_key),
            FOREIGN KEY(quote_id) REFERENCES edit_v3_quotes(quote_id) ON DELETE RESTRICT,
            FOREIGN KEY(predecessor_job_id) REFERENCES edit_v3_jobs(job_id) ON DELETE RESTRICT
        )
    """,
    "edit_v3_stage_attempts": f"""
        CREATE TABLE edit_v3_stage_attempts(
            id TEXT PRIMARY KEY CHECK(length(id)>0),
            job_id TEXT NOT NULL,
            stage TEXT NOT NULL CHECK(length(stage)>0),
            attempt INTEGER NOT NULL CHECK(attempt>0),
            worker_id TEXT NOT NULL CHECK(length(worker_id)>0),
            fencing_token INTEGER NOT NULL CHECK(fencing_token>=0),
            status TEXT NOT NULL CHECK(status IN ({_STAGE_ATTEMPT_STATUSES_SQL})),
            input_sha256 TEXT NOT NULL CHECK(length(input_sha256)=64 AND input_sha256 NOT GLOB '*[^0-9a-f]*'),
            started_at INTEGER NOT NULL,
            finished_at INTEGER,
            error_code TEXT,
            error_json TEXT,
            UNIQUE(job_id,stage,attempt),
            FOREIGN KEY(job_id) REFERENCES edit_v3_jobs(job_id) ON DELETE RESTRICT
        )
    """,
    "edit_v3_checkpoints": """
        CREATE TABLE edit_v3_checkpoints(
            id TEXT PRIMARY KEY CHECK(length(id)>0),
            job_id TEXT NOT NULL,
            stage TEXT NOT NULL CHECK(length(stage)>0),
            version INTEGER NOT NULL CHECK(version>0),
            stage_attempt_id TEXT NOT NULL,
            input_sha256 TEXT NOT NULL CHECK(length(input_sha256)=64 AND input_sha256 NOT GLOB '*[^0-9a-f]*'),
            output_json TEXT NOT NULL,
            output_sha256 TEXT NOT NULL CHECK(length(output_sha256)=64 AND output_sha256 NOT GLOB '*[^0-9a-f]*'),
            fencing_token INTEGER NOT NULL CHECK(fencing_token>=0),
            created_at INTEGER NOT NULL,
            UNIQUE(job_id,stage,version),
            UNIQUE(job_id,stage,input_sha256),
            FOREIGN KEY(job_id) REFERENCES edit_v3_jobs(job_id) ON DELETE RESTRICT,
            FOREIGN KEY(stage_attempt_id) REFERENCES edit_v3_stage_attempts(id) ON DELETE RESTRICT
        )
    """,
    "edit_v3_uploads": """
        CREATE TABLE edit_v3_uploads(
            upload_id TEXT PRIMARY KEY CHECK(length(upload_id)>0),
            environment TEXT NOT NULL CHECK(environment IN ('test','production')),
            owner_id TEXT NOT NULL CHECK(length(owner_id)>0),
            upload_type TEXT NOT NULL CHECK(length(upload_type)>0),
            object_key TEXT NOT NULL UNIQUE CHECK(length(object_key)>0 AND instr(object_key,'://')=0),
            declared_mime TEXT NOT NULL CHECK(length(declared_mime)>0),
            declared_size INTEGER NOT NULL CHECK(declared_size>=0),
            observed_mime TEXT,
            observed_size INTEGER CHECK(observed_size IS NULL OR observed_size>=0),
            observed_etag TEXT,
            sha256 TEXT CHECK(sha256 IS NULL OR (length(sha256)=64 AND sha256 NOT GLOB '*[^0-9a-f]*')),
            duration_ms INTEGER CHECK(duration_ms IS NULL OR duration_ms>=0),
            width INTEGER CHECK(width IS NULL OR width>0),
            height INTEGER CHECK(height IS NULL OR height>0),
            probe_json TEXT,
            status TEXT NOT NULL CHECK(status IN ('pending','completed','failed','expired')),
            expires_at INTEGER NOT NULL,
            completed_at INTEGER,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )
    """,
    "edit_v3_materials": """
        CREATE TABLE edit_v3_materials(
            material_id TEXT PRIMARY KEY CHECK(length(material_id)>0),
            environment TEXT NOT NULL CHECK(environment IN ('test','production')),
            owner_id TEXT NOT NULL CHECK(length(owner_id)>0),
            upload_id TEXT UNIQUE,
            source_kind TEXT NOT NULL,
            source_job_id TEXT,
            cos_key TEXT NOT NULL UNIQUE CHECK(length(cos_key)>0 AND instr(cos_key,'://')=0),
            mime_type TEXT NOT NULL CHECK(length(mime_type)>0),
            size_bytes INTEGER NOT NULL CHECK(size_bytes>=0),
            sha256 TEXT NOT NULL CHECK(length(sha256)=64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
            metadata_json TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            CHECK((source_kind='uploaded' AND upload_id IS NOT NULL AND source_job_id IS NULL) OR (source_kind='generated' AND upload_id IS NULL AND source_job_id IS NOT NULL)),
            FOREIGN KEY(upload_id) REFERENCES edit_v3_uploads(upload_id) ON DELETE RESTRICT,
            FOREIGN KEY(source_job_id) REFERENCES edit_v3_jobs(job_id) ON DELETE RESTRICT
        )
    """,
    "edit_v3_job_materials": """
        CREATE TABLE edit_v3_job_materials(
            job_id TEXT NOT NULL,
            material_id TEXT NOT NULL,
            purpose TEXT NOT NULL CHECK(length(purpose)>0),
            ordinal INTEGER NOT NULL CHECK(ordinal>=0),
            created_at INTEGER NOT NULL,
            PRIMARY KEY(job_id,material_id),
            UNIQUE(job_id,purpose,ordinal),
            FOREIGN KEY(job_id) REFERENCES edit_v3_jobs(job_id) ON DELETE RESTRICT,
            FOREIGN KEY(material_id) REFERENCES edit_v3_materials(material_id) ON DELETE RESTRICT
        )
    """,
    "edit_v3_model_calls": """
        CREATE TABLE edit_v3_model_calls(
            id TEXT PRIMARY KEY CHECK(length(id)>0),
            job_id TEXT NOT NULL,
            stage_attempt_id TEXT,
            provider TEXT NOT NULL CHECK(length(provider)>0),
            model TEXT NOT NULL CHECK(length(model)>0),
            purpose TEXT NOT NULL CHECK(length(purpose)>0),
            prompt_version TEXT NOT NULL CHECK(length(prompt_version)>0),
            request_schema_sha256 TEXT NOT NULL CHECK(length(request_schema_sha256)=64 AND request_schema_sha256 NOT GLOB '*[^0-9a-f]*'),
            response_schema_sha256 TEXT NOT NULL CHECK(length(response_schema_sha256)=64 AND response_schema_sha256 NOT GLOB '*[^0-9a-f]*'),
            request_id TEXT,
            redacted_final_output_json TEXT,
            validation_json TEXT NOT NULL,
            usage_json TEXT,
            elapsed_ms INTEGER NOT NULL CHECK(elapsed_ms>=0),
            created_at INTEGER NOT NULL,
            FOREIGN KEY(job_id) REFERENCES edit_v3_jobs(job_id) ON DELETE RESTRICT,
            FOREIGN KEY(stage_attempt_id) REFERENCES edit_v3_stage_attempts(id) ON DELETE RESTRICT
        )
    """,
    "edit_v3_provider_tasks": """
        CREATE TABLE edit_v3_provider_tasks(
            id TEXT PRIMARY KEY CHECK(length(id)>0),
            job_id TEXT NOT NULL,
            stage TEXT NOT NULL CHECK(length(stage)>0),
            stage_attempt_id TEXT,
            provider TEXT NOT NULL CHECK(length(provider)>0),
            capability TEXT NOT NULL CHECK(length(capability)>0),
            operation_key TEXT NOT NULL UNIQUE CHECK(length(operation_key)>0),
            request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64 AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
            external_id TEXT,
            status TEXT NOT NULL CHECK(length(status)>0),
            fencing_token INTEGER NOT NULL CHECK(fencing_token>=0),
            first_unknown_at INTEGER,
            last_checked_at INTEGER,
            result_json TEXT,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            FOREIGN KEY(job_id) REFERENCES edit_v3_jobs(job_id) ON DELETE RESTRICT,
            FOREIGN KEY(stage_attempt_id) REFERENCES edit_v3_stage_attempts(id) ON DELETE RESTRICT
        )
    """,
    "edit_v3_provider_usage": """
        CREATE TABLE edit_v3_provider_usage(
            id TEXT PRIMARY KEY CHECK(length(id)>0),
            job_id TEXT NOT NULL,
            provider TEXT NOT NULL CHECK(length(provider)>0),
            capability TEXT NOT NULL CHECK(length(capability)>0),
            request_id TEXT NOT NULL CHECK(length(request_id)>0),
            usage_json TEXT NOT NULL,
            cost_units INTEGER NOT NULL CHECK(cost_units>=0),
            created_at INTEGER NOT NULL,
            UNIQUE(provider,request_id),
            FOREIGN KEY(job_id) REFERENCES edit_v3_jobs(job_id) ON DELETE RESTRICT
        )
    """,
    "edit_v3_plans": """
        CREATE TABLE edit_v3_plans(
            id TEXT PRIMARY KEY CHECK(length(id)>0),
            job_id TEXT NOT NULL,
            version INTEGER NOT NULL CHECK(version>0),
            model_call_id TEXT NOT NULL,
            raw_final_output_json TEXT NOT NULL,
            normalized_plan_json TEXT NOT NULL,
            plan_sha256 TEXT NOT NULL CHECK(length(plan_sha256)=64 AND plan_sha256 NOT GLOB '*[^0-9a-f]*'),
            schema_sha256 TEXT NOT NULL CHECK(length(schema_sha256)=64 AND schema_sha256 NOT GLOB '*[^0-9a-f]*'),
            created_at INTEGER NOT NULL,
            UNIQUE(job_id,version),
            FOREIGN KEY(job_id) REFERENCES edit_v3_jobs(job_id) ON DELETE RESTRICT,
            FOREIGN KEY(model_call_id) REFERENCES edit_v3_model_calls(id) ON DELETE RESTRICT
        )
    """,
    "edit_v3_render_manifests": """
        CREATE TABLE edit_v3_render_manifests(
            id TEXT PRIMARY KEY CHECK(length(id)>0),
            job_id TEXT NOT NULL,
            attempt INTEGER NOT NULL CHECK(attempt>0),
            plan_id TEXT NOT NULL,
            manifest_json TEXT NOT NULL,
            manifest_sha256 TEXT NOT NULL CHECK(length(manifest_sha256)=64 AND manifest_sha256 NOT GLOB '*[^0-9a-f]*'),
            schema_sha256 TEXT NOT NULL CHECK(length(schema_sha256)=64 AND schema_sha256 NOT GLOB '*[^0-9a-f]*'),
            registry_sha256 TEXT NOT NULL CHECK(length(registry_sha256)=64 AND registry_sha256 NOT GLOB '*[^0-9a-f]*'),
            renderer_environment_sha256 TEXT NOT NULL CHECK(length(renderer_environment_sha256)=64 AND renderer_environment_sha256 NOT GLOB '*[^0-9a-f]*'),
            created_at INTEGER NOT NULL,
            UNIQUE(job_id,attempt),
            FOREIGN KEY(job_id) REFERENCES edit_v3_jobs(job_id) ON DELETE RESTRICT,
            FOREIGN KEY(plan_id) REFERENCES edit_v3_plans(id) ON DELETE RESTRICT
        )
    """,
    "edit_v3_renders": """
        CREATE TABLE edit_v3_renders(
            id TEXT PRIMARY KEY CHECK(length(id)>0),
            job_id TEXT NOT NULL,
            attempt INTEGER NOT NULL CHECK(attempt>0),
            manifest_id TEXT NOT NULL,
            status TEXT NOT NULL CHECK(length(status)>0),
            artifact_cos_key TEXT CHECK(artifact_cos_key IS NULL OR (length(artifact_cos_key)>0 AND instr(artifact_cos_key,'://')=0)),
            artifact_sha256 TEXT CHECK(artifact_sha256 IS NULL OR (length(artifact_sha256)=64 AND artifact_sha256 NOT GLOB '*[^0-9a-f]*')),
            evidence_json TEXT,
            performance_json TEXT,
            log_summary TEXT,
            cost_units INTEGER CHECK(cost_units IS NULL OR cost_units>=0),
            started_at INTEGER NOT NULL,
            finished_at INTEGER,
            UNIQUE(job_id,attempt),
            FOREIGN KEY(job_id) REFERENCES edit_v3_jobs(job_id) ON DELETE RESTRICT,
            FOREIGN KEY(manifest_id) REFERENCES edit_v3_render_manifests(id) ON DELETE RESTRICT
        )
    """,
    "edit_v3_quality_reports": """
        CREATE TABLE edit_v3_quality_reports(
            id TEXT PRIMARY KEY CHECK(length(id)>0),
            job_id TEXT NOT NULL,
            attempt INTEGER NOT NULL CHECK(attempt>0),
            render_id TEXT NOT NULL,
            verdict_json TEXT NOT NULL,
            verdict_sha256 TEXT NOT NULL CHECK(length(verdict_sha256)=64 AND verdict_sha256 NOT GLOB '*[^0-9a-f]*'),
            schema_sha256 TEXT NOT NULL CHECK(length(schema_sha256)=64 AND schema_sha256 NOT GLOB '*[^0-9a-f]*'),
            evidence_json TEXT NOT NULL,
            status TEXT NOT NULL CHECK(length(status)>0),
            repairable INTEGER NOT NULL CHECK(repairable IN (0,1)),
            created_at INTEGER NOT NULL,
            UNIQUE(job_id,attempt),
            FOREIGN KEY(job_id) REFERENCES edit_v3_jobs(job_id) ON DELETE RESTRICT,
            FOREIGN KEY(render_id) REFERENCES edit_v3_renders(id) ON DELETE RESTRICT
        )
    """,
    "edit_v3_billing_intents": """
        CREATE TABLE edit_v3_billing_intents(
            id TEXT PRIMARY KEY CHECK(length(id)>0),
            environment TEXT NOT NULL CHECK(environment IN ('test','production')),
            owner_id TEXT NOT NULL CHECK(length(owner_id)>0),
            job_id TEXT NOT NULL,
            operation TEXT NOT NULL CHECK(operation IN ('pre_debit','refund_delta','refund_full')),
            external_idempotency_key TEXT NOT NULL UNIQUE CHECK(length(external_idempotency_key)>0),
            request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64 AND request_sha256 NOT GLOB '*[^0-9a-f]*'),
            refund_target_total INTEGER NOT NULL CHECK(refund_target_total>=0),
            request_amount INTEGER NOT NULL CHECK(request_amount>=0),
            status TEXT NOT NULL CHECK(length(status)>0),
            first_unknown_at INTEGER,
            last_checked_at INTEGER,
            authority_evidence_json TEXT,
            reason TEXT,
            resume_state TEXT,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            completed_at INTEGER,
            UNIQUE(environment,owner_id,job_id,operation),
            FOREIGN KEY(job_id) REFERENCES edit_v3_jobs(job_id) ON DELETE RESTRICT
        )
    """,
    "edit_v3_publish_intents": """
        CREATE TABLE edit_v3_publish_intents(
            id TEXT PRIMARY KEY CHECK(length(id)>0),
            job_id TEXT NOT NULL,
            publish_generation INTEGER NOT NULL CHECK(publish_generation>=0),
            operation TEXT NOT NULL CHECK(operation IN ('register_generation','prepare_hidden','commit_publish','cancel_publish','query_decision')),
            external_idempotency_key TEXT NOT NULL UNIQUE CHECK(length(external_idempotency_key)>0),
            object_key TEXT NOT NULL CHECK(length(object_key)>0 AND instr(object_key,'://')=0),
            metadata_sha256 TEXT NOT NULL CHECK(length(metadata_sha256)=64 AND metadata_sha256 NOT GLOB '*[^0-9a-f]*'),
            expected_decision TEXT,
            status TEXT NOT NULL CHECK(length(status)>0),
            fencing_token INTEGER NOT NULL CHECK(fencing_token>=0),
            first_unknown_at INTEGER,
            last_decision_json TEXT,
            last_decision_at INTEGER,
            asset_id TEXT,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            UNIQUE(job_id,publish_generation,operation),
            FOREIGN KEY(job_id) REFERENCES edit_v3_jobs(job_id) ON DELETE RESTRICT
        )
    """,
}


def _freeze_table_ddl(statement: str) -> str:
    """Attach canonical-JSON checks and SQLite STRICT storage to frozen DDL."""

    def json_column(match: re.Match[str]) -> str:
        indentation, name, required = match.groups()
        declaration = f"{indentation}{name} TEXT{required or ''}"
        if required:
            return f"{declaration} CHECK(edit_v3_is_canonical_json({name})=1)"
        return (
            f"{declaration} CHECK({name} IS NULL OR "
            f"edit_v3_is_canonical_json({name})=1)"
        )

    frozen = re.sub(
        r"(?m)^(\s*)([a-z][a-z0-9_]*_json) TEXT( NOT NULL)?(?=,)",
        json_column,
        statement,
    ).rstrip()
    return re.sub(r"\)\s*$", ") STRICT", frozen)


_CREATE_TABLE_SQL = {
    name: _freeze_table_ddl(statement)
    for name, statement in _CREATE_TABLE_SQL.items()
}

_CREATE_INDEX_SQL = {
    "edit_v3_jobs_owner_created_idx": """
        CREATE INDEX edit_v3_jobs_owner_created_idx
        ON edit_v3_jobs(environment,owner_id,created_at DESC,job_id DESC)
    """,
    "edit_v3_jobs_claim_idx": """
        CREATE INDEX edit_v3_jobs_claim_idx
        ON edit_v3_jobs(state,lease_until,queued_at,job_id)
    """,
    "edit_v3_stage_attempts_one_running_idx": """
        CREATE UNIQUE INDEX edit_v3_stage_attempts_one_running_idx
        ON edit_v3_stage_attempts(job_id) WHERE status='running'
    """,
    "edit_v3_uploads_owner_created_idx": """
        CREATE INDEX edit_v3_uploads_owner_created_idx
        ON edit_v3_uploads(environment,owner_id,created_at,upload_id)
    """,
    "edit_v3_materials_owner_created_idx": """
        CREATE INDEX edit_v3_materials_owner_created_idx
        ON edit_v3_materials(environment,owner_id,created_at,material_id)
    """,
    "edit_v3_quotes_owner_created_idx": """
        CREATE INDEX edit_v3_quotes_owner_created_idx
        ON edit_v3_quotes(environment,owner_id,created_at,quote_id)
    """,
    "edit_v3_pricing_one_published_idx": """
        CREATE UNIQUE INDEX edit_v3_pricing_one_published_idx
        ON edit_v3_pricing_versions(status) WHERE status='published'
    """,
    "edit_v3_template_one_published_idx": """
        CREATE UNIQUE INDEX edit_v3_template_one_published_idx
        ON edit_v3_template_versions(template_id) WHERE status='published'
    """,
    "edit_v3_model_calls_request_idx": """
        CREATE UNIQUE INDEX edit_v3_model_calls_request_idx
        ON edit_v3_model_calls(provider,request_id)
        WHERE request_id IS NOT NULL AND request_id<>''
    """,
    "edit_v3_provider_tasks_external_idx": """
        CREATE UNIQUE INDEX edit_v3_provider_tasks_external_idx
        ON edit_v3_provider_tasks(provider,external_id)
        WHERE external_id IS NOT NULL AND external_id<>''
    """,
    "edit_v3_provider_tasks_due_idx": """
        CREATE INDEX edit_v3_provider_tasks_due_idx
        ON edit_v3_provider_tasks(status,first_unknown_at,id)
    """,
    "edit_v3_billing_intents_due_idx": """
        CREATE INDEX edit_v3_billing_intents_due_idx
        ON edit_v3_billing_intents(status,first_unknown_at,id)
    """,
    "edit_v3_publish_intents_due_idx": """
        CREATE INDEX edit_v3_publish_intents_due_idx
        ON edit_v3_publish_intents(status,first_unknown_at,id)
    """,
}


def _normalize_ddl(statement: str) -> str:
    return " ".join(statement.split())


_MIGRATION_DDL_MANIFEST = {
    "schema_version": SCHEMA_VERSION,
    "statements": [
        _normalize_ddl(_CREATE_TABLE_SQL[name])
        for name in sorted(_CREATE_TABLE_SQL)
    ]
    + [
        _normalize_ddl(_CREATE_INDEX_SQL[name])
        for name in sorted(_CREATE_INDEX_SQL)
    ],
}
MIGRATION_SHA256 = hashlib.sha256(canonical_json(_MIGRATION_DDL_MANIFEST)).hexdigest()


class StoreError(RuntimeError):
    """Base error carrying a stable machine-readable code."""

    def __init__(self, error_code: str, message: str):
        self.error_code = error_code
        self.message = message
        super().__init__(f"{error_code}: {message}")


class StoreConfigurationError(StoreError):
    """Raised when the V3 store cannot establish safe isolation."""


class StoreMigrationError(StoreError):
    """Raised when the V3 schema cannot be established safely."""


class StoreConflictError(StoreError):
    """Raised when an immutable store identity is reused inconsistently."""


class LeaseLost(StoreError):
    """Raised when a protected child operation no longer owns its lease."""


class _ClaimRaceLost(RuntimeError):
    """Forces rollback when a selected claim row stops matching."""


def _configuration_error(error_code: str, message: str) -> StoreConfigurationError:
    return StoreConfigurationError(error_code, message)


def _is_unc_or_device_path(raw: str) -> bool:
    normalized = raw.replace("/", "\\")
    return normalized.startswith("\\\\")


def _absolute_path(value: str | os.PathLike[str], *, role: str) -> Path:
    try:
        raw_value = os.fspath(value)
    except TypeError as exc:
        raise _configuration_error(
            f"{role}_db_path_invalid",
            f"{role.upper()} database path must be text or path-like",
        ) from exc
    if not isinstance(raw_value, str):
        raise _configuration_error(
            f"{role}_db_path_invalid",
            f"{role.upper()} database path must be text",
        )
    if not raw_value.strip():
        raise _configuration_error(
            f"{role}_db_path_required",
            f"{role.upper()} database path is required",
        )
    if _is_unc_or_device_path(raw_value):
        raise _configuration_error(
            f"{role}_db_path_network",
            f"{role.upper()} database path may not use UNC or device syntax",
        )
    windows_path = PureWindowsPath(raw_value)
    if _WINDOWS_DRIVE_RELATIVE.match(raw_value) or (
        windows_path.drive and not windows_path.root
    ):
        raise _configuration_error(
            f"{role}_db_path_not_absolute",
            f"{role.upper()} database path must be absolute",
        )
    path = Path(raw_value)
    if not path.is_absolute():
        raise _configuration_error(
            f"{role}_db_path_not_absolute",
            f"{role.upper()} database path must be absolute",
        )
    return path


def _is_reparse(metadata: os.stat_result) -> bool:
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & reparse_attribute
    )


def _assert_no_reparse_components(path: Path, *, role: str) -> None:
    current = Path(path.anchor)
    relative_parts = path.parts[1:] if path.anchor else path.parts
    for part in relative_parts:
        current /= part
        if not os.path.lexists(current):
            break
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise _configuration_error(
                f"{role}_db_identity_unknown",
                f"{role.upper()} database path identity cannot be established",
            ) from exc
        if _is_reparse(metadata):
            raise _configuration_error(
                f"{role}_db_path_reparse",
                f"{role.upper()} database path may not contain a symlink, "
                "junction, or reparse point",
            )


def _candidate_identity(path: Path, *, role: str) -> tuple[Path, os.stat_result | None]:
    try:
        parent_metadata = path.parent.lstat()
        parent = path.parent.resolve(strict=True)
    except OSError as exc:
        raise _configuration_error(
            f"{role}_db_identity_unknown",
            f"{role.upper()} database parent identity cannot be established",
        ) from exc
    if not stat.S_ISDIR(parent_metadata.st_mode):
        raise _configuration_error(
            f"{role}_db_identity_unknown",
            f"{role.upper()} database parent is not a directory",
        )

    if not os.path.lexists(path):
        if _is_reparse(parent_metadata):
            return parent / path.name, parent_metadata
        return parent / path.name, None

    try:
        metadata = path.lstat()
        resolved = path.resolve(strict=True)
        target_metadata = resolved.stat()
    except OSError as exc:
        raise _configuration_error(
            f"{role}_db_identity_unknown",
            f"{role.upper()} database identity cannot be established",
        ) from exc
    if not stat.S_ISREG(target_metadata.st_mode):
        raise _configuration_error(
            f"{role}_db_identity_unknown",
            f"{role.upper()} database must be an ordinary file",
        )
    return resolved, target_metadata


def resolve_db_path(value: str | os.PathLike[str] | None = None) -> Path:
    """Resolve an explicit local absolute V3 database path without creating it."""

    path = _v3_path_syntax(value)
    _assert_no_reparse_components(path, role="v3")
    resolved, _metadata = _candidate_identity(path, role="v3")
    return resolved


def _v3_path_syntax(value: str | os.PathLike[str] | None = None) -> Path:
    configured: str | os.PathLike[str] | None = value
    if configured is None:
        configured = os.environ.get("AI_EDIT_V3_DB_PATH")
    if configured is None:
        raise _configuration_error(
            "v3_db_path_required",
            "AI_EDIT_V3_DB_PATH is required",
        )
    return _absolute_path(configured, role="v3")


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.normpath(os.fspath(left))) == os.path.normcase(
        os.path.normpath(os.fspath(right))
    )


def assert_isolated_db(v3_path: Path, v2_path: Path | None) -> None:
    """Fail closed unless V3 and the configured V2 path have distinct identity."""

    raw_v3 = _absolute_path(v3_path, role="v3")
    _assert_no_reparse_components(raw_v3, role="v3")
    resolved_v3, _v3_metadata = _candidate_identity(raw_v3, role="v3")
    if v2_path is None:
        raise _configuration_error(
            "v2_db_path_required",
            "an explicit absolute V2 database path is required for isolation",
        )

    raw_v2 = _absolute_path(v2_path, role="v2")
    _assert_no_reparse_components(raw_v2, role="v2")
    resolved_v2, _v2_metadata = _candidate_identity(raw_v2, role="v2")
    if _same_path(resolved_v3, resolved_v2):
        raise _configuration_error(
            "v2_v3_db_same_file",
            "V2 and V3 database paths resolve to the same file",
        )

    v3_exists = os.path.lexists(raw_v3)
    v2_exists = os.path.lexists(raw_v2)
    if v3_exists and v2_exists:
        try:
            same_file = os.path.samefile(raw_v3, raw_v2)
            v3_stat = raw_v3.stat()
            v2_stat = raw_v2.stat()
        except OSError as exc:
            raise _configuration_error(
                "v2_v3_db_identity_unknown",
                "V2/V3 database identity comparison failed",
            ) from exc
        if same_file or (v3_stat.st_dev, v3_stat.st_ino) == (
            v2_stat.st_dev,
            v2_stat.st_ino,
        ):
            raise _configuration_error(
                "v2_v3_db_same_file",
                "V2 and V3 database files share one filesystem identity",
            )

    try:
        same_parent = os.path.samefile(raw_v3.parent, raw_v2.parent)
    except OSError as exc:
        raise _configuration_error(
            "v2_v3_db_identity_unknown",
            "V2/V3 parent identity comparison failed",
        ) from exc
    if same_parent and os.path.normcase(raw_v3.name) == os.path.normcase(raw_v2.name):
        raise _configuration_error(
            "v2_v3_db_same_file",
            "V2 and V3 database names alias within the same directory",
        )
def _decode_mount_path(value: str) -> str:
    return _MOUNT_ESCAPE.sub(lambda match: chr(int(match.group(1), 8)), value)


def _parse_linux_mountinfo(text: str, path: Path) -> str | None:
    candidate = os.path.normpath(os.fspath(path))
    best: tuple[int, str] | None = None
    for line in text.splitlines():
        left, separator, right = line.partition(" - ")
        if not separator:
            return None
        fields = left.split()
        trailing = right.split()
        if len(fields) < 5 or not trailing:
            return None
        mount_point = os.path.normpath(_decode_mount_path(fields[4]))
        try:
            common = os.path.commonpath((candidate, mount_point))
        except ValueError:
            continue
        if common != mount_point:
            continue
        match = (len(mount_point), trailing[0].lower())
        # Linux mountinfo is ordered bottom-to-top.  For stacked mounts with
        # the same mountpoint, the final matching entry is authoritative.
        if best is None or match[0] >= best[0]:
            best = match
    return None if best is None else best[1]


def _linux_filesystem_type(path: Path) -> str | None:
    try:
        text = Path("/proc/self/mountinfo").read_text(encoding="utf-8")
    except OSError:
        return None
    return _parse_linux_mountinfo(text, path)


def _windows_filesystem_type(path: Path) -> str | None:
    try:
        import ctypes

        root = path.anchor
        if not root:
            return None
        drive_type = int(ctypes.windll.kernel32.GetDriveTypeW(root))
    except (AttributeError, OSError, ValueError):
        return None
    return {
        0: None,
        1: None,
        2: "windows_removable",
        3: "windows_fixed",
        4: "windows_remote",
        5: "windows_cdrom",
        6: "windows_ramdisk",
    }.get(drive_type)


def _filesystem_type_for_path(path: Path) -> str | None:
    """Injection seam for deterministic filesystem-classification tests."""

    if os.name == "nt":
        return _windows_filesystem_type(path)
    if sys.platform.startswith("linux"):
        return _linux_filesystem_type(path)
    return None


def _classify_filesystem_type(fs_type: str | None) -> str:
    if not isinstance(fs_type, str) or not fs_type.strip():
        return "unknown"
    normalized = fs_type.strip().lower()
    if normalized in _LOCAL_FILESYSTEMS:
        return "local"
    if normalized in _REMOTE_FILESYSTEMS:
        return "remote"
    return "unknown"


class FilesystemClassification(NamedTuple):
    """Read-only filesystem identity and the V3 local-storage policy result."""

    filesystem_type: str | None
    policy: Literal["local", "remote", "unknown"]


def classify_filesystem(path: Path) -> FilesystemClassification:
    """Classify a path without creating, opening, or changing filesystem state."""

    if not isinstance(path, Path):
        return FilesystemClassification(filesystem_type=None, policy="unknown")
    filesystem_type = _filesystem_type_for_path(path)
    return FilesystemClassification(
        filesystem_type=filesystem_type,
        policy=_classify_filesystem_type(filesystem_type),
    )


def _assert_local_filesystem(path: Path) -> None:
    candidates = [path.parent]
    if os.path.lexists(path):
        candidates.append(path)
    for candidate in candidates:
        result = classify_filesystem(candidate)
        if result.policy == "remote":
            raise _configuration_error(
                "v3_db_network_filesystem",
                f"V3 database may not use network filesystem {result.filesystem_type}",
            )
        if result.policy != "local":
            raise _configuration_error(
                "v3_db_filesystem_unknown",
                "V3 database filesystem identity cannot be established",
            )


def _is_sqlite_busy_or_locked(error: sqlite3.OperationalError) -> bool:
    error_code = getattr(error, "sqlite_errorcode", None)
    if isinstance(error_code, int):
        return error_code & 0xFF in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
    message = str(error).lower()
    return "locked" in message or "busy" in message


def _negotiate_wal(
    connection: sqlite3.Connection,
    *,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    budget_seconds: float = 10.0,
) -> None:
    """Enter WAL while sharing one deadline across SQLite waits and backoff."""

    connection.execute("PRAGMA busy_timeout=10000")
    deadline = monotonic() + budget_seconds
    delay = 0.005

    def apply_remaining_timeout() -> None:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise sqlite3.OperationalError(
                "SQLite WAL negotiation deadline exceeded"
            )
        timeout_ms = max(1, min(10_000, math.ceil(remaining * 1000)))
        connection.execute(f"PRAGMA busy_timeout={timeout_ms}")

    while True:
        try:
            apply_remaining_timeout()
            mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
            if mode != "wal":
                apply_remaining_timeout()
                mode = str(
                    connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
                ).lower()
            if mode != "wal":
                raise StoreMigrationError(
                    "v3_wal_unavailable",
                    "SQLite did not enter WAL journal mode",
                )
            connection.execute("PRAGMA busy_timeout=10000")
            return
        except sqlite3.OperationalError as exc:
            if not _is_sqlite_busy_or_locked(exc):
                raise
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise
            sleep(min(delay, remaining))
            delay = min(delay * 2, 0.1)


class _GuardBundle:
    """OS resources proving the path identity for one live connection."""

    def release(self) -> None:  # pragma: no cover - abstract cleanup seam
        raise NotImplementedError


class _CleanupAction:
    """One explicit cleanup action retained by object identity."""

    __slots__ = ("resource", "method_name", "description")

    def __init__(self, resource: Any, method_name: str, description: str) -> None:
        self.resource = resource
        self.method_name = method_name
        self.description = description


class _CleanupOwner:
    """Ordered owner for resources whose cleanup has not yet succeeded."""

    def __init__(self) -> None:
        self._actions: list[_CleanupAction] = []

    @property
    def pending_resources(self) -> tuple[Any, ...]:
        return tuple(action.resource for action in self._actions)

    @property
    def pending_actions(self) -> tuple[tuple[Any, str, str], ...]:
        return tuple(
            (action.resource, action.method_name, action.description)
            for action in self._actions
        )

    @property
    def pending_description(self) -> str:
        if not self._actions:
            return "resource cleanup"
        return self._actions[0].description

    def append(
        self,
        resource: Any,
        method_name: str,
        *,
        description: str,
    ) -> None:
        if any(action.resource is resource for action in self._actions):
            return
        self._actions.append(_CleanupAction(resource, method_name, description))

    def extend(self, other: _CleanupOwner) -> None:
        if other is self:
            return
        for action in other._actions:
            self.append(
                action.resource,
                action.method_name,
                description=action.description,
            )
        other._actions.clear()

    def retry(self) -> None:
        with _SQLITE_OPEN_LOCK:
            while self._actions:
                action = self._actions[0]
                getattr(action.resource, action.method_name)()
                self._actions.pop(0)


def _retain_cleanup_owner(
    error: BaseException,
    owner: _CleanupOwner,
    *,
    before_existing: bool = True,
) -> _CleanupOwner:
    existing = getattr(error, "cleanup_owner", None)
    if isinstance(existing, _CleanupOwner) and existing is not owner:
        if before_existing:
            owner.extend(existing)
        else:
            existing.extend(owner)
            owner = existing
    error.cleanup_owner = owner
    return owner


def _cleanup_preserving_error(
    error: BaseException,
    owner: _CleanupOwner,
    *,
    before_existing: bool = True,
) -> bool:
    try:
        owner.retry()
    except Exception as cleanup_error:
        error.add_note(
            f"{owner.pending_description} failed during cleanup: {cleanup_error!r}"
        )
        _retain_cleanup_owner(
            error,
            owner,
            before_existing=before_existing,
        )
        return False
    return True


class _GuardedConnection(sqlite3.Connection):
    """A native connection retaining its verified path-identity evidence."""

    _identity_guard: _GuardBundle | None = None

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._cursor_lock = threading.RLock()
        self._tracked_cursors: dict[
            int,
            weakref.ReferenceType[sqlite3.Cursor],
        ] = {}

    def _track_cursor(self, cursor: sqlite3.Cursor) -> sqlite3.Cursor:
        try:
            cursor_id = id(cursor)
            connection_ref = weakref.ref(self)

            def discard(
                cursor_ref: weakref.ReferenceType[sqlite3.Cursor],
            ) -> None:
                connection = connection_ref()
                if connection is None:
                    return
                with connection._cursor_lock:
                    if connection._tracked_cursors.get(cursor_id) is cursor_ref:
                        connection._tracked_cursors.pop(cursor_id, None)

            self._tracked_cursors[cursor_id] = weakref.ref(cursor, discard)
        except BaseException:
            sqlite3.Cursor.close(cursor)
            raise
        return cursor

    def cursor(self, factory: type[sqlite3.Cursor] = sqlite3.Cursor) -> sqlite3.Cursor:
        with self._cursor_lock:
            cursor = sqlite3.Connection.cursor(self, factory)
            return self._track_cursor(cursor)

    def execute(
        self,
        sql: str,
        parameters: Sequence[Any] = (),
    ) -> sqlite3.Cursor:
        with self._cursor_lock:
            cursor = sqlite3.Connection.execute(self, sql, parameters)
            return self._track_cursor(cursor)

    def executemany(
        self,
        sql: str,
        parameters: Sequence[Sequence[Any]],
    ) -> sqlite3.Cursor:
        with self._cursor_lock:
            cursor = sqlite3.Connection.executemany(self, sql, parameters)
            return self._track_cursor(cursor)

    def executescript(self, sql_script: str) -> sqlite3.Cursor:
        with self._cursor_lock:
            cursor = sqlite3.Connection.executescript(self, sql_script)
            return self._track_cursor(cursor)

    def _retain_identity_guard(self, guard: _GuardBundle) -> None:
        if self._identity_guard is not None:
            raise RuntimeError("SQLite identity guard is already retained")
        self._identity_guard = guard

    def close(self) -> None:
        with _SQLITE_OPEN_LOCK:
            with self._cursor_lock:
                for cursor_id, cursor_ref in tuple(self._tracked_cursors.items()):
                    cursor = cursor_ref()
                    if cursor is None:
                        if self._tracked_cursors.get(cursor_id) is cursor_ref:
                            self._tracked_cursors.pop(cursor_id, None)
                        continue
                    sqlite3.Cursor.close(cursor)
                    if self._tracked_cursors.get(cursor_id) is cursor_ref:
                        self._tracked_cursors.pop(cursor_id, None)
                guard = self._identity_guard
                sqlite3.Connection.close(self)
                if guard is not None:
                    guard.release()
                    self._identity_guard = None

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            # Finalizers cannot report cleanup failures. Explicit close still
            # preserves the normal sqlite exception surface for callers.
            pass


class _WindowsGuardBundle(_GuardBundle):
    def __init__(self, handles: list[int], leaf_identity: tuple[int, int]):
        self.handles = handles
        self.leaf_identity = leaf_identity

    def release(self) -> None:
        if not self.handles:
            return
        import ctypes
        from ctypes import wintypes

        close_handle = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL
        while self.handles:
            handle = self.handles[-1]
            if not close_handle(wintypes.HANDLE(handle)):
                raise ctypes.WinError(ctypes.get_last_error())
            self.handles.pop()


class _LinuxGuardBundle(_GuardBundle):
    def __init__(
        self,
        parent_fd: int,
        leaf_fd: int,
        leaf_identity: tuple[int, int],
        *,
        ancestor_fds: list[int] | None = None,
    ):
        self.parent_fd = parent_fd
        self.leaf_fd = leaf_fd
        self.leaf_identity = leaf_identity
        if ancestor_fds is None:
            self.ancestor_fds = [parent_fd] if parent_fd >= 0 else []
        else:
            self.ancestor_fds = list(ancestor_fds)

    def release(self) -> None:
        if self.leaf_fd >= 0:
            os.close(self.leaf_fd)
            self.leaf_fd = -1
        while self.ancestor_fds:
            descriptor = self.ancestor_fds[-1]
            os.close(descriptor)
            self.ancestor_fds.pop()
            if self.parent_fd == descriptor:
                self.parent_fd = -1


def _v2_path_syntax(v2_db_path: Path | None) -> Path:
    configured: str | os.PathLike[str] | None = v2_db_path
    if configured is None:
        configured = os.environ.get("AI_EDIT_V2_DB")
    if configured is None:
        raise _configuration_error(
            "v2_db_path_required",
            "an explicit absolute V2 database path is required for isolation",
        )
    return _absolute_path(configured, role="v2")


def _windows_handle_identity(handle: int) -> tuple[tuple[int, int], int, int]:
    import ctypes
    from ctypes import wintypes

    class _ByHandleFileInformation(ctypes.Structure):
        _fields_ = (
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        )

    information = _ByHandleFileInformation()
    get_information = ctypes.windll.kernel32.GetFileInformationByHandle
    get_information.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(_ByHandleFileInformation),
    )
    get_information.restype = wintypes.BOOL
    if not get_information(
        wintypes.HANDLE(handle), ctypes.byref(information)
    ):
        raise ctypes.WinError()
    identity = (
        int(information.dwVolumeSerialNumber),
        (int(information.nFileIndexHigh) << 32) | int(information.nFileIndexLow),
    )
    return identity, int(information.dwFileAttributes), int(information.nNumberOfLinks)


def _windows_create_handle(
    path: Path,
    *,
    directory: bool,
    create_new: bool = False,
    writable: bool = False,
) -> int:
    import ctypes
    from ctypes import wintypes

    desired_access = 0
    if writable:
        desired_access = 0x80000000 | 0x40000000  # GENERIC_READ | GENERIC_WRITE
    creation = 1 if create_new else 3  # CREATE_NEW | OPEN_EXISTING
    flags = 0x00200000  # FILE_FLAG_OPEN_REPARSE_POINT
    if directory:
        flags |= 0x02000000  # FILE_FLAG_BACKUP_SEMANTICS
    else:
        flags |= 0x00000080  # FILE_ATTRIBUTE_NORMAL
    create_file = ctypes.windll.kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        os.fspath(path),
        desired_access,
        0x1 | 0x2,  # share read/write, deliberately deny delete/rename
        None,
        creation,
        flags,
        None,
    )
    invalid = ctypes.c_void_p(-1).value
    if handle == invalid:
        raise ctypes.WinError()
    return int(handle)


def _is_exact_create_race(error: OSError, *, windows: bool) -> bool:
    if windows:
        return getattr(error, "winerror", None) == 80  # ERROR_FILE_EXISTS
    return getattr(error, "errno", None) == errno.EEXIST


def _open_windows_ancestor_handles(path: Path, *, role: str) -> list[int]:
    handles: list[int] = []
    try:
        current = Path(path.anchor)
        candidates = [current]
        for part in path.parent.parts[1:]:
            current /= part
            candidates.append(current)
        for candidate in candidates:
            handle = _windows_create_handle(candidate, directory=True)
            handles.append(handle)
            _identity, attributes, _links = _windows_handle_identity(handle)
            if attributes & 0x400:
                raise _configuration_error(
                    f"{role}_db_path_reparse",
                    f"{role.upper()} database ancestor may not be a reparse point",
                )
        return handles
    except Exception as exc:
        bundle = _WindowsGuardBundle(handles, (0, 0))
        owner = _CleanupOwner()
        owner.append(
            bundle,
            "release",
            description="Windows ancestor guard release",
        )
        _cleanup_preserving_error(
            exc,
            owner,
        )
        raise


def _open_windows_v2_guard(
    path: Path,
) -> _WindowsGuardBundle:
    handles = _open_windows_ancestor_handles(path, role="v2")
    try:
        try:
            leaf_handle = _windows_create_handle(path, directory=False)
        except FileNotFoundError as exc:
            raise _configuration_error(
                "v2_db_identity_missing",
                "V2 database disappeared before the identity handshake",
            ) from exc
        handles.append(leaf_handle)
        leaf_identity, attributes, link_count = _windows_handle_identity(leaf_handle)
        if attributes & 0x400 or link_count != 1:
            raise _configuration_error(
                "v2_db_identity_unknown",
                "V2 database leaf does not have one stable ordinary-file identity",
            )
        return _WindowsGuardBundle(handles, leaf_identity)
    except Exception as exc:
        bundle = _WindowsGuardBundle(handles, (0, 0))
        owner = _CleanupOwner()
        owner.append(
            bundle,
            "release",
            description="Windows V2 guard release",
        )
        _cleanup_preserving_error(
            exc,
            owner,
        )
        raise


def _open_windows_guard(
    path: Path,
    v2_path: Path,
    *,
    v2_identity: tuple[int, int] | None = None,
) -> _WindowsGuardBundle:
    handles = _open_windows_ancestor_handles(path, role="v3")
    try:
        existed = os.path.lexists(path)
        try:
            leaf_handle = _windows_create_handle(
                path,
                directory=False,
                create_new=not existed,
                writable=True,
            )
        except OSError as exc:
            if existed or not _is_exact_create_race(exc, windows=True):
                raise
            # Another verified opener won CREATE_NEW while the process-wide
            # identity lock was held by the caller.  Reopen that exact leaf;
            # all reparse, link-count, native-ID and V2 comparisons below are
            # still mandatory.
            leaf_handle = _windows_create_handle(
                path,
                directory=False,
                writable=True,
            )
        handles.append(leaf_handle)
        leaf_identity, attributes, link_count = _windows_handle_identity(handles[-1])
        if attributes & 0x400 or link_count != 1:  # reparse point or hardlink
            raise _configuration_error(
                "v3_db_identity_unprovable",
                "V3 database leaf does not have one stable ordinary-file identity",
            )
        if v2_identity is None:
            raise _configuration_error(
                "v2_db_identity_unknown",
                "V2 native identity was not supplied to the V3 handshake",
            )
        if leaf_identity == v2_identity:
            raise _configuration_error(
                "v2_v3_db_same_file",
                "V2 and V3 database files share one filesystem identity",
            )
        return _WindowsGuardBundle(handles, leaf_identity)
    except Exception as exc:
        bundle = _WindowsGuardBundle(handles, (0, 0))
        owner = _CleanupOwner()
        owner.append(
            bundle,
            "release",
            description="Windows V3 guard release",
        )
        _cleanup_preserving_error(
            exc,
            owner,
        )
        raise


def _open_linux_parent(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path.anchor or "/", flags)
    open_descriptors = [descriptor]
    try:
        for part in path.parent.parts[1:]:
            next_descriptor = os.open(part, flags, dir_fd=descriptor)
            open_descriptors.append(next_descriptor)
            os.close(descriptor)
            open_descriptors.remove(descriptor)
            descriptor = next_descriptor
        metadata = os.fstat(descriptor)
        if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022:
            raise _configuration_error(
                "v3_db_identity_unprovable",
                "V3 database parent must be owned by the service user and not writable by others",
            )
        open_descriptors.remove(descriptor)
        return descriptor
    except Exception as exc:
        parent_fd = open_descriptors[-1] if open_descriptors else -1
        bundle = _LinuxGuardBundle(
            parent_fd,
            -1,
            (0, 0),
            ancestor_fds=open_descriptors,
        )
        owner = _CleanupOwner()
        owner.append(
            bundle,
            "release",
            description="Linux V3 parent guard release",
        )
        _cleanup_preserving_error(exc, owner)
        raise


def _open_linux_v2_guard(
    path: Path,
) -> _LinuxGuardBundle:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    ancestor_fds: list[int] = []
    leaf_fd = -1
    try:
        descriptor = os.open(path.anchor or "/", flags)
        ancestor_fds.append(descriptor)
        for part in path.parent.parts[1:]:
            descriptor = os.open(part, flags, dir_fd=descriptor)
            ancestor_fds.append(descriptor)
        parent_metadata = os.fstat(descriptor)
        if parent_metadata.st_uid != os.geteuid() or parent_metadata.st_mode & 0o022:
            raise _configuration_error(
                "v2_db_identity_unknown",
                "V2 database parent identity is not safe for a verified handshake",
            )
        try:
            leaf_fd = os.open(
                path.name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=descriptor,
            )
        except FileNotFoundError as exc:
            raise _configuration_error(
                "v2_db_identity_missing",
                "V2 database disappeared before the identity handshake",
            ) from exc
        metadata = os.fstat(leaf_fd)
        leaf_identity = metadata.st_dev, metadata.st_ino
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise _configuration_error(
                "v2_db_identity_unknown",
                "V2 database leaf does not have one stable ordinary-file identity",
            )
        return _LinuxGuardBundle(
            descriptor,
            leaf_fd,
            leaf_identity,
            ancestor_fds=ancestor_fds,
        )
    except Exception as exc:
        parent_fd = ancestor_fds[-1] if ancestor_fds else -1
        bundle = _LinuxGuardBundle(
            parent_fd,
            leaf_fd,
            (0, 0),
            ancestor_fds=ancestor_fds,
        )
        owner = _CleanupOwner()
        owner.append(
            bundle,
            "release",
            description="Linux V2 guard release",
        )
        _cleanup_preserving_error(
            exc,
            owner,
        )
        raise


def _open_linux_guard(
    path: Path,
    v2_path: Path,
    *,
    v2_identity: tuple[int, int] | None = None,
) -> _LinuxGuardBundle:
    parent_fd = _open_linux_parent(path)
    leaf_fd = -1
    try:
        flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        try:
            leaf_fd = os.open(path.name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent_fd)
        except OSError as exc:
            if not _is_exact_create_race(exc, windows=False):
                raise
            leaf_fd = os.open(path.name, flags, dir_fd=parent_fd)
        metadata = os.fstat(leaf_fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise _configuration_error(
                "v3_db_identity_unprovable",
                "V3 database leaf does not have one stable ordinary-file identity",
            )
        leaf_identity = (metadata.st_dev, metadata.st_ino)
        if v2_identity is None:
            raise _configuration_error(
                "v2_db_identity_unknown",
                "V2 native identity was not supplied to the V3 handshake",
            )
        if leaf_identity == v2_identity:
            raise _configuration_error(
                "v2_v3_db_same_file",
                "V2 and V3 database files share one filesystem identity",
            )
        return _LinuxGuardBundle(parent_fd, leaf_fd, leaf_identity)
    except Exception as exc:
        bundle = _LinuxGuardBundle(parent_fd, leaf_fd, (0, 0))
        owner = _CleanupOwner()
        owner.append(
            bundle,
            "release",
            description="Linux V3 guard release",
        )
        _cleanup_preserving_error(
            exc,
            owner,
        )
        raise


def _main_database_path(connection: sqlite3.Connection) -> Path:
    try:
        rows = connection.execute("PRAGMA database_list").fetchall()
    except sqlite3.Error as exc:
        raise _configuration_error(
            "v3_db_main_handle_mismatch",
            "SQLite main database identity cannot be inspected",
        ) from exc
    for row in rows:
        if len(row) >= 3 and row[1] == "main" and row[2]:
            return Path(row[2])
    raise _configuration_error(
        "v3_db_main_handle_mismatch",
        "SQLite main database is not bound to the requested file",
    )


def _linux_guard_descriptors(guard: _GuardBundle) -> set[int]:
    descriptors: set[int] = set()
    for attribute in ("parent_fd", "leaf_fd"):
        descriptor = getattr(guard, attribute, -1)
        if isinstance(descriptor, int) and descriptor >= 0:
            descriptors.add(descriptor)
    for descriptor in getattr(guard, "ancestor_fds", ()):
        if isinstance(descriptor, int) and descriptor >= 0:
            descriptors.add(descriptor)
    return descriptors


def _linux_regular_fd_identities(
    *,
    excluded_descriptors: set[int],
) -> dict[int, tuple[int, int]]:
    try:
        values = os.listdir("/proc/self/fd")
    except OSError as exc:
        raise _configuration_error(
            "v3_db_identity_unprovable",
            "Linux SQLite descriptor identity cannot be inspected",
        ) from exc
    identities: dict[int, tuple[int, int]] = {}
    for value in values:
        try:
            descriptor = int(value)
        except ValueError as exc:
            raise _configuration_error(
                "v3_db_identity_unprovable",
                "Linux SQLite descriptor identity cannot be inspected",
            ) from exc
        if descriptor in excluded_descriptors:
            continue
        try:
            metadata = os.fstat(descriptor)
        except OSError as exc:
            # /proc/self/fd enumeration owns a transient descriptor that is
            # normally closed before this inspection. Only descriptors that
            # remain live can contribute identity evidence.
            if exc.errno == errno.EBADF:
                continue
            raise _configuration_error(
                "v3_db_identity_unprovable",
                "Linux SQLite descriptor identity cannot be inspected",
            ) from exc
        if stat.S_ISREG(metadata.st_mode):
            identities[descriptor] = metadata.st_dev, metadata.st_ino
    return identities


def _configuration_error_preserving_cleanup(
    error_code: str,
    message: str,
    cause: BaseException,
) -> StoreConfigurationError:
    error = _configuration_error(error_code, message)
    owner = getattr(cause, "cleanup_owner", None)
    if isinstance(owner, _CleanupOwner):
        error.cleanup_owner = owner
    for note in getattr(cause, "__notes__", ()):
        error.add_note(note)
    return error


def _open_v2_handshake_guard(
    v2_path: Path,
) -> _GuardBundle:
    try:
        if os.name == "nt":
            return _open_windows_v2_guard(v2_path)
        if sys.platform.startswith("linux"):
            return _open_linux_v2_guard(v2_path)
        raise _configuration_error(
            "v2_db_identity_unknown",
            "this platform cannot prove the V2 database identity",
        )
    except StoreConfigurationError:
        raise
    except FileNotFoundError as exc:
        raise _configuration_error_preserving_cleanup(
            "v2_db_identity_unknown",
            "V2 database ancestor identity cannot be established",
            exc,
        ) from exc
    except OSError as exc:
        raise _configuration_error_preserving_cleanup(
            "v2_db_identity_unknown",
            "V2 database ancestor or leaf identity cannot be opened safely",
            exc,
        ) from exc


def _connect_with_verified_identity_under_lock(
    path: Path,
    v2_path: Path,
    v2_guard: _GuardBundle,
) -> _GuardedConnection:
    guard: _GuardBundle | None = None
    connection: sqlite3.Connection | None = None
    try:
        if os.name == "nt":
            guard = _open_windows_guard(
                path,
                v2_path,
                v2_identity=v2_guard.leaf_identity,
            )
            connect_target: str | Path = path
            connect_kwargs: dict[str, Any] = {}
            before_descriptor_identities: dict[int, tuple[int, int]] | None = None
            excluded_descriptors: set[int] | None = None
        elif sys.platform.startswith("linux"):
            linux_guard = _open_linux_guard(
                path,
                v2_path,
                v2_identity=v2_guard.leaf_identity,
            )
            guard = linux_guard
            connect_target = (
                f"file:/proc/self/fd/{linux_guard.parent_fd}/{quote(path.name, safe='')}"
                "?mode=rw&cache=private"
            )
            connect_kwargs = {"uri": True}
            excluded_descriptors = _linux_guard_descriptors(v2_guard)
            excluded_descriptors.update(_linux_guard_descriptors(linux_guard))
            before_descriptor_identities = _linux_regular_fd_identities(
                excluded_descriptors=excluded_descriptors,
            )
        else:
            raise _configuration_error(
                "v3_db_identity_unprovable",
                "this platform cannot prove the SQLite main-file identity",
            )

        with _SQLITE_OPEN_LOCK:
            try:
                connection = sqlite3.connect(
                    connect_target,
                    timeout=10.0,
                    isolation_level=None,
                    factory=_GuardedConnection,
                    check_same_thread=False,
                    **connect_kwargs,
                )
            except OSError as exc:
                raise _configuration_error(
                    "v3_db_identity_changed",
                    "V3 database path changed while SQLite was opening it",
                ) from exc
            if not isinstance(connection, _GuardedConnection):
                raise _configuration_error(
                    "v3_db_main_handle_mismatch",
                    "SQLite returned a connection without the required identity guard",
                )
            connection_guard = guard
            assert connection_guard is not None
            connection._retain_identity_guard(connection_guard)
            guard = None
            main_path = _main_database_path(connection)
            if os.name == "nt":
                if not _same_path(main_path.resolve(strict=True), path.resolve(strict=True)):
                    raise _configuration_error(
                        "v3_db_main_handle_mismatch",
                        "SQLite main database is not the requested V3 file",
                    )
                main_handle = _windows_create_handle(main_path, directory=False)
                bundle = _WindowsGuardBundle([main_handle], (0, 0))
                try:
                    main_identity, _attributes, _links = _windows_handle_identity(main_handle)
                    if main_identity != connection_guard.leaf_identity:
                        raise _configuration_error(
                            "v3_db_main_handle_mismatch",
                            "SQLite main handle does not match the guarded V3 leaf",
                        )
                    if main_identity == v2_guard.leaf_identity:
                        raise _configuration_error(
                            "v2_v3_db_same_file",
                            "SQLite main handle is the guarded V2 database",
                        )
                except Exception as exc:
                    owner = _CleanupOwner()
                    owner.append(
                        bundle,
                        "release",
                        description="Windows SQLite main-handle release",
                    )
                    _cleanup_preserving_error(
                        exc,
                        owner,
                    )
                    raise
                try:
                    bundle.release()
                except Exception as cleanup_error:
                    owner = _CleanupOwner()
                    owner.append(
                        bundle,
                        "release",
                        description="Windows SQLite main-handle release",
                    )
                    cleanup_error.add_note(
                        "Windows SQLite main handle retained for deterministic cleanup retry"
                    )
                    _retain_cleanup_owner(cleanup_error, owner)
                    raise
            else:
                assert before_descriptor_identities is not None
                assert excluded_descriptors is not None
                after_descriptor_identities = _linux_regular_fd_identities(
                    excluded_descriptors=excluded_descriptors,
                )
                matches = [
                    descriptor
                    for descriptor, identity in after_descriptor_identities.items()
                    if identity == connection_guard.leaf_identity
                    and before_descriptor_identities.get(descriptor) != identity
                ]
                if len(matches) != 1:
                    raise _configuration_error(
                        "v3_db_main_handle_mismatch",
                        "SQLite main descriptor does not uniquely match the guarded V3 leaf",
                    )
            verified_connection = connection
            connection = None
            return verified_connection
    except Exception as exc:
        owner = _CleanupOwner()
        if connection is not None:
            owner.append(
                connection,
                "close",
                description="unverified SQLite connection close",
            )
        if guard is not None:
            owner.append(
                guard,
                "release",
                description="unretained V3 guard release",
            )
        if owner.pending_resources:
            _cleanup_preserving_error(exc, owner)
        raise


def _json_tree_is_nfc(value: Any) -> bool:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value) == value
    if isinstance(value, list):
        return all(_json_tree_is_nfc(item) for item in value)
    if isinstance(value, dict):
        return all(
            isinstance(key, str)
            and unicodedata.normalize("NFC", key) == key
            and _json_tree_is_nfc(item)
            for key, item in value.items()
        )
    return True


def _is_canonical_json_text(value: Any) -> int:
    if not isinstance(value, str):
        return 0
    try:
        parsed = parse_strict_json(
            value,
            max_bytes=4 * 1024 * 1024,
            max_depth=64,
            max_items=100_000,
            max_string_chars=2 * 1024 * 1024,
        )
        if not _json_tree_is_nfc(parsed):
            return 0
        return int(canonical_json(parsed).decode("utf-8") == value)
    except (ContractError, UnicodeError, ValueError, TypeError):
        return 0


def _register_connection_functions(connection: sqlite3.Connection) -> None:
    connection.create_function(
        "edit_v3_is_canonical_json",
        1,
        _is_canonical_json_text,
        deterministic=True,
    )


def open_store(
    db_path: Path,
    *,
    v2_db_path: Path | None = None,
) -> sqlite3.Connection:
    """Open one verified connection with WAL, FK and timeout guarantees."""

    raw_v3_path = _v3_path_syntax(db_path)
    raw_v2_path = _v2_path_syntax(v2_db_path)
    _path, connection = _open_store_ordered(raw_v3_path, raw_v2_path)
    return connection


def _path_identity(path: Path) -> tuple[tuple[int, int], tuple[int, int] | None]:
    try:
        parent = path.parent.stat()
        file_identity = None
        if path.exists():
            metadata = path.stat()
            if not stat.S_ISREG(metadata.st_mode):
                raise OSError("database path is not a regular file")
            file_identity = (metadata.st_dev, metadata.st_ino)
        return (parent.st_dev, parent.st_ino), file_identity
    except OSError as exc:
        raise _configuration_error(
            "v3_db_identity_unknown",
            "V3 database identity cannot be established",
        ) from exc


def _revalidate_open_identity(
    path: Path,
    before: tuple[tuple[int, int], tuple[int, int] | None],
    v2_path: Path,
) -> None:
    _assert_no_reparse_components(path, role="v3")
    after = _path_identity(path)
    if before[0] != after[0] or (
        before[1] is not None and before[1] != after[1]
    ):
        raise _configuration_error(
            "v3_db_identity_changed",
            "V3 database path identity changed while opening",
        )
    assert_isolated_db(path, v2_path)
    _assert_local_filesystem(path)


def _open_store_ordered(
    raw_v3_path: Path,
    raw_v2_path: Path,
) -> tuple[Path, _GuardedConnection]:
    """Native-pin V2 before the first authoritative V3 filesystem access."""

    if sqlite3.threadsafety != 3:
        raise _configuration_error(
            "v3_sqlite_thread_safety_unavailable",
            "verified V3 connections require serialized SQLite thread safety",
        )
    with _SQLITE_OPEN_LOCK:
        v2_guard = _open_v2_handshake_guard(raw_v2_path)
        connection: _GuardedConnection | None = None
        try:
            path = resolve_db_path(raw_v3_path)
            assert_isolated_db(path, raw_v2_path)
            _assert_local_filesystem(path)
            before = _path_identity(path)
            connection = _connect_with_verified_identity_under_lock(
                path,
                raw_v2_path,
                v2_guard,
            )
            connection.row_factory = sqlite3.Row
            _register_connection_functions(connection)
            _negotiate_wal(connection)
            connection.execute("PRAGMA foreign_keys=ON")
            if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
                raise StoreMigrationError(
                    "v3_foreign_keys_unavailable",
                    "SQLite foreign key enforcement could not be enabled",
                )
            _revalidate_open_identity(path, before, raw_v2_path)
            verified_connection = connection
            connection = None
        except Exception as exc:
            existing_owner = getattr(exc, "cleanup_owner", None)
            if isinstance(existing_owner, _CleanupOwner) and existing_owner.pending_resources:
                if connection is not None:
                    connection_owner = _CleanupOwner()
                    connection_owner.append(
                        connection,
                        "close",
                        description="partially opened V3 connection close",
                    )
                    _cleanup_preserving_error(exc, connection_owner)
                retained_owner = getattr(exc, "cleanup_owner", existing_owner)
                retained_owner.append(
                    v2_guard,
                    "release",
                    description="V2 handshake guard release",
                )
                exc.cleanup_owner = retained_owner
                exc.add_note(
                    "V2 handshake guard retained behind earlier pending cleanup"
                )
            else:
                owner = _CleanupOwner()
                if connection is not None:
                    owner.append(
                        connection,
                        "close",
                        description="partially opened V3 connection close",
                    )
                owner.append(
                    v2_guard,
                    "release",
                    description="V2 handshake guard release",
                )
                _cleanup_preserving_error(
                    exc,
                    owner,
                )
            raise
        try:
            v2_guard.release()
        except Exception as exc:
            owner = _CleanupOwner()
            owner.append(
                verified_connection,
                "close",
                description="unreturned verified V3 connection close",
            )
            owner.append(
                v2_guard,
                "release",
                description="V2 handshake guard release retry",
            )
            _cleanup_preserving_error(
                exc,
                owner,
            )
            raise
        return path, verified_connection


def _apply_schema_v1(connection: sqlite3.Connection) -> None:
    for name in sorted(_CREATE_TABLE_SQL):
        connection.execute(_CREATE_TABLE_SQL[name])
    for name in sorted(_CREATE_INDEX_SQL):
        connection.execute(_CREATE_INDEX_SQL[name])


def _schema_tables(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def _raise_manifest_mismatch(detail: str) -> None:
    raise StoreMigrationError(
        "v3_schema_manifest_mismatch",
        f"V3 schema v1 does not match the frozen manifest: {detail}",
    )


def _validate_schema_manifest(connection: sqlite3.Connection) -> None:
    tables = _schema_tables(connection)
    if tables != set(_SCHEMA_TABLE_COLUMNS):
        _raise_manifest_mismatch("table set")

    for object_type, name, table_name, sql in connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name"
    ):
        if object_type == "table" and name in _SCHEMA_TABLE_COLUMNS:
            continue
        if object_type == "index" and name in _SCHEMA_INDEXES:
            continue
        if (
            object_type == "index"
            and sql is None
            and table_name in _SCHEMA_TABLE_COLUMNS
            and re.fullmatch(
                rf"sqlite_autoindex_{re.escape(table_name)}_[1-9][0-9]*",
                name,
            )
        ):
            continue
        _raise_manifest_mismatch(
            f"unregistered {object_type} object {name} on {table_name}"
        )

    for table, expected_columns in _SCHEMA_TABLE_COLUMNS.items():
        columns = tuple(
            row[1] for row in connection.execute(f"PRAGMA table_info({table})")
        )
        if columns != expected_columns:
            _raise_manifest_mismatch(f"columns for {table}")
        foreign_keys = {
            (row[3], row[2], row[4], row[6])
            for row in connection.execute(f"PRAGMA foreign_key_list({table})")
        }
        if foreign_keys != _SCHEMA_FOREIGN_KEYS.get(table, set()):
            _raise_manifest_mismatch(f"foreign keys for {table}")
        schema_row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        if schema_row is None or _normalize_ddl(schema_row[0]) != _normalize_ddl(
            _CREATE_TABLE_SQL[table]
        ):
            _raise_manifest_mismatch(f"DDL for {table}")

    declared_indexes = {
        row[0]: row[1]
        for row in connection.execute(
            """SELECT name,sql FROM sqlite_master
               WHERE type='index' AND name LIKE 'edit_v3_%' AND sql IS NOT NULL"""
        )
    }
    if set(declared_indexes) != set(_SCHEMA_INDEXES):
        _raise_manifest_mismatch("declared index set")
    for index_name, (expected_unique, expected_columns) in _SCHEMA_INDEXES.items():
        row = connection.execute(
            "SELECT tbl_name,sql FROM sqlite_master WHERE type='index' AND name=?",
            (index_name,),
        ).fetchone()
        if row is None or _normalize_ddl(row[1]) != _normalize_ddl(
            _CREATE_INDEX_SQL[index_name]
        ):
            _raise_manifest_mismatch(f"DDL for {index_name}")
        index_rows = connection.execute(f"PRAGMA index_list({row[0]})").fetchall()
        unique = next(
            (bool(item[2]) for item in index_rows if item[1] == index_name),
            None,
        )
        columns = tuple(
            item[2]
            for item in connection.execute(f"PRAGMA index_info({index_name})")
        )
        if unique is not expected_unique or columns != expected_columns:
            _raise_manifest_mismatch(f"shape for {index_name}")


def _validate_live_integrity(connection: sqlite3.Connection) -> None:
    for pragma in ("quick_check", "integrity_check"):
        rows = connection.execute(f"PRAGMA {pragma}").fetchall()
        if len(rows) != 1 or str(rows[0][0]).lower() != "ok":
            raise StoreMigrationError(
                "v3_integrity_check_failed",
                f"SQLite {pragma} rejected the live V3 database",
            )
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise StoreMigrationError(
            "v3_foreign_key_check_failed",
            "SQLite foreign_key_check found an orphaned V3 row",
        )


def _migrate_or_validate(connection: sqlite3.Connection) -> None:
    connection.execute("BEGIN IMMEDIATE")
    try:
        tables = _schema_tables(connection)
        if not tables:
            _apply_schema_v1(connection)
            now = int(time.time() * 1000)
            connection.execute(
                """INSERT INTO edit_v3_schema_meta(
                       id,version,migration_sha256,created_at,updated_at
                   ) VALUES(1,?,?,?,?)""",
                (SCHEMA_VERSION, MIGRATION_SHA256, now, now),
            )
            _validate_schema_manifest(connection)
            _validate_live_integrity(connection)
        else:
            if "edit_v3_schema_meta" not in tables:
                raise StoreMigrationError(
                    "v3_schema_metadata_missing",
                    "existing database objects have no valid V3 schema metadata",
                )
            rows = connection.execute(
                "SELECT id,version,migration_sha256 FROM edit_v3_schema_meta"
            ).fetchall()
            if len(rows) != 1 or rows[0][0] != 1:
                raise StoreMigrationError(
                    "v3_schema_metadata_invalid",
                    "V3 schema metadata must contain exactly singleton id 1",
                )
            version = int(rows[0][1])
            if version > SCHEMA_VERSION:
                raise StoreMigrationError(
                    "v3_schema_future_version",
                    "database schema version is newer than this runtime",
                )
            if version != SCHEMA_VERSION:
                raise StoreMigrationError(
                    "v3_schema_version_unsupported",
                    "database schema version is not supported",
                )
            if rows[0][2] != MIGRATION_SHA256:
                raise StoreMigrationError(
                    "v3_schema_migration_sha_mismatch",
                    "database migration SHA does not match the frozen migration",
                )
            _validate_schema_manifest(connection)
            _validate_live_integrity(connection)
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise


def init_db(
    db_path: Path | None = None,
    *,
    v2_db_path: Path | None = None,
) -> None:
    """Validate isolation before performing any V3 database side effect."""

    raw_v3_path = _v3_path_syntax(db_path)
    raw_v2_path = _v2_path_syntax(v2_db_path)
    _initialize_db(raw_v3_path, raw_v2_path)


def _initialize_db(raw_v3_path: Path, raw_v2_path: Path) -> Path:
    path, connection = _open_store_ordered(raw_v3_path, raw_v2_path)
    try:
        _migrate_or_validate(connection)
    finally:
        connection.close()
    return path


_T = TypeVar("_T")


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return None if row is None else dict(row)


def _json_text(value: Any) -> str:
    return canonical_json(value).decode("utf-8")


def _json_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _immutable_conflict(identity: str) -> StoreConflictError:
    return StoreConflictError(
        "idempotency_conflict",
        f"immutable V3 identity {identity} was reused with different data",
    )


def _require_integer(name: str, value: Any, *, nullable: bool = False) -> None:
    if nullable and value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int):
        raise _configuration_error(
            "integer_argument_invalid",
            f"{name} must be an integer primitive",
        )
    if value < -(2**63) or value > 2**63 - 1:
        raise _configuration_error(
            "integer_out_of_range",
            f"{name} must fit signed SQLite int64",
        )


def _require_nonblank(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _configuration_error(
            "string_argument_invalid",
            f"{name} must be a non-blank string",
        )
    return value


def _require_now_ms(now_ms: Any) -> int:
    _require_integer("now_ms", now_ms)
    if now_ms < 0:
        raise _configuration_error(
            "timestamp_argument_invalid",
            "now_ms must be a Unix epoch millisecond value",
        )
    return now_ms


def _lease_expiry(lease_seconds: Any, now_ms: Any) -> tuple[int, int]:
    now_ms = _require_now_ms(now_ms)
    _require_integer("lease_seconds", lease_seconds)
    if lease_seconds <= 0:
        raise _configuration_error(
            "lease_duration_invalid",
            "lease_seconds must be a positive integer",
        )
    lease_until = now_ms + lease_seconds * 1000
    _require_integer("lease_until", lease_until)
    return now_ms, lease_until


def _require_state_set(name: str, states: Any) -> frozenset[str]:
    if isinstance(states, (str, bytes)):
        raise _configuration_error(
            "state_set_invalid",
            f"{name} must be a non-empty state collection",
        )
    try:
        normalized = frozenset(states)
    except TypeError as exc:
        raise _configuration_error(
            "state_set_invalid",
            f"{name} must be a non-empty state collection",
        ) from exc
    if not normalized or any(
        not isinstance(state, str) or state not in ALL_STATES for state in normalized
    ):
        raise _configuration_error(
            "state_set_invalid",
            f"{name} contains an unknown or missing state",
        )
    return normalized


def _require_claim(claim: Any) -> LeaseClaim:
    if not isinstance(claim, LeaseClaim):
        raise _configuration_error(
            "lease_claim_invalid",
            "claim must be an immutable LeaseClaim",
        )
    _require_nonblank("claim.job_id", claim.job_id)
    _require_nonblank("claim.worker_id", claim.worker_id)
    _require_integer("claim.fencing_token", claim.fencing_token)
    _require_integer("claim.lease_until", claim.lease_until)
    if claim.fencing_token < 0 or claim.lease_until < 0:
        raise _configuration_error(
            "lease_claim_invalid",
            "claim token and deadline must be non-negative",
        )
    return claim


def _require_sha256(name: str, value: Any) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise _configuration_error(
            "sha256_argument_invalid",
            f"{name} must be a 64-character lowercase SHA-256",
        )
    return value


def _require_publish_operation(operation: Any) -> str:
    if operation not in _PUBLISH_OPERATIONS:
        raise _configuration_error(
            "publish_operation_invalid",
            "publish operation is not part of the frozen outbox contract",
        )
    return operation


def _require_publish_generation(generation: Any) -> int:
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or not 0 <= generation <= _SQLITE_INT64_MAX
    ):
        raise _configuration_error(
            "publish_generation_invalid",
            "publication generation must be a non-negative 64-bit integer",
        )
    return generation


def _require_delivery_object_key(value: Any, environment: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 1_024
        or not value.startswith(f"{environment}/ai-edit-v3/")
        or ".." in value
        or "\\" in value
        or "?" in value
        or "#" in value
        or "://" in value
        or any(
            ord(character) < 0x20
            or 0x7F <= ord(character) <= 0x9F
            or 0xD800 <= ord(character) <= 0xDFFF
            for character in value
        )
    ):
        raise _configuration_error(
            "delivery_object_key_invalid",
            "delivery object key must be one bounded private object key",
        )
    return value


def is_valid_publish_asset_id(value: Any) -> bool:
    """Return whether *value* is one frozen opaque publication identifier."""

    return (
        isinstance(value, str)
        and _PUBLISH_ASSET_ID_PATTERN.fullmatch(value) is not None
    )


def _normalize_publish_decision(decision: Any) -> dict[str, Any]:
    if (
        not isinstance(decision, Mapping)
        or frozenset(decision) != _PUBLISH_DECISION_KEYS
    ):
        raise _configuration_error(
            "publish_evidence_invalid",
            "publication decision must contain only the frozen canonical fields",
        )
    status = decision["status"]
    generation = decision["current_generation"]
    asset_id = decision["asset_id"]
    if not isinstance(status, str) or status not in _PUBLISH_DECISION_STATUSES:
        raise _configuration_error(
            "publish_evidence_invalid",
            "publication decision status is not canonical",
        )
    if (
        isinstance(generation, bool)
        or not isinstance(generation, int)
        or not 0 <= generation <= _SQLITE_INT64_MAX
    ):
        raise _configuration_error(
            "publish_evidence_invalid",
            "publication decision generation must be a non-negative 64-bit integer",
        )
    if status == "publish_won":
        if not is_valid_publish_asset_id(asset_id):
            raise _configuration_error(
                "publish_evidence_invalid",
                "publication winner must contain one opaque asset id",
            )
    elif asset_id is not None:
        raise _configuration_error(
            "publish_evidence_invalid",
            "non-publish decisions must not contain an asset id",
        )
    return {
        "asset_id": asset_id,
        "current_generation": generation,
        "status": status,
    }


def _normalize_publish_evidence(evidence: Any) -> dict[str, Any]:
    if not isinstance(evidence, Mapping):
        raise _configuration_error(
            "publish_evidence_invalid",
            "publication evidence must be a stable mapping",
        )
    keys = frozenset(evidence)
    if keys == _PUBLISH_DECISION_KEYS:
        return _normalize_publish_decision(evidence)
    if keys != _PUBLISH_SAFE_EVIDENCE_KEYS:
        raise _configuration_error(
            "publish_evidence_invalid",
            "publication evidence must contain only frozen safe fields",
        )
    outcome = evidence["outcome"]
    reason_code = evidence["reason_code"]
    if not isinstance(outcome, str) or outcome not in _PUBLISH_SAFE_OUTCOMES:
        raise _configuration_error(
            "publish_evidence_invalid",
            "publication evidence outcome is not frozen",
        )
    if (
        not isinstance(reason_code, str)
        or _PUBLISH_REASON_CODE_PATTERN.fullmatch(reason_code) is None
    ):
        raise _configuration_error(
            "publish_evidence_invalid",
            "publication evidence reason code is not safe",
        )
    return {"outcome": outcome, "reason_code": reason_code}


def _publish_intent_id(job_id: str, generation: int, operation: str) -> str:
    identity = f"{job_id}\0{generation}\0{operation}".encode("utf-8")
    return hashlib.sha256(identity).hexdigest()


def _lease_lost(claim: LeaseClaim) -> LeaseLost:
    return LeaseLost(
        "lease_lost",
        f"lease ownership was lost for job {claim.job_id}",
    )


def _claim_row(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    worker_id: str,
    lease_until: int,
    now_ms: int,
) -> LeaseClaim | None:
    if row["fencing_token"] >= _SQLITE_INT64_MAX:
        raise StoreConflictError(
            "fencing_token_exhausted",
            f"fencing token space is exhausted for job {row['job_id']}",
        )
    connection.execute(
        """UPDATE edit_v3_stage_attempts
           SET status='aborted_lease_lost',finished_at=?,error_code='lease_lost',
               error_json=NULL
           WHERE job_id=? AND fencing_token=? AND status='running'""",
        (now_ms, row["job_id"], row["fencing_token"]),
    )
    updated = connection.execute(
        """UPDATE edit_v3_jobs
           SET worker_id=?,fencing_token=fencing_token+1,lease_until=?,updated_at=?
           WHERE job_id=? AND state=? AND fencing_token=?
             AND fencing_token<?
             AND (worker_id IS NULL OR lease_until IS NULL OR lease_until<=?)""",
        (
            worker_id,
            lease_until,
            now_ms,
            row["job_id"],
            row["state"],
            row["fencing_token"],
            _SQLITE_INT64_MAX,
            now_ms,
        ),
    )
    if updated.rowcount != 1:
        raise _ClaimRaceLost()
    claimed = connection.execute(
        """SELECT job_id,worker_id,fencing_token,lease_until
           FROM edit_v3_jobs WHERE job_id=?""",
        (row["job_id"],),
    ).fetchone()
    return LeaseClaim(
        claimed["job_id"],
        claimed["worker_id"],
        claimed["fencing_token"],
        claimed["lease_until"],
    )


def _claim_next_job_tx(
    connection: sqlite3.Connection,
    worker_id: str,
    lease_seconds: int,
    now_ms: int,
) -> LeaseClaim | None:
    worker_id = _require_nonblank("worker_id", worker_id)
    now_ms, lease_until = _lease_expiry(lease_seconds, now_ms)
    placeholders = ",".join("?" for _ in QUEUE_CLAIMABLE_STATES)
    row = connection.execute(
        f"""SELECT job_id,state,fencing_token FROM edit_v3_jobs
            WHERE state IN ({placeholders})
              AND (worker_id IS NULL OR lease_until IS NULL OR lease_until<=?)
            ORDER BY queued_at ASC,job_id ASC LIMIT 1""",
        (*sorted(QUEUE_CLAIMABLE_STATES), now_ms),
    ).fetchone()
    if row is None:
        return None
    return _claim_row(connection, row, worker_id, lease_until, now_ms)


def _claim_job_tx(
    connection: sqlite3.Connection,
    job_id: str,
    worker_id: str,
    lease_seconds: int,
    now_ms: int,
    expected_states: Any,
) -> LeaseClaim | None:
    job_id = _require_nonblank("job_id", job_id)
    worker_id = _require_nonblank("worker_id", worker_id)
    states = _require_state_set("expected_states", expected_states)
    now_ms, lease_until = _lease_expiry(lease_seconds, now_ms)
    placeholders = ",".join("?" for _ in states)
    row = connection.execute(
        f"""SELECT job_id,state,fencing_token FROM edit_v3_jobs
            WHERE job_id=? AND state IN ({placeholders})
              AND state NOT IN (?,?,?)
              AND (worker_id IS NULL OR lease_until IS NULL OR lease_until<=?)""",
        (job_id, *sorted(states), *sorted(TERMINAL_STATES), now_ms),
    ).fetchone()
    if row is None:
        return None
    return _claim_row(connection, row, worker_id, lease_until, now_ms)


def _renew_lease_tx(
    connection: sqlite3.Connection,
    claim: LeaseClaim,
    lease_seconds: int,
    now_ms: int,
) -> bool:
    claim = _require_claim(claim)
    now_ms, desired = _lease_expiry(lease_seconds, now_ms)
    updated = connection.execute(
        """UPDATE edit_v3_jobs
           SET lease_until=CASE WHEN lease_until>? THEN lease_until ELSE ? END,
               updated_at=?
           WHERE job_id=? AND worker_id=? AND fencing_token=? AND lease_until>?""",
        (
            desired,
            desired,
            now_ms,
            claim.job_id,
            claim.worker_id,
            claim.fencing_token,
            now_ms,
        ),
    )
    return updated.rowcount == 1


def _lease_owned_tx(
    connection: sqlite3.Connection,
    claim: LeaseClaim,
    now_ms: int,
) -> bool:
    claim = _require_claim(claim)
    now_ms = _require_now_ms(now_ms)
    return (
        connection.execute(
            """SELECT 1 FROM edit_v3_jobs
               WHERE job_id=? AND worker_id=? AND fencing_token=? AND lease_until>?""",
            (claim.job_id, claim.worker_id, claim.fencing_token, now_ms),
        ).fetchone()
        is not None
    )


def _historical_publish_authority_tx(
    connection: sqlite3.Connection,
    claim: LeaseClaim,
    publish_generation: int,
    environment: str,
    now_ms: int,
) -> dict[str, Any]:
    """Validate one frozen historical publication generation under a live claim."""

    claim = _require_claim(claim)
    publish_generation = _require_publish_generation(publish_generation)
    now_ms = _require_now_ms(now_ms)
    if publish_generation >= claim.fencing_token:
        raise StoreConflictError(
            "publish_generation_not_historical",
            "publication authority must reuse an older frozen generation",
        )
    job = connection.execute(
        """SELECT * FROM edit_v3_jobs
           WHERE job_id=? AND environment=? AND worker_id=?
             AND fencing_token=? AND lease_until>?""",
        (
            claim.job_id,
            environment,
            claim.worker_id,
            claim.fencing_token,
            now_ms,
        ),
    ).fetchone()
    if job is None:
        raise _lease_lost(claim)
    if job["state"] not in _PUBLISH_AUTHORITY_STATES:
        raise StoreConflictError(
            "publish_authority_state_invalid",
            "historical publication authority requires a reconciliation state",
        )
    rows = connection.execute(
        """SELECT * FROM edit_v3_publish_intents
           WHERE job_id=? AND publish_generation=?""",
        (claim.job_id, publish_generation),
    ).fetchall()
    by_operation = {row["operation"]: row for row in rows}
    if len(rows) != len(_PUBLISH_OPERATIONS) or set(by_operation) != set(
        _PUBLISH_OPERATIONS
    ):
        raise StoreConflictError(
            "publish_intent_conflict",
            "historical publication generation has an incomplete identity set",
        )
    metadata_sha256 = rows[0]["metadata_sha256"]
    for operation in _PUBLISH_OPERATIONS:
        row = by_operation[operation]
        expected_key = (
            f"ai-edit-v3:{claim.job_id}:publish:"
            f"{_PUBLISH_KEY_SEGMENTS[operation]}:{publish_generation}"
        )
        if (
            row["id"]
            != _publish_intent_id(claim.job_id, publish_generation, operation)
            or row["job_id"] != claim.job_id
            or row["publish_generation"] != publish_generation
            or row["operation"] != operation
            or row["external_idempotency_key"] != expected_key
            or row["object_key"] != job["delivery_object_key"]
            or row["metadata_sha256"] != metadata_sha256
            or row["expected_decision"]
            != _PUBLISH_EXPECTED_DECISIONS[operation]
            or row["fencing_token"] != publish_generation
        ):
            raise StoreConflictError(
                "publish_intent_conflict",
                "historical publication authority diverged from its frozen identity",
            )
    if not any(row["status"] == "unknown" for row in rows):
        raise StoreConflictError(
            "publish_authority_missing",
            "historical publication authority has no unresolved unknown operation",
        )
    return {
        "job": dict(job),
        "publish_generation": publish_generation,
        "query": dict(by_operation["query_decision"]),
    }


def _freeze_full_refund_intent_tx(
    connection: sqlite3.Connection,
    job: Mapping[str, Any],
    now_ms: int,
) -> dict[str, Any]:
    """Create or validate the sole deterministic full-refund outbox row."""

    target = job["confirmed_preheld_total"]
    refunded = job["confirmed_refunded_total"]
    request_amount = target - refunded
    job_id = job["job_id"]
    external_key = f"ai-edit-v3:{job_id}:refund_full"
    existing = connection.execute(
        """SELECT * FROM edit_v3_billing_intents
           WHERE environment=? AND owner_id=? AND job_id=?
             AND operation='refund_full'""",
        (job["environment"], job["owner_id"], job_id),
    ).fetchone()
    if existing is None:
        unresolved = connection.execute(
            """SELECT 1 FROM edit_v3_billing_intents
               WHERE job_id=? AND operation='refund_delta'
                 AND status IN ('pending','retryable_absent','unknown',
                                'reconciliation_pending')
               LIMIT 1""",
            (job_id,),
        ).fetchone()
        if unresolved is not None:
            raise StoreConflictError(
                "overlapping_refund_intent",
                "delta refund must converge before full refund is frozen",
            )
        status = "completed" if request_amount == 0 else "pending"
        completed_at = now_ms if request_amount == 0 else None
        evidence_json = (
            _json_text({"zero_amount": True}) if request_amount == 0 else None
        )
        refund_identity = hashlib.sha256(
            f"{job_id}\0refund_full".encode("utf-8")
        ).hexdigest()
        connection.execute(
            """INSERT INTO edit_v3_billing_intents(
                   id,environment,owner_id,job_id,operation,
                   external_idempotency_key,request_sha256,
                   refund_target_total,request_amount,status,
                   authority_evidence_json,reason,resume_state,
                   created_at,updated_at,completed_at
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                refund_identity,
                job["environment"],
                job["owner_id"],
                job_id,
                "refund_full",
                external_key,
                job["request_sha256"],
                target,
                request_amount,
                status,
                evidence_json,
                "refund",
                "refund_pending",
                now_ms,
                now_ms,
                completed_at,
            ),
        )
        existing = connection.execute(
            "SELECT * FROM edit_v3_billing_intents WHERE id=?",
            (refund_identity,),
        ).fetchone()
    else:
        expected = {
            "environment": job["environment"],
            "owner_id": job["owner_id"],
            "job_id": job_id,
            "operation": "refund_full",
            "external_idempotency_key": external_key,
            "request_sha256": job["request_sha256"],
            "refund_target_total": target,
            "reason": "refund",
            "resume_state": "refund_pending",
        }
        original_refunded = target - existing["request_amount"]
        if (
            not all(existing[key] == value for key, value in expected.items())
            or isinstance(existing["request_amount"], bool)
            or not isinstance(existing["request_amount"], int)
            or not 0 <= original_refunded <= refunded <= target
        ):
            raise StoreConflictError(
                "billing_intent_conflict",
                "existing full-refund intent diverges from cancel authority",
            )
    return dict(existing)


def _get_job_for_claim_tx(
    connection: sqlite3.Connection,
    claim: LeaseClaim,
    environment: str,
    now_ms: int,
) -> dict[str, Any]:
    claim = _require_claim(claim)
    now_ms = _require_now_ms(now_ms)
    row = connection.execute(
        """SELECT * FROM edit_v3_jobs
           WHERE job_id=? AND environment=? AND worker_id=?
             AND fencing_token=? AND lease_until>?""",
        (
            claim.job_id,
            environment,
            claim.worker_id,
            claim.fencing_token,
            now_ms,
        ),
    ).fetchone()
    if row is None:
        raise _lease_lost(claim)
    return dict(row)


def _transition_leased_tx(
    connection: sqlite3.Connection,
    claim: LeaseClaim,
    expected_states: Any,
    target_state: str,
    now_ms: int,
    lease_seconds: int,
    *,
    preserve_current_lease: bool = False,
) -> bool:
    claim = _require_claim(claim)
    states = _require_state_set("expected_states", expected_states)
    target_state = _require_nonblank("target_state", target_state)
    if target_state not in ALL_STATES or any(
        target_state not in ALLOWED_TRANSITIONS[state] for state in states
    ):
        raise _configuration_error(
            "state_transition_invalid",
            "requested transition is outside the frozen V3 state graph",
        )
    if type(preserve_current_lease) is not bool:
        raise _configuration_error(
            "lease_preservation_invalid",
            "preserve_current_lease must be boolean",
        )
    if preserve_current_lease:
        now_ms = _require_now_ms(now_ms)
        desired = None
    else:
        now_ms, desired = _lease_expiry(lease_seconds, now_ms)
    placeholders = ",".join("?" for _ in states)
    running_guard = "" if states.isdisjoint(MEDIA_STATES) else (
        " AND NOT EXISTS(SELECT 1 FROM edit_v3_stage_attempts AS a"
        " WHERE a.job_id=edit_v3_jobs.job_id AND a.status='running')"
    )

    if target_state == "repair_planning":
        if preserve_current_lease:
            raise _configuration_error(
                "lease_preservation_invalid",
                "repair planning cannot preserve the current lease",
            )
        assert desired is not None
        updated = connection.execute(
            f"""UPDATE edit_v3_jobs
                SET state='repair_planning',repair_count=1,
                    repair_budget_granted_at=?,
                    processing_deadline_at=processing_deadline_at+600000,
                    lease_until=CASE WHEN lease_until>? THEN lease_until ELSE ? END,
                    updated_at=?
                WHERE job_id=? AND worker_id=? AND fencing_token=? AND lease_until>?
                  AND state IN ({placeholders}) AND repair_count=0
                  AND processing_deadline_at IS NOT NULL
                  AND processing_deadline_at<=?{running_guard}""",
            (
                now_ms,
                desired,
                desired,
                now_ms,
                claim.job_id,
                claim.worker_id,
                claim.fencing_token,
                now_ms,
                *sorted(states),
                _SQLITE_INT64_MAX - 600_000,
            ),
        )
        if updated.rowcount == 1:
            return True
        deadline_overflow = connection.execute(
            f"""SELECT 1 FROM edit_v3_jobs
                WHERE job_id=? AND worker_id=? AND fencing_token=? AND lease_until>?
                  AND state IN ({placeholders}) AND repair_count=0
                  AND processing_deadline_at IS NOT NULL
                  AND processing_deadline_at>?{running_guard}""",
            (
                claim.job_id,
                claim.worker_id,
                claim.fencing_token,
                now_ms,
                *sorted(states),
                _SQLITE_INT64_MAX - 600_000,
            ),
        ).fetchone()
        if deadline_overflow is not None:
            raise StoreConflictError(
                "processing_deadline_overflow",
                "repair budget would exceed the SQLite signed integer range",
            )
        replay = connection.execute(
            f"""UPDATE edit_v3_jobs
                SET lease_until=CASE WHEN lease_until>? THEN lease_until ELSE ? END,
                    updated_at=?
                WHERE job_id=? AND worker_id=? AND fencing_token=? AND lease_until>?
                  AND state='repair_planning' AND repair_count=1
                  AND repair_budget_granted_at IS NOT NULL
                  AND processing_deadline_at IS NOT NULL{running_guard}""",
            (
                desired,
                desired,
                now_ms,
                claim.job_id,
                claim.worker_id,
                claim.fencing_token,
                now_ms,
            ),
        )
        return replay.rowcount == 1

    clears_lease = target_state in TERMINAL_STATES or target_state in {
        "failed_reconciliation_pending",
        "failed_asset_decision_pending",
    }
    if clears_lease:
        lease_assignment = "worker_id=NULL,lease_until=NULL"
        lease_parameters: tuple[int, ...] = ()
    elif preserve_current_lease:
        lease_assignment = "lease_until=lease_until"
        lease_parameters = ()
    else:
        assert desired is not None
        lease_assignment = (
            "lease_until=CASE WHEN lease_until>? THEN lease_until ELSE ? END"
        )
        lease_parameters = (desired, desired)
    updated = connection.execute(
        f"""UPDATE edit_v3_jobs
            SET state=?,{lease_assignment},updated_at=?
            WHERE job_id=? AND worker_id=? AND fencing_token=? AND lease_until>?
              AND state IN ({placeholders}){running_guard}""",
        (
            target_state,
            *lease_parameters,
            now_ms,
            claim.job_id,
            claim.worker_id,
            claim.fencing_token,
            now_ms,
            *sorted(states),
        ),
    )
    return updated.rowcount == 1


def _start_stage_attempt_tx(
    connection: sqlite3.Connection,
    claim: LeaseClaim,
    stage: str,
    input_sha256: str,
    now_ms: int,
) -> dict[str, Any]:
    claim = _require_claim(claim)
    stage = _require_nonblank("stage", stage)
    input_sha256 = _require_sha256("input_sha256", input_sha256)
    now_ms = _require_now_ms(now_ms)
    replay = connection.execute(
        """SELECT a.* FROM edit_v3_stage_attempts AS a
           JOIN edit_v3_jobs AS j ON j.job_id=a.job_id
           WHERE a.job_id=? AND a.stage=? AND a.fencing_token=?
             AND j.worker_id=? AND j.fencing_token=? AND j.lease_until>?""",
        (
            claim.job_id,
            stage,
            claim.fencing_token,
            claim.worker_id,
            claim.fencing_token,
            now_ms,
        ),
    ).fetchone()
    if replay is not None:
        if replay["input_sha256"] != input_sha256:
            raise StoreConflictError(
                "stage_attempt_input_conflict",
                "the same lease and stage cannot be reused with another input",
            )
        return dict(replay)

    attempt = connection.execute(
        """SELECT COALESCE(MAX(attempt),0)+1
           FROM edit_v3_stage_attempts WHERE job_id=? AND stage=?""",
        (claim.job_id, stage),
    ).fetchone()[0]
    stage_attempt_id = f"stage-{uuid.uuid4().hex}"
    try:
        inserted = connection.execute(
            """INSERT INTO edit_v3_stage_attempts(
                   id,job_id,stage,attempt,worker_id,fencing_token,status,
                   input_sha256,started_at
               )
               SELECT ?,j.job_id,?,?,j.worker_id,j.fencing_token,'running',?,?
               FROM edit_v3_jobs AS j
               WHERE j.job_id=? AND j.worker_id=? AND j.fencing_token=?
                 AND j.lease_until>?""",
            (
                stage_attempt_id,
                stage,
                attempt,
                input_sha256,
                now_ms,
                claim.job_id,
                claim.worker_id,
                claim.fencing_token,
                now_ms,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise StoreConflictError(
            "stage_attempt_running",
            "the job already has a running stage attempt",
        ) from exc
    if inserted.rowcount != 1:
        raise _lease_lost(claim)
    return dict(
        connection.execute(
            "SELECT * FROM edit_v3_stage_attempts WHERE id=?",
            (stage_attempt_id,),
        ).fetchone()
    )


def _finish_stage_attempt_tx(
    connection: sqlite3.Connection,
    claim: LeaseClaim,
    stage_attempt_id: str,
    status: str,
    now_ms: int,
    error_code: str | None,
    error: Any,
) -> dict[str, Any]:
    claim = _require_claim(claim)
    stage_attempt_id = _require_nonblank("stage_attempt_id", stage_attempt_id)
    if status not in {"completed", "failed", "skipped"}:
        raise _configuration_error(
            "stage_attempt_status_invalid",
            "stage attempt status must be completed, failed or skipped",
        )
    now_ms = _require_now_ms(now_ms)
    if error_code is not None:
        _require_nonblank("error_code", error_code)
    error_json = None if error is None else _json_text(error)
    updated = connection.execute(
        """UPDATE edit_v3_stage_attempts
           SET status=?,finished_at=?,error_code=?,error_json=?
           WHERE id=? AND job_id=? AND fencing_token=? AND status='running'
             AND (?<>'skipped' OR EXISTS(
                 SELECT 1 FROM edit_v3_checkpoints AS c
                 WHERE c.stage_attempt_id=edit_v3_stage_attempts.id
             ))
             AND EXISTS(
                 SELECT 1 FROM edit_v3_jobs AS j
                 WHERE j.job_id=edit_v3_stage_attempts.job_id
                   AND j.worker_id=? AND j.fencing_token=? AND j.lease_until>?
             )""",
        (
            status,
            now_ms,
            error_code,
            error_json,
            stage_attempt_id,
            claim.job_id,
            claim.fencing_token,
            status,
            claim.worker_id,
            claim.fencing_token,
            now_ms,
        ),
    )
    if updated.rowcount == 1:
        return dict(
            connection.execute(
                "SELECT * FROM edit_v3_stage_attempts WHERE id=?",
                (stage_attempt_id,),
            ).fetchone()
        )
    existing = connection.execute(
        """SELECT a.* FROM edit_v3_stage_attempts AS a
           JOIN edit_v3_jobs AS j ON j.job_id=a.job_id
           WHERE a.id=? AND a.job_id=? AND a.fencing_token=?
             AND j.worker_id=? AND j.fencing_token=? AND j.lease_until>?""",
        (
            stage_attempt_id,
            claim.job_id,
            claim.fencing_token,
            claim.worker_id,
            claim.fencing_token,
            now_ms,
        ),
    ).fetchone()
    if (
        existing is not None
        and existing["status"] == status
        and existing["error_code"] == error_code
        and existing["error_json"] == error_json
    ):
        return dict(existing)
    if existing is not None and existing["status"] == "running" and status == "skipped":
        raise StoreConflictError(
            "skipped_checkpoint_required",
            "a skipped stage attempt requires an immutable checkpoint",
        )
    if existing is None and not _lease_owned_tx(connection, claim, now_ms):
        raise _lease_lost(claim)
    raise StoreConflictError(
        "stage_attempt_not_running",
        "stage attempt is missing, belongs elsewhere or is already closed",
    )


def _save_checkpoint_tx(
    connection: sqlite3.Connection,
    claim: LeaseClaim,
    stage_attempt_id: str,
    input_sha256: str,
    output: Any,
    now_ms: int,
) -> dict[str, Any]:
    claim = _require_claim(claim)
    stage_attempt_id = _require_nonblank("stage_attempt_id", stage_attempt_id)
    input_sha256 = _require_sha256("input_sha256", input_sha256)
    now_ms = _require_now_ms(now_ms)
    output_json = _json_text(output)
    output_sha256 = _json_sha256(output)
    attempt = connection.execute(
        """SELECT a.* FROM edit_v3_stage_attempts AS a
           JOIN edit_v3_jobs AS j ON j.job_id=a.job_id
           WHERE a.id=? AND a.job_id=? AND a.fencing_token=?
             AND j.worker_id=? AND j.fencing_token=? AND j.lease_until>?""",
        (
            stage_attempt_id,
            claim.job_id,
            claim.fencing_token,
            claim.worker_id,
            claim.fencing_token,
            now_ms,
        ),
    ).fetchone()
    if attempt is None:
        if not _lease_owned_tx(connection, claim, now_ms):
            raise _lease_lost(claim)
        raise StoreConflictError(
            "checkpoint_attempt_mismatch",
            "checkpoint attempt does not match the leased job, stage or input",
        )
    if attempt["input_sha256"] != input_sha256:
        raise StoreConflictError(
            "checkpoint_attempt_mismatch",
            "checkpoint attempt does not match the leased job, stage or input",
        )
    existing = connection.execute(
        """SELECT c.* FROM edit_v3_checkpoints AS c
           JOIN edit_v3_jobs AS j ON j.job_id=c.job_id
           WHERE c.job_id=? AND c.stage=? AND c.input_sha256=?
             AND j.worker_id=? AND j.fencing_token=? AND j.lease_until>?""",
        (
            claim.job_id,
            attempt["stage"],
            input_sha256,
            claim.worker_id,
            claim.fencing_token,
            now_ms,
        ),
    ).fetchone()
    if existing is not None:
        if (
            existing["output_json"] != output_json
            or existing["output_sha256"] != output_sha256
        ):
            raise StoreConflictError(
                "checkpoint_output_conflict",
                "checkpoint input is already bound to another immutable output",
            )
        return dict(existing)
    version = connection.execute(
        """SELECT COALESCE(MAX(version),0)+1 FROM edit_v3_checkpoints
           WHERE job_id=? AND stage=?""",
        (claim.job_id, attempt["stage"]),
    ).fetchone()[0]
    checkpoint_id = f"checkpoint-{uuid.uuid4().hex}"
    inserted = connection.execute(
        """INSERT INTO edit_v3_checkpoints(
               id,job_id,stage,version,stage_attempt_id,input_sha256,output_json,
               output_sha256,fencing_token,created_at
           )
           SELECT ?,a.job_id,a.stage,?,?,a.input_sha256,?,?,j.fencing_token,?
           FROM edit_v3_stage_attempts AS a
           JOIN edit_v3_jobs AS j ON j.job_id=a.job_id
           WHERE a.id=? AND a.job_id=? AND a.fencing_token=?
             AND a.input_sha256=? AND j.worker_id=? AND j.fencing_token=?
             AND j.lease_until>?""",
        (
            checkpoint_id,
            version,
            stage_attempt_id,
            output_json,
            output_sha256,
            now_ms,
            stage_attempt_id,
            claim.job_id,
            claim.fencing_token,
            input_sha256,
            claim.worker_id,
            claim.fencing_token,
            now_ms,
        ),
    )
    if inserted.rowcount != 1:
        raise _lease_lost(claim)
    return dict(
        connection.execute(
            "SELECT * FROM edit_v3_checkpoints WHERE id=?", (checkpoint_id,)
        ).fetchone()
    )


def _get_checkpoint_for_claim_tx(
    connection: sqlite3.Connection,
    claim: LeaseClaim,
    stage: str,
    input_sha256: str,
    now_ms: int,
) -> dict[str, Any] | None:
    claim = _require_claim(claim)
    stage = _require_nonblank("stage", stage)
    input_sha256 = _require_sha256("input_sha256", input_sha256)
    now_ms = _require_now_ms(now_ms)
    row = connection.execute(
        """SELECT c.* FROM edit_v3_checkpoints AS c
           JOIN edit_v3_jobs AS j ON j.job_id=c.job_id
           WHERE c.job_id=? AND c.stage=? AND c.input_sha256=?
             AND j.worker_id=? AND j.fencing_token=? AND j.lease_until>?""",
        (
            claim.job_id,
            stage,
            input_sha256,
            claim.worker_id,
            claim.fencing_token,
            now_ms,
        ),
    ).fetchone()
    if row is not None:
        return dict(row)
    if not _lease_owned_tx(connection, claim, now_ms):
        raise _lease_lost(claim)
    return None


def _close_running_attempts_tx(
    connection: sqlite3.Connection,
    claim: LeaseClaim,
    now_ms: int,
) -> int:
    claim = _require_claim(claim)
    now_ms = _require_now_ms(now_ms)
    if not _lease_owned_tx(connection, claim, now_ms):
        raise _lease_lost(claim)
    updated = connection.execute(
        """UPDATE edit_v3_stage_attempts
           SET status='aborted_lease_lost',finished_at=?,error_code='lease_lost',
               error_json=NULL
           WHERE job_id=? AND fencing_token=? AND status='running'
             AND EXISTS(
                 SELECT 1 FROM edit_v3_jobs AS j
                 WHERE j.job_id=edit_v3_stage_attempts.job_id
                   AND j.worker_id=? AND j.fencing_token=? AND j.lease_until>?
             )""",
        (
            now_ms,
            claim.job_id,
            claim.fencing_token,
            claim.worker_id,
            claim.fencing_token,
            now_ms,
        ),
    )
    return updated.rowcount


def _release_lease_tx(
    connection: sqlite3.Connection,
    claim: LeaseClaim,
    now_ms: int,
) -> bool:
    claim = _require_claim(claim)
    now_ms = _require_now_ms(now_ms)
    updated = connection.execute(
        """UPDATE edit_v3_jobs
           SET worker_id=NULL,lease_until=NULL,updated_at=?
           WHERE job_id=? AND worker_id=? AND fencing_token=? AND lease_until>?
             AND NOT EXISTS(
                 SELECT 1 FROM edit_v3_stage_attempts AS a
                 WHERE a.job_id=edit_v3_jobs.job_id AND a.status='running'
             )""",
        (now_ms, claim.job_id, claim.worker_id, claim.fencing_token, now_ms),
    )
    if updated.rowcount == 1:
        return True
    if not _lease_owned_tx(connection, claim, now_ms):
        return False
    raise StoreConflictError(
        "running_stage_attempt_exists",
        "a running stage attempt must be closed before lease release",
    )


def _record_provider_intent_tx(
    connection: sqlite3.Connection,
    claim: LeaseClaim,
    stage: str,
    stage_attempt_id: str,
    provider: str,
    capability: str,
    operation_key: str,
    request_sha256: str,
    now_ms: int,
) -> dict[str, Any]:
    claim = _require_claim(claim)
    stage = _require_nonblank("stage", stage)
    stage_attempt_id = _require_nonblank("stage_attempt_id", stage_attempt_id)
    provider = _require_nonblank("provider", provider)
    capability = _require_nonblank("capability", capability)
    operation_key = _require_nonblank("operation_key", operation_key)
    request_sha256 = _require_sha256("request_sha256", request_sha256)
    now_ms = _require_now_ms(now_ms)
    immutable = {
        "job_id": claim.job_id,
        "stage": stage,
        "stage_attempt_id": stage_attempt_id,
        "provider": provider,
        "capability": capability,
        "operation_key": operation_key,
        "request_sha256": request_sha256,
    }
    existing = connection.execute(
        """SELECT p.* FROM edit_v3_provider_tasks AS p
           JOIN edit_v3_jobs AS j ON j.job_id=p.job_id
           WHERE p.operation_key=? AND p.job_id=?
             AND j.worker_id=? AND j.fencing_token=? AND j.lease_until>?""",
        (
            operation_key,
            claim.job_id,
            claim.worker_id,
            claim.fencing_token,
            now_ms,
        ),
    ).fetchone()
    if existing is not None:
        if not all(existing[key] == value for key, value in immutable.items()):
            raise StoreConflictError(
                "provider_intent_conflict",
                "provider operation key is bound to another immutable intent",
            )
        return dict(existing)
    if not _lease_owned_tx(connection, claim, now_ms):
        raise _lease_lost(claim)
    collision = connection.execute(
        "SELECT 1 FROM edit_v3_provider_tasks WHERE operation_key=?",
        (operation_key,),
    ).fetchone()
    if collision is not None:
        raise StoreConflictError(
            "provider_intent_conflict",
            "provider operation key is bound to another immutable intent",
        )
    provider_task_id = f"provider-{uuid.uuid4().hex}"
    try:
        inserted = connection.execute(
            """INSERT INTO edit_v3_provider_tasks(
                   id,job_id,stage,stage_attempt_id,provider,capability,operation_key,
                   request_sha256,status,fencing_token,first_unknown_at,last_checked_at,
                   created_at,updated_at
               )
               SELECT ?,j.job_id,?,a.id,?,?,?,?,'intent_recorded',j.fencing_token,
                      ?,NULL,?,?
               FROM edit_v3_jobs AS j
               JOIN edit_v3_stage_attempts AS a ON a.job_id=j.job_id
               WHERE j.job_id=? AND j.worker_id=? AND j.fencing_token=?
                 AND j.lease_until>? AND a.id=? AND a.stage=?
                 AND a.fencing_token=j.fencing_token AND a.status='running'""",
            (
                provider_task_id,
                stage,
                provider,
                capability,
                operation_key,
                request_sha256,
                now_ms,
                now_ms,
                now_ms,
                claim.job_id,
                claim.worker_id,
                claim.fencing_token,
                now_ms,
                stage_attempt_id,
                stage,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise StoreConflictError(
            "provider_intent_conflict",
            "provider operation key conflicts with another immutable intent",
        ) from exc
    if inserted.rowcount != 1:
        if not _lease_owned_tx(connection, claim, now_ms):
            raise _lease_lost(claim)
        attempt = connection.execute(
            """SELECT a.status FROM edit_v3_stage_attempts AS a
               JOIN edit_v3_jobs AS j ON j.job_id=a.job_id
               WHERE a.id=? AND a.job_id=? AND a.stage=?
                 AND a.fencing_token=j.fencing_token
                 AND j.worker_id=? AND j.fencing_token=? AND j.lease_until>?""",
            (
                stage_attempt_id,
                claim.job_id,
                stage,
                claim.worker_id,
                claim.fencing_token,
                now_ms,
            ),
        ).fetchone()
        if attempt is not None and attempt["status"] != "running":
            raise StoreConflictError(
                "provider_attempt_not_running",
                "new provider intent requires a running stage attempt",
            )
        raise StoreConflictError(
            "provider_attempt_mismatch",
            "provider intent stage attempt does not match the current leased stage",
        )
    return dict(
        connection.execute(
            "SELECT * FROM edit_v3_provider_tasks WHERE id=?",
            (provider_task_id,),
        ).fetchone()
    )


def _get_provider_task_for_claim_tx(
    connection: sqlite3.Connection,
    claim: LeaseClaim,
    operation_key: str,
    now_ms: int,
) -> dict[str, Any] | None:
    claim = _require_claim(claim)
    operation_key = _require_nonblank("operation_key", operation_key)
    now_ms = _require_now_ms(now_ms)
    row = connection.execute(
        """SELECT p.* FROM edit_v3_provider_tasks AS p
           JOIN edit_v3_jobs AS j ON j.job_id=p.job_id
           WHERE p.job_id=? AND p.operation_key=?
             AND j.worker_id=? AND j.fencing_token=? AND j.lease_until>?""",
        (
            claim.job_id,
            operation_key,
            claim.worker_id,
            claim.fencing_token,
            now_ms,
        ),
    ).fetchone()
    if row is not None:
        return dict(row)
    if not _lease_owned_tx(connection, claim, now_ms):
        raise _lease_lost(claim)
    return None


def _bind_provider_result_tx(
    connection: sqlite3.Connection,
    claim: LeaseClaim,
    operation_key: str,
    external_id: str,
    status: str,
    result: Any,
    now_ms: int,
) -> dict[str, Any]:
    claim = _require_claim(claim)
    operation_key = _require_nonblank("operation_key", operation_key)
    external_id = _require_nonblank("external_id", external_id)
    status = _require_nonblank("status", status)
    now_ms = _require_now_ms(now_ms)
    result_json = _json_text(result)
    existing = connection.execute(
        """SELECT p.* FROM edit_v3_provider_tasks AS p
           JOIN edit_v3_jobs AS j ON j.job_id=p.job_id
           WHERE p.job_id=? AND p.operation_key=?
             AND j.worker_id=? AND j.fencing_token=? AND j.lease_until>?""",
        (
            claim.job_id,
            operation_key,
            claim.worker_id,
            claim.fencing_token,
            now_ms,
        ),
    ).fetchone()
    if existing is None:
        if not _lease_owned_tx(connection, claim, now_ms):
            raise _lease_lost(claim)
        raise StoreConflictError(
            "provider_intent_missing",
            "provider result cannot be bound before its immutable intent",
        )
    if existing["external_id"] is not None or existing["result_json"] is not None:
        if (
            existing["external_id"] == external_id
            and existing["status"] == status
            and existing["result_json"] == result_json
        ):
            return dict(existing)
        raise StoreConflictError(
            "provider_result_conflict",
            "provider result is immutable once bound",
        )
    try:
        updated = connection.execute(
            """UPDATE edit_v3_provider_tasks
               SET external_id=?,status=?,result_json=?,fencing_token=?,
                   last_checked_at=?,updated_at=?
               WHERE job_id=? AND operation_key=?
                 AND external_id IS NULL AND result_json IS NULL
                 AND EXISTS(
                     SELECT 1 FROM edit_v3_jobs AS j
                     WHERE j.job_id=edit_v3_provider_tasks.job_id
                       AND j.worker_id=? AND j.fencing_token=? AND j.lease_until>?
                 )""",
            (
                external_id,
                status,
                result_json,
                claim.fencing_token,
                now_ms,
                now_ms,
                claim.job_id,
                operation_key,
                claim.worker_id,
                claim.fencing_token,
                now_ms,
            ),
        )
    except sqlite3.IntegrityError as exc:
        raise StoreConflictError(
            "provider_external_id_conflict",
            "provider external ID is already bound",
        ) from exc
    if updated.rowcount != 1:
        if not _lease_owned_tx(connection, claim, now_ms):
            raise _lease_lost(claim)
        raise StoreConflictError(
            "provider_result_conflict",
            "provider result was concurrently or divergently bound",
        )
    return dict(
        connection.execute(
            """SELECT * FROM edit_v3_provider_tasks
               WHERE job_id=? AND operation_key=?""",
            (claim.job_id, operation_key),
        ).fetchone()
    )


def _billing_row_for_claim_tx(
    connection: sqlite3.Connection,
    intent_id: str,
    claim: LeaseClaim,
    now_ms: int,
) -> sqlite3.Row:
    claim = _require_claim(claim)
    intent_id = _require_nonblank("intent_id", intent_id)
    now_ms = _require_now_ms(now_ms)
    row = connection.execute(
        """SELECT b.*,
                  j.state AS job_state,
                  j.reconciliation_reason AS job_reconciliation_reason,
                  j.resume_state AS job_resume_state,
                  j.confirmed_preheld_total AS job_confirmed_preheld_total,
                  j.confirmed_refunded_total AS job_confirmed_refunded_total,
                  j.queued_at AS job_queued_at,
                  j.processing_deadline_at AS job_processing_deadline_at,
                  j.created_at AS job_created_at
           FROM edit_v3_billing_intents AS b
           JOIN edit_v3_jobs AS j ON j.job_id=b.job_id
           WHERE b.id=? AND j.job_id=? AND j.worker_id=?
             AND j.fencing_token=? AND j.lease_until>?""",
        (
            intent_id,
            claim.job_id,
            claim.worker_id,
            claim.fencing_token,
            now_ms,
        ),
    ).fetchone()
    if row is not None:
        return row
    if not _lease_owned_tx(connection, claim, now_ms):
        raise _lease_lost(claim)
    raise StoreConflictError(
        "billing_intent_not_found",
        "billing intent does not belong to the claimed job",
    )


def _billing_result(row: sqlite3.Row) -> dict[str, dict[str, Any]]:
    intent = {
        name: row[name]
        for name in _SCHEMA_TABLE_COLUMNS["edit_v3_billing_intents"]
    }
    job = {
        "job_id": row["job_id"],
        "state": row["job_state"],
        "reconciliation_reason": row["job_reconciliation_reason"],
        "resume_state": row["job_resume_state"],
        "confirmed_preheld_total": row["job_confirmed_preheld_total"],
        "confirmed_refunded_total": row["job_confirmed_refunded_total"],
        "queued_at": row["job_queued_at"],
        "processing_deadline_at": row["job_processing_deadline_at"],
        "created_at": row["job_created_at"],
    }
    return {"intent": intent, "job": job}


def _billing_recovery_context(row: Mapping[str, Any]) -> tuple[str, str]:
    context = (row["reason"], row["resume_state"])
    allowed = {
        "pre_debit": {("prehold", "preholding")},
        "refund_delta": {
            ("settlement", "settling"),
            ("refund", "refund_pending"),
        },
        "refund_full": {("refund", "refund_pending")},
    }
    if context not in allowed.get(row["operation"], set()):
        raise StoreConflictError(
            "billing_context_conflict",
            "billing intent has an invalid durable recovery context",
        )
    return context


class V3Store:
    """Only typed, parameterized operations may cross the V3 SQL boundary."""

    def __init__(
        self,
        db_path: Path | None = None,
        *,
        v2_db_path: Path | None = None,
        environment: str = "test",
    ):
        self.environment = self._validate_environment(environment)
        raw_v3_path = _v3_path_syntax(db_path)
        self.v2_db_path = _v2_path_syntax(v2_db_path)
        self.db_path = _initialize_db(raw_v3_path, self.v2_db_path)

    @staticmethod
    def _validate_environment(value: str) -> str:
        if value not in {"test", "production"}:
            raise _configuration_error(
                "v3_environment_invalid",
                "V3 environment must be test or production",
            )
        return value

    def _environment(self, value: str | None) -> str:
        return self._validate_environment(self.environment if value is None else value)

    def _connect(self) -> sqlite3.Connection:
        _path, connection = _open_store_ordered(self.db_path, self.v2_db_path)
        return connection

    def _write(self, operation: Callable[[sqlite3.Connection], _T]) -> _T:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            result = operation(connection)
            connection.commit()
            return result
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    def _read(self, operation: Callable[[sqlite3.Connection], _T]) -> _T:
        connection = self._connect()
        try:
            return operation(connection)
        finally:
            connection.close()

    @staticmethod
    def _same_values(
        row: sqlite3.Row,
        expected: Mapping[str, Any],
    ) -> bool:
        return all(row[key] == value for key, value in expected.items())

    def claim_next_job(
        self,
        worker_id: str,
        lease_seconds: int,
        now_ms: int,
    ) -> LeaseClaim | None:
        try:
            return self._write(
                lambda connection: _claim_next_job_tx(
                    connection, worker_id, lease_seconds, now_ms
                )
            )
        except _ClaimRaceLost:
            return None

    def claim_job(
        self,
        job_id: str,
        worker_id: str,
        lease_seconds: int,
        now_ms: int,
        *,
        expected_states: Any,
    ) -> LeaseClaim | None:
        try:
            return self._write(
                lambda connection: _claim_job_tx(
                    connection,
                    job_id,
                    worker_id,
                    lease_seconds,
                    now_ms,
                    expected_states,
                )
            )
        except _ClaimRaceLost:
            return None

    def renew_lease(
        self,
        claim: LeaseClaim,
        lease_seconds: int,
        now_ms: int,
    ) -> bool:
        return self._write(
            lambda connection: _renew_lease_tx(
                connection, claim, lease_seconds, now_ms
            )
        )

    def lease_owned(self, claim: LeaseClaim, now_ms: int) -> bool:
        return self._read(
            lambda connection: _lease_owned_tx(connection, claim, now_ms)
        )

    def get_job_for_claim(
        self,
        claim: LeaseClaim,
        now_ms: int,
        *,
        environment: str | None = None,
    ) -> dict[str, Any]:
        scoped_environment = self._environment(environment)
        return self._read(
            lambda connection: _get_job_for_claim_tx(
                connection, claim, scoped_environment, now_ms
            )
        )

    def freeze_delivery_object_key(
        self,
        claim: LeaseClaim,
        object_key: str,
        now_ms: int,
    ) -> dict[str, Any]:
        claim = _require_claim(claim)
        object_key = _require_delivery_object_key(object_key, self.environment)
        now_ms = _require_now_ms(now_ms)

        def write(connection: sqlite3.Connection) -> dict[str, Any]:
            row = connection.execute(
                """SELECT * FROM edit_v3_jobs
                   WHERE job_id=? AND worker_id=? AND fencing_token=?
                     AND lease_until>?""",
                (
                    claim.job_id,
                    claim.worker_id,
                    claim.fencing_token,
                    now_ms,
                ),
            ).fetchone()
            if row is None:
                raise _lease_lost(claim)
            if row["state"] not in {
                "staging_delivery",
                "settling",
                "publishing",
                "asset_decision_reconciling",
                "failed_asset_decision_pending",
            }:
                raise StoreConflictError(
                    "delivery_state_conflict",
                    "delivery object key is invalid in the current job state",
                )
            if row["delivery_object_key"] is not None:
                if row["delivery_object_key"] != object_key:
                    raise StoreConflictError(
                        "delivery_object_conflict",
                        "delivery object key is immutable once frozen",
                    )
                return dict(row)
            updated = connection.execute(
                """UPDATE edit_v3_jobs SET delivery_object_key=?,updated_at=?
                   WHERE job_id=? AND worker_id=? AND fencing_token=?
                     AND lease_until>? AND delivery_object_key IS NULL""",
                (
                    object_key,
                    now_ms,
                    claim.job_id,
                    claim.worker_id,
                    claim.fencing_token,
                    now_ms,
                ),
            )
            if updated.rowcount != 1:
                if not _lease_owned_tx(connection, claim, now_ms):
                    raise _lease_lost(claim)
                raise StoreConflictError(
                    "delivery_object_conflict",
                    "delivery object key could not be frozen",
                )
            return dict(
                connection.execute(
                    "SELECT * FROM edit_v3_jobs WHERE job_id=?",
                    (claim.job_id,),
                ).fetchone()
            )

        return self._write(write)

    def list_publication_ready_jobs(
        self,
        now_ms: int,
        *,
        limit: int = 100,
    ) -> tuple[dict[str, Any], ...]:
        now_ms = _require_now_ms(now_ms)
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 100
        ):
            raise _configuration_error(
                "publication_ready_limit_invalid",
                "publication-ready limit must be an integer from 1 to 100",
            )
        return self._read(
            lambda connection: tuple(
                dict(row)
                for row in connection.execute(
                    """SELECT j.job_id,j.state,j.request_sha256,j.updated_at
                       FROM edit_v3_jobs AS j
                       WHERE j.environment=?
                         AND j.state IN ('settling','publishing')
                         AND (j.worker_id IS NULL OR j.lease_until<=?)
                         AND EXISTS(
                             SELECT 1 FROM edit_v3_checkpoints AS c
                             WHERE c.job_id=j.job_id
                               AND c.stage='staging_delivery'
                               AND c.input_sha256=j.request_sha256
                         )
                       ORDER BY j.updated_at,j.job_id LIMIT ?""",
                    (self.environment, now_ms, limit),
                )
            )
        )

    def transition_leased(
        self,
        claim: LeaseClaim,
        expected_states: Any,
        target_state: str,
        now_ms: int,
        *,
        lease_seconds: int,
    ) -> bool:
        return self._write(
            lambda connection: _transition_leased_tx(
                connection,
                claim,
                expected_states,
                target_state,
                now_ms,
                lease_seconds,
            )
        )

    def start_stage_attempt(
        self,
        claim: LeaseClaim,
        stage: str,
        input_sha256: str,
        now_ms: int,
    ) -> dict[str, Any]:
        return self._write(
            lambda connection: _start_stage_attempt_tx(
                connection, claim, stage, input_sha256, now_ms
            )
        )

    def finish_stage_attempt(
        self,
        claim: LeaseClaim,
        stage_attempt_id: str,
        status: str,
        now_ms: int,
        *,
        error_code: str | None = None,
        error: Any = None,
    ) -> dict[str, Any]:
        return self._write(
            lambda connection: _finish_stage_attempt_tx(
                connection,
                claim,
                stage_attempt_id,
                status,
                now_ms,
                error_code,
                error,
            )
        )

    def save_checkpoint(
        self,
        claim: LeaseClaim,
        stage_attempt_id: str,
        input_sha256: str,
        output: Any,
        now_ms: int,
    ) -> dict[str, Any]:
        return self._write(
            lambda connection: _save_checkpoint_tx(
                connection,
                claim,
                stage_attempt_id,
                input_sha256,
                output,
                now_ms,
            )
        )

    def get_checkpoint_for_claim(
        self,
        claim: LeaseClaim,
        stage: str,
        input_sha256: str,
        now_ms: int,
    ) -> dict[str, Any] | None:
        return self._read(
            lambda connection: _get_checkpoint_for_claim_tx(
                connection, claim, stage, input_sha256, now_ms
            )
        )

    def close_running_attempts(
        self,
        claim: LeaseClaim,
        now_ms: int,
    ) -> int:
        return self._write(
            lambda connection: _close_running_attempts_tx(
                connection, claim, now_ms
            )
        )

    def release_lease(self, claim: LeaseClaim, now_ms: int) -> bool:
        return self._write(
            lambda connection: _release_lease_tx(connection, claim, now_ms)
        )

    def record_provider_intent(
        self,
        claim: LeaseClaim,
        stage: str,
        stage_attempt_id: str,
        provider: str,
        capability: str,
        operation_key: str,
        request_sha256: str,
        now_ms: int,
    ) -> dict[str, Any]:
        return self._write(
            lambda connection: _record_provider_intent_tx(
                connection,
                claim,
                stage,
                stage_attempt_id,
                provider,
                capability,
                operation_key,
                request_sha256,
                now_ms,
            )
        )

    def get_provider_task_for_claim(
        self,
        claim: LeaseClaim,
        operation_key: str,
        now_ms: int,
    ) -> dict[str, Any] | None:
        return self._read(
            lambda connection: _get_provider_task_for_claim_tx(
                connection, claim, operation_key, now_ms
            )
        )

    def bind_provider_result(
        self,
        claim: LeaseClaim,
        operation_key: str,
        external_id: str,
        status: str,
        result: Any,
        now_ms: int,
    ) -> dict[str, Any]:
        return self._write(
            lambda connection: _bind_provider_result_tx(
                connection,
                claim,
                operation_key,
                external_id,
                status,
                result,
                now_ms,
            )
        )

    def insert_pricing_version(
        self,
        version: str,
        parameters: Mapping[str, Any],
        *,
        status: str,
        created_at: int,
        published_at: int | None = None,
        retired_at: int | None = None,
    ) -> dict[str, Any]:
        _require_integer("created_at", created_at)
        _require_integer("published_at", published_at, nullable=True)
        _require_integer("retired_at", retired_at, nullable=True)
        parameters_json = _json_text(parameters)
        expected = {
            "version": version,
            "status": status,
            "parameters_json": parameters_json,
            "parameters_sha256": _json_sha256(parameters),
            "created_at": created_at,
            "published_at": published_at,
            "retired_at": retired_at,
        }

        def write(connection: sqlite3.Connection) -> dict[str, Any]:
            existing = connection.execute(
                "SELECT * FROM edit_v3_pricing_versions WHERE version=?",
                (version,),
            ).fetchone()
            if existing is not None:
                if not self._same_values(existing, expected):
                    raise _immutable_conflict(f"pricing:{version}")
                return dict(existing)
            try:
                connection.execute(
                    """INSERT INTO edit_v3_pricing_versions(
                           version,status,parameters_json,parameters_sha256,created_at,
                           published_at,retired_at
                       ) VALUES(?,?,?,?,?,?,?)""",
                    tuple(expected.values()),
                )
            except sqlite3.IntegrityError as exc:
                code = (
                    "published_pricing_conflict"
                    if status == "published"
                    else "pricing_version_invalid"
                )
                raise StoreConflictError(code, "pricing version violates frozen constraints") from exc
            return dict(
                connection.execute(
                    "SELECT * FROM edit_v3_pricing_versions WHERE version=?",
                    (version,),
                ).fetchone()
            )

        return self._write(write)

    def get_published_pricing_version(self) -> dict[str, Any] | None:
        return self._read(
            lambda connection: _row_dict(
                connection.execute(
                    """SELECT * FROM edit_v3_pricing_versions
                       WHERE status=? ORDER BY published_at DESC,version DESC LIMIT 1""",
                    ("published",),
                ).fetchone()
            )
        )

    def list_published_pricing_versions(self) -> list[dict[str, Any]]:
        return self._read(
            lambda connection: [
                dict(row)
                for row in connection.execute(
                    """SELECT * FROM edit_v3_pricing_versions
                       WHERE status=? ORDER BY published_at DESC,version DESC""",
                    ("published",),
                )
            ]
        )

    def list_template_versions(self, template_id: str) -> list[dict[str, Any]]:
        return self._read(
            lambda connection: [
                dict(row)
                for row in connection.execute(
                    """SELECT * FROM edit_v3_template_versions
                       WHERE template_id=? ORDER BY published_at DESC,version DESC""",
                    (template_id,),
                )
            ]
        )

    def insert_quote(
        self,
        owner_id: str,
        quote_id: str,
        normalized_request: Mapping[str, Any],
        *,
        pricing_version: str,
        min_points: int,
        max_points: int,
        breakdown: Mapping[str, Any],
        expires_at: int,
        created_at: int,
        template_id: str | None = None,
        template_version: str | None = None,
        environment: str | None = None,
    ) -> dict[str, Any] | None:
        _require_integer("min_points", min_points)
        _require_integer("max_points", max_points)
        _require_integer("expires_at", expires_at)
        _require_integer("created_at", created_at)
        environment = self._environment(environment)
        expected = {
            "quote_id": quote_id,
            "environment": environment,
            "owner_id": owner_id,
            "normalized_request_json": _json_text(normalized_request),
            "request_sha256": request_fingerprint(normalized_request),
            "pricing_version": pricing_version,
            "template_id": template_id,
            "template_version": template_version,
            "min_points": min_points,
            "max_points": max_points,
            "breakdown_json": _json_text(breakdown),
            "expires_at": expires_at,
            "created_at": created_at,
        }

        def write(connection: sqlite3.Connection) -> dict[str, Any] | None:
            existing = connection.execute(
                """SELECT * FROM edit_v3_quotes
                   WHERE environment=? AND owner_id=? AND quote_id=?""",
                (environment, owner_id, quote_id),
            ).fetchone()
            if existing is not None:
                if not self._same_values(existing, expected):
                    raise _immutable_conflict(f"quote:{quote_id}")
                return dict(existing)
            try:
                connection.execute(
                    """INSERT INTO edit_v3_quotes(
                           quote_id,environment,owner_id,normalized_request_json,request_sha256,
                           pricing_version,template_id,template_version,min_points,max_points,
                           breakdown_json,expires_at,created_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    tuple(expected.values()),
                )
            except sqlite3.IntegrityError as exc:
                if getattr(exc, "sqlite_errorcode", None) in {
                    getattr(sqlite3, "SQLITE_CONSTRAINT_PRIMARYKEY", -1),
                    getattr(sqlite3, "SQLITE_CONSTRAINT_UNIQUE", -1),
                }:
                    replay = connection.execute(
                        """SELECT * FROM edit_v3_quotes
                           WHERE environment=? AND owner_id=? AND quote_id=?""",
                        (environment, owner_id, quote_id),
                    ).fetchone()
                    if replay is not None:
                        if not self._same_values(replay, expected):
                            raise _immutable_conflict(f"quote:{quote_id}") from exc
                        return dict(replay)
                    return None
                raise StoreConflictError(
                    "quote_invalid",
                    "quote violates frozen pricing, template, or value constraints",
                ) from exc
            return dict(
                connection.execute(
                    """SELECT * FROM edit_v3_quotes
                       WHERE environment=? AND owner_id=? AND quote_id=?""",
                    (environment, owner_id, quote_id),
                ).fetchone()
            )

        return self._write(write)

    def get_quote(
        self,
        owner_id: str,
        quote_id: str,
        *,
        environment: str | None = None,
    ) -> dict[str, Any] | None:
        environment = self._environment(environment)
        return self._read(
            lambda connection: _row_dict(
                connection.execute(
                    """SELECT * FROM edit_v3_quotes
                       WHERE environment=? AND owner_id=? AND quote_id=?""",
                    (environment, owner_id, quote_id),
                ).fetchone()
            )
        )

    def create_job_with_predebit(
        self,
        owner_id: str,
        job_id: str,
        quote_id: str,
        idempotency_key: str,
        normalized_request: Mapping[str, Any],
        *,
        now_ms: int,
        intent_id: str,
        predecessor_job_id: str | None = None,
        material_bindings: Sequence[Mapping[str, Any]] = (),
        fail_after_job: Exception | None = None,
        environment: str | None = None,
    ) -> dict[str, dict[str, Any]]:
        _require_integer("now_ms", now_ms)
        environment = self._environment(environment)
        normalized_json = _json_text(normalized_request)
        request_sha256 = request_fingerprint(normalized_request)
        if predecessor_job_id is not None:
            _require_nonblank("predecessor_job_id", predecessor_job_id)
            if predecessor_job_id == job_id:
                raise _configuration_error(
                    "predecessor_invalid", "a job cannot be its own predecessor"
                )
        normalized_bindings: list[tuple[str, str, int]] = []
        if (
            isinstance(material_bindings, (str, bytes))
            or not isinstance(material_bindings, Sequence)
            or len(material_bindings) > 10
        ):
            raise _configuration_error(
                "job_material_binding_invalid", "material bindings are invalid"
            )
        for binding in material_bindings:
            if not isinstance(binding, Mapping) or set(binding) != {
                "material_id",
                "purpose",
                "ordinal",
            }:
                raise _configuration_error(
                    "job_material_binding_invalid", "material binding fields are invalid"
                )
            material_id = binding["material_id"]
            purpose = binding["purpose"]
            ordinal = binding["ordinal"]
            _require_integer("ordinal", ordinal)
            if (
                not isinstance(material_id, str)
                or not material_id
                or not isinstance(purpose, str)
                or not purpose
                or ordinal < 0
            ):
                raise _configuration_error(
                    "job_material_binding_invalid", "material binding values are invalid"
                )
            normalized_bindings.append((material_id, purpose, ordinal))
        if len({item[0] for item in normalized_bindings}) != len(
            normalized_bindings
        ) or len({(item[1], item[2]) for item in normalized_bindings}) != len(
            normalized_bindings
        ):
            raise _configuration_error(
                "job_material_binding_invalid", "material bindings contain duplicates"
            )

        def conflict(code: str, message: str) -> StoreConflictError:
            return StoreConflictError(code, message)

        def write(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
            existing_job = connection.execute(
                """SELECT * FROM edit_v3_jobs
                   WHERE environment=? AND owner_id=? AND idempotency_key=?""",
                (environment, owner_id, idempotency_key),
            ).fetchone()
            if existing_job is not None:
                if (
                    existing_job["request_sha256"] != request_sha256
                    or existing_job["normalized_request_json"] != normalized_json
                    or existing_job["quote_id"] != quote_id
                    or existing_job["predecessor_job_id"] != predecessor_job_id
                ):
                    raise conflict(
                        "idempotency_conflict",
                        "idempotency key was reused with a divergent request or quote",
                    )
                existing_intent = connection.execute(
                    """SELECT * FROM edit_v3_billing_intents
                       WHERE environment=? AND owner_id=? AND job_id=?
                         AND operation='pre_debit'""",
                    (environment, owner_id, existing_job["job_id"]),
                ).fetchone()
                if existing_intent is None:
                    raise conflict(
                        "billing_intent_missing",
                        "replayed job is missing its atomic pre-debit intent",
                    )
                expected_key = f"ai-edit-v3:{existing_job['job_id']}:pre_debit"
                replay_quote = connection.execute(
                    """SELECT max_points FROM edit_v3_quotes
                       WHERE environment=? AND owner_id=? AND quote_id=?""",
                    (environment, owner_id, quote_id),
                ).fetchone()
                if (
                    replay_quote is None
                    or existing_intent["external_idempotency_key"] != expected_key
                    or existing_intent["request_sha256"] != request_sha256
                    or existing_intent["refund_target_total"] != 0
                    or existing_intent["request_amount"] != replay_quote["max_points"]
                ):
                    raise conflict(
                        "billing_intent_conflict",
                        "replayed pre-debit intent violates immutable fields",
                    )
                stored_bindings = [
                    (row["material_id"], row["purpose"], row["ordinal"])
                    for row in connection.execute(
                        """SELECT material_id,purpose,ordinal
                           FROM edit_v3_job_materials WHERE job_id=?
                           ORDER BY ordinal,material_id""",
                        (existing_job["job_id"],),
                    )
                ]
                if stored_bindings != sorted(
                    normalized_bindings, key=lambda item: (item[2], item[0])
                ):
                    raise conflict(
                        "job_material_binding_conflict",
                        "replayed job has divergent material bindings",
                    )
                return {"job": dict(existing_job), "intent": dict(existing_intent)}

            quote_row = connection.execute(
                """SELECT * FROM edit_v3_quotes
                   WHERE environment=? AND owner_id=? AND quote_id=?""",
                (environment, owner_id, quote_id),
            ).fetchone()
            if quote_row is None:
                raise conflict("quote_not_found", "quote does not belong to this owner")
            if now_ms >= quote_row["expires_at"]:
                raise conflict("quote_expired", "quote has expired")
            if (
                quote_row["request_sha256"] != request_sha256
                or quote_row["normalized_request_json"] != normalized_json
            ):
                raise conflict("quote_request_mismatch", "request does not match frozen quote")
            pricing_row = connection.execute(
                "SELECT 1 FROM edit_v3_pricing_versions WHERE version=?",
                (quote_row["pricing_version"],),
            ).fetchone()
            if pricing_row is None:
                raise conflict("quote_pricing_missing", "frozen pricing version is absent")
            if quote_row["template_id"] is not None:
                template_row = connection.execute(
                    """SELECT status FROM edit_v3_template_versions
                       WHERE template_id=? AND version=?""",
                    (quote_row["template_id"], quote_row["template_version"]),
                ).fetchone()
                if template_row is None or template_row["status"] != "published":
                    raise conflict(
                        "quote_template_unpublished",
                        "frozen template version is absent or unpublished",
                    )

            if predecessor_job_id is not None:
                predecessor = connection.execute(
                    """SELECT state FROM edit_v3_jobs
                       WHERE environment=? AND owner_id=? AND job_id=?""",
                    (environment, owner_id, predecessor_job_id),
                ).fetchone()
                if predecessor is None:
                    raise conflict(
                        "retry_predecessor_not_found",
                        "retry predecessor does not belong to this owner",
                    )
                if predecessor["state"] not in {"refunded", "prehold_absent"}:
                    raise conflict(
                        "retry_not_allowed",
                        "retry predecessor is not a terminal failed outcome",
                    )
            for material_id, _purpose, _ordinal in normalized_bindings:
                material = connection.execute(
                    """SELECT 1 FROM edit_v3_materials
                       WHERE environment=? AND owner_id=? AND material_id=?""",
                    (environment, owner_id, material_id),
                ).fetchone()
                if material is None:
                    raise conflict(
                        "job_material_not_found",
                        "job material does not belong to this owner",
                    )

            external_key = f"ai-edit-v3:{job_id}:pre_debit"
            try:
                connection.execute(
                    """INSERT INTO edit_v3_jobs(
                           job_id,environment,owner_id,state,normalized_request_json,
                           request_sha256,quote_id,predecessor_job_id,idempotency_key,
                           created_at,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        job_id,
                        environment,
                        owner_id,
                        "created_draft",
                        normalized_json,
                        request_sha256,
                        quote_id,
                        predecessor_job_id,
                        idempotency_key,
                        now_ms,
                        now_ms,
                    ),
                )
                for material_id, purpose, ordinal in normalized_bindings:
                    connection.execute(
                        """INSERT INTO edit_v3_job_materials(
                               job_id,material_id,purpose,ordinal,created_at
                           ) VALUES(?,?,?,?,?)""",
                        (job_id, material_id, purpose, ordinal, now_ms),
                    )
                if fail_after_job is not None:
                    raise fail_after_job
                connection.execute(
                    """INSERT INTO edit_v3_billing_intents(
                           id,environment,owner_id,job_id,operation,
                           external_idempotency_key,request_sha256,
                           refund_target_total,request_amount,status,created_at,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        intent_id,
                        environment,
                        owner_id,
                        job_id,
                        "pre_debit",
                        external_key,
                        request_sha256,
                        0,
                        quote_row["max_points"],
                        "pending",
                        now_ms,
                        now_ms,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise conflict(
                    "billing_create_invalid",
                    "job and pre-debit intent violate frozen constraints",
                ) from exc
            return {
                "job": dict(
                    connection.execute(
                        "SELECT * FROM edit_v3_jobs WHERE job_id=?", (job_id,)
                    ).fetchone()
                ),
                "intent": dict(
                    connection.execute(
                        "SELECT * FROM edit_v3_billing_intents WHERE id=?", (intent_id,)
                    ).fetchone()
                ),
            }

        return self._write(write)

    def get_billing_for_claim(
        self,
        intent_id: str,
        claim: LeaseClaim,
        now_ms: int,
    ) -> dict[str, dict[str, Any]]:
        return self._read(
            lambda connection: _billing_result(
                _billing_row_for_claim_tx(connection, intent_id, claim, now_ms)
            )
        )

    def begin_billing_transmission(
        self,
        intent_id: str,
        claim: LeaseClaim,
        now_ms: int,
    ) -> dict[str, dict[str, Any]]:
        def write(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
            row = _billing_row_for_claim_tx(connection, intent_id, claim, now_ms)
            if row["status"] not in {"pending", "retryable_absent"}:
                return _billing_result(row)
            if row["operation"] == "pre_debit":
                reason = "prehold"
                resume_state = "preholding"
                created_at = row["job_created_at"]
                admission_expired = (
                    now_ms >= created_at
                    and now_ms - created_at >= _PREHOLD_ADMISSION_TIMEOUT_MS
                )
                if admission_expired:
                    if row["job_state"] == "created_draft" and not _transition_leased_tx(
                        connection,
                        claim,
                        {"created_draft"},
                        "preholding",
                        now_ms,
                        1,
                        preserve_current_lease=True,
                    ):
                        raise _lease_lost(claim)
                    if row["job_state"] not in {"created_draft", "preholding"}:
                        raise StoreConflictError(
                            "billing_state_conflict",
                            "expired pre-debit is outside its admission states",
                        )
                    if not _transition_leased_tx(
                        connection,
                        claim,
                        {"preholding"},
                        "billing_reconciling",
                        now_ms,
                        1,
                        preserve_current_lease=True,
                    ):
                        raise _lease_lost(claim)
                    context_update = connection.execute(
                        """UPDATE edit_v3_jobs
                           SET reconciliation_reason='prehold',
                               resume_state='preholding',updated_at=?
                           WHERE job_id=? AND worker_id=? AND fencing_token=?
                             AND lease_until>? AND state='billing_reconciling'
                             AND created_at=?""",
                        (
                            now_ms,
                            claim.job_id,
                            claim.worker_id,
                            claim.fencing_token,
                            now_ms,
                            created_at,
                        ),
                    )
                    if context_update.rowcount != 1:
                        if not _lease_owned_tx(connection, claim, now_ms):
                            raise _lease_lost(claim)
                        raise StoreConflictError(
                            "billing_state_conflict",
                            "expired pre-debit context could not be frozen",
                        )
                    admission_deadline_at = (
                        created_at + _PREHOLD_ADMISSION_TIMEOUT_MS
                    )
                    evidence_json = _json_text(
                        {
                            "admission_deadline_at": admission_deadline_at,
                            "observed_at": now_ms,
                            "transmission": "not_started",
                        }
                    )
                    intent_update = connection.execute(
                        """UPDATE edit_v3_billing_intents
                           SET status='reconciliation_pending',first_unknown_at=?,
                               last_checked_at=?,authority_evidence_json=?,
                               reason='prehold',resume_state='preholding',updated_at=?
                           WHERE id=? AND operation='pre_debit'
                             AND status IN ('pending','retryable_absent')
                             AND EXISTS(
                                 SELECT 1 FROM edit_v3_jobs AS j
                                 WHERE j.job_id=edit_v3_billing_intents.job_id
                                   AND j.job_id=? AND j.worker_id=?
                                   AND j.fencing_token=? AND j.lease_until>?
                                   AND j.state='billing_reconciling'
                                   AND j.created_at=?
                             )""",
                        (
                            created_at,
                            now_ms,
                            evidence_json,
                            now_ms,
                            intent_id,
                            claim.job_id,
                            claim.worker_id,
                            claim.fencing_token,
                            now_ms,
                            created_at,
                        ),
                    )
                    if intent_update.rowcount != 1:
                        if not _lease_owned_tx(connection, claim, now_ms):
                            raise _lease_lost(claim)
                        raise StoreConflictError(
                            "billing_intent_conflict",
                            "expired pre-debit admission could not be frozen",
                        )
                    if not _transition_leased_tx(
                        connection,
                        claim,
                        {"billing_reconciling"},
                        "failed_reconciliation_pending",
                        now_ms,
                        1,
                        preserve_current_lease=True,
                    ):
                        raise _lease_lost(claim)
                    return {
                        "intent": dict(
                            connection.execute(
                                "SELECT * FROM edit_v3_billing_intents WHERE id=?",
                                (intent_id,),
                            ).fetchone()
                        ),
                        "job": {
                            "job_id": claim.job_id,
                            "state": "failed_reconciliation_pending",
                            "reconciliation_reason": "prehold",
                            "resume_state": "preholding",
                            "confirmed_preheld_total": row[
                                "job_confirmed_preheld_total"
                            ],
                            "confirmed_refunded_total": row[
                                "job_confirmed_refunded_total"
                            ],
                            "queued_at": row["job_queued_at"],
                            "processing_deadline_at": row[
                                "job_processing_deadline_at"
                            ],
                            "created_at": created_at,
                        },
                    }
                if row["job_state"] == "created_draft":
                    if not _transition_leased_tx(
                        connection,
                        claim,
                        {"created_draft"},
                        "preholding",
                        now_ms,
                        1,
                    ):
                        raise _lease_lost(claim)
                elif row["job_state"] != "preholding":
                    raise StoreConflictError(
                        "billing_state_conflict",
                        "pre-debit can transmit only while preholding",
                    )
            else:
                reason, resume_state = _billing_recovery_context(row)
                if row["job_state"] != resume_state:
                    raise StoreConflictError(
                        "billing_state_conflict",
                        "refund can transmit only from its frozen resume state",
                    )
            updated = connection.execute(
                """UPDATE edit_v3_billing_intents
                   SET status='unknown',first_unknown_at=?,last_checked_at=NULL,
                       authority_evidence_json=NULL,reason=?,resume_state=?,updated_at=?
                   WHERE id=? AND status IN ('pending','retryable_absent')
                     AND EXISTS(
                         SELECT 1 FROM edit_v3_jobs AS j
                         WHERE j.job_id=edit_v3_billing_intents.job_id
                           AND j.job_id=? AND j.worker_id=?
                           AND j.fencing_token=? AND j.lease_until>?
                     )""",
                (
                    now_ms,
                    reason,
                    resume_state,
                    now_ms,
                    intent_id,
                    claim.job_id,
                    claim.worker_id,
                    claim.fencing_token,
                    now_ms,
                ),
            )
            if updated.rowcount != 1:
                if not _lease_owned_tx(connection, claim, now_ms):
                    raise _lease_lost(claim)
                raise StoreConflictError(
                    "billing_intent_conflict",
                    "billing intent could not enter its transmission window",
                )
            return _billing_result(
                _billing_row_for_claim_tx(connection, intent_id, claim, now_ms)
            )

        return self._write(write)

    def create_refund_intent(
        self,
        claim: LeaseClaim,
        operation: str,
        refund_target_total: int,
        *,
        intent_id: str,
        now_ms: int,
    ) -> dict[str, dict[str, Any]]:
        claim = _require_claim(claim)
        if operation not in {"refund_delta", "refund_full"}:
            raise _configuration_error(
                "billing_operation_invalid", "refund operation is invalid"
            )
        _require_integer("refund_target_total", refund_target_total)
        _require_integer("now_ms", now_ms)

        def write(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
            job = connection.execute(
                """SELECT * FROM edit_v3_jobs
                   WHERE job_id=? AND worker_id=? AND fencing_token=?
                     AND lease_until>?""",
                (
                    claim.job_id,
                    claim.worker_id,
                    claim.fencing_token,
                    now_ms,
                ),
            ).fetchone()
            if job is None:
                raise _lease_lost(claim)
            expected_state = "settling" if operation == "refund_delta" else "refund_pending"
            existing = connection.execute(
                """SELECT * FROM edit_v3_billing_intents
                   WHERE environment=? AND owner_id=? AND job_id=? AND operation=?""",
                (job["environment"], job["owner_id"], claim.job_id, operation),
            ).fetchone()
            if existing is not None:
                if existing["refund_target_total"] != refund_target_total:
                    raise StoreConflictError(
                        "billing_intent_conflict",
                        "refund operation was reused with a divergent cumulative target",
                    )
                return {
                    "intent": dict(existing),
                    "job": {
                        "job_id": job["job_id"],
                        "state": job["state"],
                        "reconciliation_reason": job["reconciliation_reason"],
                        "resume_state": job["resume_state"],
                        "confirmed_preheld_total": job["confirmed_preheld_total"],
                        "confirmed_refunded_total": job["confirmed_refunded_total"],
                        "queued_at": job["queued_at"],
                        "processing_deadline_at": job["processing_deadline_at"],
                    },
                }
            unresolved = connection.execute(
                """SELECT 1 FROM edit_v3_billing_intents
                   WHERE job_id=? AND operation IN ('refund_delta','refund_full')
                     AND status IN ('pending','retryable_absent','unknown','reconciliation_pending')
                   LIMIT 1""",
                (claim.job_id,),
            ).fetchone()
            if unresolved is not None:
                raise StoreConflictError(
                    "overlapping_refund_intent",
                    "another cumulative refund remains unresolved",
                )
            if job["state"] != expected_state:
                raise StoreConflictError(
                    "billing_state_conflict",
                    "refund intent is invalid in the current job state",
                )
            preheld = job["confirmed_preheld_total"]
            refunded = job["confirmed_refunded_total"]
            if not refunded <= refund_target_total <= preheld:
                raise StoreConflictError(
                    "refund_target_invalid",
                    "refund target must stay within confirmed cumulative totals",
                )
            request_amount = refund_target_total - refunded
            external_key = f"ai-edit-v3:{claim.job_id}:{operation}"
            status = "completed" if request_amount == 0 else "pending"
            completed_at = now_ms if request_amount == 0 else None
            evidence_json = _json_text({"zero_amount": True}) if request_amount == 0 else None
            reason = "settlement" if operation == "refund_delta" else "refund"
            resume_state = expected_state
            inserted = connection.execute(
                """INSERT INTO edit_v3_billing_intents(
                       id,environment,owner_id,job_id,operation,
                       external_idempotency_key,request_sha256,
                       refund_target_total,request_amount,status,
                       authority_evidence_json,reason,resume_state,
                       created_at,updated_at,completed_at
                   )
                   SELECT ?,j.environment,j.owner_id,j.job_id,?,?,?,?,?,?,?,?,?,?,?,?
                   FROM edit_v3_jobs AS j
                   WHERE j.job_id=? AND j.worker_id=? AND j.fencing_token=?
                     AND j.lease_until>? AND j.state=?
                     AND j.confirmed_refunded_total<=?
                     AND ?<=j.confirmed_preheld_total""",
                (
                    intent_id,
                    operation,
                    external_key,
                    job["request_sha256"],
                    refund_target_total,
                    request_amount,
                    status,
                    evidence_json,
                    reason,
                    resume_state,
                    now_ms,
                    now_ms,
                    completed_at,
                    claim.job_id,
                    claim.worker_id,
                    claim.fencing_token,
                    now_ms,
                    expected_state,
                    refund_target_total,
                    refund_target_total,
                ),
            )
            if inserted.rowcount != 1:
                if not _lease_owned_tx(connection, claim, now_ms):
                    raise _lease_lost(claim)
                raise StoreConflictError(
                    "refund_target_invalid",
                    "refund target changed before intent creation",
                )
            intent = dict(
                connection.execute(
                    "SELECT * FROM edit_v3_billing_intents WHERE id=?", (intent_id,)
                ).fetchone()
            )
            return {
                "intent": intent,
                "job": {
                    "job_id": job["job_id"],
                    "state": job["state"],
                    "reconciliation_reason": job["reconciliation_reason"],
                    "resume_state": job["resume_state"],
                    "confirmed_preheld_total": preheld,
                    "confirmed_refunded_total": refunded,
                    "queued_at": job["queued_at"],
                    "processing_deadline_at": job["processing_deadline_at"],
                },
            }

        try:
            return self._write(write)
        except sqlite3.IntegrityError as exc:
            raise StoreConflictError(
                "billing_intent_conflict",
                "refund intent violates frozen uniqueness or cumulative constraints",
            ) from exc

    def freeze_failed_full_refund(
        self,
        claim: LeaseClaim,
        *,
        now_ms: int,
    ) -> dict[str, dict[str, Any]]:
        """Atomically freeze the full-refund outbox and leave failed state."""

        claim = _require_claim(claim)
        now_ms = _require_now_ms(now_ms)

        def write(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
            job = connection.execute(
                """SELECT * FROM edit_v3_jobs
                   WHERE job_id=? AND worker_id=? AND fencing_token=?
                     AND lease_until>?""",
                (
                    claim.job_id,
                    claim.worker_id,
                    claim.fencing_token,
                    now_ms,
                ),
            ).fetchone()
            if job is None:
                raise _lease_lost(claim)
            if job["state"] not in {"failed", "refund_pending"}:
                raise StoreConflictError(
                    "billing_state_conflict",
                    "full refund freeze requires failed or refund-pending state",
                )

            target = job["confirmed_preheld_total"]
            refunded = job["confirmed_refunded_total"]
            existing = connection.execute(
                """SELECT * FROM edit_v3_billing_intents
                   WHERE environment=? AND owner_id=? AND job_id=?
                     AND operation='refund_full'""",
                (job["environment"], job["owner_id"], claim.job_id),
            ).fetchone()
            if existing is None:
                unresolved = connection.execute(
                    """SELECT 1 FROM edit_v3_billing_intents
                       WHERE job_id=? AND operation='refund_delta'
                         AND status IN ('pending','retryable_absent','unknown',
                                        'reconciliation_pending')
                       LIMIT 1""",
                    (claim.job_id,),
                ).fetchone()
                if unresolved is not None:
                    raise StoreConflictError(
                        "overlapping_refund_intent",
                        "delta refund must converge before full refund is frozen",
                    )
                if not 0 <= refunded <= target:
                    raise StoreConflictError(
                        "refund_target_invalid",
                        "full refund target conflicts with cumulative totals",
                    )
                request_amount = target - refunded
                status = "completed" if request_amount == 0 else "pending"
                completed_at = now_ms if request_amount == 0 else None
                evidence_json = (
                    _json_text({"zero_amount": True})
                    if request_amount == 0
                    else None
                )
                intent_id = hashlib.sha256(
                    f"{claim.job_id}\0refund_full".encode("utf-8")
                ).hexdigest()
                connection.execute(
                    """INSERT INTO edit_v3_billing_intents(
                           id,environment,owner_id,job_id,operation,
                           external_idempotency_key,request_sha256,
                           refund_target_total,request_amount,status,
                           authority_evidence_json,reason,resume_state,
                           created_at,updated_at,completed_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        intent_id,
                        job["environment"],
                        job["owner_id"],
                        claim.job_id,
                        "refund_full",
                        f"ai-edit-v3:{claim.job_id}:refund_full",
                        job["request_sha256"],
                        target,
                        request_amount,
                        status,
                        evidence_json,
                        "refund",
                        "refund_pending",
                        now_ms,
                        now_ms,
                        completed_at,
                    ),
                )
                existing = connection.execute(
                    "SELECT * FROM edit_v3_billing_intents WHERE id=?",
                    (intent_id,),
                ).fetchone()
            else:
                expected = {
                    "environment": job["environment"],
                    "owner_id": job["owner_id"],
                    "job_id": claim.job_id,
                    "operation": "refund_full",
                    "external_idempotency_key": (
                        f"ai-edit-v3:{claim.job_id}:refund_full"
                    ),
                    "request_sha256": job["request_sha256"],
                    "refund_target_total": target,
                    "reason": "refund",
                    "resume_state": "refund_pending",
                }
                original_refunded = target - existing["request_amount"]
                if (
                    not self._same_values(existing, expected)
                    or isinstance(existing["request_amount"], bool)
                    or not isinstance(existing["request_amount"], int)
                    or not 0 <= original_refunded <= refunded <= target
                ):
                    raise StoreConflictError(
                        "billing_intent_conflict",
                        "existing full-refund intent diverges from failed recovery",
                    )

            if job["state"] == "failed" and not _transition_leased_tx(
                connection,
                claim,
                {"failed"},
                "refund_pending",
                now_ms,
                1,
                preserve_current_lease=True,
            ):
                if not _lease_owned_tx(connection, claim, now_ms):
                    raise _lease_lost(claim)
                raise StoreConflictError(
                    "billing_state_conflict",
                    "failed job could not enter refund-pending state",
                )

            terminal_refund = (
                existing["status"] == "completed"
                and refunded == target
            )
            if terminal_refund and not _transition_leased_tx(
                connection,
                claim,
                {"refund_pending"},
                "refunded",
                now_ms,
                1,
                preserve_current_lease=True,
            ):
                if not _lease_owned_tx(connection, claim, now_ms):
                    raise _lease_lost(claim)
                raise StoreConflictError(
                    "billing_state_conflict",
                    "zero or confirmed full refund could not become terminal",
                )

            current_job = connection.execute(
                "SELECT * FROM edit_v3_jobs WHERE job_id=?",
                (claim.job_id,),
            ).fetchone()
            return {"job": dict(current_job), "intent": dict(existing)}

        try:
            return self._write(write)
        except sqlite3.IntegrityError as exc:
            raise StoreConflictError(
                "billing_intent_conflict",
                "failed full-refund freeze violates frozen constraints",
            ) from exc

    def get_job_billing_for_claim(
        self,
        claim: LeaseClaim,
        now_ms: int,
    ) -> dict[str, Any]:
        claim = _require_claim(claim)
        now_ms = _require_now_ms(now_ms)

        def read(connection: sqlite3.Connection) -> dict[str, Any]:
            row = connection.execute(
                """SELECT job_id,state,confirmed_preheld_total,
                          confirmed_refunded_total,request_sha256
                   FROM edit_v3_jobs
                   WHERE job_id=? AND worker_id=? AND fencing_token=?
                     AND lease_until>?""",
                (
                    claim.job_id,
                    claim.worker_id,
                    claim.fencing_token,
                    now_ms,
                ),
            ).fetchone()
            if row is None:
                raise _lease_lost(claim)
            return dict(row)

        return self._read(read)

    def mark_billing_reconciling(
        self,
        intent_id: str,
        claim: LeaseClaim,
        now_ms: int,
    ) -> dict[str, dict[str, Any]]:
        def write(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
            row = _billing_row_for_claim_tx(connection, intent_id, claim, now_ms)
            reason, resume_state = _billing_recovery_context(row)
            if row["job_state"] == "failed_reconciliation_pending":
                if (
                    row["job_reconciliation_reason"] != reason
                    or row["job_resume_state"] != resume_state
                ):
                    raise StoreConflictError(
                        "billing_context_conflict",
                        "failed billing context conflicts with its intent",
                    )
                return _billing_result(row)
            if row["job_state"] != "billing_reconciling":
                if row["job_state"] != resume_state:
                    raise StoreConflictError(
                        "billing_state_conflict",
                        "job is outside the intent reconciliation source state",
                    )
                if not _transition_leased_tx(
                    connection,
                    claim,
                    {resume_state},
                    "billing_reconciling",
                    now_ms,
                    1,
                ):
                    raise _lease_lost(claim)
            elif (
                row["job_reconciliation_reason"] not in {None, reason}
                or row["job_resume_state"] not in {None, resume_state}
            ):
                raise StoreConflictError(
                    "billing_context_conflict",
                    "job reconciliation context conflicts with its intent",
                )
            updated = connection.execute(
                """UPDATE edit_v3_jobs
                   SET reconciliation_reason=?,resume_state=?,updated_at=?
                   WHERE job_id=? AND worker_id=? AND fencing_token=?
                     AND lease_until>? AND state='billing_reconciling'""",
                (
                    reason,
                    resume_state,
                    now_ms,
                    claim.job_id,
                    claim.worker_id,
                    claim.fencing_token,
                    now_ms,
                ),
            )
            if updated.rowcount != 1:
                raise _lease_lost(claim)
            return _billing_result(
                _billing_row_for_claim_tx(connection, intent_id, claim, now_ms)
            )

        return self._write(write)

    def confirm_predebit(
        self,
        intent_id: str,
        claim: LeaseClaim,
        *,
        authority_created_at: int,
        processing_deadline_at: int | None,
        authority_evidence: Mapping[str, Any],
        now_ms: int,
    ) -> dict[str, dict[str, Any]]:
        _require_integer("authority_created_at", authority_created_at)
        _require_integer(
            "processing_deadline_at", processing_deadline_at, nullable=True
        )
        evidence_json = _json_text(authority_evidence)

        def write(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
            row = _billing_row_for_claim_tx(connection, intent_id, claim, now_ms)
            if row["operation"] != "pre_debit":
                raise StoreConflictError(
                    "billing_operation_conflict",
                    "pre-debit confirmation requires a pre-debit intent",
                )
            amount = row["request_amount"]
            if row["status"] == "completed":
                if row["authority_evidence_json"] != evidence_json:
                    raise StoreConflictError(
                        "billing_authority_conflict",
                        "completed pre-debit authority is immutable",
                    )
                return _billing_result(row)
            source_state = row["job_state"]
            if source_state not in {
                "preholding",
                "billing_reconciling",
                "failed_reconciliation_pending",
            }:
                raise StoreConflictError(
                    "billing_state_conflict",
                    "pre-debit confirmation is invalid in the current job state",
                )
            job_created_at = row["job_created_at"]
            if authority_created_at < job_created_at:
                raise StoreConflictError(
                    "billing_authority_conflict",
                    "pre-debit authority cannot predate its job",
                )
            late_authority = (
                authority_created_at - job_created_at
                >= _PREHOLD_ADMISSION_TIMEOUT_MS
            )
            refund_route = (
                late_authority
                or source_state == "failed_reconciliation_pending"
            )
            if refund_route:
                if processing_deadline_at is not None:
                    raise StoreConflictError(
                        "processing_deadline_conflict",
                        "refund-routed pre-debit authority cannot start media time",
                    )
                if source_state == "preholding":
                    if not _transition_leased_tx(
                        connection,
                        claim,
                        {"preholding"},
                        "billing_reconciling",
                        now_ms,
                        1,
                        preserve_current_lease=True,
                    ):
                        raise _lease_lost(claim)
                    source_state = "billing_reconciling"
                if not _transition_leased_tx(
                    connection,
                    claim,
                    {source_state},
                    "refund_pending",
                    now_ms,
                    1,
                    preserve_current_lease=True,
                ):
                    raise _lease_lost(claim)
                job_update = connection.execute(
                    """UPDATE edit_v3_jobs
                       SET confirmed_preheld_total=?,reconciliation_reason=NULL,
                           resume_state=NULL,updated_at=?
                       WHERE job_id=? AND worker_id=? AND fencing_token=?
                         AND lease_until>? AND state='refund_pending'
                         AND confirmed_preheld_total IN (0,?)
                         AND queued_at IS NULL AND processing_deadline_at IS NULL""",
                    (
                        amount,
                        now_ms,
                        claim.job_id,
                        claim.worker_id,
                        claim.fencing_token,
                        now_ms,
                        amount,
                    ),
                )
            else:
                if (
                    authority_created_at
                    > _SQLITE_INT64_MAX - _PROCESSING_DEADLINE_MS
                    or processing_deadline_at
                    != authority_created_at + _PROCESSING_DEADLINE_MS
                ):
                    raise StoreConflictError(
                        "processing_deadline_conflict",
                        "timely pre-debit authority has an invalid media deadline",
                    )
                if not _transition_leased_tx(
                    connection,
                    claim,
                    {source_state},
                    "queued",
                    now_ms,
                    1,
                ):
                    raise _lease_lost(claim)
                job_update = connection.execute(
                    """UPDATE edit_v3_jobs
                       SET confirmed_preheld_total=?,queued_at=COALESCE(queued_at,?),
                           processing_deadline_at=COALESCE(processing_deadline_at,?),
                           reconciliation_reason=NULL,resume_state=NULL,updated_at=?
                       WHERE job_id=? AND worker_id=? AND fencing_token=? AND lease_until>?
                         AND confirmed_preheld_total IN (0,?)
                         AND (queued_at IS NULL OR queued_at=?)
                         AND (processing_deadline_at IS NULL OR processing_deadline_at=?)""",
                    (
                        amount,
                        authority_created_at,
                        processing_deadline_at,
                        now_ms,
                        claim.job_id,
                        claim.worker_id,
                        claim.fencing_token,
                        now_ms,
                        amount,
                        authority_created_at,
                        processing_deadline_at,
                    ),
                )
            if job_update.rowcount != 1:
                if not _lease_owned_tx(connection, claim, now_ms):
                    raise _lease_lost(claim)
                raise StoreConflictError(
                    "predebit_total_conflict",
                    "confirmed pre-debit totals or deadline conflict",
                )
            intent_update = connection.execute(
                """UPDATE edit_v3_billing_intents
                   SET status='completed',last_checked_at=?,
                       authority_evidence_json=?,updated_at=?,completed_at=?
                   WHERE id=? AND status!='completed'
                     AND EXISTS(
                         SELECT 1 FROM edit_v3_jobs AS j
                         WHERE j.job_id=edit_v3_billing_intents.job_id
                           AND j.job_id=? AND j.worker_id=?
                           AND j.fencing_token=? AND j.lease_until>?
                     )""",
                (
                    now_ms,
                    evidence_json,
                    now_ms,
                    now_ms,
                    intent_id,
                    claim.job_id,
                    claim.worker_id,
                    claim.fencing_token,
                    now_ms,
                ),
            )
            if intent_update.rowcount != 1:
                raise StoreConflictError(
                    "billing_intent_conflict",
                    "pre-debit intent could not be confirmed",
                )
            return _billing_result(
                _billing_row_for_claim_tx(connection, intent_id, claim, now_ms)
            )

        return self._write(write)

    def confirm_refund(
        self,
        intent_id: str,
        claim: LeaseClaim,
        *,
        authority_evidence: Mapping[str, Any],
        now_ms: int,
    ) -> dict[str, dict[str, Any]]:
        evidence_json = _json_text(authority_evidence)

        def write(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
            row = _billing_row_for_claim_tx(connection, intent_id, claim, now_ms)
            if row["operation"] not in {"refund_delta", "refund_full"}:
                raise StoreConflictError(
                    "billing_operation_conflict",
                    "refund confirmation requires a refund intent",
                )
            if row["status"] == "completed":
                if row["authority_evidence_json"] != evidence_json:
                    raise StoreConflictError(
                        "billing_authority_conflict",
                        "completed refund authority is immutable",
                    )
                return _billing_result(row)
            refunded = row["job_confirmed_refunded_total"]
            target = row["refund_target_total"]
            amount = row["request_amount"]
            preheld = row["job_confirmed_preheld_total"]
            if refunded + amount != target or not 0 <= target <= preheld:
                raise StoreConflictError(
                    "refund_total_conflict",
                    "refund response would violate the cumulative target",
                )
            job_update = connection.execute(
                """UPDATE edit_v3_jobs
                   SET confirmed_refunded_total=?,updated_at=?
                   WHERE job_id=? AND worker_id=? AND fencing_token=? AND lease_until>?
                     AND confirmed_refunded_total=?
                     AND confirmed_preheld_total>=?""",
                (
                    target,
                    now_ms,
                    claim.job_id,
                    claim.worker_id,
                    claim.fencing_token,
                    now_ms,
                    refunded,
                    target,
                ),
            )
            if job_update.rowcount != 1:
                if not _lease_owned_tx(connection, claim, now_ms):
                    raise _lease_lost(claim)
                raise StoreConflictError(
                    "refund_total_conflict",
                    "cumulative refunded total changed before confirmation",
                )
            intent_update = connection.execute(
                """UPDATE edit_v3_billing_intents
                   SET status='completed',last_checked_at=?,
                       authority_evidence_json=?,updated_at=?,completed_at=?
                   WHERE id=? AND status!='completed'
                     AND EXISTS(
                         SELECT 1 FROM edit_v3_jobs AS j
                         WHERE j.job_id=edit_v3_billing_intents.job_id
                           AND j.job_id=? AND j.worker_id=?
                           AND j.fencing_token=? AND j.lease_until>?
                     )""",
                (
                    now_ms,
                    evidence_json,
                    now_ms,
                    now_ms,
                    intent_id,
                    claim.job_id,
                    claim.worker_id,
                    claim.fencing_token,
                    now_ms,
                ),
            )
            if intent_update.rowcount != 1:
                raise StoreConflictError(
                    "billing_intent_conflict",
                    "refund intent could not be confirmed",
                )
            return _billing_result(
                _billing_row_for_claim_tx(connection, intent_id, claim, now_ms)
            )

        return self._write(write)

    def record_billing_unknown(
        self,
        intent_id: str,
        claim: LeaseClaim,
        *,
        authority_evidence: Mapping[str, Any],
        now_ms: int,
    ) -> dict[str, dict[str, Any]]:
        evidence_json = _json_text(authority_evidence)

        def write(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
            _billing_row_for_claim_tx(connection, intent_id, claim, now_ms)
            updated = connection.execute(
                """UPDATE edit_v3_billing_intents
                   SET last_checked_at=?,authority_evidence_json=?,updated_at=?
                   WHERE id=? AND status IN ('unknown','reconciliation_pending')
                     AND EXISTS(
                         SELECT 1 FROM edit_v3_jobs AS j
                         WHERE j.job_id=edit_v3_billing_intents.job_id
                           AND j.job_id=? AND j.worker_id=?
                           AND j.fencing_token=? AND j.lease_until>?
                     )""",
                (
                    now_ms,
                    evidence_json,
                    now_ms,
                    intent_id,
                    claim.job_id,
                    claim.worker_id,
                    claim.fencing_token,
                    now_ms,
                ),
            )
            if updated.rowcount != 1:
                if not _lease_owned_tx(connection, claim, now_ms):
                    raise _lease_lost(claim)
                raise StoreConflictError(
                    "billing_intent_conflict",
                    "billing intent is not in an unknown state",
                )
            return _billing_result(
                _billing_row_for_claim_tx(connection, intent_id, claim, now_ms)
            )

        return self._write(write)

    def mark_billing_authority_absent(
        self,
        intent_id: str,
        claim: LeaseClaim,
        *,
        now_ms: int,
    ) -> dict[str, dict[str, Any]]:
        evidence_json = _json_text({"authoritative": True, "transaction": None})

        def write(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
            row = _billing_row_for_claim_tx(connection, intent_id, claim, now_ms)
            reason, resume_state = _billing_recovery_context(row)
            terminal_absence = row["operation"] == "pre_debit"
            next_reason = reason
            next_resume_state = resume_state
            if row["job_state"] == resume_state:
                status = "retryable_absent"
                completed_at = None
                target_state = None
                transition_source = None
            elif row["job_state"] == "billing_reconciling":
                status = "absent" if terminal_absence else "retryable_absent"
                completed_at = now_ms if terminal_absence else None
                target_state = "prehold_absent" if terminal_absence else resume_state
                transition_source = "billing_reconciling"
            elif row["job_state"] == "failed_reconciliation_pending":
                status = "absent" if terminal_absence else "retryable_absent"
                completed_at = now_ms if terminal_absence else None
                target_state = "prehold_absent" if terminal_absence else "refund_pending"
                transition_source = "failed_reconciliation_pending"
                if not terminal_absence:
                    next_reason = "refund"
                    next_resume_state = "refund_pending"
            else:
                raise StoreConflictError(
                    "billing_state_conflict",
                    "billing absence is invalid in the current job state",
                )
            intent_update = connection.execute(
                """UPDATE edit_v3_billing_intents
                   SET status=?,first_unknown_at=?,last_checked_at=?,
                       authority_evidence_json=?,reason=?,resume_state=?,
                       updated_at=?,completed_at=?
                   WHERE id=? AND status IN ('unknown','reconciliation_pending')
                     AND EXISTS(
                         SELECT 1 FROM edit_v3_jobs AS j
                         WHERE j.job_id=edit_v3_billing_intents.job_id
                           AND j.job_id=? AND j.worker_id=?
                           AND j.fencing_token=? AND j.lease_until>?
                     )""",
                (
                    status,
                    row["first_unknown_at"] if target_state == "prehold_absent" else None,
                    now_ms,
                    evidence_json,
                    next_reason,
                    next_resume_state,
                    now_ms,
                    completed_at,
                    intent_id,
                    claim.job_id,
                    claim.worker_id,
                    claim.fencing_token,
                    now_ms,
                ),
            )
            if intent_update.rowcount != 1:
                raise StoreConflictError(
                    "billing_intent_conflict",
                    "pre-debit absence could not be recorded",
                )
            if target_state is not None:
                cleared = connection.execute(
                    """UPDATE edit_v3_jobs
                       SET reconciliation_reason=NULL,resume_state=NULL,updated_at=?
                       WHERE job_id=? AND worker_id=? AND fencing_token=?
                         AND lease_until>? AND state=?""",
                    (
                        now_ms,
                        claim.job_id,
                        claim.worker_id,
                        claim.fencing_token,
                        now_ms,
                        transition_source,
                    ),
                )
                if cleared.rowcount != 1:
                    if not _lease_owned_tx(connection, claim, now_ms):
                        raise _lease_lost(claim)
                    raise StoreConflictError(
                        "billing_state_conflict",
                        "billing absence source state changed",
                    )
                if not _transition_leased_tx(
                    connection,
                    claim,
                    {transition_source},
                    target_state,
                    now_ms,
                    1,
                ):
                    raise _lease_lost(claim)
            if target_state == "prehold_absent":
                return {
                    "intent": dict(
                        connection.execute(
                            "SELECT * FROM edit_v3_billing_intents WHERE id=?",
                            (intent_id,),
                        ).fetchone()
                    ),
                    "job": {
                        "job_id": claim.job_id,
                        "state": target_state,
                        "reconciliation_reason": None,
                        "resume_state": None,
                        "confirmed_preheld_total": 0,
                        "confirmed_refunded_total": 0,
                        "queued_at": None,
                        "processing_deadline_at": None,
                    },
                }
            return _billing_result(
                _billing_row_for_claim_tx(connection, intent_id, claim, now_ms)
            )

        return self._write(write)

    def timeout_billing_reconciliation(
        self,
        intent_id: str,
        claim: LeaseClaim,
        now_ms: int,
    ) -> dict[str, dict[str, Any]]:
        def write(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
            row = _billing_row_for_claim_tx(connection, intent_id, claim, now_ms)
            if row["job_state"] != "billing_reconciling":
                raise StoreConflictError(
                    "billing_state_conflict",
                    "timeout requires billing reconciliation state",
                )
            reason, resume_state = _billing_recovery_context(row)
            if (
                row["job_reconciliation_reason"] != reason
                or row["job_resume_state"] != resume_state
            ):
                raise StoreConflictError(
                    "billing_context_conflict",
                    "billing timeout context conflicts with its intent",
                )
            intent_update = connection.execute(
                """UPDATE edit_v3_billing_intents
                   SET status='reconciliation_pending',last_checked_at=?,updated_at=?
                   WHERE id=? AND status IN ('unknown','reconciliation_pending')
                     AND EXISTS(
                         SELECT 1 FROM edit_v3_jobs AS j
                         WHERE j.job_id=edit_v3_billing_intents.job_id
                           AND j.job_id=? AND j.worker_id=?
                           AND j.fencing_token=? AND j.lease_until>?
                     )""",
                (
                    now_ms,
                    now_ms,
                    intent_id,
                    claim.job_id,
                    claim.worker_id,
                    claim.fencing_token,
                    now_ms,
                ),
            )
            if intent_update.rowcount != 1:
                raise StoreConflictError(
                    "billing_intent_conflict",
                    "billing timeout intent update failed",
                )
            if not _transition_leased_tx(
                connection,
                claim,
                {"billing_reconciling"},
                "failed_reconciliation_pending",
                now_ms,
                1,
            ):
                raise _lease_lost(claim)
            intent = dict(
                connection.execute(
                    "SELECT * FROM edit_v3_billing_intents WHERE id=?", (intent_id,)
                ).fetchone()
            )
            return {
                "intent": intent,
                "job": {
                    "job_id": claim.job_id,
                    "state": "failed_reconciliation_pending",
                    "reconciliation_reason": row["job_reconciliation_reason"],
                    "resume_state": row["job_resume_state"],
                    "confirmed_preheld_total": row["job_confirmed_preheld_total"],
                    "confirmed_refunded_total": row["job_confirmed_refunded_total"],
                    "queued_at": row["job_queued_at"],
                    "processing_deadline_at": row["job_processing_deadline_at"],
                },
            }

        return self._write(write)

    def list_due_billing_intents(
        self,
        now_ms: int,
        *,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        _require_integer("now_ms", now_ms)
        _require_integer("limit", limit)
        if limit < 1:
            raise _configuration_error(
                "billing_limit_invalid", "billing intent limit must be positive"
            )
        return self._read(
            lambda connection: [
                dict(row)
                for row in connection.execute(
                    """SELECT * FROM edit_v3_billing_intents
                       WHERE environment=?
                         AND status IN ('pending','retryable_absent','unknown','reconciliation_pending')
                         AND created_at<=?
                       ORDER BY CASE WHEN first_unknown_at IS NULL THEN created_at
                                     ELSE first_unknown_at END,id
                       LIMIT ?""",
                    (self.environment, now_ms, limit),
                )
            ]
        )

    def insert_upload(
        self,
        owner_id: str,
        upload_id: str,
        *,
        upload_type: str,
        object_key: str,
        declared_mime: str,
        declared_size: int,
        expires_at: int,
        created_at: int,
        environment: str | None = None,
    ) -> dict[str, Any] | None:
        _require_integer("declared_size", declared_size)
        _require_integer("expires_at", expires_at)
        _require_integer("created_at", created_at)
        environment = self._environment(environment)
        immutable = {
            "upload_id": upload_id,
            "environment": environment,
            "owner_id": owner_id,
            "upload_type": upload_type,
            "object_key": object_key,
            "declared_mime": declared_mime,
            "declared_size": declared_size,
            "expires_at": expires_at,
            "created_at": created_at,
        }

        def write(connection: sqlite3.Connection) -> dict[str, Any] | None:
            existing = connection.execute(
                """SELECT * FROM edit_v3_uploads
                   WHERE environment=? AND owner_id=? AND upload_id=?""",
                (environment, owner_id, upload_id),
            ).fetchone()
            if existing is not None:
                if not self._same_values(existing, immutable):
                    raise _immutable_conflict(f"upload:{upload_id}")
                return dict(existing)
            try:
                connection.execute(
                    """INSERT INTO edit_v3_uploads(
                           upload_id,environment,owner_id,upload_type,object_key,declared_mime,
                           declared_size,status,expires_at,created_at,updated_at
                       ) VALUES(?,?,?,?,?,?,?,'pending',?,?,?)""",
                    (
                        upload_id,
                        environment,
                        owner_id,
                        upload_type,
                        object_key,
                        declared_mime,
                        declared_size,
                        expires_at,
                        created_at,
                        created_at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                if getattr(exc, "sqlite_errorcode", None) in {
                    getattr(sqlite3, "SQLITE_CONSTRAINT_PRIMARYKEY", -1),
                    getattr(sqlite3, "SQLITE_CONSTRAINT_UNIQUE", -1),
                }:
                    replay = connection.execute(
                        """SELECT * FROM edit_v3_uploads
                           WHERE environment=? AND owner_id=? AND upload_id=?""",
                        (environment, owner_id, upload_id),
                    ).fetchone()
                    if replay is not None:
                        if not self._same_values(replay, immutable):
                            raise _immutable_conflict(f"upload:{upload_id}") from exc
                        return dict(replay)
                    owned_object_key = connection.execute(
                        """SELECT 1 FROM edit_v3_uploads
                           WHERE environment=? AND owner_id=? AND object_key=?""",
                        (environment, owner_id, object_key),
                    ).fetchone()
                    if owned_object_key is None:
                        return None
                raise StoreConflictError(
                    "upload_identity_conflict",
                    "upload ID or object key is already bound",
                ) from exc
            return dict(
                connection.execute(
                    """SELECT * FROM edit_v3_uploads
                       WHERE environment=? AND owner_id=? AND upload_id=?""",
                    (environment, owner_id, upload_id),
                ).fetchone()
            )

        return self._write(write)

    def complete_upload(
        self,
        owner_id: str,
        upload_id: str,
        *,
        observed_mime: str,
        observed_size: int,
        observed_etag: str,
        sha256: str,
        duration_ms: int | None,
        width: int | None,
        height: int | None,
        probe: Mapping[str, Any],
        completed_at: int,
        environment: str | None = None,
    ) -> dict[str, Any] | None:
        _require_integer("observed_size", observed_size)
        _require_integer("duration_ms", duration_ms, nullable=True)
        _require_integer("width", width, nullable=True)
        _require_integer("height", height, nullable=True)
        _require_integer("completed_at", completed_at)
        environment = self._environment(environment)
        completion = {
            "observed_mime": observed_mime,
            "observed_size": observed_size,
            "observed_etag": observed_etag,
            "sha256": sha256,
            "duration_ms": duration_ms,
            "width": width,
            "height": height,
            "probe_json": _json_text(probe),
        }

        def write(connection: sqlite3.Connection) -> dict[str, Any] | None:
            existing = connection.execute(
                """SELECT * FROM edit_v3_uploads
                   WHERE environment=? AND owner_id=? AND upload_id=?""",
                (environment, owner_id, upload_id),
            ).fetchone()
            if existing is None:
                return None
            if existing["status"] == "completed":
                if not self._same_values(existing, completion):
                    raise _immutable_conflict(f"upload-completion:{upload_id}")
                return dict(existing)
            if existing["status"] != "pending":
                raise StoreConflictError(
                    "upload_not_completable",
                    "upload is not in the pending state",
                )
            connection.execute(
                """UPDATE edit_v3_uploads
                   SET observed_mime=?,observed_size=?,observed_etag=?,sha256=?,duration_ms=?,
                       width=?,height=?,probe_json=?,status='completed',completed_at=?,updated_at=?
                   WHERE environment=? AND owner_id=? AND upload_id=? AND status='pending'""",
                (
                    *completion.values(),
                    completed_at,
                    completed_at,
                    environment,
                    owner_id,
                    upload_id,
                ),
            )
            return dict(
                connection.execute(
                    """SELECT * FROM edit_v3_uploads
                       WHERE environment=? AND owner_id=? AND upload_id=?""",
                    (environment, owner_id, upload_id),
                ).fetchone()
            )

        return self._write(write)

    def get_upload_for_owner(
        self,
        owner_id: str,
        upload_id: str,
        *,
        environment: str | None = None,
    ) -> dict[str, Any] | None:
        environment = self._environment(environment)
        return self._read(
            lambda connection: _row_dict(
                connection.execute(
                    """SELECT * FROM edit_v3_uploads
                       WHERE environment=? AND owner_id=? AND upload_id=?""",
                    (environment, owner_id, upload_id),
                ).fetchone()
            )
        )

    def get_material_for_upload(
        self,
        owner_id: str,
        upload_id: str,
        *,
        environment: str | None = None,
    ) -> dict[str, Any] | None:
        environment = self._environment(environment)
        return self._read(
            lambda connection: _row_dict(
                connection.execute(
                    """SELECT * FROM edit_v3_materials
                       WHERE environment=? AND owner_id=? AND upload_id=?""",
                    (environment, owner_id, upload_id),
                ).fetchone()
            )
        )

    def resolve_request_uploads_for_owner(
        self,
        owner_id: str,
        *,
        source_upload_id: str | None,
        material_ids: Sequence[str],
        environment: str | None = None,
    ) -> dict[str, Any] | None:
        environment = self._environment(environment)
        if source_upload_id is not None:
            _require_nonblank("source_upload_id", source_upload_id)
        if (
            isinstance(material_ids, (str, bytes))
            or not isinstance(material_ids, Sequence)
            or len(material_ids) > 10
            or any(not isinstance(value, str) or not value for value in material_ids)
            or len(set(material_ids)) != len(material_ids)
        ):
            raise _configuration_error(
                "material_ids_invalid", "material IDs must be a unique sequence of at most ten"
            )

        def read(connection: sqlite3.Connection) -> dict[str, Any] | None:
            source = None
            if source_upload_id is not None:
                source = connection.execute(
                    """SELECT * FROM edit_v3_uploads
                       WHERE environment=? AND owner_id=? AND upload_id=?""",
                    (environment, owner_id, source_upload_id),
                ).fetchone()
                if source is None:
                    return None
            materials: list[dict[str, Any]] = []
            for material_id in material_ids:
                row = connection.execute(
                    """SELECT * FROM edit_v3_materials
                       WHERE environment=? AND owner_id=? AND material_id=?""",
                    (environment, owner_id, material_id),
                ).fetchone()
                if row is None:
                    return None
                materials.append(dict(row))
            return {
                "source_upload": None if source is None else dict(source),
                "materials": materials,
            }

        return self._read(read)

    def insert_material(
        self,
        owner_id: str,
        material_id: str,
        *,
        source_kind: str,
        cos_key: str,
        mime_type: str,
        size_bytes: int,
        sha256: str,
        metadata: Mapping[str, Any],
        created_at: int,
        upload_id: str | None = None,
        source_job_id: str | None = None,
        environment: str | None = None,
    ) -> dict[str, Any] | None:
        _require_integer("size_bytes", size_bytes)
        _require_integer("created_at", created_at)
        if not (
            (source_kind == "uploaded" and upload_id is not None and source_job_id is None)
            or (
                source_kind == "generated"
                and upload_id is None
                and source_job_id is not None
            )
        ):
            raise _configuration_error(
                "material_source_invalid",
                "material source fields do not form a supported authority union",
            )
        environment = self._environment(environment)
        expected = {
            "material_id": material_id,
            "environment": environment,
            "owner_id": owner_id,
            "upload_id": upload_id,
            "source_kind": source_kind,
            "source_job_id": source_job_id,
            "cos_key": cos_key,
            "mime_type": mime_type,
            "size_bytes": size_bytes,
            "sha256": sha256,
            "metadata_json": _json_text(metadata),
            "created_at": created_at,
        }

        def write(connection: sqlite3.Connection) -> dict[str, Any] | None:
            existing = connection.execute(
                """SELECT * FROM edit_v3_materials
                   WHERE environment=? AND owner_id=? AND material_id=?""",
                (environment, owner_id, material_id),
            ).fetchone()
            if existing is not None:
                if not self._same_values(existing, expected):
                    raise _immutable_conflict(f"material:{material_id}")
                return dict(existing)

            if source_kind == "uploaded":
                upload_replay = connection.execute(
                    """SELECT * FROM edit_v3_materials
                       WHERE environment=? AND owner_id=? AND upload_id=?""",
                    (environment, owner_id, upload_id),
                ).fetchone()
                if upload_replay is not None:
                    replay_expected = {
                        key: value
                        for key, value in expected.items()
                        if key != "material_id"
                    }
                    if not self._same_values(upload_replay, replay_expected):
                        raise _immutable_conflict(f"material-upload:{upload_id}")
                    return dict(upload_replay)

            if source_kind == "uploaded":
                upload = connection.execute(
                    """SELECT * FROM edit_v3_uploads
                       WHERE environment=? AND owner_id=? AND upload_id=?""",
                    (environment, owner_id, upload_id),
                ).fetchone()
                if upload is None:
                    return None
                if (
                    upload["status"] != "completed"
                    or upload["upload_type"] != "material_image"
                    or upload["observed_mime"]
                    not in {"image/jpeg", "image/png", "image/webp"}
                    or upload["observed_size"] is None
                    or upload["sha256"] is None
                ):
                    raise StoreConflictError(
                        "material_upload_invalid",
                        "only a completed, verified material-image upload may be promoted",
                    )
                authority = {
                    "cos_key": upload["object_key"],
                    "mime_type": upload["observed_mime"],
                    "size_bytes": upload["observed_size"],
                    "sha256": upload["sha256"],
                }
                if any(expected[key] != value for key, value in authority.items()):
                    raise StoreConflictError(
                        "material_upload_metadata_mismatch",
                        "material metadata must exactly match the completed upload authority",
                    )
            else:
                source_job = connection.execute(
                    """SELECT environment,owner_id FROM edit_v3_jobs
                       WHERE environment=? AND owner_id=? AND job_id=?""",
                    (environment, owner_id, source_job_id),
                ).fetchone()
                if source_job is None:
                    return None

            try:
                connection.execute(
                    """INSERT INTO edit_v3_materials(
                           material_id,environment,owner_id,upload_id,source_kind,source_job_id,
                           cos_key,mime_type,size_bytes,sha256,metadata_json,created_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    tuple(expected.values()),
                )
            except sqlite3.IntegrityError as exc:
                if getattr(exc, "sqlite_errorcode", None) in {
                    getattr(sqlite3, "SQLITE_CONSTRAINT_PRIMARYKEY", -1),
                    getattr(sqlite3, "SQLITE_CONSTRAINT_UNIQUE", -1),
                }:
                    material_replay = connection.execute(
                        """SELECT * FROM edit_v3_materials
                           WHERE environment=? AND owner_id=? AND material_id=?""",
                        (environment, owner_id, material_id),
                    ).fetchone()
                    if material_replay is not None:
                        if not self._same_values(material_replay, expected):
                            raise _immutable_conflict(f"material:{material_id}") from exc
                        return dict(material_replay)
                    if source_kind == "uploaded":
                        upload_replay = connection.execute(
                            """SELECT * FROM edit_v3_materials
                               WHERE environment=? AND owner_id=? AND upload_id=?""",
                            (environment, owner_id, upload_id),
                        ).fetchone()
                        if upload_replay is not None:
                            replay_expected = {
                                key: value
                                for key, value in expected.items()
                                if key != "material_id"
                            }
                            if not self._same_values(upload_replay, replay_expected):
                                raise _immutable_conflict(
                                    f"material-upload:{upload_id}"
                                ) from exc
                            return dict(upload_replay)
                    owned_cos_key = connection.execute(
                        """SELECT 1 FROM edit_v3_materials
                           WHERE environment=? AND owner_id=? AND cos_key=?""",
                        (environment, owner_id, cos_key),
                    ).fetchone()
                    if owned_cos_key is None:
                        return None
                raise StoreConflictError(
                    "material_identity_conflict",
                    "material ID, upload, or COS key is already bound",
                ) from exc
            return dict(
                connection.execute(
                    """SELECT * FROM edit_v3_materials
                       WHERE environment=? AND owner_id=? AND material_id=?""",
                    (environment, owner_id, material_id),
                ).fetchone()
            )

        return self._write(write)

    def bind_job_materials(
        self,
        owner_id: str,
        job_id: str,
        materials: Sequence[Mapping[str, Any]],
        *,
        created_at: int,
        environment: str | None = None,
    ) -> list[dict[str, Any]] | None:
        _require_integer("created_at", created_at)
        environment = self._environment(environment)
        normalized: list[tuple[str, str, int]] = []
        for binding in materials:
            if not isinstance(binding, Mapping) or set(binding) != {
                "material_id",
                "purpose",
                "ordinal",
            }:
                raise _configuration_error(
                    "job_material_binding_invalid",
                    "material binding fields are invalid",
                )
            material_id = binding["material_id"]
            purpose = binding["purpose"]
            ordinal = binding["ordinal"]
            _require_integer("ordinal", ordinal)
            if (
                not isinstance(material_id, str)
                or not material_id
                or not isinstance(purpose, str)
                or not purpose
                or ordinal < 0
            ):
                raise _configuration_error(
                    "job_material_binding_invalid",
                    "material binding values are invalid",
                )
            normalized.append((material_id, purpose, ordinal))
        if len({item[0] for item in normalized}) != len(normalized) or len(
            {(item[1], item[2]) for item in normalized}
        ) != len(normalized):
            raise _configuration_error(
                "job_material_binding_invalid",
                "material bindings contain duplicate identities",
            )

        def write(connection: sqlite3.Connection) -> list[dict[str, Any]] | None:
            job = connection.execute(
                """SELECT 1 FROM edit_v3_jobs
                   WHERE environment=? AND owner_id=? AND job_id=?""",
                (environment, owner_id, job_id),
            ).fetchone()
            if job is None:
                return None
            for material_id, _purpose, _ordinal in normalized:
                material = connection.execute(
                    """SELECT 1 FROM edit_v3_materials
                       WHERE environment=? AND owner_id=? AND material_id=?""",
                    (environment, owner_id, material_id),
                ).fetchone()
                if material is None:
                    return None
            for material_id, purpose, ordinal in normalized:
                existing = connection.execute(
                    """SELECT jm.* FROM edit_v3_job_materials AS jm
                       JOIN edit_v3_jobs AS j ON j.job_id=jm.job_id
                       WHERE j.environment=? AND j.owner_id=?
                         AND jm.job_id=? AND jm.material_id=?""",
                    (environment, owner_id, job_id, material_id),
                ).fetchone()
                if existing is not None:
                    if (
                        existing["purpose"] != purpose
                        or existing["ordinal"] != ordinal
                        or existing["created_at"] != created_at
                    ):
                        raise _immutable_conflict(
                            f"job-material:{job_id}:{material_id}"
                        )
                    continue
                try:
                    connection.execute(
                        """INSERT INTO edit_v3_job_materials(
                               job_id,material_id,purpose,ordinal,created_at
                           ) VALUES(?,?,?,?,?)""",
                        (job_id, material_id, purpose, ordinal, created_at),
                    )
                except sqlite3.IntegrityError as exc:
                    raise StoreConflictError(
                        "job_material_identity_conflict",
                        "job material binding conflicts with an immutable slot",
                    ) from exc
            return [
                dict(row)
                for row in connection.execute(
                    """SELECT jm.* FROM edit_v3_job_materials AS jm
                       JOIN edit_v3_jobs AS j ON j.job_id=jm.job_id
                       WHERE j.environment=? AND j.owner_id=? AND jm.job_id=?
                       ORDER BY jm.ordinal,jm.material_id""",
                    (environment, owner_id, job_id),
                )
            ]

        return self._write(write)

    def create_publish_intents(
        self,
        claim: LeaseClaim,
        metadata_sha256: str,
        *,
        now_ms: int,
    ) -> tuple[dict[str, Any], ...]:
        """Freeze the five external publication identities under a live claim."""

        claim = _require_claim(claim)
        metadata_sha256 = _require_sha256("metadata_sha256", metadata_sha256)
        now_ms = _require_now_ms(now_ms)

        def write(connection: sqlite3.Connection) -> tuple[dict[str, Any], ...]:
            job = connection.execute(
                """SELECT owner_id,delivery_object_key FROM edit_v3_jobs
                   WHERE job_id=? AND worker_id=? AND fencing_token=?
                     AND lease_until>?""",
                (
                    claim.job_id,
                    claim.worker_id,
                    claim.fencing_token,
                    now_ms,
                ),
            ).fetchone()
            if job is None:
                raise _lease_lost(claim)
            object_key = job["delivery_object_key"]
            if (
                not isinstance(object_key, str)
                or not object_key.strip()
                or object_key != object_key.strip()
                or "://" in object_key
            ):
                raise StoreConflictError(
                    "delivery_object_missing",
                    "publication requires one immutable stable delivery object key",
                )

            divergent = connection.execute(
                """SELECT 1 FROM edit_v3_publish_intents
                   WHERE job_id=? AND (object_key<>? OR metadata_sha256<>?)
                   LIMIT 1""",
                (claim.job_id, object_key, metadata_sha256),
            ).fetchone()
            if divergent is not None:
                raise StoreConflictError(
                    "publish_intent_conflict",
                    "publication identity was reused with divergent immutable delivery data",
                )

            rows = connection.execute(
                """SELECT * FROM edit_v3_publish_intents
                   WHERE job_id=? AND publish_generation=?""",
                (claim.job_id, claim.fencing_token),
            ).fetchall()
            if rows:
                by_operation = {row["operation"]: row for row in rows}
                if set(by_operation) != set(_PUBLISH_OPERATIONS):
                    raise StoreConflictError(
                        "publish_intent_conflict",
                        "publication generation has an incomplete outbox identity set",
                    )
                for operation in _PUBLISH_OPERATIONS:
                    row = by_operation[operation]
                    expected_key = (
                        f"ai-edit-v3:{claim.job_id}:publish:"
                        f"{_PUBLISH_KEY_SEGMENTS[operation]}:{claim.fencing_token}"
                    )
                    if (
                        row["id"]
                        != _publish_intent_id(
                            claim.job_id, claim.fencing_token, operation
                        )
                        or row["external_idempotency_key"] != expected_key
                        or row["object_key"] != object_key
                        or row["metadata_sha256"] != metadata_sha256
                        or row["expected_decision"]
                        != _PUBLISH_EXPECTED_DECISIONS[operation]
                        or row["fencing_token"] != claim.fencing_token
                    ):
                        raise StoreConflictError(
                            "publish_intent_conflict",
                            "publication outbox replay diverged from its frozen identity",
                        )
                return tuple(dict(by_operation[name]) for name in _PUBLISH_OPERATIONS)

            for operation in _PUBLISH_OPERATIONS:
                external_key = (
                    f"ai-edit-v3:{claim.job_id}:publish:"
                    f"{_PUBLISH_KEY_SEGMENTS[operation]}:{claim.fencing_token}"
                )
                connection.execute(
                    """INSERT INTO edit_v3_publish_intents(
                           id,job_id,publish_generation,operation,
                           external_idempotency_key,object_key,metadata_sha256,
                           expected_decision,status,fencing_token,created_at,updated_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        _publish_intent_id(
                            claim.job_id, claim.fencing_token, operation
                        ),
                        claim.job_id,
                        claim.fencing_token,
                        operation,
                        external_key,
                        object_key,
                        metadata_sha256,
                        _PUBLISH_EXPECTED_DECISIONS[operation],
                        "planned",
                        claim.fencing_token,
                        now_ms,
                        now_ms,
                    ),
                )
            created = connection.execute(
                """SELECT * FROM edit_v3_publish_intents
                   WHERE job_id=? AND publish_generation=?""",
                (claim.job_id, claim.fencing_token),
            ).fetchall()
            by_operation = {row["operation"]: dict(row) for row in created}
            return tuple(by_operation[name] for name in _PUBLISH_OPERATIONS)

        try:
            return self._write(write)
        except sqlite3.IntegrityError as exc:
            raise StoreConflictError(
                "publish_intent_conflict",
                "publication outbox violates a frozen identity constraint",
            ) from exc

    def get_publish_context_for_claim(
        self,
        claim: LeaseClaim,
        now_ms: int,
    ) -> dict[str, Any]:
        claim = _require_claim(claim)
        now_ms = _require_now_ms(now_ms)

        def read(connection: sqlite3.Connection) -> dict[str, Any]:
            job = connection.execute(
                """SELECT job_id,owner_id,state,delivery_object_key,asset_id,
                          confirmed_preheld_total,confirmed_refunded_total,
                          request_sha256
                   FROM edit_v3_jobs
                   WHERE job_id=? AND worker_id=? AND fencing_token=?
                     AND lease_until>?""",
                (
                    claim.job_id,
                    claim.worker_id,
                    claim.fencing_token,
                    now_ms,
                ),
            ).fetchone()
            if job is None:
                raise _lease_lost(claim)
            rows = connection.execute(
                """SELECT * FROM edit_v3_publish_intents
                   WHERE job_id=? AND publish_generation=?""",
                (claim.job_id, claim.fencing_token),
            ).fetchall()
            by_operation = {row["operation"]: dict(row) for row in rows}
            if rows and set(by_operation) != set(_PUBLISH_OPERATIONS):
                raise StoreConflictError(
                    "publish_intent_conflict",
                    "publication generation has an incomplete outbox identity set",
                )
            return {
                "job": dict(job),
                "intents": tuple(
                    by_operation[name]
                    for name in _PUBLISH_OPERATIONS
                    if name in by_operation
                ),
            }

        return self._read(read)

    def get_historical_publish_authority_for_claim(
        self,
        claim: LeaseClaim,
        publish_generation: int,
        now_ms: int,
    ) -> dict[str, Any]:
        """Return the frozen query identity for one unresolved older generation."""

        claim = _require_claim(claim)
        publish_generation = _require_publish_generation(publish_generation)
        now_ms = _require_now_ms(now_ms)
        return self._read(
            lambda connection: _historical_publish_authority_tx(
                connection,
                claim,
                publish_generation,
                self.environment,
                now_ms,
            )
        )

    def record_historical_publish_authority(
        self,
        claim: LeaseClaim,
        publish_generation: int,
        evidence: Mapping[str, Any],
        *,
        now_ms: int,
    ) -> dict[str, Any]:
        """Record a historical query result without creating a new generation."""

        claim = _require_claim(claim)
        publish_generation = _require_publish_generation(publish_generation)
        normalized = _normalize_publish_evidence(evidence)
        if "outcome" in normalized and (
            normalized["outcome"] != "unknown"
            or normalized["reason_code"] == "definitive_not_accepted"
        ):
            raise _configuration_error(
                "publish_evidence_invalid",
                "historical publication authority accepts only ambiguous evidence",
            )
        evidence_json = _json_text(normalized)
        now_ms = _require_now_ms(now_ms)

        def write(connection: sqlite3.Connection) -> dict[str, Any]:
            authority = _historical_publish_authority_tx(
                connection,
                claim,
                publish_generation,
                self.environment,
                now_ms,
            )
            job = authority["job"]
            query = authority["query"]
            status = normalized.get("status")
            if status not in {"publish_won", "cancel_won"}:
                updated = connection.execute(
                    """UPDATE edit_v3_publish_intents
                       SET status='unknown',
                           first_unknown_at=COALESCE(first_unknown_at,?),
                           last_decision_json=?,last_decision_at=?,updated_at=?
                       WHERE id=?""",
                    (now_ms, evidence_json, now_ms, now_ms, query["id"]),
                )
                if updated.rowcount != 1:
                    raise StoreConflictError(
                        "publish_intent_missing",
                        "historical query identity disappeared before recording",
                    )
                return {
                    "decision": normalized,
                    "job": job,
                    "publish_generation": publish_generation,
                    "query": dict(
                        connection.execute(
                            "SELECT * FROM edit_v3_publish_intents WHERE id=?",
                            (query["id"],),
                        ).fetchone()
                    ),
                }

            if status == "publish_won":
                asset_id = normalized["asset_id"]
                if job["asset_id"] not in {None, asset_id}:
                    raise StoreConflictError(
                        "asset_decision_conflict",
                        "publication winner conflicts with the frozen asset id",
                    )
                refund = connection.execute(
                    """SELECT 1 FROM edit_v3_billing_intents
                       WHERE job_id=? AND operation='refund_full' LIMIT 1""",
                    (claim.job_id,),
                ).fetchone()
                cancel_winner = connection.execute(
                    """SELECT 1 FROM edit_v3_publish_intents
                       WHERE job_id=? AND status='cancel_won' LIMIT 1""",
                    (claim.job_id,),
                ).fetchone()
                if refund is not None or cancel_winner is not None:
                    raise StoreConflictError(
                        "asset_refund_conflict",
                        "published asset cannot coexist with cancel authority",
                    )
                updated = connection.execute(
                    """UPDATE edit_v3_jobs SET asset_id=?,updated_at=?
                       WHERE job_id=? AND worker_id=? AND fencing_token=?
                         AND lease_until>? AND (asset_id IS NULL OR asset_id=?)""",
                    (
                        asset_id,
                        now_ms,
                        claim.job_id,
                        claim.worker_id,
                        claim.fencing_token,
                        now_ms,
                        asset_id,
                    ),
                )
                if updated.rowcount != 1:
                    raise _lease_lost(claim)
                target_state = "completed"
                refund_intent = None
            else:
                publish_winner = connection.execute(
                    """SELECT 1 FROM edit_v3_publish_intents
                       WHERE job_id=? AND status='publish_won' LIMIT 1""",
                    (claim.job_id,),
                ).fetchone()
                if job["asset_id"] is not None or publish_winner is not None:
                    raise StoreConflictError(
                        "asset_refund_conflict",
                        "cancel winner cannot overwrite an authoritative publication",
                    )
                refund_intent = _freeze_full_refund_intent_tx(
                    connection, job, now_ms
                )
                target_state = "failed"

            connection.execute(
                """UPDATE edit_v3_publish_intents
                   SET status=?,last_decision_json=?,last_decision_at=?,
                       asset_id=?,updated_at=? WHERE job_id=?""",
                (
                    status,
                    evidence_json,
                    now_ms,
                    normalized["asset_id"],
                    now_ms,
                    claim.job_id,
                ),
            )
            if not _transition_leased_tx(
                connection,
                claim,
                {job["state"]},
                target_state,
                now_ms,
                1,
                preserve_current_lease=True,
            ):
                if not _lease_owned_tx(connection, claim, now_ms):
                    raise _lease_lost(claim)
                raise StoreConflictError(
                    "publish_authority_transition_conflict",
                    "historical publication authority could not finalize the job",
                )
            final_job = dict(
                connection.execute(
                    "SELECT * FROM edit_v3_jobs WHERE job_id=?",
                    (claim.job_id,),
                ).fetchone()
            )
            return {
                "decision": normalized,
                "intent": refund_intent,
                "job": final_job,
                "publish_generation": publish_generation,
                "query": query,
            }

        try:
            return self._write(write)
        except sqlite3.IntegrityError as exc:
            raise StoreConflictError(
                "publish_authority_conflict",
                "historical publication authority violates frozen constraints",
            ) from exc

    def list_due_publish_intents(
        self,
        now_ms: int,
        *,
        limit: int = 100,
        cursor: tuple[int, str] | None = None,
    ) -> tuple[dict[str, Any], ...]:
        now_ms = _require_now_ms(now_ms)
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 100
        ):
            raise _configuration_error(
                "publish_limit_invalid",
                "publish due-list limit must be an integer from 1 to 100",
            )
        if cursor is not None:
            if not isinstance(cursor, tuple) or len(cursor) != 2:
                raise _configuration_error(
                    "publish_cursor_invalid",
                    "publish cursor must be a due-time and intent-id tuple",
                )
            due_at, intent_id = cursor
            if (
                isinstance(due_at, bool)
                or not isinstance(due_at, int)
                or due_at < 0
                or due_at > _SQLITE_INT64_MAX
                or not isinstance(intent_id, str)
                or not intent_id
                or intent_id != intent_id.strip()
            ):
                raise _configuration_error(
                    "publish_cursor_invalid",
                    "publish cursor contains an invalid due time or intent id",
                )

        def read(connection: sqlite3.Connection) -> tuple[dict[str, Any], ...]:
            if cursor is None:
                rows = connection.execute(
                    """SELECT p.*,COALESCE(p.first_unknown_at,p.updated_at) AS due_at
                       FROM edit_v3_publish_intents AS p
                       WHERE p.status IN ('pending','unknown')
                         AND COALESCE(p.first_unknown_at,p.updated_at)<=?
                       ORDER BY due_at,p.id LIMIT ?""",
                    (now_ms, limit),
                ).fetchall()
            else:
                due_at, intent_id = cursor
                rows = connection.execute(
                    """SELECT p.*,COALESCE(p.first_unknown_at,p.updated_at) AS due_at
                       FROM edit_v3_publish_intents AS p
                       WHERE p.status IN ('pending','unknown')
                         AND COALESCE(p.first_unknown_at,p.updated_at)<=?
                         AND (COALESCE(p.first_unknown_at,p.updated_at)>?
                              OR (COALESCE(p.first_unknown_at,p.updated_at)=?
                                  AND p.id>?))
                       ORDER BY due_at,p.id LIMIT ?""",
                    (now_ms, due_at, due_at, intent_id, limit),
                ).fetchall()
            return tuple(dict(row) for row in rows)

        return self._read(read)

    def begin_publish_operation(
        self,
        claim: LeaseClaim,
        operation: str,
        *,
        now_ms: int,
    ) -> dict[str, Any]:
        claim = _require_claim(claim)
        operation = _require_publish_operation(operation)
        now_ms = _require_now_ms(now_ms)

        def write(connection: sqlite3.Connection) -> dict[str, Any]:
            row = connection.execute(
                """SELECT * FROM edit_v3_publish_intents
                   WHERE job_id=? AND publish_generation=? AND operation=?""",
                (claim.job_id, claim.fencing_token, operation),
            ).fetchone()
            if row is None:
                if not _lease_owned_tx(connection, claim, now_ms):
                    raise _lease_lost(claim)
                raise StoreConflictError(
                    "publish_intent_missing",
                    "publication operation is missing its durable outbox identity",
                )
            if row["status"] in {"publish_won", "cancel_won", "stale_generation"}:
                return dict(row)
            updated = connection.execute(
                """UPDATE edit_v3_publish_intents
                   SET status='pending',updated_at=?
                   WHERE id=? AND EXISTS(
                       SELECT 1 FROM edit_v3_jobs AS j
                       WHERE j.job_id=edit_v3_publish_intents.job_id
                         AND j.job_id=? AND j.worker_id=?
                         AND j.fencing_token=? AND j.lease_until>?
                   )""",
                (
                    now_ms,
                    row["id"],
                    claim.job_id,
                    claim.worker_id,
                    claim.fencing_token,
                    now_ms,
                ),
            )
            if updated.rowcount != 1:
                raise _lease_lost(claim)
            return dict(
                connection.execute(
                    "SELECT * FROM edit_v3_publish_intents WHERE id=?",
                    (row["id"],),
                ).fetchone()
            )

        return self._write(write)

    def record_publish_operation(
        self,
        claim: LeaseClaim,
        operation: str,
        status: str,
        evidence: Mapping[str, Any],
        *,
        now_ms: int,
    ) -> dict[str, Any]:
        claim = _require_claim(claim)
        operation = _require_publish_operation(operation)
        if status not in {"accepted", "pending", "unknown"}:
            raise _configuration_error(
                "publish_status_invalid",
                "publication operation status is not persistable",
            )
        evidence = _normalize_publish_evidence(evidence)
        if "outcome" in evidence:
            outcome = evidence["outcome"]
            reason_code = evidence["reason_code"]
            valid_safe_evidence = (
                outcome == "unknown"
                and status == "unknown"
                and reason_code != "definitive_not_accepted"
            ) or (
                outcome == "definitive_not_accepted"
                and status == "pending"
                and reason_code == "definitive_not_accepted"
            )
            if not valid_safe_evidence:
                raise _configuration_error(
                    "publish_evidence_invalid",
                    "publication operation status conflicts with safe evidence",
                )
        else:
            expected_status = (
                "accepted"
                if operation in _PUBLISH_ACCEPTED_OPERATIONS
                else "unknown"
            )
            if (
                evidence["status"] != "accepted"
                or evidence["current_generation"] != claim.fencing_token
                or status != expected_status
            ):
                raise _configuration_error(
                    "publish_evidence_invalid",
                    "publication operation accepts only its current canonical evidence",
                )
        evidence_json = _json_text(evidence)
        now_ms = _require_now_ms(now_ms)

        def write(connection: sqlite3.Connection) -> dict[str, Any]:
            updated = connection.execute(
                """UPDATE edit_v3_publish_intents
                   SET status=?,
                       first_unknown_at=CASE
                           WHEN ?='unknown' THEN COALESCE(first_unknown_at,?)
                           ELSE first_unknown_at
                       END,
                       last_decision_json=?,last_decision_at=?,updated_at=?
                   WHERE job_id=? AND publish_generation=? AND operation=?
                     AND EXISTS(
                         SELECT 1 FROM edit_v3_jobs AS j
                         WHERE j.job_id=edit_v3_publish_intents.job_id
                           AND j.job_id=? AND j.worker_id=?
                           AND j.fencing_token=? AND j.lease_until>?
                     )""",
                (
                    status,
                    status,
                    now_ms,
                    evidence_json,
                    now_ms,
                    now_ms,
                    claim.job_id,
                    claim.fencing_token,
                    operation,
                    claim.job_id,
                    claim.worker_id,
                    claim.fencing_token,
                    now_ms,
                ),
            )
            if updated.rowcount != 1:
                if not _lease_owned_tx(connection, claim, now_ms):
                    raise _lease_lost(claim)
                raise StoreConflictError(
                    "publish_intent_missing",
                    "publication response has no matching durable operation",
                )
            return dict(
                connection.execute(
                    """SELECT * FROM edit_v3_publish_intents
                       WHERE job_id=? AND publish_generation=? AND operation=?""",
                    (claim.job_id, claim.fencing_token, operation),
                ).fetchone()
            )

        return self._write(write)

    def record_publish_winner(
        self,
        claim: LeaseClaim,
        operation: str,
        asset_id: str,
        decision: Mapping[str, Any],
        *,
        now_ms: int,
    ) -> dict[str, Any]:
        claim = _require_claim(claim)
        operation = _require_publish_operation(operation)
        if not is_valid_publish_asset_id(asset_id):
            raise _configuration_error(
                "asset_id_invalid", "asset id must be one opaque stable identifier"
            )
        decision = _normalize_publish_decision(decision)
        if decision["status"] != "publish_won" or decision["asset_id"] != asset_id:
            raise _configuration_error(
                "publish_evidence_invalid",
                "publication winner decision must match the frozen asset id",
            )
        decision_json = _json_text(decision)
        now_ms = _require_now_ms(now_ms)

        def write(connection: sqlite3.Connection) -> dict[str, Any]:
            job = connection.execute(
                """SELECT asset_id FROM edit_v3_jobs
                   WHERE job_id=? AND worker_id=? AND fencing_token=?
                     AND lease_until>?""",
                (
                    claim.job_id,
                    claim.worker_id,
                    claim.fencing_token,
                    now_ms,
                ),
            ).fetchone()
            if job is None:
                raise _lease_lost(claim)
            if job["asset_id"] not in {None, asset_id}:
                raise StoreConflictError(
                    "asset_decision_conflict",
                    "publication winner conflicts with the frozen asset id",
                )
            refund = connection.execute(
                """SELECT 1 FROM edit_v3_billing_intents
                   WHERE job_id=? AND operation='refund_full' LIMIT 1""",
                (claim.job_id,),
            ).fetchone()
            if refund is not None:
                raise StoreConflictError(
                    "asset_refund_conflict",
                    "published asset cannot coexist with a full-refund intent",
                )
            operation_row = connection.execute(
                """SELECT 1 FROM edit_v3_publish_intents
                   WHERE job_id=? AND publish_generation=? AND operation=?""",
                (claim.job_id, claim.fencing_token, operation),
            ).fetchone()
            if operation_row is None:
                raise StoreConflictError(
                    "publish_intent_missing",
                    "publication winner has no matching durable operation",
                )
            updated = connection.execute(
                """UPDATE edit_v3_jobs SET asset_id=?,updated_at=?
                   WHERE job_id=? AND worker_id=? AND fencing_token=?
                     AND lease_until>? AND (asset_id IS NULL OR asset_id=?)""",
                (
                    asset_id,
                    now_ms,
                    claim.job_id,
                    claim.worker_id,
                    claim.fencing_token,
                    now_ms,
                    asset_id,
                ),
            )
            if updated.rowcount != 1:
                raise _lease_lost(claim)
            connection.execute(
                """UPDATE edit_v3_publish_intents
                   SET status='publish_won',last_decision_json=?,
                       last_decision_at=?,asset_id=?,updated_at=?
                   WHERE job_id=?""",
                (decision_json, now_ms, asset_id, now_ms, claim.job_id),
            )
            return {
                "job_id": claim.job_id,
                "asset_id": asset_id,
                "decision": decision,
            }

        return self._write(write)

    def record_cancel_winner_and_refund(
        self,
        claim: LeaseClaim,
        operation: str,
        decision: Mapping[str, Any],
        *,
        now_ms: int,
    ) -> dict[str, Any]:
        """Persist cancel authority and freeze the Task 6 full-refund outbox."""

        claim = _require_claim(claim)
        operation = _require_publish_operation(operation)
        decision = _normalize_publish_decision(decision)
        if decision["status"] != "cancel_won" or decision["asset_id"] is not None:
            raise _configuration_error(
                "publish_evidence_invalid",
                "cancel winner decision must be canonical",
            )
        decision_json = _json_text(decision)
        now_ms = _require_now_ms(now_ms)

        def write(connection: sqlite3.Connection) -> dict[str, Any]:
            job = connection.execute(
                """SELECT * FROM edit_v3_jobs
                   WHERE job_id=? AND worker_id=? AND fencing_token=?
                     AND lease_until>?""",
                (
                    claim.job_id,
                    claim.worker_id,
                    claim.fencing_token,
                    now_ms,
                ),
            ).fetchone()
            if job is None:
                raise _lease_lost(claim)
            operation_row = connection.execute(
                """SELECT 1 FROM edit_v3_publish_intents
                   WHERE job_id=? AND publish_generation=? AND operation=?""",
                (claim.job_id, claim.fencing_token, operation),
            ).fetchone()
            if operation_row is None:
                raise StoreConflictError(
                    "publish_intent_missing",
                    "cancel winner has no matching durable publication operation",
                )
            publish_winner = connection.execute(
                """SELECT 1 FROM edit_v3_publish_intents
                   WHERE job_id=? AND status='publish_won' LIMIT 1""",
                (claim.job_id,),
            ).fetchone()
            if job["asset_id"] is not None or publish_winner is not None:
                raise StoreConflictError(
                    "asset_refund_conflict",
                    "cancel winner cannot overwrite an authoritative publication",
                )

            target = job["confirmed_preheld_total"]
            refunded = job["confirmed_refunded_total"]
            existing = _freeze_full_refund_intent_tx(connection, job, now_ms)

            connection.execute(
                """UPDATE edit_v3_publish_intents
                   SET status='cancel_won',last_decision_json=?,
                       last_decision_at=?,asset_id=NULL,updated_at=?
                   WHERE job_id=?""",
                (decision_json, now_ms, now_ms, claim.job_id),
            )
            return {
                "job": {
                    "job_id": claim.job_id,
                    "state": job["state"],
                    "confirmed_preheld_total": target,
                    "confirmed_refunded_total": refunded,
                },
                "intent": dict(existing),
                "decision": decision,
            }

        try:
            return self._write(write)
        except sqlite3.IntegrityError as exc:
            raise StoreConflictError(
                "billing_intent_conflict",
                "cancel winner violates frozen refund or publication constraints",
            ) from exc

    def get_job_for_owner(
        self,
        owner_id: str,
        job_id: str,
        *,
        environment: str | None = None,
    ) -> dict[str, Any] | None:
        environment = self._environment(environment)
        return self._read(
            lambda connection: _row_dict(
                connection.execute(
                    """SELECT * FROM edit_v3_jobs
                       WHERE environment=? AND owner_id=? AND job_id=?""",
                    (environment, owner_id, job_id),
                ).fetchone()
            )
        )

    def get_job_by_idempotency_for_owner(
        self,
        owner_id: str,
        idempotency_key: str,
        *,
        environment: str | None = None,
    ) -> dict[str, Any] | None:
        environment = self._environment(environment)
        return self._read(
            lambda connection: _row_dict(
                connection.execute(
                    """SELECT * FROM edit_v3_jobs
                       WHERE environment=? AND owner_id=? AND idempotency_key=?""",
                    (environment, owner_id, idempotency_key),
                ).fetchone()
            )
        )

    def get_latest_plan_for_owner(
        self,
        owner_id: str,
        job_id: str,
        *,
        environment: str | None = None,
    ) -> dict[str, Any] | None:
        environment = self._environment(environment)
        return self._read(
            lambda connection: _row_dict(
                connection.execute(
                    """SELECT p.* FROM edit_v3_plans AS p
                       JOIN edit_v3_jobs AS j ON j.job_id=p.job_id
                       WHERE j.environment=? AND j.owner_id=? AND p.job_id=?
                       ORDER BY p.version DESC LIMIT 1""",
                    (environment, owner_id, job_id),
                ).fetchone()
            )
        )

    @staticmethod
    def _encode_job_cursor(
        environment: str,
        owner_id: str,
        created_at: int,
        job_id: str,
    ) -> str:
        raw = canonical_json(
            {
                "created_at": created_at,
                "environment": environment,
                "job_id": job_id,
                "owner_id": owner_id,
            }
        )
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    @staticmethod
    def _decode_job_cursor(cursor: str) -> dict[str, Any]:
        if not isinstance(cursor, str) or not cursor or len(cursor) > 1024:
            raise _configuration_error("job_cursor_invalid", "job cursor is invalid")
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            raw = base64.b64decode(
                padded.encode("ascii"),
                altchars=b"-_",
                validate=True,
            )
            value = parse_strict_json(
                raw,
                max_bytes=1024,
                max_depth=2,
                max_items=8,
                max_string_chars=256,
            )
        except (UnicodeError, ContractError, ValueError, binascii.Error) as exc:
            raise _configuration_error("job_cursor_invalid", "job cursor is invalid") from exc
        if (
            not isinstance(value, dict)
            or set(value) != {"created_at", "environment", "job_id", "owner_id"}
            or isinstance(value["created_at"], bool)
            or not isinstance(value["created_at"], int)
            or not isinstance(value["environment"], str)
            or not isinstance(value["job_id"], str)
            or not value["job_id"]
            or not isinstance(value["owner_id"], str)
            or not value["owner_id"]
        ):
            raise _configuration_error("job_cursor_invalid", "job cursor is invalid")
        _require_integer("created_at", value["created_at"])
        return value

    def list_jobs_for_owner(
        self,
        owner_id: str,
        *,
        environment: str | None = None,
        limit: int = 20,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        environment = self._environment(environment)
        _require_integer("limit", limit)
        if not 1 <= limit <= 100:
            raise _configuration_error(
                "job_page_limit_invalid",
                "job page limit must be from 1 to 100",
            )
        decoded = None if cursor is None else self._decode_job_cursor(cursor)
        if decoded is not None and (
            decoded["environment"] != environment or decoded["owner_id"] != owner_id
        ):
            raise _configuration_error(
                "job_cursor_scope_mismatch",
                "job cursor is bound to another owner or environment",
            )

        def read(connection: sqlite3.Connection) -> dict[str, Any]:
            if decoded is None:
                rows = connection.execute(
                    """SELECT * FROM edit_v3_jobs
                       WHERE environment=? AND owner_id=?
                       ORDER BY created_at DESC,job_id DESC LIMIT ?""",
                    (environment, owner_id, limit + 1),
                ).fetchall()
            else:
                rows = connection.execute(
                    """SELECT * FROM edit_v3_jobs
                       WHERE environment=? AND owner_id=?
                         AND (created_at < ? OR (created_at = ? AND job_id < ?))
                       ORDER BY created_at DESC,job_id DESC LIMIT ?""",
                    (
                        environment,
                        owner_id,
                        decoded["created_at"],
                        decoded["created_at"],
                        decoded["job_id"],
                        limit + 1,
                    ),
                ).fetchall()
            has_more = len(rows) > limit
            items = [dict(row) for row in rows[:limit]]
            next_cursor = None
            if has_more and items:
                last = items[-1]
                next_cursor = self._encode_job_cursor(
                    environment,
                    owner_id,
                    last["created_at"],
                    last["job_id"],
                )
            return {"items": items, "next_cursor": next_cursor}

        return self._read(read)


def _configured_store(db_path: Path | None) -> V3Store:
    return V3Store(db_path=db_path)


def claim_next_job(
    worker_id: str,
    lease_seconds: int,
    now_ms: int,
    *,
    db_path: Path | None = None,
) -> LeaseClaim | None:
    return _configured_store(db_path).claim_next_job(worker_id, lease_seconds, now_ms)


def claim_job(
    job_id: str,
    worker_id: str,
    lease_seconds: int,
    now_ms: int,
    *,
    expected_states: Any,
    db_path: Path | None = None,
) -> LeaseClaim | None:
    return _configured_store(db_path).claim_job(
        job_id,
        worker_id,
        lease_seconds,
        now_ms,
        expected_states=expected_states,
    )


def renew_lease(
    claim: LeaseClaim,
    lease_seconds: int,
    now_ms: int,
    *,
    db_path: Path | None = None,
) -> bool:
    return _configured_store(db_path).renew_lease(claim, lease_seconds, now_ms)


def lease_owned(
    claim: LeaseClaim,
    now_ms: int,
    *,
    db_path: Path | None = None,
) -> bool:
    return _configured_store(db_path).lease_owned(claim, now_ms)


def release_lease(
    claim: LeaseClaim,
    now_ms: int,
    *,
    db_path: Path | None = None,
) -> bool:
    return _configured_store(db_path).release_lease(claim, now_ms)


def transition_leased(
    claim: LeaseClaim,
    expected_states: Any,
    target_state: str,
    now_ms: int,
    *,
    lease_seconds: int,
    db_path: Path | None = None,
) -> bool:
    return _configured_store(db_path).transition_leased(
        claim,
        expected_states,
        target_state,
        now_ms,
        lease_seconds=lease_seconds,
    )


def start_stage_attempt(
    claim: LeaseClaim,
    stage: str,
    input_sha256: str,
    now_ms: int,
    *,
    db_path: Path | None = None,
) -> dict[str, Any]:
    return _configured_store(db_path).start_stage_attempt(
        claim, stage, input_sha256, now_ms
    )


def finish_stage_attempt(
    claim: LeaseClaim,
    stage_attempt_id: str,
    status: str,
    now_ms: int,
    *,
    error_code: str | None = None,
    error: Any = None,
    db_path: Path | None = None,
) -> dict[str, Any]:
    return _configured_store(db_path).finish_stage_attempt(
        claim,
        stage_attempt_id,
        status,
        now_ms,
        error_code=error_code,
        error=error,
    )


def save_checkpoint(
    claim: LeaseClaim,
    stage_attempt_id: str,
    input_sha256: str,
    output: Any,
    now_ms: int,
    *,
    db_path: Path | None = None,
) -> dict[str, Any]:
    return _configured_store(db_path).save_checkpoint(
        claim, stage_attempt_id, input_sha256, output, now_ms
    )


def get_checkpoint_for_claim(
    claim: LeaseClaim,
    stage: str,
    input_sha256: str,
    now_ms: int,
    *,
    db_path: Path | None = None,
) -> dict[str, Any] | None:
    return _configured_store(db_path).get_checkpoint_for_claim(
        claim, stage, input_sha256, now_ms
    )


def record_provider_intent(
    claim: LeaseClaim,
    stage: str,
    stage_attempt_id: str,
    provider: str,
    capability: str,
    operation_key: str,
    request_sha256: str,
    now_ms: int,
    *,
    db_path: Path | None = None,
) -> dict[str, Any]:
    return _configured_store(db_path).record_provider_intent(
        claim,
        stage,
        stage_attempt_id,
        provider,
        capability,
        operation_key,
        request_sha256,
        now_ms,
    )


def get_provider_task_for_claim(
    claim: LeaseClaim,
    operation_key: str,
    now_ms: int,
    *,
    db_path: Path | None = None,
) -> dict[str, Any] | None:
    return _configured_store(db_path).get_provider_task_for_claim(
        claim, operation_key, now_ms
    )


def bind_provider_result(
    claim: LeaseClaim,
    operation_key: str,
    external_id: str,
    status: str,
    result: Any,
    now_ms: int,
    *,
    db_path: Path | None = None,
) -> dict[str, Any]:
    return _configured_store(db_path).bind_provider_result(
        claim, operation_key, external_id, status, result, now_ms
    )


def close_running_attempts(
    claim: LeaseClaim,
    now_ms: int,
    *,
    db_path: Path | None = None,
) -> int:
    return _configured_store(db_path).close_running_attempts(claim, now_ms)
