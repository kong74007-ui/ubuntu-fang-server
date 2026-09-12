# 阻断项：v09 放大字号会被生产链路拒绝

## 结论

**`reference-featured-layout.patch` 里 v09 的四个放大字号（100 / 62 / 72 / 78 px）无法上线。**

把 v09 的 `top1` 从当前生产的 **88px** 放大到任何更大的值（90 / 92 / 94 / 96 / 98 / 100px 全部实测），
服务端在**真实 App payload** 上直接抛错：

```
ValueError: HyperFrames 文案无法在完整语义边界内排入模板
```

经 `/v1/jobs` 返回给 App 即为 **HTTP 400 → 任务根本创建不了**（不是视觉问题，是功能问题）。

## 复现材料

### 输入 payload（真实来源）

取自生产任务库 `relay…` 任务（App 于 09-11 20:04 发给 `ref-09-urumqi-soft-brush` 的那一份，
`manifest-before.json` / `manifest-after.json` 的 `input` 字段为逐字复制）：

```json
{
  "top_text": "选赛道别只看谁现在最火 要看三年后 客户还会不会继续消费",
  "bottom_text": "大健康是长期需求赛道 想入局的 评论区回复 勾兑",
  "semantic_layout": {
    "version": 1, "model": "gpt-4.1-mini",
    "source_sha256": "89f75f1c37103c3ff3a4fb3a632034b6a4833e6d945d05cd762abd96f49a8dd1",
    "top1_end": 11, "top_break_after": [11, 17],
    "bottom_break_after": [10, 15, 21]
  }
}
```

### 失败发生的那一层

```
service.validate_payload(raw, require_reference_semantic_layout=True)
  -> _reference_semantic_text_layout()
    -> _pack_reference_semantic_span(top, 0, top1_end=12, top_break_after=[11,17], contract["top1"])
       -> ValueError: HyperFrames 文案无法在完整语义边界内排入模板
```

**根因（第一性原理）**：`top1_end = 11` 把 `top1` 的语义跨度锁死为
`选赛道别只看谁现在最火`（11 个字），这个跨度内部**没有任何 AI 断点**（`top_break_after` 只有 11、17 两个位置），
因此它**只能排一行**；而该行的宽度预算 = `max_width_px 996`。

| v09 top1 字号 | 服务端判定 |
|---|---|
| 88px（当前生产） | ✅ 通过 |
| 90 / 92 / 94 / 96 / 98 / 100px | ❌ 全部失败 |

说明 88px 已经贴着 996px 的宽度上限，**任何放大都会溢出**。这不是排版 bug，
而是"字号上限"与"AI 给出的语义跨度只能单行"共同决定的硬约束。

### 对照组（同一份 payload、同一套 after 包）

| 模板 | after 包链路 |
|---|---|
| `ref-06-guangzhou-yellow-button` | ✅ 通过 |
| `ref-14-karamay-green` | ✅ 通过 |
| `ref-17-shenzhen-yellow-red` | ✅ 通过 |
| `ref-09-urumqi-soft-brush` | ❌ 失败（如上） |

且 v06 / v14 / v17 的 `display_text` 在 before 与 after 包上**逐字相同**（见两份 manifest），
所以这两组 before/after 是严格同源对照。

## 本仓库在本轮采取的动作

* **未修改任何断句、字号或排版逻辑**（按评审要求冻结）。
* **删除了失效的 v09-after 证据**：那条 MP4 是用 before 包产出的 `display_text` 渲染的，
  而该 `display_text` 在 after 包上根本产不出来，不能作为验收证据。
* 总览图中 v09 的 after 位置标注为 `BLOCKED: server 400`。

## 可选的解决方向（需人工确认，本仓库未实施）

1. **放弃 v09 放大**：保留 88px，本次只做配色回滚（v06 / v14 / v17）。零风险。
2. **小幅放大**：实测上限就是 88px（90px 即失败），因此"放大"在该 payload 下不可行。
3. **改 App 侧 AI 的断点**：让 `top_break_after` 在第一分句内也给出断点
   （例如 `选赛道｜别只看谁现在最火`），服务端即可把 top1 排成两行 → 放得下。
   这属于调用方行为，不在本仓库范围。
4. **放宽服务端 top1 的单行约束**：会改动断句/排版逻辑，与本次"冻结断句排版"的要求冲突。

推荐 **方向 1 或 3**；在明确授权前不做任何变更。
