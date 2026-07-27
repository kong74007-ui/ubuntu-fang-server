const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const root = path.resolve(__dirname, '..');

test('V2 editor uses its isolated page and API namespace', () => {
  const v2 = fs.readFileSync(path.join(root, 'site/workbench/ai-edit-v2.html'), 'utf8');
  assert.match(v2, /data-active="ai_edit_v2"/);
  assert.doesNotMatch(v2, /\/api\/gen\/ai-edit\//);
  assert.match(v2, /\/api\/v2\/edit\//);
});

test('shared shell routes the gated V2 editor to its isolated page', () => {
  const shell = fs.readFileSync(path.join(root, 'site/workbench/cloud-shell.js'), 'utf8');
  assert.match(shell, /\{k:'ai_edit_v2',l:'AI智能剪辑',i:'edit',gated:true\}/);
  assert.match(shell, /NAV_PAGES=\{ai_edit_v2:'ai-edit-v2\.html'\}/);
});
