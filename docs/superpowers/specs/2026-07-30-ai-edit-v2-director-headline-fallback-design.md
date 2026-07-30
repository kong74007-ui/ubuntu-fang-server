# AI Edit V2 导演空标题兜底设计

## 背景

真实任务 `843cb7c6-efed-49ec-addc-7286c4c96db4` 在转码、ASR 和文案对齐完成后失败。Director 最终校验错误为 `scenes[7].headline不能为空`。Qwen 初次输出及最多两次修复仍可能保留空标题，当前严格 Schema 会因此终止整条任务并退款。

## 目标

当 Qwen 场景已经包含非空 `intent`，但 `headline` 缺失、为空或只有空白时，Director 在 Schema 校验前使用该场景的 `intent` 确定性补齐 `headline`，避免单个空展示标题导致整条剪辑失败。

## 设计

- 修改 `ai_edit_v2_director._normalize_structural_fields()`。
- 对每个场景保留现有 `id` 归一化。
- `headline` 为缺失、空字符串或纯空白，并且 `intent` 为非空字符串时，将 `intent.strip()` 写入 `headline`。
- 非空 `headline` 保持原样，不由系统改写。
- `intent` 同样无效时不兜底，由现有 Schema 按原错误拒绝。
- 补齐发生在 `validate_edit_plan()` 之前，因此后续仍走完整严格校验。

## 方案取舍

1. **采用：从同场景 `intent` 确定性补齐。** 不增加模型调用，不创造字幕事实，能直接消除本次失败模式。
2. 不采用允许空标题。Shotstack 虽可跳过空卡片，但这会改变当前 V2 场景协议，并让模板需要的重点卡片静默消失。
3. 不采用继续增加 Qwen 重试。重试已经无法保证修复，会增加耗时和调用成本。

## 边界

- 不从字幕正文改写文案，不修改 `caption_plan`。
- 不修改 Schema、素材解析、渲染、质检、计费、任务状态或数据库。
- 不自动重跑历史失败任务，不触发第三方调用或点数扣除。

## 验收

- 空白 `headline` 加载后直接变为同场景去除首尾空白的 `intent`。
- 该输出通过现有 `validate_edit_plan()`，且不触发第二次 Qwen 调用。
- 原有合法非空标题保持不变。
- Director、Schema 与 Pipeline 相关测试全部通过。
