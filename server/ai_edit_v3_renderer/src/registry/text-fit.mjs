import {assertSafeText} from "./layout-primitives.mjs";

const RATIOS = new Set(["16:9", "9:16"]);

/** Choose a deterministic type step without changing authoritative text. */
export function fitOverlayText({text, ratio, bounds, fontSizeSteps, lineHeight, maxLines}) {
  const normalized = assertSafeText(text, {maxChars: 480, maxLines: 12});
  if (!normalized.trim()) throw new Error("overlay_authoritative_text_empty");
  if (!RATIOS.has(ratio)) throw new Error("overlay_ratio_invalid");
  if (!validBounds(bounds) || !Array.isArray(fontSizeSteps) || !fontSizeSteps.length ||
      !fontSizeSteps.every((value, index) => Number.isInteger(value) && value > 0 && (index === 0 || value < fontSizeSteps[index - 1])) ||
      typeof lineHeight !== "number" || !Number.isFinite(lineHeight) || lineHeight < 1 || lineHeight > 2 ||
      !Number.isInteger(maxLines) || maxLines < 1 || maxLines > 12) {
    throw new Error("overlay_text_fit_config_invalid");
  }
  for (let index = 0; index < fontSizeSteps.length; index += 1) {
    const fontSize = fontSizeSteps[index];
    const estimatedLines = estimateLines(normalized, bounds.width, fontSize);
    if (estimatedLines <= maxLines && estimatedLines * fontSize * lineHeight <= bounds.height) {
      return Object.freeze({text: normalized, fontSize, lineHeight, estimatedLines, step: index, truncated: false});
    }
  }
  throw new Error("overlay_text_fit_unavailable");
}

function estimateLines(text, width, fontSize) {
  const capacity = width / fontSize;
  let lines = 0;
  for (const line of text.split("\n")) {
    const units = [...line].reduce((total, character) => total + displayUnits(character), 0);
    lines += Math.max(1, Math.ceil(units / capacity));
  }
  return lines;
}

function displayUnits(character) {
  if (/\p{Mark}/u.test(character)) return 0;
  if (/^[\u0000-\u00ff]$/u.test(character)) return 0.56;
  return 1;
}

function validBounds(bounds) {
  return bounds && typeof bounds === "object" && !Array.isArray(bounds) &&
    Number.isFinite(bounds.width) && bounds.width > 0 && Number.isFinite(bounds.height) && bounds.height > 0;
}
