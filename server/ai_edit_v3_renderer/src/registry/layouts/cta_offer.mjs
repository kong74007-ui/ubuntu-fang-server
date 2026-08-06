import {assetOrFallback, assertLayoutInput, clipAttributes, createContract, layoutResult, textSlot} from "./layout-v2-primitives.mjs";

export const CTA_OFFER_CONTRACT = createContract({
  id: "cta_offer", moduleId: "layouts/cta_offer@2.0.0",
  variants: ["offer_card", "qr_placeholder", "action_steps"], requiredSlots: ["message"], optionalSlots: ["accent"], identitySlots: [],
});

export function compileCtaOffer(input) {
  const prepared = assertLayoutInput(CTA_OFFER_CONTRACT, input);
  const message = textSlot({prefix: prepared.prefix, slot: "message", value: prepared.slots.message, duration: prepared.duration, trackIndex: 3, maxChars: 120, maxLines: 3});
  const accent = assetOrFallback({prefix: prepared.prefix, slot: "accent", value: prepared.slots.accent, duration: prepared.duration, trackIndex: 4});
  const bodies = {
    offer_card: `<main class="hf-v2-offer-card"><header><i></i><i></i></header><section>${message}<footer id="${prepared.prefix}_offer_footer" class="clip" ${clipAttributes(prepared.duration, 2)}><b></b></footer></section><aside>${accent}</aside></main>`,
    qr_placeholder: `<article class="hf-v2-qr-offer"><figure>${accent}<figcaption><svg viewBox="0 0 100 100" aria-hidden="true"><path d="M8 8h28v28H8zM64 8h28v28H64zM8 64h28v28H8zM58 58h12v12H58zM78 58h14v34H78zM58 78h12v14H58z"></path></svg></figcaption></figure><section>${message}</section></article>`,
    action_steps: `<section class="hf-v2-action-steps"><nav><ol><li><b>1</b></li><li><b>2</b></li><li><b>3</b></li></ol></nav><main>${message}</main><aside>${accent}</aside><footer><span></span></footer></section>`,
  };
  return layoutResult({contract: CTA_OFFER_CONTRACT, variantId: input.variantId, ratio: input.ratio, input: prepared, structure: `cta-${input.variantId}`, body: bodies[input.variantId], criticalRegions: regions(input.variantId, input.ratio)});
}

function regions(variantId, ratio) {
  const portrait = ratio === "9:16";
  return {
    offer_card: portrait ? {message: {x: 100, y: 480, width: 880, height: 430}, accent: {x: 250, y: 980, width: 580, height: 360}} : {message: {x: 260, y: 240, width: 930, height: 420}, accent: {x: 1280, y: 240, width: 430, height: 420}},
    qr_placeholder: portrait ? {message: {x: 100, y: 1050, width: 880, height: 300}, accent: {x: 250, y: 300, width: 580, height: 580}} : {message: {x: 930, y: 310, width: 760, height: 340}, accent: {x: 250, y: 230, width: 520, height: 520}},
    action_steps: portrait ? {message: {x: 130, y: 620, width: 820, height: 360}, accent: {x: 310, y: 1080, width: 460, height: 300}} : {message: {x: 540, y: 260, width: 840, height: 380}, accent: {x: 1410, y: 270, width: 330, height: 360}},
  }[variantId];
}
