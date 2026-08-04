import assert from "node:assert/strict";
import {mkdtemp, readFile} from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import {compileProject} from "../src/compile-project.mjs";
import {compileProjectV2} from "../src/compile-project-v2.mjs";
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

test("v2 compiler consumes overlay instances and maps instance animation targets", async () => {
  const outputRoot = path.join(await mkdtemp(path.join(os.tmpdir(), "v3-compile-v2-")), "project");
  const manifest = fixtureManifest("v2 component scene");
  manifest.version = "2.0";
  manifest.compositions[0].overlay_instances = [{instance_id: "headline_01", component_id: "headline_block"}];
  manifest.compositions[0].overlay_ids = ["headline_block"];
  manifest.compositions[0].animations = [{target: "headline_01", preset: "fade", direction: "none", duration_ms: 400, delay_ms: 0}];
  await compileProjectV2({manifest, outputRoot});
  const scene = await readFile(path.join(outputRoot, "compositions", "composition_01.html"), "utf8");
  assert.match(scene, /composition_01_headline_01_headline_block/);
});

test("v2 compiler keeps repeated components distinct by instance animation target", async () => {
  const outputRoot = path.join(await mkdtemp(path.join(os.tmpdir(), "v3-compile-v2-repeat-")), "project");
  const manifest = fixtureManifest("repeated component instances");
  manifest.version = "2.0";
  manifest.compositions[0].overlay_ids = ["headline_block", "headline_block"];
  manifest.compositions[0].overlay_instances = [
    {instance_id: "headline_a", component_id: "headline_block"},
    {instance_id: "headline_b", component_id: "headline_block"},
  ];
  manifest.compositions[0].animations = [
    {target: "headline_a", preset: "fade", direction: "none", duration_ms: 400, delay_ms: 0},
    {target: "headline_b", preset: "scale", direction: "none", duration_ms: 400, delay_ms: 50},
  ];
  await compileProjectV2({manifest, outputRoot});
  const scene = await readFile(path.join(outputRoot, "compositions", "composition_01.html"), "utf8");
  assert.match(scene, /#composition_01_headline_a_headline_block/);
  assert.match(scene, /#composition_01_headline_b_headline_block/);
});

test("v2 compiler renders a real clean_center manifest through the V2 layout dispatcher", async () => {
  const outputRoot = path.join(await mkdtemp(path.join(os.tmpdir(), "v3-compile-v2-layout-")), "project");
  const manifest = fixtureManifest("V2 speaker caption");
  manifest.version = "2.0";
  manifest.output_spec = {ratio: "16:9", width: 1920, height: 1080, fps_num: 30, fps_den: 1};
  manifest.source_video = {path: "media/source.mp4", silent: true};
  manifest.source_segments = [{
    id: "segment_01", source_path: "media/source.mp4", source_start_ms: 0, source_end_ms: 4000,
    output_start_ms: 0, output_end_ms: 4000,
  }];
  manifest.compositions[0] = {
    ...manifest.compositions[0], layout_id: "speaker_fullscreen", layout_variant: "clean_center",
    overlay_ids: ["standard_caption"], overlay_instances: [{instance_id: "caption_01", component_id: "standard_caption"}],
  };

  await compileProjectV2({manifest, outputRoot});
  const scene = await readFile(path.join(outputRoot, "compositions", "composition_01.html"), "utf8");
  assert.match(scene, /data-layout-v2="speaker_fullscreen"/);
  assert.match(scene, /data-layout-variant="clean_center"/);
  assert.match(scene, /data-layout-ratio="16:9"/);
  assert.match(scene, /data-slot="speaker"/);
  assert.match(scene, /src="media\/source\.mp4"/);
  assert.doesNotMatch(scene, /<(?:div|section)[^>]+class="[^"]*\bhf-layout-frame\b/);
  const ids = [...scene.matchAll(/\sid="([^"]+)"/gu)].map((match) => match[1]);
  assert.equal(new Set(ids).size, ids.length, "V2 layout targets must not shadow the composition root");
});

test("v2 routes title and caption overlays into distinct audited safe-area hosts", async () => {
  const outputRoot = path.join(await mkdtemp(path.join(os.tmpdir(), "v3-compile-v2-safe-hosts-")), "project");
  const manifest = fixtureManifest("Safe caption");
  manifest.version = "2.0";
  manifest.output_spec = {ratio: "16:9", width: 1920, height: 1080, fps_num: 30, fps_den: 1};
  manifest.source_video = {path: "media/source.mp4", silent: true};
  manifest.source_segments = [{id: "segment_01", source_path: "media/source.mp4", source_start_ms: 0, source_end_ms: 4000, output_start_ms: 0, output_end_ms: 4000}];
  manifest.compositions[0] = {...manifest.compositions[0], layout_id: "speaker_fullscreen", layout_variant: "headline_top", overlay_ids: ["headline_block", "standard_caption"], overlay_instances: [
    {instance_id: "headline_01", component_id: "headline_block", placement: "safe_top"},
    {instance_id: "caption_01", component_id: "standard_caption", placement: "safe_bottom"},
  ]};

  await compileProjectV2({manifest, outputRoot});
  const scene = await readFile(path.join(outputRoot, "compositions", "composition_01.html"), "utf8");
  assert.match(scene, /data-safe-host="title"[^>]*data-safe-area="16:9"[\s\S]*?headline_block/);
  assert.match(scene, /data-safe-host="captions"[^>]*data-safe-area="16:9"[\s\S]*?standard_caption/);
  assert.doesNotMatch(scene.match(/data-safe-host="title"[\s\S]*?<\/aside>/u)?.[0] ?? "", /standard_caption/);
});

test("v2 safe hosts bind repeated component DOM to the exact overlay instance", async () => {
  const outputRoot = path.join(await mkdtemp(path.join(os.tmpdir(), "v3-compile-v2-repeated-safe-hosts-")), "project");
  const manifest = fixtureManifest("Repeated headline instances");
  manifest.version = "2.0";
  manifest.output_spec = {ratio: "16:9", width: 1920, height: 1080, fps_num: 30, fps_den: 1};
  manifest.source_video = {path: "media/source.mp4", silent: true};
  manifest.source_segments = [{id: "segment_01", source_path: "media/source.mp4", source_start_ms: 0, source_end_ms: 4000, output_start_ms: 0, output_end_ms: 4000}];
  manifest.compositions[0] = {...manifest.compositions[0], layout_id: "speaker_fullscreen", layout_variant: "headline_top", overlay_ids: ["headline_block", "headline_block"], overlay_instances: [
    {instance_id: "headline_top", component_id: "headline_block", placement: "safe_top"},
    {instance_id: "headline_bottom", component_id: "headline_block", placement: "safe_bottom"},
  ], animations: [
    {target: "headline_top", preset: "fade", direction: "none", duration_ms: 400, delay_ms: 0},
    {target: "headline_bottom", preset: "scale", direction: "none", duration_ms: 400, delay_ms: 50},
  ]};

  await compileProjectV2({manifest, outputRoot});
  const scene = await readFile(path.join(outputRoot, "compositions", "composition_01.html"), "utf8");
  const hosts = Object.fromEntries([...scene.matchAll(/<aside\b[^>]*data-safe-host="(title|captions)"[^>]*>([\s\S]*?)<\/aside>/gu)]
    .map((match) => [match[1], match[2]]));
  const topId = "composition_01_headline_top_headline_block";
  const bottomId = "composition_01_headline_bottom_headline_block";

  assert.equal((scene.match(new RegExp(`\\sid="${topId}"`, "gu")) ?? []).length, 1);
  assert.equal((scene.match(new RegExp(`\\sid="${bottomId}"`, "gu")) ?? []).length, 1);
  assert.match(hosts.title, new RegExp(`id="${topId}"`, "u"));
  assert.doesNotMatch(hosts.title, new RegExp(`id="${bottomId}"`, "u"));
  assert.match(hosts.captions, new RegExp(`id="${bottomId}"`, "u"));
  assert.doesNotMatch(hosts.captions, new RegExp(`id="${topId}"`, "u"));
  assert.match(scene, new RegExp(`tl\\.fromTo\\("#${topId}"`, "u"));
  assert.match(scene, new RegExp(`tl\\.fromTo\\("#${bottomId}"`, "u"));
});

test("v2 compiler hydrates compiled steps text from parent safe-text nodes", async () => {
  const outputRoot = path.join(await mkdtemp(path.join(os.tmpdir(), "v3-compile-v2-steps-")), "project");
  const manifest = fixtureManifest("Prepare");
  manifest.version = "2.0";
  manifest.compositions[0] = {
    ...manifest.compositions[0], layout_id: "steps_stack", layout_variant: "numbered_cards",
    overlay_ids: ["standard_caption"], overlay_instances: [{instance_id: "caption_01", component_id: "standard_caption"}],
  };
  manifest.captions = [
    {id: "caption_01", start_ms: 0, end_ms: 1333, text: "Prepare"},
    {id: "caption_02", start_ms: 1333, end_ms: 2666, text: "Execute"},
    {id: "caption_03", start_ms: 2666, end_ms: 4000, text: "Review"},
  ];

  await compileProjectV2({manifest, outputRoot});
  const scene = await readFile(path.join(outputRoot, "compositions", "composition_01.html"), "utf8");
  const hydrated = hydrateCompiledStepText(scene);
  assert.deepEqual(hydrated, ["Prepare", "Execute", "Review"]);
});

test("v2 speaker uses the intersecting source segment once with local and media offsets", async () => {
  const outputRoot = path.join(await mkdtemp(path.join(os.tmpdir(), "v3-compile-v2-offset-")), "project");
  const manifest = fixtureManifest("First");
  manifest.version = "2.0";
  manifest.output_spec = {ratio: "16:9", width: 1920, height: 1080, fps_num: 30, fps_den: 1};
  manifest.duration_ms = 8000;
  manifest.source_video = {path: "media/source.mp4", silent: true};
  manifest.source_segments = [{id: "segment_01", source_path: "media/source.mp4", source_start_ms: 700, source_end_ms: 6700, output_start_ms: 1000, output_end_ms: 7000}];
  manifest.compositions = [
    {...manifest.compositions[0], id: "composition_01", start_ms: 0, end_ms: 4000, layout_id: "steps_stack", layout_variant: "vertical_steps", overlay_instances: [{instance_id: "caption_01", component_id: "standard_caption"}]},
    {...manifest.compositions[0], id: "composition_02", start_ms: 4000, end_ms: 8000, layout_id: "speaker_fullscreen", layout_variant: "clean_center", overlay_instances: [{instance_id: "caption_02", component_id: "standard_caption"}]},
  ];
  manifest.compositions.forEach((composition) => composition.overlay_ids = ["standard_caption"]);
  manifest.captions = [{id: "caption_01", start_ms: 0, end_ms: 4000, text: "First"}, {id: "caption_02", start_ms: 4000, end_ms: 8000, text: "Second"}];

  await compileProjectV2({manifest, outputRoot});
  const scene = await readFile(path.join(outputRoot, "compositions", "composition_02.html"), "utf8");
  assert.match(scene, /data-slot="speaker"[\s\S]*?<video[^>]+data-start="0"[^>]+data-duration="3"[^>]+data-playback-start="3\.7"/);
  assert.equal((scene.match(/src="media\/source\.mp4"/g) ?? []).length, 1, "V2 speaker does not append a second legacy source video");
  assert.match(scene, /width:820px;height:650px/);
  assert.match(scene, /hf-v2-speaker>video\{width:100%;height:100%;object-fit:var\(--hf-image-fit\)/);
});

test("v2 speaker preserves every intersecting source clip in composition order", async () => {
  const outputRoot = path.join(await mkdtemp(path.join(os.tmpdir(), "v3-compile-v2-multiclip-")), "project");
  const manifest = fixtureManifest("Two source clips");
  manifest.version = "2.0";
  manifest.output_spec = {ratio: "16:9", width: 1920, height: 1080, fps_num: 30, fps_den: 1};
  manifest.source_video = {path: "media/source.mp4", silent: true};
  manifest.source_segments = [
    {id: "segment_01", source_path: "media/source.mp4", source_start_ms: 100, source_end_ms: 2600, output_start_ms: 500, output_end_ms: 3000},
    {id: "segment_02", source_path: "media/source.mp4", source_start_ms: 300, source_end_ms: 2300, output_start_ms: 3000, output_end_ms: 5000},
  ];
  manifest.duration_ms = 5000;
  manifest.compositions = [
    {...manifest.compositions[0], id: "composition_01", start_ms: 0, end_ms: 1000, layout_id: "steps_stack", layout_variant: "vertical_steps", overlay_instances: [{instance_id: "caption_01", component_id: "standard_caption"}]},
    {...manifest.compositions[0], id: "composition_02", start_ms: 1000, end_ms: 5000, layout_id: "speaker_fullscreen", layout_variant: "clean_center", overlay_instances: [{instance_id: "caption_02", component_id: "standard_caption"}]},
  ];
  manifest.compositions.forEach((composition) => composition.overlay_ids = ["standard_caption"]);
  manifest.captions = [{id: "caption_01", start_ms: 0, end_ms: 1000, text: "Intro"}, {id: "caption_02", start_ms: 1000, end_ms: 5000, text: "Two source clips"}];

  await compileProjectV2({manifest, outputRoot});
  const scene = await readFile(path.join(outputRoot, "compositions", "composition_02.html"), "utf8");
  const speaker = scene.match(/data-slot="speaker"[\s\S]*?<\/div>/u)?.[0] ?? "";
  assert.equal((speaker.match(/<video\b/g) ?? []).length, 2);
  assert.equal((scene.match(/src="media\/source\.mp4"/g) ?? []).length, 2, "only speaker-host clips render source media");
  assert.match(speaker, /id="composition_02_speaker_clip_0"[^>]+data-start="0"[^>]+data-duration="2"[^>]+data-playback-start="0\.6"/);
  assert.match(speaker, /id="composition_02_speaker_clip_1"[^>]+data-start="2"[^>]+data-duration="2"[^>]+data-playback-start="0\.3"/);
});

test("v2 speaker-side and material-pip layouts preserve source clips and consume semantic bindings", async () => {
  const cases = [
    {layoutId: "speaker_left_info_right", variantId: "card_stack", bindings: [{slot_id: "evidence", asset_id: "evidence_asset"}], expectedSlots: ["speaker", "evidence"]},
    {layoutId: "speaker_right_evidence_left", variantId: "document_panel", bindings: [{slot_id: "evidence", asset_id: "evidence_asset"}], expectedSlots: ["speaker", "evidence"]},
    {layoutId: "material_fullscreen_speaker_pip", variantId: "pip_round", bindings: [{slot_id: "primary", asset_id: "primary_asset"}, {slot_id: "detail", asset_id: "evidence_asset"}], expectedSlots: ["speaker", "primary", "detail"]},
  ];
  for (const item of cases) {
    const outputRoot = path.join(await mkdtemp(path.join(os.tmpdir(), `v3-${item.layoutId}-`)), "project");
    const manifest = fixtureManifest("semantic speaker layout");
    manifest.version = "2.0";
    manifest.source_video = {path: "media/source.mp4", silent: true};
    manifest.source_segments = [
      {id: "segment_01", source_path: "media/source.mp4", source_start_ms: 400, source_end_ms: 2400, output_start_ms: 500, output_end_ms: 2500},
      {id: "segment_02", source_path: "media/source.mp4", source_start_ms: 900, source_end_ms: 2400, output_start_ms: 2500, output_end_ms: 4000},
    ];
    manifest.assets = [
      {id: "primary_asset", kind: "image", path: "media/primary.png"},
      {id: "evidence_asset", kind: "image", path: "media/evidence.png"},
    ];
    manifest.compositions[0] = {
      ...manifest.compositions[0], start_ms: 0, end_ms: 4000,
      layout_id: item.layoutId, layout_variant: item.variantId,
      asset_ids: item.bindings.map(({asset_id}) => asset_id), layout_slot_bindings: item.bindings,
      overlay_instances: [{instance_id: "caption_01", component_id: "standard_caption"}],
    };
    await compileProjectV2({manifest, outputRoot});
    const scene = await readFile(path.join(outputRoot, "compositions", "composition_01.html"), "utf8");
    assert.equal((scene.match(/src="media\/source\.mp4"/gu) ?? []).length, 2, `${item.layoutId} preserves both source intersections`);
    assert.match(scene, /data-playback-start="0\.4"/u);
    assert.match(scene, /data-playback-start="0\.9"/u);
    for (const slot of item.expectedSlots) assert.match(scene, new RegExp(`data-slot="${slot}"`), `${item.layoutId} consumes ${slot}`);
    for (const {asset_id} of item.bindings) {
      const asset = manifest.assets.find(({id}) => id === asset_id);
      assert.match(scene, new RegExp(`src="${asset.path.replaceAll("/", "\\/")}"`), `${item.layoutId} renders bound ${asset_id}`);
    }
  }
});

test("v2 editorial proof method and CTA layouts consume bindings and authoritative captions end to end", async () => {
  const cases = [
    {layoutId: "editorial_collage", variantId: "magazine_grid", bindings: [{slot_id: "primary", asset_id: "primary_asset"}, {slot_id: "detail", asset_id: "detail_asset"}], paths: ["primary.png", "detail.png"], text: "权威编辑文案"},
    {layoutId: "comparison_split", variantId: "vertical_divide", bindings: [{slot_id: "primary", asset_id: "primary_asset"}, {slot_id: "detail", asset_id: "detail_asset"}], paths: ["primary.png", "detail.png"], text: "权威对比文案"},
    {layoutId: "number_proof", variantId: "hero_number", bindings: [{slot_id: "evidence", asset_id: "detail_asset"}], paths: ["detail.png"], text: "权威数据 38%"},
    {layoutId: "quote_reversal", variantId: "diagonal_statement", bindings: [{slot_id: "evidence", asset_id: "detail_asset"}], paths: ["detail.png"], text: "权威观点文案"},
    {layoutId: "method_timeline", variantId: "horizontal_timeline", bindings: [{slot_id: "accent", asset_id: "detail_asset"}], paths: ["detail.png"], text: "权威方法步骤"},
    {layoutId: "cta_offer", variantId: "offer_card", bindings: [{slot_id: "accent", asset_id: "detail_asset"}], paths: ["detail.png"], text: "权威行动文案"},
  ];
  for (const item of cases) {
    const outputRoot = path.join(await mkdtemp(path.join(os.tmpdir(), `v3-${item.layoutId}-e2e-`)), "project");
    const manifest = fixtureManifest(item.text);
    manifest.version = "2.0";
    manifest.assets = [
      {id: "primary_asset", kind: "image", path: "media/primary.png"},
      {id: "detail_asset", kind: "image", path: "media/detail.png"},
    ];
    manifest.compositions[0] = {
      ...manifest.compositions[0], layout_id: item.layoutId, layout_variant: item.variantId,
      asset_ids: item.bindings.map(({asset_id}) => asset_id), layout_slot_bindings: item.bindings,
      overlay_instances: [{instance_id: "caption_01", component_id: "standard_caption"}],
    };
    await compileProjectV2({manifest, outputRoot});
    const scene = await readFile(path.join(outputRoot, "compositions", "composition_01.html"), "utf8");
    for (const mediaPath of item.paths) assert.match(scene, new RegExp(`src="media\\/${mediaPath}"`), `${item.layoutId} consumes ${mediaPath}`);
    if (!["editorial_collage", "comparison_split"].includes(item.layoutId)) assert.match(scene, new RegExp(`data-safe-text="${item.text}"`), `${item.layoutId} renders authoritative caption text`);
  }

  for (const layoutId of ["number_proof", "quote_reversal", "method_timeline", "cta_offer"]) {
    const manifest = fixtureManifest("removed");
    manifest.version = "2.0";
    manifest.captions = [];
    manifest.compositions[0] = {...manifest.compositions[0], layout_id: layoutId, layout_variant: {
      number_proof: "hero_number", quote_reversal: "diagonal_statement", method_timeline: "horizontal_timeline", cta_offer: "offer_card",
    }[layoutId], overlay_instances: [{instance_id: "caption_01", component_id: "standard_caption"}]};
    await assert.rejects(compileProjectV2({manifest, outputRoot: path.join(await mkdtemp(path.join(os.tmpdir(), `v3-${layoutId}-empty-`)), "project")}), /layout_required_slot_missing/);
  }

  const unconsumed = fixtureManifest("权威行动文案");
  unconsumed.version = "2.0";
  unconsumed.assets = [{id: "detail_asset", kind: "image", path: "media/detail.png"}];
  unconsumed.compositions[0] = {
    ...unconsumed.compositions[0], layout_id: "cta_offer", layout_variant: "offer_card",
    asset_ids: ["detail_asset"], layout_slot_bindings: [{slot_id: "evidence", asset_id: "detail_asset"}],
    overlay_instances: [{instance_id: "caption_01", component_id: "standard_caption"}],
  };
  await assert.rejects(compileProjectV2({manifest: unconsumed, outputRoot: path.join(await mkdtemp(path.join(os.tmpdir(), "v3-cta-unconsumed-")), "project")}), /layout_slot_binding_unconsumed/);
});

test("v2 product binds primary only through explicit layout slot bindings", async () => {
  const outputRoot = path.join(await mkdtemp(path.join(os.tmpdir(), "v3-compile-v2-bindings-")), "project");
  const manifest = fixtureManifest("Product");
  manifest.version = "2.0";
  manifest.assets = [
    {id: "evidence_asset", kind: "image", path: "media/evidence.png"},
    {id: "primary_asset", kind: "image", path: "media/primary.png"},
  ];
  manifest.compositions[0] = {...manifest.compositions[0], layout_id: "product_hero", layout_variant: "center_pedestal", asset_ids: ["evidence_asset", "primary_asset"], layout_slot_bindings: [{slot_id: "primary", asset_id: "primary_asset"}, {slot_id: "detail", asset_id: "evidence_asset"}], overlay_instances: [{instance_id: "caption_01", component_id: "standard_caption"}]};

  await compileProjectV2({manifest, outputRoot});
  const scene = await readFile(path.join(outputRoot, "compositions", "composition_01.html"), "utf8");
  assert.match(scene, /data-slot="primary"[^>]*><img alt="" src="media\/primary\.png"/);
  assert.match(scene, /data-slot="detail"[^>]*><img alt="" src="media\/evidence\.png"/);
  const missing = structuredClone(manifest);
  missing.compositions[0].layout_slot_bindings = [{slot_id: "evidence", asset_id: "evidence_asset"}];
  await assert.rejects(compileProjectV2({manifest: missing, outputRoot: path.join(await mkdtemp(path.join(os.tmpdir(), "v3-compile-v2-no-primary-")), "project")}), /layout_required_slot_missing/);
  const impersonating = structuredClone(manifest);
  impersonating.compositions[0].layout_slot_bindings = [{slot_id: "primary", asset_id: "primary_asset"}, {slot_id: "detail", asset_id: "primary_asset"}];
  await assert.rejects(compileProjectV2({manifest: impersonating, outputRoot: path.join(await mkdtemp(path.join(os.tmpdir(), "v3-compile-v2-duplicate-primary-")), "project")}), /layout_slot_identity_invalid/);
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

test("compiler renders one material as one column and captions on their own time ranges", async () => {
  const outputRoot = path.join(await mkdtemp(path.join(os.tmpdir(), "v3-timed-captions-")), "project");
  const manifest = fixtureManifest("First caption");
  manifest.assets = [{id: "material_01", kind: "image", path: "media/material-01.png"}];
  manifest.compositions[0].layout_id = "editorial_collage";
  manifest.compositions[0].asset_ids = ["material_01"];
  manifest.compositions[0].animations = [{
    target: "standard_caption", preset: "subtitle_pop", direction: "up", duration_ms: 280, delay_ms: 500,
  }];
  manifest.captions = [
    {id: "caption_01", start_ms: 0, end_ms: 1800, text: "First caption"},
    {id: "caption_02", start_ms: 1800, end_ms: 3900, text: "Second caption"},
    {id: "caption_03", start_ms: 3900, end_ms: 4000, text: "Tail caption"},
  ];

  await compileProject({manifest, outputRoot});
  const scene = await readFile(path.join(outputRoot, "compositions", "composition_01.html"), "utf8");

  assert.match(scene, /class="hf-materials hf-material-count-1" style="grid-template-columns:1fr"/);
  assert.match(scene, /data-safe-text="First caption"[^>]+data-start="0"[^>]+data-duration="1\.8"/);
  assert.match(scene, /data-safe-text="Second caption"[^>]+data-start="1\.8"[^>]+data-duration="2\.1"/);
  assert.match(scene, /data-safe-text="Tail caption"[^>]+data-start="3\.9"[^>]+data-duration="0\.1"/);
  assert.doesNotMatch(scene, /data-safe-text="First caption Second caption"/);
  assert.match(scene, /tl\.fromTo\("#composition_01_caption_1_standard_caption"[\s\S]+,0\.5\);/);
  assert.match(scene, /tl\.fromTo\("#composition_01_caption_2_standard_caption"[\s\S]+,2\.3\);/);
  assert.match(scene, /tl\.fromTo\("#composition_01_caption_3_standard_caption"[\s\S]+"duration":0\.1[\s\S]+,3\.9\);/);
  assert.doesNotMatch(scene, /tl\.fromTo\("#composition_01_root \.hf-overlay-standard_caption"/);
});

test("compiler skips sub-frame caption animation without shifting later caption ids", async () => {
  const outputRoot = path.join(await mkdtemp(path.join(os.tmpdir(), "v3-short-caption-")), "project");
  const manifest = fixtureManifest("Short");
  manifest.compositions[0].animations = [{
    target: "standard_caption", preset: "subtitle_pop", direction: "up", duration_ms: 280, delay_ms: 0,
  }];
  manifest.captions = [
    {id: "caption_01", start_ms: 0, end_ms: 10, text: "Short"},
    {id: "caption_02", start_ms: 10, end_ms: 4000, text: "Visible"},
  ];

  await compileProject({manifest, outputRoot});
  const scene = await readFile(path.join(outputRoot, "compositions", "composition_01.html"), "utf8");

  assert.doesNotMatch(scene, /tl\.fromTo\("#composition_01_caption_1_standard_caption"/);
  assert.match(scene, /tl\.fromTo\("#composition_01_caption_2_standard_caption"/);
});

test("compiler raises source video above fullscreen material for speaker pip", async () => {
  const outputRoot = path.join(await mkdtemp(path.join(os.tmpdir(), "v3-speaker-pip-")), "project");
  const manifest = fixtureManifest("Speaker remains visible");
  manifest.source_video = {path: "media/source.mp4", silent: true};
  manifest.source_segments = [{
    id: "segment_01", source_path: "media/source.mp4", source_start_ms: 0, source_end_ms: 4000,
    output_start_ms: 0, output_end_ms: 4000,
  }];
  manifest.assets = [{id: "material_01", kind: "image", path: "media/material-01.png"}];
  manifest.compositions[0].layout_id = "material_fullscreen_speaker_pip";
  manifest.compositions[0].asset_ids = ["material_01"];

  await compileProject({manifest, outputRoot});
  const scene = await readFile(path.join(outputRoot, "compositions", "composition_01.html"), "utf8");

  assert.match(
    scene,
    /class="hf-source-video hf-source-video-pip clip"[^>]+style="inset:auto 7\.85% 8\.8% auto;width:26\.6%;height:30\.6%;z-index:3;border-radius:var\(--hf-radius\)"/,
  );
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

function hydrateCompiledStepText(scene) {
  const nodes = [...scene.matchAll(/<li[^>]+data-safe-text="([^"]+)"[^>]*><span><\/span><\/li>/gu)].map((match) => {
    const span = {textContent: ""};
    return {dataset: {safeText: match[1]}, querySelector: (selector) => selector === "span" ? span : null, span};
  });
  assert.ok(nodes.length > 0, "compiled steps must put data-safe-text on a parent with a child span");
  const loop = scene.match(/for\(const node of root\.querySelectorAll\('\[data-safe-text\]'\)\)node\.querySelector\('span'\)\.textContent=node\.dataset\.safeText;/u)?.[0];
  assert.ok(loop, "compiled scene contains the safe-text hydration loop");
  new Function("root", loop)({querySelectorAll: () => nodes});
  return nodes.map(({span}) => span.textContent);
}
