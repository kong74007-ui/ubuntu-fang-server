# AI 智能剪辑 V2 Phase A Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立完全独立的 V2 页面/API/数据库/Worker 基础，完成上传归属、媒体标准化、ASR 与确定性对齐、动态报价上限预扣、状态机、幂等和重启恢复。

**Architecture:** 内容 API 只增加 `/api/v2/edit/*` 薄分发；任务、素材、报价和租约全部写入 `ai_edit_v2.db`。独立 Worker 以 SQLite 租约领取任务并按检查点推进。点数仍由认证服务保管，但新增兼容旧调用的事务键，保证预扣、结算和退款幂等。

**Tech Stack:** Python 3、SQLite/WAL、`unittest`、腾讯云 COS SDK、FFprobe/FFmpeg、阿里云 fun-asr、原生 HTML/JavaScript。

## Global Constraints

- [ ] 遵守 master plan 全部约束；旧 `jobs`、`video_assets`、`ai_edit.db` 和 `/api/gen/*` 不参与 V2 状态机。
- [ ] Phase A 的外部 ASR、COS 和点数调用在测试中全部注入 fake，不消耗真实费用。
- [ ] 所有时间函数、UUID、HTTP 客户端和 subprocess runner 可注入，禁止依赖真实时钟导致不稳定测试。
- [ ] 上传完成前不信任浏览器提供的 MIME、大小或时长；必须通过 COS HEAD 与 FFprobe 核验。
- [ ] 只提交本计划列出的文件；不部署、不推送生产、不在服务器运行目录修改。

---

## 1. Phase A 精确文件结构

**Create**

- `server/content_domains/ai_edit_v2_schema.py`：请求枚举、状态、TypedDict 和纯校验函数。
- `server/content_domains/ai_edit_v2_store.py`：SQLite schema v1、事务、租约、检查点和事件。
- `server/content_domains/ai_edit_v2_api.py`：用户 API 与 webhook 以外的 Phase A HTTP 分发。
- `server/content_domains/ai_edit_v2_pipeline.py`：状态转换、时间预算、阶段执行器注册表。
- `server/content_domains/ai_edit_v2_billing.py`：报价、预扣、结算、全退。
- `server/content_domains/ai_edit_v2_media.py`：媒体探测、标准化和任务临时目录。
- `server/content_domains/ai_edit_v2_asr.py`：fun-asr Provider 和归一化结果。
- `server/content_domains/ai_edit_v2_alignment.py`：平台原文对齐。
- `server/ai_edit_v2_worker.py`：独立轮询 Worker 入口。
- `site/workbench/ai-edit.html`：Phase A 表单、报价和进度骨架。
- `tests/test_ai_edit_v2_schema.py`
- `tests/test_ai_edit_v2_store.py`
- `tests/test_ai_edit_v2_api.py`
- `tests/test_ai_edit_v2_billing.py`
- `tests/test_ai_edit_v2_media.py`
- `tests/test_ai_edit_v2_alignment.py`
- `tests/test_ai_edit_v2_pipeline.py`
- `tests/test_ai_edit_v2_ui.js`
- `site/admin/ai-edit-v2-pricing.html`：版本化价格表管理与发布。

**Modify**

- `server/content_domains/core.py`：两处 V2 前缀分发，每处不超过 8 行。
- `server/content_domains/cos.py`：增加 PUT 签名、HEAD、下载、删除对象原语。
- `server/auth_server.py`：内部点数接口接受 `transaction_key` 并原子去重。
- `server/admin_api.py`、`site/admin/index.html`：价格表草稿、二次确认发布和审计入口。
- `deploy/huangque-secrets.env.example`：增加 V2 变量名和非敏感默认值。
- `deploy/nginx-fang-locations.conf`：代理 `/api/v2/edit/`，回调查询串不记日志。
- `deploy/systemd/huangque-ai-edit-v2.service`：独立 Worker 服务模板。

## 2. 冻结接口

```python
EDIT_PLAN_VERSION = "2.0"
CREATION_MODES = {"natural_brief", "platform_template", "open_generation"}
ASPECT_RATIOS = {"9:16", "16:9"}
MATERIAL_PURPOSES = {"primary", "required", "reference"}
REFERENCE_MODES = {"direct_use", "style_only"}

def open_store(db_path: str) -> sqlite3.Connection: ...
def create_quote(owner: str, draft: dict, now: int) -> dict: ...
def create_job(owner: str, payload: dict, quote_id: str,
               idempotency_key: str, now: int) -> dict: ...
def claim_next_job(worker_id: str, lease_seconds: int, now: int) -> dict | None: ...
def renew_lease(job_id: str, worker_id: str, lease_seconds: int, now: int) -> bool: ...
def transition(job_id: str, expected: str, target: str,
               checkpoint: dict, now: int) -> bool: ...
def dispatch(handler, method: str, path: str, user: dict | None) -> bool: ...
def probe_media(path: str, runner=subprocess.run) -> dict: ...
def normalize_media(source: str, destination: str, media_type: str,
                    runner=subprocess.run) -> dict: ...
def transcribe(cos_key: str, client, deadline_at: int) -> dict: ...
def align_platform_text(original: str, asr_words: list[dict]) -> dict: ...
```

ASR 归一化结果固定为：

```python
{"language": "zh-CN", "duration_ms": 25000,
 "sentences": [{"start_ms": 0, "end_ms": 2100, "text": "..."}],
 "words": [{"start_ms": 0, "end_ms": 180, "text": "黄", "confidence": 0.98}]}
```

## Task 1: 锁定 Schema、状态和输入限制

**Files:**
- Create: `server/content_domains/ai_edit_v2_schema.py`
- Test: `tests/test_ai_edit_v2_schema.py`

- [ ] **Step 1: 写失败测试，覆盖两个上传窗口和协议禁区**

```python
class SchemaTests(unittest.TestCase):
    def test_rejects_more_than_ten_required_materials(self):
        draft = valid_draft(required_material_ids=[str(i) for i in range(11)])
        with self.assertRaisesRegex(ValueError, "必须使用.*10"):
            schema.validate_job_draft(draft)

    def test_edit_plan_rejects_urls_provider_fields_and_wrong_version(self):
        plan = valid_plan(version="1.0", scenes=[{"provider": "shotstack", "url": "https://x"}])
        with self.assertRaises(ValueError):
            schema.validate_edit_plan(plan)
```

- [ ] **Step 2: 运行 `python -m unittest tests.test_ai_edit_v2_schema -v`**；预期因模块不存在而失败。
- [ ] **Step 3: 最小实现枚举、容量常量、状态迁移表、`validate_job_draft` 和 `validate_edit_plan`**；递归拒绝键 `url|cos_key|provider|api_key|html|code`。
- [ ] **Step 4: 增加正向测试**，验证 9:16/16:9、中文、10 分钟、500/200/15/50MB、1GB、目标时长和三入口。
- [ ] **Step 5: 重跑测试**；预期全部通过。
- [ ] **Step 6: 提交**

```powershell
git add server/content_domains/ai_edit_v2_schema.py tests/test_ai_edit_v2_schema.py
git commit -m "feat(ai-edit-v2): define foundation contracts"
```

## Task 2: 建立独立数据库、租约队列和检查点

**Files:**
- Create: `server/content_domains/ai_edit_v2_store.py`
- Test: `tests/test_ai_edit_v2_store.py`

**Interfaces:** Consumes Task 1 状态常量；Produces `init_db/open_store/create_job/claim_next_job/renew_lease/transition/record_stage_attempt/record_provider_event`。

- [ ] **Step 1: 写失败测试**，临时数据库执行 `init_db()` 后断言存在 `edit_v2_jobs`、`edit_v2_materials`、`edit_v2_job_materials`、`edit_v2_stage_attempts`、`edit_v2_provider_jobs`、`edit_v2_provider_events`、`edit_v2_quotes`、`edit_v2_billing`、`edit_v2_render_artifacts` 和 `edit_v2_schema_meta`。
- [ ] **Step 2: 写并发租约测试**：两个连接同时领取仅一个获得同一任务；租约过期可恢复，活跃租约不可偷取；终态永不重新领取。
- [ ] **Step 3: 运行 `python -m unittest tests.test_ai_edit_v2_store -v`**；预期导入失败。
- [ ] **Step 4: 实现 schema v1**：WAL、`busy_timeout=10000`、外键、`BEGIN IMMEDIATE`；jobs 唯一键 `(owner,idempotency_key)`，provider events 唯一指纹，billing 唯一 `transaction_key`。
- [ ] **Step 5: 实现 CAS**：`transition` 必须带 expected state；checkpoint 追加版本而非覆盖原始 `director_plan_json`。
- [ ] **Step 6: 增加终态清理测试**：参考风格临时字段被清空，COS 对象键和审计记录保留。
- [ ] **Step 7: 重跑测试**；预期全部通过。
- [ ] **Step 8: 提交**

```powershell
git add server/content_domains/ai_edit_v2_store.py tests/test_ai_edit_v2_store.py
git commit -m "feat(ai-edit-v2): add isolated store and lease queue"
```

## Task 3: 增加 COS 直传与对象核验

**Files:**
- Modify: `server/content_domains/cos.py`
- Create: `tests/test_ai_edit_v2_cos.py`

**Interfaces:** Produces `presign_put(rel_key, content_type, expires=900) -> str`、`head_object(rel_key) -> dict`、`download_file(rel_key,destination)`、`delete_object(rel_key)`；数据库仍只接收 `rel_key`。

- [ ] **Step 1: 写 fake COS client 测试**，验证 PUT 签名最长 900 秒、HEAD 返回 `content_length/content_type/etag`、私有键不被拼成公开 URL。
- [ ] **Step 2: 写安全测试**，`../`、绝对路径、查询串、非 `ai-edit-v2/{owner_hash}/{uuid}/...` 前缀被拒绝。
- [ ] **Step 3: 运行 `python -m unittest tests.test_ai_edit_v2_cos -v`**；预期缺少接口失败。
- [ ] **Step 4: 最小扩展 `cos.py`**，保留 `upload/put_bytes/put_file/object_url` 行为不变，并集中 `_validate_rel_key`。
- [ ] **Step 5: 重跑 COS 新测试和 `python -m unittest tests.test_collect_cos_and_refund tests.test_private_assets -v`**；预期全部通过。
- [ ] **Step 6: 提交**

```powershell
git add server/content_domains/cos.py tests/test_ai_edit_v2_cos.py
git commit -m "feat(ai-edit-v2): add private direct-upload primitives"
```

## Task 4: 实现上传、素材归属和 Phase A HTTP 薄接线

**Files:**
- Create: `server/content_domains/ai_edit_v2_api.py`
- Create: `tests/test_ai_edit_v2_api.py`
- Modify: `server/content_domains/core.py`
- Modify: `deploy/nginx-fang-locations.conf`

**Interfaces:** `dispatch(...) -> bool`；Phase A 路由为 capabilities/templates(empty)/materials/uploads/uploads/{id}/complete/quotes/jobs/jobs/{id}/retry。

- [ ] **Step 1: 写 HTTP 分发单元测试**，使用 fake handler 的 `_send/_json_body/_token`，验证未登录 401、他人素材 404、重复 complete 返回同一素材、数量/总容量超限 400。
- [ ] **Step 2: 写 `core.py` 门禁测试**，断言只有 `/api/v2/edit/` 前缀调用 `ai_edit_v2_api.dispatch`，旧 handler 白名单不新增 `ai_edit_v2`。
- [ ] **Step 3: 运行 `python -m unittest tests.test_ai_edit_v2_api tests.test_content_domains -v`**；预期失败。
- [ ] **Step 4: 实现 API**：上传创建只返回 PUT URL 和对象键的 opaque upload id；complete 以 HEAD 结果覆盖客户端声明并写 `edit_v2_materials`。
- [ ] **Step 5: 实现继任任务测试与最小接口**：`POST /jobs/{id}/retry` 仅接受允许重试的终态失败、重新报价并创建新 job id，旧任务和旧计费事务保持终态不复活。
- [ ] **Step 6: 在 `core.H.do_GET/do_POST` 增加不超过 8 行的前缀接线**；Webhook 暂返回能力关闭，不读取用户 token。
- [ ] **Step 7: nginx 增加 `/api/v2/edit/` 到 8096，并为 `/api/v2/edit/webhooks/` 使用不含 `$args` 的专用 access log 格式。
- [ ] **Step 8: 重跑定向测试**；预期全部通过且 core 行数门禁不超限；如门禁超限，删除等量旧注释而非放宽上限。
- [ ] **Step 9: 提交**

```powershell
git add server/content_domains/ai_edit_v2_api.py server/content_domains/core.py deploy/nginx-fang-locations.conf tests/test_ai_edit_v2_api.py tests/test_content_domains.py
git commit -m "feat(ai-edit-v2): expose isolated upload api"
```

## Task 5: 媒体探测、标准化和临时文件清理

**Files:**
- Create: `server/content_domains/ai_edit_v2_media.py`
- Test: `tests/test_ai_edit_v2_media.py`

**Interfaces:** Consumes COS object key; Produces normalized object key and media metadata `{duration_ms,width,height,fps,video_codec,audio_codec,sample_rate,channels}`。

- [ ] **Step 1: 写 runner 注入测试**，断言 FFprobe 使用 JSON 输出并拒绝 0 秒、超过 600 秒、视频无视频流、音频无音轨和声明类型不符。
- [ ] **Step 2: 写标准化命令测试**，视频目标 `MP4/H.264/AAC/30fps/48kHz`，音频目标 `M4A/AAC/48kHz`，命令参数使用列表且不经 shell。
- [ ] **Step 3: 运行 `python -m unittest tests.test_ai_edit_v2_media -v`**；预期导入失败。
- [ ] **Step 4: 实现任务专属 `TemporaryDirectory(prefix="ai-edit-v2-")`、probe、按需 normalize、前后时长差校验和 finally 清理；始终保留原 COS 对象。
- [ ] **Step 5: 增加缺少 ffmpeg、超时、输出为空测试**，均返回稳定错误码而非堆栈。
- [ ] **Step 6: 重跑测试**；预期全部通过。
- [ ] **Step 7: 提交**

```powershell
git add server/content_domains/ai_edit_v2_media.py tests/test_ai_edit_v2_media.py
git commit -m "feat(ai-edit-v2): validate and normalize source media"
```

## Task 6: fun-asr 与平台原文确定性对齐

**Files:**
- Create: `server/content_domains/ai_edit_v2_asr.py`
- Create: `server/content_domains/ai_edit_v2_alignment.py`
- Test: `tests/test_ai_edit_v2_alignment.py`

**Interfaces:** `transcribe(...)` 只输出冻结 ASR 格式；`align_platform_text(...)` 输出 `aligned_words/coverage/monotonic/anchors`。平台资产有原文时字幕字符只能来自原文。

- [ ] **Step 1: 写失败测试**，ASR 将“黄雀引擎二”识别成“黄鹊引擎2”，原文“黄雀引擎2”必须胜出且时间戳仍单调。
- [ ] **Step 2: 写数字/价格测试**，原文 `499元、1000积分` 不得被 ASR 的 `四百九十九、100积分` 覆盖。
- [ ] **Step 3: 写低覆盖测试**，覆盖率 `<0.85` 或时间倒退抛 `AlignmentError("alignment_low_coverage")`。
- [ ] **Step 4: 运行 `python -m unittest tests.test_ai_edit_v2_alignment -v`**；预期失败。
- [ ] **Step 5: 实现字符归一化、动态规划序列对齐、锚点插值和单调约束；外部媒体仅修复标点/断句，不改字符正文。
- [ ] **Step 6: 实现 fun-asr client 的提交/轮询/超时归一化**，测试 fake client 验证已有 provider task id 时只查询不重提。
- [ ] **Step 7: 重跑测试**；预期全部通过。
- [ ] **Step 8: 提交**

```powershell
git add server/content_domains/ai_edit_v2_asr.py server/content_domains/ai_edit_v2_alignment.py tests/test_ai_edit_v2_alignment.py
git commit -m "feat(ai-edit-v2): add deterministic transcript alignment"
```

## Task 7: 动态报价、上限预扣和跨服务幂等

**Files:**
- Create: `server/content_domains/ai_edit_v2_billing.py`
- Create: `tests/test_ai_edit_v2_billing.py`
- Create: `site/admin/ai-edit-v2-pricing.html`
- Modify: `server/auth_server.py`
- Modify: `server/content_domains/points.py`
- Modify: `server/admin_api.py`
- Modify: `site/admin/index.html`
- Modify: `tests/test_auth_points.py`

**Interfaces:** 内部点数 POST body 新增可选 `transaction_key: str`；重复相同 key+操作+用户+金额返回原结果，字段冲突返回 409。旧无 key 请求沿用现状。

- [ ] **Step 1: 写认证服务失败测试**：同一 `transaction_key` 连续预扣只减一次；退款只加一次；同 key 改金额返回 409。
- [ ] **Step 2: 写 V2 失败测试**：报价包含 `min_points/max_points/breakdown/price_version/expires_at`；过期报价、owner 不符和草稿 hash 不符禁止建任务。
- [ ] **Step 3: 运行 `python -m unittest tests.test_auth_points tests.test_ai_edit_v2_billing -v`**；预期失败。
- [ ] **Step 4: 在 `users.db` 新增 `points_transactions(transaction_key PRIMARY KEY, operation, username, amount, points_after, created_at)`，与 users 更新、points_audit 写入同一事务。
- [ ] **Step 5: 扩展 `points.deduct_points/refund_points` 接收可选 transaction_key；旧调用签名保持兼容。
- [ ] **Step 6: 实现版本化价格表读取、上限预扣、成功实际结算退差额、失败全退，V2 DB 账单状态使用 CAS。
- [ ] **Step 7: 实现管理员价格表页面和 API**：草稿校验、分项预览、二次确认发布、版本不可变和审计；任务固定使用已确认版本。
- [ ] **Step 8: 增加崩溃恢复测试**：认证服务已扣但 V2 响应丢失时重放同 key 不二扣；退款响应丢失时重放不二退。
- [ ] **Step 9: 重跑测试**；预期全部通过。
- [ ] **Step 10: 提交**

```powershell
git add server/auth_server.py server/content_domains/points.py server/content_domains/ai_edit_v2_billing.py server/admin_api.py site/admin/ai-edit-v2-pricing.html site/admin/index.html tests/test_auth_points.py tests/test_ai_edit_v2_billing.py
git commit -m "feat(ai-edit-v2): make precharge and settlement idempotent"
```

## Task 8: 独立 Worker、状态机、时间预算和重启恢复

**Files:**
- Create: `server/content_domains/ai_edit_v2_pipeline.py`
- Create: `server/ai_edit_v2_worker.py`
- Create: `tests/test_ai_edit_v2_pipeline.py`
- Create: `deploy/systemd/huangque-ai-edit-v2.service`
- Modify: `deploy/huangque-secrets.env.example`

**Interfaces:** `run_stage(job_id, expected_state) -> StageResult`；Worker 通过租约轮询，不调用旧 `core.enqueue_job`。

- [ ] **Step 1: 写状态迁移测试**，非法跳跃拒绝；排队秒数与处理秒数分离；处理从 normalizing 开始计时。
- [ ] **Step 2: 写 45/60 分钟测试**：45 分钟无成片直接失败全退；已有成片且有明确 QC issue 才能进入 repairing，额外最多 15 分钟。
- [ ] **Step 3: 写恢复测试**：进程在 transcribing 后退出，新 Worker 从 provider task id/checkpoint 查询继续，不重复提交、不重复计费。
- [ ] **Step 4: 运行 `python -m unittest tests.test_ai_edit_v2_pipeline -v`**；预期失败。
- [ ] **Step 5: 实现阶段注册表、CAS 转换、租约心跳、退避轮询、SIGTERM 停止领取并等待当前阶段检查点落库。
- [ ] **Step 6: systemd 使用 `ExecStart=/usr/bin/python3 /home/ubuntu/content-api/ai_edit_v2_worker.py`、独立 EnvironmentFile、Restart、TimeoutStopSec；不修改旧 content service。
- [ ] **Step 7: env example 增加规格中全部变量名，默认 `AI_EDIT_V2_ENABLED=0`、workers=5、normal=2700、repair=900。
- [ ] **Step 8: 重跑测试**；预期全部通过。
- [ ] **Step 9: 提交**

```powershell
git add server/content_domains/ai_edit_v2_pipeline.py server/ai_edit_v2_worker.py deploy/systemd/huangque-ai-edit-v2.service deploy/huangque-secrets.env.example tests/test_ai_edit_v2_pipeline.py
git commit -m "feat(ai-edit-v2): add leased worker and recoverable pipeline"
```

## Task 9: Phase A 页面与端到端假 Provider 验证

**Files:**
- Create: `site/workbench/ai-edit.html`
- Create: `tests/test_ai_edit_v2_ui.js`
- Modify: `site/workbench/cloud-shell.js`
- Modify: `tests/test_cloud_shell_sidebar.js`
- Modify: `tests/test_stamp_assets.py`

- [ ] **Step 1: 写 UI 测试**，断言三入口、两个上传窗口、reference mode、比例、目标时长、报价区间/预扣确认、排队/处理/修复时长和结果区域存在。
- [ ] **Step 2: 写安全测试**，页面不出现供应商模型名、内部错误堆栈、签名查询参数或可编辑时间线。
- [ ] **Step 3: 运行 `node --test tests/test_ai_edit_v2_ui.js tests/test_cloud_shell_sidebar.js`**；预期缺页面/入口失败。
- [ ] **Step 4: 实现页面最小状态流**：draft→quote→confirm→job polling；required/reference 各最多 10，上传进度与失败可重试。
- [ ] **Step 5: `cloud-shell.js` 仅新增导航和 `ai_edit_v2:'ai-edit.html'` 映射，更新 stamp 资产清单。
- [ ] **Step 6: 使用内存 fake Provider 跑 `created -> ... -> directing` 的集成测试**，断言 Phase A 在导演前有合法 aligned transcript、账单 hold 和可恢复 checkpoint。
- [ ] **Step 7: 运行 Phase A 全部测试与仓库门禁**；预期退出码 0。
- [ ] **Step 8: 提交**

```powershell
git add site/workbench/ai-edit.html site/workbench/cloud-shell.js tests/test_ai_edit_v2_ui.js tests/test_cloud_shell_sidebar.js tests/test_stamp_assets.py
git commit -m "feat(ai-edit-v2): add phase-a workbench flow"
```

## Phase A 验收

```powershell
python -m unittest tests.test_ai_edit_v2_schema tests.test_ai_edit_v2_store tests.test_ai_edit_v2_cos tests.test_ai_edit_v2_api tests.test_ai_edit_v2_billing tests.test_ai_edit_v2_media tests.test_ai_edit_v2_alignment tests.test_ai_edit_v2_pipeline -v
node --test tests/test_ai_edit_v2_ui.js tests/test_cloud_shell_sidebar.js
python -m unittest tests.test_content_domains tests.test_jobs_store tests.test_auth_points -v
python scripts/ci_validate.py
python scripts/stamp_assets.py --check
```

预期：全部通过；假 Provider 任务到达 `directing` 检查点；旧 handler 清单未变化；数据库路径测试证明 V2 不打开旧剪辑库；未产生真实第三方调用或费用。
