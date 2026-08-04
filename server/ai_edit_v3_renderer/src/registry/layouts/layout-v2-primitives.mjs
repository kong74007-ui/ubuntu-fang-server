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

export function copySlot(value) {
  const text = value?.text;
  if (typeof text === "string" && text) {
    return `<div class="hf-v2-copy-text" data-safe-text="${escapeAttribute(assertSafeText(text, {maxChars: 240, maxLines: 3}))}"><span></span></div>`;
  }
  return `<svg class="hf-v2-copy-fallback" data-fallback="copy_graphic" data-fallback-state="rendered" viewBox="0 0 160 90" aria-hidden="true" focusable="false"><rect x="8" y="8" width="144" height="74" rx="12"></rect><path d="M28 32h78M28 48h104M28 64h62"></path></svg>`;
}

export function textSlot({prefix, slot, value, duration, trackIndex, maxChars = 160, maxLines = 3}) {
  const text = value?.text;
  if (typeof text !== "string" || !text) throw new Error("layout_required_slot_missing");
  return `<div id="${prefix}_${slot}" class="hf-v2-text-slot clip" data-slot="${slot}" data-v2-region="${slot}" data-safe-text="${escapeAttribute(assertSafeText(text, {maxChars, maxLines}))}" ${clipAttributes(duration, trackIndex)}><span></span></div>`;
}

export function proofSlot({prefix, value, duration, trackIndex}) {
  if (!value || typeof value !== "object") throw new Error("layout_required_slot_missing");
  const label = assertSafeText(value.label, {maxChars: 48, maxLines: 1});
  const metric = assertSafeText(value.value, {maxChars: 24, maxLines: 1});
  return `<dl id="${prefix}_proof" class="hf-v2-proof-slot clip" data-slot="proof" data-v2-region="proof" ${clipAttributes(duration, trackIndex)}><dt data-safe-text="${escapeAttribute(label)}"><span></span></dt><dd data-safe-text="${escapeAttribute(metric)}"><span></span></dd></dl>`;
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
  return `${selector}{position:absolute;inset:0;overflow:hidden}${selector} .hf-v2-safe-title{position:absolute;left:${safe.title.x}px;top:${safe.title.y}px;width:${safe.title.width}px;height:${safe.title.height}px;z-index:20}${selector} .hf-v2-safe-captions{position:absolute;left:${safe.captions.x}px;top:${safe.captions.y}px;width:${safe.captions.width}px;height:${safe.captions.height}px;z-index:20}${selector} .hf-v2-slot{width:100%;height:100%}${selector} .hf-v2-slot>img,${selector} .hf-v2-slot>video,${selector} .hf-v2-speaker>img,${selector} .hf-v2-speaker>video{width:100%;height:100%;object-fit:var(--hf-image-fit)}${selector} .hf-v2-text-slot,${selector} .hf-v2-proof-slot,${selector} .hf-v2-steps{box-sizing:border-box;color:var(--hf-accent);font-weight:700;line-height:1.25}${selector} .hf-v2-text-slot{display:grid;place-items:center;padding:28px;font-size:${ratio === "9:16" ? 48 : 54}px}${selector} .hf-v2-proof-slot{display:grid;place-items:center;margin:0;padding:24px}${selector} .hf-v2-proof-slot dt{font-size:${ratio === "9:16" ? 30 : 34}px}${selector} .hf-v2-proof-slot dd{margin:0;font-size:${ratio === "9:16" ? 86 : 110}px}${selector} .hf-v2-fallback{display:grid;place-items:center;background:var(--hf-surface)}${selector} .hf-v2-fallback svg{width:42%;height:42%;fill:none;stroke:var(--hf-accent);stroke-width:6}${variantCss(selector, contract.id, variantId, ratio)}${regions}`;
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
  if (layoutId === "speaker_left_info_right") {
    if (variantId === "card_stack") return `${selector} .hf-v2-left-speaker-card,${selector} .hf-v2-right-card-stack{position:absolute;inset:0}${selector} .hf-v2-right-card-stack{background:linear-gradient(90deg,transparent ${portrait ? 60 : 55}%,var(--hf-surface) 100%)}${selector} .hf-v2-right-card-stack header,${selector} .hf-v2-right-card-stack footer{position:absolute;right:${portrait ? 60 : 100}px;width:${portrait ? 310 : 610}px;display:flex;gap:16px}${selector} .hf-v2-right-card-stack header{top:${portrait ? 330 : 150}px}${selector} .hf-v2-right-card-stack footer{bottom:${portrait ? 430 : 180}px}`;
    if (variantId === "number_focus") return `${selector} .hf-v2-left-speaker-number,${selector} .hf-v2-left-speaker-number>figure,${selector} .hf-v2-number-proof{position:absolute;inset:0}${selector} .hf-v2-number-proof>svg{position:absolute;right:${portrait ? 45 : 100}px;top:${portrait ? 220 : 130}px;width:${portrait ? 280 : 360}px;height:${portrait ? 280 : 360}px;fill:none;stroke:var(--hf-accent);stroke-width:5}`;
    return `${selector} .hf-v2-left-speaker-evidence,${selector} .hf-v2-evidence-canvas,${selector} .hf-v2-speaker-cutout{position:absolute;inset:0}${selector} .hf-v2-evidence-canvas:before{content:"";position:absolute;inset:${portrait ? "120px 30px 420px 360px" : "70px 50px 100px 690px"};background:var(--hf-surface)}${selector} .hf-v2-speaker-cutout figcaption{position:absolute;left:${portrait ? 90 : 140}px;bottom:${portrait ? 300 : 90}px;width:${portrait ? 420 : 650}px;height:12px;background:var(--hf-accent)}`;
  }
  if (layoutId === "speaker_right_evidence_left") {
    if (variantId === "document_panel") return `${selector} .hf-v2-document-stage,${selector} .hf-v2-document-panel,${selector} .hf-v2-right-speaker{position:absolute;inset:0}${selector} .hf-v2-document-panel:before{content:"";position:absolute;left:${portrait ? 35 : 70}px;top:${portrait ? 150 : 80}px;width:${portrait ? 680 : 960}px;height:${portrait ? 1050 : 850}px;background:var(--hf-surface);transform:rotate(-1deg)}${selector} .hf-v2-document-panel footer{position:absolute;left:${portrait ? 90 : 120}px;width:${portrait ? 500 : 760}px;bottom:${portrait ? 740 : 190}px;display:grid;gap:12px}`;
    if (variantId === "comparison_panel") return `${selector} .hf-v2-comparison-stage{position:absolute;inset:0;display:grid;grid-template-columns:${portrait ? "1fr 18px 1fr" : ".9fr 24px 1.1fr"}}${selector} .hf-v2-comparison-proof{position:absolute;inset:0}${selector} .hf-v2-comparison-proof header{display:grid;grid-template-columns:1fr 1fr;gap:12px}${selector} .hf-v2-comparison-divider{position:absolute;left:${portrait ? 540 : 960}px;top:12%;bottom:12%;width:4px;background:var(--hf-accent)}`;
    return `${selector} .hf-v2-quote-proof,${selector} .hf-v2-quote-speaker{position:absolute;inset:0}${selector} .hf-v2-quote-proof:before{content:"";position:absolute;left:${portrait ? 45 : 90}px;top:${portrait ? 100 : 70}px;width:${portrait ? 870 : 1180}px;height:${portrait ? 680 : 720}px;background:var(--hf-surface)}${selector} .hf-v2-quote-proof>span{position:absolute;left:${portrait ? 65 : 115}px;top:${portrait ? 80 : 50}px;font-size:${portrait ? 150 : 190}px;color:var(--hf-accent)}`;
  }
  if (layoutId === "material_fullscreen_speaker_pip") {
    if (variantId === "pip_round") return `${selector} .hf-v2-pip-round-stage{position:absolute;inset:0}${selector} .hf-v2-pip-round{position:absolute;inset:0}${selector} [data-v2-region="speaker"]{border-radius:50%;overflow:hidden;border:8px solid var(--hf-accent)}${selector} .hf-v2-pip-orbit{position:absolute;right:${portrait ? 70 : 110}px;bottom:${portrait ? 430 : 80}px;width:${portrait ? 180 : 230}px;height:${portrait ? 180 : 180}px}`;
    if (variantId === "pip_card") return `${selector} .hf-v2-pip-card-stage,${selector} .hf-v2-pip-card-stage>figure,${selector} .hf-v2-pip-card{position:absolute;inset:0}${selector} .hf-v2-pip-card:before{content:"";position:absolute;right:${portrait ? 55 : 90}px;bottom:${portrait ? 350 : 70}px;width:${portrait ? 430 : 470}px;height:${portrait ? 620 : 550}px;background:var(--hf-surface);box-shadow:0 24px 70px rgba(0,0,0,.35)}${selector} .hf-v2-pip-card footer{position:absolute;right:${portrait ? 85 : 120}px;bottom:${portrait ? 380 : 100}px;width:${portrait ? 370 : 410}px;height:100px}`;
    return `${selector} .hf-v2-pip-edge-stage,${selector} .hf-v2-edge-primary,${selector} .hf-v2-edge-rail{position:absolute;inset:0}${selector} .hf-v2-edge-rail:before{content:"";position:absolute;right:0;top:0;bottom:0;width:${portrait ? 320 : 420}px;background:var(--hf-surface);border-left:6px solid var(--hf-accent)}${selector} .hf-v2-edge-rail header{position:absolute;right:25px;top:${portrait ? 250 : 100}px;width:${portrait ? 270 : 370}px;height:${portrait ? 250 : 210}px}${selector} .hf-v2-edge-rail footer{position:absolute;right:25px;bottom:${portrait ? 240 : 80}px;width:${portrait ? 270 : 370}px;height:10px;background:var(--hf-accent)}`;
  }
  if (layoutId === "editorial_collage") {
    if (variantId === "magazine_grid") return `${selector} .hf-v2-magazine-grid,${selector} .hf-v2-magazine-grid>figure,${selector} .hf-v2-magazine-grid>aside{position:absolute;inset:0}${selector} .hf-v2-magazine-grid>header{position:absolute;left:${portrait ? 70 : 110}px;top:${portrait ? 170 : 90}px;display:flex;gap:18px}${selector} .hf-v2-magazine-grid>footer{position:absolute;right:${portrait ? 70 : 120}px;bottom:${portrait ? 300 : 100}px;display:flex;gap:14px}`;
    if (variantId === "layered_cards") return `${selector} .hf-v2-layered-cards,${selector} .hf-v2-layered-cards>figure,${selector} .hf-v2-layered-cards>aside,${selector} .hf-v2-layer-back{position:absolute;inset:0}${selector} .hf-v2-layer-back svg{position:absolute;left:${portrait ? 70 : 240}px;top:${portrait ? 260 : 80}px;width:${portrait ? 850 : 1300}px;height:${portrait ? 1200 : 900}px;fill:var(--hf-surface);stroke:var(--hf-accent)}`;
    return `${selector} .hf-v2-film-strip,${selector} .hf-v2-film-strip>ol,${selector} .hf-v2-film-strip>ol>li{position:absolute;inset:0}${selector} .hf-v2-film-strip>nav{position:absolute;left:0;right:0;top:${portrait ? 330 : 180}px;display:flex;justify-content:space-around}${selector} .hf-v2-film-strip>footer{position:absolute;left:6%;right:6%;bottom:${portrait ? 330 : 90}px;height:40px}`;
  }
  if (layoutId === "comparison_split") {
    if (variantId === "vertical_divide") return `${selector} .hf-v2-vertical-compare,${selector} .hf-v2-vertical-compare>section,${selector} .hf-v2-vertical-compare>aside{position:absolute;inset:0}${selector} .hf-v2-compare-divider{position:absolute;${portrait ? "left:70px;right:70px;top:875px;height:8px" : "left:956px;top:150px;bottom:150px;width:8px"};background:var(--hf-accent)}`;
    if (variantId === "before_after_slider") return `${selector} .hf-v2-before-after,${selector} .hf-v2-before-after>div,${selector} .hf-v2-before-after>aside{position:absolute;inset:0}${selector} .hf-v2-before-after>figcaption{position:absolute;left:50%;top:${portrait ? 300 : 150}px;bottom:${portrait ? 580 : 170}px;width:6px;background:var(--hf-accent)}`;
    return `${selector} .hf-v2-score-compare,${selector} .hf-v2-score-compare>section,${selector} .hf-v2-score-compare>aside{position:absolute;inset:0}${selector} .hf-v2-score-compare meter{position:absolute;bottom:${portrait ? 610 : 130}px;width:${portrait ? 320 : 560}px}${selector} .hf-v2-score-compare>header{position:absolute;left:12%;right:12%;top:${portrait ? 250 : 110}px;display:flex;justify-content:space-between}`;
  }
  if (layoutId === "number_proof") {
    if (variantId === "hero_number") return `${selector} .hf-v2-hero-number,${selector} .hf-v2-hero-number>aside{position:absolute;inset:0}${selector} .hf-v2-hero-number>header{position:absolute;left:${portrait ? 60 : 100}px;top:${portrait ? 280 : 120}px;width:${portrait ? 960 : 1200}px;height:10px;background:var(--hf-accent)}`;
    if (variantId === "metric_grid") return `${selector} .hf-v2-metric-grid,${selector} .hf-v2-metric-grid>div,${selector} .hf-v2-metric-grid>footer{position:absolute;inset:0}${selector} .hf-v2-metric-grid>ul{position:absolute;left:${portrait ? 90 : 900}px;right:${portrait ? 90 : 180}px;top:${portrait ? 650 : 170}px;display:grid;grid-template-columns:repeat(3,1fr);gap:18px}`;
    return `${selector} .hf-v2-chart-callout,${selector} .hf-v2-chart-callout>aside,${selector} .hf-v2-chart-callout>figure{position:absolute;inset:0}${selector} .hf-v2-chart-callout>svg{position:absolute;left:${portrait ? 80 : 150}px;top:${portrait ? 280 : 140}px;width:${portrait ? 900 : 1100}px;height:${portrait ? 480 : 420}px;fill:none;stroke:var(--hf-accent);stroke-width:4}`;
  }
  if (layoutId === "quote_reversal") {
    if (variantId === "diagonal_statement") return `${selector} .hf-v2-diagonal-statement,${selector} .hf-v2-diagonal-statement>footer{position:absolute;inset:0}${selector} .hf-v2-diagonal-statement>svg{position:absolute;inset:5%;width:90%;height:90%;fill:none;stroke:var(--hf-accent);stroke-width:3}`;
    if (variantId === "strike_reveal") return `${selector} .hf-v2-strike-reveal,${selector} .hf-v2-strike-reveal>section,${selector} .hf-v2-strike-reveal>aside{position:absolute;inset:0}${selector} .hf-v2-strike-reveal>header{position:absolute;left:${portrait ? 100 : 240}px;top:${portrait ? 500 : 250}px;width:${portrait ? 780 : 900}px;height:18px;background:var(--hf-accent)}`;
    return `${selector} .hf-v2-question-answer,${selector} .hf-v2-question-answer>section,${selector} .hf-v2-question-answer>aside{position:absolute;inset:0}${selector} .hf-v2-answer-divider{position:absolute;${portrait ? "left:100px;right:100px;top:810px;height:6px" : "left:900px;top:190px;bottom:190px;width:6px"};background:var(--hf-accent)}`;
  }
  if (layoutId === "method_timeline") {
    if (variantId === "horizontal_timeline") return `${selector} .hf-v2-horizontal-timeline,${selector} .hf-v2-horizontal-timeline>aside{position:absolute;inset:0}${selector} .hf-v2-horizontal-timeline>svg{position:absolute;left:${portrait ? 90 : 150}px;top:${portrait ? 800 : 520}px;width:${portrait ? 900 : 1620}px;height:120px;fill:none;stroke:var(--hf-accent);stroke-width:3}`;
    if (variantId === "vertical_milestones") return `${selector} .hf-v2-vertical-milestones,${selector} .hf-v2-vertical-milestones>nav,${selector} .hf-v2-vertical-milestones>figure{position:absolute;inset:0}${selector} .hf-v2-vertical-milestones>header{position:absolute;left:${portrait ? 90 : 190}px;top:15%;bottom:15%;width:8px;background:var(--hf-accent)}`;
    return `${selector} .hf-v2-chapter-route,${selector} .hf-v2-chapter-route>aside,${selector} .hf-v2-chapter-route>section{position:absolute;inset:0}${selector} .hf-v2-chapter-route>ol{position:absolute;left:${portrait ? 70 : 100}px;top:${portrait ? 320 : 220}px;display:grid;gap:${portrait ? 170 : 90}px}${selector} .hf-v2-chapter-route>footer{position:absolute;left:8%;right:8%;bottom:${portrait ? 330 : 100}px}`;
  }
  if (layoutId === "cta_offer") {
    if (variantId === "offer_card") return `${selector} .hf-v2-offer-card,${selector} .hf-v2-offer-card>section,${selector} .hf-v2-offer-card>aside{position:absolute;inset:0}${selector} .hf-v2-offer-card:before{content:"";position:absolute;left:${portrait ? 60 : 180}px;right:${portrait ? 60 : 180}px;top:${portrait ? 340 : 150}px;bottom:${portrait ? 380 : 150}px;background:var(--hf-surface);border:4px solid var(--hf-accent)}`;
    if (variantId === "qr_placeholder") return `${selector} .hf-v2-qr-offer,${selector} .hf-v2-qr-offer>figure,${selector} .hf-v2-qr-offer>section{position:absolute;inset:0}${selector} .hf-v2-qr-offer figcaption{position:absolute;left:${portrait ? 400 : 420}px;top:${portrait ? 470 : 380}px;width:${portrait ? 280 : 220}px;height:${portrait ? 280 : 220}px}${selector} .hf-v2-qr-offer figcaption svg{width:100%;height:100%;fill:var(--hf-accent)}`;
    return `${selector} .hf-v2-action-steps,${selector} .hf-v2-action-steps>main,${selector} .hf-v2-action-steps>aside{position:absolute;inset:0}${selector} .hf-v2-action-steps>nav{position:absolute;left:${portrait ? 100 : 170}px;top:${portrait ? 320 : 180}px}${selector} .hf-v2-action-steps>nav ol{display:flex;gap:${portrait ? 180 : 300}px}${selector} .hf-v2-action-steps>footer{position:absolute;left:10%;right:10%;bottom:${portrait ? 330 : 100}px;height:10px;background:var(--hf-accent)}`;
  }
  if (variantId === "vertical_steps") return `${selector} .hf-v2-vertical-process{position:absolute;inset:0}${selector} .hf-v2-process-accent{position:absolute;left:${portrait ? 740 : 1460}px;top:${portrait ? 360 : 180}px;width:${portrait ? 240 : 200}px;height:${portrait ? 300 : 560}px}`;
  if (variantId === "numbered_cards") return `${selector} .hf-v2-card-process{position:absolute;inset:0}${selector} .hf-v2-card-counter{position:absolute;left:${portrait ? 100 : 180}px;top:${portrait ? 210 : 110}px;width:${portrait ? 260 : 380}px;height:${portrait ? 120 : 90}px;background:var(--hf-surface)}${selector} .hf-v2-card-process footer{position:absolute;left:${portrait ? 100 : 1520}px;top:${portrait ? 1400 : 650}px;width:${portrait ? 220 : 180}px;height:${portrait ? 160 : 120}px}`;
  return `${selector} .hf-v2-progress-line{position:absolute;left:${portrait ? 100 : 180}px;top:${portrait ? 380 : 180}px;width:${portrait ? 880 : 1560}px;height:${portrait ? 960 : 560}px;fill:none;stroke:var(--hf-accent);stroke-width:2}${selector} .hf-v2-progress-nodes{position:absolute;inset:0}${selector} aside:not(.hf-v2-safe-area){position:absolute;left:${portrait ? 760 : 1500}px;top:${portrait ? 1200 : 600}px;width:${portrait ? 180 : 180}px;height:${portrait ? 180 : 130}px}`;
}

function freezeBoxes(boxes) {
  return Object.freeze(Object.fromEntries(Object.entries(boxes).map(([name, box]) => [name, Object.freeze({...box})])));
}
