import {boundedClauses, overlayContext, overlayResult, safeTextAttribute} from "./overlay-v2-primitives.mjs";

const CONFIG = Object.freeze({componentId: "bullet_list", maxLines: 6, lineHeight: 1.2, bounds: Object.freeze({"16:9": Object.freeze({width: 470, height: 480}), "9:16": Object.freeze({width: 280, height: 820})}), fontSizeSteps: Object.freeze({"16:9": Object.freeze([38, 34, 30, 26, 22]), "9:16": Object.freeze([34, 30, 26, 22, 20])})});

export function compileOverlayComponent(context) {
  const value = overlayContext(context, CONFIG); const root = `${value.instanceId}_bullet_list`;
  const items = boundedClauses(value.textFit.text, 5).map((item, index) => `<li id="${root}_item_${index + 1}" data-public-target="bullets" data-safe-text="${safeTextAttribute(item)}"><span></span></li>`).join("");
  const html = `<section id="${root}" class="hf-overlay-v2 hf-overlay-v2-bullets clip" data-overlay-v2="bullet_list" data-placement="${value.placement}" data-text-fit-step="${value.textFit.step}" ${value.clip}><ul id="${root}_items" data-public-target="items">${items}</ul></section>`;
  return overlayResult({html, publicTargets: ["root", "items", "bullets"], textFit: value.textFit, fallback: "stacked_bullets", safeMaximums: value.safeMaximums});
}
