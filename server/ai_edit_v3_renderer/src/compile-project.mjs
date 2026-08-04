import {copyFile, mkdir, writeFile} from "node:fs/promises";
import path from "node:path";
import {fileURLToPath} from "node:url";

import {applyAnimation, compileAnimationScript} from "./registry/animations.mjs";
import {assertSafeId, assertSafeText, escapeAttribute, seconds} from "./registry/layout-primitives.mjs";
import {compileOverlay, getOverlayContract} from "./registry/overlays.mjs";
import {getRegistrySha256, resolveLayout, resolveOverlay, resolveTheme} from "./registry/index.mjs";
import {applyTransition, compileTransitionScript} from "./registry/transitions.mjs";

const MODULE_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

export async function compileProject({manifest, outputRoot, sceneOptions = {}}) {
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
    const sceneHtml = compileScene({manifest, composition, theme, ...sceneOptions});
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
    return `<div id="${composition.id}_host" data-composition-id="${composition.id}" data-composition-src="compositions/${composition.id}.html" data-start="${start}" data-duration="${sceneDuration}" data-track-index="0"></div>`;
  }).join("");
  return `<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><style>@font-face{font-family:"Noto Sans SC";src:url("assets/fonts/NotoSansSC-Regular.woff2") format("woff2");font-weight:400}@font-face{font-family:"Noto Sans SC";src:url("assets/fonts/NotoSansSC-Bold.woff2") format("woff2");font-weight:700}html,body{margin:0;background:transparent;overflow:hidden}#main{position:relative;overflow:hidden}</style></head><body><div id="main" data-composition-id="main" data-width="${width}" data-height="${height}" data-start="0" data-duration="${duration}">${hosts}</div><script src="vendor/gsap.min.js"></script><script>window.__timelines=window.__timelines||{};const tl=gsap.timeline({paused:true});window.__timelines["main"]=tl;Object.freeze(${JSON.stringify(compositionIds)});</script></body></html>`;
}

function compileScene({manifest, composition, theme, layoutResolver = resolveLayout, buildLayoutInput = legacyLayoutInput, compileSource = compileSourceVideo}) {
  const {width, height, ratio} = manifest.output_spec;
  const durationMs = composition.end_ms - composition.start_ms;
  const duration = seconds(durationMs);
  const prefix = assertSafeId(composition.id, "composition_id");
  const layout = layoutResolver(composition.layout_id, composition.layout_variant, ratio);
  const captions = manifest.captions
    .filter((caption) => caption.start_ms < composition.end_ms && caption.end_ms > composition.start_ms)
    .map((caption) => ({
      text: assertSafeText(caption.text, {maxChars: 240, maxLines: 3}),
      startMs: Math.max(caption.start_ms, composition.start_ms) - composition.start_ms,
      endMs: Math.min(caption.end_ms, composition.end_ms) - composition.start_ms,
    }));
  const overlayBindings = manifest.version === "2.0"
    ? composition.overlay_instances.map(({instance_id, component_id}) => ({instanceId: instance_id, componentId: component_id}))
    : composition.overlay_ids.map((componentId) => ({instanceId: componentId, componentId}));
  const overlayByTarget = new Map(overlayBindings.map((binding) => [binding.instanceId, binding]));
  const overlays = overlayBindings.map(({instanceId, componentId}, index) => {
    resolveOverlay(componentId);
    const overlayContract = getOverlayContract(componentId);
    const idPrefix = manifest.version === "2.0" ? `${prefix}_${instanceId}` : prefix;
    if (componentId === "standard_caption") {
      return captions.map((caption, captionIndex) => compileOverlay({
        overlayId: componentId,
        idPrefix: `${idPrefix}_caption_${captionIndex + 1}`,
        text: caption.text,
        startMs: caption.startMs,
        durationMs: caption.endMs - caption.startMs,
        trackIndex: index + 21 + captionIndex,
      })).join("");
    }
    const overlayText = [...captions.map((caption) => caption.text).join(" ")].slice(0, overlayContract.maxChars).join("");
    return compileOverlay({
      overlayId: componentId,
      idPrefix,
      text: overlayText,
      durationMs,
      trackIndex: index + 21,
    });
  }).join("");
  const assetById = new Map((manifest.assets ?? []).map((asset) => [asset.id, asset]));
  const assets = (composition.asset_ids ?? []).map((assetId) => {
    const asset = assetById.get(assetId);
    if (!asset) throw new Error("composition_asset_unknown");
    return {id: asset.id, kind: asset.kind, relativePath: asset.path};
  });
  const layoutOutput = layout.compile(buildLayoutInput({manifest, composition, prefix, durationMs, overlays, scene: composition, assets, captions, theme, layout}));
  const body = typeof layoutOutput === "string" ? layoutOutput : layoutOutput?.html;
  if (typeof body !== "string") throw new Error("layout_compile_invalid");
  const sourceVideo = compileSource({manifest, composition, prefix, layout});
  const variables = Object.entries(theme).sort(([left], [right]) => left.localeCompare(right))
    .map(([key, value]) => `${key}:${escapeAttribute(value)}`).join(";");
  const rootId = `${prefix}_root`;
  const timeline = timelineRecorder();
  const fps = manifest.output_spec.fps_num / manifest.output_spec.fps_den;
  const minimumAnimationMs = Math.ceil(1000 / fps);
  const animationScript = (composition.animations ?? []).flatMap((animation) => {
    const binding = overlayByTarget.get(animation.target);
    if (!binding) throw new Error("animation_target_unknown");
    const animationPrefix = manifest.version === "2.0" ? `${prefix}_${binding.instanceId}` : prefix;
    const targets = binding.componentId === "standard_caption"
      ? captions.map((caption, captionIndex) => ({
        target: `#${animationPrefix}_caption_${captionIndex + 1}_standard_caption`,
        windowStartMs: caption.startMs,
        windowDurationMs: caption.endMs - caption.startMs,
        delayMs: animation.delay_ms,
      })).filter((item) => item.windowDurationMs >= minimumAnimationMs)
      : [{
        target: `#${animationPrefix}_${binding.componentId}`,
        windowStartMs: 0,
        windowDurationMs: durationMs,
        delayMs: animation.delay_ms,
      }];
    return targets.map(({target, windowStartMs, windowDurationMs, delayMs}) => {
      const audit = applyAnimation({
        timeline, preset: animation.preset, target,
        params: {durationMs: animation.duration_ms, delayMs},
        sceneDurationMs: windowDurationMs, fps,
      });
      return compileAnimationScript({...audit, target, startMs: audit.startMs + windowStartMs});
    });
  }).join("");
  const transitionAudit = applyTransition({
    timeline, transition: composition.transition, outgoing: `#${prefix}_background`, incoming: `#${rootId}`,
    boundaryMs: 0, sceneDurationMs: durationMs, fps: manifest.output_spec.fps_num / manifest.output_spec.fps_den,
  });
  const transitionScript = compileTransitionScript({...transitionAudit, outgoing: `#${prefix}_background`, incoming: `#${rootId}`});
  return `<template id="${prefix}_template"><div id="${rootId}" data-composition-id="${prefix}" data-width="${width}" data-height="${height}" data-start="0" data-duration="${duration}" style="${variables}">${body}${sourceVideo}</div><style>#${rootId}{position:relative;overflow:hidden;color:var(--hf-text);font-family:var(--hf-font)}#${rootId} .hf-background{position:absolute;inset:0;z-index:0;background:linear-gradient(145deg,var(--hf-bg),var(--hf-surface))}#${rootId} .hf-source-video{position:absolute;inset:0;width:100%;height:100%;object-fit:var(--hf-image-fit);z-index:1}#${rootId} .hf-layout-frame{position:absolute;inset:5%;z-index:2;display:grid;gap:var(--hf-gap)}#${rootId} .hf-speaker-zone,#${rootId} .hf-materials{display:grid;place-items:center;position:relative;overflow:hidden;border:1px solid var(--hf-border);border-radius:var(--hf-radius);background:rgba(23,42,66,.14);box-shadow:var(--hf-shadow)}#${rootId} .hf-materials{background:var(--hf-surface-strong)}#${rootId} .hf-speaker-zone span{display:${manifest.source_video ? "none" : "block"}}#${rootId} .hf-speaker-zone span,#${rootId} .hf-fallback span{color:var(--hf-muted);font-size:34px}#${rootId} .hf-materials{grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;padding:12px}#${rootId} .hf-material-count-1{grid-template-columns:1fr}#${rootId} .hf-asset{width:100%;height:100%;min-height:0;object-fit:var(--hf-image-fit);border-radius:18px}#${rootId} .hf-fallback{display:grid;place-items:center;width:100%;height:100%}#${rootId} .hf-layout-speaker_fullscreen{grid-template-columns:1fr}#${rootId} .hf-layout-speaker_fullscreen .hf-materials{display:none}#${rootId} .hf-layout-speaker_left_info_right{grid-template-columns:1.15fr .85fr}#${rootId} .hf-layout-speaker_right_evidence_left{grid-template-columns:.85fr 1.15fr}#${rootId} .hf-layout-speaker_right_evidence_left .hf-speaker-zone{order:2}#${rootId} .hf-layout-material_fullscreen_speaker_pip .hf-materials,#${rootId} .hf-layout-product_hero .hf-materials{position:absolute;inset:0}#${rootId} .hf-layout-material_fullscreen_speaker_pip .hf-speaker-zone{position:absolute;right:3%;bottom:4%;width:28%;height:34%;z-index:2}#${rootId} .hf-layout-product_hero .hf-speaker-zone{display:none}#${rootId} .hf-layout-editorial_collage{grid-template-columns:.75fr 1.25fr}#${rootId} .hf-layout-editorial_collage .hf-materials{grid-template-columns:repeat(2,1fr)}#${rootId} .hf-layout-comparison_split{grid-template-columns:1fr 1fr}#${rootId} .hf-layout-steps_stack,#${rootId} .hf-layout-method_timeline{grid-template-rows:.55fr 1.45fr}#${rootId} .hf-layout-number_proof .hf-speaker-zone{display:none}#${rootId} .hf-layout-number_proof .hf-materials{font-size:96px}#${rootId} .hf-layout-quote_reversal{transform:rotate(-1deg);inset:9% 7%}#${rootId} .hf-layout-cta_offer{inset:12%;transform:scale(.94)}#${rootId} .hf-variant-emphasis_b .hf-speaker-zone{border-width:3px}#${rootId} .hf-safe-area{position:absolute;inset:8% 7%;z-index:20;display:flex;flex-direction:column;justify-content:flex-end;gap:var(--hf-gap)}#${rootId} .hf-overlay{max-width:88%;padding:18px 28px;border:1px solid var(--hf-border);border-radius:20px;background:rgba(7,17,31,.82);font-size:40px;font-weight:700;line-height:1.28;box-shadow:var(--hf-shadow)}#${rootId} .hf-overlay-standard_caption{align-self:center;text-align:center;font-size:34px}</style><script>(()=>{const root=document.querySelector('#${rootId}');for(const node of root.querySelectorAll('[data-safe-text]'))node.querySelector('span').textContent=node.dataset.safeText;const tl=gsap.timeline({paused:true});tl.set(root,{autoAlpha:1},0);${transitionScript}${animationScript}window.__timelines=window.__timelines||{};window.__timelines["${prefix}"] = tl;})();</script></template>`;
}

function legacyLayoutInput({prefix, durationMs, overlays, scene, assets, manifest}) {
  return {idPrefix: prefix, durationMs, hasVideo: Boolean(manifest.source_video), overlays, scene, assets};
}

export function compileSourceVideo({manifest, composition, prefix}) {
  if (!manifest.source_video) return "";
  if (manifest.source_video.silent !== true) throw new Error("source_video_audio_forbidden");
  const sourcePath = manifest.source_video.path;
  if (typeof sourcePath !== "string" || !/^(?!\/)(?![A-Za-z]:)(?!.*\\)(?!.*(?:^|\/)\.\.(?:\/|$))[A-Za-z0-9._/-]+$/u.test(sourcePath)) {
    throw new Error("source_video_path_invalid");
  }
  const speakerPip = composition.layout_id === "material_fullscreen_speaker_pip";
  const sourceClass = speakerPip ? "hf-source-video hf-source-video-pip clip" : "hf-source-video clip";
  const pipStyle = speakerPip
    ? ` style="inset:auto 7.85% 8.8% auto;width:26.6%;height:30.6%;z-index:3;border-radius:var(--hf-radius)"`
    : "";
  return sourceSegmentClips({manifest, composition}).map((clip) => {
    return `<video id="${prefix}_source_${clip.index}" class="${sourceClass}" muted playsinline preload="auto" src="${escapeAttribute(sourcePath)}" data-start="${seconds(clip.localStartMs)}" data-duration="${seconds(clip.durationMs)}" data-playback-start="${seconds(clip.playbackStartMs)}" data-volume="0" data-track-index="10"${pipStyle}></video>`;
  }).join("");
}

/** Returns source intervals that actually intersect this composition. */
export function sourceSegmentClips({manifest, composition}) {
  if (!manifest.source_video) return [];
  const sourcePath = manifest.source_video.path;
  const segments = Array.isArray(manifest.source_segments) ? manifest.source_segments : [];
  return segments.flatMap((segment, index) => {
    const start = Math.max(segment.output_start_ms, composition.start_ms);
    const end = Math.min(segment.output_end_ms, composition.end_ms);
    if (end <= start) return [];
    if (segment.source_path !== sourcePath) throw new Error("source_segment_path_mismatch");
    return [{index, localStartMs: start - composition.start_ms, playbackStartMs: segment.source_start_ms + (start - segment.output_start_ms), durationMs: end - start}];
  });
}

function timelineRecorder() {
  const timeline = {};
  for (const method of ["fromTo", "to", "set"]) timeline[method] = () => timeline;
  return timeline;
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
