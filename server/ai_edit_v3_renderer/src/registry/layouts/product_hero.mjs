import {assetOrFallback, assertLayoutInput, clipAttributes, copySlot, createContract, layoutResult} from "./layout-v2-primitives.mjs";

export const PRODUCT_HERO_CONTRACT = createContract({
  id: "product_hero", moduleId: "layouts/product_hero@2.0.0",
  variants: ["center_pedestal", "split_copy", "detail_gallery"], requiredSlots: ["primary"], optionalSlots: ["detail"], identitySlots: ["primary"],
});

export function compileProductHero(input) {
  const prepared = assertLayoutInput(PRODUCT_HERO_CONTRACT, input);
  const primary = assetOrFallback({prefix: prepared.prefix, slot: "primary", value: prepared.slots.primary, duration: prepared.duration, trackIndex: 3});
  const detail = assetOrFallback({prefix: prepared.prefix, slot: "detail", value: prepared.slots.detail, duration: prepared.duration, trackIndex: 4});
  const copy = copySlot(prepared.slots.copy);
  const bodies = {
    center_pedestal: `<figure class="hf-v2-product-pedestal">${primary}<figcaption id="${prepared.prefix}_product_plinth" class="hf-v2-product-plinth clip" ${clipAttributes(prepared.duration, 2)}></figcaption></figure><aside class="hf-v2-detail-orbit">${detail}</aside>`,
    split_copy: `<section id="${prepared.prefix}_product_copy" class="hf-v2-product-copy clip" data-v2-region="copy" ${clipAttributes(prepared.duration, 2)}>${copy}</section><figure class="hf-v2-product-frame">${primary}${detail}</figure>`,
    detail_gallery: `<main class="hf-v2-product-gallery"><figure>${primary}</figure><ul class="hf-v2-detail-strip"><li>${detail}</li></ul></main>`,
  };
  const critical = input.ratio === "16:9"
    ? {primary: {x: 610, y: 170, width: 700, height: 570}, ...(input.variantId === "split_copy" ? {copy: {x: 96, y: 170, width: 430, height: 570}} : {})}
    : {primary: {x: 150, y: 440, width: 780, height: 720}, ...(input.variantId === "split_copy" ? {copy: {x: 60, y: 110, width: 960, height: 220}} : {})};
  return layoutResult({contract: PRODUCT_HERO_CONTRACT, variantId: input.variantId, ratio: input.ratio, input: prepared, structure: `product-${input.variantId}`, body: bodies[input.variantId], criticalRegions: critical});
}
