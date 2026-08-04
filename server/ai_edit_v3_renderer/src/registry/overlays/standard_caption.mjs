import {overlayContext, overlayResult} from "./overlay-v2-primitives.mjs";

const CONFIG = Object.freeze({
  componentId: "standard_caption", maxLines: 3, lineHeight: 1.25,
  bounds: Object.freeze({"16:9": Object.freeze({width: 1280, height: 150}), "9:16": Object.freeze({width: 900, height: 260})}),
  fontSizeSteps: Object.freeze({"16:9": Object.freeze([46, 42, 38, 34, 30]), "9:16": Object.freeze([44, 40, 36, 32, 28])}),
});

export function compileOverlayComponent(context) {
  const value = overlayContext(context, CONFIG);
  const root = `${value.instanceId}_standard_caption`;
  const html = `<div id="${root}" class="hf-overlay-v2 hf-overlay-v2-caption clip" data-overlay-v2="standard_caption" data-placement="${value.placement}" data-text-fit-step="${value.textFit.step}" ${value.clip}><p id="${root}_caption" data-public-target="caption" data-safe-text="${value.safeText}"><span></span></p><span id="${root}_emphasis" data-public-target="emphasis" aria-hidden="true"></span></div>`;
  return overlayResult({html, publicTargets: ["root", "caption", "emphasis"], textFit: value.textFit, fallback: "caption_stack", safeMaximums: value.safeMaximums});
}
