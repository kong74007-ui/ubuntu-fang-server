import {mkdir, rm} from "node:fs/promises";
import path from "node:path";
import {fileURLToPath} from "node:url";

import {compileProject} from "../src/compile-project.mjs";
import {getRegistrySha256} from "../src/registry/index.mjs";
import {LAYOUT_CONTRACTS} from "../src/registry/layouts.mjs";

const root = path.dirname(fileURLToPath(import.meta.url));
await mkdir(path.join(root, "fixtures"), {recursive: true});
for (const ratio of ["16:9", "9:16"]) {
  const name = ratio === "16:9" ? "landscape" : "portrait";
  const outputRoot = path.join(root, "fixtures", name);
  await rm(outputRoot, {recursive: true, force: true});
  await compileProject({manifest: fixtureManifest(ratio), outputRoot});
}
for (const name of ["animations", "transitions"]) {
  const outputRoot = path.join(root, "fixtures", name);
  await rm(outputRoot, {recursive: true, force: true});
  await compileProject({manifest: motionManifest(name), outputRoot});
}

function fixtureManifest(ratio) {
  const output = ratio === "16:9" ? {width: 1920, height: 1080} : {width: 1080, height: 1920};
  const combinations = LAYOUT_CONTRACTS.flatMap(({id}) => ["balanced_a", "emphasis_b"].map((variant) => ({id, variant})));
  const captions = [];
  const compositions = combinations.map(({id, variant}, index) => {
    const start = index * 3000;
    const sceneId = `scene_${String(index + 1).padStart(2, "0")}`;
    captions.push({
      id: `caption_${String(index + 1).padStart(2, "0")}`,
      start_ms: start,
      end_ms: start + 3000,
      text: index % 3 === 0 ? "真实产品与门店素材，建立清晰可信的商业表达" : index % 3 === 1 ? "AI Edit V3 重点数字 98.6%" : "从问题到方法，再到明确行动",
    });
    const overlay = index % 4 === 0 ? "headline_block" : index % 4 === 1 ? "number_proof" : index % 4 === 2 ? "info_card" : "standard_caption";
    return {
      id: sceneId, scene_id: sceneId, start_ms: start, end_ms: start + 3000,
      layout_id: id, layout_variant: variant, overlay_ids: [overlay],
      animations: [{target: overlay, preset: index % 2 === 0 ? "fade" : "card_reveal", direction: "none", duration_ms: 500, delay_ms: 100}],
      transition: ["hard_cut", "soft_wipe", "directional_slide", "light_flash", "card_match_cut"][index % 5],
      asset_ids: [],
    };
  });
  return {
    registry_sha256: getRegistrySha256(), duration_ms: compositions.at(-1).end_ms,
    output_spec: {ratio, ...output, fps_num: 30, fps_den: 1},
    theme: {palette_id: "midnight_gold", typography_id: "editorial_sans", density: "balanced", motion_energy: "medium", image_fit: "cover"},
    source_video: null, assets: [], compositions, captions,
  };
}

function motionManifest(kind) {
  const animations = [
    "card_reveal", "count_up", "fade", "highlight_draw", "image_pan_zoom", "light_sweep", "rotate",
    "scale", "slide", "split_screen", "stagger", "stamp", "subtitle_pop", "wipe",
  ];
  const transitions = ["card_match_cut", "directional_slide", "hard_cut", "light_flash", "soft_wipe"];
  const count = kind === "animations" ? animations.length : transitions.length;
  const captions = [];
  const compositions = Array.from({length: count}, (_, index) => {
    const start = index * 2000;
    const id = `${kind}_${String(index + 1).padStart(2, "0")}`;
    captions.push({id: `caption_${String(index + 1).padStart(2, "0")}`, start_ms: start, end_ms: start + 2000, text: kind === "animations" ? `动画 ${animations[index]}` : `转场 ${transitions[index]}`});
    return {
      id, scene_id: id, start_ms: start, end_ms: start + 2000,
      layout_id: index % 2 ? "editorial_collage" : "speaker_left_info_right", layout_variant: index % 2 ? "emphasis_b" : "balanced_a",
      overlay_ids: ["standard_caption"],
      animations: kind === "animations" ? [{target: "standard_caption", preset: animations[index], direction: "none", duration_ms: 500, delay_ms: 120}] : [],
      transition: kind === "transitions" ? transitions[index] : "hard_cut",
      asset_ids: [],
    };
  });
  return {
    registry_sha256: getRegistrySha256(), duration_ms: compositions.at(-1).end_ms,
    output_spec: {ratio: "16:9", width: 1920, height: 1080, fps_num: 30, fps_den: 1},
    theme: {palette_id: "midnight_gold", typography_id: "editorial_sans", density: "balanced", motion_energy: "medium", image_fit: "cover"},
    source_video: null, assets: [], compositions, captions,
  };
}
