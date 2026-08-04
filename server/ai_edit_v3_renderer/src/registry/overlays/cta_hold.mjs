import {boundedClauses, overlayContext, overlayResult, safeTextAttribute} from "./overlay-v2-primitives.mjs";

const CONFIG = Object.freeze({componentId: "cta_hold", maxLines: 5, lineHeight: 1.18, bounds: Object.freeze({"16:9": Object.freeze({width: 580, height: 380}), "9:16": Object.freeze({width: 280, height: 520})}), fontSizeSteps: Object.freeze({"16:9": Object.freeze([44, 40, 36, 32, 28]), "9:16": Object.freeze([40, 36, 32, 28, 24])})});

export function compileOverlayComponent(context) {
  const value = overlayContext(context, CONFIG); const root = `${value.instanceId}_cta_hold`; const clauses = boundedClauses(value.textFit.text, 2);
  const html = `<section id="${root}" class="hf-overlay-v2 hf-overlay-v2-cta clip" data-overlay-v2="cta_hold" data-placement="${value.placement}" data-text-fit-step="${value.textFit.step}" ${value.clip}><strong id="${root}_action" data-public-target="action" data-safe-text="${safeTextAttribute(clauses[0])}"><span></span></strong><small id="${root}_support" data-public-target="support" data-safe-text="${safeTextAttribute(clauses[1] ?? " ")}"><span></span></small><span id="${root}_accent" data-public-target="accent" aria-hidden="true"></span></section>`;
  return overlayResult({html, publicTargets: ["root", "action", "support", "accent"], textFit: value.textFit, fallback: "action_only", safeMaximums: value.safeMaximums});
}
