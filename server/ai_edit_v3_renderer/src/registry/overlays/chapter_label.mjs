import {overlayContext, overlayResult} from "./overlay-v2-primitives.mjs";

const CONFIG = Object.freeze({componentId: "chapter_label", maxLines: 2, lineHeight: 1.1, bounds: Object.freeze({"16:9": Object.freeze({width: 760, height: 130}), "9:16": Object.freeze({width: 760, height: 160})}), fontSizeSteps: Object.freeze({"16:9": Object.freeze([42, 38, 34, 30]), "9:16": Object.freeze([40, 36, 32, 28])})});

export function compileOverlayComponent(context) {
  const value = overlayContext(context, CONFIG); const root = `${value.instanceId}_chapter_label`;
  const html = `<header id="${root}" class="hf-overlay-v2 hf-overlay-v2-chapter clip" data-overlay-v2="chapter_label" data-placement="${value.placement}" data-text-fit-step="${value.textFit.step}" ${value.clip}><span id="${root}_chapter" data-public-target="chapter" data-safe-text="${value.safeText}"><span></span></span><i id="${root}_rule" data-public-target="rule" aria-hidden="true"></i></header>`;
  return overlayResult({html, publicTargets: ["root", "chapter", "rule"], textFit: value.textFit, fallback: "compact_chapter", safeMaximums: value.safeMaximums});
}
