import {overlayContext, overlayResult} from "./overlay-v2-primitives.mjs";

const CONFIG = Object.freeze({componentId: "emphasis_caption", maxLines: 3, lineHeight: 1.18, bounds: Object.freeze({"16:9": Object.freeze({width: 1280, height: 150}), "9:16": Object.freeze({width: 900, height: 260})}), fontSizeSteps: Object.freeze({"16:9": Object.freeze([48, 44, 40, 36, 32]), "9:16": Object.freeze([46, 42, 38, 34, 30])})});

export function compileOverlayComponent(context) {
  const value = overlayContext(context, CONFIG); const root = `${value.instanceId}_emphasis_caption`;
  const html = `<div id="${root}" class="hf-overlay-v2 hf-overlay-v2-emphasis clip" data-overlay-v2="emphasis_caption" data-placement="${value.placement}" data-text-fit-step="${value.textFit.step}" ${value.clip}><p id="${root}_caption" data-public-target="caption" data-safe-text="${value.safeText}"><span></span></p><mark id="${root}_highlight" data-public-target="highlight" aria-hidden="true"></mark></div>`;
  return overlayResult({html, publicTargets: ["root", "caption", "highlight"], textFit: value.textFit, fallback: "standard_caption", safeMaximums: value.safeMaximums});
}
