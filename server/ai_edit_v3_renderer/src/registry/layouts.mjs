import {assertSafeId, escapeAttribute, seconds} from "./layout-primitives.mjs";

const IDS = [
  "comparison_split", "cta_offer", "editorial_collage", "material_fullscreen_speaker_pip",
  "method_timeline", "number_proof", "product_hero", "quote_reversal", "speaker_fullscreen",
  "speaker_left_info_right", "speaker_right_evidence_left", "steps_stack",
];
const RATIOS = Object.freeze(["16:9", "9:16"]);
const VARIANTS = Object.freeze(["balanced_a", "emphasis_b"]);
const OVERLAYS = Object.freeze([
  "bullet_list", "chapter_label", "cta_hold", "emphasis_caption", "headline_block", "info_card",
  "lower_third", "number_proof", "product_tag", "quote_card", "standard_caption", "step_indicator",
]);
const ANIMATIONS = Object.freeze([
  "card_reveal", "count_up", "fade", "highlight_draw", "image_pan_zoom", "light_sweep", "rotate",
  "scale", "slide", "split_screen", "stagger", "stamp", "subtitle_pop", "wipe",
]);

export const LAYOUT_CONTRACTS = Object.freeze(IDS.map((id) => Object.freeze({
  id,
  version: "1.0.0",
  supportedRatios: RATIOS,
  variants: VARIANTS,
  requiredSlots: Object.freeze(id.startsWith("speaker_") ? ["speaker"] : id === "cta_offer" ? ["message"] : ["primary"]),
  optionalSlots: Object.freeze(["image", "evidence", "product"]),
  fallbackVariant: "balanced_a",
  allowedOverlays: OVERLAYS,
  allowedAnimations: ANIMATIONS,
  safeAreas: Object.freeze({"16:9": "landscape_title_caption_safe", "9:16": "portrait_title_caption_safe"}),
})));

const BY_ID = new Map(LAYOUT_CONTRACTS.map((contract) => [contract.id, contract]));

export function getLayoutContract(layoutId, variantId, ratio) {
  const contract = BY_ID.get(layoutId);
  if (!contract) throw new Error("layout_unknown");
  if (!contract.variants.includes(variantId)) throw new Error("layout_variant_unknown");
  if (!contract.supportedRatios.includes(ratio)) throw new Error("layout_ratio_unknown");
  return Object.freeze({contract, variantId, ratio, geometry: geometryFor(layoutId, variantId, ratio)});
}

export function compileLayout({layoutId, variantId, ratio, scene, assets, idPrefix, durationMs, overlays, hasVideo}) {
  void scene;
  const resolved = getLayoutContract(layoutId, variantId, ratio);
  const prefix = assertSafeId(idPrefix, "id_prefix");
  const duration = seconds(durationMs);
  const optionalAssets = Array.isArray(assets) ? assets.slice(0, 6) : [];
  const media = optionalAssets.map((asset, index) => assetElement({asset, index, prefix, duration})).join("");
  const fallback = optionalAssets.length === 0
    ? `<div id="${prefix}_fallback" class="hf-fallback clip" data-fallback="no_optional_media" data-start="0" data-duration="${duration}" data-track-index="3"><span>${hasVideo ? "主体画面" : "AI 视觉节奏"}</span></div>`
    : "";
  const speaker = `<div id="${prefix}_speaker" class="hf-speaker-zone clip" data-start="0" data-duration="${duration}" data-track-index="2"><span>${hasVideo ? "主体视频" : "旁白主线"}</span></div>`;
  const frame = `<div id="${prefix}_frame" class="hf-layout-frame hf-layout-${layoutId} hf-variant-${variantId} clip" data-layout-id="${layoutId}" data-layout-variant="${variantId}" data-start="0" data-duration="${duration}" data-track-index="1">${speaker}<div id="${prefix}_materials" class="hf-materials">${media}${fallback}</div></div>`;
  return `<div id="${prefix}_background" class="hf-background clip" data-start="0" data-duration="${duration}" data-track-index="0"></div>${frame}<div id="${prefix}_safe" class="hf-safe-area clip" data-start="0" data-duration="${duration}" data-track-index="20">${overlays ?? ""}</div>`;
}

function assetElement({asset, index, prefix, duration}) {
  if (!asset || typeof asset !== "object" || !["image", "video"].includes(asset.kind)) throw new Error("layout_asset_invalid");
  const assetId = assertSafeId(asset.id, "asset_id");
  const relativePath = asset.relativePath ?? asset.path;
  if (typeof relativePath !== "string" || !/^(?!\/)(?![A-Za-z]:)(?!.*\\)(?!.*(?:^|\/)\.\.(?:\/|$))[A-Za-z0-9._/-]+$/u.test(relativePath)) {
    throw new Error("layout_asset_path_invalid");
  }
  const src = escapeAttribute(relativePath);
  const id = `${prefix}_asset_${assetId}`;
  if (asset.kind === "video") return `<video id="${id}" class="hf-asset hf-asset-${index} clip" muted playsinline preload="metadata" src="${src}" data-start="0" data-duration="${duration}" data-track-index="${index + 3}"></video>`;
  return `<img id="${id}" class="hf-asset hf-asset-${index} clip" alt="" src="${src}" data-start="0" data-duration="${duration}" data-track-index="${index + 3}">`;
}

function geometryFor(layoutId, variantId, ratio) {
  const [width, height] = ratio === "16:9" ? [1920, 1080] : [1080, 1920];
  const emphasis = variantId === "emphasis_b";
  const splitLeft = layoutId === "speaker_right_evidence_left" || layoutId === "comparison_split";
  const splitRight = layoutId === "speaker_left_info_right";
  const face = ratio === "16:9"
    ? {x: splitLeft ? 1160 : splitRight ? 220 : 700, y: 90, width: 520, height: 560}
    : {x: 240, y: 140, width: 600, height: 680};
  const text = ratio === "16:9"
    ? {x: 100, y: 760, width: 1720, height: 230}
    : {x: 70, y: 1260, width: 940, height: 520};
  const product = (layoutId === "product_hero" || layoutId === "material_fullscreen_speaker_pip")
    ? (ratio === "16:9" ? {x: 680, y: 90, width: 560, height: 560} : {x: 210, y: 130, width: 660, height: 720})
    : undefined;
  const boxes = {face_critical: face, text_safe: text, title_safe: ratio === "16:9" ? {x: 100, y: 60, width: 900, height: 160} : {x: 70, y: 70, width: 940, height: 180}};
  if (product) boxes.product_critical = product;
  return Object.freeze({width, height, emphasis, boxes: Object.freeze(boxes)});
}
