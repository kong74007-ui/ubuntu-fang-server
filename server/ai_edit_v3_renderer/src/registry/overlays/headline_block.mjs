import {overlayContext, overlayResult} from "./overlay-v2-primitives.mjs";

const CONFIG = Object.freeze({
  componentId: "headline_block", maxLines: 3, lineHeight: 1.12,
  bounds: Object.freeze({"16:9": Object.freeze({width: 1080, height: 176}), "9:16": Object.freeze({width: 900, height: 220})}),
  fontSizeSteps: Object.freeze({"16:9": Object.freeze([64, 58, 52, 46, 40]), "9:16": Object.freeze([58, 52, 46, 40, 36])}),
});

export function compileOverlayComponent(context) {
  const value = overlayContext(context, CONFIG);
  const root = `${value.instanceId}_headline_block`;
  const html = `<header id="${root}" class="hf-overlay-v2 hf-overlay-v2-headline clip" data-overlay-v2="headline_block" data-placement="${value.placement}" data-text-fit-step="${value.textFit.step}" ${value.clip}><h1 id="${root}_headline" data-public-target="headline" data-safe-text="${value.safeText}"><span></span></h1><span id="${root}_underline" data-public-target="underline" aria-hidden="true"></span></header>`;
  return overlayResult({html, publicTargets: ["root", "headline", "underline"], textFit: value.textFit, fallback: "compact_headline", safeMaximums: value.safeMaximums});
}
