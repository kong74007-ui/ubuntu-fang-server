import {assetOrFallback, assertLayoutInput, clipAttributes, createContract, layoutResult} from "./layout-v2-primitives.mjs";

export const EDITORIAL_COLLAGE_CONTRACT = createContract({
  id: "editorial_collage", moduleId: "layouts/editorial_collage@2.0.0",
  variants: ["magazine_grid", "layered_cards", "film_strip"], requiredSlots: ["primary"], optionalSlots: ["detail"], identitySlots: ["primary"],
});

export function compileEditorialCollage(input) {
  const prepared = assertLayoutInput(EDITORIAL_COLLAGE_CONTRACT, input);
  const primary = assetOrFallback({prefix: prepared.prefix, slot: "primary", value: prepared.slots.primary, duration: prepared.duration, trackIndex: 3});
  const detail = assetOrFallback({prefix: prepared.prefix, slot: "detail", value: prepared.slots.detail, duration: prepared.duration, trackIndex: 4});
  const bodies = {
    magazine_grid: `<main class="hf-v2-magazine-grid"><header class="clip" ${clipAttributes(prepared.duration, 2)}><i></i><i></i></header><figure>${primary}</figure><aside>${detail}</aside><footer><b></b><b></b><b></b></footer></main>`,
    layered_cards: `<article class="hf-v2-layered-cards"><section class="hf-v2-layer-back"><svg viewBox="0 0 100 100" aria-hidden="true"><rect x="8" y="8" width="84" height="84" rx="8"></rect></svg></section><figure>${primary}<figcaption><span></span></figcaption></figure><aside>${detail}</aside></article>`,
    film_strip: `<section class="hf-v2-film-strip"><nav><i></i><i></i><i></i><i></i></nav><ol><li>${primary}</li><li>${detail}</li></ol><footer><svg viewBox="0 0 100 20" aria-hidden="true"><path d="M0 10h100"></path></svg></footer></section>`,
  };
  return layoutResult({contract: EDITORIAL_COLLAGE_CONTRACT, variantId: input.variantId, ratio: input.ratio, input: prepared, structure: `editorial-${input.variantId}`, body: bodies[input.variantId], criticalRegions: regions(input.variantId, input.ratio)});
}

function regions(variantId, ratio) {
  const portrait = ratio === "9:16";
  return {
    magazine_grid: portrait ? {primary: {x: 70, y: 250, width: 650, height: 980}, detail: {x: 680, y: 900, width: 330, height: 470}} : {primary: {x: 110, y: 160, width: 1080, height: 760}, detail: {x: 1260, y: 240, width: 520, height: 560}},
    layered_cards: portrait ? {primary: {x: 120, y: 330, width: 780, height: 1040}, detail: {x: 630, y: 220, width: 340, height: 450}} : {primary: {x: 310, y: 130, width: 1100, height: 790}, detail: {x: 1260, y: 170, width: 430, height: 520}},
    film_strip: portrait ? {primary: {x: 70, y: 410, width: 940, height: 620}, detail: {x: 180, y: 1070, width: 720, height: 390}} : {primary: {x: 130, y: 250, width: 1060, height: 600}, detail: {x: 1230, y: 250, width: 560, height: 600}},
  }[variantId];
}
