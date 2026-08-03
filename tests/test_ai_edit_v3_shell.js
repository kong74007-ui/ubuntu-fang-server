const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const root = path.resolve(__dirname, '..');

test('V3 editor keeps an isolated page, API namespace, and capability gate', () => {
  const shell = fs.readFileSync(path.join(root, 'site/workbench/cloud-shell.js'), 'utf8');
  const page = fs.readFileSync(path.join(root, 'site/workbench/ai-edit-v3.html'), 'utf8');

  assert.match(shell, /\{k:'ai_edit_v3',l:'AI智能剪辑 V3',i:'edit',v3Gated:true\}/);
  assert.match(shell, /V3_NAV_PAGES=\{ai_edit_v3:'ai-edit-v3\.html'\}/);
  assert.match(shell, /\/api\/v3\/edit\/capabilities/);
  assert.match(shell, /aiEditV3Visible=true/);
  assert.match(page, /data-active="ai_edit_v3"/);
  assert.match(page, /\/api\/v3\/edit\//);
  assert.doesNotMatch(page, /\/api\/v2\/edit\//);
  assert.doesNotMatch(page, /\/api\/gen\/ai-edit\//);
});
