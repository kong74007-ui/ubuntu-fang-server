# OpenClaw Agents 架构（广州服务器）

> 盘点时间：2026-06-21 ｜ 来源：远程实查服务器进程 + `openclaw.json` + `openclaw channels status`
> ⚠️ 本文档**只记录架构与 App ID**。飞书 App Secret、服务器密码、中转站 Token 等机密**一律不入库**（见文末"机密存放位置"）。

## 服务器
- 腾讯云 CVM（广州），公网 `129.204.166.13`，登录用户 `ubuntu@`，主机名 `VM-0-15-ubuntu`，Ubuntu 22.04（4 核 / 3.6G 内存 / 59G 盘）
- OpenClaw 安装路径：`/home/ubuntu/.npm-global/lib/node_modules/openclaw`（版本 `2026.6.6`）
- 多开方式：**多 home 隔离**，每套独立数据目录 + 独立网关端口（用 `OPENCLAW_CONFIG_PATH` / `OPENCLAW_STATE_DIR` / `OPENCLAW_GATEWAY_PORT` 区分）。非 systemd 托管 agent（仅 `openclaw-gateway.service` 这一个 systemd 单元），其余为常驻进程。

## 总览：3 个实例 / 4 个子 agent / 4 个飞书 App（全部在线 running）

| 实例 (home) | 网关端口 | 子 agent (id) | 身份 | 模型 | 飞书 App ID | 飞书状态 |
|---|---|---|---|---|---|---|
| `.openclaw`（主） | 18789 | `main` | 🥶 小冬 | deepseek/deepseek-v4-pro | `cli_aabaa4d43cf81bd0`（default） | ✅ running |
| `.openclaw`（主） | 18789 | `ai` | 东晟AI健康管家 | xiaole/gpt-5.4 | `cli_aabfb56d0b781cd9`（account: dongsheng） | ✅ running |
| `.openclaw-second` | 1890 | `main` | 📝 文案策划 | 默认（deepseek-v4-pro） | `cli_aabe46e2d0b8dbe4`（default） | ✅ running |
| `.openclaw-visual` | 1891 | `main` | 🎨 视觉设计 | 默认 | `cli_aabc1d3f9e789bec`（default） | ✅ running |

- 数据目录占用：`.openclaw` 341M ｜ `.openclaw-second` 54M ｜ `.openclaw-visual` 77M
- `.openclaw-visual` 实例带环境变量 `IMAGE_OUT_DIR=/home/ubuntu/.openclaw-visual/media/outbound`（出图实例）

## 各实例明细

### 1. `.openclaw`（主实例，gateway 18789）
两个子 agent，靠 `bindings` 路由把第二个 agent 接到第二个飞书 App：
- `main` = **小冬** 🥶，默认 deepseek-v4-pro，走 default 飞书 App `cli_aabaa4d43cf81bd0`
- `ai` = **东晟AI健康管家**，模型 `xiaole/gpt-5.4`，独立 workspace `workspace-dongsheng`
- bindings 路由规则：
  ```json
  [{ "type": "route", "agentId": "ai",
     "match": { "channel": "feishu", "accountId": "dongsheng" } }]
  ```
  即：飞书账号 `dongsheng`（App `cli_aabfb56d0b781cd9`）的消息 → 路由给 `ai`（东晟AI健康管家）。

### 2. `.openclaw-second`（gateway 1890）
- `main` = **文案策划** 📝
- 飞书 default App `cli_aabe46e2d0b8dbe4`，无 bindings（单 agent 单 App 直连）
- ⚠️ 残留：目录里有个 `agents/visual` + `workspace-visual`，是"视觉设计" agent 拆分搬迁到 `.openclaw-visual` 后的残留；配置 `agents.list` 只激活 `main`，该残留可清理。

### 3. `.openclaw-visual`（gateway 1891）
- `main` = **视觉设计** 🎨（出图实例，`IMAGE_OUT_DIR` 指向本 home 的 media/outbound）
- 飞书 default App `cli_aabc1d3f9e789bec`，无 bindings

## 飞书绑定核对结论
- **是否都有飞书在线进程**：✅ 4 个飞书渠道全部 `openclaw channels status` 报 `running`
- **是否都绑定 App ID**：✅ 4 个 agent 各自绑定独立 App ID，无遗漏
  - 小冬 → `cli_aabaa4d43cf81bd0`
  - 东晟AI健康管家 → `cli_aabfb56d0b781cd9`
  - 文案策划 → `cli_aabe46e2d0b8dbe4`
  - 视觉设计 → `cli_aabc1d3f9e789bec`

## 已知小问题
1. `.openclaw-second` 残留 `visual` agent 目录 + `workspace-visual`（可清理）。
2. `--deep` 探测三套都提示 `plugins.entries.feishu: plugin not installed`，但渠道实际 `running`（飞书走内置/全局方式跑，不影响使用）；若想 `doctor` 干净通过，可补登记该插件。

## 机密存放位置（不入库）
以下机密**只在服务器本地**，不写入本仓库：
- 飞书 App Secret：各 home 的 `~/.openclaw*/openclaw.json` → `channels.feishu.appSecret` / `channels.feishu.accounts.*.appSecret`
- 服务器登录密码：本机 `~/.ssh/.xiaodong-server-pass`
- 中转站（生图/模型网关）Token：本机 `~/.claude/settings1.json`（base_url `https://api.zelong.vip`）
