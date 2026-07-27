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

### Task 5: 从推送提交部署测试环境

**Files:**
- Deploy: `server/auth_server.py` -> `/home/ubuntu/auth-service/auth_server.py`
- Deploy: `server/admin_api.py`, `server/ai_edit_v2_worker.py` -> `/home/ubuntu/content-api/`
- Deploy: `server/content_domains/*.py` changed by the integration -> `/home/ubuntu/content-api/content_domains/`
- Deploy: `site/workbench/*.html`, `site/workbench/cloud-shell.js` changed by the integration -> `/var/www/html/workbench/`
- Deploy: `site/admin/*.html` changed by the integration -> `/var/www/html/admin/`
- Deploy: `deploy/nginx-fang-locations.conf` -> `/etc/nginx/snippets/fang-app-locations.conf`
- Deploy: `deploy/systemd/huangque-ai-edit-v2.service` -> `/etc/systemd/system/huangque-ai-edit-v2.service`
- Deploy: `deploy/systemd/huangque-content.service.d/ai-edit-v2.conf` -> `/etc/systemd/system/huangque-content.service.d/ai-edit-v2.conf`
- Create server-only: `/etc/huangque/ai-edit-v2.env` with mode `0600`

**Interfaces:**
- Consumes: pushed integration commit SHA and existing test secrets/databases。
- Produces: disabled-by-default V2 Worker plus two accessible editor pages。

- [ ] **Step 1: 部署前只读检查和数据库备份**

Run remotely: verify host is `8.134.216.162`, count active `content_jobs.db` tasks, record current service PIDs, copy any existing `ai_edit_v2.db` to a timestamped backup without deleting the original, and back up every runtime/config file that Step 3 will replace into a timestamped server-only directory。

Expected: host matches test server；如果存在运行中生成任务，则等待清零后再重启 `huangque-content`。

- [ ] **Step 2: 创建禁用状态的V2环境文件**

Server-only values must be:

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

Do not print or add secret values. Install as root mode `0600`.

- [ ] **Step 3: 上传到临时目录并原子安装文件**

Upload only files changed between the server's previous deployed Git sources and the pushed integration commit. Use `/tmp/ai-edit-v2-<sha>/` as staging, then `sudo install` each file into its mapped runtime path. Do not switch or clean `/home/admin/ubuntu-fang-server` and do not copy databases from Git。

- [ ] **Step 4: 校验配置并执行迁移**

Run remotely:

```bash
sudo nginx -t
sudo systemd-analyze verify /etc/systemd/system/huangque-ai-edit-v2.service
cd /home/ubuntu/content-api
sudo -u ubuntu env AI_EDIT_V2_DB=/home/ubuntu/content-api/ai_edit_v2.db \
  python3 -c 'from content_domains import ai_edit_v2_store as s; s.init_db()'
```

Expected: Nginx syntax OK；systemd unit无错误；数据库schema version为2。

- [ ] **Step 5: 重启受影响服务**

Run remotely:

```bash
sudo systemctl daemon-reload
sudo systemctl restart huangque-auth
sudo systemctl restart huangque-admin
sudo systemctl restart huangque-content
sudo systemctl enable --now huangque-ai-edit-v2
```

Expected: 四个服务均为 `active`；V2 Worker日志显示 submissions disabled/reconciliation-only，且不claim任务。

- [ ] **Step 6: 完成测试站外部验收**

Run:

```bash
curl -fsS http://127.0.0.1:8096/api/gen/health
curl -I http://8.134.216.162/workbench/ai-edit
curl -I http://8.134.216.162/workbench/ai-edit-v2
```

Authenticated browser/API checks must confirm：旧一键剪辑入口可见；V2 capability返回 `accepts_submissions=false`；会员、邀请、充值和后台页面仍可访问；V2数据库存在但无任务被领取；生产域名未被访问或修改。

- [ ] **Step 7: 失败时按文件清单回滚**

如果 Nginx、systemd、健康检查、会员回归或任一编辑器入口验收失败：停止继续验收；从 Step 1 的逐文件备份恢复本次替换文件和原 V2 数据库，执行 `sudo nginx -t` 后只重启本次已重启的服务，并再次确认旧一键剪辑、会员系统和 `/api/gen/health` 恢复。不得通过切换服务器工作区分支或整站覆盖回滚。

Expected: 回滚后服务均为 `active`，健康检查恢复，且部署失败的 V2 入口不对用户开放。

- [ ] **Step 8: 记录部署结果**

Report exact integration branch、commit SHA、deployed files、restarted services、health responses、capability state、database schema version and remaining Phase B-E gaps. Never include passwords, API keys, tokens, signed URLs or database contents。
