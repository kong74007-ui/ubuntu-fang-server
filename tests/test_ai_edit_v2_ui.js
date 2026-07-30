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

  const withoutCover = platformCard({
    reference_id: '32',
    filename: 'no-cover.mp4',
    summary: '无封面口播',
    preview_url: '/api/gen/file/no-cover.mp4',
    thumbnail_url: '',
    created_at: 1,
  });
  assert.match(withoutCover, /暂无封面/);
  assert.doesNotMatch(withoutCover, /点击加载视频/);
});

test('updating the selected platform card preserves the gallery DOM and horizontal position', () => {
  const page = fs.readFileSync(pagePath, 'utf8');
  const source = page.match(/function refreshPlatformSelection\(\)\{[^\n]+\}/)?.[0];
  assert.ok(source, 'refreshPlatformSelection must be present');
  const updates = [];
  const cards = ['31', '32'].map((platformId) => ({
    dataset: {platformId},
    classList: {
      toggle: (name, on) => updates.push({platformId, kind: 'class', name, on}),
    },
    setAttribute: (name, value) => updates.push({platformId, kind: 'attribute', name, value}),
  }));
  const gallery = {
    innerHTML: '<button>existing cards</button>',
    scrollLeft: 240,
    querySelectorAll: () => cards,
  };
  const state = {platformSelectedId: '32'};
  const refreshPlatformSelection = Function(
    'state', '$', `${source}; return refreshPlatformSelection;`,
  )(state, () => gallery);

  refreshPlatformSelection();

  assert.equal(gallery.innerHTML, '<button>existing cards</button>');
  assert.equal(gallery.scrollLeft, 240);
  assert.deepEqual(updates, [
    {platformId: '31', kind: 'class', name: 'on', on: false},
    {platformId: '31', kind: 'attribute', name: 'aria-pressed', value: 'false'},
    {platformId: '32', kind: 'class', name: 'on', on: true},
    {platformId: '32', kind: 'attribute', name: 'aria-pressed', value: 'true'},
  ]);
});

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
    'state', 'api', 'setMainSubject', '$', 'renderWorkspacePanel',
    `${source}; return selectPlatformAsset;`,
  )(
    state,
    async () => { throw new Error('selection must not call an API'); },
    (...args) => selected.push(args),
    (id) => messages[id],
    () => {},
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

test('platform preview loads its video only after the explicit play action', () => {
  const page = fs.readFileSync(pagePath, 'utf8');
  const renderSource = page.match(/function renderSubjectPreview\(\)\{[^\n]+\}/)?.[0];
  const activateSource = page.match(/function activateSubjectPreview\(\)\{[^\n]+\}/)?.[0];
  assert.ok(renderSource, 'renderSubjectPreview must be present');
  assert.ok(activateSource, 'activateSubjectPreview must be present');
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

test('an active platform preview is not recreated and an old player error cannot affect a new subject', () => {
  const page = fs.readFileSync(pagePath, 'utf8');
  const renderSource = page.match(/function renderSubjectPreview\(\)\{[^\n]+\}/)?.[0];
  const activateSource = page.match(/function activateSubjectPreview\(\)\{[^\n]+\}/)?.[0];
  assert.ok(renderSource && activateSource, 'lazy preview functions must be present');
  const playButton = {};
  const videoElement = {};
  let assignments = 0;
  let html = '';
  const previewBox = {
    get innerHTML() { return html; },
    set innerHTML(value) { assignments += 1; html = value; },
    querySelector: (selector) => html.includes('<video')
      && (selector === 'video' || selector.startsWith('video[data-preview-revision='))
      ? videoElement
      : null,
  };
  const state = {
    main: {name: '平台口播 A', kind: 'video', input_mode: 'platform_video', platform_id: '31'},
    mainPreviewUrl: '/media/a.mp4',
    mainPosterUrl: '/media/a.jpg',
    mainPreviewActivated: true,
    mainPreviewError: '',
    mainPreviewRevision: 1,
  };
  const $ = (id) => id === 'subjectPreview' ? previewBox : playButton;
  const renderSubjectPreview = Function(
    'state', '$', 'escapeHtml',
    `${activateSource}; ${renderSource}; return renderSubjectPreview;`,
  )(state, $, (value) => String(value ?? ''));

  renderSubjectPreview();
  const oldError = videoElement.onerror;
  renderSubjectPreview();

  assert.equal(assignments, 1, 're-rendering the panel must reuse the active player');
  assert.equal(typeof oldError, 'function');

  state.main = {name: '平台口播 B', kind: 'video', input_mode: 'platform_video', platform_id: '32'};
  state.mainPreviewUrl = '/media/b.mp4';
  state.mainPreviewRevision = 2;
  state.mainPreviewActivated = false;
  state.mainPreviewError = '';
  oldError();

  assert.equal(state.mainPreviewActivated, false);
  assert.equal(state.mainPreviewError, '');
});

test('switching subjects resets lazy preview state and preserves uploaded-video preview', () => {
  const page = fs.readFileSync(pagePath, 'utf8');
  const setSource = page.match(/function setMainSubject\(subject,previewUrl,ownsPreview(?:,posterUrl)?\)\{[^\n]+\}/)?.[0];
  const stopSource = page.match(/function stopSubjectPreview\(\)\{[^\n]+\}/)?.[0];
  const renderSource = page.match(/function renderSubjectPreview\(\)\{[^\n]+\}/)?.[0];
  const activateSource = page.match(/function activateSubjectPreview\(\)\{[^\n]+\}/)?.[0];
  assert.ok(setSource, 'setMainSubject must be present');
  assert.ok(stopSource, 'stopSubjectPreview must be present');
  assert.ok(renderSource && activateSource, 'preview functions must be present');
  const state = {
    main: {name: '旧主体', kind: 'video', input_mode: 'platform_video'},
    mainPreviewUrl: 'blob:old',
    mainPreviewOwned: true,
    mainPosterUrl: '/media/old-cover.jpg',
    mainPreviewActivated: true,
    mainPreviewError: '旧错误',
    platformSelectedId: '31',
  };
  const revoked = [];
  const ratios = [];
  let selectionRefreshes = 0;
  let galleryRebuilds = 0;
  const stopped = {pause: 0, removed: [], load: 0};
  const currentVideo = {
    pause: () => { stopped.pause += 1; },
    removeAttribute: (name) => stopped.removed.push(name),
    load: () => { stopped.load += 1; },
    onerror: () => {},
  };
  const previewBox = {querySelector: () => currentVideo};
  const setMainSubject = Function(
    'state', 'URL', 'setAspectRatio', 'renderPlatformAssets', 'refreshPlatformSelection',
    'invalidateQuote', '$',
    `${stopSource}; ${setSource}; return setMainSubject;`,
  )(
    state,
    {revokeObjectURL: (url) => revoked.push(url)},
    (ratio) => ratios.push(ratio),
    () => { galleryRebuilds += 1; },
    () => { selectionRefreshes += 1; },
    () => {},
    () => previewBox,
  );

  setMainSubject(
    {name: '新主体', kind: 'video', input_mode: 'platform_video', platform_id: '32', ratio: '9:16', asset: null},
    '/media/new.mp4',
    false,
    '/media/new-cover.jpg',
  );

  assert.deepEqual(revoked, ['blob:old']);
  assert.equal(stopped.pause, 1);
  assert.deepEqual(stopped.removed, ['src']);
  assert.equal(stopped.load, 1);
  assert.equal(currentVideo.onerror, null);
  assert.equal(state.mainPreviewActivated, false);
  assert.equal(state.mainPreviewError, '');
  assert.equal(state.mainPosterUrl, '/media/new-cover.jpg');
  assert.deepEqual(ratios, ['9:16']);
  assert.equal(selectionRefreshes, 1);
  assert.equal(galleryRebuilds, 0);

  ratios.length = 0;
  setMainSubject(
    {name: '未知比例', kind: 'video', input_mode: 'platform_video', platform_id: '33', ratio: null, asset: null},
    '/media/unknown.mp4',
    false,
    '/media/unknown-cover.jpg',
  );
  assert.deepEqual(ratios, ['16:9']);

  const playButton = {};
  const renderedPreviewBox = {
    innerHTML: '',
    querySelector: () => null,
  };
  const $ = (id) => id === 'subjectPreview' ? renderedPreviewBox : playButton;
  const renderSubjectPreview = Function(
    'state', '$', 'escapeHtml',
    `${activateSource}; ${renderSource}; return renderSubjectPreview;`,
  )(state, $, (value) => String(value ?? ''));

  state.mainPosterUrl = '';
  renderSubjectPreview();
  assert.match(renderedPreviewBox.innerHTML, /暂无封面/);
  assert.doesNotMatch(renderedPreviewBox.innerHTML, /<video\b/);

  state.main = {name: '本地上传', kind: 'video', input_mode: 'external_video'};
  state.mainPreviewUrl = 'blob:uploaded';
  renderSubjectPreview();
  assert.match(renderedPreviewBox.innerHTML, /<video\b/);
  assert.match(renderedPreviewBox.innerHTML, /src="blob:uploaded"/);
});

test('an unresolved platform subject can request a quote', () => {
  const page = fs.readFileSync(pagePath, 'utf8');
  const source = page.match(/function renderWorkspacePanel\(\)\{[^\n]+\}/)?.[0];
  assert.ok(source, 'renderWorkspacePanel must be present');
  const state = {
    main: {name: '平台口播', platform_id: '31', asset: null},
    candidates: [],
    jobId: null,
    busy: false,
    quote: null,
  };
  const elements = {
    subjectSummary: {},
    editModeSummary: {},
    materialCount: {},
    ratioSummary: {},
    aspectRatio: {value: '9:16'},
    primaryAction: {},
  };
  const requestQuote = () => {};
  const renderWorkspacePanel = Function(
    'state', '$', 'renderSubjectPreview', 'modeLabel', 'requestQuote', 'confirmJob',
    `${source}; return renderWorkspacePanel;`,
  )(
    state,
    (id) => elements[id],
    () => {},
    () => 'AI智能剪辑',
    requestQuote,
    () => {},
  );

  renderWorkspacePanel();

  assert.equal(elements.primaryAction.textContent, '获取价格区间');
  assert.equal(elements.primaryAction.disabled, false);
  assert.equal(elements.primaryAction.onclick, requestQuote);
});

test('requesting a quote imports the selected platform subject before building the draft', async () => {
  const page = fs.readFileSync(pagePath, 'utf8');
  const ensureSource = page.match(/async function ensureMainAsset\(\)\{[^\n]+\}/)?.[0];
  const quoteSource = page.match(/async function requestQuote\(\)\{[^\n]+\}/)?.[0];
  assert.ok(ensureSource, 'ensureMainAsset must be present');
  assert.ok(quoteSource, 'requestQuote must be present');
  const state = {
    main: {name: '平台口播', kind: 'video', input_mode: 'platform_video', platform_id: '31', asset: null},
    busy: false,
    quote: null,
  };
  const elements = {
    formMessage: {textContent: ''},
    quoteMin: {textContent: ''},
    quoteMax: {textContent: ''},
  };
  const paths = [];
  const api = async (path) => {
    paths.push(path);
    if (path.endsWith('/import')) {
      return {material: {id: 901, size_bytes: 2048, duration_ms: 12000, width: 1080, height: 1920}};
    }
    assert.ok(state.main.asset, 'draft quote must happen after main asset import');
    return {quote: {id: 'quote-1', minimum_points: 48, maximum_points: 64, held_points: 64}};
  };
  const buildDraft = () => {
    assert.ok(state.main.asset, 'buildDraft must happen after main asset import');
    return {main_input: state.main.asset};
  };
  const requestQuote = Function(
    'state', '$', 'api', 'buildDraft', 'renderWorkspacePanel', 'beginBusy', 'endBusy',
    `${ensureSource}; ${quoteSource}; return requestQuote;`,
  )(
    state,
    (id) => elements[id],
    api,
    buildDraft,
    () => {},
    () => { state.busy = true; },
    () => { state.busy = false; },
  );

  await requestQuote();

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
  assert.equal(elements.quoteMin.textContent, 48);
  assert.equal(elements.quoteMax.textContent, 64);
});

test('a completed import cannot overwrite a newly selected platform subject', async () => {
  const page = fs.readFileSync(pagePath, 'utf8');
  const ensureSource = page.match(/async function ensureMainAsset\(\)\{[^\n]+\}/)?.[0];
  const quoteSource = page.match(/async function requestQuote\(\)\{[^\n]+\}/)?.[0];
  assert.ok(ensureSource && quoteSource, 'lazy import functions must be present');
  let resolveImport;
  const importResult = new Promise((resolve) => { resolveImport = resolve; });
  const paths = [];
  const oldMain = {name: '旧主体', kind: 'video', input_mode: 'platform_video', platform_id: '31', asset: null};
  const state = {main: oldMain, busy: false, quote: null};
  const elements = {
    formMessage: {textContent: ''},
    quoteMin: {textContent: ''},
    quoteMax: {textContent: ''},
  };
  const api = async (path) => {
    paths.push(path);
    if (path.endsWith('/import')) return importResult;
    throw new Error('stale selection must not request a quote');
  };
  const requestQuote = Function(
    'state', '$', 'api', 'buildDraft', 'renderWorkspacePanel', 'beginBusy', 'endBusy',
    `${ensureSource}; ${quoteSource}; return requestQuote;`,
  )(
    state,
    (id) => elements[id],
    api,
    () => { throw new Error('stale selection must not build a draft'); },
    () => {},
    () => { state.busy = true; },
    () => { state.busy = false; },
  );

  const pendingQuote = requestQuote();
  await Promise.resolve();
  state.main = {name: '新主体', kind: 'video', input_mode: 'platform_video', platform_id: '32', asset: null};
  resolveImport({material: {id: 901, size_bytes: 2048, duration_ms: 12000}});
  await pendingQuote;

  assert.equal(state.main.platform_id, '32');
  assert.equal(state.main.asset, null);
  assert.deepEqual(paths, ['/api/v2/edit/platform-assets/31/import']);
  assert.equal(elements.formMessage.textContent, '主体已切换，请重新获取价格');
});

test('a quote response cannot be applied after the edit configuration changes', async () => {
  const page = fs.readFileSync(pagePath, 'utf8');
  const ensureSource = page.match(/async function ensureMainAsset\(\)\{[^\n]+\}/)?.[0];
  const quoteSource = page.match(/async function requestQuote\(\)\{[^\n]+\}/)?.[0];
  assert.ok(ensureSource && quoteSource, 'lazy import functions must be present');
  let resolveQuote;
  const quoteResult = new Promise((resolve) => { resolveQuote = resolve; });
  const paths = [];
  const state = {
    main: {
      name: '平台口播',
      kind: 'video',
      input_mode: 'platform_video',
      platform_id: '31',
      asset: null,
    },
    busy: false,
    quote: null,
  };
  const elements = {
    formMessage: {textContent: ''},
    quoteMin: {textContent: ''},
    quoteMax: {textContent: ''},
  };
  let ratio = '9:16';
  const buildDraft = () => ({main_input: state.main.asset, aspect_ratio: ratio});
  const api = async (path) => {
    paths.push(path);
    if (path.endsWith('/import')) {
      return {material: {id: 901, size_bytes: 2048, duration_ms: 12000}};
    }
    return quoteResult;
  };
  const requestQuote = Function(
    'state', '$', 'api', 'buildDraft', 'renderWorkspacePanel', 'beginBusy', 'endBusy',
    `${ensureSource}; ${quoteSource}; return requestQuote;`,
  )(
    state,
    (id) => elements[id],
    api,
    buildDraft,
    () => {},
    () => { state.busy = true; },
    () => { state.busy = false; },
  );

  const pendingQuote = requestQuote();
  for (let index = 0; index < 4 && paths.length < 2; index += 1) await Promise.resolve();
  assert.deepEqual(paths, [
    '/api/v2/edit/platform-assets/31/import',
    '/api/v2/edit/quote',
  ]);
  ratio = '16:9';
  resolveQuote({quote: {id: 'stale-quote', minimum_points: 48, maximum_points: 64, held_points: 64}});
  await pendingQuote;

  assert.equal(state.quote, null);
  assert.equal(elements.formMessage.textContent, '剪辑配置已变更，请重新获取价格');
});

test('a quote finishing while a new subject uploads cannot unlock or restore the old quote', async () => {
  const page = fs.readFileSync(pagePath, 'utf8');
  const quoteSource = page.match(/async function requestQuote\(\)\{[^\n]+\}/)?.[0];
  const uploadSource = page.match(/async function uploadSubject\(file,expectedKind\)\{[^\n]+\}/)?.[0];
  assert.ok(quoteSource && uploadSource, 'quote and subject upload functions must be present');
  let resolveQuote;
  let resolveUpload;
  const quoteResult = new Promise((resolve) => { resolveQuote = resolve; });
  const uploadResult = new Promise((resolve) => { resolveUpload = resolve; });
  const originalMain = {
    name: '主体 A',
    kind: 'video',
    input_mode: 'platform_video',
    platform_id: '31',
    asset: {asset_id: '31', kind: 'video', size_bytes: 100, duration_ms: 1000},
  };
  const state = {
    main: originalMain,
    busy: false,
    busyCount: 0,
    subjectIntentRevision: 0,
    quote: null,
    jobRequestKey: null,
  };
  const elements = {
    formMessage: {textContent: ''},
    quoteMin: {textContent: ''},
    quoteMax: {textContent: ''},
  };
  const beginBusy = () => {
    state.busyCount += 1;
    state.busy = true;
  };
  const endBusy = () => {
    state.busyCount = Math.max(0, state.busyCount - 1);
    state.busy = state.busyCount > 0;
  };
  const requestQuote = Function(
    'state', '$', 'api', 'buildDraft', 'renderWorkspacePanel',
    'ensureMainAsset', 'beginBusy', 'endBusy',
    `${quoteSource}; return requestQuote;`,
  )(
    state,
    (id) => elements[id],
    async () => quoteResult,
    () => ({main_input: state.main.asset, aspect_ratio: '9:16'}),
    () => {},
    async () => state.main.asset,
    beginBusy,
    endBusy,
  );
  const selected = [];
  const revoked = [];
  const uploadSubject = Function(
    'state', '$', 'fileKind', 'URL', 'renderWorkspacePanel', 'uploadToPrivateStore',
    'setMainSubject', 'invalidateQuote', 'beginBusy', 'endBusy',
    `${uploadSource}; return uploadSubject;`,
  )(
    state,
    (id) => elements[id],
    () => 'video',
    {
      createObjectURL: () => 'blob:subject-b',
      revokeObjectURL: (url) => revoked.push(url),
    },
    () => {},
    async (item) => {
      await uploadResult;
      item.asset = {asset_id: 'B', kind: 'video', width: 1080, height: 1920};
    },
    (subject) => {
      selected.push(subject);
      state.main = subject;
    },
    () => {
      state.quote = null;
      state.jobRequestKey = null;
    },
    beginBusy,
    endBusy,
  );

  const pendingQuote = requestQuote();
  await Promise.resolve();
  const pendingUpload = uploadSubject({name: 'subject-b.mp4', type: 'video/mp4'}, 'video');
  await Promise.resolve();
  resolveQuote({quote: {id: 'quote-a', minimum_points: 48, maximum_points: 64, held_points: 64}});
  await pendingQuote;
  const busyAfterQuote = state.busy;
  const quoteAfterQuote = state.quote;
  resolveUpload();
  await pendingUpload;

  assert.equal(busyAfterQuote, true);
  assert.equal(quoteAfterQuote, null);
  assert.equal(state.busy, false);
  assert.equal(selected.length, 1);
  assert.equal(selected[0].name, 'subject-b.mp4');
  assert.deepEqual(revoked, []);
});

test('a late subject upload cannot overwrite a platform subject selected afterward', async () => {
  const page = fs.readFileSync(pagePath, 'utf8');
  const uploadSource = page.match(/async function uploadSubject\(file,expectedKind\)\{[^\n]+\}/)?.[0];
  assert.ok(uploadSource, 'uploadSubject must be present');
  let resolveUpload;
  const uploadResult = new Promise((resolve) => { resolveUpload = resolve; });
  const platformMain = {
    name: '后来选择的主体 C',
    kind: 'video',
    input_mode: 'platform_video',
    platform_id: '33',
    asset: null,
  };
  const state = {
    main: {name: '主体 A'},
    busy: false,
    busyCount: 0,
    subjectIntentRevision: 0,
    quote: null,
  };
  const elements = {formMessage: {textContent: ''}};
  const selected = [];
  const revoked = [];
  const beginBusy = () => {
    state.busyCount += 1;
    state.busy = true;
  };
  const endBusy = () => {
    state.busyCount = Math.max(0, state.busyCount - 1);
    state.busy = state.busyCount > 0;
  };
  const uploadSubject = Function(
    'state', '$', 'fileKind', 'URL', 'renderWorkspacePanel', 'uploadToPrivateStore',
    'setMainSubject', 'invalidateQuote', 'beginBusy', 'endBusy',
    `${uploadSource}; return uploadSubject;`,
  )(
    state,
    (id) => elements[id],
    () => 'video',
    {
      createObjectURL: () => 'blob:subject-b',
      revokeObjectURL: (url) => revoked.push(url),
    },
    () => {},
    async (item) => {
      await uploadResult;
      item.asset = {asset_id: 'B', kind: 'video', width: 1080, height: 1920};
    },
    (subject) => {
      selected.push(subject);
      state.main = subject;
    },
    () => { state.quote = null; },
    beginBusy,
    endBusy,
  );

  const pendingUpload = uploadSubject({name: 'subject-b.mp4', type: 'video/mp4'}, 'video');
  await Promise.resolve();
  state.subjectIntentRevision += 1;
  state.main = platformMain;
  resolveUpload();
  await pendingUpload;

  assert.equal(state.main, platformMain);
  assert.equal(selected.length, 0);
  assert.deepEqual(revoked, ['blob:subject-b']);
  assert.equal(state.busy, false);
});

test('confirming a quote never posts a job when the current draft no longer matches it', async () => {
  const page = fs.readFileSync(pagePath, 'utf8');
  const confirmSource = page.match(/async function confirmJob\(\)\{[^\n]+\}/)?.[0];
  assert.ok(confirmSource, 'confirmJob must be present');
  const quotedDraft = {main_input: {asset_id: '31'}, aspect_ratio: '9:16'};
  const currentDraft = {main_input: {asset_id: '31'}, aspect_ratio: '16:9'};
  const state = {
    quote: {id: 'quote-1', held_points: 64},
    quotedDraftFingerprint: JSON.stringify(quotedDraft),
    busy: false,
    busyCount: 0,
    jobRequestKey: null,
    jobId: null,
    pollTimer: null,
  };
  const elements = {
    formMessage: {textContent: ''},
    taskDetails: {hidden: true},
  };
  let apiCalls = 0;
  const beginBusy = () => {
    state.busyCount += 1;
    state.busy = true;
  };
  const endBusy = () => {
    state.busyCount = Math.max(0, state.busyCount - 1);
    state.busy = state.busyCount > 0;
  };
  const confirmJob = Function(
    'state', '$', 'api', 'buildDraft', 'renderWorkspacePanel', 'invalidateQuote',
    'beginBusy', 'endBusy', 'crypto', 'sessionStorage', 'trackJob',
    'setInterval', 'clearInterval', 'pollJob',
    `${confirmSource}; return confirmJob;`,
  )(
    state,
    (id) => elements[id],
    async () => {
      apiCalls += 1;
      return {job_id: 'job-1', status: 'queued', held_points: 64};
    },
    () => currentDraft,
    () => {},
    () => {
      state.quote = null;
      state.quotedDraftFingerprint = null;
    },
    beginBusy,
    endBusy,
    {randomUUID: () => 'request-1'},
    {setItem: () => {}},
    () => {},
    () => 1,
    () => {},
    () => {},
  );

  await confirmJob();

  assert.equal(apiCalls, 0);
  assert.equal(state.jobId, null);
  assert.equal(state.quote, null);
  assert.equal(elements.formMessage.textContent, '剪辑配置已变更，请重新获取价格');
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
