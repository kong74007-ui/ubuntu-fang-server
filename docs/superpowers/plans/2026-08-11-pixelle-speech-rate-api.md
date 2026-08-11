# Pixelle Speech Rate API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose Pixelle's native `tts_speed` setting through both video generation API paths.

**Architecture:** Add an immutable upstream patch that extends `VideoGenerateRequest` with a validated float and copies it into sync and async `video_params`. Keep all runtime behavior inside pinned Pixelle; the deployment repository only installs and verifies the patch.

**Tech Stack:** Bash installer, Git patches, Python unittest, Pydantic, Pixelle Video.

## Global Constraints

- `tts_speed` range is `0.5` through `2.0`, default `1.0`.
- Both sync and async endpoints must pass the value to Pixelle.
- Existing external narration requests remain valid and do not apply Pixelle TTS.
- Submit a PR only; do not merge or deploy.

---

### Task 1: Add a tested speech-rate deployment patch

**Files:**
- Create: `deploy/pixelle-video/patches/0008-support-tts-speed-api.patch`
- Modify: `deploy/pixelle-video/install.sh`
- Modify: `tests/test_pixelle_deployment.py`

**Interfaces:**
- Consumes: upstream `VideoGenerateRequest` and sync/async `video_params` dictionaries.
- Produces: JSON request field `tts_speed: float` passed to `generate_video(tts_speed=...)`.

- [ ] **Step 1: Write the failing deployment contract test**

Add a test that requires an installer variable for patch 0008, fail-closed `git apply --check`, and patch markers for `tts_speed: float = Field(default=1.0, ge=0.5, le=2.0` plus two `"tts_speed": request_body.tts_speed` insertions.

- [ ] **Step 2: Run the focused test and verify failure**

Run: `python -m unittest tests.test_pixelle_deployment.PixelleDeploymentTests.test_tts_speed_patch_is_fail_closed_and_covers_sync_and_async`

Expected: FAIL because patch 0008 and installer wiring do not exist.

- [ ] **Step 3: Implement the minimal patch and installer wiring**

Create patch 0008 against pinned upstream commit `848b054e4fae40dabc62ec58e960b573e83793ac`. Add `tts_speed` to `api/schemas/video.py`; add it to both dictionaries in `api/routers/video.py`. In `install.sh`, define the patch path, verify it exists, run `git apply --check`, then apply it before release activation.

- [ ] **Step 4: Run focused and deployment regression tests**

Run: `python -m unittest tests.test_pixelle_deployment tests.test_pixelle_external_audio`

Expected: PASS.

- [ ] **Step 5: Verify patch applicability against pinned upstream**

Run the repository's existing Pixelle patch verification procedure or apply all numbered patches in order to a clean pinned source checkout. Expected: every patch applies without fuzz or rejected hunks.

- [ ] **Step 6: Commit**

```bash
git add deploy/pixelle-video/patches/0008-support-tts-speed-api.patch deploy/pixelle-video/install.sh tests/test_pixelle_deployment.py docs/superpowers/plans/2026-08-11-pixelle-speech-rate-api.md
git commit -m "feat(pixelle): expose speech rate in video API"
```

### Task 2: Publish the generation-server PR

**Files:** No source changes.

**Interfaces:**
- Produces: generation-server PR URL and exact head SHA for the dependent main-site PR.

- [ ] **Step 1: Run repository status and secret checks**

Run the targeted unit tests again, `git diff --check origin/main...HEAD`, and confirm no credentials or generated media are present.

- [ ] **Step 2: Push the branch and open a PR**

Push `codex/pixelle-speech-rate` and open a PR targeting `main`, explicitly documenting that it only exposes existing Pixelle behavior and is a prerequisite for the main-site PR.

