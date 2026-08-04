import {assetOrFallback, assertLayoutInput, clipAttributes, createContract, layoutResult} from "./layout-v2-primitives.mjs";

export const PRODUCT_HERO_CONTRACT = createContract({
  id: "product_hero", moduleId: "layouts/product_hero@2.0.0",
  variants: ["center_pedestal", "split_copy", "detail_gallery"], requiredSlots: ["primary"], optionalSlots: ["detail"], identitySlots: ["primary"],
});

export function compileProductHero(input) {
  const prepared = assertLayoutInput(PRODUCT_HERO_CONTRACT, input);
  const primary = assetOrFallback({prefix: prepared.prefix, slot: "primary", value: prepared.slots.primary, duration: prepared.duration, trackIndex: 3});
  const detail = assetOrFallback({prefix: prepared.prefix, slot: "detail", value: prepared.slots.detail, duration: prepared.duration, trackIndex: 4});
  const bodies = {
    center_pedestal: `<figure class="hf-v2-product-pedestal">${primary}<figcaption class="hf-v2-product-plinth clip" ${clipAttributes(prepared.duration, 2)}></figcaption></figure><aside class="hf-v2-detail-orbit">${detail}</aside>`,
    split_copy: `<section class="hf-v2-product-copy clip" ${clipAttributes(prepared.duration, 2)}></section><figure class="hf-v2-product-frame">${primary}${detail}</figure>`,
    detail_gallery: `<main class="hf-v2-product-gallery"><figure>${primary}</figure><ul class="hf-v2-detail-strip"><li>${detail}</li></ul></main>`,
  };
  const critical = input.ratio === "16:9" ? {product: {x: 610, y: 170, width: 700, height: 570}} : {product: {x: 150, y: 440, width: 780, height: 720}};
  return layoutResult({contract: PRODUCT_HERO_CONTRACT, variantId: input.variantId, ratio: input.ratio, input: prepared, structure: `product-${input.variantId}`, body: bodies[input.variantId], criticalRegions: critical});
}
