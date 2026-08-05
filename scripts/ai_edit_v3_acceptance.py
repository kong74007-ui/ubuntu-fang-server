from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from jsonschema import Draft202012Validator


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


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate")
    validate.add_argument("--matrix", type=Path, required=True)
    args = parser.parse_args(argv)
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
