# AI 智能剪辑 V2 Master Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在完全隔离旧“一键剪辑”和 PR #20 的前提下，分五个可审查阶段交付通用 AI 视频剪辑 V2，并以 30 条测试集证明其计费、素材、混合渲染、创意和交付质量。

**Architecture:** V2 由现有内容 API 中的薄路由、独立 `ai_edit_v2.db`、独立租约队列 Worker、Provider 适配层、Shotstack 稳定渲染和隔离 Remotion 托管渲染组成。网站主进程只做鉴权、编排、计费和状态，不执行 AI 代码或重型渲染；所有媒体使用私有 COS 对象键持久化，签名 URL 仅在调用前生成。

**Tech Stack:** Python 3 标准库、SQLite/WAL、`unittest`、腾讯云 COS SDK、FFprobe/FFmpeg、阿里云 fun-asr、Qwen、Shotstack API、Node.js/TypeScript、Remotion 托管渲染、原生 HTML/CSS/JavaScript。

## Global Constraints

- [ ] 只在 `codex/ai-edit-v2` 开发；每次动代码前执行 `git fetch origin --prune`、`git status --short --branch`、`git branch --show-current`、`git log --oneline -5`。
- [ ] 保持旧“一键剪辑”、旧数据库、旧接口和 PR #20 代码不变；V2 不读取 `ai_edit.db`，不注册 `/api/gen/*` 剪辑路由。
- [ ] V2 固定使用页面 `site/workbench/ai-edit.html`、API `/api/v2/edit/*`、数据库 `ai_edit_v2.db`、日志前缀 `[ai-edit-v2]`。
- [ ] 所有功能先写失败测试，再做最小实现；每个任务测试通过后独立提交，提交中不得混入其他任务。
- [ ] 不提交密钥、数据库、用户数据、生成产物、签名 URL 或完整第三方响应；配置只提交变量名和无敏感示例。
- [ ] 测试环境默认 `AI_EDIT_V2_ENABLED=0`；部署必须来自已 push 的提交，且本计划不授权生产部署、服务器热改或整站覆盖。
- [ ] 数据库只保存 COS 对象键；签名 URL 不入库、不入日志、不进入 Qwen 上下文。
- [ ] AI 生成代码只能送往隔离 Remotion 服务；内容 API 与 V2 Worker 均不得解释、执行或 `eval` 该代码。
- [ ] 计费操作以全局唯一 `transaction_key` 幂等；内部重试不重复扣费，未交付质检合格成片必须全额退款一次。
- [ ] 内容安全审核明确不在本阶段；文件安全、归属、权限、代码沙箱和配置脱敏仍为硬要求。
- [ ] 每阶段结束执行该阶段定向测试、`python -m unittest discover -s tests`、`python scripts/ci_validate.py` 和 `python scripts/stamp_assets.py --check`。

---

## 1. 精确目录与职责

```text
server/
├── ai_edit_v2_worker.py                     # 独立 Worker 进程入口
├── content_domains/
│   ├── ai_edit_v2_api.py                    # /api/v2/edit/* HTTP 分发
│   ├── ai_edit_v2_schema.py                 # 请求、edit-plan 2.0、Render Graph 校验
│   ├── ai_edit_v2_store.py                  # ai_edit_v2.db 与租约/事件/检查点
│   ├── ai_edit_v2_pipeline.py               # 状态机、时间预算、恢复编排
│   ├── ai_edit_v2_billing.py                # 报价、预扣、结算、退款
│   ├── ai_edit_v2_media.py                  # FFprobe/FFmpeg 与临时目录
│   ├── ai_edit_v2_asr.py                    # fun-asr Provider 归一化
│   ├── ai_edit_v2_alignment.py              # 原文与 ASR 确定性对齐
│   ├── ai_edit_v2_director.py               # Qwen edit-plan 2.0
│   ├── ai_edit_v2_reference.py              # 参考风格抽象与终态清理
│   ├── ai_edit_v2_assets.py                 # 双窗口、四级匹配、素材槽位
│   ├── ai_edit_v2_audio.py                  # BGM/SFX/ducking/声音增强
│   ├── ai_edit_v2_router.py                 # 逐场景 Shotstack/Remotion 路由
│   ├── ai_edit_v2_quality.py                # 技术、内容、声音、创意质检
│   ├── ai_edit_v2_providers/                # 统一 Provider 与生成媒体适配器
│   ├── ai_edit_v2_templates/                # 审核模板 manifest 和版本
│   └── renderers/
│       ├── shotstack_v2.py                  # 稳定场景与最终时间线
│       └── remotion_v2.py                   # 隔离服务 API 客户端
services/
└── ai-edit-remotion/                        # 独立 Node/Remotion 沙箱与渲染服务
site/workbench/ai-edit.html                  # V2 唯一用户页面
tests/test_ai_edit_v2_*.py                   # Python 单元/集成测试
tests/test_ai_edit_v2_ui.js                  # 页面契约测试
tests/fixtures/ai_edit_v2/                   # 脱敏协议、媒体与验收清单
scripts/ai_edit_v2_acceptance.py             # 30 条验收执行器
deploy/systemd/huangque-ai-edit-v2.service   # 测试环境 Worker 模板
docs/operations/ai-edit-v2-runbook.md        # 测试环境运行与回滚手册
```

公共文件只允许以下增量：

- `server/content_domains/core.py`：在 `H.do_GET/do_POST` 顶部将 `/api/v2/edit/*` 交给 V2 API，旧 `/api/gen/*` 分支不改。
- `server/auth_server.py`：内部点数接口增加可选 `transaction_key` 原子幂等能力，旧调用保持兼容。
- `server/content_domains/cos.py`：补充 V2 所需的直传签名、对象核验、下载和删除原语，旧上传接口不变。
- `site/workbench/cloud-shell.js`：只新增“AI智能剪辑”导航入口和 `ai_edit_v2` 任务页面映射。
- `deploy/nginx-fang-locations.conf`、`deploy/huangque-secrets.env.example`：仅增加 V2 测试路由和变量名。

## 2. 跨阶段冻结接口

所有阶段必须复用下列签名，不得各自定义同名不同义类型：

```python
class ProviderRequest(TypedDict):
    capability: str
    job_id: str
    idempotency_key: str
    input_cos_keys: list[str]
    options: dict[str, object]
    deadline_at: int
    max_cost_points: int

class ProviderResult(TypedDict):
    provider_job_id: str
    status: str
    output_cos_keys: list[str]
    usage: dict[str, float]
    cost_points: int
    metadata: dict[str, object]

class StageResult(TypedDict):
    next_state: str
    checkpoint: dict[str, object]
    actual_cost_points: int

def dispatch(handler, method: str, path: str, user: dict | None) -> bool: ...
def claim_next_job(worker_id: str, lease_seconds: int, now: int) -> dict | None: ...
def run_stage(job_id: str, expected_state: str) -> StageResult: ...
def quote_job(owner: str, draft: dict, price_version: str) -> dict: ...
def precharge(job_id: str, owner: str, amount: int, transaction_key: str) -> None: ...
def settle(job_id: str, actual: int, transaction_key: str) -> None: ...
def refund(job_id: str, transaction_key: str) -> None: ...
def validate_edit_plan(plan: dict) -> dict: ...
def resolve_materials(job_id: str, plan: dict) -> dict: ...
def build_render_graph(resolved_plan: dict) -> dict: ...
def run_quality_checks(job_id: str, output_cos_key: str) -> dict: ...
```

协议版本固定为 `edit-plan 2.0`；`resolved_plan` 和 `render_graph` 是独立不可变版本，禁止覆盖原始导演方案。

## 3. 阶段依赖和交付门槛

| 顺序 | 阶段计划 | 依赖 | 独立完成定义 |
|---|---|---|---|
| A | `phase-a-foundation-plan.md` | 无 | V2 API/DB/Worker、双窗口上传、媒体标准化、ASR 对齐、报价预扣和状态机在假 Provider 下闭环 |
| B | `phase-b-stable-render-plan.md` | A | 平台模板、Qwen 语义导演、四级素材解析、Shotstack 稳定渲染、COS 交付与硬质检闭环 |
| C | `phase-c-generated-media-audio-plan.md` | A、B | 图片/短视频/图标图表/BGM/SFX Provider、声音处理和成本检查点进入稳定链路 |
| D | `phase-d-remotion-creative-plan.md` | A、B、C | 混合路由、隔离 Remotion、自由代码 2 次修复、降级和参考风格学习闭环 |
| E | `phase-e-quality-acceptance-plan.md` | A–D | 30 条测试、5–10 并发、45/60 分钟、创意八维和降级率验收报告完成 |

阶段 B 的端到端夹具必须提供完整且已授权的用户/平台素材；素材存在缺口时停在明确的 `generation_required` 检查点，由阶段 C 补齐后才能继续，不允许用假 URL 或空白媒体冒充完成。阶段 D 不得在阶段 B 的稳定交付通过前启用开放生成。阶段 E 通过前 `AI_EDIT_V2_ENABLED` 保持默认关闭。

## 4. 共同状态与数据契约

正常状态固定为：

```text
created -> validating -> quoting -> precharging -> queued -> normalizing
-> transcribing -> aligning_transcript -> directing -> resolving_assets
-> generating_assets -> designing_audio -> routing_render -> rendering
-> assembling -> quality_check -> repairing -> settling -> storing -> completed
```

失败状态固定为 `{stage}_failed`，其中 `{stage}` 取 `validation|transcription|director|asset|render|quality|settlement|storage`。`repairing` 仅允许从 `quality_check` 进入，只有已产生成片且存在明确修复项时使用，修复预算最多 15 分钟；处理预算从 `normalizing` 起最多 45 分钟，总预算最多 60 分钟。

任务事件必须记录 `job_id`、`stage`、`attempt`、`provider`、`model`、`request_id`、`duration_ms`、`usage`、`cost_points`、`switch_reason` 和脱敏错误码。不得记录完整文案、签名 URL、密钥或完整 Provider 响应。

## 5. 总体提交顺序

- [ ] 完成阶段 A 的每个任务提交并通过 A 门禁；标记 tag 候选 `ai-edit-v2-phase-a`。
- [ ] 从同一分支继续阶段 B；不得重写 A 的冻结接口，只能兼容扩展。
- [ ] 完成阶段 C 后运行 B+C 联合端到端，证明真实生成素材和声音均从检查点恢复。
- [ ] 完成阶段 D 后运行稳定模板回归与开放生成降级测试，证明失败不破坏稳定路线。
- [ ] 阶段 E 只修复验收中暴露的阻断缺陷；每个缺陷先补回归测试再修复并独立提交。
- [ ] 全部阶段通过后使用 `superpowers:requesting-code-review`，再使用 `superpowers:finishing-a-development-branch` 决定 PR；未经用户明确批准不部署。

## 6. 总体验收命令

```powershell
python -m unittest discover -s tests -p "test_ai_edit_v2_*.py"
node --test tests/test_ai_edit_v2_ui.js
python -m unittest discover -s tests
python scripts/ci_validate.py
python scripts/stamp_assets.py --check
```

期望：所有命令退出码为 `0`；旧 `test_content_domains.py` 的 handler 白名单和 `core.py` 体积门禁保持通过，证明 V2 未污染旧能力注册表。

## 7. Master 完成定义

- [ ] A–E 每个任务均有独立 commit、测试证据和可回滚边界。
- [ ] `rg -n "ai_edit\.db|/api/gen/.+edit|PR #20" server site tests` 不显示 V2 读取或修改旧实现。
- [ ] `rg -n "sk-|SecretKey|api[_-]?key\s*=" server site tests docs` 不出现真实密钥。
- [ ] 30 条验收报告包含素材来源/授权、入口、比例、用时、费用、降级、硬门槛和创意八维。
- [ ] 测试环境成功任务进入视频资产库并可播可下载；失败任务只退款一次。
- [ ] 生产环境未部署、生产密钥未使用、内容安全审核未被误标为已完成。
