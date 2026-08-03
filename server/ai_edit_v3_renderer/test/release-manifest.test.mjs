import assert from "node:assert/strict";
import {mkdtemp, mkdir, readFile, writeFile} from "node:fs/promises";
import {tmpdir} from "node:os";
import {join} from "node:path";
import test from "node:test";

import {
  canonicalReleaseBytes,
  computeRendererBuildId,
  inspectRendererRelease,
  validateRendererRelease,
} from "../src/release-manifest.mjs";


test("package pins the only renderer libraries", async () => {
  const pkg = JSON.parse(await readFile(new URL("../package.json", import.meta.url)));
  assert.deepEqual(pkg.engines, {node: ">=22 <23"});
  assert.equal(pkg.dependencies.hyperframes, "0.7.84");
  assert.equal(pkg.dependencies.gsap, "3.15.0");
  assert.equal(Object.keys(pkg.dependencies).sort().join(","), "gsap,hyperframes");
});


test("release schema and build id are exact and deterministic", () => {
  const release = {
    schema_version: 1,
    git_commit: "a".repeat(40),
    package_lock_sha256: "b".repeat(64),
    node: {version: "v22.22.0", sha256: "c".repeat(64)},
    chromium: {version: "Chromium 140.0.0", sha256: "d".repeat(64)},
    ffmpeg: {version: "ffmpeg version 7.1", sha256: "e".repeat(64)},
    ffprobe: {version: "ffprobe version 7.1", sha256: "f".repeat(64)},
    hyperframes_version: "0.7.84",
    gsap_version: "3.15.0",
    locale: "C.UTF-8",
    timezone: "UTC",
    encoder_argv: ["-c:v", "libx264", "-pix_fmt", "yuv420p"],
    thread_count: 2,
    fonts: [{relative_path: "assets/fonts/a.woff2", sha256: "1".repeat(64)}],
  };
  const id = computeRendererBuildId(release);
  assert.match(id, /^sha256:[0-9a-f]{64}$/);
  assert.equal(computeRendererBuildId({...release, renderer_build_id: id}), id);
  assert.deepEqual(JSON.parse(canonicalReleaseBytes({...release, renderer_build_id: id})), {...release, renderer_build_id: id});
  validateRendererRelease({...release, renderer_build_id: id});
  for (const schemaVersion of ["1", 0, 2, undefined]) {
    assert.throws(() => validateRendererRelease({...release, schema_version: schemaVersion, renderer_build_id: id}), /renderer_schema_version_invalid/);
  }
});


test("inspection records actual binary and font bytes", async () => {
  const root = await mkdtemp(join(tmpdir(), "v3-release-"));
  await mkdir(join(root, "assets", "fonts"), {recursive: true});
  await writeFile(join(root, "package-lock.json"), "{}\n");
  await writeFile(join(root, "assets", "fonts", "font.woff2"), "font-bytes");
  const executable = join(root, process.platform === "win32" ? "fake.cmd" : "fake");
  await writeFile(executable, process.platform === "win32" ? "@echo v22.22.0\r\n" : "#!/bin/sh\necho v22.22.0\n");

  const release = await inspectRendererRelease({
    repoRoot: root,
    releaseRoot: root,
    nodePath: executable,
    chromiumPath: executable,
    ffmpegPath: executable,
    ffprobePath: executable,
    gitCommit: "a".repeat(40),
    versionProbe: async () => "v22.22.0",
  });

  assert.equal(release.schema_version, 1);
  assert.equal(release.fonts.length, 1);
  assert.match(release.renderer_build_id, /^sha256:/);
});
