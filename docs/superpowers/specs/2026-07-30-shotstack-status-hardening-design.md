# Shotstack 渲染状态加固设计

## 问题

Shotstack 合法中间状态包含 `preprocessing` 和 `saving`，现有适配器未识别，导致平台提前进入 `render_failed` 并退款，而第三方任务仍可能继续运行。

## 设计

- 将 `queued`、`fetching`、`preprocessing`、`rendering`、`saving` 统一映射为内部 `pending`。
- `done` 映射为 `succeeded`，`failed` 映射为 `failed`；保留现有兼容别名。
- 未知状态继续失败关闭，但只持久化经过字符集和长度限制的状态词，不保存完整响应。
- 失败的渲染阶段明细必须为 `failed`，继续使用现有全额退款逻辑。

## 验收

- 新增状态映射和未知状态留痕测试。
- Shotstack、Pipeline、Runtime、E2E 测试通过。
- 合并部署测试环境后，从测试网站创建任务并监视到成片可播放、可下载。
