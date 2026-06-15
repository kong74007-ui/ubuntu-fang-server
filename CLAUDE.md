# CLAUDE.md — 抖音评论区获客系统

## 这是什么
关键词 → 抖音搜视频 → 扒评论区 → 意图过滤 → 精准客户名单。为大鹏老板公司 AI 板块获客场景而建。

## 架构（双层）
- **发现层** MediaCrawler（关键词搜索+评论采集）— 本地 Mac `~/code/MediaCrawler`，服务器无头化进行中
- **深采层** 小探/Douyin_TikTok_Download_API（账号深采+下载+口播ASR）— 服务器 `129.204.166.13:8501`（systemd `xiaotan`）
- **过滤层** 本仓库 `scripts/leads_filter.py`

## 红线（务必遵守）
- `browser_data/`（抖音 cookie）、`data/`（真实名单含 PII）**永不进 git**，已 gitignore。
- 仓库保持 **private**。

## 上游工具（不在本仓库分发）
- MediaCrawler: https://github.com/NanmiCoder/MediaCrawler （标准模式 `ENABLE_CDP_MODE=False`）
- 小探: https://github.com/Evil0ctal/Douyin_TikTok_Download_API

## 下一步
封装飞书 Bot：团队发关键词 → 服务器引擎跑 → 回传名单（团队内部用）。详见 README 路线图。

## 相关记忆
本机 AI-Memory：`reference-mediacrawler-keyword-leads`、`reference-douyin-tiktok-download-api`、`project-dapeng-ai-division`。
