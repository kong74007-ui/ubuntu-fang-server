const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const root = path.resolve(__dirname, '..');

test('legacy editor keeps its own page and API namespace', () => {
  const legacy = fs.readFileSync(path.join(root, 'site/workbench/ai-edit.html'), 'utf8');
  assert.match(legacy, /data-active="ai-edit"/);
  assert.match(legacy, /\/api\/gen\/ai-edit\//);
  assert.doesNotMatch(legacy, /\/api\/v2\/edit\//);
});

test('V2 editor uses its isolated page and API namespace', () => {
  const v2 = fs.readFileSync(path.join(root, 'site/workbench/ai-edit-v2.html'), 'utf8');
  assert.match(v2, /data-active="ai_edit_v2"/);
  assert.doesNotMatch(v2, /\/api\/gen\/ai-edit\//);
  assert.match(v2, /\/api\/v2\/edit\//);
});

test('backend namespaces and V2 database remain isolated', () => {
  const legacyApi = fs.readFileSync(path.join(root, 'server/content_domains/ai_edit_api.py'), 'utf8');
  const v2Api = fs.readFileSync(path.join(root, 'server/content_domains/ai_edit_v2_api.py'), 'utf8');
  const v2Store = fs.readFileSync(path.join(root, 'server/content_domains/ai_edit_v2_store.py'), 'utf8');
  assert.match(legacyApi, /LEGACY_API_PREFIX = "\/api\/gen\/ai-edit\/"/);
  assert.doesNotMatch(legacyApi, /ai_edit_v2\.db/);
  assert.match(v2Api, /API_PREFIX = "\/api\/v2\/edit\/"/);
  assert.match(v2Store, /DEFAULT_DB_NAME = "ai_edit_v2\.db"/);
});

test('navigation keeps legacy visible and gates only V2', () => {
  const shell = fs.readFileSync(path.join(root, 'site/workbench/cloud-shell.js'), 'utf8');
  assert.match(shell, /\{k:'ai-edit',l:'一键剪辑',i:'edit'\}/);
  assert.match(shell, /\{k:'ai_edit_v2',l:'AI智能剪辑 V2',i:'edit',gated:true\}/);
  assert.match(shell, /NAV_PAGES=\{ai_edit_v2:'ai-edit-v2\.html'\}/);
});
