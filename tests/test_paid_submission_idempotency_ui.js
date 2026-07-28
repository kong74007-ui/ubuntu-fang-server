const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const root = path.resolve(__dirname, '..');
const videoPage = fs.readFileSync(path.join(root, 'site', 'workbench', 'video.html'), 'utf8');
const aiEditPage = fs.readFileSync(path.join(root, 'site', 'workbench', 'ai-edit.html'), 'utf8');
const VIDEO_STORAGE = 'hq_video_batch_pending_submit_v1';
const AI_EDIT_STORAGE = 'hq_ai_edit_pending_submit_v1';

let uuidSequence = 0;

function extractFunction(source, name) {
  const marker = `function ${name}(`;
  const start = source.indexOf(marker);
  assert.notEqual(start, -1, `missing production function ${name}`);
  const open = source.indexOf('{', start + marker.length);
  assert.notEqual(open, -1, `missing body for production function ${name}`);

  let depth = 0;
  let quote = '';
  let escaped = false;
  let lineComment = false;
  let blockComment = false;
  for (let index = open; index < source.length; index += 1) {
    const char = source[index];
    const next = source[index + 1];
    if (lineComment) {
      if (char === '\n') lineComment = false;
      continue;
    }
    if (blockComment) {
      if (char === '*' && next === '/') {
        blockComment = false;
        index += 1;
      }
      continue;
    }
    if (quote) {
      if (escaped) escaped = false;
      else if (char === '\\') escaped = true;
      else if (char === quote) quote = '';
      continue;
    }
    if (char === '/' && next === '/') {
      lineComment = true;
      index += 1;
      continue;
    }
    if (char === '/' && next === '*') {
      blockComment = true;
      index += 1;
      continue;
    }
    if (char === "'" || char === '"' || char === '`') {
      quote = char;
      continue;
    }
    if (char === '{') depth += 1;
    if (char === '}') {
      depth -= 1;
      if (depth === 0) return source.slice(start, index + 1);
    }
  }
  assert.fail(`unterminated production function ${name}`);
}

function memoryStorage() {
  const values = new Map();
  return {
    getItem(key) {
      return values.has(key) ? values.get(key) : null;
    },
    setItem(key, value) {
      values.set(key, String(value));
    },
    removeItem(key) {
      values.delete(key);
    },
  };
}

function throwingStorage() {
  const storage = memoryStorage();
  storage.setItem('hq_user', JSON.stringify({ username: 'fang' }));
  storage.setItem = function setItem() {
    throw new Error('storage disabled');
  };
  return storage;
}

function noOpStorage() {
  const storage = memoryStorage();
  storage.setItem('hq_user', JSON.stringify({ username: 'fang' }));
  storage.setItem = function setItem() {};
  return storage;
}

function accountStorageKey(prefix, username) {
  return `${prefix}:${encodeURIComponent(username)}`;
}

function response(status, data) {
  return {
    status,
    ok: status >= 200 && status < 300,
    json() {
      return Promise.resolve(data);
    },
  };
}

function queuedFetch(sequence, calls) {
  const queue = sequence.slice();
  return function fetchStub(url, options) {
    calls.push({
      url,
      options: Object.assign({}, options, {
        headers: Object.assign({}, options && options.headers),
      }),
    });
    assert.ok(queue.length, `unexpected fetch to ${url}`);
    const next = queue.shift();
    if (next instanceof Error) return Promise.reject(next);
    return Promise.resolve(response(next.status, next.data));
  };
}

function makeElement(value) {
  return {
    value: value || '',
    disabled: false,
    textContent: '',
    style: {},
    classList: { add() {}, remove() {}, toggle() {} },
  };
}

function loadVideoHarness(options) {
  const calls = [];
  const toasts = [];
  const statuses = [];
  const script = makeElement(options.text || '同一段口播文案');
  if (options.username !== null) {
    try {
      options.storage.setItem('hq_user', JSON.stringify({ username: options.username || 'fang' }));
    } catch (_error) {}
  }
  const context = {
    VIDEO_BATCH_PENDING_STORAGE: VIDEO_STORAGE,
    localStorage: options.storage,
    fetch: queuedFetch(options.responses || [], calls),
    window: { crypto: { randomUUID: () => `00000000-0000-4000-8000-${String(++uuidSequence).padStart(12, '0')}` } },
    token: 'test-token',
    talkingBatchItems: options.items || [
      { label: '甲', image_data: 'data:image/png;base64,AAAA', preview: 'preview-a' },
      { label: '乙', image_data: 'data:image/png;base64,BBBB', preview: 'preview-b' },
    ],
    mode: 'text',
    audioData: '',
    selectedVoice: 'voice-a',
    selectedResolution: options.resolution || '1080p',
    selectedRatio: options.ratio || '9:16',
    selectedMotion: 'medium',
    bgmData: '',
    selectedBgmVolume: 0.18,
    location: { href: '' },
    $(id) {
      assert.equal(id, 'scriptText');
      return script;
    },
    setSubmitLock() {},
    setBusy() {},
    renderVideoDrafts() {},
    setVideoStatus(value) { statuses.push(value); },
    trackVideoJob() {},
    refreshVideoPoints() {},
    setTimeout() {},
    loadVideoHistory() {},
    resumeTrackedVideoTask() {},
    toast(value) { toasts.push(value); },
  };
  vm.runInNewContext([
    extractFunction(videoPage, 'paidSubmissionAccount'),
    extractFunction(videoPage, 'videoBatchPendingStorageKey'),
    extractFunction(videoPage, 'videoBatchFingerprint'),
    extractFunction(videoPage, 'newVideoBatchRequestKey'),
    extractFunction(videoPage, 'loadVideoBatchRequest'),
    extractFunction(videoPage, 'prepareVideoBatchRequest'),
    extractFunction(videoPage, 'clearVideoBatchRequest'),
    extractFunction(videoPage, 'paidSubmissionResponseUncertain'),
    extractFunction(videoPage, 'talkingPayload'),
    extractFunction(videoPage, 'submitVideoBatch'),
    'this.__exports={submitVideoBatch};',
  ].join('\n'), context);
  return {
    calls,
    statuses,
    toasts,
    submit() { return context.__exports.submitVideoBatch(script.value); },
  };
}

function loadAiEditHarness(options) {
  const calls = [];
  const toasts = [];
  const elements = {
    generateBtn: makeElement(),
    resultActions: makeElement(),
    factName: makeElement(options.productName || '只应出现在请求正文里的产品名'),
    factCategory: makeElement('护肤'),
    factSpec: makeElement('30ml'),
    factClaims: makeElement('不夸大'),
  };
  if (options.username !== null) {
    try {
      options.storage.setItem('hq_user', JSON.stringify({ username: options.username || 'fang' }));
    } catch (_error) {}
  }
  const context = {
    AI_EDIT_PENDING_STORAGE: AI_EDIT_STORAGE,
    localStorage: options.storage,
    fetch: queuedFetch(options.responses || [], calls),
    window: {
      crypto: { randomUUID: () => `10000000-0000-4000-8000-${String(++uuidSequence).padStart(12, '0')}` },
      HQ: null,
    },
    token: 'test-token',
    location: { href: '' },
    selectedSource: { id: options.sourceId || 'source-a' },
    currentJob: 0,
    lastFailedJob: 0,
    styleId: options.styleId || 'auto',
    materials: [{ id: 'material-a', usage: 'auto' }],
    selectedMaterialIds: { 'material-a': true },
    $(id) {
      assert.ok(elements[id], `unexpected element ${id}`);
      return elements[id];
    },
    toast(value) { toasts.push(value); },
    saveActive() {},
    setBilling() {},
    loadHistory() {},
    poll() {},
  };
  vm.runInNewContext([
    extractFunction(aiEditPage, 'paidSubmissionAccount'),
    extractFunction(aiEditPage, 'aiEditPendingStorageKey'),
    extractFunction(aiEditPage, 'aiEditSubmitFingerprint'),
    extractFunction(aiEditPage, 'newAiEditSubmitKey'),
    extractFunction(aiEditPage, 'loadAiEditSubmitRequest'),
    extractFunction(aiEditPage, 'prepareAiEditSubmitRequest'),
    extractFunction(aiEditPage, 'clearAiEditSubmitRequest'),
    extractFunction(aiEditPage, 'paidSubmissionResponseUncertain'),
    extractFunction(aiEditPage, 'api'),
    extractFunction(aiEditPage, 'productFacts'),
    extractFunction(aiEditPage, 'selectedRefs'),
    extractFunction(aiEditPage, 'submit'),
    'this.__exports={submit};',
  ].join('\n'), context);
  return {
    calls,
    toasts,
    submit() { return context.__exports.submit(); },
  };
}

function pending(storage, key, username = 'fang') {
  const raw = storage.getItem(accountStorageKey(key, username));
  return raw ? JSON.parse(raw) : null;
}

test('video batch persists a body-free key before fetch and reuses it after 5xx until success', async () => {
  const storage = memoryStorage();
  const first = loadVideoHarness({ storage, responses: [{ status: 503, data: { detail: 'upstream busy' } }] });
  await first.submit();

  const saved = pending(storage, VIDEO_STORAGE);
  assert.deepEqual(Object.keys(saved).sort(), ['account', 'fingerprint', 'key']);
  assert.equal(JSON.stringify(saved).includes('data:image'), false, 'pending storage must not contain base64 media');
  assert.equal(first.calls[0].options.headers['Idempotency-Key'], saved.key);
  assert.equal(JSON.parse(first.calls[0].options.body).avatars.length, 2, 'the real request body is still sent');

  const retry = loadVideoHarness({
    storage,
    responses: [{ status: 201, data: { batch_id: 'batch-a', job_ids: [11, 12], cost: 40 } }],
  });
  await retry.submit();
  assert.equal(retry.calls[0].options.headers['Idempotency-Key'], saved.key, 'reload retry must reuse the key');
  assert.equal(pending(storage, VIDEO_STORAGE), null, 'accepted batch clears the pending record');
});

test('video batch keeps in-progress ambiguity, blocks changed input, and clears a definitive 4xx', async () => {
  const storage = memoryStorage();
  const first = loadVideoHarness({
    storage,
    responses: [{ status: 409, data: { code: 'idempotency_in_progress', detail: 'still working' } }],
  });
  await first.submit();
  const originalKey = pending(storage, VIDEO_STORAGE).key;

  const changed = loadVideoHarness({
    storage,
    text: '已经修改的口播文案',
    responses: [{ status: 201, data: { job_ids: [21, 22], cost: 40 } }],
  });
  await changed.submit();
  assert.equal(changed.calls.length, 0, 'changed input must not start a second paid request');
  assert.ok(changed.toasts.some((value) => value.includes('上一笔提交结果尚未确认')));

  const terminal = loadVideoHarness({
    storage,
    responses: [{ status: 400, data: { detail: 'invalid payload' } }],
  });
  await terminal.submit();
  assert.equal(terminal.calls[0].options.headers['Idempotency-Key'], originalKey);
  assert.equal(pending(storage, VIDEO_STORAGE), null, 'definitive 4xx clears the key');

  const next = loadVideoHarness({
    storage,
    text: '已经修改的口播文案',
    responses: [{ status: 201, data: { batch_id: 'batch-b', job_ids: [21, 22], cost: 40 } }],
  });
  await next.submit();
  assert.notEqual(next.calls[0].options.headers['Idempotency-Key'], originalKey);
});

test('video batch retains a successful-looking response that has no job ids', async () => {
  const storage = memoryStorage();
  const harness = loadVideoHarness({
    storage,
    responses: [{ status: 201, data: { detail: 'accepted without receipt' } }],
  });
  await harness.submit();
  assert.ok(pending(storage, VIDEO_STORAGE), 'missing receipt is ambiguous and must keep the key');
});

test('AI edit persists a body-free key before its real fetch and reuses it after 5xx until success', async () => {
  const storage = memoryStorage();
  const first = loadAiEditHarness({ storage, responses: [{ status: 503, data: { detail: 'busy' } }] });
  await first.submit();

  const saved = pending(storage, AI_EDIT_STORAGE);
  assert.deepEqual(Object.keys(saved).sort(), ['account', 'fingerprint', 'key']);
  assert.equal(JSON.stringify(saved).includes('产品名'), false, 'pending storage must not contain request content');
  assert.equal(first.calls[0].url, '/api/gen/ai-edit/jobs');
  assert.equal(first.calls[0].options.headers['Idempotency-Key'], saved.key);
  assert.equal(JSON.parse(first.calls[0].options.body).source_video_asset_id, 'source-a');

  const retry = loadAiEditHarness({ storage, responses: [{ status: 201, data: { job_id: 31, billing_state: 'HELD' } }] });
  await retry.submit();
  assert.equal(retry.calls[0].options.headers['Idempotency-Key'], saved.key);
  assert.equal(pending(storage, AI_EDIT_STORAGE), null, 'accepted AI edit clears the pending record');
});

test('AI edit keeps in-progress ambiguity, blocks changed input, and clears a definitive 4xx', async () => {
  const storage = memoryStorage();
  const first = loadAiEditHarness({
    storage,
    responses: [{ status: 409, data: { code: 'idempotency_in_progress', detail: 'still working' } }],
  });
  await first.submit();
  const originalKey = pending(storage, AI_EDIT_STORAGE).key;

  const changed = loadAiEditHarness({
    storage,
    styleId: 'brand_premium',
    responses: [{ status: 201, data: { job_id: 41 } }],
  });
  await changed.submit();
  assert.equal(changed.calls.length, 0, 'changed input must not start a second paid request');
  assert.ok(changed.toasts.some((value) => value.includes('上一笔提交结果尚未确认')));

  const terminal = loadAiEditHarness({
    storage,
    responses: [{ status: 422, data: { detail: 'invalid source' } }],
  });
  await terminal.submit();
  assert.equal(terminal.calls[0].options.headers['Idempotency-Key'], originalKey);
  assert.equal(pending(storage, AI_EDIT_STORAGE), null);

  const next = loadAiEditHarness({
    storage,
    styleId: 'brand_premium',
    responses: [{ status: 201, data: { job_id: 42 } }],
  });
  await next.submit();
  assert.notEqual(next.calls[0].options.headers['Idempotency-Key'], originalKey);
});

test('AI edit retains a successful-looking response that has no job id', async () => {
  const storage = memoryStorage();
  const harness = loadAiEditHarness({
    storage,
    responses: [{ status: 201, data: { billing_state: 'HELD' } }],
  });
  await harness.submit();
  assert.ok(pending(storage, AI_EDIT_STORAGE), 'missing receipt is ambiguous and must keep the key');
});

test('paid submits fail closed when durable browser storage throws or silently drops writes', async () => {
  for (const [name, makeStorage, loadHarness] of [
    ['video throw', throwingStorage, loadVideoHarness],
    ['video no-op', noOpStorage, loadVideoHarness],
    ['AI edit throw', throwingStorage, loadAiEditHarness],
    ['AI edit no-op', noOpStorage, loadAiEditHarness],
  ]) {
    const storage = makeStorage();
    const harness = loadHarness({ storage, username: null, responses: [] });
    await harness.submit();
    assert.equal(harness.calls.length, 0, `${name} must not send a paid request`);
    assert.ok(harness.toasts.some((value) => value.includes('无法保存安全提交编号')), name);
  }
});

test('pending paid keys are isolated by account and one account cannot clear another', async () => {
  const storage = memoryStorage();
  const accountA = loadVideoHarness({
    storage,
    username: 'account-a',
    responses: [{ status: 503, data: { code: 'points_result_unknown', detail: 'unknown' } }],
  });
  await accountA.submit();
  const pendingA = pending(storage, VIDEO_STORAGE, 'account-a');
  assert.ok(pendingA);

  const accountB = loadVideoHarness({
    storage,
    username: 'account-b',
    responses: [{ status: 201, data: { batch_id: 'batch-b', job_ids: [51, 52], cost: 40 } }],
  });
  await accountB.submit();
  assert.notEqual(accountB.calls[0].options.headers['Idempotency-Key'], pendingA.key);
  assert.ok(pending(storage, VIDEO_STORAGE, 'account-a'), 'account B must not clear account A');
  assert.equal(pending(storage, VIDEO_STORAGE, 'account-b'), null);

  const accountARetry = loadVideoHarness({
    storage,
    username: 'account-a',
    responses: [{ status: 201, data: { batch_id: 'batch-a', job_ids: [61, 62], cost: 40 } }],
  });
  await accountARetry.submit();
  assert.equal(accountARetry.calls[0].options.headers['Idempotency-Key'], pendingA.key);
});

test('definitive compensated 500 clears the key while unknown 5xx retains it', async () => {
  for (const [storageKey, loadHarness] of [
    [VIDEO_STORAGE, loadVideoHarness],
    [AI_EDIT_STORAGE, loadAiEditHarness],
  ]) {
    const definitiveStorage = memoryStorage();
    const definitive = loadHarness({
      storage: definitiveStorage,
      responses: [{ status: 500, data: { code: 'submission_compensated', detail: 'refunded' } }],
    });
    await definitive.submit();
    assert.equal(pending(definitiveStorage, storageKey), null);

    const unknownStorage = memoryStorage();
    const unknown = loadHarness({
      storage: unknownStorage,
      responses: [{ status: 502, data: { code: 'points_result_unknown', detail: 'unknown' } }],
    });
    await unknown.submit();
    assert.ok(pending(unknownStorage, storageKey));
  }
});

test('malformed or unclassified 409 remains ambiguous for both paid submitters', async () => {
  for (const [storageKey, loadHarness] of [
    [VIDEO_STORAGE, loadVideoHarness],
    [AI_EDIT_STORAGE, loadAiEditHarness],
  ]) {
    const storage = memoryStorage();
    const harness = loadHarness({ storage, responses: [{ status: 409, data: {} }] });
    await harness.submit();
    assert.ok(pending(storage, storageKey), 'unknown 409 must retain the stable request key');
  }
});
