from __future__ import annotations

import json
import hashlib
import os
import re
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Collection, Mapping, Protocol, Sequence


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


DIMENSION_ANCHORS = {
    "事实准确": {
        0: "出现与准确文本或授权事实冲突、虚构或无法追溯的可见事实",
        1: "无明确错误，但至少一项事实仅能追溯到弱证据或表达存在歧义",
        2: "所有口播与可见事实均与准确文本一致并可追溯到授权证据",
    },
    "素材相关": {
        0: "存在无关、错主体、错产品、错门店或误导性素材",
        1: "素材主题相关但至少一处语义、时机或主体表达不够精确",
        2: "全部素材与当前语义、主体和出现时机准确匹配",
    },
    "前三秒钩子": {
        0: "前三秒没有清晰钩子或钩子与后文事实不一致",
        1: "前三秒有相关信息但吸引力、可读性或承诺清晰度一般",
        2: "前三秒以准确、清晰且有吸引力的视听信息建立观看理由",
    },
    "叙事节奏": {
        0: "结构难以理解，存在明显拖沓、跳跃或信息拥堵",
        1: "主线可理解但至少一段节奏、停留或转场时机不理想",
        2: "开场、展开、证明和收束连贯，节奏与信息密度匹配",
    },
    "布局清晰": {
        0: "主体、文字或素材互相遮挡，关键层级无法辨认",
        1: "层级基本可辨，但至少一处拥挤、失衡或安全区利用不佳",
        2: "主体、字幕、卡片与素材层级明确且在安全区内稳定呈现",
    },
    "字幕可读": {
        0: "存在错字、漏字、遮挡、越界或无法按正常速度阅读的字幕",
        1: "字幕准确可读，但至少一处断句、字号、对比或停留时间不佳",
        2: "字幕准确、同步、断句自然，字号、对比和停留时间均适合发布",
    },
    "声音质量": {
        0: "存在削波、异常静音、重复人声、明显不同步或对白不可辨",
        1: "对白可辨且无阻断故障，但响度、混音或音效时机仍可改善",
        2: "对白清晰同步，响度稳定，BGM与音效服务内容且不遮蔽人声",
    },
    "视觉一致性": {
        0: "颜色、字体、动效或素材风格冲突，成片呈现拼贴失控",
        1: "整体风格基本统一，但至少一处组件或素材语言不协调",
        2: "颜色、字体、动效、转场与素材语言统一且保留内容驱动差异",
    },
}

DIMENSIONS = tuple(DIMENSION_ANCHORS)
CRITICAL_DIMENSIONS = ("事实准确", "素材相关", "字幕可读", "声音质量")
_CONTINUITY_REASONS = frozenset({"speaker_continuity", "step_sequence", "evidence_hold"})
_REVIEWER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,63}$")
_CREATIVE_REGISTRY_SHA256 = "2023e55cc2092cdf29632ff2f5d05c1d0123d5d9c1950efcb00b46315e4de2ba"
_CANONICAL_LAYOUT_VARIANTS = {
    "speaker_fullscreen": frozenset({"clean_center", "headline_top", "caption_sidebar"}),
    "speaker_left_info_right": frozenset({"card_stack", "number_focus", "image_evidence"}),
    "speaker_right_evidence_left": frozenset({"document_panel", "comparison_panel", "quote_evidence"}),
    "material_fullscreen_speaker_pip": frozenset({"pip_round", "pip_card", "pip_edge"}),
    "product_hero": frozenset({"center_pedestal", "split_copy", "detail_gallery"}),
    "editorial_collage": frozenset({"magazine_grid", "layered_cards", "film_strip"}),
    "comparison_split": frozenset({"vertical_divide", "before_after_slider", "score_compare"}),
    "steps_stack": frozenset({"vertical_steps", "numbered_cards", "progress_path"}),
    "number_proof": frozenset({"hero_number", "metric_grid", "chart_callout"}),
    "quote_reversal": frozenset({"diagonal_statement", "strike_reveal", "question_answer"}),
    "method_timeline": frozenset({"horizontal_timeline", "vertical_milestones", "chapter_route"}),
    "cta_offer": frozenset({"offer_card", "qr_placeholder", "action_steps"}),
}


@dataclass(frozen=True)
class HumanReview:
    reviewer_id: str
    cases: Mapping[str, Mapping[str, int]]


@dataclass(frozen=True)
class HumanCaseDecision:
    publishable: bool
    average_total: float
    personal_passes: int
    reviewer_count: int
    reason: str


@dataclass(frozen=True)
class HumanSummary:
    cases: Mapping[str, HumanCaseDecision]

    @property
    def publishable_count(self) -> int:
        return sum(decision.publishable for decision in self.cases.values())


@dataclass(frozen=True)
class CreativeDistributionReport:
    passed: bool
    errors: tuple[str, ...]


def _validated_reviewer_id(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not _REVIEWER_ID.fullmatch(value)
        or value.casefold() in {"self", "system", "ai", "qwen", "codex"}
    ):
        raise ValueError("reviewer_id_invalid")
    return value


def _validated_scores(raw: Mapping[str, int]) -> dict[str, int]:
    if not isinstance(raw, Mapping) or len(raw) != len(DIMENSIONS) or set(raw) != set(DIMENSIONS):
        raise ValueError("human_review_dimensions_invalid")
    values = dict(raw)
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value not in (0, 1, 2)
        for value in values.values()
    ):
        raise ValueError("human_review_score_invalid")
    return values


def validate_human_review(
    path: Path,
    *,
    expected_cases: Collection[str],
    reviewer_id: str,
) -> HumanReview:
    expected_reviewer = _validated_reviewer_id(reviewer_id)
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("human_review_json_invalid") from exc
    if (
        not isinstance(payload, Mapping)
        or set(payload) != {"version", "reviewer_id", "cases"}
        or payload.get("version") != "1.0"
    ):
        raise ValueError("human_review_json_invalid")
    if _validated_reviewer_id(payload.get("reviewer_id")) != expected_reviewer:
        raise ValueError("reviewer_id_mismatch")
    cases = payload.get("cases")
    if not isinstance(cases, Mapping) or set(cases) != set(expected_cases):
        raise ValueError("human_review_case_set_invalid")
    validated: dict[str, Mapping[str, int]] = {}
    for case_id, raw_case in cases.items():
        if (
            not isinstance(raw_case, Mapping)
            or set(raw_case) != {"scores", "justifications"}
        ):
            raise ValueError("human_review_case_invalid")
        case_scores = _validated_scores(raw_case.get("scores"))
        justifications = raw_case.get("justifications")
        if not isinstance(justifications, Mapping) or set(justifications) != set(DIMENSIONS):
            raise ValueError("human_review_justifications_invalid")
        for dimension, score in case_scores.items():
            justification = justifications.get(dimension)
            if (
                not isinstance(justification, Mapping)
                or set(justification) != {"anchor", "note"}
                or justification.get("anchor") != DIMENSION_ANCHORS[dimension][score]
                or not isinstance(justification.get("note"), str)
                or len(justification["note"].strip()) < 8
            ):
                raise ValueError("human_review_anchor_invalid")
        validated[str(case_id)] = case_scores
    return HumanReview(expected_reviewer, validated)


def _personal_pass(values: Mapping[str, int]) -> bool:
    return (
        sum(values.values()) >= 13
        and all(values[name] > 0 for name in CRITICAL_DIMENSIONS)
    )


def _assert_distinct_reviews(*reviews: HumanReview | None) -> None:
    present = [review for review in reviews if review is not None]
    if len({id(review) for review in present}) != len(present):
        raise ValueError("review_object_reused")
    reviewer_ids = [_validated_reviewer_id(review.reviewer_id) for review in present]
    if len({value.casefold() for value in reviewer_ids}) != len(reviewer_ids):
        raise ValueError("reviewer_id_reused")


def reconcile_human_reviews(
    first: HumanReview,
    second: HumanReview,
    third: HumanReview | None,
) -> HumanSummary:
    _assert_distinct_reviews(first, second, third)
    if not first.cases or any(
        not isinstance(case_id, str) or not re.fullmatch(r"case_[0-9]{2}", case_id)
        for case_id in first.cases
    ):
        raise ValueError("primary_case_set_invalid")
    if set(first.cases) != set(second.cases):
        raise ValueError("primary_case_set_mismatch")
    first_scores = {case_id: _validated_scores(raw) for case_id, raw in first.cases.items()}
    second_scores = {case_id: _validated_scores(raw) for case_id, raw in second.cases.items()}
    disputed = {
        case_id for case_id in first_scores
        if _personal_pass(first_scores[case_id]) != _personal_pass(second_scores[case_id])
    }
    if disputed and third is None:
        raise ValueError(f"third_reviewer_required:{sorted(disputed)[0]}")
    if not disputed and third is not None:
        raise ValueError("third_reviewer_not_required")
    if third is not None and set(third.cases) != disputed:
        raise ValueError("third_reviewer_case_set_invalid")
    third_scores = (
        {case_id: _validated_scores(raw) for case_id, raw in third.cases.items()}
        if third is not None else {}
    )
    decisions: dict[str, HumanCaseDecision] = {}
    for case_id in first_scores:
        score_sets = [first_scores[case_id], second_scores[case_id]]
        if case_id in disputed:
            score_sets.append(third_scores[case_id])
        totals = [sum(values.values()) for values in score_sets]
        average_total = sum(totals) / len(totals)
        personal_passes = sum(_personal_pass(values) for values in score_sets)
        zero_critical = next((
            name for name in CRITICAL_DIMENSIONS
            if any(values[name] == 0 for values in score_sets)
        ), None)
        publishable = (
            average_total >= 13
            and zero_critical is None
            and (len(score_sets) == 2 or personal_passes >= 2)
        )
        if zero_critical is not None:
            reason = f"critical_dimension_zero:{zero_critical}"
        elif average_total < 13:
            reason = "average_total_below_13"
        elif len(score_sets) == 3 and personal_passes < 2:
            reason = "personal_pass_votes_below_2"
        else:
            reason = "publishable"
        decisions[case_id] = HumanCaseDecision(
            publishable, average_total, personal_passes, len(score_sets), reason
        )
    return HumanSummary(decisions)


def validate_creative_distribution(
    scenes: Sequence[Mapping[str, Any]],
) -> CreativeDistributionReport:
    errors: list[str] = []
    if not scenes:
        return CreativeDistributionReport(False, ("scenes_missing",))
    layouts = [str(scene.get("layout", "")) for scene in scenes]
    for index, scene in enumerate(scenes):
        layout = scene.get("layout")
        variant = scene.get("variant")
        if layout not in _CANONICAL_LAYOUT_VARIANTS:
            errors.append(f"layout_unknown:{index}")
        elif variant not in _CANONICAL_LAYOUT_VARIANTS[layout]:
            errors.append(f"layout_variant_unknown:{index}")
        if scene.get("registry_sha256") != _CREATIVE_REGISTRY_SHA256:
            errors.append(f"layout_registry_mismatch:{index}")
    unique_layouts = set(layouts)
    if len(unique_layouts) < 8:
        errors.append("layout_diversity_below_8")
    for layout in sorted(unique_layouts):
        indices = [index for index, value in enumerate(layouts) if value == layout]
        variants = {str(scenes[index].get("variant", "")) for index in indices}
        if len(variants) < 2:
            errors.append(f"layout_variant_diversity_below_2:{layout}")
        if len(indices) / len(scenes) > 0.35:
            errors.append(f"layout_share_above_35_percent:{layout}")
    run_layout = None
    run_length = 0
    for index, (layout, scene) in enumerate(zip(layouts, scenes, strict=True)):
        if layout == run_layout:
            run_length += 1
        else:
            run_layout = layout
            run_length = 1
        if run_length > 2 and scene.get("continuity_reason") not in _CONTINUITY_REASONS:
            errors.append(f"unjustified_layout_run:{index}")
    return CreativeDistributionReport(not errors, tuple(errors))


def human_acceptance_passes(summary: HumanSummary, *, expected_cases: int = 20) -> bool:
    return (
        expected_cases == 20
        and len(summary.cases) == expected_cases
        and summary.publishable_count >= 16
    )


def build_blind_review_package(
    cases: Sequence[Mapping[str, Any]],
) -> Mapping[str, Any]:
    blinded: list[Mapping[str, str]] = []
    observed: set[str] = set()
    expected = {f"case_{index:02d}" for index in range(1, 21)}
    case_ids = [
        case.get("case_id") if isinstance(case, Mapping) else None
        for case in cases
    ]
    if (
        len(cases) != 20
        or any(not isinstance(case, Mapping) for case in cases)
        or any(not isinstance(case_id, str) for case_id in case_ids)
        or set(case_ids) != expected
    ):
        raise ValueError("blind_package_case_set_invalid")
    for blind_index, case in enumerate(sorted(cases, key=lambda value: value["case_id"]), start=1):
        case_id = case.get("case_id")
        if (
            not isinstance(case_id, str)
            or not re.fullmatch(r"case_[0-9]{2}", case_id)
            or case_id in observed
        ):
            raise ValueError("blind_package_case_invalid")
        observed.add(case_id)
        blinded.append({
            "case_id": case_id,
            "media_filename": f"blind_{blind_index:03d}.mp4",
        })
    return {"version": "1.0", "cases": blinded}
