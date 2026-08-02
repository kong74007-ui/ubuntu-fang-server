from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


REQUIRED_OUTCOMES = (
    "all_input_types",
    "all_creation_modes",
    "authoritative_text",
    "punctuation_only",
    "material_matching",
    "required_material_failure",
    "injection_rejected",
    "invalid_model_rejected",
)
INPUT_TYPES = frozenset(
    {
        "platform_talking_head",
        "uploaded_video",
        "existing_audio",
        "uploaded_audio",
        "script_to_audio_video",
    }
)
CREATION_MODES = frozenset({"platform_template", "style_prompt", "ai_open"})


@dataclass(frozen=True)
class PhaseBGateReport:
    passed: bool
    missing: tuple[str, ...]
    case_count: int


@dataclass(frozen=True)
class CapabilityEvidence:
    name: str
    status: str


def validate_phase_b_cases(path: Path) -> PhaseBGateReport:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = payload.get("cases") if isinstance(payload, dict) else None
    if not isinstance(cases, list):
        raise ValueError("phase_b_cases_invalid")
    identifiers: set[str] = set()
    present: set[str] = set()
    input_types: set[str] = set()
    creation_modes: set[str] = set()
    for case in cases:
        if not isinstance(case, dict) or set(case) != {"id", "outcome", "input_type", "creation_mode"}:
            raise ValueError("phase_b_case_invalid")
        identifier = case["id"]
        if not isinstance(identifier, str) or not identifier:
            raise ValueError("phase_b_case_id_invalid")
        if identifier in identifiers:
            raise ValueError("phase_b_case_id_duplicate")
        identifiers.add(identifier)
        outcome = case["outcome"]
        input_type = case["input_type"]
        creation_mode = case["creation_mode"]
        if outcome not in REQUIRED_OUTCOMES:
            raise ValueError("phase_b_case_outcome_invalid")
        if input_type not in INPUT_TYPES or creation_mode not in CREATION_MODES:
            raise ValueError("phase_b_case_dimension_invalid")
        present.add(outcome)
        input_types.add(input_type)
        creation_modes.add(creation_mode)
    missing = [name for name in REQUIRED_OUTCOMES if name not in present]
    if "all_input_types" in present and input_types != INPUT_TYPES:
        missing.append("all_input_types")
    if "all_creation_modes" in present and creation_modes != CREATION_MODES:
        missing.append("all_creation_modes")
    deduplicated = tuple(dict.fromkeys(missing))
    return PhaseBGateReport(not deduplicated, deduplicated, len(cases))


def collect_phase_b_capabilities(runtime: Any) -> tuple[CapabilityEvidence, ...]:
    evidence: list[CapabilityEvidence] = []
    for name in ("ffmpeg", "fun_asr", "dashscope", "tts", "image_generation", "cos"):
        try:
            status = runtime.capability_status(name)
        except Exception:
            status = "missing_or_unavailable"
        evidence.append(CapabilityEvidence(name, str(status)))
    return tuple(evidence)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    report = validate_phase_b_cases(args.path)
    print(json.dumps(report.__dict__, ensure_ascii=False, sort_keys=True))
    raise SystemExit(0 if report.passed else 1)
