# AI 智能剪辑 V2 Phase B Stable Render Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 Phase A 基础上交付可稳定验收的平台模板/自然要求导演、四级素材解析、Shotstack 常规场景、最终合成、COS 入库和硬质检端到端。

**Architecture:** Qwen 只产生 `edit-plan 2.0` 语义方案，服务端校验并解析素材槽位，生成不可变 `resolved_plan` 和 Shotstack `render_graph`。Shotstack 回调只触发主动回查，成片经 FFprobe/FFmpeg 和内容检查后转存私有 COS、写入现有视频资产库，再进行实际结算。

**Tech Stack:** Python 3、SQLite、`unittest`、Qwen JSON 模式、Shotstack Stage API、腾讯云 COS、FFprobe/FFmpeg、原生管理后台与工作台页面。

## Global Constraints

- [ ] 依赖 Phase A 全部通过；不得更改 Phase A 冻结签名，只能向 TypedDict 增加可选字段。
- [ ] Qwen 输入不含签名 URL、COS key、数据库 ID、供应商字段；Qwen 输出不含字幕正文或具体素材 ID。
- [ ] 本阶段稳定渲染只启用受审核 Shotstack 能力；Remotion 路由在 Phase D 接入，当前高级场景必须明确降级为稳定 Shotstack 组件。
- [ ] “必须使用”素材使用率不是建议值：提交渲染前和完成事务内均必须为 100%，否则失败退款。
- [ ] Shotstack Stage Key 只从环境读取；计划测试使用 fake client，不发起真实渲染。
- [ ] 旧视频资产读取/生成逻辑不改，只通过公开写入函数增加 V2 成片记录。

---

## 1. Phase B 精确文件结构

**Create**

- `server/content_domains/ai_edit_v2_templates.py`：模板 manifest、版本、发布状态和实例化边界。
- `server/content_domains/ai_edit_v2_templates/stable_business_v1.json`
- `server/content_domains/ai_edit_v2_templates/stable_story_v1.json`
- `server/content_domains/ai_edit_v2_director.py`：Qwen 提示词、一次协议修复和事实锁。
- `server/content_domains/ai_edit_v2_assets.py`：质量判定、四级匹配和槽位解释。
- `server/content_domains/ai_edit_v2_router.py`：Phase B 的 Shotstack 稳定路由。
- `server/content_domains/renderers/__init__.py`
- `server/content_domains/renderers/shotstack_v2.py`：timeline 构建、提交、回查和 webhook 归一化。
- `server/content_domains/ai_edit_v2_quality.py`：硬门槛和修复清单。
- `site/admin/ai-edit-v2-templates.html`：管理员模板发布页。
- `tests/test_ai_edit_v2_templates.py`
- `tests/test_ai_edit_v2_director.py`
- `tests/test_ai_edit_v2_assets.py`
- `tests/test_ai_edit_v2_shotstack.py`
- `tests/test_ai_edit_v2_quality.py`
- `tests/test_ai_edit_v2_delivery.py`

**Modify**

- `server/content_domains/ai_edit_v2_schema.py`：增加冻结 edit-plan 2.0 全结构校验。
- `server/content_domains/ai_edit_v2_pipeline.py`：接入 directing/resolving_assets/routing_render/rendering/assembling/quality_check/storing。
- `server/content_domains/ai_edit_v2_api.py`：模板列表、Shotstack webhook、结果查询字段。
- `server/content_domains/video.py`：增加独立 `record_external_video_asset(...)` 薄函数。
- `server/admin_api.py`：V2 模板审核、发布、下架和审计接口。
- `site/admin/index.html`：增加模板管理入口。
- `site/workbench/tasks.js`：识别 V2 任务并发送站内终态通知。
- `deploy/huangque-secrets.env.example`：Shotstack Stage 变量名。

## 2. 冻结接口

```python
def build_director_input(job: dict, transcript: dict,
                         style_abstract: dict | None) -> dict: ...
def direct(job: dict, transcript: dict, qwen_client) -> dict: ...
def list_published_templates(entry_mode: str | None = None) -> list[dict]: ...
def instantiate_template(template_version: str, plan: dict) -> dict: ...
def resolve_materials(job_id: str, plan: dict,
                      material_repo, quality_checker) -> dict: ...
def build_render_graph(resolved_plan: dict,
                       capabilities: dict) -> dict: ...
def build_timeline(render_graph: dict, signed_url_factory) -> dict: ...
def submit_render(job_id: str, timeline: dict, client) -> ProviderResult: ...
def reconcile_render(provider_job_id: str, client) -> ProviderResult: ...
def run_quality_checks(job_id: str, local_mp4: str,
                       expected: dict, runner=subprocess.run) -> dict: ...
def record_external_video_asset(job_id: str, username: str, title: str,
                                cos_key: str, duration: float,
                                width: int, height: int) -> int: ...
```

`resolved_plan.materials[*]` 固定为 `{slot_id,source,asset_id,cos_key,start_ms,end_ms,reason}`；`render_graph` 只保存对象键，`build_timeline` 才将键替换为短期 URL。

## Task 1: 平台模板目录和管理员发布审计

**Files:**
- Create: `server/content_domains/ai_edit_v2_templates.py`
- Create: `server/content_domains/ai_edit_v2_templates/stable_business_v1.json`
- Create: `server/content_domains/ai_edit_v2_templates/stable_story_v1.json`
- Create: `tests/test_ai_edit_v2_templates.py`
- Modify: `server/admin_api.py`
- Create: `site/admin/ai-edit-v2-templates.html`
- Modify: `site/admin/index.html`

- [ ] **Step 1: 写失败测试**：普通用户不能发布；管理员发布必须带二次确认 token；manifest 缺字体授权、组件版本、声音策略或能力边界时拒绝。
- [ ] **Step 2: 写实例化差异测试**：同模板对两段结构不同内容生成不同场景数、布局序列和素材位置，禁止仅换色/标题。
- [ ] **Step 3: 运行 `python -m unittest tests.test_ai_edit_v2_templates -v`**；预期失败。
- [ ] **Step 4: 实现只读 manifest loader、版本校验、published 状态和审计记录；模板内容不允许任意代码。
- [ ] **Step 5: 实现管理页二次确认、预览、发布/下架；用户 API 只返回已发布版本和安全字段。
- [ ] **Step 6: 重跑测试和 `python scripts/ci_validate.py`**；预期通过。
- [ ] **Step 7: 提交**

```powershell
git add server/content_domains/ai_edit_v2_templates.py server/content_domains/ai_edit_v2_templates server/admin_api.py site/admin/ai-edit-v2-templates.html site/admin/index.html tests/test_ai_edit_v2_templates.py
git commit -m "feat(ai-edit-v2): add audited platform templates"
```

## Task 2: Qwen 语义导演与 edit-plan 2.0 完整校验

**Files:**
- Create: `server/content_domains/ai_edit_v2_director.py`
- Create: `tests/test_ai_edit_v2_director.py`
- Modify: `server/content_domains/ai_edit_v2_schema.py`

**Interfaces:** Consumes 确定性 transcript 和抽象风格；Produces validated `edit-plan 2.0`，一次结构修复后仍非法即 `director_failed`。

- [ ] **Step 1: 写失败测试**，Qwen 输出必须含 `editorial_decisions/style_system/scenes/overlays/materials/caption_plan/audio_plan/delivery`；时间重叠、场景空洞、素材槽位悬空被拒绝。
- [ ] **Step 2: 写事实锁测试**：品牌、产品、数字、价格和招商事实只能引用 `fact_id`，Qwen 自造 `4999元` 被拒绝。
- [ ] **Step 3: 写隐私测试**，构建的 Qwen input 中无 `cos_key/asset_id/signed_url/provider_job_id`。
- [ ] **Step 4: 运行 `python -m unittest tests.test_ai_edit_v2_director -v`**；预期失败。
- [ ] **Step 5: 实现 prompt 版本、JSON 提取、一次 repair prompt、schema 校验和原始响应摘要脱敏；字幕正文由 alignment 数据后置生成。
- [ ] **Step 6: 增加目标时长测试**：语义压缩记录删改决定；必须事实放不下时延长并写 `target_extension_reason`。
- [ ] **Step 7: 重跑测试**；预期通过。
- [ ] **Step 8: 提交**

```powershell
git add server/content_domains/ai_edit_v2_director.py server/content_domains/ai_edit_v2_schema.py tests/test_ai_edit_v2_director.py
git commit -m "feat(ai-edit-v2): generate validated edit plan 2.0"
```

## Task 3: 四级素材匹配与必须素材强约束

**Files:**
- Create: `server/content_domains/ai_edit_v2_assets.py`
- Create: `tests/test_ai_edit_v2_assets.py`

**Interfaces:** Material levels 固定为 current task → user history → platform library → generated request。Phase B 的 level 4 只产出明确 `generation_request`，Phase C 才执行生成。

- [ ] **Step 1: 写优先级测试**，同一语义有四级候选时选当前任务；`style_only` 永不成为 direct asset；其他用户素材不可见。
- [ ] **Step 2: 写质量排除测试**，重复、模糊、无关、构图不合格的参考素材记录稳定 `exclusion_code` 和用户可读原因。
- [ ] **Step 3: 写必须素材测试**，每个合法 required material 至少绑定一次场景；绑定率不是 100% 时 `resolve_materials` 失败。
- [ ] **Step 4: 运行 `python -m unittest tests.test_ai_edit_v2_assets -v`**；预期失败。
- [ ] **Step 5: 实现确定性评分排序、slot binding、不可变 `resolved_plan` 版本和数据库关联事务。
- [ ] **Step 6: 写 level 4 测试**，缺口产出 `{slot_id,kind,semantic,generation_intent,deadline_at,max_cost_points}`，不伪造 URL 或 asset id。
- [ ] **Step 7: 重跑测试**；预期通过。
- [ ] **Step 8: 提交**

```powershell
git add server/content_domains/ai_edit_v2_assets.py tests/test_ai_edit_v2_assets.py
git commit -m "feat(ai-edit-v2): resolve semantic material slots"
```

## Task 4: Shotstack render graph 与短期签名边界

**Files:**
- Create: `server/content_domains/ai_edit_v2_router.py`
- Create: `server/content_domains/renderers/__init__.py`
- Create: `server/content_domains/renderers/shotstack_v2.py`
- Create: `tests/test_ai_edit_v2_shotstack.py`

**Interfaces:** Phase B router 把 `basic_caption|basic_card|broll|standard_transition` 路由 Shotstack；其他能力返回 `stable_fallback_required`，不冒充高级完成。

- [ ] **Step 1: 写 timeline 快照测试**，不同内容的场景/布局/卡片结构不同；字幕正文来自 aligned transcript；最终输出 1080p、H.264、AAC、目标比例。
- [ ] **Step 2: 写签名安全测试**，数据库 render graph 无 URL；签名 factory 仅在 `submit_render` 前调用；输出日志过滤 query string。
- [ ] **Step 3: 写幂等提交测试**，已有 provider job id 时调用 reconcile，不再次 POST；提交超时先查询 idempotency reference。
- [ ] **Step 4: 运行 `python -m unittest tests.test_ai_edit_v2_shotstack -v`**；预期失败。
- [ ] **Step 5: 实现 graph、timeline adapter、Stage client、有限退避回查和状态归一化；HTTP client 可注入。
- [ ] **Step 6: 增加字幕/素材开关测试**，关闭字幕不生成 caption track，无素材槽不生成 B-roll track。
- [ ] **Step 7: 重跑测试**；预期通过。
- [ ] **Step 8: 提交**

```powershell
git add server/content_domains/ai_edit_v2_router.py server/content_domains/renderers tests/test_ai_edit_v2_shotstack.py
git commit -m "feat(ai-edit-v2): adapt stable scenes to shotstack"
```

## Task 5: Webhook 去重、主动回查与重启恢复

**Files:**
- Modify: `server/content_domains/ai_edit_v2_api.py`
- Modify: `server/content_domains/ai_edit_v2_pipeline.py`
- Modify: `server/content_domains/ai_edit_v2_store.py`
- Create: `tests/test_ai_edit_v2_webhooks.py`

- [ ] **Step 1: 写失败测试**：错误 token 403；合法 token 使用常量时间比较；重复 body 只记录一次；乱序 processing 不覆盖 completed。
- [ ] **Step 2: 写回查测试**，webhook body 的 URL/status 不作为真值，只取 provider task id 并由 Shotstack client 主动查询。
- [ ] **Step 3: 运行 `python -m unittest tests.test_ai_edit_v2_webhooks -v`**；预期失败。
- [ ] **Step 4: 实现 SHA-256 事件指纹、唯一约束、终态单调映射和脱敏日志。
- [ ] **Step 5: 写 Worker 重启测试**，rendering job 已绑定 provider id 时恢复为 reconcile，成功后只生成一个 artifact 版本。
- [ ] **Step 6: 重跑测试**；预期通过。
- [ ] **Step 7: 提交**

```powershell
git add server/content_domains/ai_edit_v2_api.py server/content_domains/ai_edit_v2_pipeline.py server/content_domains/ai_edit_v2_store.py tests/test_ai_edit_v2_webhooks.py
git commit -m "feat(ai-edit-v2): reconcile shotstack webhooks safely"
```

## Task 6: 技术、内容和声音硬质检

**Files:**
- Create: `server/content_domains/ai_edit_v2_quality.py`
- Create: `tests/test_ai_edit_v2_quality.py`

**Interfaces:** `run_quality_checks` returns `{passed, checks, repairable_issues, fatal_issues, metrics}`；只有 `repairable_issues` 非空且已有成片才可 repairing。

- [ ] **Step 1: 写媒体测试**，拒绝不可解码、非 1080p、比例错误、无 AAC 音轨、时长异常、>300ms 黑帧、卡帧、缺图和错误占位。
- [ ] **Step 2: 写内容测试**，required 100%、事实锁、字幕语音一致、无明显补录口型冲突；任一失败为硬失败。
- [ ] **Step 3: 写布局测试**，文字越界/重叠、安全区、人脸/产品/Logo/二维码裁切生成明确 issue code。
- [ ] **Step 4: 写声音测试**，异常静音/爆音、对白可懂度、BGM/SFX 压人声失败。
- [ ] **Step 5: 运行 `python -m unittest tests.test_ai_edit_v2_quality -v`**；预期失败。
- [ ] **Step 6: 实现 runner 注入的 FFprobe/FFmpeg 检查器和确定性内容对照；视觉检查先接接口 fake，Phase E 才接评分模型。
- [ ] **Step 7: 重跑测试**；预期通过。
- [ ] **Step 8: 提交**

```powershell
git add server/content_domains/ai_edit_v2_quality.py tests/test_ai_edit_v2_quality.py
git commit -m "feat(ai-edit-v2): enforce stable render quality gates"
```

## Task 7: COS 成片、视频资产库和结算交付

**Files:**
- Create: `tests/test_ai_edit_v2_delivery.py`
- Modify: `server/content_domains/ai_edit_v2_pipeline.py`
- Modify: `server/content_domains/video.py`
- Modify: `server/content_domains/ai_edit_v2_api.py`
- Modify: `site/workbench/ai-edit.html`
- Modify: `site/workbench/tasks.js`
- Modify: `tests/test_notify_task_focus.py`

- [ ] **Step 1: 写失败测试**，只有 quality passed 才能 settling/storing；COS 上传成功后数据库保存 key；资产记录 owner 正确且幂等。
- [ ] **Step 2: 写下载测试**，查询 API 每次生成新的短期播放/下载 URL，响应不泄漏 COS key；他人 job 404。
- [ ] **Step 3: 写结算测试**，实际费用小于 hold 时只退差额一次；storage 失败无合格交付时全额退款一次。
- [ ] **Step 4: 运行 `python -m unittest tests.test_ai_edit_v2_delivery -v`**；预期失败。
- [ ] **Step 5: 实现 `record_external_video_asset` 薄函数和 V2 delivery transaction；不改变旧 `record_video_asset`。
- [ ] **Step 6: 页面显示质检、实际结算、降级状态、播放器、MP4 下载和“已进入视频资产库”，不显示供应商。
- [ ] **Step 7: 扩展全局任务追踪器**，V2 completed/failed 产生一次站内通知并可跳转 `ai-edit.html?job_id=...`，不增加短信/微信/邮件渠道。
- [ ] **Step 8: fake Shotstack 端到端从 directing 跑到 completed，并验证可从 checkpoint 重放不二扣/二写。
- [ ] **Step 9: 重跑测试与旧视频资产测试**；预期通过。
- [ ] **Step 10: 提交**

```powershell
git add server/content_domains/ai_edit_v2_pipeline.py server/content_domains/ai_edit_v2_api.py server/content_domains/video.py site/workbench/ai-edit.html site/workbench/tasks.js tests/test_ai_edit_v2_delivery.py tests/test_notify_task_focus.py
git commit -m "feat(ai-edit-v2): deliver quality-passed videos to assets"
```

## Phase B 验收

```powershell
python -m unittest tests.test_ai_edit_v2_templates tests.test_ai_edit_v2_director tests.test_ai_edit_v2_assets tests.test_ai_edit_v2_shotstack tests.test_ai_edit_v2_webhooks tests.test_ai_edit_v2_quality tests.test_ai_edit_v2_delivery -v
python -m unittest tests.test_content_domains tests.test_private_assets tests.test_video_history_and_elapsed -v
python scripts/ci_validate.py
python scripts/stamp_assets.py --check
```

预期：使用 fake Qwen/Shotstack/COS 的自然要求与平台模板各完成一条；必须素材使用率 100%；Stage 回调重复/乱序不破坏终态；旧任务、资产和 handler 回归全部通过。
