import {overlayContext, overlayResult} from "./overlay-v2-primitives.mjs";

const CONFIG = Object.freeze({
  componentId: "info_card", maxLines: 5, lineHeight: 1.25,
  bounds: Object.freeze({"16:9": Object.freeze({width: 480, height: 420}), "9:16": Object.freeze({width: 280, height: 760})}),
  fontSizeSteps: Object.freeze({"16:9": Object.freeze([42, 38, 34, 30, 26]), "9:16": Object.freeze([38, 34, 30, 26, 22])}),
});

export function compileOverlayComponent(context) {
  const value = overlayContext(context, CONFIG);
  const root = `${value.instanceId}_info_card`;
  const html = `<article id="${root}" class="hf-overlay-v2 hf-overlay-v2-info-card clip" data-overlay-v2="info_card" data-placement="${value.placement}" data-text-fit-step="${value.textFit.step}" ${value.clip}><header id="${root}_label" data-public-target="label" aria-hidden="true"><span></span></header><p id="${root}_body" data-public-target="body" data-safe-text="${value.safeText}"><span></span></p><span id="${root}_accent" data-public-target="accent" aria-hidden="true"></span></article>`;
  return overlayResult({html, publicTargets: ["root", "label", "body", "accent"], textFit: value.textFit, fallback: "compact_info_card", safeMaximums: value.safeMaximums});
}
