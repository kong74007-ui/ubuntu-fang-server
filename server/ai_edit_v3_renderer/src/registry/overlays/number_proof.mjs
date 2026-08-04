import {metricParts, overlayContext, overlayResult, safeTextAttribute} from "./overlay-v2-primitives.mjs";

const CONFIG = Object.freeze({componentId: "number_proof", maxLines: 5, lineHeight: 1.14, bounds: Object.freeze({"16:9": Object.freeze({width: 580, height: 380}), "9:16": Object.freeze({width: 280, height: 520})}), fontSizeSteps: Object.freeze({"16:9": Object.freeze([46, 42, 38, 34, 30]), "9:16": Object.freeze([42, 38, 34, 30, 26])})});

export function compileOverlayComponent(context) {
  const value = overlayContext(context, CONFIG); const root = `${value.instanceId}_number_proof`; const parts = metricParts(value.textFit.text);
  const html = `<dl id="${root}" class="hf-overlay-v2 hf-overlay-v2-number-proof clip" data-overlay-v2="number_proof" data-placement="${value.placement}" data-text-fit-step="${value.textFit.step}" ${value.clip}><dt id="${root}_label" data-public-target="label" data-safe-text="${safeTextAttribute(parts.label || value.textFit.text)}"><span></span></dt><dd id="${root}_metric_value" data-public-target="metric_value" data-safe-text="${safeTextAttribute(parts.value)}"><span></span></dd><dd id="${root}_unit" data-public-target="unit" data-safe-text="${safeTextAttribute(parts.unit || " ")}"><span></span></dd></dl>`;
  return overlayResult({html, publicTargets: ["root", "metric_value", "unit", "label"], textFit: value.textFit, fallback: "fact_card", safeMaximums: value.safeMaximums});
}
