import {getRegistryContract} from "./index.mjs";


const THEME_FIELDS = ["palette_id", "typography_id", "density", "motion_energy", "image_fit"];


export function buildVisualCapabilitiesContract(contract = getRegistryContract()) {
  const layoutIds = contract.layouts_v2.map(({id}) => id);
  const overlayIds = contract.overlays.map(({id}) => id);
  const emptyById = (ids) => Object.fromEntries(ids.map((id) => [id, []]));
  return {
    version: "ai-edit-v3-visual-capabilities-v1",
    layout_capabilities: layoutIds,
    layout_variants: Object.fromEntries(
      contract.layouts_v2.map(({id, variants}) => [id, [...variants]]),
    ),
    overlay_capabilities: overlayIds,
    overlay_variants: emptyById(overlayIds),
    overlay_animation_targets: emptyById(overlayIds),
    layout_animation_targets: emptyById(layoutIds),
    animation_capabilities: contract.animations.map(({id}) => id),
    transition_capabilities: contract.transitions
      .filter(({identityRequired}) => !identityRequired)
      .map(({id}) => id),
    theme_capabilities: Object.fromEntries(
      THEME_FIELDS.map((id) => [id, [...contract.theme[id]]]),
    ),
    theme_profile_ids: [...contract.theme.theme_profile_id],
    identity_match_capability: false,
  };
}


export function visualCapabilitiesBytes(contract = getRegistryContract()) {
  return `${JSON.stringify(buildVisualCapabilitiesContract(contract), null, 2)}\n`;
}
