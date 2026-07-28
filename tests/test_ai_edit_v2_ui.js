const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const root = path.resolve(__dirname, '..');
const pagePath = path.join(root, 'site/workbench/ai-edit.html');
const videoPath = path.join(root, 'site/workbench/video.html');
const shellPath = path.join(root, 'site/workbench/cloud-shell.js');
const tasksPath = path.join(root, 'site/workbench/tasks.js');

test('AI edit page exposes the frozen Phase A task flow', () => {
  assert.equal(fs.existsSync(pagePath), true, 'site/workbench/ai-edit.html must exist');
  const page = fs.readFileSync(pagePath, 'utf8');
  assert.match(page, /data-active="ai_edit_v2"/);
  for (const mode of ['natural_brief', 'platform_template', 'open_generation']) {
    assert.match(page, new RegExp(`data-creation-mode="${mode}"`));
  }
  assert.match(page, /id="mainInput"[^>]*accept="video\/\*,audio\/\*"/);
  assert.match(page, /id="requiredInput"[^>]*multiple[^>]*accept="image\/\*,video\/\*,audio\/\*"/);
  assert.match(page, /id="referenceInput"[^>]*multiple[^>]*accept="image\/\*,video\/\*,audio\/\*"/);
  assert.match(page, /最多 10 个/);
  assert.match(page, /value="direct_use"/);
  assert.match(page, /value="style_only"/);
  assert.match(page, /value="16:9"/);
  assert.match(page, /value="9:16"/);
  assert.match(page, /id="targetDuration"/);
  assert.match(page, /id="quoteMin"/);
  assert.match(page, /id="quoteMax"/);
  assert.match(page, /按价格上限预扣/);
  assert.match(page, /id="confirmPrecharge"/);
  for (const id of ['queueTime', 'processingTime', 'repairTime', 'resultVideo', 'downloadResult']) {
    assert.match(page, new RegExp(`id="${id}"`));
  }
  for (const id of ['elapsedTime', 'estimatedTime', 'degradationList', 'qualitySummary', 'actualCharge', 'refundedDifference', 'assetResult', 'retryJobBtn']) {
    assert.match(page, new RegExp(`id="${id}"`), id);
  }
});

test('page implements draft quote confirmation upload retry and job polling', () => {
  const page = fs.readFileSync(pagePath, 'utf8');
  for (const name of ['buildDraft', 'requestQuote', 'confirmJob', 'pollJob', 'uploadFiles', 'retryUpload', 'retryJob']) {
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
  assert.match(page, /data\.degradations/);
  assert.match(page, /data\.quality/);
  assert.match(page, /actual_charge_points/);
  assert.match(page, /refunded_difference_points/);
  assert.match(page, /data\.output\.asset_url/);
  assert.match(page, /estimated_remaining_seconds===0/);
  assert.match(page, /HQTasks\.upsert/);
});

test('legacy video workflow keeps its controls and links to the stable editor', () => {
  const video = fs.readFileSync(videoPath, 'utf8');
  assert.match(video, /data-function="talking"/);
  assert.match(video, /id="generateBtn"/);
  assert.match(video, /href="ai-edit\.html"[^>]*data-ai-edit-v2-entry/);
});

test('shared task tracker resumes V2 editing jobs without changing legacy video routing', () => {
  const tasks = fs.readFileSync(tasksPath, 'utf8');
  assert.match(tasks, /ai_edit_v2/);
  assert.match(tasks, /ai-edit\.html\?task=/);
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
  assert.match(shell, /\{k:'ai_edit_v2',l:'AI智能剪辑',i:'edit',gated:true\}/);
  assert.match(shell, /\/api\/v2\/edit\/capabilities/);
  assert.match(shell, /accepts_submissions/);
  assert.match(shell, /NAV_PAGES=\{ai_edit_v2:'ai-edit\.html'\}/);
  const page = fs.readFileSync(pagePath, 'utf8');
  assert.match(page, /loadCapability/);
  assert.match(page, /功能尚未开放/);
});
