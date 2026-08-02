import {createHash} from "node:crypto";

import {ANIMATION_CONTRACTS} from "./animations.mjs";
import {compileLayout, LAYOUT_CONTRACTS} from "./layouts.mjs";
import {getOverlayContract, OVERLAY_CONTRACTS} from "./overlays.mjs";
import {resolveTheme as resolveBoundedTheme, THEME_CONTRACT} from "./themes.mjs";
import {TRANSITION_CONTRACTS} from "./transitions.mjs";

export const REGISTRY_VERSION = "ai-edit-v3-registry-v1";
const RATIOS = Object.freeze(["16:9", "9:16"]);
const VARIANTS = Object.freeze(["balanced_a", "emphasis_b"]);
function deepFreeze(value) {
  if (value && typeof value === "object" && !Object.isFrozen(value)) {
    for (const child of Object.values(value)) deepFreeze(child);
    Object.freeze(value);
  }
  return value;
}

function canonicalize(value) {
  if (Array.isArray(value)) return value.map(canonicalize);
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.keys(value).sort().map((key) => [key, canonicalize(value[key])]));
  }
  return value;
}

function normalizeEntries(entries, field) {
  if (!Array.isArray(entries)) throw new Error("registry_entries_invalid");
  const sorted = [...entries].map((entry) => canonicalize(structuredClone(entry))).sort((a, b) => a.id.localeCompare(b.id));
  if (sorted.some((entry) => !entry || typeof entry.id !== "string")) throw new Error("registry_id_invalid");
  if (new Set(sorted.map(({id}) => id)).size !== sorted.length) throw new Error("registry_id_duplicate");
  if (sorted.some(({id}) => !/^[a-z][a-z0-9_]*$/u.test(id))) throw new Error(`registry_${field}_id_invalid`);
  return sorted;
}

export function createRegistryContract({layouts, overlays, animations, transitions}) {
  return deepFreeze(canonicalize({
    version: REGISTRY_VERSION,
    layouts: normalizeEntries(layouts, "layout"),
    overlays: normalizeEntries(overlays, "overlay"),
    animations: normalizeEntries(animations, "animation"),
    transitions: normalizeEntries(transitions, "transition"),
    theme: canonicalize(structuredClone(THEME_CONTRACT)),
  }));
}

const CONTRACT = createRegistryContract({
  layouts: LAYOUT_CONTRACTS,
  overlays: OVERLAY_CONTRACTS,
  animations: ANIMATION_CONTRACTS,
  transitions: TRANSITION_CONTRACTS,
});
const SHA256 = `sha256:${createHash("sha256").update(JSON.stringify(CONTRACT)).digest("hex")}`;
const LAYOUT_BY_ID = new Map(LAYOUT_CONTRACTS.map((contract) => [contract.id, contract]));

export function getRegistryContract() {
  return CONTRACT;
}

export function getRegistrySha256() {
  return SHA256;
}

export function resolveLayout(layoutId, variantId, ratio) {
  const contract = LAYOUT_BY_ID.get(layoutId);
  if (!contract) throw new Error("layout_unknown");
  if (!contract.variants.includes(variantId)) throw new Error("layout_variant_unknown");
  if (!contract.supportedRatios.includes(ratio)) throw new Error("layout_ratio_unknown");
  return Object.freeze({
    contract,
    variantId,
    ratio,
    compile: (input) => compileLayout({layoutId, variantId, ratio, ...input}),
  });
}

export function resolveOverlay(overlayId) {
  return getOverlayContract(overlayId);
}

export function resolveTheme(theme) {
  return resolveBoundedTheme(theme);
}
