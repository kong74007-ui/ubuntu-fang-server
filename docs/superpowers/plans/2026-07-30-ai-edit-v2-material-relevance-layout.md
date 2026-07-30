# AI Edit V2 Material Relevance and Stable Layout Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent unverified or repeated supplemental assets from entering AI Edit V2 videos and map semantic scene layouts to visibly different, audited Shotstack compositions.

**Architecture:** Keep Qwen provider-neutral and keep Shotstack as the only stable renderer. Strengthen the material repository/resolver boundary so only current-job candidates with trusted relevance evidence are auto-selected, then carry fixed layout metadata through the internal render graph into Shotstack clips.

**Tech Stack:** Python 3.12, SQLite, unittest, Qwen edit-plan 2.0, Shotstack Edit API, OpenAI image fallback.

## Global Constraints

- Automatic materials are current-job uploads followed by GPT image generation only.
- User history and platform public assets remain disabled.
- Required materials must be used at least once or the task fails.
- Qwen never receives COS keys, signed URLs, Shotstack fields, coordinates, HTML, JSX, or executable code.
- Shotstack receives only audited fixed layout enums and dimensions.

---

### Task 1: Enforce trusted material relevance

**Files:**
- Modify: `server/content_domains/ai_edit_v2_runtime.py`
- Modify: `server/content_domains/ai_edit_v2_materials.py`
- Test: `tests/test_ai_edit_v2_runtime.py`
- Test: `tests/test_ai_edit_v2_materials.py`

**Interfaces:**
- Consumes: `_MaterialRepositories._asset(row, required=False)` and `resolve_materials(job_id, plan, repositories, image_provider)`.
- Produces: current-job candidates with explicit relevance evidence, or a `gpt_image` fallback.

- [ ] **Step 1: Write failing tests**

Add one runtime test proving an ordinary database row without trusted analysis is not emitted as `relevant=True`, and one resolver test proving a non-required candidate with no relevance evidence is excluded as `relevance_unverified` and triggers image generation.

- [ ] **Step 2: Verify RED**

Run:

```powershell
python -m unittest tests.test_ai_edit_v2_materials tests.test_ai_edit_v2_runtime -v
```

Expected: FAIL because `_asset` currently invents `relevant=True, score=1.0` and `_exclusion_code` accepts candidates without evidence.

- [ ] **Step 3: Implement the minimal fix**

Read a persisted, allowlisted relevance result when present. Do not invent relevance for ordinary materials. Required materials keep explicit user-authorized relevance. Add `relevance_unverified` before ratio selection for ordinary candidates whose `relevant` value is not exactly `True` or whose score is not a finite number in `[0, 1]`.

- [ ] **Step 4: Verify GREEN**

Run the two suites again and require all tests to pass.

- [ ] **Step 5: Commit**

```powershell
git add server/content_domains/ai_edit_v2_runtime.py server/content_domains/ai_edit_v2_materials.py tests/test_ai_edit_v2_runtime.py tests/test_ai_edit_v2_materials.py
git commit -m "fix(ai-edit-v2): require material relevance evidence"
```

### Task 2: Map semantic layouts to stable Shotstack compositions

**Files:**
- Modify: `server/content_domains/ai_edit_v2_shotstack.py`
- Modify: `server/content_domains/ai_edit_v2_schema.py`
- Test: `tests/test_ai_edit_v2_shotstack.py`
- Test: `tests/test_ai_edit_v2_schema.py`

**Interfaces:**
- Consumes: scene `layout`, `visual_type`, aspect ratio, primary video, and resolved material slots.
- Produces: render graph components with audited `position`, `width`, `height`, and `fit`, compiled to valid Shotstack clips.

- [ ] **Step 1: Write failing render-graph tests**

Create literal expectations for `speaker_focus`, `speaker_product_split`, `split_screen`, `full_bleed`, and `data_card`. Assert that `speaker_focus` has no material clip, split layouts use bounded PIP dimensions, and `full_bleed` remains full-canvas.

- [ ] **Step 2: Verify RED**

Run:

```powershell
python -m unittest tests.test_ai_edit_v2_shotstack -v
```

Expected: FAIL because the graph currently ignores `layout` and all B-roll compiles without layout parameters.

- [ ] **Step 3: Extend the audited graph contract**

Allow only fixed `position`, positive integer `width`/`height`, and `fit` in `{"crop", "contain"}` on visual components. Reject arbitrary values before provider submission.

- [ ] **Step 4: Implement deterministic layout mapping**

Add a private mapping function keyed by `(aspect_ratio, layout, component_type)`. Apply it when building material components and title cards. Keep primary video full-canvas; use fixed safe widths/heights for PIP and information cards.

- [ ] **Step 5: Compile layout fields to Shotstack**

Copy only validated fields onto image/video clips. Keep existing visual z-order, transitions, captions, mastered audio muting, and short-lived signing unchanged.

- [ ] **Step 6: Verify GREEN**

Run:

```powershell
python -m unittest tests.test_ai_edit_v2_shotstack tests.test_ai_edit_v2_schema -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```powershell
git add server/content_domains/ai_edit_v2_shotstack.py server/content_domains/ai_edit_v2_schema.py tests/test_ai_edit_v2_shotstack.py tests/test_ai_edit_v2_schema.py
git commit -m "feat(ai-edit-v2): adapt semantic shotstack layouts"
```

### Task 3: Tighten Qwen layout and slot instructions

**Files:**
- Modify: `server/content_domains/ai_edit_v2_director.py`
- Test: `tests/test_ai_edit_v2_director.py`

**Interfaces:**
- Consumes: the existing safe director context.
- Produces: the unchanged edit-plan 2.0 schema with more coherent layout, visual type, and slot choices.

- [ ] **Step 1: Write a failing prompt-contract test**

Assert the system prompt requires semantic agreement between `layout`, `visual_type`, and `material_slots`; forbids slots for `speaker_focus`; limits slot reuse; and asks for layout variation only when content supports it.

- [ ] **Step 2: Verify RED**

Run:

```powershell
python -m unittest tests.test_ai_edit_v2_director -v
```

Expected: FAIL because these constraints are absent.

- [ ] **Step 3: Add the minimal prompt rules**

Add Chinese instructions only. Do not add provider fields, coordinates, new schema fields, or model changes.

- [ ] **Step 4: Verify GREEN**

Run the director tests and require all tests to pass.

- [ ] **Step 5: Commit**

```powershell
git add server/content_domains/ai_edit_v2_director.py tests/test_ai_edit_v2_director.py
git commit -m "fix(ai-edit-v2): constrain director material layouts"
```

### Task 4: Full verification and PR handoff

**Files:**
- Verify all files changed in Tasks 1-3.

**Interfaces:**
- Produces: a clean branch ready for independent review and test-environment deployment.

- [ ] **Step 1: Run focused tests**

```powershell
python -m unittest tests.test_ai_edit_v2_materials tests.test_ai_edit_v2_runtime tests.test_ai_edit_v2_shotstack tests.test_ai_edit_v2_schema tests.test_ai_edit_v2_director -v
```

- [ ] **Step 2: Run the complete V2 suite**

```powershell
python -m unittest discover -s tests -p "test_ai_edit_v2*.py"
```

- [ ] **Step 3: Run static verification**

```powershell
python -m py_compile server/content_domains/ai_edit_v2_materials.py server/content_domains/ai_edit_v2_runtime.py server/content_domains/ai_edit_v2_shotstack.py server/content_domains/ai_edit_v2_schema.py server/content_domains/ai_edit_v2_director.py
python scripts/ci_validate.py
git diff --check origin/main...HEAD
```

- [ ] **Step 4: Review the diff**

Confirm no user-history lookup, provider URL persistence, arbitrary layout coordinates, model replacement, Remotion/HyperFrames integration, or unrelated refactor was added.

- [ ] **Step 5: Push and open a draft PR**

Push `codex/ai-edit-v2-material-layout`, open a draft PR with root-cause evidence and test results, and leave deployment for a separately authorized test-environment step.
