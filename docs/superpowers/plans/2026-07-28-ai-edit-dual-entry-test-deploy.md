# AI 剪辑双入口测试集成与部署 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将最新 `main`、会员系统和现有一键剪辑合并成可追溯的测试分支，在测试站同时提供旧“一键剪辑”和独立的“AI 智能剪辑 V2”。

**Architecture:** 旧模块保留 `site/workbench/ai-edit.html` 与 `/api/gen/ai-edit/*`；V2 页面迁移到 `site/workbench/ai-edit-v2.html`，后端继续使用 `/api/v2/edit/*` 与 `ai_edit_v2.db`。共享认证、点数、导航和服务文件通过 Git 冲突解决合并，不在服务器热改；部署只来自推送后的测试集成分支。

**Tech Stack:** Git/GitHub、Python 3、SQLite、Node.js `node:test`、静态 HTML/JavaScript、Nginx、systemd、腾讯云 COS。

## Global Constraints

- 旧“一键剪辑”路径固定为 `/workbench/ai-edit`，页面固定为 `site/workbench/ai-edit.html`。
- 新“AI 智能剪辑 V2”路径固定为 `/workbench/ai-edit-v2`，页面固定为 `site/workbench/ai-edit-v2.html`。
- V2 后端固定使用 `/api/v2/edit/*` 和 `ai_edit_v2.db`，不得改写旧一键剪辑任务或项目数据。
- 测试集成分支必须以 `main@55a661d` 或更新提交为基线，并保留 `origin/codex/membership-system` 与 `origin/codex/one-click-ai-edit` 的现有功能。
- 所有冲突解决、测试和提交先进入 Git；禁止直接修改服务器运行目录。
- 首次部署固定 `AI_EDIT_V2_ENABLED=0`，真实处理器完成前不得开放 V2 提交。
- 只部署测试服务器 `8.134.216.162`，禁止连接或修改生产服务器。

---

## File Structure and Responsibilities

- `site/workbench/ai-edit.html`：旧一键剪辑页面，只调用旧 `/api/gen/ai-edit/*`。
- `site/workbench/ai-edit-v2.html`：V2 Phase A 页面，只调用 `/api/v2/edit/*`。
- `site/workbench/cloud-shell.js`：同时注册 `ai-edit` 与 `ai_edit_v2`，只对 V2 应用 capability gate。
- `tests/test_ai_edit_v2_ui.js`：V2 页面和服务端 capability 门禁契约。
- `tests/test_ai_edit_dual_entry.js`：两个入口、两个页面和API命名空间隔离契约。
- `server/auth_server.py`、`server/content_domains/points.py`：合并会员逻辑与 V2 幂等点数事务。
- `server/content_domains/core.py`：同时接线旧一键剪辑 handler 与 V2 `/api/v2/edit/*` dispatcher。
- `server/admin_api.py`、`site/admin/index.html`：同时保留会员管理和 V2 定价管理。
- `server/content_domains/ai_edit*.py`：旧一键剪辑领域模块。
- `server/content_domains/ai_edit_v2_*.py`、`server/ai_edit_v2_worker.py`：V2 独立领域模块和 Worker。
- `deploy/nginx-fang-locations.conf`：同时代理旧 `/api/gen/*` 与新 `/api/v2/edit/*`。
- `deploy/systemd/huangque-ai-edit-v2.service`、`deploy/systemd/huangque-content.service.d/ai-edit-v2.conf`：V2 Worker和内容服务环境接线。

### Task 1: 先固定 V2 独立页面路径

**Files:**
- Create: `site/workbench/ai-edit-v2.html`
- Modify: `site/workbench/cloud-shell.js`
- Modify: `tests/test_ai_edit_v2_ui.js`
- Create: `tests/test_ai_edit_dual_entry.js`

**Interfaces:**
- Consumes: `GET /api/v2/edit/capabilities -> { accepts_submissions: boolean }`。
- Produces: 导航键 `ai_edit_v2 -> ai-edit-v2.html`；旧导航键 `ai-edit -> ai-edit.html` 在后续合并中保留。

- [ ] **Step 1: 写会失败的双入口静态契约测试**

```js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const root = path.resolve(__dirname, '..');

test('legacy and V2 editors use separate pages and APIs', () => {
  const legacy = fs.readFileSync(path.join(root, 'site/workbench/ai-edit.html'), 'utf8');
  const v2 = fs.readFileSync(path.join(root, 'site/workbench/ai-edit-v2.html'), 'utf8');
  assert.match(legacy, /data-active="ai-edit"/);
  assert.match(v2, /data-active="ai_edit_v2"/);
  assert.doesNotMatch(legacy, /\/api\/v2\/edit\//);
  assert.doesNotMatch(v2, /\/api\/gen\/ai-edit\//);
});
```

- [ ] **Step 2: 运行测试确认路径尚未拆分**

Run: `node --test tests/test_ai_edit_dual_entry.js`

Expected: FAIL，提示 `site/workbench/ai-edit-v2.html` 不存在或旧页面仍是V2页面。

- [ ] **Step 3: 复制当前 main 的 V2 页面为独立页面并更新V2测试**

将当前 `site/workbench/ai-edit.html` 原样复制为 `site/workbench/ai-edit-v2.html`；把 `tests/test_ai_edit_v2_ui.js` 的 `pagePath` 改为 `ai-edit-v2.html`，并把导航断言改为：

```js
assert.match(shell, /NAV_PAGES=\{ai_edit_v2:'ai-edit-v2\.html'\}/);
```

- [ ] **Step 4: 更新共享导航的 V2 路由映射**

```js
var NAV_PAGES={ai_edit_v2:'ai-edit-v2.html'};
```

同时将通知映射中的 `ai_edit_v2` 改为 `ai-edit-v2.html`；V2条目继续保留 `gated:true`。

- [ ] **Step 5: 运行 V2 页面测试**

Run: `node --test tests/test_ai_edit_v2_ui.js`

Expected: 4 tests PASS。

- [ ] **Step 6: 提交独立V2页面**

```bash
git add site/workbench/ai-edit-v2.html site/workbench/cloud-shell.js tests/test_ai_edit_v2_ui.js tests/test_ai_edit_dual_entry.js
git commit -m "feat(ai-edit-v2): isolate test workbench entry"
```

### Task 2: 合并会员系统并保留V2基础设施

**Files:**
- Merge: `origin/codex/membership-system`
- Modify on conflict: `server/auth_server.py`
- Modify on conflict: `server/content_domains/core.py`
- Modify on conflict: `server/content_domains/feature_flags.py`
- Modify on conflict: `server/content_domains/points.py`
- Modify on conflict: `server/func_names.py`
- Modify on conflict: `server/admin_api.py`
- Modify on conflict: `site/admin/index.html`
- Modify on conflict: `site/workbench/cloud-shell.js`
- Modify on conflict: `tests/test_auth_points.py`
- Modify on conflict: `tests/test_content_domains.py`

**Interfaces:**
- Consumes: 会员等级、邀请奖励、后台充值接口和 `transaction_key` 点数幂等接口。
- Produces: 同一认证服务同时支持会员系统与 `/api/auth/points/{deduct,refund}` 幂等事务；后台同时提供会员管理和 V2 定价入口。

- [ ] **Step 1: 开始不自动提交的会员分支合并**

Run: `git merge --no-ff --no-commit origin/codex/membership-system`

Expected: Git报告共享文件冲突，工作区进入 MERGING 状态。

- [ ] **Step 2: 按职责解决后端冲突**

- `auth_server.py`：保留会员注册、期限、充值、邀请限制和奖励台账；同时保留 V2 点数端点的内部令牌校验、`transaction_key` 重放和409冲突行为。
- `points.py`：保留旧调用兼容签名，并让 `deduct_points`、`refund_points` 继续接受可选 `transaction_key`。
- `core.py`：保留会员访问门禁，同时保留 V2 dispatcher；旧 `HANDLERS` 不添加V2，V2仍在路由前缀层独立分发。
- `feature_flags.py`、`func_names.py`：合并双方键集合，任何已存在会员键和V2键都不得删除。
- `admin_api.py`、`site/admin/index.html`：会员管理入口和V2定价入口同时存在。

- [ ] **Step 3: 解决工作台缓存戳冲突**

除 `cloud-shell.js` 外，优先保留会员分支页面业务内容；`cloud-shell.js` 同时保留邀请中心、旧工作台入口和V2 gated入口。随后运行 `python scripts/stamp_assets.py` 统一生成当前资源哈希，不手写旧版本号。

- [ ] **Step 4: 运行会员与V2后端测试**

Run: `python -m unittest tests.test_auth_points tests.test_ai_edit_v2_billing tests.test_ai_edit_v2_api tests.test_content_domains -v`

Expected: 所有测试PASS；点数重放不重复扣款，会员与邀请测试无回退。

- [ ] **Step 5: 完成会员合并提交**

```bash
git add -A
git commit -m "merge: preserve membership in AI edit V2 test branch"
```

### Task 3: 合并现有一键剪辑并完成双入口导航

**Files:**
- Merge: `origin/codex/one-click-ai-edit`
- Modify on conflict: `site/workbench/ai-edit.html`
- Modify on conflict: `site/workbench/cloud-shell.js`
- Modify on conflict: `server/content_domains/core.py`
- Preserve/Create: `server/content_domains/ai_edit.py`
- Preserve/Create: `server/content_domains/ai_edit_api.py`
- Preserve/Create: `server/content_domains/ai_edit_store.py`
- Modify: `tests/test_ai_edit_dual_entry.js`
- Modify on conflict: `tests/test_content_domains.py`

**Interfaces:**
- Consumes: 旧一键剪辑 `/api/gen/ai-edit/*` 和V2 `/api/v2/edit/*`。
- Produces: 两个页面、两个导航键、两个API命名空间互不覆盖。

- [ ] **Step 1: 开始不自动提交的一键剪辑合并**

Run: `git merge --no-ff --no-commit origin/codex/one-click-ai-edit`

Expected: `core.py`、`cloud-shell.js`、旧页面和缓存戳页面出现冲突。

- [ ] **Step 2: 保留旧页面并组合共享路由**

- `site/workbench/ai-edit.html` 使用一键剪辑分支版本，保持 `data-active="ai-edit"`。
- `site/workbench/ai-edit-v2.html` 保持Task 1版本，使用 `data-active="ai_edit_v2"`。
- `core.py` 同时保留旧 `ai_edit` handler注册和V2前缀dispatcher。
- `cloud-shell.js` 导航同时包含：

```js
{k:'ai-edit',l:'一键剪辑',i:'edit'},
{k:'ai_edit_v2',l:'AI智能剪辑 V2',i:'edit',gated:true}
```

并设置 `NAV_PAGES={ai_edit_v2:'ai-edit-v2.html'}`；旧 `ai-edit` 使用默认同名页面解析。

- [ ] **Step 3: 完成双入口测试断言**

```js
test('navigation keeps legacy visible and gates only V2', () => {
  const shell = fs.readFileSync(path.join(root, 'site/workbench/cloud-shell.js'), 'utf8');
  assert.match(shell, /\{k:'ai-edit',l:'一键剪辑',i:'edit'\}/);
  assert.match(shell, /\{k:'ai_edit_v2',l:'AI智能剪辑 V2',i:'edit',gated:true\}/);
  assert.match(shell, /NAV_PAGES=\{ai_edit_v2:'ai-edit-v2\.html'\}/);
});
```

- [ ] **Step 4: 统一缓存戳并运行旧、新模块测试**

Run: `python scripts/stamp_assets.py`

Run: `node --test tests/test_ai_edit_dual_entry.js tests/test_ai_edit_v2_ui.js tests/test_cloud_shell_sidebar.js`

Run: `python -m unittest tests.test_content_domains tests.test_ai_edit_v2_api tests.test_ai_edit_v2_store -v`

Expected: Node和Python全部PASS；两个入口各出现一次。

- [ ] **Step 5: 完成一键剪辑合并提交**

```bash
git add -A
git commit -m "merge: keep legacy and V2 AI editors isolated"
```

### Task 4: 全量验证并推送测试集成分支

**Files:**
- Verify: entire repository
- Modify only if generated: cache stamp references changed by `scripts/stamp_assets.py`

**Interfaces:**
- Consumes: Tasks 1-3 integrated tree。
- Produces: 可从Git复现的测试部署提交。

- [ ] **Step 1: 扫描未解决冲突**

Run: `git diff --check`

Run: `rg -n "^(<<<<<<<|=======|>>>>>>>)" --glob '!docs/superpowers/plans/**'`

Expected: `git diff --check` 无输出且退出码为0；`rg` 无匹配并以“未找到”状态退出。密钥与敏感文件检查由后续 `scripts/ci_validate.py` 执行。

- [ ] **Step 2: 运行完整Python测试**

Run: `python -m unittest discover -s tests -q`

Expected: `OK`，0 failures，0 errors。

- [ ] **Step 3: 运行前端与静态门禁**

Run: `node --test tests/test_ai_edit_dual_entry.js tests/test_ai_edit_v2_ui.js tests/test_cloud_shell_sidebar.js`

Run: `python scripts/ci_validate.py && python scripts/stamp_assets.py --check`

Expected: Node 0 fail；静态检查通过；缓存戳9/9通过。

- [ ] **Step 4: 提交验证产生的缓存戳变化**

```bash
git add site/workbench site/admin tests
git commit -m "test: verify dual AI edit integration"
```

如果没有文件变化，则跳过该提交，不创建空提交。

- [ ] **Step 5: 推送测试集成分支**

Run: `git push -u origin codex/test-ai-edit-v2-integration`

Expected: 远端分支指向本地最终提交；部署只能使用该提交SHA。

### Task 5: 从推送提交部署测试环境（本轮只完成可审计计划，不执行）

本轮修复禁止连接任何服务器、禁止部署和重启。后续只有在用户明确授权部署后，才可执行本节；目标必须硬锁为测试机 `8.134.216.162`，禁止连接生产服务器、生产域名或生产 IP。首次部署始终保持 `AI_EDIT_V2_ENABLED=0`。

**Runtime dependency closure（源文件必须按下列目标保留目录层级）：**

- Auth 依赖先于入口安装：`server/invites.py` -> `/home/ubuntu/auth-service/invites.py`；`server/wechat_virtual_pay.py` -> `/home/ubuntu/auth-service/wechat_virtual_pay.py`；`server/wxpay.py` -> `/home/ubuntu/auth-service/wxpay.py`；最后才是 `server/auth_server.py` -> `/home/ubuntu/auth-service/auth_server.py`。
- Auth 配置：`deploy/systemd/huangque-auth.service.d/invite-test.conf` -> `/etc/systemd/system/huangque-auth.service.d/invite-test.conf`。
- Content 入口：`server/func_names.py` -> `/home/ubuntu/content-api/func_names.py`；`server/admin_api.py`、`server/imggen_api.py`、`server/leadgen_api.py` 安装到 `/home/ubuntu/content-api/` 对应同名文件；`server/ai_edit_v2_worker.py` -> `/home/ubuntu/content-api/ai_edit_v2_worker.py`。
- Content domains：把最终 manifest 中变化的 `server/content_domains/*.py` 安装到 `/home/ubuntu/content-api/content_domains/`；至少重新评估 `ai_edit.py`、`ai_edit_api.py`、`ai_edit_store.py`、`ai_edit_v2_api.py`、`core.py`、`feature_flags.py`、`image.py`、`jobs_store.py`、`miniprogram_security.py`、`points.py`、`registry.py`、`startup_recovery.py`、`submission_idempotency.py`、`video.py`、`video_openai.py`、`video_xai.py`。
- V2 Worker 完整依赖：若目标机尚无同 SHA 文件，manifest 必须包含 `ai_edit_v2_alignment.py`、`ai_edit_v2_asr.py`、`ai_edit_v2_billing.py`、`ai_edit_v2_cos.py`、`ai_edit_v2_feature.py`、`ai_edit_v2_media.py`、`ai_edit_v2_pipeline.py`、`ai_edit_v2_runtime.py`、`ai_edit_v2_schema.py`、`ai_edit_v2_store.py`；不能只上传 Worker 入口。
- 嵌套 vendor 文件保持嵌套路径：`server/content_domains/vendor/gsap.min.js` -> `/home/ubuntu/content-api/content_domains/vendor/gsap.min.js`。
- Web：manifest 覆盖变化的 `site/workbench/` 页面与 `cloud-shell.js`、`site/register.html`、`site/admin/index.html`、`site/api-admin/index.html`；发布规范必须包含 `site/api-docs/openapi.json` -> `/var/www/huangquechuanmei/api-docs/openapi.json`。字体与云支付图片依赖包含 `site/assets/fonts/` 下实际使用文件及 `site/assets/cloud/virtual-pay-item-200.png`。`docs/api/openapi.json` 是 repo-only 文档，不安装到 Web 根目录，但仍须与发布规范通过同一契约测试。
- Nginx：`deploy/nginx-fang-locations.conf` -> `/etc/nginx/snippets/fang-app-locations.conf`。`/api/invite/` 必须在这个被 `deploy/nginx-fang.conf` 实际 include 的 snippet 内，不能只上传未被 include 的 `nginx-fang-invite-location.conf`。
- systemd：安装 `huangque-ai-edit-v2.service`、`huangque-content.service.d/ai-edit-v2.conf`、受影响服务的 `hardening.conf`；server-only `/etc/huangque/ai-edit-v2.env` 必须 `root:root 0600`。
- `deploy/huangque-secrets.env.example`、`deploy/setup-dev-server.sh` 和 `server/sync_virtual_pay_goods.py` 是 repo-only/运维入口，不作为运行时 import 依赖部署；`setup-dev-server.sh` 自身必须保持 Auth import closure 与 invite route 完整。

**Interfaces:**

- Consumes: 已 push 的 `FINAL_SHA`、测试机当前 `DEPLOYED_SOURCE_SHA`、目标文件哈希和既有测试机数据库。
- Produces: 一份逐文件 restore/install manifest、SQLite 一致性备份，以及默认禁用提交的 V2 Worker 与双编辑器入口。

- [ ] **Step 1: 固定最终提交并生成“当前部署 -> FINAL_SHA”逐文件 manifest**

本地先确认 `FINAL_SHA=$(git rev-parse HEAD)` 已 push 且远端分支精确指向该 SHA。部署会话第一条硬门禁确认 SSH 目标字面值和远端主机都是 `8.134.216.162`；任何不匹配立即退出。

在测试机只读获取上次部署记录中的 `DEPLOYED_SOURCE_SHA`（例如 server-only deployment record），并为 runtime closure 每个目标记录 `PRESENT` 或 `MISSING`、文件大小和 `target_sha256`。如果找不到可信部署 SHA，记录 `DEPLOYED_SOURCE_SHA=UNKNOWN`，不能猜测或用服务器工作区当前分支替代。

本地生成：

```bash
if [ "$DEPLOYED_SOURCE_SHA" != "UNKNOWN" ]; then
  git diff --name-only "$DEPLOYED_SOURCE_SHA" "$FINAL_SHA"
else
  echo "DEPLOYED_SOURCE_SHA=UNKNOWN: skip git diff and seed the full runtime closure"
fi
```

可信 SHA 分支将 Git diff 与上述 runtime dependency closure 取并集。UNKNOWN 分支禁止执行 git diff；必须直接把上面列出的完整 runtime dependency closure 全量写入候选 manifest（包括运行时 Web OpenAPI），逐一读取目标的 `PRESENT/MISSING` 与哈希，不能因缺少 diff 而省略文件。manifest 每行必须有 `source_path`、`target_path`、`service`、`source_sha256`、`target_sha256/PRESENT/MISSING` 和 action。无论使用哪个分支，安装/跳过决策都只按 source_sha256 与 target_sha256：只有二者完全相等才写 `SKIP_HASH_EQUAL`；SHA 未知、目标缺失或内容不同一律不能跳过。记录为何纳入依赖闭包，不得用“Git 未变化”替代目标机哈希验证。

- [ ] **Step 2: 创建文件备份和一致性 SQLite 备份**

在 root-only `0700` 的 `$RUN` 目录内先保存 manifest 中每个目标的原文件或 `MISSING` 标记、服务 PID/状态、Nginx 生效链接、V2 flag 和数据库基线。确认 `content_jobs.db` 的 active jobs = 0 后才允许计划重启 content；未清零就等待，不杀任务。

`users.db` 是 WAL 数据库，禁止在线 `cp`。必须使用 SQLite backup API：

```bash
sudo sqlite3 /home/ubuntu/auth-service/users.db ".backup '$RUN/sqlite/users.db'"
sudo sqlite3 /home/ubuntu/content-api/content_jobs.db ".backup '$RUN/sqlite/content_jobs.db'"
sudo sqlite3 /home/ubuntu/content-api/admin_config.db ".backup '$RUN/sqlite/admin_config.db'"
sudo sqlite3 /home/ubuntu/content-api/feature_flags.db ".backup '$RUN/sqlite/feature_flags.db'"
# ai_edit_v2.db 存在时同样用 .backup，不复制活动中的 DB/WAL。
```

这些 `.backup` 命令必须以 root 身份执行，才能遍历并写入 root-only `0700` 的 `$RUN`；备份完成后保持 `$RUN` 与其中数据库仅 root 可读，不为方便而放宽目录权限。

对原库和每份备份执行 `PRAGMA quick_check` 与 `PRAGMA integrity_check`，记录 `user_version`、schema hash、文件 SHA/size 和 non-PII row counts。`users.db` 至少记录 users、tokens、recharge_orders、points_audit、points_transactions、membership_*、user_invites、invite_* 的行数；表不存在记 `MISSING`，不得导出用户名、token、订单号或其他用户数据。Auth 重启后重复同一组完整性/schema/行数检查，任何意外减少立即停止。

- [ ] **Step 3: 创建保持关闭的 V2 环境文件**

Server-only values：

```dotenv
AI_EDIT_V2_ENABLED=0
AI_EDIT_V2_DB=/home/ubuntu/content-api/ai_edit_v2.db
AI_EDIT_V2_PRICING_DB=/home/ubuntu/content-api/admin_config.db
AI_EDIT_V2_WORKERS=5
AI_EDIT_V2_LEASE_SECONDS=180
AI_EDIT_V2_POLL_SECONDS=1
AI_EDIT_V2_NORMAL_TIMEOUT_SECONDS=2700
AI_EDIT_V2_REPAIR_TIMEOUT_SECONDS=900
```

先在 restore manifest 中把 `/etc/huangque/ai-edit-v2.env` 标为 `PRESENT` 或 `MISSING`。若文件 PRESENT（manifest 状态），必须由 root 读取到不回显的临时文件，保留所有未列出的键和原值，只逐键更新上述非敏感值；尤其不得删除或覆盖 `AI_EDIT_V2_COS_SECRET_ID`、`AI_EDIT_V2_COS_SECRET_KEY`、`AI_EDIT_V2_WEBHOOK_SECRET`、provider 配置、`AI_EDIT_V2_REMOTION_BASE`、`AI_EDIT_V2_REMOTION_TOKEN` 等 server-only 值。禁止用只含本节几行的 heredoc/重定向整文件覆盖。若无法证明保留式 patch，立即停止，不得安装。

若文件 `MISSING`，只能从已批准的 server-only secret manifest 构造完整文件；不得用 repo 示例中的占位值启动服务。无论 `PRESENT` 还是 `MISSING`，都不得打印、提交或写入部署报告中的密钥。先在同目录写 root-only 临时文件并原子 `install`/rename，重新断言 owner/mode 为 `root:root 0600`，随后只读取非敏感 flag 并断言 `AI_EDIT_V2_ENABLED=0`。

- [ ] **Step 4: 上传 staging 并按依赖顺序原子安装**

从 `FINAL_SHA` checkout/artifact 构造 `/tmp/ai-edit-v2-<sha>/`，校验 staging hash 等于 manifest 的 `source_sha256`。禁止整站 rsync、禁止 `rsync --delete`、禁止切换或清理服务器工作区，也不得从 Git 上传任何数据库。

安装顺序必须是：Auth/Content import 依赖 -> Python 入口 -> nested vendor -> assets before HTML -> Nginx/systemd/env config before restart。每个文件用 `sudo install` 到 manifest 的精确目标，保留正确 owner/mode；不允许把 `vendor/gsap.min.js` 扁平化到 domains 根目录。

- [ ] **Step 5: 重启前执行配置、import 和迁移门禁**

先确认 `/etc/nginx/sites-enabled/` 中 fang 生效链接确实 include `/etc/nginx/snippets/fang-app-locations.conf`，并验证 `/api/invite/`、`/api/auth/`、`/api/gen/`、`/api/v2/edit/` 都只有预期 upstream。执行：

```bash
sudo nginx -t
sudo systemd-analyze verify /etc/systemd/system/huangque-ai-edit-v2.service
sudo systemd-analyze verify /etc/systemd/system/huangque-auth.service
sudo systemd-analyze verify /etc/systemd/system/huangque-content.service
```

完成 service user import smoke：以各 unit 的实际 User、WorkingDirectory、EnvironmentFile 和 `PYTHONDONTWRITEBYTECODE=1` 导入 `auth_server`、`invites`、`wechat_virtual_pay`、`wxpay`、`content_domains.core`、`content_domains.ai_edit_api` 与 `ai_edit_v2_worker`。任何 ImportError 都必须在重启前停止。

只有 Step 2 的数据库备份和完整性记录成功后，才以 `ubuntu` 用户运行幂等 schema 初始化；V2 初始化期间显式传入 `AI_EDIT_V2_ENABLED=0`。再次确认没有生产域名连接。

- [ ] **Step 6: 只重启 manifest 标记受影响的服务**

执行 `systemctl daemon-reload` 后，按 Auth -> Admin -> Content -> V2 Worker 顺序重启/启动实际受影响服务。每次重启后等待 `active`、检查 journal 和本地 health，再进行下一项。若 content active jobs = 0 门禁不再成立，停止而不是重启。

V2 Worker 可安装并运行 reconciliation-only，但 capability 必须显示 `accepts_submissions=false`，日志不得出现 claim 新任务。未变化且被标记 `SKIP_HASH_EQUAL` 的服务不得为了方便而重启。

- [ ] **Step 7: 完成测试站外部验收和数据库 post-check**

只访问测试机 `8.134.216.162`：验证 `/api/gen/health`、`/workbench/ai-edit`、`/workbench/ai-edit-v2`、登录、会员、邀请、充值预览和后台页面；验证旧/新路由隔离、批量请求幂等 header、V2 capability 关闭。不得发起真实扣点生成或真实支付。

重复 Step 2 的 SQLite integrity/schema/non-PII counts，记录允许的新增行并证明无意外减少；确认 `users.db`、content DB 与 V2 DB 无异常 WAL/锁，四个服务 PID/状态和 Nginx config 均符合 manifest。

- [ ] **Step 8: 失败时按 manifest 回滚，数据库恢复须单独批准**

文件/config 回滚可以按 restore manifest 原子执行，随后 `nginx -t`、`systemd-analyze verify`，且只重启本次已重启服务。禁止通过服务器工作区分支、整站覆盖或旧目录 rsync 回滚。

禁止盲目自动覆盖 users.db。先冻结 Auth 写入并停止 `huangque-auth`，再把失败部署后的当前库备份为 `.backup '$RUN/sqlite/post-deploy-current-users.db'`，比较新增注册、token、订单、点数、会员和邀请写入。只要存在 post-deploy 写入，就必须获得 explicit operator approval 并完成对账；默认优先回滚代码、保留向后兼容的 additive schema 和新数据。

只有确认可丢弃或已经合并新写入后，才在服务停止状态原子恢复 `users.db`，修复 owner/mode，处理旧 `-wal`、`-shm`，再执行完整性与行数检查。`content_jobs.db`、`admin_config.db`、`feature_flags.db`、`ai_edit_v2.db` 同样先保存 post-deploy 当前库并判断是否有新写入；即使 V2 disabled，reconciliation 仍可能写库，不能默认覆盖。

- [ ] **Step 9: 记录部署或回滚结果**

报告 branch、`FINAL_SHA`、`DEPLOYED_SOURCE_SHA`、manifest、每个 INSTALL/SKIP 原因、备份目录、数据库检查、部署文件、重启服务、health/capability、回滚决策和 Phase B-E 未完成项。不得记录密码、API key、token、signed URL 或数据库内容。
