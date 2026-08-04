import {boundedClauses, overlayContext, overlayResult, safeTextAttribute} from "./overlay-v2-primitives.mjs";

const CONFIG = Object.freeze({componentId: "step_indicator", maxLines: 6, lineHeight: 1.18, bounds: Object.freeze({"16:9": Object.freeze({width: 470, height: 470}), "9:16": Object.freeze({width: 280, height: 820})}), fontSizeSteps: Object.freeze({"16:9": Object.freeze([36, 32, 28, 24, 22]), "9:16": Object.freeze([32, 28, 25, 22, 20])})});

export function compileOverlayComponent(context) {
  const value = overlayContext(context, CONFIG); const root = `${value.instanceId}_step_indicator`; const steps = boundedClauses(value.textFit.text, 5);
  const items = steps.map((item, index) => `<li data-safe-text="${safeTextAttribute(item)}"><span></span><i aria-hidden="true">${index + 1}</i></li>`).join("");
  const html = `<nav id="${root}" class="hf-overlay-v2 hf-overlay-v2-steps clip" data-overlay-v2="step_indicator" data-placement="${value.placement}" data-text-fit-step="${value.textFit.step}" ${value.clip}><ol id="${root}_progress" data-public-target="progress">${items}</ol><span id="${root}_current" data-public-target="current" aria-hidden="true">1</span><span id="${root}_total" data-public-target="total" aria-hidden="true">${steps.length}</span></nav>`;
  return overlayResult({html, publicTargets: ["root", "progress", "current", "total"], textFit: value.textFit, fallback: "compact_steps", safeMaximums: value.safeMaximums});
}
