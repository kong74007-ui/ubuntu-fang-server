import {assetOrFallback, assertLayoutInput, clipAttributes, createContract, layoutResult} from "./layout-v2-primitives.mjs";

export const COMPARISON_SPLIT_CONTRACT = createContract({
  id: "comparison_split", moduleId: "layouts/comparison_split@2.0.0",
  variants: ["vertical_divide", "before_after_slider", "score_compare"], requiredSlots: ["primary"], optionalSlots: ["detail"], identitySlots: ["primary"],
});

export function compileComparisonSplit(input) {
  const prepared = assertLayoutInput(COMPARISON_SPLIT_CONTRACT, input);
  const primary = assetOrFallback({prefix: prepared.prefix, slot: "primary", value: prepared.slots.primary, duration: prepared.duration, trackIndex: 3});
  const detail = assetOrFallback({prefix: prepared.prefix, slot: "detail", value: prepared.slots.detail, duration: prepared.duration, trackIndex: 4});
  const bodies = {
    vertical_divide: `<main class="hf-v2-vertical-compare"><section>${primary}</section><div class="hf-v2-compare-divider"><span></span></div><aside>${detail}</aside></main>`,
    before_after_slider: `<figure class="hf-v2-before-after"><div>${primary}</div><aside>${detail}</aside><figcaption id="${prepared.prefix}_comparison_slider" class="clip" ${clipAttributes(prepared.duration, 2)}><span></span><i></i></figcaption></figure>`,
    score_compare: `<article class="hf-v2-score-compare"><header><b></b><b></b></header><section>${primary}<meter min="0" max="100" value="72"></meter></section><aside>${detail}<meter min="0" max="100" value="48"></meter></aside><footer><i></i></footer></article>`,
  };
  return layoutResult({contract: COMPARISON_SPLIT_CONTRACT, variantId: input.variantId, ratio: input.ratio, input: prepared, structure: `comparison-${input.variantId}`, body: bodies[input.variantId], criticalRegions: regions(input.variantId, input.ratio)});
}

function regions(variantId, ratio) {
  const portrait = ratio === "9:16";
  return {
    vertical_divide: portrait ? {primary: {x: 70, y: 260, width: 940, height: 600}, detail: {x: 70, y: 900, width: 940, height: 560}} : {primary: {x: 110, y: 170, width: 800, height: 720}, detail: {x: 1010, y: 170, width: 800, height: 720}},
    before_after_slider: portrait ? {primary: {x: 90, y: 300, width: 900, height: 1040}, detail: {x: 540, y: 300, width: 450, height: 1040}} : {primary: {x: 130, y: 150, width: 1660, height: 760}, detail: {x: 960, y: 150, width: 830, height: 760}},
    score_compare: portrait ? {primary: {x: 90, y: 360, width: 420, height: 780}, detail: {x: 570, y: 360, width: 420, height: 780}} : {primary: {x: 180, y: 230, width: 700, height: 620}, detail: {x: 1040, y: 230, width: 700, height: 620}},
  }[variantId];
}
