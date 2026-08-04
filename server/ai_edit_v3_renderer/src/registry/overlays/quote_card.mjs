import {overlayContext, overlayResult} from "./overlay-v2-primitives.mjs";

const CONFIG = Object.freeze({componentId: "quote_card", maxLines: 6, lineHeight: 1.24, bounds: Object.freeze({"16:9": Object.freeze({width: 470, height: 470}), "9:16": Object.freeze({width: 280, height: 780})}), fontSizeSteps: Object.freeze({"16:9": Object.freeze([42, 38, 34, 30, 26]), "9:16": Object.freeze([38, 34, 30, 26, 22])})});

export function compileOverlayComponent(context) {
  const value = overlayContext(context, CONFIG); const root = `${value.instanceId}_quote_card`;
  const html = `<blockquote id="${root}" class="hf-overlay-v2 hf-overlay-v2-quote clip" data-overlay-v2="quote_card" data-placement="${value.placement}" data-text-fit-step="${value.textFit.step}" ${value.clip}><span id="${root}_accent" data-public-target="accent" aria-hidden="true">“</span><p id="${root}_quote" data-public-target="quote" data-safe-text="${value.safeText}"><span></span></p><footer id="${root}_attribution" data-public-target="attribution" aria-hidden="true"><span></span></footer></blockquote>`;
  return overlayResult({html, publicTargets: ["root", "quote", "accent", "attribution"], textFit: value.textFit, fallback: "plain_quote", safeMaximums: value.safeMaximums});
}
