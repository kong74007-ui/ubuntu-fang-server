# Task 7 实施报告

## Fix Round 2

- `production_dependencies()` now constructs `ProductionServices` with eight concrete stage methods. The enabled worker calls `assert_production_ready()` before the first job claim, so missing provider, callback, webhook, or private COS configuration fails startup explicitly.
- The concrete path composes the existing Task 2-6 media/FFprobe, private COS, DashScope ASR/Qwen, alignment, material resolver/OpenAI image adapter, audio planner/ElevenLabs adapter, Shotstack, and final COS persistence boundaries. No Task 8 quality implementation was added.
- Every stable stage has a fail-closed semantic output schema before recursive artifact validation. Tests cover a missing field and wrong type for all eight stages, garbage transcription, and artifact-only normalization.
- The integration test invokes the real worker `_process_claimed` and real `run_job`, with concrete stages and real adapter classes. Only external HTTP, COS, download, and process transports are faked; the job reaches `quality_checking` and final bytes are verified in private COS.
- Final verification: targeted runtime/pipeline suite passed; full `test_ai_edit_v2*.py` discovery passed (exit 0). No deploy, restart, live provider call, Task 8 work, or Task 11 acceptance-gate change.

## Fix Round 1

- 生产 worker 已从旧的逐阶段 `run_stage` 切换到 `run_job`，每次 claim 使用唯一 lease token，并装配 `runtime.production_dependencies()`；bundle 显式提供 Task 2-6 的 DashScope、OpenAI image、ElevenLabs、Shotstack adapter 类型、各阶段 handler/reconciler 和私有 COS verifier。功能开关关闭时仍只做账务 reconcile、不 claim 新任务。
- 增加逐阶段输出契约及递归 artifact extractor。`normalizing`、`resolving_materials`、`generating_media`、`rendering`、`postprocessing` 必须提供带 `cos_key`、`etag`、正 `size_bytes` 的产物；nested artifact、缺失 metadata、COS size/etag 不一致均失败关闭。
- heartbeat 失租会传播 `job_lease_lost`。检查点 prepare/increment/invalidate/complete、stage attempt、provider identity 和迁移全部以 job lease owner/expiry 作 fencing；旧 worker 失租后不能保存晚到 provider ID 或覆盖新 worker。
- handler 前后、checkpoint 前和 transition 前均检查 lease/deadline；晚结果保持 checkpoint 为 running，不推进状态。provider identity 已先持久化时仍供下一 worker reconcile。
- 失败 transition 原子写 `edit_v2_jobs.error_code`；成功 transition 清空旧错误，API/restart 读取数据库可得到稳定终态错误。

Fix Round 1 RED：worker 仍调用 `run_stage`；nested artifact 被漏检；heartbeat 失租后晚 provider ID 可写；晚 handler 结果触发 CAS 而非预算失败；终态 `error_code` 为 NULL。

```text
Task 7 定向：Ran 70 tests in 19.486s, OK
全量 test_ai_edit_v2*.py：Ran 262 tests in 24.058s, OK
```

未部署、未重启、未调用真实供应商；生产 bundle 的真实 adapter 类型和 handler/reconciler 边界由测试覆盖，真实供应商验收继续属于 Task 11。

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
