import assert from "node:assert/strict";
import {mkdtemp, writeFile} from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import {buildRenderReport, canonicalReportBytes} from "../src/report.mjs";

test("identical frozen evidence produces byte-identical reports", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "v3-determinism-"));
  const output = path.join(root, "silent.mp4");
  await writeFile(output, Buffer.from("video"));
  const args = {
    manifest: {duration_ms: 1000, output_spec: {width: 1920, height: 1080, fps_num: 30, fps_den: 1}, renderer_environment: {renderer_build_id: "sha256:" + "1".repeat(64)}},
    verifiedFiles: [], compiledProject: {expectedFrames: 30, compositionIds: ["main"], registrySha256: "sha256:" + "2".repeat(64)},
    execution: {outputPath: output, outputSha256: "b".repeat(64), outputSize: 5, elapsedMs: 10, stdoutSha256: "c".repeat(64), stderrSha256: "d".repeat(64), commandSha256: "e".repeat(64)},
    snapshots: [], outputPath: output,
  };
  const first = await buildRenderReport({...args, reportPath: path.join(root, "a.json")});
  const second = await buildRenderReport({...args, reportPath: path.join(root, "b.json")});
  assert.deepEqual(canonicalReportBytes(first), canonicalReportBytes(second));
});
