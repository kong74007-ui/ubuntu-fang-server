# AI Edit V3 容量与背压验收

本检查只测量，不通过降低清晰度、关闭沙箱或削弱质检来换取通过。

## 冻结门槛

| Profile | vCPU | RAM | 临时 SSD | 流水线并发 | 渲染槽位 |
|---|---:|---:|---:|---:|---:|
| parallel-5 | 8 | 16 GiB | 80 GiB | 5 | 2 |
| stress-10 | 16 | 32 GiB | 160 GiB | 10 | 4 |

`parallel-5` 要求端到端 p50 不超过 25 分钟、p95 不超过 45 分钟。`stress-10` 是安全压力测试，任何崩溃、跨任务读取、重复供应商/计费调用或账务损坏都判定为 `capacity_blocked`。

## 预扣前背压

队列深度大于 50，或可用临时磁盘小于本任务预留值时，必须在扣点前返回 `capacity_unavailable` 和正数 `Retry-After`。

## 记录指标

每次运行记录队列等待、端到端与各阶段 p50/p95、CPU/RAM/磁盘峰值、渲染槽位峰值、背压次数、超时、沙箱资源限制事件，以及四类安全计数。同一容量 profile 的每条任务必须提供一致且非空的阶段集合；可选阶段用 `0 ms` 明示未执行，禁止直接省略。每个阶段报告保留样本数，样本数必须等于本次任务数。冻结报告必须保留原始计数，不得用降质参数重跑覆盖。

本地合成门禁：

```text
python scripts/ai_edit_v3_capacity.py verify --fixture tests/fixtures/ai_edit_v3/capacity-synthetic.json
```
