# AI 智能剪辑 V2 Phase E Quality and Acceptance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用版本化视觉基准、创意八维、30 条多类型测试集、5–10 并发和 45/60 分钟预算证明 V2 可稳定交付，并形成测试环境运营、回滚和实验性补录去留结论。

**Architecture:** 验收执行器从脱敏 manifest 创建任务并收集不可变运行指标；技术硬门槛由确定性检查器判定，创意八维先由视觉模型评分、再由人工抽检校准。验收报告将成功率、降级率、费用、耗时和失败原因与每个任务绑定，未达门槛时 V2 保持默认关闭。

**Tech Stack:** Python 3、`unittest`、SQLite、JSON/CSV 报告、视觉评分 Provider、FFprobe/FFmpeg、Node UI 测试、测试环境 systemd/nginx 运行手册。

## Global Constraints

- [ ] 依赖 Phase A–D 全部通过；Phase E 不放宽任何内容、技术、声音或安全硬门槛。
- [ ] 30 条素材优先使用已有真实且获授权素材；公开/AI 生成补充素材必须在 manifest 写来源、授权状态和用途。
- [ ] 压力测试只在用户明确批准的测试窗口执行；计划和自动化默认 dry-run，不触发真实任务。
- [ ] 视觉模型评分不能覆盖技术失败；技术硬门槛失败的任务直接不合格。
- [ ] 测试环境并发初始为 5，逐级到 10；供应商限额、CPU、内存、磁盘或错误率越界立即停止升级。
- [ ] 45/60 分钟是初始配置，报告可以建议调整但不得自动修改生产参数。
- [ ] 内容安全审核仍不在本阶段，不得在报告中标记为已覆盖。

---

## 1. Phase E 精确文件结构

**Create**

- `tests/fixtures/ai_edit_v2/acceptance-manifest.json`：30 条脱敏用例元数据。
- `tests/fixtures/ai_edit_v2/visual-baselines.json`：已审核 Remotion/Shotcraft 视觉基准描述和授权。
- `server/content_domains/ai_edit_v2_scoring.py`：八维评分、版本和人工校准。
- `tests/test_ai_edit_v2_scoring.py`
- `tests/test_ai_edit_v2_acceptance_manifest.py`
- `tests/test_ai_edit_v2_concurrency.py`
- `tests/test_ai_edit_v2_time_budget.py`
- `tests/test_ai_edit_v2_experimental_dub.py`
- `scripts/ai_edit_v2_acceptance.py`：dry-run/execute/report 命令。
- `scripts/ai_edit_v2_load_test.py`：5→10 阶梯并发和停止条件。
- `docs/operations/ai-edit-v2-runbook.md`：测试部署、观测、停用和回滚。
- `docs/reports/ai-edit-v2-acceptance-template.md`：报告固定字段。

**Modify**

- `server/content_domains/ai_edit_v2_quality.py`：八维评分接线但保留硬门槛优先级。
- `server/content_domains/ai_edit_v2_store.py`：验收指标和降级统计查询。
- `server/content_domains/ai_edit_v2_pipeline.py`：实验性少量改写/补录功能开关。
- `server/content_domains/ai_edit_v2_audio.py`：补录遮盖约束。
- `deploy/systemd/huangque-ai-edit-v2.service`：5–10 worker 参数由 env 控制。
- `deploy/huangque-secrets.env.example`：评分和实验开关变量名。

## 2. 冻结验收数据接口

```python
def score_creativity(job_id: str, frames: list[str], audio_metrics: dict,
                     plan: dict, client, rubric_version: str) -> dict: ...
def validate_manifest(manifest: dict) -> dict: ...
def run_case(case: dict, api_client, dry_run: bool = True) -> dict: ...
def summarize(results: list[dict]) -> dict: ...
def should_increase_concurrency(sample: dict, thresholds: dict) -> bool: ...
```

八维固定为 `content_match/structural_difference/information_hierarchy/animation_quality/pacing/sound_design/visual_finish/technical_quality`，每维 1–5；成功样本平均分 `>=4` 且任一维 `>=3`。

## Task 1: 建立 30 条测试集 manifest 和授权门禁

**Files:**
- Create: `tests/fixtures/ai_edit_v2/acceptance-manifest.json`
- Create: `tests/fixtures/ai_edit_v2/visual-baselines.json`
- Create: `tests/test_ai_edit_v2_acceptance_manifest.py`

- [ ] **Step 1: 写失败测试**，manifest 必须恰好 30 条，六类 `digital_talking/real_talking/audio_only/product/travel_food/interview_course` 各 5 条。
- [ ] **Step 2: 写覆盖测试**，每类包含不同长度、9:16/16:9、完整/缺失素材；全局覆盖 natural brief、platform template、open generation。
- [ ] **Step 3: 写来源测试**，每个媒体条目必须有 `source/license_or_consent/sha256/owner_scope`；缺失或未授权拒绝执行。
- [ ] **Step 4: 写 must/reference 测试**，至少包含 required=10 边界、direct_use、style_only、纯音频无主视频和目标时长延长用例。
- [ ] **Step 5: 运行 `python -m unittest tests.test_ai_edit_v2_acceptance_manifest -v`**；预期失败。
- [ ] **Step 6: 建立 30 条元数据清单和视觉基准描述；文件路径只指向测试服务器受控素材，不提交真实用户媒体。
- [ ] **Step 7: 重跑测试**；预期通过。
- [ ] **Step 8: 提交**

```powershell
git add tests/fixtures/ai_edit_v2/acceptance-manifest.json tests/fixtures/ai_edit_v2/visual-baselines.json tests/test_ai_edit_v2_acceptance_manifest.py
git commit -m "test(ai-edit-v2): define authorized acceptance corpus"
```

## Task 2: 创意八维评分和人工校准

**Files:**
- Create: `server/content_domains/ai_edit_v2_scoring.py`
- Create: `tests/test_ai_edit_v2_scoring.py`
- Modify: `server/content_domains/ai_edit_v2_quality.py`

- [ ] **Step 1: 写 schema 测试**，视觉模型必须返回八个整数分、逐维证据帧、置信度和 rubric_version；缺维度或越界拒绝。
- [ ] **Step 2: 写硬门槛优先测试**，MP4/事实/必须素材/声音任一硬失败时，创意 5 分也不能通过。
- [ ] **Step 3: 写伪多风格测试**，结构指纹高度相同且仅颜色/标题变化时 structural_difference 不得超过 2。
- [ ] **Step 4: 写人工校准测试**，抽检记录保留 model_score/human_score/delta/reason；系统性偏差只生成新 rubric 版本，不篡改旧结果。
- [ ] **Step 5: 运行 `python -m unittest tests.test_ai_edit_v2_scoring -v`**；预期失败。
- [ ] **Step 6: 实现帧采样、脱敏评分输入、严格 JSON 校验、结构指纹和版本化校准。
- [ ] **Step 7: 重跑测试**；预期通过。
- [ ] **Step 8: 提交**

```powershell
git add server/content_domains/ai_edit_v2_scoring.py server/content_domains/ai_edit_v2_quality.py tests/test_ai_edit_v2_scoring.py
git commit -m "feat(ai-edit-v2): score creativity with calibrated rubric"
```

## Task 3: 45/60 分钟与排队时间验证

**Files:**
- Create: `tests/test_ai_edit_v2_time_budget.py`
- Modify: `server/content_domains/ai_edit_v2_pipeline.py`
- Modify: `server/content_domains/ai_edit_v2_store.py`

- [ ] **Step 1: 写 fake clock 测试**，queued 时间单独累计且不消耗 processing budget；normalizing 首次进入时固定 `processing_started_at`。
- [ ] **Step 2: 写 45 分钟测试**，无成片到期进入相应 failed 并全退；不能进入 repairing。
- [ ] **Step 3: 写 60 分钟测试**，45 分钟内已有成片且 QC 给出明确 repairable issue 才获得最多 900 秒；60 分钟硬终止。
- [ ] **Step 4: 写快速完成测试**，系统不等待预算耗满，阶段完成立即推进。
- [ ] **Step 5: 运行 `python -m unittest tests.test_ai_edit_v2_time_budget -v`**；预期失败。
- [ ] **Step 6: 修正预算计算、API 剩余时间字段和 stage attempt 指标；参数只从版本化 env/config 读取。
- [ ] **Step 7: 重跑测试**；预期通过。
- [ ] **Step 8: 提交**

```powershell
git add server/content_domains/ai_edit_v2_pipeline.py server/content_domains/ai_edit_v2_store.py tests/test_ai_edit_v2_time_budget.py
git commit -m "test(ai-edit-v2): enforce queue and processing budgets"
```

## Task 4: 5–10 Worker 阶梯并发和停止条件

**Files:**
- Create: `scripts/ai_edit_v2_load_test.py`
- Create: `tests/test_ai_edit_v2_concurrency.py`
- Modify: `deploy/systemd/huangque-ai-edit-v2.service`

**Interfaces:** dry-run 默认；真实执行必须显式 `--execute --base-url <test> --account-file <local-untracked>`，拒绝生产域名。

- [ ] **Step 1: 写租约并发测试**，10 worker 不重复领取，单用户/全局上限生效，终态数等于创建数。
- [ ] **Step 2: 写停止策略测试**，错误率、P95 stage time、供应商 429、CPU、内存、磁盘任一越界时不升并发并停止新提交。
- [ ] **Step 3: 写安全测试**，脚本默认只输出计划；目标为生产域名/IP 时硬拒绝；账号凭据不写报告。
- [ ] **Step 4: 运行 `python -m unittest tests.test_ai_edit_v2_concurrency -v`**；预期失败。
- [ ] **Step 5: 实现 5→7→10 阶梯、每级样本/冷却、指标采集和 JSON 报告；systemd workers 仍由 env 控制。
- [ ] **Step 6: 重跑测试**；预期通过。
- [ ] **Step 7: 提交**

```powershell
git add scripts/ai_edit_v2_load_test.py deploy/systemd/huangque-ai-edit-v2.service tests/test_ai_edit_v2_concurrency.py
git commit -m "test(ai-edit-v2): add guarded worker load test"
```

## Task 5: 实验性少量改写和补录隔离验证

**Files:**
- Create: `tests/test_ai_edit_v2_experimental_dub.py`
- Modify: `server/content_domains/ai_edit_v2_pipeline.py`
- Modify: `server/content_domains/ai_edit_v2_audio.py`
- Modify: `deploy/huangque-secrets.env.example`

**Interfaces:** 功能开关 `AI_EDIT_V2_EXPERIMENTAL_DUB=0` 默认关闭；该能力不是首轮稳定验收依赖。

- [ ] **Step 1: 写关闭测试**，默认永不调用配音 Provider，导演不得把补录作为完成必要条件。
- [ ] **Step 2: 写事实保护测试**，启用时不能改品牌、产品、数字、价格或招商事实；只允许配置上限内少量补句。
- [ ] **Step 3: 写遮盖测试**，补录区间必须由 B-roll、产品/门店、AI 素材、图表或 MG 覆盖；人物正脸/明显口型区间拒绝。
- [ ] **Step 4: 写禁止项测试**，不得触发数字人口型重生成；无法安全遮盖时缩短或取消补句。
- [ ] **Step 5: 运行 `python -m unittest tests.test_ai_edit_v2_experimental_dub -v`**；预期失败。
- [ ] **Step 6: 实现功能开关和实验记录，仅在测试环境明确开启；报告统计自然度、口型风险、额外费用和耗时。
- [ ] **Step 7: 重跑测试**；预期通过。
- [ ] **Step 8: 提交**

```powershell
git add server/content_domains/ai_edit_v2_pipeline.py server/content_domains/ai_edit_v2_audio.py deploy/huangque-secrets.env.example tests/test_ai_edit_v2_experimental_dub.py
git commit -m "test(ai-edit-v2): isolate experimental narration patching"
```

## Task 6: 30 条执行器、聚合报告和验收判定

**Files:**
- Create: `scripts/ai_edit_v2_acceptance.py`
- Create: `tests/test_ai_edit_v2_acceptance_runner.py`
- Create: `docs/reports/ai-edit-v2-acceptance-template.md`
- Modify: `server/content_domains/ai_edit_v2_store.py`

- [ ] **Step 1: 写 dry-run 测试**，输出 30 条预计入口、素材、比例、Provider 能力和最大预扣，不创建任务。
- [ ] **Step 2: 写聚合测试**，报告含技术门槛、required 使用率、事实准确率、45/60、开放降级率、八维、费用、主备切换、失败原因和退款次数。
- [ ] **Step 3: 写门槛测试**：技术 100%、required 100%、事实 100%、成功任务时限 100%、开放降级 `<=20%`、成功样本八维平均 `>=4` 且最低维 `>=3`。
- [ ] **Step 4: 写失败报告测试**，任何未交付任务必须退款一次；报告不泄漏账号、完整文案、COS key、签名 URL、密钥或堆栈。
- [ ] **Step 5: 运行 `python -m unittest tests.test_ai_edit_v2_acceptance_runner -v`**；预期失败。
- [ ] **Step 6: 实现 manifest 校验、提交/轮询、指标聚合、Markdown+JSON 输出和非零退出码验收判定。
- [ ] **Step 7: 重跑测试**；预期通过。
- [ ] **Step 8: 提交**

```powershell
git add scripts/ai_edit_v2_acceptance.py server/content_domains/ai_edit_v2_store.py tests/test_ai_edit_v2_acceptance_runner.py docs/reports/ai-edit-v2-acceptance-template.md
git commit -m "test(ai-edit-v2): automate thirty-case acceptance"
```

## Task 7: 测试环境运行、观测、停用和回滚手册

**Files:**
- Create: `docs/operations/ai-edit-v2-runbook.md`
- Create: `tests/test_ai_edit_v2_runbook.py`

- [ ] **Step 1: 写文档门禁测试**，要求手册包含测试域名确认、Stage Key、env 权限、DB 备份、单文件部署、服务启动、健康检查、日志、停用、回滚和退款核对。
- [ ] **Step 2: 写禁止项测试**，手册不得出现生产密钥/值、生产部署命令、整站 rsync、服务器热改或删除数据库命令。
- [ ] **Step 3: 运行 `python -m unittest tests.test_ai_edit_v2_runbook -v`**；预期失败。
- [ ] **Step 4: 编写手册**：默认 disabled；只从 pushed commit 部署；先 DB 备份/迁移 dry-run，再启动 worker；健康检查需 API、DB、COS、Provider Stage、FFprobe/FFmpeg 和 Remotion sandbox。
- [ ] **Step 5: 明确回滚**：置 `AI_EDIT_V2_ENABLED=0` 停新提交、Worker drain、等待/退款在途、恢复上一 commit 的本次文件；不影响旧模块。
- [ ] **Step 6: 重跑测试**；预期通过。
- [ ] **Step 7: 提交**

```powershell
git add docs/operations/ai-edit-v2-runbook.md tests/test_ai_edit_v2_runbook.py
git commit -m "docs(ai-edit-v2): define test operations and rollback"
```

## Task 8: 全量回归、验收执行门槛和最终提交

**Files:**
- Modify only if a failing test first proves a blocking defect in files introduced by Phase A–E.

- [ ] **Step 1: 执行全量自动化**

```powershell
python -m unittest discover -s tests -p "test_ai_edit_v2_*.py"
node --test tests/test_ai_edit_v2_ui.js
npm test --prefix services/ai-edit-remotion
npm run typecheck --prefix services/ai-edit-remotion
python -m unittest discover -s tests
python scripts/ci_validate.py
python scripts/stamp_assets.py --check
```

预期全部退出码 0。

- [ ] **Step 2: 执行 `python scripts/ai_edit_v2_acceptance.py --manifest tests/fixtures/ai_edit_v2/acceptance-manifest.json`**；预期只 dry-run，不创建任务。
- [ ] **Step 3: 用户批准测试窗口后，才可按 runbook 在测试环境执行 30 条和 5→10 并发；未经批准跳过真实执行并在风险项注明。
- [ ] **Step 4: 对每个真实失败先新增最小回归测试、确认红灯、修复、确认绿灯并独立 commit；不得批量无测试修补。
- [ ] **Step 5: 生成最终报告，实验补录给出“保持关闭/继续实验/进入正式”的证据结论；内容安全列为未覆盖。
- [ ] **Step 6: 若仅报告/基准更新，提交**

```powershell
git add docs/reports tests/fixtures/ai_edit_v2
git commit -m "docs(ai-edit-v2): record acceptance evidence"
```

## Phase E 验收

自动化门槛必须 100% 通过。真实 30 条执行还必须满足：技术硬门槛 100%、必须素材 100%、事实准确 100%、成功任务在 45/60 分钟内、开放降级率不高于 20%、创意八维达标、COS 可播可下载、失败只退款一次。任一项不满足时，`AI_EDIT_V2_ENABLED` 保持默认关闭，不申请生产上线。
