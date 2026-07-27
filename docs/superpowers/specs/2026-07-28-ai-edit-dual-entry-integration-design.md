# 一键剪辑与 AI 智能剪辑 V2 双入口集成设计

## 目标

在测试环境同时保留现有“一键剪辑”和 PR #21 引入的“AI 智能剪辑 V2”，避免两个模块争用同一个页面路径，并保持旧模块行为不变。

## 路由与命名

- 现有“一键剪辑”继续使用 `/workbench/ai-edit`，保留 `site/workbench/ai-edit.html` 作为唯一页面正本。
- 新模块显示名称为“AI 智能剪辑 V2”，使用 `/workbench/ai-edit-v2`，页面正本为 `site/workbench/ai-edit-v2.html`。
- V2 后端继续使用独立 `/api/v2/edit/*`，数据库继续使用 `ai_edit_v2.db`，不得调用或修改旧一键剪辑的 `/api/gen/ai-edit/*`、旧任务库或旧项目数据。

## 分支集成顺序

测试集成分支以已合并 PR #21 的最新 `main` 为基线，依次合并：

1. `origin/codex/membership-system`，保留当前测试环境会员和邀请功能。
2. `origin/codex/one-click-ai-edit`，保留现有一键剪辑实现。
3. 解决冲突时，旧 `ai-edit.html` 采用一键剪辑版本；PR #21 的 Phase A 页面迁移为 `ai-edit-v2.html`。

所有冲突解决必须提交并推送到测试集成分支后才能部署，不允许在服务器运行目录热改。

## 导航与功能门禁

- 导航中“一键剪辑”始终指向 `/workbench/ai-edit`，行为与当前测试环境一致。
- “AI 智能剪辑 V2”指向 `/workbench/ai-edit-v2`。
- V2 入口继续受服务端 `/api/v2/edit/capabilities` 控制；`accepts_submissions=false` 时隐藏 V2 入口并禁止提交。
- 旧一键剪辑入口不受 V2 capability 影响。
- 两个页面不得使用相同的导航键、路由判断或标题，防止激活状态和缓存戳互相覆盖。

## 部署范围

- 从推送后的测试集成分支部署，不直接从服务器检出目录修改文件。
- 只部署集成后发生变化的后端、工作台、后台、Nginx和systemd文件。
- `AI_EDIT_V2_ENABLED=0` 为首次部署默认值；真实 Provider 尚未完成前，不开放 V2 提交。
- 会员系统与旧一键剪辑的数据文件、配置和运行目录原样保留。
- 不部署生产环境。

## 验证

自动化验证必须覆盖：

- 两个页面文件同时存在，并且路由不同。
- 导航中两个模块只各注册一次，标签和链接正确。
- V2 capability 关闭时只隐藏 V2，不隐藏旧一键剪辑。
- 旧一键剪辑前后端回归测试通过。
- V2 Phase A、会员系统、认证点数及工作台侧栏测试通过。
- 静态检查、缓存戳和 Git diff 检查通过。

测试环境外部验收必须确认：

- `/workbench/ai-edit` 仍能打开旧一键剪辑。
- `/workbench/ai-edit-v2` 页面可访问，但 capability 关闭时不能提交任务。
- 会员、邀请、充值和旧工作台入口没有回退。
- `/api/gen/health`、认证服务、后台服务和 V2 Worker 均正常。
- 未触碰生产服务器。

## 回滚

出现回归时，先保持 `AI_EDIT_V2_ENABLED=0`，停止并禁用 V2 Worker，再从测试集成分支上一部署提交恢复本次文件。不得回滚或覆盖会员系统、旧一键剪辑数据库及用户数据。
