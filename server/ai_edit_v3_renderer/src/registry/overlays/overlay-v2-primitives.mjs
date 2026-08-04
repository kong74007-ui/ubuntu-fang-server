import {assertSafeId, escapeAttribute, seconds} from "../layout-primitives.mjs";
import {fitOverlayText} from "../text-fit.mjs";
import {assertOverlayTextBudget, getOverlayPlacementBudget, OVERLAY_PLACEMENTS_BY_COMPONENT} from "./overlay-placement-contract.mjs";

const PLACEMENTS = new Set(["title_safe", "subtitle_safe", "left_panel", "right_panel", "center", "lower_third"]);
export {OVERLAY_PLACEMENTS_BY_COMPONENT};

export function overlayContext(context, config) {
  if (!context || typeof context !== "object" || Array.isArray(context)) throw new Error("manifest_overlay_instance_invalid");
  const allowed = new Set(["componentId", "instanceId", "content", "placement", "ratio", "durationMs", "startMs", "trackIndex"]);
  if (Object.keys(context).some((key) => !allowed.has(key))) throw new Error("manifest_overlay_instance_invalid");
  if (context.componentId !== config.componentId) throw new Error("manifest_component_projection_invalid");
  const instanceId = assertSafeId(context.instanceId, "overlay_instance_id");
  if (!PLACEMENTS.has(context.placement)) throw new Error("manifest_overlay_placement_invalid");
  if (!OVERLAY_PLACEMENTS_BY_COMPONENT[config.componentId]?.includes(context.placement)) throw new Error("manifest_overlay_placement_invalid");
  if (!context.content || typeof context.content !== "object" || Array.isArray(context.content) || Object.keys(context.content).some((key) => key !== "text")) {
    throw new Error("manifest_overlay_content_ref_invalid");
  }
  const budget = assertOverlayTextBudget(config.componentId, context.placement, context.ratio, context.content.text);
  const registered = getOverlayPlacementBudget(config.componentId, context.placement, context.ratio);
  if (config.maxLines < registered.max_lines || config.lineHeight !== registered.line_height || JSON.stringify(config.fontSizeSteps[context.ratio]) !== JSON.stringify(registered.font_size_steps)) throw new Error("overlay_component_catalog_mismatch");
  const contentBounds = registered.content_box;
  const textFit = fitOverlayText({
    text: context.content.text, ratio: context.ratio, bounds: contentBounds,
    fontSizeSteps: registered.font_size_steps, lineHeight: registered.line_height, maxLines: registered.max_lines,
  });
  const startMs = context.startMs ?? 0;
  const durationMs = context.durationMs;
  const trackIndex = context.trackIndex ?? 21;
  if (!Number.isInteger(trackIndex) || trackIndex < 0) throw new Error("overlay_track_invalid");
  return Object.freeze({
    instanceId, textFit, placement: context.placement, ratio: context.ratio,
    clip: `data-start="${seconds(startMs)}" data-duration="${seconds(durationMs)}" data-track-index="${trackIndex}"`,
    typeStyle: `style="--hf-overlay-font-size:${textFit.fontSize}px;--hf-overlay-line-height:${textFit.lineHeight}"`,
    safeText: escapeAttribute(textFit.text),
    safeMaximums: Object.freeze({maxLines: budget.max_lines, maxChars: budget.max_chars, bounds: contentBounds, hostBox: registered.host_box, chromeWidth: registered.chrome.width, chromeHeight: registered.chrome.height, contentHeightFactor: registered.chrome.content_height_factor}),
  });
}

export function overlayResult({html, publicTargets, textFit, fallback, safeMaximums}) {
  const contentHeight = Math.ceil(textFit.estimatedLines * textFit.fontSize * textFit.lineHeight * safeMaximums.contentHeightFactor);
  const contentWidth = Math.ceil(safeMaximums.bounds.width);
  const styledHtml = html.replace(
    `data-text-fit-step="${textFit.step}"`,
    `data-text-fit-step="${textFit.step}" data-host-box="${safeMaximums.hostBox.width},${safeMaximums.hostBox.height}" data-content-box="${contentWidth},${contentHeight}" style="--hf-overlay-font-size:${textFit.fontSize}px;--hf-overlay-line-height:${textFit.lineHeight}"`,
  );
  const rootId = styledHtml.match(/^<[^>]+\bid="([a-z][a-z0-9_]*)"/u)?.[1];
  if (!rootId) throw new Error("overlay_root_target_invalid");
  return Object.freeze({
    html: styledHtml,
    animationTarget: `#${rootId}`,
    publicTargets: Object.freeze([...publicTargets]),
    textAudit: Object.freeze({authoritativeText: textFit.text, fontSize: textFit.fontSize, estimatedLines: textFit.estimatedLines, truncated: false}),
    geometryAudit: Object.freeze({hostWidth: safeMaximums.hostBox.width, hostHeight: safeMaximums.hostBox.height, contentWidth, contentHeight, chromeWidth: safeMaximums.chromeWidth, chromeHeight: safeMaximums.chromeHeight}),
    fallback,
    safeMaximums,
  });
}

export function safeTextAttribute(value) {
  return escapeAttribute(value);
}

/** Split visible clauses deterministically while preserving every code point. */
export function boundedClauses(text, maximum) {
  const clauses = text.match(/[^\u3002\uff01\uff1f\uff1b;]+[\u3002\uff01\uff1f\uff1b;]?/gu)?.filter(Boolean) ?? [text];
  if (clauses.length <= maximum) return clauses;
  return [...clauses.slice(0, maximum - 1), clauses.slice(maximum - 1).join("")];
}

export function metricParts(text) {
  const match = text.match(/(?<value>\d+(?:\.\d+)?)\s*(?<unit>%|\u5143|\u4e2a|\u4eba|\u4ef6|\u4e07|\u4ebf|\u4efd|\u500d|\u5e74|\u5929|\u79ef\u5206)?/u);
  if (!match?.groups) return Object.freeze({value: text, unit: "", label: ""});
  const before = text.slice(0, match.index);
  const after = text.slice(match.index + match[0].length);
  return Object.freeze({value: match.groups.value, unit: match.groups.unit ?? "", label: `${before}${after}`});
}
