# AI Edit V2 Material Boundary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop AI Edit V2 from using historical talking-head videos as automatic B-roll and refresh private playback URLs at preview/download click time.

**Architecture:** Restrict the provider-neutral material resolver to current-job uploads and carry a resolver-wide set of selected optional assets so later slots fall back to GPT image generation. Keep private COS URLs short-lived; the asset page obtains a fresh owner-scoped URL immediately before preview or download.

**Tech Stack:** Python 3.12, SQLite, unittest, vanilla JavaScript, Tencent COS, existing `/api/gen/video/assets` endpoint.

## Global Constraints

- Material sources are current-task uploads followed by GPT image generation only.
- User history, platform public assets, and AI short-video generation are disabled.
- The primary talking-head video is never a supplemental candidate.
- Private COS signatures remain five minutes and are never persisted.
- Only the test environment may be deployed; production must remain untouched.

---

### Task 1: Restrict material sources and cross-slot reuse

**Files:**
- Modify: `server/content_domains/ai_edit_v2_materials.py`
- Modify: `server/content_domains/ai_edit_v2_runtime.py`
- Test: `tests/test_ai_edit_v2_materials.py`
- Test: `tests/test_ai_edit_v2_runtime.py`

**Interfaces:**
- Consumes: `resolve_materials(job_id, plan, repositories, image_provider)` and `_MaterialRepositories.search(source, job_id, slot)`.
- Produces: a resolved plan whose materials come only from `current_upload` or `gpt_image`.

- [ ] **Step 1: Write failing resolver tests**

Add tests that record repository search calls and assert they equal `['current_upload']`; provide history/public candidates and assert they are ignored and GPT image generation is used. Add a two-slot plan with one optional current upload and assert the upload fills only one slot while the second slot is generated.

```python
self.assertEqual(repos.search_calls, ['current_upload', 'current_upload'])
self.assertEqual(resolved['materials']['slot_one']['source'], 'current_upload')
self.assertEqual(resolved['materials']['slot_two']['source'], 'gpt_image')
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m unittest tests.test_ai_edit_v2_materials -v`

Expected: FAIL because the resolver still queries `user_history`/`platform_public` and reuses the same asset across slots.

- [ ] **Step 3: Implement the minimal resolver change**

Set the active source priority to only `('current_upload',)`. Create `selected_optional_assets: set[str]` outside the slot loop, pass it to candidate exclusion, and add a selected optional asset after choosing it. Required assets retain the existing at-least-once semantics.

```python
SOURCE_PRIORITY = ('current_upload',)
selected_optional_assets: set[str] = set()
```

Change `_MaterialRepositories.search` so only `current_upload` returns non-primary rows bound to the current job; every other source returns an empty list.

- [ ] **Step 4: Run resolver/runtime tests and verify GREEN**

Run: `python -m unittest tests.test_ai_edit_v2_materials tests.test_ai_edit_v2_runtime -v`

Expected: PASS.

- [ ] **Step 5: Commit the material boundary**

```bash
git add server/content_domains/ai_edit_v2_materials.py server/content_domains/ai_edit_v2_runtime.py tests/test_ai_edit_v2_materials.py tests/test_ai_edit_v2_runtime.py
git commit -m "Fix V2 material source boundaries"
```

### Task 2: Refresh V2 playback URLs on user action

**Files:**
- Modify: `site/workbench/assets.html`
- Create: `tests/test_ai_edit_v2_asset_preview_ui.py`

**Interfaces:**
- Consumes: owner-scoped `GET /api/gen/video/assets?limit=120` and an asset object containing `id`, `mode`, and `video_url`.
- Produces: `refreshVideoAssetUrl(asset) -> Promise<string>` and preview/download handlers that use its result.

- [ ] **Step 1: Write failing asset-page tests**

Add static contract tests asserting that V2 click handlers call `refreshVideoAssetUrl(x)`, the refresh request uses `cache:'no-store'` plus the Bearer token, matches the returned item by numeric/string asset ID, rejects empty URLs, and legacy assets return their existing URL without fetching.

```python
self.assertIn("function refreshVideoAssetUrl(x)", html)
self.assertIn("cache:'no-store'", refresh_block)
self.assertIn("Authorization:'Bearer '+tok", refresh_block)
self.assertIn("refreshVideoAssetUrl(x).then", video_card)
```

- [ ] **Step 2: Run test and verify RED**

Run: `python -m unittest tests.test_ai_edit_v2_asset_preview_ui -v`

Expected: FAIL because the helper and click-time refresh do not exist.

- [ ] **Step 3: Implement click-time refresh**

Add `refreshVideoAssetUrl(x)`. For non-V2 assets return the existing URL. For V2, fetch the asset list with `cache:'no-store'`, require HTTP success, find the same ID and `mode==='ai_edit_v2'`, require a non-empty `video_url`, update `x.video_url`, and return it.

Update preview and download handlers to wait for this promise. On error, show `无法刷新视频地址，请重试` and do not call `openAssetVideoModal` or `downloadAsset` with the old URL.

- [ ] **Step 4: Run UI tests and JavaScript syntax check**

Run: `python -m unittest tests.test_ai_edit_v2_asset_preview_ui tests.test_assets_collect_ui -v`

Run: the repository's existing JavaScript syntax/test command used by CI.

Expected: PASS with no syntax errors.

- [ ] **Step 5: Commit the playback refresh**

```bash
git add site/workbench/assets.html tests/test_ai_edit_v2_asset_preview_ui.py
git commit -m "Refresh V2 asset links on demand"
```

### Task 3: Full verification, PR, test deployment, and real task

**Files:**
- Verify all files from Tasks 1 and 2.

**Interfaces:**
- Consumes: the two independently tested commits.
- Produces: reviewed PR, test-only deployment, and one real completed video without historical mouth-video B-roll.

- [ ] **Step 1: Run complete verification**

Run:

```bash
python -m unittest discover -s tests -p 'test_ai_edit_v2*.py'
python -m py_compile server/content_domains/ai_edit_v2_materials.py server/content_domains/ai_edit_v2_runtime.py
git diff --check origin/main...HEAD
```

Also run the repository JavaScript suite used by CI.

- [ ] **Step 2: Push an isolated PR**

Push `codex/ai-edit-v2-material-sources`, open a draft PR describing both root causes, tests, and test-only deployment boundary, then request independent review.

- [ ] **Step 3: Merge only after both CI gates pass**

Require PR CI success, independent Go review, merge to main, and main CI success.

- [ ] **Step 4: Deploy only the changed runtime/static files to test**

Before deployment require zero active jobs and SQLite `quick_check=ok`; create a root-only rollback backup. Restart only affected services and verify HTTP 200, service state, deployed hashes, and authenticated asset API behavior.

- [ ] **Step 5: Run one real editing task**

Use a platform talking-head video without supplemental uploads. Verify director slots resolve to GPT images rather than `user_history`, Shotstack renders successfully, quality passes, billing settles/refunds the difference, and the asset enters the library.

- [ ] **Step 6: Verify expired-page recovery**

Keep the asset page open beyond five minutes, then click preview and download. Verify a new signed URL is fetched, the modal reaches `readyState >= 3`, and the download returns non-empty MP4 bytes.
