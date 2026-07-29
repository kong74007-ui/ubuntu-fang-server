# Shotstack Status Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 防止 Shotstack 合法中间状态触发提前失败，并完成测试网站真实出片验证。

**Architecture:** Shotstack 适配器统一官方状态为内部三态；流水线只持久化安全未知状态词。代码经 PR 合并后部署测试环境，再通过网站创建并监视真实任务。

**Tech Stack:** Python 3.12、SQLite、Shotstack Edit API、unittest

## Global Constraints

- 不保存 Shotstack 完整响应或密钥。
- 不修改生产环境。
- 不取消或重放已经退款的旧任务。

---

### Task 1: 状态映射与留痕

**Files:**
- Modify: `server/content_domains/ai_edit_v2_shotstack.py`
- Modify: `server/content_domains/ai_edit_v2_pipeline.py`
- Test: `tests/test_ai_edit_v2_shotstack.py`
- Test: `tests/test_ai_edit_v2_pipeline.py`

- [x] 写入并运行失败测试。
- [x] 将 `preprocessing`、`saving` 映射为 `pending`。
- [x] 安全持久化未知状态词。
- [x] 运行 105 项相关测试。

### Task 2: PR 与测试环境验证

**Files:**
- Modify: 本计划列出的代码、测试和文档

- [ ] 提交并推送独立 PR，等待 GitHub 门禁。
- [ ] 合并并部署测试环境。
- [ ] 从测试网站创建任务并监视到成片交付。
