import {resolveTheme} from "./registry/index.mjs";

const FORBIDDEN = new Set(["html", "css", "javascript", "script", "expression", "plugin", "url", "output_path", "command", "env", "systemd_property"]);
const DESIGN_INTENT_VALUES = Object.freeze({
  density: new Set(["minimal", "balanced", "dense"]),
  motion_energy: new Set(["low", "medium", "high"]),
  image_fit: new Set(["contain", "cover", "smart_crop"]),
  decoration_intensity: new Set(["low", "medium", "high"]),
});
const VISUAL_VALUE_FORBIDDEN = /(?:https?:)?\/\/|url\s*\(|@font-face|\banimation\b/i;


function inspect(value) {
  if (Array.isArray(value)) { for (const item of value) inspect(item); return; }
  if (!value || typeof value !== "object") return;
  for (const [key, child] of Object.entries(value)) {
    if (FORBIDDEN.has(key.toLowerCase())) throw new Error("manifest_executable_field_forbidden");
    inspect(child);
  }
}

function sameJsonValue(left, right) {
  if (typeof left !== typeof right) return false;
  if (left === null || right === null) return left === right;
  if (typeof left === "number") return Number.isFinite(left) && Number.isFinite(right) && left === right;
  if (typeof left === "string" || typeof left === "boolean") return left === right;
  if (typeof left !== "object") return false;
  if (Array.isArray(left) || Array.isArray(right)) {
    return Array.isArray(left) && Array.isArray(right) && left.length === right.length
      && left.every((value, index) => sameJsonValue(value, right[index]));
  }
  const leftKeys = Object.keys(left);
  const rightKeys = Object.keys(right);
  return leftKeys.length === rightKeys.length
    && leftKeys.every((key) => Object.hasOwn(right, key) && sameJsonValue(left[key], right[key]));
}

function validateDesignIntent(intent) {
  if (!intent || typeof intent !== "object" || Array.isArray(intent)) throw new Error("manifest_design_intent_invalid");
  const keys = Object.keys(intent);
  if (keys.length !== Object.keys(DESIGN_INTENT_VALUES).length || keys.some((key) => !Object.hasOwn(DESIGN_INTENT_VALUES, key))) {
    throw new Error("manifest_design_intent_invalid");
  }
  for (const [key, values] of Object.entries(DESIGN_INTENT_VALUES)) {
    if (typeof intent[key] !== "string" || !values.has(intent[key]) || VISUAL_VALUE_FORBIDDEN.test(intent[key])) throw new Error("manifest_design_intent_invalid");
  }
}

function validateVisualValues(value) {
  if (typeof value === "string") {
    if (VISUAL_VALUE_FORBIDDEN.test(value)) throw new Error("manifest_visual_value_forbidden");
    return;
  }
  if (Array.isArray(value)) { for (const item of value) validateVisualValues(item); return; }
  if (value && typeof value === "object") for (const item of Object.values(value)) validateVisualValues(item);
}


function deepCopyFreeze(value) {
  if (Array.isArray(value)) return Object.freeze(value.map(deepCopyFreeze));
  if (value && typeof value === "object") {
    const result = Object.create(null);
    for (const key of Object.keys(value)) result[key] = deepCopyFreeze(value[key]);
    return Object.freeze(result);
  }
  return value;
}


export function validateManifest(document, expected) {
  if (!document || typeof document !== "object" || Array.isArray(document)) throw new Error("manifest_invalid");
  inspect(document);
  const schemaByVersion = expected.schemaSha256ByVersion ?? {"1.0": expected.schemaSha256};
  if (typeof document.version !== "string" || typeof schemaByVersion[document.version] !== "string") throw new Error("manifest_version_invalid");
  if (document.renderer_environment?.renderer_build_id !== expected.rendererBuildId) throw new Error("manifest_renderer_build_mismatch");
  if (`sha256:${document.registry_sha256}` !== expected.registrySha256) throw new Error("manifest_registry_mismatch");
  if (document.schema_sha256 !== schemaByVersion[document.version]) throw new Error("manifest_schema_mismatch");
  const output = document.output_spec;
  if (!output || output.fps_num !== 30 || output.fps_den !== 1) throw new Error("manifest_output_invalid");
  const dimensions = output.ratio === "16:9" ? [1920, 1080] : output.ratio === "9:16" ? [1080, 1920] : null;
  if (!dimensions || output.width !== dimensions[0] || output.height !== dimensions[1]) throw new Error("manifest_output_invalid");
  if (document.source_video !== null && document.source_video?.silent !== true) throw new Error("manifest_source_audio_forbidden");
  if (!Number.isInteger(document.duration_ms) || document.duration_ms < 1) throw new Error("manifest_duration_invalid");
  if (!Array.isArray(document.compositions) || document.compositions.length < 1) throw new Error("manifest_compositions_invalid");
  const ids = new Set();
  let cursor = 0;
  for (const composition of document.compositions) {
    if (!composition || typeof composition.id !== "string" || ids.has(composition.id)) throw new Error("manifest_composition_id_invalid");
    ids.add(composition.id);
    if (composition.start_ms !== cursor || !Number.isInteger(composition.end_ms) || composition.end_ms <= cursor) throw new Error("manifest_composition_timeline_invalid");
    cursor = composition.end_ms;
    if (document.version === "2.0") {
      const instances = composition.overlay_instances;
      if (!Array.isArray(composition.overlay_ids) || !Array.isArray(instances)
        || JSON.stringify(composition.overlay_ids) !== JSON.stringify(instances.map((item) => item?.component_id))) {
        throw new Error("manifest_component_projection_invalid");
      }
      validateOverlayComposition(composition);
      const bindings = composition.layout_slot_bindings;
      if (composition.layout_id === "product_hero" && (!Array.isArray(bindings) || !bindings.some((item) => item?.slot_id === "primary"))) {
        throw new Error("manifest_layout_required_slot_missing");
      }
    }
  }
  if (cursor !== document.duration_ms) throw new Error("manifest_composition_timeline_invalid");
  if (document.version === "2.0") {
    let resolved;
    try {
      validateDesignIntent(document.design_intent);
      validateVisualValues(document.design_tokens);
      resolved = resolveTheme({
        profileId: document.theme_profile_id,
        intent: document.design_intent,
        variationSeed: document.variation_seed,
      });
    } catch (error) {
      if (error?.message === "manifest_design_intent_invalid" || error?.message === "manifest_visual_value_forbidden") throw error;
      throw new Error("manifest_design_tokens_mismatch");
    }
    if (!sameJsonValue(document.design_tokens, resolved)) throw new Error("manifest_design_tokens_mismatch");
  }
  return deepCopyFreeze(document);
}

function validateOverlayComposition(composition) {
  const placements = new Set(["title_safe", "subtitle_safe", "left_panel", "right_panel", "center", "lower_third"]);
  const references = new Set(["headline", "highlight"]);
  for (const instance of composition.overlay_instances) {
    if (!instance || typeof instance !== "object" || Array.isArray(instance)) throw new Error("manifest_overlay_instance_invalid");
    const allowed = new Set(["instance_id", "component_id", "content_ref", "placement", "variant"]);
    if (Object.keys(instance).some((key) => !allowed.has(key))) throw new Error("manifest_overlay_instance_invalid");
    if (!references.has(instance.content_ref)) throw new Error("manifest_overlay_content_ref_invalid");
    if (!placements.has(instance.placement)) throw new Error("manifest_overlay_placement_invalid");
  }
  const content = composition.authoritative_content;
  if (!content || typeof content !== "object" || Array.isArray(content) || Object.keys(content).length !== 2 || !Object.hasOwn(content, "headline") || !Object.hasOwn(content, "highlight")) {
    throw new Error("manifest_overlay_content_ref_invalid");
  }
  for (const value of Object.values(content)) {
    if (!value || typeof value !== "object" || Array.isArray(value) || Object.keys(value).length !== 2 || !Object.hasOwn(value, "text") || !Object.hasOwn(value, "source_caption_ids") || typeof value.text !== "string" || !value.text || value.text.length > 4000 || !Array.isArray(value.source_caption_ids) || new Set(value.source_caption_ids).size !== value.source_caption_ids.length || !value.source_caption_ids.every((item) => typeof item === "string" && /^[a-z0-9_]{1,64}$/u.test(item))) {
      throw new Error("manifest_overlay_content_ref_invalid");
    }
  }
}
