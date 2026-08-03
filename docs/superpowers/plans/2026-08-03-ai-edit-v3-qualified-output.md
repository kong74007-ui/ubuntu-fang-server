# AI Edit V3 Qualified Output Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the V3 single-card technical sample path with a content-driven, scene-bound, quality-gated talking-head edit and prove it with a real online test render.

**Architecture:** Qwen supplies bounded creative choices while Python deterministically compiles caption-aligned scenes and material requests. The render manifest binds material IDs per scene, the HyperFrames renderer adapts one-material geometry, and a deterministic structural visual inspector blocks the exact failure pattern before publication.

**Tech Stack:** Python 3, `unittest`, JSON Schema, Node.js, HyperFrames 0.7.84, GSAP 3.15.0, FFmpeg/FFprobe, systemd, private COS, Qwen3.7-Max, OpenAI image generation, ElevenLabs audio.

## Global Constraints

- Keep V2 behavior, data, API, worker, Shotstack path, and site entry unchanged.
- Keep Qwen on `qwen3.7-max-2026-06-08`; no silent director-model fallback.
- Preserve authoritative captions and existing component allowlists.
- Use no user-history or public-library materials.
- Preserve private COS, ownership, sandbox, billing, and publication contracts.
- Deploy only the merged test-environment commit and create no production release.

---

### Task 1: Compile content-driven multi-scene plans

**Files:**
- Modify: `tests/test_ai_edit_v3_production.py`
- Modify: `server/content_domains/ai_edit_v3/production.py`

**Interfaces:**
- Consumes: `QwenCompiledDirector._creative_payload()` and the aligned `timeline` director request.
- Produces: schema-valid edit-plan 2.0 scenes with continuous timestamps, legal layouts, scene-local material slots, and at most four material requests.

- [ ] Add a failing test using six caption ranges over 26 seconds that expects at least four scenes, more than one layout, and no generic `product_hero` scene for a talking-head source.
- [ ] Run that single test and confirm it fails because `_compile()` returns one scene.
- [ ] Add a failing test proving malformed optional layout sequences still compile to multiple scenes.
- [ ] Implement caption grouping, safe layout sequencing, and scene-local material request generation.
- [ ] Rerun the director tests and commit the green change.

### Task 2: Bind materials to their requesting scenes

**Files:**
- Modify: `tests/test_ai_edit_v3_production.py`
- Modify: `server/content_domains/ai_edit_v3/production.py`

**Interfaces:**
- Consumes: edit-plan material request IDs and frozen `materials.json` ordering.
- Produces: render-manifest compositions whose `asset_ids` equal only the IDs in that scene's `material_slots`.

- [ ] Add a failing compile-stage test with two scenes and two materials that expects one asset per requesting scene and none in the speaker-only scene.
- [ ] Run the test and confirm the current all-assets-for-all-scenes behavior fails it.
- [ ] Build a request-ID-to-render-asset map and compile scene-local `asset_ids`.
- [ ] Preserve the frozen material hash binding and rerun production and contract tests.
- [ ] Commit the green change.

### Task 3: Render one material without an empty half and keep captions scene-local

**Files:**
- Modify: `server/ai_edit_v3_renderer/test/layouts.test.mjs`
- Modify: `server/ai_edit_v3_renderer/test/compile-project.test.mjs`
- Modify: `server/ai_edit_v3_renderer/src/registry/layouts.mjs`
- Modify: `server/ai_edit_v3_renderer/src/compile-project.mjs`

**Interfaces:**
- Consumes: composition-local asset IDs and caption time ranges.
- Produces: material-count CSS classes, a one-column one-asset region, and one scene's bounded caption text.

- [ ] Add a failing layout test expecting `hf-material-count-1` and a one-column rule.
- [ ] Add a failing compiler test with two captions in adjacent scenes and assert each scene file excludes the other scene's text.
- [ ] Run the two Node tests and confirm the expected failures.
- [ ] Emit the material-count class and one-column CSS.
- [ ] Bound caption selection to the current composition and keep escaped text behavior.
- [ ] Run all renderer tests and commit the green change.

### Task 4: Replace unconditional visual passes with a blocking structural inspector

**Files:**
- Modify: `tests/test_ai_edit_v3_production.py`
- Modify: `tests/test_ai_edit_v3_quality.py`
- Modify: `server/content_domains/ai_edit_v3/production.py`

**Interfaces:**
- Consumes: frozen render manifest and renderer report.
- Produces: complete quality-verdict-v1 findings whose visual pass/fail values reflect structural evidence.

- [ ] Add a failing test that feeds the asset-147 one-scene/product-hero/full-transcript pattern and expects blocking failures for obstruction, text visibility, and opening consistency.
- [ ] Add a passing test for a continuous multi-scene speaker/material sequence.
- [ ] Run the failing test and confirm the current inspector returns passes.
- [ ] Implement deterministic structural findings with real manifest measurements and evidence IDs.
- [ ] Run all production and quality tests and commit the green change.

### Task 5: Regression, PR, deploy, and real-output acceptance

**Files:**
- Modify only if a failing gate requires a scoped correction.

**Interfaces:**
- Consumes: the merged commit and the existing test-environment deployment/runbooks.
- Produces: a completed asset whose full timeline is visually reviewed and whose task evidence is retained.

- [ ] Run all V3 Python tests, renderer Node tests, renderer validation, and the repository CI-equivalent commands.
- [ ] Inspect the final diff for V2/public-file changes, secret leakage, generated artifacts, and contract drift.
- [ ] Commit any final scoped corrections, push the branch, open a PR, and wait for all required checks.
- [ ] Merge only the exact reviewed head, deploy the merged V3 files to the Fang test server, and restart only V3 services with no active V3 task.
- [ ] Create one real talking-head V3 task from the same content class as asset 147 and monitor it through completion.
- [ ] Download the output, inspect keyframes across the entire duration, play the full video, and verify scene variation, speaker visibility, material relevance, captions, audio, and absence of blank regions.
- [ ] If any visual acceptance item fails, add a new failing regression test and repeat the smallest affected task before claiming completion.
