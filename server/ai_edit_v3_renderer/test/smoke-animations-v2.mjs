import {mkdir, mkdtemp, readFile, writeFile} from "node:fs/promises";
import os from "node:os";
import path from "node:path";

import {compileProjectV2} from "../src/compile-project-v2.mjs";
import {getRegistrySha256} from "../src/registry/index.mjs";
import {renderHyperframes} from "../src/render-hyperframes.mjs";

const chromiumPath = process.env.V3_SMOKE_CHROMIUM;
if (!chromiumPath) throw new Error("smoke_chromium_path_missing");

const fixturePath = new URL("../../../tests/fixtures/ai_edit_v3/valid-render-manifest-v2.json", import.meta.url);
const manifest = structuredClone(JSON.parse(new TextDecoder("utf-8", {fatal: true}).decode(await readFile(fixturePath))));
manifest.registry_sha256 = getRegistrySha256();
manifest.duration_ms = 6000;
manifest.output_spec = {...manifest.output_spec, ratio: "9:16", width: 1080, height: 1920};
manifest.source_video = null;
manifest.source_segments[0] = {...manifest.source_segments[0], source_end_ms: 6000, output_end_ms: 6000};
manifest.master_audio = {...manifest.master_audio, duration_ms: 6000};
manifest.captions = [
  {id: "caption_01", start_ms: 0, end_ms: 1500, text: "Seek safe motion"},
  {id: "caption_02", start_ms: 1500, end_ms: 3000, text: "Matched card transition"},
  {id: "caption_03", start_ms: 3000, end_ms: 4500, text: "Shared flash layer"},
  {id: "caption_04", start_ms: 4500, end_ms: 6000, text: "Deterministic replay"},
];
const authority = {
  headline: {text: "Seek safe motion", source_caption_ids: ["caption_01"]},
  highlight: {text: "Matched card transition", source_caption_ids: ["caption_01"]},
};
const base = manifest.compositions[0];
manifest.compositions = [
  {
    ...structuredClone(base), id: "composition_01", scene_id: "scene_01", start_ms: 0, end_ms: 1500,
    layout_id: "product_hero", layout_variant: "center_pedestal", authoritative_content: authority,
    overlay_ids: ["headline_block", "headline_block"],
    overlay_instances: [
      {instance_id: "wipe_01", component_id: "headline_block", content_ref: "headline", placement: "title_safe"},
      {instance_id: "sweep_01", component_id: "headline_block", content_ref: "highlight", placement: "title_safe"},
    ],
    animations: [
      {target: "wipe_01", preset: "wipe", direction: "right", duration_ms: 600, delay_ms: 0},
      {target: "sweep_01", preset: "light_sweep", direction: "right", duration_ms: 600, delay_ms: 100},
    ],
    transition: "hard_cut", asset_ids: ["asset_01"], layout_slot_bindings: [{slot_id: "primary", asset_id: "asset_01"}],
  },
  {
    ...structuredClone(base), id: "composition_02", scene_id: "scene_02", start_ms: 1500, end_ms: 3000,
    layout_id: "product_hero", layout_variant: "detail_gallery", authoritative_content: authority,
    overlay_ids: ["emphasis_caption"],
    overlay_instances: [{instance_id: "subtitle_01", component_id: "emphasis_caption", content_ref: "highlight", placement: "subtitle_safe"}],
    animations: [{target: "subtitle_01", preset: "subtitle_pop", direction: "up", duration_ms: 600, delay_ms: 0}],
    transition: "card_match_cut", asset_ids: ["asset_01"], layout_slot_bindings: [{slot_id: "primary", asset_id: "asset_01"}],
  },
  {
    ...structuredClone(base), id: "composition_03", scene_id: "scene_03", start_ms: 3000, end_ms: 4500,
    layout_id: "product_hero", layout_variant: "split_copy", authoritative_content: authority,
    overlay_ids: ["headline_block"],
    overlay_instances: [{instance_id: "flash_title_01", component_id: "headline_block", content_ref: "headline", placement: "title_safe"}],
    animations: [{target: "flash_title_01", preset: "scale", direction: "none", duration_ms: 500, delay_ms: 0}],
    transition: "light_flash", asset_ids: ["asset_01"], layout_slot_bindings: [{slot_id: "primary", asset_id: "asset_01"}],
  },
  {
    ...structuredClone(base), id: "composition_04", scene_id: "scene_04", start_ms: 4500, end_ms: 6000,
    layout_id: "product_hero", layout_variant: "center_pedestal", authoritative_content: authority,
    overlay_ids: ["emphasis_caption"],
    overlay_instances: [{instance_id: "flash_caption_01", component_id: "emphasis_caption", content_ref: "highlight", placement: "subtitle_safe"}],
    animations: [{target: "flash_caption_01", preset: "highlight_draw", direction: "right", duration_ms: 500, delay_ms: 0}],
    transition: "light_flash", asset_ids: ["asset_01"], layout_slot_bindings: [{slot_id: "primary", asset_id: "asset_01"}],
  },
];

const root = await mkdtemp(path.join(os.tmpdir(), "v3-motion-browser-smoke-"));
const projectRoot = path.join(root, "project");
const compiled = await compileProjectV2({manifest, outputRoot: projectRoot});
const indexHtml = await readFile(path.join(projectRoot, "index.html"), "utf8");
const flashLayerMatches = indexHtml.match(/id="transition_flash_global"/g) ?? [];
const flashCalls = indexHtml.match(/"#transition_flash_global"/g) ?? [];
if (flashLayerMatches.length !== 1 || flashCalls.length !== 4) throw new Error("motion_smoke_flash_layer_reuse_failed");
if (!indexHtml.includes("data-transition-audit=\"card_match_cut:matched:primary:asset_01\"")) throw new Error("motion_smoke_card_match_missing");
await mkdir(path.join(projectRoot, "media"), {recursive: true});
await writeFile(path.join(projectRoot, "media", "image.png"), Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M/wHwAF/gL+XvN1WQAAAABJRU5ErkJggg==", "base64"));
const outputPath = path.join(root, "motion-smoke.mp4");
const execution = await renderHyperframes({
  projectRoot, outputPath, chromiumPath, timeoutMs: 120_000, environment: process.env,
});
const snapshotHashes = execution.snapshots.map((item) => item.sha256);
if (snapshotHashes.length < 2 || new Set(snapshotHashes).size < 2) throw new Error("motion_smoke_frames_static");
process.stdout.write(`${JSON.stringify({
  outputPath: execution.outputPath, outputSha256: execution.outputSha256, outputSize: execution.outputSize,
  snapshotCount: snapshotHashes.length, uniqueSnapshotCount: new Set(snapshotHashes).size,
  snapshotSha256: snapshotHashes, compiledSnapshotsMs: compiled.snapshotTimesMs,
  flashLayerCount: flashLayerMatches.length, flashOperationCount: flashCalls.length,
  cardMatchCompiled: true,
})}\n`);
