import catalogJson from "./overlay-placement-v1.json" with {type: "json"};
import {overlayPlacementBox} from "../layouts/layout-v2-primitives.mjs";

const RATIOS = new Set(["16:9", "9:16"]);
const PLACEMENTS = new Set(["title_safe", "subtitle_safe", "left_panel", "right_panel", "center", "lower_third"]);

const catalog = validateOverlayPlacementCatalog(structuredClone(catalogJson));
const budgetIndex = new Map(catalog.entries.map((entry) => [key(entry.component_id, entry.placement, entry.ratio), entry]));

export const OVERLAY_PLACEMENT_CATALOG = catalog;
export const OVERLAY_PLACEMENT_BUDGETS = deepFreeze(Object.fromEntries(
  [...new Set(catalog.entries.map((entry) => entry.component_id))].map((componentId) => [componentId, Object.fromEntries(
    [...new Set(catalog.entries.filter((entry) => entry.component_id === componentId).map((entry) => entry.placement))].map((placement) => [placement, Object.fromEntries(
      catalog.entries.filter((entry) => entry.component_id === componentId && entry.placement === placement).map((entry) => [entry.ratio, entry]),
    )]),
  )]),
));
export const OVERLAY_PLACEMENTS_BY_COMPONENT = deepFreeze(Object.fromEntries(
  Object.entries(OVERLAY_PLACEMENT_BUDGETS).map(([componentId, placements]) => [componentId, Object.keys(placements)]),
));

export function getOverlayPlacementBudget(componentId, placement, ratio) {
  const budget = budgetIndex.get(key(componentId, placement, ratio));
  if (!budget) throw new Error("manifest_overlay_placement_invalid");
  return budget;
}

export function assertOverlayTextBudget(componentId, placement, ratio, text) {
  const budget = getOverlayPlacementBudget(componentId, placement, ratio);
  if (typeof text !== "string" || !text || [...text].length > budget.max_chars || text.split("\n").length > budget.max_lines) {
    throw new Error("manifest_overlay_text_budget_exceeded");
  }
  return budget;
}

export function validateOverlayPlacementCatalog(value) {
  if (!value || value.version !== "overlay-placement-v1" || !Array.isArray(value.entries) || !value.entries.length) throw new Error("overlay_placement_catalog_invalid");
  const seen = new Set();
  for (const entry of value.entries) {
    const allowed = ["component_id", "placement", "ratio", "max_chars", "max_lines", "font_size_steps", "line_height", "content_box", "host_box", "chrome"];
    if (!entry || typeof entry !== "object" || Array.isArray(entry) || Object.keys(entry).some((field) => !allowed.includes(field)) || Object.keys(entry).length !== allowed.length) throw new Error("overlay_placement_catalog_invalid");
    if (!/^[a-z][a-z0-9_]*$/u.test(entry.component_id) || !PLACEMENTS.has(entry.placement) || !RATIOS.has(entry.ratio)) throw new Error("overlay_placement_catalog_invalid");
    const identity = key(entry.component_id, entry.placement, entry.ratio);
    if (seen.has(identity)) throw new Error("overlay_placement_catalog_invalid");
    seen.add(identity);
    if (![entry.max_chars, entry.max_lines].every(Number.isInteger) || entry.max_chars < 1 || entry.max_chars > 480 || entry.max_lines < 1 || entry.max_lines > 12) throw new Error("overlay_placement_catalog_invalid");
    if (!Array.isArray(entry.font_size_steps) || !entry.font_size_steps.length || !entry.font_size_steps.every((size, index) => Number.isInteger(size) && size >= 20 && (index === 0 || size < entry.font_size_steps[index - 1]))) throw new Error("overlay_placement_catalog_invalid");
    if (typeof entry.line_height !== "number" || entry.line_height < 1 || entry.line_height > 2 || !validBox(entry.content_box) || !validBox(entry.host_box)) throw new Error("overlay_placement_catalog_invalid");
    const actualHost = overlayPlacementBox(entry.ratio, entry.placement);
    if (entry.host_box.width !== actualHost.width || entry.host_box.height !== actualHost.height) throw new Error("overlay_placement_catalog_invalid");
    if (!entry.chrome || Object.keys(entry.chrome).sort().join(",") !== "content_height_factor,height,width" || ![entry.chrome.width, entry.chrome.height, entry.chrome.content_height_factor].every((item) => typeof item === "number" && Number.isFinite(item) && item >= 0)) throw new Error("overlay_placement_catalog_invalid");
    if (entry.content_box.width + entry.chrome.width > entry.host_box.width || entry.content_box.height * entry.chrome.content_height_factor + entry.chrome.height > entry.host_box.height) throw new Error("overlay_placement_catalog_invalid");
  }
  return deepFreeze(value);
}

function validBox(value) {
  return value && typeof value === "object" && !Array.isArray(value) && Object.keys(value).sort().join(",") === "height,width" && [value.width, value.height].every((item) => Number.isInteger(item) && item > 0);
}

function key(componentId, placement, ratio) {
  return `${componentId}:${placement}:${ratio}`;
}

function deepFreeze(value) {
  if (value && typeof value === "object" && !Object.isFrozen(value)) {
    for (const child of Object.values(value)) deepFreeze(child);
    Object.freeze(value);
  }
  return value;
}
