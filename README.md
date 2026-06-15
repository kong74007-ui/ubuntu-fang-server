# 抖音评论区获客系统（douyin-leadgen）

> 输入一个关键词 → 自动搜出相关视频 → 扒光评论区 → 过滤出**精准潜在客户名单**（带抖音号/属地/需求原文）。

为大鹏老板公司 AI 板块的「降本增效」获客场景而建。核心逻辑：**在抖音搜行业关键词，评论区里那群「问怎么拓客 / 求方法 / 报预算」的人，就是精准需求池。**

---

## 一、它解决什么

传统找客户靠人工刷评论、手动记。本系统把整个链路自动化：

```
关键词(如"美业获客")
      │
      ▼
 ① 搜索相关视频  ──────────────┐
      │                        │  发现层
 ② 逐条扒评论区               │  (MediaCrawler)
      │                        │
      ▼ ───────────────────────┘
 ③ 意图过滤器  ←── 本项目核心脚本 scripts/leads_filter.py
      │   · 保留：怎么拓客/怎么收费/求带/报预算/没开单…
      │   · 剔除：同行中介引流话术（"需要我推荐给你"…）
      ▼
 ④ 精准客户名单（昵称 + 抖音号 + IP属地 + 需求原文 + 来源视频）
```

## 二、架构：双层互补

| 层 | 工具 | 职责 | 部署位置 |
|---|---|---|---|
| **发现层** | [MediaCrawler](https://github.com/NanmiCoder/MediaCrawler) | 关键词搜索 + 评论采集（7 平台，抖音/小红书等） | 本地 Mac / 服务器(无头) |
| **深采层** | [Douyin_TikTok_Download_API（小探）](https://github.com/Evil0ctal/Douyin_TikTok_Download_API) | 给定账号深采：画像/作品/无水印下载/口播 ASR | 服务器 `:8501`（systemd 守护） |
| **过滤层** | 本项目 `scripts/leads_filter.py` | 评论 → 意图分类 → 干净客户名单 | 任意 |

> 发现层负责"关键词→一批种子号/评论"，深采层负责"选定号→深扒"。获客场景发现层一个就够；做对标分析时再上深采层。

## 三、实测成果（2026-06-15）

关键词「美业获客」单次跑：

- 抓取：**14 条视频 / 140 条评论**
- 过滤：🔥 **精准客户 42 个** | 🗑️ 同行中介噪音 12（已剔除）| 💬 闲聊 86
- 名单含顶级线索（如"店装修花 30 万快坚持不了，月业绩才 3000"、"我有 5000 拓客预算谁能帮我"）

> 真实名单含 PII，按红线**不进仓库**，只在本地 `data/`（已 gitignore）。脱敏成果见 `docs/`。

## 四、快速开始

依赖两个上游开源项目（不随本仓库分发，自行 clone）：

```bash
# 发现层
git clone https://github.com/NanmiCoder/MediaCrawler.git
cd MediaCrawler && uv sync && uv run playwright install chromium
# 配置 config/base_config.py：PLATFORM=dy / KEYWORDS=你的词 /
#   CRAWLER_TYPE=search / ENABLE_GET_COMMENTS=True / ENABLE_CDP_MODE=False
uv run main.py --platform dy --lt qrcode --type search   # 首次扫码，之后免登录

# 过滤出客户名单
python scripts/leads_filter.py \
  --comments MediaCrawler/data/douyin/jsonl/search_comments_*.jsonl \
  --contents MediaCrawler/data/douyin/jsonl/search_contents_*.jsonl \
  --out data/leads.md
```

详细部署（含服务器无头化、踩坑）见 `docs/部署记录.md`。

## 五、路线图

- [x] 本地跑通 关键词→评论→客户名单
- [x] 小探深采层部署到服务器（`:8501`，4 接口验证）
- [ ] MediaCrawler 服务器无头化（搬登录态，机房 IP 可行性验证中）
- [ ] **封装飞书 Bot**：团队成员发关键词 → 服务器跑 → 回传名单卡片/Excel（团队内部用）
- [ ] 放大：多关键词、评论回复层、按需求强度排序

## 六、安全与合规

- 浏览器登录态 / cookie：**永不进 git**（见 `.gitignore`）
- 客户名单含个人信息：私有仓库 + 本地隔离，仅用于正当商业触达，遵守平台规则
- 上游工具各自遵循其开源协议（MediaCrawler、Douyin_TikTok_Download_API）

---

*本仓库为「战斗成果」沉淀：架构、部署 playbook 与自研过滤脚本。上游爬虫工具不在此分发。*
