# AI Edit V2 素材相关性与稳定布局设计

## 目标

修复 AI Edit V2 成片中“补充素材与口播内容无关”和“所有场景看起来像同一种全屏覆盖模板”的问题，同时保持第一版稳定边界：只使用当前任务素材或 GPT 生图，最终仍由 Shotstack 渲染，不引入 Remotion、HyperFrames、自由代码 MG 或 AI 短视频。

## 已确认根因

1. 主分支已经停止查询 `user_history` 和 `platform_public`，普通当前任务素材也已经默认只使用一次。
2. 运行时仓库把所有当前任务素材统一标记为 `relevant=True, score=1.0`。解析器因此无法区分“产品图”“门店图”和与当前素材槽无关的内容。
3. Qwen 的素材槽只有槽位 ID，解析器仅能从场景 `headline/intent` 推断语义，没有独立的槽位语义字段。
4. Shotstack 适配器忽略场景的 `layout` 与 `visual_type`，补充图片和视频均作为同一种全屏 B-roll 叠加，标题卡也统一居中。

## 设计

### 素材解析

- 继续只查询当前任务绑定素材；禁止历史素材和公共素材。
- “必须使用”素材保留强制至少使用一次的语义，不因低相关性被静默替换。
- 普通补充素材必须带有当前槽位的相关性判断：
  - `relevant=False`：拒绝。
  - `relevant=True` 且有数值 `score`：按分数选择。
  - 缺少相关性证据：拒绝并调用 GPT 生图，不能默认视为相关。
- 同一普通素材默认只填充一个槽位；后续槽位生成新图片。
- 解析记录继续保存 `semantic_query`、分数和稳定的 `exclusion_code`，不得保存签名 URL。

第一版不新增向量数据库或额外视觉模型。运行时仓库只将已经带有受信任分析结果的素材转换为候选；未分析的普通素材不自动占位。必须使用素材仍可进入候选池，由用户的明确选择覆盖自动相关性判断。

### Shotstack 布局适配

保持现有稳定组件白名单，但让场景语义决定组件参数：

- `speaker_focus`：主体视频全屏，素材不覆盖人物；标题作为顶部短卡。
- `speaker_product_split`：主体视频保留，图片或视频使用右侧/下部画中画；竖屏使用下部卡片，横屏使用右侧卡片。
- `split_screen`：补充素材占一侧，主体画面占另一侧。
- `full_bleed`：补充素材全屏覆盖，适用于纯 B-roll 场景。
- `data_card`：主体视频保留，素材使用居中信息卡尺寸，标题位于卡片上方。

内部 `render_graph` 为视觉组件增加受审计的 `position`、`width`、`height`、`fit` 字段，其中 `fit` 仅允许 `contain` 或保持比例裁切的 `crop`。编译为 Shotstack Timeline 时只映射固定枚举和固定尺寸，不允许 Qwen 输出任意坐标、HTML 或代码。

### Qwen 约束

Qwen 仍只输出 provider-neutral `edit-plan 2.0`。提示词增加：

- `speaker_focus` 不得创建补充素材槽。
- 需要产品、门店、图表或 B-roll 时才创建槽位。
- 同一槽位 ID不得跨语义不同的场景复用。
- 连续场景避免重复同一 `layout`，但内容不适合变化时允许保持稳定。
- `layout`、`visual_type` 与素材槽必须语义一致。

不更换 Qwen 模型；先验证确定性解析和布局映射效果。

## 失败与降级

- 普通素材缺少可信相关性时，转 GPT 生图。
- GPT 生图失败时沿用现有 `image_generation_degraded`，该槽位不渲染素材，主体视频继续出片。
- 必须使用素材无效或无法渲染时继续硬失败并退款。
- 未识别布局、非法位置或尺寸继续在提交 Shotstack 前失败，不向供应商发送请求。

## 验收标准

- 素材解析器从不查询 `user_history` 或 `platform_public`。
- 未带可信相关性证据的普通上传素材不会自动入片。
- 同一普通素材不会自动填满多个槽位。
- 五种稳定布局至少产生三类不同的 Shotstack 视觉参数组合。
- `speaker_focus` 不被补充素材覆盖。
- `speaker_product_split` 与 `split_screen` 在 9:16、16:9 下均保持画布内安全尺寸。
- Qwen 提示词明确约束槽位、布局和重复，但不出现 Shotstack 字段。
- 相关单测、全量 AI Edit V2 测试、语法检查和 `git diff --check` 全部通过。
