import {copyFile, mkdir, writeFile} from "node:fs/promises";
import path from "node:path";
import {fileURLToPath} from "node:url";

import {assertSafeId, assertSafeText, seconds} from "./registry/layout-primitives.mjs";
import {compileOverlay} from "./registry/overlays.mjs";
import {getRegistrySha256, resolveLayout, resolveOverlay, resolveTheme} from "./registry/index.mjs";

const MODULE_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

export async function compileProject({manifest, outputRoot}) {
  assertManifestShape(manifest);
  const registrySha256 = getRegistrySha256();
  const suppliedRegistry = manifest.registry_sha256;
  if (suppliedRegistry !== registrySha256 && suppliedRegistry !== registrySha256.slice(7)) {
    throw new Error("registry_sha256_mismatch");
  }
  const theme = resolveTheme(manifest.theme);
  const projectRoot = path.resolve(outputRoot);
  await mkdir(projectRoot, {recursive: false});
  await mkdir(path.join(projectRoot, "compositions"));
  await mkdir(path.join(projectRoot, "vendor"));
  await mkdir(path.join(projectRoot, "assets", "fonts"), {recursive: true});
  await copyRuntime(projectRoot);

  const compositionIds = ["main"];
  const snapshotTimes = new Set([0, manifest.duration_ms]);
  for (const composition of manifest.compositions) {
    const compositionId = assertSafeId(composition.id, "composition_id");
    if (compositionIds.includes(compositionId)) throw new Error("composition_id_duplicate");
    compositionIds.push(compositionId);
    snapshotTimes.add(composition.start_ms);
    snapshotTimes.add(Math.floor((composition.start_ms + composition.end_ms) / 2));
    const sceneHtml = compileScene({manifest, composition, theme});
    await writeFile(path.join(projectRoot, "compositions", `${compositionId}.html`), sceneHtml, {encoding: "utf8", flag: "wx"});
  }
  const entry = compileIndex({manifest, compositionIds});
  await writeFile(path.join(projectRoot, "index.html"), entry, {encoding: "utf8", flag: "wx"});
  const expectedFrames = Math.ceil(manifest.duration_ms * manifest.output_spec.fps_num / manifest.output_spec.fps_den / 1000);
  return Object.freeze({
    projectRoot,
    entryRelativePath: "index.html",
    compositionIds: Object.freeze(compositionIds),
    registrySha256,
    expectedFrames,
    snapshotTimesMs: Object.freeze([...snapshotTimes].sort((a, b) => a - b)),
  });
}

function compileIndex({manifest, compositionIds}) {
  const {width, height} = manifest.output_spec;
  const duration = seconds(manifest.duration_ms);
  const hosts = manifest.compositions.map((composition, index) => {
    const start = seconds(composition.start_ms);
    const sceneDuration = seconds(composition.end_ms - composition.start_ms);
    return `<div data-composition-id="${composition.id}" data-composition-src="compositions/${composition.id}.html" data-start="${start}" data-duration="${sceneDuration}" data-track-index="${index}"></div>`;
  }).join("");
  return `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>@font-face{font-family:"Noto Sans SC";src:url("assets/fonts/NotoSansSC-Regular.woff2") format("woff2");font-weight:400}@font-face{font-family:"Noto Sans SC";src:url("assets/fonts/NotoSansSC-Bold.woff2") format("woff2");font-weight:700}html,body{margin:0;background:transparent;overflow:hidden}#main{position:relative;overflow:hidden}</style></head><body><div id="main" data-composition-id="main" data-width="${width}" data-height="${height}" data-duration="${duration}">${hosts}</div><script src="vendor/gsap.min.js"></script><script>window.__timelines=window.__timelines||{};const tl=gsap.timeline({paused:true});window.__timelines["main"]=tl;Object.freeze(${JSON.stringify(compositionIds)});</script></body></html>`;
}

function compileScene({manifest, composition, theme}) {
  const {width, height, ratio} = manifest.output_spec;
  const durationMs = composition.end_ms - composition.start_ms;
  const duration = seconds(durationMs);
  const prefix = assertSafeId(composition.id, "composition_id");
  const layout = resolveLayout(composition.layout_id, composition.layout_variant, ratio);
  const captions = manifest.captions
    .filter((caption) => caption.start_ms < composition.end_ms && caption.end_ms > composition.start_ms)
    .map((caption) => assertSafeText(caption.text, {maxChars: 240, maxLines: 3}));
  const overlayText = captions.join(" ");
  const overlays = composition.overlay_ids.map((overlayId, index) => {
    resolveOverlay(overlayId);
    return compileOverlay({
      overlayId,
      idPrefix: prefix,
      text: overlayText,
      durationMs,
      trackIndex: index + 2,
    });
  }).join("");
  const body = layout.compile({idPrefix: prefix, durationMs, hasVideo: Boolean(manifest.source_video), overlays});
  const variables = Object.entries(theme).sort(([left], [right]) => left.localeCompare(right))
    .map(([key, value]) => `${key}:${value}`).join(";");
  const rootId = `${prefix}_root`;
  return `<template id="${prefix}_template"><div id="${rootId}" data-composition-id="${prefix}" data-width="${width}" data-height="${height}" data-duration="${duration}" style="${variables}">${body}</div><style>[data-composition-id="${prefix}"]{position:relative;overflow:hidden;color:var(--hf-text);font-family:var(--hf-font)}[data-composition-id="${prefix}"] .hf-background{position:absolute;inset:0;background:linear-gradient(145deg,var(--hf-bg),var(--hf-surface))}[data-composition-id="${prefix}"] .hf-media{position:absolute;inset:var(--hf-pad);display:grid;place-items:center;border:1px solid var(--hf-border);border-radius:var(--hf-radius);background:var(--hf-surface-strong);box-shadow:var(--hf-shadow);overflow:hidden}[data-composition-id="${prefix}"] .hf-media span{color:var(--hf-muted);font-size:34px}[data-composition-id="${prefix}"] .hf-safe-area{position:absolute;inset:8% 7%;display:flex;flex-direction:column;justify-content:flex-end;gap:var(--hf-gap);pointer-events:none}[data-composition-id="${prefix}"] .hf-overlay{max-width:88%;padding:18px 28px;border:1px solid var(--hf-border);border-radius:20px;background:rgba(7,17,31,.82);font-size:40px;font-weight:700;line-height:1.28;box-shadow:var(--hf-shadow)}[data-composition-id="${prefix}"] .hf-overlay-standard_caption{align-self:center;text-align:center;font-size:34px}</style><script>(()=>{const root=document.querySelector('[data-composition-id="${prefix}"]');for(const node of root.querySelectorAll('[data-safe-text]'))node.querySelector('span').textContent=node.dataset.safeText;const tl=gsap.timeline({paused:true});tl.set(root,{autoAlpha:1},0);window.__timelines=window.__timelines||{};window.__timelines["${prefix}"] = tl;})();</script></template>`;
}

async function copyRuntime(projectRoot) {
  await copyFile(path.join(MODULE_ROOT, "node_modules", "gsap", "dist", "gsap.min.js"), path.join(projectRoot, "vendor", "gsap.min.js"));
  for (const file of ["NotoSansSC-Regular.woff2", "NotoSansSC-Bold.woff2"]) {
    await copyFile(path.join(MODULE_ROOT, "assets", "fonts", file), path.join(projectRoot, "assets", "fonts", file));
  }
}

function assertManifestShape(manifest) {
  if (!manifest || typeof manifest !== "object" || Array.isArray(manifest)) throw new Error("manifest_invalid");
  if (!Number.isInteger(manifest.duration_ms) || manifest.duration_ms <= 0) throw new Error("manifest_duration_invalid");
  if (!manifest.output_spec || !Array.isArray(manifest.compositions) || !Array.isArray(manifest.captions)) throw new Error("manifest_shape_invalid");
  if (!Number.isInteger(manifest.output_spec.fps_num) || !Number.isInteger(manifest.output_spec.fps_den)) throw new Error("manifest_fps_invalid");
  const ids = new Set();
  let expectedStart = 0;
  for (const composition of manifest.compositions) {
    if (ids.has(composition.id)) throw new Error("composition_id_duplicate");
    ids.add(composition.id);
    if (composition.start_ms !== expectedStart || composition.end_ms <= composition.start_ms) throw new Error("composition_timeline_invalid");
    expectedStart = composition.end_ms;
    if (!Array.isArray(composition.overlay_ids)) throw new Error("composition_overlays_invalid");
  }
  if (expectedStart !== manifest.duration_ms) throw new Error("composition_timeline_invalid");
}
