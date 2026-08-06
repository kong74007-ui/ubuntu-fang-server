import {assetOrFallback, assertLayoutInput, clipAttributes, createContract, layoutResult, speakerSlot} from "./layout-v2-primitives.mjs";

export const SPEAKER_RIGHT_EVIDENCE_LEFT_CONTRACT = createContract({
  id: "speaker_right_evidence_left", moduleId: "layouts/speaker_right_evidence_left@2.0.0",
  variants: ["document_panel", "comparison_panel", "quote_evidence"], requiredSlots: ["speaker"], optionalSlots: ["evidence"], identitySlots: ["speaker"],
});

export function compileSpeakerRightEvidenceLeft(input) {
  const prepared = assertLayoutInput(SPEAKER_RIGHT_EVIDENCE_LEFT_CONTRACT, input);
  const speaker = speakerSlot({prefix: prepared.prefix, value: prepared.slots.speaker, duration: prepared.duration, trackIndex: 3});
  const evidence = assetOrFallback({prefix: prepared.prefix, slot: "evidence", value: prepared.slots.evidence, duration: prepared.duration, trackIndex: 4});
  const bodies = {
    document_panel: `<article class="hf-v2-document-stage"><section class="hf-v2-document-panel">${evidence}<footer id="${prepared.prefix}_document_footer" class="clip" ${clipAttributes(prepared.duration, 2)}><i></i><i></i><i></i></footer></section><figure class="hf-v2-right-speaker">${speaker}</figure></article>`,
    comparison_panel: `<main class="hf-v2-comparison-stage"><aside class="hf-v2-comparison-proof"><header><b></b><b></b></header><div>${evidence}</div></aside><section class="hf-v2-comparison-divider"><span></span></section><figure>${speaker}</figure></main>`,
    quote_evidence: `<blockquote class="hf-v2-quote-proof"><span aria-hidden="true">“</span><figure>${evidence}</figure><footer><i></i></footer></blockquote><aside class="hf-v2-quote-speaker">${speaker}</aside>`,
  };
  return layoutResult({
    contract: SPEAKER_RIGHT_EVIDENCE_LEFT_CONTRACT, variantId: input.variantId, ratio: input.ratio, input: prepared,
    structure: `speaker-right-${input.variantId}`, body: bodies[input.variantId], criticalRegions: regions(input.variantId, input.ratio),
  });
}

function regions(variantId, ratio) {
  const portrait = ratio === "9:16";
  const byVariant = {
    document_panel: portrait ? {evidence: {x: 60, y: 250, width: 600, height: 720}, speaker: {x: 590, y: 650, width: 430, height: 820}} : {evidence: {x: 100, y: 150, width: 850, height: 700}, speaker: {x: 1080, y: 130, width: 740, height: 790}},
    comparison_panel: portrait ? {evidence: {x: 70, y: 430, width: 470, height: 650}, speaker: {x: 590, y: 360, width: 430, height: 900}} : {evidence: {x: 130, y: 220, width: 720, height: 620}, speaker: {x: 1050, y: 190, width: 700, height: 680}},
    quote_evidence: portrait ? {evidence: {x: 90, y: 210, width: 820, height: 430}, speaker: {x: 390, y: 720, width: 620, height: 760}} : {evidence: {x: 150, y: 180, width: 940, height: 480}, speaker: {x: 1240, y: 280, width: 520, height: 620}},
  };
  return byVariant[variantId];
}
