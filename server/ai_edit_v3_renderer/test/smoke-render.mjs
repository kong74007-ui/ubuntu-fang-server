import os from "node:os";
import path from "node:path";
import {copyFile, mkdir, mkdtemp} from "node:fs/promises";

import {compileProject} from "../src/compile-project.mjs";
import {getRegistrySha256} from "../src/registry/index.mjs";
import {renderHyperframes} from "../src/render-hyperframes.mjs";

const chromiumPath = process.env.PUPPETEER_EXECUTABLE_PATH;
if (!chromiumPath) throw new Error("smoke_chromium_path_missing");
const root = await mkdtemp(path.join(os.tmpdir(), "v3-hf-smoke-"));
const projectRoot = path.join(root, "project");
const sourceVideo = process.env.SMOKE_SOURCE_VIDEO;
const ratio = sourceVideo ? "9:16" : "16:9";
const manifest = {
  registry_sha256: getRegistrySha256(), duration_ms: 1000,
  output_spec: {ratio, width: ratio === "9:16" ? 1080 : 1920, height: ratio === "9:16" ? 1920 : 1080, fps_num: 30, fps_den: 1},
  theme: {palette_id: "midnight_gold", typography_id: "editorial_sans", density: "balanced", motion_energy: "medium", image_fit: "cover"},
  source_video: sourceVideo ? {path: "media/source.mp4", silent: true} : null,
  source_segments: sourceVideo ? [{id: "segment_01", source_path: "media/source.mp4", source_start_ms: 0, source_end_ms: 1000, output_start_ms: 0, output_end_ms: 1000}] : [], assets: [],
  compositions: [{
    id: "scene_01", scene_id: "scene_01", start_ms: 0, end_ms: 1000,
    layout_id: "speaker_fullscreen", layout_variant: "balanced_a",
    overlay_ids: ["headline_block"], animations: [{target: "headline_block", preset: "fade", direction: "none", duration_ms: 300, delay_ms: 0}],
    transition: "hard_cut", asset_ids: [],
  }],
  captions: [{id: "caption_01", start_ms: 0, end_ms: 1000, text: "AI 智能剪辑 V3"}],
};
await compileProject({manifest, outputRoot: projectRoot});
if (sourceVideo) {
  await mkdir(path.join(projectRoot, "media"), {recursive: true});
  await copyFile(sourceVideo, path.join(projectRoot, "media", "source.mp4"));
}
const outputPath = path.join(root, "silent.mp4");
const result = await renderHyperframes({projectRoot, outputPath, chromiumPath, timeoutMs: 120_000, environment: process.env});
process.stdout.write(`${JSON.stringify({outputPath, sha256: result.outputSha256, size: result.outputSize, snapshots: result.snapshots.length, elapsedMs: result.elapsedMs})}\n`);
