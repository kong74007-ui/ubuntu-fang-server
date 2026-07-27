const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const root = path.resolve(__dirname, '..');

test('legacy and V2 editors use separate pages and APIs', () => {
  const legacy = fs.readFileSync(path.join(root, 'site/workbench/ai-edit.html'), 'utf8');
  const v2 = fs.readFileSync(path.join(root, 'site/workbench/ai-edit-v2.html'), 'utf8');
  assert.match(legacy, /data-active="ai-edit"/);
  assert.match(v2, /data-active="ai_edit_v2"/);
  assert.doesNotMatch(legacy, /\/api\/v2\/edit\//);
  assert.doesNotMatch(v2, /\/api\/gen\/ai-edit\//);
});
