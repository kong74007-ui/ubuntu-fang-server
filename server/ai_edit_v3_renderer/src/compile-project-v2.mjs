import {compileProject} from "./compile-project.mjs";

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
  return compileProject({manifest: {...manifest, compositions}, outputRoot});
}
