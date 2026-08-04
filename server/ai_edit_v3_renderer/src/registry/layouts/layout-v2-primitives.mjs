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
  if (typeof overlays !== "string") throw new Error("layout_overlays_invalid");
  return Object.freeze({prefix, duration: seconds(durationMs), slots, style: styleFromTokens(designTokens), overlays});
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
    ? `<video muted playsinline preload="metadata" src="${asset.path}"></video>`
    : `<img alt="" src="${asset.path}">`;
  return `<div id="${prefix}_speaker" class="hf-v2-speaker clip" data-slot="speaker" data-v2-region="speaker" ${attrs}>${element}</div>`;
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
  const publicSlots = Object.fromEntries([...contract.requiredSlots, ...contract.optionalSlots].map((slot) => [slot, `#${input.prefix}_${slot}`]));
  const html = `<section id="${root}" class="hf-v2-layout hf-v2-layout-${contract.id} clip" data-layout-v2="${contract.id}" data-layout-variant="${variantId}" data-layout-ratio="${ratio}" data-layout-structure="${structure}" data-start="0" data-duration="${input.duration}" data-track-index="1"${input.style}>${body}<aside id="${safe}" class="hf-v2-safe-area clip" data-safe-area="${ratio}" ${clipAttributes(input.duration, 20)}>${input.overlays}</aside><style data-layout-audit="${contract.id}">${layoutCss({contract, variantId, ratio, criticalRegions})}</style></section>`;
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
  return `${selector}{position:absolute;inset:0;overflow:hidden}${selector} .hf-v2-safe-area{position:absolute;inset:0;z-index:20}${selector} .hf-v2-slot>img,${selector} .hf-v2-slot>video{width:100%;height:100%;object-fit:var(--hf-image-fit)}${selector} .hf-v2-fallback{display:grid;place-items:center;background:var(--hf-surface)}${selector} .hf-v2-fallback svg{width:42%;height:42%;fill:none;stroke:var(--hf-accent);stroke-width:6}${regions}`;
}

function freezeBoxes(boxes) {
  return Object.freeze(Object.fromEntries(Object.entries(boxes).map(([name, box]) => [name, Object.freeze({...box})])));
}
