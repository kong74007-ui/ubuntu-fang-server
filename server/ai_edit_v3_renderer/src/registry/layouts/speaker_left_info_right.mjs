import {assetOrFallback, assertLayoutInput, clipAttributes, createContract, layoutResult, speakerSlot} from "./layout-v2-primitives.mjs";

export const SPEAKER_LEFT_INFO_RIGHT_CONTRACT = createContract({
  id: "speaker_left_info_right", moduleId: "layouts/speaker_left_info_right@2.0.0",
  variants: ["card_stack", "number_focus", "image_evidence"], requiredSlots: ["speaker"], optionalSlots: ["evidence"], identitySlots: ["speaker"],
});

export function compileSpeakerLeftInfoRight(input) {
  const prepared = assertLayoutInput(SPEAKER_LEFT_INFO_RIGHT_CONTRACT, input);
  const speaker = speakerSlot({prefix: prepared.prefix, value: prepared.slots.speaker, duration: prepared.duration, trackIndex: 3});
  const evidence = assetOrFallback({prefix: prepared.prefix, slot: "evidence", value: prepared.slots.evidence, duration: prepared.duration, trackIndex: 4});
  const bodies = {
    card_stack: `<main class="hf-v2-left-speaker-card">${speaker}</main><aside class="hf-v2-right-card-stack"><header class="clip" ${clipAttributes(prepared.duration, 2)}><i></i><i></i></header><section>${evidence}</section><footer><b></b><b></b></footer></aside>`,
    number_focus: `<article class="hf-v2-left-speaker-number"><figure>${speaker}</figure><aside class="hf-v2-number-proof"><svg viewBox="0 0 100 100" aria-hidden="true"><circle cx="50" cy="50" r="42"></circle><path d="M28 56h44"></path></svg><section>${evidence}</section></aside></article>`,
    image_evidence: `<section class="hf-v2-left-speaker-evidence"><div class="hf-v2-evidence-canvas">${evidence}</div><figure class="hf-v2-speaker-cutout">${speaker}<figcaption><span></span></figcaption></figure></section>`,
  };
  return layoutResult({
    contract: SPEAKER_LEFT_INFO_RIGHT_CONTRACT, variantId: input.variantId, ratio: input.ratio, input: prepared,
    structure: `speaker-left-${input.variantId}`, body: bodies[input.variantId], criticalRegions: regions(input.variantId, input.ratio),
  });
}

function regions(variantId, ratio) {
  const portrait = ratio === "9:16";
  const byVariant = {
    card_stack: portrait ? {speaker: {x: 60, y: 320, width: 610, height: 920}, evidence: {x: 700, y: 460, width: 320, height: 520}} : {speaker: {x: 96, y: 140, width: 980, height: 760}, evidence: {x: 1160, y: 230, width: 620, height: 500}},
    number_focus: portrait ? {speaker: {x: 70, y: 470, width: 560, height: 860}, evidence: {x: 660, y: 280, width: 360, height: 360}} : {speaker: {x: 120, y: 170, width: 900, height: 720}, evidence: {x: 1240, y: 360, width: 450, height: 330}},
    image_evidence: portrait ? {speaker: {x: 80, y: 690, width: 620, height: 840}, evidence: {x: 570, y: 210, width: 450, height: 780}} : {speaker: {x: 110, y: 210, width: 760, height: 700}, evidence: {x: 780, y: 120, width: 1040, height: 760}},
  };
  return byVariant[variantId];
}
