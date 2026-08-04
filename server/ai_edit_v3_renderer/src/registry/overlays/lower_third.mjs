import {overlayContext, overlayResult} from "./overlay-v2-primitives.mjs";

const CONFIG = Object.freeze({componentId: "lower_third", maxLines: 3, lineHeight: 1.16, bounds: Object.freeze({"16:9": Object.freeze({width: 560, height: 130}), "9:16": Object.freeze({width: 640, height: 150})}), fontSizeSteps: Object.freeze({"16:9": Object.freeze([38, 34, 30, 26]), "9:16": Object.freeze([36, 32, 28, 24])})});

export function compileOverlayComponent(context) {
  const value = overlayContext(context, CONFIG); const root = `${value.instanceId}_lower_third`;
  const html = `<aside id="${root}" class="hf-overlay-v2 hf-overlay-v2-lower-third clip" data-overlay-v2="lower_third" data-placement="${value.placement}" data-text-fit-step="${value.textFit.step}" ${value.clip}><strong id="${root}_name" data-public-target="name" data-safe-text="${value.safeText}"><span></span></strong><small id="${root}_role" data-public-target="role" aria-hidden="true"><span></span></small><span id="${root}_accent" data-public-target="accent" aria-hidden="true"></span></aside>`;
  return overlayResult({html, publicTargets: ["root", "name", "role", "accent"], textFit: value.textFit, fallback: "name_only", safeMaximums: value.safeMaximums});
}
