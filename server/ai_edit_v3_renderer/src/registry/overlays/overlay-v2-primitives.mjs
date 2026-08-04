import {assertSafeId, escapeAttribute, seconds} from "../layout-primitives.mjs";
import {fitOverlayText} from "../text-fit.mjs";

const PLACEMENTS = new Set(["title_safe", "subtitle_safe", "left_panel", "right_panel", "center", "lower_third"]);

export function overlayContext(context, config) {
  if (!context || typeof context !== "object" || Array.isArray(context)) throw new Error("manifest_overlay_instance_invalid");
  const allowed = new Set(["componentId", "instanceId", "content", "placement", "ratio", "durationMs", "startMs", "trackIndex"]);
  if (Object.keys(context).some((key) => !allowed.has(key))) throw new Error("manifest_overlay_instance_invalid");
  if (context.componentId !== config.componentId) throw new Error("manifest_component_projection_invalid");
  const instanceId = assertSafeId(context.instanceId, "overlay_instance_id");
  if (!PLACEMENTS.has(context.placement)) throw new Error("manifest_overlay_placement_invalid");
  if (!context.content || typeof context.content !== "object" || Array.isArray(context.content) || Object.keys(context.content).some((key) => key !== "text")) {
    throw new Error("manifest_overlay_content_ref_invalid");
  }
  const textFit = fitOverlayText({
    text: context.content.text, ratio: context.ratio, bounds: config.bounds[context.ratio],
    fontSizeSteps: config.fontSizeSteps[context.ratio], lineHeight: config.lineHeight, maxLines: config.maxLines,
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
    safeMaximums: Object.freeze({maxLines: config.maxLines, maxChars: 480, bounds: Object.freeze({...config.bounds[context.ratio]})}),
  });
}

export function overlayResult({html, publicTargets, textFit, fallback, safeMaximums}) {
  const styledHtml = html.replace(
    `data-text-fit-step="${textFit.step}"`,
    `data-text-fit-step="${textFit.step}" style="--hf-overlay-font-size:${textFit.fontSize}px;--hf-overlay-line-height:${textFit.lineHeight}"`,
  );
  const rootId = styledHtml.match(/^<[^>]+\bid="([a-z][a-z0-9_]*)"/u)?.[1];
  if (!rootId) throw new Error("overlay_root_target_invalid");
  return Object.freeze({
    html: styledHtml,
    animationTarget: `#${rootId}`,
    publicTargets: Object.freeze([...publicTargets]),
    textAudit: Object.freeze({authoritativeText: textFit.text, fontSize: textFit.fontSize, estimatedLines: textFit.estimatedLines, truncated: false}),
    fallback,
    safeMaximums,
  });
}

export function safeTextAttribute(value) {
  return escapeAttribute(value);
}

/** Split visible clauses deterministically while preserving every code point. */
export function boundedClauses(text, maximum) {
  const clauses = text.match(/[^。！？；;]+[。！？；;]?/gu)?.filter(Boolean) ?? [text];
  if (clauses.length <= maximum) return clauses;
  return [...clauses.slice(0, maximum - 1), clauses.slice(maximum - 1).join("")];
}

export function metricParts(text) {
  const match = text.match(/(?<value>\d+(?:\.\d+)?)\s*(?<unit>%|元|万|亿|份|个|倍|积分)?/u);
  if (!match?.groups) return Object.freeze({value: text, unit: "", label: ""});
  const before = text.slice(0, match.index);
  const after = text.slice(match.index + match[0].length);
  return Object.freeze({value: match.groups.value, unit: match.groups.unit ?? "", label: `${before}${after}`});
}
