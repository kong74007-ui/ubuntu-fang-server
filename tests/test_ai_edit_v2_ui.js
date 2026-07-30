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
  assert.match(page, /\$\('videoSubjectInput'\)\.onchange=function\(\)\{uploadSubject\(this\.files&&this\.files\[0\],'video'\)/);
  assert.match(page, /\$\('audioSubjectInput'\)\.onchange=function\(\)\{uploadSubject\(this\.files&&this\.files\[0\],'audio'\)/);
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
    querySelector: (selector) => {
      if (!html.includes('<video')) return null;
      if (selector === 'video') return videoElement;
      const revision = selector.match(/^video\[data-preview-revision="(\d+)"\]$/)?.[1];
      return revision && html.includes(`data-preview-revision="${revision}"`)
        ? videoElement
        : null;
    },
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
  assert.match(previewBox.innerHTML, /data-preview-revision="1"/);
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
  assert.equal(state.subjectIntentRevision, 1);
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
  assert.equal(state.subjectIntentRevision, 2);

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
  const beginSource = page.match(/function beginBusy\(\)\{[^\n]+\}/)?.[0];
  const endSource = page.match(/function endBusy\(\)\{[^\n]+\}/)?.[0];
  const quoteSource = page.match(/async function requestQuote\(\)\{[^\n]+\}/)?.[0];
  const uploadSource = page.match(/async function uploadSubject\(file,expectedKind\)\{[^\n]+\}/)?.[0];
  assert.ok(
    beginSource && endSource && quoteSource && uploadSource,
    'busy, quote and subject upload functions must be present',
  );
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
  const busyHelpers = Function(
    'state',
    `${beginSource}; ${endSource}; return {beginBusy, endBusy};`,
  )(state);
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
    busyHelpers.beginBusy,
    busyHelpers.endBusy,
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
    busyHelpers.beginBusy,
    busyHelpers.endBusy,
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

test('production busy helpers keep overlapping operations locked until both finish', () => {
  const page = fs.readFileSync(pagePath, 'utf8');
  const beginSource = page.match(/function beginBusy\(\)\{[^\n]+\}/)?.[0];
  const endSource = page.match(/function endBusy\(\)\{[^\n]+\}/)?.[0];
  assert.ok(beginSource && endSource, 'production busy helpers must be present');
  const state = {busy: false, busyCount: 0};
  const helpers = Function(
    'state',
    `${beginSource}; ${endSource}; return {beginBusy, endBusy};`,
  )(state);

  helpers.beginBusy();
  helpers.beginBusy();
  helpers.endBusy();

  assert.equal(state.busyCount, 1);
  assert.equal(state.busy, true);

  helpers.endBusy();
  assert.equal(state.busyCount, 0);
  assert.equal(state.busy, false);

  helpers.endBusy();
  assert.equal(state.busyCount, 0);
  assert.equal(state.busy, false);
});

test('a late subject upload cannot overwrite a platform subject selected afterward', async () => {
  const page = fs.readFileSync(pagePath, 'utf8');
  const stopSource = page.match(/function stopSubjectPreview\(\)\{[^\n]+\}/)?.[0];
  const setSource = page.match(/function setMainSubject\(subject,previewUrl,ownsPreview,posterUrl\)\{[^\n]+\}/)?.[0];
  const uploadSource = page.match(/async function uploadSubject\(file,expectedKind\)\{[^\n]+\}/)?.[0];
  assert.ok(stopSource && setSource && uploadSource, 'subject selection and upload functions must be present');
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
  const previewBox = {querySelector: () => null};
  const invalidateQuote = () => { state.quote = null; };
  const setMainSubject = Function(
    'state', 'URL', 'setAspectRatio', 'refreshPlatformSelection',
    'invalidateQuote', '$',
    `${stopSource}; ${setSource}; return setMainSubject;`,
  )(
    state,
    {revokeObjectURL: (url) => revoked.push(url)},
    () => {},
    () => {},
    invalidateQuote,
    () => previewBox,
  );
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
    (subject, previewUrl, ownsPreview, posterUrl) => {
      selected.push(subject);
      setMainSubject(subject, previewUrl, ownsPreview, posterUrl);
    },
    invalidateQuote,
    beginBusy,
    endBusy,
  );

  const pendingUpload = uploadSubject({name: 'subject-b.mp4', type: 'video/mp4'}, 'video');
  await Promise.resolve();
  setMainSubject(platformMain, '/media/c.mp4', false, '/media/c.jpg');
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

test('a successful quote fingerprint is accepted by confirmJob for the unchanged draft', async () => {
  const page = fs.readFileSync(pagePath, 'utf8');
  const beginSource = page.match(/function beginBusy\(\)\{[^\n]+\}/)?.[0];
  const endSource = page.match(/function endBusy\(\)\{[^\n]+\}/)?.[0];
  const ensureSource = page.match(/async function ensureMainAsset\(\)\{[^\n]+\}/)?.[0];
  const quoteSource = page.match(/async function requestQuote\(\)\{[^\n]+\}/)?.[0];
  const confirmSource = page.match(/async function confirmJob\(\)\{[^\n]+\}/)?.[0];
  const clearQuerySource = page.match(/function clearTaskQuery\(\)\{[^\n]+\}/)?.[0];
  assert.ok(
    beginSource && endSource && ensureSource && quoteSource && confirmSource && clearQuerySource,
    'quote-to-confirm production functions must be present',
  );
  const draft = {
    creation_mode: 'open_generation',
    aspect_ratio: '9:16',
    main_input: {asset_id: '31', kind: 'video', size_bytes: 100, duration_ms: 1000},
  };
  const state = {
    main: {
      name: '主体 A',
      kind: 'video',
      input_mode: 'platform_video',
      platform_id: '31',
      asset: draft.main_input,
    },
    subjectIntentRevision: 1,
    quote: null,
    quotedDraftFingerprint: null,
    busy: false,
    busyCount: 0,
    jobRequestKey: null,
    jobId: null,
    pollTimer: null,
  };
  const elements = {
    formMessage: {textContent: ''},
    quoteMin: {textContent: ''},
    quoteMax: {textContent: ''},
    taskDetails: {hidden: true},
  };
  const paths = [];
  const location = {search: '?task=failed-history', pathname: '/workbench/ai-edit-v2', hash: ''};
  const history = {
    replaceState(_state, _title, url) {
      location.search = new URL(url, 'https://example.test').search;
    },
  };
  const api = async (path, options) => {
    paths.push(path);
    if (path === '/api/v2/edit/quote') {
      assert.deepEqual(JSON.parse(options.body), {draft});
      return {quote: {id: 'quote-1', minimum_points: 48, maximum_points: 64, held_points: 64}};
    }
    assert.equal(path, '/api/v2/edit/jobs');
    assert.deepEqual(JSON.parse(options.body), {
      draft,
      quote_id: 'quote-1',
      idempotency_key: 'request-1',
    });
    return {job_id: 'job-1', status: 'queued', held_points: 64};
  };
  const workflow = Function(
    'state', '$', 'api', 'buildDraft', 'renderWorkspacePanel', 'invalidateQuote',
    'crypto', 'sessionStorage', 'trackJob', 'setInterval', 'clearInterval', 'pollJob',
    'location', 'history', 'URLSearchParams',
    `${clearQuerySource}; ${beginSource}; ${endSource}; ${ensureSource}; ${quoteSource}; ${confirmSource}; return {requestQuote, confirmJob};`,
  )(
    state,
    (id) => elements[id],
    api,
    () => ({
      creation_mode: draft.creation_mode,
      aspect_ratio: draft.aspect_ratio,
      main_input: draft.main_input,
    }),
    () => {},
    () => {
      state.quote = null;
      state.quotedDraftFingerprint = null;
    },
    {randomUUID: () => 'request-1'},
    {setItem: () => {}},
    () => {},
    () => 1,
    () => {},
    () => {},
    location,
    history,
    URLSearchParams,
  );

  await workflow.requestQuote();
  assert.equal(state.quotedDraftFingerprint, JSON.stringify(draft));
  await workflow.confirmJob();

  assert.deepEqual(paths, ['/api/v2/edit/quote', '/api/v2/edit/jobs']);
  assert.equal(state.jobId, 'job-1');
  assert.equal(state.busy, false);
  assert.equal(state.busyCount, 0);
  assert.equal(location.search, '');
});

test('a late create response cannot replace a job opened while creation was pending', async () => {
  const page = fs.readFileSync(pagePath, 'utf8');
  const confirmSource = page.match(/async function confirmJob\(\)\{[^\n]+\}/)?.[0];
  assert.ok(confirmSource, 'confirmJob must be present');

  let resolveCreate;
  const createResponse = new Promise((resolve) => { resolveCreate = resolve; });
  const draft = {main_input: {asset_id: 'subject-A'}, aspect_ratio: '9:16'};
  const state = {
    quote: {id: 'quote-A', held_points: 64},
    quotedDraftFingerprint: JSON.stringify(draft),
    busy: false,
    busyCount: 0,
    jobRequestKey: null,
    jobId: null,
    jobViewRevision: 0,
    pollTimer: null,
  };
  const elements = {
    formMessage: {textContent: ''},
    taskDetails: {hidden: true},
  };
  const stored = new Map();
  const tracked = [];
  const clearedTimers = [];
  const confirmJob = Function(
    'state', '$', 'api', 'buildDraft', 'renderWorkspacePanel', 'invalidateQuote',
    'beginBusy', 'endBusy', 'crypto', 'sessionStorage', 'trackJob', 'trackJobById',
    'setInterval', 'clearInterval', 'pollJob',
    `${confirmSource}; return confirmJob;`,
  )(
    state,
    (id) => elements[id],
    async (_path, options) => {
      assert.deepEqual(JSON.parse(options.body), {
        draft,
        quote_id: 'quote-A',
        idempotency_key: 'request-A',
      });
      return createResponse;
    },
    () => draft,
    () => {},
    () => {},
    () => { state.busyCount += 1; state.busy = true; },
    () => { state.busyCount -= 1; state.busy = state.busyCount > 0; },
    {randomUUID: () => 'request-A'},
    {
      setItem: (key, value) => stored.set(key, value),
      getItem: (key) => stored.get(key) || null,
    },
    () => {},
    (id, status) => tracked.push({id, status}),
    () => 8,
    (timer) => clearedTimers.push(timer),
    () => {},
  );

  const pendingCreate = confirmJob();
  await Promise.resolve();
  state.jobId = 'job-B';
  state.jobViewRevision = 1;
  state.jobRequestKey = 'request-B';
  state.pollTimer = 9;
  stored.set('ai_edit_v2_job_id', 'job-B');
  stored.set('ai_edit_v2_idempotency_key', 'request-B');
  resolveCreate({job_id: 'job-A', status: 'queued', held_points: 64});
  await pendingCreate;

  assert.equal(state.jobId, 'job-B');
  assert.equal(state.jobRequestKey, 'request-B');
  assert.equal(state.pollTimer, 9);
  assert.equal(stored.get('ai_edit_v2_job_id'), 'job-B');
  assert.equal(stored.get('ai_edit_v2_idempotency_key'), 'request-B');
  assert.deepEqual(tracked, [{id: 'job-A', status: 'queued'}]);
  assert.deepEqual(clearedTimers, []);
});

test('a late create response is not adopted after its quote and draft are invalidated', async () => {
  const page = fs.readFileSync(pagePath, 'utf8');
  const confirmSource = page.match(/async function confirmJob\(\)\{[^\n]+\}/)?.[0];
  assert.ok(confirmSource, 'confirmJob must be present');

  let resolveCreate;
  const createResponse = new Promise((resolve) => { resolveCreate = resolve; });
  let draft = {main_input: {asset_id: 'subject-A'}, aspect_ratio: '9:16'};
  const state = {
    quote: {id: 'quote-A', held_points: 64},
    quotedDraftFingerprint: JSON.stringify(draft),
    busy: false, busyCount: 0,
    jobRequestKey: null, jobId: null, jobViewRevision: 0, pollTimer: null,
  };
  const elements = {formMessage: {textContent: ''}, taskDetails: {hidden: true}};
  const stored = new Map();
  const tracked = [];
  const confirmJob = Function(
    'state', '$', 'api', 'buildDraft', 'renderWorkspacePanel', 'invalidateQuote',
    'beginBusy', 'endBusy', 'crypto', 'sessionStorage', 'trackJob', 'trackJobById',
    'setInterval', 'clearInterval', 'pollJob', 'clearTaskQuery',
    `${confirmSource}; return confirmJob;`,
  )(
    state,
    (id) => elements[id],
    async () => createResponse,
    () => draft,
    () => {},
    () => {},
    () => { state.busyCount += 1; state.busy = true; },
    () => { state.busyCount -= 1; state.busy = state.busyCount > 0; },
    {randomUUID: () => 'request-A'},
    {setItem: (key, value) => stored.set(key, value)},
    () => {},
    (id, status) => tracked.push({id, status}),
    () => 8, () => {}, () => {}, () => {},
  );

  const pendingCreate = confirmJob();
  await Promise.resolve();
  draft = {main_input: {asset_id: 'subject-B'}, aspect_ratio: '9:16'};
  state.quote = null;
  state.quotedDraftFingerprint = null;
  state.jobRequestKey = null;
  resolveCreate({job_id: 'job-for-A', status: 'queued', held_points: 64});
  await pendingCreate;

  assert.equal(state.jobId, null);
  assert.equal(state.jobRequestKey, null);
  assert.equal(stored.has('ai_edit_v2_job_id'), false);
  assert.deepEqual(tracked, [{id: 'job-for-A', status: 'queued'}]);
});

test('a quote response is ignored after the page switches through another job context', async () => {
  const page = fs.readFileSync(pagePath, 'utf8');
  const ensureSource = page.match(/async function ensureMainAsset\(\)\{[^\n]+\}/)?.[0];
  const quoteSource = page.match(/async function requestQuote\(\)\{[^\n]+\}/)?.[0];
  assert.ok(ensureSource && quoteSource, 'quote functions must be present');

  let resolveQuote;
  const quoteResponse = new Promise((resolve) => { resolveQuote = resolve; });
  const main = {
    name: 'subject-A.mp4', kind: 'video', input_mode: 'external_video',
    asset: {asset_id: 'subject-A', kind: 'video'},
  };
  const draft = {main_input: main.asset, aspect_ratio: '9:16'};
  const state = {
    main, subjectIntentRevision: 1,
    jobId: null, jobViewRevision: 1, terminalJobVisible: false,
    quote: null, quotedDraftFingerprint: null,
    busy: false, busyCount: 0,
  };
  const elements = {
    formMessage: {textContent: ''},
    quoteMin: {textContent: '—'}, quoteMax: {textContent: '—'},
  };
  const requestQuote = Function(
    'state', '$', 'api', 'buildDraft', 'renderWorkspacePanel', 'invalidateQuote',
    'beginBusy', 'endBusy',
    `${ensureSource}; ${quoteSource}; return requestQuote;`,
  )(
    state,
    (id) => elements[id],
    async () => quoteResponse,
    () => draft,
    () => {},
    () => {},
    () => { state.busyCount += 1; state.busy = true; },
    () => { state.busyCount -= 1; state.busy = state.busyCount > 0; },
  );

  const pendingQuote = requestQuote();
  await Promise.resolve();
  state.jobId = 'job-B';
  state.jobViewRevision = 2;
  state.jobId = null;
  state.jobViewRevision = 3;
  state.terminalJobVisible = true;
  resolveQuote({quote: {id: 'quote-for-A', minimum_points: 48, maximum_points: 64, held_points: 64}});
  await pendingQuote;

  assert.equal(state.quote, null);
  assert.equal(state.quotedDraftFingerprint, null);
  assert.equal(elements.quoteMin.textContent, '—');
  assert.equal(elements.quoteMax.textContent, '—');
});

test('a completed job releases the composer instead of permanently locking the next edit', async () => {
  const page = fs.readFileSync(pagePath, 'utf8');
  const renderSource = page.match(/function renderWorkspacePanel\(\)\{[^\n]+\}/)?.[0];
  const pollSource = page.match(/async function pollJob\(\)\{[^\n]+\}/)?.[0];
  assert.ok(renderSource && pollSource, 'workspace and polling functions must be present');

  const requestQuote = () => {};
  const confirmJob = () => {};
  const removedSessionKeys = [];
  const state = {
    main: {
      name: 'subject.mp4',
      kind: 'video',
      asset: {asset_id: '31', kind: 'video'},
    },
    mainPreviewUrl: '',
    candidates: [],
    quote: {id: 'old-quote', held_points: 64},
    quotedDraftFingerprint: '{"old":true}',
    jobRequestKey: 'old-request',
    jobId: 'completed-job',
    pollTimer: 7,
    busy: false,
  };
  const elements = {
    subjectSummary: {textContent: ''},
    editModeSummary: {textContent: ''},
    materialCount: {textContent: ''},
    ratioSummary: {textContent: ''},
    aspectRatio: {value: '9:16'},
    primaryAction: {textContent: '', disabled: false, onclick: undefined},
    taskDetails: {hidden: true},
    jobStatus: {textContent: ''},
    queueTime: {textContent: ''},
    processingTime: {textContent: ''},
    repairTime: {textContent: ''},
    elapsedTime: {textContent: ''},
    estimatedTime: {textContent: ''},
    remainingTime: {textContent: ''},
    degradationList: {textContent: ''},
    qualitySummary: {textContent: ''},
    actualCharge: {textContent: ''},
    refundedDifference: {textContent: ''},
    retryJobBtn: {hidden: true},
    resultVideo: {src: '', hidden: true},
    downloadResult: {href: '', style: {display: 'none'}},
    assetResult: {href: '', style: {display: 'none'}},
    quoteMin: {textContent: '48'},
    quoteMax: {textContent: '64'},
  };
  const sessionStorage = {
    getItem(key) {
      if (key === 'ai_edit_v2_job_id') return 'completed-job';
      if (key === 'ai_edit_v2_idempotency_key') return 'old-request';
      return null;
    },
    removeItem(key) {
      removedSessionKeys.push(key);
    },
  };
  let clearedTimer = null;
  const workflow = Function(
    'state', '$', 'api', 'trackJob', 'formatSeconds', 'stageLabel',
    'clearInterval', 'renderSubjectPreview', 'modeLabel', 'requestQuote',
    'confirmJob', 'sessionStorage',
    `${renderSource}; ${pollSource}; return {renderWorkspacePanel, pollJob};`,
  )(
    state,
    (id) => elements[id],
    async () => ({
      job: {status: 'completed'},
      stage: 'completed',
      timing: {queue_seconds: 1, processing_seconds: 2, repair_seconds: 0, remaining_seconds: 0},
      elapsed_seconds: 3,
      estimated_remaining_seconds: 0,
      degradations: [],
      quality: {summary: 'passed'},
      billing: {actual_charge_points: 15, refunded_difference_points: 49},
      output: {
        play_url: '/result.mp4',
        download_url: '/download.mp4',
        asset_url: '/assets/1',
      },
    }),
    () => {},
    (seconds) => String(seconds ?? 0),
    (status) => status,
    (timer) => {
      clearedTimer = timer;
    },
    () => {},
    () => 'AI智能剪辑',
    requestQuote,
    confirmJob,
    sessionStorage,
  );

  workflow.renderWorkspacePanel();
  assert.equal(elements.primaryAction.disabled, true);
  assert.equal(elements.primaryAction.onclick, null);

  await workflow.pollJob();

  assert.equal(clearedTimer, 7);
  assert.equal(state.pollTimer, null);
  assert.equal(state.jobId, null);
  assert.equal(state.jobRequestKey, null);
  assert.equal(state.quote, null);
  assert.equal(state.quotedDraftFingerprint, null);
  assert.deepEqual(removedSessionKeys.sort(), [
    'ai_edit_v2_idempotency_key',
    'ai_edit_v2_job_id',
  ]);
  assert.equal(elements.primaryAction.disabled, false);
  assert.equal(elements.primaryAction.onclick, requestQuote);
  assert.equal(elements.taskDetails.hidden, false);
  assert.equal(elements.downloadResult.style.display, 'inline');
});

test('a restored failed job releases the composer while keeping its retry action available', async () => {
  const page = fs.readFileSync(pagePath, 'utf8');
  const renderSource = page.match(/function renderWorkspacePanel\(\)\{[^\n]+\}/)?.[0];
  const pollSource = page.match(/async function pollJob\(\)\{[^\n]+\}/)?.[0];
  assert.ok(renderSource && pollSource, 'workspace and polling functions must be present');

  const requestQuote = () => {};
  const state = {
    main: {name: 'subject.mp4', kind: 'video', asset: {asset_id: '31', kind: 'video'}},
    mainPreviewUrl: '',
    candidates: [],
    quote: {id: 'old-quote', held_points: 64},
    quotedDraftFingerprint: '{"old":true}',
    jobRequestKey: 'old-request',
    jobId: 'failed-job',
    retryJobId: null,
    jobRestoreSource: 'session',
    jobRestoreFirstPoll: true,
    jobViewRevision: 2,
    pollInFlightToken: null,
    pollTimer: 7,
    terminalJobVisible: false,
    busy: false,
  };
  const elements = {
    subjectSummary: {textContent: ''}, editModeSummary: {textContent: ''},
    materialCount: {textContent: ''}, ratioSummary: {textContent: ''},
    aspectRatio: {value: '9:16'}, primaryAction: {textContent: '', disabled: false},
    taskDetails: {hidden: true}, jobStatus: {textContent: ''},
    queueTime: {textContent: ''}, processingTime: {textContent: ''},
    repairTime: {textContent: ''}, elapsedTime: {textContent: ''},
    estimatedTime: {textContent: ''}, remainingTime: {textContent: ''},
    degradationList: {textContent: ''}, qualitySummary: {textContent: ''},
    actualCharge: {textContent: ''}, refundedDifference: {textContent: ''},
    retryJobBtn: {hidden: true, disabled: false},
    resultVideo: {src: '', hidden: true},
    downloadResult: {href: '', style: {display: 'none'}},
    assetResult: {href: '', style: {display: 'none'}},
    quoteMin: {textContent: '48'}, quoteMax: {textContent: '64'},
  };
  const removed = [];
  const workflow = Function(
    'state', '$', 'api', 'trackJob', 'formatSeconds', 'stageLabel',
    'clearInterval', 'renderSubjectPreview', 'modeLabel', 'requestQuote',
    'confirmJob', 'sessionStorage',
    `${renderSource}; ${pollSource}; return {renderWorkspacePanel, pollJob};`,
  )(
    state,
    (id) => elements[id],
    async () => ({job: {status: 'render_failed'}, stage: 'render_failed', timing: {}, quality: {}, billing: {}}),
    () => {}, () => '0:00', (status) => status, () => {}, () => {}, () => 'AI智能剪辑',
    requestQuote, () => {},
    {
      getItem: (key) => key === 'ai_edit_v2_job_id' ? 'failed-job' : 'old-request',
      removeItem: (key) => removed.push(key),
    },
  );

  await workflow.pollJob();

  assert.equal(state.jobId, null);
  assert.equal(state.retryJobId, 'failed-job');
  assert.equal(state.terminalJobVisible, true);
  assert.equal(state.quote, null);
  assert.equal(state.jobRequestKey, null);
  assert.equal(elements.retryJobBtn.hidden, false);
  assert.equal(elements.primaryAction.disabled, false);
  assert.equal(elements.primaryAction.onclick, requestQuote);
  assert.deepEqual(removed, []);
});

test('an implicitly restored completed job does not reopen as the current task', async () => {
  const page = fs.readFileSync(pagePath, 'utf8');
  const pollSource = page.match(/async function pollJob\(\)\{[^\n]+\}/)?.[0];
  assert.ok(pollSource, 'pollJob must be present');

  const state = {
    main: null,
    quote: null,
    quotedDraftFingerprint: null,
    jobRequestKey: 'old-request',
    jobId: 'completed-job',
    jobRestoreSource: 'session',
    jobRestoreFirstPoll: true,
    pollTimer: 7,
  };
  const elements = {
    taskDetails: {hidden: true},
    jobStatus: {textContent: ''},
    queueTime: {textContent: ''},
    processingTime: {textContent: ''},
    repairTime: {textContent: ''},
    elapsedTime: {textContent: ''},
    estimatedTime: {textContent: ''},
    remainingTime: {textContent: ''},
    degradationList: {textContent: ''},
    qualitySummary: {textContent: ''},
    actualCharge: {textContent: ''},
    refundedDifference: {textContent: ''},
    retryJobBtn: {hidden: true},
    resultVideo: {src: '', hidden: true},
    downloadResult: {href: '', style: {display: 'none'}},
    assetResult: {href: '', style: {display: 'none'}},
    quoteMin: {textContent: ''},
    quoteMax: {textContent: ''},
  };
  const removedSessionKeys = [];
  let resolveCompletedResponse;
  const completedResponse = new Promise((resolve) => {
    resolveCompletedResponse = resolve;
  });
  const pollJob = Function(
    'state', '$', 'api', 'trackJob', 'formatSeconds', 'stageLabel',
    'clearInterval', 'sessionStorage', 'renderWorkspacePanel',
    `${pollSource}; return pollJob;`,
  )(
    state,
    (id) => elements[id],
    async () => completedResponse,
    () => {},
    () => '0:00',
    (status) => status,
    () => {},
    {
      getItem: (key) => key === 'ai_edit_v2_job_id' ? 'completed-job' : 'old-request',
      removeItem: (key) => removedSessionKeys.push(key),
    },
    () => {},
  );

  const pendingPoll = pollJob();
  await Promise.resolve();
  state.main = {name: 'new-subject.mp4', kind: 'video', asset: {asset_id: 'new-subject'}};
  resolveCompletedResponse({
    job: {status: 'completed'},
    stage: 'completed',
    timing: {},
    quality: {},
    billing: {},
    output: {
      play_url: '/stale-result.mp4',
      download_url: '/stale-download.mp4',
      asset_url: '/assets/stale',
    },
  });
  await pendingPoll;

  assert.equal(state.jobId, null);
  assert.equal(state.jobRestoreSource, null);
  assert.equal(state.jobRestoreFirstPoll, false);
  assert.equal(state.terminalJobVisible, false);
  assert.equal(elements.taskDetails.hidden, true);
  assert.equal(elements.resultVideo.src, '');
  assert.equal(elements.resultVideo.hidden, true);
  assert.equal(elements.downloadResult.style.display, 'none');
  assert.equal(elements.assetResult.style.display, 'none');
  assert.deepEqual(removedSessionKeys.sort(), [
    'ai_edit_v2_idempotency_key',
    'ai_edit_v2_job_id',
  ]);
});

test('a restored running job keeps its result visible when it completes later', async () => {
  const page = fs.readFileSync(pagePath, 'utf8');
  const pollSource = page.match(/async function pollJob\(\)\{[^\n]+\}/)?.[0];
  assert.ok(pollSource, 'pollJob must be present');

  const state = {
    main: null,
    quote: null,
    quotedDraftFingerprint: null,
    jobRequestKey: 'request-1',
    jobId: 'running-job',
    jobRestoreSource: 'session',
    jobRestoreFirstPoll: true,
    pollTimer: 7,
  };
  const elements = {
    taskDetails: {hidden: true},
    jobStatus: {textContent: ''},
    queueTime: {textContent: ''},
    processingTime: {textContent: ''},
    repairTime: {textContent: ''},
    elapsedTime: {textContent: ''},
    estimatedTime: {textContent: ''},
    remainingTime: {textContent: ''},
    degradationList: {textContent: ''},
    qualitySummary: {textContent: ''},
    actualCharge: {textContent: ''},
    refundedDifference: {textContent: ''},
    retryJobBtn: {hidden: true},
    resultVideo: {src: '', hidden: true},
    downloadResult: {href: '', style: {display: 'none'}},
    assetResult: {href: '', style: {display: 'none'}},
    quoteMin: {textContent: ''},
    quoteMax: {textContent: ''},
  };
  const responses = [
    {job: {status: 'rendering'}, stage: 'rendering', timing: {}, quality: {}, billing: {}},
    {
      job: {status: 'completed'},
      stage: 'completed',
      timing: {},
      quality: {},
      billing: {},
      output: {
        play_url: '/new-result.mp4',
        download_url: '/new-download.mp4',
        asset_url: '/assets/new',
      },
    },
  ];
  const pollJob = Function(
    'state', '$', 'api', 'trackJob', 'formatSeconds', 'stageLabel',
    'clearInterval', 'sessionStorage', 'renderWorkspacePanel',
    `${pollSource}; return pollJob;`,
  )(
    state,
    (id) => elements[id],
    async () => responses.shift(),
    () => {},
    () => '0:00',
    (status) => status,
    () => {},
    {
      getItem: (key) => key === 'ai_edit_v2_job_id' ? 'running-job' : 'request-1',
      removeItem: () => {},
    },
    () => {},
  );

  await pollJob();
  assert.equal(state.jobId, 'running-job');
  assert.equal(state.jobRestoreFirstPoll, false);

  await pollJob();

  assert.equal(state.jobId, null);
  assert.equal(state.terminalJobVisible, true);
  assert.equal(elements.taskDetails.hidden, false);
  assert.equal(elements.resultVideo.src, '/new-result.mp4');
  assert.equal(elements.downloadResult.style.display, 'inline');
});

test('viewing a completed history task does not erase another active session job', async () => {
  const page = fs.readFileSync(pagePath, 'utf8');
  const pollSource = page.match(/async function pollJob\(\)\{[^\n]+\}/)?.[0];
  assert.ok(pollSource, 'pollJob must be present');

  const state = {
    main: null,
    quote: null,
    quotedDraftFingerprint: null,
    jobRequestKey: null,
    jobId: 'history-job',
    jobRestoreSource: 'explicit',
    jobRestoreFirstPoll: true,
    pollTimer: 7,
  };
  const elements = {
    taskDetails: {hidden: true},
    jobStatus: {textContent: ''},
    queueTime: {textContent: ''},
    processingTime: {textContent: ''},
    repairTime: {textContent: ''},
    elapsedTime: {textContent: ''},
    estimatedTime: {textContent: ''},
    remainingTime: {textContent: ''},
    degradationList: {textContent: ''},
    qualitySummary: {textContent: ''},
    actualCharge: {textContent: ''},
    refundedDifference: {textContent: ''},
    retryJobBtn: {hidden: true},
    resultVideo: {src: '', hidden: true},
    downloadResult: {href: '', style: {display: 'none'}},
    assetResult: {href: '', style: {display: 'none'}},
    quoteMin: {textContent: ''},
    quoteMax: {textContent: ''},
  };
  const stored = new Map([
    ['ai_edit_v2_job_id', 'active-session-job'],
    ['ai_edit_v2_idempotency_key', 'active-session-request'],
  ]);
  const removed = [];
  const pollJob = Function(
    'state', '$', 'api', 'trackJob', 'formatSeconds', 'stageLabel',
    'clearInterval', 'sessionStorage', 'renderWorkspacePanel',
    `${pollSource}; return pollJob;`,
  )(
    state,
    (id) => elements[id],
    async () => ({job: {status: 'completed'}, stage: 'completed', timing: {}, quality: {}, billing: {}}),
    () => {},
    () => '0:00',
    (status) => status,
    () => {},
    {
      getItem: (key) => stored.get(key) || null,
      removeItem(key) {
        removed.push(key);
        stored.delete(key);
      },
    },
    () => {},
  );

  await pollJob();

  assert.equal(state.jobId, null);
  assert.equal(elements.taskDetails.hidden, false);
  assert.deepEqual(removed, []);
  assert.equal(stored.get('ai_edit_v2_job_id'), 'active-session-job');
  assert.equal(stored.get('ai_edit_v2_idempotency_key'), 'active-session-request');
});

test('restored jobs distinguish stale session recovery from explicit task viewing', () => {
  const page = fs.readFileSync(pagePath, 'utf8');
  const restoreSource = page.match(/function restorePendingJob\([^)]*\)\{[^\n]+\}/)?.[0];
  assert.ok(restoreSource, 'restorePendingJob must be present');

  const restore = (search, requestedSource, requestedJobId) => {
    const state = {
      jobId: null,
      jobRequestKey: null,
      jobRestoreSource: null,
      pollTimer: null,
    };
    const elements = {
      taskDetails: {hidden: true},
      jobStatus: {textContent: ''},
    };
    const restorePendingJob = Function(
      'state', '$', 'location', 'sessionStorage', 'URLSearchParams',
      'clearInterval', 'setInterval', 'pollJob',
      `${restoreSource}; return restorePendingJob;`,
    )(
      state,
      (id) => elements[id],
      {search},
      {
        getItem(key) {
          if (key === 'ai_edit_v2_job_id') return 'session-job';
          if (key === 'ai_edit_v2_idempotency_key') return 'session-request';
          return null;
        },
      },
      URLSearchParams,
      () => {},
      () => 3,
      () => {},
    );
    restorePendingJob(requestedSource, requestedJobId);
    return state;
  };

  const implicit = restore('', undefined);
  assert.equal(implicit.jobId, 'session-job');
  assert.equal(implicit.jobRestoreSource, 'session');
  assert.equal(implicit.jobRestoreFirstPoll, true);
  assert.equal(implicit.jobRequestKey, 'session-request');

  const explicitUrl = restore('?task=url-job', undefined);
  assert.equal(explicitUrl.jobId, 'url-job');
  assert.equal(explicitUrl.jobRestoreSource, 'explicit');
  assert.equal(explicitUrl.jobRequestKey, null);

  const explicitResume = restore('', 'explicit');
  assert.equal(explicitResume.jobId, 'session-job');
  assert.equal(explicitResume.jobRestoreSource, 'explicit');

  const explicitEvent = restore('?task=url-job', 'explicit', 'event-job');
  assert.equal(explicitEvent.jobId, 'event-job');
  assert.equal(explicitEvent.jobRestoreSource, 'explicit');
  assert.equal(explicitEvent.jobRequestKey, null);
});

test('a late completed response cannot release a newer active job', async () => {
  const page = fs.readFileSync(pagePath, 'utf8');
  const pollSource = page.match(/async function pollJob\(\)\{[^\n]+\}/)?.[0];
  assert.ok(pollSource, 'pollJob must be present');

  let resolveResponse;
  const response = new Promise((resolve) => {
    resolveResponse = resolve;
  });
  const state = {
    main: null,
    quote: null,
    quotedDraftFingerprint: null,
    jobRequestKey: 'old-request',
    jobId: 'old-job',
    jobRestoreSource: null,
    pollTimer: 7,
  };
  const elements = {
    taskDetails: {hidden: true},
    jobStatus: {textContent: ''},
    queueTime: {textContent: ''},
    processingTime: {textContent: ''},
    repairTime: {textContent: ''},
    elapsedTime: {textContent: ''},
    estimatedTime: {textContent: ''},
    remainingTime: {textContent: ''},
    degradationList: {textContent: ''},
    qualitySummary: {textContent: ''},
    actualCharge: {textContent: ''},
    refundedDifference: {textContent: ''},
    retryJobBtn: {hidden: true},
    resultVideo: {src: '', hidden: true},
    downloadResult: {href: '', style: {display: 'none'}},
    assetResult: {href: '', style: {display: 'none'}},
    quoteMin: {textContent: ''},
    quoteMax: {textContent: ''},
  };
  const removedSessionKeys = [];
  const clearedTimers = [];
  const pollJob = Function(
    'state', '$', 'api', 'trackJob', 'formatSeconds', 'stageLabel',
    'clearInterval', 'sessionStorage', 'renderWorkspacePanel',
    `${pollSource}; return pollJob;`,
  )(
    state,
    (id) => elements[id],
    async (path) => {
      assert.equal(path, '/api/v2/edit/jobs/old-job');
      return response;
    },
    () => {},
    () => '0:00',
    (status) => status,
    (timer) => clearedTimers.push(timer),
    {removeItem: (key) => removedSessionKeys.push(key)},
    () => {},
  );

  const pending = pollJob();
  await Promise.resolve();
  state.jobId = 'new-job';
  state.jobRequestKey = 'new-request';
  state.pollTimer = 9;
  resolveResponse({
    job: {status: 'completed'},
    stage: 'completed',
    timing: {},
    quality: {},
    billing: {},
  });
  await pending;

  assert.equal(state.jobId, 'new-job');
  assert.equal(state.jobRequestKey, 'new-request');
  assert.equal(state.pollTimer, 9);
  assert.deepEqual(clearedTimers, []);
  assert.deepEqual(removedSessionKeys, []);
});

test('polling allows only one request at a time and resumes after it settles', async () => {
  const page = fs.readFileSync(pagePath, 'utf8');
  const pollSource = page.match(/async function pollJob\(\)\{[^\n]+\}/)?.[0];
  assert.ok(pollSource, 'pollJob must be present');

  let resolveFirst;
  const firstResponse = new Promise((resolve) => { resolveFirst = resolve; });
  let apiCalls = 0;
  const state = {
    main: null,
    quote: null,
    quotedDraftFingerprint: null,
    jobRequestKey: 'request-1',
    jobId: 'same-job',
    jobRestoreSource: null,
    jobRestoreFirstPoll: false,
    jobViewRevision: 1,
    pollInFlightToken: null,
    pollTimer: 7,
  };
  const elements = {
    taskDetails: {hidden: true},
    jobStatus: {textContent: ''},
    queueTime: {textContent: ''},
    processingTime: {textContent: ''},
    repairTime: {textContent: ''},
    elapsedTime: {textContent: ''},
    estimatedTime: {textContent: ''},
    remainingTime: {textContent: ''},
    degradationList: {textContent: ''},
    qualitySummary: {textContent: ''},
    actualCharge: {textContent: ''},
    refundedDifference: {textContent: ''},
    retryJobBtn: {hidden: true, disabled: false},
    resultVideo: {src: '', hidden: true},
    downloadResult: {href: '', style: {display: 'none'}},
    assetResult: {href: '', style: {display: 'none'}},
    quoteMin: {textContent: ''},
    quoteMax: {textContent: ''},
  };
  const pollJob = Function(
    'state', '$', 'api', 'trackJob', 'formatSeconds', 'stageLabel',
    'clearInterval', 'sessionStorage', 'renderWorkspacePanel',
    `${pollSource}; return pollJob;`,
  )(
    state,
    (id) => elements[id],
    async () => {
      apiCalls += 1;
      if (apiCalls === 1) return firstResponse;
      return {job: {status: 'render_failed'}, stage: 'render_failed', timing: {}, quality: {}, billing: {}};
    },
    () => {},
    () => '0:00',
    (status) => status,
    () => {},
    {getItem: () => null, removeItem: () => {}},
    () => {},
  );

  const firstPoll = pollJob();
  await pollJob();
  assert.equal(apiCalls, 1);
  assert.equal(state.pollInFlightToken.jobId, 'same-job');

  resolveFirst({job: {status: 'rendering'}, stage: 'rendering', timing: {}, quality: {}, billing: {}});
  await firstPoll;
  assert.equal(elements.jobStatus.textContent, 'rendering');
  assert.equal(state.pollInFlightToken, null);
  assert.equal(state.pollTimer, 7);

  await pollJob();

  assert.equal(elements.jobStatus.textContent, 'render_failed');
  assert.equal(elements.retryJobBtn.hidden, false);
  assert.equal(state.pollTimer, null);
  assert.equal(apiCalls, 2);
  assert.equal(state.pollInFlightToken, null);
});

test('switching jobs starts a new poll without letting the old poll clear its token', async () => {
  const page = fs.readFileSync(pagePath, 'utf8');
  const pollSource = page.match(/async function pollJob\(\)\{[^\n]+\}/)?.[0];
  assert.ok(pollSource, 'pollJob must be present');

  let rejectA;
  let resolveB;
  const responseA = new Promise((_resolve, reject) => { rejectA = reject; });
  const responseB = new Promise((resolve) => { resolveB = resolve; });
  const calls = [];
  const state = {
    main: null,
    quote: null,
    quotedDraftFingerprint: null,
    jobRequestKey: 'request-A',
    jobId: 'job-A',
    jobRestoreSource: null,
    jobRestoreFirstPoll: false,
    jobViewRevision: 1,
    pollInFlightToken: null,
    pollTimer: 7,
  };
  const elements = {
    taskDetails: {hidden: true},
    jobStatus: {textContent: ''},
    queueTime: {textContent: ''},
    processingTime: {textContent: ''},
    repairTime: {textContent: ''},
    elapsedTime: {textContent: ''},
    estimatedTime: {textContent: ''},
    remainingTime: {textContent: ''},
    degradationList: {textContent: ''},
    qualitySummary: {textContent: ''},
    actualCharge: {textContent: ''},
    refundedDifference: {textContent: ''},
    retryJobBtn: {hidden: true, disabled: false},
    resultVideo: {src: '', hidden: true},
    downloadResult: {href: '', style: {display: 'none'}},
    assetResult: {href: '', style: {display: 'none'}},
    quoteMin: {textContent: ''},
    quoteMax: {textContent: ''},
  };
  const pollJob = Function(
    'state', '$', 'api', 'trackJob', 'formatSeconds', 'stageLabel',
    'clearInterval', 'sessionStorage', 'renderWorkspacePanel',
    `${pollSource}; return pollJob;`,
  )(
    state,
    (id) => elements[id],
    async (path) => {
      calls.push(path);
      return path.endsWith('/job-A') ? responseA : responseB;
    },
    () => {},
    () => '0:00',
    (status) => status,
    () => {},
    {getItem: () => null, removeItem: () => {}},
    () => {},
  );

  const pollA = pollJob();
  state.jobId = 'job-B';
  state.jobViewRevision = 2;
  state.jobRequestKey = 'request-B';
  state.pollTimer = 9;
  const pollB = pollJob();

  assert.deepEqual(calls, ['/api/v2/edit/jobs/job-A', '/api/v2/edit/jobs/job-B']);
  const tokenB = state.pollInFlightToken;
  assert.equal(tokenB.jobId, 'job-B');

  rejectA(new Error('old-job-network-error'));
  await pollA;
  assert.equal(state.jobId, 'job-B');
  assert.equal(state.pollInFlightToken, tokenB);
  assert.notEqual(elements.jobStatus.textContent, 'old-job-network-error');

  resolveB({job: {status: 'rendering'}, stage: 'rendering', timing: {}, quality: {}, billing: {}});
  await pollB;
  assert.equal(elements.jobStatus.textContent, 'rendering');
  assert.equal(state.pollInFlightToken, null);
  assert.equal(state.pollTimer, 9);
});

test('retrying a failed restored job becomes a current-page task', async () => {
  const page = fs.readFileSync(pagePath, 'utf8');
  const retrySource = page.match(/async function retryJob\(\)\{[^\n]+\}/)?.[0];
  const clearQuerySource = page.match(/function clearTaskQuery\(\)\{[^\n]+\}/)?.[0];
  assert.ok(retrySource && clearQuerySource, 'retry and task-query cleanup must be present');

  const state = {
    jobId: null,
    retryJobId: 'failed-job',
    jobRestoreSource: null,
    jobRequestKey: null,
    jobViewRevision: 2,
    pollTimer: 7,
  };
  const elements = {
    retryJobBtn: {hidden: false, disabled: false},
    formMessage: {textContent: ''},
  };
  const stored = new Map();
  const location = {search: '?task=failed-job', pathname: '/workbench/ai-edit-v2', hash: ''};
  const history = {
    replaceState(_state, _title, url) {
      location.search = new URL(url, 'https://example.test').search;
    },
  };
  const retryJob = Function(
    'state', '$', 'sessionStorage', 'crypto', 'api', 'trackJob',
    'clearInterval', 'setInterval', 'pollJob', 'location', 'history', 'URLSearchParams',
    'beginBusy', 'endBusy', 'renderWorkspacePanel',
    `${clearQuerySource}; ${retrySource}; return retryJob;`,
  )(
    state,
    (id) => elements[id],
    {
      getItem: (key) => stored.get(key) || null,
      setItem: (key, value) => stored.set(key, value),
      removeItem: (key) => stored.delete(key),
    },
    {randomUUID: () => 'retry-request'},
    async () => ({job_id: 'retry-job', status: 'queued', held_points: 64}),
    () => {},
    () => {},
    () => 8,
    () => {},
    location,
    history,
    URLSearchParams,
    () => {},
    () => {},
    () => {},
  );

  await retryJob();

  assert.equal(state.jobId, 'retry-job');
  assert.equal(state.retryJobId, null);
  assert.equal(state.jobRestoreSource, null);
  assert.equal(state.jobRequestKey, 'retry-request');
  assert.equal(elements.retryJobBtn.hidden, true);
  assert.equal(location.search, '');
});

test('retry locks the composer while creating and running its successor task', async () => {
  const page = fs.readFileSync(pagePath, 'utf8');
  const beginSource = page.match(/function beginBusy\(\)\{[^\n]+\}/)?.[0];
  const endSource = page.match(/function endBusy\(\)\{[^\n]+\}/)?.[0];
  const renderSource = page.match(/function renderWorkspacePanel\(\)\{[^\n]+\}/)?.[0];
  const retrySource = page.match(/async function retryJob\(\)\{[^\n]+\}/)?.[0];
  assert.ok(beginSource && endSource && renderSource && retrySource, 'retry lifecycle functions must be present');

  let resolveRetry;
  const retryResponse = new Promise((resolve) => { resolveRetry = resolve; });
  const requestQuote = () => {};
  const state = {
    main: {name: 'subject.mp4', kind: 'video', asset: {asset_id: 'subject-A'}},
    mainPreviewUrl: '', candidates: [], quote: null,
    jobId: null, retryJobId: 'failed-job', jobViewRevision: 2,
    jobRestoreSource: null, jobRequestKey: null, terminalJobVisible: true,
    pollTimer: null, busy: false, busyCount: 0,
  };
  const elements = {
    subjectSummary: {textContent: ''}, editModeSummary: {textContent: ''},
    materialCount: {textContent: ''}, ratioSummary: {textContent: ''},
    aspectRatio: {value: '9:16'}, primaryAction: {textContent: '', disabled: false},
    retryJobBtn: {hidden: false, disabled: false}, formMessage: {textContent: ''},
  };
  const retryJob = Function(
    'state', '$', 'sessionStorage', 'crypto', 'api', 'trackJob', 'trackJobById',
    'clearInterval', 'setInterval', 'pollJob', 'clearTaskQuery',
    'renderSubjectPreview', 'modeLabel', 'requestQuote', 'confirmJob',
    `${beginSource}; ${endSource}; ${renderSource}; ${retrySource}; return retryJob;`,
  )(
    state,
    (id) => elements[id],
    {getItem: () => null, setItem: () => {}, removeItem: () => {}},
    {randomUUID: () => 'retry-request'},
    async () => retryResponse,
    () => {}, () => {}, () => {}, () => 8, () => {}, () => {},
    () => {}, () => 'AI智能剪辑', requestQuote, () => {},
  );

  const pendingRetry = retryJob();
  await Promise.resolve();
  assert.equal(state.busy, true);
  assert.equal(elements.primaryAction.disabled, true);
  assert.equal(elements.primaryAction.textContent, '正在准备素材');

  resolveRetry({job_id: 'retry-job', status: 'queued', held_points: 64});
  await pendingRetry;

  assert.equal(state.busy, false);
  assert.equal(state.jobId, 'retry-job');
  assert.equal(elements.primaryAction.disabled, true);
  assert.equal(elements.primaryAction.textContent, '任务已提交');
});

test('a late retry response cannot replace a job opened while retry was pending', async () => {
  const page = fs.readFileSync(pagePath, 'utf8');
  const retrySource = page.match(/async function retryJob\(\)\{[^\n]+\}/)?.[0];
  assert.ok(retrySource, 'retryJob must be present');

  let resolveRetry;
  const retryResponse = new Promise((resolve) => { resolveRetry = resolve; });
  const state = {
    jobId: null,
    retryJobId: 'failed-job-A',
    jobRestoreSource: null,
    jobRequestKey: null,
    jobViewRevision: 2,
    pollTimer: 7,
  };
  const elements = {
    retryJobBtn: {hidden: false, disabled: false},
    formMessage: {textContent: ''},
  };
  const stored = new Map([
    ['ai_edit_v2_job_id', 'failed-job-A'],
    ['ai_edit_v2_idempotency_key', 'request-A'],
  ]);
  const tracked = [];
  const clearedTimers = [];
  const retryJob = Function(
    'state', '$', 'sessionStorage', 'crypto', 'api', 'trackJob', 'trackJobById',
    'clearInterval', 'setInterval', 'pollJob', 'beginBusy', 'endBusy', 'renderWorkspacePanel',
    `${retrySource}; return retryJob;`,
  )(
    state,
    (id) => elements[id],
    {
      getItem: (key) => stored.get(key) || null,
      setItem: (key, value) => stored.set(key, value),
      removeItem: (key) => stored.delete(key),
    },
    {randomUUID: () => 'retry-request-A'},
    async () => retryResponse,
    () => {},
    (id, status) => tracked.push({id, status}),
    (timer) => clearedTimers.push(timer),
    () => 8,
    () => {},
    () => {},
    () => {},
    () => {},
  );

  const pendingRetry = retryJob();
  await Promise.resolve();
  state.jobId = 'job-B';
  state.retryJobId = null;
  state.jobViewRevision = 3;
  state.jobRequestKey = 'request-B';
  state.pollTimer = 9;
  stored.set('ai_edit_v2_job_id', 'job-B');
  stored.set('ai_edit_v2_idempotency_key', 'request-B');
  resolveRetry({job_id: 'retry-job-A', status: 'queued', held_points: 64});
  await pendingRetry;

  assert.equal(state.jobId, 'job-B');
  assert.equal(state.jobRequestKey, 'request-B');
  assert.equal(state.pollTimer, 9);
  assert.equal(stored.get('ai_edit_v2_job_id'), 'job-B');
  assert.equal(stored.get('ai_edit_v2_idempotency_key'), 'request-B');
  assert.deepEqual(tracked, [{id: 'retry-job-A', status: 'queued'}]);
  assert.deepEqual(clearedTimers, []);
});

test('opening another task clears the previous terminal result media', () => {
  const page = fs.readFileSync(pagePath, 'utf8');
  const invalidateSource = page.match(/function invalidateQuote\(\)\{[^\n]+\}/)?.[0];
  const restoreSource = page.match(/function restorePendingJob\([^)]*\)\{[^\n]+\}/)?.[0];
  assert.ok(invalidateSource && restoreSource, 'terminal cleanup and restore functions must be present');

  const videoEvents = [];
  const state = {
    jobId: null,
    retryJobId: null,
    jobRequestKey: null,
    jobViewRevision: 4,
    terminalJobVisible: true,
    quote: null,
    quotedDraftFingerprint: null,
    pollTimer: null,
  };
  const elements = {
    taskDetails: {hidden: false},
    jobStatus: {textContent: ''},
    resultVideo: {
      hidden: false,
      pause: () => videoEvents.push('pause'),
      removeAttribute: (name) => videoEvents.push(`remove:${name}`),
      load: () => videoEvents.push('load'),
    },
    downloadResult: {href: '/old.mp4', style: {display: 'inline'}},
    assetResult: {href: '/assets/old', style: {display: 'inline'}},
    retryJobBtn: {hidden: true},
    quoteMin: {textContent: '48'},
    quoteMax: {textContent: '64'},
  };
  let pollCalls = 0;
  const workflow = Function(
    'state', '$', 'renderWorkspacePanel', 'location', 'sessionStorage',
    'URLSearchParams', 'clearInterval', 'setInterval', 'pollJob',
    `${invalidateSource}; ${restoreSource}; return restorePendingJob;`,
  )(
    state,
    (id) => elements[id],
    () => {},
    {search: ''},
    {getItem: () => null, removeItem: () => {}},
    URLSearchParams,
    () => {},
    () => 8,
    () => { pollCalls += 1; },
  );

  workflow('explicit', 'job-B');

  assert.equal(state.jobId, 'job-B');
  assert.equal(state.terminalJobVisible, false);
  assert.equal(elements.taskDetails.hidden, false);
  assert.equal(elements.resultVideo.hidden, true);
  assert.equal(elements.downloadResult.style.display, 'none');
  assert.equal(elements.assetResult.style.display, 'none');
  assert.deepEqual(videoEvents, ['pause', 'remove:src', 'load']);
  assert.equal(pollCalls, 1);
});

test('changing the draft after a failed job clears its saved retry state', () => {
  const page = fs.readFileSync(pagePath, 'utf8');
  const invalidateSource = page.match(/function invalidateQuote\(\)\{[^\n]+\}/)?.[0];
  assert.ok(invalidateSource, 'invalidateQuote must be present');

  const state = {
    jobId: null,
    retryJobId: 'failed-job',
    terminalJobVisible: true,
    quote: null,
    quotedDraftFingerprint: null,
    jobRequestKey: null,
  };
  const elements = {
    taskDetails: {hidden: false},
    resultVideo: {hidden: true, removeAttribute: () => {}, load: () => {}},
    downloadResult: {href: '', style: {display: 'none'}},
    assetResult: {href: '', style: {display: 'none'}},
    retryJobBtn: {hidden: false},
    quoteMin: {textContent: ''}, quoteMax: {textContent: ''},
  };
  const stored = new Map([
    ['ai_edit_v2_job_id', 'failed-job'],
    ['ai_edit_v2_idempotency_key', 'failed-request'],
  ]);
  const invalidateQuote = Function(
    'state', '$', 'renderWorkspacePanel', 'sessionStorage',
    `${invalidateSource}; return invalidateQuote;`,
  )(
    state,
    (id) => elements[id],
    () => {},
    {
      getItem: (key) => stored.get(key) || null,
      removeItem: (key) => stored.delete(key),
    },
  );

  invalidateQuote();

  assert.equal(state.retryJobId, null);
  assert.equal(state.terminalJobVisible, false);
  assert.equal(stored.has('ai_edit_v2_job_id'), false);
  assert.equal(stored.has('ai_edit_v2_idempotency_key'), false);
});

test('invalidating a quote never clears an active task result or session pointer', () => {
  const page = fs.readFileSync(pagePath, 'utf8');
  const invalidateSource = page.match(/function invalidateQuote\(\)\{[^\n]+\}/)?.[0];
  assert.ok(invalidateSource, 'invalidateQuote must be present');

  const state = {
    jobId: 'active-job',
    retryJobId: null,
    terminalJobVisible: true,
    quote: {id: 'old-quote'},
    quotedDraftFingerprint: 'old-fingerprint',
    jobRequestKey: 'active-request',
  };
  const elements = {
    taskDetails: {hidden: false},
    resultVideo: {hidden: false, removeAttribute: () => { throw new Error('active video must not be cleared'); }},
    downloadResult: {href: '/active.mp4', style: {display: 'inline'}},
    assetResult: {href: '/assets/active', style: {display: 'inline'}},
    retryJobBtn: {hidden: true},
    quoteMin: {textContent: '48'}, quoteMax: {textContent: '64'},
  };
  const removed = [];
  const invalidateQuote = Function(
    'state', '$', 'renderWorkspacePanel', 'sessionStorage',
    `${invalidateSource}; return invalidateQuote;`,
  )(
    state,
    (id) => elements[id],
    () => {},
    {
      getItem: () => 'active-job',
      removeItem: (key) => removed.push(key),
    },
  );

  invalidateQuote();

  assert.equal(state.jobId, 'active-job');
  assert.equal(state.terminalJobVisible, true);
  assert.equal(elements.taskDetails.hidden, false);
  assert.equal(elements.resultVideo.hidden, false);
  assert.equal(elements.downloadResult.href, '/active.mp4');
  assert.equal(elements.assetResult.href, '/assets/active');
  assert.deepEqual(removed, []);
  assert.equal(state.quote, null);
});

test('changing a new draft clears the previous completed result panel', () => {
  const page = fs.readFileSync(pagePath, 'utf8');
  const invalidateSource = page.match(/function invalidateQuote\(\)\{[^\n]+\}/)?.[0];
  assert.ok(invalidateSource, 'invalidateQuote must be present');

  const videoEvents = [];
  const state = {
    jobId: null,
    terminalJobVisible: true,
    quote: {id: 'old-quote'},
    quotedDraftFingerprint: 'old-fingerprint',
    jobRequestKey: 'old-request',
  };
  const elements = {
    taskDetails: {hidden: false},
    resultVideo: {
      hidden: false,
      pause: () => videoEvents.push('pause'),
      removeAttribute: (name) => videoEvents.push(`remove:${name}`),
      load: () => videoEvents.push('load'),
    },
    downloadResult: {href: '/old.mp4', style: {display: 'inline'}},
    assetResult: {href: '/assets/old', style: {display: 'inline'}},
    retryJobBtn: {hidden: true},
    quoteMin: {textContent: '48'},
    quoteMax: {textContent: '64'},
  };
  const invalidateQuote = Function(
    'state', '$', 'renderWorkspacePanel',
    `${invalidateSource}; return invalidateQuote;`,
  )(
    state,
    (id) => elements[id],
    () => {},
  );

  invalidateQuote();

  assert.equal(state.terminalJobVisible, false);
  assert.equal(elements.taskDetails.hidden, true);
  assert.equal(elements.resultVideo.hidden, true);
  assert.deepEqual(videoEvents, ['pause', 'remove:src', 'load']);
  assert.equal(elements.downloadResult.style.display, 'none');
  assert.equal(elements.assetResult.style.display, 'none');
  assert.equal(state.quote, null);
  assert.equal(state.quotedDraftFingerprint, null);
  assert.equal(state.jobRequestKey, null);
});

test('requesting a new quote clears the previous result even when the draft is unchanged', async () => {
  const page = fs.readFileSync(pagePath, 'utf8');
  const invalidateSource = page.match(/function invalidateQuote\(\)\{[^\n]+\}/)?.[0];
  const quoteSource = page.match(/async function requestQuote\(\)\{[^\n]+\}/)?.[0];
  assert.ok(invalidateSource && quoteSource, 'quote lifecycle functions must be present');

  const videoEvents = [];
  const main = {
    name: 'subject.mp4',
    kind: 'video',
    input_mode: 'external_video',
    asset: {asset_id: 'subject-1', kind: 'video'},
  };
  const state = {
    main,
    subjectIntentRevision: 1,
    terminalJobVisible: true,
    jobId: null,
    quote: null,
    quotedDraftFingerprint: null,
    jobRequestKey: null,
    busy: false,
  };
  const elements = {
    taskDetails: {hidden: false},
    resultVideo: {
      hidden: false,
      pause: () => videoEvents.push('pause'),
      removeAttribute: (name) => videoEvents.push(`remove:${name}`),
      load: () => videoEvents.push('load'),
    },
    downloadResult: {href: '/old.mp4', style: {display: 'inline'}},
    assetResult: {href: '/assets/old', style: {display: 'inline'}},
    retryJobBtn: {hidden: true},
    quoteMin: {textContent: '48'},
    quoteMax: {textContent: '64'},
    formMessage: {textContent: ''},
  };
  const draft = {main_input: main.asset, aspect_ratio: '9:16'};
  const requestQuote = Function(
    'state', '$', 'api', 'buildDraft', 'renderWorkspacePanel',
    'ensureMainAsset', 'beginBusy', 'endBusy',
    `${invalidateSource}; ${quoteSource}; return requestQuote;`,
  )(
    state,
    (id) => elements[id],
    async () => ({quote: {id: 'new-quote', minimum_points: 50, maximum_points: 70, held_points: 70}}),
    () => draft,
    () => {},
    async () => main.asset,
    () => { state.busy = true; },
    () => { state.busy = false; },
  );

  await requestQuote();

  assert.equal(state.terminalJobVisible, false);
  assert.equal(elements.taskDetails.hidden, true);
  assert.deepEqual(videoEvents, ['pause', 'remove:src', 'load']);
  assert.equal(elements.downloadResult.style.display, 'none');
  assert.equal(elements.assetResult.style.display, 'none');
  assert.equal(state.quote.id, 'new-quote');
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
