# AI 智能剪辑 V3 Phase C HyperFrames and Audio Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 Phase A、B 已通过的前提下，交付固定版本、确定性、每任务无网络隔离的 HyperFrames 渲染链路，以及每任务 ElevenLabs BGM/SFX、唯一 48 kHz 双声道母带、静音画面渲染、最终 mux、完整阻断质检和私有 COS 发布链路。

**Architecture:** Python Worker 是唯一持有身份、密钥、V3 状态、供应商连接、COS 与账务能力的控制面；它生成并校验音频、冻结 render manifest，并只通过固定 `renderctl <action> <instance_id>` 边界启动每任务 systemd 沙箱。Node.js 22.x 渲染器只消费冻结 manifest 与只读本地素材，用固定 HyperFrames/GSAP 注册表编译同步 seek-safe composition，输出无声音画、关键帧和报告；Python 再用 FFmpeg 合并唯一母带，执行阻断质检，并通过私有 COS 与 Phase A 的共享资产发布 Saga 收敛。

**Tech Stack:** Python 3.12、`unittest`、SQLite/WAL、FFmpeg/FFprobe、ElevenLabs `music_v2` 与 `eleven_text_to_sound_v2`、Node.js 22.x、HyperFrames 0.7.84、GSAP 3.15.0、固定 Chromium 与字体发布包、systemd `DynamicUser` 沙箱、腾讯云私有 COS、JSON Schema 2020-12。

## Global Constraints

- [ ] 只在 `codex/ai-edit-v3` 分支执行；每个任务开始前运行 `git status --short --branch`、`git branch --show-current`、`git log --oneline -5`，发现不属于本计划的改动时保留并避开，不覆盖、不清理。
- [ ] Phase A、B 的测试与退出门槛必须先通过；Phase C 只消费它们冻结的任务、租约、账务、发布、媒体、时间线、edit-plan 和素材接口，不新建第二套状态机、Store、Provider 基类或发布裁决。
- [ ] JSON Schema 校验只复用 Phase A 已安装并版本报告的 `jsonschema.Draft202012Validator` 与三份冻结 Schema；Phase C 不新增 `pip install jsonschema`、不另建 Python 依赖文件、不在 CI 重复安装，若 Phase A 的依赖安装门槛未完成则不得开始 Phase C。
- [ ] V3 固定使用 `/api/v3/edit/*`、`AI_EDIT_V3_DB_PATH`、`edit_v3_*`、`ai-edit-v3:*`、`ai_edit_v3`、`{environment}/ai-edit-v3/{owner_hmac}/{job_id}/...` 和 `[ai-edit-v3]`；不得导入 V2 Store、V2 provider、V2 COS 前缀或修改 `ai_edit_v2.db`。
- [ ] `AI_EDIT_V3_ENABLED` 默认保持 `0`；本计划不授权打开测试或生产功能开关，不授权 push、PR、merge、测试部署、生产部署、生产数据库迁移、生产价格发布或真实生产计费。
- [ ] 输出只能是 `1920x1080` 或 `1080x1920`、H.264、`yuv420p`、AAC、MP4；所有协议时间使用整数毫秒，画面和唯一母带从 PTS 0 开始。
- [ ] Node.js 固定为 22.x，`package.json` 固定 `hyperframes: "0.7.84"` 与 `gsap: "3.15.0"`；Chromium、FFmpeg、FFprobe、字体、代码提交、`package-lock.json` 和发布包都记录精确版本或 SHA-256，禁止 `latest`。
- [ ] 生产渲染路径不得执行 `npm install`、`npm update`、`npx`、动态插件下载或网络字体加载；依赖安装、Chromium/字体封装和发布清单生成只发生在受控发布构建阶段。
- [ ] Qwen 或用户只能选择注册表中的语义 ID 和受限参数；渲染器不得接收或执行 HTML、CSS、JavaScript、GSAP、HyperFrames、Shader、Three.js、任意表达式或插件字段。
- [ ] 每个 composition 同步注册且只注册一条 `gsap.timeline({paused:true})`；host ID、内部 `data-composition-id` 和 `window.__timelines` key 完全相同，组装后 DOM ID 全局唯一。
- [ ] 禁止 `Date.now()`、`performance.now()`、未播种 `Math.random()`、定时器创建 timeline、无限循环、运行时布局测量、对 `.clip` 生命周期做外部可见性控制，以及 tween `display` 或原始 `visibility`。
- [ ] Node 只渲染静音画面：不创建 `<audio>`，所有 `<video>` 强制 `muted` 且音量为 0；`volume_fade` 只由 Python 编译为 FFmpeg 音频自动化，不进入 GSAP。
- [ ] 首发注册表必须包含 12 个布局且每个布局至少 2 个结构变体、14 个视觉动画、5 个转场、12 类覆盖层、4 个已发布模板；横竖屏快照必须覆盖全部布局和模板。
- [ ] 每条任务必须通过 ElevenLabs `music_v2` 新生成一条覆盖完整成片且无歌词的 BGM；SFX 使用 `eleven_text_to_sound_v2`，只生成导演声明的 cue，不读取历史音频库。
- [ ] 人声是主轨；口播期间 BGM 至少低 12 dB，最终综合响度 `-16 LUFS ± 2 LU`，true peak 不高于 `-1 dBTP`，输出唯一 48 kHz 双声道母带，不允许重复对白、削波或异常长静音。
- [ ] 渲染输入冻结后只读，拒绝绝对路径、`..`、反斜杠逃逸、URL、符号链接、硬链接、设备文件和非普通文件；打开文件句柄后再次校验真实路径、类型、大小和 SHA-256。
- [ ] 渲染进程不持有任何供应商密钥，启用 `PrivateNetwork=yes`、`PrivateUsers=yes`、`PrivateMounts=yes`、`NoNewPrivileges=yes`、`ProtectSystem=strict`、`ProtectHome=yes`、`PrivateTmp=yes`、`RestrictSUIDSGID=yes`；不得使用 Chromium `--no-sandbox`。
- [ ] 单个 render unit 上限固定为 2 vCPU、3 GiB RAM、8 GiB 临时磁盘、64 个进程或线程；超时、失租或中断必须终止整个 Chromium/Node/FFmpeg control group。
- [ ] 创作目标为预扣确认后 10–25 分钟；无修复绝对上限 45 分钟，首次明确可修复问题只允许原子追加一次 10 分钟，总上限 55 分钟，重启和重领不得重置时钟。
- [ ] 所有子进程使用 argv 数组且 `shell=False`；FFmpeg/FFprobe 只允许本地 `file` 与 `pipe` 协议，日志不得包含签名 URL、Cookie、Authorization、API Key 或未脱敏路径。
- [ ] 质检所有阻断项必须 100% 通过后才能结算和发布；第一次明确可修复问题最多进入一次修复，第二次失败、事实错误、required 素材缺失、跨 owner 素材或不可信 manifest 必须失败。
- [ ] 最终对象始终私有，交付 key 不可变并包含 render attempt 与内容 SHA；可读性必须用 `Range: bytes=0-0` 的签名 GET 验证并得到 HTTP 206，不以 HEAD 替代。
- [ ] 单元测试只使用 fake transport、合成媒体和脱敏 fixture；真实 ElevenLabs、COS、账务、Qwen、test-server systemd smoke 均需要另一次明确测试环境授权。
- [ ] 每个任务先写精确失败测试并观察 RED，再写最小实现并观察 GREEN；每个任务独立 commit，提交前运行该任务的定向测试与 `git diff --check`。

---

## 1. Frozen Phase C File Map

### Python control plane

```text
server/content_domains/ai_edit_v3/
  contracts.py                 # extend Phase A strict render-manifest validation and canonical freeze
  runtime.py                   # wire audio generator, renderer release preflight and sandbox capability
  store.py                     # persist audio/render/QC/delivery checkpoints through existing V3 transactions
  pipeline.py                  # only owner of generating_audio -> completed state transitions
  media.py                     # extend Phase B process runner with final mux and decoded hash evidence
  audio.py                     # audio intents, per-task generation orchestration and unique master build
  quality.py                   # normalized blocking/reparable evidence; never changes state itself
  delivery.py                  # immutable private COS stage, Range GET and AssetPublisher client use
  providers/
    elevenlabs.py              # stateless ElevenLabs protocol adapter; no DB access
  renderers/
    __init__.py                # public Renderer Protocol and RenderResult export
    hyperframes.py             # only Python-to-renderctl boundary
server/ai_edit_v3_worker.py    # consume the extended RuntimeDependencies only
```

### Fixed Node renderer release

```text
server/ai_edit_v3_renderer/
  package.json
  package-lock.json
  hyperframes.json
  renderer-release.lock.json
  src/
    render.mjs
    release-manifest.mjs
    parse-canonical-json.mjs
    validate-manifest.mjs
    validate-files.mjs
    compile-project.mjs
    render-hyperframes.mjs
    report.mjs
    registry/
      index.mjs
      layout-primitives.mjs
      layouts.mjs
      overlays.mjs
      animations.mjs
      transitions.mjs
      themes.mjs
  assets/
    fonts/
      NotoSansSC-Regular.woff2
      NotoSansSC-Bold.woff2
      OFL.txt
  test/
    release-manifest.test.mjs
    validate-manifest.test.mjs
    validate-files.test.mjs
    registry.test.mjs
    compile-project.test.mjs
    layouts.test.mjs
    animations.test.mjs
    transitions.test.mjs
    render.test.mjs
    determinism.test.mjs
    security.test.mjs
    render-fixtures.mjs
    fixtures/
      landscape/
      portrait/
      animations/
      transitions/
```

### Template catalog, deployment and tests

```text
server/content_domains/ai_edit_v3/catalog/templates-v1.json
server/content_domains/ai_edit_v3/catalog/template-previews/
  commercial-diagnostic-landscape-v1.png
  commercial-diagnostic-portrait-v1.png
  editorial-explainer-landscape-v1.png
  editorial-explainer-portrait-v1.png
deploy/systemd/huangque-ai-edit-v3.service
deploy/systemd/huangque-ai-edit-v3-render@.service
deploy/libexec/huangque-ai-edit-v3-renderctl
deploy/sudoers.d/huangque-ai-edit-v3-render
deploy/tmpfiles.d/huangque-ai-edit-v3.conf
deploy/huangque-secrets.env.example
tests/test_ai_edit_v3_renderer_release.py
tests/test_ai_edit_v3_render_manifest.py
tests/test_ai_edit_v3_template_catalog.py
tests/test_ai_edit_v3_elevenlabs.py
tests/test_ai_edit_v3_secrets_example.py
tests/test_ai_edit_v3_audio.py
tests/test_ai_edit_v3_render_sandbox.py
tests/test_ai_edit_v3_hyperframes.py
tests/test_ai_edit_v3_mux.py
tests/test_ai_edit_v3_quality.py
tests/test_ai_edit_v3_delivery.py
tests/test_ai_edit_v3_phase_c_pipeline.py
tests/test_ai_edit_v3_ci_wiring.py
tests/fixtures/ai_edit_v3/phase-c-cases.json
docs/operations/ai-edit-v3-renderer-release.md
docs/operations/ai-edit-v3-renderer-runbook.md
docs/verification/ai-edit-v3-phase-c.md
.github/workflows/ci.yml
```

`server/ai_edit_v3_renderer/renderer-release.lock.json` is generated from actual build inputs and committed with real hashes; it never contains example hashes. Chromium itself is packaged in the content-addressed deployment artifact rather than committed to Git. Template preview PNGs are deterministic outputs generated from the pinned renderer and are committed because they are product catalog assets.

## 2. Frozen Cross-Phase Interfaces

Phase C must consume these Phase A/B contracts without renaming them:

```python
@dataclass(frozen=True)
class StageContext:
    claim: LeaseClaim
    attempt_id: str
    stage_attempt_id: str
    deadline_at: float
    assert_active: Callable[[], None]

@dataclass(frozen=True)
class ProviderResult:
    provider: str
    capability: str
    request_id: str | None
    payload: Mapping[str, Any]
    usage: Mapping[str, int | float]
    elapsed_ms: int

@dataclass(frozen=True)
class TextTimeline:
    duration_ms: int
    captions: tuple[Caption, ...]
    source_segments: tuple[SourceSegment, ...]
    authoritative_text_sha256: str | None
    alignment_coverage: float

@dataclass(frozen=True)
class ResolvedMaterial:
    slot_id: str
    source: Literal["current_upload", "generated", "omitted_optional"]
    material_id: str | None
    cos_key: str | None
    match_score: float | None
    reason: str

def validate_render_manifest(
    manifest: Any,
    *,
    sandbox_root: Path,
) -> dict[str, Any]: ...

class AssetPublisher(Protocol):
    def register_generation(
        self, mode: str, source_job_id: str, generation: int, idempotency_key: str
    ) -> PublicationDecision: ...

    def prepare_hidden(
        self, mode: str, source_job_id: str, owner: str, object_key: str,
        generation: int, idempotency_key: str
    ) -> PublicationDecision: ...

    def commit_publish(
        self, mode: str, source_job_id: str, generation: int, idempotency_key: str
    ) -> PublicationDecision: ...

    def cancel_publish(
        self, mode: str, source_job_id: str, generation: int, idempotency_key: str
    ) -> PublicationDecision: ...

    def query_decision(
        self, mode: str, source_job_id: str, idempotency_key: str
    ) -> PublicationDecision | None: ...
```

`PublicationDecision` is the Phase A frozen dataclass with `status: Literal["accepted", "stale_generation", "publish_won", "cancel_won"]`, `current_generation: int` and `asset_id: str | None`.

`RuntimeDependencies` keeps the Phase A fields `store, clock, points, assets, cos, tts, asr, director, image_generator, audio_generator, renderer, process_supervisor, stage_handlers`. Phase C replaces the `object | None` annotations for `cos` and `audio_generator` with the Protocols below and supplies the concrete renderer; it does not add an alternate dependency container.

The Python renderer boundary remains:

```python
@dataclass(frozen=True)
class RenderRequest:
    instance_id: str
    job_id: str
    attempt: int
    manifest_path: Path
    input_root: Path
    output_root: Path
    manifest_sha256: str
    renderer_build_id: str
    deadline_at: float

@dataclass(frozen=True)
class RenderResult:
    silent_video_relpath: str
    sha256: str
    report_relpath: str
    snapshots: tuple[str, ...]
    environment: Mapping[str, str]
    performance: Mapping[str, int | float]

class Renderer(Protocol):
    def render(
        self, manifest_path: Path, input_root: Path, output_dir: Path, *,
        instance_id: str, deadline_at: float,
        assert_active: Callable[[], None]
    ) -> RenderResult: ...

    def terminate(self, instance_id: str) -> None: ...
```

`HyperframesRenderer.render()` constructs the richer `RenderRequest` after reading the already validated manifest; callers continue using the frozen `Renderer` Protocol.

Phase C supporting types are also frozen once and reused by every task:

```python
@dataclass(frozen=True)
class TimeRange:
    start_ms: int
    end_ms: int

@dataclass(frozen=True)
class VolumeFade:
    target_id: str
    start_ms: int
    end_ms: int
    from_db: float
    to_db: float

@dataclass(frozen=True)
class TemplateContract:
    template_id: str
    version: int
    status: Literal["published"]
    title: str
    category: Literal["商业诊断", "编辑式知识讲解"]
    creative_direction: Literal["commercial_diagnostic", "editorial_explainer"]
    supported_ratios: tuple[Literal["16:9", "9:16"], ...]
    allowed_layouts: tuple[str, ...]
    capabilities_sha256: str
    preview_relative_path: str
    preview_sha256: str
    catalog_sha256: str

class PrivateCos(Protocol):
    def put_file(
        self, source: Path, object_key: str, content_type: str, *,
        private: bool, if_absent: bool
    ) -> Mapping[str, Any]: ...

    def presign_get(self, object_key: str, *, expires_seconds: int) -> str: ...

    def range_get(
        self, signed_url: str, *, start: int, end: int
    ) -> tuple[int, Mapping[str, str], bytes]: ...

@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    elapsed_ms: int

class CommandRunner(Protocol):
    def run(
        self, argv: Sequence[str], *, timeout_seconds: float,
        environment: Mapping[str, str]
    ) -> CommandResult: ...

class VisualInspector(Protocol):
    def inspect(
        self, request: Mapping[str, Any], *, deadline_at: float
    ) -> ProviderResult: ...
```

Pipeline stage helpers return the Phase A `StageOutcome(next_state, checkpoint, checkpoint_input_sha256, provider_result)` type.

## 3. Frozen Registry Contract

The IDs are exact and cannot be aliased:

- Layouts: `speaker_fullscreen`, `speaker_left_info_right`, `speaker_right_evidence_left`, `material_fullscreen_speaker_pip`, `product_hero`, `editorial_collage`, `comparison_split`, `steps_stack`, `number_proof`, `quote_reversal`, `method_timeline`, `cta_offer`.
- Overlays: `standard_caption`, `emphasis_caption`, `headline_block`, `chapter_label`, `lower_third`, `number_proof`, `bullet_list`, `info_card`, `quote_card`, `product_tag`, `step_indicator`, `cta_hold`.
- Visual animations: `fade`, `slide`, `scale`, `rotate`, `wipe`, `stagger`, `count_up`, `image_pan_zoom`, `card_reveal`, `stamp`, `light_sweep`, `highlight_draw`, `split_screen`, `subtitle_pop`.
- Audio automation outside Node: `volume_fade`.
- Transitions: `hard_cut`, `soft_wipe`, `directional_slide`, `light_flash`, `card_match_cut`.
- Template IDs: `commercial_diagnostic_landscape_v1`, `commercial_diagnostic_portrait_v1`, `editorial_explainer_landscape_v1`, `editorial_explainer_portrait_v1`.

Every registry entry exports a frozen contract containing `id`, `version`, `supportedRatios`, `variants`, `requiredSlots`, `optionalSlots`, `fallbackVariant`, `allowedOverlays`, `allowedAnimations`, `safeAreas` and a pure compiler function. Registry SHA is computed from canonical contract JSON, not JavaScript source ordering.

---

### Task 1: Pin the renderer release and freeze its secure manifest boundary

**Files:**

- Create: `server/ai_edit_v3_renderer/package.json`
- Create: `server/ai_edit_v3_renderer/package-lock.json`
- Create: `server/ai_edit_v3_renderer/hyperframes.json`
- Create: `server/ai_edit_v3_renderer/src/release-manifest.mjs`
- Create: `server/ai_edit_v3_renderer/renderer-release.lock.json`
- Create: `server/ai_edit_v3_renderer/test/release-manifest.test.mjs`
- Create: `server/ai_edit_v3_renderer/assets/fonts/NotoSansSC-Regular.woff2`
- Create: `server/ai_edit_v3_renderer/assets/fonts/NotoSansSC-Bold.woff2`
- Create: `server/ai_edit_v3_renderer/assets/fonts/OFL.txt`
- Create: `tests/test_ai_edit_v3_renderer_release.py`
- Create: `docs/operations/ai-edit-v3-renderer-release.md`

**Interfaces:**

```javascript
export async function inspectRendererRelease({
  repoRoot,
  releaseRoot,
  nodePath,
  chromiumPath,
  ffmpegPath,
  ffprobePath,
}) // -> Promise<RendererRelease>

export function canonicalReleaseBytes(release) // -> Buffer
export function computeRendererBuildId(releaseWithoutBuildId) // -> "sha256:<64 hex>"
export async function writeRendererReleaseLock(release, destination) // atomic write
export async function writeArtifactAttestation({rendererBuildId, archivePath, destination})
```

`RendererRelease.schema_version` is the JSON integer `1`; writers emit exactly `1` and readers fail closed on every other value or type. The remaining fields are `renderer_build_id`, `git_commit`, `package_lock_sha256`, exact Node/Chromium/FFmpeg/FFprobe version strings and binary SHA-256 values, exact HyperFrames/GSAP versions, locale `C.UTF-8`, timezone `UTC`, encoder arguments, thread count, and a sorted list of `{relative_path, sha256}` font records. `renderer_build_id` hashes canonical release inputs with both ID and archive attestation omitted. After the immutable archive is built, `writeArtifactAttestation()` writes a separate adjacent record containing `renderer_build_id` and the actual `release_archive_sha256`; keeping the attestation outside the archive avoids a self-referential hash.

- [ ] **Step 1 (RED): Write the pinned-dependency test.**

```javascript
test("package pins the only renderer libraries", async () => {
  const pkg = JSON.parse(await readFile(new URL("../package.json", import.meta.url)));
  assert.deepEqual(pkg.engines, {node: ">=22 <23"});
  assert.equal(pkg.dependencies.hyperframes, "0.7.84");
  assert.equal(pkg.dependencies.gsap, "3.15.0");
  assert.equal(Object.keys(pkg.dependencies).sort().join(","), "gsap,hyperframes");
});
```

- [ ] **Step 2 (RED): Write release-manifest tests** that use temporary fake executables and real font bytes to prove `schema_version === 1`, rejection of `"1"`, `0`, `2` and missing schema version, sorted canonical output, SHA calculation, exact-version capture, missing binary rejection, Node major-version rejection, changed font/hash detection, and build-ID change after any input changes.
- [ ] **Step 3: Run `npm test -- test/release-manifest.test.mjs` from `server/ai_edit_v3_renderer`** and confirm RED because `package.json` and `release-manifest.mjs` do not exist.
- [ ] **Step 4 (GREEN): Create the exact `package.json`.**

```json
{
  "name": "huangque-ai-edit-v3-renderer",
  "version": "1.0.0",
  "private": true,
  "type": "module",
  "engines": {"node": ">=22 <23"},
  "scripts": {
    "test": "node --test",
    "release:lock": "node src/release-manifest.mjs",
    "hf:check": "hyperframes check",
    "hf:keyframes": "hyperframes keyframes",
    "hf:snapshot": "hyperframes snapshot",
    "render:fixtures": "node test/render-fixtures.mjs"
  },
  "dependencies": {
    "gsap": "3.15.0",
    "hyperframes": "0.7.84"
  }
}
```

- [ ] **Step 5 (GREEN): Generate and verify the lockfile** with `npm install --package-lock-only --ignore-scripts`, then run `npm ci --ignore-scripts` and `npm ls hyperframes gsap --depth=0`; expected versions are exactly `hyperframes@0.7.84` and `gsap@3.15.0`, with no `invalid` or `extraneous`.
- [ ] **Step 6 (GREEN): Implement `release-manifest.mjs`** so it invokes version probes with argv arrays, hashes binaries and fonts by streaming bytes, sorts all maps/lists before canonicalization, rejects missing hashes, writes lock and external archive attestation through same-directory temporary files plus `fsync` and rename, and never executes package-manager commands.
- [ ] **Step 7: Add the two Noto Sans SC WOFF2 files and OFL license** from the approved release input, then run `npm run release:lock -- --release-root . --chromium <approved-local-browser> --ffmpeg <approved-local-ffmpeg> --ffprobe <approved-local-ffprobe>` in the release-build environment; commit only the lock containing the real measured hashes.
- [ ] **Step 8: Add the Python cross-check** that recomputes `package-lock.json`, font and lock hashes, verifies the adjacent archive attestation, and fails runtime preflight when the active release or archive differs from `renderer_build_id`.
- [ ] **Step 9: Document the immutable release layout** `/opt/huangque/ai-edit-v3-renderer/releases/<renderer_build_id>` plus an atomically switched `/opt/huangque/ai-edit-v3-renderer/current` link; document that activation and deployment require separate authorization.
- [ ] **Step 10: Run GREEN verification.**

```powershell
Push-Location server/ai_edit_v3_renderer
npm ci --ignore-scripts
npm ls hyperframes gsap --depth=0
npm test -- test/release-manifest.test.mjs
Pop-Location
python -m unittest tests.test_ai_edit_v3_renderer_release -v
git diff --check
```

Expected: all tests PASS; version listing shows only the exact two pins; release tests prove a one-byte dependency/font/binary change changes `renderer_build_id`.

#### Manifest and file-security sub-cycle

**Files:**

- Modify: `server/content_domains/ai_edit_v3/contracts.py`
- Modify: `server/content_domains/ai_edit_v3/schemas/render-manifest-v1.schema.json`
- Create: `server/ai_edit_v3_renderer/src/parse-canonical-json.mjs`
- Create: `server/ai_edit_v3_renderer/src/validate-manifest.mjs`
- Create: `server/ai_edit_v3_renderer/src/validate-files.mjs`
- Create: `server/ai_edit_v3_renderer/test/validate-manifest.test.mjs`
- Create: `server/ai_edit_v3_renderer/test/validate-files.test.mjs`
- Create: `server/ai_edit_v3_renderer/test/security.test.mjs`
- Create: `tests/test_ai_edit_v3_render_manifest.py`
- Create: `tests/fixtures/ai_edit_v3/render-manifest-valid.json`

**Interfaces:**

```python
@dataclass(frozen=True)
class FrozenRenderManifest:
    path: Path
    sha256: str
    document: Mapping[str, Any]

def freeze_render_manifest(
    document: Mapping[str, Any],
    destination: Path,
    *,
    sandbox_root: Path,
) -> FrozenRenderManifest: ...
```

```javascript
export function parseCanonicalJson(bytes, limits) // -> plain object
export function validateManifest(document, expected) // -> frozen normalized object
export async function verifyInputFiles({manifest, inputRoot}) // -> VerifiedFile[]
```

`limits` is fixed to 512 KiB UTF-8, depth 24, 5000 total array elements and 4000 characters per string. `expected` contains the request manifest SHA, renderer build ID, registry SHA and schema SHA. `VerifiedFile` contains `relativePath`, `realPath`, `size`, `sha256`, `mode`, `nlink` and an open file descriptor that is closed only after the read-only project input is sealed.

- [ ] **Step 1 (RED): Add Python tests** for canonical atomic write, manifest SHA, `additionalProperties: false`, exact output dimensions/ratio, exactly one `master_audio`, silent-or-null `source_video`, valid component IDs and manifest/schema/build hash mismatch.
- [ ] **Step 2 (RED): Add strict-JSON tests** for duplicate keys, BOM, invalid UTF-8, `NaN`, `Infinity`, multiple roots, trailing content, excessive depth/count/string length and prototype-pollution keys `__proto__`, `constructor`, `prototype`.
- [ ] **Step 3 (RED): Add path and file tests** for `/absolute`, `C:\drive`, `../escape`, `a\..\b`, `file:`, `http:`, NUL, symlink, hard link with `nlink > 1`, FIFO/device, hash mismatch, size mismatch, case-collision and file replacement between first stat and open.
- [ ] **Step 4: Run the two RED suites.**

```powershell
python -m unittest tests.test_ai_edit_v3_render_manifest -v
Push-Location server/ai_edit_v3_renderer
npm test -- test/validate-manifest.test.mjs test/validate-files.test.mjs test/security.test.mjs
Pop-Location
```

Expected: imports or assertions fail because the strict parser and validators are absent.

- [ ] **Step 5 (GREEN): Implement Python canonical freeze** with `ensure_ascii=False`, sorted keys, compact separators, UTF-8, same-directory temporary file, file `fsync`, directory `fsync`, rename, final read-back hash and the existing `validate_render_manifest()` cross-field checks.
- [ ] **Step 6 (GREEN): Implement Node canonical parsing** without `eval`, `Function`, YAML, reviver side effects or dynamic import; duplicate-key scanning must happen before `JSON.parse`, and accepted data must be recursively copied into null-prototype objects and deep-frozen.
- [ ] **Step 7 (GREEN): Implement manifest semantic checks** for exact versions, dimensions, integer millisecond/frame fields, one audio master, silent video, registry allowlists, absent URL/code/output-path fields, globally unique IDs, contiguous scenes and valid asset references.
- [ ] **Step 8 (GREEN): Implement file verification** with Linux `O_RDONLY | O_NOFOLLOW`, `lstat`/`fstat`, `nlink === 1`, containment under the resolved input root, streamed SHA-256 and post-open size/type verification; reject a manifest before starting Chromium when any file fails.
- [ ] **Step 9: Add a security assertion** that searches every accepted manifest key recursively and rejects `html`, `css`, `javascript`, `script`, `expression`, `plugin`, `url`, `output_path`, `command`, `env` and `systemd_property`.
- [ ] **Step 10: Run GREEN verification.**

```powershell
python -m unittest tests.test_ai_edit_v3_render_manifest tests.test_ai_edit_v3_contracts tests.test_ai_edit_v3_schemas -v
Push-Location server/ai_edit_v3_renderer
npm test -- test/validate-manifest.test.mjs test/validate-files.test.mjs test/security.test.mjs
Pop-Location
git diff --check
```

Expected: all malicious fixtures are rejected before Chromium; the valid fixture yields the same SHA in Python and Node.

- [ ] **Step 23: Commit the complete release and manifest boundary.**

```powershell
git add server/ai_edit_v3_renderer/package.json server/ai_edit_v3_renderer/package-lock.json server/ai_edit_v3_renderer/hyperframes.json server/ai_edit_v3_renderer/renderer-release.lock.json server/ai_edit_v3_renderer/assets/fonts server/ai_edit_v3_renderer/src/release-manifest.mjs server/ai_edit_v3_renderer/src/parse-canonical-json.mjs server/ai_edit_v3_renderer/src/validate-manifest.mjs server/ai_edit_v3_renderer/src/validate-files.mjs server/ai_edit_v3_renderer/test/release-manifest.test.mjs server/ai_edit_v3_renderer/test/validate-manifest.test.mjs server/ai_edit_v3_renderer/test/validate-files.test.mjs server/ai_edit_v3_renderer/test/security.test.mjs server/content_domains/ai_edit_v3/contracts.py server/content_domains/ai_edit_v3/schemas/render-manifest-v1.schema.json tests/test_ai_edit_v3_renderer_release.py tests/test_ai_edit_v3_render_manifest.py tests/fixtures/ai_edit_v3/render-manifest-valid.json docs/operations/ai-edit-v3-renderer-release.md
git commit -m "build(ai-edit-v3): pin secure renderer release"
```

### Task 2: Build the frozen component registry and safe compiler

**Files:**

- Create: `server/ai_edit_v3_renderer/src/registry/index.mjs`
- Create: `server/ai_edit_v3_renderer/src/registry/layout-primitives.mjs`
- Create: `server/ai_edit_v3_renderer/src/registry/overlays.mjs`
- Create: `server/ai_edit_v3_renderer/src/registry/themes.mjs`
- Create: `server/ai_edit_v3_renderer/src/compile-project.mjs`
- Create: `server/ai_edit_v3_renderer/test/registry.test.mjs`
- Create: `server/ai_edit_v3_renderer/test/compile-project.test.mjs`

**Interfaces:**

```javascript
export const REGISTRY_VERSION = "ai-edit-v3-registry-v1";
export function getRegistryContract() // -> canonical JSON-safe contract
export function getRegistrySha256() // -> "sha256:<64 hex>"
export function resolveLayout(layoutId, variantId, ratio) // -> LayoutCompiler
export function resolveOverlay(overlayId) // -> OverlayCompiler
export function resolveTheme(theme) // -> frozen CSS-token map
export async function compileProject({manifest, outputRoot}) // -> CompiledProject
```

`CompiledProject` contains only `projectRoot`, `entryRelativePath`, `compositionIds`, `registrySha256`, `expectedFrames` and `snapshotTimesMs`. User/model text is written through `textContent` or escaped JSON data; it is never concatenated into HTML, CSS selectors, JavaScript source, URL attributes or style declarations.

- [ ] **Step 1 (RED): Write registry tests** proving exact ID sets, no aliasing, duplicate-ID rejection, sorted canonical contract, stable registry SHA, allowed ratio/variant enforcement and failure on an unknown overlay/layout/theme token.
- [ ] **Step 2 (RED): Write compiler-injection tests** with `</script>`, `<img onerror=...>`, `javascript:`, CSS braces, Unicode bidi controls and duplicate DOM IDs; output must display literal safe text or reject control characters and must contain no executable injection.
- [ ] **Step 3 (RED): Write HyperFrames structure tests** requiring a sized top-level root, sub-compositions wrapped in `<template>`, styles/scripts inside each template, `class="clip"`, globally unique prefixed IDs, and exact equality of host/root/timeline IDs.
- [ ] **Step 4: Run `npm test -- test/registry.test.mjs test/compile-project.test.mjs`** and observe RED.
- [ ] **Step 5 (GREEN): Implement registry canonicalization** as plain frozen data with sorted IDs and explicit capabilities; compute `registry_sha256` from that data and fail when it differs from the manifest.
- [ ] **Step 6 (GREEN): Implement safe layout primitives** for full-bleed child backgrounds, safe-area boxes, sized block transform wrappers, media slots, text nodes and prefixed IDs; do not set a full-frame background on the composition root.
- [ ] **Step 7 (GREEN): Implement all 12 overlay compilers** with bounded text/line counts, safe-area metadata, allowed animation targets and an explicit omission fallback for optional content.
- [ ] **Step 8 (GREEN): Implement bounded theme resolution** for palette, typography, density, motion energy, image fit, radius, border, shadow, spacing and texture enums; emit CSS variables from fixed token maps only.
- [ ] **Step 9 (GREEN): Implement project compilation** as one standalone top composition plus one template-wrapped sub-composition per scene, one synchronous paused GSAP timeline per composition, no network tags, no inline event handlers, no `<audio>`, and all `<video muted playsinline>` elements.
- [ ] **Step 10: Run GREEN verification.**

```powershell
Push-Location server/ai_edit_v3_renderer
npm test -- test/registry.test.mjs test/compile-project.test.mjs test/security.test.mjs
Pop-Location
git diff --check
```

Expected: exact registry sets and compiler security tests PASS; generated projects contain no user/model executable surface.

- [ ] **Step 11: Commit.**

```powershell
git add server/ai_edit_v3_renderer/src/registry/index.mjs server/ai_edit_v3_renderer/src/registry/layout-primitives.mjs server/ai_edit_v3_renderer/src/registry/overlays.mjs server/ai_edit_v3_renderer/src/registry/themes.mjs server/ai_edit_v3_renderer/src/compile-project.mjs server/ai_edit_v3_renderer/test/registry.test.mjs server/ai_edit_v3_renderer/test/compile-project.test.mjs
git commit -m "feat(ai-edit-v3): add safe component compiler"
```

### Task 3: Implement all layouts, templates, animations and transitions

**Files:**

- Create: `server/ai_edit_v3_renderer/src/registry/layouts.mjs`
- Modify: `server/ai_edit_v3_renderer/src/registry/index.mjs`
- Create: `server/ai_edit_v3_renderer/test/layouts.test.mjs`
- Create: `server/ai_edit_v3_renderer/test/render-fixtures.mjs`
- Create: `server/ai_edit_v3_renderer/test/fixtures/landscape/`
- Create: `server/ai_edit_v3_renderer/test/fixtures/portrait/`
- Create: `server/content_domains/ai_edit_v3/catalog/templates-v1.json`
- Create: `server/content_domains/ai_edit_v3/catalog/template-previews/commercial-diagnostic-landscape-v1.png`
- Create: `server/content_domains/ai_edit_v3/catalog/template-previews/commercial-diagnostic-portrait-v1.png`
- Create: `server/content_domains/ai_edit_v3/catalog/template-previews/editorial-explainer-landscape-v1.png`
- Create: `server/content_domains/ai_edit_v3/catalog/template-previews/editorial-explainer-portrait-v1.png`
- Modify: `server/content_domains/ai_edit_v3/store.py`
- Create: `tests/test_ai_edit_v3_template_catalog.py`

**Interfaces:**

```python
def load_template_catalog() -> tuple[TemplateContract, ...]: ...
def seed_template_versions(
    templates: Sequence[TemplateContract],
    *,
    db_path: Path,
) -> None: ...
```

```javascript
export function compileLayout({
  layoutId,
  variantId,
  ratio,
  scene,
  assets,
  idPrefix,
}) // -> HTMLElement
```

Every layout exposes exactly `balanced_a` and `emphasis_b` in both `16:9` and `9:16`. A template can restrict its published ratio, but the underlying layout still has snapshots for both ratios.

- [ ] **Step 1 (RED): Write the 48-case matrix test** for 12 layouts × 2 variants × 2 ratios; assert resolved size, safe areas, required slots, optional fallback, unique IDs, no clipped text and no face/product critical-region overlap in the supplied geometry fixture.
- [ ] **Step 2 (RED): Write template catalog tests** requiring exactly four version-1 published records, two `16:9`, two `9:16`, both creative directions, the exact non-empty Chinese `title` and `category` values consumed by Phase D, at least two allowed layouts per template, existing preview PNG with matching SHA, non-empty capabilities and immutable catalog hashes.
- [ ] **Step 3 (RED): Write idempotent seed tests** proving the same catalog creates one row per `(template_id, version)`, changed content under the same version is rejected, unpublished or ratio-mismatched templates cannot seed as active, and no V2 table is read.
- [ ] **Step 4: Run RED tests.**

```powershell
Push-Location server/ai_edit_v3_renderer
npm test -- test/layouts.test.mjs
Pop-Location
python -m unittest tests.test_ai_edit_v3_template_catalog -v
```

Expected: failures identify the absent layout registry, catalog and previews.

- [ ] **Step 5 (GREEN): Implement six speaker/material layouts** `speaker_fullscreen`, `speaker_left_info_right`, `speaker_right_evidence_left`, `material_fullscreen_speaker_pip`, `product_hero`, `editorial_collage` with both variants and explicit optional-slot fallback.
- [ ] **Step 6 (GREEN): Implement six information/closing layouts** `comparison_split`, `steps_stack`, `number_proof`, `quote_reversal`, `method_timeline`, `cta_offer` with both variants and exact safe-area contracts.
- [ ] **Step 7: Generate two deterministic fixture projects containing all 48 cases**—one landscape and one portrait, each with both variants of all 12 layouts—and cover bounded Chinese, long Chinese, Chinese/English/digit mixed text, no optional image, one image, multiple images and non-standard source aspect ratios.
- [ ] **Step 8: Run the local HyperFrames structure gate** using the pinned local executable.

```powershell
Push-Location server/ai_edit_v3_renderer
npm run hf:check -- test/fixtures/landscape --strict --json
npm run hf:check -- test/fixtures/portrait --strict --json
npm run hf:snapshot -- test/fixtures/landscape --at 0,1.5,3
npm run hf:snapshot -- test/fixtures/portrait --at 0,1.5,3
Pop-Location
```

Expected: `check` exits 0 with zero persistent findings; midpoint snapshots show mounted, styled, full-size sub-compositions.

- [ ] **Step 9 (GREEN): Create the four exact catalog records** and render their preview PNGs from the pinned fixture pipeline; write each actual preview SHA into `templates-v1.json`, then seed only through `store.seed_template_versions()`.
- [ ] **Step 10: Inspect the four preview PNGs** at original resolution and record in the commit notes that text is readable, composition roots are not black, safe areas are respected, and each template visibly differs in hierarchy rather than color only.
- [ ] **Step 11: Run GREEN verification.**

```powershell
Push-Location server/ai_edit_v3_renderer
npm test -- test/layouts.test.mjs test/registry.test.mjs test/compile-project.test.mjs
npm run hf:check -- test/fixtures/landscape --strict --json
npm run hf:check -- test/fixtures/portrait --strict --json
Pop-Location
python -m unittest tests.test_ai_edit_v3_template_catalog tests.test_ai_edit_v3_store -v
git diff --check
```

Expected: all 48 layout/variant/ratio cases and all four real template previews PASS.

#### Motion and transition sub-cycle

**Files:**

- Create: `server/ai_edit_v3_renderer/src/registry/animations.mjs`
- Create: `server/ai_edit_v3_renderer/src/registry/transitions.mjs`
- Modify: `server/ai_edit_v3_renderer/src/registry/index.mjs`
- Modify: `server/ai_edit_v3_renderer/src/compile-project.mjs`
- Create: `server/ai_edit_v3_renderer/test/animations.test.mjs`
- Create: `server/ai_edit_v3_renderer/test/transitions.test.mjs`
- Create: `server/ai_edit_v3_renderer/test/fixtures/animations/`
- Create: `server/ai_edit_v3_renderer/test/fixtures/transitions/`

**Interfaces:**

```javascript
export function applyAnimation({
  timeline,
  preset,
  target,
  params,
  sceneDurationMs,
  fps,
}) // mutates the one paused scene timeline and returns normalized audit data

export function applyTransition({
  timeline,
  transition,
  outgoing,
  incoming,
  boundaryMs,
  durationMs,
  protectedCaptionRanges,
}) // returns normalized boundary audit data
```

Parameter contracts are exact:

| Preset | Allowed parameters |
| --- | --- |
| `fade` | `from_opacity` 0–1, `duration_ms` 120–1200 |
| `slide` | `direction` left/right/up/down, `distance_px` 16–240, `duration_ms` 160–1200 |
| `scale` | `from_scale` 0.70–1.30, `to_scale` 0.70–1.30, `duration_ms` 160–1200 |
| `rotate` | `from_degrees` -20–20, `to_degrees` -20–20, `duration_ms` 160–1200 |
| `wipe` | `direction` left/right/up/down, `duration_ms` 180–1200 |
| `stagger` | `axis` x/y, `distance_px` 8–120, `each_ms` 20–180, `max_children` 1–24 |
| `count_up` | integer `start`, integer `end`, `decimals` 0–2, `duration_ms` 300–2000 |
| `image_pan_zoom` | `start_scale`/`end_scale` 1.00–1.25, `x_percent`/`y_percent` -10–10 |
| `card_reveal` | `axis` x/y, `distance_px` 12–120, `duration_ms` 180–1200 |
| `stamp` | `start_scale` 0.50–0.95, `overshoot_scale` 1.00–1.15, `duration_ms` 180–800 |
| `light_sweep` | `angle_degrees` -45–45, `duration_ms` 200–1200 |
| `highlight_draw` | `direction` left/right, `duration_ms` 180–1200 |
| `split_screen` | `direction` left/right/up/down, `duration_ms` 200–1200 |
| `subtitle_pop` | `from_scale` 0.80–1.00, `overshoot_scale` 1.00–1.12, `duration_ms` 100–500 |

Transition duration contracts are `hard_cut=0`, `soft_wipe=120–600 ms`, `directional_slide=150–700 ms`, `light_flash=80–240 ms`, `card_match_cut=180–700 ms`.

- [ ] **Step 1 (RED): Write allowlist/range tests** for every preset and transition; reject unknown properties, booleans used as numbers, NaN/Infinity, unsupported targets, excessive child counts, `volume_fade` in Node and transition windows that alter protected caption timing.
- [ ] **Step 2 (RED): Write seek tests** for each visual animation at 0%, 50% and 100%, requiring the same painted state after direct seek and sequential seek, a readable final hold, no reset to rest and no final black frame.
- [ ] **Step 3 (RED): Write runtime-source tests** rejecting clocks, timers, random calls, async timeline construction, `repeat:-1`, `play()`, dynamic layout reads, layout-property tweens, `.clip` visibility control and multiple timelines per composition.
- [ ] **Step 4 (RED): Write transition boundary tests** at final outgoing frame, first overlap frame, midpoint and first fully incoming frame; verify globally unique subjects and exact caption windows.
- [ ] **Step 5: Run `npm test -- test/animations.test.mjs test/transitions.test.mjs test/security.test.mjs`** and observe RED.
- [ ] **Step 6 (GREEN): Implement the 14 visual presets** using only synchronous paused GSAP transforms, opacity/autoAlpha on non-clip wrappers, clip-path/mask variables and fixed setup-time constants; use `immediateRender:false` where later `from`/`fromTo` tweens touch an earlier property.
- [ ] **Step 7 (GREEN): Implement the five transitions** inside the single scene timeline, clamp only within the declared ranges, preserve caption time windows and retain the outgoing/incoming subject identity required by `card_match_cut`.
- [ ] **Step 8: Generate animation and transition fixture compositions** with an actual subject selector, semantic pose labels, first/proof/final-minus-hold/final sample times and no helper-only motion.
- [ ] **Step 9: Run HyperFrames motion diagnostics.**

```powershell
Push-Location server/ai_edit_v3_renderer
npm run hf:check -- test/fixtures/animations --strict --json
npm run hf:check -- test/fixtures/transitions --strict --json
npm run hf:keyframes -- test/fixtures/animations --json
npm run hf:keyframes -- test/fixtures/transitions --json
npm run hf:snapshot -- test/fixtures/animations --at 0,0.5,1,1.4
npm run hf:snapshot -- test/fixtures/transitions --at 0,0.2,0.4,0.8
Pop-Location
```

Expected: check exits 0; keyframe reports identify the real animated target and explicit stops; snapshots show first, proof, final hold and transition boundary states.

- [ ] **Step 10: Run GREEN verification.**

```powershell
Push-Location server/ai_edit_v3_renderer
npm test -- test/animations.test.mjs test/transitions.test.mjs test/registry.test.mjs test/compile-project.test.mjs test/security.test.mjs
Pop-Location
git diff --check
```

- [ ] **Step 23: Commit the complete layout, template and motion registry.**

```powershell
git add server/ai_edit_v3_renderer/src/registry/layouts.mjs server/ai_edit_v3_renderer/src/registry/animations.mjs server/ai_edit_v3_renderer/src/registry/transitions.mjs server/ai_edit_v3_renderer/src/registry/index.mjs server/ai_edit_v3_renderer/src/compile-project.mjs server/ai_edit_v3_renderer/test/layouts.test.mjs server/ai_edit_v3_renderer/test/animations.test.mjs server/ai_edit_v3_renderer/test/transitions.test.mjs server/ai_edit_v3_renderer/test/render-fixtures.mjs server/ai_edit_v3_renderer/test/fixtures/landscape server/ai_edit_v3_renderer/test/fixtures/portrait server/ai_edit_v3_renderer/test/fixtures/animations server/ai_edit_v3_renderer/test/fixtures/transitions server/content_domains/ai_edit_v3/catalog server/content_domains/ai_edit_v3/store.py tests/test_ai_edit_v3_template_catalog.py
git commit -m "feat(ai-edit-v3): add templates and motion registry"
```

### Task 4: Add the per-task ElevenLabs music and sound-effect adapter

**Files:**

- Create: `server/content_domains/ai_edit_v3/providers/elevenlabs.py`
- Create: `tests/test_ai_edit_v3_elevenlabs.py`
- Create: `tests/fixtures/ai_edit_v3/providers/elevenlabs/music-success.json`
- Create: `tests/fixtures/ai_edit_v3/providers/elevenlabs/sfx-success.json`
- Modify: `server/content_domains/ai_edit_v3/runtime.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class MusicGenerationRequest:
    prompt: str
    duration_ms: int
    mood: str
    energy: str
    bpm_min: int
    bpm_max: int
    forbidden_features: tuple[str, ...]

@dataclass(frozen=True)
class SfxGenerationRequest:
    prompt: str
    duration_ms: int
    cue_id: str
    required: bool

class AudioGenerator(Protocol):
    def generate_music(
        self, request: MusicGenerationRequest, *, output_path: Path,
        idempotency_key: str, deadline_at: float
    ) -> ProviderResult: ...

    def generate_sfx(
        self, request: SfxGenerationRequest, *, output_path: Path,
        idempotency_key: str, deadline_at: float
    ) -> ProviderResult: ...
```

The adapter uses model `music_v2` for `/v1/music` and `eleven_text_to_sound_v2` for `/v1/sound-generation`. Music requests always send the provider's instrumental-only control; no caller can override the model, endpoint path, `force_instrumental`, Authorization header name or timeout budget.

- [ ] **Step 1 (RED): Write exact request tests** for the two model IDs, fixed endpoint paths, instrumental BGM, 3000–600000 ms music duration, 500–30000 ms SFX duration, bounded prompt strings and `Idempotency-Key`.
- [ ] **Step 2 (RED): Write response tests** for streaming non-empty audio to a caller-owned path, request ID, content type, byte count, SHA-256, reported usage and provider model in `ProviderResult`; reject HTML/JSON error bodies, empty audio and oversized responses.
- [ ] **Step 3 (RED): Write retry-classification tests**: DNS/connect failure before request body is `DefinitiveNotAccepted`; 429 and explicitly unaccepted 5xx are bounded retryable; timeout after body send is `SubmissionUnknown` and cannot call the transport a second time.
- [ ] **Step 4 (RED): Write secrecy tests** proving `ELEVENLABS_API_KEY`, Authorization headers, audio bytes, signed URLs and full prompts do not enter exception text, logs, fixtures or persisted result payloads.
- [ ] **Step 5: Run `python -m unittest tests.test_ai_edit_v3_elevenlabs -v`** and observe RED because the adapter is absent.
- [ ] **Step 6 (GREEN): Implement the injected HTTP transport adapter** with argv-free stdlib HTTP calls, deadline-derived timeout, bounded response streaming, same-directory temporary output, `fsync`, rename and SHA verification; the adapter must not access SQLite or COS.
- [ ] **Step 7 (GREEN): Wire runtime preflight** to report ElevenLabs separately as `implemented`, `configured_and_wired` or `missing_or_unavailable`; no task requiring BGM may enter prehold when the capability is not ready.
- [ ] **Step 8: Run GREEN verification.**

```powershell
python -m unittest tests.test_ai_edit_v3_elevenlabs tests.test_ai_edit_v3_feature -v
git diff --check
```

Expected: all tests PASS with zero real HTTP calls; renderer environment fixtures contain no ElevenLabs variable.

- [ ] **Step 9: Commit.**

```powershell
git add server/content_domains/ai_edit_v3/providers/elevenlabs.py server/content_domains/ai_edit_v3/runtime.py tests/test_ai_edit_v3_elevenlabs.py tests/fixtures/ai_edit_v3/providers/elevenlabs
git commit -m "feat(ai-edit-v3): add per-job elevenlabs audio"
```

### Task 5: Add the ElevenLabs secret name through its isolated shared-config boundary

**Files:**

- Modify: `deploy/huangque-secrets.env.example`
- Create: `tests/test_ai_edit_v3_secrets_example.py`

**Interfaces:**

The V3 server environment exposes exactly `ELEVENLABS_API_KEY`; `runtime.py` reads it only in the network Worker. The render unit has no `EnvironmentFile` and never receives this name or value.

- [ ] **Step 1 (RED): Write the shared-config boundary test** that extracts the Phase A-created `/etc/huangque/ai-edit-v3.env` example section, requires one `ELEVENLABS_API_KEY=replace-with-elevenlabs-key` entry, and rejects a second occurrence or a non-example value. Task 8 owns the later renderer-unit assertion because that unit does not exist yet.
- [ ] **Step 2: Run `python -m unittest tests.test_ai_edit_v3_secrets_example tests.test_systemd_secrets -v`** and observe RED because the V3 example block lacks the ElevenLabs variable.
- [ ] **Step 3 (GREEN): Add the single example variable** under the existing V3 provider subsection with a comment limiting it to `music_v2` and `eleven_text_to_sound_v2`; do not add a real key, provider URL or renderer environment entry.
- [ ] **Step 4: Run GREEN verification.**

```powershell
python -m unittest tests.test_ai_edit_v3_secrets_example tests.test_systemd_secrets -v
python scripts/ci_validate.py
git diff --check
```

Expected: both tests PASS; the render unit remains secret-free.

- [ ] **Step 5: Commit only the shared example boundary and its test.**

```powershell
git add deploy/huangque-secrets.env.example tests/test_ai_edit_v3_secrets_example.py
git commit -m "chore(ai-edit-v3): document elevenlabs secret name"
```

### Task 6: Compile audio cues and produce the unique 48 kHz stereo master

**Files:**

- Create: `server/content_domains/ai_edit_v3/audio.py`
- Modify: `server/content_domains/ai_edit_v3/media.py`
- Modify: `server/content_domains/ai_edit_v3/store.py`
- Create: `tests/test_ai_edit_v3_audio.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class AudioGenerationPlan:
    music: MusicGenerationRequest
    sfx: tuple[SfxGenerationRequest, ...]
    volume_fades: tuple[VolumeFade, ...]
    protected_ranges: tuple[TimeRange, ...]
    duration_ms: int

@dataclass(frozen=True)
class GeneratedAudioAsset:
    cue_id: str
    kind: Literal["bgm", "sfx"]
    relative_path: str
    object_key: str
    sha256: str
    duration_ms: int
    sample_rate: int
    channels: int
    provider_request_id: str
    usage: Mapping[str, int | float]

@dataclass(frozen=True)
class MasterAudio:
    relative_path: str
    sha256: str
    duration_ms: int
    sample_rate: Literal[48000]
    channels: Literal[2]
    integrated_lufs: float
    true_peak_dbtp: float
    audit: Mapping[str, Any]

def compile_audio_plan(
    edit_plan: Mapping[str, Any],
    timeline: TextTimeline,
) -> AudioGenerationPlan: ...

def generate_task_audio(
    job_id: str,
    plan: AudioGenerationPlan,
    generator: AudioGenerator,
    cos: PrivateCos,
    output_root: Path,
    context: StageContext,
) -> tuple[GeneratedAudioAsset, ...]: ...

def build_master_audio(
    voice_source: Path,
    source_segments: Sequence[SourceSegment],
    plan: AudioGenerationPlan,
    generated: Sequence[GeneratedAudioAsset],
    output_path: Path,
    *,
    deadline_at: float,
) -> MasterAudio: ...
```

- [ ] **Step 1 (RED): Write cue-plan tests** requiring one instrumental BGM for every job, only declared SFX cues, SFX limited to reversal/number/method/transition/CTA roles, exact required/optional flags, and no SFX peak overlapping protected brand/digit/price/key-sentence ranges.
- [ ] **Step 2 (RED): Write `volume_fade` tests** for target `bgm` or a declared SFX cue, integer start/end within duration, `from_db`/`to_db` from -60 through 0, no overlaps on the same target, and rejection from the Node animation list.
- [ ] **Step 3 (RED): Write generation recovery tests** proving the BGM failure is terminal after bounded allowed retries, optional SFX failure is audited and omitted, required SFX failure is terminal, and a completed provider checkpoint is reused without reading a historical audio library or resubmitting.
- [ ] **Step 4 (RED): Write master tests with generated WAV fixtures** for source-segment trimming without time stretch, voice-centered stereo, BGM coverage, at least 12 dB dialogue ducking, SFX delay/trim, two-pass loudness, 48 kHz, two channels, `-16 LUFS ± 2 LU`, true peak `<= -1 dBTP`, exact SHA and duration error within 40 ms.
- [ ] **Step 5 (RED): Write failure tests** for missing voice, duplicated dialogue inputs, clipping, non-monotonic source segments, abnormal speech-region silence over 500 ms, output longer/shorter than the frozen duration, malformed loudnorm JSON and process timeout.
- [ ] **Step 6: Run `python -m unittest tests.test_ai_edit_v3_audio -v`** and observe RED.
- [ ] **Step 7 (GREEN): Implement deterministic audio-plan compilation** from only `audio_cues` plus authoritative protected ranges; clamp nothing silently—unknown/invalid cues fail validation, while explicit optional SFX degradation is retained in the result.
- [ ] **Step 8 (GREEN): Implement per-task generation orchestration** that persists provider intent before submit, uses `ai-edit-v3:{job_id}:audio:bgm` and `ai-edit-v3:{job_id}:audio:sfx:{cue_id}` keys, verifies decoded audio, stores task-private COS objects, and writes stable object keys rather than URLs.
- [ ] **Step 9 (GREEN): Implement the FFmpeg filter graph** using argument arrays: concatenate retained voice intervals with `atrim`/`asetpts`, normalize to 48 kHz stereo PCM, apply compiled fades, sidechain-compress plus at least `-12dB` BGM attenuation during voice, place SFX with `adelay`, and mix to one pre-master.
- [ ] **Step 10 (GREEN): Implement two-pass EBU R128 mastering** to `pcm_s24le` WAV at 48 kHz stereo, parse measurement JSON, apply `I=-16:TP=-1:LRA=11`, then FFprobe the result and record commands with redacted paths, exact versions and measured values.
- [ ] **Step 11: Ensure every media subprocess uses the Phase B process supervisor** with `shell=False`, `stdin=DEVNULL`, bounded stdout/stderr, a new process group, `file,pipe` protocol allowlist and full-group termination on `deadline_at` or `assert_active()` failure.
- [ ] **Step 12: Run GREEN verification.**

```powershell
python -m unittest tests.test_ai_edit_v3_audio tests.test_ai_edit_v3_media tests.test_ai_edit_v3_elevenlabs -v
python -m unittest tests.test_ai_edit_v2_audio tests.test_ai_edit_v2_media -v
git diff --check
```

Expected: synthetic mixes meet the frozen audio thresholds; BGM is always new and required; `volume_fade` is present only in FFmpeg audit data.

- [ ] **Step 13: Commit.**

```powershell
git add server/content_domains/ai_edit_v3/audio.py server/content_domains/ai_edit_v3/media.py server/content_domains/ai_edit_v3/store.py tests/test_ai_edit_v3_audio.py
git commit -m "feat(ai-edit-v3): build unique mastered audio"
```

### Task 7: Render deterministic silent video and emit machine-readable evidence

**Files:**

- Create: `server/ai_edit_v3_renderer/src/render.mjs`
- Create: `server/ai_edit_v3_renderer/src/render-hyperframes.mjs`
- Create: `server/ai_edit_v3_renderer/src/report.mjs`
- Modify: `server/ai_edit_v3_renderer/src/compile-project.mjs`
- Create: `server/ai_edit_v3_renderer/test/render.test.mjs`
- Create: `server/ai_edit_v3_renderer/test/determinism.test.mjs`
- Modify: `server/ai_edit_v3_renderer/test/security.test.mjs`

**Interfaces:**

```javascript
export async function renderHyperframes({
  projectRoot,
  outputPath,
  chromiumPath,
  timeoutMs,
  environment,
  signal,
}) // -> RenderExecution

export async function buildRenderReport({
  manifest,
  verifiedFiles,
  compiledProject,
  execution,
  snapshots,
  outputPath,
}) // -> RenderReport
```

The only production entry command is:

```text
/usr/bin/node /work/release/src/render.mjs \
  --request /work/input/request.json \
  --input-root /work/input/assets \
  --output-root /work/output
```

The unit supplies all three fixed paths. The request file supplies hashes and logical IDs but cannot replace those paths.

- [ ] **Step 1 (RED): Write entrypoint tests** for valid fixed arguments, unexpected flag rejection, missing/extra positional argument rejection, request/manifest/build hash mismatch, non-empty output-root rejection and output path confinement.
- [ ] **Step 2 (RED): Write silent-output tests** requiring zero `<audio>` elements, every source `<video>` muted with volume 0, an MP4 containing a video stream and no audio stream, and report field `audio_streams: 0`.
- [ ] **Step 3 (RED): Write process tests** proving the renderer spawns only the local `node_modules/.bin/hyperframes` executable with an argv array, fixed local Chromium, strict checks and fixed output; reject `npx`, shell strings, environment PATH lookup and runtime package download.
- [ ] **Step 4 (RED): Write report tests** requiring manifest/registry/build/input/output hashes, Node/Chromium/FFmpeg/FFprobe versions, frame count, render elapsed time, peak RSS, CPU time, snapshot hashes, DOM geometry evidence and a bounded redacted error record.
- [ ] **Step 5 (RED): Write decoded determinism tests** that render the same fixture twice and compare FFmpeg `framemd5`; assert identical decoded frame hashes and keyframe PNG hashes without asserting MP4 container-byte identity.
- [ ] **Step 6: Run RED tests.**

```powershell
Push-Location server/ai_edit_v3_renderer
npm test -- test/render.test.mjs test/determinism.test.mjs test/security.test.mjs
Pop-Location
```

Expected: tests fail because the render entrypoint and report do not exist.

- [ ] **Step 7 (GREEN): Implement `render.mjs` orchestration** in the order parse request → verify release → parse/validate manifest → verify files → compile fixed project → run local HyperFrames check/render → snapshot evidence → FFprobe output → write report atomically.
- [ ] **Step 8 (GREEN): Implement local HyperFrames execution** with one `HYPERFRAMES_RUN_ID`, telemetry disabled for the service, no `--no-sandbox`, no inherited proxy variables, bounded output capture and abort propagation to the entire child process group.
- [ ] **Step 9 (GREEN): Make compilation silent by construction**: source video keeps framework-owned playback and seeking but always emits `muted`, never emits an audio track, never calls `play()`, and uses the same `source_segments` mapping as the master.
- [ ] **Step 10 (GREEN): Emit `silent-video.mp4`, `render-report.json` and bounded keyframe PNGs** only under the fixed output root; write through temporary files and rename after decode/hash verification.
- [ ] **Step 11: Run the HyperFrames final gate and GREEN tests.**

```powershell
Push-Location server/ai_edit_v3_renderer
npm test -- test/render.test.mjs test/determinism.test.mjs test/security.test.mjs
npm run render:fixtures
npm run hf:check -- test/fixtures/landscape --strict --json
npm run hf:check -- test/fixtures/portrait --strict --json
Pop-Location
```

Expected: both ratios render silent video; two runs have identical decoded frame hashes; HyperFrames reports zero persistent findings.

- [ ] **Step 12: Commit.**

```powershell
git add server/ai_edit_v3_renderer/src/render.mjs server/ai_edit_v3_renderer/src/render-hyperframes.mjs server/ai_edit_v3_renderer/src/report.mjs server/ai_edit_v3_renderer/src/compile-project.mjs server/ai_edit_v3_renderer/test/render.test.mjs server/ai_edit_v3_renderer/test/determinism.test.mjs server/ai_edit_v3_renderer/test/security.test.mjs
git commit -m "feat(ai-edit-v3): render deterministic silent video"
```

### Task 8: Add the root-owned sandbox and connect the Python renderer boundary

**Files:**

- Create: `deploy/systemd/huangque-ai-edit-v3.service`
- Create: `deploy/systemd/huangque-ai-edit-v3-render@.service`
- Create: `deploy/libexec/huangque-ai-edit-v3-renderctl`
- Create: `deploy/sudoers.d/huangque-ai-edit-v3-render`
- Create: `deploy/tmpfiles.d/huangque-ai-edit-v3.conf`
- Create: `tests/test_ai_edit_v3_render_sandbox.py`
- Modify: `tests/test_systemd_secrets.py`

**Interfaces:**

```text
huangque-ai-edit-v3-renderctl start <instance_id>
huangque-ai-edit-v3-renderctl query <instance_id>
huangque-ai-edit-v3-renderctl stop <instance_id>
```

`instance_id` must match `[a-z0-9_-]{1,64}`. `query` writes one bounded JSON object with `state` in `queued|running|succeeded|failed|stopped`, `result_ready`, `exit_status`, `error_code` and no filesystem path. All spool/state/result paths are derived from the validated ID and fixed constants; the helper accepts no path, environment assignment, unit name, systemd property or shell fragment.

The render unit must contain this security baseline:

```ini
[Unit]
Description=Huangque AI Edit V3 isolated render %i
After=local-fs.target

[Service]
Type=exec
DynamicUser=yes
PrivateNetwork=yes
PrivateUsers=yes
PrivateMounts=yes
PrivateTmp=yes
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
CapabilityBoundingSet=
AmbientCapabilities=
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
UMask=0077
KillMode=control-group
TimeoutStopSec=30
RuntimeMaxSec=3300
CPUQuota=200%
MemoryMax=3G
TasksMax=64
LimitFSIZE=8G
StateDirectory=huangque-ai-edit-v3-render/%i
StateDirectoryMode=0700
Environment=HOME=/work/output/home
Environment=LANG=C.UTF-8
Environment=LC_ALL=C.UTF-8
Environment=TZ=UTC
WorkingDirectory=/work/output
TemporaryFileSystem=/work:rw,nodev,nosuid,size=8G
BindReadOnlyPaths=/opt/huangque/ai-edit-v3-renderer/current:/work/release
BindReadOnlyPaths=/var/lib/huangque-ai-edit-v3-render/%i/input:/work/input
BindPaths=/var/lib/huangque-ai-edit-v3-render/%i/output:/work/output
InaccessiblePaths=/etc/huangque
InaccessiblePaths=/home
InaccessiblePaths=/var/spool/huangque-ai-edit-v3
InaccessiblePaths=/var/lib/huangque-ai-edit-v3
InaccessiblePaths=/var/lib/huangque-ai-edit-v3-render
ExecStart=/usr/bin/node /work/release/src/render.mjs --request /work/input/request.json --input-root /work/input/assets --output-root /work/output
```

The implementation must verify these directives with the test server's actual systemd version; failure to support any required isolation or quota directive blocks the Phase C gate and does not authorize weakening the unit.

- [ ] **Step 1 (RED): Write static unit tests** requiring every directive above, no `EnvironmentFile` in the render unit, no provider variable, no `--no-sandbox`, fixed `ExecStart`, `KillMode=control-group` and exact resource limits.
- [ ] **Step 2 (RED): Write helper parser tests** for valid IDs and rejection of uppercase, slash, dot-dot, whitespace, newline, glob, option prefix, overlength, extra arguments, `start other.service`, systemd property injection and spool symlink.
- [ ] **Step 3 (RED): Write authorization tests** proving the Worker principal can call only the exact helper path with `start|query|stop`, while direct `systemctl`, other units, helper internal functions and environment-prefixed sudo commands are denied.
- [ ] **Step 4 (RED): Write lifecycle tests with injected systemctl/filesystem adapters** for atomic incoming-to-sealed move, recursive regular-file/link/hash checks, one active unit per ID, idempotent query, result collection to a fixed worker-readable result spool, stop of the fixed unit, 8 GiB project-quota enforcement and terminal cleanup.
- [ ] **Step 5: Run `python -m unittest tests.test_ai_edit_v3_render_sandbox tests.test_systemd_secrets -v`** and observe RED.
- [ ] **Step 6 (GREEN): Implement the helper as a root-owned Python executable** with constant directories, `openat`/no-follow traversal, argv-only `systemctl`, atomic seals, bounded JSON output, no shell, no imports from the application and a command-specific allowlist.
- [ ] **Step 7 (GREEN): Implement `start`** to validate and seal `/var/spool/huangque-ai-edit-v3/incoming/<id>`, verify request/manifest/assets, prepare the private instance state/output with an 8 GiB filesystem project quota, then start only `huangque-ai-edit-v3-render@<id>.service`.
- [ ] **Step 8 (GREEN): Implement `query` and `stop`** so query reads fixed unit properties, validates successful outputs and atomically exposes a result bundle under `/var/spool/huangque-ai-edit-v3/results/<id>`; stop addresses only the fixed unit and relies on control-group killing.
- [ ] **Step 9 (GREEN): Add tmpfiles and sudoers records** with root ownership, a dedicated V3 worker group, non-listable parent directories and no wildcard path argument beyond the validated instance ID.
- [ ] **Step 10: Run local GREEN verification.**

```powershell
python -m unittest tests.test_ai_edit_v3_render_sandbox tests.test_systemd_secrets -v
python scripts/ci_validate.py
git diff --check
```

- [ ] **Step 11: Run syntax verification on an authorized Linux host only.**

```bash
sudo systemd-analyze verify \
  deploy/systemd/huangque-ai-edit-v3.service \
  'deploy/systemd/huangque-ai-edit-v3-render@.service'
sudo visudo -cf deploy/sudoers.d/huangque-ai-edit-v3-render
python -m unittest tests.test_ai_edit_v3_render_sandbox -v
```

Expected: all exit 0. This is a file/syntax gate; installing units or starting a service remains a separate test-deployment action.

#### Python renderer boundary sub-cycle

**Files:**

- Modify: `server/content_domains/ai_edit_v3/renderers/__init__.py`
- Create: `server/content_domains/ai_edit_v3/renderers/hyperframes.py`
- Modify: `server/content_domains/ai_edit_v3/runtime.py`
- Create: `tests/test_ai_edit_v3_hyperframes.py`

**Interfaces:**

```python
class HyperframesRenderer:
    def __init__(
        self,
        *,
        renderctl_path: Path,
        spool_root: Path,
        renderer_build_id: str,
        command_runner: CommandRunner,
        clock: Callable[[], float],
    ) -> None: ...

    def render(
        self, manifest_path: Path, input_root: Path, output_dir: Path, *,
        instance_id: str, deadline_at: float,
        assert_active: Callable[[], None]
    ) -> RenderResult: ...

    def terminate(self, instance_id: str) -> None: ...
```

- [ ] **Step 1 (RED): Write constructor and request tests** for absolute root-owned helper path, exact active build ID, valid instance ID, manifest hash, input hash list, empty output directory and atomic spool request creation.
- [ ] **Step 2 (RED): Write argv tests** requiring exactly `[renderctl_path, "start"|"query"|"stop", instance_id]`, `shell=False`, minimal environment and no path/unit/property/user text in helper arguments.
- [ ] **Step 3 (RED): Write polling tests** for queued/running/succeeded/failed/stopped, bounded backoff, malformed/oversized helper JSON, output bundle hash verification and stable error codes.
- [ ] **Step 4 (RED): Write lease/deadline tests** where `assert_active()` raises, the clock reaches `deadline_at`, or the Python process is interrupted; every path calls `stop` once, waits for the control group to end and rejects late output.
- [ ] **Step 5 (RED): Write release mismatch tests** proving Python refuses to spool when the manifest build ID, active lock, package-lock/font/browser hashes or startup preflight differ.
- [ ] **Step 6: Run `python -m unittest tests.test_ai_edit_v3_hyperframes -v`** and observe RED.
- [ ] **Step 7 (GREEN): Implement `HyperframesRenderer`** by constructing a `RenderRequest`, copying only the canonical manifest and verified regular assets into the fixed incoming instance directory, atomically sealing `request.json`, then invoking only the helper's three actions.
- [ ] **Step 8 (GREEN): Implement bounded polling and result parsing**; verify `silent-video.mp4`, `render-report.json`, snapshots, output SHA, environment fingerprint and absence of audio before returning `RenderResult`.
- [ ] **Step 9 (GREEN): Wire runtime preflight** to check helper ownership/mode, active release lock, Node/Chromium/FFmpeg/FFprobe versions, font hashes and a no-network sandbox probe; unavailable sandbox disables new V3 work without affecting V2.
- [ ] **Step 10: Run GREEN verification.**

```powershell
python -m unittest tests.test_ai_edit_v3_hyperframes tests.test_ai_edit_v3_feature tests.test_ai_edit_v3_render_sandbox -v
python -m py_compile server/content_domains/ai_edit_v3/renderers/__init__.py server/content_domains/ai_edit_v3/renderers/hyperframes.py
git diff --check
```

Expected: renderer tests PASS and every cancellation path issues a fixed-unit stop.

- [ ] **Step 23: Commit the complete sandbox and Python supervision boundary.**

```powershell
git add deploy/systemd/huangque-ai-edit-v3.service deploy/systemd/huangque-ai-edit-v3-render@.service deploy/libexec/huangque-ai-edit-v3-renderctl deploy/sudoers.d/huangque-ai-edit-v3-render deploy/tmpfiles.d/huangque-ai-edit-v3.conf server/content_domains/ai_edit_v3/renderers/__init__.py server/content_domains/ai_edit_v3/renderers/hyperframes.py server/content_domains/ai_edit_v3/runtime.py tests/test_ai_edit_v3_render_sandbox.py tests/test_ai_edit_v3_hyperframes.py tests/test_systemd_secrets.py
git commit -m "feat(ai-edit-v3): supervise isolated renders"
```

### Task 9: Mux the unique master and execute every blocking quality gate

**Files:**

- Modify: `server/content_domains/ai_edit_v3/media.py`
- Create: `server/content_domains/ai_edit_v3/quality.py`
- Modify: `server/ai_edit_v3_renderer/src/report.mjs`
- Create: `tests/test_ai_edit_v3_mux.py`
- Create: `tests/test_ai_edit_v3_quality.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class FinalMux:
    relative_path: str
    sha256: str
    duration_ms: int
    video_codec: Literal["h264"]
    audio_codec: Literal["aac"]
    width: int
    height: int
    fps_num: int
    fps_den: int
    sample_rate: Literal[48000]
    channels: Literal[2]
    audit: Mapping[str, Any]

@dataclass(frozen=True)
class QualityFinding:
    check_id: str
    status: Literal["pass", "fail", "unknown"]
    blocking: bool
    repairable: bool
    measured: Mapping[str, int | float | str | bool]
    evidence: tuple[Mapping[str, Any], ...]
    executor: Mapping[str, str]

@dataclass(frozen=True)
class QualityReport:
    passed: bool
    findings: tuple[QualityFinding, ...]
    repairable_ids: tuple[str, ...]
    report_sha256: str

def mux_master_audio(
    silent_video: Path,
    master_audio: Path,
    output_path: Path,
    *,
    duration_ms: int,
    deadline_at: float,
) -> FinalMux: ...

def run_blocking_quality(
    final_mux: FinalMux,
    manifest: Mapping[str, Any],
    render_report: Mapping[str, Any],
    *,
    owner_evidence: Mapping[str, Any],
    visual_inspector: VisualInspector,
    deadline_at: float,
) -> QualityReport: ...
```

- [ ] **Step 1 (RED): Write mux tests** requiring silent-video PTS 0, master PTS 0, video stream copied without re-encode, one AAC 48 kHz stereo stream, H.264/yuv420p video, exact 1080p dimensions, fast-start MP4 and total duration error no greater than one frame and 40 ms.
- [ ] **Step 2 (RED): Write decoded determinism tests** that render/mux the same manifest twice and compare video `framemd5` plus decoded 48 kHz stereo PCM SHA; explicitly do not compare MP4 container bytes.
- [ ] **Step 3 (RED): Write technical QC tests** for full decode, codec/dimensions, manifest duration, non-director black over 300 ms, speech with undeclared freeze over 2 seconds, speech silence over 500 ms, clipping, loudness, duplicate dialogue fingerprint and lip/voice sampled offset over 80 ms.
- [ ] **Step 4 (RED): Write content/layout QC tests** for 100% accurate-caption coverage, zero protected fact errors, no safe-area overflow/text clipping, no main-face/product obstruction, owner/task/hash provenance, required-slot traceability, semantic conflict, generated image presented as real evidence, and first-three-second visible hook.
- [ ] **Step 5 (RED): Write visual-verdict tests** for the fixed `quality-verdict-v1` schema, 256 KiB/depth 16/check count 64/evidence count 8 limits, duplicate keys, unknown check, missing evidence, `unknown`, unavailable model and invalid output; every blocking case fails and no second prompt may convert it to pass.
- [ ] **Step 6 (RED): Write repair classification tests** allowing only re-mux, corrupted-frame re-render, optional-material replacement, text wrap/font/layout change and BGM/SFX level/timing change; fact errors, required missing material, cross-owner input and untrusted manifest are non-repairable.
- [ ] **Step 7: Run RED tests.**

```powershell
python -m unittest tests.test_ai_edit_v3_mux tests.test_ai_edit_v3_quality -v
```

Expected: failures identify absent mux/QC functions.

- [ ] **Step 8 (GREEN): Implement mux with an argv array** using only `file,pipe`, fixed stream maps `0:v:0` and `1:a:0`, `-c:v copy`, AAC 192 kbps, 48 kHz stereo, explicit frozen duration and `+faststart`; write to a temporary MP4, fully decode it, then rename.
- [ ] **Step 9 (GREEN): Implement deterministic technical analyzers** using FFprobe JSON, FFmpeg full decode/black/freeze/silence/EBU R128 analysis, decoded fingerprints and bounded evidence frames; record exact executor versions and input hashes.
- [ ] **Step 10 (GREEN): Consume Node DOM geometry evidence** for overflow and safe areas, consume Phase B authoritative captions/material provenance for fact/owner checks, and call the fixed visual inspector only for the checks that require vision.
- [ ] **Step 11 (GREEN): Validate and normalize every finding** through `quality-verdict-v1.schema.json`; `quality.py` returns evidence and repairability only and never mutates the job, increments `repair_count` or publishes.
- [ ] **Step 12: Run GREEN verification.**

```powershell
python -m unittest tests.test_ai_edit_v3_mux tests.test_ai_edit_v3_quality tests.test_ai_edit_v3_audio tests.test_ai_edit_v3_hyperframes -v
python -m unittest tests.test_ai_edit_v2_quality tests.test_ai_edit_v2_audio -v
git diff --check
```

Expected: all blocking thresholds are executable and fail closed; decoded video/PCM hashes match across identical renders.

- [ ] **Step 13: Commit.**

```powershell
git add server/content_domains/ai_edit_v3/media.py server/content_domains/ai_edit_v3/quality.py server/ai_edit_v3_renderer/src/report.mjs tests/test_ai_edit_v3_mux.py tests/test_ai_edit_v3_quality.py
git commit -m "feat(ai-edit-v3): mux and inspect final renders"
```

### Task 10: Stage private COS delivery and drive the publication Saga

**Files:**

- Modify: `server/content_domains/ai_edit_v3/delivery.py`
- Modify: `server/content_domains/ai_edit_v3/pipeline.py`
- Modify: `server/content_domains/ai_edit_v3/store.py`
- Modify: `server/content_domains/ai_edit_v3/runtime.py`
- Modify: `server/ai_edit_v3_worker.py`
- Modify: `tests/test_ai_edit_v3_delivery.py`
- Create: `tests/test_ai_edit_v3_phase_c_pipeline.py`
- Create: `tests/fixtures/ai_edit_v3/phase-c-cases.json`
- Create: `docs/operations/ai-edit-v3-renderer-runbook.md`

**Interfaces:**

```python
@dataclass(frozen=True)
class StagedDelivery:
    object_key: str
    sha256: str
    size_bytes: int
    etag: str
    range_status: Literal[206]
    content_range: str

def stage_private_delivery(
    owner: str,
    owner_hmac: str,
    job_id: str,
    render_attempt: int,
    final_mux: FinalMux,
    *,
    environment: Literal["test", "production"],
    cos: PrivateCos,
) -> StagedDelivery: ...

def run_phase_c_stages(
    claim: LeaseClaim,
    runtime: RuntimeDependencies,
    *,
    db_path: Path,
) -> StageOutcome: ...
```

The immutable key is exactly `{environment}/ai-edit-v3/{owner_hmac}/{job_id}/delivery/{render_attempt}-{content_sha256}.mp4`. `run_phase_c_stages()` is the only transition owner for `generating_audio -> mixing_audio -> compiling -> rendering -> quality_checking -> repair_planning -> staging_delivery -> settling -> publishing`.

- [ ] **Step 1 (RED): Write private-object tests** for the exact V3 prefix, HMAC owner scope, server-generated job ID, key-regex rejection, private ACL, no overwrite, stable-key retry, new attempt/content key, database persistence of key but not URL, and test credentials unable to write `production/ai-edit-v3/`.
- [ ] **Step 2 (RED): Write signed-read tests** requiring a 300-second GET signature, `Range: bytes=0-0`, HTTP 206, exactly one byte and valid `Content-Range`; assert HEAD is never called and a signed URL is neither logged nor persisted.
- [ ] **Step 3 (RED): Write publication tests** using Phase A's `AssetPublisher`: register current fencing generation, prepare one hidden `ai_edit_v3` asset, settle the exact cumulative delta target, commit publish and persist stable `asset_id`; no asset is visible before `publish_won`.
- [ ] **Step 4 (RED): Write response-loss tests** for `register_generation`, `prepare_hidden`, `commit_publish`, `cancel_publish` and `query_decision`; each freezes operation/key/generation/expected decision/first-unknown time, enters `asset_decision_reconciling`, reaches `failed_asset_decision_pending` after five minutes and later converges only from authoritative `publish_won` or `cancel_won`.
- [ ] **Step 5 (RED): Write one-winner tests**: publish first means `completed` with no full refund; cancel first means invisible tombstone then cumulative full-refund target; stale generation can neither first-publish nor cancel after a newer generation.
- [ ] **Step 6 (RED): Write end-to-end fake integration tests** for a talking-head portrait and audio-led landscape job across every Phase C state, durable input fingerprints, skipped stages, crash before/after each checkpoint, stale fencing write, 45-minute deadline and a single atomic 10-minute repair extension.
- [ ] **Step 7 (RED): Write terminal tests** proving second QC failure, BGM terminal failure, BGM `SubmissionUnknown` without resubmit, sandbox failure, untrusted manifest and non-repairable QC cancel publication before refund; `completed`, `refunded` and pending-reconciliation states do not lock a successor job.
- [ ] **Step 8: Run RED tests.**

```powershell
python -m unittest tests.test_ai_edit_v3_delivery tests.test_ai_edit_v3_phase_c_pipeline -v
```

Expected: missing delivery/orchestration behavior causes RED without real COS, points or provider calls.

- [ ] **Step 9 (GREEN): Implement private staging** with streaming SHA/size verification, immutable private PUT and one-byte signed Range GET; return only stable object metadata.
- [ ] **Step 10 (GREEN): Implement the existing Saga client use** with persisted idempotency keys for all five operations, monotonic generation, hidden prepare, settlement-before-commit and authoritative response-loss reconciliation.
- [ ] **Step 11 (GREEN): Implement Phase C orchestration** using Phase A transactions and StageContext checks; persist audio, manifest, render, mux, QC and delivery checkpoints before transitions, and terminate the entire render/media group on lease/deadline loss.
- [ ] **Step 12 (GREEN): Implement once-only repair routing** by atomically changing `repair_count` from 0 to 1 only when the first `QualityReport` has a permitted repairable finding; the second quality pass cannot enter repair again.
- [ ] **Step 13: Create the non-secret Phase C fixture matrix** with both ratios, both product paths, all four templates, no/current/generated images, long mixed captions, every layout/animation/transition, optional/required SFX, `volume_fade`, repairable and non-repairable QC outcomes.
- [ ] **Step 14: Write the renderer-specific runbook** with release verification, capability preflight, spool ownership, start/query/stop diagnosis, safe cleanup, checkpoint recovery, log redaction and the explicit rule that a sandbox failure cannot be bypassed with `--no-sandbox`, network access or weaker systemd controls. Leave Phase D's general V3 operations runbook to Phase D.
- [ ] **Step 15: Run the focused GREEN gate.**

```powershell
python -m unittest tests.test_ai_edit_v3_delivery tests.test_ai_edit_v3_phase_c_pipeline tests.test_ai_edit_v3_quality -v
git diff --check
```

Expected: private staging, response-loss reconciliation, one-winner publication and terminal cleanup all pass without real COS, point-service or provider calls.

- [ ] **Step 16: Commit only the delivery/pipeline boundary and its renderer runbook.**

```powershell
git add server/content_domains/ai_edit_v3/delivery.py server/content_domains/ai_edit_v3/pipeline.py server/content_domains/ai_edit_v3/store.py server/content_domains/ai_edit_v3/runtime.py server/ai_edit_v3_worker.py tests/test_ai_edit_v3_delivery.py tests/test_ai_edit_v3_phase_c_pipeline.py tests/fixtures/ai_edit_v3/phase-c-cases.json docs/operations/ai-edit-v3-renderer-runbook.md
git commit -m "feat(ai-edit-v3): complete phase c delivery pipeline"
```

### Task 11: Wire the pinned renderer checks into CI as an isolated change

**Files:**

- Modify: `.github/workflows/ci.yml`
- Create: `tests/test_ai_edit_v3_ci_wiring.py`

**Required workflow contract:**

```yaml
- uses: actions/setup-node@v6
  with:
    node-version: "22"
    cache: npm
    cache-dependency-path: |
      design-system/package-lock.json
      server/ai_edit_v3_renderer/package-lock.json

- name: Verify AI Edit V3 renderer
  working-directory: server/ai_edit_v3_renderer
  run: |
    npm ci --ignore-scripts
    npm ls hyperframes gsap --depth=0
    npm test
```

This task changes no Python dependency installation. It reuses Phase A's dependency gate, including Phase A's installed and version-reported `jsonschema` package.

- [ ] **Step 1 (RED): Create a dependency-free workflow contract test** that reads `.github/workflows/ci.yml` as text and requires `actions/setup-node@v6`, Node `22`, both package-lock cache paths, the exact renderer working directory, and all three renderer commands in one step.
- [ ] **Step 2: Run `python -m unittest tests.test_ai_edit_v3_ci_wiring -v`** and observe RED because the renderer lockfile and test commands are not wired into CI.
- [ ] **Step 3 (GREEN): Expand the existing setup-node cache input** to a YAML block containing both lockfiles, then add the renderer step shown above without changing Phase A's Python dependency-install step.
- [ ] **Step 4: Run the CI wiring gate locally.**

```powershell
python -m unittest tests.test_ai_edit_v3_ci_wiring -v
Push-Location server/ai_edit_v3_renderer
npm ci --ignore-scripts
npm ls hyperframes gsap --depth=0
npm test
Pop-Location
python scripts/ci_validate.py
git diff --check
```

Expected: the contract test and renderer tests pass, the exact dependency graph is reported, and CI contains no runtime package-download path.

- [ ] **Step 5: Commit only CI wiring and its contract test.**

```powershell
git add .github/workflows/ci.yml tests/test_ai_edit_v3_ci_wiring.py
git commit -m "ci(ai-edit-v3): test pinned renderer package"
```

### Task 12: Run the complete Phase C gate and record immutable evidence

**Files:**

- Create: `docs/verification/ai-edit-v3-phase-c.md`

- [ ] **Step 1: Run the complete local gate** and record every actual command, exit code, test count and elapsed time in the verification report.

```powershell
Push-Location server/ai_edit_v3_renderer
npm ci --ignore-scripts
npm ls hyperframes gsap --depth=0
npm test
npm run hf:check -- test/fixtures/landscape --strict --json
npm run hf:check -- test/fixtures/portrait --strict --json
npm run hf:check -- test/fixtures/animations --strict --json
npm run hf:check -- test/fixtures/transitions --strict --json
npm run hf:keyframes -- test/fixtures/animations --json
npm run hf:keyframes -- test/fixtures/transitions --json
npm run hf:snapshot -- test/fixtures/animations --at 0,0.5,1,1.4
npm run hf:snapshot -- test/fixtures/transitions --at 0,0.2,0.4,0.8
npm run hf:snapshot -- test/fixtures/landscape --at 0,1.5,3
npm run hf:snapshot -- test/fixtures/portrait --at 0,1.5,3
npm run render:fixtures
Pop-Location
python -m unittest tests.test_ai_edit_v3_renderer_release tests.test_ai_edit_v3_render_manifest tests.test_ai_edit_v3_template_catalog tests.test_ai_edit_v3_elevenlabs tests.test_ai_edit_v3_secrets_example tests.test_ai_edit_v3_audio tests.test_ai_edit_v3_render_sandbox tests.test_ai_edit_v3_hyperframes tests.test_ai_edit_v3_mux tests.test_ai_edit_v3_quality tests.test_ai_edit_v3_delivery tests.test_ai_edit_v3_phase_c_pipeline tests.test_ai_edit_v3_ci_wiring -v
python -m unittest discover -s tests -p "test_ai_edit_v3_*.py" -v
python -m unittest discover -s tests -p "test_ai_edit_v2_*.py" -v
node --test tests/test_ai_edit_v2_ui.js
$pythonFiles = @('server/ai_edit_v3_worker.py')
$pythonFiles += @(Get-ChildItem -LiteralPath 'server/content_domains/ai_edit_v3' -Recurse -File -Filter '*.py' | ForEach-Object { $_.FullName })
python -m py_compile @pythonFiles
python scripts/ci_validate.py
python scripts/stamp_assets.py --check
git diff --check
```

Expected: every command exits 0; dependency versions are exact; `hyperframes check` has zero persistent findings; V2 remains green; no real key or signed URL appears in tracked or untracked V3 fixtures.

- [ ] **Step 2: On a separately authorized test-server deployment, run the sandbox gate without enabling V3 user traffic.**

```bash
sudo systemd-analyze verify \
  /etc/systemd/system/huangque-ai-edit-v3.service \
  '/etc/systemd/system/huangque-ai-edit-v3-render@.service'
sudo systemd-analyze security huangque-ai-edit-v3-render@probe.service
sudo visudo -cf /etc/sudoers.d/huangque-ai-edit-v3-render
python -m unittest tests.test_ai_edit_v3_render_sandbox -v
```

Expected: verify/visudo/tests exit 0; the security report confirms the required namespaces and restrictions. Do not start the network Worker merely to run this static gate.

- [ ] **Step 3: With separate authorization for isolated test units, prove two-task isolation**: start two fixture renders, record different dynamic UIDs and mount namespaces, prove outbound DNS/HTTP fails, prove provider variables are absent, prove each instance cannot open the sibling input through guessed path, inherited descriptor or `/proc`, and prove Worker attempts to start another unit or inject a property are denied.
- [ ] **Step 4: Prove resource and failure behavior on the test server**: force Chromium crash, memory pressure, 8 GiB disk limit, 64-task limit, wall timeout and Worker cancellation; every case must stop the full control group, leave no child process, close the running stage and preserve a resumable checkpoint or stable terminal error.
- [ ] **Step 5: Render one `1920x1080` and one `1080x1920` fixture twice**; FFprobe must show H.264/AAC, 48 kHz stereo and correct dimensions, all blocking QC must pass, decoded video `framemd5` and PCM hashes must match between identical runs, and private COS verification must return HTTP 206 to `Range: bytes=0-0`.
- [ ] **Step 6: Record the test-host facts**—CPU/RAM/free SSD, exact release/build ID, Node/Chromium/FFmpeg/FFprobe versions, systemd version, render elapsed time, peak RSS/CPU/disk, unit IDs, decoded hashes, QC report hashes and COS key prefixes—without credentials or signed URLs.
- [ ] **Step 7: Complete `docs/verification/ai-edit-v3-phase-c.md`** with local and authorized-host evidence, explicit skipped gates, redacted logs and a final pass/fail table. A gate that lacks authorization or evidence remains pending, not passed.
- [ ] **Step 8: Run the final plan-quality scan and `git diff --check`;** require exactly twelve top-level tasks, exactly twelve commit commands, zero incomplete-instruction markers, zero undefined interface names, balanced Markdown fences and no whitespace errors.

- [ ] **Step 9: Commit only the verification evidence after every required gate is green.**

```powershell
git add docs/verification/ai-edit-v3-phase-c.md
git commit -m "docs(ai-edit-v3): record phase c verification"
```

## Phase C Exit Gate

- [ ] The active release identifies exact Node 22.x, HyperFrames 0.7.84, GSAP 3.15.0, Chromium, FFmpeg, FFprobe, font, package-lock, commit and archive hashes; runtime uses no package download.
- [ ] Twelve layouts with two variants each, fourteen visual animations, five transitions, twelve overlays and four published templates pass the required checks and horizontal/vertical snapshots.
- [ ] Every task generates a new instrumental `music_v2` BGM; declared SFX use `eleven_text_to_sound_v2`; the sole 48 kHz stereo master meets ducking, LUFS and true-peak thresholds.
- [ ] HyperFrames output is silent; final MP4 has exactly one AAC master track; audio/video begin at PTS 0 and duration drift is no greater than one frame and 40 ms.
- [ ] Same manifest, release and environment produce matching decoded video-frame and PCM hashes.
- [ ] Every blocking technical, fact, material, layout, sound and visual-verdict check passes; only one targeted repair is possible.
- [ ] Every render has a unique DynamicUser and mount namespace, no external network or provider secret, no sibling access and complete control-group termination.
- [ ] Private COS object keys are immutable and owner-scoped; Range GET returns 206; publication/refund converges through one authoritative Saga winner.
- [ ] All Phase C, V3, V2, CI, asset-stamp and diff gates are green; `AI_EDIT_V3_ENABLED` remains `0`.
- [ ] Production Go/No-Go remains pending. Passing this plan does not authorize test traffic, production deployment, production migration, production keys, production pricing or production enablement.
