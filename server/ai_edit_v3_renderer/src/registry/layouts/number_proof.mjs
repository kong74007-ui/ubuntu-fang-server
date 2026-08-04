import {assetOrFallback, assertLayoutInput, clipAttributes, createContract, layoutResult, proofSlot} from "./layout-v2-primitives.mjs";

export const NUMBER_PROOF_CONTRACT = createContract({
  id: "number_proof", moduleId: "layouts/number_proof@2.0.0",
  variants: ["hero_number", "metric_grid", "chart_callout"], requiredSlots: ["proof"], optionalSlots: ["evidence"], identitySlots: [],
});

export function compileNumberProof(input) {
  const prepared = assertLayoutInput(NUMBER_PROOF_CONTRACT, input);
  const proof = proofSlot({prefix: prepared.prefix, value: prepared.slots.proof, duration: prepared.duration, trackIndex: 3});
  const evidence = assetOrFallback({prefix: prepared.prefix, slot: "evidence", value: prepared.slots.evidence, duration: prepared.duration, trackIndex: 4});
  const bodies = {
    hero_number: `<main class="hf-v2-hero-number"><header><span></span></header>${proof}<aside>${evidence}</aside></main>`,
    metric_grid: `<section class="hf-v2-metric-grid"><div>${proof}</div><ul><li><b></b></li><li><b></b></li><li><b></b></li></ul><footer>${evidence}</footer></section>`,
    chart_callout: `<article class="hf-v2-chart-callout"><svg viewBox="0 0 100 60" aria-hidden="true"><path d="M5 52 25 35 45 43 66 18 95 8"></path><circle cx="66" cy="18" r="3"></circle></svg><aside>${proof}</aside><figure>${evidence}<figcaption class="clip" ${clipAttributes(prepared.duration, 2)}><i></i></figcaption></figure></article>`,
  };
  return layoutResult({contract: NUMBER_PROOF_CONTRACT, variantId: input.variantId, ratio: input.ratio, input: prepared, structure: `number-${input.variantId}`, body: bodies[input.variantId], criticalRegions: regions(input.variantId, input.ratio)});
}

function regions(variantId, ratio) {
  const portrait = ratio === "9:16";
  return {
    hero_number: portrait ? {proof: {x: 90, y: 340, width: 900, height: 420}, evidence: {x: 180, y: 870, width: 720, height: 430}} : {proof: {x: 160, y: 180, width: 1040, height: 500}, evidence: {x: 1270, y: 250, width: 500, height: 450}},
    metric_grid: portrait ? {proof: {x: 90, y: 260, width: 900, height: 340}, evidence: {x: 90, y: 980, width: 900, height: 430}} : {proof: {x: 140, y: 170, width: 700, height: 360}, evidence: {x: 1050, y: 190, width: 690, height: 590}},
    chart_callout: portrait ? {proof: {x: 580, y: 300, width: 420, height: 360}, evidence: {x: 90, y: 820, width: 900, height: 540}} : {proof: {x: 1190, y: 160, width: 560, height: 360}, evidence: {x: 180, y: 610, width: 720, height: 330}},
  }[variantId];
}
