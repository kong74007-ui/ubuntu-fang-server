from __future__ import annotations

import argparse
from contextlib import contextmanager
import errno
import getpass
import hashlib
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping, Protocol, Sequence

from jsonschema import Draft202012Validator

from server.content_domains.ai_edit_v3.acceptance_export import (
    AcceptanceConfig,
    RunManifest,
    TestSession,
    build_blind_review_package,
    collect_case_evidence,
    run_cases,
    verify_case_evidence,
    write_json_exclusive,
    load_test_session,
)
from server.content_domains.ai_edit_v3.acceptance_verify import (
    CaseEvidence as MachineCaseEvidence,
    load_quality_evidence,
    probe_final_output,
    verify_quality_evidence,
)
from server.content_domains.ai_edit_v3.contracts import (
    normalize_job_request,
    request_fingerprint,
)


INPUT_TYPES = (
    "platform_talking_head",
    "uploaded_video",
    "existing_audio",
    "uploaded_audio",
    "script_to_audio_video",
)
CREATION_MODES = ("ai_auto", "style_prompt", "template_reference")
VIDEO_INPUTS = frozenset({"platform_talking_head", "uploaded_video"})
RATIOS = frozenset({"16:9", "9:16"})
TEMPLATES = {
    "commercial-diagnostic-landscape-v1": "16:9",
    "commercial-diagnostic-portrait-v1": "9:16",
    "editorial-explainer-landscape-v1": "16:9",
    "editorial-explainer-portrait-v1": "9:16",
}
CATEGORIES = frozenset({"commercial", "knowledge", "product", "franchise", "store"})
REQUIRED_RISKS = frozenset({
    "no_images",
    "complete_images",
    "incomplete_images",
    "ratio_mismatch",
    "unrelated_person_material",
})
SAFE_ALIAS = re.compile(r"^[a-z0-9][a-z0-9_./-]{2,127}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
SECRET_LIKE = re.compile(r"(?:sk[-_][a-z0-9_-]{8,}|api[_-]?key|token=|secret=)", re.I)


@dataclass(frozen=True)
class MatrixReport:
    passed: bool
    case_count: int
    duplicate_case_ids: tuple[str, ...]
    missing_pairs: tuple[tuple[str, str], ...]
    errors: tuple[str, ...] = ()


def _unsafe_alias(value: Any) -> bool:
    if not isinstance(value, str):
        return True
    normalized = value.replace("\\", "/")
    return (
        not SAFE_ALIAS.fullmatch(value)
        or bool(re.match(r"^[a-z]:/", normalized, re.I))
        or normalized.startswith("/")
        or "://" in normalized
        or "?" in normalized
        or "#" in normalized
        or ".." in normalized.split("/")
        or bool(SECRET_LIKE.search(normalized))
    )


def _validate_binding(case_id: str, label: str, binding: Any, errors: list[str]) -> None:
    if not isinstance(binding, dict):
        errors.append(f"{case_id}:{label}:invalid")
        return
    for field in ("alias", "owner_alias", "authorization_ref"):
        if _unsafe_alias(binding.get(field)):
            errors.append(f"{case_id}:{label}.{field}:unsafe")
    if not SHA256.fullmatch(str(binding.get("sha256", ""))):
        errors.append(f"{case_id}:{label}.sha256:invalid")


def _binding_has_ratio(binding: Any, ratio: str) -> bool:
    return isinstance(binding, dict) and binding.get("intrinsic_ratio") == ratio


def validate_matrix(path: Path) -> MatrixReport:
    document = json.loads(path.read_text(encoding="utf-8"))
    errors: list[str] = []
    schema_path = path.with_name("acceptance-20.schema.json")
    if schema_path.exists():
        try:
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            Draft202012Validator.check_schema(schema)
        except Exception:
            errors.append("schema:invalid")
        else:
            for issue in Draft202012Validator(schema).iter_errors(document):
                location = ".".join(str(part) for part in issue.absolute_path) or "document"
                errors.append(f"schema:{location}:{issue.validator}")
    else:
        errors.append("schema:missing")

    cases = document.get("cases", []) if isinstance(document, dict) else []
    if not isinstance(cases, list):
        cases = []
        errors.append("document:cases:invalid")
    case_ids = [str(case.get("case_id", "")) for case in cases if isinstance(case, dict)]
    duplicate_case_ids = tuple(sorted({
        case_id for case_id in case_ids if case_ids.count(case_id) > 1
    }))
    observed_pairs = {
        (str(case.get("input_type")), str(case.get("creation_mode")))
        for case in cases if isinstance(case, dict)
    }
    missing_pairs = tuple(
        (input_type, mode)
        for input_type in INPUT_TYPES
        for mode in CREATION_MODES
        if (input_type, mode) not in observed_pairs
    )

    if len(cases) != 20:
        errors.append("matrix:case_count:expected_20")
    if duplicate_case_ids:
        errors.append("matrix:case_ids:duplicate")

    root_authorization = document.get("authorization_ref") if isinstance(document, dict) else None
    if _unsafe_alias(root_authorization):
        errors.append("document:authorization_ref:unsafe")
    template_ratios = {"16:9": 0, "9:16": 0}
    used_templates: set[str] = set()
    video_count = 0
    risk_tags: set[str] = set()
    categories: set[str] = set()
    for index, case in enumerate(cases, start=1):
        if not isinstance(case, dict):
            errors.append(f"case_{index:02d}:invalid")
            continue
        case_id = str(case.get("case_id", f"case_{index:02d}"))
        input_type = str(case.get("input_type", ""))
        mode = str(case.get("creation_mode", ""))
        ratio = str(case.get("ratio", ""))
        video_count += input_type in VIDEO_INPUTS
        categories.add(str(case.get("content_category", "")))
        risk_tags.update(str(tag) for tag in case.get("risk_tags", []) if isinstance(tag, str))
        _validate_binding(case_id, "source", case.get("source"), errors)
        source = case.get("source") if isinstance(case.get("source"), dict) else {}
        materials = case.get("materials", []) if isinstance(case.get("materials"), list) else []
        for material_index, material in enumerate(case.get("materials", []), start=1):
            _validate_binding(case_id, f"materials[{material_index}]", material, errors)
            if isinstance(material, dict) and not str(material.get("media_type", "")).startswith("image/"):
                errors.append(f"{case_id}:materials[{material_index}].media_type:not_image")
        if _unsafe_alias(case.get("authorization_ref")):
            errors.append(f"{case_id}:authorization_ref:unsafe")
        authorizations = [case.get("authorization_ref"), source.get("authorization_ref")]
        authorizations.extend(
            material.get("authorization_ref") for material in materials if isinstance(material, dict)
        )
        if any(value != root_authorization for value in authorizations):
            errors.append(f"{case_id}:authorization_ref:chain_mismatch")
        for material_index, material in enumerate(materials, start=1):
            if (
                isinstance(material, dict)
                and material.get("owner_alias") != source.get("owner_alias")
            ):
                errors.append(f"{case_id}:materials[{material_index}].owner_alias:chain_mismatch")
        expected_media = {
            "platform_talking_head": ("video/mp4",),
            "uploaded_video": ("video/mp4",),
            "existing_audio": ("audio/mpeg", "audio/wav"),
            "uploaded_audio": ("audio/mpeg", "audio/wav"),
            "script_to_audio_video": ("text/plain",),
        }.get(input_type)
        if not expected_media or source.get("media_type") not in expected_media:
            errors.append(f"{case_id}:source.media_type:input_mismatch")
        if expected_media == ("video/mp4",) and source.get("intrinsic_ratio") not in RATIOS:
            errors.append(f"{case_id}:source.intrinsic_ratio:required")
        if expected_media == ("video/mp4",):
            for field in ("talking_head_kind", "subject_alias", "background_alias"):
                if _unsafe_alias(case.get(field)):
                    errors.append(f"{case_id}:{field}:required")
        tags = set(case.get("risk_tags", []))
        if "no_images" in tags and materials:
            errors.append(f"{case_id}:risk_tags:no_images_unproven")
        if "complete_images" in tags and (
            not materials
            or any(material.get("semantic_status") != "matched" for material in materials if isinstance(material, dict))
        ):
            errors.append(f"{case_id}:risk_tags:complete_images_unproven")
        if "incomplete_images" in tags and (
            not materials
            or not any(material.get("semantic_status") == "partial" for material in materials if isinstance(material, dict))
        ):
            errors.append(f"{case_id}:risk_tags:incomplete_images_unproven")
        visual_bindings = [source] if source.get("media_type") == "video/mp4" else []
        visual_bindings.extend(material for material in materials if isinstance(material, dict))
        if "ratio_mismatch" in tags and not any(
            binding.get("intrinsic_ratio") in RATIOS
            and not _binding_has_ratio(binding, ratio)
            for binding in visual_bindings
        ):
            errors.append(f"{case_id}:risk_tags:ratio_mismatch_unproven")
        unrelated_people = [
            material for material in materials
            if isinstance(material, dict)
            and material.get("contains_person") is True
            and material.get("person_role") == "unrelated_person"
        ]
        if "unrelated_person_material" in tags and not unrelated_people:
            errors.append(f"{case_id}:risk_tags:unrelated_person_material_unproven")
        if unrelated_people and "unrelated_person_material" not in tags:
            errors.append(f"{case_id}:risk_tags:unrelated_person_material_unlabeled")
        template_id = case.get("template_id")
        if mode == "template_reference":
            if template_id not in TEMPLATES or TEMPLATES.get(template_id) != ratio:
                errors.append(f"{case_id}:template_id:invalid_for_ratio")
            else:
                used_templates.add(str(template_id))
                template_ratios[ratio] += 1
        elif template_id is not None:
            errors.append(f"{case_id}:template_id:unexpected")
        if mode == "style_prompt" and not str(case.get("style_prompt") or "").strip():
            errors.append(f"{case_id}:style_prompt:required")
        if mode != "style_prompt" and case.get("style_prompt") is not None:
            errors.append(f"{case_id}:style_prompt:unexpected")

    if video_count != 10:
        errors.append("matrix:input_balance:expected_10_video_10_audio")
    if set(TEMPLATES) != used_templates:
        errors.append("matrix:templates:all_published_required")
    if any(template_ratios[ratio] < 3 for ratio in RATIOS):
        errors.append("matrix:template_ratios:minimum_3_each")
    if not CATEGORIES.issubset(categories):
        errors.append("matrix:content_categories:incomplete")
    if not REQUIRED_RISKS.issubset(risk_tags):
        errors.append("matrix:risk_coverage:incomplete")
    video_cases = [
        case for case in cases
        if isinstance(case, dict) and case.get("input_type") in VIDEO_INPUTS
    ]
    if len({case.get("subject_alias") for case in video_cases}) < 5:
        errors.append("matrix:talking_heads:subject_diversity_below_5")
    if len({case.get("background_alias") for case in video_cases}) < 5:
        errors.append("matrix:talking_heads:background_diversity_below_5")
    duration_buckets = {
        "short" if case.get("source", {}).get("duration_ms", 0) < 20000
        else "medium" if case.get("source", {}).get("duration_ms", 0) <= 40000
        else "long"
        for case in video_cases
    }
    if duration_buckets != {"short", "medium", "long"}:
        errors.append("matrix:talking_heads:duration_diversity_incomplete")
    if not CATEGORIES.issubset({str(case.get("content_category")) for case in video_cases}):
        errors.append("matrix:talking_heads:content_diversity_incomplete")

    unique_errors = tuple(dict.fromkeys(errors))
    return MatrixReport(
        passed=not unique_errors and not missing_pairs,
        case_count=len(cases),
        duplicate_case_ids=duplicate_case_ids,
        missing_pairs=missing_pairs,
        errors=unique_errors,
    )


def execute_local_fake_run(args: argparse.Namespace) -> int:
    try:
        matrix_report = validate_matrix(args.matrix)
    except (OSError, ValueError, json.JSONDecodeError):
        return 4
    if not matrix_report.passed or args.concurrency < 1 or args.concurrency > 10:
        return 4
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,63}", args.run_id):
        return 4
    output_root = Path(os.environ.get(
        "AI_EDIT_V3_ACCEPTANCE_OUTPUT_ROOT",
        ".artifacts/ai-edit-v3/acceptance",
    ))
    run_dir = output_root / args.run_id
    try:
        document = json.loads(args.matrix.read_text(encoding="utf-8"))
        cases = list(document["cases"])
        if args.subset == "parallel-5":
            cases = cases[:5]
        elif args.subset == "stress-10":
            cases = cases[:10]
        root = Path(__file__).resolve().parents[1]
        fixture_name = os.environ.get("AI_EDIT_V3_ACCEPTANCE_FAKE_RESPONSE", "completed.json")
        if fixture_name not in {
            "completed.json", "refunded.json", "prehold_absent.json",
            "failed_reconciliation_pending.json", "failed_asset_decision_pending.json",
        }:
            return 4
        fixture = json.loads((
            root / "tests/fixtures/ai_edit_v3/acceptance-responses" / fixture_name
        ).read_text(encoding="utf-8"))
        manifest = {
            "version": "1.0",
            "run_id": args.run_id,
            "environment": "local-fake",
            "commit_sha": _git_commit(root),
            "matrix_sha256": _sha256_file(args.matrix),
            "schema_sha256": _sha256_file(args.matrix.with_name("acceptance-20.schema.json")),
            "template_registry_sha256": _sha256_file(
                root / "server/content_domains/ai_edit_v3/catalog/templates-v1.json"
            ),
            "renderer_release_sha256": _sha256_file(
                root / "server/ai_edit_v3_renderer/renderer-release.lock.json"
            ),
            "concurrency": args.concurrency,
            "subset": args.subset,
            "case_ids": [case["case_id"] for case in cases],
            "case_request_sha256": {
                case["case_id"]: case["source"]["sha256"] for case in cases
            },
        }
        manifest_path = run_dir / "run-manifest.json"
        report_path = run_dir / "report.json"
        if run_dir.exists():
            if report_path.exists():
                return 4
            persisted_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if persisted_manifest != manifest:
                return 4
        else:
            run_dir.mkdir(parents=True, exist_ok=False)
            write_json_exclusive(manifest_path, manifest)
        created_idempotency_keys: set[str] = set()

        def api_factory(case: Mapping[str, Any]) -> _LocalFixtureApi:
            response = json.loads(json.dumps(fixture))
            case_id = str(case["case_id"])
            response["job_id"] = f"job-{args.run_id}-{case_id}"
            response["attempt_id"] = f"attempt-{args.run_id}-{case_id}"
            response["normalized_request_sha256"] = case["source"]["sha256"]
            response["quote"]["quote_id"] = f"quote-{args.run_id}-{case_id}"
            response["asset_id"] = f"asset-{args.run_id}-{case_id}"
            response["stable_cos_key"] = f"acceptance/{args.run_id}/{case_id}/final.mp4"
            return _LocalFixtureApi(case_id, response, created_idempotency_keys)

        summary = run_cases(
            AcceptanceConfig(args.run_id, run_dir, api_factory),
            RunManifest(tuple(cases)),
            concurrency=args.concurrency,
        )
        if summary.result_code == 4:
            return 4
        case_results = list(summary.case_results)
        statuses = [item["status"] for item in case_results]
        completed = all(status == "completed" for status in statuses)
        report = {
            "version": "1.0",
            "run_id": args.run_id,
            "environment": "local-fake",
            "status": "completed" if completed else "failed",
            "case_count": len(cases),
            "case_statuses": case_results,
            "manifest_sha256": _sha256_file(run_dir / "run-manifest.json"),
        }
        write_json_exclusive(report_path, report)
        return summary.result_code
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return 4


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        write_json_exclusive(temporary, payload)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _aggregate_file_lock(
    path: Path, *, timeout_seconds: float = 10.0, poll_seconds: float = 0.05,
):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError("acceptance_aggregate_lock_timeout") from exc
                time.sleep(poll_seconds)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _git_commit(root: Path) -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


class _LocalFixtureApi:
    def __init__(
        self,
        case_id: str,
        response: Mapping[str, Any],
        created_idempotency_keys: set[str],
    ) -> None:
        self.case_id = case_id
        self.response = dict(response)
        self.created_idempotency_keys = created_idempotency_keys

    def upload_source(self, case: Mapping[str, Any]) -> Mapping[str, Any]:
        return {
            "upload_id": f"upload-{self.case_id}",
            "owner_alias": case["source"]["owner_alias"],
        }

    def quote(self, case: Mapping[str, Any], upload: Mapping[str, Any]) -> Mapping[str, Any]:
        if upload.get("owner_alias") != case["source"]["owner_alias"]:
            raise ValueError("owner_scope_mismatch")
        return dict(self.response["quote"])

    def create_job(self, idempotency_key: str) -> str:
        if not idempotency_key.endswith(f":{self.case_id}"):
            raise ValueError("idempotency_key_mismatch")
        if idempotency_key in self.created_idempotency_keys:
            raise ValueError("duplicate_idempotency_key")
        self.created_idempotency_keys.add(idempotency_key)
        return str(self.response["job_id"])

    def get_job(self, job_id: str) -> dict[str, str]:
        status = str(self.response["status"])
        if (
            status == "failed"
            and self.response.get("settlement", {}).get("state") == "prehold_absent"
        ):
            status = "prehold_absent"
        return {"job_id": job_id, "status": status}

    def get_result(self, job_id: str) -> Mapping[str, Any]:
        return json.loads(json.dumps(self.response))

    def verify_range(self, playback_url: str) -> bool:
        return playback_url.startswith("https://playback.invalid/")


class RealRunUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class AcceptanceBindings:
    owners: Mapping[str, TestSession]
    cases: Mapping[str, Mapping[str, Any]]


def load_authorized_bindings(
    matrix_path: Path,
    bindings_path: Path,
    *,
    environment: Mapping[str, str],
    authorization_ref: str,
    subset: str | None = None,
) -> AcceptanceBindings:
    """Load strict owner-scoped authority without persisting session values."""

    try:
        matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
        bindings = json.loads(bindings_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RealRunUnavailable("asset_bindings_invalid") from exc
    if (
        not isinstance(matrix, Mapping)
        or matrix.get("authorization_ref") != authorization_ref
        or not isinstance(matrix.get("cases"), list)
        or not isinstance(bindings, Mapping)
        or set(bindings) != {"version", "owners", "cases"}
        or bindings.get("version") != "2.0"
        or not isinstance(bindings.get("owners"), list)
        or not isinstance(bindings.get("cases"), list)
    ):
        raise RealRunUnavailable("asset_bindings_invalid")
    matrix_cases = list(matrix["cases"])
    if subset == "parallel-5":
        matrix_cases = matrix_cases[:5]
    elif subset == "stress-10":
        matrix_cases = matrix_cases[:10]
    elif subset is not None:
        raise RealRunUnavailable("acceptance_subset_invalid")

    owners: dict[str, TestSession] = {}
    for raw in bindings["owners"]:
        if (
            not isinstance(raw, Mapping)
            or set(raw) != {"owner_alias", "session_env"}
            or not SAFE_ALIAS.fullmatch(str(raw.get("owner_alias", "")))
            or not re.fullmatch(
                r"AI_EDIT_V3_TEST_SESSION_[A-Z0-9_]{1,64}",
                str(raw.get("session_env", "")),
            )
            or raw["owner_alias"] in owners
        ):
            raise RealRunUnavailable("owner_binding_invalid")
        secret = environment.get(str(raw["session_env"]), "")
        if not isinstance(secret, str) or not secret.strip():
            raise RealRunUnavailable("test_session_missing")
        owners[str(raw["owner_alias"])] = TestSession(secret)

    raw_cases: dict[str, Mapping[str, Any]] = {}
    for raw in bindings["cases"]:
        if (
            not isinstance(raw, Mapping)
            or set(raw) != {"case_id", "owner_alias", "source", "materials"}
            or not isinstance(raw.get("case_id"), str)
            or raw["case_id"] in raw_cases
        ):
            raise RealRunUnavailable("case_binding_invalid")
        raw_cases[str(raw["case_id"])] = raw
    expected_ids = {
        str(case.get("case_id")) for case in matrix_cases if isinstance(case, Mapping)
    }
    if set(raw_cases) != expected_ids or len(expected_ids) != len(matrix_cases):
        raise RealRunUnavailable("asset_binding_case_set_mismatch")

    normalized: dict[str, Mapping[str, Any]] = {}
    expected_kinds = {
        "uploaded_video": "upload",
        "uploaded_audio": "upload",
        "platform_talking_head": "platform_asset",
        "existing_audio": "audio_asset",
        "script_to_audio_video": "tts",
    }
    for case in matrix_cases:
        if not isinstance(case, Mapping) or not isinstance(case.get("source"), Mapping):
            raise RealRunUnavailable("acceptance_matrix_invalid")
        case_id = str(case["case_id"])
        frozen_source = case["source"]
        raw = raw_cases[case_id]
        owner_alias = raw.get("owner_alias")
        if (
            owner_alias not in owners
            or owner_alias != frozen_source.get("owner_alias")
            or case.get("authorization_ref") != authorization_ref
            or frozen_source.get("authorization_ref") != authorization_ref
        ):
            raise RealRunUnavailable("owner_binding_mismatch")
        source = raw.get("source")
        materials = raw.get("materials")
        if not isinstance(source, Mapping) or not isinstance(materials, list):
            raise RealRunUnavailable("case_binding_invalid")
        kind = expected_kinds.get(str(case.get("input_type")))
        common = {"kind", "alias", "authorization_ref", "sha256"}
        kind_fields = {
            "upload": {"path", "upload_type", "content_type"},
            "platform_asset": {"asset_id"},
            "audio_asset": {"asset_id"},
            "tts": {"text_path", "voice_id"},
        }.get(kind or "")
        if (
            kind_fields is None
            or set(source) != common | kind_fields
            or source.get("kind") != kind
            or source.get("alias") != frozen_source.get("alias")
            or source.get("authorization_ref") != authorization_ref
            or source.get("sha256") != frozen_source.get("sha256")
        ):
            raise RealRunUnavailable("source_binding_invalid")
        if kind in {"upload", "tts"}:
            field = "path" if kind == "upload" else "text_path"
            path = Path(str(source.get(field, "")))
            if (
                not path.is_absolute()
                or not path.is_file()
                or not SHA256.fullmatch(str(source.get("sha256", "")))
                or _sha256_file(path) != source["sha256"]
            ):
                raise RealRunUnavailable("source_binding_invalid")
        if kind == "upload":
            expected_upload = (
                "main_video" if case.get("input_type") == "uploaded_video" else "main_audio"
            )
            if (
                source.get("upload_type") != expected_upload
                or source.get("content_type") != frozen_source.get("media_type")
            ):
                raise RealRunUnavailable("source_binding_invalid")
        elif kind in {"platform_asset", "audio_asset"}:
            if not isinstance(source.get("asset_id"), str) or not source["asset_id"]:
                raise RealRunUnavailable("source_binding_invalid")
        elif kind == "tts":
            if not isinstance(source.get("voice_id"), str) or not source["voice_id"]:
                raise RealRunUnavailable("source_binding_invalid")

        frozen_materials = case.get("materials")
        if not isinstance(frozen_materials, list) or len(materials) != len(frozen_materials):
            raise RealRunUnavailable("material_binding_invalid")
        normalized_materials: list[dict[str, Any]] = []
        for material, frozen in zip(materials, frozen_materials, strict=True):
            fields = {
                "kind", "alias", "owner_alias", "authorization_ref",
                "path", "sha256", "content_type",
            }
            if (
                not isinstance(material, Mapping)
                or not isinstance(frozen, Mapping)
                or set(material) != fields
                or material.get("kind") != "upload"
                or material.get("owner_alias") != owner_alias
            ):
                raise RealRunUnavailable("material_owner_mismatch")
            path = Path(str(material.get("path", "")))
            if (
                material.get("alias") != frozen.get("alias")
                or material.get("authorization_ref") != authorization_ref
                or material.get("authorization_ref") != frozen.get("authorization_ref")
                or material.get("sha256") != frozen.get("sha256")
                or material.get("content_type") != frozen.get("media_type")
                or material.get("content_type") not in {"image/jpeg", "image/png", "image/webp"}
                or not path.is_absolute()
                or not path.is_file()
                or _sha256_file(path) != material.get("sha256")
            ):
                raise RealRunUnavailable("material_binding_invalid")
            normalized_materials.append(dict(material))
        normalized[case_id] = {
            "case_id": case_id,
            "owner_alias": owner_alias,
            "source": dict(source),
            "materials": tuple(normalized_materials),
        }
    return AcceptanceBindings(owners=dict(owners), cases=normalized)


class _FileBody:
    def __init__(self, path: Path, *, chunk_size: int = 1024 * 1024) -> None:
        self.path = path
        self.chunk_size = chunk_size

    def __iter__(self):
        with self.path.open("rb") as handle:
            while chunk := handle.read(self.chunk_size):
                yield chunk


class HttpRealRunApi:
    _MAX_JSON_BYTES = 1024 * 1024

    def __init__(
        self,
        *,
        base_url: str,
        session: Any,
        bindings_path: Path,
        opener: Any | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        parsed = urllib.parse.urlsplit(base_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise RealRunUnavailable("test_base_url_invalid")
        if not isinstance(bindings_path, Path) or not bindings_path.is_file():
            raise RealRunUnavailable("asset_bindings_missing")
        reveal = getattr(session, "reveal", None)
        if not callable(reveal) or not str(reveal()).strip():
            raise RealRunUnavailable("test_session_missing")
        self._origin = urllib.parse.urlunsplit(
            ("https", parsed.netloc, "", "", "")
        ).rstrip("/")
        self._session = session
        self._bindings_path = bindings_path.resolve()
        self._opener = opener or urllib.request.build_opener(
            urllib.request.ProxyHandler({})
        )
        self._environment = dict(os.environ if environment is None else environment)
        self._authorized_uploads: dict[str, dict[str, str]] = {}
        self._authorized_cases: dict[str, dict[str, Any]] = {}

    def __repr__(self) -> str:
        return (
            f"HttpRealRunApi(origin={self._origin!r}, "
            f"session=[REDACTED], bindings_path={self._bindings_path!r})"
        )

    def _json_get(self, path: str) -> Mapping[str, Any]:
        return self._json_request("GET", path, None, expected_statuses=(200,))

    def _json_request(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any] | None,
        *,
        expected_statuses: tuple[int, ...],
        session: TestSession | None = None,
        idempotency_key: str | None = None,
    ) -> Mapping[str, Any]:
        data = None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {(session or self._session).reveal()}",
        }
        if body is not None:
            data = json.dumps(
                dict(body),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if idempotency_key is not None:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,127}", idempotency_key):
                raise RealRunUnavailable("idempotency_key_invalid")
            headers["Idempotency-Key"] = idempotency_key
        request = urllib.request.Request(
            self._origin + path,
            data=data,
            headers=headers,
            method=method,
        )
        try:
            response = self._opener.open(request, timeout=15)
            try:
                status = int(getattr(response, "status", 0))
                if status not in expected_statuses:
                    raise RealRunUnavailable("test_api_http_error")
                raw = response.read(self._MAX_JSON_BYTES + 1)
            finally:
                response.close()
        except RealRunUnavailable:
            raise
        except Exception as exc:
            raise RealRunUnavailable("test_api_unavailable") from exc
        if not isinstance(raw, bytes) or len(raw) > self._MAX_JSON_BYTES:
            raise RealRunUnavailable("capabilities_response_too_large")
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RealRunUnavailable("capabilities_json_invalid") from exc
        if not isinstance(payload, Mapping):
            raise RealRunUnavailable("capabilities_shape_invalid")
        return payload

    def capabilities(self) -> dict[str, object]:
        payload = self._json_get("/api/v3/edit/capabilities")
        acceptance = payload.get("acceptance")
        if not isinstance(acceptance, Mapping):
            raise RealRunUnavailable("acceptance_capabilities_missing")
        return dict(acceptance)

    def upload_authorized_sources(
        self,
        matrix_path: Path | None = None,
        authorization_ref: str = "",
        subset: str | None = None,
    ) -> None:
        if self._authorized_uploads:
            raise RealRunUnavailable("authorized_upload_phase_replayed")
        if matrix_path is None:
            raise RealRunUnavailable("acceptance_matrix_missing")
        try:
            binding_version = json.loads(
                self._bindings_path.read_text(encoding="utf-8")
            ).get("version")
        except (OSError, AttributeError, json.JSONDecodeError) as exc:
            raise RealRunUnavailable("asset_bindings_invalid") from exc
        if binding_version != "2.0":
            raise RealRunUnavailable("asset_bindings_version_unsupported")
        self._prepare_authorized_cases_v2(
            matrix_path,
            authorization_ref=authorization_ref,
            subset=subset,
        )

    def _upload_bound_file(
        self,
        *,
        session: TestSession,
        path: Path,
        upload_type: str,
        content_type: str,
        expected_sha256: str,
    ) -> str:
        created = self._json_request(
            "POST",
            "/api/v3/edit/uploads",
            {
                "upload_type": upload_type,
                "filename": path.name,
                "content_type": content_type,
                "size_bytes": path.stat().st_size,
            },
            expected_statuses=(201,),
            session=session,
        )
        upload_id = created.get("upload_id")
        put_url = created.get("put_url")
        if not isinstance(upload_id, str) or not upload_id:
            raise RealRunUnavailable("upload_id_invalid")
        if not isinstance(put_url, str) or not self._safe_signed_upload_url(put_url):
            raise RealRunUnavailable("put_url_invalid")
        self._put_file(put_url, path, content_type)
        completed = self._json_request(
            "POST",
            f"/api/v3/edit/uploads/{urllib.parse.quote(upload_id, safe='')}/complete",
            {},
            expected_statuses=(200,),
            session=session,
        )
        if (
            completed.get("upload_id") != upload_id
            or completed.get("sha256") != expected_sha256
        ):
            raise RealRunUnavailable("upload_completion_invalid")
        return upload_id

    def _catalog_contains(
        self,
        *,
        session: TestSession,
        path: str,
        identity_field: str,
        identity: str,
    ) -> None:
        payload = self._json_request(
            "GET", path, None, expected_statuses=(200,), session=session
        )
        items = payload.get("items")
        if (
            not isinstance(items, list)
            or not any(
                isinstance(item, Mapping) and item.get(identity_field) == identity
                for item in items
            )
        ):
            raise RealRunUnavailable("catalog_authority_missing")

    def _prepare_authorized_cases_v2(
        self,
        matrix_path: Path,
        *,
        authorization_ref: str,
        subset: str | None,
    ) -> None:
        bindings = load_authorized_bindings(
            matrix_path,
            self._bindings_path,
            environment=self._environment,
            authorization_ref=authorization_ref,
            subset=subset,
        )
        matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
        cases = list(matrix["cases"])
        if subset == "parallel-5":
            cases = cases[:5]
        elif subset == "stress-10":
            cases = cases[:10]
        prepared: dict[str, dict[str, Any]] = {}
        for case in cases:
            case_id = str(case["case_id"])
            binding = bindings.cases[case_id]
            owner_alias = str(binding["owner_alias"])
            session = bindings.owners[owner_alias]
            source = binding["source"]
            source_fields: dict[str, Any]
            if source["kind"] == "upload":
                upload_id = self._upload_bound_file(
                    session=session,
                    path=Path(source["path"]),
                    upload_type=str(source["upload_type"]),
                    content_type=str(source["content_type"]),
                    expected_sha256=str(source["sha256"]),
                )
                source_fields = {"source_upload_id": upload_id}
            elif source["kind"] == "platform_asset":
                self._catalog_contains(
                    session=session,
                    path="/api/v3/edit/platform-assets",
                    identity_field="asset_id",
                    identity=str(source["asset_id"]),
                )
                source_fields = {"source_asset_id": str(source["asset_id"])}
            elif source["kind"] == "audio_asset":
                self._catalog_contains(
                    session=session,
                    path="/api/v3/edit/audio-assets",
                    identity_field="asset_id",
                    identity=str(source["asset_id"]),
                )
                source_fields = {"source_asset_id": str(source["asset_id"])}
            else:
                self._catalog_contains(
                    session=session,
                    path="/api/v3/edit/voices",
                    identity_field="voice_id",
                    identity=str(source["voice_id"]),
                )
                try:
                    text_value = Path(source["text_path"]).read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError) as exc:
                    raise RealRunUnavailable("tts_text_invalid") from exc
                if not text_value.strip() or len(text_value) > 4000:
                    raise RealRunUnavailable("tts_text_invalid")
                source_fields = {
                    "tts_input": {
                        "text": text_value,
                        "voice_id": str(source["voice_id"]),
                    }
                }
            material_ids: list[str] = []
            for material in binding["materials"]:
                upload_id = self._upload_bound_file(
                    session=session,
                    path=Path(material["path"]),
                    upload_type="material_image",
                    content_type=str(material["content_type"]),
                    expected_sha256=str(material["sha256"]),
                )
                created = self._json_request(
                    "POST",
                    "/api/v3/edit/materials",
                    {"upload_id": upload_id},
                    expected_statuses=(201,),
                    session=session,
                )
                material_id = created.get("material_id")
                if (
                    not isinstance(material_id, str)
                    or not material_id
                    or created.get("sha256") != material["sha256"]
                ):
                    raise RealRunUnavailable("material_creation_invalid")
                material_ids.append(material_id)
            prepared[case_id] = {
                "owner_alias": owner_alias,
                "session": session,
                "source_fields": source_fields,
                "material_asset_ids": tuple(material_ids),
            }
        self._authorized_cases = prepared
        self._authorized_uploads = {
            case_id: {
                "upload_id": f"authority-{case_id}",
                "owner_alias": str(value["owner_alias"]),
            }
            for case_id, value in prepared.items()
        }

    def upload_source(self, case: Mapping[str, Any]) -> Mapping[str, Any]:
        case_id = case.get("case_id")
        source = case.get("source")
        if not isinstance(case_id, str) or not isinstance(source, Mapping):
            raise RealRunUnavailable("acceptance_case_invalid")
        uploaded = self._authorized_uploads.get(case_id)
        if uploaded is None or uploaded["owner_alias"] != source.get("owner_alias"):
            raise RealRunUnavailable("authorized_upload_binding_missing")
        return dict(uploaded)

    def for_case(self, case: Mapping[str, Any]) -> "HttpCaseApi":
        case_id = case.get("case_id")
        authority = self._authorized_cases.get(str(case_id))
        if authority is None:
            raise RealRunUnavailable("authorized_case_binding_missing")
        return HttpCaseApi(self, case, authority)

    def expected_request_sha256(self, case: Mapping[str, Any]) -> str:
        return self.for_case(case).expected_request_sha256(case)

    @staticmethod
    def _safe_signed_upload_url(value: str) -> bool:
        parsed = urllib.parse.urlsplit(value)
        return bool(
            parsed.scheme == "https"
            and parsed.hostname
            and parsed.username is None
            and parsed.password is None
            and not parsed.fragment
        )

    def _put_file(self, url: str, path: Path, content_type: str) -> None:
        size = path.stat().st_size
        request = urllib.request.Request(
            url,
            data=_FileBody(path),
            headers={
                "Content-Type": content_type,
                "Content-Length": str(size),
            },
            method="PUT",
        )
        try:
            response = self._opener.open(request, timeout=120)
            try:
                status = int(getattr(response, "status", 0))
                response.read(64 * 1024 + 1)
            finally:
                response.close()
        except Exception as exc:
            raise RealRunUnavailable("authorized_upload_failed") from exc
        if not 200 <= status < 300:
            raise RealRunUnavailable("authorized_upload_failed")

    def _load_authorized_sources(
        self,
        matrix_path: Path,
        *,
        authorization_ref: str,
        subset: str | None,
    ) -> tuple[dict[str, str], ...]:
        try:
            payload = json.loads(self._bindings_path.read_text(encoding="utf-8"))
            matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RealRunUnavailable("asset_bindings_invalid") from exc
        if (
            not isinstance(matrix, Mapping)
            or matrix.get("authorization_ref") != authorization_ref
            or not isinstance(matrix.get("cases"), list)
        ):
            raise RealRunUnavailable("acceptance_authorization_mismatch")
        cases = list(matrix["cases"])
        if subset == "parallel-5":
            cases = cases[:5]
        elif subset == "stress-10":
            cases = cases[:10]
        elif subset is not None:
            raise RealRunUnavailable("acceptance_subset_invalid")
        expected: dict[str, Mapping[str, Any]] = {}
        for case in cases:
            if not isinstance(case, Mapping) or not isinstance(case.get("source"), Mapping):
                raise RealRunUnavailable("acceptance_matrix_invalid")
            case_id = case.get("case_id")
            if not isinstance(case_id, str) or case_id in expected:
                raise RealRunUnavailable("acceptance_matrix_invalid")
            if (
                case.get("authorization_ref") != authorization_ref
                or case["source"].get("authorization_ref") != authorization_ref
            ):
                raise RealRunUnavailable("acceptance_authorization_mismatch")
            if case.get("input_type") in {"uploaded_video", "uploaded_audio"}:
                expected[case_id] = case["source"]
        if (
            not isinstance(payload, Mapping)
            or set(payload) != {"version", "sources"}
            or payload.get("version") != "1.0"
            or not isinstance(payload.get("sources"), list)
            or not 0 <= len(payload["sources"]) <= 20
        ):
            raise RealRunUnavailable("asset_bindings_invalid")
        result: list[dict[str, str]] = []
        seen: set[str] = set()
        fields = {
            "case_id", "alias", "owner_alias", "authorization_ref", "path",
            "sha256", "upload_type", "content_type",
        }
        for raw in payload["sources"]:
            if not isinstance(raw, Mapping) or set(raw) != fields:
                raise RealRunUnavailable("asset_binding_invalid")
            source = {name: raw[name] for name in fields}
            if any(not isinstance(value, str) or not value for value in source.values()):
                raise RealRunUnavailable("asset_binding_invalid")
            case_id = source["case_id"]
            frozen = expected.get(case_id)
            path = Path(source["path"])
            if (
                case_id in seen
                or frozen is None
                or not re.fullmatch(r"case_[0-9]{2}", case_id)
                or not SAFE_ALIAS.fullmatch(source["owner_alias"])
                or source["alias"] != frozen.get("alias")
                or source["owner_alias"] != frozen.get("owner_alias")
                or source["authorization_ref"] != authorization_ref
                or source["sha256"] != frozen.get("sha256")
                or source["content_type"] != frozen.get("media_type")
                or source["upload_type"] not in {"main_video", "main_audio"}
                or source["upload_type"]
                != ("main_video" if str(frozen.get("media_type", "")).startswith("video/") else "main_audio")
                or source["content_type"]
                not in {"video/mp4", "audio/mpeg", "audio/wav", "audio/mp4"}
                or not path.is_absolute()
                or not path.is_file()
                or not SHA256.fullmatch(source["sha256"])
                or _sha256_file(path) != source["sha256"]
            ):
                raise RealRunUnavailable("asset_binding_invalid")
            seen.add(case_id)
            result.append(source)
        if seen != set(expected):
            raise RealRunUnavailable("asset_binding_case_set_mismatch")
        return tuple(result)


class HttpCaseApi:
    """Immutable, owner-scoped real API surface for one acceptance case."""

    def __init__(
        self,
        transport: HttpRealRunApi,
        case: Mapping[str, Any],
        authority: Mapping[str, Any],
    ) -> None:
        self._transport = transport
        self._case = dict(case)
        self._authority = dict(authority)
        self._session = authority["session"]
        self._request: dict[str, Any] | None = None
        self._quote_id: str | None = None

    def upload_source(self, case: Mapping[str, Any]) -> Mapping[str, Any]:
        if case.get("case_id") != self._case.get("case_id"):
            raise RealRunUnavailable("acceptance_case_mismatch")
        return {
            "upload_id": f"authority-{self._case['case_id']}",
            "owner_alias": self._authority["owner_alias"],
        }

    def _build_request(self) -> dict[str, Any]:
        input_type = str(self._case["input_type"])
        request = {
            "input_type": input_type,
            **dict(self._authority["source_fields"]),
            "ratio": (
                "auto"
                if input_type in {"platform_talking_head", "uploaded_video"}
                else str(self._case["ratio"])
            ),
            "creation_mode": str(self._case["creation_mode"]),
            "material_asset_ids": list(self._authority["material_asset_ids"]),
        }
        if request["creation_mode"] == "style_prompt":
            request["style_prompt"] = str(self._case["style_prompt"])
        elif request["creation_mode"] == "template_reference":
            request["template_id"] = str(self._case["template_id"])
        return request

    def expected_request_sha256(self, case: Mapping[str, Any]) -> str:
        if case.get("case_id") != self._case.get("case_id"):
            raise RealRunUnavailable("acceptance_case_mismatch")
        return request_fingerprint(normalize_job_request(self._build_request()))

    def quote(
        self,
        case: Mapping[str, Any],
        upload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if (
            case.get("case_id") != self._case.get("case_id")
            or upload.get("owner_alias") != self._authority["owner_alias"]
        ):
            raise RealRunUnavailable("owner_scope_mismatch")
        request = self._build_request()
        quote = self._transport._json_request(
            "POST", "/api/v3/edit/quote", request,
            expected_statuses=(201,), session=self._session,
        )
        quote_id = quote.get("quote_id")
        pricing_version = quote.get("pricing_version")
        held_points = quote.get("max_points")
        expected_request_sha256 = self.expected_request_sha256(case)
        if (
            not isinstance(quote_id, str)
            or not quote_id
            or not isinstance(pricing_version, str)
            or not pricing_version
            or isinstance(held_points, bool)
            or not isinstance(held_points, int)
            or held_points < 0
            or quote.get("request_sha256") != expected_request_sha256
        ):
            raise RealRunUnavailable("quote_response_invalid")
        self._request = request
        self._quote_id = quote_id
        return {
            "quote_id": quote_id,
            "pricing_version": pricing_version,
            "held_points": held_points,
        }

    def create_job(self, idempotency_key: str) -> str:
        if self._request is None or self._quote_id is None:
            raise RealRunUnavailable("quote_required")
        created = self._transport._json_request(
            "POST",
            "/api/v3/edit/jobs",
            {**self._request, "quote_id": self._quote_id},
            expected_statuses=(202,),
            session=self._session,
            idempotency_key=idempotency_key,
        )
        job_id = created.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            raise RealRunUnavailable("job_id_invalid")
        return job_id

    def get_job(self, job_id: str) -> dict[str, str]:
        payload = self._transport._json_request(
            "GET",
            f"/api/v3/edit/jobs/{urllib.parse.quote(job_id, safe='')}",
            None,
            expected_statuses=(200,),
            session=self._session,
        )
        if payload.get("job_id") != job_id or not isinstance(payload.get("state"), str):
            raise RealRunUnavailable("job_response_invalid")
        state = str(payload["state"])
        return {
            "job_id": job_id,
            "status": "failed" if state == "prehold_absent" else state,
        }

    def get_result(self, job_id: str) -> Mapping[str, Any]:
        evidence_payload = self._transport._json_request(
            "GET",
            f"/api/v3/edit/jobs/{urllib.parse.quote(job_id, safe='')}/acceptance-evidence",
            None,
            expected_statuses=(200,),
            session=self._session,
        )
        evidence = evidence_payload.get("evidence")
        state = evidence_payload.get("state")
        if (
            evidence_payload.get("job_id") != job_id
            or not isinstance(evidence, Mapping)
            or not isinstance(state, str)
        ):
            raise RealRunUnavailable("acceptance_evidence_invalid")
        merged = {
            "status": "failed" if state == "prehold_absent" else state,
            **dict(evidence),
        }
        if state == "completed":
            result_payload = self._transport._json_request(
                "GET",
                f"/api/v3/edit/jobs/{urllib.parse.quote(job_id, safe='')}/result",
                None,
                expected_statuses=(200,),
                session=self._session,
            )
            result = result_payload.get("result")
            if result_payload.get("job_id") != job_id or not isinstance(result, Mapping):
                raise RealRunUnavailable("acceptance_evidence_invalid")
            playback_url = result.get("play_url")
            if isinstance(playback_url, str):
                merged["playback_url"] = playback_url
        return merged

    def verify_range(self, playback_url: str) -> bool:
        if not self._transport._safe_signed_upload_url(playback_url):
            return False
        request = urllib.request.Request(
            playback_url,
            headers={"Range": "bytes=0-0"},
            method="GET",
        )
        try:
            response = self._transport._opener.open(request, timeout=15)
            try:
                status = int(getattr(response, "status", 0))
                content_range = response.headers.get("Content-Range", "")
                content_length = response.headers.get("Content-Length")
                body = response.read(2)
            finally:
                response.close()
        except Exception:
            return False
        match = re.fullmatch(r"bytes 0-0/([1-9][0-9]*)", content_range)
        return bool(
            status == 206
            and match is not None
            and len(body) == 1
            and (content_length is None or content_length == "1")
        )


def build_real_run_api() -> HttpRealRunApi:
    base_url = os.environ.get("AI_EDIT_V3_TEST_BASE_URL", "").strip()
    bindings_text = os.environ.get("AI_EDIT_V3_ASSET_BINDINGS", "").strip()
    if not base_url or not bindings_text:
        raise RealRunUnavailable("real_test_api_not_configured")
    bindings = Path(bindings_text)
    if not bindings.is_file():
        raise RealRunUnavailable("real_test_api_not_configured")
    try:
        session = load_test_session(os.environ, getpass.getpass)
    except (EOFError, KeyboardInterrupt, ValueError) as exc:
        raise RealRunUnavailable("test_session_missing") from exc
    return HttpRealRunApi(
        base_url=base_url,
        session=session,
        bindings_path=bindings,
    )


class RealRunApi(Protocol):
    def capabilities(self) -> dict[str, object]: ...
    def upload_authorized_sources(
        self, matrix_path: Path | None, authorization_ref: str, subset: str | None
    ) -> None: ...


@dataclass(frozen=True)
class RealRunConfig:
    expected_sha: str
    environment: str
    authorization_ref: str
    matrix_path: Path | None = None
    subset: str | None = None


@dataclass(frozen=True)
class RealRunResult:
    exit_code: int
    reason: str


def run_real_acceptance(api: RealRunApi, config: RealRunConfig) -> RealRunResult:
    if config.environment != "test":
        return RealRunResult(2, "environment_not_test")
    if not config.authorization_ref.strip():
        return RealRunResult(2, "authorization_missing")
    try:
        capabilities = api.capabilities()
    except (RealRunUnavailable, OSError, TypeError, ValueError, json.JSONDecodeError):
        return RealRunResult(2, "capabilities_unavailable")
    if not isinstance(capabilities, Mapping):
        return RealRunResult(2, "capabilities_invalid")
    if capabilities.get("environment") != "test":
        return RealRunResult(2, "deployed_environment_mismatch")
    if capabilities.get("deployed_sha") != config.expected_sha:
        return RealRunResult(2, "deployed_sha_mismatch")
    active_jobs = capabilities.get("active_v3_jobs")
    if isinstance(active_jobs, bool) or active_jobs != 0:
        return RealRunResult(2, "active_v3_jobs")
    if capabilities.get("v3_enabled") is not True:
        return RealRunResult(2, "v3_not_enabled")
    if capabilities.get("providers_ready") is not True:
        return RealRunResult(2, "providers_not_ready")
    if capabilities.get("accepts_uploads") is not True:
        return RealRunResult(2, "uploads_not_ready")
    if capabilities.get("accepts_new_jobs") is not True:
        return RealRunResult(2, "new_jobs_not_ready")
    try:
        api.upload_authorized_sources(
            config.matrix_path, config.authorization_ref, config.subset
        )
    except (RealRunUnavailable, OSError, TypeError, ValueError):
        return RealRunResult(2, "authorized_source_upload_failed")
    return RealRunResult(0, "preflight_passed")


def execute_run_command(args: argparse.Namespace) -> int:
    if args.environment == "local-fake":
        return execute_local_fake_run(args)
    expected_sha = os.environ.get("AI_EDIT_V3_EXPECTED_SHA", "").strip()
    authorization_ref = os.environ.get(
        "AI_EDIT_V3_ACCEPTANCE_AUTHORIZATION_REF", "",
    ).strip()
    if not expected_sha or not authorization_ref:
        return 2
    try:
        matrix_report = validate_matrix(args.matrix)
    except (OSError, ValueError, json.JSONDecodeError):
        return 2
    if not matrix_report.passed:
        return 2
    try:
        api = build_real_run_api()
    except RealRunUnavailable:
        return 2
    result = run_real_acceptance(api, RealRunConfig(
        expected_sha=expected_sha,
        environment=args.environment,
        authorization_ref=authorization_ref,
        matrix_path=args.matrix,
        subset=args.subset,
    ))
    if result.exit_code != 0:
        return result.exit_code
    return execute_preflighted_cases(api, args)


def execute_verify_command(args: argparse.Namespace) -> int:
    try:
        report = json.loads(args.report.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 4
    if not isinstance(report, dict):
        return 4
    if "profiles" in report:
        profiles = report.get("profiles")
        if (
            set(report) != {"version", "run_id", "environment", "profiles"}
            or report.get("version") != "1.0"
            or report.get("environment") != "test"
            or not isinstance(report.get("run_id"), str)
            or not isinstance(profiles, Mapping)
            or not profiles
            or not set(profiles).issubset({"single", "parallel-5", "stress-10"})
        ):
            return 4
        results: list[int] = []
        for profile, metadata in profiles.items():
            profile_report = args.report.parent / profile / "report.json"
            try:
                profile_payload = json.loads(profile_report.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return 4
            if (
                not isinstance(metadata, Mapping)
                or set(metadata) != {
                    "status", "case_count", "manifest_sha256", "report_sha256",
                }
                or not profile_report.is_file()
                or metadata.get("report_sha256") != _sha256_file(profile_report)
                or not isinstance(profile_payload, Mapping)
                or profile_payload.get("run_id") != report["run_id"]
                or profile_payload.get("environment") != "test"
                or metadata.get("status") != profile_payload.get("status")
                or metadata.get("case_count") != profile_payload.get("case_count")
                or metadata.get("manifest_sha256")
                != profile_payload.get("manifest_sha256")
            ):
                return 4
            result = execute_verify_command(type("VerifyArgs", (), {
                "report": profile_report,
                "strict": args.strict,
            })())
            if result == 4:
                return 4
            results.append(result)
        return 0 if all(result == 0 for result in results) else 3
    run_dir = args.report.parent
    manifest_path = run_dir / "run-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 4
    case_ids = manifest.get("case_ids")
    case_statuses = report.get("case_statuses")
    if report.get("environment") == "test":
        expected_profiles = {
            "single": (None, 1, 20),
            "parallel-5": ("parallel-5", 5, 5),
            "stress-10": ("stress-10", 10, 10),
        }
        profile = run_dir.name
        if (
            profile not in expected_profiles
            or manifest.get("profile") != profile
            or (
                manifest.get("subset"), manifest.get("concurrency"),
                report.get("case_count"),
            ) != expected_profiles[profile]
        ):
            return 4
    if (
        not isinstance(case_ids, list)
        or not isinstance(case_statuses, list)
        or report.get("case_count") != len(case_ids)
        or len(case_statuses) != len(case_ids)
        or report.get("manifest_sha256") != _sha256_file(manifest_path)
        or report.get("run_id") != manifest.get("run_id")
        or report.get("environment") != manifest.get("environment")
    ):
        return 4
    observed_statuses: list[str] = []
    for expected_case_id, summary in zip(case_ids, case_statuses, strict=True):
        if not isinstance(summary, dict) or summary.get("case_id") != expected_case_id:
            return 4
        case_dir = run_dir / str(expected_case_id)
        evidence_path = case_dir / "evidence.json"
        verdict = verify_case_evidence(case_dir, strict=args.strict)
        if not verdict.passed or summary.get("evidence_sha256") != _sha256_file(evidence_path):
            return 4
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        expected_key = f"acceptance:{report.get('run_id')}:{expected_case_id}"
        if (
            evidence.get("case_id") != expected_case_id
            or evidence.get("idempotency_key") != expected_key
            or evidence.get("status") != summary.get("status")
            or evidence.get("normalized_request_sha256")
            != manifest.get("case_request_sha256", {}).get(expected_case_id)
            or summary.get("normalized_request_sha256")
            != evidence.get("normalized_request_sha256")
        ):
            return 4
        observed_statuses.append(str(evidence["status"]))
    all_completed = all(status == "completed" for status in observed_statuses)
    expected_report_status = "completed" if all_completed else "failed"
    if report.get("status") != expected_report_status:
        return 4
    return 0 if all_completed else 3


def _refresh_acceptance_aggregate(run_root: Path, run_id: str) -> None:
    lock_path = run_root / ".acceptance.lock"
    with _aggregate_file_lock(lock_path):
        profiles: dict[str, Mapping[str, Any]] = {}
        for profile in ("single", "parallel-5", "stress-10"):
            report_path = run_root / profile / "report.json"
            if not report_path.is_file():
                continue
            report = json.loads(report_path.read_text(encoding="utf-8"))
            if (
                not isinstance(report, Mapping)
                or report.get("run_id") != run_id
                or report.get("environment") != "test"
                or execute_verify_command(type("VerifyArgs", (), {
                    "report": report_path, "strict": True,
                })()) not in {0, 3}
            ):
                raise ValueError("acceptance_profile_invalid")
            profiles[profile] = {
                "status": report["status"],
                "case_count": report["case_count"],
                "manifest_sha256": report["manifest_sha256"],
                "report_sha256": _sha256_file(report_path),
            }
        if not profiles:
            raise ValueError("acceptance_profiles_missing")
        _write_json_atomic(run_root / "acceptance.json", {
            "version": "1.0", "run_id": run_id,
            "environment": "test", "profiles": profiles,
        })


def execute_preflighted_cases(api: Any, args: argparse.Namespace) -> int:
    profile = args.subset or "single"
    expected_concurrency = {
        "single": 1,
        "parallel-5": 5,
        "stress-10": 10,
    }.get(profile)
    if (
        args.environment != "test"
        or args.concurrency < 1
        or args.concurrency > 10
        or args.concurrency != expected_concurrency
        or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,63}", args.run_id)
    ):
        return 4
    expected_sha = os.environ.get("AI_EDIT_V3_EXPECTED_SHA", "").strip()
    authorization_ref = os.environ.get(
        "AI_EDIT_V3_ACCEPTANCE_AUTHORIZATION_REF", "",
    ).strip()
    if not re.fullmatch(r"[0-9a-f]{40}", expected_sha) or not authorization_ref:
        return 4
    output_root = Path(os.environ.get(
        "AI_EDIT_V3_ACCEPTANCE_OUTPUT_ROOT",
        ".artifacts/ai-edit-v3/acceptance",
    ))
    run_root = output_root / args.run_id
    run_dir = run_root / profile
    try:
        document = json.loads(args.matrix.read_text(encoding="utf-8"))
        if (
            not isinstance(document, Mapping)
            or document.get("authorization_ref") != authorization_ref
            or not isinstance(document.get("cases"), list)
        ):
            return 4
        cases = list(document["cases"])
        if args.subset == "parallel-5":
            cases = cases[:5]
        elif args.subset == "stress-10":
            cases = cases[:10]
        elif args.subset is not None:
            return 4
        root = Path(__file__).resolve().parents[1]
        manifest = {
            "version": "1.0",
            "run_id": args.run_id,
            "environment": "test",
            "profile": profile,
            "commit_sha": expected_sha,
            "matrix_sha256": _sha256_file(args.matrix),
            "schema_sha256": _sha256_file(
                args.matrix.with_name("acceptance-20.schema.json")
            ),
            "template_registry_sha256": _sha256_file(
                root / "server/content_domains/ai_edit_v3/catalog/templates-v1.json"
            ),
            "renderer_release_sha256": _sha256_file(
                root / "server/ai_edit_v3_renderer/renderer-release.lock.json"
            ),
            "concurrency": args.concurrency,
            "subset": args.subset,
            "case_ids": [case["case_id"] for case in cases],
            "case_request_sha256": {
                case["case_id"]: api.expected_request_sha256(case) for case in cases
            },
        }
        manifest_path = run_dir / "run-manifest.json"
        report_path = run_dir / "report.json"
        if run_dir.exists():
            if report_path.exists():
                _refresh_acceptance_aggregate(run_root, args.run_id)
                return 4
            persisted = json.loads(manifest_path.read_text(encoding="utf-8"))
            if persisted != manifest:
                return 4
        else:
            run_dir.mkdir(parents=True, exist_ok=False)
            write_json_exclusive(manifest_path, manifest)

        summary = run_cases(
            AcceptanceConfig(args.run_id, run_dir, api.for_case),
            RunManifest(tuple(cases)),
            concurrency=args.concurrency,
        )
        if summary.result_code == 4:
            return 4
        completed = all(
            item["status"] == "completed" for item in summary.case_results
        )
        report = {
            "version": "1.0",
            "run_id": args.run_id,
            "environment": "test",
            "status": "completed" if completed else "failed",
            "case_count": len(cases),
            "case_statuses": list(summary.case_results),
            "manifest_sha256": _sha256_file(manifest_path),
        }
        write_json_exclusive(report_path, report)
        _refresh_acceptance_aggregate(run_root, args.run_id)
        return summary.result_code
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return 4


def execute_machine_verify_command(args: argparse.Namespace) -> int:
    try:
        evidence = load_quality_evidence(args.evidence)
        probe = probe_final_output(args.media)
    except (OSError, ValueError):
        return 4
    verdict = verify_quality_evidence(MachineCaseEvidence(
        checks={**evidence.checks, **probe.checks},
        metrics={**evidence.metrics, **probe.metrics},
        analyzers=evidence.analyzers,
        output_sha256=probe.output_sha256,
        lip_sync_applicable=evidence.lip_sync_applicable,
    ))
    if verdict.passed:
        return 0
    return 3


def execute_blind_export_command(args: argparse.Namespace) -> int:
    try:
        source = json.loads(args.source.read_text(encoding="utf-8"))
        if not isinstance(source, Mapping) or not isinstance(source.get("cases"), list):
            return 4
        package = build_blind_review_package(source["cases"])
        args.output.parent.mkdir(parents=True, exist_ok=True)
        write_json_exclusive(args.output, package)
    except (FileExistsError, OSError, ValueError, json.JSONDecodeError):
        return 4
    return 0


@dataclass(frozen=True)
class GateSummary:
    passed: bool
    blockers: tuple[str, ...]
    capacity_blocked: bool = False

    def __post_init__(self) -> None:
        if (
            type(self.passed) is not bool
            or type(self.capacity_blocked) is not bool
            or not isinstance(self.blockers, tuple)
            or len(set(self.blockers)) != len(self.blockers)
            or any(
                not isinstance(blocker, str)
                or not re.fullmatch(r"[a-z0-9][a-z0-9_.:-]{2,127}", blocker)
                for blocker in self.blockers
            )
            or (self.passed and bool(self.blockers))
            or (not self.passed and not self.capacity_blocked and not self.blockers)
            or (self.capacity_blocked and (self.passed or bool(self.blockers)))
        ):
            raise ValueError("gate_summary_invalid")


@dataclass(frozen=True)
class GoNoGoDecision:
    status: Literal["GO_FOR_PRODUCTION_REVIEW", "NO_GO", "CAPACITY_BLOCKED"]
    blockers: tuple[str, ...]


def build_go_no_go(
    *,
    machine: GateSummary,
    human: GateSummary,
    faults: GateSummary,
    capacity: GateSummary,
    regressions: GateSummary,
) -> GoNoGoDecision:
    summaries = (machine, human, faults, capacity, regressions)
    blockers = tuple(dict.fromkeys(
        blocker
        for summary in summaries
        for blocker in summary.blockers
    ))
    if blockers or any(
        not summary.passed and not summary.capacity_blocked
        for summary in summaries
    ):
        return GoNoGoDecision("NO_GO", blockers)
    if capacity.capacity_blocked:
        return GoNoGoDecision("CAPACITY_BLOCKED", ())
    return GoNoGoDecision("GO_FOR_PRODUCTION_REVIEW", ())


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate")
    validate.add_argument("--matrix", type=Path, required=True)
    run = commands.add_parser("run")
    run.add_argument("--environment", choices=("local-fake", "test"), required=True)
    run.add_argument("--matrix", type=Path, required=True)
    run.add_argument("--run-id", required=True)
    run.add_argument("--concurrency", type=int, required=True)
    run.add_argument("--subset", choices=("parallel-5", "stress-10"))
    verify = commands.add_parser("verify")
    verify.add_argument("--report", type=Path, required=True)
    verify.add_argument("--strict", action="store_true", required=True)
    machine_verify = commands.add_parser("machine-verify")
    machine_verify.add_argument("--media", type=Path, required=True)
    machine_verify.add_argument("--evidence", type=Path, required=True)
    blind_export = commands.add_parser("blind-export")
    blind_export.add_argument("--source", type=Path, required=True)
    blind_export.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "run":
        return execute_run_command(args)
    if args.command == "verify":
        return execute_verify_command(args)
    if args.command == "machine-verify":
        return execute_machine_verify_command(args)
    if args.command == "blind-export":
        return execute_blind_export_command(args)
    report = validate_matrix(args.matrix)
    pair_count = len(INPUT_TYPES) * len(CREATION_MODES) - len(report.missing_pairs)
    if report.passed:
        print(f"{report.case_count} cases; {pair_count}/15 input-mode pairs; valid")
        return 0
    print(
        f"invalid matrix: cases={report.case_count}; pairs={pair_count}/15; "
        f"duplicates={len(report.duplicate_case_ids)}; errors={len(report.errors)}"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
