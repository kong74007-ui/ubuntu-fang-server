import assert from "node:assert/strict";
import {mkdtemp, readFile} from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import {compileProject} from "../src/compile-project.mjs";
import {getRegistrySha256} from "../src/registry/index.mjs";

test("compiler emits standalone HyperFrames roots and safe template scenes", async () => {
  const outputRoot = path.join(await mkdtemp(path.join(os.tmpdir(), "v3-compile-")), "project");
  const compiled = await compileProject({manifest: fixtureManifest("正常字幕"), outputRoot});
  assert.deepEqual(Object.keys(compiled).sort(), [
    "compositionIds", "entryRelativePath", "expectedFrames", "projectRoot", "registrySha256", "snapshotTimesMs",
  ]);
  assert.equal(compiled.entryRelativePath, "index.html");
  assert.equal(compiled.registrySha256, getRegistrySha256());
  assert.equal(compiled.expectedFrames, 120);
  assert.deepEqual(compiled.compositionIds, ["main", "composition_01"]);

  const index = await readFile(path.join(outputRoot, "index.html"), "utf8");
  const scene = await readFile(path.join(outputRoot, "compositions", "composition_01.html"), "utf8");
  assert.match(index, /id="main"[^>]+data-composition-id="main"[^>]+data-width="1080"[^>]+data-height="1920"/);
  assert.match(index, /data-composition-id="composition_01"[^>]+data-composition-src="compositions\/composition_01.html"/);
  assert.match(scene, /^<template id="composition_01_template">/);
  assert.match(scene, /class="[^"]*\bclip\b[^"]*"/);
  assert.match(scene, /data-composition-id="composition_01"/);
  assert.match(scene, /window\.__timelines\["composition_01"\] = tl/);
  assert.match(scene, /<style>[\s\S]+<\/style>[\s\S]*<script>[\s\S]+<\/script>[\s\S]*<\/template>$/);
  assert.doesNotMatch(scene, /<div[^>]+data-composition-id="composition_01"[^>]+style="[^"]*background/);
  assert.match(scene, /class="hf-background clip"/);
  assert.equal(new Set([...`${index}${scene}`.matchAll(/\sid="([^"]+)"/g)].map((match) => match[1])).size,
    [...`${index}${scene}`.matchAll(/\sid="([^"]+)"/g)].length);
});

test("compiler treats hostile model text as data and rejects bidi controls", async () => {
  const hostile = '</script><img onerror="globalThis.pwned=1"> javascript: {color:red}';
  const outputRoot = path.join(await mkdtemp(path.join(os.tmpdir(), "v3-inject-")), "project");
  await compileProject({manifest: fixtureManifest(hostile), outputRoot});
  const scene = await readFile(path.join(outputRoot, "compositions", "composition_01.html"), "utf8");
  assert.doesNotMatch(scene, /<img onerror=/);
  assert.doesNotMatch(scene, /<\/script><img/);
  assert.match(scene, /&lt;\/script&gt;&lt;img onerror=&quot;globalThis\.pwned=1&quot;&gt;/);
  assert.match(scene, /data-safe-text=/);

  const bidiRoot = path.join(await mkdtemp(path.join(os.tmpdir(), "v3-bidi-")), "project");
  await assert.rejects(
    compileProject({manifest: fixtureManifest("safe\u202Egnp.exe"), outputRoot: bidiRoot}),
    /text_control_forbidden/,
  );
});

test("compiler rejects registry drift and unknown capability tokens", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "v3-drift-"));
  const drift = fixtureManifest("text");
  drift.registry_sha256 = "sha256:" + "0".repeat(64);
  await assert.rejects(compileProject({manifest: drift, outputRoot: path.join(root, "a")}), /registry_sha256_mismatch/);
  const unknown = fixtureManifest("text");
  unknown.compositions[0].overlay_ids = ["unknown_overlay"];
  await assert.rejects(compileProject({manifest: unknown, outputRoot: path.join(root, "b")}), /overlay_unknown/);
});

test("compiler places only muted source-video clips and never emits audio elements", async () => {
  const outputRoot = path.join(await mkdtemp(path.join(os.tmpdir(), "v3-source-")), "project");
  const manifest = fixtureManifest("主体口播");
  manifest.source_video = {path: "media/source.mp4", silent: true};
  manifest.source_segments = [{
    id: "segment_01", source_path: "media/source.mp4", source_start_ms: 750, source_end_ms: 4750,
    output_start_ms: 0, output_end_ms: 4000,
  }];
  await compileProject({manifest, outputRoot});
  const scene = await readFile(path.join(outputRoot, "compositions", "composition_01.html"), "utf8");
  assert.match(scene, /<video[^>]+class="hf-source-video clip"[^>]+muted[^>]+playsinline/);
  assert.match(scene, /data-playback-start="0\.75"/);
  assert.match(scene, /src="media\/source\.mp4"/);
  assert.doesNotMatch(scene, /<audio\b/);
  assert.doesNotMatch(scene, /data-has-audio/);
});

function fixtureManifest(text) {
  return {
    registry_sha256: getRegistrySha256(),
    duration_ms: 4000,
    output_spec: {ratio: "9:16", width: 1080, height: 1920, fps_num: 30, fps_den: 1},
    theme: {
      palette_id: "midnight_gold", typography_id: "editorial_sans", density: "balanced",
      motion_energy: "medium", image_fit: "cover",
    },
    source_video: null,
    assets: [],
    compositions: [{
      id: "composition_01", scene_id: "scene_01", start_ms: 0, end_ms: 4000,
      layout_id: "speaker_fullscreen", layout_variant: "balanced_a",
      overlay_ids: ["standard_caption"], animations: [], transition: "hard_cut", asset_ids: [],
    }],
    captions: [{id: "caption_01", start_ms: 0, end_ms: 4000, text}],
  };
}
