import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import test from "node:test";

import {
  REGISTRY_VERSION,
  createRegistryContract,
  getRegistryContract,
  getRegistrySha256,
  resolveLayout,
  resolveOverlay,
  resolveTheme,
} from "../src/registry/index.mjs";
import {buildVisualCapabilitiesContract} from "../src/registry/visual-capabilities.mjs";

const LAYOUTS = [
  "comparison_split", "cta_offer", "editorial_collage", "material_fullscreen_speaker_pip",
  "method_timeline", "number_proof", "product_hero", "quote_reversal", "speaker_fullscreen",
  "speaker_left_info_right", "speaker_right_evidence_left", "steps_stack",
];
const OVERLAYS = [
  "bullet_list", "chapter_label", "cta_hold", "emphasis_caption", "headline_block", "info_card",
  "lower_third", "number_proof", "product_tag", "quote_card", "standard_caption", "step_indicator",
];

test("registry exposes only the frozen versioned capability set", () => {
  const contract = getRegistryContract();
  assert.equal(REGISTRY_VERSION, "ai-edit-v3-registry-v1");
  assert.deepEqual(contract.layouts.map(({id}) => id), LAYOUTS);
  assert.deepEqual(contract.overlays.map(({id}) => id), OVERLAYS);
  assert.deepEqual(contract.animations.map(({id}) => id), [
    "card_reveal", "count_up", "fade", "highlight_draw", "image_pan_zoom", "light_sweep", "rotate",
    "scale", "slide", "split_screen", "stagger", "stamp", "subtitle_pop", "wipe",
  ]);
  assert.deepEqual(contract.transitions.map(({id}) => id), [
    "card_match_cut", "directional_slide", "hard_cut", "light_flash", "soft_wipe",
  ]);
  assert.equal(contract.transitions.find(({id}) => id === "card_match_cut").identityRequired, true);
  assert(contract.transitions.filter(({id}) => id !== "card_match_cut").every(({identityRequired}) => identityRequired === false));
  assert(Object.isFrozen(contract));
  assert.match(getRegistrySha256(), /^sha256:[0-9a-f]{64}$/);
  assert.equal(getRegistrySha256(), getRegistrySha256());
  assert.equal(JSON.stringify(contract), JSON.stringify(getRegistryContract()));
});

test("visual capability artifact is derived from the renderer registry", () => {
  const contract = getRegistryContract();
  const artifact = JSON.parse(readFileSync(
    new URL("../src/registry/visual-capabilities-v1.json", import.meta.url),
    "utf8",
  ));

  assert.deepEqual(artifact, buildVisualCapabilitiesContract(contract));
  assert.equal(artifact.identity_match_capability, false);
  assert(!artifact.transition_capabilities.includes("card_match_cut"));
});

test("registry rejects duplicate IDs and never resolves aliases", () => {
  const contract = getRegistryContract();
  assert.throws(() => createRegistryContract({
    layouts: [contract.layouts[0], contract.layouts[0]],
    overlays: contract.overlays,
    animations: contract.animations,
    transitions: contract.transitions,
  }), /registry_id_duplicate/);
  assert.throws(() => resolveLayout("SPEAKER_FULLSCREEN", "balanced_a", "9:16"), /layout_unknown/);
  assert.throws(() => resolveLayout("speaker_fullscreen", "balanced-a", "9:16"), /layout_variant_unknown/);
  assert.throws(() => resolveLayout("speaker_fullscreen", "balanced_a", "1:1"), /layout_ratio_unknown/);
  assert.throws(() => resolveOverlay("cta_block"), /overlay_unknown/);
  assert.throws(() => resolveTheme({...validTheme(), palette_id: "user_css"}), /theme_token_unknown/);
});

test("layout and theme resolutions are bounded frozen values", () => {
  const layout = resolveLayout("speaker_fullscreen", "emphasis_b", "16:9");
  assert.equal(layout.contract.id, "speaker_fullscreen");
  assert.equal(layout.variantId, "emphasis_b");
  assert.equal(layout.ratio, "16:9");
  assert(Object.isFrozen(layout));
  const theme = resolveTheme(validTheme());
  assert.equal(theme["--hf-accent"], "#d9a441");
  assert(Object.isFrozen(theme));
});

function validTheme() {
  return {
    palette_id: "midnight_gold",
    typography_id: "editorial_sans",
    density: "balanced",
    motion_energy: "medium",
    image_fit: "cover",
  };
}
