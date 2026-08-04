import assert from "node:assert/strict";
import {spawn} from "node:child_process";
import {cp, mkdtemp, mkdir, readFile, writeFile} from "node:fs/promises";
import {tmpdir} from "node:os";
import {dirname, join} from "node:path";
import {fileURLToPath, pathToFileURL} from "node:url";
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
  for (const schemaVersion of ["1", 0, 3, undefined]) {
    assert.throws(() => validateRendererRelease({...release, schema_version: schemaVersion, renderer_build_id: id}), /renderer_schema_version_invalid/);
  }
});


test("inspection records actual binary and font bytes", async () => {
  const root = await mkdtemp(join(tmpdir(), "v3-release-"));
  await mkdir(join(root, "assets", "fonts"), {recursive: true});
  await mkdir(join(root, "src"), {recursive: true});
  await writeFile(join(root, "src", "render.mjs"), "export {};\n");
  await writeFile(join(root, "package.json"), "{}\n");
  await writeFile(join(root, "package-lock.json"), "{}\n");
  await writeFile(join(root, "hyperframes.json"), "{}\n");
  await writeFile(join(root, "registry-sha256.txt"), `sha256:${"1".repeat(64)}\n`);
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

  assert.equal(release.schema_version, 2);
  assert.equal(release.fonts.length, 1);
  assert.match(release.renderer_build_id, /^sha256:/);
});


test("release identity covers every allowlisted runtime input", async () => {
  const root = await mkdtemp(join(tmpdir(), "v3-release-tree-"));
  await mkdir(join(root, "src", "registry"), {recursive: true});
  await mkdir(join(root, "assets", "fonts"), {recursive: true});
  await writeFile(join(root, "src", "render.mjs"), "export const value = 1;\n");
  await writeFile(join(root, "src", "registry", "layouts.mjs"), "export const layout = 1;\n");
  await writeFile(join(root, "assets", "fonts", "font.woff2"), "font-bytes");
  await writeFile(join(root, "package.json"), "{}\n");
  await writeFile(join(root, "package-lock.json"), "{}\n");
  await writeFile(join(root, "hyperframes.json"), "{}\n");
  await writeFile(join(root, "registry-sha256.txt"), `sha256:${"1".repeat(64)}\n`);
  const executable = join(root, process.platform === "win32" ? "fake.cmd" : "fake");
  await writeFile(executable, process.platform === "win32" ? "@echo v22.22.0\r\n" : "#!/bin/sh\necho v22.22.0\n");
  const inspect = (releaseRoot) => inspectRendererRelease({
    repoRoot: releaseRoot,
    releaseRoot,
    nodePath: executable,
    chromiumPath: executable,
    ffmpegPath: executable,
    ffprobePath: executable,
    gitCommit: "a".repeat(40),
    versionProbe: async () => "v22.22.0",
  });
  const baseline = await inspect(root);
  for (const relativePath of [
    ["src", "registry", "layouts.mjs"],
    ["package-lock.json"],
    ["assets", "fonts", "font.woff2"],
    ["hyperframes.json"],
  ]) {
    const copy = `${root}-${relativePath.at(-1)}`;
    await cp(root, copy, {recursive: true});
    await writeFile(join(copy, ...relativePath), "changed-byte");
    const changed = await inspect(copy);
    assert.notEqual(changed.renderer_build_id, baseline.renderer_build_id, relativePath.join("/"));
  }
  assert.ok(Array.isArray(baseline.release_tree_files));
  assert.match(baseline.release_tree_sha256, /^[0-9a-f]{64}$/);
  assert.ok(!baseline.release_tree_files.some((item) => item.relative_path === "renderer-release.lock.json"));
});


test("locked release tree is self-contained for v1 and v2 manifest schema validation", async () => {
  const rendererRoot = fileURLToPath(new URL("..", import.meta.url));
  const lock = JSON.parse(await readFile(join(rendererRoot, "renderer-release.lock.json"), "utf8"));
  const releaseRoot = await mkdtemp(join(tmpdir(), "v3-independent-release-"));
  for (const item of lock.release_tree_files) {
    const destination = join(releaseRoot, item.relative_path);
    await mkdir(dirname(destination), {recursive: true});
    await cp(join(rendererRoot, item.relative_path), destination);
  }

  const cacheBust = `?independent=${Date.now()}`;
  const {MANIFEST_SCHEMA_SHA256_BY_VERSION} = await import(`${pathToFileURL(join(releaseRoot, "src", "manifest-schema-digests.mjs")).href}${cacheBust}`);
  const {validateManifest} = await import(`${pathToFileURL(join(releaseRoot, "src", "validate-manifest.mjs")).href}${cacheBust}`);
  const base = {
    registry_sha256: "registry", renderer_environment: {renderer_build_id: "build"},
    output_spec: {ratio: "9:16", width: 1080, height: 1920, fps_num: 30, fps_den: 1},
    duration_ms: 4000, master_audio: {path: "media/master.wav"}, source_video: null,
    compositions: [{id: "scene_1", start_ms: 0, end_ms: 4000}],
  };
  const visual = {
    theme_profile_id: "editorial_clean", design_intent: {density: "balanced", motion_energy: "medium", image_fit: "cover", decoration_intensity: "medium"}, variation_seed: "0123456789abcdef",
    design_tokens: {
      "--hf-theme-profile": "editorial_clean", "--hf-bg": "#f7f4ed", "--hf-surface": "#ffffff", "--hf-text": "#17212b", "--hf-accent": "#315b8a",
      "--hf-font": "\"Noto Sans SC\", sans-serif", "--hf-type-scale": "0.960", "--hf-gap": "28px", "--hf-radius": "26px", "--hf-border": "rgba(49,91,138,.28)",
      "--hf-shadow": "0 18px 48px rgba(23,33,43,.14)", "--hf-texture": "none", "--hf-density": "balanced", "--hf-motion-distance": "36px", "--hf-image-fit": "cover",
    },
  };
  const expected = {rendererBuildId: "build", registrySha256: "sha256:registry", schemaSha256ByVersion: MANIFEST_SCHEMA_SHA256_BY_VERSION};
  assert.equal(validateManifest({...base, version: "1.0", schema_sha256: MANIFEST_SCHEMA_SHA256_BY_VERSION["1.0"]}, expected).version, "1.0");
  assert.equal(validateManifest({...base, ...visual, version: "2.0", schema_sha256: MANIFEST_SCHEMA_SHA256_BY_VERSION["2.0"], compositions: [{...base.compositions[0], overlay_ids: [], overlay_instances: []}]}, expected).version, "2.0");
});


test("manifest schema digest generator checks authoritative schema bytes and rejects stale output", async () => {
  const rendererRoot = fileURLToPath(new URL("..", import.meta.url));
  const script = join(rendererRoot, "scripts", "write-manifest-schema-digests.mjs");
  const current = await runNode(script, ["--check"]);
  assert.equal(current.code, 0, current.stderr);

  const stale = join(await mkdtemp(join(tmpdir(), "v3-stale-schema-digests-")), "manifest-schema-digests.mjs");
  await writeFile(stale, "export const MANIFEST_SCHEMA_SHA256_BY_VERSION = {};\n", "utf8");
  const rejected = await runNode(script, ["--check", "--output", stale]);
  assert.notEqual(rejected.code, 0);
  assert.match(rejected.stderr, /manifest_schema_digests_stale/);
});


function runNode(script, args) {
  return new Promise((resolve) => {
    const child = spawn(process.execPath, [script, ...args], {stdio: ["ignore", "pipe", "pipe"]});
    const stdout = [];
    const stderr = [];
    child.stdout.on("data", (chunk) => stdout.push(chunk));
    child.stderr.on("data", (chunk) => stderr.push(chunk));
    child.on("close", (code) => resolve({code, stdout: Buffer.concat(stdout).toString("utf8"), stderr: Buffer.concat(stderr).toString("utf8")}));
  });
}
