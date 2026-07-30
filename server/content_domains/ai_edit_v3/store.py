"""Isolated SQLite persistence boundary for AI Edit V3."""

from __future__ import annotations

import base64
import binascii
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
from contextlib import suppress
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path, PureWindowsPath
from typing import Any, TypeVar
from urllib.parse import quote

from .contracts import (
    ALLOWED_TRANSITIONS,
    ContractError,
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
            source_kind TEXT NOT NULL CHECK(length(source_kind)>0),
            source_job_id TEXT,
            cos_key TEXT NOT NULL UNIQUE CHECK(length(cos_key)>0 AND instr(cos_key,'://')=0),
            mime_type TEXT NOT NULL CHECK(length(mime_type)>0),
            size_bytes INTEGER NOT NULL CHECK(size_bytes>=0),
            sha256 TEXT NOT NULL CHECK(length(sha256)=64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
            metadata_json TEXT NOT NULL,
            created_at INTEGER NOT NULL,
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
    return resolved, metadata


def resolve_db_path(value: str | os.PathLike[str] | None = None) -> Path:
    """Resolve an explicit local absolute V3 database path without creating it."""

    configured: str | os.PathLike[str] | None = value
    if configured is None:
        configured = os.environ.get("AI_EDIT_V3_DB_PATH")
    if configured is None:
        raise _configuration_error(
            "v3_db_path_required",
            "AI_EDIT_V3_DB_PATH is required",
        )
    path = _absolute_path(configured, role="v3")
    _assert_no_reparse_components(path, role="v3")
    resolved, _metadata = _candidate_identity(path, role="v3")
    return resolved


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


def _assert_local_filesystem(path: Path) -> None:
    candidates = [path.parent]
    if os.path.lexists(path):
        candidates.append(path)
    for candidate in candidates:
        fs_type = _filesystem_type_for_path(candidate)
        classification = _classify_filesystem_type(fs_type)
        if classification == "remote":
            raise _configuration_error(
                "v3_db_network_filesystem",
                f"V3 database may not use network filesystem {fs_type}",
            )
        if classification != "local":
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


class _GuardedConnection(sqlite3.Connection):
    _identity_guard: _GuardBundle | None = None

    def close(self) -> None:
        guard = getattr(self, "_identity_guard", None)
        if guard is None:
            super().close()
            return
        with _SQLITE_OPEN_LOCK:
            try:
                super().close()
            finally:
                self._identity_guard = None
                guard.release()


class _WindowsGuardBundle(_GuardBundle):
    def __init__(self, handles: list[int], leaf_identity: tuple[int, int]):
        self.handles = handles
        self.leaf_identity = leaf_identity

    def release(self) -> None:
        if not self.handles:
            return
        import ctypes
        from ctypes import wintypes

        close_handle = ctypes.windll.kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL
        while self.handles:
            close_handle(wintypes.HANDLE(self.handles.pop()))


class _LinuxGuardBundle(_GuardBundle):
    def __init__(
        self,
        parent_fd: int,
        leaf_fd: int,
        leaf_identity: tuple[int, int],
    ):
        self.parent_fd = parent_fd
        self.leaf_fd = leaf_fd
        self.leaf_identity = leaf_identity

    def release(self) -> None:
        for attribute in ("leaf_fd", "parent_fd"):
            descriptor = getattr(self, attribute, -1)
            if descriptor >= 0:
                with suppress(OSError):
                    os.close(descriptor)
                setattr(self, attribute, -1)


def _resolve_v2_path(v2_db_path: Path | None) -> Path:
    configured: str | os.PathLike[str] | None = v2_db_path
    if configured is None:
        configured = os.environ.get("AI_EDIT_V2_DB")
    if configured is None:
        raise _configuration_error(
            "v2_db_path_required",
            "an explicit absolute V2 database path is required for isolation",
        )
    path = _absolute_path(configured, role="v2")
    _assert_no_reparse_components(path, role="v2")
    resolved, _metadata = _candidate_identity(path, role="v2")
    return resolved


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


def _open_windows_guard(path: Path, v2_path: Path) -> _WindowsGuardBundle:
    handles: list[int] = []
    try:
        current = Path(path.anchor)
        handles.append(_windows_create_handle(current, directory=True))
        for part in path.parent.parts[1:]:
            current /= part
            handles.append(_windows_create_handle(current, directory=True))
        existed = os.path.lexists(path)
        try:
            leaf_handle = _windows_create_handle(
                path,
                directory=False,
                create_new=not existed,
                writable=True,
            )
        except FileExistsError:
            if existed:
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
        if os.path.lexists(v2_path):
            v2_handle = _windows_create_handle(v2_path, directory=False)
            handles.append(v2_handle)
            v2_identity, _attributes, _links = _windows_handle_identity(v2_handle)
            if leaf_identity == v2_identity:
                raise _configuration_error(
                    "v2_v3_db_same_file",
                    "V2 and V3 database files share one filesystem identity",
                )
        return _WindowsGuardBundle(handles, leaf_identity)
    except Exception:
        _WindowsGuardBundle(handles, (0, 0)).release()
        raise


def _open_linux_parent(path: Path) -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path.anchor or "/", flags)
    try:
        for part in path.parent.parts[1:]:
            next_descriptor = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        metadata = os.fstat(descriptor)
        if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022:
            raise _configuration_error(
                "v3_db_identity_unprovable",
                "V3 database parent must be owned by the service user and not writable by others",
            )
        return descriptor
    except Exception:
        with suppress(OSError):
            os.close(descriptor)
        raise


def _open_linux_guard(path: Path, v2_path: Path) -> _LinuxGuardBundle:
    parent_fd = _open_linux_parent(path)
    leaf_fd = -1
    try:
        flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        try:
            leaf_fd = os.open(path.name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent_fd)
        except FileExistsError:
            leaf_fd = os.open(path.name, flags, dir_fd=parent_fd)
        metadata = os.fstat(leaf_fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise _configuration_error(
                "v3_db_identity_unprovable",
                "V3 database leaf does not have one stable ordinary-file identity",
            )
        leaf_identity = (metadata.st_dev, metadata.st_ino)
        if os.path.lexists(v2_path):
            v2_metadata = os.stat(v2_path, follow_symlinks=False)
            if leaf_identity == (v2_metadata.st_dev, v2_metadata.st_ino):
                raise _configuration_error(
                    "v2_v3_db_same_file",
                    "V2 and V3 database files share one filesystem identity",
                )
        return _LinuxGuardBundle(parent_fd, leaf_fd, leaf_identity)
    except Exception:
        _LinuxGuardBundle(parent_fd, leaf_fd, (0, 0)).release()
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


def _linux_fd_snapshot() -> set[int]:
    try:
        return {int(value) for value in os.listdir("/proc/self/fd")}
    except (OSError, ValueError) as exc:
        raise _configuration_error(
            "v3_db_identity_unprovable",
            "Linux SQLite descriptor identity cannot be inspected",
        ) from exc


def _connect_with_verified_identity(
    path: Path,
    v2_path: Path,
) -> _GuardedConnection:
    with _SQLITE_OPEN_LOCK:
        return _connect_with_verified_identity_under_lock(path, v2_path)


def _connect_with_verified_identity_under_lock(
    path: Path,
    v2_path: Path,
) -> _GuardedConnection:
    if os.name == "nt":
        guard: _GuardBundle = _open_windows_guard(path, v2_path)
        connect_target: str | Path = path
        connect_kwargs: dict[str, Any] = {}
        before_descriptors: set[int] | None = None
    elif sys.platform.startswith("linux"):
        linux_guard = _open_linux_guard(path, v2_path)
        guard = linux_guard
        connect_target = (
            f"file:/proc/self/fd/{linux_guard.parent_fd}/{quote(path.name, safe='')}"
            "?mode=rw&cache=private"
        )
        connect_kwargs = {"uri": True}
        before_descriptors = _linux_fd_snapshot()
    else:
        raise _configuration_error(
            "v3_db_identity_unprovable",
            "this platform cannot prove the SQLite main-file identity",
        )

    connection: sqlite3.Connection | None = None
    try:
        with _SQLITE_OPEN_LOCK:
            try:
                connection = sqlite3.connect(
                    connect_target,
                    timeout=10.0,
                    isolation_level=None,
                    factory=_GuardedConnection,
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
            main_path = _main_database_path(connection)
            if os.name == "nt":
                if not _same_path(main_path.resolve(strict=True), path.resolve(strict=True)):
                    raise _configuration_error(
                        "v3_db_main_handle_mismatch",
                        "SQLite main database is not the requested V3 file",
                    )
                main_handle = _windows_create_handle(main_path, directory=False)
                try:
                    main_identity, _attributes, _links = _windows_handle_identity(main_handle)
                finally:
                    _WindowsGuardBundle([main_handle], (0, 0)).release()
                if main_identity != guard.leaf_identity:
                    raise _configuration_error(
                        "v3_db_main_handle_mismatch",
                        "SQLite main handle does not match the guarded V3 leaf",
                    )
            else:
                assert before_descriptors is not None
                after_descriptors = _linux_fd_snapshot()
                matches: list[int] = []
                for descriptor in after_descriptors - before_descriptors:
                    try:
                        metadata = os.fstat(descriptor)
                    except OSError:
                        continue
                    if stat.S_ISREG(metadata.st_mode) and (
                        metadata.st_dev,
                        metadata.st_ino,
                    ) == guard.leaf_identity:
                        matches.append(descriptor)
                if len(matches) != 1:
                    raise _configuration_error(
                        "v3_db_main_handle_mismatch",
                        "SQLite main descriptor does not uniquely match the guarded V3 leaf",
                    )
            connection._identity_guard = guard
            return connection
    except Exception:
        if connection is not None:
            connection.close()
        guard.release()
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

    path = resolve_db_path(db_path)
    configured_v2 = _resolve_v2_path(v2_db_path)
    assert_isolated_db(path, configured_v2)
    _assert_local_filesystem(path)
    connection = _connect_with_verified_identity(path, configured_v2)
    try:
        connection.row_factory = sqlite3.Row
        _register_connection_functions(connection)
        _negotiate_wal(connection)
        connection.execute("PRAGMA foreign_keys=ON")
        if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise StoreMigrationError(
                "v3_foreign_keys_unavailable",
                "SQLite foreign key enforcement could not be enabled",
            )
        return connection
    except Exception:
        connection.close()
        raise


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

    path = resolve_db_path(db_path)
    configured_v2: Path | None = v2_db_path
    if configured_v2 is None:
        raw_v2 = os.environ.get("AI_EDIT_V2_DB")
        configured_v2 = Path(raw_v2) if raw_v2 else None
    if configured_v2 is None:
        raise _configuration_error(
            "v2_db_path_required",
            "an explicit absolute V2 database path is required for isolation",
        )
    assert_isolated_db(path, configured_v2)
    _assert_local_filesystem(path)
    before = _path_identity(path)
    connection = open_store(path, v2_db_path=configured_v2)
    try:
        _revalidate_open_identity(path, before, configured_v2)
        _migrate_or_validate(connection)
    finally:
        connection.close()


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
        configured_v2 = v2_db_path
        if configured_v2 is None:
            raw_v2 = os.environ.get("AI_EDIT_V2_DB")
            configured_v2 = Path(raw_v2) if raw_v2 else None
        if configured_v2 is None:
            raise _configuration_error(
                "v2_db_path_required",
                "an explicit absolute V2 database path is required for isolation",
            )
        self.v2_db_path = _absolute_path(configured_v2, role="v2")
        init_db(db_path, v2_db_path=self.v2_db_path)
        self.db_path = resolve_db_path(db_path)

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
        assert_isolated_db(self.db_path, self.v2_db_path)
        _assert_local_filesystem(self.db_path)
        before = _path_identity(self.db_path)
        connection = open_store(self.db_path, v2_db_path=self.v2_db_path)
        try:
            _revalidate_open_identity(self.db_path, before, self.v2_db_path)
            return connection
        except Exception:
            connection.close()
            raise

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
                "SELECT * FROM edit_v3_quotes WHERE quote_id=?",
                (quote_id,),
            ).fetchone()
            if existing is not None:
                if existing["environment"] != environment or existing["owner_id"] != owner_id:
                    return None
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
                raise StoreConflictError(
                    "quote_invalid",
                    "quote violates frozen pricing, template, or value constraints",
                ) from exc
            return dict(
                connection.execute(
                    "SELECT * FROM edit_v3_quotes WHERE quote_id=?",
                    (quote_id,),
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
                "SELECT * FROM edit_v3_uploads WHERE upload_id=?",
                (upload_id,),
            ).fetchone()
            if existing is not None:
                if existing["environment"] != environment or existing["owner_id"] != owner_id:
                    return None
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
                raise StoreConflictError(
                    "upload_identity_conflict",
                    "upload ID or object key is already bound",
                ) from exc
            return dict(
                connection.execute(
                    "SELECT * FROM edit_v3_uploads WHERE upload_id=?",
                    (upload_id,),
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
            "completed_at": completed_at,
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
            if source_kind == "uploaded":
                upload = connection.execute(
                    "SELECT * FROM edit_v3_uploads WHERE upload_id=?",
                    (upload_id,),
                ).fetchone()
                if upload is None or (
                    upload["environment"] != environment
                    or upload["owner_id"] != owner_id
                ):
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
                    "SELECT environment,owner_id FROM edit_v3_jobs WHERE job_id=?",
                    (source_job_id,),
                ).fetchone()
                if source_job is None or (
                    source_job["environment"] != environment
                    or source_job["owner_id"] != owner_id
                ):
                    return None

            existing = connection.execute(
                "SELECT * FROM edit_v3_materials WHERE material_id=?",
                (material_id,),
            ).fetchone()
            if existing is not None:
                if existing["environment"] != environment or existing["owner_id"] != owner_id:
                    return None
                if not self._same_values(existing, expected):
                    raise _immutable_conflict(f"material:{material_id}")
                return dict(existing)

            if source_kind == "uploaded":
                upload_replay = connection.execute(
                    "SELECT * FROM edit_v3_materials WHERE upload_id=?",
                    (upload_id,),
                ).fetchone()
                if upload_replay is not None:
                    if (
                        upload_replay["environment"] != environment
                        or upload_replay["owner_id"] != owner_id
                    ):
                        return None
                    replay_expected = {
                        key: value
                        for key, value in expected.items()
                        if key != "material_id"
                    }
                    if not self._same_values(upload_replay, replay_expected):
                        raise _immutable_conflict(f"material-upload:{upload_id}")
                    return dict(upload_replay)
            try:
                connection.execute(
                    """INSERT INTO edit_v3_materials(
                           material_id,environment,owner_id,upload_id,source_kind,source_job_id,
                           cos_key,mime_type,size_bytes,sha256,metadata_json,created_at
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    tuple(expected.values()),
                )
            except sqlite3.IntegrityError as exc:
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
                    """SELECT * FROM edit_v3_job_materials
                       WHERE job_id=? AND material_id=?""",
                    (job_id, material_id),
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
                    """SELECT * FROM edit_v3_job_materials
                       WHERE job_id=? ORDER BY ordinal,material_id""",
                    (job_id,),
                )
            ]

        return self._write(write)

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
