# Task 7 实施报告

- 分支：`codex/ai-edit-v2-stable-release`
- 基线 SHA：`72f714aff0f3ff756bd4f98ce812f471fe50482b`
- Task 7 SHA：本报告所在的单独提交（以交付时 `git rev-parse HEAD` 为准）
- 部署/重启：未执行

## 实现

- 新增 `run_job(job_id, dependencies, db_path=None) -> dict`，顺序执行 `normalizing -> transcribing -> aligning -> directing -> resolving_materials -> generating_media -> rendering -> postprocessing`，并停在 Task 8 边界 `quality_checking`；未实现质检、结算或交付。
- SQLite schema 升至 v6，新增稳定阶段检查点，持久化输入 SHA-256 fingerprint、输出、provider task/reference、跨进程 attempt count。
- 只有 fingerprint 一致且 `verify_artifact` 对所有已存产物返回真时才复用检查点；损坏或缺少验证的产物会重新执行阶段。
- job 使用独占持久租约，长轮询期间后台续租；重复 worker 无法同时执行同一任务，过期租约可被重启进程领取。
- provider 阶段初次调用后最多重试 2 次，计数跨进程保存；普通预算沿用 2700 秒，修复预算沿用既有 900 秒规则。
- 崩溃前保存的 provider task/reference 会强制进入对应 reconciler；没有 reconciler 时失败关闭，绝不先调用 submit handler。并发、duplicate retry 与 crash recovery 测试证明 OpenAI image、ElevenLabs、Shotstack 对应阶段各只执行一次提交路径。

## TDD 与验证

RED：

```text
python -m unittest tests.test_ai_edit_v2_pipeline tests.test_ai_edit_v2_runtime -v
Ran 30 tests
FAILED (errors=6): run_job、稳定阶段映射和 runtime 契约尚不存在
```

GREEN（Task 7 定向）：

```text
python -m unittest tests.test_ai_edit_v2_pipeline tests.test_ai_edit_v2_runtime tests.test_ai_edit_v2_store -v
Ran 64 tests in 21.977s
OK
```

全量 AI Edit V2：

```text
python -m unittest discover -s tests -p "test_ai_edit_v2*.py" -v
Ran 256 tests in 28.394s
OK
```

静态检查：`py_compile`、`git diff --check` 均退出 0。

## 风险/未完成

- 未调用真实供应商、未部署测试环境；真实 OpenAI、ElevenLabs、Shotstack 计费与恢复仍属于 Task 11 验收门禁。
- `run_job` 通过显式 dependencies 注入 Task 2-6 adapter handlers/reconcilers；生产依赖装配和 HTTP/Webhook 路由不在 Task 7 范围。
- 仅推进到 `quality_checking`；硬质检、原子交付、结算和入库均未提前实现。
