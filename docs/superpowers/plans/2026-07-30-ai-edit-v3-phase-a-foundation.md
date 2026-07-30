# AI 智能剪辑 V3 Phase A Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在完全不改变 V2 行为的前提下，交付 V3 Phase A 的严格协议、独立 SQLite/WAL 数据库、fencing 租约、崩溃安全点数意图、共享资产发布 Saga、五类输入与三类创作入口、上传与报价 API，以及可恢复的 fake-provider Worker 基础。

**Architecture:** V3 使用独立 `ai_edit_v3` Python 包、`AI_EDIT_V3_DB_PATH` 和 `edit_v3_*` 表；`service.py` 负责 API 用例编排，`pipeline.py` 是唯一任务状态转换者，`store.py` 是唯一 V3 SQL 边界。JSON Schema 运行时使用一份完整固定版本的 Python 依赖清单，业务依赖清单与公共 CI 接线分成两个提交；点数账本和共享视频资产库只在规格补充获得明确授权后，才分别增加只读交易查询与 generation 仲裁接口。V3 再通过持久 outbox、累计退款目标和发布裁决接入；Phase A 不实现真实 Qwen、ASR、音频或渲染能力，生产 runtime 对这些缺失能力 fail closed，测试使用协议一致的 fake。

**Tech Stack:** Python 3.12、SQLite/WAL、`unittest`、JSON Schema 2020-12、`jsonschema==4.26.0` 及完整固定版本依赖闭包、腾讯云 COS 私有对象适配接口、标准库 HTTP server、线程 Worker 与可注入时钟。

## Global Constraints

- [ ] 只在 `codex/ai-edit-v3` 分支实施；每个任务开始前运行 `git status --short --branch`、`git branch --show-current` 和 `git log --oneline -5`，发现范围外改动时保留并绕开，不覆盖其他人的文件。
- [ ] V3 API 固定为 `/api/v3/edit/*`，数据库变量固定为 `AI_EDIT_V3_DB_PATH`，表前缀固定为 `edit_v3_*`，Worker 固定为 `server/ai_edit_v3_worker.py`。
- [ ] V3 COS Key 固定为 `{environment}/ai-edit-v3/{owner_hmac}/{job_id}/...`，`environment` 只允许 `test` 或 `production`；点数键固定为 `ai-edit-v3:*`，资产模式固定为 `ai_edit_v3`，日志前缀固定为 `[ai-edit-v3]`。
- [ ] `AI_EDIT_V3_ENABLED=0` 是默认值；关闭时所有上传、报价、预扣和创建写接口 fail closed，但 owner 仍可读取既有 capabilities、任务、计划和结果。
- [ ] V3 不导入 V2 Store、V2 provider 或 V2 COS，不读取、修改、迁移或创建 `ai_edit_v2.db` 中的对象；V2/V3 Worker 不得交叉领取任务。
- [ ] 五类输入固定为 `platform_talking_head`、`uploaded_video`、`existing_audio`、`uploaded_audio`、`script_to_audio_video`；三类创作入口固定为 `ai_auto`、`style_prompt`、`template_reference`，未使用的联合字段必须缺席而不是 `null`。
- [ ] 主视频或主音频时长固定为 3 秒至 10 分钟；图片最多 10 张，只允许本次上传的 JPEG、PNG、WebP，单张最大 25 MB，单任务上传总量最大 1 GiB。
- [ ] Qwen 后续阶段只能使用 `qwen3.7-max-2026-06-08` 和 `cn-beijing` Workspace 多模态端点；Phase A 不调用真实 Qwen、TTS、fun-asr、生图、ElevenLabs、COS 或真实点数扣退。
- [ ] 三份 Schema 使用 JSON Schema 2020-12，根对象和所有嵌套对象设置 `additionalProperties: false`；Schema SHA-256 是持久审计字段，启动时不匹配即 fail closed。
- [ ] Python Schema 运行时只从 `deploy/requirements-ai-edit-v3.txt` 安装；`jsonschema` 固定为 `4.26.0`，直接依赖和传递依赖全部固定版本，CI 与测试机不得临时执行无版本 `pip install jsonschema`。
- [ ] 每次 claim 单调递增 `fencing_token`；所有 lease 保护写入在同一 SQL 条件中检查 `worker_id`、当前 token 和 `lease_until > now`。
- [ ] 预扣确认前使用 5 分钟准入窗口；预扣确认后冻结 45 分钟处理绝对截止时间，只有首次明确可修复质检可原子追加一次 10 分钟，总上限 55 分钟；队列等待上限 10 分钟。
- [ ] 账务或资产裁决首次 unknown 后最多 5 分钟进入 `failed_reconciliation_pending` 或 `failed_asset_decision_pending`；两种状态停止媒体工作、不锁用户新任务，也不得显示为已退款或已发布。
- [ ] `completed`、`refunded`、`prehold_absent` 是不可重开终态；retry 创建新的继任 job、quote 和 pre-debit，不复活前任任务。
- [ ] 规格必须先以独立设计提交记录 `server/auth_server.py`、`server/content_domains/points.py`、新增 `server/content_domains/video_asset_publish.py` 和 `.github/workflows/ci.yml` 这四个实现既有账务、发布与 CI 语义所必需的公共接点；工作树中的未提交规格草稿或实施计划不构成可执行边界。
- [ ] 规格补充获批后，点数共享改动、资产发布共享改动和公共 CI 接线仍分别作为独立提交；`core.py` 路由接线另作独立提交，不把不同协作组公共文件和 V3 大块业务放进同一提交。
- [ ] 不提交真实密钥、Cookie、Authorization、签名 URL、数据库、用户媒体、Provider 完整响应或运行时目录；日志和错误不得返回堆栈、本地路径或 URL 查询参数。
- [ ] 本计划只授权写代码、测试和本地提交；不授权 push、PR merge、测试部署、生产部署、生产迁移、真实价格发布或开启生产功能。

---

## 1. Phase A File Map

### Shared prerequisite gate P1: authoritative points lookup

- Modify: `server/auth_server.py:201-207,1336-1422,2857-2896`
- Modify: `server/content_domains/points.py:115-158`
- Modify: `tests/test_auth_points.py`

### Shared prerequisite gate P2: asset publication arbitration

- Create: `server/content_domains/video_asset_publish.py`
- Modify: `server/content_domains/core.py:18,278-410`
- Create: `tests/test_video_asset_publish.py`

### V3 pinned Python dependency

- Create: `deploy/requirements-ai-edit-v3.txt`
- Create: `tests/test_ai_edit_v3_dependencies.py`

### Shared prerequisite gate P3: public CI installation

- Modify: `.github/workflows/ci.yml:31-48`
- Modify: `tests/test_ai_edit_v3_dependencies.py`

### V3 foundation package

- Create: `server/content_domains/ai_edit_v3/__init__.py`
- Create: `server/content_domains/ai_edit_v3/contracts.py`
- Create: `server/content_domains/ai_edit_v3/service.py`
- Create: `server/content_domains/ai_edit_v3/store.py`
- Create: `server/content_domains/ai_edit_v3/billing.py`
- Create: `server/content_domains/ai_edit_v3/delivery.py`
- Create: `server/content_domains/ai_edit_v3/feature.py`
- Create: `server/content_domains/ai_edit_v3/runtime.py`
- Create: `server/content_domains/ai_edit_v3/pipeline.py`
- Create: `server/content_domains/ai_edit_v3/api.py`
- Create: `server/content_domains/ai_edit_v3/providers/__init__.py`
- Create: `server/content_domains/ai_edit_v3/providers/base.py`
- Create: `server/content_domains/ai_edit_v3/renderers/__init__.py`
- Create: `server/content_domains/ai_edit_v3/schemas/edit-plan-2.0.schema.json`
- Create: `server/content_domains/ai_edit_v3/schemas/render-manifest-v1.schema.json`
- Create: `server/content_domains/ai_edit_v3/schemas/quality-verdict-v1.schema.json`
- Create: `server/ai_edit_v3_worker.py`
- Modify: `server/content_domains/core.py:18,1280-1283,1797-1800`
- Modify: `deploy/huangque-secrets.env.example`

### Phase A tests and fixtures

- Create: `tests/test_ai_edit_v3_contracts.py`
- Create: `tests/test_ai_edit_v3_schemas.py`
- Create: `tests/test_ai_edit_v3_store.py`
- Create: `tests/test_ai_edit_v3_billing.py`
- Create: `tests/test_ai_edit_v3_delivery.py`
- Create: `tests/test_ai_edit_v3_feature.py`
- Create: `tests/test_ai_edit_v3_service.py`
- Create: `tests/test_ai_edit_v3_api.py`
- Create: `tests/test_ai_edit_v3_pipeline.py`
- Create: `tests/test_ai_edit_v3_worker.py`
- Create: `tests/test_ai_edit_v3_isolation.py`
- Create: `tests/fixtures/ai_edit_v3/valid-edit-plan-2.0.json`
- Create: `tests/fixtures/ai_edit_v3/valid-render-manifest-v1.json`
- Create: `tests/fixtures/ai_edit_v3/valid-quality-verdict-v1.json`

`server/content_domains/video.py`、Workbench 页面、管理后台、真实 Provider、媒体标准化和渲染器不属于 Phase A 修改范围。私有播放签名在 Phase D 单独接线；Phase A 的共享 `prepare_hidden` 记录存放在独立 publication 表中，只有 `commit_publish` 获胜时才插入用户可见 `video_assets` 行，因此不会把隐藏资产暴露给现有列表。

File lifecycle is fixed: `deploy/requirements-ai-edit-v3.txt` and `tests/test_ai_edit_v3_dependencies.py` are first created in Task A1; Task A2 only modifies the already-created dependency test and the existing CI workflow. `pipeline.py` is first created in Task 10, `delivery.py` in Task 8, `store.py` in Task 4, and later references to those paths are `Modify`. Existing shared files (`auth_server.py`, `points.py`, `core.py`, `.github/workflows/ci.yml`, `deploy/huangque-secrets.env.example`) are never labeled `Create`.

## 2. Frozen Interfaces

### 2.1 Request and API application boundary

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

def dispatch(handler: Any, method: str, path: str,
             user: dict[str, Any] | None,
             *, service: EditV3Service | None = None) -> bool: ...
```

### 2.2 Schema and strict JSON boundary

```python
def parse_strict_json(raw: str | bytes, *, max_bytes: int,
                      max_depth: int, max_items: int,
                      max_string_chars: int) -> Any: ...
def validate_edit_plan(plan: Any, *, timeline: Mapping[str, Any]) -> dict[str, Any]: ...
def validate_render_manifest(manifest: Any, *, sandbox_root: Path) -> dict[str, Any]: ...
def validate_quality_verdict(verdict: Any) -> dict[str, Any]: ...
def schema_sha256(name: str) -> str: ...
```

`parse_strict_json` 使用 `object_pairs_hook` 拒绝重复键，并拒绝 `NaN`、`Infinity`、多个根对象、尾随内容、控制字符和超过限制的树。director 最终回答上限为 512 KiB、深度 24、数组元素总数 5000、单字符串 4000 字符；quality verdict 上限为 256 KiB、深度 16、检查项 64、每项证据最多 8 个。

### 2.3 Lease, provider and pipeline boundary

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

@dataclass(frozen=True)
class StageOutcome:
    next_state: str
    checkpoint: Mapping[str, Any]
    checkpoint_input_sha256: str
    provider_result: ProviderResult | None = None

def claim_next_job(worker_id: str, lease_seconds: int, now: int,
                   *, db_path: Path | None = None) -> LeaseClaim | None: ...
def renew_lease(claim: LeaseClaim, lease_seconds: int, now: int,
                *, db_path: Path | None = None) -> bool: ...
def transition_leased(claim: LeaseClaim, expected_states: Collection[str],
                      target_state: str, checkpoint: Mapping[str, Any], now: int,
                      *, lease_seconds: int,
                      db_path: Path | None = None) -> bool: ...
def run_job(claim: LeaseClaim, runtime: RuntimeDependencies,
            *, db_path: Path | None = None) -> JobRunResult: ...
```

### 2.4 Billing and publication boundary

```python
@dataclass(frozen=True)
class LedgerTransaction:
    transaction_key: str
    operation: Literal["deduct", "refund"]
    owner: str
    amount: int
    points_after: int
    created_at: int

@dataclass(frozen=True)
class LedgerResult:
    accepted: bool
    transaction: LedgerTransaction | None
    error_code: str | None

class PointsLedger(Protocol):
    def deduct(self, owner: str, amount: int, transaction_key: str,
               reason: str) -> LedgerResult: ...
    def refund(self, owner: str, amount: int, transaction_key: str,
               reason: str) -> LedgerResult: ...
    def query_transaction(self, owner: str,
                          transaction_key: str) -> LedgerTransaction | None: ...

@dataclass(frozen=True)
class PublicationDecision:
    status: Literal["accepted", "stale_generation", "publish_won", "cancel_won"]
    current_generation: int
    asset_id: str | None

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

### 2.5 Renderer boundary reserved by Phase A

```python
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

Phase A 只在 `renderers/__init__.py` 定义这两个 DTO 和 `Renderer` Protocol，不创建可执行 renderer。生产 capability 把 renderer 标记为 `missing_or_unavailable`，因此新任务不会越过预扣准入。

## 3. State and Database Contract

`contracts.py` 固定以下状态集合；`pipeline.py` 只能使用 `ALLOWED_TRANSITIONS`，store 不接受表外状态：

```python
TERMINAL_STATES = frozenset({"completed", "refunded", "prehold_absent"})
RECONCILIATION_STATES = frozenset({
    "billing_reconciling", "failed_reconciliation_pending",
    "asset_decision_reconciling", "failed_asset_decision_pending",
})
MEDIA_STATES = (
    "queued", "generating_voice", "normalizing", "transcribing", "aligning",
    "planning", "resolving_materials", "generating_images",
    "generating_audio", "mixing_audio", "compiling", "rendering",
    "quality_checking", "repair_planning", "staging_delivery",
)
ALLOWED_TRANSITIONS = {
    "created_draft": {"preholding"},
    "preholding": {"queued", "prehold_absent", "billing_reconciling"},
    "queued": {"generating_voice", "failed"},
    "generating_voice": {"normalizing", "failed"},
    "normalizing": {"transcribing", "failed"},
    "transcribing": {"aligning", "failed"},
    "aligning": {"planning", "failed"},
    "planning": {"resolving_materials", "failed"},
    "resolving_materials": {"generating_images", "failed"},
    "generating_images": {"generating_audio", "failed"},
    "generating_audio": {"mixing_audio", "failed"},
    "mixing_audio": {"compiling", "failed"},
    "compiling": {"rendering", "failed"},
    "rendering": {"quality_checking", "failed"},
    "quality_checking": {"repair_planning", "staging_delivery", "failed"},
    "repair_planning": {"compiling", "failed"},
    "staging_delivery": {"settling", "failed"},
    "settling": {"publishing", "billing_reconciling"},
    "publishing": {"completed", "failed", "asset_decision_reconciling"},
    "asset_decision_reconciling": {
        "completed", "failed", "publishing", "failed_asset_decision_pending",
    },
    "failed_asset_decision_pending": {"completed", "failed"},
    "failed": {"refund_pending"},
    "refund_pending": {"refunded", "billing_reconciling"},
    "billing_reconciling": {
        "queued", "prehold_absent", "publishing", "settling", "refunded",
        "refund_pending", "failed_reconciliation_pending",
    },
    "failed_reconciliation_pending": {
        "prehold_absent", "refund_pending", "refunded",
    },
    "completed": set(),
    "refunded": set(),
    "prehold_absent": set(),
}
```

Schema v1 创建以下表并仅通过 `store.py` 访问：

| Table | Required Phase A keys and constraints |
| --- | --- |
| `edit_v3_schema_meta` | singleton schema version、migration SHA、created/updated time |
| `edit_v3_jobs` | text UUID PK、environment、owner、state、normalized request JSON/SHA、quote/predecessor/idempotency、worker/token/lease、deadline/repair、billing/asset context、result/error；`UNIQUE(environment, owner, idempotency_key)` |
| `edit_v3_stage_attempts` | job/stage/attempt/token/status/input SHA/start/end/error；一个 job 只允许一个 active stage attempt |
| `edit_v3_checkpoints` | immutable version、job/stage/input SHA/output JSON/SHA/token；`UNIQUE(job_id, stage, version)` |
| `edit_v3_uploads` | owner、upload type、object key、declared/observed MIME/size、probe JSON、status、expiry；对象 key 唯一 |
| `edit_v3_materials` | owner、upload/source kind、stable COS key、MIME、size、SHA、metadata JSON |
| `edit_v3_job_materials` | job/material/purpose/order；`UNIQUE(job_id, material_id)` |
| `edit_v3_quotes` | owner、normalized request JSON/SHA、pricing version、min/max、breakdown、expires；quote ID PK |
| `edit_v3_pricing_versions` | immutable version、status `draft/published/retired`、parameter JSON/SHA；至多一个 published current version |
| `edit_v3_template_versions` | template/version/status/ratios/capability contract/SHA；`UNIQUE(template_id, version)` |
| `edit_v3_model_calls` | provider/model/purpose/request/response Schema SHA、request ID、redacted final output、validation JSON、usage/elapsed |
| `edit_v3_provider_tasks` | job/stage/operation key/request SHA/external id/status/unknown time；operation key 唯一 |
| `edit_v3_provider_usage` | job/provider/capability/request ID/usage JSON/cost units；request usage 唯一 |
| `edit_v3_plans` | job/version/raw final/normalized plan/plan SHA/Schema SHA |
| `edit_v3_render_manifests` | job/attempt/manifest JSON/SHA/Schema SHA/environment SHA |
| `edit_v3_renders` | job/attempt/status/artifact/evidence/performance JSON |
| `edit_v3_quality_reports` | job/attempt/verdict/Schema SHA/evidence/status/repairable |
| `edit_v3_billing_intents` | environment/owner/job/operation/key/request SHA/target/request amount/status/unknown/evidence；`UNIQUE(environment, owner, job_id, operation)`，operation 仅 `pre_debit/refund_delta/refund_full` |
| `edit_v3_publish_intents` | job/generation/operation/key/expected decision/status/first unknown/last decision/asset ID；generation 单调，operation key 唯一 |

`edit_v3_jobs` 同时维护 `confirmed_preheld_total` 和 `confirmed_refunded_total`，CHECK 固定为 `0 <= confirmed_refunded_total <= confirmed_preheld_total`。数据库只保存稳定 COS Key，不保存签名 URL。

### 3.1 Schema v1 冻结列清单

为避免 Task 5–13 各自推测列名，Schema v1 在 Task 4 冻结以下最小列集。所有时间字段使用 Unix epoch 毫秒 `INTEGER`；布尔值使用受 CHECK 约束的 `INTEGER`；JSON 使用 `TEXT` 且只能写入规范 JSON；SHA-256 使用 64 位小写十六进制 `TEXT`。可以增加普通索引，但不得增加第 20 张 `edit_v3_*` 表或删除下列列：

- `edit_v3_schema_meta`：`id=1, version, migration_sha256, created_at, updated_at`。
- `edit_v3_jobs`：`job_id, environment, owner_id, state, normalized_request_json, request_sha256, quote_id, predecessor_job_id, idempotency_key, worker_id, fencing_token, lease_until, queued_at, processing_deadline_at, repair_count, repair_budget_granted_at, reconciliation_reason, resume_state, confirmed_preheld_total, confirmed_refunded_total, delivery_object_key, asset_id, result_json, error_code, error_json, created_at, updated_at`。
- `edit_v3_stage_attempts`：`id, job_id, stage, attempt, worker_id, fencing_token, status, input_sha256, started_at, finished_at, error_code, error_json`。
- `edit_v3_checkpoints`：`id, job_id, stage, version, stage_attempt_id, input_sha256, output_json, output_sha256, fencing_token, created_at`。
- `edit_v3_uploads`：`upload_id, environment, owner_id, upload_type, object_key, declared_mime, declared_size, observed_mime, observed_size, observed_etag, sha256, duration_ms, width, height, probe_json, status, expires_at, completed_at, created_at, updated_at`。
- `edit_v3_materials`：`material_id, environment, owner_id, upload_id, source_kind, source_job_id, cos_key, mime_type, size_bytes, sha256, metadata_json, created_at`。
- `edit_v3_job_materials`：`job_id, material_id, purpose, ordinal, created_at`。
- `edit_v3_quotes`：`quote_id, environment, owner_id, normalized_request_json, request_sha256, pricing_version, template_id, template_version, min_points, max_points, breakdown_json, expires_at, created_at`。
- `edit_v3_pricing_versions`：`version, status, parameters_json, parameters_sha256, created_at, published_at, retired_at`。
- `edit_v3_template_versions`：`template_id, version, status, preview_cos_key, supported_ratios_json, capability_contract_json, sha256, created_at, published_at`。
- `edit_v3_model_calls`：`id, job_id, stage_attempt_id, provider, model, purpose, prompt_version, request_schema_sha256, response_schema_sha256, request_id, redacted_final_output_json, validation_json, usage_json, elapsed_ms, created_at`。
- `edit_v3_provider_tasks`：`id, job_id, stage, stage_attempt_id, provider, capability, operation_key, request_sha256, external_id, status, fencing_token, first_unknown_at, last_checked_at, result_json, created_at, updated_at`。
- `edit_v3_provider_usage`：`id, job_id, provider, capability, request_id, usage_json, cost_units, created_at`。
- `edit_v3_plans`：`id, job_id, version, model_call_id, raw_final_output_json, normalized_plan_json, plan_sha256, schema_sha256, created_at`。
- `edit_v3_render_manifests`：`id, job_id, attempt, plan_id, manifest_json, manifest_sha256, schema_sha256, registry_sha256, renderer_environment_sha256, created_at`。
- `edit_v3_renders`：`id, job_id, attempt, manifest_id, status, artifact_cos_key, artifact_sha256, evidence_json, performance_json, log_summary, cost_units, started_at, finished_at`。
- `edit_v3_quality_reports`：`id, job_id, attempt, render_id, verdict_json, verdict_sha256, schema_sha256, evidence_json, status, repairable, created_at`。
- `edit_v3_billing_intents`：`id, environment, owner_id, job_id, operation, external_idempotency_key, request_sha256, refund_target_total, request_amount, status, first_unknown_at, last_checked_at, authority_evidence_json, reason, resume_state, created_at, updated_at, completed_at`。
- `edit_v3_publish_intents`：每个外部 operation 一行，列为 `id, job_id, publish_generation, operation, external_idempotency_key, object_key, metadata_sha256, expected_decision, status, fencing_token, first_unknown_at, last_decision_json, last_decision_at, asset_id, created_at, updated_at`。

Schema v1 同时冻结以下最低约束与索引：

- `jobs` 唯一 `(environment, owner_id, idempotency_key)`，分页索引 `(environment, owner_id, created_at DESC, job_id DESC)`，claim 索引 `(state, lease_until, queued_at, job_id)`，`repair_count IN (0,1)`，状态只能来自本节冻结状态全集。
- `stage_attempts` 唯一 `(job_id, stage, attempt)`，并以 partial unique 保证一个 job 只有一个 `status='running'` 的 attempt；Schema v1 的状态全集冻结为 `running`、`completed`、`failed`、`skipped`、`aborted_lease_lost`，其中租约丢失清理必须写入 `aborted_lease_lost`，不得遗留永久 `running` 记录。
- `checkpoints` 唯一 `(job_id, stage, version)` 和 `(job_id, stage, input_sha256)`。
- `uploads.object_key` 唯一；`materials.cos_key` 唯一且非空 `upload_id` 唯一；`job_materials` 唯一 `(job_id, material_id)` 和 `(job_id, purpose, ordinal)`。
- `pricing_versions` 至多一个 `published`；`template_versions` 唯一 `(template_id, version)`，每个模板至多一个 `published`。
- `provider_tasks.operation_key`、非空供应商 request ID 和所有外部幂等键不可重复；`provider_usage` 唯一 `(provider, request_id)`。
- plan、manifest、render 和 quality 分别按 `(job_id, version)` 或 `(job_id, attempt)` 唯一。
- billing 唯一 `(environment, owner_id, job_id, operation)`；publish 唯一 `(job_id, publish_generation, operation)`；二者都有按 `status/first_unknown_at` 的恢复索引。
- 所有 job 子表外键指向 `jobs`；job-material 双向外键；job 的 quote/predecessor、quote 的 pricing/template、material 的 upload、render 的 manifest、quality 的 render 均有外键。跨 owner/environment 绑定还必须由同一事务的条件查询验证。

公开读取 primitive 必须把 `environment` 和 `owner_id` 放入 SQL `WHERE`；`get_quote` 的公开形式为 `get_quote(owner_id, quote_id, *, environment=...)`，不存在和 owner 不匹配返回相同结果。任务分页使用 `(created_at, job_id)` 严格 keyset cursor，cursor 绑定 environment 与 owner，不使用 `OFFSET`。

V3 初始化在显式 `v2_db_path` 和 `AI_EDIT_V2_DB` 都缺失时必须 fail closed；不能因为 V2 模块存在默认路径而跳过比较，也不得导入或打开 V2 数据库来发现默认值。

---

## Authorization Gate A0: Record the required shared-file specification clarification

The approved product and safety semantics require authoritative ledger lookup, publication arbitration and clean-runner dependency installation. Before implementation, the design record must explicitly map those already-approved semantics to `server/auth_server.py`, `server/content_domains/points.py`, `server/content_domains/video_asset_publish.py` and `.github/workflows/ci.yml`; this implementation plan cannot silently substitute for that record.

- [ ] A design-only revision to `docs/superpowers/specs/2026-07-30-ai-edit-v3-design.md` explicitly lists all four paths and their read-only ledger, publication arbitration and CI-install responsibilities.
- [ ] The clarification is committed separately with a design-only commit such as `docs(ai-edit-v3): clarify required shared boundaries`; it adds no product capability and preserves every deployment/production authorization gate.
- [ ] The approved revision is clean in the implementation worktree. An uncommitted diff, a local draft, this plan or a code-review comment does not satisfy this gate.

Verification from the repository root:

```powershell
$spec = 'docs/superpowers/specs/2026-07-30-ai-edit-v3-design.md'
git diff --exit-code -- $spec
git diff --cached --exit-code -- $spec
@(
  'server/auth_server.py',
  'server/content_domains/points.py',
  'server/content_domains/video_asset_publish.py',
  '.github/workflows/ci.yml'
) | ForEach-Object {
  if (-not (Select-String -LiteralPath $spec -SimpleMatch $_ -Quiet)) {
    throw "approved spec does not authorize $_"
  }
}
$boundaryCommit = git log --format='%H %s' --grep='clarify required shared boundaries' -- $spec | Select-Object -First 1
if (-not $boundaryCommit) { throw 'shared-boundary specification clarification commit not found' }
$boundaryCommit
```

Expected: both unstaged and staged `git diff --exit-code` checks return `0`, all four paths are found, and the displayed history entry is the separate shared-boundary specification clarification. If any condition fails, stop before editing any shared path.

### Task A1: Pin the executable JSON Schema dependency closure

**Files:**
- Create: `deploy/requirements-ai-edit-v3.txt`
- Create: `tests/test_ai_edit_v3_dependencies.py`

**Interfaces:**
- Consumes: Python 3.10 or newer; CI remains fixed at Python 3.12.
- Produces: a complete exact-version dependency file installed with `pip --no-deps`, plus `assert_schema_runtime() -> None` test coverage proving `jsonschema==4.26.0` exposes `Draft202012Validator`.

- [ ] **Step 1: Write the failing dependency-manifest test**

```python
EXPECTED = (
    "attrs==25.4.0",
    "jsonschema==4.26.0",
    "jsonschema-specifications==2025.9.1",
    "referencing==0.37.0",
    "rpds-py==0.30.0",
    'typing-extensions==4.15.0; python_version < "3.13"',
)

def test_v3_dependency_file_is_a_complete_exact_pin_set(self):
    lines = tuple(
        line.strip() for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    self.assertEqual(lines, EXPECTED)
    self.assertFalse(any(">=" in line or "~=" in line for line in lines))
```

- [ ] **Step 2: Run the manifest test to verify RED**

Run: `python -m unittest tests.test_ai_edit_v3_dependencies.V3DependencyManifestTests -v`

Expected: FAIL because `deploy/requirements-ai-edit-v3.txt` does not exist.

- [ ] **Step 3: Create the exact dependency file**

```text
# Complete Python dependency closure for AI Edit V3 JSON Schema validation.
# Install with --no-deps so no unpinned transitive package is introduced.
attrs==25.4.0
jsonschema==4.26.0
jsonschema-specifications==2025.9.1
referencing==0.37.0
rpds-py==0.30.0
typing-extensions==4.15.0; python_version < "3.13"
```

- [ ] **Step 4: Install the closure into the active development interpreter**

Run: `python -m pip install --disable-pip-version-check --no-input --no-deps --requirement deploy/requirements-ai-edit-v3.txt`

Expected: exit `0`; pip installs only the six explicitly listed distributions applicable to the interpreter.

- [ ] **Step 5: Add and run the runtime test**

```python
def test_jsonschema_runtime_is_exact_and_supports_draft_2020_12(self):
    self.assertEqual(importlib.metadata.version("jsonschema"), "4.26.0")
    schema = {"$schema": "https://json-schema.org/draft/2020-12/schema", "type": "object"}
    Draft202012Validator.check_schema(schema)
```

Run: `python -m unittest tests.test_ai_edit_v3_dependencies -v`

Expected: PASS.

- [ ] **Step 6: Verify the installed dependency graph**

Run: `python -m pip check`

Expected: exit `0` with `No broken requirements found.`

- [ ] **Step 7: Commit only the V3 dependency contract**

```powershell
git add deploy/requirements-ai-edit-v3.txt tests/test_ai_edit_v3_dependencies.py
git commit -m "build(ai-edit-v3): pin schema validation dependencies"
```

**Installation responsibility:** Task A1's implementer owns the dependency file and local test installation. Task A2's shared-CI owner installs it on the GitHub Actions Python 3.12 runner. A separately authorized test-deployment operator—not the feature implementer—must, after merged main CI passes and before restarting `huangque-content` or starting the V3 Worker, run the following from the merged repository root because the current service interpreter is `/usr/bin/python3`:

```bash
sudo install -o root -g root -m 0644 deploy/requirements-ai-edit-v3.txt \
  /home/ubuntu/content-api/requirements-ai-edit-v3.txt
sudo /usr/bin/python3 -m pip install --disable-pip-version-check --no-input --no-deps \
  --requirement /home/ubuntu/content-api/requirements-ai-edit-v3.txt
sudo -u ubuntu /usr/bin/python3 -c 'from importlib.metadata import version; from jsonschema import Draft202012Validator; assert version("jsonschema") == "4.26.0"; Draft202012Validator.check_schema({"$schema":"https://json-schema.org/draft/2020-12/schema","type":"object"})'
sudo /usr/bin/python3 -m pip check
```

Every command must exit `0`. A package-install failure blocks the test deployment; this plan does not authorize SSH, package installation or service restart.

### Task A2: Install the pinned dependency in public CI as an isolated shared commit

**Files:**
- Modify: `.github/workflows/ci.yml:31-48`
- Modify: `tests/test_ai_edit_v3_dependencies.py`

**Interfaces:**
- Consumes: the exact Task A1 dependency file and Authorization Gate A0.
- Produces: one CI step named `安装 AI Edit V3 Python 依赖` before static validation and Python tests; existing cryptography, V2, JavaScript and design-system gates remain unchanged.

- [ ] **Gate check: verify shared CI authorization before editing**

Run the complete Authorization Gate A0 PowerShell block.

Expected: every A0 condition passes. Otherwise stop; do not edit `.github/workflows/ci.yml`.

- [ ] **Step 1: Write the failing CI-order test**

```python
def test_ci_installs_v3_pins_before_validation_and_tests(self):
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    install = workflow.index(
        "python -m pip install --disable-pip-version-check --no-input --no-deps "
        "--requirement deploy/requirements-ai-edit-v3.txt"
    )
    self.assertLess(install, workflow.index("python scripts/ci_validate.py"))
    self.assertLess(install, workflow.index("python -m unittest discover -s tests -v"))
```

- [ ] **Step 2: Run the CI-order test to verify RED**

Run: `python -m unittest tests.test_ai_edit_v3_dependencies.V3CiDependencyTests -v`

Expected: FAIL because the install command is absent from `.github/workflows/ci.yml`.

- [ ] **Step 3: Add only the pinned V3 install step**

```yaml
      - name: 安装 AI Edit V3 Python 依赖
        run: python -m pip install --disable-pip-version-check --no-input --no-deps --requirement deploy/requirements-ai-edit-v3.txt
```

Place it after Python setup and the existing cryptography install, and before `python scripts/ci_validate.py` and unittest discovery. Do not change action versions, triggers, permissions, timeouts or any existing test command.

- [ ] **Step 4: Run dependency and CI-validator tests**

Run: `python -m unittest tests.test_ai_edit_v3_dependencies tests.test_ci_validate -v`

Expected: PASS.

- [ ] **Step 5: Commit only the public CI boundary**

```powershell
git add .github/workflows/ci.yml tests/test_ai_edit_v3_dependencies.py
git commit -m "ci(ai-edit-v3): install pinned schema dependencies"
```

**Gate P3:** Phase A cannot claim clean CI until the separately authorized CI commit passes GitHub Actions. Do not combine this commit with Schema, store, API, billing or Worker code.

### Task 1: Add the read-only points transaction query gate

**Files:**
- Modify: `server/auth_server.py:1336-1422,2857-2896`
- Modify: `server/content_domains/points.py:115-158`
- Modify: `tests/test_auth_points.py`

**Interfaces:**
- Consumes: existing `points_transactions(transaction_key, operation, username, amount, points_after, created_at)`.
- Produces: `auth_server.get_points_transaction(username: str, transaction_key: str) -> dict[str, Any] | None` and `points.get_points_transaction(username: str, transaction_key: str) -> dict[str, Any] | None`.
- HTTP contract: internal-only `POST /api/auth/points/transaction` with `{"username": "alice", "transaction_key": "ai-edit-v3:job-1:pre_debit"}`; response is `200 {"found": false}` or `200 {"found": true, "transaction": {...}}`.

- [ ] **Gate check: verify points shared-file authorization before editing**

Run the complete Authorization Gate A0 PowerShell block.

Expected: the clean, approved specification explicitly lists `server/auth_server.py` and `server/content_domains/points.py`. Otherwise stop; do not edit either file or its shared tests.

- [ ] **Step 1: Write a failing direct-query test**

```python
def test_transaction_query_is_owner_bound_and_read_only(self):
    deduct_points("alice", 12, "v3", "ai-edit-v3:j1:pre_debit")
    before = self.scalar("SELECT COUNT(*) FROM points_audit")
    row = get_points_transaction("alice", "ai-edit-v3:j1:pre_debit")
    self.assertEqual(row["operation"], "deduct")
    self.assertEqual(row["amount"], 12)
    self.assertIsNone(get_points_transaction("bob", "ai-edit-v3:j1:pre_debit"))
    self.assertEqual(self.scalar("SELECT COUNT(*) FROM points_audit"), before)
```

- [ ] **Step 2: Run the red test**

Run: `python -m unittest tests.test_auth_points.PointsTransactionTests.test_transaction_query_is_owner_bound_and_read_only -v`

Expected: FAIL with an import or attribute error for `get_points_transaction`.

- [ ] **Step 3: Implement the pure database lookup**

```python
def get_points_transaction(username, transaction_key):
    transaction_key = _transaction_key(transaction_key)
    if not username or not transaction_key:
        return None
    c = db()
    try:
        row = c.execute(
            """SELECT transaction_key,operation,username,amount,points_after,created_at
               FROM points_transactions
               WHERE transaction_key=? AND username=?""",
            (transaction_key, username),
        ).fetchone()
        return dict(row) if row else None
    finally:
        c.close()
```

- [ ] **Step 4: Add failing HTTP boundary tests** for missing internal token (`401/403` according to existing `_require_internal` behavior), malformed key (`400`), absent row (`200 found=false`), owner mismatch (`200 found=false`) and found row (`200 found=true`).

- [ ] **Step 5: Run the HTTP tests to verify RED**

Run: `python -m unittest tests.test_auth_points.PointsTransactionHttpTests -v`

Expected: FAIL because `/api/auth/points/transaction` has no route.

- [ ] **Step 6: Add the internal route and content-service client**. The auth route calls `_require_internal()` before parsing the body. `points.get_points_transaction` calls `_auth_points_request("/api/auth/points/transaction", payload)` and returns `None` only when `found` is false; transport errors remain `AuthPointsError` and are never converted to “not found.”

- [ ] **Step 7: Run the complete points regression**

Run: `python -m unittest tests.test_auth_points -v`

Expected: PASS, including existing deduct/refund replay and conflict tests.

- [ ] **Step 8: Commit the shared gate**

```powershell
git add server/auth_server.py server/content_domains/points.py tests/test_auth_points.py
git commit -m "feat(points): add read-only transaction lookup"
```

**Gate P1:** Do not implement V3 unknown billing reconciliation until this commit is present and `tests.test_auth_points` passes.

### Task 2: Add the shared hidden-asset publication Saga gate

**Files:**
- Create: `server/content_domains/video_asset_publish.py`
- Modify: `server/content_domains/core.py:18,278-410`
- Create: `tests/test_video_asset_publish.py`

**Interfaces:**
- Consumes: the shared `audio_assets.db` selected by existing `CONTENT_ASSET_DB`, plus the existing `video_assets` table.
- Produces: `init_schema(conn) -> None` and `AssetPublicationService(connect: Callable[[], sqlite3.Connection])`; its `register_generation(...)`, `prepare_hidden(...)`, `commit_publish(...)`, `cancel_publish(...)` and `query_decision(...)` methods implement the exact `AssetPublisher` signatures in section 2.4. The production instance uses the existing `CONTENT_ASSET_DB`; tests inject a temporary connection factory.
- Publication uniqueness: `(mode, source_job_id)`; only `mode == "ai_edit_v3"` is accepted by this first client.

- [ ] **Gate check: verify publication shared-file authorization before editing**

Run the complete Authorization Gate A0 PowerShell block.

Expected: the clean, approved specification explicitly lists `server/content_domains/video_asset_publish.py`. Otherwise stop; do not create the module or modify `core.py` for its schema hook.

- [ ] **Step 1: Write a failing schema test**

```python
def test_publication_schema_keeps_hidden_rows_out_of_video_assets(self):
    self.create_legacy_video_assets_table()
    publish.init_schema(self.conn)
    self.publisher.prepare_hidden(
        "ai_edit_v3", "job-1", "alice", "test/ai-edit-v3/o/job-1/delivery/a.mp4",
        3, "ai-edit-v3:job-1:publish:prepare:3",
    )
    self.assertEqual(self.scalar("SELECT COUNT(*) FROM video_assets"), 0)
    self.assertEqual(self.scalar("SELECT COUNT(*) FROM video_asset_publications"), 1)
    index_sql = self.scalar(
        "SELECT sql FROM sqlite_master WHERE name='uq_video_assets_ai_edit_v3_source_job'"
    )
    normalized = " ".join(index_sql.lower().split())
    self.assertIn(
        "where mode='ai_edit_v3' and source_job_id is not null",
        normalized,
    )
```

- [ ] **Step 2: Run the schema test to verify RED**

Run: `python -m unittest tests.test_video_asset_publish.VideoAssetPublishSchemaTests -v`

Expected: FAIL because `video_asset_publish` does not exist.

- [ ] **Step 3: Implement the shared schema migration**. `init_schema(conn)` adds nullable `source_job_id TEXT`, `publication_generation INTEGER` and `published_at INTEGER` columns to `video_assets`; creates `uq_video_assets_ai_edit_v3_source_job` on `(mode, source_job_id)` with the exact predicate `WHERE mode='ai_edit_v3' AND source_job_id IS NOT NULL`; creates `video_asset_publications` keyed by `(mode, source_job_id)`; and creates `video_asset_publication_ops` keyed by `idempotency_key` with immutable operation, generation, request SHA and serialized response. The partial-index predicate must retain both the V3 mode clause and the non-null clause so unrelated legacy modes receive no new uniqueness rule.

- [ ] **Step 4: Write failing one-winner tests**

```python
def test_cancel_tombstone_beats_late_commit(self):
    self.publisher.register_generation("ai_edit_v3", "job-1", 7, "reg-7")
    self.publisher.prepare_hidden("ai_edit_v3", "job-1", "alice", self.key, 7, "prep-7")
    cancelled = self.publisher.cancel_publish("ai_edit_v3", "job-1", 7, "cancel-7")
    late = self.publisher.commit_publish("ai_edit_v3", "job-1", 7, "commit-7")
    self.assertEqual(cancelled.status, "cancel_won")
    self.assertEqual(late.status, "cancel_won")
    self.assertEqual(self.visible_assets("job-1"), [])

def test_publish_winner_returns_stable_asset_and_blocks_refund_side(self):
    self.publisher.register_generation("ai_edit_v3", "job-2", 4, "reg-4")
    self.publisher.prepare_hidden("ai_edit_v3", "job-2", "alice", self.key2, 4, "prep-4")
    won = self.publisher.commit_publish("ai_edit_v3", "job-2", 4, "commit-4")
    late_cancel = self.publisher.cancel_publish("ai_edit_v3", "job-2", 4, "cancel-4")
    self.assertEqual(won.status, "publish_won")
    self.assertEqual(late_cancel.status, "publish_won")
    self.assertEqual(late_cancel.asset_id, won.asset_id)

def test_query_decision_replays_the_same_persisted_external_key(self):
    self.publisher.register_generation("ai_edit_v3", "job-3", 5, "reg-5")
    first = self.publisher.query_decision(
        "ai_edit_v3", "job-3", "ai-edit-v3:job-3:publish:query:5"
    )
    second = self.publisher.query_decision(
        "ai_edit_v3", "job-3", "ai-edit-v3:job-3:publish:query:5"
    )
    self.assertEqual(second, first)
    self.assertEqual(
        self.operation_count("ai-edit-v3:job-3:publish:query:5"), 1
    )
```

- [ ] **Step 5: Run the arbitration tests to verify RED**

Run: `python -m unittest tests.test_video_asset_publish.VideoAssetPublishArbitrationTests -v`

Expected: FAIL because arbitration functions are absent.

- [ ] **Step 6: Implement transaction-key replay and generation fencing**. Every mutating call starts `BEGIN IMMEDIATE`, replays only an identical idempotency request, returns `stale_generation` when `generation < current_generation`, and never changes a final `publish_won` or `cancel_won` decision. `query_decision` also requires its persisted external idempotency key, writes/replays one immutable query operation record, and rejects reuse of that key for a different `(mode, source_job_id)`; it never changes generation or the publication decision. `commit_publish` inserts one `video_assets` row with `status="done"`, `phase="completed"`, `mode="ai_edit_v3"`, stable `source_job_id` and immutable `video_file`; `cancel_publish` writes the tombstone without inserting a visible asset.

- [ ] **Step 7: Add crash/replay and predicate-isolation tests** for response loss after each operation including `query_decision`, duplicate identical calls, conflicting reuse of an idempotency key, deterministic query-key replay, higher generation registration, stale prepare/commit/cancel, commit-before-cancel, cancel-before-commit and concurrent commit/cancel barriers. Insert two non-`ai_edit_v3` rows with the same non-null `source_job_id` and prove the partial index does not reject them; insert two `ai_edit_v3` rows with the same `source_job_id` and prove the second insert is rejected.

- [ ] **Step 8: Run the shared service tests repeatedly**

Run: `python -m unittest tests.test_video_asset_publish -v`

Run again: `python -m unittest tests.test_video_asset_publish -v`

Expected: both runs PASS with one visible asset at most and one final decision exactly.

- [ ] **Step 9: Add the minimal shared schema hook in `core.init_db()`**. Import `video_asset_publish` beside existing domain modules and call `video_asset_publish.init_schema(c)` after the legacy `video_assets` table exists; do not change `record_video_asset` or `list_video_assets` in this task.

- [ ] **Step 10: Run shared video regressions**

Run: `python -m unittest tests.test_video_batch tests.test_ai_edit_v2_asset_library tests.test_ai_edit_v2_delivery -v`

Expected: PASS with unchanged V2 behavior.

- [ ] **Step 11: Commit the shared gate**

```powershell
git add server/content_domains/video_asset_publish.py server/content_domains/core.py tests/test_video_asset_publish.py
git commit -m "feat(video-assets): add hidden publication arbitration"
```

**Gate P2:** Do not enable V3 `publishing` or `refund_full` transitions until this commit is present and one-winner fault tests pass.

### Task 3: Freeze strict request contracts and all three machine-readable Schemas

**Files:**
- Create: `server/content_domains/ai_edit_v3/__init__.py`
- Create: `server/content_domains/ai_edit_v3/contracts.py`
- Create: `server/content_domains/ai_edit_v3/schemas/edit-plan-2.0.schema.json`
- Create: `server/content_domains/ai_edit_v3/schemas/render-manifest-v1.schema.json`
- Create: `server/content_domains/ai_edit_v3/schemas/quality-verdict-v1.schema.json`
- Create: `tests/test_ai_edit_v3_contracts.py`
- Create: `tests/test_ai_edit_v3_schemas.py`
- Create: `tests/fixtures/ai_edit_v3/valid-edit-plan-2.0.json`
- Create: `tests/fixtures/ai_edit_v3/valid-render-manifest-v1.json`
- Create: `tests/fixtures/ai_edit_v3/valid-quality-verdict-v1.json`

**Interfaces:**
- Consumes: Task A1's installed exact dependency closure, standard `Mapping`, `Path`, `hashlib`, `json`, and `jsonschema.Draft202012Validator`.
- Produces: section 2.1 request functions, section 2.2 validators, state constants from section 3 and `ContractError(error_code, field_path, message)`.

- [ ] **Dependency gate: verify the exact Schema runtime before contract work**

Run: `python -m unittest tests.test_ai_edit_v3_dependencies -v`

Expected: PASS with `jsonschema==4.26.0`. If it fails, execute Task A1's exact install command; do not add an import fallback or weaken Schema tests.

- [ ] **Step 1: Write failing strict-union tests**

```python
def test_uploaded_video_rejects_even_null_unused_sources(self):
    body = valid_request(
        input_type="uploaded_video", source_upload_id="up-1",
        source_asset_id=None,
    )
    with self.assertRaisesRegex(ContractError, "input_discriminator_conflict"):
        normalize_job_request(body)

def test_creation_mode_fields_are_mutually_exclusive(self):
    body = valid_request(
        creation_mode="ai_auto", style_prompt="not allowed",
    )
    with self.assertRaisesRegex(ContractError, "creation_mode_conflict"):
        normalize_job_request(body)
```

- [ ] **Step 2: Run the request tests to verify RED**

Run: `python -m unittest tests.test_ai_edit_v3_contracts.RequestContractTests -v`

Expected: FAIL because the V3 package does not exist.

- [ ] **Step 3: Implement normalization and canonical fingerprinting**. Reject booleans where integers are required, unknown keys, control characters, duplicate material IDs, more than ten images and style prompts outside 1–1000 characters. Normalize only whitespace explicitly allowed by the contract; encode canonical JSON with UTF-8, `sort_keys=True`, compact separators and `allow_nan=False`; return lowercase SHA-256 hex.

- [ ] **Step 4: Add one positive and one negative fixture for every input/creation combination**. The 15 positive cases cover `5×3`; negatives cover wrong ratio, wrong authority field, client-supplied COS Key/model/render field, missing TTS text/voice, unpublished template reference marker and material list overflow.

- [ ] **Step 5: Run request contract tests**

Run: `python -m unittest tests.test_ai_edit_v3_contracts -v`

Expected: PASS.

- [ ] **Step 6: Write failing Schema meta-validation tests**

```python
def test_all_schemas_are_draft_2020_12_and_closed_recursively(self):
    for name in SCHEMA_NAMES:
        schema = load_schema(name)
        Draft202012Validator.check_schema(schema)
        assert_all_object_nodes_closed(schema)

def test_edit_plan_rejects_unknown_component_and_broken_timeline(self):
    plan = load_fixture("valid-edit-plan-2.0.json")
    plan["scenes"][0]["layout_id"] = "freeform_canvas"
    with self.assertRaisesRegex(ContractError, "director_capability_unknown"):
        validate_edit_plan(plan, timeline=self.timeline)
```

- [ ] **Step 7: Run Schema tests to verify RED**

Run: `python -m unittest tests.test_ai_edit_v3_schemas -v`

Expected: FAIL because the schema files and validators are absent.

- [ ] **Step 8: Author `edit-plan-2.0.schema.json`** with the exact root fields `version/duration_ms/ratio/creative_concept/theme/narrative_arc/captions/source_segments/scenes/materials/audio_cues`; enforce `duration_ms=3000..600000`, scenes `1..120`, captions `1..2000`, source segments `1..240`, materials `0..40`, audio cues `0..64`, at most four material slots and eight animations per scene, lowercase ASCII IDs, visible-text discriminators and the approved enums.

- [ ] **Step 9: Author `render-manifest-v1.schema.json`** with exact root fields `version/schema_sha256/renderer_environment/output_spec/duration_ms/edit_plan_sha256/registry_sha256/theme/seed/source_video/source_segments/master_audio/assets/compositions/captions`; require one `master_audio`, relative POSIX media paths, SHA-256 strings, silent source video declaration and no output path, URL, script or environment-secret fields.

- [ ] **Step 10: Author `quality-verdict-v1.schema.json`** with at most 64 checks, each result in `pass/fail/unknown`, confidence `0..1`, at most eight evidence objects, required evidence for `pass`, fixed check IDs and no repair prompt/result field.

- [ ] **Step 11: Implement strict parsing and cross-field validators**. Enforce scene continuity from zero to `duration_ms`, caption and cue bounds, source/output segment monotonicity, reference existence, capability membership, accurate-text protection, ratio/dimensions, asset path resolution below `sandbox_root`, ordinary-file checks and file SHA matching. Phase A `compressed` visible text is fail-closed to NFC-equivalent accurate text until a versioned trusted compression allowlist exists. Capability lists are mandatory, reject explicit null, and freeze `balanced_a` plus the published theme tokens. For render manifests, video-mode segments bind to `source_video` and its duration; when `source_video` is null, audio-mode segments bind to `master_audio`, use identity source/output intervals and remain within its duration.

- [ ] **Step 12: Add parser adversarial tests** for duplicate keys, two JSON roots, trailing text, `NaN`, `Infinity`, depth overflow, element overflow, string overflow, control characters, unknown nested keys, parent paths, absolute paths and symlinks.

- [ ] **Step 13: Run all contract and Schema tests**

Run: `python -m unittest tests.test_ai_edit_v3_contracts tests.test_ai_edit_v3_schemas -v`

Expected: PASS; each fixture records and matches its computed Schema SHA-256.

- [ ] **Step 14: Commit the contract boundary**

```powershell
git add server/content_domains/ai_edit_v3/__init__.py server/content_domains/ai_edit_v3/contracts.py server/content_domains/ai_edit_v3/schemas tests/test_ai_edit_v3_contracts.py tests/test_ai_edit_v3_schemas.py tests/fixtures/ai_edit_v3
git commit -m "feat(ai-edit-v3): freeze foundation contracts and schemas"
```

### Task 4: Build the isolated V3 store and migration guard

**Files:**
- Create: `server/content_domains/ai_edit_v3/store.py`
- Create: `tests/test_ai_edit_v3_store.py`

**Interfaces:**
- Consumes: `contracts.ALLOWED_TRANSITIONS`, canonical JSON and request SHA functions.
- Produces: `resolve_db_path(value: str | os.PathLike[str] | None = None) -> Path`, `assert_isolated_db(v3_path: Path, v2_path: Path | None) -> None`, `open_store(db_path: Path) -> sqlite3.Connection`, `init_db(db_path: Path | None = None, *, v2_db_path: Path | None = None) -> None`, and a `V3Store` wrapper exposing only parameterized operations.
- Environment contract: `AI_EDIT_V3_DB_PATH` is required outside tests and must be absolute; V2 comparison uses explicit `v2_db_path` or the configured `AI_EDIT_V2_DB` path.

- [ ] **Step 1: Write failing absolute-path and alias tests**

```python
def test_v3_database_must_be_absolute_and_not_alias_v2(self):
    with self.assertRaisesRegex(StoreConfigurationError, "v3_db_path_not_absolute"):
        resolve_db_path("relative/ai_edit_v3.db")
    v2 = self.root / "ai_edit_v2.db"
    v2.touch()
    hardlink = self.root / "ai_edit_v3.db"
    os.link(v2, hardlink)
    with self.assertRaisesRegex(StoreConfigurationError, "v2_v3_db_same_file"):
        assert_isolated_db(hardlink, v2)
```

- [ ] **Step 2: Run the path tests to verify RED**

Run: `python -m unittest tests.test_ai_edit_v3_store.V3StorePathTests -v`

Expected: FAIL because `store.py` does not exist.

- [ ] **Step 3: Implement path and filesystem checks**. Compare normalized absolute paths before creation; use `os.path.samefile` and `(st_dev, st_ino)` when both files exist; resolve parent-directory aliases; reject filesystem types `nfs`, `nfs4`, `cifs`, `smb3`, `fuse.sshfs` and `cosfs` by parsing `/proc/self/mountinfo` on Linux. Keep filesystem detection injectable so tests do not depend on the host filesystem.

- [ ] **Step 4: Write failing schema tests** that assert the complete table list in section 3, foreign keys enabled, `journal_mode=wal`, `busy_timeout>=10000`, billing operation CHECKs, cumulative refund CHECKs and absence of every `edit_v2_*` table.

- [ ] **Step 5: Run the schema tests to verify RED**

Run: `python -m unittest tests.test_ai_edit_v3_store.V3StoreSchemaTests -v`

Expected: FAIL because schema initialization is absent.

- [ ] **Step 6: Implement schema version 1**. `open_store` sets `busy_timeout` before changing journal mode, retries locked `PRAGMA journal_mode=WAL` with bounded exponential backoff for at most ten seconds, then enables foreign keys. `init_db` uses versioned, idempotent migrations and `BEGIN IMMEDIATE`; it never attaches or opens a V2 database.

- [ ] **Step 7: Add repeated concurrent migration tests**. For each of fresh DB, native DELETE-mode schema-v0 DB and existing WAL schema-v0 DB, start eight threads behind a barrier and repeat 50 rounds; all connections must finish at schema version 1 with the same table/index set and no lock error.

- [ ] **Step 8: Run migration races**

Run: `python -m unittest tests.test_ai_edit_v3_store.V3StoreMigrationRaceTests -v`

Expected: PASS for all three initial database modes.

- [ ] **Step 9: Add immutable quote/upload/material/job primitives**. Implement `insert_pricing_version`, `get_published_pricing_version`, `insert_quote`, `get_quote`, `insert_upload`, `complete_upload`, `insert_material`, `bind_job_materials`, `get_job_for_owner`, `list_jobs_for_owner` and cursor pagination. Owner mismatch returns no row; no function accepts raw SQL fragments or table names from callers.

- [ ] **Step 10: Run store tests twice**

Run: `python -m unittest tests.test_ai_edit_v3_store -v`

Run again: `python -m unittest tests.test_ai_edit_v3_store -v`

Expected: both runs PASS and leave no database outside each test temporary directory.

- [ ] **Step 11: Commit the isolated store**

```powershell
git add server/content_domains/ai_edit_v3/store.py tests/test_ai_edit_v3_store.py
git commit -m "feat(ai-edit-v3): add isolated versioned store"
```

### Task 5: Add fencing leases, immutable checkpoints and the exhaustive state CAS

**Files:**
- Modify: `server/content_domains/ai_edit_v3/store.py`
- Modify: `server/content_domains/ai_edit_v3/contracts.py`
- Modify: `tests/test_ai_edit_v3_store.py`
- Create: `tests/test_ai_edit_v3_pipeline.py`

**Interfaces:**
- Consumes: `LeaseClaim` and the complete state graph from sections 2.3 and 3.
- Produces: `claim_next_job`, `renew_lease`, `lease_owned`, `transition_leased`, `start_stage_attempt`, `finish_stage_attempt`, `save_checkpoint`, `record_provider_intent`, `bind_provider_result` and `close_running_attempts`.

- [ ] **Step 1: Write a failing stale-token test**

```python
def test_expired_worker_cannot_write_after_reclaim(self):
    self.seed_queued("job-1")
    old = claim_next_job("worker-a", 10, 100, db_path=self.db)
    new = claim_next_job("worker-b", 10, 111, db_path=self.db)
    self.assertGreater(new.fencing_token, old.fencing_token)
    self.assertFalse(transition_leased(
        old, {"queued"}, "generating_voice", {"status": "started"}, 112,
        lease_seconds=10, db_path=self.db,
    ))
    self.assertTrue(transition_leased(
        new, {"queued"}, "generating_voice", {"status": "started"}, 112,
        lease_seconds=10, db_path=self.db,
    ))
```

- [ ] **Step 2: Run the fencing test to verify RED**

Run: `python -m unittest tests.test_ai_edit_v3_store.V3LeaseTests.test_expired_worker_cannot_write_after_reclaim -v`

Expected: FAIL because lease operations are absent.

- [ ] **Step 3: Implement atomic claim and renewal**. Claim selects one runnable nonterminal job under `BEGIN IMMEDIATE`, increments `fencing_token`, sets worker and expiry, and returns the persisted token. Renewal and every subsequent mutation use `WHERE job_id=? AND worker_id=? AND fencing_token=? AND lease_until>?` in the same statement; a separate pre-read is not accepted as fencing.

- [ ] **Step 4: Add stale-write tests for every leased mutation**: transition, stage start, stage finish, checkpoint, provider intent, provider result binding, deadline extension, billing intent update and publish intent update. Each old-token call must return false or raise `LeaseLost` without altering row counts or state.

- [ ] **Step 5: Implement immutable stage/checkpoint semantics**. `start_stage_attempt` records `running` with token and input SHA; `finish_stage_attempt` requires the same claim and changes exactly one running row; `save_checkpoint` appends a new version only when input SHA changes, otherwise returns the existing version. `skipped` is a first-class stage result and still creates an attempt/checkpoint.

- [ ] **Step 6: Write a failing state-graph exhaustiveness test**

```python
def test_every_state_has_an_explicit_transition_contract(self):
    self.assertEqual(set(ALLOWED_TRANSITIONS), ALL_STATES)
    self.assertEqual(
        {state for state, targets in ALLOWED_TRANSITIONS.items() if not targets},
        TERMINAL_STATES,
    )
    for terminal in TERMINAL_STATES:
        self.assertFalse(store.force_transition_for_test(terminal, "queued"))
```

- [ ] **Step 7: Run the graph test to verify RED, then implement CAS validation**

Run: `python -m unittest tests.test_ai_edit_v3_pipeline.V3StateContractTests -v`

Expected before implementation: FAIL for a missing or incomplete state set. After implementing the exact section 3 graph and rejecting all table-external edges, rerun and expect PASS.

- [ ] **Step 8: Add deadline tests**. Confirm pre-debit sets `processing_deadline_at` once; reclaim and restart preserve it; the first `quality_checking -> repair_planning` CAS increments `repair_count` to one and adds exactly 600 seconds; a second repair and any other deadline extension fail.

- [ ] **Step 9: Add lease-loss cleanup tests**. A claim lost during a running stage closes that stage with `status="aborted_lease_lost"`; reclaim observes no permanent `running` attempt; a terminal job is never claimable.

- [ ] **Step 10: Run the store/pipeline contract suites**

Run: `python -m unittest tests.test_ai_edit_v3_store tests.test_ai_edit_v3_pipeline -v`

Expected: PASS, including two-worker barriers and all stale-token mutations.

- [ ] **Step 11: Commit fencing and state CAS**

```powershell
git add server/content_domains/ai_edit_v3/store.py server/content_domains/ai_edit_v3/contracts.py tests/test_ai_edit_v3_store.py tests/test_ai_edit_v3_pipeline.py
git commit -m "feat(ai-edit-v3): fence leases and state transitions"
```

### Task 6: Implement pricing, atomic pre-debit intent creation and ledger reconciliation

**Files:**
- Create: `server/content_domains/ai_edit_v3/billing.py`
- Modify: `server/content_domains/ai_edit_v3/store.py`
- Create: `tests/test_ai_edit_v3_billing.py`

**Interfaces:**
- Consumes: `PointsLedger`, authoritative transaction lookup from Gate P1, store quote/job/intent transactions and fencing claims.
- Produces: `QuoteBreakdown`, `BillingIntentDraft`, `BillingOutcome`, `create_quote`, `create_job_with_predebit`, `process_pending_intent`, `reconcile_unknown_intent`, `request_delta_refund`, `request_full_refund` and `list_due_billing_intents`.
- Keys: `ai-edit-v3:{job_id}:pre_debit`, `ai-edit-v3:{job_id}:refund_delta`, `ai-edit-v3:{job_id}:refund_full`; immutable request SHA disambiguates amount and target.

- [ ] **Step 1: Write failing quote freeze tests**

```python
def test_quote_freezes_request_price_version_and_fifteen_minute_expiry(self):
    quote = create_quote("alice", self.request, now=1_000, store=self.store)
    self.assertEqual(quote["request_sha256"], request_fingerprint(self.request))
    self.assertEqual(quote["expires_at"], 1_900)
    self.assertGreaterEqual(quote["max_points"], quote["min_points"])
    self.store.publish_pricing_version(self.more_expensive_version)
    self.assertEqual(self.store.get_quote(quote["quote_id"])["max_points"], quote["max_points"])
```

- [ ] **Step 2: Run quote tests to verify RED**

Run: `python -m unittest tests.test_ai_edit_v3_billing.V3QuoteTests -v`

Expected: FAIL because `billing.py` does not exist.

- [ ] **Step 3: Implement immutable pricing and quote math**. Store an explicit breakdown for base task, duration tier, TTS ceiling, Qwen ceiling, image ceiling, BGM/SFX ceiling, render complexity and one-repair reserve. Reject absent published pricing, expired quote, owner mismatch, request SHA mismatch and template/version mismatch before creating a billing intent.

- [ ] **Step 4: Write a failing local-transaction test**

```python
def test_job_and_predebit_intent_commit_together(self):
    with self.assertRaises(InjectedCommitFailure):
        self.billing.create_job_with_predebit(
            "alice", self.request, self.quote_id, "client-key-1", now=2_000,
            failpoint="after_job_before_intent",
        )
    self.assertEqual(self.store.count_jobs(), 0)
    self.assertEqual(self.store.count_billing_intents(), 0)
```

- [ ] **Step 5: Run the atomicity test to verify RED**

Run: `python -m unittest tests.test_ai_edit_v3_billing.V3PreDebitTests.test_job_and_predebit_intent_commit_together -v`

Expected: FAIL because the atomic store operation is absent.

- [ ] **Step 6: Implement `create_job_with_predebit` as one V3 transaction**. Insert `created_draft`, frozen quote reference, normalized request, owner/idempotency record and `pre_debit` intent before commit. Replaying the same owner/key/request returns the original job; the same key with a different request SHA raises `idempotency_conflict`; the outbox is the only caller of `PointsLedger.deduct`.

- [ ] **Step 7: Write failing unknown-response tests**. Fake the ledger applying the debit and then raising a transport error. Verify the job enters `billing_reconciling(reason="prehold", resume_state="preholding")`, a second deduct is never called, `query_transaction` confirms the original key, and the job then enters `queued` with a 45-minute absolute deadline.

- [ ] **Step 8: Implement pending and unknown reconciliation**. A never-submitted `pending` intent may call deduct/refund once. Once request transmission is uncertain, persist `first_unknown_at`, external key, reason, resume state and target, then only query authoritative ledger. After 300 seconds without authority, return `failed_reconciliation_pending`; do not convert transport failure to transaction absence.

- [ ] **Step 9: Add cumulative-refund tests**. Cover zero delta as a completed intent, successful delta followed by publication cancellation, full refund target equal to preheld total, request amount equal to `target-confirmed_refunded_total`, duplicated responses, overlapping refund prohibition and database rejection of over-refund.

- [ ] **Step 10: Add crash matrix tests** around intent insert, external request, authority query and local confirmation. Each failpoint must converge to one debit and cumulative refunds no greater than the confirmed prehold.

- [ ] **Step 11: Run the billing suite**

Run: `python -m unittest tests.test_ai_edit_v3_billing -v`

Expected: PASS with zero duplicate deduct/refund calls and explicit pending states for unresolved authority.

- [ ] **Step 12: Commit billing foundation**

```powershell
git add server/content_domains/ai_edit_v3/billing.py server/content_domains/ai_edit_v3/store.py tests/test_ai_edit_v3_billing.py
git commit -m "feat(ai-edit-v3): add crash-safe billing intents"
```

### Task 7: Freeze provider, renderer and runtime dependency contracts with fail-closed capabilities

**Files:**
- Create: `server/content_domains/ai_edit_v3/providers/__init__.py`
- Create: `server/content_domains/ai_edit_v3/providers/base.py`
- Create: `server/content_domains/ai_edit_v3/renderers/__init__.py`
- Create: `server/content_domains/ai_edit_v3/feature.py`
- Create: `server/content_domains/ai_edit_v3/runtime.py`
- Create: `tests/test_ai_edit_v3_feature.py`

**Interfaces:**
- Consumes: frozen Schema hashes, store isolation check, P1/P2 protocol implementations and environment variables.
- Produces: `ProviderResult`, `DefinitiveNotAccepted`, `SubmissionUnknown`, `RenderRequest`, `RenderResult`, `Renderer`, `RuntimeDependencies`, `CapabilityReport`, `load_config`, `build_runtime`, `preflight` and `assert_ready_for_request`.

- [ ] **Step 1: Write failing capability-state tests**

```python
def test_environment_variable_without_wiring_is_not_ready(self):
    env = {"AI_EDIT_V3_ENABLED": "1", "DASHSCOPE_API_KEY": "configured"}
    report = preflight(self.runtime(env=env, director=None))
    self.assertEqual(report.items["director"].status, "missing_or_unavailable")
    self.assertFalse(report.accepts_new_jobs)

def test_disabled_runtime_allows_reads_but_rejects_writes(self):
    report = preflight(self.runtime(env={"AI_EDIT_V3_ENABLED": "0"}))
    self.assertTrue(report.allows_existing_reads)
    self.assertFalse(report.accepts_uploads)
    self.assertFalse(report.accepts_new_jobs)
```

- [ ] **Step 2: Run feature tests to verify RED**

Run: `python -m unittest tests.test_ai_edit_v3_feature -v`

Expected: FAIL because feature/runtime modules are absent.

- [ ] **Step 3: Implement the three-state capability model**. Each capability reports one of `implemented`, `configured_and_wired`, `missing_or_unavailable`, plus a stable reason code. Required Phase A gates include isolated DB, all three Schema hashes, points query and asset publication contract; Phase B/C capabilities remain unavailable unless an injected implementation passes its probe.

- [ ] **Step 4: Define runtime protocols and dependencies**

```python
@dataclass(frozen=True)
class RuntimeDependencies:
    store: V3Store
    clock: Clock
    points: PointsLedger
    assets: AssetPublisher
    cos: object | None
    tts: object | None
    asr: object | None
    director: object | None
    image_generator: object | None
    audio_generator: object | None
    renderer: Renderer | None
    process_supervisor: ProcessSupervisor
    stage_handlers: Mapping[str, StageHandler]
```

`Clock.now() -> float`; `ProcessSupervisor.terminate_job(job_id: str) -> None`; `StageHandler(job: Mapping[str, Any], context: StageContext) -> StageOutcome`. Later phases replace the `object | None` provider fields with their concrete Protocols without renaming the fields.

- [ ] **Step 5: Implement configuration validation**. Accept `AI_EDIT_V3_ENABLED`, absolute `AI_EDIT_V3_DB_PATH`, `AI_EDIT_V3_ENVIRONMENT=test|production`, owner-HMAC secret reference and concurrency limits. Reject V2/V3 same file, network database filesystem, missing Schema/hash support, production COS write permission in test mode and unsupported environment values before opening the queue.

- [ ] **Step 6: Add renderer and provider DTO tests**. Verify dataclasses are immutable, `SubmissionUnknown` differs from `DefinitiveNotAccepted`, renderer input has no URL/key/environment override fields and runtime version report includes Python, SQLite and exact `jsonschema` runtime version.

- [ ] **Step 7: Run feature/runtime tests**

Run: `python -m unittest tests.test_ai_edit_v3_feature -v`

Expected: PASS; production-style runtime with missing Phase B/C dependencies rejects new jobs without affecting reads.

- [ ] **Step 8: Commit runtime contracts**

```powershell
git add server/content_domains/ai_edit_v3/providers server/content_domains/ai_edit_v3/renderers server/content_domains/ai_edit_v3/feature.py server/content_domains/ai_edit_v3/runtime.py tests/test_ai_edit_v3_feature.py
git commit -m "feat(ai-edit-v3): add fail-closed runtime contracts"
```

### Task 8: Add the V3 publication outbox and authoritative decision recovery

**Files:**
- Create: `server/content_domains/ai_edit_v3/delivery.py`
- Modify: `server/content_domains/ai_edit_v3/store.py`
- Create: `tests/test_ai_edit_v3_delivery.py`

**Interfaces:**
- Consumes: Gate P2 `AssetPublisher`, `LeaseClaim`, stable delivery object keys and store publish intents.
- Produces: `PublicationProgress(next_state: str, checkpoint: Mapping[str, Any])`, `SharedAssetPublisher`, `create_publish_intent`, `register_current_generation`, `prepare_hidden`, `advance_publish`, `request_cancel`, `reconcile_asset_decision` and `list_due_publish_intents`. Task 10 is the only consumer allowed to apply `PublicationProgress.next_state` to a job.
- State evidence: operation in `prepare/register_generation/commit_publish/cancel_publish/query_decision`, persisted external key, generation, expected decision, first unknown time and last authoritative response.

- [ ] **Step 1: Write a failing publish-winner recovery test**

```python
def test_lost_commit_response_recovers_publish_winner_without_refund(self):
    claim = self.seed_settled_job(fencing_token=9)
    self.publisher.commit_effect_then_timeout = True
    outcome = self.delivery.advance_publish(claim, now=1_000)
    self.assertEqual(outcome.next_state, "asset_decision_reconciling")
    recovered = self.delivery.reconcile_asset_decision(claim, now=1_010)
    self.assertEqual(recovered.next_state, "completed")
    self.assertIsNotNone(self.store.get_job("alice", claim.job_id)["asset_id"])
    self.assertEqual(self.store.count_operation(claim.job_id, "refund_full"), 0)
```

- [ ] **Step 2: Run delivery recovery tests to verify RED**

Run: `python -m unittest tests.test_ai_edit_v3_delivery.V3PublicationRecoveryTests.test_lost_commit_response_recovers_publish_winner_without_refund -v`

Expected: FAIL because `delivery.py` does not exist.

- [ ] **Step 3: Implement persistent publish-intent creation**. `create_publish_intent` runs under the current lease, freezes `publish_generation=claim.fencing_token`, object key, immutable metadata SHA and deterministic keys for register/prepare/commit/cancel/query. The stable delivery object is never overwritten and no shared asset call occurs inside the V3 SQLite transaction.

- [ ] **Step 4: Implement generation-first operation order**. Before prepare, commit or cancel, call `register_generation` for the claim token. Persist the outbound operation before invoking the shared service. If a request may have reached the service but its response is lost, record `asset_decision_reconciling` and do not substitute a new idempotency key.

- [ ] **Step 5: Write failing cancellation tests**

```python
def test_cancel_must_win_before_full_refund_is_created(self):
    claim = self.seed_publish_job(fencing_token=11)
    self.publisher.cancel_status = "cancel_won"
    outcome = self.delivery.request_cancel(claim, now=2_000)
    self.assertEqual(outcome.next_state, "failed")
    full = self.store.get_billing_intent(claim.job_id, "refund_full")
    self.assertEqual(full["refund_target_total"], self.preheld)

def test_unknown_cancel_does_not_create_refund(self):
    claim = self.seed_publish_job(fencing_token=12)
    self.publisher.cancel_raises_unknown = True
    outcome = self.delivery.request_cancel(claim, now=2_000)
    self.assertEqual(outcome.next_state, "asset_decision_reconciling")
    self.assertIsNone(self.store.get_billing_intent(claim.job_id, "refund_full"))
```

- [ ] **Step 6: Run cancellation tests to verify RED, then implement one-winner handling**. `publish_won` always converges to completed and suppresses full refund; `cancel_won` creates the cumulative full-refund target and converges through `failed -> refund_pending`; `accepted/undecided` remains reconciling; stale generation causes the worker to stop and reload authority.

- [ ] **Step 7: Add five-minute pending tests**. At 299 seconds remain `asset_decision_reconciling`; at 300 seconds enter `failed_asset_decision_pending`, stop media handlers and release the lease. A later authoritative `publish_won` completes; a later `cancel_won` creates exactly one full-refund intent.
- [ ] In every recovery test, assert `reconcile_asset_decision` calls `query_decision(mode, source_job_id, persisted_query_idempotency_key)` and reuses the exact key frozen by Step 3 across retries, process restart and the transition into `failed_asset_decision_pending`; a new query key is a test failure.

- [ ] **Step 8: Add stale-worker race tests**. An old token cannot register, prepare, commit, cancel, save a decision or create refund after a higher token is claimed. Cover the special case where the old worker legally committed before the new generation registered: recovery must observe `publish_won`, complete and never refund.

- [ ] **Step 9: Run delivery and shared Saga suites**

Run: `python -m unittest tests.test_ai_edit_v3_delivery tests.test_video_asset_publish -v`

Expected: PASS for response-loss, concurrent arbitration and stale-token matrices.

- [ ] **Step 10: Commit the V3 publication client**

```powershell
git add server/content_domains/ai_edit_v3/delivery.py server/content_domains/ai_edit_v3/store.py tests/test_ai_edit_v3_delivery.py
git commit -m "feat(ai-edit-v3): reconcile asset publication decisions"
```

### Task 9: Implement upload intents, owner-bound application service and Phase A HTTP contracts

**Files:**
- Create: `server/content_domains/ai_edit_v3/service.py`
- Create: `server/content_domains/ai_edit_v3/api.py`
- Modify: `server/content_domains/ai_edit_v3/delivery.py`
- Modify: `server/content_domains/ai_edit_v3/store.py`
- Create: `tests/test_ai_edit_v3_service.py`
- Create: `tests/test_ai_edit_v3_api.py`

**Interfaces:**
- Consumes: section 2.1 request contract, feature gate, quote/billing store operations, V3 object-key builder and injected `UploadObjectStore`/`UploadInspector` fakes.
- Produces: exact `EditV3Service` and `dispatch` signatures, `build_object_key`, `create_upload`, `complete_upload`, `create_material` and all Phase A routes under `/api/v3/edit/*`.
- Source interfaces: `SourceCatalog.resolve_platform_asset(owner, asset_id)`, `resolve_audio_asset(owner, asset_id)`, `resolve_voice(owner, voice_id)` and `resolve_template(template_id, ratio)` return frozen owner-safe records or `None`; `CapacityGate.check(normalized_request) -> CapacityDecision` reports queue slots, required temporary bytes and `retry_after` before any pre-debit intent is created.

- [ ] **Step 1: Write failing V3 object-key tests**

```python
def test_object_key_uses_environment_owner_hmac_job_and_scope(self):
    key = build_object_key(
        environment="test", owner="alice", job_id="019f-test-job",
        scope="materials/uploaded", filename="image.webp",
        owner_hmac_secret=b"test-only-secret",
    )
    self.assertRegex(
        key,
        r"^test/ai-edit-v3/[0-9a-f]{24}/019f-test-job/materials/uploaded/[a-z0-9._-]+$",
    )
    self.assertNotIn("alice", key)
```

- [ ] **Step 2: Run the key tests to verify RED**

Run: `python -m unittest tests.test_ai_edit_v3_service.V3ObjectKeyTests -v`

Expected: FAIL because the key builder is absent.

- [ ] **Step 3: Implement `UploadObjectStore` and key validation**. The protocol exposes `presign_put(key, content_type, expires=900)`, `head_object(key)` and `delete_object(key)`. Use HMAC-SHA256 truncated to 24 lowercase hex characters, server-generated job/upload IDs, an explicit scope enum and a filename sanitizer. Reject `..`, backslashes, drive prefixes, absolute paths, query/fragment delimiters, unknown environments and keys outside the current environment prefix.

- [ ] **Step 4: Write failing upload ownership tests**. Cover unauthenticated request, owner A completing owner B upload as indistinguishable `404`, idempotent complete, observed HEAD size/MIME overriding browser claims, image-count 11, image 25 MB + 1 byte, task total 1 GiB + 1 byte, unsupported image type and upload expiry.

- [ ] **Step 5: Run upload tests to verify RED**

Run: `python -m unittest tests.test_ai_edit_v3_service.V3UploadTests -v`

Expected: FAIL because upload service methods are absent.

- [ ] **Step 6: Implement upload transaction flow**. `create_upload` writes the owner-bound intent before returning a 900-second PUT URL. `complete_upload` reads authoritative object metadata, invokes injected `UploadInspector`, persists observed type/size/duration/dimensions and returns the same upload record on exact replay. Provider URLs and signed PUT URLs are never stored.

- [ ] **Step 7: Implement `POST /materials` promotion**. `create_material(owner, upload_id)` accepts only a completed JPEG/PNG/WebP upload owned by that owner, creates one `edit_v3_materials` row on the first call and returns the same material ID on replay. Main video/audio uploads cannot be promoted to supplemental materials; an owner mismatch remains indistinguishable from absence.

- [ ] **Step 8: Write failing quote/job application tests**

```python
def test_job_requires_exact_quote_fingerprint_and_one_predebit_intent(self):
    quote = self.service.quote("alice", self.request, now=1_000)
    changed = dict(self.request, style_prompt="changed")
    with self.assertRaisesRegex(ServiceError, "quote_request_mismatch"):
        self.service.create_job(
            "alice", changed, quote["quote_id"], "client-key-1", now=1_001,
        )
    job = self.service.create_job(
        "alice", self.request, quote["quote_id"], "client-key-1", now=1_001,
    )
    replay = self.service.create_job(
        "alice", self.request, quote["quote_id"], "client-key-1", now=1_002,
    )
    self.assertEqual(replay["job_id"], job["job_id"])
    self.assertEqual(self.store.count_operation(job["job_id"], "pre_debit"), 1)
```

- [ ] **Step 9: Implement `EditV3Service`**. Resolve and freeze owner-bound platform/audio/voice/template/upload references before quote. Call `CapacityGate.check` before returning an actionable quote and again immediately before the atomic job+pre-debit transaction; queue full or insufficient temporary disk returns `capacity_unavailable` with `Retry-After` and creates neither job nor intent. Job creation re-normalizes input, verifies quote owner/SHA/price/version/expiry, calls the atomic job+pre-debit store operation and never invokes providers or renderers. Retry accepts only eligible predecessor failure states, records `predecessor_job_id`, resolves frozen bindings, creates a fresh quote/job/pre-debit and reserves the `retry:` namespace from client keys.

- [ ] **Step 10: Write failing HTTP dispatch tests** for every route listed in design section 17. Phase A implements capabilities, uploads, upload complete, materials, quote, jobs, list/detail/plan/result/retry; platform/audio/voice/template routes return capability-specific `503` until an injected catalog is ready, never an unqualified empty success. Verify 401, owner 404, capacity `Retry-After`, missing/oversized `Idempotency-Key`, disabled write `503`, JSON size/duplicate-key rejection and sanitized error responses.

- [ ] **Step 11: Run HTTP tests to verify RED**

Run: `python -m unittest tests.test_ai_edit_v3_api -v`

Expected: FAIL because `dispatch` is absent.

- [ ] **Step 12: Implement thin `api.dispatch`**. It performs method/path matching, authentication presence, body/header extraction and response mapping only, then calls `EditV3Service`. It does not import providers, ledger clients, shared publication service or renderer internals. Unrecognized non-V3 paths return `False`; recognized V3 paths always return `True` after sending a response.

- [ ] **Step 13: Run service/API suites**

Run: `python -m unittest tests.test_ai_edit_v3_service tests.test_ai_edit_v3_api -v`

Expected: PASS across all 15 normalized request combinations, owner boundaries and job idempotency conflicts.

- [ ] **Step 14: Commit the V3 application boundary**

```powershell
git add server/content_domains/ai_edit_v3/service.py server/content_domains/ai_edit_v3/api.py server/content_domains/ai_edit_v3/delivery.py server/content_domains/ai_edit_v3/store.py tests/test_ai_edit_v3_service.py tests/test_ai_edit_v3_api.py
git commit -m "feat(ai-edit-v3): add owner-bound foundation api"
```

### Task 10: Implement the sole-state-owner pipeline and reconciliation-first Worker

**Files:**
- Create: `server/content_domains/ai_edit_v3/pipeline.py`
- Create: `server/ai_edit_v3_worker.py`
- Modify: `server/content_domains/ai_edit_v3/runtime.py`
- Modify: `server/content_domains/ai_edit_v3/store.py`
- Modify: `tests/test_ai_edit_v3_pipeline.py`
- Create: `tests/test_ai_edit_v3_worker.py`

**Interfaces:**
- Consumes: `LeaseClaim`, `StageContext`, `StageOutcome`, `RuntimeDependencies`, billing/publish reconcilers and injected stage handlers.
- Produces: `JobRunResult`, `run_job`, `run_reconciliation_pass`, `worker_config`, `run_worker(stop_event, *, config=None, runtime=None)`.

- [ ] **Step 1: Write a failing skipped-stage audit test**

```python
def test_no_work_stage_records_skipped_before_transition(self):
    claim = self.seed_claim(input_type="uploaded_audio", state="generating_voice")
    result = run_job(claim, self.runtime_with_skipped("generating_voice"), db_path=self.db)
    attempt = self.store.latest_attempt(claim.job_id, "generating_voice")
    self.assertEqual(attempt["status"], "skipped")
    self.assertEqual(result.state, "normalizing")
```

- [ ] **Step 2: Run pipeline tests to verify RED**

Run: `python -m unittest tests.test_ai_edit_v3_pipeline.V3PipelineTests.test_no_work_stage_records_skipped_before_transition -v`

Expected: FAIL because `run_job` is absent.

- [ ] **Step 3: Implement one-stage execution**. Load the job through store, assert the claim, open a stage attempt, build `StageContext`, call exactly one registered handler, save immutable checkpoint/provider evidence, finish the attempt and transition using one fenced CAS. Missing handler returns a fail-closed `capability_unavailable` outcome; pipeline never invents success.

- [ ] **Step 4: Implement state-specific branches**. Billing and asset reconciliation call only their dedicated reconcilers; media stages call registered handlers; `quality_checking` grants one repair budget atomically; failures go through `failed -> refund_pending`; terminal and safety-pending states stop media processing.

- [ ] **Step 5: Write failing lease-loss process cleanup tests**. The fake handler blocks while a second worker reclaims. Verify `assert_active` raises `LeaseLost`, `ProcessSupervisor.terminate_job(job_id)` is called once, the running attempt closes and no checkpoint/state write from the old token succeeds.

- [ ] **Step 6: Implement heartbeat and cleanup**. Renew at a fraction of the lease, but treat renewal failure as final for that claim. Deadline, queue timeout, stop event and lease loss terminate the complete job process group and close active attempts before returning `JobRunResult`.

- [ ] **Step 7: Write failing reconciliation-only Worker tests**

```python
def test_disabled_worker_reconciles_but_never_claims_media(self):
    runtime = self.runtime(enabled=False, due_billing=1, queued_jobs=1)
    run_worker(self.stop_after_one_loop(), config=self.config, runtime=runtime)
    self.assertEqual(runtime.billing_queries, 1)
    self.assertEqual(runtime.claim_calls, 0)
    self.assertEqual(runtime.media_handler_calls, 0)
```

- [ ] **Step 8: Run Worker tests to verify RED**

Run: `python -m unittest tests.test_ai_edit_v3_worker -v`

Expected: FAIL because the Worker entry point is absent.

- [ ] **Step 9: Implement reconciliation-first polling**. Every loop processes due billing and asset-decision outboxes before queue claims. Disabled or not-ready runtime performs safe read-only reconciliation only. Ready runtime enforces queue wait/deadline, configured `pipeline_concurrency`, bounded claims and graceful stop without claiming V2 tables.

- [ ] **Step 10: Add fake end-to-end crash tests**. Inject deterministic handlers for all media states, kill before and after each stage transaction, restart with a higher token and prove exactly one checkpoint per input SHA, no permanent running attempt, one debit, bounded refund and one publication decision. Repeat for success, media failure, billing unknown and asset unknown.

- [ ] **Step 11: Run pipeline and Worker suites**

Run: `python -m unittest tests.test_ai_edit_v3_pipeline tests.test_ai_edit_v3_worker -v`

Expected: PASS with no real network, provider, COS or point mutation.

- [ ] **Step 12: Commit the Worker foundation**

```powershell
git add server/content_domains/ai_edit_v3/pipeline.py server/content_domains/ai_edit_v3/runtime.py server/content_domains/ai_edit_v3/store.py server/ai_edit_v3_worker.py tests/test_ai_edit_v3_pipeline.py tests/test_ai_edit_v3_worker.py
git commit -m "feat(ai-edit-v3): add fenced recovery worker"
```

### Task 11: Register the isolated V3 API routes in the shared content server

**Files:**
- Modify: `server/content_domains/core.py:18,1280-1283,1797-1800`
- Modify: `tests/test_ai_edit_v3_api.py`

**Interfaces:**
- Consumes: `ai_edit_v3.api.dispatch` from Task 9.
- Produces: shared HTTP forwarding for `/api/v3/edit/*`; all non-V3 routing remains byte-for-byte behaviorally compatible.

- [ ] **Step 1: Write failing shared-dispatch tests**

```python
def test_core_forwards_only_v3_prefix_to_v3_dispatch(self):
    with patch.object(ai_edit_v3_api, "dispatch", return_value=True) as dispatch:
        response = self.core_request("GET", "/api/v3/edit/capabilities", user=self.user)
    self.assertEqual(response.status, 200)
    dispatch.assert_called_once()

def test_v2_prefix_never_enters_v3_dispatch(self):
    with patch.object(ai_edit_v3_api, "dispatch", return_value=False) as dispatch:
        self.core_request("GET", "/api/v2/edit/capabilities", user=self.user)
    dispatch.assert_not_called()
```

- [ ] **Step 2: Run shared-dispatch tests to verify RED**

Run: `python -m unittest tests.test_ai_edit_v3_api.V3CoreDispatchTests -v`

Expected: FAIL because `core.py` does not import or dispatch V3.

- [ ] **Step 3: Add the minimal import and two dispatch branches**. Import `ai_edit_v3.api` once beside existing V2 API modules. In GET and POST handlers, call V3 dispatch only when the path starts with `/api/v3/edit/`; pass the same authenticated user shape as V2. Do not alter V2 route order, webhook behavior, job database or legacy cost map.

- [ ] **Step 4: Run V3 and V2 API regressions**

Run: `python -m unittest tests.test_ai_edit_v3_api tests.test_ai_edit_v2_api -v`

Expected: PASS; disabled V3 returns its own capability reason while every V2 route retains its prior response.

- [ ] **Step 5: Commit only the route boundary**

```powershell
git add server/content_domains/core.py tests/test_ai_edit_v3_api.py
git commit -m "feat(ai-edit-v3): register isolated api routes"
```

### Task 12: Document default-off, non-secret Phase A configuration

**Files:**
- Modify: `deploy/huangque-secrets.env.example`
- Modify: `tests/test_ai_edit_v3_feature.py`

**Interfaces:**
- Consumes: `feature.load_config` from Task 7.
- Produces: one auditable list of Phase A environment variable names with no operational secret value.

- [ ] **Step 1: Write a failing environment-manifest test**

```python
def test_env_example_is_default_off_and_contains_no_v3_secret_value(self):
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    self.assertIn("AI_EDIT_V3_ENABLED=0", text)
    self.assertIn("AI_EDIT_V3_ENVIRONMENT=test", text)
    self.assertIn("AI_EDIT_V3_DB_PATH=/home/ubuntu/content-api/ai_edit_v3.db", text)
    for name in V3_SECRET_NAMES:
        self.assertRegex(text, rf"(?m)^{name}=\s*$")
```

- [ ] **Step 2: Run the manifest test to verify RED**

Run: `python -m unittest tests.test_ai_edit_v3_feature.V3EnvironmentManifestTests -v`

Expected: FAIL because the V3 variables are absent.

- [ ] **Step 3: Add the exact example variables**

```dotenv
AI_EDIT_V3_ENABLED=0
AI_EDIT_V3_ENVIRONMENT=test
AI_EDIT_V3_DB_PATH=/home/ubuntu/content-api/ai_edit_v3.db
AI_EDIT_V3_OWNER_HMAC_SECRET=
AI_EDIT_V3_COS_SECRET_ID=
AI_EDIT_V3_COS_SECRET_KEY=
AI_EDIT_V3_COS_REGION=
AI_EDIT_V3_COS_BUCKET=
AI_EDIT_V3_COS_PREFIX=test/ai-edit-v3
AI_EDIT_V3_PIPELINE_CONCURRENCY=5
AI_EDIT_V3_RENDER_SLOTS=2
AI_EDIT_V3_QUEUE_LIMIT=50
```

The file documents names only. It does not configure the service, create the DB directory, grant COS permissions or enable V3.

- [ ] **Step 4: Add config parsing tests**. Empty secret fields remain unavailable capabilities; production environment rejects a `test/ai-edit-v3` prefix and test environment rejects a `production/ai-edit-v3` prefix; concurrency must be positive, render slots cannot exceed pipeline concurrency and queue limit is fixed to a safe positive integer.

- [ ] **Step 5: Run feature and repository secret checks**

Run: `python -m unittest tests.test_ai_edit_v3_feature tests.test_systemd_secrets -v`

Expected: PASS and no secret-like V3 value in the example.

- [ ] **Step 6: Commit only configuration documentation and its tests**

```powershell
git add deploy/huangque-secrets.env.example tests/test_ai_edit_v3_feature.py
git commit -m "docs(ai-edit-v3): declare default-off foundation config"
```

### Task 13: Prove dual-version isolation and close the Phase A gate

**Files:**
- Create: `tests/test_ai_edit_v3_isolation.py`
- Modify: `tests/test_ai_edit_v3_store.py`
- Modify: `tests/test_ai_edit_v3_worker.py`
- Modify: `tests/test_ai_edit_v3_billing.py`
- Modify: `tests/test_ai_edit_v3_delivery.py`

**Interfaces:**
- Consumes: all Phase A contracts, Authorization Gate A0 and prerequisite Gates P1/P2/P3.
- Produces: executable evidence that V3 cannot mutate or claim V2 state, stale workers cannot write, and unresolved authorities remain safe.

- [ ] **Gate check: verify A0 and all three prerequisite gates**

Run the Authorization Gate A0 block, then confirm Task A2's GitHub Actions run passed and Tasks 1/2 passed their shared suites.

Expected: approved clean specification revision plus green P1, P2 and P3 evidence. Otherwise Phase A cannot enter its closing verification.

- [ ] **Step 1: Write a failing cross-database isolation test**

```python
def test_each_database_contains_only_its_version_tables(self):
    v2_db = self.root / "ai_edit_v2.db"
    v3_db = self.root / "ai_edit_v3.db"
    self.init_v2(v2_db)
    init_db(v3_db, v2_db_path=v2_db)
    self.assertFalse(any(name.startswith("edit_v3_") for name in tables(v2_db)))
    self.assertFalse(any(name.startswith("edit_v2_") for name in tables(v3_db)))
```

- [ ] **Step 2: Add Worker and namespace isolation tests**. Assert V3 Worker opens only the configured V3 DB, V2 Worker never sees a V3 row, V3 transaction keys start `ai-edit-v3:`, V3 object keys start the configured environment prefix, asset mode is `ai_edit_v3`, and V3 errors/log records start with the V3 prefix.

- [ ] **Step 3: Run isolation tests to verify RED**

Run: `python -m unittest tests.test_ai_edit_v3_isolation -v`

Expected: at least one assertion fails until every namespace and Worker boundary is explicit.

- [ ] **Step 4: Close only the exposed isolation gaps**. Fix the responsible V3 module; do not add compatibility fallbacks to V2 adapters and do not relax an assertion. Re-run the single failing test after each correction.

- [ ] **Step 5: Add the complete fencing and crash matrix**. For every leased mutation, lose the lease and reclaim with a higher token; for every billing operation and publication operation, inject a crash immediately before and after the external call. Assert one pre-debit, refunds bounded by confirmed prehold, one authoritative asset decision, no duplicate visible asset, no permanent running attempt and no user-visible false refund/publication claim.

- [ ] **Step 6: Add safety-pending convergence tests**. Hold ledger or asset service unavailable beyond 300 seconds, verify the matching pending state and zero media calls, then restore the authority and verify convergence to the evidence-supported `completed`, `refunded` or `prehold_absent` state.

- [ ] **Step 7: Add tracked and untracked V3 fixture scanning**. Inspect paths containing `ai_edit_v3` or `ai-edit-v3` and reject private-key headers, Authorization/Cookie values, cloud secret patterns, signed query parameters, non-test database files and media output extensions. Allow only explicit fake tokens such as `test-only-secret` inside tests.

- [ ] **Step 8: Run the full Phase A V3 suite**

Run: `python -m unittest tests.test_ai_edit_v3_dependencies tests.test_auth_points tests.test_video_asset_publish tests.test_ai_edit_v3_contracts tests.test_ai_edit_v3_schemas tests.test_ai_edit_v3_store tests.test_ai_edit_v3_billing tests.test_ai_edit_v3_delivery tests.test_ai_edit_v3_feature tests.test_ai_edit_v3_service tests.test_ai_edit_v3_api tests.test_ai_edit_v3_pipeline tests.test_ai_edit_v3_worker tests.test_ai_edit_v3_isolation -v`

Expected: PASS with no skipped safety, fencing, billing or publication test.

- [ ] **Step 9: Run all V2 Python regressions**

Run: `python -m unittest discover -s tests -p "test_ai_edit_v2_*.py" -v`

Expected: all current V2 Python tests PASS; compare the executed count with the approved 413-test Python baseline and investigate any collection decrease before continuing.

- [ ] **Step 10: Run V2 frontend regression**

Run: `node --test tests/test_ai_edit_v2_ui.js`

Expected: all current V2 frontend tests PASS; compare the executed count with the approved 47-test frontend baseline and investigate any collection decrease before continuing.

- [ ] **Step 11: Run repository validation**

Run: `python scripts/ci_validate.py`

Run: `python scripts/stamp_assets.py --check`

Run: `git diff --check`

Expected: each command exits `0`.

- [ ] **Step 12: Commit the isolation evidence**

```powershell
git add tests/test_ai_edit_v3_isolation.py tests/test_ai_edit_v3_store.py tests/test_ai_edit_v3_worker.py tests/test_ai_edit_v3_billing.py tests/test_ai_edit_v3_delivery.py
git commit -m "test(ai-edit-v3): prove phase-a isolation and recovery"
```

- [ ] **Step 13: Verify the Phase A change boundary after commit**

Run: `git diff --name-only 0ef1d37...HEAD`

Expected: only the files declared in this plan appear. The output must not contain V2 implementation modules, `server/content_domains/video.py`, Workbench pages, admin pages, renderer implementation, production database files or generated media.

## 4. Phase A Coverage and Exit Gate

| Approved Phase A requirement | Implementing task | Proof command |
| --- | --- | --- |
| Exact executable JSON Schema dependency closure | Task A1 | `python -m unittest tests.test_ai_edit_v3_dependencies.V3DependencyManifestTests -v` |
| Separately authorized public CI installation | Task A2 / Gate P3 | GitHub Actions `代码与安全门禁` passes with the dedicated install step |
| Shared authoritative point lookup | Task 1 | `python -m unittest tests.test_auth_points -v` |
| Shared hidden prepare and one-winner publication Saga | Task 2 | `python -m unittest tests.test_video_asset_publish -v` |
| Five input and three creation-mode contracts | Task 3 and Task 9 | `python -m unittest tests.test_ai_edit_v3_contracts tests.test_ai_edit_v3_service -v` |
| Three JSON Schema 2020-12 documents and hashes | Task 3 | `python -m unittest tests.test_ai_edit_v3_schemas -v` |
| Independent absolute-path V3 database and migrations | Task 4 | `python -m unittest tests.test_ai_edit_v3_store.V3StoreMigrationRaceTests -v` |
| Monotonic fencing lease and exhaustive state graph | Task 5 | `python -m unittest tests.test_ai_edit_v3_store tests.test_ai_edit_v3_pipeline -v` |
| Quote, pre-debit, cumulative refund and unknown reconciliation | Task 6 | `python -m unittest tests.test_ai_edit_v3_billing -v` |
| Explicit capability readiness and default-off operation | Task 7 and Task 12 | `python -m unittest tests.test_ai_edit_v3_feature -v` |
| Asset decision unknown and stale-worker publication defense | Task 8 | `python -m unittest tests.test_ai_edit_v3_delivery -v` |
| Upload, owner boundary, quote/job/retry and HTTP shell | Task 9 and Task 11 | `python -m unittest tests.test_ai_edit_v3_service tests.test_ai_edit_v3_api -v` |
| Reconciliation-first fake-provider Worker and crash recovery | Task 10 | `python -m unittest tests.test_ai_edit_v3_pipeline tests.test_ai_edit_v3_worker -v` |
| V2/V3 database, Worker, point, COS and asset isolation | Task 13 | `python -m unittest tests.test_ai_edit_v3_isolation -v` |

Phase A is complete only when Authorization Gate A0 and prerequisite Gates P1/P2/P3 are satisfied, the exact dependency runtime is installed in CI, all Phase A and V2 regression commands pass, `AI_EDIT_V3_ENABLED` remains `0`, no real external capability was called, and the diff contains only the declared files. A later explicitly authorized test deployment additionally requires the Task A1 test-server installation commands to pass before service restart. Completion authorizes preparation of the Phase B implementation review; it does not authorize push, merge, deployment, package installation on a server, real-provider smoke, real point mutation or production activity.
