import {assertSafeId, assertSafeText, escapeAttribute, seconds} from "./layout-primitives.mjs";
import {compileOverlayComponent as compileHeadlineBlock} from "./overlays/headline_block.mjs";
import {compileOverlayComponent as compileInfoCard} from "./overlays/info_card.mjs";
import {compileOverlayComponent as compileStandardCaption} from "./overlays/standard_caption.mjs";
import {compileOverlayComponent as compileBulletList} from "./overlays/bullet_list.mjs";
import {compileOverlayComponent as compileChapterLabel} from "./overlays/chapter_label.mjs";
import {compileOverlayComponent as compileCtaHold} from "./overlays/cta_hold.mjs";
import {compileOverlayComponent as compileEmphasisCaption} from "./overlays/emphasis_caption.mjs";
import {compileOverlayComponent as compileLowerThird} from "./overlays/lower_third.mjs";
import {compileOverlayComponent as compileNumberProof} from "./overlays/number_proof.mjs";
import {compileOverlayComponent as compileProductTag} from "./overlays/product_tag.mjs";
import {compileOverlayComponent as compileQuoteCard} from "./overlays/quote_card.mjs";
import {compileOverlayComponent as compileStepIndicator} from "./overlays/step_indicator.mjs";

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
const V2_COMPILERS = new Map([
  ["bullet_list", compileBulletList],
  ["chapter_label", compileChapterLabel],
  ["cta_hold", compileCtaHold],
  ["emphasis_caption", compileEmphasisCaption],
  ["headline_block", compileHeadlineBlock],
  ["info_card", compileInfoCard],
  ["lower_third", compileLowerThird],
  ["number_proof", compileNumberProof],
  ["product_tag", compileProductTag],
  ["quote_card", compileQuoteCard],
  ["standard_caption", compileStandardCaption],
  ["step_indicator", compileStepIndicator],
]);

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

export function compileOverlayV2(context) {
  const compiler = V2_COMPILERS.get(context?.componentId);
  if (!compiler) throw new Error("overlay_v2_unavailable");
  return compiler(context);
}
