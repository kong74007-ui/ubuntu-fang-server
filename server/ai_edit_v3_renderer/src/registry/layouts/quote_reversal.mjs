import {assetOrFallback, assertLayoutInput, clipAttributes, createContract, layoutResult, textSlot} from "./layout-v2-primitives.mjs";

export const QUOTE_REVERSAL_CONTRACT = createContract({
  id: "quote_reversal", moduleId: "layouts/quote_reversal@2.0.0",
  variants: ["diagonal_statement", "strike_reveal", "question_answer"], requiredSlots: ["quote"], optionalSlots: ["evidence"], identitySlots: [],
});

export function compileQuoteReversal(input) {
  const prepared = assertLayoutInput(QUOTE_REVERSAL_CONTRACT, input);
  const quote = textSlot({prefix: prepared.prefix, slot: "quote", value: prepared.slots.quote, duration: prepared.duration, trackIndex: 3, maxChars: 180, maxLines: 4});
  const evidence = assetOrFallback({prefix: prepared.prefix, slot: "evidence", value: prepared.slots.evidence, duration: prepared.duration, trackIndex: 4});
  const bodies = {
    diagonal_statement: `<blockquote class="hf-v2-diagonal-statement"><svg viewBox="0 0 100 100" aria-hidden="true"><path d="M0 85 100 15"></path></svg>${quote}<footer>${evidence}</footer></blockquote>`,
    strike_reveal: `<article class="hf-v2-strike-reveal"><header><del><span></span></del></header><section>${quote}<i></i></section><aside>${evidence}</aside></article>`,
    question_answer: `<main class="hf-v2-question-answer"><section><b>?</b>${evidence}</section><div class="hf-v2-answer-divider"><span></span></div><aside>${quote}<footer class="clip" ${clipAttributes(prepared.duration, 2)}><i></i><i></i></footer></aside></main>`,
  };
  return layoutResult({contract: QUOTE_REVERSAL_CONTRACT, variantId: input.variantId, ratio: input.ratio, input: prepared, structure: `quote-${input.variantId}`, body: bodies[input.variantId], criticalRegions: regions(input.variantId, input.ratio)});
}

function regions(variantId, ratio) {
  const portrait = ratio === "9:16";
  return {
    diagonal_statement: portrait ? {quote: {x: 90, y: 360, width: 900, height: 520}, evidence: {x: 530, y: 940, width: 450, height: 420}} : {quote: {x: 180, y: 220, width: 1040, height: 480}, evidence: {x: 1270, y: 350, width: 500, height: 400}},
    strike_reveal: portrait ? {quote: {x: 100, y: 650, width: 880, height: 500}, evidence: {x: 140, y: 230, width: 800, height: 350}} : {quote: {x: 240, y: 360, width: 1080, height: 390}, evidence: {x: 1370, y: 220, width: 370, height: 500}},
    question_answer: portrait ? {quote: {x: 110, y: 900, width: 860, height: 430}, evidence: {x: 160, y: 260, width: 760, height: 480}} : {quote: {x: 990, y: 240, width: 750, height: 500}, evidence: {x: 170, y: 240, width: 650, height: 500}},
  }[variantId];
}
