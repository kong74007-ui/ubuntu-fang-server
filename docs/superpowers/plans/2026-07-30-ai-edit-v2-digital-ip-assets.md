# AI Edit V2 Digital IP Assets Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restrict the AI Edit V2 account asset carousel and platform import endpoint to verified current-account Digital IP talking videos.

**Architecture:** Add one provenance predicate in `ai_edit_v2_platform_assets.py` that cross-checks each `video_assets` row against its original `jobs` row. Reuse it for list and import, expose a fixed `asset_type`, and keep a defensive UI filter and explicit Digital IP copy.

**Tech Stack:** Python 3.12, SQLite, unittest, vanilla JavaScript.

## Global Constraints

- Only the Fang test environment may be deployed; production is out of scope.
- Uploaded video and audio remain valid through the existing upload controls.
- Source validation is server authoritative and fails closed.

---

### Task 1: Provenance enforcement

**Files:**
- Modify: `tests/test_ai_edit_v2_api.py`
- Modify: `server/content_domains/ai_edit_v2_platform_assets.py`

**Interfaces:**
- Consumes: `video_assets.job_id`, `video_assets.username`, `video_assets.mode`, and `jobs(id, username, kind, payload)`.
- Produces: `_is_digital_ip_asset(row) -> bool`; list items with `asset_type="digital_ip"`.

- [ ] Add fixtures for another owner, other asset mode, wrong task kind, mismatched payload mode, incomplete asset, missing video file, and valid text/audio assets.
- [ ] Run the focused API test and verify it fails because unverified rows remain listable/importable.
- [ ] Implement the provenance predicate and reuse it in `list_assets()` and `import_asset()`.
- [ ] Run the focused API test and verify it passes.

### Task 2: UI contract

**Files:**
- Modify: `tests/test_ai_edit_v2_ui.js`
- Modify: `site/workbench/ai-edit-v2.html`

**Interfaces:**
- Consumes: `/api/v2/edit/platform-assets` items with `asset_type="digital_ip"`.
- Produces: a carousel containing only verified Digital IP items while retaining both upload controls.

- [ ] Add assertions for the dedicated endpoint, defensive `asset_type` filter, Digital IP copy, and absence of the generic video-assets endpoint in the subject loader.
- [ ] Run the focused Node test and verify it fails on the missing contract.
- [ ] Implement the defensive filter and copy update.
- [ ] Run the focused Node test and verify it passes.

### Task 3: Verification and delivery

**Files:**
- Verify all modified files.

- [ ] Run all AI Edit V2 Python and JavaScript tests.
- [ ] Run Python syntax compilation and `git diff --check`.
- [ ] Commit, push, and open a pull request.
- [ ] Obtain independent review and green CI before test deployment.
- [ ] Verify zero active V2 jobs, SQLite health and backup, deploy only Fang test, then verify the live list and import rejection behavior.
