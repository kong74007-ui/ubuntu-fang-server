from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from jsonschema import Draft202012Validator

from server.content_domains.ai_edit_v3.acceptance_export import (
    AcceptanceConfig,
    RunManifest,
    collect_case_evidence,
    run_cases,
    verify_case_evidence,
    write_json_exclusive,
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
    matrix_report = validate_matrix(args.matrix)
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


def build_real_run_api() -> Any:
    raise RuntimeError("real_test_api_not_enabled_before_phase_e_task_7")


def execute_run_command(args: argparse.Namespace) -> int:
    if args.environment != "local-fake":
        return 2
    return execute_local_fake_run(args)


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
    args = parser.parse_args(argv)
    if args.command == "run":
        return execute_run_command(args)
    if args.command == "verify":
        return execute_verify_command(args)
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
