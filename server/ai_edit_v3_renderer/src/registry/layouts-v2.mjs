import {MATERIAL_FULLSCREEN_SPEAKER_PIP_CONTRACT, compileMaterialFullscreenSpeakerPip} from "./layouts/material_fullscreen_speaker_pip.mjs";
import {PRODUCT_HERO_CONTRACT, compileProductHero} from "./layouts/product_hero.mjs";
import {SPEAKER_FULLSCREEN_CONTRACT, compileSpeakerFullscreen} from "./layouts/speaker_fullscreen.mjs";
import {SPEAKER_LEFT_INFO_RIGHT_CONTRACT, compileSpeakerLeftInfoRight} from "./layouts/speaker_left_info_right.mjs";
import {SPEAKER_RIGHT_EVIDENCE_LEFT_CONTRACT, compileSpeakerRightEvidenceLeft} from "./layouts/speaker_right_evidence_left.mjs";
import {STEPS_STACK_CONTRACT, compileStepsStack} from "./layouts/steps_stack.mjs";

export const LAYOUT_V2_CONTRACTS = Object.freeze([
  SPEAKER_FULLSCREEN_CONTRACT, SPEAKER_LEFT_INFO_RIGHT_CONTRACT, SPEAKER_RIGHT_EVIDENCE_LEFT_CONTRACT,
  MATERIAL_FULLSCREEN_SPEAKER_PIP_CONTRACT, PRODUCT_HERO_CONTRACT, STEPS_STACK_CONTRACT,
]);

const COMPILERS = new Map([
  [SPEAKER_FULLSCREEN_CONTRACT.id, compileSpeakerFullscreen],
  [SPEAKER_LEFT_INFO_RIGHT_CONTRACT.id, compileSpeakerLeftInfoRight],
  [SPEAKER_RIGHT_EVIDENCE_LEFT_CONTRACT.id, compileSpeakerRightEvidenceLeft],
  [MATERIAL_FULLSCREEN_SPEAKER_PIP_CONTRACT.id, compileMaterialFullscreenSpeakerPip],
  [PRODUCT_HERO_CONTRACT.id, compileProductHero],
  [STEPS_STACK_CONTRACT.id, compileStepsStack],
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
