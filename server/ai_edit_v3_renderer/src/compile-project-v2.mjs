import {compileProject, compileSourceVideo, sourceSegmentClips} from "./compile-project.mjs";
import {resolveLayout, resolveLayoutV2} from "./registry/index.mjs";

const SAFE_HOST_BY_PLACEMENT = Object.freeze({
  title_safe: "title",
  safe_top: "title",
  left_panel: "title",
  right_panel: "title",
  center: "title",
  subtitle_safe: "captions",
  safe_bottom: "captions",
  lower_third: "captions",
});

/**
 * V2 binds model-facing instance identifiers to the renderer's stable component
 * identifiers before handing the immutable legacy compiler its projection.
 */
export async function compileProjectV2({manifest, outputRoot}) {
  if (!manifest || manifest.version !== "2.0" || !Array.isArray(manifest.compositions)) {
    throw new Error("manifest_v2_invalid");
  }
  const compositions = manifest.compositions.map((composition) => {
    const instances = composition.overlay_instances;
    if (!Array.isArray(instances) || instances.length !== composition.overlay_ids?.length) {
      throw new Error("manifest_component_projection_invalid");
    }
    const instanceToComponent = new Map();
    for (const item of instances) {
      if (!item || typeof item.instance_id !== "string" || typeof item.component_id !== "string" || instanceToComponent.has(item.instance_id)) {
        throw new Error("manifest_component_projection_invalid");
      }
      instanceToComponent.set(item.instance_id, item.component_id);
    }
    if (JSON.stringify(composition.overlay_ids) !== JSON.stringify(instances.map((item) => item.component_id))) {
      throw new Error("manifest_component_projection_invalid");
    }
    return {
      ...composition,
      animations: (composition.animations ?? []).map((animation) => {
        if (!instanceToComponent.has(animation.target)) throw new Error("manifest_component_animation_target_invalid");
        return {...animation};
      }),
    };
  });
  return compileProject({
    manifest: {...manifest, compositions}, outputRoot,
    sceneOptions: {layoutResolver: resolveV2OrLegacyLayout, buildLayoutInput: buildV2LayoutInput, compileSource: compileV2Source},
  });
}

function resolveV2OrLegacyLayout(layoutId, variantId, ratio) {
  try {
    return resolveLayoutV2(layoutId, variantId, ratio);
  } catch (error) {
    if (!/^(?:layout_unknown|layout_variant_unknown)$/u.test(error?.message ?? "")) throw error;
    return resolveLayout(layoutId, variantId, ratio);
  }
}

function buildV2LayoutInput({manifest, composition, prefix, durationMs, overlays, overlayEntries, assets, captions, theme, layout}) {
  if (layout.contract.version !== "2.0.0") {
    return {idPrefix: prefix, durationMs, hasVideo: Boolean(manifest.source_video), overlays, scene: composition, assets};
  }
  const bindings = assetSlotBindings(composition, assets);
  const slots = layout.contract.id === "speaker_fullscreen"
    ? {speaker: sourceSlot(manifest, composition, prefix), evidence: bindings.get("evidence")}
    : layout.contract.id === "product_hero"
      ? productSlots(bindings, captions)
      : {steps: {items: captions.map(({text}) => text).slice(0, 6)}, accent: bindings.get("accent")};
  return {
    idPrefix: prefix, durationMs, slots, overlays: routeV2Overlays(overlayEntries, composition.overlay_instances),
    designTokens: {"--hf-accent": theme["--hf-accent"], "--hf-surface": theme["--hf-surface"]},
  };
}

function routeV2Overlays(entries, instances) {
  const hosts = {title: "", captions: ""};
  const byInstance = new Map((entries ?? []).map((entry) => [entry.instanceId, entry]));
  for (const instance of instances ?? []) {
    const host = SAFE_HOST_BY_PLACEMENT[instance.placement ?? "safe_bottom"];
    if (!host) throw new Error("manifest_overlay_placement_invalid");
    const entry = byInstance.get(instance.instance_id);
    if (!entry || entry.componentId !== instance.component_id || typeof entry.html !== "string") throw new Error("manifest_component_projection_invalid");
    hosts[host] += entry.html;
  }
  return hosts;
}

function sourceSlot(manifest, composition, prefix) {
  if (!manifest.source_video?.path) return undefined;
  const clips = sourceSegmentClips({manifest, composition});
  if (!clips.length) return undefined;
  return {id: `${prefix}_speaker`, kind: "video", relativePath: manifest.source_video.path, clips};
}

function assetSlotBindings(composition, assets) {
  const available = new Map(assets.map((asset) => [asset.id, asset]));
  if (composition.layout_slot_bindings === undefined) return new Map();
  if (!Array.isArray(composition.layout_slot_bindings)) throw new Error("layout_slot_bindings_invalid");
  const slots = new Map();
  for (const binding of composition.layout_slot_bindings) {
    if (!binding || !["primary", "detail", "evidence", "accent", "steps"].includes(binding.slot_id) || typeof binding.asset_id !== "string" || slots.has(binding.slot_id)) throw new Error("layout_slot_bindings_invalid");
    const asset = available.get(binding.asset_id);
    if (!asset) throw new Error("layout_slot_asset_unknown");
    slots.set(binding.slot_id, asset);
  }
  return slots;
}

function requiredBinding(bindings, slot) {
  const asset = bindings.get(slot);
  if (!asset) throw new Error("layout_required_slot_missing");
  return asset;
}

function productSlots(bindings, captions) {
  const primary = requiredBinding(bindings, "primary");
  const detail = bindings.get("detail");
  if (detail?.id === primary.id) throw new Error("layout_slot_identity_invalid");
  return {primary, detail, copy: captions[0] ? {text: captions[0].text} : undefined};
}

function compileV2Source({manifest, composition, prefix, layout}) {
  if (layout.contract.version === "2.0.0") return "";
  return compileSourceVideo({manifest, composition, prefix});
}
