from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, Sequence

from jsonschema import Draft202012Validator

from server.content_domains.ai_edit_v3.acceptance_export import (
    AcceptanceConfig,
    RunManifest,
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
        "server/content_out/ai-edit-v3-acceptance",
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
            "case_source_sha256": {
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
        if not idempotency_key.endswith(f"/{self.case_id}"):
            raise ValueError("idempotency_key_mismatch")
        if idempotency_key in self.created_idempotency_keys:
            raise ValueError("duplicate_idempotency_key")
        self.created_idempotency_keys.add(idempotency_key)
        return str(self.response["job_id"])

    def get_job(self, job_id: str) -> dict[str, str]:
        return {"job_id": job_id, "status": str(self.response["status"])}

    def get_result(self, job_id: str) -> Mapping[str, Any]:
        return json.loads(json.dumps(self.response))

    def verify_range(self, playback_url: str) -> bool:
        return playback_url.startswith("https://playback.invalid/")


class RealRunUnavailable(RuntimeError):
    pass


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
        self._authorized_uploads: dict[str, dict[str, str]] = {}

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
    ) -> Mapping[str, Any]:
        data = None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._session.reveal()}",
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
        sources = self._load_authorized_sources(
            matrix_path, authorization_ref=authorization_ref, subset=subset
        )
        uploaded: dict[str, dict[str, str]] = {}
        for source in sources:
            path = Path(source["path"])
            created = self._json_request(
                "POST",
                "/api/v3/edit/uploads",
                {
                    "upload_type": source["upload_type"],
                    "filename": path.name,
                    "content_type": source["content_type"],
                    "size_bytes": path.stat().st_size,
                },
                expected_statuses=(201,),
            )
            upload_id = created.get("upload_id")
            put_url = created.get("put_url")
            if not isinstance(upload_id, str) or not upload_id:
                raise RealRunUnavailable("upload_id_invalid")
            if not isinstance(put_url, str) or not self._safe_signed_upload_url(put_url):
                raise RealRunUnavailable("put_url_invalid")
            self._put_file(put_url, path, source["content_type"])
            completed = self._json_request(
                "POST",
                f"/api/v3/edit/uploads/{urllib.parse.quote(upload_id, safe='')}/complete",
                {},
                expected_statuses=(200,),
            )
            if completed.get("upload_id") != upload_id:
                raise RealRunUnavailable("upload_completion_invalid")
            uploaded[source["case_id"]] = {
                "upload_id": upload_id,
                "owner_alias": source["owner_alias"],
            }
        self._authorized_uploads = uploaded

    def upload_source(self, case: Mapping[str, Any]) -> Mapping[str, Any]:
        case_id = case.get("case_id")
        source = case.get("source")
        if not isinstance(case_id, str) or not isinstance(source, Mapping):
            raise RealRunUnavailable("acceptance_case_invalid")
        uploaded = self._authorized_uploads.get(case_id)
        if uploaded is None or uploaded["owner_alias"] != source.get("owner_alias"):
            raise RealRunUnavailable("authorized_upload_binding_missing")
        return dict(uploaded)

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
            expected[case_id] = case["source"]
        if (
            not isinstance(payload, Mapping)
            or set(payload) != {"version", "sources"}
            or payload.get("version") != "1.0"
            or not isinstance(payload.get("sources"), list)
            or not 1 <= len(payload["sources"]) <= 20
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
    run_dir = args.report.parent
    manifest_path = run_dir / "run-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 4
    case_ids = manifest.get("case_ids")
    case_statuses = report.get("case_statuses")
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
        expected_key = f"acceptance/{report.get('run_id')}/{expected_case_id}"
        if (
            evidence.get("case_id") != expected_case_id
            or evidence.get("idempotency_key") != expected_key
            or evidence.get("status") != summary.get("status")
            or evidence.get("normalized_request_sha256")
            != manifest.get("case_source_sha256", {}).get(expected_case_id)
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


def execute_preflighted_cases(api: Any, args: argparse.Namespace) -> int:
    # Task 7 supplies the authorized test-environment implementation.
    return 2


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
