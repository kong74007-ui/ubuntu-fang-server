import {assetOrFallback, assertLayoutInput, clipAttributes, createContract, layoutResult, speakerSlot} from "./layout-v2-primitives.mjs";

export const SPEAKER_FULLSCREEN_CONTRACT = createContract({
  id: "speaker_fullscreen", moduleId: "layouts/speaker_fullscreen@2.0.0",
  variants: ["clean_center", "headline_top", "caption_sidebar"], requiredSlots: ["speaker"], optionalSlots: ["evidence"], identitySlots: ["speaker"],
});

export function compileSpeakerFullscreen(input) {
  const prepared = assertLayoutInput(SPEAKER_FULLSCREEN_CONTRACT, input);
  const speaker = speakerSlot({prefix: prepared.prefix, value: prepared.slots.speaker, duration: prepared.duration, trackIndex: 3});
  const evidence = assetOrFallback({prefix: prepared.prefix, slot: "evidence", value: prepared.slots.evidence, duration: prepared.duration, trackIndex: 4});
  const bodies = {
    clean_center: `<main class="hf-v2-speaker-stage">${speaker}</main><footer class="hf-v2-evidence-dock">${evidence}</footer>`,
    headline_top: `<header id="${prepared.prefix}_headline_band" class="hf-v2-headline-band clip" ${clipAttributes(prepared.duration, 2)}></header><main class="hf-v2-speaker-stage">${speaker}${evidence}</main>`,
    caption_sidebar: `<main class="hf-v2-speaker-stage">${speaker}</main><aside class="hf-v2-caption-rail">${evidence}</aside>`,
  };
  const critical = input.ratio === "16:9" ? {speaker: {x: 550, y: 96, width: 820, height: 650}} : {speaker: {x: 120, y: 340, width: 840, height: 940}};
  return layoutResult({contract: SPEAKER_FULLSCREEN_CONTRACT, variantId: input.variantId, ratio: input.ratio, input: prepared, structure: `speaker-${input.variantId}`, body: bodies[input.variantId], criticalRegions: critical});
}
