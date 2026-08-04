import {assetOrFallback, assertLayoutInput, clipAttributes, createContract, layoutResult, stepsSlot} from "./layout-v2-primitives.mjs";

export const METHOD_TIMELINE_CONTRACT = createContract({
  id: "method_timeline", moduleId: "layouts/method_timeline@2.0.0",
  variants: ["horizontal_timeline", "vertical_milestones", "chapter_route"], requiredSlots: ["steps"], optionalSlots: ["accent"], identitySlots: [],
});

export function compileMethodTimeline(input) {
  const prepared = assertLayoutInput(METHOD_TIMELINE_CONTRACT, input);
  const steps = stepsSlot({prefix: prepared.prefix, value: prepared.slots.steps, duration: prepared.duration, trackIndex: 3});
  const accent = assetOrFallback({prefix: prepared.prefix, slot: "accent", value: prepared.slots.accent, duration: prepared.duration, trackIndex: 4});
  const bodies = {
    horizontal_timeline: `<main class="hf-v2-horizontal-timeline"><svg viewBox="0 0 100 20" aria-hidden="true"><path d="M5 10h90"></path></svg>${steps}<aside>${accent}</aside></main>`,
    vertical_milestones: `<section class="hf-v2-vertical-milestones"><header class="clip" ${clipAttributes(prepared.duration, 2)}><i></i></header><nav>${steps}</nav><figure>${accent}<figcaption><span></span></figcaption></figure></section>`,
    chapter_route: `<article class="hf-v2-chapter-route"><aside>${accent}</aside><ol><li><b>01</b></li><li><b>02</b></li><li><b>03</b></li></ol><section>${steps}</section><footer><svg viewBox="0 0 100 30" aria-hidden="true"><path d="M0 15h100"></path></svg></footer></article>`,
  };
  return layoutResult({contract: METHOD_TIMELINE_CONTRACT, variantId: input.variantId, ratio: input.ratio, input: prepared, structure: `method-${input.variantId}`, body: bodies[input.variantId], criticalRegions: regions(input.variantId, input.ratio)});
}

function regions(variantId, ratio) {
  const portrait = ratio === "9:16";
  return {
    horizontal_timeline: portrait ? {steps: {x: 90, y: 470, width: 900, height: 780}, accent: {x: 710, y: 250, width: 260, height: 260}} : {steps: {x: 150, y: 340, width: 1620, height: 420}, accent: {x: 1440, y: 130, width: 270, height: 200}},
    vertical_milestones: portrait ? {steps: {x: 130, y: 300, width: 650, height: 1050}, accent: {x: 760, y: 570, width: 250, height: 420}} : {steps: {x: 260, y: 150, width: 800, height: 750}, accent: {x: 1190, y: 260, width: 500, height: 470}},
    chapter_route: portrait ? {steps: {x: 330, y: 390, width: 650, height: 900}, accent: {x: 70, y: 290, width: 230, height: 930}} : {steps: {x: 530, y: 210, width: 1180, height: 600}, accent: {x: 100, y: 210, width: 330, height: 600}},
  }[variantId];
}
