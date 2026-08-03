import {assertSafeId, assertSafeText, escapeAttribute, seconds} from "./layout-primitives.mjs";

const DEFINITIONS = [
  ["bullet_list", 220, 5, true],
  ["chapter_label", 28, 1, true],
  ["cta_hold", 80, 2, true],
  ["emphasis_caption", 64, 2, true],
  ["headline_block", 60, 2, true],
  ["info_card", 180, 5, true],
  ["lower_third", 48, 2, true],
  ["number_proof", 42, 2, true],
  ["product_tag", 48, 2, true],
  ["quote_card", 120, 4, true],
  ["standard_caption", 80, 2, false],
  ["step_indicator", 32, 1, true],
];

export const OVERLAY_CONTRACTS = Object.freeze(DEFINITIONS.map(([id, maxChars, maxLines, optional]) => Object.freeze({
  id,
  version: "1.0.0",
  maxChars,
  maxLines,
  optional,
  safeArea: "title_caption_safe",
  allowedAnimationTargets: Object.freeze([id]),
})));

const BY_ID = new Map(OVERLAY_CONTRACTS.map((contract) => [contract.id, contract]));

export function getOverlayContract(id) {
  const contract = BY_ID.get(id);
  if (!contract) throw new Error("overlay_unknown");
  return contract;
}

export function compileOverlay({overlayId, idPrefix, text, durationMs, startMs = 0, trackIndex = 2}) {
  const contract = getOverlayContract(overlayId);
  const prefix = assertSafeId(idPrefix, "id_prefix");
  const normalized = assertSafeText(text ?? "", contract);
  if (!normalized && contract.optional) return "";
  const safeText = escapeAttribute(normalized || " ");
  return `<div id="${prefix}_${contract.id}" class="hf-overlay hf-overlay-${contract.id} clip" data-overlay-id="${contract.id}" data-safe-text="${safeText}" data-start="${seconds(startMs)}" data-duration="${seconds(durationMs)}" data-track-index="${trackIndex}"><span></span></div>`;
}
