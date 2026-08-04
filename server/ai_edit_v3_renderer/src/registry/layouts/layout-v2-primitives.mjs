import {assertSafeId, assertSafeText, escapeAttribute, seconds} from "../layout-primitives.mjs";

const RATIO_SIZES = Object.freeze({"16:9": Object.freeze([1920, 1080]), "9:16": Object.freeze([1080, 1920])});
const SAFE_CSS_VALUE = /^(?:#[0-9a-fA-F]{6}|rgba?\([0-9., ]+\))$/u;

export const V2_RATIOS = Object.freeze(["16:9", "9:16"]);

export function createContract({id, moduleId, variants, requiredSlots, optionalSlots, identitySlots}) {
  return Object.freeze({
    id,
    version: "2.0.0",
    moduleId,
    variants: Object.freeze([...variants]),
    supportedRatios: V2_RATIOS,
    requiredSlots: Object.freeze([...requiredSlots]),
    optionalSlots: Object.freeze([...optionalSlots]),
    identitySlots: Object.freeze([...identitySlots]),
    fallback: "no_optional_media",
    safeAreas: Object.freeze({
      "16:9": Object.freeze({title: "landscape_title_safe", captions: "landscape_caption_safe"}),
      "9:16": Object.freeze({title: "portrait_title_safe", captions: "portrait_caption_safe"}),
    }),
  });
}

export function assertLayoutInput(contract, {variantId, ratio, idPrefix, durationMs, slots, designTokens, overlays = ""}) {
  if (!contract.variants.includes(variantId)) throw new Error("layout_variant_unknown");
  if (!V2_RATIOS.includes(ratio)) throw new Error("layout_ratio_unknown");
  const prefix = assertSafeId(idPrefix, "id_prefix");
  if (!slots || typeof slots !== "object" || Array.isArray(slots)) throw new Error("layout_slots_invalid");
  for (const slot of contract.requiredSlots) if (!slots[slot]) throw new Error("layout_required_slot_missing");
  if (typeof overlays !== "string" && (!overlays || typeof overlays !== "object" || Array.isArray(overlays) || !["title", "captions"].every((key) => typeof overlays[key] === "string"))) throw new Error("layout_overlays_invalid");
  return Object.freeze({prefix, duration: seconds(durationMs), slots, style: styleFromTokens(designTokens), overlays: typeof overlays === "string" ? {title: "", captions: overlays} : overlays});
}

export function assetOrFallback({prefix, slot, value, duration, trackIndex}) {
  const attrs = clipAttributes(duration, trackIndex);
  if (!value) return `<div id="${prefix}_${slot}" class="hf-v2-slot hf-v2-fallback clip" data-slot="${slot}" data-v2-region="${slot}" data-fallback="no_optional_media" data-fallback-state="rendered" ${attrs}><svg viewBox="0 0 100 100" aria-hidden="true" focusable="false"><circle cx="50" cy="50" r="38"></circle><path d="M28 62 43 47l11 10 18-20"></path></svg></div>`;
  const asset = normalizeAsset(value, slot);
  const element = asset.kind === "video"
    ? `<video muted playsinline preload="metadata" src="${asset.path}"></video>`
    : `<img alt="" src="${asset.path}">`;
  return `<div id="${prefix}_${slot}" class="hf-v2-slot hf-v2-slot-media clip" data-slot="${slot}" data-v2-region="${slot}" ${attrs}>${element}</div>`;
}

export function speakerSlot({prefix, value, duration, trackIndex}) {
  const asset = normalizeAsset(value, "speaker");
  const attrs = clipAttributes(duration, trackIndex);
  const element = asset.kind === "video"
    ? speakerVideos({prefix, value, asset, duration, trackIndex})
    : `<img alt="" src="${asset.path}">`;
  return `<div id="${prefix}_speaker" class="hf-v2-speaker clip" data-slot="speaker" data-v2-region="speaker" ${attrs}>${element}</div>`;
}

function speakerVideos({prefix, value, asset, duration, trackIndex}) {
  const clips = Array.isArray(value.clips) ? value.clips : [{index: 0, localStartMs: 0, durationMs: Math.round(Number(duration) * 1000), playbackStartMs: 0}];
  if (!clips.length) throw new Error("layout_required_slot_missing");
  return clips.map((clip, ordinal) => {
    if (!Number.isInteger(clip.localStartMs) || clip.localStartMs < 0 || !Number.isInteger(clip.durationMs) || clip.durationMs <= 0 || !Number.isInteger(clip.playbackStartMs) || clip.playbackStartMs < 0) throw new Error("layout_source_clip_invalid");
    const id = `${prefix}_speaker_clip_${Number.isInteger(clip.index) ? clip.index : ordinal}`;
    return `<video id="${id}" muted playsinline preload="metadata" src="${asset.path}" data-start="${seconds(clip.localStartMs)}" data-duration="${seconds(clip.durationMs)}" data-playback-start="${seconds(clip.playbackStartMs)}" data-track-index="${trackIndex}"></video>`;
  }).join("");
}

export function stepsSlot({prefix, value, duration, trackIndex}) {
  if (!value || typeof value !== "object" || !Array.isArray(value.items) || value.items.length < 1 || value.items.length > 6) throw new Error("layout_required_slot_missing");
  const items = value.items.map((item, index) => `<li data-step-index="${index}" data-safe-text="${escapeAttribute(assertSafeText(item, {maxChars: 80, maxLines: 1}))}"><span></span></li>`).join("");
  return `<ol id="${prefix}_steps" class="hf-v2-steps clip" data-slot="steps" data-v2-region="steps" ${clipAttributes(duration, trackIndex)}>${items}</ol>`;
}

export function layoutResult({contract, variantId, ratio, input, structure, body, criticalRegions}) {
  const [width, height] = RATIO_SIZES[ratio];
  const safeAreas = ratio === "16:9"
    ? {title: {x: 96, y: 54, width: 1120, height: 176}, captions: {x: 160, y: 804, width: 1600, height: 180}}
    : {title: {x: 60, y: 84, width: 960, height: 220}, captions: {x: 60, y: 1480, width: 960, height: 280}};
  const root = `${input.prefix}_layout`;
  const safe = `${input.prefix}_safe`;
  const titleSafe = `${input.prefix}_safe_title`;
  const publicSlots = Object.fromEntries([...contract.requiredSlots, ...contract.optionalSlots].map((slot) => [slot, `#${input.prefix}_${slot}`]));
  const html = `<section id="${root}" class="hf-v2-layout hf-v2-layout-${contract.id} clip" data-layout-v2="${contract.id}" data-layout-variant="${variantId}" data-layout-ratio="${ratio}" data-layout-structure="${structure}" data-start="0" data-duration="${input.duration}" data-track-index="1"${input.style}>${body}<aside id="${titleSafe}" class="hf-v2-safe-area hf-v2-safe-title clip" data-safe-host="title" data-safe-area="${ratio}" ${clipAttributes(input.duration, 19)}>${input.overlays.title}</aside><aside id="${safe}" class="hf-v2-safe-area hf-v2-safe-captions clip" data-safe-host="captions" data-safe-area="${ratio}" ${clipAttributes(input.duration, 20)}>${input.overlays.captions}</aside><style data-layout-audit="${contract.id}">${layoutCss({contract, variantId, ratio, criticalRegions})}</style></section>`;
  return Object.freeze({
    html,
    publicTargets: Object.freeze({root: `#${root}`, safeArea: `#${safe}`, slots: Object.freeze(publicSlots)}),
    identitySlots: contract.identitySlots,
    geometryAudit: Object.freeze({width, height, safeAreas: freezeBoxes(safeAreas), criticalRegions: freezeBoxes(criticalRegions)}),
  });
}

export function clipAttributes(duration, trackIndex) {
  return `data-start="0" data-duration="${duration}" data-track-index="${trackIndex}"`;
}

function normalizeAsset(value, slot) {
  if (!value || typeof value !== "object" || !["image", "video"].includes(value.kind)) throw new Error("layout_slot_invalid");
  assertSafeId(value.id, "slot_asset_id");
  if (typeof value.relativePath !== "string" || !/^(?!\/)(?![A-Za-z]:)(?!.*\\\\)(?!.*(?:^|\/)\.\.(?:\/|$))[A-Za-z0-9._/-]+$/u.test(value.relativePath)) throw new Error("layout_slot_path_invalid");
  return Object.freeze({kind: value.kind, path: escapeAttribute(value.relativePath), slot});
}

function styleFromTokens(tokens) {
  if (!tokens || typeof tokens !== "object" || Array.isArray(tokens)) throw new Error("layout_design_tokens_invalid");
  const entries = Object.entries(tokens).sort(([left], [right]) => left.localeCompare(right));
  if (!entries.every(([name, value]) => ["--hf-accent", "--hf-surface"].includes(name) && typeof value === "string" && SAFE_CSS_VALUE.test(value))) throw new Error("layout_design_tokens_invalid");
  return entries.length ? ` style="${entries.map(([name, value]) => `${name}:${escapeAttribute(value)}`).join(";")}"` : "";
}

function layoutCss({contract, variantId, ratio, criticalRegions}) {
  const selector = `.hf-v2-layout[data-layout-v2="${contract.id}"][data-layout-variant="${variantId}"][data-layout-ratio="${ratio}"]`;
  const regions = Object.entries(criticalRegions).map(([name, box]) => `${selector} [data-v2-region="${name}"]{position:absolute;left:${box.x}px;top:${box.y}px;width:${box.width}px;height:${box.height}px}`).join("");
  const safe = ratio === "16:9" ? {title: {x: 96, y: 54, width: 1120, height: 176}, captions: {x: 160, y: 804, width: 1600, height: 180}} : {title: {x: 60, y: 84, width: 960, height: 220}, captions: {x: 60, y: 1480, width: 960, height: 280}};
  return `${selector}{position:absolute;inset:0;overflow:hidden}${selector} .hf-v2-safe-title{position:absolute;left:${safe.title.x}px;top:${safe.title.y}px;width:${safe.title.width}px;height:${safe.title.height}px;z-index:20}${selector} .hf-v2-safe-captions{position:absolute;left:${safe.captions.x}px;top:${safe.captions.y}px;width:${safe.captions.width}px;height:${safe.captions.height}px;z-index:20}${selector} .hf-v2-slot>img,${selector} .hf-v2-slot>video,${selector} .hf-v2-speaker>img,${selector} .hf-v2-speaker>video{width:100%;height:100%;object-fit:var(--hf-image-fit)}${selector} .hf-v2-fallback{display:grid;place-items:center;background:var(--hf-surface)}${selector} .hf-v2-fallback svg{width:42%;height:42%;fill:none;stroke:var(--hf-accent);stroke-width:6}${variantCss(selector, contract.id, variantId, ratio)}${regions}`;
}

function variantCss(selector, layoutId, variantId, ratio) {
  const portrait = ratio === "9:16";
  if (layoutId === "speaker_fullscreen") {
    if (variantId === "clean_center") return `${selector} .hf-v2-speaker-stage{position:absolute;inset:0}${selector} .hf-v2-evidence-dock{position:absolute;left:${portrait ? 60 : 96}px;top:${portrait ? 1320 : 760}px;width:${portrait ? 260 : 340}px;height:${portrait ? 180 : 160}px}`;
    if (variantId === "headline_top") return `${selector} .hf-v2-headline-band{position:absolute;left:${portrait ? 60 : 96}px;top:${portrait ? 84 : 54}px;width:${portrait ? 960 : 1120}px;height:${portrait ? 220 : 176}px;background:var(--hf-surface)}${selector} .hf-v2-speaker-stage{position:absolute;inset:0}`;
    return `${selector} .hf-v2-speaker-stage{position:absolute;inset:0}${selector} .hf-v2-caption-rail{position:absolute;left:${portrait ? 60 : 1420}px;top:${portrait ? 1320 : 190}px;width:${portrait ? 960 : 360}px;height:${portrait ? 180 : 550}px}`;
  }
  if (layoutId === "product_hero") {
    if (variantId === "center_pedestal") return `${selector} .hf-v2-product-pedestal{position:absolute;inset:0}${selector} .hf-v2-product-plinth{position:absolute;left:${portrait ? 150 : 610}px;top:${portrait ? 1180 : 760}px;width:${portrait ? 780 : 700}px;height:${portrait ? 90 : 70}px;background:var(--hf-surface)}${selector} .hf-v2-detail-orbit{position:absolute;left:${portrait ? 720 : 1320}px;top:${portrait ? 360 : 160}px;width:${portrait ? 210 : 300}px;height:${portrait ? 210 : 240}px}`;
    if (variantId === "split_copy") return `${selector} .hf-v2-product-copy{position:absolute;left:${portrait ? 60 : 96}px;top:${portrait ? 110 : 170}px;width:${portrait ? 960 : 430}px;height:${portrait ? 220 : 570}px;background:var(--hf-surface)}${selector} .hf-v2-product-frame{position:absolute;inset:0}`;
    return `${selector} .hf-v2-product-gallery{position:absolute;inset:0}${selector} .hf-v2-detail-strip{position:absolute;left:${portrait ? 150 : 1340}px;top:${portrait ? 1220 : 240}px;width:${portrait ? 780 : 260}px;height:${portrait ? 210 : 420}px}`;
  }
  if (variantId === "vertical_steps") return `${selector} .hf-v2-vertical-process{position:absolute;inset:0}${selector} .hf-v2-process-accent{position:absolute;left:${portrait ? 740 : 1460}px;top:${portrait ? 360 : 180}px;width:${portrait ? 240 : 200}px;height:${portrait ? 300 : 560}px}`;
  if (variantId === "numbered_cards") return `${selector} .hf-v2-card-process{position:absolute;inset:0}${selector} .hf-v2-card-counter{position:absolute;left:${portrait ? 100 : 180}px;top:${portrait ? 210 : 110}px;width:${portrait ? 260 : 380}px;height:${portrait ? 120 : 90}px;background:var(--hf-surface)}${selector} .hf-v2-card-process footer{position:absolute;left:${portrait ? 100 : 1520}px;top:${portrait ? 1400 : 650}px;width:${portrait ? 220 : 180}px;height:${portrait ? 160 : 120}px}`;
  return `${selector} .hf-v2-progress-line{position:absolute;left:${portrait ? 100 : 180}px;top:${portrait ? 380 : 180}px;width:${portrait ? 880 : 1560}px;height:${portrait ? 960 : 560}px;fill:none;stroke:var(--hf-accent);stroke-width:2}${selector} .hf-v2-progress-nodes{position:absolute;inset:0}${selector} aside:not(.hf-v2-safe-area){position:absolute;left:${portrait ? 760 : 1500}px;top:${portrait ? 1200 : 600}px;width:${portrait ? 180 : 180}px;height:${portrait ? 180 : 130}px}`;
}

function freezeBoxes(boxes) {
  return Object.freeze(Object.fromEntries(Object.entries(boxes).map(([name, box]) => [name, Object.freeze({...box})])));
}
