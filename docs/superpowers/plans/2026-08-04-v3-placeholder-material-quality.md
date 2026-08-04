# AI Edit V3 Placeholder And Material Quality Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent material-free talking-head scenes from rendering empty split panels or debug copy, and prevent generated supplemental images from introducing unrelated presenters.

**Architecture:** Keep Qwen responsible for creative intent while applying deterministic safety constraints when compiling the plan. Enforce the same material/layout invariant in manifest quality inspection and bounded repair, remove visible renderer fallback copy as defense in depth, and constrain generated B-roll prompts at the provider boundary.

**Tech Stack:** Python 3.12 `unittest`, Node.js `node:test`, HyperFrames renderer, immutable renderer release lock.

## Global Constraints

- Existing V2 behavior and public API contracts remain unchanged.
- Talking-head scenes without bound material assets use `speaker_fullscreen`.
- Layouts that visually reserve material space must not pass the blocking publication quality gate with an empty `asset_ids` list.
- Generated supplemental visuals must not contain presenters, talking heads, portraits, recognizable people, visible text, logos, or watermarks.
- Renderer output must not expose internal placeholder labels such as `主体画面`, `主体视频`, `智能剪辑画面`, or `AI 视觉节奏`.
- Only files changed by this plan may be deployed, and deployment must use a pushed commit.

---

### Task 1: Compile material-free talking-head scenes safely

**Files:**
- Modify: `tests/test_ai_edit_v3_production.py`
- Modify: `server/content_domains/ai_edit_v3/production.py`

**Interfaces:**
- Consumes: `QwenCompiledDirector._compile(request, creative)` and the existing edit-plan schema.
- Produces: Plans where every talking-head scene with `material_slots == []` has `layout_id == "speaker_fullscreen"`.

- [ ] **Step 1: Write the failing director regression test**

Add a test whose fake Qwen result requests `speaker_left_info_right` for a talking-head scene with no uploaded or generated material, then assert that the compiled scene is `speaker_fullscreen` while a later scene with a material slot may still use a material layout.

- [ ] **Step 2: Run the focused test and verify RED**

Run: `python -m unittest tests.test_ai_edit_v3_production.ProductionDirectorTests.test_material_free_talking_head_scene_ignores_split_layout_request -v`

Expected: FAIL because the first scene currently compiles to `speaker_left_info_right`.

- [ ] **Step 3: Implement the minimal compiler constraint**

In the `has_speaker_video` branch with no slots, allow only `speaker_fullscreen`; retain current material-aware layout selection for scenes with slots.

- [ ] **Step 4: Run focused and director regression tests**

Run: `python -m unittest tests.test_ai_edit_v3_production.ProductionDirectorTests -v`

Expected: PASS.

### Task 2: Reject and repair material/layout mismatches before publication

**Files:**
- Modify: `tests/test_ai_edit_v3_production.py`
- Modify: `server/content_domains/ai_edit_v3/production.py`

**Interfaces:**
- Consumes: render manifest compositions containing `layout_id` and `asset_ids`.
- Produces: `material_semantic_identity=fail` for material-dependent layouts with no assets, plus deterministic repair to `speaker_fullscreen` for source-video manifests.

- [ ] **Step 1: Write failing inspection and repair tests**

Add one inspector test using a bounded, varied talking-head manifest where a split scene has no assets and assert that `material_semantic_identity` fails. Add one repair test asserting the same scene becomes `speaker_fullscreen` with empty `asset_ids` and the repaired manifest passes required checks.

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `python -m unittest tests.test_ai_edit_v3_production.ProductionStageCoordinatorTests.test_visual_inspector_rejects_material_layout_without_assets tests.test_ai_edit_v3_production.ProductionStageCoordinatorTests.test_repair_manifest_replaces_material_layout_without_assets -v`

Expected: at least one FAIL because the current inspector does not validate layout/asset compatibility and repair does not normalize the bad layout.

- [ ] **Step 3: Implement the invariant and bounded repair**

Define the material-dependent layout set once in `production.py`. During inspection, require each such composition to have at least one known asset. During `material_semantic_identity` repair, convert empty source-video material layouts to `speaker_fullscreen` without changing timing or captions.

- [ ] **Step 4: Run the focused tests and production suite**

Run: `python -m unittest tests.test_ai_edit_v3_production tests.test_ai_edit_v3_quality tests.test_ai_edit_v3_openai_image tests.test_ai_edit_v3_materials -v`

Expected: PASS.

### Task 3: Remove visible debug placeholder copy from renderer output

**Files:**
- Modify: `server/ai_edit_v3_renderer/test/layouts.test.mjs`
- Modify: `server/ai_edit_v3_renderer/src/registry/layouts.mjs`
- Modify: `server/ai_edit_v3_renderer/src/registry/layout-primitives.mjs`
- Modify: `server/ai_edit_v3_renderer/renderer-release.lock.json`

**Interfaces:**
- Consumes: `compileLayout(...)` and `compilePrimitiveLayout(...)` HTML.
- Produces: decorative empty-state markup with no user-visible internal labels.

- [ ] **Step 1: Write failing behavior tests**

Assert that both compiler paths omit `主体画面`, `主体视频`, `智能剪辑画面`, and `AI 视觉节奏` for empty-media inputs while retaining the required clip structure.

- [ ] **Step 2: Run the renderer test and verify RED**

Run: `npm test -- --test-name-pattern="placeholder copy"`

Expected: FAIL because the current HTML contains internal Chinese placeholder labels.

- [ ] **Step 3: Remove visible labels without weakening timing metadata**

Keep the background, speaker zone, fallback clip, IDs, timing, and track metadata, but make placeholder elements decorative and text-free.

- [ ] **Step 4: Refresh immutable renderer metadata**

Run: `npm run release:lock`

Expected: a new `renderer_build_id` and updated hashes for the modified renderer source files.

- [ ] **Step 5: Run all renderer tests and lock verification**

Run: `npm test`

Run: `npm run release:lock:check`

Expected: PASS.

### Task 4: Keep generated supplemental imagery free of unrelated presenters

**Files:**
- Modify: `tests/test_ai_edit_v3_production.py`
- Modify: `server/content_domains/ai_edit_v3/production.py`

**Interfaces:**
- Consumes: a material request semantic and plan ratio in the `generating_images` stage.
- Produces: a provider prompt explicitly requesting supplemental B-roll/graphics with no presenter, talking head, portrait, recognizable person, text, logo, or watermark.

- [ ] **Step 1: Write the failing prompt-boundary test**

Extend the generating-images test double to capture the real prompt and assert the required exclusions are present, while continuing to assert the existing bounded probe timeout.

- [ ] **Step 2: Run the focused test and verify RED**

Run: `python -m unittest tests.test_ai_edit_v3_production.ProductionStageCoordinatorTests.test_generating_images_probes_with_a_bounded_timeout -v`

Expected: FAIL because the existing prompt does not prohibit people or presenter imagery.

- [ ] **Step 3: Add the minimal supplemental-image prompt constraints**

Update the production prompt at the image-provider boundary without changing the material schema, provider API, ratio, idempotency key, or timeout behavior.

- [ ] **Step 4: Run all relevant local verification**

Run: `python -m unittest tests.test_ai_edit_v3_production tests.test_ai_edit_v3_quality tests.test_ai_edit_v3_openai_image tests.test_ai_edit_v3_materials -v`

Run: `npm test` in `server/ai_edit_v3_renderer`.

Run: `git diff --check`.

Expected: all tests pass and the diff is clean.

### Task 5: Deliver and prove the fix on the test site

**Files:**
- No additional source files unless CI or review identifies an in-scope defect.

**Interfaces:**
- Consumes: the verified branch commit and the existing test deployment workflow.
- Produces: a merged PR, immutable renderer release deployment, service verification, and a new real V3 output inspected frame-by-frame.

- [ ] **Step 1: Commit, push, open PR, and wait for all required checks**

Use an intentional commit containing only the plan files, implementation, tests, and regenerated renderer lock. Do not merge while any required check or review is pending or failing.

- [ ] **Step 2: Deploy only the pushed merge commit to the test environment**

Deploy the changed Python production module and immutable renderer release, restart only the affected V3 services, and verify public `/api/v3/edit/` health plus worker readiness.

- [ ] **Step 3: Create and monitor one real UI task**

Use the same 26-second platform talking-head asset so the regression is directly comparable. Wait for a terminal state without submitting duplicate chargeable tasks.

- [ ] **Step 4: Download and inspect the delivered MP4**

Verify codec, dimensions, frame rate, duration, audio loudness, representative scene frames, transition frames, absence of visible placeholder labels, and absence of generated substitute presenters. If any blocking defect remains, return to root-cause diagnosis before another fix.

- [ ] **Step 5: Report evidence**

Report branch, commit, PR, CI/review, deployed paths, restarted services, public verification, job ID, asset ID, elapsed time, output path, SHA-256, and remaining risks.
