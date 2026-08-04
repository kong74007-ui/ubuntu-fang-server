import {compileProject, compileSourceVideo} from "./compile-project.mjs";
import {resolveLayout, resolveLayoutV2} from "./registry/index.mjs";

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

function buildV2LayoutInput({manifest, composition, prefix, durationMs, overlays, assets, captions, theme, layout}) {
  if (layout.contract.version !== "2.0.0") {
    return {idPrefix: prefix, durationMs, hasVideo: Boolean(manifest.source_video), overlays, scene: composition, assets};
  }
  const slots = layout.contract.id === "speaker_fullscreen"
    ? {speaker: sourceSlot(manifest, prefix), evidence: assets[0]}
    : layout.contract.id === "product_hero"
      ? {primary: assets[0], detail: assets[1]}
      : {steps: {items: captions.map(({text}) => text).slice(0, 6)}, accent: assets[0]};
  return {
    idPrefix: prefix, durationMs, slots, overlays,
    designTokens: {"--hf-accent": theme["--hf-accent"], "--hf-surface": theme["--hf-surface"]},
  };
}

function sourceSlot(manifest, prefix) {
  if (!manifest.source_video?.path) return undefined;
  return {id: `${prefix}_speaker`, kind: "video", relativePath: manifest.source_video.path};
}

function compileV2Source({manifest, composition, prefix, layout}) {
  if (layout.contract.version === "2.0.0") return "";
  return compileSourceVideo({manifest, composition, prefix});
}
