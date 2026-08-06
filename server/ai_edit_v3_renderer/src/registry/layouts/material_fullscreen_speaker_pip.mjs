import {assetOrFallback, assertLayoutInput, clipAttributes, createContract, layoutResult, speakerSlot} from "./layout-v2-primitives.mjs";

export const MATERIAL_FULLSCREEN_SPEAKER_PIP_CONTRACT = createContract({
  id: "material_fullscreen_speaker_pip", moduleId: "layouts/material_fullscreen_speaker_pip@2.0.0",
  variants: ["pip_round", "pip_card", "pip_edge"], requiredSlots: ["speaker", "primary"], optionalSlots: ["detail"], identitySlots: ["speaker", "primary"],
});

export function compileMaterialFullscreenSpeakerPip(input) {
  const prepared = assertLayoutInput(MATERIAL_FULLSCREEN_SPEAKER_PIP_CONTRACT, input);
  const speaker = speakerSlot({prefix: prepared.prefix, value: prepared.slots.speaker, duration: prepared.duration, trackIndex: 4});
  const primary = assetOrFallback({prefix: prepared.prefix, slot: "primary", value: prepared.slots.primary, duration: prepared.duration, trackIndex: 2});
  const detail = assetOrFallback({prefix: prepared.prefix, slot: "detail", value: prepared.slots.detail, duration: prepared.duration, trackIndex: 3});
  const bodies = {
    pip_round: `<main class="hf-v2-pip-round-stage"><figure class="hf-v2-pip-primary">${primary}</figure><aside class="hf-v2-pip-round">${speaker}</aside><footer class="hf-v2-pip-orbit">${detail}</footer></main>`,
    pip_card: `<article class="hf-v2-pip-card-stage"><figure>${primary}<figcaption id="${prepared.prefix}_pip_card_caption" class="clip" ${clipAttributes(prepared.duration, 2)}><i></i><i></i></figcaption></figure><section class="hf-v2-pip-card">${speaker}<footer>${detail}</footer></section></article>`,
    pip_edge: `<section class="hf-v2-pip-edge-stage"><div class="hf-v2-edge-primary">${primary}</div><aside class="hf-v2-edge-rail"><header>${detail}</header><figure>${speaker}</figure><footer><span></span></footer></aside></section>`,
  };
  return layoutResult({
    contract: MATERIAL_FULLSCREEN_SPEAKER_PIP_CONTRACT, variantId: input.variantId, ratio: input.ratio, input: prepared,
    structure: `material-pip-${input.variantId}`, body: bodies[input.variantId], criticalRegions: regions(input.variantId, input.ratio),
  });
}

function regions(variantId, ratio) {
  const portrait = ratio === "9:16";
  const byVariant = {
    pip_round: portrait ? {primary: {x: 0, y: 0, width: 1080, height: 1920}, speaker: {x: 690, y: 1090, width: 300, height: 300}} : {primary: {x: 0, y: 0, width: 1920, height: 1080}, speaker: {x: 1450, y: 620, width: 310, height: 310}},
    pip_card: portrait ? {primary: {x: 40, y: 120, width: 1000, height: 1500}, speaker: {x: 620, y: 1060, width: 360, height: 470}} : {primary: {x: 70, y: 60, width: 1780, height: 920}, speaker: {x: 1390, y: 520, width: 390, height: 430}},
    pip_edge: portrait ? {primary: {x: 0, y: 0, width: 820, height: 1920}, speaker: {x: 760, y: 820, width: 300, height: 520}} : {primary: {x: 0, y: 0, width: 1550, height: 1080}, speaker: {x: 1500, y: 280, width: 360, height: 520}},
  };
  return byVariant[variantId];
}
