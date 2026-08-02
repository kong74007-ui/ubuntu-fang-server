# AI 智能剪辑 V3 Master Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不改变 AI 智能剪辑 V2 的前提下，分五个可审查阶段交付测试环境可用的 AI 智能剪辑 V3：支持五类主输入、三种创作入口、Qwen3.7-Max 语义导演、仅本次图片与缺图生图、ElevenLabs BGM/SFX、HyperFrames 确定性渲染、崩溃安全账务和资产发布，并用 20 条真实样本证明可行性。

**Architecture:** V3 是独立的 Python 控制面、SQLite/WAL 任务库、网络 Worker 和每任务无网络 Node.js 渲染沙箱。Python 是唯一持有身份、密钥和持久状态的边界；Qwen 只输出通过 JSON Schema 验证的语义 `edit-plan 2.0`；编译器将白名单组件转为冻结 render manifest；HyperFrames 只渲染无声画面；FFmpeg 生成唯一音频母带并完成最终 mux；共享点数账本和视频资产库通过幂等查询与发布 Saga 连接。

**Tech Stack:** Python 3、SQLite/WAL、`unittest`、FFprobe/FFmpeg、阿里云 fun-asr、DashScope `qwen3.7-max-2026-06-08`、网站现有 TTS/生图服务、ElevenLabs、Node.js 22.x、HyperFrames 0.7.84、GSAP 3.15.0、Chromium 固定发布包、腾讯云 COS、原生 HTML/CSS/JavaScript、systemd。

## Global Constraints

- [ ] 仅在 `codex/ai-edit-v3` 分支实施；开始每个任务前执行 `git status --short --branch`、`git branch --show-current` 和 `git log --oneline -5`，确认没有混入其他任务改动。
- [ ] V3 固定使用 `/api/v3/edit/*`、`AI_EDIT_V3_DB_PATH`、`edit_v3_*`、`ai-edit-v3:*`、`ai_edit_v3`、`{environment}/ai-edit-v3/...` 与 `[ai-edit-v3]`；不得导入 V2 Store、V2 provider、V2 COS 或修改 `ai_edit_v2.db`。
- [ ] `AI_EDIT_V3_ENABLED` 默认保持 `0`；功能关闭时禁止新报价、上传、预扣和创建任务，但允许 owner 读取既有任务、结果和短期播放地址。
- [ ] 每个功能先新增精确失败测试，再写最小实现，再运行定向测试与受影响的 V2 回归；每个任务独立 commit，不把公共协作组文件与大块业务实现混入一个提交。
- [ ] 不提交真实密钥、Cookie、Authorization、签名 URL、数据库、用户素材、渲染成片、Provider 原始完整响应或运行时生成目录；示例配置只写变量名和无敏感占位说明。
- [ ] Qwen 固定使用北京地域 Workspace 专属多模态端点和 `qwen3.7-max-2026-06-08`，不得静默回退；Qwen 不得输出或执行 HTML、CSS、JavaScript、GSAP、HyperFrames 或任意代码。
- [ ] 第一版只允许最多 10 张本次上传的 JPEG/PNG/WebP 图片；不得检索用户历史素材、平台公共素材或其他口播视频，不生成 AI 短视频。
- [ ] HyperFrames、GSAP、Chromium、字体和 FFmpeg 均使用冻结版本或内容哈希；生产渲染期不得 `npm install`、`npx`、下载插件、访问外网或读取供应商密钥。
- [ ] 账务响应和资产裁决响应未知时必须进入显式可对账状态；未经权威账本确认不得声称退款，未经资产服务裁决不得声称发布或开始全额退款。
- [ ] `completed`、`refunded`、`prehold_absent` 不可重开；`failed_reconciliation_pending` 与 `failed_asset_decision_pending` 停止媒体处理且不锁用户新任务。
- [ ] 测试部署、真实 Provider smoke、真实点数、PR push/merge 和生产操作均是后续独立授权；本计划本身不授权执行这些动作。

---

## 1. Frozen Repository Map

```text
server/content_domains/ai_edit_v3/
  __init__.py                 # public package boundary
  api.py                      # HTTP DTO, auth and owner boundary only
  service.py                  # API-facing application service
  contracts.py                # strict JSON, discriminated unions and state contract
  store.py                    # sole V3 SQLite access, leases, attempts and intents
  pipeline.py                 # sole job-state transition owner
  runtime.py                  # config, DI, capability/preflight and version report
  media.py                    # probe, normalize, frames and final mux
  source.py                   # normalize five primary-input variants
  source_map.py               # deterministic source-to-output segment mapping
  transcript.py               # ASR normalization and deterministic alignment
  director.py                 # prompt construction, JSON extraction and validation
  materials.py                # current-upload-only matching and generation decisions
  audio.py                    # BGM/SFX plan, master mix and loudness evidence
  quality.py                  # normalized blocking/reparable verdict evidence
  delivery.py                 # V3 COS staging and shared publication client
  billing.py                  # quote, pre-debit, cumulative refund and reconciliation
  feature.py                  # V3-only config and capability gate
  acceptance_export.py        # redacted acceptance evidence export
  acceptance_verify.py        # strict machine acceptance aggregation
  providers/
    base.py
    asr.py
    tts.py
    dashscope.py
    image_generation.py
    elevenlabs.py
  renderers/
    __init__.py
    hyperframes.py             # only Python-to-renderer process boundary
  catalog/
    templates-v1.json
    template-previews/
  schemas/
    edit-plan-2.0.schema.json
    render-manifest-v1.schema.json
    quality-verdict-v1.schema.json

server/ai_edit_v3_worker.py
server/ai_edit_v3_renderer/
  package.json
  package-lock.json
  hyperframes.json
  renderer-release.lock.json
  src/
  assets/fonts/
  test/
site/workbench/ai-edit-v3.html
site/assets/ai-edit-v3/
site/admin/ai-edit-v3-pricing.html
tests/test_ai_edit_v3_*.py
tests/test_ai_edit_v3_ui.js
tests/fixtures/ai_edit_v3/
scripts/ai_edit_v3_acceptance.py
scripts/ai_edit_v3_fault_matrix.py
scripts/ai_edit_v3_capacity.py
deploy/systemd/huangque-ai-edit-v3.service
deploy/systemd/huangque-ai-edit-v3-render@.service
deploy/requirements-ai-edit-v3.txt
deploy/libexec/huangque-ai-edit-v3-renderctl
deploy/sudoers.d/huangque-ai-edit-v3-render
deploy/tmpfiles.d/huangque-ai-edit-v3.conf
server/content_domains/video_asset_publish.py
docs/operations/ai-edit-v3-runbook.md
```

Shared files are limited to `server/auth_server.py`, `server/content_domains/points.py`, `server/content_domains/video_asset_publish.py`, `server/content_domains/core.py`, `server/content_domains/video.py`, `site/workbench/assets.html`, `site/workbench/cloud-shell.js`, `site/workbench/tasks.js`, mechanically stamped existing `site/workbench/*.html`, `server/admin_api.py`, `site/admin/index.html`, `deploy/huangque-secrets.env.example` and `.github/workflows/ci.yml`. Each shared boundary is a separate reviewable commit and must run its V2 regression before proceeding.

## 2. Frozen Cross-Phase Interfaces

### 2.1 Request and service boundary

```python
def normalize_job_request(body: Mapping[str, Any]) -> dict[str, Any]: ...
def canonical_json(value: Any) -> bytes: ...
def request_fingerprint(value: Mapping[str, Any]) -> str: ...

class EditV3Service:
    def quote(self, owner: str, request: Mapping[str, Any], *, now: int) -> dict: ...
    def create_job(self, owner: str, request: Mapping[str, Any], quote_id: str,
                   idempotency_key: str, *, now: int) -> dict: ...
    def retry_job(self, owner: str, predecessor_job_id: str,
                  idempotency_key: str, *, now: int) -> dict: ...
    def get_job(self, owner: str, job_id: str) -> dict: ...
    def list_jobs(self, owner: str, *, cursor: str | None, limit: int) -> dict: ...
    def get_plan(self, owner: str, job_id: str) -> dict: ...
    def get_result(self, owner: str, job_id: str) -> dict: ...

def dispatch(handler: Any, method: str, path: str, user: dict[str, Any] | None,
             *, service: EditV3Service | None = None) -> bool: ...
```

The five `input_type` variants and three `creation_mode` variants are strict discriminated unions: unused fields are absent rather than `null`. `POST /jobs` and `POST /retry` read `Idempotency-Key` from the header. The normalized job document—not browser state—is the quote fingerprint authority.

### 2.2 Lease, provider and stage boundary

```python
@dataclass(frozen=True)
class LeaseClaim:
    job_id: str
    worker_id: str
    fencing_token: int
    lease_until: int

@dataclass(frozen=True)
class ProviderResult:
    provider: str
    capability: str
    request_id: str | None
    payload: Mapping[str, Any]
    usage: Mapping[str, int | float]
    elapsed_ms: int

@dataclass(frozen=True)
class StageContext:
    claim: LeaseClaim
    attempt_id: str
    stage_attempt_id: str
    deadline_at: float
    assert_active: Callable[[], None]

def claim_next_job(worker_id: str, lease_seconds: int, now: int,
                   *, db_path: Path | None = None) -> LeaseClaim | None: ...
def transition_leased(claim: LeaseClaim, expected_states: Collection[str],
                      target_state: str, checkpoint: Mapping[str, Any], now: int,
                      *, lease_seconds: int, db_path: Path | None = None) -> bool: ...
def run_job(claim: LeaseClaim, runtime: RuntimeDependencies,
            *, db_path: Path | None = None) -> JobRunResult: ...
```

Every leased mutation checks `worker_id`, current `fencing_token` and `lease_until > now` in the same SQL statement. Providers return typed outcomes only; they never update V3 state. `pipeline.py` is the only state transition owner.

### 2.3 Billing and publication boundary

```python
class PointsLedger(Protocol):
    def deduct(self, owner: str, amount: int, transaction_key: str,
               reason: str) -> LedgerResult: ...
    def refund(self, owner: str, amount: int, transaction_key: str,
               reason: str) -> LedgerResult: ...
    def query_transaction(self, owner: str,
                          transaction_key: str) -> LedgerTransaction | None: ...

class AssetPublisher(Protocol):
    def register_generation(self, mode: str, source_job_id: str, generation: int,
                            idempotency_key: str) -> PublicationDecision: ...
    def prepare_hidden(self, mode: str, source_job_id: str, owner: str,
                       object_key: str, generation: int,
                       idempotency_key: str) -> PublicationDecision: ...
    def commit_publish(self, mode: str, source_job_id: str, generation: int,
                       idempotency_key: str) -> PublicationDecision: ...
    def cancel_publish(self, mode: str, source_job_id: str, generation: int,
                       idempotency_key: str) -> PublicationDecision: ...
    def query_decision(self, mode: str, source_job_id: str,
                       idempotency_key: str) -> PublicationDecision | None: ...
```

`server/auth_server.py` must expose an internal-only, owner-bound, read-only transaction query before V3 unknown reconciliation is enabled. `server/content_domains/video_asset_publish.py` must own generation registration, hidden preparation and one-winner `publish_won`/`cancel_won` arbitration before V3 delivery can be marked complete.

### 2.4 Director and renderer boundary

```python
def validate_edit_plan(plan: Any, *, timeline: Mapping[str, Any]) -> dict[str, Any]: ...
def validate_render_manifest(manifest: Any, *, sandbox_root: Path) -> dict[str, Any]: ...

@dataclass(frozen=True)
class RenderRequest:
    instance_id: str
    job_id: str
    attempt: int
    manifest_path: Path
    input_root: Path
    output_root: Path
    manifest_sha256: str
    renderer_build_id: str
    deadline_at: float

@dataclass(frozen=True)
class RenderResult:
    silent_video_relpath: str
    sha256: str
    report_relpath: str
    snapshots: tuple[str, ...]
    environment: Mapping[str, str]
    performance: Mapping[str, int | float]
```

The renderer receives only a frozen manifest and verified local files. Python invokes the root-owned launcher as `renderctl <action> <instance_id>`; user-controlled paths, environment variables, unit names, shell fragments and systemd properties never cross this interface.

## 3. Phase Order and Gates

| Order | Plan | Depends on | Exit gate |
| --- | --- | --- | --- |
| A | `2026-07-30-ai-edit-v3-phase-a-foundation.md` | None | Shared ledger query and publication Saga exist; strict contracts, isolated DB, fencing, quote/prehold/reconciliation, API shell and fake-provider worker pass; V2 unchanged. |
| B | `2026-07-30-ai-edit-v3-phase-b-director-materials.md` | A | All five inputs produce accurate timelines; fixed Qwen endpoint yields valid plans after at most one repair; only current images are used; required/optional material behavior passes. |
| C | `2026-07-30-ai-edit-v3-phase-c-hyperframes-audio.md` | A, B | Four templates and the component registry render deterministic horizontal/vertical samples in a no-network per-job sandbox; master audio, mux, blocking QC and private COS staging pass. |
| D | `2026-07-30-ai-edit-v3-phase-d-site-delivery.md` | A–C | Complete test-site user flow—selection/upload, creation mode, quote, task, result, retry, playback/download and task notification—passes without V2 regression. |
| E | `2026-07-30-ai-edit-v3-phase-e-acceptance.md` | A–D | Twenty real samples, failure injection, 5-concurrent baseline, 10-concurrent stress and V2 isolation meet every approved gate; evidence package is ready for a separate production Go/No-Go. |

- [ ] Do not start Phase B until both shared safety prerequisites and the Phase A gate are green.
- [ ] Do not call real Qwen, TTS, image, ElevenLabs or COS in unit tests; use protocol-faithful fakes and recorded redacted fixtures. Real-provider smoke requires separate test-environment authorization.
- [ ] Do not start a test deployment from an unmerged branch. Before any later authorized deployment, wait for main CI and confirm no active jobs.
- [ ] Do not interpret completion of Phase E as production approval; content safety remains an explicit production blocker.

## 4. State and Deadline Contract

The complete V3 state graph in the approved design is authoritative. Implementation must preserve these special rules:

- [ ] `created_draft -> preholding -> queued` is the only media admission path; media work cannot start before authoritative pre-debit success.
- [ ] `billing_reconciling` stores `reason`, `resume_state`, immutable external key, cumulative refund target and first-unknown time; after five minutes it becomes `failed_reconciliation_pending`.
- [ ] `asset_decision_reconciling` stores operation, external key, generation, expected decision and first-unknown time; after five minutes it becomes `failed_asset_decision_pending`.
- [ ] Queue wait is capped at ten minutes. The processing deadline freezes at first confirmed prehold: 45 minutes without repair, one atomic ten-minute extension only when first-pass QC identifies a repairable defect, total 55 minutes.
- [ ] On lease loss or deadline, terminate the whole task process group and close the running stage transactionally before another worker may progress.
- [ ] User retry creates a new successor job, quote and pre-debit; it never reopens or mutates the predecessor.

## 5. Commit and Review Topology

Implement each task in the phase plans as one intentionally scoped commit. In addition, use these mandatory shared-boundary commits:

1. `feat(points): add read-only transaction lookup` — only auth/points code and tests.
2. `feat(video-assets): add hidden publication arbitration` — only shared asset publication schema/service and tests.
3. `ci(ai-edit-v3): install pinned schema dependencies` — only `.github/workflows/ci.yml` plus its workflow-contract test; the dependency manifest is created in its own preceding V3-owned commit.
4. `feat(ai-edit-v3): register isolated api routes` — only minimal `core.py` dispatch plus the V3 route regression test; `api.py` is completed in its preceding V3-owned task.
5. `docs(ai-edit-v3): declare default-off foundation config` — only the Phase A names/defaults in `deploy/huangque-secrets.env.example` plus `tests/test_ai_edit_v3_feature.py`.
6. `chore(ai-edit-v3): document elevenlabs secret name` — only the later ElevenLabs example-name addition in `deploy/huangque-secrets.env.example` plus its static test.
7. `ci(ai-edit-v3): test pinned renderer package` — only `.github/workflows/ci.yml` plus its renderer workflow-contract test.
8. `feat(ai-edit-v3): extend global task tracking` — only `site/workbench/tasks.js` plus its tests.
9. `feat(ai-edit-v3): add gated workbench navigation` — only `site/workbench/cloud-shell.js` plus its tests.
10. `feat(ai-edit-v3): add private asset playback signing` — only `server/content_domains/video.py` plus its tests.
11. `feat(ai-edit-v3): refresh private asset playback` — only `site/workbench/assets.html` plus its tests.
12. `feat(ai-edit-v3): add independent pricing api` — only `server/admin_api.py` plus its tests.
13. `feat(ai-edit-v3): link pricing administration` — only `site/admin/index.html` plus its tests; the V3-owned pricing page is committed separately beforehand.
14. `chore(workbench): refresh shared shell cache stamps` — only the generated `cloud-shell.js?v=` token changes enumerated by Phase D.

At the end of every phase:

- [ ] Run the phase-specific commands in that plan and preserve raw exit codes in the PR description.
- [ ] Run `python -m unittest discover -s tests -p "test_ai_edit_v2_*.py" -v` and `node --test tests/test_ai_edit_v2_ui.js`.
- [ ] Run `python scripts/ci_validate.py`, `python scripts/stamp_assets.py --check` and `git diff --check`.
- [ ] Use `superpowers:requesting-code-review`; resolve P0/P1 findings before the next phase.
- [ ] Confirm `git diff --name-only <phase-base>...HEAD` contains only the declared phase and shared-boundary files.

## 6. Final Verification Contract

```powershell
python -m unittest discover -s tests -p "test_ai_edit_v3_*.py" -v
node --test tests/test_ai_edit_v3_ui.js tests/test_ai_edit_v2_ui.js tests/test_ai_edit_dual_entry.js tests/test_cloud_shell_sidebar.js
python -m unittest discover -s tests -p "test_ai_edit_v2_*.py" -v
python -m unittest discover -s tests -v
python scripts/ci_validate.py
python scripts/stamp_assets.py --check
git diff --check
```

```powershell
Push-Location server/ai_edit_v3_renderer
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
Pop-Location
```

- [ ] All commands exit `0`; dependency listing shows exactly HyperFrames `0.7.84` and GSAP `3.15.0`.
- [ ] No real credential or signed URL appears in tracked files or captured reports.
- [ ] The acceptance report proves all 20 outputs are 1080p H.264/AAC MP4 and all blocking quality gates pass.
- [ ] The report proves no duplicate debit/refund/provider submit, no over-refund, no cross-owner material, no duplicate visible asset and no permanently running stage under fault injection.
- [ ] V3 remains disabled by default and no production migration, key configuration, price publication or service enablement has occurred.

## 7. Master Definition of Done

- [ ] All five phase plans are completed in order and each task has test evidence plus an isolated commit.
- [ ] All frozen interfaces above have one implementation owner and no phase introduced a competing DTO or state transition path.
- [ ] Shared point and asset services support authoritative reconciliation; safety-pending jobs later converge from persisted evidence.
- [ ] Four published templates, twelve layouts, fourteen visual animations, five transitions and one FFmpeg `volume_fade` automation are covered by snapshots or seek tests.
- [ ] The website exposes the simple approved flow and never loads a full platform video until the user actively plays it.
- [ ] The 20-sample audit package satisfies every threshold in the approved design and explicitly records any capacity-blocked environment result.
- [ ] An independent production Go/No-Go remains pending; production is not enabled by this implementation plan.
