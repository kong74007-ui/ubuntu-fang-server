import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import test from "node:test";

import * as registry from "../src/registry/index.mjs";

const CASES = Object.freeze([
  {id: "speaker_fullscreen", variants: ["clean_center", "headline_top", "caption_sidebar"], required: "speaker", optional: "evidence", identitySlots: ["speaker"]},
  {id: "product_hero", variants: ["center_pedestal", "split_copy", "detail_gallery"], required: "primary", optional: "detail", identitySlots: ["primary"]},
  {id: "steps_stack", variants: ["vertical_steps", "numbered_cards", "progress_path"], required: "steps", optional: "accent", identitySlots: []},
]);
const RATIOS = Object.freeze(["16:9", "9:16"]);

function v2Contracts() {
  assert.equal(typeof registry.getLayoutV2Contracts, "function", "V2 contracts must be exposed independently from the legacy registry");
  return registry.getLayoutV2Contracts();
}

function resolveV2(layoutId, variantId, ratio) {
  assert.equal(typeof registry.resolveLayoutV2, "function", "V2 dispatch must not reuse the legacy V1 resolver");
  return registry.resolveLayoutV2(layoutId, variantId, ratio);
}

function requiredSlots(layout) {
  if (layout.required === "steps") return {steps: {items: ["准备", "执行", "复盘"]}};
  return {[layout.required]: {id: `${layout.id}_01`, kind: layout.required === "speaker" ? "video" : "image", relativePath: `media/${layout.id}.png`}};
}

function compileCase(layout, variantId, ratio, slots = requiredSlots(layout)) {
  return resolveV2(layout.id, variantId, ratio).compile({
    idPrefix: `v2_${layout.id}_${variantId.replaceAll("_", "")}`,
    durationMs: 3000,
    slots,
    designTokens: {"--hf-accent": "#315b8a", "--hf-surface": "#ffffff"},
  });
}

test("layout v2 publishes exactly the three independent representative module contracts", () => {
  const contracts = v2Contracts();
  assert.deepEqual(contracts.map(({id}) => id), CASES.map(({id}) => id));
  assert.deepEqual(registry.getRegistryContract().layouts_v2.map(({id}) => id), CASES.map(({id}) => id).sort());
  for (const expected of CASES) {
    const contract = contracts.find(({id}) => id === expected.id);
    assert.ok(contract, `${expected.id} contract is registered`);
    assert.equal(contract.version, "2.0.0");
    assert.match(contract.moduleId, new RegExp(`^layouts/${expected.id}@2\\.0\\.0$`));
    assert.deepEqual(contract.variants, expected.variants);
    assert.deepEqual(contract.supportedRatios, RATIOS);
    assert.deepEqual(contract.requiredSlots, [expected.required]);
    assert.deepEqual(contract.optionalSlots, [expected.optional]);
    assert.deepEqual(contract.identitySlots, expected.identitySlots);
    assert.equal(contract.fallback, "no_optional_media");
    for (const ratio of RATIOS) assert.ok(contract.safeAreas[ratio], `${expected.id} declares ${ratio} safe areas`);
  }
});

test("layout v2 dispatch leaves legacy V1 layout resolution unchanged", () => {
  const legacy = registry.resolveLayout("speaker_fullscreen", "balanced_a", "16:9");
  assert.equal(legacy.variantId, "balanced_a");
  assert.equal(resolveV2("speaker_fullscreen", "clean_center", "16:9").variantId, "clean_center");
  assert.throws(() => registry.resolveLayout("speaker_fullscreen", "clean_center", "16:9"), /layout_variant_unknown/);
});

test("layout v2 capability entries are bound into the checked-in registry hash", async () => {
  const recorded = await readFile(new URL("../registry-sha256.txt", import.meta.url), "utf8");
  assert.equal(recorded, `${registry.getRegistrySha256()}\n`);
});

test("layout v2 compiles all nine variants for both ratios with auditable structural contracts", () => {
  const signatures = new Map();
  for (const layout of CASES) for (const ratio of RATIOS) for (const variantId of layout.variants) {
    const compiled = compileCase(layout, variantId, ratio);
    assert.deepEqual(Object.keys(compiled).sort(), ["geometryAudit", "html", "identitySlots", "publicTargets"]);
    assert.equal(typeof compiled.html, "string");
    assert.match(compiled.html, new RegExp(`data-layout-v2="${layout.id}"`));
    assert.match(compiled.html, new RegExp(`data-layout-variant="${variantId}"`));
    assert.match(compiled.html, /data-fallback="no_optional_media"/);
    assert.match(compiled.html, /data-fallback-state="rendered"/);
    const signature = compiled.html.match(/data-layout-structure="([^"]+)"/u)?.[1];
    assert.ok(signature, `${layout.id}/${variantId} exposes a structural signature`);
    const variantKey = `${layout.id}/${variantId}`;
    if (signatures.has(variantKey)) assert.equal(signatures.get(variantKey), signature, "ratio changes geometry, not the variant DOM structure");
    else assert.equal([...signatures.values()].includes(signature), false, `each variant has a distinct DOM structure: ${signature}`);
    signatures.set(variantKey, signature);
    assert.deepEqual(compiled.identitySlots, layout.identitySlots);
    assert.deepEqual(Object.keys(compiled.publicTargets).sort(), ["root", "safeArea", "slots"]);
    for (const target of [compiled.publicTargets.root, compiled.publicTargets.safeArea, ...Object.values(compiled.publicTargets.slots)]) {
      assert.match(target, /^#[a-z][a-z0-9_]*$/u);
      assert.match(compiled.html, new RegExp(`id="${target.slice(1)}"`));
    }
    const expectedSize = ratio === "16:9" ? [1920, 1080] : [1080, 1920];
    assert.deepEqual([compiled.geometryAudit.width, compiled.geometryAudit.height], expectedSize);
    for (const box of Object.values(compiled.geometryAudit.safeAreas)) assertBox(box, compiled.geometryAudit);
    for (const box of Object.values(compiled.geometryAudit.criticalRegions)) assertBox(box, compiled.geometryAudit);
  }
  assert.equal(signatures.size, 9);
});

test("layout v2 fails closed for required slots and renders a nonblank optional-slot fallback", () => {
  for (const layout of CASES) {
    assert.throws(() => compileCase(layout, layout.variants[0], "16:9", {}), /layout_required_slot_missing/);
    const compiled = compileCase(layout, layout.variants[0], "9:16");
    assert.match(compiled.html, new RegExp(`data-slot="${layout.optional}"`));
    assert.match(compiled.html, /data-fallback="no_optional_media"/);
    assert.match(compiled.html, /data-fallback-state="rendered"/);
  }
});

function assertBox(box, audit) {
  assert.ok(box.x >= 0 && box.y >= 0 && box.width > 0 && box.height > 0);
  assert.ok(box.x + box.width <= audit.width && box.y + box.height <= audit.height);
}
