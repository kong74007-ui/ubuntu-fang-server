# AI 智能剪辑 V3 设计规格

## 1. 文档状态

- 产品名称：AI 智能剪辑 V3
- 设计日期：2026-07-30
- 设计状态：已完成逐节用户确认，等待用户复核书面规格
- 开发范围：测试环境开发与可行性验证
- 输出规格：1080p、H.264/AAC、MP4
- 源需求：《AI智能剪辑第一版开发方案》V1.0

本文定义一个与现有 AI 智能剪辑 V2 完全隔离的新模块。V3 聚焦两条首发链路：平台口播视频智能包装，以及基于音频的无口型讲解视频生成。V3 使用 Qwen3.7-Max 生成结构化导演方案，以 HyperFrames 和 GSAP 进行确定性编排与渲染。

本文是 V3 第一版的设计基线。实施计划、功能代码、测试环境部署、生产环境部署和真实生产计费分别经过独立审批。本文获批不授权生产部署、生产数据库迁移或生产功能开启。

## 2. 决策摘要

1. V3 独立于 V2，使用独立页面、API、数据库、表、Worker、COS 前缀、计费幂等键、日志前缀和功能开关。
2. V2 的页面、Shotstack 渲染、任务、数据、资产和运行服务保持不变。
3. 导演与素材理解统一使用 `qwen3.7-max-2026-06-08`，不再使用 `qwen-plus`，也不允许静默降级到其他导演模型。
4. Qwen 只输出语义导演方案、素材需求、组件 ID、动画预设和声音意图；Qwen 不生成或执行 HTML、CSS、JavaScript、GSAP 或 HyperFrames 代码。
5. V3 沿用 `edit-plan 2.0` 作为导演中间协议。产品版本 V3 与协议版本 2.0 是两个独立版本号。
6. 渲染只使用 HyperFrames 0.7.84 和 GSAP 3.15.0 的固定版本，不使用 Shotstack、Remotion 或 ChatCut。
7. 每次只读取用户本次上传的最多 10 张图片；不得读取用户历史素材或平台公共素材。
8. 缺少合适图片时调用网站现有生图 API；第一版不生成 AI 短视频素材。
9. 每条任务都由 ElevenLabs 新生成无歌词 BGM 和必要音效，第一版不消费历史音频库；FFmpeg 负责 ducking、混音和响度标准化。
10. 渲染进程只读取冻结清单和本地素材，不持有供应商密钥，渲染期间禁止访问外部网络。
11. 普通任务创作与交付目标耗时 10 至 25 分钟，从预扣成功开始计时；无修复上限 45 分钟，明确可修复的质检问题只允许追加一次最多 10 分钟，总上限 55 分钟。外部点数账本或共享资产发布裁决持续返回未知结果是仅有的两个安全性 SLA 例外，必须在预算内停止媒体处理并显式进入待确认状态，不能伪装为已退款或已发布。
12. 动态报价按上限预扣，成功后按实际费用结算并退差额，最终失败幂等全额退款。
13. 测试环境使用 20 条真实差异化样本验收；达到门槛后再单独决定是否进入生产评审。

## 3. 产品目标

### 3.1 用户目标

用户无需编辑时间线或理解专业剪辑参数，只需要选择主视频或主音频、填写可选风格要求、上传可选图片并确认报价。系统自动交付一条可播放、可下载的成片。

### 3.2 业务目标

- 将平台已生成的数字化 IP 口播视频包装为更接近人工商业短视频剪辑的成片。
- 将已有音频或平台 TTS 音频转换为无口型的讲解视频。
- 让每条视频的镜头结构、布局和动画服务于实际内容，而不是只替换模板颜色。
- 保留完整任务审计、成本、素材来源、渲染清单和质检证据。
- 在不影响 V2 的前提下验证 HyperFrames 服务化渲染的稳定性和可发布率。

### 3.3 第一版明确不做

- AI 短视频素材生成。
- 重新生成人物口播画面或重新生成口型。
- 用户在线编辑时间线。
- 用户上传模板代码或自定义插件。
- Qwen 生成并执行代码。
- 任意代码 MG、Shader、WebGL、Three.js、3D 或 Canvas 自由绘制。
- 用户历史素材库检索。
- 平台公共图片或视频素材库检索。
- 参考视频学习与复刻。
- 内容安全审核；该能力作为生产上线前的已知风险项单独设计。
- 生产部署、生产数据迁移和生产真实计费启用。

## 4. 版本隔离

### 4.1 独立资源

| 资源 | V3 值 |
| --- | --- |
| 开发分支 | `codex/ai-edit-v3`，从最新 `origin/main` 创建 |
| 用户页面 | `site/workbench/ai-edit-v3.html` |
| API 前缀 | `/api/v3/edit/*` |
| 数据库 | `AI_EDIT_V3_DB_PATH` 指向的独立绝对路径，测试默认文件名 `ai_edit_v3.db` |
| 数据表前缀 | `edit_v3_*` |
| COS 前缀 | `{environment}/ai-edit-v3/{owner_hmac}/{job_id}/...` |
| Worker | `server/ai_edit_v3_worker.py` |
| systemd 服务 | 控制 Worker `huangque-ai-edit-v3.service`；无网络渲染沙箱 `huangque-ai-edit-v3-render@.service` |
| 功能开关 | `AI_EDIT_V3_ENABLED=0` |
| 计费幂等键 | `ai-edit-v3:*` |
| 视频资产模式 | `ai_edit_v3` |
| 前端任务类型 | `ai_edit_v3` |
| 日志前缀 | `[ai-edit-v3]` |

### 4.2 与 V2 的关系

V3 不导入、修改或迁移 `ai_edit_v2.db`。V3 Worker 不领取 V2 任务，V2 Worker 不领取 V3 任务。V3 不调用绑定 V2 Store、V2 COS 前缀或 V2 幂等键的供应商适配器。

允许复用的只有稳定公共接口和经过重新封装的通用算法，例如：

- 登录认证和账号归属校验。
- 平台点数服务接口。
- `video_assets` 最终资产库写入接口。
- 数字化 IP 口播来源查询接口。
- FFprobe、FFmpeg 调用模式。
- 原文与 ASR 时间戳的确定性对齐算法。
- SQLite WAL、任务租约、幂等检查点和退款设计原则。

所有复用必须通过 V3 适配层，并使用 V3 自己的数据库、对象路径和幂等命名空间。

### 4.3 共享接点

V3 最终仅以最小增量接入以下公共文件：

- `server/content_domains/core.py`：注册 V3 API 处理器。
- `server/content_domains/video.py`：为 V3 私有成片生成新的短期播放和下载地址。
- `site/workbench/cloud-shell.js`：受服务端能力开关控制的入口。
- `site/workbench/tasks.js`：恢复和跳转 V3 任务。
- `server/admin_api.py`：V3 独立价格版本管理。
- `site/admin/index.html`：V3 价格管理入口。
- `deploy/huangque-secrets.env.example`：仅增加变量名和占位说明，不写真实密钥。

公共文件按仓库协作组边界拆分 PR，不把多个协作组的公共接点和全部 V3 业务代码塞入同一审查单元。

## 5. 用户流程与页面

### 5.1 页面原则

- 单页完成创作配置和任务查看。
- 不展示供应商、模型密钥、渲染器字段或可编辑时间线。
- 主体资产卡片只加载封面，用户在右侧预览区主动播放时才加载完整视频。
- 页面切换主体资产时，不重新加载未播放的视频。
- 用户看到的任务状态使用业务语言，不暴露内部阶段代码。
- 第一步的数字化 IP 口播使用单排横向滚动的竖屏封面卡；卡片不创建 `<video>`，选择只更新右侧封面和元数据，主动点击右侧播放后才创建并加载播放器。
- 第二步的平台模板使用紧凑的 `9:16` 预览图片卡横向选择，不使用下拉框，也不加载模板视频。
- 第三步只叫“补充素材”，使用 `1:1` 加号上传框和图片缩略图；不出现“参考素材”窗口，单任务最多 10 张。
- 页面右侧固定显示小尺寸主预览、当前选择摘要、报价、任务状态和唯一开始按钮；不要求用户确认复杂计费复选框。

主体输入选定后，第二步只提供三个互斥创作入口：

| `creation_mode` | 页面行为 | 导演约束 |
| --- | --- | --- |
| `ai_auto` | 无需额外输入，AI 按内容自由判断 | Qwen 在完整能力白名单内选择主题、布局、变体和节奏 |
| `style_prompt` | 用户输入自然语言风格要求 | 系统先规范化和安全包裹提示词，Qwen 在不改事实、不越白名单的前提下完善为完整方案 |
| `template_reference` | 用户选择平台已发布的可用模板预览卡 | 模板冻结视觉语法和风格边界，Qwen 仍按内容选择场景、布局变体和节奏；同模板只保持风格统一，不要求逐条完全相同 |

用户不能编辑时间线、上传模板代码或让模型执行自定义代码。模板具有版本号、预览图、支持比例、组件能力和发布状态；报价与任务冻结 `template_id` 和版本。第一版不支持上传任意参考视频学习风格。

首发必须随系统初始化至少 4 个已发布模板：至少 2 个支持 `16:9`、2 个支持 `9:16`，并至少覆盖“商业诊断”和“编辑式知识讲解”两类视觉方向。每个模板必须提供预览图、至少 2 个布局结构变体、组件能力合同和可通过的横/竖屏快照；`GET /templates` 在功能开启时不得返回空列表。

### 5.2 口播视频剪辑

1. 用户选择“口播视频剪辑”。
2. 页面只列出当前账号已完成且来源可验证的数字化 IP 口播视频。
3. 用户也可以上传一个新视频作为外部视频输入。
4. 页面不要求用户重新选择比例；服务端根据主视频方向确定 `16:9` 或 `9:16` 标准输出。横向视频使用 `16:9`，纵向视频使用 `9:16`，正方形使用 `16:9`。非标准比例默认完整保留主体并使用模糊或主题背景补边，不执行可能裁掉人物、产品或字幕的自动满幅裁切。
5. 用户选择 AI 自动、自然语言要求或平台模板三种创作入口之一。
6. 用户可上传 0 至 10 张图片。
7. 用户获取报价并开始创作。

### 5.3 音频生成视频

1. 用户选择“音频生成视频”。
2. 用户从三种互斥来源选择一种：已有音频资产、上传音频、输入文案并选择已有音色生成音频。
3. 文案加音色模式只允许选择当前账号有权使用且状态正常的克隆音色或平台通用音色；V3 不负责新建音色克隆。
4. 文案、音色和预估 TTS 成本在报价时冻结；任务开始后由 V3 通过网站现有语音生成服务产生主音频。
5. 若音频来自用户输入文案和音色生成，用户输入文案是准确文本，服务端不接受客户端在任务创建后替换正文。
6. 用户选择 `16:9` 或 `9:16`，默认 `16:9`。
7. 用户选择 AI 自动、自然语言要求或平台模板三种创作入口之一。
8. 用户可上传 0 至 10 张图片。
9. 用户获取报价并开始创作。

### 5.4 页面状态

内部状态映射为以下用户文案：

| 内部阶段 | 用户状态 |
| --- | --- |
| `created_draft`、`preholding`、`queued` | 已提交，等待处理 |
| `generating_voice` | 正在生成主音频 |
| `normalizing`、`transcribing`、`aligning` | 正在分析内容 |
| `planning` | 正在生成剪辑方案 |
| `resolving_materials`、`generating_images` | 正在准备画面素材 |
| `generating_audio`、`mixing_audio` | 正在设计声音 |
| `compiling`、`rendering` | 正在生成视频 |
| `quality_checking`、`repair_planning` | 正在检查成片 |
| `billing_reconciling`、`failed_reconciliation_pending`、`refund_pending` | 正在核对点数 |
| `asset_decision_reconciling`、`failed_asset_decision_pending` | 正在核对成片保存状态 |
| `staging_delivery`、`settling`、`publishing` | 正在保存成片 |
| `completed` | 创作完成 |
| `prehold_absent` | 创作失败，确认未扣点 |
| `failed`、`refunded` | 创作失败，点数已退还或正在退还 |

终态任务不得锁死下一次创作。页面可查看历史任务，但只允许一个当前创作上下文控制当前按钮和进度。

## 6. 总体架构

```text
用户页面
  -> V3 API：鉴权、校验、报价、预扣、任务创建
  -> V3 SQLite：任务、租约、检查点、审计、计费意图
  -> Python Worker
       -> 可选：网站现有语音服务生成主音频
       -> FFprobe / FFmpeg 标准化
       -> fun-asr 与确定性文本对齐
       -> Qwen3.7-Max 素材理解与导演方案
       -> 本次图片匹配 / 网站生图 API
       -> ElevenLabs BGM / SFX
       -> FFmpeg 生成唯一最终混音母带
       -> 冻结 edit-plan 2.0 与 render manifest
       -> 无网络 Node.js HyperFrames 渲染无声画面
       -> FFmpeg 按原始时间基合并唯一母带音轨
       -> 技术、画面、声音、内容质检
       -> 一次针对性修复或交付
  -> 私有 COS
  -> 用户视频资产库
  -> 网页播放、MP4 下载和站内任务通知
```

### 6.1 Python 控制层

Python 负责所有包含身份、密钥和持久状态的操作：

- 鉴权、归属和输入校验。
- COS 上传、下载和交付。
- ASR、Qwen、生图和 ElevenLabs 调用。
- edit-plan 校验、素材解析和 render manifest 冻结。
- 任务租约、断点恢复和绝对超时。
- 点数预扣、结算和退款。
- 质检、一次修复和最终资产入库。

### 6.2 Node.js 渲染层

Node.js 渲染程序是一个本地、无密钥、无网络的确定性执行器：

- 输入：任务 ID、冻结后的 render manifest 路径、本地只读素材根目录。输出目录只由 Python 创建并作为独立进程参数传入，不允许 manifest 再声明输出路径。
- 输出：MP4、结构校验报告、关键帧截图、渲染日志和性能统计。
- 不读取 V3 数据库。
- 不调用 COS、DashScope、ElevenLabs 或生图 API。
- 不接受自然语言或 Qwen 原始输出。
- 不执行 render manifest 之外的动态脚本。
- 不支持插件下载和运行时网络资源。

### 6.3 渲染依赖

- Node.js：22.x。
- HyperFrames：`0.7.84`。
- GSAP：`3.15.0`。
- Chromium：固定可复现版本并随内容寻址的渲染发布包交付。
- FFmpeg、FFprobe：固定版本并在启动时报告版本。
- 字体：随渲染包发布并记录字体文件 SHA-256，不依赖系统临时字体。

升级上述依赖时必须单独执行组件快照、关键帧和整片回归，不允许使用 `latest` 在运行时自动升级。

### 6.4 外部能力与启动预检

V3 只引用服务端变量名，不在代码、文档或数据库写入真实密钥：

- DashScope：`DASHSCOPE_API_KEY`、`DASHSCOPE_WORKSPACE`，固定 Qwen 多模态端点和模型快照。
- ElevenLabs：`ELEVENLABS_API_KEY`，只授予音乐生成和音效生成所需权限。
- COS：复用站点私有 COS 适配接口，但使用测试环境专属凭据和 V3 前缀权限。
- fun-asr、网站现有 TTS、网站现有生图：通过各自现有服务端适配接口调用，V3 不复制它们的密钥或绕过 owner 检查。

启动预检分别报告“已实现”“已配置且已接线”“缺失或不可用”，不得把只存在环境变量但未接线的 Provider 宣称为可用。任一当前任务必需能力缺失时，`GET /capabilities` 返回明确禁用原因，创建和预扣 fail closed；不影响 V2。

## 7. 模块结构

```text
server/content_domains/ai_edit_v3/
  __init__.py
  api.py
  contracts.py
  store.py
  pipeline.py
  runtime.py
  media.py
  transcript.py
  director.py
  materials.py
  audio.py
  quality.py
  delivery.py
  billing.py
  feature.py
  providers/
    base.py
    dashscope.py
    image_generation.py
    elevenlabs.py
  renderers/
    hyperframes.py
  schemas/
    edit-plan-2.0.schema.json
    render-manifest-v1.schema.json
    quality-verdict-v1.schema.json

server/ai_edit_v3_worker.py

server/ai_edit_v3_renderer/
  package.json
  package-lock.json
  src/
    render.mjs
    validate-manifest.mjs
    compile-project.mjs
    registry/
      layouts.mjs
      overlays.mjs
      animations.mjs
      transitions.mjs
      themes.mjs
  assets/
    fonts/
  test/

site/workbench/ai-edit-v3.html
site/assets/ai-edit-v3/
tests/test_ai_edit_v3_*.py
tests/test_ai_edit_v3_ui.js
tests/fixtures/ai_edit_v3/
deploy/systemd/huangque-ai-edit-v3.service
deploy/systemd/huangque-ai-edit-v3-render@.service
```

每个模块只依赖公开接口，禁止 Python 业务模块导入 Node 渲染器内部实现。`renderers/hyperframes.py` 是控制层与渲染层的唯一边界。

模块职责固定如下，禁止通过循环依赖绕过边界：

| 模块 | 唯一职责 | 禁止事项 |
| --- | --- | --- |
| `api.py` | HTTP、鉴权、DTO 和 owner 边界 | 不直接调用供应商、账务或渲染器 |
| `pipeline.py` | 唯一状态转换与阶段编排者 | 不直接拼接 SQL 或供应商 HTTP |
| `store.py` | 唯一 V3 数据库访问层和事务实现 | 不调用外部网络 |
| `runtime.py` | 配置、依赖注入、启动预检和版本报告 | 不承载业务阶段 |
| `billing.py` | 点数意图、outbox、对账和幂等账务接口 | 不发布视频资产 |
| `delivery.py` | 私有 COS、不可变成片对象和资产发布 | 不直接修改点数账本 |
| `quality.py` | 生成规范化质检证据和报告 | 不自行改变任务状态或决定重试 |
| `providers/*` | 网络协议适配和响应规范化 | 不访问数据库或跨调用缓存用户数据 |
| `renderers/hyperframes.py` | 唯一受控子进程边界 | 不承担导演、素材选择或账务职责 |

Python 控制 Worker 使用需要网络的 `huangque-ai-edit-v3.service`。每次渲染由它启动独立的 `huangque-ai-edit-v3-render@.service` 沙箱单元；渲染单元使用固定发布包、无网络和独立非特权用户。第一版不依赖运行时下载容器或 npm 包。

## 8. 输入校验与媒体标准化

### 8.1 账号和对象归属

- 平台口播资产必须属于当前账号。
- 平台口播必须能追溯到已完成且来源类型受允许的数字化 IP 生成任务。
- 客户端只提交资产 ID，不提交权威文案、COS Key 或供应商 URL。
- 所有上传记录和素材记录按 owner 隔离。
- 不属于当前账号的对象统一按不存在处理，避免泄露资产存在性。

### 8.2 第一版容量规则

- 主视频：一个。
- 主音频：一个。
- 文案加音色模式在语音生成完成前没有主音频文件，但冻结的文案和音色 ID 共同构成主输入，且与已有音频、上传音频互斥。
- 主视频和主音频互斥；口播视频的原声音属于主视频，不作为第二个主输入。
- 图片：最多 10 张。
- 图片格式：JPEG、PNG、WebP。
- 主视频格式：服务端最终以 FFprobe 检测结果为准，不信任扩展名或客户端 MIME。
- 主音频格式：服务端最终以 FFprobe 检测结果为准。
- 单张图片最大 25 MB。
- 单任务上传总量最大 1 GiB。
- 主视频或主音频时长范围为 3 秒至 10 分钟；超过范围在预扣前拒绝。测试样本超过 10 分钟时应先作为后续容量扩展单独评审，不得绕过限制。
- 主视频最大边长 4096 像素、最大 60 fps；编码参数导致 FFprobe 或首段解码超过 30 秒时按 `input_decode_complexity_exceeded` 拒绝。
- 单张图片最大边长 12000 像素且解码后总像素不超过 80 MP；解码在受限子进程中完成，超过内存或 10 秒解码预算时拒绝，防止压缩炸弹。
- 中文为第一版唯一导演语言。

### 8.3 标准化输出

必要时 FFmpeg 将媒体转换为：

- 视频：H.264、恒定帧率、`yuv420p`。
- 音频：AAC 交付轨；中间处理使用 48 kHz PCM 或无损容器。
- 声道：双声道交付，语音来源按需要居中。
- 旋转：物理应用旋转信息，避免后续宽高解释不一致。
- 时间基：统一、单调、可由毫秒安全换算为帧。

FFprobe 结果和执行命令参数列表写入阶段审计；日志不包含签名 URL 或密钥。

## 9. 文本与时间轴

### 9.1 平台口播视频

- 平台原始口播文案是准确文本。
- fun-asr 只提供逐字和逐句时间戳。
- 确定性文本对齐算法将原文映射到 ASR 时间轴。
- 品牌名、产品名、数字和价格以原文为准。
- Qwen 不得改写原口播内容。
- 对齐覆盖率低、时间倒退或无法建立单调映射时，任务失败，不使用错误 ASR 文本替代原文。

### 9.2 TTS 音频

- 用户输入文案是准确文本。
- V3 通过网站现有语音生成服务调用已选择的可用音色，不在 V3 内实现新的音色克隆算法。
- 生成请求保存冻结文案哈希、音色 ID、owner、provider task ID、实际模型、耗时和用量。
- 服务端从权威 TTS 任务记录读取文案、音色和音频归属。
- 若 TTS Provider 已提供可信时间戳，则规范化后使用；否则对生成音频执行 ASR 并将原文确定性对齐到时间戳。
- 画面不得伪造人物口型。

### 9.3 外部视频或音频

- fun-asr 同时负责文本识别和时间戳。
- 可执行确定性的标点和断句清理。
- Qwen 只能修复标点和断句，不得改变词语、数字、品牌名、价格或原意。
- 清理前后去除标点后的字符序列必须一致，否则拒绝结果。

### 9.4 时间约束

- 所有时间在协议中使用整数毫秒。
- 场景必须从 0 开始、连续、无重叠、无间隙并结束于 `duration_ms`。
- 字幕时间必须位于所属场景和总时长内。
- 帧转换只在冻结 render manifest 时执行，使用统一舍入规则。

### 9.5 内容重剪与自适应时长

- 页面第一版不要求用户填写目标时长，输出时长由内容结构自适应决定。
- Qwen 可以提出保留或删除完整语义段，但不能改写词语。确定性编译器把建议映射回对齐后的源区间，并只在句间停顿、完整短语边界和可接受画面切点落刀。
- 保留片段维持原始先后顺序，不通过重排制造原文没有的因果关系。删除品牌、产品、数字、价格所在句时必须把整句作为可审计删除段处理，不得拼接出新的事实表达。
- 每个保留片段记录 `source_start_ms/source_end_ms` 与重排后的 `output_start_ms/output_end_ms`；字幕、口播画面和人声都使用同一映射，保证跳剪不造成音画漂移。
- 冻结的 `duration_ms` 是保留片段合并后的输出时长。60 秒输入可能根据重复、停顿和内容完整性生成较短成片，但系统不承诺固定压缩到 30 秒。
- 第一版不调用克隆音色补录、不插入模型新写的人物台词，也不生成人物口播画面。若内容无法在不改意的前提下缩短，就保留完整内容。

## 10. Qwen3.7-Max 导演

### 10.1 模型配置

- Provider：阿里云百炼 / DashScope。
- 固定模型：`qwen3.7-max-2026-06-08`。
- 固定调用面：阿里云中国站 `cn-beijing` 地域的 DashScope `MultiModalConversation`，REST 路径为 `https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation`；`WorkspaceId` 只从服务端 `DASHSCOPE_WORKSPACE` 读取，必须通过严格的阿里云 Workspace ID 字符集和长度校验后才能构造主机名。不得接受完整端点覆盖值，不得复用 V2 的 `text-generation` 路径。启动预检必须使用配置的真实端点执行一次不含用户数据的最小多模态调用，并校验地域、Workspace、API Key、请求格式和模型快照；任一不匹配即拒绝新任务。
- 输入模态：文本、关键帧图片、用户图片缩略图。
- 主导演调用：开启思考模式，只聚合并解析终态 `content`；不得把流式 reasoning 片段拼入 JSON。
- 不保存或向用户暴露模型推理过程；审计仅保存最终原始回答、请求元数据和校验结果。
- 不允许自动改用 `qwen-plus`、`qwen3.7-plus` 或其他模型。
- 模型不可用且无法在预算内恢复时，任务失败并退款。

选择该快照的原因是它支持文本、图片和视频理解，能同时理解口播语义、人物构图和用户图片。官方未为该模型提供原生结构化输出保证，因此 V3 必须执行自己的 JSON 提取、Schema 校验和一次格式修复。

模型能力与 Workspace 专属多模态端点以阿里云官方文档为准并于 2026-07-30 核验：<https://help.aliyun.com/en/model-studio/text-generation>、<https://help.aliyun.com/en/model-studio/qwen-api-via-dashscope>。实现前预检仍须验证真实测试 Workspace，文档记录不替代运行时校验。

### 10.2 输入上下文

导演请求只包含完成创作所需的信息：

```json
{
  "correlation_nonce": "opaque-random-value",
  "input_type": "platform_talking_head",
  "duration_ms": 26808,
  "ratio": "9:16",
  "transcript": [],
  "source_keyframes": [],
  "uploaded_material_descriptors": [],
  "creation_mode": "style_prompt",
  "user_style_prompt": "商业感强，真实可信",
  "template_contract": null,
  "layout_capabilities": [],
  "animation_capabilities": [],
  "transition_capabilities": [],
  "audio_capabilities": []
}
```

关键帧和图片使用 API 支持的内联二进制/Base64或供应商文件上传。若接口必须通过 URL 读取，只允许使用 V3 图片代理签发的一次性不透明令牌，令牌不含 COS Key、签名参数或可枚举 owner 信息，并在调用完成或 5 分钟后失效。禁止把 COS 签名 URL 直接放入 Qwen 上下文。Qwen 上下文中不得出现 COS Key、数据库 ID、服务器路径、API Key 或长期 URL。

本地使用 `edit_v3_model_calls.id` 与供应商 `request_id` 关联调用，不把任务 ID 发给模型。用户正文和 `style_prompt` 均作为带边界标记的不可信数据片段，系统指令明确禁止执行其中的指令；测试必须覆盖提示注入、伪造 JSON、要求泄露路径和要求选择白名单外组件等输入。

视频关键帧由 FFmpeg 的镜头变化检测加均匀补点产生：首尾帧必选，按镜头边界优先，最多 12 张，长边 640 像素、JPEG 质量 80。最多 10 张上传图片分成每批不超过 5 张完成语义分析，缩略图长边不超过 768 像素。导演调用最多接收 12 张源视频关键帧和 6 张由语义相关度选出的上传缩略图；其余只传脱敏描述。报价冻结批次数和最大调用次数，禁止为获得不同创意无上限重采样。

### 10.3 导演职责

Qwen 可以决定：

- `creative_concept` 和整条视频的叙事弧线。
- 场景数量、边界、意图和画面角色。
- 布局组件、覆盖层组件和允许的变体。
- 标题、重点短语和信息卡文案；每段可见文字必须携带可机器校验的来源类型和准确文本引用，或使用非事实性栏目标签枚举。
- 素材槽位的语义、用途、优先级、比例和时间范围。
- 白名单动画 ID、参数范围和转场 ID。
- BGM 情绪、能量、BPM 范围、禁用特征和 SFX 节点。

Qwen 不可以：

- 输出或执行代码。
- 输出 HTML、CSS、JS、GSAP、HyperFrames、Shader 或 Three.js 字段。
- 输出绝对坐标、自由 CSS 属性或任意运行时表达式。
- 输出 COS Key、签名 URL、本地路径或数据库字段。
- 修改准确文本中的事实、品牌、产品、价格和数字。
- 选择白名单外的布局、动画、转场或音频能力。

### 10.4 调用与修复

1. 主调用请求严格 JSON 文本。
2. 后端限制终态回答最大 512 KiB、JSON 最大深度 24、数组元素总数 5000、单字符串 4000 字符；拒绝重复键、`NaN`、`Infinity`、多个对象、尾随内容或夹带可执行片段。
3. 后端按冻结的机器可读 Schema 执行类型、必填字段、`additionalProperties: false`、枚举、时间轴、文本事实和能力白名单校验。
4. 校验失败时，将脱敏后的字段路径和错误代码交给同一模型修复一次。
5. 修复请求不得引入新的素材、文本事实或能力。
6. 第二次仍不合法时进入 `director_failed`，全额退款。

已经收到明确响应的逻辑调用不自动重复。建立连接前失败、429 或明确未受理的 5xx 可以按退避策略有限重试；请求体已发送但结果不确定且供应商没有幂等或结果查询能力时进入 `provider_unknown`，不得盲目重发。该任务失败并全额退款，平台异步核对并承担可能重复或未确认的供应商成本。

### 10.5 审计

每次调用至少记录：

```json
{
  "provider": "dashscope",
  "model": "qwen3.7-max-2026-06-08",
  "request_id": "provider-request-id",
  "purpose": "director_primary",
  "prompt_version": "ai-edit-v3-director-v1",
  "request_schema_sha256": "sha256:...",
  "response_schema_sha256": "sha256:...",
  "raw_final_output": {},
  "normalized_plan": {},
  "elapsed_ms": 38200,
  "token_usage": {},
  "validation_errors": []
}
```

不记录 API Key、签名 URL、内部推理过程或未脱敏用户凭据。

## 11. edit-plan 2.0

### 11.1 顶层结构

```json
{
  "version": "2.0",
  "duration_ms": 26808,
  "ratio": "9:16",
  "creative_concept": "真实舞台证据与商业方法资产",
  "theme": {},
  "narrative_arc": [],
  "captions": [],
  "source_segments": [],
  "scenes": [],
  "materials": [],
  "audio_cues": []
}
```

### 11.2 场景结构

```json
{
  "id": "scene_01",
  "start_ms": 0,
  "end_ms": 5580,
  "intent": "打破错误认知",
  "layout_id": "speaker_right_evidence_left",
  "layout_variant": "balanced_a",
  "visual_type": "belief_reversal",
  "headline": {
    "text": "不是不够努力",
    "text_kind": "verbatim",
    "source_caption_ids": ["caption_01"]
  },
  "highlight": {
    "text": "是方法不对",
    "text_kind": "compressed",
    "source_caption_ids": ["caption_02", "caption_03"]
  },
  "overlay_ids": ["headline_block", "evidence_label"],
  "material_slots": [
    {
      "id": "slot_01",
      "semantic": "人物舞台演讲",
      "purpose": "evidence",
      "priority": "required",
      "ratio": "9:16",
      "start_ms": 600,
      "end_ms": 5200
    }
  ],
  "animations": [
    {
      "target": "headline_block",
      "preset": "slide",
      "direction": "left",
      "duration_ms": 420,
      "delay_ms": 120
    }
  ],
  "transition": "hard_cut"
}
```

### 11.3 协议规则

- 所有 ID 只能使用小写 ASCII、数字和下划线。
- 文本字段必须有长度上限并进行控制字符过滤。
- 场景引用的组件、覆盖层和动画必须存在于冻结能力清单中。
- `required` 素材槽未解析成功时不得继续渲染。
- `optional` 素材槽可以按明确降级规则省略，降级原因写入计划。
- Qwen 输出的 `materials` 仅描述语义需求，不包含真实资产标识。
- render manifest 才包含真实本地素材路径；该清单永远不回传给 Qwen。
- 除逐字字幕外，所有标题、重点词、信息卡、引用和 CTA 文本都使用统一 `visible_text` 联合：
  - `verbatim`：`text` 必须等于所引用 `source_caption_ids` 的连续准确文本片段。
  - `compressed`：必须引用一个或多个连续字幕 ID；允许压缩措辞，但品牌、产品、人物、数字、价格、否定词和因果方向必须逐项保持，且不得新增承诺。
  - `ui_label`：不允许自由 `text` 或事实引用，只允许 `ui_label_id` 取 `chapter`、`step`、`category`、`evidence_marker`、`cta_prompt`，最终中文由编译器固定映射。

### 11.4 机器可读 Schema 是唯一权威

阶段 A 必须创建并冻结以下两个编排 Schema，本文示例只用于说明，不能替代它们：

- `server/content_domains/ai_edit_v3/schemas/edit-plan-2.0.schema.json`
- `server/content_domains/ai_edit_v3/schemas/render-manifest-v1.schema.json`

编排 Schema 使用 JSON Schema 2020-12，根对象和所有嵌套对象均设置 `additionalProperties: false`，并完整定义类型、必填项、最小/最大长度、枚举、数值范围、数组数量和唯一性。实现同时执行 Schema 无法表达的交叉约束：场景连续覆盖总时长、字幕落在场景内、引用 ID 存在、组件能力匹配、required 槽位解析完成、准确文本事实不变、音频 cue 不越界、横竖屏尺寸与比例一致。第三个 `quality-verdict-v1.schema.json` 在第 21.5 节定义，用于约束视觉模型质检。

`edit-plan 2.0` 至少对 `theme`、`narrative_arc`、`captions`、`scenes`、`materials` 和 `audio_cues` 给出非占位定义；每类都必须定义最大数量和完整降级结构。`render-manifest-v1` 必须对媒体、场景、组件实例、动画、转场、字幕、唯一母带音轨、文件哈希和执行环境指纹给出可校验结构。Schema 本身运行元 Schema 校验，并用合法样例、逐字段非法样例和模糊输入测试。

每个导演调用审计、规范化计划、render manifest 和 quality verdict 都记录其输入/输出 Schema SHA-256；任务环境指纹记录三份 Schema 的完整哈希集合。Worker 启动时校验 Schema 哈希与代码支持版本一致；不一致时 fail closed，不接受新任务。

首发 `edit-plan 2.0` 字段合同固定为：

| 字段 | 必填与上限 | 核心约束 |
| --- | --- | --- |
| `version` | 必填 | 常量 `2.0` |
| `duration_ms` | 必填整数，3000–600000 | 必须等于冻结主时间线 |
| `ratio` | 必填 | 仅 `16:9` 或 `9:16` |
| `creative_concept` | 必填字符串，1–240 字符 | 不得新增事实性承诺 |
| `theme` | 必填对象 | 必含 `palette_id`、`typography_id`、`density`、`motion_energy`、`image_fit`，值均来自能力清单 |
| `narrative_arc` | 必填数组，1–12 项 | 每项含 `id`、`role`、`start_ms`、`end_ms`、`summary`；`role` 仅允许 `hook/problem/evidence/method/offer/cta` |
| `captions` | 必填数组，1–2000 项 | 每项含 `id/start_ms/end_ms/text/emphasis`，按准确文本顺序且时间单调 |
| `source_segments` | 必填数组，1–240 项 | 每项含源/输出起止时间、准确文本范围和 `keep_reason`；源顺序单调、输出连续 |
| `scenes` | 必填数组，1–120 项 | 每场至少 500 ms；含第 11.2 节字段；全部可见文案符合 `visible_text` 联合，素材槽最多 4 个、动画最多 8 个 |
| `materials` | 必填数组，0–40 项 | 只描述 `request_id/semantic/purpose/priority/ratio/time_range`，不含真实资产 ID |
| `audio_cues` | 必填数组，0–64 项 | 只允许 `bgm/sfx/volume_fade` 类型、受控参数和合法时间范围 |

首发 `render-manifest-v1` 根字段固定包含 `version`、相关 Schema SHA、`renderer_environment`、`output_spec`、`duration_ms`、`edit_plan_sha256`、`registry_sha256`、`theme`、`seed`、`source_video`、`source_segments`、`master_audio`、`assets`、`compositions` 和 `captions`。其中 `source_video` 可以为空但若存在必须声明静音；`source_segments` 是画面与人声共同的唯一剪辑映射；`master_audio` 必须唯一；所有媒体只允许相对路径和 SHA；`compositions` 只能引用注册表中的组件、视觉动画与转场。

## 12. 创意组件语法

### 12.1 设计原则

V3 不使用一套固定模板，也不允许 Qwen 自由编程。系统提供可组合、可参数化、可测试的视觉语法。Qwen 为每条内容选择创意概念、布局组合、视觉节奏和动画顺序，编译器将选择转换为确定性 HyperFrames 工程。

同一任务内通过冻结主题保持统一；不同任务通过内容选择、组件变体、设计令牌和确定性变化种子产生差异。变化种子由任务 ID 和方案版本计算，同一任务重渲染保持一致。

### 12.2 首发布局

| 布局 ID | 用途 |
| --- | --- |
| `speaker_fullscreen` | 人物全屏、动态字幕和轻覆盖层 |
| `speaker_left_info_right` | 人物左侧、右侧信息卡 |
| `speaker_right_evidence_left` | 人物右侧、左侧证据图或要点 |
| `material_fullscreen_speaker_pip` | 图片全屏、人物画中画 |
| `product_hero` | 产品图、卖点和标签的主视觉 |
| `editorial_collage` | 多图编辑拼贴 |
| `comparison_split` | 前后、问题与方法或两组数据对比 |
| `steps_stack` | 方法步骤和流程卡片 |
| `number_proof` | 数字、结果和证据强调 |
| `quote_reversal` | 金句、观点反转和划线强调 |
| `method_timeline` | 时间线、阶段和方法拆解 |
| `cta_offer` | CTA、赠品、行动指引和结尾停留 |

每个布局首发至少提供两个结构变体。变体可以改变主次关系、分栏比例、图片窗数量、标题位置和卡片层次，但不得突破字幕安全区、人物安全区和产品安全区。

### 12.3 覆盖层组件

- 标准字幕。
- 重点词字幕。
- 主标题与副标题。
- 章节标签。
- 下三分之一人物或身份条。
- 数字证明。
- 要点列表。
- 信息卡片。
- 引用卡片。
- 产品标签。
- 进度或步骤指示。
- CTA 和结尾停留。

### 12.4 动画白名单

首发只允许以下动画：

- `fade`
- `slide`
- `scale`
- `rotate`
- `wipe`
- `stagger`
- `count_up`
- `image_pan_zoom`
- `card_reveal`
- `stamp`
- `light_sweep`
- `highlight_draw`
- `split_screen`
- `subtitle_pop`
- `volume_fade`（音频自动化能力，见第 15 节，不进入 GSAP）

其中前 14 项是视觉动画并由 GSAP 执行，`volume_fade` 是第 15 项导演能力，由 Python 编译到 `audio_cues` 后交给 FFmpeg 执行。每项拥有固定参数类型、最小值、最大值和允许目标；编译器拒绝未知属性、越界参数和不支持目标。

### 12.5 转场白名单

- `hard_cut`
- `soft_wipe`
- `directional_slide`
- `light_flash`
- `card_match_cut`

转场时长由编译器限制，不能覆盖关键发音或缩短准确字幕窗口。

### 12.6 主题令牌

每个任务冻结：

- 主色、辅助色、背景色、文字色和强调色。
- 标题、正文、字幕和数字的字体层级。
- 卡片圆角、边框、阴影和装饰强度。
- 动画能量、缓动曲线组和转场强度。
- 图片裁切和构图策略。
- 信息密度和留白等级。
- 纹理使用等级。

Qwen 只能从范围化令牌中选择或给出受限数值，不能输出 CSS。

### 12.7 三秒钩子与创意差异

- 每条成片的前 3 秒必须建立与准确文本一致的钩子，至少包含一种：核心问题、反常识结论、明确收益、关键数字或直接情境；不得凭空添加恐吓、夸大承诺或原文不存在的事实。
- 钩子必须同时在画面结构、标题或节奏中可见，不能只把第一句普通字幕改颜色。
- 同一条视频保持一个冻结主题和动效能量；不同场景可以换布局，但色彩、字体、卡片语言和运动节奏必须属于同一视觉系统。
- 20 条验收样本至少覆盖 8 种首发布局和每种已使用布局的 2 个结构变体；单一布局不得占全部场景的 35% 以上。连续使用同一布局不得超过 2 个场景，除非导演给出 `continuity_reason` 且人工验收确认内容需要持续人物全屏。
- “自由创意”是对内容选择组件和结构，不是换色模板。验收报告统计布局、变体、动画、转场和主题令牌的分布，发现机械重复时即使技术质检通过，也不能计入“人工可直接发布”。

## 13. HyperFrames 编译与渲染

### 13.1 工程结构

- 顶层 composition 声明整片宽度、高度、时长和帧率。
- 每个场景编译为一个子 composition。
- 子 composition 的 host ID、内部 `data-composition-id` 和 `window.__timelines` 键完全一致。
- 每个 composition 同步注册且只注册一条 `gsap.timeline({paused:true})`。
- 媒体元素由 HyperFrames 管理播放、寻帧和解码。
- 所有组装后的 DOM ID 全局唯一。

### 13.2 确定性规则

- 禁止 `Date.now()`、`performance.now()`、未播种随机数和无限循环动画。
- 禁止渲染期间网络请求和动态插件加载。
- 不在动画时读取动态布局尺寸。
- 空间动画只使用 GSAP 变换别名和允许的视觉属性。
- 不对 `.clip` 生命周期执行外部可见性控制。
- 同一冻结 render manifest 的关键帧和音频时间线必须可重复。

### 13.3 render manifest

render manifest 由后端在所有素材和音频准备完成后生成，至少包含：

- manifest 版本和 SHA-256。
- 任务 ID、比例、帧率、宽高和总时长。
- edit-plan 版本和 SHA-256。
- `renderer_build_id`、代码提交 SHA、`package-lock.json` SHA、内容寻址发布包 SHA。
- Node、Chromium、FFmpeg 和 FFprobe 精确版本，编码器参数、线程数、Locale 和 Timezone。
- 组件注册表版本与 SHA、所有字体 SHA。
- 主题令牌和确定性变化种子。
- 场景、字幕、组件、动画和转场。
- 本地沙箱中的素材相对路径及每个文件 SHA-256。
- 唯一最终混音母带的相对路径、SHA-256、采样率、声道和时长。

清单不得包含输出路径、外部 URL、密钥、父目录跳转或任意脚本字段。输出目录由 Python 单独创建并传入，形成唯一权威来源。

### 13.4 渲染沙箱

- 每个任务建立独立、不可复用的工作目录。
- Python 将已验证素材下载到工作目录，并验证大小、MIME 和 SHA-256。
- 冻结后输入目录只读；拒绝符号链接、硬链接、设备文件和其他非普通文件。最终打开文件句柄后再次校验其解析路径、文件类型、大小和 SHA-256，避免路径穿越和 TOCTOU。
- 每个 render unit 实例使用 `DynamicUser=yes` 或经验证的等价独立 UID，并启用独立 mount、user、PID 和临时目录命名空间；并发任务不得共享 UID、工作目录或可枚举的父目录权限。
- Node 子进程使用该实例非特权用户和最小环境变量，不继承供应商密钥，不使用 Chromium `--no-sandbox`。
- 渲染沙箱单元启用 `PrivateNetwork=true`、`PrivateUsers=true`、`PrivateMounts=true`、`NoNewPrivileges=true`、`ProtectSystem=strict`、`ProtectHome=true`、`PrivateTmp=true`、`RestrictSUIDSGID=true`，并配置 CPU、内存、进程数和临时磁盘配额。
- mount namespace 只 `BindReadOnlyPaths` 固定渲染发布包和当前任务的冻结输入，只 `BindPaths` 当前任务输出目录；V3 任务根目录、其他任务目录、密钥目录和宿主机临时目录均设为 `InaccessiblePaths`。launcher 为本实例动态 UID 配置最小访问权，unit 结束即回收。
- 持有供应商密钥的 Python Worker 保持非 root，不能获得任意 `systemctl` 权限。root 拥有的固定 launcher 或窄权限 polkit 规则只允许启动、查询和停止 `huangque-ai-edit-v3-render@.service`，禁止其他 unit 和任意 unit 属性覆盖。
- 渲染实例 ID 由服务端生成并只允许 `[a-z0-9_-]{1,64}`；任务描述先写入 root 配置的固定 spool 目录，unit 只按实例 ID 读取。用户路径、用户文字、环境变量、systemd 属性和 shell 片段都不能成为 unit 参数。
- Node 渲染阶段禁止外部网络；所有依赖、字体和素材必须本地存在。
- 禁止通过 shell 字符串拼接命令；FFmpeg 只允许本地 `file` 和 `pipe` 协议。超时时终止整个 Chromium/Node/FFmpeg 进程组。
- CPU、内存、子进程数量、解码像素、墙钟时间和磁盘写入量均受限制。
- 完成或终态失败后按第 19 节生命周期规则清理媒体临时文件和中间对象。

## 14. 素材处理

### 14.1 本次上传图片

- 单任务最多 10 张。
- 图片完成 COS 上传后由服务端进行解码、方向修正、尺寸检测和基础质量检查。
- 使用 Qwen3.7-Max 的批量视觉分析生成脱敏语义描述、主体类型、构图方向、可用比例和风险标签。
- director 只接收语义描述和缩略视觉上下文，不接收 COS Key。
- 素材解析器根据槽位语义、场景意图、比例和时间范围进行匹配。
- 同一图片允许在有明确导演理由时复用，但不得在多个相邻场景机械重复。

### 14.2 禁止来源

- 不检索用户历史图片、视频或口播资产作为补充素材。
- 不检索不存在的平台公共素材库。
- 不把其他口播视频当作 B-roll。
- 不使用未在当前任务输入中声明的用户资产。

### 14.3 生图

- required 槽位没有合适用户图片时调用网站现有生图 API。
- 提示词由场景语义、主题令牌、比例和事实边界组成。
- 不得生成可被误认为用户真实门店、真实客户、真实产品证明或真实人物经历的虚构证据。
- 对产品、包装或品牌外观有准确性要求时，优先使用用户图片；无法安全生成时让槽位失败，不伪造。
- AI 图片生成后先写入私有 COS，再下载到渲染沙箱。
- 生成请求、provider request ID、成本、COS Key 和对应槽位写入 V3 审计。

### 14.4 素材降级

- required 槽位失败：任务不得进入渲染。
- optional 槽位失败：编译器按照注册组件的显式降级变体移除该槽位并重新排版。
- 不允许用语义无关素材填空。
- 所有降级写入任务结果，便于统计和人工验收。

## 15. BGM、音效和混音

### 15.1 ElevenLabs

- BGM 模型：`music_v2`。
- 音效模型：`eleven_text_to_sound_v2`。
- BGM 不包含人声。
- 每条任务必须生成一条可覆盖完整成片的 BGM；BGM 在有限重试后仍失败则任务失败并退款，不以未经用户授权的历史音乐替代。
- Qwen 描述情绪、能量、BPM 范围和禁用特征；ElevenLabs 不参与时间线决策。
- SFX 仅用于观点反转、数字、方法节点、转场和 CTA；单条任务可以没有 SFX，只有导演声明的 cue 才生成，失败时按 cue 的 required/optional 级别处理。
- 被标记为保护词的品牌、数字、价格和关键句时间范围内，不放置会遮挡发音的 SFX 峰值。
- 保存 Provider 模型、request ID、用量、生成参数和当时适用的授权/条款版本元数据；生产上线前另做音乐商业使用与生成内容标识评审。

### 15.2 生成与保存

第一版每个任务都调用 ElevenLabs 新生成 BGM 和所需 SFX，不读取或复用历史音频。合格结果保存为该任务的私有派生素材，便于审计、下载和后续产品评估；是否建立可复用音频库属于后续版本，不在首发读取路径中。

### 15.3 混音

- 人声始终是主轨。
- 平台口播视频和外部视频的原始人声保持原 PTS、采样顺序和播放速度，不做时长拉伸；任何画面删减都同步裁切对应原声区间。
- FFmpeg 对 BGM 执行基于人声活动的 ducking。
- 口播期间 BGM 相对人声至少低 12 dB。
- SFX 不覆盖重要发音。
- 最终混音目标综合响度为 `-16 LUFS ± 2 LU`。
- 最终 true peak 不高于 `-1 dBTP`。
- 输出不存在削波、长异常静音、双重对白或未静音的重复源音轨。
- Python 在渲染前把原人声、BGM、SFX 和所有 `volume_fade` cue 编译为唯一 48 kHz 双声道母带。HyperFrames 中所有源视频元素强制静音，只渲染无声画面；渲染完成后 FFmpeg 按冻结的原始 PTS 合并该母带并编码 AAC。
- 母带和无声画面起始 PTS 都归一为 0，最终音画总时长偏差不超过一帧且不超过 40 ms；抽检口型关键点与人声事件偏差不超过 80 ms。任何重复对白或偏差超限均为阻断错误。

## 16. 任务状态、检查点与恢复

### 16.1 状态机

```text
created_draft
-> preholding
-> queued
-> generating_voice
-> normalizing
-> transcribing
-> aligning
-> planning
-> resolving_materials
-> generating_images
-> generating_audio
-> mixing_audio
-> compiling
-> rendering(attempt=1)
-> quality_checking

quality_checking(pass, attempt=1) -> staging_delivery
quality_checking(repairable, attempt=1, repair_count<1)
  -> repair_planning -> compiling -> rendering(attempt=2) -> quality_checking
quality_checking(pass, attempt=2) -> staging_delivery
quality_checking(nonrepairable, 任意attempt) -> failed
quality_checking(fail, attempt=2) -> failed

staging_delivery -> settling

preholding(confirmed_success) -> queued
preholding(confirmed_absent_or_rejected) -> prehold_absent
preholding(unknown) -> billing_reconciling(reason=prehold, resume=preholding)

settling(confirmed_refund_target_reached) -> publishing
settling(unknown) -> billing_reconciling(reason=settlement, resume=settling)

publishing(publish_won) -> completed
publishing(cancel_won) -> failed -> refund_pending
publishing(any_prepare_register_commit_cancel_or_query_unknown)
  -> asset_decision_reconciling
asset_decision_reconciling(authoritative_publish_won) -> completed
asset_decision_reconciling(authoritative_cancel_won) -> failed -> refund_pending
asset_decision_reconciling(no_decision_and_idempotent_resume_allowed) -> publishing
asset_decision_reconciling(timeout_5m) -> failed_asset_decision_pending
failed_asset_decision_pending(authoritative_publish_won) -> completed
failed_asset_decision_pending(authoritative_cancel_won) -> failed -> refund_pending

任一不可恢复媒体阶段 -> failed -> refund_pending
refund_pending(confirmed_full_target_reached) -> refunded
refund_pending(unknown) -> billing_reconciling(reason=refund, resume=refund_pending)

billing_reconciling(prehold_confirmed) -> queued
billing_reconciling(prehold_absent) -> prehold_absent
billing_reconciling(settlement_target_reached) -> publishing
billing_reconciling(settlement_needs_idempotent_progress) -> settling
billing_reconciling(full_refund_target_reached) -> refunded
billing_reconciling(refund_needs_idempotent_progress) -> refund_pending
billing_reconciling(inconsistent_or_timeout_5m) -> failed_reconciliation_pending

failed_reconciliation_pending(prehold_absent) -> prehold_absent
failed_reconciliation_pending(any_prehold_confirmed) -> refund_pending
failed_reconciliation_pending(full_refund_target_reached) -> refunded
```

上述分支是穷尽状态合同。每条 `billing_reconciling` 记录必须冻结 `reason`、`resume_state`、外部幂等号、目标累计退款额和首次未知时间，不能仅靠一个无上下文状态恢复。任何账务 unknown 都必须留在上述可恢复状态之一，并在首次未知 5 分钟内转入 `failed_reconciliation_pending`；不得让 `preholding`、`settling` 或 `refund_pending` 永久悬挂。

每条 `asset_decision_reconciling` 记录必须冻结未知操作类型 `prepare/register_generation/commit_publish/cancel_publish/query_decision`、外部幂等号、generation、期望裁决、首次未知时间和最后权威响应。任何资产操作响应丢失都先查询同一 `(mode, source_job_id)` 的权威 generation 与裁决；只有明确“未受理”才能用原幂等号继续。首次未知 5 分钟后转入 `failed_asset_decision_pending`，停止所有媒体工作且既不发布也不退款；持久 outbox 继续查询，直到只按 `publish_won` 或 `cancel_won` 分支收敛。

`repair_count` 初始为 0，只有从首次质检原子进入 `repair_planning` 时才能加 1；第二次质检不允许再次修复。`completed`、`refunded` 和 `prehold_absent` 是不可重新打开的终态；`failed_reconciliation_pending` 与 `failed_asset_decision_pending` 都是停止媒体工作的可对账状态，不锁用户新任务。`prehold_absent` 表示权威账本最终确认未发生预扣，属于账务终态而不是“已退款”。

没有工作量的阶段允许以 `skipped` 检查点完成，但不得跳过状态审计。例如已有主视频或主音频时，`generating_voice` 记录为 `skipped`。

### 16.2 租约和幂等

- Worker 使用有期限租约领取任务。
- 每次领取生成单调递增的 `fencing_token`。只有同时匹配 owner、未过期租约和当前 token 的 Worker 才能续租、提交阶段结果或完成检查点；过期 Worker 的任何写回均被数据库条件更新拒绝。
- 供应商提交前先持久化提交意图和幂等键。
- 已知结果按 provider task ID 恢复，不盲目重复提交。
- 每个阶段结果带输入指纹；输入改变时不得复用旧结果。
- 首次预扣成功时冻结绝对 `processing_deadline_at`；重启、重领、阶段重试和机器时钟变化都不得重置它。仅在首次进入修复路径时原子追加一次 10 分钟并记录 `repair_budget_granted_at`。
- 租约丢失或超时时终止该任务的完整 Chromium、Node 和 FFmpeg 进程组，并在同一数据库事务中关闭仍为 `running` 的阶段记录。
- `completed`、`refunded` 和 `prehold_absent` 终态不可重新打开；对账状态只能按上方权威账本分支收敛。
- 用户重试创建继任任务并重新报价、重新预扣，不复用不完整任务状态。

### 16.3 端到端超时

- 预扣尚未明确成功时使用独立的 5 分钟准入窗口，从 `created_draft` 开始；超时直接进入 `failed_reconciliation_pending`，不启动媒体或供应商阶段，创作时钟不开始。
- 用户端创作时钟从预扣明确成功开始，到 `completed`、`refunded`、`failed_reconciliation_pending` 或 `failed_asset_decision_pending` 结束，包含排队、媒体处理、首次对账/裁决窗口、交付和通知准备。
- 排队等待硬上限 10 分钟；超过后不再启动供应商调用，任务失败并全额退款。
- 普通端到端目标 10 至 25 分钟。
- 无修复任务端到端上限 45 分钟。
- 只有首次成片存在明确可修复问题时，才原子追加一次最多 10 分钟的修复预算；端到端总上限 55 分钟。
- 达到绝对预算仍未完成时终止整个任务进程组并关闭活动阶段。若权威账本仍无法确认是否预扣，则在预算内进入 `failed_reconciliation_pending`；若共享资产服务仍无法确认发布/取消裁决，则进入 `failed_asset_decision_pending`。两种状态都不再运行媒体或生成供应商阶段、不锁死用户下一次创作，并由持久 outbox 低频查询。账本确认已扣后执行累计目标全额退款，确认未扣则进入 `prehold_absent`；资产裁决确认发布获胜则完成，确认取消获胜才允许退款。外部权威服务恢复时间不计入 45/55 分钟创作 SLA，页面不得错报“已退款”或“已发布”。

### 16.4 并发、容量和背压

第一版使用可配置并发但冻结测试基线：

- 5 并发最低测试主机：8 vCPU、16 GiB RAM、80 GiB 可用临时 SSD；`pipeline_concurrency=5`、`render_slots=2`。
- 10 并发压力主机：16 vCPU、32 GiB RAM、160 GiB 可用临时 SSD；`pipeline_concurrency=10`、`render_slots=4`。如果现有测试服务器低于该配置，10 并发只标记为容量阻塞，不通过降低画质或解除沙箱限制伪造通过。
- 单个渲染沙箱上限：2 vCPU、3 GiB RAM、8 GiB 临时磁盘、64 个进程或线程，墙钟上限由任务剩余预算决定。
- 10 条同时处理是压力验收；单机资源不足时允许使用第二个同版本 Worker 节点，但两节点必须共享同一权威数据库访问方案和 fencing 语义。SQLite 仅支持单机 Worker；启用多节点前必须将 V3 store 切换为具备行锁或等价租约语义的服务端数据库，不得把 SQLite 放到网络文件系统。
- 单机待处理队列上限 50 条，且按估算临时磁盘预留做准入。队列满或磁盘不足时在预扣前返回 `capacity_unavailable` 和 `Retry-After`，不得先扣点再等待。
- 5 条并行任务是最低验收，10 条是压力验收；记录队列等待、端到端 p50/p95、各阶段 p50/p95、CPU/RAM/磁盘峰值和被背压请求数。
- 5 条并行时 p50 不超过 25 分钟、p95 不超过 45 分钟；10 条压力下不得越权、崩溃、重复调用或破坏账务，超出端到端预算的任务必须按规则退款。

## 17. HTTP API

### 17.1 用户接口

- `GET /api/v3/edit/capabilities`
- `GET /api/v3/edit/platform-assets`
- `GET /api/v3/edit/audio-assets`
- `GET /api/v3/edit/voices`
- `GET /api/v3/edit/templates`
- `POST /api/v3/edit/uploads`
- `POST /api/v3/edit/uploads/{upload_id}/complete`
- `POST /api/v3/edit/materials`
- `POST /api/v3/edit/quote`
- `POST /api/v3/edit/jobs`
- `GET /api/v3/edit/jobs`
- `GET /api/v3/edit/jobs/{job_id}`
- `GET /api/v3/edit/jobs/{job_id}/plan`
- `GET /api/v3/edit/jobs/{job_id}/result`
- `POST /api/v3/edit/jobs/{job_id}/retry`

所有读写接口都要求登录并按 owner 限制。写接口在 `AI_EDIT_V3_ENABLED=0` 时 fail closed；能力、状态和结果读取仍可用于解释已存在任务。

`GET /platform-assets` 只返回当前 owner 已完成、来源为允许的数字化 IP 口播生成任务且具备封面/权威文案的资产，不返回普通上传、AI 剪辑成片或其他视频资产。列表响应只含封面、时长、比例、标题和 ID，不含视频 URL；`GET /jobs/{job_id}` 或专用预览授权只在用户主动播放所选资产时签发 300 秒媒体 URL。

`GET /audio-assets` 只返回当前 owner 有权使用、状态正常且实际对象可读的音频资产摘要；不返回其他用户音频或 V3 的 BGM/SFX 派生素材。`GET /voices` 同样只返回当前 owner 可用的克隆音色和平台通用音色摘要，不返回 Provider 密钥或底层克隆数据。

### 17.2 创建任务

报价和创建任务使用同一个规范化判别联合。`POST /quote` 不含 `quote_id`；`POST /jobs` 必须提交由该完全相同输入产生的 `quote_id`。允许的五种输入为：

| `input_type` | 权威来源字段 | 比例规则 |
| --- | --- | --- |
| `platform_talking_head` | `source_asset_id` | 只能为 `auto`，服务端按第 5.2 节决定 |
| `uploaded_video` | `source_upload_id` | 只能为 `auto`，服务端按第 5.2 节决定 |
| `existing_audio` | `source_asset_id` | `16:9` 或 `9:16`，默认 `16:9` |
| `uploaded_audio` | `source_upload_id` | `16:9` 或 `9:16`，默认 `16:9` |
| `script_to_audio_video` | `tts_input` | `16:9` 或 `9:16`，默认 `16:9` |

```json
{
  "input_type": "platform_talking_head",
  "source_asset_id": "video_123",
  "ratio": "auto",
  "creation_mode": "style_prompt",
  "style_prompt": "商业感强，真实可信",
  "material_asset_ids": ["image_01", "image_02"],
  "quote_id": "quote_xxx"
}
```

客户端不得提交权威正文、COS Key、输出对象路径、模型名或渲染组件内部字段。

文案加音色模式使用以下互斥输入，而不是 `source_asset_id`：

```json
{
  "input_type": "script_to_audio_video",
  "tts_input": {
    "text": "由用户确认并随报价冻结的文案",
    "voice_id": "voice_123"
  },
  "ratio": "16:9",
  "creation_mode": "style_prompt",
  "style_prompt": "克制、清晰的知识讲解",
  "material_asset_ids": [],
  "quote_id": "quote_xxx"
}
```

服务端重新校验音色归属和状态，并使用报价内保存的文案哈希确认正文未变化。

判别联合执行严格互斥：`source_asset_id`、`source_upload_id` 和 `tts_input` 必须且只能出现与 `input_type` 对应的一种；其他两种字段即使为空也不得出现。`source_asset_id` 根据判别类型进一步校验为受允许的平台口播或音频资产，`source_upload_id` 校验已完成上传、owner、探测 MIME 和媒体类型。`material_asset_ids` 只能引用本次 V3 上传流程产生并属于当前 owner 的图片记录。

创作入口也执行严格判别：`ai_auto` 不允许 `style_prompt` 或 `template_id`；`style_prompt` 必须提交 1 至 1000 个字符的 `style_prompt` 且不允许 `template_id`；`template_reference` 必须提交一个已发布且支持目标比例的 `template_id`，不允许 `style_prompt`。模板版本由服务端按 `template_id` 解析并随报价冻结，客户端不能指定未发布版本。

报价保存规范化请求 JSON 及 SHA-256。创建任务时服务端重新规范化请求并要求其哈希、价格版本和 owner 与有效报价完全一致；任一字段变化都必须重新报价。`GET /jobs` 只分页返回当前 owner 的 V3 历史任务摘要，不复用或暴露 V2 任务。

### 17.3 幂等

- 报价与创建任务分别使用请求指纹。
- `POST /jobs` 和 `POST /retry` 要求 `Idempotency-Key`。
- 同一 owner、同一 key 和同一规范化请求返回同一结果。
- 同一 key 与不同请求返回冲突，不创建第二个任务或第二次扣款。

### 17.4 响应边界

用户 API 不返回：

- 供应商 API Key。
- 原始签名 URL 的持久值。
- 本地路径。
- Qwen 内部推理过程。
- Render manifest。
- Node 命令行、环境变量或服务端日志。

`GET /plan` 返回脱敏后的导演方案，删除真实对象键、内部成本字段和供应商原始响应。

## 18. 数据存储

`ai_edit_v3.db` 使用 SQLite WAL、外键、busy timeout 和版本化迁移。至少包含以下职责表：

- `edit_v3_jobs`：任务、owner、输入、状态、时限、结果和错误。
- `edit_v3_stage_attempts`：阶段尝试、租约、输入指纹、开始和结束时间。
- `edit_v3_checkpoints`：不可变阶段结果版本。
- `edit_v3_uploads`：上传意图、对象键、MIME、大小和完成状态。
- `edit_v3_materials`：本次任务图片和 AI 图片元数据。
- `edit_v3_job_materials`：任务与素材关系及用途。
- `edit_v3_quotes`：报价、请求指纹、价格版本和有效期。
- `edit_v3_pricing_versions`：已发布的版本化价格参数。
- `edit_v3_template_versions`：已发布模板的版本、预览、支持比例、能力约束和冻结哈希。
- `edit_v3_model_calls`：模型审计。
- `edit_v3_provider_tasks`：供应商任务与恢复状态。
- `edit_v3_provider_usage`：可结算的实际用量。
- `edit_v3_plans`：原始最终回答、规范化 edit-plan 和 SHA-256。
- `edit_v3_render_manifests`：冻结清单、组件版本和 SHA-256。
- `edit_v3_renders`：渲染尝试、日志摘要、产物和成本。
- `edit_v3_quality_reports`：技术、画面、内容和声音报告。
- `edit_v3_billing_intents`：预扣、结算和退款意图。
- `edit_v3_publish_intents`：跨 V3 数据库与共享资产服务的发布 Saga、单调 `publish_generation`、外部幂等键、发布/取消裁决、`asset_id` 和恢复证据。

数据库只保存稳定 COS 对象键，不保存长期可访问 URL。播放和下载时生成短期签名地址。

测试环境使用显式绝对路径 `AI_EDIT_V3_DB_PATH`。启动时解析真实路径并拒绝与 V2 数据库同路径、同 inode/文件标识或软硬链接别名；数据库文件、备份和迁移锁均不得放在 COS、SMB、NFS 等网络文件系统。所有 `edit_v3_*` 表都带环境或由独立数据库天然限定环境，迁移只允许访问 V3 数据库。

`edit_v3_billing_intents` 对 `(environment, owner_id, job_id, operation)` 建唯一约束；`operation` 只允许 `pre_debit`、`refund_delta` 和 `refund_full`。每行保存不可变的外部幂等号、请求指纹、`refund_target_total`、本次实际请求额、状态和权威账本对账证据。任务账务汇总强制 `0 <= confirmed_refunded_total <= confirmed_preheld_total`，任何退款响应都先按外部幂等号从权威账本确认，再更新累计值。任务的 `fencing_token`、`processing_deadline_at`、`repair_budget_granted_at` 和最终资产发布标识必须可事务校验。

## 19. COS 对象规则

```text
{environment}/ai-edit-v3/{owner_hmac}/{job_id}/source/...
{environment}/ai-edit-v3/{owner_hmac}/{job_id}/normalized/...
{environment}/ai-edit-v3/{owner_hmac}/{job_id}/materials/uploaded/...
{environment}/ai-edit-v3/{owner_hmac}/{job_id}/materials/generated/...
{environment}/ai-edit-v3/{owner_hmac}/{job_id}/audio/...
{environment}/ai-edit-v3/{owner_hmac}/{job_id}/render/attempt-{n}/...
{environment}/ai-edit-v3/{owner_hmac}/{job_id}/delivery/{render_attempt}-{content_sha256}.mp4
```

- `environment` 只允许冻结配置中的 `test` 或 `production`；测试凭据仅允许访问 `test/ai-edit-v3/` 前缀。`owner_hmac` 使用服务端专用密钥生成的 HMAC 截断值，不使用裸哈希或可枚举 owner ID。job ID 由服务端生成。
- Key 必须通过 V3 正则校验并拒绝 `..`、盘符、绝对路径和反斜杠逃逸。
- 所有对象保持私有。
- 渲染前使用 GET 权限的短期签名地址由 Python 获取对象；签名地址不进入数据库或 Qwen。
- 成片交付后用带 Range 的 GET 验证可读性，不使用 HEAD 代替 GET 签名验证。
- V3 资产播放和下载只走 V3 私有对象签名器，签名 GET 默认有效期 300 秒。共享 `video_assets` 对 `(mode='ai_edit_v3', source_job_id)` 建唯一约束，防止重试或崩溃重复发布。
- 交付对象 Key 不可变，重渲染必须使用新的 attempt 与内容 SHA，不覆盖旧 `final.mp4`。未完成任务的交付对象不进入用户可见资产查询。

生命周期规则：用户上传的主媒体、本次图片、成功生成的 AI 图片、BGM/SFX 和最终成片作为用户私有资产保存，直到用户删除或账户策略清理；标准化文件、关键帧、临时 PCM、无声画面和失败上传 7 天内删除；Qwen 最终原始回答保留 30 天；脱敏模型审计、Schema/manifest 哈希、质检摘要和账务证据保留 180 天。删除任务必须幂等并记录对象删除失败以重试；生产期若法规或产品保留策略不同，需单独评审。

## 20. 计费

### 20.1 报价

报价由已发布的 V3 价格版本计算，包含：

- 基础任务成本。
- 输入时长阶梯。
- 文案加音色模式的 TTS 预估时长和调用上限。
- 预计 Qwen 调用上限。
- 预计生图数量上限。
- BGM 和 SFX 上限。
- HyperFrames 渲染时长与复杂度等级。
- 一次允许修复的最大风险准备。

报价返回最低值、最高值、明细、价格版本、请求指纹和 15 分钟有效期。报价不承诺一定消耗最高值。

### 20.2 预扣与结算

- 用户确认后按最高值预扣。
- 每个供应商调用和本地渲染记录实际用量。
- 成功后按已冻结价格版本结算实际费用，并退回差额。
- 实际费用不得超过预扣上限；异常超出由平台承担并记录告警。
- 最终失败把累计退款目标提升到全部预扣额；不会在已退差额基础上再次增加一个完整预扣额。
- 预扣、结算和退款都有独立幂等键和持久意图。
- 不确定的支付响应进入对账状态，不重复扣款或提前宣称退款成功。

### 20.3 崩溃安全账务与发布协议

1. `POST /jobs` 在一个本地事务内写入 `created_draft` 任务、冻结报价引用和 `pre_debit` 意图；事务提交后由账务 outbox 使用该意图的不可变幂等号执行预扣。
2. 只有预扣明确成功，pipeline 才把任务置为 `queued`。不确定响应进入 `billing_reconciling`，只能查询或消费确定的对账结果，不得再次发起预扣。
3. 质检通过后，`staging_delivery` 先把不可变成片上传私有 COS 并完成 Range GET，但不创建用户可见资产。
4. `settling` 以已冻结价格版本计算实际费用。`refund_delta` 的累计退款目标为 `confirmed_preheld_total - actual_charge`；本次退款额只能是 `refund_target_total - confirmed_refunded_total`。差额为零也记录完成意图。只有权威账本确认达到目标后才允许 `publishing`。
5. `publishing` 使用发布 Saga，不能假设 V3 SQLite 与共享 `video_assets` 跨库原子。V3 事务先写不可变 `publish_intent`，其中 `publish_generation` 等于当前单调 fencing token；任何新 Worker 在发布或取消前都必须先把更高 generation 注册到资产服务。
6. 资产服务必须提供按 `(mode='ai_edit_v3', source_job_id)` 唯一的二阶段合同：`prepare_hidden(generation)` 只创建不可见资产；`commit_publish(generation)` 与 `cancel_publish(generation)` 在资产服务自己的事务中对同一行做一次性裁决。只接受当前最高 generation；较旧 Worker 的准备、发布和取消都被拒绝。
7. 若 `commit_publish` 先赢，资产变为可见并返回稳定 `asset_id`；后到取消返回 `publish_won`，pipeline 必须记录该 ID 并完成任务，禁止再发起全额退款。若 `cancel_publish` 先赢，写入持久 cancel tombstone、保持或恢复资产不可见；任何晚到 `commit_publish` 都返回 `cancel_won`，此后才能进入全额退款。
8. Worker 失租、任务超时或准备退款时，必须先用最新 generation 调用 `cancel_publish` 完成上述裁决。旧 Worker 若在新 generation 注册前已经合法发布，则发布获胜并由恢复 Worker收敛为 `completed`；新 generation 或 cancel tombstone 已生效后，旧 Worker 永远不能首次发布。
9. `commit_publish` 获胜后，V3 再开启本地事务保存 `asset_id` 并置为 `completed`；崩溃恢复先查询资产裁决。`cancel_publish` 获胜后，才把 `refund_full.refund_target_total` 设置为 `confirmed_preheld_total`，本次只退 `confirmed_preheld_total - confirmed_refunded_total`。因此发布和退款只能有一个获胜结果。
10. 任一退款意图结果未知时，不得启动另一个可能重叠的退款；必须先按外部幂等号确认累计成功退款。数据库 CHECK、事务条件更新和账本对账共同保证累计退款不小于 0 且不超过预扣额。
11. 进程可在每一步前后崩溃；恢复逻辑先读取意图、权威账本、资产服务 generation/裁决和资产唯一键，再决定查询或继续，禁止根据内存状态重新扣款、退款或发布。

`prepare_hidden`、`register_generation`、`commit_publish`、`cancel_publish` 和 `query_decision` 每个调用都必须有持久外部幂等号。任一调用已经发出但响应丢失时立即进入 `asset_decision_reconciling`，不切换到退款或完成；5 分钟仍拿不到权威 generation/裁决则进入 `failed_asset_decision_pending`。该状态中的 outbox 只查询或用同一幂等号恢复明确未受理的操作，绝不另起发布/取消请求。权威结果为 `publish_won` 才完成，为 `cancel_won` 才设置全额退款目标。

账务首次对账最多占用创作预算 5 分钟。仍不确定时任务在预算内进入 `failed_reconciliation_pending`，停止全部媒体和供应商调用、保持成片不可交付并触发高优先级告警；持久 outbox 继续只读查询。只有账本确认预扣存在后才能退款，确认未扣则进入 `prehold_absent`，未知时绝不盲目退款。该状态不锁死用户新任务，且测试环境不得显示“已退款”。

### 20.4 测试环境

测试环境可以完整验证点数流程，但不授权生产价格发布。正式价格和供应商成本系数在生产评审阶段单独确认。

## 21. 质检与一次修复

### 21.1 技术质检

- MP4 可完整解码。
- 视频编码为 H.264，音频编码为 AAC。
- 输出为 `1920x1080` 或 `1080x1920`。
- 帧率、采样率和声道符合冻结清单。
- 时长与冻结清单在允许误差内一致。
- 无连续黑帧、异常冻结、长异常静音、削波或重复音轨。
- 文件存在、大小合理，上传 COS 后可通过带 Range 的 GET 读取。

### 21.2 文本和事实质检

- 字幕与准确文本一致。
- 品牌名、产品名、数字和价格正确。
- 外部媒体的文本清理只改变标点和断句。
- 标题和信息卡不得表达准确文本中不存在的事实性承诺。

### 21.3 素材质检

- 每个 required 槽位都有来源可追踪的匹配素材。
- 素材语义与所在口播段落一致。
- 不出现其他用户、错误人物、错误产品、错误门店或历史口播视频。
- AI 图片不得冒充真实证据。

### 21.4 布局和声音质检

- 字幕、标题和卡片不越过安全区。
- 不遮挡人物面部和关键产品区域。
- 重点动画不过度重复。
- BGM 和 SFX 不覆盖人声。
- 响度和峰值达到第 15.3 节标准。

### 21.5 可执行质检矩阵

所有阻断项必须 100% 通过才允许结算和发布。每项报告包含执行器版本、输入哈希、量化值、证据帧或时间段、通过状态、阻断级别和可修复性。

固定视觉模型的输出必须通过 `server/content_domains/ai_edit_v3/schemas/quality-verdict-v1.schema.json`。该 Schema 同样使用 JSON Schema 2020-12、所有对象 `additionalProperties: false`，并限制响应为 256 KiB、深度 16、检查项 64、证据项每检查最多 8 个。每个 verdict 必须包含检查 ID、`pass/fail/unknown`、置信度、证据帧 SHA/时间戳、简短理由和模型请求 ID；拒绝重复键、NaN、Infinity、未知检查或没有证据的 `pass`。非法输出、证据缺失、模型不可用或 `unknown` 对阻断项一律按不通过处理，模型不得通过第二次提示把结果“修复成通过”。

| 检查项 | 执行器 | 首发阈值 | 级别 | 自动修复 |
| --- | --- | --- | --- | --- |
| 完整解码、编码、尺寸 | FFprobe、FFmpeg 全片解码 | 解码错误 0；H.264/AAC；仅 `1920x1080` 或 `1080x1920` | 阻断 | 损坏编码允许一次重渲染 |
| 音画时长与同步 | FFprobe PTS、PCM/口型事件抽检 | 起点均为 0；总时长偏差不超过一帧且不超过 40 ms；口型抽检偏差不超过 80 ms | 阻断 | 仅重新 mux 或按同一 PTS 重渲染 |
| 黑帧 | FFmpeg luma 规则加证据帧 | 非导演声明黑场不得连续超过 300 ms | 阻断 | 一次重渲染 |
| 异常冻结 | 帧差规则，结合 manifest 静态场景声明 | 有人声且未声明静态停留时，不得连续 2 秒无有效像素变化 | 阻断 | 一次重渲染或合法变体调整 |
| 异常静音、削波、重复对白 | FFmpeg/EBU R128/轨道指纹 | 语音区间异常静音不超过 500 ms；true peak 和 LUFS 符合第 15.3 节；对白指纹只出现一条 | 阻断 | 允许一次重新混音 |
| 字幕事实 | 确定性文本比对 | 保护词、品牌、产品、数字、价格错误 0；字幕覆盖准确文本 100% | 阻断 | 不得用模型改写修复 |
| 字幕和组件越界 | DOM 几何检查加关键帧截图 | 安全区外像素 0；文本裁切、溢出和不可见行 0 | 阻断 | 允许一次换行、字号或布局变体修复 |
| 人脸和产品遮挡 | 固定版本视觉检测器加 DOM 交集 | 标题、卡片不得覆盖主脸关键区或标记的产品关键区；字幕与允许底部区例外由组件规则声明 | 阻断 | 允许一次位置或布局修复 |
| 素材归属 | 数据库 owner、任务绑定和对象哈希 | 跨 owner、历史素材、未声明资产为 0 | 阻断 | 不可修复，失败退款 |
| 素材语义与人物/产品 | 固定 `qwen3.7-max-2026-06-08` 视觉复核加来源元数据 | required 素材与槽位无高置信冲突；错误人物、产品、门店为 0 | 阻断 | optional 可替换一次；required 缺失失败 |
| 虚构事实与 AI 证据 | 规则比对、生成来源标签、固定模型复核 | 不得把生成图描述为真实案例、门店、客户或效果证明 | 阻断 | 不可通过措辞掩盖 |
| 三秒钩子与视觉一致性 | manifest 规则加人工评分 | 前 3 秒存在合规钩子；整片主题一致，无机械换色重复 | 发布评分 | 可调整组件和节奏一次 |

固定视觉模型只输出规范化判定和证据，不直接修改作品；高影响模型判断必须保留证据帧供人工复核。模型不可用时相关阻断项不得默认通过。

人工“可直接发布”由两名测试人员分别按事实准确、素材相关、前三秒钩子、叙事节奏、布局清晰、字幕可读、声音质量、视觉一致性 8 个维度打 `0/1/2` 分。单条平均总分至少 `13/16`，且事实准确、素材相关、字幕可读、声音质量均不得为 0，才计为可直接发布；意见不一致时由第三人复核。20 条样本中至少 16 条满足该标准。

### 21.6 修复

只允许对明确可修复问题执行一次针对性修复：

- 调整字幕换行、字号或安全区。
- 更换错误或低质量 optional 素材。
- 调整卡片位置和布局变体。
- 调整 BGM、SFX 音量或时间点。
- 重新渲染损坏帧或整片。

字幕事实错误、required 素材缺失、跨用户素材、准确文本被改写、清单不可信或无法验证的输出不得通过自动修复掩盖，必须失败并退款。

## 22. 错误模型

错误响应包含稳定 `error_code`、用户可理解的中文信息、阶段和是否可重试。日志保留脱敏技术原因。

主要错误族：

- `input_*`：归属、格式、大小、数量和时长。
- `billing_*`：报价、预扣、结算和退款。
- `media_*`：FFprobe、FFmpeg 和解码。
- `asr_*`：提交、轮询、超时和时间戳。
- `alignment_*`：覆盖率、单调性和事实保护。
- `director_*`：模型、JSON、Schema 和事实规则。
- `material_*`：匹配、生图、跨用户和 required 缺失。
- `audio_*`：BGM、SFX、混音和响度。
- `compile_*`：组件、动画和 manifest。
- `render_*`：HyperFrames、Chromium、资源和超时。
- `quality_*`：技术、文本、素材、布局和声音。
- `delivery_*`：COS、资产库和签名读取。

未知错误不得向用户返回堆栈、路径、密钥、URL 查询参数或供应商原始响应。

## 23. 安全

- 所有供应商密钥仅存在于服务端密钥文件或环境变量。
- 前端、数据库、render manifest、渲染目录和日志中不保存密钥。
- Qwen 不生成代码，渲染器不执行用户或模型代码。
- Node 渲染进程禁止网络并使用最小权限。
- 所有 JSON 和字符串执行长度、类型、枚举和控制字符校验。
- 所有文件路径基于任务沙箱解析，拒绝绝对路径、父目录、软硬链接和非普通文件，并在打开文件句柄后再次验证真实路径和 SHA。
- 文件类型依赖实际解码和 MIME 探测，不依赖文件名。
- FFmpeg 和 FFprobe 禁止网络协议、concat 外部列表和未登记输入；所有子进程使用参数数组，不经过 shell。
- 供应商返回内容视为不可信输入。
- V3 功能开关默认关闭；依赖、密钥或二进制缺失时拒绝新任务。
- 启动预检必须拒绝 V2/V3 数据库同文件、测试凭据可写生产 COS 前缀、渲染包或 Schema 哈希不匹配、Node/Chromium/FFmpeg 版本漂移，以及渲染沙箱仍可访问网络。
- 仓库密钥扫描覆盖已跟踪和未跟踪的 V3 fixture。

第一版不开发内容安全审核，但测试环境不得因此被描述为生产就绪。生产评审必须为内容安全、版权、违规素材和生成内容标识制定单独方案。

## 24. 可观测性

每个任务使用 job ID、attempt ID、stage attempt ID 和 provider request ID 串联日志。至少记录：

- 排队、处理和各阶段耗时。
- Qwen 首次通过、格式修复和失败数量。
- 每个素材槽的来源、匹配分数、降级和生成状态。
- ElevenLabs 每条任务的生成请求、模型、用量和结果。
- 编译器组件、变体、动画和主题版本。
- HyperFrames 渲染耗时、峰值内存、CPU 时间和输出帧数。
- 技术、内容、布局和声音质检结果。
- 预估成本、预扣、实际成本、结算和退款。
- 资产发布 generation、准备/发布/取消操作、权威裁决和待确认时长。
- 用户可发布人工验收结果。

日志中的 owner 使用不可逆短标识；不记录完整签名 URL、Cookie、Authorization 或用户隐私正文之外的不必要数据。

## 25. 测试策略

### 25.1 单元测试

- V3 输入 Schema 和 owner 边界。
- edit-plan 2.0 字段、时间轴和事实校验。
- `visible_text` 的逐字引用、压缩事实保护、非法引用和 `ui_label` 枚举。
- Qwen JSON 提取和一次修复。
- 三个机器可读 Schema 的元 Schema、合法/非法样例、重复键、深度、数量、长度和模糊输入。
- Qwen 多模态端点契约、终态 content 聚合、提示注入和 `provider_unknown` 行为。
- 动画、布局和转场白名单。
- 素材匹配、禁止来源和 required/optional 降级。
- 生图、ElevenLabs 和 COS 适配器幂等性。
- render manifest 路径、哈希和安全校验。
- 报价、预扣、结算、退款和不确定响应对账。
- 状态机、三类账务 unknown 的穷尽迁移、fencing token、任务租约、崩溃恢复、绝对截止时间和继任任务。
- 账务意图唯一键、累计退款上限、差额退款成功后发布失败、每一步前后崩溃、响应不确定对账、发布 Saga 和重复资产发布。
- 失租 Worker 在新 generation 注册、取消 tombstone 和退款之后尝试首次发布必须失败；发布先赢时恢复为完成且不退款，取消先赢时资产始终不可见且只退款到累计目标。
- `prepare_hidden`、`register_generation`、`commit_publish`、`cancel_publish`、`query_decision` 分别覆盖“服务端已提交但响应丢失”和“持续不可用超过 5 分钟”；任务必须进入资产裁决待确认状态，不锁新任务，并在服务恢复后按唯一权威裁决收敛。

### 25.2 Node 渲染器测试

- 12 个首发布局的横屏和竖屏快照。
- 14 个视觉动画预设的起点、中点和终点寻帧；`volume_fade` 单独执行 FFmpeg 音频包络测试。
- 5 个转场的边界帧。
- 字幕安全区、长文本、数字、英文混排和缺图降级。
- `hyperframes check` 零发现。
- 相同 render manifest、渲染发布包和执行环境的解码像素帧哈希及音频 PCM 哈希一致；不要求 MP4 容器字节完全一致。
- 禁止网络、父目录路径和未知组件。
- Chromium 崩溃、超时、内存限制和中断恢复。
- 软硬链接、TOCTOU、设备文件、图片压缩炸弹、FFmpeg 网络协议、shell 注入和整个进程组终止。
- `PrivateNetwork` 沙箱通过主动外连失败测试，测试时渲染进程环境中不存在任何供应商密钥。
- 非 root Worker 只能启动、查询和停止固定 render unit；越权启动其他 unit、注入 unit 属性、伪造实例 ID 和构造 spool 路径均被拒绝。
- 两个并发 render unit 使用不同动态 UID 和 mount namespace；任一实例按猜测路径、文件描述符继承或 `/proc` 尝试读取 sibling job 输入都必须失败。
- `quality-verdict-v1` 缺证据、未知字段、非法 JSON、`unknown` 或模型不可用时阻断发布，不允许提示修复为通过。

### 25.3 双版本隔离测试

- V3 Worker 不领取 V2 任务。
- V2 Worker 不领取 V3 任务。
- V3 数据库不创建 `edit_v2_*` 表。
- V2 数据库不创建 `edit_v3_*` 表。
- V2/V3 的报价、点数、COS Key、Webhook、任务 ID 和资产模式不串线。
- 关闭 V3 不影响 V2 页面和任务。
- V3 失败不修改 V2 任务、资产或账务记录。
- 错配相同数据库、COS 环境前缀、签名器或凭据权限时启动失败，不允许自动修正后继续。
- 当前 413 项 V2 Python 测试和 47 项 V2 前端测试持续全部通过。

### 25.4 端到端样本

测试环境至少执行 20 条真实差异化样本：

- 10 条口播视频，覆盖不同人物、背景、时长和内容类型。
- 10 条音频生成视频，横屏和竖屏均覆盖。
- 覆盖有图片、无图片、图片语义不完整和比例不一致。
- 覆盖商业口播、知识讲解、产品介绍、招商和门店内容。
- 五类输入 `platform_talking_head`、`uploaded_video`、`existing_audio`、`uploaded_audio`、`script_to_audio_video` 与三类创作入口 `ai_auto`、`style_prompt`、`template_reference` 的 `5×3` 组合都至少出现 1 次；其余 5 条复测高风险组合。
- `template_reference` 至少覆盖 3 条横屏和 3 条竖屏，且 4 个首发模板都至少进入 1 条真实渲染。

每条记录输入、导演方案、素材映射、声音资产、render manifest、耗时、成本、质检结果和人工可发布结论。

此外执行以下故障与容量场景：两个 Worker 竞争同一租约并让旧 token 写回、每个持久阶段前后强制终止进程、账务响应不确定、资产裁决响应不确定、Qwen 响应模糊测试、渲染沙箱断网验证、COS 上传后资产发布前崩溃，以及 5 条并行与 10 条压力任务。每个场景必须证明无重复扣款、无超额退款、无重复供应商提交、无跨任务素材、无永久 `running` 阶段，并进入 `completed/refunded/prehold_absent/failed_reconciliation_pending/failed_asset_decision_pending` 中符合权威证据的状态；两种待确认状态必须在对应权威服务恢复后收敛。

## 26. 测试环境验收门槛

| 指标 | 门槛 |
| --- | --- |
| 最终出片 | 20 条均生成可播放的 1080p H.264/AAC MP4 |
| 导演方案 | 最多一次格式修复后 Schema 通过率 100% |
| 素材事实 | 错误人物、产品、门店和跨用户素材为 0 |
| 首次渲染成功率 | 至少 95% |
| 修复后技术质检 | 100% |
| 人工可直接发布率 | 至少 80% |
| 普通任务目标 | 10 至 25 分钟 |
| 无修复创作上限 | 不超过 45 分钟；外部账本未知时在此时间内转待对账 |
| 一次修复创作总上限 | 不超过 55 分钟；外部账本未知时在此时间内转待对账 |
| 账务未知 | 5 分钟内停止媒体处理并进入 `failed_reconciliation_pending`，不重复扣退、不锁新任务；账本恢复后正确收敛 |
| 资产裁决未知 | 5 分钟内进入 `failed_asset_decision_pending`，既不退款也不宣称发布、不锁新任务；资产服务恢复后按唯一裁决收敛 |
| V2 回归 | 413 项 Python 与 47 项前端测试全部通过 |
| 版本隔离 | 数据库、COS、任务、点数和资产无串线 |
| 三秒钩子 | 20 条前 3 秒均有与准确文本一致的可见钩子 |
| 创意覆盖 | 至少 8 种布局、每种已使用布局至少 2 个变体，单一布局占比不超过 35% |
| 模板可用性 | 至少 4 个已发布模板，横竖屏各至少 2 个，API 非空且均通过真实渲染 |
| 阻断质检 | 第 21.5 节全部阻断项 100% 通过 |
| 5 条并行 | p50 不超过 25 分钟、p95 不超过 45 分钟，资源不越限 |
| 10 条压力 | 无崩溃、串线、重复调用、超额退款或账务错报；超时任务正确退款或在账本未知时显式待对账 |

任何一项安全、跨用户、账务或事实准确性门槛失败都直接阻止生产评审，不使用平均值掩盖严重问题。

## 27. 分阶段实施

### 阶段 A：协议与任务基础

交付 V3 包结构、三份机器可读 Schema、五类输入与三类创作入口 API 契约、独立数据库、状态机、fencing 租约、崩溃安全账务、发布 Saga、报价、预扣、输入上传和双版本隔离测试。

进入下一阶段条件：V3 基础测试通过，V2 460 项基线持续通过，V3 关闭时不影响现有网站。

### 阶段 B：文本、Qwen 与素材

交付媒体标准化、fun-asr、确定性文本对齐、Qwen3.7-Max 固定多模态端点导演、一次格式修复、图片语义分析、仅本次图片匹配和网站生图适配。

进入下一阶段条件：多类文案稳定产出合法 edit-plan，素材禁止来源和 required/optional 行为通过真实测试。

### 阶段 C：声音与 HyperFrames 渲染

交付每任务 ElevenLabs 音频生成、FFmpeg 唯一母带、组件注册表、动画白名单、4 个首发模板的版本/初始化数据/预览图与快照、render manifest、无网络 Node 渲染器、最终 mux、私有 COS 产物和完整阻断质检。

进入下一阶段条件：本地和测试服务器均能稳定渲染横竖屏样片，确定性和沙箱测试通过。

### 阶段 D：网站接入与交付

交付 V3 页面、平台口播选择、上传、三类创作入口、模板预览 API、报价、任务状态、结果、重试、任务中心、私有资产播放、结算和退款。

进入下一阶段条件：完整用户流程在测试环境通过，V2 无回归。

### 阶段 E：20 条样本验收

执行第 25.4 节样本并生成可审计报告。达到第 26 节全部门槛后，才能发起独立生产 Go/No-Go 评审。

## 28. PR 与部署边界

- 设计规格单独提交。
- 实施按阶段拆分为可独立测试和回滚的提交与 PR。
- 公共路由、资产播放、导航和管理后台按仓库协作组边界拆分审查。
- 每个 PR 必须给出变更文件、测试、风险和未完成项。
- 不从未合并分支直接部署。
- 测试部署前等待主 CI 完成并确认没有活动任务。
- 只部署本次提交涉及的文件。
- 部署前备份被替换文件，部署后验证服务健康、接口、真实任务和资产播放。
- 生产部署、生产数据库迁移、生产密钥配置和生产功能开启必须获得新的明确授权。

## 29. 已确认设计结论

- 独立 V3，V2 保持不动。
- 使用 Qwen3.7-Max，不使用 qwen-plus。
- 固定 `qwen3.7-max-2026-06-08` 作为首发模型。
- Qwen 只做导演，不生成或执行代码。
- 使用组件语法、主题令牌和白名单动画实现内容驱动创意。
- HyperFrames 与 GSAP 是唯一主渲染路线。
- 只使用本次上传图片，缺图调用现有生图 API。
- 不使用用户历史素材和不存在的公共素材库。
- ElevenLabs 负责 BGM 和 SFX，FFmpeg 负责最终混音。
- 输出一条 1080p MP4，自动进入用户视频资产库。
- 动态报价、上限预扣、实际结算、差额退还、最终失败全额退款。
- 第一阶段只进入测试环境，完成 20 条样本验收后再讨论生产。

本文没有授权修改 V2、合并 PR、部署测试站或生产站。下一步是在用户复核本规格后，编写逐任务实施计划。
