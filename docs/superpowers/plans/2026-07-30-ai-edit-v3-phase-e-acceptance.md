# AI 智能剪辑 V3 Phase E Acceptance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用可复跑、可审计的 20 条真实差异化样本、故障注入和 5/10 并发试验，证明 V3 的出片质量、创意差异、账务/发布安全、资源隔离、耗时和 V2 隔离达到已批准门槛，并形成独立生产 Go/No-Go 所需证据。

**Architecture:** Phase E 不新增另一条生产路径。一个 V3 专用 acceptance harness 读取不含密钥的冻结矩阵，经测试环境公开 API 创建真实任务，轮询并收集稳定 ID/哈希/审计证据；单独的 fault harness 在受控测试依赖和进程边界注入崩溃或未知响应；机器验证与双人盲评分别产出不可变 JSON，再由聚合器生成最终判定。所有授权、运行环境和实际 Provider 调用都显式记录。

**Tech Stack:** Python 3、`unittest`、站点 V3 HTTP API、SQLite/WAL、systemd、FFprobe/FFmpeg、HyperFrames CLI、腾讯云 COS Range GET、JSON/CSV/Markdown 报告、测试环境真实 DashScope/ASR/TTS/生图/ElevenLabs。

## Global Constraints

- [ ] 只有 Phase A–D 均已合并、主 CI 为绿、V3 仍默认关闭且测试服务器没有活动 V3 任务时，才申请测试部署与真实 Provider 执行授权。
- [ ] Phase E 脚本和 fixture 可以开发并用 fake/local 模式验证；真实媒体上传、真实点数预扣和外部 Provider 调用必须在授权后的测试环境执行。
- [ ] 验收输入由用户明确授权；矩阵只保存稳定资产 ID、内容哈希和许可元数据，不保存 Cookie、API Key、签名 URL 或可长期访问 URL。
- [ ] 运行记录不可覆盖：每次 run 使用唯一 `run_id` 和内容哈希目录；聚合器拒绝混用不同 commit、价格版本、Schema、renderer build 或环境指纹的结果。
- [ ] Phase E 只修验收暴露的阻断缺陷。每个修复先新增可复现失败测试、独立提交、重新跑受影响样本与全部安全回归。
- [ ] 任一跨用户、账务、事实、沙箱或资产发布门槛失败即 No-Go；不得用平均分或删除失败样本掩盖。
- [ ] 通过测试环境验收只允许发起独立生产评审；不授权生产部署、价格发布、生产密钥配置或内容安全能力豁免。
- [ ] 每个 Task 的 `Required RED anchor` 先写入其声明的测试文件并运行该 Task 的第一条定向命令；实现前只允许因目标接口/行为缺失而失败，最小实现后重跑同一命令必须 exit `0`。真实执行 Task 7 的 RED/GREEN 只验证 preflight，真实 Provider 结果不替代代码测试。
- [ ] `scripts/ai_edit_v3_acceptance.py` 始终只有一个 `if __name__ == "__main__": raise SystemExit(main())` 入口，并且它保持为文件最后两行；后续 Task 必须在该入口之前插入定义并替换/扩展现有 `main()`，不得把新定义追加到入口之后。

---

### Task 1: Define and validate the frozen 20-sample matrix

**Files:**

- Create: `tests/fixtures/ai_edit_v3/acceptance-20.schema.json`
- Create: `tests/fixtures/ai_edit_v3/acceptance-20.json`
- Create: `scripts/ai_edit_v3_acceptance.py`
- Create: `tests/test_ai_edit_v3_acceptance_manifest.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class AcceptanceCase:
    case_id: str
    input_type: str
    creation_mode: str
    source_asset_id: str | None
    source_upload_fixture: str | None
    tts_fixture: Mapping[str, str] | None
    ratio: str
    template_id: str | None
    style_prompt: str | None
    material_fixtures: tuple[str, ...]
    content_category: str
    risk_tags: tuple[str, ...]
    authorization_ref: str

def validate_matrix(path: Path) -> MatrixReport: ...
def materialize_run(matrix: AcceptanceMatrix, environment: str,
                    commit_sha: str) -> RunManifest: ...
```

**Required RED test — write this complete minimal test first:**

```python
# tests/test_ai_edit_v3_acceptance_manifest.py
import json
import tempfile
import unittest
from pathlib import Path

from scripts.ai_edit_v3_acceptance import validate_matrix


class AcceptanceMatrixTests(unittest.TestCase):
    def test_matrix_reports_the_single_missing_input_mode_pair(self) -> None:
        inputs = (
            "platform_talking_head",
            "uploaded_video",
            "existing_audio",
            "uploaded_audio",
            "script_to_audio_video",
        )
        modes = ("ai_auto", "style_prompt", "template_reference")
        omitted = ("uploaded_audio", "template_reference")
        cases = [
            {
                "case_id": f"case_{index:02d}",
                "input_type": input_type,
                "creation_mode": mode,
            }
            for index, (input_type, mode) in enumerate(
                (
                    (input_type, mode)
                    for input_type in inputs
                    for mode in modes
                    if (input_type, mode) != omitted
                ),
                start=1,
            )
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "matrix.json"
            path.write_text(json.dumps({"cases": cases}), encoding="utf-8")
            report = validate_matrix(path)

        self.assertFalse(report.passed)
        self.assertEqual(report.missing_pairs, (omitted,))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **RED:** run `python -m unittest tests.test_ai_edit_v3_acceptance_manifest.AcceptanceMatrixTests.test_matrix_reports_the_single_missing_input_mode_pair -v`.
  Expected: `ERROR` with `ModuleNotFoundError: No module named 'scripts.ai_edit_v3_acceptance'`; an assertion failure is not an acceptable first RED.

**Required minimal GREEN implementation — write the complete code below:**

```python
# scripts/ai_edit_v3_acceptance.py
from __future__ import annotations

import json
import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

INPUT_TYPES = (
    "platform_talking_head",
    "uploaded_video",
    "existing_audio",
    "uploaded_audio",
    "script_to_audio_video",
)
CREATION_MODES = ("ai_auto", "style_prompt", "template_reference")


@dataclass(frozen=True)
class MatrixReport:
    passed: bool
    case_count: int
    duplicate_case_ids: tuple[str, ...]
    missing_pairs: tuple[tuple[str, str], ...]


def validate_matrix(path: Path) -> MatrixReport:
    document = json.loads(path.read_text(encoding="utf-8"))
    cases = document["cases"]
    case_ids = [str(case["case_id"]) for case in cases]
    duplicate_case_ids = tuple(
        sorted({case_id for case_id in case_ids if case_ids.count(case_id) > 1})
    )
    observed_pairs = {
        (str(case["input_type"]), str(case["creation_mode"])) for case in cases
    }
    missing_pairs = tuple(
        (input_type, mode)
        for input_type in INPUT_TYPES
        for mode in CREATION_MODES
        if (input_type, mode) not in observed_pairs
    )
    return MatrixReport(
        passed=(
            len(cases) == 20
            and not duplicate_case_ids
            and not missing_pairs
        ),
        case_count=len(cases),
        duplicate_case_ids=duplicate_case_ids,
        missing_pairs=missing_pairs,
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
        f"invalid matrix: cases={report.case_count}; "
        f"pairs={pair_count}/15; duplicates={len(report.duplicate_case_ids)}"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **GREEN:** rerun `python -m unittest tests.test_ai_edit_v3_acceptance_manifest.AcceptanceMatrixTests.test_matrix_reports_the_single_missing_input_mode_pair -v`.
  Expected: `Ran 1 test` and `OK`.
- [ ] Extend the same test file with literal cases for exactly 20 unique cases, exactly 10 talking-head/video cases and 10 audio-to-video cases, and complete coverage of all `5 × 3` input/mode pairs at least once; extend `validate_matrix()` until each new assertion passes.
- [ ] Add literal high-risk cases covering both ratios, with/without images, semantically incomplete images, ratio mismatch, commercial/knowledge/product/franchise/store categories, at least three horizontal and three vertical `template_reference` cases, and all four published templates.
- [ ] Add Schema and fixture validation requiring SHA-256, media type, duration range, owner test account alias and authorization reference; reject absolute paths, query-string URLs and secret-like values.
- [ ] Populate `acceptance-20.json` with authorized aliases only; load environment-specific stable IDs from an ignored local binding file.
- [ ] Add `test_validate_cli_returns_nonzero_for_an_incomplete_matrix` and `test_validate_cli_prints_exact_success_for_the_frozen_matrix`; call `main(["validate", "--matrix", ...])` directly, assert exit codes `1` and `0`, and assert the successful stdout is exactly `20 cases; 15/15 input-mode pairs; valid`. Task 2 must extend this same dispatcher for `run` and `verify`, not create a second entry point.
- [ ] Run `python scripts/ai_edit_v3_acceptance.py validate --matrix tests/fixtures/ai_edit_v3/acceptance-20.json` and `python -m unittest tests.test_ai_edit_v3_acceptance_manifest -v`.
  Expected: both commands exit `0`; the validator prints `20 cases; 15/15 input-mode pairs; valid`.
- [ ] Commit:

```bash
git add tests/fixtures/ai_edit_v3/acceptance-20.schema.json tests/fixtures/ai_edit_v3/acceptance-20.json scripts/ai_edit_v3_acceptance.py tests/test_ai_edit_v3_acceptance_manifest.py
git commit -m "test(ai-edit-v3): define acceptance sample matrix"
```

### Task 2: Build the API-driven evidence collector

**Files:**

- Modify: `scripts/ai_edit_v3_acceptance.py`
- Create: `server/content_domains/ai_edit_v3/acceptance_export.py`
- Create: `tests/test_ai_edit_v3_acceptance_runner.py`
- Create: `tests/fixtures/ai_edit_v3/acceptance-responses/`

**Interfaces:**

```python
def run_cases(config: AcceptanceConfig, manifest: RunManifest,
              *, concurrency: int, subset: str | None = None) -> RunSummary: ...
def resume_or_create_case(checkpoint: CaseCheckpoint,
                          api: V3Api) -> CaseCheckpoint: ...
def collect_case_evidence(client: V3ApiClient, case: AcceptanceCase,
                          run_dir: Path) -> CaseEvidence: ...
def verify_case_evidence(case_dir: Path, *, strict: bool) -> CaseVerdict: ...
def execute_run_command(args: argparse.Namespace) -> int: ...
def execute_verify_command(args: argparse.Namespace) -> int: ...
def execute_local_fake_run(args: argparse.Namespace) -> int: ...
def execute_preflighted_cases(api: V3Api, args: argparse.Namespace) -> int: ...
def build_real_run_api() -> V3Api: ...
```

**Required RED test — write this complete minimal test first:**

```python
# tests/test_ai_edit_v3_acceptance_runner.py
import unittest

from server.content_domains.ai_edit_v3.acceptance_export import (
    CaseCheckpoint,
    resume_or_create_case,
)


class FakeV3Api:
    def __init__(self) -> None:
        self.created_idempotency_keys: list[str] = []
        self.fetched_job_ids: list[str] = []

    def create_job(self, idempotency_key: str) -> str:
        self.created_idempotency_keys.append(idempotency_key)
        return "unexpected-new-job"

    def get_job(self, job_id: str) -> dict[str, str]:
        self.fetched_job_ids.append(job_id)
        return {"job_id": job_id, "status": "rendering"}


class AcceptanceRunnerTests(unittest.TestCase):
    def test_restart_uses_persisted_job_and_idempotency_key(self) -> None:
        api = FakeV3Api()
        checkpoint = CaseCheckpoint(
            case_id="case_01",
            idempotency_key="acceptance/run-01/case-01",
            job_id="job-17",
        )

        resumed = resume_or_create_case(checkpoint, api)

        self.assertEqual(resumed.job_id, "job-17")
        self.assertEqual(resumed.idempotency_key, "acceptance/run-01/case-01")
        self.assertEqual(api.created_idempotency_keys, [])
        self.assertEqual(api.fetched_job_ids, ["job-17"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **RED:** run `python -m unittest tests.test_ai_edit_v3_acceptance_runner.AcceptanceRunnerTests.test_restart_uses_persisted_job_and_idempotency_key -v`.
  Expected: `ERROR` with `ModuleNotFoundError: No module named 'server.content_domains.ai_edit_v3.acceptance_export'`; the test must not reach a real HTTP endpoint.

**Required minimal GREEN implementation — write the complete code below:**

```python
# server/content_domains/ai_edit_v3/acceptance_export.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class V3Api(Protocol):
    def create_job(self, idempotency_key: str) -> str: ...
    def get_job(self, job_id: str) -> dict[str, str]: ...


@dataclass(frozen=True)
class CaseCheckpoint:
    case_id: str
    idempotency_key: str
    job_id: str | None


def resume_or_create_case(checkpoint: CaseCheckpoint, api: V3Api) -> CaseCheckpoint:
    if checkpoint.job_id is not None:
        api.get_job(checkpoint.job_id)
        return checkpoint
    job_id = api.create_job(checkpoint.idempotency_key)
    return CaseCheckpoint(
        case_id=checkpoint.case_id,
        idempotency_key=checkpoint.idempotency_key,
        job_id=job_id,
    )
```

- [ ] **GREEN:** rerun `python -m unittest tests.test_ai_edit_v3_acceptance_runner.AcceptanceRunnerTests.test_restart_uses_persisted_job_and_idempotency_key -v`.
  Expected: `Ran 1 test` and `OK`; `create_job()` has zero calls.
- [ ] Add protocol-faithful fake tests for session injection through environment or interactive prompt only, owner-scoped upload, quote, `Idempotency-Key`, creation, polling and result retrieval; assert neither CLI args nor reports contain the credential.
- [ ] Extend checkpoints and immutable evidence to include normalized request hash, quote/version/held points, job/attempt/stage timings, plan/schema hashes, material decisions, provider usage, audio evidence, render build/manifest hashes, QC, settlement/refund, publication generation, asset ID and output hash.
- [ ] Keep short playback URLs in process memory only; persist stable COS/object or asset identifiers and prove Range GET verification does not log the signed URL.
- [ ] Write complete CLI dispatch tests before extending Task 1's `main()`: `test_run_cli_calls_execute_run_command_once`, `test_run_cli_propagates_nonzero_exit`, `test_verify_cli_opens_the_named_report_and_runs_strict_verification`, and `test_verify_cli_rejects_missing_or_invalid_evidence`. Invoke `main([...])`, not the handlers directly; patched handlers must record the parsed matrix, run ID, concurrency, subset/report and strict values. Expected exits are the handler's `0/2/3/4`, never an implicit `None -> 0`.
- [ ] Extend the existing `validate` parser with `run` and `verify`, and dispatch explicitly: `run` requires `--environment`, `--matrix`, `--run-id` and `--concurrency`, accepts only the frozen subset values, and calls `execute_run_command(args)`; `verify` requires `--report` plus `--strict` and calls `execute_verify_command(args)`. Unknown/missing commands are argparse exit `2`. Implement `execute_local_fake_run` and `execute_preflighted_cases` over the same `run_cases`/checkpoint/evidence path; both return the documented `0/3/4` result instead of falling through. `build_real_run_api` reads the authenticated test client only from process environment or an interactive secret prompt and never a CLI argument. Before Task 7, `execute_run_command` only accepts `environment=local-fake`; `environment=test` returns preflight exit `2` before building a client or uploading. Every run creates evidence files atomically with exclusive create and includes commit, environment, Schema, template registry and renderer release fingerprints.
- [ ] Add fake end-to-end response fixtures for `completed`, `refunded`, `prehold_absent`, `failed_reconciliation_pending` and `failed_asset_decision_pending`.
- [ ] Run `python -m unittest tests.test_ai_edit_v3_acceptance_runner tests.test_ai_edit_v3_acceptance_manifest -v`.
  Expected: all tests pass; the fake API records no duplicate task or idempotency key.
- [ ] Commit:

```bash
git add scripts/ai_edit_v3_acceptance.py server/content_domains/ai_edit_v3/acceptance_export.py tests/test_ai_edit_v3_acceptance_runner.py tests/fixtures/ai_edit_v3/acceptance-responses
git commit -m "test(ai-edit-v3): collect immutable acceptance evidence"
```

### Task 3: Implement machine output and quality verification

**Files:**

- Create: `server/content_domains/ai_edit_v3/acceptance_verify.py`
- Modify: `scripts/ai_edit_v3_acceptance.py`
- Create: `tests/test_ai_edit_v3_acceptance_verify.py`

**Interfaces:**

```python
def probe_final_output(path: Path) -> OutputProbe: ...
def verify_quality_evidence(evidence: CaseEvidence) -> MachineVerdict: ...
def aggregate_machine_verdicts(verdicts: Sequence[MachineVerdict]) -> MachineSummary: ...
```

**Required RED test — write this complete minimal test first:**

```python
# tests/test_ai_edit_v3_acceptance_verify.py
import unittest

from server.content_domains.ai_edit_v3.acceptance_verify import (
    CaseEvidence,
    verify_quality_evidence,
)


class AcceptanceVerifyTests(unittest.TestCase):
    def test_missing_material_ownership_evidence_fails_closed(self) -> None:
        evidence = CaseEvidence(
            checks={
                "stream_contract": True,
                "decoded_media": True,
                "material_ownership": None,
                "fact_traceability": True,
                "range_get_206": True,
            }
        )

        verdict = verify_quality_evidence(evidence)

        self.assertFalse(verdict.passed)
        self.assertEqual(
            verdict.blockers,
            ("quality_evidence_missing:material_ownership",),
        )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **RED:** run `python -m unittest tests.test_ai_edit_v3_acceptance_verify.AcceptanceVerifyTests.test_missing_material_ownership_evidence_fails_closed -v`.
  Expected: `ERROR` with `ModuleNotFoundError: No module named 'server.content_domains.ai_edit_v3.acceptance_verify'`.

**Required minimal GREEN implementation — write the complete code below:**

```python
# server/content_domains/ai_edit_v3/acceptance_verify.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

BLOCKING_CHECKS = (
    "stream_contract",
    "decoded_media",
    "material_ownership",
    "fact_traceability",
    "range_get_206",
)


@dataclass(frozen=True)
class CaseEvidence:
    checks: Mapping[str, bool | None]


@dataclass(frozen=True)
class MachineVerdict:
    passed: bool
    blockers: tuple[str, ...]


def verify_quality_evidence(evidence: CaseEvidence) -> MachineVerdict:
    blockers: list[str] = []
    for name in BLOCKING_CHECKS:
        value = evidence.checks.get(name)
        if value is None:
            blockers.append(f"quality_evidence_missing:{name}")
        elif value is not True:
            blockers.append(f"quality_evidence_failed:{name}")
    return MachineVerdict(passed=not blockers, blockers=tuple(blockers))
```

- [ ] **GREEN:** rerun `python -m unittest tests.test_ai_edit_v3_acceptance_verify.AcceptanceVerifyTests.test_missing_material_ownership_evidence_fails_closed -v`.
  Expected: `Ran 1 test` and `OK`.
- [ ] Extend the RED table tests and implementation to require exactly one video and one audio stream, 1920×1080 or 1080×1920, H.264, AAC, yuv420p, 48 kHz stereo, monotonic PTS, and A/V duration error no greater than both one frame and 40 ms.
- [ ] Add argument-list FFprobe/FFmpeg full-decode tests for `-16 LUFS ± 2 LU`, true peak `<= -1 dBTP`, no duplicate dialogue, no abnormal silence and sampled talking-head lip/audio offset `<=80 ms`; parse stream and metric JSON rather than trusting exit code alone.
- [ ] Add required blocking evidence for plan/schema/manifest hashes, resolved required material, prohibited historical/public/other-user source, visible fact traceability, accurate three-second hook, unique master audio and Range GET HTTP `206`.
- [ ] Run `python -m unittest tests.test_ai_edit_v3_acceptance_verify -v` against the declared known-good and known-bad local MP4 fixtures.
  Expected: the good fixture passes; every bad fixture fails with its exact `quality_evidence_failed:<name>` blocker.
- [ ] Commit:

```bash
git add server/content_domains/ai_edit_v3/acceptance_verify.py scripts/ai_edit_v3_acceptance.py tests/test_ai_edit_v3_acceptance_verify.py
git commit -m "test(ai-edit-v3): verify rendered acceptance outputs"
```

### Task 4: Define blinded human creative review

**Files:**

- Create: `tests/fixtures/ai_edit_v3/human-review.schema.json`
- Create: `docs/operations/ai-edit-v3-human-review.md`
- Modify: `server/content_domains/ai_edit_v3/acceptance_export.py`
- Modify: `scripts/ai_edit_v3_acceptance.py`
- Create: `tests/test_ai_edit_v3_human_review.py`

**Interfaces:**

```python
def validate_human_review(path: Path, *, expected_cases: Collection[str],
                          reviewer_id: str) -> HumanReview: ...
def reconcile_human_reviews(first: HumanReview, second: HumanReview,
                            tiebreak: HumanReview | None) -> HumanSummary: ...
```

**Frozen scoring contract:**

- The eight dimensions, in order, are `事实准确`, `素材相关`, `前三秒钩子`, `叙事节奏`, `布局清晰`, `字幕可读`, `声音质量`, `视觉一致性`.
- Each score is the integer `0`, `1` or `2` and must be justified against the following literal anchor text; `bool`, fractions, omitted dimensions and additional dimensions are invalid.

```python
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
```

- A reviewer’s personal decision is pass only when their eight-dimension total is at least `13/16` and their scores for all four critical dimensions `事实准确`, `素材相关`, `字幕可读`, `声音质量` are greater than `0`.
- Reviewer 1 and reviewer 2 must be different people and different in-memory/deserialized review objects. If their personal pass booleans differ for a case, that case requires reviewer 3. No other score-distance heuristic triggers reviewer 3.
- Without reviewer 3, the case passes only when the two totals average at least `13/16` and both reviewers score every critical dimension greater than `0`.
- With reviewer 3, the case passes only when all three totals average at least `13/16`, at least two of the three personal decisions pass, and all three reviewers score every critical dimension greater than `0`.
- Reviewer 3 must have a third unique `reviewer_id`, a distinct review object, and scores for exactly the disputed cases. Supplying reviewer 3 for a non-disputed case or omitting any disputed case is invalid.

**Required RED test — write this complete minimal test first:**

```python
# tests/test_ai_edit_v3_human_review.py
import unittest

from server.content_domains.ai_edit_v3.acceptance_export import (
    DIMENSION_ANCHORS,
    HumanReview,
    reconcile_human_reviews,
)


def scores(*, facts: int = 2, materials: int = 2, hook: int = 2,
           narrative: int = 1, layout: int = 1, captions: int = 2,
           audio: int = 2, visual: int = 1) -> dict[str, int]:
    return {
        "事实准确": facts,
        "素材相关": materials,
        "前三秒钩子": hook,
        "叙事节奏": narrative,
        "布局清晰": layout,
        "字幕可读": captions,
        "声音质量": audio,
        "视觉一致性": visual,
    }


class HumanReviewTests(unittest.TestCase):
    def test_same_review_object_cannot_fill_both_reviewer_slots(self) -> None:
        review = HumanReview("reviewer-a", {"case_01": scores()})
        with self.assertRaisesRegex(ValueError, "^review_object_reused$"):
            reconcile_human_reviews(review, review, None)

    def test_duplicate_reviewer_id_is_rejected_for_distinct_objects(self) -> None:
        first = HumanReview("reviewer-a", {"case_01": scores()})
        second = HumanReview("reviewer-a", {"case_01": scores()})
        with self.assertRaisesRegex(ValueError, "^reviewer_id_reused$"):
            reconcile_human_reviews(first, second, None)

    def test_two_reviewer_average_13_with_nonzero_critical_scores_passes(self) -> None:
        first = HumanReview("reviewer-a", {"case_01": scores()})
        second = HumanReview("reviewer-b", {"case_01": scores()})

        summary = reconcile_human_reviews(first, second, None)

        self.assertTrue(summary.cases["case_01"].publishable)
        self.assertEqual(summary.cases["case_01"].average_total, 13)

    def test_split_personal_decision_requires_unique_third_reviewer(self) -> None:
        first = HumanReview("reviewer-a", {"case_01": scores()})
        second = HumanReview(
            "reviewer-b",
            {"case_01": scores(materials=0, narrative=2)},
        )
        with self.assertRaisesRegex(
            ValueError, "^third_reviewer_required:case_01$"
        ):
            reconcile_human_reviews(first, second, None)

    def test_three_reviewer_rule_uses_average_votes_and_all_critical_scores(self) -> None:
        first = HumanReview("reviewer-a", {"case_01": scores()})
        second = HumanReview(
            "reviewer-b",
            {"case_01": scores(materials=0, narrative=2)},
        )
        third = HumanReview("reviewer-c", {"case_01": scores()})

        summary = reconcile_human_reviews(first, second, third)

        self.assertFalse(summary.cases["case_01"].publishable)
        self.assertEqual(
            summary.cases["case_01"].reason,
            "critical_dimension_zero:素材相关",
        )

    def test_three_reviewer_average_13_and_two_personal_passes_is_publishable(self) -> None:
        first = HumanReview(
            "reviewer-a", {"case_01": scores(narrative=2)}
        )
        second = HumanReview(
            "reviewer-b", {"case_01": scores(visual=0)}
        )
        third = HumanReview(
            "reviewer-c", {"case_01": scores()}
        )

        summary = reconcile_human_reviews(first, second, third)

        self.assertTrue(summary.cases["case_01"].publishable)
        self.assertEqual(summary.cases["case_01"].average_total, 13)
        self.assertEqual(summary.cases["case_01"].personal_passes, 2)

    def test_literal_anchor_table_has_eight_dimensions_and_three_scores(self) -> None:
        self.assertEqual(
            tuple(DIMENSION_ANCHORS),
            (
                "事实准确", "素材相关", "前三秒钩子", "叙事节奏",
                "布局清晰", "字幕可读", "声音质量", "视觉一致性",
            ),
        )
        self.assertTrue(
            all(tuple(anchors) == (0, 1, 2) for anchors in DIMENSION_ANCHORS.values())
        )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **RED:** run `python -m unittest tests.test_ai_edit_v3_human_review.HumanReviewTests -v`.
  Expected: `ERROR` with `ImportError: cannot import name 'DIMENSION_ANCHORS'`; a reused object must not be silently accepted.

**Required minimal GREEN implementation — write this complete code:**

```python
# append to server/content_domains/ai_edit_v3/acceptance_export.py
from dataclasses import dataclass
from typing import Mapping

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


def _validated_scores(raw: Mapping[str, int]) -> dict[str, int]:
    if len(raw) != len(DIMENSIONS) or set(raw) != set(DIMENSIONS):
        raise ValueError("human_review_dimensions_invalid")
    values = dict(raw)
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value not in (0, 1, 2)
        for value in values.values()
    ):
        raise ValueError("human_review_score_invalid")
    return values


def _personal_pass(values: Mapping[str, int]) -> bool:
    return (
        sum(values.values()) >= 13
        and all(values[name] > 0 for name in CRITICAL_DIMENSIONS)
    )


def _assert_distinct_reviews(*reviews: HumanReview | None) -> None:
    present = [review for review in reviews if review is not None]
    if len({id(review) for review in present}) != len(present):
        raise ValueError("review_object_reused")
    reviewer_ids = [review.reviewer_id for review in present]
    if len(set(reviewer_ids)) != len(reviewer_ids):
        raise ValueError("reviewer_id_reused")


def reconcile_human_reviews(
    first: HumanReview,
    second: HumanReview,
    third: HumanReview | None,
) -> HumanSummary:
    _assert_distinct_reviews(first, second, third)
    if set(first.cases) != set(second.cases):
        raise ValueError("primary_case_set_mismatch")

    first_scores = {
        case_id: _validated_scores(raw) for case_id, raw in first.cases.items()
    }
    second_scores = {
        case_id: _validated_scores(raw) for case_id, raw in second.cases.items()
    }
    disputed = {
        case_id
        for case_id in first_scores
        if _personal_pass(first_scores[case_id])
        != _personal_pass(second_scores[case_id])
    }
    if disputed and third is None:
        raise ValueError(f"third_reviewer_required:{sorted(disputed)[0]}")
    if third is not None and set(third.cases) != disputed:
        raise ValueError("third_reviewer_case_set_invalid")

    third_scores = (
        {
            case_id: _validated_scores(raw)
            for case_id, raw in third.cases.items()
        }
        if third is not None
        else {}
    )
    decisions: dict[str, HumanCaseDecision] = {}
    for case_id in first_scores:
        score_sets = [first_scores[case_id], second_scores[case_id]]
        if case_id in disputed:
            score_sets.append(third_scores[case_id])
        totals = [sum(values.values()) for values in score_sets]
        average_total = sum(totals) / len(totals)
        personal_passes = sum(_personal_pass(values) for values in score_sets)
        zero_critical = next(
            (
                name
                for name in CRITICAL_DIMENSIONS
                if any(values[name] == 0 for values in score_sets)
            ),
            None,
        )
        required_votes = 2 if len(score_sets) == 3 else 0
        publishable = (
            average_total >= 13
            and zero_critical is None
            and (len(score_sets) == 2 or personal_passes >= required_votes)
        )
        if zero_critical is not None:
            reason = f"critical_dimension_zero:{zero_critical}"
        elif average_total < 13:
            reason = "average_total_below_13"
        elif len(score_sets) == 3 and personal_passes < required_votes:
            reason = "personal_pass_votes_below_2"
        else:
            reason = "publishable"
        decisions[case_id] = HumanCaseDecision(
            publishable=publishable,
            average_total=average_total,
            personal_passes=personal_passes,
            reviewer_count=len(score_sets),
            reason=reason,
        )
    return HumanSummary(cases=decisions)
```

- [ ] **GREEN:** rerun `python -m unittest tests.test_ai_edit_v3_human_review.HumanReviewTests -v`.
  Expected: `Ran 7 tests` and `OK`.
- [ ] Encode the literal anchors and exact reconciliation contract in `human-review.schema.json` and `ai-edit-v3-human-review.md`; export a blind package that hides renderer, template, prompt, provider and case implementation metadata.
- [ ] Extend tests to all 20 cases, require at least 16 publishable, reject self-review and invalid reviewer IDs, and verify the two-person and three-person average formulas exactly.
- [ ] Add creative distribution evidence: at least eight layouts, at least two variants for each used layout, no layout above 35% of scenes and no more than two consecutive identical layouts without an accepted `continuity_reason`.
- [ ] Run `python -m unittest tests.test_ai_edit_v3_human_review -v` with one synthetic passing package and one mechanical-template failure.
  Expected: all unit tests pass; the pass package reports `>=16/20`; the mechanical package is rejected.
- [ ] Commit:

```bash
git add tests/fixtures/ai_edit_v3/human-review.schema.json docs/operations/ai-edit-v3-human-review.md scripts/ai_edit_v3_acceptance.py server/content_domains/ai_edit_v3/acceptance_export.py tests/test_ai_edit_v3_human_review.py
git commit -m "test(ai-edit-v3): add blinded creative review"
```

### Task 5: Build the crash, unknown-response and isolation fault matrix

**Files:**

- Create: `scripts/ai_edit_v3_fault_matrix.py`
- Create: `tests/test_ai_edit_v3_fault_matrix.py`
- Create: `tests/fixtures/ai_edit_v3/fault-matrix.json`

**Interfaces:**

```python
def enumerate_fault_points() -> tuple[FaultCase, ...]: ...
def build_fault_harness(environment: Literal["local-fake", "test"]
                        ) -> FaultHarness: ...
def run_fault_case(case: FaultCase, harness: FaultHarness) -> FaultVerdict: ...
def assert_authoritative_convergence(verdict: FaultVerdict) -> None: ...
def execute_fault_matrix(environment: Literal["local-fake", "test"],
                         *, strict: bool) -> FaultMatrixReport: ...
def main(argv: Sequence[str] | None = None) -> int: ...
```

**Required RED test — write this complete minimal test first:**

```python
# tests/test_ai_edit_v3_fault_matrix.py
import unittest

from scripts.ai_edit_v3_fault_matrix import (
    FaultVerdict,
    assert_authoritative_convergence,
)


class FaultMatrixTests(unittest.TestCase):
    def test_publish_response_loss_converges_once_without_refund(self) -> None:
        verdict = FaultVerdict(
            final_state="completed",
            confirmed_preheld_points=64,
            refunded_points=0,
            visible_asset_count=1,
            provider_submit_count=1,
            billing_request_count=1,
            publication_winner="publish_won",
        )

        assert_authoritative_convergence(verdict)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **RED:** run `python -m unittest tests.test_ai_edit_v3_fault_matrix.FaultMatrixTests.test_publish_response_loss_converges_once_without_refund -v`.
  Expected: `ERROR` with `ModuleNotFoundError: No module named 'scripts.ai_edit_v3_fault_matrix'`.

**Required minimal GREEN implementation — write the complete code below:**

```python
# scripts/ai_edit_v3_fault_matrix.py
from __future__ import annotations

from dataclasses import dataclass

ALLOWED_FINAL_STATES = {
    "completed",
    "refunded",
    "prehold_absent",
    "failed_reconciliation_pending",
    "failed_asset_decision_pending",
}


@dataclass(frozen=True)
class FaultVerdict:
    final_state: str
    confirmed_preheld_points: int
    refunded_points: int
    visible_asset_count: int
    provider_submit_count: int
    billing_request_count: int
    publication_winner: str | None


def assert_authoritative_convergence(verdict: FaultVerdict) -> None:
    if verdict.final_state not in ALLOWED_FINAL_STATES:
        raise AssertionError(f"non_authoritative_state:{verdict.final_state}")
    if not 0 <= verdict.refunded_points <= verdict.confirmed_preheld_points:
        raise AssertionError("refund_exceeds_confirmed_prehold")
    if verdict.provider_submit_count > 1:
        raise AssertionError("duplicate_provider_submit")
    if verdict.billing_request_count > 1:
        raise AssertionError("duplicate_billing_request")
    if verdict.visible_asset_count not in (0, 1):
        raise AssertionError("duplicate_visible_asset")
    if verdict.final_state == "completed":
        if verdict.publication_winner != "publish_won":
            raise AssertionError("completed_without_publish_winner")
        if verdict.visible_asset_count != 1:
            raise AssertionError("completed_without_one_visible_asset")
        if verdict.refunded_points != 0:
            raise AssertionError("completed_was_refunded")
    if verdict.publication_winner == "cancel_won" and verdict.visible_asset_count:
        raise AssertionError("asset_visible_after_cancel_won")
```

- [ ] **GREEN:** rerun `python -m unittest tests.test_ai_edit_v3_fault_matrix.FaultMatrixTests.test_publish_response_loss_converges_once_without_refund -v`.
  Expected: `Ran 1 test` and `OK`.
- [ ] Define the literal fault matrix JSON and table tests for process kill immediately before/after every persistent transition, provider intent/result bind, COS upload, pre-debit/refund request and publication operation.
- [ ] Add two-worker lease competition, expiry/reclaim and stale fencing writes across transitions, checkpoints, provider result, billing intent and delivery intent.
- [ ] Add billing accepted-response-lost, rejection and five-minute outage for pre-debit, delta refund and full refund; assert cumulative refund never exceeds confirmed preheld points.
- [ ] Add `register_generation`, `prepare_hidden`, `commit_publish`, `cancel_publish` and `query_decision` response loss/outage; assert one irreversible `publish_won` or `cancel_won` and no visibility before publish wins.
- [ ] Add Chromium crash/OOM/timeout, FFmpeg child leak, network attempt, path traversal, symlink, hardlink, device file, TOCTOU swap, image bomb, environment secret read, sibling-job read and systemd property/unit injection.
- [ ] Keep fault hooks test-build-only and add a build-time assertion that production config cannot import or enable them. Implement `build_fault_harness("local-fake")` with injected local fakes. Implement `build_fault_harness("test")` as an API/process-control harness that checks `AI_EDIT_V3_FAULT_AUTHORIZATION_REF`, the test-only environment marker and deployed SHA before exposing any hook; missing/mismatched authority raises `FaultHarnessUnavailable` before a task or point mutation. Production is not an accepted environment value.
- [ ] Write `test_cli_run_executes_every_declared_local_fake_case` before adding the CLI. It must invoke `main(["run", "--environment", "local-fake", "--strict"])`, capture stdout, assert exit `0`, assert the reported executed IDs equal `enumerate_fault_points()` in order, and assert every verdict passed `assert_authoritative_convergence`. Add a failing-case test that expects exit `1`; a parser-only test or patched always-passing runner is not sufficient.
- [ ] Implement the one real CLI dispatcher by merging these definitions and imports into `ai_edit_v3_fault_matrix.py`; `build_fault_harness("test")` must remain fail closed until Task 7 supplies an authorized test-environment harness:

```python
import argparse
import json
from dataclasses import asdict
from typing import Literal, Sequence


@dataclass(frozen=True)
class FaultMatrixReport:
    passed: bool
    executed_case_ids: tuple[str, ...]
    failures: tuple[str, ...]


def execute_fault_matrix(
    environment: Literal["local-fake", "test"],
    *,
    strict: bool,
) -> FaultMatrixReport:
    harness = build_fault_harness(environment)
    executed: list[str] = []
    failures: list[str] = []
    for case in enumerate_fault_points():
        executed.append(case.case_id)
        try:
            verdict = run_fault_case(case, harness)
            assert_authoritative_convergence(verdict)
        except Exception as error:
            failures.append(f"{case.case_id}:{type(error).__name__}")
    return FaultMatrixReport(
        passed=(strict and bool(executed) and not failures),
        executed_case_ids=tuple(executed),
        failures=tuple(failures),
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--environment", choices=("local-fake", "test"), required=True)
    run.add_argument("--strict", action="store_true", required=True)
    args = parser.parse_args(argv)
    report = execute_fault_matrix(args.environment, strict=args.strict)
    print(json.dumps(asdict(report), ensure_ascii=False, sort_keys=True))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] Run `python -m unittest tests.test_ai_edit_v3_fault_matrix -v` and `python scripts/ai_edit_v3_fault_matrix.py run --environment local-fake --strict`.
  Expected: both exit `0`; no duplicate charge/refund/provider submit, cross-job read, duplicate visible asset or permanent `running` stage.
- [ ] Commit:

```bash
git add scripts/ai_edit_v3_fault_matrix.py tests/test_ai_edit_v3_fault_matrix.py tests/fixtures/ai_edit_v3/fault-matrix.json
git commit -m "test(ai-edit-v3): exercise crash safety and isolation"
```

### Task 6: Measure five-concurrent baseline and ten-concurrent stress

**Files:**

- Create: `scripts/ai_edit_v3_capacity.py`
- Create: `tests/test_ai_edit_v3_capacity.py`
- Create: `tests/fixtures/ai_edit_v3/capacity-synthetic.json`
- Create: `docs/operations/ai-edit-v3-capacity.md`

**Interfaces:**

```python
def read_host_capacity() -> HostCapacity: ...
def validate_capacity(profile: Literal["parallel-5", "stress-10"],
                      host: HostCapacity) -> CapacityDecision: ...
def aggregate_capacity(run: RunSummary) -> CapacityReport: ...
def verify_capacity_fixture(path: Path) -> CapacityFixtureReport: ...
def main(argv: Sequence[str] | None = None) -> int: ...
```

**Required RED test — write this complete minimal test first:**

```python
# tests/test_ai_edit_v3_capacity.py
import unittest

from scripts.ai_edit_v3_capacity import HostCapacity, validate_capacity


class CapacityTests(unittest.TestCase):
    def test_stress_profile_is_blocked_on_parallel_five_host(self) -> None:
        decision = validate_capacity(
            "stress-10",
            HostCapacity(
                vcpu=8,
                ram_gib=16,
                temp_gib=80,
                pipeline_concurrency=5,
                render_slots=2,
            ),
        )
        self.assertEqual(decision.status, "capacity_blocked")
        self.assertFalse(decision.may_lower_quality_or_sandbox)
        self.assertEqual(
            decision.reasons,
            (
                "vcpu<16",
                "ram_gib<32",
                "temp_gib<160",
                "pipeline_concurrency<10",
                "render_slots<4",
            ),
        )


if __name__ == "__main__":
    unittest.main()
```

- [ ] **RED:** run `python -m unittest tests.test_ai_edit_v3_capacity.CapacityTests.test_stress_profile_is_blocked_on_parallel_five_host -v`.
  Expected: `ERROR` with `ModuleNotFoundError: No module named 'scripts.ai_edit_v3_capacity'`.

**Required minimal GREEN implementation — write the complete code below:**

```python
# scripts/ai_edit_v3_capacity.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class HostCapacity:
    vcpu: int
    ram_gib: int
    temp_gib: int
    pipeline_concurrency: int
    render_slots: int


@dataclass(frozen=True)
class CapacityDecision:
    status: Literal["ready", "capacity_blocked"]
    reasons: tuple[str, ...]
    may_lower_quality_or_sandbox: bool = False


PROFILE_MINIMUMS = {
    "parallel-5": HostCapacity(8, 16, 80, 5, 2),
    "stress-10": HostCapacity(16, 32, 160, 10, 4),
}


def validate_capacity(
    profile: Literal["parallel-5", "stress-10"],
    host: HostCapacity,
) -> CapacityDecision:
    required = PROFILE_MINIMUMS[profile]
    reasons = tuple(
        f"{name}<{getattr(required, name)}"
        for name in (
            "vcpu",
            "ram_gib",
            "temp_gib",
            "pipeline_concurrency",
            "render_slots",
        )
        if getattr(host, name) < getattr(required, name)
    )
    return CapacityDecision(
        status="capacity_blocked" if reasons else "ready",
        reasons=reasons,
        may_lower_quality_or_sandbox=False,
    )
```

- [ ] **GREEN:** rerun `python -m unittest tests.test_ai_edit_v3_capacity.CapacityTests.test_stress_profile_is_blocked_on_parallel_five_host -v`.
  Expected: `Ran 1 test` and `OK`.
- [ ] Add table tests for the exact five-task minimum `(8 vCPU, 16 GiB RAM, 80 GiB free temp SSD, pipeline_concurrency=5, render_slots=2)` and ten-task minimum `(16, 32, 160, 10, 4)`.
- [ ] Add pre-debit rejection when queue depth is greater than `50` or reserved temp disk is insufficient; return `capacity_unavailable` and a positive `Retry-After`.
- [ ] Aggregate queue wait, end-to-end and stage p50/p95, CPU/RAM/disk peaks, render-slot occupancy, backpressure, timeouts and sandbox resource-limit events.
- [ ] Enforce parallel-five p50 `<=25 minutes` and p95 `<=45 minutes`; enforce ten-task safety with no crash, cross-lineage access, duplicate call or billing corruption.
- [ ] Never lower resolution, disable sandboxing or weaken QC to make a profile pass; report the frozen `capacity_blocked` result with measurements.
- [ ] Write `test_verify_cli_reads_fixture_and_reports_measured_counts` before adding the CLI. It must call `main(["verify", "--fixture", fixture])`, assert the real JSON fixture was opened, assert stdout contains the exact measured numerator and denominator from that fixture, and assert exit `0`; a malformed fixture and a mismatched expected status must each return `1`.
- [ ] Implement the one real capacity dispatcher by merging these definitions and imports into `ai_edit_v3_capacity.py` after the aggregate logic is green:

```python
import argparse
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping, Sequence


@dataclass(frozen=True)
class CapacityFixtureReport:
    passed: bool
    profile: str
    expected_status: str
    observed_status: str
    measured_numerator: int
    measured_denominator: int


def verify_capacity_fixture(path: Path) -> CapacityFixtureReport:
    payload: Mapping[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    host = HostCapacity(**payload["host"])
    decision = validate_capacity(payload["profile"], host)
    numerator = int(payload["measured"]["numerator"])
    denominator = int(payload["measured"]["denominator"])
    expected = str(payload["expected_status"])
    return CapacityFixtureReport(
        passed=(
            denominator > 0
            and 0 <= numerator <= denominator
            and decision.status == expected
        ),
        profile=str(payload["profile"]),
        expected_status=expected,
        observed_status=decision.status,
        measured_numerator=numerator,
        measured_denominator=denominator,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--fixture", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        report = verify_capacity_fixture(args.fixture)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return 1
    print(json.dumps(asdict(report), ensure_ascii=False, sort_keys=True))
    return 0 if report.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] Run `python -m unittest tests.test_ai_edit_v3_capacity -v` and `python scripts/ai_edit_v3_capacity.py verify --fixture tests/fixtures/ai_edit_v3/capacity-synthetic.json`.
  Expected: all tests pass and the synthetic report includes exact measured numerator/denominator fields.
- [ ] Commit:

```bash
git add scripts/ai_edit_v3_capacity.py tests/test_ai_edit_v3_capacity.py tests/fixtures/ai_edit_v3/capacity-synthetic.json docs/operations/ai-edit-v3-capacity.md
git commit -m "test(ai-edit-v3): measure capacity and backpressure"
```

### Task 7: Execute the authorized real test-environment acceptance run

**Files:**

- Modify: `scripts/ai_edit_v3_acceptance.py`
- Modify: `tests/test_ai_edit_v3_acceptance_runner.py`
- Runtime output only, gitignored: `.artifacts/ai-edit-v3/acceptance/${run_id}/`
- Create after redaction: `docs/verification/ai-edit-v3-acceptance-${run_id}.md`

**Operational interface:**

```text
ai_edit_v3_acceptance.py run
  --environment test
  --matrix tests/fixtures/ai_edit_v3/acceptance-20.json
  --run-id <UUID supplied by AI_EDIT_V3_ACCEPTANCE_RUN_ID>
  --concurrency <1|5|10>
  [--subset <parallel-5|stress-10>]

Input authority: deployed capability response, gitignored local asset-binding manifest and persisted run manifest.
Output authority: the first invocation exclusively creates `.artifacts/ai-edit-v3/acceptance/${run_id}/`; each invocation then exclusively creates one immutable `single`, `parallel-5` or `stress-10` profile below that run and atomically refreshes the aggregate `acceptance.json`. Reusing an existing profile is exit `4`; later profiles under the same run ID are valid and cannot overwrite earlier evidence.
Exit codes: 0=all requested cases collected; 2=preflight/authorization mismatch before any task; 3=one or more tasks reached a non-success terminal state; 4=evidence integrity failure.
```

**Required RED test — write this complete minimal preflight test first:**

```python
# append to tests/test_ai_edit_v3_acceptance_runner.py
import unittest
from unittest.mock import patch

from scripts.ai_edit_v3_acceptance import RealRunConfig, main, run_real_acceptance


class FakeRealRunApi:
    def __init__(self) -> None:
        self.upload_calls: list[str] = []

    def capabilities(self) -> dict[str, object]:
        return {
            "deployed_sha": "def",
            "environment": "test",
            "v3_enabled": True,
            "providers_ready": True,
            "active_v3_jobs": 0,
        }

    def upload_authorized_sources(self) -> None:
        self.upload_calls.append("upload")


class AcceptanceCliTests(unittest.TestCase):
    def test_real_runner_refuses_deployed_sha_mismatch_before_upload(self) -> None:
        api = FakeRealRunApi()
        result = run_real_acceptance(
            api,
            RealRunConfig(
                expected_sha="abc",
                environment="test",
                authorization_ref="approval-2026-07-31",
            ),
        )

        self.assertEqual(result.exit_code, 2)
        self.assertEqual(result.reason, "deployed_sha_mismatch")
        self.assertEqual(api.upload_calls, [])

    def test_test_environment_cli_uses_preflight_before_any_upload(self) -> None:
        api = FakeRealRunApi()
        with patch.dict(
            "os.environ",
            {
                "AI_EDIT_V3_EXPECTED_SHA": "abc",
                "AI_EDIT_V3_ACCEPTANCE_AUTHORIZATION_REF": "approval-test-only",
            },
            clear=False,
        ):
            with patch(
                "scripts.ai_edit_v3_acceptance.build_real_run_api",
                return_value=api,
            ):
                exit_code = main([
                    "run", "--environment", "test",
                    "--matrix", "tests/fixtures/ai_edit_v3/acceptance-20.json",
                    "--run-id", "00000000-0000-4000-8000-000000000001",
                    "--concurrency", "1",
                ])

        self.assertEqual(exit_code, 2)
        self.assertEqual(api.upload_calls, [])
```

- [ ] **RED:** run `python -m unittest tests.test_ai_edit_v3_acceptance_runner.AcceptanceCliTests.test_real_runner_refuses_deployed_sha_mismatch_before_upload -v`.
  Expected: `ERROR` with `ImportError: cannot import name 'RealRunConfig'`; `upload_authorized_sources()` must not run.

**Required minimal GREEN implementation — insert this complete code before the single final entry-point guard in `scripts/ai_edit_v3_acceptance.py`, and replace Task 2's provisional `execute_run_command`:**

```python
import os
from dataclasses import dataclass
from typing import Protocol


class RealRunApi(Protocol):
    def capabilities(self) -> dict[str, object]: ...
    def upload_authorized_sources(self) -> None: ...


@dataclass(frozen=True)
class RealRunConfig:
    expected_sha: str
    environment: str
    authorization_ref: str


@dataclass(frozen=True)
class RealRunResult:
    exit_code: int
    reason: str


def run_real_acceptance(api: RealRunApi, config: RealRunConfig) -> RealRunResult:
    if config.environment != "test":
        return RealRunResult(2, "environment_not_test")
    if not config.authorization_ref.strip():
        return RealRunResult(2, "authorization_missing")
    capabilities = api.capabilities()
    if capabilities.get("environment") != "test":
        return RealRunResult(2, "deployed_environment_mismatch")
    if capabilities.get("deployed_sha") != config.expected_sha:
        return RealRunResult(2, "deployed_sha_mismatch")
    if capabilities.get("active_v3_jobs") != 0:
        return RealRunResult(2, "active_v3_jobs")
    if capabilities.get("v3_enabled") is not True:
        return RealRunResult(2, "v3_not_enabled")
    if capabilities.get("providers_ready") is not True:
        return RealRunResult(2, "providers_not_ready")
    api.upload_authorized_sources()
    return RealRunResult(0, "preflight_passed")


def execute_run_command(args: argparse.Namespace) -> int:
    if args.environment == "local-fake":
        return execute_local_fake_run(args)
    expected_sha = os.environ.get("AI_EDIT_V3_EXPECTED_SHA", "").strip()
    authorization_ref = os.environ.get(
        "AI_EDIT_V3_ACCEPTANCE_AUTHORIZATION_REF", ""
    ).strip()
    if not expected_sha or not authorization_ref:
        return 2
    api = build_real_run_api()
    result = run_real_acceptance(
        api,
        RealRunConfig(
            expected_sha=expected_sha,
            environment=args.environment,
            authorization_ref=authorization_ref,
        ),
    )
    if result.exit_code != 0:
        return result.exit_code
    return execute_preflighted_cases(api, args)
```

- [ ] **GREEN:** rerun `python -m unittest tests.test_ai_edit_v3_acceptance_runner.AcceptanceCliTests.test_real_runner_refuses_deployed_sha_mismatch_before_upload tests.test_ai_edit_v3_acceptance_runner.AcceptanceCliTests.test_test_environment_cli_uses_preflight_before_any_upload -v`.
  Expected: `Ran 2 tests` and `OK`; upload call count remains zero in both paths.
- [ ] Add table tests for every exit-2 condition (`environment_not_test`, missing authorization, environment/SHA mismatch, active jobs, feature disabled and provider unavailable), and one `main(["run", ...])` pass case that asserts exactly one `upload_authorized_sources()` call and exactly one `execute_preflighted_cases(api, args)` call. `execute_preflighted_cases` consumes the already-uploaded, owner-bound inputs from the successful preflight and must never call `upload_authorized_sources()` again.
- [ ] Obtain explicit authorization for test deployment, test credentials, real Provider calls and test point mutations; record the authorization reference in the run manifest.
- [ ] Confirm all Phase A–D PRs are merged, main CI is green, there are no active V3 jobs, the deployed SHA equals the intended SHA and `GET /api/v3/edit/capabilities` reports every required provider as configured and wired.
- [ ] Freeze one UUID for every Task 7 command in the current PowerShell session; reject a reused output directory:

```powershell
$env:AI_EDIT_V3_ACCEPTANCE_RUN_ID = [guid]::NewGuid().ToString('D')
$runId = $env:AI_EDIT_V3_ACCEPTANCE_RUN_ID
$runDir = Join-Path '.artifacts/ai-edit-v3/acceptance' $runId
$acceptanceReport = "docs/verification/ai-edit-v3-acceptance-$runId.md"
if (Test-Path -LiteralPath $runDir) { throw "acceptance run already exists: $runId" }
```

- [ ] Execute `python scripts/ai_edit_v3_acceptance.py validate --matrix tests/fixtures/ai_edit_v3/acceptance-20.json`.
- [ ] Execute `python scripts/ai_edit_v3_acceptance.py run --environment test --matrix tests/fixtures/ai_edit_v3/acceptance-20.json --run-id $runId --concurrency 1` and retain all 20 cases, including failures.
- [ ] Execute both blinded human reviews and any required tiebreak; import them with `python scripts/ai_edit_v3_acceptance.py verify --report "$runDir/acceptance.json" --strict`.
- [ ] Execute `python scripts/ai_edit_v3_fault_matrix.py run --environment test --strict` in the authorized fault-test deployment, not on production.
- [ ] Execute `python scripts/ai_edit_v3_acceptance.py run --environment test --matrix tests/fixtures/ai_edit_v3/acceptance-20.json --run-id $runId --subset parallel-5 --concurrency 5`.
- [ ] When the host preflight passes, execute `python scripts/ai_edit_v3_acceptance.py run --environment test --matrix tests/fixtures/ai_edit_v3/acceptance-20.json --run-id $runId --subset stress-10 --concurrency 10`; otherwise persist the measured `capacity_blocked` result under the same run ID without starting ten tasks.
- [ ] Redact credentials, private text beyond required evidence and short URLs; hash the evidence directory and write the immutable hash into the Markdown summary.
- [ ] Run `python -m unittest tests.test_ai_edit_v3_acceptance_runner -v` after evidence collection.
  Expected: all tests pass; no test contacts a real Provider and the committed report contains no credential or short URL.
- [ ] Do not commit media or raw private evidence. After user approval of the report content, commit:

```powershell
if (-not (Test-Path -LiteralPath $acceptanceReport -PathType Leaf)) { throw "acceptance report missing: $acceptanceReport" }
git add -- scripts/ai_edit_v3_acceptance.py tests/test_ai_edit_v3_acceptance_runner.py $acceptanceReport
git commit -m "docs(ai-edit-v3): record test acceptance evidence"
```

### Task 8: Run full isolation regression and issue the Go/No-Go package

**Files:**

- Create: `docs/verification/ai-edit-v3-go-no-go-${run_id}.md`
- Modify: `scripts/ai_edit_v3_acceptance.py`
- Create: `tests/test_ai_edit_v3_go_no_go.py`

This task does not modify production modules. If verification finds a blocker, record `NO_GO`, stop this task, and create a separate repair plan naming the concrete production file and regression test before changing code.

**Interfaces:**

```python
def build_go_no_go(*, machine: GateSummary, human: GateSummary,
                   faults: GateSummary, capacity: GateSummary,
                   regressions: GateSummary) -> GoNoGoDecision: ...
```

**Required RED test — write this complete minimal test first:**

```python
# tests/test_ai_edit_v3_go_no_go.py
import unittest

from scripts.ai_edit_v3_acceptance import (
    GateSummary,
    build_go_no_go,
)


class GoNoGoDecisionTests(unittest.TestCase):
    def test_any_safety_failure_forces_no_go(self) -> None:
        decision = build_go_no_go(
            machine=GateSummary(True, ()),
            human=GateSummary(True, ()),
            faults=GateSummary(False, ("cross_owner_material",)),
            capacity=GateSummary(True, ()),
            regressions=GateSummary(True, ()),
        )
        self.assertEqual(decision.status, "NO_GO")
        self.assertEqual(decision.blockers, ("cross_owner_material",))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **RED:** run `python -m unittest tests.test_ai_edit_v3_go_no_go.GoNoGoDecisionTests.test_any_safety_failure_forces_no_go -v`.
  Expected: `ERROR` with `ImportError: cannot import name 'GateSummary'`.

**Required minimal GREEN implementation — insert this complete code before the single final entry-point guard in `scripts/ai_edit_v3_acceptance.py`:**

```python
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class GateSummary:
    passed: bool
    blockers: tuple[str, ...]
    capacity_blocked: bool = False


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
    blockers = tuple(
        dict.fromkeys(
            blocker
            for summary in summaries
            for blocker in summary.blockers
        )
    )
    if blockers or any(not summary.passed and not summary.capacity_blocked
                       for summary in summaries):
        return GoNoGoDecision("NO_GO", blockers)
    if capacity.capacity_blocked:
        return GoNoGoDecision("CAPACITY_BLOCKED", ())
    return GoNoGoDecision("GO_FOR_PRODUCTION_REVIEW", ())
```

- [ ] **GREEN:** rerun `python -m unittest tests.test_ai_edit_v3_go_no_go.GoNoGoDecisionTests.test_any_safety_failure_forces_no_go -v`.
  Expected: `Ran 1 test` and `OK`.
- [ ] Add table tests proving all-pass maps only to `GO_FOR_PRODUCTION_REVIEW`, capacity-only blockage maps to `CAPACITY_BLOCKED`, and every machine/human/fault/regression failure maps to `NO_GO`.
- [ ] Run `python -m unittest discover -s tests -p "test_ai_edit_v3_*.py" -v`.
- [ ] Run `node --test tests/test_ai_edit_v3_ui.js tests/test_ai_edit_v2_ui.js tests/test_ai_edit_dual_entry.js tests/test_cloud_shell_sidebar.js`.
- [ ] Run `python -m unittest discover -s tests -p "test_ai_edit_v2_*.py" -v` and `python -m unittest discover -s tests -v`.
- [ ] From `server/ai_edit_v3_renderer`, run the exact Phase C fixture commands below; no targetless HyperFrames command is allowed:

```powershell
npm ci --ignore-scripts
npm ls hyperframes gsap --depth=0
npm test
npm run hf:check -- test/fixtures/landscape --strict --json
npm run hf:check -- test/fixtures/portrait --strict --json
npm run hf:check -- test/fixtures/animations --strict --json
npm run hf:check -- test/fixtures/transitions --strict --json
npm run hf:keyframes -- test/fixtures/animations --json
npm run hf:keyframes -- test/fixtures/transitions --json
npm run hf:snapshot -- test/fixtures/animations --at 0,0.5,1,1.4
npm run hf:snapshot -- test/fixtures/transitions --at 0,0.2,0.4,0.8
npm run hf:snapshot -- test/fixtures/landscape --at 0,1.5,3
npm run hf:snapshot -- test/fixtures/portrait --at 0,1.5,3
npm run render:fixtures
```

  Expected: every command exits `0`; dependency versions equal the frozen release, both fixture projects have zero persistent check findings, keyframe validation passes, all requested snapshots are produced and deterministic fixture rendering passes.
- [ ] Run `python scripts/ci_validate.py`, `python scripts/stamp_assets.py --check` and `git diff --check`.
- [ ] Compare deployed migrations, V3/V2 DB identities, table prefixes, COS prefixes, point transaction namespaces, worker claim sets, asset modes and private signers; any overlap is No-Go.
- [ ] Tabulate every approved threshold with measured numerator, denominator, evidence path and pass/fail; do not mark a threshold passed from narrative judgment alone.
- [ ] Classify outcome as `GO_FOR_PRODUCTION_REVIEW`, `NO_GO`, or `CAPACITY_BLOCKED`; `GO_FOR_PRODUCTION_REVIEW` is not permission to deploy production.
- [ ] Use `superpowers:requesting-code-review` for an independent read-only review of the report and evidence index.
- [ ] Rerun `python -m unittest tests.test_ai_edit_v3_go_no_go -v`.
  Expected: all aggregator tests pass and the generated decision contains measured blockers only.
- [ ] After all reported hashes and counts are reverified, commit:

```powershell
$runId = $env:AI_EDIT_V3_ACCEPTANCE_RUN_ID
if ([string]::IsNullOrWhiteSpace($runId)) { throw 'AI_EDIT_V3_ACCEPTANCE_RUN_ID is required' }
$goNoGoReport = "docs/verification/ai-edit-v3-go-no-go-$runId.md"
if (-not (Test-Path -LiteralPath $goNoGoReport -PathType Leaf)) { throw "Go/No-Go report missing: $goNoGoReport" }
git add -- scripts/ai_edit_v3_acceptance.py tests/test_ai_edit_v3_go_no_go.py $goNoGoReport
git commit -m "docs(ai-edit-v3): publish acceptance decision"
```

## Phase E Definition of Done

- [ ] All 20 authorized outputs are playable 1080p H.264/AAC MP4, all blocking QC items pass, first-render success is at least 95%, and post-repair technical pass is 100%.
- [ ] At least 16/20 are independently judged directly publishable, every accepted case satisfies the frozen score rule, and creative distribution meets layout/variant/repetition limits.
- [ ] Every `5 × 3` input/mode combination and all four templates have real evidence; 3-second hooks are accurate in all 20 cases.
- [ ] Fault tests prove crash-safe charging/refunding, one-winner asset publication, fencing, no duplicate Provider submission, no cross-owner material and convergence from both pending states.
- [ ] Five-concurrent latency and safety gates pass; ten-concurrent stress either passes on the required host or is honestly recorded as capacity blocked.
- [ ] V2's current Python and UI baselines remain fully green and V3 remains disabled in production.
- [ ] The final package is sufficient for a separate production Go/No-Go review and explicitly lists content safety as an unresolved production prerequisite.
