# 黄雀团队 · Git 协作规矩（一页版）

> 给两个人（+各自的 AI：Claude Code / Codex）看的协作约定。**大白话，照着做就不打架。**
> 单一事实源在 GitHub `design-sync` 分支。线上：https://huangquechuanmei.com ｜ 服务器：`dapeng-server`（129.204.166.13）

---

## 一条铁律（最重要，先记这条）

**🚫 不要直接在服务器上改代码。**

为什么：服务器上直接改 = 没备份、没记录、两个人一改就互相覆盖、改坏了没法回滚。我们已经吃过好几次这个亏。

✅ 正确姿势：**所有改动先走 git（本地改 → push 到 GitHub），再从 git 部署到服务器。** GitHub 是唯一的"正本"，服务器只是"跑正本的地方"。

> **⚠️ 第二条铁律：动手前一定先 `git pull`（AI 也不例外）。**
> 真实教训（2026-06-28）：Claude 没先 pull 就改 + 抢先部署，差点把强哥刚做的音频模块覆盖掉，靠 rebase + stash 才救回。**每次修改/部署前先 pull**，见下面"干活四步"第 1 步。

---

## 干活四步（每次都这样）

```bash
# 1. 干活前：先拉最新（治"覆盖"——别人 push 过的先同步下来）
git pull

# 2. 改完：提交，说人话
git commit -m "fix: 修了配音点数不退的问题"

# 3. 推上去（这一步做完，GitHub 才有你的改动）
git push

# 4. 部署：只推自己的文件 + 重启自己的服务（见下表）
```

> **「部署前必 pull」**：动手前 `git pull` 一下，把对方的改动先合进来，就不会用你的旧版本盖掉人家的新版本。

---

## 各管各的文件（改不同文件 = 不会冲突）

后端已经按"一个能力 = 一个文件 + 一个端口 + 一个服务"拆开了。**只动自己那栏的文件，谁都伤不到谁。**

| 能力 | 文件 | 端口 | 服务名 | 谁的 |
|---|---|---|---|---|
| 采集 / 获客 / 关键词搜 | `server/leadgen_api.py` | 8100 | `huangque-leadgen-api` | Tang |
| 视频下载 | `server/dl_service.py` | 8097 | `huangque-dl` | Tang |
| 作图 nano banana | `server/imggen_api.py` | 8101 | `huangque-imggen-api` | Tang |
| **作图 gpt-image / 文案 / 配音 / 豆包** | **`server/content_api.py`** | **8096** | **`huangque-content`** | **同事** |
| 登录 / 点数 | `auth_server.py` | 8095 | — | 共用，别乱动 |

> **同事日常只改 `content_api.py`。** Tang 改另外三个独立服务。两边各自 push、各自部署，互不影响。

**部署命令（把 XXX 换成你的文件/服务）：**
```bash
rsync -az --rsync-path="sudo rsync" -e "ssh -i ~/.ssh/dapeng_server_ed25519" \
  server/content_api.py dapeng-server:/home/ubuntu/content-api/
ssh dapeng-server "sudo systemctl restart huangque-content"
```

---

## 前端部署约束（防止旧页面覆盖新页面）

前端页面也必须走 git。**不要从旧目录、旧分支、AI 临时目录直接覆盖线上 HTML。**

> 🚩 **前端唯一正本目录 = `site/workbench/`（已和强哥确认 2026-06-28）。** 历史上散落的根 `workbench/`、文档曾误写的 `huangque-web/workbench/` 都已废弃删除，别再用。所有工作台页面 + 它们的依赖（`shell.css`、`assets/`）都在 `site/workbench/` 里。

当前工作台页面对应关系（线上位置统一在 `/var/www/huangquechuanmei/workbench/`）：

| 页面 | Git 文件（正本） |
|---|---|
| 资产库 | `site/workbench/assets.html` |
| 音频模块（音色卡片/克隆） | `site/workbench/audio.html` |
| 视频模块 | `site/workbench/video.html` |
| 作图 | `site/workbench/banana.html` |
| 采集 / 获客 | `site/workbench/collect.html` · `leads.html` |

前端部署前必须确认：

```bash
git branch --show-current   # 必须是 design-sync，或双方确认过的当前协作分支
git pull                    # 必须先拉最新
git status                  # 必须确认没有未提交的本地脏改动
```

部署前端只允许从 GitHub 最新代码对应的文件同步到线上。**如果只是改音频页，就只部署 `audio.html`；如果只是改资产页，就只部署 `assets.html`。不要整站 rsync 旧目录。**

> 谁最后部署前端，谁负责先确认自己手里的 HTML 包含对方已经 push 的最新改动。否则很容易把别人的新功能（比如音频资产入口、个人音色卡片）用旧页面盖掉。

---

## 几条小规矩

- **commit 说人话**：`fix: 修好图生图卡死` 比 `update` 强一百倍。出事好查。
- **当前主线 = `design-sync`**：真实产品（新后端+前端+API文档+管理台）全在 `design-sync`，和生产严格同步。`main` 暂时落后 100+ 提交、且独占少量旧归档（两边分叉），**正在合并统一**。统一前：在 `design-sync` 干活，别动 `main`。
- **改坏了能回滚**：因为都在 git 里，任何时候 `git log` 找到好的那次，回滚就行。这就是"别直接改服务器"换来的安全感。
- **🔴 密钥绝不进 git**：API key / 密码 / cookie 一律放服务器的 `content.env`（600 权限），代码里只读环境变量。`browser_data/`、`data/`、`*.env`、`*.db` 已经 gitignore，别硬塞进来。仓库保持 private。

---

## 协作约定（共享契约 · 2026-06-29 补）

这几条是改"大家共用的东西"时的规矩，碰它们前**先在群里说一声**，别闷头改：

- **改共用数据库表结构先打招呼**：`users.db`（点数）、`content_jobs.db`（任务）是所有服务共用的契约。给 `jobs`/`users` 表加列、改字段前，先通知——否则别人读它的代码会崩。私有库（`tikhub_cache.db`、`audio_assets.db`）随便改。
- **同一块代码/功能两人同时动，先对一下**：比如 `api-admin`/`api-docs`、`content_api.py`、前端 `cloud-shell.js` 这种公共件，俩人同时改容易互相覆盖。要动公共件前问一句"这块你在弄吗"。
- **单一事实源 = git，生产完整可还原**：服务器只是"跑正本的地方"。生产环境的完整记录（服务/端口/部署配置/上游工具/从零还原步骤）在 `deploy/生产环境清单与还原手册.md`——它要和生产保持一致，**禁止只改服务器不回写 git**（服务器上那 24 个 `content_api.py.bak` 就是反面教材）。

---

## 正式工作流：GitHub Flow（2026-06-29 起 · 三人都照这个）

**核心：`main` 永远保持"可部署"（拉下来就能跑）。没测好的东西留在自己分支，绝不直接进 main。**

```
main ── 永远可部署（只通过合并 PR 进东西，没人直接 push main）
 ├─ feature-zelong   Tang 开发
 └─ feature-qiang    强哥开发

每次干活：
git checkout main && git pull        # 1. 拉最新主线
git checkout -b feature-xxx          # 2. 开/切到自己的分支
…改代码…
git commit && git push feature-xxx   # 3. 先 push 进 git（活保住、有备份）
（部署到服务器测，从已 push 的分支）   # 4. 再上服务器（绝不先改服务器后 push）
开 PR：feature-xxx → main             # 5. 对方瞄一眼(review) 再合
合并后 → checkout main && pull        # 6. 主线更新
rsync 改的文件上服务器 + 重启服务      # 7. 从 main 部署，生产 = main 状态
```

口诀：**改在分支 → 先 push 再部署 → 合回 main → 生产 = main。**

要点：
- **谁都不直接 push `main`**，只能通过合并自己的 feature 分支（GitHub 上把 main 设成"保护分支/只许 PR"最稳）。
- **先 push 再部署**：git 是正本，服务器只跑正本。先改服务器后 push = 漂移（那 24 个 `.bak` 就是教训）。
- 合并后**删掉 feature 分支**（或下次接着用），别让废分支堆着。
- 分支命名：`feature-zelong` / `feature-qiang` / `feature-作图优化`，清楚就行。

---

_有疑问当面对一遍，达成共识即可。最后更新：2026-06-29_
