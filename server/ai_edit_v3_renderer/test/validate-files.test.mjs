import assert from "node:assert/strict";
import {createHash} from "node:crypto";
import {link, mkdir, mkdtemp, symlink, writeFile} from "node:fs/promises";
import {tmpdir} from "node:os";
import {join} from "node:path";
import test from "node:test";

import {verifyInputFiles} from "../src/validate-files.mjs";


const hash = (bytes) => createHash("sha256").update(bytes).digest("hex");


test("file verifier opens regular single-link contained files", async () => {
  const root = await mkdtemp(join(tmpdir(), "v3-files-"));
  await mkdir(join(root, "media"));
  await writeFile(join(root, "media", "a.bin"), "hello");
  const manifest = {assets: [{path: "media/a.bin", size_bytes: 5, sha256: hash("hello")}], source_video: null, master_audio: null};
  const verified = await verifyInputFiles({manifest, inputRoot: root});
  assert.equal(verified.length, 1);
  assert.equal(verified[0].size, 5);
  await Promise.all(verified.map((item) => item.handle.close()));
});


test("file verifier rejects escape url symlink hardlink and identity mismatch", async () => {
  const root = await mkdtemp(join(tmpdir(), "v3-files-bad-"));
  await mkdir(join(root, "media"));
  await writeFile(join(root, "media", "a.bin"), "hello");
  for (const path of ["/absolute", "C:\\drive", "../escape", "a\\..\\b", "file:x", "http:x", "a\0b"]) {
    await assert.rejects(() => verifyInputFiles({manifest: {assets: [{path, size_bytes: 5, sha256: hash("hello")}], source_video: null, master_audio: null}, inputRoot: root}));
  }
  try {
    await symlink(join(root, "media", "a.bin"), join(root, "media", "link.bin"));
    await assert.rejects(() => verifyInputFiles({manifest: {assets: [{path: "media/link.bin", size_bytes: 5, sha256: hash("hello")}], source_video: null, master_audio: null}, inputRoot: root}), /render_input_symlink/);
  } catch (error) {
    if (error?.code !== "EPERM") throw error;
  }
  await link(join(root, "media", "a.bin"), join(root, "media", "hard.bin"));
  await assert.rejects(() => verifyInputFiles({manifest: {assets: [{path: "media/a.bin", size_bytes: 5, sha256: hash("hello")}], source_video: null, master_audio: null}, inputRoot: root}), /render_input_hardlink/);
});
