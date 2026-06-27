# 黄雀团队 · Git 协作规矩（一页版）

> 给两个人（+各自的 AI：Claude Code / Codex）看的协作约定。**大白话，照着做就不打架。**
> 单一事实源在 GitHub `design-sync` 分支。线上：https://huangquechuanmei.com ｜ 服务器：`dapeng-server`（129.204.166.13）

---

## 一条铁律（最重要，先记这条）

**🚫 不要直接在服务器上改代码。**

为什么：服务器上直接改 = 没备份、没记录、两个人一改就互相覆盖、改坏了没法回滚。我们已经吃过好几次这个亏。

✅ 正确姿势：**所有改动先走 git（本地改 → push 到 GitHub），再从 git 部署到服务器。** GitHub 是唯一的"正本"，服务器只是"跑正本的地方"。

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
- **main 分支 = 稳定版**：现在大家在 `design-sync` 分支干活，main 只放"线上能跑的版本"。别往 main 直推没测过的东西。
- **改坏了能回滚**：因为都在 git 里，任何时候 `git log` 找到好的那次，回滚就行。这就是"别直接改服务器"换来的安全感。
- **🔴 密钥绝不进 git**：API key / 密码 / cookie 一律放服务器的 `content.env`（600 权限），代码里只读环境变量。`browser_data/`、`data/`、`*.env`、`*.db` 已经 gitignore，别硬塞进来。仓库保持 private。

---

## 升级路线（现在不用，等顺了再说）

四步走利索后，再升级成"**各开自己的分支 → 提 PR → 合进 main**"，main 永远干净、改动可评审。现在先把上面四步走顺就够。

---

_有疑问当面对一遍，达成共识即可。最后更新：2026-06-27_
