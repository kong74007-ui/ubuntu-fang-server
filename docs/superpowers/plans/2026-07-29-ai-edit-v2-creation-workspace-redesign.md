# AI Edit V2 Creation Workspace Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the AI Edit V2 page as a five-step creation workspace with portrait platform-video cards, one optional candidate-material uploader, adaptive duration, and a sticky preview/quote/task panel.

**Architecture:** Preserve edit-plan protocol 2.0, the existing V2 draft, billing, queue, renderer, and quality state machine. Extend the owner-scoped platform asset list with safe presentation metadata, then replace the page's dropdown-driven state with explicit subject, edit-mode, candidate-material, output-ratio, and right-panel view states inside the existing isolated V2 page.

**Tech Stack:** Python 3, SQLite, existing HTTP handler, vanilla JavaScript, HTML/CSS, Node.js `node:test`, Python `unittest`.

## Global Constraints

- The subject is exactly one platform video, uploaded video, or uploaded audio.
- Platform video covers use a fixed `9:16` presentation frame without stretching or destructive cropping.
- Edit modes are mutually exclusive: `platform_template`, `natural_brief`, or `open_generation`; default to `open_generation`.
- Supplemental uploads are one optional candidate pool of image/video/audio files, maximum 10; AI may use any subset.
- The user interface must not show “必须使用” or “参考使用”.
- Output ratio remains selectable as `16:9` or `9:16`.
- `target_duration_ms` is always `null`; duration is AI-adaptive.
- Do not show a precharge confirmation checkbox; clicking the priced start button is the explicit submission action.
- Desktop uses a left configuration column and sticky right preview/status panel; mobile uses one column.
- Do not expose filesystem paths, COS object keys, signed provider URLs, provider names, or editable timelines.
- Do not change protocol version, billing semantics, queue ownership, renderer selection, or quality state transitions.
- Work and deploy only against the test environment; production remains untouched.

---

## File Structure

- Modify `server/content_domains/ai_edit_v2_platform_assets.py`: produce owner-scoped, safe card presentation fields.
- Modify `tests/test_ai_edit_v2_api.py`: cover platform-card metadata, owner isolation, and path redaction.
- Modify `site/workbench/ai-edit-v2.html`: own the five-step layout, portrait subject cards, candidate uploader, draft mapping, preview summary, quote button, and task state.
- Modify `tests/test_ai_edit_v2_ui.js`: exercise the page's observable state transitions and markup contract.
- Reuse `site/workbench/tasks.js`: no route or task-tracker changes.
- Reuse `server/content_domains/ai_edit_v2_api.py`: the endpoint continues to call `platform_assets.list_assets(owner)`.

### Task 1: Safe platform-video card metadata

**Files:**
- Modify: `server/content_domains/ai_edit_v2_platform_assets.py:87-109`
- Test: `tests/test_ai_edit_v2_api.py:198-246`

**Interfaces:**
- Consumes: legacy `video_assets(id, username, video_file, image_file, text, ratio, status, created_at, updated_at)`.
- Produces: `list_assets(owner) -> list[{id, reference_id, filename, summary, ratio, status, created_at, preview_url, thumbnail_url}]`.
- Security: `preview_url` and `thumbnail_url` are authenticated relative `/api/gen/file/...` URLs only.

- [ ] **Step 1: Extend the fixture and write the failing API assertions**

Add `image_file` to the test table and assert literal public output:

```python
conn.execute("""CREATE TABLE video_assets(
    id INTEGER PRIMARY KEY,job_id TEXT,username TEXT,mode TEXT,
    image_file TEXT,video_file TEXT,text TEXT,ratio TEXT,status TEXT,
    created_at INTEGER,updated_at INTEGER)""")
conn.execute(
    "INSERT INTO video_assets VALUES(?,?,?,?,?,?,?,?,?,?,?)",
    (31, "source-job-31", "alice", "text", "image/cover 31.jpg",
     "platform-video.mp4", "authoritative script 29", "16:9", "done", 1, 2),
)
```

Expected item:

```python
{
    "id": 31,
    "reference_id": "31",
    "filename": "platform-video.mp4",
    "summary": "authoritative script 29",
    "ratio": "16:9",
    "status": "done",
    "created_at": 1,
    "preview_url": "/api/gen/file/platform-video.mp4",
    "thumbnail_url": "/api/gen/file/image/cover%2031.jpg",
}
```

Also assert that neither `original_text`, `username`, absolute paths, `video_file`, nor `image_file` appears in the serialized item.

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```powershell
python -m unittest tests.test_ai_edit_v2_api.AIEditV2APITest.test_platform_asset_is_owner_scoped_and_imports_authoritative_text_without_client_truth -v
```

Expected: FAIL because `summary`, `created_at`, `preview_url`, and `thumbnail_url` are absent.

- [ ] **Step 3: Implement safe relative media URLs and expanded query fields**

Add:

```python
from urllib.parse import quote

def _preview_url(value: str | None) -> str | None:
    rel = str(value or "").replace("\\", "/").lstrip("/")
    if not rel or ".." in rel.split("/") or ":" in rel:
        return None
    return "/api/gen/file/" + quote(rel, safe="/")
```

Change the list query to select `image_file`, `text`, and `created_at`, then build:

```python
summary = " ".join(str(row["text"] or "").split())[:120]
{
    "id": int(row["id"]),
    "reference_id": str(row["id"]),
    "filename": os.path.basename(str(row["video_file"])),
    "summary": summary,
    "ratio": row["ratio"],
    "status": row["status"],
    "created_at": int(row["created_at"] or 0),
    "preview_url": _preview_url(row["video_file"]),
    "thumbnail_url": _preview_url(row["image_file"]),
}
```

Filter out any row whose `preview_url` is `None`.

- [ ] **Step 4: Run backend regression tests and verify GREEN**

Run:

```powershell
python -m unittest tests.test_ai_edit_v2_api tests.test_ai_edit_v2_e2e -v
```

Expected: all tests PASS, with owner `bob` still absent from Alice's response.

- [ ] **Step 5: Commit Task 1**

```powershell
git add server/content_domains/ai_edit_v2_platform_assets.py tests/test_ai_edit_v2_api.py
git commit -m "feat(ai-edit-v2): expose safe platform card metadata"
```

### Task 2: Five-step layout and portrait subject gallery

**Files:**
- Modify: `site/workbench/ai-edit-v2.html:7-101`
- Test: `tests/test_ai_edit_v2_ui.js`

**Interfaces:**
- Consumes: Task 1 platform items.
- Produces DOM IDs: `platformGallery`, `platformCount`, `platformReload`, `videoSubjectInput`, `audioSubjectInput`, `subjectPreview`, `subjectSummary`, `editModeSection`, `candidateInput`, `candidateGrid`, `aspectRatio`, `workspacePanel`.

- [ ] **Step 1: Write failing structural and responsive tests**

Replace old assertions for `mainAssetSelect`, `requiredInput`, `referenceInput`, `targetDuration`, and `inputMode` with:

```javascript
test('creation workspace presents the confirmed five-step layout', () => {
  const page = fs.readFileSync(pagePath, 'utf8');
  const headings = [...page.matchAll(/<h2>([^<]+)<\/h2>/g)].map((match) => match[1]);
  assert.deepEqual(headings.slice(0, 5), [
    '1. 选择主体视频或音频',
    '2. 选择剪辑方式',
    '3. 上传补充素材（可选）',
    '4. 选择画面比例',
    '5. 报价并开始创作',
  ]);
  assert.match(page, /id="platformGallery"/);
  assert.match(page, /id="videoSubjectInput"[^>]*accept="video\/\*"/);
  assert.match(page, /id="audioSubjectInput"[^>]*accept="audio\/\*"/);
  assert.match(page, /\.platform-card-media\{[^}]*aspect-ratio:9\/16/);
  assert.match(page, /\.candidate-add\{[^}]*aspect-ratio:1/);
  assert.match(page, /\.workspace-panel\{[^}]*position:sticky/);
  assert.doesNotMatch(page, /id="targetDuration"|id="requiredInput"|id="referenceInput"/);
  assert.doesNotMatch(page, /必须使用|参考使用/);
});
```

- [ ] **Step 2: Run the UI test and verify RED**

Run:

```powershell
node --test tests/test_ai_edit_v2_ui.js
```

Expected: FAIL because the old form still leads with creation mode and has no portrait gallery.

- [ ] **Step 3: Replace the page body with the five sections and right panel shell**

Use this semantic skeleton:

```html
<div class="layout"><main>
  <section class="card" id="subjectSection"><h2>1. 选择主体视频或音频</h2>
    <div class="section-head"><div class="hint">只显示当前账号已完成的口播视频</div><b id="platformCount">0 条可用</b></div>
    <div id="platformGallery" class="platform-gallery"></div>
    <button id="platformReload" class="secondary compact" hidden>重新加载</button>
    <div class="subject-upload-actions">
      <label class="subject-upload">＋ 上传视频<input id="videoSubjectInput" type="file" accept="video/*" hidden></label>
      <label class="subject-upload">＋ 上传音频<input id="audioSubjectInput" type="file" accept="audio/*" hidden></label>
    </div>
  </section>
  <section class="card" id="editModeSection"><h2>2. 选择剪辑方式</h2></section>
  <section class="card" id="candidateSection"><h2>3. 上传补充素材（可选）</h2></section>
  <section class="card" id="ratioSection"><h2>4. 选择画面比例</h2></section>
</main><aside>
  <section class="card workspace-panel" id="workspacePanel"><h2>5. 报价并开始创作</h2></section>
</aside></div>
```

Add gallery CSS with `grid-template-columns:repeat(auto-fill,minmax(150px,1fr))`, `.platform-card-media{aspect-ratio:9/16;object-fit:contain}`, selected/focus-visible states, and the existing `@media(max-width:980px)` one-column fallback.

- [ ] **Step 4: Render platform items as accessible portrait buttons**

Implement:

```javascript
function platformCard(item){
  return '<button class="platform-card" type="button" data-platform-id="'+escapeHtml(item.reference_id)+'" aria-pressed="false">'+
    '<span class="platform-card-media">'+
      '<video preload="metadata" muted playsinline src="'+escapeHtml(item.preview_url)+'" poster="'+escapeHtml(item.thumbnail_url||'')+'"></video>'+
      '<span class="duration">--:--</span></span>'+
    '<b>'+escapeHtml(item.summary||item.filename)+'</b>'+
    '<small>'+escapeHtml(formatDate(item.created_at))+'</small></button>';
}
```

On `loadedmetadata`, fill the duration badge without downloading the full video. On gallery failure, show a visible error and `platformReload`; do not hide the upload labels.

- [ ] **Step 5: Run the UI test and verify GREEN**

Run:

```powershell
node --test tests/test_ai_edit_v2_ui.js tests/test_ai_edit_dual_entry.js tests/test_cloud_shell_sidebar.js
```

Expected: all tests PASS and legacy routing assertions remain unchanged.

- [ ] **Step 6: Commit Task 2**

```powershell
git add site/workbench/ai-edit-v2.html tests/test_ai_edit_v2_ui.js
git commit -m "feat(ai-edit-v2): add portrait subject workspace"
```

### Task 3: Subject selection and three edit modes

**Files:**
- Modify: `site/workbench/ai-edit-v2.html`
- Test: `tests/test_ai_edit_v2_ui.js`

**Interfaces:**
- Produces state: `state.main`, `state.mainPreviewUrl`, `state.mode`, `state.platformItems`.
- Mode mapping: template=`platform_template`, prompt=`natural_brief`, automatic=`open_generation`.

- [ ] **Step 1: Write failing behavioral tests for single subject and conditional mode controls**

Extract the page functions as existing tests do and assert:

```javascript
assert.equal(defaultState.mode, 'open_generation');
selectMode('platform_template');
assert.equal(templatePanel.hidden, false);
assert.equal(promptPanel.hidden, true);
selectMode('natural_brief');
assert.equal(templatePanel.hidden, true);
assert.equal(promptPanel.hidden, false);
selectMode('open_generation');
assert.equal(templatePanel.hidden, true);
assert.equal(promptPanel.hidden, true);
```

For subject replacement, select platform asset A then an uploaded audio asset and assert the audio asset is the only `state.main` and the old object URL is revoked.

- [ ] **Step 2: Run the focused UI test and verify RED**

Run:

```powershell
node --test --test-name-pattern="single subject|edit mode" tests/test_ai_edit_v2_ui.js
```

Expected: FAIL because old state defaults to `natural_brief` and subject selection is dropdown-driven.

- [ ] **Step 3: Implement the explicit state transitions**

Initialize:

```javascript
var state={
  mode:'open_generation', main:null, mainPreviewUrl:null,
  candidates:[], platformItems:[], quote:null, jobRequestKey:null,
  jobId:null, pollTimer:null, acceptsSubmissions:false
};
```

Add `setMainSubject(subject, previewUrl)` to revoke the previous local object URL, replace `state.main`, update `aspectRatio` for video dimensions, clear gallery `aria-pressed`, render the right preview, and invalidate the quote.

Add `selectEditMode(mode)` to toggle `.on`, `aria-pressed`, `templatePanel.hidden`, and `promptPanel.hidden`; it must not clear subject or candidates.

Platform card selection calls the existing `POST /api/v2/edit/platform-assets/{id}/import`; only the successful response calls `setMainSubject` with `input_mode='platform_video'`.

- [ ] **Step 4: Implement separate video and audio uploads**

Bind `videoSubjectInput` and `audioSubjectInput` to the existing upload ticket/COS flow using purpose `primary`. Reject a mismatched MIME type before creating a ticket. On success set `input_mode` to `external_video` or `audio_only` respectively.

- [ ] **Step 5: Run UI tests and verify GREEN**

Run:

```powershell
node --test tests/test_ai_edit_v2_ui.js
```

Expected: all tests PASS; switching edit modes does not change the selected subject.

- [ ] **Step 6: Commit Task 3**

```powershell
git add site/workbench/ai-edit-v2.html tests/test_ai_edit_v2_ui.js
git commit -m "feat(ai-edit-v2): add subject and edit mode state"
```

### Task 4: One candidate-material pool and adaptive-duration draft

**Files:**
- Modify: `site/workbench/ai-edit-v2.html`
- Test: `tests/test_ai_edit_v2_ui.js`
- Test: `tests/test_ai_edit_v2_api.py`

**Interfaces:**
- Produces: `state.candidates: UploadItem[]` with maximum length 10.
- Draft compatibility: `required_materials=[]`, `reference_materials=ready candidates with reference_mode='direct_use'`, `target_duration_ms=null`.

- [ ] **Step 1: Write failing candidate and draft behavior tests**

Add assertions:

```javascript
const draft = buildDraft();
assert.deepEqual(draft.required_materials, []);
assert.equal(draft.reference_materials.length, 2);
assert.equal(draft.reference_materials[0].reference_mode, 'direct_use');
assert.equal(draft.target_duration_ms, null);
assert.equal(draft.aspect_ratio, '9:16');
```

Test that an 11-file selection is rejected before `api('/api/v2/edit/uploads')` is called, while deleting one candidate permits one additional upload and invalidates the quote.

- [ ] **Step 2: Run candidate tests and verify RED**

Run:

```powershell
node --test --test-name-pattern="candidate|adaptive duration" tests/test_ai_edit_v2_ui.js
```

Expected: FAIL because old code maintains separate required/reference arrays and reads `targetDuration`.

- [ ] **Step 3: Implement the square add card and candidate rendering**

Use:

```html
<div id="candidateGrid" class="candidate-grid">
  <label class="candidate-add" aria-label="添加补充素材">＋
    <input id="candidateInput" type="file" multiple accept="image/*,video/*,audio/*" hidden>
  </label>
</div>
```

Render each item as a `1:1` card with preview, kind badge, progress, delete, and retry. Upload with purpose `reference` and `reference_mode='direct_use'` only as an internal protocol adapter; do not render either legacy term in visible copy.

- [ ] **Step 4: Replace draft construction**

Build:

```javascript
var draft={
  creation_mode:state.mode,
  brief:state.mode==='natural_brief' ? $('brief').value.trim() : '请根据内容完成一条高质量视频',
  language:'zh-CN',
  aspect_ratio:$('aspectRatio').value,
  target_duration_ms:null,
  input_mode:state.main.input_mode,
  main_input:state.main.asset,
  required_materials:[],
  reference_materials:state.candidates.map(function(item){return item.asset})
};
```

Keep template ID/version only for `platform_template` and require non-empty prompt only for `natural_brief`.

- [ ] **Step 5: Run frontend and API validation tests**

Run:

```powershell
node --test tests/test_ai_edit_v2_ui.js
python -m unittest tests.test_ai_edit_v2_api -v
```

Expected: all tests PASS; the existing server schema accepts the compatibility mapping without a protocol change.

- [ ] **Step 6: Commit Task 4**

```powershell
git add site/workbench/ai-edit-v2.html tests/test_ai_edit_v2_ui.js
git commit -m "feat(ai-edit-v2): add optional candidate material pool"
```

### Task 5: Sticky preview, quote action, and task-state panel

**Files:**
- Modify: `site/workbench/ai-edit-v2.html`
- Test: `tests/test_ai_edit_v2_ui.js`

**Interfaces:**
- Consumes: subject, mode, candidates, ratio, quote, and existing `pollJob()` response.
- Produces: preview, summary rows, context-aware primary button, status stage, result playback/download/asset links.

- [ ] **Step 1: Write failing preview and action-state tests**

Assert the real render functions produce these observable states:

```javascript
renderWorkspacePanel();
assert.equal(primaryAction.textContent, '请先选择主体');
assert.equal(primaryAction.disabled, true);

state.main = readyVideo;
renderWorkspacePanel();
assert.equal(primaryAction.textContent, '获取价格区间');

state.quote = {held_points: 30, minimum_points: 20, maximum_points: 30};
renderWorkspacePanel();
assert.equal(primaryAction.textContent, '开始创作 · 30点');
assert.equal(materialCount.textContent, '2个');
assert.doesNotMatch(page, /id="confirmPrecharge"/);
```

Add stage mapping assertions for `queued`, `transcribing`, `planning`, `resolving_materials`, `rendering`, `quality_check`, `repairing`, `completed`, and a `_failed` status.

- [ ] **Step 2: Run preview tests and verify RED**

Run:

```powershell
node --test --test-name-pattern="workspace panel|task stage" tests/test_ai_edit_v2_ui.js
```

Expected: FAIL because quote and task status are separate cards and no subject preview summary exists.

- [ ] **Step 3: Implement one right-panel render function**

`renderWorkspacePanel()` must update:

- `subjectPreview`
- `subjectSummary`
- `editModeSummary`
- `materialCount`
- `ratioSummary`
- `priceSummary`
- `primaryAction`

Before quote, `primaryAction.onclick=requestQuote`; after quote, it becomes `confirmJob` and displays the held-point ceiling. Clicking that priced button is the explicit precharge acceptance. Do not render `confirmPrecharge`. Disable the action while imports/uploads/requests are pending.

- [ ] **Step 4: Merge task status into the same panel**

Keep current timing, quality, billing, retry, result video, download, and asset link elements inside `workspacePanel`. Map API stages to Chinese labels without changing stored statuses. On `completed`, show result actions; on failure, show retry and refund information.

- [ ] **Step 5: Run all page regression tests**

Run:

```powershell
node --test tests/test_ai_edit_v2_ui.js tests/test_ai_edit_dual_entry.js tests/test_cloud_shell_sidebar.js
```

Expected: all tests PASS, with legacy page routing unchanged.

- [ ] **Step 6: Commit Task 5**

```powershell
git add site/workbench/ai-edit-v2.html tests/test_ai_edit_v2_ui.js
git commit -m "feat(ai-edit-v2): unify preview quote and task status"
```

### Task 6: Full verification and test-site deployment

**Files:**
- Verify: all changed files
- Deploy: test host `/home/ubuntu/content-api` and `/var/www/html/workbench/ai-edit-v2.html`

**Interfaces:**
- Consumes: Tasks 1-5.
- Produces: traceable test deployment and evidence for user acceptance.

- [ ] **Step 1: Run the complete focused verification suite**

```powershell
python -m unittest tests.test_ai_edit_v2_api tests.test_ai_edit_v2_e2e -v
node --test tests/test_ai_edit_v2_ui.js tests/test_ai_edit_dual_entry.js tests/test_cloud_shell_sidebar.js
git diff --check
```

Expected: zero failures and zero whitespace errors.

- [ ] **Step 2: Verify no secrets or forbidden provider terms entered the page**

```powershell
rg -n "sk-|AKID|q-signature|shotstack|remotion|qwen|fun-asr|openai|gpt-image" site/workbench/ai-edit-v2.html
```

Expected: no matches.

- [ ] **Step 3: Back up and deploy only to the test environment**

Back up the current test files, copy changed Python files into `/home/ubuntu/content-api/content_domains/`, copy the HTML into `/var/www/html/workbench/`, then restart `huangque-content` and `huangque-ai-edit-v2`. Do not touch production hosts or production DNS.

- [ ] **Step 4: Verify server health and deployed hashes**

```powershell
ssh admin@8.134.216.162 sudo systemctl is-active huangque-content huangque-ai-edit-v2
curl.exe -sSI https://fang.huangquechuanmei.com/workbench/ai-edit-v2
```

Expected: both services `active`, page HTTP 200, and local/deployed SHA-256 hashes equal.

- [ ] **Step 5: Verify authenticated test behavior**

Using the test account:

1. Confirm 11 portrait platform cards appear.
2. Select a card and verify the right preview changes.
3. Switch all three edit modes and verify only the applicable control appears.
4. Upload image/video/audio candidates and confirm the maximum of 10.
5. Select `9:16`, obtain a quote, and confirm `target_duration_ms` is absent from user input and `null` in the request.
6. Submit one task and verify the right panel advances through the task state and presents the completed result or an actionable failure/refund state.

- [ ] **Step 6: Commit any deployment manifest update, if the repository already tracks one**

If no manifest file changes, leave the working tree clean and do not create an empty commit.
