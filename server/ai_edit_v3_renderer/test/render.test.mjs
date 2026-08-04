import assert from "node:assert/strict";
import {mkdtemp, mkdir, readFile, writeFile} from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import {buildRenderReport} from "../src/report.mjs";
import {buildRenderCommand, renderHyperframes} from "../src/render-hyperframes.mjs";
import {selectManifestCompiler} from "../src/render.mjs";

test("render dispatches manifest v1 and v2 before compilation", () => {
  assert.equal(selectManifestCompiler("1.0"), "legacy");
  assert.equal(selectManifestCompiler("2.0"), "component");
  assert.throws(() => selectManifestCompiler("3.0"), /render_manifest_version_unknown/);
});

test("render command is fixed, silent, strict and does not inherit provider secrets", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "v3-render-"));
  await writeFile(path.join(root, "index.html"), '<div id="main" data-composition-id="main" data-width="1920" data-height="1080" data-duration="1"></div>');
  const output = path.join(root, "silent.mp4");
  const calls = [];
  const execution = await renderHyperframes({
    projectRoot: root,
    outputPath: output,
    chromiumPath: "/opt/chromium/chrome",
    timeoutMs: 10_000,
    environment: {PATH: "/usr/bin", ELEVENLABS_API_KEY: "must-not-leak", DASHCOPE_API_KEY: "must-not-leak"},
    commandRunner: async (spec) => {
      calls.push(spec);
      if (spec.argv.includes("render")) await writeFile(output, Buffer.from("silent-video"));
      else {
        const snapshotRoot = spec.argv[spec.argv.indexOf("--output") + 1];
        await mkdir(snapshotRoot, {recursive: true});
        await writeFile(path.join(snapshotRoot, "frame-000.png"), Buffer.from("png"));
      }
      return {returncode: 0, stdout: Buffer.from("ok"), stderr: Buffer.alloc(0), elapsedMs: 7};
    },
  });
  assert.equal(calls.length, 2);
  const render = calls[0];
  assert.deepEqual(render.argv, buildRenderCommand({projectRoot: root, outputPath: output}));
  assert.ok(render.argv.includes("--strict-all"));
  assert.ok(render.argv.includes("--no-best-effort"));
  assert.equal(render.environment.PUPPETEER_EXECUTABLE_PATH, path.resolve("/opt/chromium/chrome"));
  assert.equal(render.environment.HYPERFRAMES_BROWSER_PATH, path.resolve("/opt/chromium/chrome"));
  assert.equal(render.environment.ELEVENLABS_API_KEY, undefined);
  assert.equal(render.environment.DASHCOPE_API_KEY, undefined);
  assert.equal(execution.outputSha256.length, 64);
  assert.equal(execution.snapshots.length, 1);
});

test("render report binds verified inputs, output, frames and snapshots", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "v3-report-"));
  const output = path.join(root, "silent.mp4");
  const snapshot = path.join(root, "frame.png");
  await writeFile(output, Buffer.from("video"));
  await writeFile(snapshot, Buffer.from("png"));
  const report = await buildRenderReport({
    manifest: {duration_ms: 1000, output_spec: {width: 1920, height: 1080, fps_num: 30, fps_den: 1}, renderer_environment: {renderer_build_id: "sha256:" + "1".repeat(64)}},
    verifiedFiles: [{relativePath: "media/source.mp4", size: 5, sha256: "a".repeat(64)}],
    compiledProject: {expectedFrames: 30, compositionIds: ["main", "scene_01"], registrySha256: "sha256:" + "2".repeat(64)},
    execution: {outputPath: output, outputSha256: "b".repeat(64), outputSize: 5, elapsedMs: 10, stdoutSha256: "c".repeat(64), stderrSha256: "d".repeat(64), commandSha256: "e".repeat(64)},
    snapshots: [{path: snapshot, sha256: "f".repeat(64), size: 3}],
    outputPath: output,
  });
  assert.equal(report.status, "done");
  assert.equal(report.expected_frames, 30);
  assert.equal(report.output.sha256, "b".repeat(64));
  assert.deepEqual(report.verified_inputs, [{path: "media/source.mp4", size_bytes: 5, sha256: "a".repeat(64)}]);
  assert.equal(report.snapshots[0].sha256, "f".repeat(64));
  assert.equal(JSON.parse(await readFile(output.replace(/\.mp4$/, ".report.json"), "utf8")).status, "done");
});
