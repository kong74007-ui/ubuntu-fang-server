# AI Edit V2 平台口播懒加载 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让账号内数字化 IP 口播视频在卡片选择时只更新封面和选择状态，仅在右侧点击播放后加载视频，并把平台素材导入延迟到获取报价之前。

**Architecture:** 保持现有单页前端、后端 API 和数据库不变，在 `ai-edit-v2.html` 的客户端状态中区分“已选但未导入”“封面预览”“已激活播放”。报价入口通过 `ensureMainAsset()` 将当前平台选择解析为 V2 主体素材，并在异步返回时校验选择身份，避免旧请求覆盖新主体。

**Tech Stack:** 原生 HTML/CSS/JavaScript、Node.js `node:test`、现有 Python `unittest` API 回归套件。

## Global Constraints

- 仅修改“账号内数字化 IP 口播视频”的选择、预览和导入时机。
- 用户上传视频、上传音频、补充素材、模板、提示词、比例、计费和任务流程保持不变。
- 不修改后端接口和数据库结构；继续使用 `/api/v2/edit/platform-assets`、`/api/v2/edit/platform-assets/{id}/import` 和 `/api/v2/edit/quote`。
- 初始平台预览 DOM 不得包含 `<video>` 或视频 `src`；只有用户明确点击右侧播放按钮后才创建播放器。
- 测试不得调用“开始创作”、不得创建剪辑任务、不得扣点。
- 仅允许部署 Fang 测试环境；生产环境不在本计划授权范围内。

---

### Task 1: 平台主体选择与右侧按需播放

**Files:**
- Modify: `tests/test_ai_edit_v2_ui.js`
- Modify: `site/workbench/ai-edit-v2.html:23-25,64,77-87`

**Interfaces:**
- Consumes: 平台列表项 `{reference_id, summary, filename, preview_url, thumbnail_url, ratio}`。
- Produces: `setMainSubject(subject, previewUrl, ownsPreview, posterUrl)`、`activateSubjectPreview()`；新增状态 `mainPosterUrl`、`mainPreviewActivated`、`mainPreviewError`。

- [ ] **Step 1: 写平台卡片选择零导入的失败测试**

在 `tests/test_ai_edit_v2_ui.js` 增加行为测试，提取并执行 `selectPlatformAsset()`：

```js
test('selecting a platform subject stores an unresolved subject without importing or loading video', async () => {
  const page = fs.readFileSync(pagePath, 'utf8');
  const source = page.match(/(?:async )?function selectPlatformAsset\(id\)\{[^\n]+\}/)?.[0];
  assert.ok(source, 'selectPlatformAsset must be present');
  const state = {
    platformItems: [{
      reference_id: '31',
      summary: '平台口播',
      filename: 'talking.mp4',
      preview_url: '/media/talking.mp4',
      thumbnail_url: '/media/cover.jpg',
      ratio: '9:16',
    }],
  };
  const selected = [];
  const messages = {formMessage: {textContent: '旧消息'}};
  const selectPlatformAsset = Function(
    'state', 'api', 'setMainSubject', '$',
    `${source}; return selectPlatformAsset;`,
  )(
    state,
    async () => { throw new Error('selection must not call an API'); },
    (...args) => selected.push(args),
    (id) => messages[id],
  );

  await selectPlatformAsset('31');

  assert.equal(selected.length, 1);
  assert.deepEqual(selected[0][0].asset, null);
  assert.equal(selected[0][0].platform_id, '31');
  assert.equal(selected[0][0].ratio, '9:16');
  assert.equal(selected[0][1], '/media/talking.mp4');
  assert.equal(selected[0][3], '/media/cover.jpg');
  assert.equal(messages.formMessage.textContent, '');
});
```

- [ ] **Step 2: 写封面先显示、点击后才创建播放器的失败测试**

同文件增加一个真实状态/DOM 双桩测试；第一次 `renderSubjectPreview()` 断言包含封面和播放按钮但不包含 `<video>` 与视频 URL，触发按钮后再断言包含 `controls`、`playsinline`、`autoplay`、`preload="metadata"` 和当前视频 URL：

```js
test('platform preview loads its video only after the explicit play action', () => {
  const page = fs.readFileSync(pagePath, 'utf8');
  const renderSource = page.match(/function renderSubjectPreview\(\)\{[^\n]+\}/)?.[0];
  const activateSource = page.match(/function activateSubjectPreview\(\)\{[^\n]+\}/)?.[0];
  assert.ok(renderSource && activateSource, 'lazy preview functions must be present');
  const playButton = {};
  const videoElement = {};
  const previewBox = {
    innerHTML: '',
    querySelector: (selector) => selector === 'video' && previewBox.innerHTML.includes('<video')
      ? videoElement
      : null,
  };
  const state = {
    main: {name: '平台口播', kind: 'video', input_mode: 'platform_video'},
    mainPreviewUrl: '/media/talking.mp4',
    mainPosterUrl: '/media/cover.jpg',
    mainPreviewActivated: false,
    mainPreviewError: '',
  };
  const $ = (id) => id === 'subjectPreview' ? previewBox : playButton;
  const renderSubjectPreview = Function(
    'state', '$', 'escapeHtml',
    `${activateSource}; ${renderSource}; return renderSubjectPreview;`,
  )(state, $, (value) => String(value ?? ''));

  renderSubjectPreview();
  assert.match(previewBox.innerHTML, /\/media\/cover\.jpg/);
  assert.match(previewBox.innerHTML, /点击播放后加载视频/);
  assert.doesNotMatch(previewBox.innerHTML, /<video\b|\/media\/talking\.mp4/);

  playButton.onclick();
  assert.match(previewBox.innerHTML, /<video\b/);
  assert.match(previewBox.innerHTML, /controls/);
  assert.match(previewBox.innerHTML, /playsinline/);
  assert.match(previewBox.innerHTML, /autoplay/);
  assert.match(previewBox.innerHTML, /preload="metadata"/);
  assert.match(previewBox.innerHTML, /src="\/media\/talking\.mp4"/);

  videoElement.onerror();
  assert.doesNotMatch(previewBox.innerHTML, /<video\b/);
  assert.match(previewBox.innerHTML, /视频加载失败，请重试/);
});
```

- [ ] **Step 3: 写切换主体清除旧播放器、缺失封面降级以及上传视频仍立即预览的失败测试**

分别验证：

```js
assert.equal(state.mainPreviewActivated, false);
assert.equal(state.mainPreviewError, '');
assert.equal(state.mainPosterUrl, '/media/new-cover.jpg');
```

把 `mainPosterUrl` 置空时断言仍显示统一占位和播放按钮；把主体改为 `input_mode:'external_video'` 时断言直接渲染 `<video src="blob:uploaded">`，保证本次改动不改变上传主体行为。

- [ ] **Step 4: 运行定向测试并确认 RED**

Run:

```powershell
node --test tests/test_ai_edit_v2_ui.js
```

Expected: 新增测试因 `activateSubjectPreview`、封面状态和未导入选择尚不存在而失败；既有测试继续通过。

- [ ] **Step 5: 实现最小懒加载状态与交互**

在 `site/workbench/ai-edit-v2.html`：

1. 给 `state` 增加 `mainPosterUrl:null`、`mainPreviewActivated:false`、`mainPreviewError:''`。
2. 给预览区增加封面、播放按钮、错误提示样式，保持 9:16 内容在现有右侧面板内缩放。
3. 新增：

```js
function activateSubjectPreview(){
  if(!state.main||state.main.kind!=='video'||!state.mainPreviewUrl)return;
  state.mainPreviewActivated=true;
  state.mainPreviewError='';
  renderSubjectPreview();
}
```

4. `renderSubjectPreview()` 对 `input_mode==='platform_video' && !mainPreviewActivated` 只输出封面/占位、播放按钮和“点击播放后加载视频”；激活后才输出：

```html
<video controls playsinline autoplay preload="metadata" src="..."></video>
```

播放器 `error` 事件把状态恢复到封面，显示“视频加载失败，请重试”，并重新绑定播放按钮。上传视频和上传音频沿用立即预览。
5. `setMainSubject(subject, previewUrl, ownsPreview, posterUrl)` 清除旧对象 URL，设置新封面，并把 `mainPreviewActivated` 与 `mainPreviewError` 复位；优先使用 `subject.ratio` 更新比例。
6. `selectPlatformAsset(id)` 只调用 `setMainSubject()`，写入 `{asset:null, platform_id, ratio}`，不设 `busy`、不调用 `/import`。

- [ ] **Step 6: 运行定向测试并确认 GREEN**

Run:

```powershell
node --test tests/test_ai_edit_v2_ui.js
```

Expected: 全部 UI 测试通过。

- [ ] **Step 7: 提交 Task 1**

```powershell
git add tests/test_ai_edit_v2_ui.js site/workbench/ai-edit-v2.html
git commit -m "feat(ai-edit-v2): lazy load platform previews"
```

---

### Task 2: 报价前延迟导入与异步防串选

**Files:**
- Modify: `tests/test_ai_edit_v2_ui.js`
- Modify: `site/workbench/ai-edit-v2.html:78,96-97`

**Interfaces:**
- Consumes: `state.main.platform_id` 和现有平台导入响应 `{material:{id,size_bytes,duration_ms,width,height}}`。
- Produces: `ensureMainAsset(): Promise<object>`；`requestQuote()` 在调用 `buildDraft()` 前等待该函数完成。

- [ ] **Step 1: 写未导入平台主体仍可点击报价的失败测试**

提取并执行 `renderWorkspacePanel()`，传入 `{main:{platform_id:'31',asset:null}, quote:null, busy:false}`，断言主按钮文本为“获取价格区间”、`disabled === false`、`onclick === requestQuote`。

- [ ] **Step 2: 写报价调用顺序与素材回填的失败测试**

提取 `ensureMainAsset()` 和 `requestQuote()`，用可观察 API 假实现记录调用顺序：

```js
assert.deepEqual(paths, [
  '/api/v2/edit/platform-assets/31/import',
  '/api/v2/edit/quote',
]);
assert.deepEqual(state.main.asset, {
  asset_id: '901',
  kind: 'video',
  size_bytes: 2048,
  duration_ms: 12000,
});
assert.equal(state.quote.id, 'quote-1');
```

`buildDraft()` 在执行时断言 `state.main.asset` 已存在，从行为上证明“先导入、后报价”。

- [ ] **Step 3: 写旧导入结果不得覆盖新选择的失败测试**

让 `/import` 返回一个可控 Promise；调用 `requestQuote()` 后立即把 `state.main` 替换成 `platform_id:'32'`，再完成旧 Promise。断言：

```js
assert.equal(state.main.platform_id, '32');
assert.equal(state.main.asset, null);
assert.equal(paths.includes('/api/v2/edit/quote'), false);
assert.equal(formMessage.textContent, '主体已切换，请重新获取价格');
```

- [ ] **Step 4: 运行定向测试并确认 RED**

Run:

```powershell
node --test tests/test_ai_edit_v2_ui.js
```

Expected: 新增测试因未导入平台主体按钮仍被禁用、`ensureMainAsset` 不存在而失败。

- [ ] **Step 5: 实现报价前延迟导入**

新增：

```js
async function ensureMainAsset(){
  if(!state.main)throw new Error('请先选择主体视频或音频');
  if(state.main.asset)return state.main.asset;
  var platformId=state.main.platform_id;
  if(!platformId)throw new Error('请先选择主体视频或音频');
  $('formMessage').textContent='正在准备平台口播视频…';
  var imported=await api(
    '/api/v2/edit/platform-assets/'+encodeURIComponent(platformId)+'/import',
    {method:'POST',body:'{}'}
  );
  if(!state.main||String(state.main.platform_id)!==String(platformId)){
    throw new Error('主体已切换，请重新获取价格');
  }
  var material=imported.material;
  state.main.asset={
    asset_id:String(material.id),
    kind:'video',
    size_bytes:Number(material.size_bytes),
    duration_ms:Number(material.duration_ms)
  };
  return state.main.asset;
}
```

把 `renderWorkspacePanel()` 的主体有效条件改为“已有 `asset` 或已有 `platform_id`”。在 `requestQuote()` 中捕获当前 `state.main` 对象，先 `await ensureMainAsset()`，再构建 draft 和请求报价；报价返回后再次确认当前主体对象未变化，变化时显示“主体已切换，请重新获取价格”，不保存旧报价。

- [ ] **Step 6: 运行定向测试并确认 GREEN**

Run:

```powershell
node --test tests/test_ai_edit_v2_ui.js
```

Expected: 全部 UI 测试通过，且新测试覆盖按钮、调用顺序、回填和防串选。

- [ ] **Step 7: 提交 Task 2**

```powershell
git add tests/test_ai_edit_v2_ui.js site/workbench/ai-edit-v2.html
git commit -m "fix(ai-edit-v2): import platform subject before quote"
```

---

### Task 3: 全量验证、独立审查与 PR

**Files:**
- Verify: `site/workbench/ai-edit-v2.html`
- Verify: `tests/test_ai_edit_v2_ui.js`
- Verify: `tests/test_ai_edit_dual_entry.js`
- Verify: `tests/test_ai_edit_v2_api.py`

**Interfaces:**
- Consumes: Task 1 和 Task 2 的两个提交。
- Produces: 可审查的 GitHub PR；不在本任务中创建剪辑任务或部署生产。

- [ ] **Step 1: 运行前端定向回归**

```powershell
node --test tests/test_ai_edit_v2_ui.js tests/test_ai_edit_dual_entry.js
```

Expected: 0 failures。

- [ ] **Step 2: 运行 V2 API 回归**

```powershell
python -m unittest tests.test_ai_edit_v2_api -q
```

Expected: 0 failures。

- [ ] **Step 3: 运行语法和差异检查**

```powershell
python -m py_compile server/content_domains/ai_edit_v2_platform_assets.py
git diff --check origin/main...HEAD
git status --short --branch
```

Expected: Python 编译成功、无空白错误、只有计划内提交。

- [ ] **Step 4: 请求独立代码审查**

审查者只读检查：

- 选择平台卡片是否完全不调用导入 API。
- 封面状态是否不存在视频 `src`。
- 上传主体是否保持原行为。
- 报价是否先导入且只有当前选择能回填。
- 异常时是否保留选择并恢复按钮。
- 是否无后端、数据库、密钥和生产部署改动。

发现问题时先新增或强化失败测试，再修复并重复 Task 3 的验证。

- [ ] **Step 5: 推送分支并创建 PR**

```powershell
git push -u origin codex/ai-edit-v2-lazy-preview
```

PR 标题：

```text
feat(ai-edit-v2): lazy load platform video previews
```

PR 描述必须列出：选择零导入、点击后加载、报价前导入、防串选、测试结果、仅测试环境范围。

- [ ] **Step 6: 等待并核验 PR CI**

检查 GitHub “代码与安全门禁”通过，确认 PR head 与本地 `HEAD` 一致。CI 未绿不得合并；本计划不授权生产部署。
