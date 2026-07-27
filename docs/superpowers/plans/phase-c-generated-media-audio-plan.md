# AI 智能剪辑 V2 Phase C Generated Media and Audio Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 补齐缺失图片、短视频、图标插画、图表资产、AI BGM、转场/重音音效、声音增强和对白避让，使 Phase B 的素材缺口全部进入可恢复、可计量、受预扣与时间预算约束的真实生成链路。

**Architecture:** 所有第三方能力实现统一 Provider 接口，由策略层按能力、预算、截止时间和兼容协议选择主备路线。生成产物先质检，再上传私有 COS、入 V2 素材库和检查点；render graph 只引用 COS key。音频设计由确定性混音规格实现，不允许 BGM/SFX 遮盖对白。

**Tech Stack:** Python 3、SQLite、`unittest`、可注入 HTTP Provider、腾讯云 COS、FFmpeg/FFprobe、AI 图片/视频/音乐/音效 API。

## Global Constraints

- [ ] 依赖 Phase A、B 全部通过；不得直接调用旧页面中的供应商名称或把供应商字段展示给用户。
- [ ] Provider 配置只读环境变量；测试使用 fake，不提交或打印密钥、签名 URL、完整提示词和完整响应。
- [ ] 生成素材必须先过类型/尺寸/时长/可解码检查，再上传 COS 和入库；第三方临时 URL 不入数据库。
- [ ] 主备切换必须同时满足剩余预扣、剩余时间、协议兼容和内容硬门槛；否则减少非必须生成、稳定降级或失败退款。
- [ ] 人声主导内容 BGM 强制无歌词；纯视觉内容是否允许歌词必须写入 audio decision。
- [ ] 内部重试复用同一个 provider idempotency key，不新增用户扣费。

---

## 1. Phase C 精确文件结构

**Create**

- `server/content_domains/ai_edit_v2_providers/__init__.py`
- `server/content_domains/ai_edit_v2_providers/base.py`：统一类型、错误和主备策略。
- `server/content_domains/ai_edit_v2_providers/image.py`
- `server/content_domains/ai_edit_v2_providers/video.py`
- `server/content_domains/ai_edit_v2_providers/music.py`
- `server/content_domains/ai_edit_v2_providers/sfx.py`
- `server/content_domains/ai_edit_v2_generated_assets.py`：生成请求编排、质检、COS 和检查点。
- `server/content_domains/ai_edit_v2_audio.py`：声音设计、增强、ducking 和混音规格。
- `tests/test_ai_edit_v2_providers.py`
- `tests/test_ai_edit_v2_generated_assets.py`
- `tests/test_ai_edit_v2_audio.py`
- `tests/fixtures/ai_edit_v2/provider_responses/*.json`：脱敏成功/失败/超时响应。

**Modify**

- `server/content_domains/ai_edit_v2_assets.py`：执行 level 4 generation request 并回填槽位。
- `server/content_domains/ai_edit_v2_pipeline.py`：接入 generating_assets/designing_audio 和检查点恢复。
- `server/content_domains/ai_edit_v2_store.py`：provider usage/cost/switch reason 查询接口。
- `server/content_domains/renderers/shotstack_v2.py`：消费音频设计轨和生成素材。
- `server/content_domains/ai_edit_v2_quality.py`：生成媒体和最终混音硬检查。
- `deploy/huangque-secrets.env.example`：Provider 变量名。

## 2. 冻结接口

```python
class Provider(Protocol):
    capability: str
    def submit(self, request: ProviderRequest) -> ProviderResult: ...
    def query(self, provider_job_id: str, deadline_at: int) -> ProviderResult: ...

def choose_provider(capability: str, request: ProviderRequest,
                    candidates: list[Provider], budget: dict) -> Provider: ...
def execute_with_fallback(request: ProviderRequest,
                          candidates: list[Provider], store) -> ProviderResult: ...
def generate_missing_assets(job_id: str, requests: list[dict],
                            providers: dict[str, list[Provider]]) -> list[dict]: ...
def design_audio(plan: dict, transcript: dict, source_audio: dict) -> dict: ...
def build_audio_filter(audio_design: dict, paths: dict[str, str]) -> list[str]: ...
def validate_generated_asset(kind: str, path: str,
                             expected: dict, runner=subprocess.run) -> dict: ...
```

`audio_design` 固定含 `dialogue_track`、`bgm`、`sfx_cues`、`loudness`、`ducking`、`cleanup` 和 `lyrics_policy`；所有时间单位为毫秒，响度为 LUFS，峰值为 dBTP。

## Task 1: 统一 Provider、费用计量和安全主备切换

**Files:**
- Create: `server/content_domains/ai_edit_v2_providers/__init__.py`
- Create: `server/content_domains/ai_edit_v2_providers/base.py`
- Create: `tests/test_ai_edit_v2_providers.py`
- Modify: `server/content_domains/ai_edit_v2_store.py`

- [ ] **Step 1: 写失败测试**，主 Provider 429/5xx/明确失败时只在备用路线费用和预计用时均不突破上限时切换，并记录原因。
- [ ] **Step 2: 写提交不确定测试**，网络超时但可能已收费时必须先按 idempotency key/query 查原任务，禁止盲目重提。
- [ ] **Step 3: 写协议测试**，候选输出 MIME/比例/时长不兼容时不可切换；ProviderResult 缺 usage/cost/provider_job_id 被拒绝。
- [ ] **Step 4: 运行 `python -m unittest tests.test_ai_edit_v2_providers -v`**；预期失败。
- [ ] **Step 5: 实现 ProviderError 分类、指数退避上限、预算快照、选择策略和每次 attempt 持久化。
- [ ] **Step 6: 增加日志脱敏测试**，Authorization、query token、提示词和完整响应不进入日志。
- [ ] **Step 7: 重跑测试**；预期通过。
- [ ] **Step 8: 提交**

```powershell
git add server/content_domains/ai_edit_v2_providers server/content_domains/ai_edit_v2_store.py tests/test_ai_edit_v2_providers.py
git commit -m "feat(ai-edit-v2): add budget-aware provider routing"
```

## Task 2: 图片、图标插画和图表资产生成

**Files:**
- Create: `server/content_domains/ai_edit_v2_providers/image.py`
- Create: `server/content_domains/ai_edit_v2_generated_assets.py`
- Create: `tests/test_ai_edit_v2_generated_assets.py`
- Create: `tests/fixtures/ai_edit_v2/provider_responses/image-success.json`

**Interfaces:** Consumes semantic generation request; Produces material record with `source="generated"`、asset id、COS key、dimensions、cost and provider checkpoint。

- [ ] **Step 1: 写失败测试**，generation prompt 只能包含语义/风格/比例/避免项，不含用户名、原始完整文案、COS key 或签名 URL。
- [ ] **Step 2: 写图片质量测试**，解码失败、比例偏差、尺寸不足、空白占位拒绝；图标要求透明通道时无 alpha 拒绝。
- [ ] **Step 3: 写图表测试**，动态图表的数据必须来自 plan 的 `fact_id`，不得由生成模型虚构数字。
- [ ] **Step 4: 运行 `python -m unittest tests.test_ai_edit_v2_generated_assets -v`**；预期失败。
- [ ] **Step 5: 实现 image adapter、质量检查、私有 COS 上传、素材入库和 slot 回填；同一 request checkpoint 重放不再生成。
- [ ] **Step 6: 增加 required slot 失败测试**，生成失败且无合法降级素材时任务失败退款；非 required 可删除该镜头需求并记录。
- [ ] **Step 7: 重跑测试**；预期通过。
- [ ] **Step 8: 提交**

```powershell
git add server/content_domains/ai_edit_v2_providers/image.py server/content_domains/ai_edit_v2_generated_assets.py tests/test_ai_edit_v2_generated_assets.py tests/fixtures/ai_edit_v2/provider_responses/image-success.json
git commit -m "feat(ai-edit-v2): generate checked visual assets"
```

## Task 3: 缺失短视频生成与回填

**Files:**
- Create: `server/content_domains/ai_edit_v2_providers/video.py`
- Create: `tests/test_ai_edit_v2_generated_video.py`
- Create: `tests/fixtures/ai_edit_v2/provider_responses/video-success.json`
- Modify: `server/content_domains/ai_edit_v2_generated_assets.py`
- Modify: `server/content_domains/ai_edit_v2_assets.py`

- [ ] **Step 1: 写失败测试**，只有 `kind=image_or_video|video` 且 `generation_allowed=true` 的槽位能请求视频；时长不得超过槽位时长和预算。
- [ ] **Step 2: 写输出测试**，必须可解码、有视频流、无错误占位、比例匹配；音轨可选但存在时必须可解析。
- [ ] **Step 3: 写回填测试**，生成视频上传 COS 后 `resolved_plan.materials[slot_id]` 获得真实 asset id/cos key，Shotstack B-roll track 使用该槽位而非空白。
- [ ] **Step 4: 运行 `python -m unittest tests.test_ai_edit_v2_generated_video -v`**；预期失败。
- [ ] **Step 5: 实现 adapter、轮询/恢复、质检、COS 入库和 immutable resolved plan v+1。
- [ ] **Step 6: 写预算降级测试**，视频生成将突破上限时改用同语义图片＋受控镜头运动并记录 `budget_fallback_to_image`。
- [ ] **Step 7: 重跑测试**；预期通过。
- [ ] **Step 8: 提交**

```powershell
git add server/content_domains/ai_edit_v2_providers/video.py server/content_domains/ai_edit_v2_generated_assets.py server/content_domains/ai_edit_v2_assets.py tests/test_ai_edit_v2_generated_video.py tests/fixtures/ai_edit_v2/provider_responses/video-success.json
git commit -m "feat(ai-edit-v2): generate and bind missing video assets"
```

## Task 4: AI BGM 与歌词政策

**Files:**
- Create: `server/content_domains/ai_edit_v2_providers/music.py`
- Create: `server/content_domains/ai_edit_v2_audio.py`
- Create: `tests/test_ai_edit_v2_audio.py`
- Create: `tests/fixtures/ai_edit_v2/provider_responses/music-success.json`

- [ ] **Step 1: 写失败测试**，digital human/真人/访谈/课程等人声主导内容 `lyrics_policy` 必须为 `instrumental_only`；纯视觉内容才可由导演明确允许 vocal。
- [ ] **Step 2: 写时长/循环测试**，BGM 覆盖成片但不粗暴截断，循环点和淡入淡出位于安全区；生成长度计入报价与实际费用。
- [ ] **Step 3: 运行 `python -m unittest tests.test_ai_edit_v2_audio -v`**；预期失败。
- [ ] **Step 4: 实现 music adapter 和 audio design；Provider prompt 只描述情绪、BPM 范围、结构、无歌词规则和时长。
- [ ] **Step 5: 写输出检查**，音频可解码、时长、采样率、声道和峰值正常；失败可切备用但不得突破预算。
- [ ] **Step 6: 重跑测试**；预期通过。
- [ ] **Step 7: 提交**

```powershell
git add server/content_domains/ai_edit_v2_providers/music.py server/content_domains/ai_edit_v2_audio.py tests/test_ai_edit_v2_audio.py tests/fixtures/ai_edit_v2/provider_responses/music-success.json
git commit -m "feat(ai-edit-v2): generate policy-safe background music"
```

## Task 5: 转场音效、重音音效和语义同步

**Files:**
- Create: `server/content_domains/ai_edit_v2_providers/sfx.py`
- Create: `tests/test_ai_edit_v2_sfx.py`
- Create: `tests/fixtures/ai_edit_v2/provider_responses/sfx-success.json`
- Modify: `server/content_domains/ai_edit_v2_audio.py`

- [ ] **Step 1: 写 cue 测试**，只有语义转折、镜头切换或 MG 重点产生 cue；相邻 cue 小于 300ms 时合并/择优，避免音效堆叠。
- [ ] **Step 2: 写重音保护测试**，品牌、数字、价格口播区间不放遮盖性强音效，SFX 峰值和相对对白增益受限。
- [ ] **Step 3: 运行 `python -m unittest tests.test_ai_edit_v2_sfx -v`**；预期失败。
- [ ] **Step 4: 实现 sfx adapter、cue 去重、COS 检查点和 audio_design 回填。
- [ ] **Step 5: 增加 Provider 失败测试**，非必须音效生成失败可静默移除视觉不受影响，但必须记录费用、原因和最终未使用状态。
- [ ] **Step 6: 重跑测试**；预期通过。
- [ ] **Step 7: 提交**

```powershell
git add server/content_domains/ai_edit_v2_providers/sfx.py server/content_domains/ai_edit_v2_audio.py tests/test_ai_edit_v2_sfx.py tests/fixtures/ai_edit_v2/provider_responses/sfx-success.json
git commit -m "feat(ai-edit-v2): generate semantic transition and emphasis sfx"
```

## Task 6: 声音增强、对白 ducking 和最终混音

**Files:**
- Modify: `server/content_domains/ai_edit_v2_audio.py`
- Modify: `server/content_domains/renderers/shotstack_v2.py`
- Modify: `server/content_domains/ai_edit_v2_quality.py`
- Create: `tests/test_ai_edit_v2_audio_mix.py`

**Interfaces:** `build_audio_filter` returns an argv/filter script reference, never a shell string. Default targets: dialogue `-16 LUFS` stereo/`-19 LUFS` mono, true peak `<= -1 dBTP`; exact values remain versioned config.

- [ ] **Step 1: 写 filtergraph 测试**，降噪、去混响、人声增强、响度统一按检测结果启用；原始文件始终保留并可回退。
- [ ] **Step 2: 写 ducking 测试**，对白活跃区 BGM 自动下降，转场/重音 SFX 不遮挡关键事实，非对白区平滑恢复。
- [ ] **Step 3: 写原 BGM 处理测试**，检测不合适原音乐后可削弱/移除但不损坏人声；处理异常回退原音轨并标记 QC。
- [ ] **Step 4: 运行 `python -m unittest tests.test_ai_edit_v2_audio_mix -v`**；预期失败。
- [ ] **Step 5: 实现 FFmpeg 参数列表、两遍响度测量/应用、sidechain ducking 和音轨合成；超时和输出为空为稳定错误码。
- [ ] **Step 6: 扩展质量检查，验证静音、爆音、对白/BGM 比、关键信息可懂度和音轨存在。
- [ ] **Step 7: 重跑音频与 Shotstack 测试**；预期通过。
- [ ] **Step 8: 提交**

```powershell
git add server/content_domains/ai_edit_v2_audio.py server/content_domains/renderers/shotstack_v2.py server/content_domains/ai_edit_v2_quality.py tests/test_ai_edit_v2_audio_mix.py
git commit -m "feat(ai-edit-v2): mix enhanced dialogue with ducked audio"
```

## Task 7: 检查点、实际成本和 Phase B+C 端到端

**Files:**
- Modify: `server/content_domains/ai_edit_v2_pipeline.py`
- Modify: `server/content_domains/ai_edit_v2_billing.py`
- Modify: `server/content_domains/ai_edit_v2_assets.py`
- Create: `tests/test_ai_edit_v2_generated_e2e.py`
- Modify: `deploy/huangque-secrets.env.example`

- [ ] **Step 1: 写恢复测试**，图片/视频/BGM/SFX 每种成功检查点在 Worker 重启后复用，不重复调用、不重复记成本。
- [ ] **Step 2: 写成本测试**，每次 provider usage 汇入实际结算；总成本接近上限时先移除非必须 SFX，再视频降图片，仍不足则失败全退。
- [ ] **Step 3: 写端到端测试**，一个缺图片/短视频/BGM/SFX 的任务从 generating_assets 到 completed，所有槽位回填、COS key 有效、最终音轨过 QC。
- [ ] **Step 4: 运行 `python -m unittest tests.test_ai_edit_v2_generated_e2e -v`**；预期失败。
- [ ] **Step 5: 实现 pipeline stage aggregation、检查点指纹、实际费用汇总和降级顺序。
- [ ] **Step 6: env example 增加主备图片/视频/音乐/SFX Provider 变量名，不给实际值。
- [ ] **Step 7: 重跑 Phase B+C 全套测试**；预期通过。
- [ ] **Step 8: 提交**

```powershell
git add server/content_domains/ai_edit_v2_pipeline.py server/content_domains/ai_edit_v2_billing.py server/content_domains/ai_edit_v2_assets.py deploy/huangque-secrets.env.example tests/test_ai_edit_v2_generated_e2e.py
git commit -m "feat(ai-edit-v2): checkpoint generated media and actual cost"
```

## Phase C 验收

```powershell
python -m unittest tests.test_ai_edit_v2_providers tests.test_ai_edit_v2_generated_assets tests.test_ai_edit_v2_generated_video tests.test_ai_edit_v2_audio tests.test_ai_edit_v2_sfx tests.test_ai_edit_v2_audio_mix tests.test_ai_edit_v2_generated_e2e -v
python -m unittest tests.test_ai_edit_v2_shotstack tests.test_ai_edit_v2_quality tests.test_ai_edit_v2_delivery -v
python scripts/ci_validate.py
```

预期：五类生成资产全部可从检查点恢复；主备切换不突破点数/时间；人声视频无歌词；生成素材真实回填时间线；最终混音不压对白；没有 Provider URL、密钥或完整响应进入数据库和日志。
