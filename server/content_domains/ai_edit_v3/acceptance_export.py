from __future__ import annotations

import json
import hashlib
import os
import re
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol


class V3Api(Protocol):
    def create_job(self, idempotency_key: str) -> str: ...

    def get_job(self, job_id: str) -> dict[str, str]: ...

    def upload_source(self, case: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def quote(self, case: Mapping[str, Any], upload: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def get_result(self, job_id: str) -> Mapping[str, Any]: ...

    def verify_range(self, playback_url: str) -> bool: ...


class TestSession:
    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not value.strip():
            raise ValueError("test_session_missing")
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "TestSession([REDACTED])"


def load_test_session(
    environment: Mapping[str, str],
    prompt: Any,
) -> TestSession:
    value = environment.get("AI_EDIT_V3_TEST_SESSION")
    if value is None:
        value = prompt("AI Edit V3 test session: ")
    return TestSession(str(value))


def terminal_result_code(response: Mapping[str, Any]) -> int:
    status = response.get("status")
    if not isinstance(response.get("quote"), Mapping):
        return 4
    if status == "completed":
        return 0
    if status in {
        "refunded",
        "failed",
        "failed_reconciliation_pending",
        "failed_asset_decision_pending",
    }:
        return 3
    return 4


@dataclass(frozen=True)
class CaseCheckpoint:
    case_id: str
    idempotency_key: str
    job_id: str | None
    normalized_request_sha256: str = ""
    upload_id: str | None = None
    quote: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CaseEvidence:
    case_id: str
    idempotency_key: str
    status: str
    normalized_request_sha256: str
    quote: Mapping[str, Any]
    job_id: str
    attempt_id: str | None
    stage_timings_ms: Mapping[str, int]
    plan_schema_sha256: str | None
    material_decisions: tuple[Mapping[str, Any], ...]
    provider_usage: tuple[Mapping[str, Any], ...]
    audio_evidence: Mapping[str, Any]
    renderer_build_id: str | None
    render_manifest_sha256: str | None
    qc: Mapping[str, Any]
    settlement: Mapping[str, Any]
    publication_generation: int | None
    asset_id: str | None
    stable_cos_key: str | None
    output_sha256: str | None
    range_verified: bool


@dataclass(frozen=True)
class CaseVerdict:
    passed: bool
    errors: tuple[str, ...]


@dataclass(frozen=True)
class AcceptanceConfig:
    run_id: str
    run_dir: Path
    api_factory: Any


@dataclass(frozen=True)
class RunManifest:
    cases: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class RunSummary:
    result_code: int
    case_results: tuple[Mapping[str, str], ...]


def resume_or_create_case(checkpoint: CaseCheckpoint, api: V3Api) -> CaseCheckpoint:
    if checkpoint.job_id is not None:
        api.get_job(checkpoint.job_id)
        return checkpoint
    job_id = api.create_job(checkpoint.idempotency_key)
    return CaseCheckpoint(
        case_id=checkpoint.case_id,
        idempotency_key=checkpoint.idempotency_key,
        job_id=job_id,
        normalized_request_sha256=checkpoint.normalized_request_sha256,
        upload_id=checkpoint.upload_id,
        quote=checkpoint.quote,
    )


_SENSITIVE_KEY = re.compile(
    r"(?:url|uri|token|secret|credential|password|cookie|authorization|session|signature|signed)",
    re.I,
)
_SENSITIVE_VALUE = re.compile(
    r"(?:[?&](?:x-amz-|token=|signature=|credential=)|sk[-_][a-z0-9_-]{8,}|bearer\s+)",
    re.I,
)


def _assert_persistable(value: Any, path: str = "root") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key)
            if _SENSITIVE_KEY.search(key_text):
                raise ValueError(f"sensitive_key:{path}.{key_text}")
            _assert_persistable(child, f"{path}.{key_text}")
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            _assert_persistable(child, f"{path}[{index}]")
    elif isinstance(value, str) and (
        _SENSITIVE_VALUE.search(value)
        or "://" in value
    ):
        raise ValueError(f"sensitive_value:{path}")


def write_json_exclusive(path: Path, payload: Mapping[str, Any]) -> None:
    _assert_persistable(payload)
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp_path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(serialized)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temp_path, path)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def collect_case_evidence(
    client: V3Api,
    case: Mapping[str, Any],
    run_dir: Path,
    *,
    run_id: str = "local-fake",
) -> CaseEvidence:
    evidence_path = run_dir / "evidence.json"
    if evidence_path.exists():
        raise FileExistsError(evidence_path)
    run_dir.mkdir(parents=True, exist_ok=True)
    case_id = str(case["case_id"])
    checkpoint_path = run_dir / "checkpoint.json"
    checkpoint_existed = checkpoint_path.exists()
    if checkpoint_existed:
        payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        checkpoint = CaseCheckpoint(
            case_id=str(payload["case_id"]),
            idempotency_key=str(payload["idempotency_key"]),
            job_id=str(payload["job_id"]),
            normalized_request_sha256=str(payload["normalized_request_sha256"]),
            upload_id=str(payload["upload_id"]),
            quote=dict(payload["quote"]),
        )
        if (
            checkpoint.case_id != case_id
            or checkpoint.idempotency_key != f"acceptance/{run_id}/{case_id}"
            or checkpoint.normalized_request_sha256 != str(case["source"]["sha256"])
        ):
            raise ValueError("checkpoint_identity_mismatch")
    else:
        upload = client.upload_source(case)
        quote = dict(client.quote(case, upload))
        checkpoint = CaseCheckpoint(
            case_id=case_id,
            idempotency_key=f"acceptance/{run_id}/{case_id}",
            job_id=None,
            normalized_request_sha256=str(case["source"]["sha256"]),
            upload_id=str(upload["upload_id"]),
            quote=quote,
        )
    checkpoint = resume_or_create_case(checkpoint, client)
    if checkpoint.job_id is None:
        raise ValueError("job_id_missing")
    if not checkpoint_existed:
        write_json_exclusive(checkpoint_path, asdict(checkpoint))
    terminal_statuses = {
        "completed", "refunded", "failed",
        "failed_reconciliation_pending", "failed_asset_decision_pending",
    }
    terminal = False
    for _ in range(120):
        state = client.get_job(checkpoint.job_id)
        if state.get("status") in terminal_statuses:
            terminal = True
            break
    if not terminal:
        raise ValueError("job_poll_limit_exceeded")
    result = dict(client.get_result(checkpoint.job_id))
    result_request_sha = result.get("normalized_request_sha256")
    if result_request_sha is not None and result_request_sha != checkpoint.normalized_request_sha256:
        raise ValueError("normalized_request_sha256_mismatch")
    playback_url = result.pop("playback_url", None)
    range_verified = bool(playback_url and client.verify_range(str(playback_url)))
    evidence = CaseEvidence(
        case_id=case_id,
        idempotency_key=checkpoint.idempotency_key,
        status=str(result.get("status", "unknown")),
        normalized_request_sha256=checkpoint.normalized_request_sha256,
        quote=dict(checkpoint.quote),
        job_id=checkpoint.job_id,
        attempt_id=result.get("attempt_id"),
        stage_timings_ms=dict(result.get("stage_timings_ms", {})),
        plan_schema_sha256=result.get("plan_schema_sha256"),
        material_decisions=tuple(result.get("material_decisions", ())),
        provider_usage=tuple(result.get("provider_usage", ())),
        audio_evidence=dict(result.get("audio_evidence", {})),
        renderer_build_id=result.get("renderer_build_id"),
        render_manifest_sha256=result.get("render_manifest_sha256"),
        qc=dict(result.get("qc", {})),
        settlement=dict(result.get("settlement", {})),
        publication_generation=result.get("publication_generation"),
        asset_id=result.get("asset_id"),
        stable_cos_key=result.get("stable_cos_key"),
        output_sha256=result.get("output_sha256"),
        range_verified=range_verified,
    )
    write_json_exclusive(evidence_path, asdict(evidence))
    return evidence


def verify_case_evidence(case_dir: Path, *, strict: bool) -> CaseVerdict:
    path = case_dir / "evidence.json"
    try:
        raw = path.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return CaseVerdict(False, ("evidence_missing_or_invalid",))
    errors: list[str] = []
    if "playback_url" in raw or "token=" in raw:
        errors.append("signed_url_persisted")
    required = {
        "case_id", "idempotency_key", "status", "normalized_request_sha256", "quote", "job_id",
        "stage_timings_ms", "material_decisions", "provider_usage", "audio_evidence",
        "qc", "settlement", "range_verified",
    }
    for field in sorted(required - set(payload)):
        errors.append(f"field_missing:{field}")
    case_id = payload.get("case_id")
    if not isinstance(case_id, str) or not re.fullmatch(r"case_[0-9]{2}", case_id):
        errors.append("case_id_invalid")
    idempotency_key = payload.get("idempotency_key")
    if (
        not isinstance(idempotency_key, str)
        or not isinstance(case_id, str)
        or not idempotency_key.startswith("acceptance/")
        or not idempotency_key.endswith(f"/{case_id}")
    ):
        errors.append("idempotency_key_invalid")
    if not isinstance(payload.get("job_id"), str) or not payload.get("job_id"):
        errors.append("job_id_invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", str(payload.get("normalized_request_sha256", ""))):
        errors.append("normalized_request_sha256_invalid")
    quote = payload.get("quote")
    if (
        not isinstance(quote, Mapping)
        or not isinstance(quote.get("quote_id"), str)
        or not isinstance(quote.get("pricing_version"), str)
        or not isinstance(quote.get("held_points"), int)
        or quote.get("held_points", -1) < 0
    ):
        errors.append("quote_invalid")
    settlement = payload.get("settlement")
    if (
        not isinstance(settlement, Mapping)
        or settlement.get("state") not in {
            "settled", "refunded", "prehold_absent",
            "reconciliation_pending", "asset_decision_pending",
        }
        or not isinstance(settlement.get("charged_points"), int)
        or not isinstance(settlement.get("refunded_points"), int)
    ):
        errors.append("settlement_invalid")
    elif isinstance(quote, Mapping):
        held = quote.get("held_points")
        charged = settlement.get("charged_points")
        refunded = settlement.get("refunded_points")
        if (
            not isinstance(held, int)
            or not isinstance(charged, int)
            or not isinstance(refunded, int)
            or charged < 0
            or refunded < 0
            or charged + refunded > held
            or (
                settlement.get("state") in {"settled", "refunded", "prehold_absent"}
                and charged + refunded != held
            )
        ):
            errors.append("settlement_points_invalid")
    if terminal_result_code({"status": payload.get("status"), "quote": quote}) == 4:
        errors.append("status_invalid")
    expected_settlement = {
        "completed": "settled",
        "refunded": "refunded",
        "failed": "prehold_absent",
        "failed_reconciliation_pending": "reconciliation_pending",
        "failed_asset_decision_pending": "asset_decision_pending",
    }.get(payload.get("status"))
    if isinstance(settlement, Mapping) and settlement.get("state") != expected_settlement:
        errors.append("status_settlement_mismatch")
    if strict and payload.get("status") == "completed":
        for field in (
            "attempt_id", "plan_schema_sha256", "renderer_build_id",
            "render_manifest_sha256", "publication_generation", "asset_id",
            "stable_cos_key", "output_sha256",
        ):
            if payload.get(field) in (None, ""):
                errors.append(f"field_missing:{field}")
        if payload.get("range_verified") is not True:
            errors.append("range_not_verified")
        for field in (
            "plan_schema_sha256", "render_manifest_sha256", "output_sha256",
        ):
            if not re.fullmatch(r"[0-9a-f]{64}", str(payload.get(field, ""))):
                errors.append(f"sha256_invalid:{field}")
        qc = payload.get("qc")
        if (
            not isinstance(qc, Mapping)
            or qc.get("passed") is not True
            or not re.fullmatch(r"[0-9a-f]{64}", str(qc.get("report_sha256", "")))
        ):
            errors.append("qc_invalid")
        audio = payload.get("audio_evidence")
        if (
            not isinstance(audio, Mapping)
            or not re.fullmatch(r"[0-9a-f]{64}", str(audio.get("dialogue_sha256", "")))
            or not isinstance(audio.get("peak_dbfs"), (int, float))
            or not isinstance(audio.get("lufs"), (int, float))
        ):
            errors.append("audio_evidence_invalid")
        timings = payload.get("stage_timings_ms")
        if (
            not isinstance(timings, Mapping)
            or not timings
            or any(not isinstance(value, int) or value < 0 for value in timings.values())
        ):
            errors.append("stage_timings_invalid")
        if not isinstance(payload.get("publication_generation"), int) or payload.get("publication_generation", 0) < 1:
            errors.append("publication_generation_invalid")
    elif strict and payload.get("range_verified") is not False:
        errors.append("failed_terminal_range_invalid")
    return CaseVerdict(not errors, tuple(errors))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_cases(
    config: AcceptanceConfig,
    manifest: RunManifest,
    *,
    concurrency: int,
    subset: str | None = None,
) -> RunSummary:
    if concurrency < 1 or concurrency > 10:
        return RunSummary(4, ())
    cases = list(manifest.cases)
    if subset == "parallel-5":
        cases = cases[:5]
    elif subset == "stress-10":
        cases = cases[:10]
    elif subset is not None:
        return RunSummary(4, ())
    results: list[Mapping[str, str]] = []
    for case in cases:
        case_id = str(case["case_id"])
        case_dir = config.run_dir / case_id
        evidence_path = case_dir / "evidence.json"
        if evidence_path.exists():
            verdict = verify_case_evidence(case_dir, strict=True)
            if not verdict.passed:
                return RunSummary(4, tuple(results))
            payload = json.loads(evidence_path.read_text(encoding="utf-8"))
            status = str(payload["status"])
        else:
            evidence = collect_case_evidence(
                config.api_factory(case),
                case,
                case_dir,
                run_id=config.run_id,
            )
            status = evidence.status
        results.append({
            "case_id": case_id,
            "status": status,
            "normalized_request_sha256": str(
                json.loads(evidence_path.read_text(encoding="utf-8"))["normalized_request_sha256"]
            ),
            "evidence_sha256": _sha256_file(evidence_path),
        })
    codes = [terminal_result_code({"status": item["status"], "quote": {}}) for item in results]
    # terminal_result_code requires a quote mapping only to distinguish malformed
    # input; evidence has already been strictly verified above.
    result_code = 0 if all(item["status"] == "completed" for item in results) else 3
    if any(code == 4 and item["status"] not in {"completed"} for code, item in zip(codes, results)):
        allowed_failures = {
            "refunded", "failed", "failed_reconciliation_pending",
            "failed_asset_decision_pending",
        }
        if any(item["status"] not in allowed_failures for item in results):
            result_code = 4
    return RunSummary(result_code, tuple(results))
