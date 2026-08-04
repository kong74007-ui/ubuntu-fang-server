import {metricParts, overlayContext, overlayResult, safeTextAttribute} from "./overlay-v2-primitives.mjs";

const CONFIG = Object.freeze({componentId: "product_tag", maxLines: 4, lineHeight: 1.16, bounds: Object.freeze({"16:9": Object.freeze({width: 470, height: 300}), "9:16": Object.freeze({width: 280, height: 520})}), fontSizeSteps: Object.freeze({"16:9": Object.freeze([38, 34, 30, 26, 22]), "9:16": Object.freeze([34, 30, 26, 22, 20])})});

export function compileOverlayComponent(context) {
  const value = overlayContext(context, CONFIG); const root = `${value.instanceId}_product_tag`; const metric = metricParts(value.textFit.text);
  const html = `<aside id="${root}" class="hf-overlay-v2 hf-overlay-v2-product-tag clip" data-overlay-v2="product_tag" data-placement="${value.placement}" data-text-fit-step="${value.textFit.step}" ${value.clip}><strong id="${root}_product" data-public-target="product" data-safe-text="${value.safeText}"><span></span></strong><span id="${root}_label" data-public-target="label" aria-hidden="true"><span></span></span><span id="${root}_price" data-public-target="price" data-safe-text="${safeTextAttribute(metric.value === value.textFit.text ? " " : `${metric.value}${metric.unit}`)}"><span></span></span></aside>`;
  return overlayResult({html, publicTargets: ["root", "product", "label", "price"], textFit: value.textFit, fallback: "product_name_only", safeMaximums: value.safeMaximums});
}
