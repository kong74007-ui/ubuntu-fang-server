import {assetOrFallback, assertLayoutInput, clipAttributes, createContract, layoutResult, stepsSlot} from "./layout-v2-primitives.mjs";

export const STEPS_STACK_CONTRACT = createContract({
  id: "steps_stack", moduleId: "layouts/steps_stack@2.0.0",
  variants: ["vertical_steps", "numbered_cards", "progress_path"], requiredSlots: ["steps"], optionalSlots: ["accent"], identitySlots: [],
});

export function compileStepsStack(input) {
  const prepared = assertLayoutInput(STEPS_STACK_CONTRACT, input);
  const steps = stepsSlot({prefix: prepared.prefix, value: prepared.slots.steps, duration: prepared.duration, trackIndex: 3});
  const accent = assetOrFallback({prefix: prepared.prefix, slot: "accent", value: prepared.slots.accent, duration: prepared.duration, trackIndex: 4});
  const stepCount = prepared.slots.steps.items.length;
  const bodies = {
    vertical_steps: `<main class="hf-v2-vertical-process">${steps}</main><aside class="hf-v2-process-accent">${accent}</aside>`,
    numbered_cards: `<section class="hf-v2-card-process"><header id="${prepared.prefix}_card_counter" class="hf-v2-card-counter clip" data-v2-region="counter" data-safe-text="${stepCount}" ${clipAttributes(prepared.duration, 2)}><span></span></header>${steps}<footer>${accent}</footer></section>`,
    progress_path: `<svg id="${prepared.prefix}_progress_line" class="hf-v2-progress-line clip" viewBox="0 0 100 100" aria-hidden="true" ${clipAttributes(prepared.duration, 2)}><path d="M8 88C25 16 70 84 92 12"></path></svg><nav class="hf-v2-progress-nodes">${steps}</nav><aside>${accent}</aside>`,
  };
  const criticalRegions = {steps: input.ratio === "16:9" ? {x: 180, y: 180, width: 1560, height: 560} : {x: 100, y: 380, width: 880, height: 960}};
  if (input.variantId === "numbered_cards") criticalRegions.counter = input.ratio === "16:9" ? {x: 180, y: 110, width: 380, height: 90} : {x: 100, y: 210, width: 260, height: 120};
  return layoutResult({contract: STEPS_STACK_CONTRACT, variantId: input.variantId, ratio: input.ratio, input: prepared, structure: `steps-${input.variantId}`, body: bodies[input.variantId], criticalRegions});
}
