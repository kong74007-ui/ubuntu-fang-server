const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const root = path.resolve(__dirname, '..');
const pagePath = path.join(root, 'site/workbench/ai-edit-v2.html');
const videoPath = path.join(root, 'site/workbench/video.html');
const shellPath = path.join(root, 'site/workbench/cloud-shell.js');
const tasksPath = path.join(root, 'site/workbench/tasks.js');

test('creation workspace presents the confirmed five-step layout', () => {
  assert.equal(fs.existsSync(pagePath), true, 'site/workbench/ai-edit-v2.html must exist');
  const page = fs.readFileSync(pagePath, 'utf8');
  assert.match(page, /data-active="ai_edit_v2"/);
  const headings = [...page.matchAll(/<h2>([^<]+)<\/h2>/g)].map((match) => match[1]);
  assert.deepEqual(headings.slice(0, 5), [
    '1. 选择主体视频或音频',
    '2. 选择剪辑方式',
    '3. 上传补充素材（可选）',
    '4. 选择画面比例',
    '5. 报价并开始创作',
  ]);
  for (const mode of ['natural_brief', 'platform_template', 'open_generation']) {
    assert.match(page, new RegExp(`data-creation-mode="${mode}"`));
  }
  assert.match(page, /id="platformGallery"/);
  assert.match(page, /id="platformCount"/);
  assert.match(page, /id="platformReload"/);
  assert.match(page, /id="videoSubjectInput"[^>]*accept="video\/\*"/);
  assert.match(page, /id="audioSubjectInput"[^>]*accept="audio\/\*"/);
  assert.match(page, /id="candidateInput"[^>]*multiple[^>]*accept="image\/\*,video\/\*,audio\/\*"/);
  assert.match(page, /id="candidateGrid"/);
  assert.match(page, /\.platform-card-media\{[^}]*aspect-ratio:9\/16/);
  assert.match(page, /\.candidate-add\{[^}]*aspect-ratio:1/);
  assert.match(page, /\.workspace-panel\{[^}]*position:sticky/);
  assert.match(page, /value="16:9"/);
  assert.match(page, /value="9:16"/);
  assert.match(page, /id="quoteMin"/);
  assert.match(page, /id="quoteMax"/);
  assert.match(page, /失败全额退款/);
  assert.doesNotMatch(page, /id="confirmPrecharge"/);
  assert.doesNotMatch(page, /id="targetDuration"|id="requiredInput"|id="referenceInput"|id="mainAssetSelect"/);
  assert.doesNotMatch(page, /必须使用|参考使用/);
  for (const id of ['queueTime', 'processingTime', 'repairTime', 'resultVideo', 'downloadResult']) {
    assert.match(page, new RegExp(`id="${id}"`));
  }
  for (const id of ['elapsedTime', 'estimatedTime', 'degradationList', 'qualitySummary', 'actualCharge', 'refundedDifference', 'assetResult', 'retryJobBtn']) {
    assert.match(page, new RegExp(`id="${id}"`), id);
  }
});

test('page implements subject cards candidate uploads quote creation and job polling', () => {
  const page = fs.readFileSync(pagePath, 'utf8');
  for (const name of ['buildDraft', 'requestQuote', 'confirmJob', 'pollJob', 'setMainSubject', 'selectEditMode', 'renderPlatformAssets', 'renderCandidates', 'renderWorkspacePanel', 'retryUpload', 'retryJob']) {
    assert.match(page, new RegExp(`function ${name}\\(`), name);
  }
  for (const endpoint of ['/api/v2/edit/uploads', '/api/v2/edit/quote', '/api/v2/edit/jobs']) {
    assert.ok(page.includes(endpoint), endpoint);
  }
  assert.match(page, /files\.length>10/);
  assert.match(page, /setInterval\(pollJob/);
  assert.match(page, /state\.jobRequestKey/);
  assert.match(page, /sessionStorage/);
  assert.match(page, /sessionStorage\.setItem\(retryStorageKey,key\).*await api/s);
  assert.match(page, /sessionStorage\.getItem\(retryStorageKey\)/);
  assert.match(page, /sessionStorage\.removeItem\(retryStorageKey\)/);
  assert.match(page, /billing_pending/);
  assert.match(page, /target_duration_ms:null/);
  assert.match(page, /required_materials:\[\]/);
  assert.match(page, /reference_mode:'direct_use'/);
  assert.match(page, /template_id/);
  assert.match(page, /template_version/);
  assert.match(page, /function loadPlatformAssets\(/);
  assert.match(page, /\/api\/v2\/edit\/platform-assets/);
  assert.match(page, /data-platform-id/);
  assert.doesNotMatch(page, /original_text/);
  assert.match(page, /data\.degradations/);
  assert.match(page, /data\.quality/);
  assert.match(page, /actual_charge_points/);
  assert.match(page, /refunded_difference_points/);
  assert.match(page, /data\.output\.asset_url/);
  assert.match(page, /estimated_remaining_seconds===0/);
  assert.match(page, /HQTasks\.upsert/);
});

test('platform cards render lightweight covers without loading video sources', () => {
  const page = fs.readFileSync(pagePath, 'utf8');
  const source = page.match(/function platformCard\(item\)\{[^\n]+\}/)?.[0];
  assert.ok(source, 'platformCard must be present');

  const platformCard = Function(
    'state', 'escapeHtml', 'formatDate', `${source}; return platformCard;`
  )(
    {platformSelectedId: null},
    (value) => String(value ?? ''),
    () => '2026-07-29',
  );
  const html = platformCard({
    reference_id: '31',
    filename: 'talking.mp4',
    summary: '平台口播',
    preview_url: '/api/gen/file/talking.mp4',
    thumbnail_url: '/api/gen/file/image/cover.jpg',
    created_at: 1,
  });

  assert.match(html, /<img[^>]+src="\/api\/gen\/file\/image\/cover\.jpg"/);
  assert.doesNotMatch(html, /<video\b/);
  assert.doesNotMatch(html, /src="\/api\/gen\/file\/talking\.mp4"/);
});

test('subject carousel only accepts verified digital IP assets', () => {
  const page = fs.readFileSync(pagePath, 'utf8');
  const source = page.match(/async function loadPlatformAssets\(\)\{[^\n]+\}/)?.[0] || '';

  assert.match(source, /\/api\/v2\/edit\/platform-assets/);
  assert.match(source, /asset_type==='digital_ip'/);
  assert.doesNotMatch(source, /\/api\/gen\/video\/assets/);
  assert.match(page, /账号内已完成的数字化 IP 口播视频/);
  assert.match(page, /暂无数字化 IP 口播视频/);
  assert.match(page, /id="videoSubjectInput"/);
  assert.match(page, /id="audioSubjectInput"/);
});

test('platform gallery stays in one horizontal row', () => {
  const page = fs.readFileSync(pagePath, 'utf8');
  const rule = page.match(/\.platform-gallery\{([^}]*)\}/)?.[1] || '';
  assert.match(rule, /display:flex/);
  assert.match(rule, /overflow-x:auto/);
  assert.match(rule, /flex-wrap:nowrap/);
  const cardRule = page.match(/\.platform-card\{([^}]*)\}/)?.[1] || '';
  assert.match(cardRule, /flex:0 0 142px/);
});

test('template mode uses compact image preview cards instead of a dropdown', () => {
  const page = fs.readFileSync(pagePath, 'utf8');
  assert.match(page, /id="templateGallery"/);
  assert.doesNotMatch(page, /id="templateSelect"/);
  assert.match(page, /function renderTemplates\(/);
  assert.match(page, /function selectTemplate\(/);
  assert.match(page, /\.template-card\{[^}]*flex:0 0 110px/);
  assert.match(page, /\.template-card-media\{[^}]*aspect-ratio:9\/16/);

  const source = page.match(/function templateCard\(item\)\{[^\n]+\}/)?.[0];
  assert.ok(source, 'templateCard must be present');
  const templateCard = Function(
    'state', 'escapeHtml', `${source}; return templateCard;`
  )(
    {selectedTemplate: null},
    (value) => String(value ?? ''),
  );
  const html = templateCard({
    id: 'business_diagnostic',
    name: '商业诊断',
    version: '1.0',
    preview_image_url: '/assets/ai-edit-v2/templates/business-diagnostic.svg',
  });

  assert.match(html, /<img[^>]+loading="lazy"[^>]+src="\/assets\/ai-edit-v2\/templates\/business-diagnostic\.svg"/);
  assert.doesNotMatch(html, /<video\b/);
});

test('narrow screens keep summary values beside their labels and readable', () => {
  const page = fs.readFileSync(pagePath, 'utf8');
  const narrowRules = page.match(/@media\(max-width:700px\)\{([^\n]+)\}/)?.[1] || '';

  assert.match(narrowRules, /\.summary-row\{[^}]*grid-template-columns:72px minmax\(0,1fr\)/);
  assert.match(narrowRules, /\.summary-row b\{[^}]*-webkit-line-clamp:2/);
});

test('legacy video workflow keeps its controls and links to the stable editor', () => {
  const video = fs.readFileSync(videoPath, 'utf8');
  assert.match(video, /data-function="talking"/);
  assert.match(video, /id="generateBtn"/);
  assert.match(video, /href="ai-edit-v2\.html"[^>]*data-ai-edit-v2-entry/);
});

test('shared task tracker resumes V2 editing jobs without changing legacy video routing', () => {
  const tasks = fs.readFileSync(tasksPath, 'utf8');
  assert.match(tasks, /ai_edit_v2/);
  assert.match(tasks, /ai-edit-v2\.html\?task=/);
  assert.match(tasks, /normalizing/);
  assert.match(tasks, /quality_check/);
  assert.match(tasks, /repairing/);
  assert.match(tasks, /settling/);
  assert.match(tasks, /video\.html\?task=/);
});

test('user page does not expose provider internals or an editable timeline', () => {
  const page = fs.readFileSync(pagePath, 'utf8');
  for (const forbidden of ['shotstack', 'remotion', 'qwen', 'fun-asr', 'openai', 'gpt-image']) {
    assert.doesNotMatch(page, new RegExp(forbidden, 'i'), forbidden);
  }
  assert.doesNotMatch(page, /contenteditable/i);
  assert.doesNotMatch(page, /error\.stack/);
  assert.doesNotMatch(page, /[?&](token|secret|signature)=/i);
  assert.doesNotMatch(page, /可编辑时间线|时间线编辑器/);
});

test('shared shell exposes AI edit only after the server capability allows it', () => {
  const shell = fs.readFileSync(shellPath, 'utf8');
  assert.match(shell, /\{k:'ai_edit_v2',l:'AI智能剪辑 V2',i:'edit',gated:true\}/);
  assert.match(shell, /\/api\/v2\/edit\/capabilities/);
  assert.match(shell, /accepts_submissions/);
  assert.match(shell, /NAV_PAGES=\{ai_edit_v2:'ai-edit-v2\.html'\}/);
  const page = fs.readFileSync(pagePath, 'utf8');
  assert.match(page, /loadCapability/);
  assert.match(page, /功能尚未开放/);
});

test('successful capability check clears the initial closed-state message', () => {
  const page = fs.readFileSync(pagePath, 'utf8');
  const source = page.match(/function setSubmissionEnabled\(enabled\)\{[^\n]+\}/)?.[0];
  assert.ok(source, 'setSubmissionEnabled must be present');

  const formMessage = {textContent: '功能尚未开放'};
  const controls = [{disabled: true, closest: () => null}];
  const document = {querySelectorAll: () => controls};
  const state = {acceptsSubmissions: false};
  const $ = () => formMessage;
  const setSubmissionEnabled = Function(
    'state', 'document', '$', `${source}; return setSubmissionEnabled;`
  )(state, document, $);

  setSubmissionEnabled(true);

  assert.equal(state.acceptsSubmissions, true);
  assert.equal(controls[0].disabled, false);
  assert.equal(formMessage.textContent, '');
});

test('page initialization loads capability and asset sources only once', async () => {
  const page = fs.readFileSync(pagePath, 'utf8');
  const start = page.lastIndexOf('setSubmissionEnabled(false);');
  const end = page.indexOf('\n})();', start);
  assert.notEqual(start, -1, 'page initialization must be present');
  assert.notEqual(end, -1, 'page initialization must terminate');
  const initialization = page.slice(start, end);
  const calls = {capability: 0, platformAssets: 0, templates: 0, templateRenders: 0};
  const state = {acceptsSubmissions: false};

  const run = Function(
    'state', 'setSubmissionEnabled', 'restorePendingJob', 'loadCapability',
    'loadPlatformAssets', 'loadMaterials', 'api', 'renderTemplates',
    `${initialization}; return Promise.resolve().then(() => Promise.resolve());`
  );
  await run(
    state,
    () => {},
    () => {},
    async () => { calls.capability += 1; state.acceptsSubmissions = true; },
    async () => { calls.platformAssets += 1; },
    async () => {},
    async (path) => { if (path.endsWith('/templates')) calls.templates += 1; return {items: []}; },
    () => { calls.templateRenders += 1; },
  );

  assert.deepEqual(calls, {capability: 1, platformAssets: 1, templates: 1, templateRenders: 1});
});
