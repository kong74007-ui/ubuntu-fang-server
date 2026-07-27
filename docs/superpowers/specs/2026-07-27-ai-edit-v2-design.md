# AI 智能剪辑 V2 设计规格

## 1. 文档状态与目标

本文定义黄雀传媒测试环境的新模块“AI智能剪辑 V2”。它与测试站现有“一键剪辑”并行存在，不替换、不迁移、不复用旧模块数据库，也不继续修改 PR #20 的 V1 实现。

V2 的目标是形成以下可验证闭环：用户选择平台口播视频/音频资产或上传外部视频/音频，系统完成媒体校验与标准化、语音时间戳、确定性字幕、AI导演方案、四级素材解析、Shotstack云端渲染、成片质检、COS交付和视频资产入库。

本阶段只允许开发和部署测试环境；不合并或部署生产环境；暂不开发内容安全审核；计费暂时沿用“提交扣30点、失败幂等退款”，真正的点数冻结、确认和释放留待后续阶段。

## 2. 隔离边界

### 2.1 必须保持不变的内容

- 测试网站现有“一键剪辑”入口、接口、任务和数据保持不变。
- PR #20 的 `/api/v1/edit/*`、旧 `edit-plan 1.0` 和相关数据库代码保持不变。
- 旧 `ai_edit.db`、历史 HyperFrames 任务和历史成片不迁移到 V2。
- 生产服务器、生产数据库和生产域名不在本阶段操作范围内。

### 2.2 V2 独立资源

- 开发分支：`codex/ai-edit-v2`，从最新 `main` 创建。
- 页面：`site/workbench/ai-edit.html`，用户侧名称为“AI智能剪辑”。
- API 前缀：`/api/v2/edit/*`。
- 数据库：`ai_edit_v2.db`。
- 后台队列：独立的 AI Edit V2 队列和 Worker 配置。
- 日志前缀：`[ai-edit-v2]`，禁止记录密钥、完整签名URL、用户完整文案和供应商完整响应。

公共文件只允许做最小增量接线，例如注册API处理器、任务能力和工作台入口；不得把V2业务逻辑写入 `core.py` 或旧页面。

## 3. 用户流程

1. 用户进入“AI智能剪辑”，旧“一键剪辑”仍作为独立入口显示。
2. 用户选择自己在平台生成的视频/音频资产，或上传一个外部视频/音频。
3. 用户可上传本次补充图片或选择历史素材，并选择剪辑风格与画面比例。
4. 前端提交带 `Idempotency-Key` 的V2任务请求。
5. 服务端校验登录状态、资产归属、会员/点数条件、文件类型和任务并发限制。
6. 服务端扣除30点并创建任务；后续任意失败只退款一次。
7. 系统取得COS源文件、执行FFprobe检测，并在必要时用FFmpeg标准化。
8. `fun-asr`生成逐句和逐字时间戳；平台口播执行原文对齐，外部视频/音频采用ASR正文。
9. `qwen-plus`仅生成 `edit-plan 2.0` 语义导演方案。
10. 素材解析器按固定优先级解析语义素材槽位，缺图时才调用GPT图片模型。
11. 所有生成素材先上传COS并入库，再生成短期签名URL。
12. Shotstack适配器把已解析的中间方案转换成Timeline JSON并提交Stage渲染。
13. Webhook和Worker轮询共同跟踪供应商任务；重启后复用原供应商任务ID。
14. 成片下载后执行完整质检，通过后转存COS并写入用户视频资产库。
15. 任务变为 `completed`，前端展示播放和下载地址；失败时展示明确阶段和退款结果。

## 4. 模块划分

建议新增以下职责单一的模块：

- `ai_edit_v2_api.py`：鉴权、幂等、上传、提交、查询、重试和Webhook入口。
- `ai_edit_v2_store.py`：V2数据库、任务、阶段尝试、素材关系和事件去重。
- `ai_edit_v2_pipeline.py`：状态机和完整任务编排。
- `ai_edit_v2_schema.py`：提交参数与 `edit-plan 2.0` 白名单校验。
- `ai_edit_v2_asr.py`：fun-asr提交、轮询和结果归一化。
- `ai_edit_v2_alignment.py`：平台原文与ASR时间戳的确定性对齐。
- `ai_edit_v2_planner.py`：Qwen提示词、结构化输出和一次修复。
- `ai_edit_v2_assets.py`：四级素材搜索、生成、入库和解析。
- `ai_edit_v2_media.py`：FFprobe、FFmpeg标准化和媒体临时文件管理。
- `ai_edit_v2_quality.py`：解码、编码、时长、黑帧、静音和占位视频检查。
- `ai_edit_v2_billing.py`：本阶段30点扣除和幂等退款的封装。
- `renderers/shotstack_v2.py`：`edit-plan 2.0` 到Shotstack Timeline的隔离适配器。

每个模块通过明确的数据对象调用，不读取其他模块的私有表，不在模块间传递API Key。

## 5. HTTP API

### 5.1 用户接口

- `GET /api/v2/edit/styles`：返回风格、固定单价、可选素材和能力开关。
- `POST /api/v2/edit/uploads`：创建源视频或补充图片的COS直传签名。
- `POST /api/v2/edit/uploads/{id}/complete`：核验COS对象大小、MIME和归属并完成上传。
- `POST /api/v2/edit/jobs`：幂等创建任务、扣30点并入队。
- `GET /api/v2/edit/jobs/{id}`：只允许任务所有者查询状态、阶段、进度和结果。
- `POST /api/v2/edit/jobs/{id}/retry`：为允许重试的终态失败任务创建继任任务，复用安全产物并重新扣30点。

### 5.2 供应商接口

- `POST /api/v2/edit/webhooks/shotstack?token={secret}`：接收Shotstack回调。

Webhook随机密钥来自 `AI_EDIT_V2_WEBHOOK_SECRET`，使用常量时间比较；无效密钥返回401。Nginx必须为该回调路径关闭包含查询参数的访问日志，应用日志也必须剥离token。回调体只用于提取供应商任务ID，最终状态必须通过Shotstack API重新查询。相同 `provider_job_id + normalized_status` 只记录一次，重复或乱序回调不得改变已完成终态。

### 5.3 上传限制

- 源视频接受MP4和MOV；源音频接受MP3、WAV和M4A；单文件最大1GiB，最长10分钟。
- 补充图片接受JPG、PNG和WebP，单张最大25MiB。
- 每个任务最多选择20个补充素材，其中本次上传图片最多10张。
- 每个任务最多自动生成8张图片。
- 上传PUT签名默认15分钟；私有GET签名只在实际使用前生成。

## 6. 数据库设计

`ai_edit_v2.db` 至少包含以下表：

### 6.1 `edit_v2_jobs`

保存任务所有者、源资产、风格、比例、当前状态、当前进度、幂等键、扣点状态、原始导演方案、已解析渲染方案、供应商任务ID、输出COS键、错误码、错误摘要、版本和时间戳。

关键约束：

- `job_id` 与核心 `content_jobs.db` 任务一一对应。
- `username + endpoint + idempotency_key` 唯一。
- `provider_job_id` 一旦设置不得绑定为另一个值。
- 终态只能通过条件更新从非终态进入，避免Worker、Webhook和清道夫竞争覆盖。
- 数据库只保存COS对象键，不保存临时签名URL。

### 6.2 `edit_v2_materials`

保存素材所有者、类型、语义角色、来源、COS对象键、MIME、尺寸、状态和内容摘要。平台公共素材使用专用公共所有者标记；用户私有素材必须按账号过滤。

### 6.3 `edit_v2_job_materials`

保存任务、语义槽位、素材ID、优先级和选用原因。每个槽位最终只能绑定一个选中素材。

### 6.4 `edit_v2_stage_attempts`

每次阶段执行保存阶段名、尝试次数、开始/结束时间、耗时、结果、错误码、供应商请求ID、供应商计量值和估算费用。敏感响应和签名URL不得写入。

### 6.5 `edit_v2_provider_events`

保存Webhook事件指纹、供应商任务ID、归一化状态和接收时间，用唯一索引实现重复通知去重。

### 6.6 `edit_v2_billing`

本阶段保存 `deducted/refunded` 状态、30点金额和唯一交易键。它只作为现有先扣后退流程的幂等台账，不宣称实现真正资金冻结。

## 7. 状态机与进度

正常状态严格为：

```text
created
-> validating
-> normalizing
-> transcribing
-> aligning_transcript
-> planning
-> resolving_assets
-> generating_assets
-> submitting_render
-> rendering
-> quality_check
-> storing
-> completed
```

失败状态按阶段区分：

```text
validation_failed
normalization_failed
transcription_failed
alignment_failed
planning_failed
asset_failed
render_failed
quality_failed
storage_failed
```

每次状态变化必须同时更新任务行和阶段尝试记录。查询接口返回阶段、百分比、最近更新时间、可否重试、用户可读错误和退款状态；不向用户返回供应商密钥、内部堆栈或完整供应商响应。

## 8. 媒体检测与标准化

### 8.1 输入检测

源媒体从COS下载到任务专属临时目录后，FFprobe必须确认：

- 容器和视频流可解析。
- 时长大于0且不超过10分钟。
- 视频源至少有一个视频流和一个可用音轨；音频源至少有一个可用音轨。
- 真实文件类型与声明类型一致。
- 像素尺寸、帧率、视频编码和音频编码可读取。

### 8.2 标准化触发条件

视频容器不是MP4、视频不是H.264、音频不是AAC、时间基异常、帧率不稳定或媒体无法被Shotstack稳定读取时执行FFmpeg标准化。视频标准化输出为MP4/H.264/AAC、30fps、48kHz双声道，并保留源画面宽高比；音频标准化输出为M4A/AAC、48kHz双声道。最终视频画布由Shotstack输出层统一为1080×1920或1920×1080。

标准化前后时长误差超过200ms即失败。标准化产物上传到任务私有COS键，ASR和Shotstack使用同一份标准化输入，临时文件在成功或失败后均清理。

## 9. 文案、ASR与确定性字幕

### 9.1 平台内生成的口播

若视频或音频资产包含平台保存的原口播文案：

- 原文负责语义和字符准确性。
- fun-asr只提供分句、逐字时间戳和对齐锚点。
- 对齐模块对规范化后的原文和ASR文本执行确定性序列对齐。
- 品牌名、产品名、数字、价格和专有词以原文为准。
- 字幕正文由对齐结果生成，不允许Qwen重写。

对齐结果保存覆盖率和置信度。覆盖率低于85%或时间戳无法单调映射时进入 `alignment_failed`，不得静默改用错误字幕。

### 9.2 外部上传视频

- fun-asr同时负责转录正文和时间戳。
- Qwen可建议标点和断句，但不得增删事实、数字或改变原意。
- 服务端必须验证修复结果与ASR字符序列一致；不一致时使用原ASR文本。

### 9.3 字幕开关

- `captions=false`：不生成字幕文件，不创建Shotstack字幕轨。
- `captions=true`：服务端根据确定性时间轴生成VTT；Qwen只可输出强调词索引，不输出最终字幕正文和时间。

## 10. `edit-plan 2.0` 协议

Qwen只输出平台协议，不输出COS键、签名URL、数据库ID、HTML、CSS或Shotstack字段。

顶层结构：

```json
{
  "version": "2.0",
  "duration_ms": 44920,
  "style": "business_diagnostic",
  "captions": [],
  "scenes": [],
  "overlays": [],
  "materials": [],
  "audio_cues": []
}
```

第一阶段风格白名单：

- `business_diagnostic`：知识、观点和诊断型口播。
- `product_story`：痛点、证据、过程、结果型产品讲解。
- `story_broll`：音频或叙事主导、B-roll占比较高的故事画面。

每个场景至少包含：

```json
{
  "id": "scene_01",
  "start_ms": 0,
  "end_ms": 5800,
  "intent": "打破美业暴利认知",
  "layout": "speaker_fullscreen",
  "visual_type": "diagnostic_hook",
  "headline": "美业真的赚钱吗？",
  "material_slots": ["slot_01"],
  "transition": "hard_cut"
}
```

`materials[]` 是纯语义素材槽位：

```json
{
  "slot_id": "slot_01",
  "start_ms": 1200,
  "end_ms": 4200,
  "semantic": "美容门店经营压力",
  "recommended_visual": "冷清门店和账本特写",
  "kind": "image",
  "ratio": "9:16",
  "width": 1080,
  "height": 1920,
  "required": false,
  "generation_prompt": "写实商业摄影，美容门店经营压力"
}
```

`captions[]` 只保存服务端生成的字幕分组和强调索引；Qwen返回内容不得覆盖确定性字幕。`overlays[]` 只允许服务端白名单类型和纯文本。`audio_cues[]` 第一阶段必须为空数组，为以后背景音乐和音效扩展保留协议位置。

所有时间必须是整数毫秒并满足 `0 <= start_ms < end_ms <= duration_ms`。场景必须按时间排序、不得重叠且覆盖完整输出时间轴。未知字段、未知布局、未知转场、任意URL和不安全文本均拒绝。

Qwen首次结果校验失败时允许带校验错误修复一次；第二次失败进入 `planning_failed`。原始Qwen方案保持不可变，素材解析器生成独立的 `resolved_plan`。

## 11. 四级素材解析

素材槽位按以下固定优先级解析：

1. 用户本次上传且语义匹配的素材。
2. 当前用户历史素材库中可用且语义匹配的素材。
3. 平台公共素材库中可用且语义匹配的素材。
4. 当 `auto_assets=true` 时调用GPT图片模型生成。

解析器负责把语义槽位转换成：

```text
slot_id -> source -> asset_id -> cos_key
```

Qwen不能选择具体数据库ID。匹配必须同时满足账号权限、素材类型、比例/尺寸要求和可用状态。相同槽位只选最高优先级的一项；匹配结果及理由写入任务素材关系表。

AI图片生成后必须完成：生成成功、文件校验、私有COS上传、素材入库、任务关联、槽位回填。任何一步失败都不能把临时URL交给Shotstack。`auto_assets=false` 时不得调用生图；非必需槽位缺失时回退源视频，必需槽位缺失时进入 `asset_failed`。

COS签名URL只在构建Shotstack请求前为实际选中的对象生成，不写入任务数据库、日志或Qwen上下文。

## 12. Shotstack适配与渲染

`shotstack_v2.py` 只接受通过服务端校验且已解析素材的 `resolved_plan`：

- 场景、字幕、重点卡、素材、转场和画面运动转换为受控Timeline字段。
- 三种风格必须在卡片布局、字幕样式、B-roll密度、转场或镜头运动上形成可测试差异。
- HTML和样式只能由服务端模板生成。
- 所有素材必须是当前任务授权的HTTPS短期签名URL。
- 输出固定为MP4和目标1080p画布。
- Stage环境使用 `https://api.shotstack.io/edit/stage`。

提交成功后立即保存 `provider_job_id`。Worker每5秒查询一次状态，Webhook只用于加速唤醒。已有供应商任务ID的任务在服务重启后继续查询原任务，不重复提交。

Shotstack提交发生“请求已发出但响应超时”的不确定状态时不得自动再次POST；任务进入可人工核对的 `render_failed/submission_uncertain`，确认供应商未创建任务后才允许重试，防止重复渲染和重复费用。

## 13. Webhook、重试与恢复

### 13.1 自动重试

- COS读取、下载和状态查询类网络错误最多重试3次，指数退避。
- ASR状态查询最多重试3次，已取得ASR任务ID时复用原任务。
- Qwen结构错误只修复一次，不进行无限重试。
- 单个图片槽位最多生成2次，第二次仍失败则按槽位必需性失败或降级。
- Shotstack状态查询可重试；不确定的提交请求禁止自动重复POST。

### 13.2 用户重试

任务内部的自动阶段重试复用已完成的标准化媒体、ASR结果、生成素材和供应商任务ID，不重复扣点。`POST /api/v2/edit/jobs/{id}/retry` 只允许任务所有者操作白名单终态失败任务；它创建新的继任任务和新计费事务，可复用安全且仍有效的产物，但必须重新扣30点，不能复活已退款的旧任务。

### 13.3 服务重启恢复

- 已有 `provider_job_id`：恢复到 `rendering` 并查询原任务。
- 已完成ASR或素材阶段：从最后一个持久化检查点继续。
- 尚未产生可复用外部任务ID的运行中调用：按阶段重试规则处理。
- 无法安全恢复：进入对应失败状态并执行幂等退款。

## 14. 计费第一阶段

当前规则固定为：

```text
提交成功 -> 扣除30点
任务成功 -> 保持扣点
任务失败 -> 幂等退还30点
```

同一幂等提交不得重复扣点，同一失败任务不得重复退款。同一任务内部的自动阶段重试不重复扣点；终态失败后的用户重试会创建新任务并重新扣30点。数据库保留 `billing_state`、供应商计量和估算费用字段，为后续真正的 `hold -> capture -> release` 设计预留空间，但本阶段不修改认证服务的余额模型。

## 15. 成片质检

下载Shotstack成片后，至少执行：

1. HTTP状态、Content-Type、大小上限和最小有效文件大小检查。
2. FFprobe确认容器、时长、视频流、音轨和帧率可读取。
3. FFmpeg完整解码到空输出，任何解码错误即失败。
4. 输出必须为1080×1920或1920×1080。
5. 视频必须为H.264，音轨必须为AAC。
6. 成片与计划时长误差不得超过200ms。
7. 使用 `blackdetect` 检测超过300ms的连续异常黑帧。
8. 使用 `silencedetect` 检测全程静音，或源音频不存在但成片新增的连续3秒以上异常静音。
9. 供应商状态必须为成功，文件必须包含有效可变化视频帧，不能是错误占位文件。

质检失败的文件不得进入视频资产库；保存错误摘要、质检指标和阶段耗时后退款。质检成功后才转存最终COS键并写入用户视频资产库。

## 16. 配置变量

只提交配置变量名和非敏感示例，不提交实际值：

```text
AI_EDIT_V2_ENABLED
AI_EDIT_V2_DB
AI_EDIT_V2_WORKERS
AI_EDIT_V2_WEBHOOK_SECRET
AI_EDIT_V2_QWEN_MODEL
AI_EDIT_V2_ASR_MODEL
AI_EDIT_V2_IMAGE_PROVIDER
SHOTSTACK_API_BASE
SHOTSTACK_API_KEY
DASHSCOPE_API_KEY
COS_SECRET_ID
COS_SECRET_KEY
COS_REGION
COS_BUCKET
COS_SIGN_EXPIRE
```

测试环境默认 `AI_EDIT_V2_ENABLED=0`。正确的Shotstack Stage Key通过测试服务器安全配置注入，验证通过后才能开启；Production Key不得配置到测试环境。

## 17. UI设计边界

“AI智能剪辑”使用独立页面，至少包含：

- 选择平台口播视频/音频资产或上传外部视频/音频。
- 上传本次补充图片、选择历史素材。
- 三种剪辑风格和9:16/16:9比例。
- 自动字幕与自动补图开关，且开关必须真实影响后端请求和执行。
- 生成按钮内显示总价30点，按钮下显示单价说明。
- 阶段进度、预计等待提示、失败阶段、退款结果和可重试状态。
- 成片播放、下载和返回视频资产库。

页面不得展示供应商模型名、API Key、COS签名参数或内部异常堆栈。旧“一键剪辑”页面和交互必须通过回归测试保持不变。

## 18. 自动化测试

最低测试范围：

- V2数据库初始化不读取或修改旧 `ai_edit.db`。
- 用户不能引用其他账号的源视频和私有素材。
- 幂等提交只创建一个任务并只扣一次30点。
- 所有失败竞争路径最多退款一次。
- 平台原文正确覆盖ASR同音错字、品牌名和数字，时间戳仍单调有效。
- 低置信度对齐进入 `alignment_failed`。
- 外部视频/音频ASR断句修复不得改变字符序列。
- Qwen输出严格使用 `version=2.0`，禁止URL、COS键和Shotstack字段。
- `captions=false` 不生成VTT和字幕轨。
- `auto_assets=false` 不调用图片生成API。
- 四级素材优先级和账号权限正确。
- 生成图片完成COS入库和槽位回填后才进入Timeline。
- 三种风格产生可观测的Timeline结构差异。
- Webhook无密钥拒绝，重复和乱序事件不覆盖终态。
- 重启后复用ASR、图片和Shotstack任务ID。
- Shotstack不确定提交不会自动重复POST。
- FFmpeg标准化和九项成片质检分别有成功与失败测试。
- 旧“一键剪辑”入口、提交和结果展示回归通过。

## 19. 测试环境POC与验收

使用Shotstack Stage执行9次真实POC：

1. 30秒知识/诊断口播，`business_diagnostic`，执行3次。
2. 20秒产品讲解，`product_story`，选择用户产品图片，执行3次。
3. 音频主导的叙事口播，`story_broll`，允许自动生成缺图，执行3次。

每次记录任务ID、各阶段耗时、第三方请求ID、供应商计量、估算费用、扣点/退款、最终COS键和质检指标，不记录密钥与签名URL。

通过标准：

- 至少8/9次无需人工干预完成。
- 成功任务全部通过质检、进入当前用户视频资产库并可播放下载。
- 平台口播字幕中的品牌名、数字和产品名与原文一致。
- 三种风格在布局、素材占比、字幕或镜头节奏上有明显差异。
- 用户素材优先和AI缺图生成各至少验证2次。
- 失败任务错误阶段准确且只退款一次。
- 旧“一键剪辑”功能不受影响。

POC未通过前保持 `AI_EDIT_V2_ENABLED=0` 的默认状态，不提交生产上线申请。

## 20. 非目标

- 本阶段不开发图片、文案、视频的内容安全审核。
- 本阶段不实现真正的点数冻结、确认和释放。
- 不迁移旧 `ai_edit.db` 或历史HyperFrames任务。
- 不修改PR #20的V1协议和实现。
- 不接入Creatomate、Remotion或其他第二渲染器。
- 不自动生成背景音乐或音效，`audio_cues` 第一阶段保持空数组。
- 不部署生产环境。

## 21. 分支、提交与实施所有权

- 本设计和后续实现使用独立分支 `codex/ai-edit-v2`。
- 本设计文档先单独提交，用户复核通过后再编写详细实施计划。
- 后续只允许一个任务负责V2实现；PR #20审查任务不修改V2文件。
- 实施必须按任务拆分测试和提交，先失败测试、再最小实现、再运行验证。
- 测试部署只能从已推送的V2提交选择性部署本次修改文件，不允许服务器热改或整站覆盖。
