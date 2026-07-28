# AI Edit V2 Stable First Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在已合并的 AI Edit V2 Phase A 基础上，交付只使用 Shotstack 的第一版稳定智能剪辑，并在测试环境完成真实供应商端到端验收。

**Architecture:** 保持 `/api/v2/edit/*`、`ai_edit_v2.db`、独立 Worker 和 Provider 抽象。平台原文或外部 ASR 文本形成确定性字幕时间轴，Qwen 只输出 `edit-plan 2.0`，服务端解析素材并生成不可变 `resolved_plan`，再转换为 Shotstack `render_graph`；GPT 图片与 ElevenLabs 音频先入私有 COS，最终成片通过硬质检后才结算和入库。

**Tech Stack:** Python 3、SQLite WAL、原生 `unittest`、FFmpeg/FFprobe、DashScope Fun-ASR/Qwen、OpenAI 图片 API、ElevenLabs Music/SFX API、Shotstack Edit API、腾讯云 COS、原生工作台 JavaScript。

## Global Constraints

- 从最新 `origin/main` 创建独立 `codex/ai-edit-v2-stable-release` 分支和 worktree；不在 `codex/ai-edit-v2` 文档分支实现代码。
- 不修改旧“一键剪辑”、PR #20 V1、旧任务表或生产数据库。
- 只部署测试环境；生产部署必须另行授权。
- 第一版禁用 Remotion、HyperFrames、video-shotcraft、AI 短视频和自由代码 MG。
- 所有真实密钥只从测试服务器私有环境读取，不进入 Git、前端、数据库、fixture、日志或 PR。
- 所有外部提交都要保存幂等引用；提交结果不确定时先回查，禁止盲目重提可能收费的任务。
- 平台口播以原文为真值；外部输入只允许标点和断句修复，不得改变原意。
- “必须使用”素材使用率必须为 100%。
- 正常预算 2700 秒，修复预算 900 秒；每个外部阶段最多 2 次重试。
- 测试 Worker 默认 5，可配置为 10。
- 未交付通过硬质检的成片全额退款且只退款一次。
- 每个任务按 TDD 顺序执行；每个任务完成后运行该任务测试并独立提交。

---

## File Map

### Existing Phase A files to extend

- `server/content_domains/ai_edit_v2_schema.py`：冻结 `edit-plan 2.0`、`resolved_plan` 和 `render_graph` 校验。
- `server/content_domains/ai_edit_v2_store.py`：供应商任务、事件、检查点、渲染产物和降级记录。
- `server/content_domains/ai_edit_v2_pipeline.py`：阶段编排、预算、重试、恢复和终态退款。
- `server/content_domains/ai_edit_v2_asr.py`：真实 Fun-ASR 适配。
- `server/content_domains/ai_edit_v2_alignment.py`：平台原文对齐和外部文本标点约束。
- `server/content_domains/ai_edit_v2_media.py`：FFprobe/FFmpeg 标准化和后处理。
- `server/content_domains/ai_edit_v2_cos.py`：私有对象、短期签名、下载和删除。
- `server/content_domains/ai_edit_v2_billing.py`：报价、上限预扣、结算和退款幂等。
- `server/content_domains/ai_edit_v2_api.py`：用户接口、Shotstack Webhook 和结果字段。
- `server/content_domains/ai_edit_v2_feature.py`：能力开关和真实就绪检查。
- `site/workbench/video.html`：第一版创建入口。
- `site/workbench/tasks.js`：V2 状态、终态通知、播放和下载。
- `deploy/huangque-secrets.env.example`：变量名和安全默认值，不含真实值。

### New focused files

- `server/content_domains/ai_edit_v2_director.py`：Qwen 导演、结构化修复和提示边界。
- `server/content_domains/ai_edit_v2_templates.py`：已审核稳定模板目录、版本和内容驱动实例化参数。
- `server/content_domains/ai_edit_v2_materials.py`：四级素材解析和必须素材约束。
- `server/content_domains/ai_edit_v2_providers/base.py`：统一 Provider 结果、错误和费用类型。
- `server/content_domains/ai_edit_v2_providers/dashscope.py`：Fun-ASR 与 Qwen HTTP 客户端。
- `server/content_domains/ai_edit_v2_providers/openai_image.py`：缺图生成与 COS 回填。
- `server/content_domains/ai_edit_v2_providers/elevenlabs.py`：`music_v2` 和 `eleven_text_to_sound_v2`。
- `server/content_domains/ai_edit_v2_audio.py`：BGM/SFX 语义 cue、ducking 和混音。
- `server/content_domains/ai_edit_v2_shotstack.py`：稳定组件到 Shotstack Timeline、提交和回查。
- `server/content_domains/ai_edit_v2_quality.py`：内容、技术、画面和声音硬质检。
- `server/content_domains/ai_edit_v2_delivery.py`：成片 COS、视频资产库和完成事务。
- `scripts/ai_edit_v2_provider_smoke.py`：不打印密钥的真实供应商最小烟测。

---

### Task 1: Freeze Stable Capabilities and Provider Contracts

**Files:**
- Create: `server/content_domains/ai_edit_v2_providers/__init__.py`
- Create: `server/content_domains/ai_edit_v2_providers/base.py`
- Modify: `server/content_domains/ai_edit_v2_feature.py`
- Modify: `deploy/huangque-secrets.env.example`
- Test: `tests/test_ai_edit_v2_provider_base.py`
- Test: `tests/test_ai_edit_v2_feature.py`

**Interfaces:**
- Produces: `ProviderResult`, `ProviderError`, `RetryableProviderError`, `UnknownSubmissionError`.
- Produces: `capability()` with `shotstack=true` only when stable runtime is fully configured and all advanced renderers false.

- [ ] **Step 1: Write failing Provider contract tests**

```python
def test_provider_result_has_cost_and_request_identity():
    result = ProviderResult(
        provider="elevenlabs", capability="music", request_id="req-1",
        payload={"cos_key": "music/a.mp3"}, cost_units=12, elapsed_ms=900,
    )
    assert result.request_id == "req-1"
    assert result.cost_units == 12

def test_capability_disables_advanced_renderers(monkeypatch):
    monkeypatch.setenv("AI_EDIT_V2_ENABLED", "1")
    value = capability()
    assert value["renderers"]["remotion"] is False
    assert value["renderers"]["hyperframes"] is False
    assert value["generation"]["ai_video"] is False
```

- [ ] **Step 2: Run tests and verify failure**

Run: `python -m unittest tests.test_ai_edit_v2_provider_base tests.test_ai_edit_v2_feature -v`

Expected: FAIL because Provider types and stable capability fields do not exist.

- [ ] **Step 3: Implement the frozen Provider types**

```python
@dataclass(frozen=True)
class ProviderResult:
    provider: str
    capability: str
    request_id: str
    payload: dict[str, Any]
    cost_units: int
    elapsed_ms: int

class ProviderError(RuntimeError): pass
class RetryableProviderError(ProviderError): pass
class UnknownSubmissionError(ProviderError): pass
```

Add stable capability readiness for DashScope, OpenAI image, ElevenLabs, Shotstack, COS, FFmpeg and FFprobe. Add only variable names and safe defaults to the env example, including `ELEVENLABS_API_KEY`, `ELEVENLABS_MUSIC_MODEL=music_v2`, `ELEVENLABS_SFX_MODEL=eleven_text_to_sound_v2`, `AI_EDIT_V2_WORKERS=5`, time budgets and provider base URLs.

- [ ] **Step 4: Run tests**

Run: `python -m unittest tests.test_ai_edit_v2_provider_base tests.test_ai_edit_v2_feature tests.test_systemd_secrets -v`

Expected: PASS; secret scanner confirms no real credential is committed.

- [ ] **Step 5: Commit**

```bash
git add server/content_domains/ai_edit_v2_providers server/content_domains/ai_edit_v2_feature.py deploy/huangque-secrets.env.example tests/test_ai_edit_v2_provider_base.py tests/test_ai_edit_v2_feature.py
git commit -m "feat(ai-edit-v2): freeze stable provider capabilities"
```

### Task 2: Connect Fun-ASR and Deterministic Text Timeline

**Files:**
- Create: `server/content_domains/ai_edit_v2_providers/dashscope.py`
- Modify: `server/content_domains/ai_edit_v2_asr.py`
- Modify: `server/content_domains/ai_edit_v2_alignment.py`
- Test: `tests/test_ai_edit_v2_dashscope.py`
- Test: `tests/test_ai_edit_v2_alignment.py`
- Fixture: `tests/fixtures/ai_edit_v2/provider_responses/fun_asr_success.json`

**Interfaces:**
- Produces: `DashScopeClient.submit_asr(cos_url, reference) -> ProviderResult`.
- Produces: `DashScopeClient.query_asr(provider_task_id) -> ProviderResult`.
- Produces: `build_text_timeline(source_type, original_text, asr_result) -> dict`.

- [ ] **Step 1: Add failing platform and external-input tests**

```python
def test_platform_text_uses_original_words_with_asr_times():
    result = build_text_timeline("platform_video", "品牌价格是99元", ASR_FIXTURE)
    assert "".join(x["text"] for x in result["words"]) == "品牌价格是99元"

def test_external_cleanup_rejects_word_change():
    with self.assertRaises(AlignmentError):
        build_text_timeline("external_video", None, changed_meaning_fixture)
```

- [ ] **Step 2: Verify failure**

Run: `python -m unittest tests.test_ai_edit_v2_dashscope tests.test_ai_edit_v2_alignment -v`

Expected: FAIL because the real client and unified timeline function do not exist.

- [ ] **Step 3: Implement submit/query normalization**

Use `DASHSCOPE_API_KEY` only in the Authorization header. Normalize provider output to stable word and sentence records:

```python
{"words": [{"text": "品", "start_ms": 0, "end_ms": 120}],
 "sentences": [{"text": "品牌价格是99元", "start_ms": 0, "end_ms": 1800}]}
```

Persist provider task ID before polling. A timeout after submit raises `UnknownSubmissionError`; the pipeline must reconcile by reference or saved task ID before another submit.

- [ ] **Step 4: Implement deterministic timeline selection**

Platform input calls `align_platform_text`; external input calls `validate_punctuation_only`. Reject empty words, negative timestamps, non-monotonic ranges and insufficient platform alignment confidence with stable error codes.

- [ ] **Step 5: Run ASR and alignment tests**

Run: `python -m unittest tests.test_ai_edit_v2_dashscope tests.test_ai_edit_v2_alignment tests.test_ai_edit_v2_asr -v`

Expected: PASS, including punctuation-only and brand/number preservation.

- [ ] **Step 6: Commit**

```bash
git add server/content_domains/ai_edit_v2_providers/dashscope.py server/content_domains/ai_edit_v2_asr.py server/content_domains/ai_edit_v2_alignment.py tests/test_ai_edit_v2_dashscope.py tests/test_ai_edit_v2_alignment.py tests/fixtures/ai_edit_v2/provider_responses/fun_asr_success.json
git commit -m "feat(ai-edit-v2): connect fun asr text timeline"
```

### Task 3: Implement the Constrained Qwen Director

**Files:**
- Create: `server/content_domains/ai_edit_v2_director.py`
- Create: `server/content_domains/ai_edit_v2_templates.py`
- Modify: `server/content_domains/ai_edit_v2_schema.py`
- Test: `tests/test_ai_edit_v2_director.py`
- Test: `tests/test_ai_edit_v2_templates.py`
- Test: `tests/test_ai_edit_v2_schema.py`
- Fixture: `tests/fixtures/ai_edit_v2/provider_responses/qwen_edit_plan_success.json`

**Interfaces:**
- Consumes: deterministic `text_timeline`, user style text or stable template ID, target ratio and target duration.
- Produces: `generate_edit_plan(context, client, max_repairs=2) -> dict` validated as version `2.0`.
- Produces: `get_published_template(template_id, version=None) -> dict` and `list_published_templates() -> list[dict]`.

- [ ] **Step 1: Write failing director-boundary tests**

```python
def test_director_returns_semantic_plan_without_provider_fields():
    plan = generate_edit_plan(CONTEXT, fake_qwen)
    assert plan["version"] == "2.0"
    assert "tracks" not in json.dumps(plan)
    assert "cos_key" not in json.dumps(plan)

def test_director_stops_after_two_schema_repairs():
    with self.assertRaises(DirectorError) as exc:
        generate_edit_plan(CONTEXT, always_invalid_qwen, max_repairs=2)
    self.assertEqual(exc.exception.code, "director_schema_invalid")

def test_template_fixes_style_not_scene_coordinates():
    template = get_published_template("business_diagnostic")
    self.assertIn("component_family", template)
    self.assertNotIn("fixed_scenes", template)
    self.assertNotIn("material_coordinates", template)
```

- [ ] **Step 2: Verify failure**

Run: `python -m unittest tests.test_ai_edit_v2_director tests.test_ai_edit_v2_templates tests.test_ai_edit_v2_schema -v`

Expected: FAIL because director module and complete scene/audio schema are absent.

- [ ] **Step 3: Extend schema and implement Qwen prompts**

Require each scene to include `id`, `start_ms`, `end_ms`, `intent`, `layout`, `visual_type`, `headline`, `material_slots` and `transition`. Permit only stable component enums. Implement an audited template catalog whose published versions fix the component family, typography, palette relationships, motion intensity and sound policy, but never fixed scenes, headlines or material coordinates. The system prompt explicitly forbids transcript rewrite, provider fields, URLs and code. Repair prompts contain only schema errors and the previous model response, never credentials.

- [ ] **Step 4: Test schemas, repair count and forbidden keys**

Run: `python -m unittest tests.test_ai_edit_v2_director tests.test_ai_edit_v2_templates tests.test_ai_edit_v2_schema -v`

Expected: PASS; invalid duration, overlapping scenes, unknown components and forbidden fields fail deterministically.

- [ ] **Step 5: Commit**

```bash
git add server/content_domains/ai_edit_v2_director.py server/content_domains/ai_edit_v2_templates.py server/content_domains/ai_edit_v2_schema.py tests/test_ai_edit_v2_director.py tests/test_ai_edit_v2_templates.py tests/test_ai_edit_v2_schema.py tests/fixtures/ai_edit_v2/provider_responses/qwen_edit_plan_success.json
git commit -m "feat(ai-edit-v2): add constrained qwen director"
```

### Task 4: Resolve Materials and Generate Missing Images

**Files:**
- Create: `server/content_domains/ai_edit_v2_materials.py`
- Create: `server/content_domains/ai_edit_v2_providers/openai_image.py`
- Modify: `server/content_domains/ai_edit_v2_store.py`
- Modify: `server/content_domains/ai_edit_v2_cos.py`
- Test: `tests/test_ai_edit_v2_materials.py`
- Test: `tests/test_ai_edit_v2_openai_image.py`

**Interfaces:**
- Produces: `resolve_materials(job_id, plan, repositories, image_provider) -> resolved_plan`.
- Produces: `OpenAIImageProvider.generate(slot, idempotency_key) -> ProviderResult` whose payload contains an internal asset ID and COS key, never a permanent provider URL.

- [ ] **Step 1: Write failing priority and required-material tests**

```python
def test_material_priority_prefers_current_upload():
    resolved = resolve_materials(JOB, PLAN, repos_with_all_four_levels, fake_image)
    assert resolved["materials"]["product_1"]["source"] == "current_upload"

def test_required_material_must_be_used_once_or_more():
    with self.assertRaises(MaterialResolutionError) as exc:
        resolve_materials(JOB, plan_without_required_use, repos, fake_image)
    self.assertEqual(exc.exception.code, "required_material_unused")
```

- [ ] **Step 2: Verify failure**

Run: `python -m unittest tests.test_ai_edit_v2_materials tests.test_ai_edit_v2_openai_image -v`

Expected: FAIL because resolver and image Provider do not exist.

- [ ] **Step 3: Add persisted resolver records**

Store semantic query, time range, ratio, dimensions, source, asset ID, COS key, required flag, selected score and exclusion code. Do not store signed URLs. Reject duplicate, blurred, irrelevant or invalid-ratio candidates before selection.

- [ ] **Step 4: Implement GPT image generation and COS-first storage**

Generate only when the first three repositories have no qualified asset. Download provider output with size/content-type limits, upload to the job-owned private COS prefix, verify `head_object`, create the asset row, then return the internal record. A non-required generation failure marks `image_generation_degraded`; a required slot without any valid fallback fails the task.

- [ ] **Step 5: Run tests**

Run: `python -m unittest tests.test_ai_edit_v2_materials tests.test_ai_edit_v2_openai_image tests.test_ai_edit_v2_cos tests.test_ai_edit_v2_store -v`

Expected: PASS; no provider URL or signed URL is persisted.

- [ ] **Step 6: Commit**

```bash
git add server/content_domains/ai_edit_v2_materials.py server/content_domains/ai_edit_v2_providers/openai_image.py server/content_domains/ai_edit_v2_store.py server/content_domains/ai_edit_v2_cos.py tests/test_ai_edit_v2_materials.py tests/test_ai_edit_v2_openai_image.py
git commit -m "feat(ai-edit-v2): resolve materials and generate images"
```

### Task 5: Generate ElevenLabs Music and Sound Effects

**Files:**
- Create: `server/content_domains/ai_edit_v2_providers/elevenlabs.py`
- Create: `server/content_domains/ai_edit_v2_audio.py`
- Test: `tests/test_ai_edit_v2_elevenlabs.py`
- Test: `tests/test_ai_edit_v2_audio.py`
- Fixture: `tests/fixtures/ai_edit_v2/provider_responses/elevenlabs_music_headers.json`

**Interfaces:**
- Produces: `ElevenLabsProvider.generate_music(prompt, duration_ms, idempotency_key) -> ProviderResult`.
- Produces: `ElevenLabsProvider.generate_sfx(prompt, duration_ms, idempotency_key) -> ProviderResult`.
- Produces: `build_audio_plan(edit_plan, text_timeline) -> dict`.
- Produces: `mix_audio(video_path, voice_path, bgm_path, sfx, output_path, runner) -> str`.

- [ ] **Step 1: Write failing model, cue and degradation tests**

```python
def test_music_is_forced_instrumental():
    provider.generate_music("calm business", 30000, "job:music")
    assert transport.last_json["model_id"] == "music_v2"
    assert transport.last_json["force_instrumental"] is True

def test_sfx_avoids_protected_speech_ranges():
    plan = build_audio_plan(EDIT_PLAN, TIMELINE_WITH_PRICE)
    assert not any(cue_overlaps(c, PRICE_RANGE) for c in plan["sfx"])
```

- [ ] **Step 2: Verify failure**

Run: `python -m unittest tests.test_ai_edit_v2_elevenlabs tests.test_ai_edit_v2_audio -v`

Expected: FAIL because ElevenLabs Provider and audio planner do not exist.

- [ ] **Step 3: Implement ElevenLabs requests and COS storage**

Use `xi-api-key` from `ELEVENLABS_API_KEY`. Use `music_v2` and `eleven_text_to_sound_v2`; reject unexpected content type or empty output. Record request ID, elapsed time and provider cost metadata without logging headers. Upload outputs to private COS before adding them to `resolved_plan`.

- [ ] **Step 4: Implement cue limits, ducking and loudness processing**

Merge or choose between cues less than 300 ms apart. Exclude brand/product/number/price ranges from strong SFX. Build FFmpeg arguments as a list, use dialogue-led ducking and two-pass loudness measurement/application. Return stable errors for timeout, clipping, empty output or missing voice stream.

- [ ] **Step 5: Add allowed degradation behavior**

Music failure yields `music_generation_degraded` and continues without new BGM. Individual non-required SFX failure yields `sfx_generation_degraded` and removes that cue. Never treat these degradations as a full-provider success.

- [ ] **Step 6: Run tests**

Run: `python -m unittest tests.test_ai_edit_v2_elevenlabs tests.test_ai_edit_v2_audio tests.test_ai_edit_v2_media -v`

Expected: PASS, including instrumental flag, cue spacing, protected ranges and silent degradation.

- [ ] **Step 7: Commit**

```bash
git add server/content_domains/ai_edit_v2_providers/elevenlabs.py server/content_domains/ai_edit_v2_audio.py tests/test_ai_edit_v2_elevenlabs.py tests/test_ai_edit_v2_audio.py tests/fixtures/ai_edit_v2/provider_responses/elevenlabs_music_headers.json
git commit -m "feat(ai-edit-v2): generate stable music and sound effects"
```

### Task 6: Build and Reconcile Shotstack Renders

**Files:**
- Create: `server/content_domains/ai_edit_v2_shotstack.py`
- Modify: `server/content_domains/ai_edit_v2_schema.py`
- Modify: `server/content_domains/ai_edit_v2_store.py`
- Test: `tests/test_ai_edit_v2_shotstack.py`
- Fixture: `tests/fixtures/ai_edit_v2/provider_responses/shotstack_render_success.json`

**Interfaces:**
- Produces: `build_render_graph(resolved_plan, signed_assets, font_url) -> dict`.
- Produces: `ShotstackClient.submit(render_graph, reference) -> ProviderResult`.
- Produces: `ShotstackClient.reconcile(provider_task_id=None, reference=None) -> ProviderResult`.

- [ ] **Step 1: Write failing render-graph tests**

```python
def test_render_graph_uses_exact_caption_timestamps():
    graph = build_render_graph(RESOLVED_PLAN, SIGNED_ASSETS, FONT_URL)
    caption = find_caption(graph, "所有店都关门了")
    assert caption["start"] == 0.0
    assert caption["length"] == 1.84

def test_render_graph_rejects_advanced_components():
    with self.assertRaises(RenderGraphError):
        build_render_graph(PLAN_WITH_FREE_CODE_MG, SIGNED_ASSETS, FONT_URL)
```

- [ ] **Step 2: Verify failure**

Run: `python -m unittest tests.test_ai_edit_v2_shotstack tests.test_ai_edit_v2_schema -v`

Expected: FAIL because stable Shotstack adapter does not exist.

- [ ] **Step 3: Implement audited component mapping**

Map only `basic_caption`, `basic_card`, `broll_image`, `broll_video`, `standard_transition` and `audio_bed`. `audio_bed` receives the single mastered track produced by `mix_audio`; Shotstack must not independently mix the same BGM or SFX a second time. Use aligned transcript timestamps and the bundled Noto Sans SC URL; never call Shotstack Chinese auto-caption. Generate short-lived COS GET URLs only immediately before submit.

- [ ] **Step 4: Implement idempotent submit, polling and webhook reconciliation**

Persist the provider task ID in the same durable stage attempt before further work. On timeout, query by saved task ID or reference. Webhook events are deduplicated and only wake reconciliation; final status and output URL come from authenticated provider query.

- [ ] **Step 5: Run Shotstack tests**

Run: `python -m unittest tests.test_ai_edit_v2_shotstack tests.test_ai_edit_v2_store -v`

Expected: PASS for duplicate submit, timeout reconciliation, duplicate/out-of-order webhook and forbidden component tests.

- [ ] **Step 6: Commit**

```bash
git add server/content_domains/ai_edit_v2_shotstack.py server/content_domains/ai_edit_v2_schema.py server/content_domains/ai_edit_v2_store.py tests/test_ai_edit_v2_shotstack.py tests/fixtures/ai_edit_v2/provider_responses/shotstack_render_success.json
git commit -m "feat(ai-edit-v2): render stable shotstack timelines"
```

### Task 7: Wire the Recoverable End-to-End Pipeline

**Files:**
- Modify: `server/content_domains/ai_edit_v2_pipeline.py`
- Modify: `server/content_domains/ai_edit_v2_runtime.py`
- Modify: `server/content_domains/ai_edit_v2_store.py`
- Test: `tests/test_ai_edit_v2_pipeline.py`
- Test: `tests/test_ai_edit_v2_runtime.py`

**Interfaces:**
- Consumes: Task 2-6 adapters.
- Produces: `run_job(job_id, dependencies, db_path=None) -> dict`.
- Produces: restart-safe checkpoints for every stable state.

- [ ] **Step 1: Write failing full state-machine tests**

```python
def test_platform_job_reaches_quality_check_once():
    result = run_job(JOB_ID, fake_dependencies, db_path=DB)
    assert result["state"] == "quality_checking"
    assert submission_count("shotstack") == 1

def test_restart_reuses_generated_assets_and_provider_job():
    crash_after("rendering_submitted")
    run_job(JOB_ID, fake_dependencies, db_path=DB)
    assert submission_count("openai_image") == 1
    assert submission_count("elevenlabs_music") == 1
    assert submission_count("shotstack") == 1
```

- [ ] **Step 2: Verify failure**

Run: `python -m unittest tests.test_ai_edit_v2_pipeline tests.test_ai_edit_v2_runtime -v`

Expected: FAIL because Phase A pipeline does not execute stable providers end-to-end.

- [ ] **Step 3: Implement stable stage aggregation**

Execute `normalizing -> transcribing -> aligning -> directing -> resolving_materials -> generating_media -> rendering -> postprocessing -> quality_checking`. Each stage uses an input fingerprint and only reuses output when the fingerprint and stored artifact verification match.

- [ ] **Step 4: Implement retries, budgets and recovery**

Apply maximum 2 retries per provider stage, normal deadline 2700 seconds and repair deadline 900 seconds. Renew the job lease during long polling. On process restart, claim expired jobs and reconcile durable provider IDs before any resubmit.

- [ ] **Step 5: Run pipeline concurrency tests**

Run: `python -m unittest tests.test_ai_edit_v2_pipeline tests.test_ai_edit_v2_runtime tests.test_ai_edit_v2_store -v`

Expected: PASS with concurrent retry, lease expiry, duplicate worker and restart fixtures.

- [ ] **Step 6: Commit**

```bash
git add server/content_domains/ai_edit_v2_pipeline.py server/content_domains/ai_edit_v2_runtime.py server/content_domains/ai_edit_v2_store.py tests/test_ai_edit_v2_pipeline.py tests/test_ai_edit_v2_runtime.py
git commit -m "feat(ai-edit-v2): run recoverable stable pipeline"
```

### Task 8: Add Hard Quality Gates and Atomic Delivery

**Files:**
- Create: `server/content_domains/ai_edit_v2_quality.py`
- Create: `server/content_domains/ai_edit_v2_delivery.py`
- Modify: `server/content_domains/ai_edit_v2_billing.py`
- Modify: `server/content_domains/ai_edit_v2_pipeline.py`
- Test: `tests/test_ai_edit_v2_quality.py`
- Test: `tests/test_ai_edit_v2_delivery.py`
- Test: `tests/test_ai_edit_v2_billing.py`

**Interfaces:**
- Produces: `inspect_output(path, resolved_plan, runner) -> QualityReport`.
- Produces: `deliver(job_id, output_path, report, actual_cost, db_path=None) -> dict`.

- [ ] **Step 1: Write failing hard-gate tests**

```python
def test_caption_tofu_or_out_of_bounds_fails_quality():
    report = inspect_output(BAD_CAPTION_VIDEO, PLAN, fake_runner)
    assert report.passed is False
    assert "caption_invalid" in report.error_codes

def test_delivery_settles_and_inserts_asset_once():
    deliver(JOB, VIDEO, PASS_REPORT, 42, db_path=DB)
    deliver(JOB, VIDEO, PASS_REPORT, 42, db_path=DB)
    assert asset_count(JOB) == 1
    assert settlement_count(JOB) == 1
```

- [ ] **Step 2: Verify failure**

Run: `python -m unittest tests.test_ai_edit_v2_quality tests.test_ai_edit_v2_delivery tests.test_ai_edit_v2_billing -v`

Expected: FAIL because hard quality and delivery transaction modules do not exist.

- [ ] **Step 3: Implement quality checks**

Check decodable video/audio streams, 1080p target dimensions, duration tolerance, black/blank frames, rotation, caption safe area and font glyphs, required-material coverage, transcript facts, silence, clipping and dialogue/BGM/SFX balance. Emit stable codes and distinguish repairable from terminal defects.

- [ ] **Step 4: Implement repair and delivery transaction**

Repair only the failing layer within the 900-second budget. After a pass, upload final MP4 to the user-owned private COS prefix, verify object metadata, insert one video asset row, settle actual cost and transition to `completed`. If storage or verification fails, do not settle success; terminal failure invokes `refund_failure` once.

- [ ] **Step 5: Run quality, billing and failure-race tests**

Run: `python -m unittest tests.test_ai_edit_v2_quality tests.test_ai_edit_v2_delivery tests.test_ai_edit_v2_billing tests.test_collect_cos_and_refund -v`

Expected: PASS for duplicate delivery, concurrent failure, settlement/refund races and unplayable output.

- [ ] **Step 6: Commit**

```bash
git add server/content_domains/ai_edit_v2_quality.py server/content_domains/ai_edit_v2_delivery.py server/content_domains/ai_edit_v2_billing.py server/content_domains/ai_edit_v2_pipeline.py tests/test_ai_edit_v2_quality.py tests/test_ai_edit_v2_delivery.py tests/test_ai_edit_v2_billing.py
git commit -m "feat(ai-edit-v2): gate quality and deliver atomically"
```

### Task 9: Complete V2 API and Test-Workbench Experience

**Files:**
- Modify: `server/content_domains/ai_edit_v2_api.py`
- Modify: `server/content_domains/core.py`
- Modify: `site/workbench/video.html`
- Modify: `site/workbench/tasks.js`
- Test: `tests/test_ai_edit_v2_api.py`
- Test: `tests/test_ai_edit_v2_ui.js`

**Interfaces:**
- Produces: `POST /api/v2/edit/quote`, `POST /api/v2/edit/jobs`, `GET /api/v2/edit/jobs/{id}`, `POST /api/v2/edit/jobs/{id}/retry`, `GET /api/v2/edit/capabilities`.
- Produces: authenticated Shotstack webhook endpoint with event deduplication and active reconciliation.

- [ ] **Step 1: Write failing API/UI tests**

```python
def test_job_response_exposes_progress_but_not_provider_names():
    response = get_job(owner="alice", job_id=JOB)
    assert response["stage"] == "rendering"
    assert "provider" not in json.dumps(response).lower()

def test_completed_response_has_player_download_and_asset():
    response = get_job(owner="alice", job_id=COMPLETED)
    assert response["output"]["play_url"]
    assert response["output"]["download_url"]
    assert response["output"]["asset_id"]
```

- [ ] **Step 2: Verify failure**

Run: `python -m unittest tests.test_ai_edit_v2_api -v`

Run: `node --test tests/test_ai_edit_v2_ui.js`

Expected: FAIL because stable output, degradation and provider-neutral progress fields are incomplete.

- [ ] **Step 3: Complete owner-safe API responses**

Return quote range, held amount, stage, elapsed/estimated time, degradation list, quality summary, actual charge, refunded difference and signed play/download links. Never return COS keys, provider IDs, signed source inputs, stack traces or supplier names. Retry creates a successor task and a new quote/hold; it never mutates a terminal task.

- [ ] **Step 4: Complete the test-workbench UI**

Add platform video/external video/audio selection, creation mode, required/reference upload groups, ratio, optional target duration, quote confirmation, status polling, degradation display, quality result, playback, download and asset-library link. Do not expose renderer selection or advanced disabled features.

- [ ] **Step 5: Run API/UI and old-flow regressions**

Run: `python -m unittest tests.test_ai_edit_v2_api tests.test_ai_edit_v2_admin_pricing -v`

Run: `node --test tests/test_ai_edit_v2_ui.js tests/test_video_points_refresh.js`

Expected: PASS; old video page behavior remains compatible.

- [ ] **Step 6: Commit**

```bash
git add server/content_domains/ai_edit_v2_api.py server/content_domains/core.py site/workbench/video.html site/workbench/tasks.js tests/test_ai_edit_v2_api.py tests/test_ai_edit_v2_ui.js
git commit -m "feat(ai-edit-v2): expose stable editing workflow"
```

### Task 10: Add Provider Smoke Tests and Full Fake E2E

**Files:**
- Create: `scripts/ai_edit_v2_provider_smoke.py`
- Create: `tests/test_ai_edit_v2_e2e.py`
- Create: `tests/fixtures/ai_edit_v2/e2e/platform_video.json`
- Create: `tests/fixtures/ai_edit_v2/e2e/external_video.json`
- Create: `tests/fixtures/ai_edit_v2/e2e/audio_only.json`
- Modify: `docs/superpowers/plans/2026-07-28-ai-edit-v2-stable-release-plan.md`

**Interfaces:**
- Produces: `python scripts/ai_edit_v2_provider_smoke.py --provider <name>` with exit code only and redacted request IDs.
- Produces: three fake-provider E2E fixtures ending in a verified asset.

- [ ] **Step 1: Write failing E2E tests**

```python
def test_platform_video_e2e(): assert_run_fixture("platform_video", expected="completed")
def test_external_video_e2e(): assert_run_fixture("external_video", expected="completed")
def test_audio_only_e2e(): assert_run_fixture("audio_only", expected="completed")
```

- [ ] **Step 2: Verify failure**

Run: `python -m unittest tests.test_ai_edit_v2_e2e -v`

Expected: FAIL because fixtures and fake dependency bundle are not complete.

- [ ] **Step 3: Implement fake-provider E2E and smoke CLI**

The fake suite must exercise quote, hold, job creation, normalization, transcript, director, materials, generated image, BGM, SFX, Shotstack, quality, COS, settlement and asset delivery. The smoke CLI supports `dashscope-asr`, `dashscope-qwen`, `openai-image`, `elevenlabs-music`, `elevenlabs-sfx`, `shotstack` and `cos`; it must redact headers and never print response bodies containing signed URLs.

- [ ] **Step 4: Run targeted and full automated suites**

Run: `python -m unittest tests.test_ai_edit_v2_e2e -v`

Run: `python -m unittest discover -s tests -p 'test_ai_edit_v2*.py' -v`

Run: `node --test tests/test_ai_edit_v2_ui.js`

Expected: all PASS.

- [ ] **Step 5: Run repository regressions and secret scan**

Run the repository's documented backend and frontend test commands, then:

```bash
git diff --check
git grep -nE 'sk_[A-Za-z0-9_-]{20,}|xi-api-key:[[:space:]]*[^$]' -- ':!docs/superpowers/plans/2026-07-28-ai-edit-v2-stable-release-plan.md'
```

Expected: tests PASS; secret scan prints no credential.

- [ ] **Step 6: Commit**

```bash
git add scripts/ai_edit_v2_provider_smoke.py tests/test_ai_edit_v2_e2e.py tests/fixtures/ai_edit_v2/e2e docs/superpowers/plans/2026-07-28-ai-edit-v2-stable-release-plan.md
git commit -m "test(ai-edit-v2): cover stable provider pipeline"
```

### Task 11: PR Review and Test-Environment Deployment

**Files:**
- Modify only if required by verified deployment: `deploy/huangque-secrets.env.example`
- No real secret, database or generated artifact is committed.

**Interfaces:**
- Consumes: pushed commit with green CI and approved PR.
- Produces: test-environment deployment evidence; does not authorize production.

- [ ] **Step 1: Run verification before publishing**

Run: `git status --short --branch`

Run all Task 10 test commands again from a clean worktree. Expected: clean worktree and all PASS.

- [ ] **Step 2: Push branch and open a draft PR**

Push `codex/ai-edit-v2-stable-release`; open a draft PR targeting `main`. Include scope, disabled capabilities, database isolation, test evidence, migration behavior, rollback and the statement “test environment only; no production authorization.”

- [ ] **Step 3: Pass code, security and CI gates**

Require green CI and resolve all actionable review threads. Re-run tests after every review fix. Do not merge while any required check or review is unresolved.

- [ ] **Step 4: Inject test secrets outside Git**

The test-environment operator configures DashScope, OpenAI, ElevenLabs, Shotstack and private COS values in the service's private environment file. Confirm variable presence by name and length only; never print values. Keep `AI_EDIT_V2_ENABLED=0`.

- [ ] **Step 5: Run real provider smoke tests**

Run each `scripts/ai_edit_v2_provider_smoke.py` provider command once. Record UTC/Beijing timestamps, provider capability, request ID suffix, elapsed time and billed usage without response bodies or secrets. Expected: all exit 0.

- [ ] **Step 6: Deploy the pushed commit to test environment**

Deploy only files changed by the merged/pushed commit using the repository deployment procedure. Initialize the isolated V2 database, restart only the affected test service, verify its health endpoint, then set `AI_EDIT_V2_ENABLED=1` only if runtime capability is ready.

- [ ] **Step 7: Run three real end-to-end samples**

Submit one platform talking-head video, one external video and one audio-only job. Verify owner isolation, progress, duration, accurate Chinese captions, required materials, BGM/SFX ducking, 1080p MP4, COS storage, playback, download, actual settlement and video-asset insertion. Record total time and provider cost per sample.

- [ ] **Step 8: Exercise failure and rollback**

Disable one non-required media Provider and verify documented degradation. Force one terminal render failure and verify exactly one full refund. Restart the Worker during polling and verify reconcile without duplicate provider jobs. Rollback is `AI_EDIT_V2_ENABLED=0`, drain V2 Workers and restore the previous pushed commit; old editing remains unaffected.

- [ ] **Step 9: Publish deployment report**

Report exactly:

```text
分支：
提交：
修改文件：
是否部署：
部署文件：
是否重启服务：
验证结果：
真实样本耗时与费用：
降级/退款/重启恢复结果：
风险/未完成：
生产环境：未操作
```

Do not claim the first release ready until all three real samples and failure drills pass.

---

## Plan Completion Gate

Implementation is complete only when Tasks 1-10 are merged or present in one reviewable branch with all automated tests green, and Task 11 has produced verified test-environment evidence. Passing fake-provider tests alone is not deployment completion. Passing one happy-path render alone is not release completion. Production remains disabled until separately approved.
