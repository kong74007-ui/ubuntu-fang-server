import {COMPARISON_SPLIT_CONTRACT, compileComparisonSplit} from "./layouts/comparison_split.mjs";
import {CTA_OFFER_CONTRACT, compileCtaOffer} from "./layouts/cta_offer.mjs";
import {EDITORIAL_COLLAGE_CONTRACT, compileEditorialCollage} from "./layouts/editorial_collage.mjs";
import {MATERIAL_FULLSCREEN_SPEAKER_PIP_CONTRACT, compileMaterialFullscreenSpeakerPip} from "./layouts/material_fullscreen_speaker_pip.mjs";
import {METHOD_TIMELINE_CONTRACT, compileMethodTimeline} from "./layouts/method_timeline.mjs";
import {NUMBER_PROOF_CONTRACT, compileNumberProof} from "./layouts/number_proof.mjs";
import {PRODUCT_HERO_CONTRACT, compileProductHero} from "./layouts/product_hero.mjs";
import {QUOTE_REVERSAL_CONTRACT, compileQuoteReversal} from "./layouts/quote_reversal.mjs";
import {SPEAKER_FULLSCREEN_CONTRACT, compileSpeakerFullscreen} from "./layouts/speaker_fullscreen.mjs";
import {SPEAKER_LEFT_INFO_RIGHT_CONTRACT, compileSpeakerLeftInfoRight} from "./layouts/speaker_left_info_right.mjs";
import {SPEAKER_RIGHT_EVIDENCE_LEFT_CONTRACT, compileSpeakerRightEvidenceLeft} from "./layouts/speaker_right_evidence_left.mjs";
import {STEPS_STACK_CONTRACT, compileStepsStack} from "./layouts/steps_stack.mjs";

export const LAYOUT_V2_CONTRACTS = Object.freeze([
  SPEAKER_FULLSCREEN_CONTRACT, SPEAKER_LEFT_INFO_RIGHT_CONTRACT, SPEAKER_RIGHT_EVIDENCE_LEFT_CONTRACT,
  MATERIAL_FULLSCREEN_SPEAKER_PIP_CONTRACT, PRODUCT_HERO_CONTRACT, EDITORIAL_COLLAGE_CONTRACT,
  COMPARISON_SPLIT_CONTRACT, STEPS_STACK_CONTRACT, NUMBER_PROOF_CONTRACT, QUOTE_REVERSAL_CONTRACT,
  METHOD_TIMELINE_CONTRACT, CTA_OFFER_CONTRACT,
]);

const COMPILERS = new Map([
  [SPEAKER_FULLSCREEN_CONTRACT.id, compileSpeakerFullscreen],
  [SPEAKER_LEFT_INFO_RIGHT_CONTRACT.id, compileSpeakerLeftInfoRight],
  [SPEAKER_RIGHT_EVIDENCE_LEFT_CONTRACT.id, compileSpeakerRightEvidenceLeft],
  [MATERIAL_FULLSCREEN_SPEAKER_PIP_CONTRACT.id, compileMaterialFullscreenSpeakerPip],
  [PRODUCT_HERO_CONTRACT.id, compileProductHero],
  [EDITORIAL_COLLAGE_CONTRACT.id, compileEditorialCollage],
  [COMPARISON_SPLIT_CONTRACT.id, compileComparisonSplit],
  [STEPS_STACK_CONTRACT.id, compileStepsStack],
  [NUMBER_PROOF_CONTRACT.id, compileNumberProof],
  [QUOTE_REVERSAL_CONTRACT.id, compileQuoteReversal],
  [METHOD_TIMELINE_CONTRACT.id, compileMethodTimeline],
  [CTA_OFFER_CONTRACT.id, compileCtaOffer],
]);
const CONTRACTS = new Map(LAYOUT_V2_CONTRACTS.map((contract) => [contract.id, contract]));

export function getLayoutV2Contracts() {
  return LAYOUT_V2_CONTRACTS;
}

export function resolveLayoutV2(layoutId, variantId, ratio) {
  const contract = CONTRACTS.get(layoutId);
  if (!contract) throw new Error("layout_unknown");
  if (!contract.variants.includes(variantId)) throw new Error("layout_variant_unknown");
  if (!contract.supportedRatios.includes(ratio)) throw new Error("layout_ratio_unknown");
  const compile = COMPILERS.get(layoutId);
  return Object.freeze({contract, variantId, ratio, compile: (input) => compile({layoutId, variantId, ratio, ...input})});
}
