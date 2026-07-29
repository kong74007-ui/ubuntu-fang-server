# AI Edit V2 Director Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让真实 Qwen 导演输出稳定通过 V2 Schema，并让失败详情和阶段终态可诊断。

**Architecture:** 导演边界先对两类无语义结构偏差做确定性标准化，再执行原有严格校验；最终失败只向流水线传递脱敏字段路径。稳定流水线负责关闭当前阶段尝试并持久化安全详情。

**Tech Stack:** Python 3.12、SQLite、unittest、DashScope Qwen

## Global Constraints

- 不持久化 Qwen 原始响应、用户文案、密钥或签名地址。
- 不放宽未知枚举、额外字段、场景连续性或字幕事实锁。
- 不重新运行已退款任务；真实验证只单测 Qwen 导演边界。

---

### Task 1: 加固导演结构边界

**Files:**
- Modify: `server/content_domains/ai_edit_v2_director.py`
- Test: `tests/test_ai_edit_v2_director.py`

**Interfaces:**
- Consumes: `generate_edit_plan(context, client, max_repairs=2)`
- Produces: 合规 `edit-plan 2.0` 或带安全 `detail` 的 `DirectorError`

- [x] 写入真实失败形态测试：字符串 `style_system` 与空 `scene.id` 经标准化后通过。
- [x] 运行定向测试并确认旧实现失败。
- [x] 实现最小标准化、完整字段类型契约和安全错误详情。
- [x] 运行导演与 Schema 测试并确认通过。

### Task 2: 关闭失败阶段尝试并持久化安全路径

**Files:**
- Modify: `server/content_domains/ai_edit_v2_pipeline.py`
- Test: `tests/test_ai_edit_v2_pipeline.py`

**Interfaces:**
- Consumes: 阶段异常的稳定 `code` 与可选安全 `detail`
- Produces: `edit_v2_stage_attempts.status=failed`、稳定错误码和安全 `output_summary_json`

- [x] 写入流水线失败后阶段尝试终态与详情测试。
- [x] 运行定向测试并确认旧实现残留 `running`。
- [x] 在 `_stable_failure` 前完成当前 attempt，并保持既有退款路径。
- [x] 运行流水线、Runtime 与端到端测试。

### Task 3: 发布修复

**Files:**
- Modify: 本计划列出的代码、测试和文档

**Interfaces:**
- Consumes: 通过的本地验证结果
- Produces: 独立 Git 分支、提交和 GitHub PR

- [ ] 运行 `git diff --check` 和相关 131+ 项测试。
- [ ] 明确提交代码与测试，推送分支并创建 Draft PR。
- [ ] 等待 GitHub 门禁并报告结果，不合并、不部署。
