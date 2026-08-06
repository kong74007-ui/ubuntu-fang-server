import assert from "node:assert/strict";
import {mkdir, mkdtemp, readFile, writeFile} from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import * as registry from "../src/registry/index.mjs";
import {checkRegistryHash} from "../src/write-registry-hash.mjs";

const CASES = Object.freeze([
  {id: "speaker_fullscreen", variants: ["clean_center", "headline_top", "caption_sidebar"], required: ["speaker"], optional: ["evidence"], identitySlots: ["speaker"]},
  {id: "speaker_left_info_right", variants: ["card_stack", "number_focus", "image_evidence"], required: ["speaker"], optional: ["evidence"], identitySlots: ["speaker"]},
  {id: "speaker_right_evidence_left", variants: ["document_panel", "comparison_panel", "quote_evidence"], required: ["speaker"], optional: ["evidence"], identitySlots: ["speaker"]},
  {id: "material_fullscreen_speaker_pip", variants: ["pip_round", "pip_card", "pip_edge"], required: ["speaker", "primary"], optional: ["detail"], identitySlots: ["speaker", "primary"]},
  {id: "product_hero", variants: ["center_pedestal", "split_copy", "detail_gallery"], required: ["primary"], optional: ["detail"], identitySlots: ["primary"]},
  {id: "editorial_collage", variants: ["magazine_grid", "layered_cards", "film_strip"], required: ["primary"], optional: ["detail"], identitySlots: ["primary"]},
  {id: "comparison_split", variants: ["vertical_divide", "before_after_slider", "score_compare"], required: ["primary"], optional: ["detail"], identitySlots: ["primary"]},
  {id: "steps_stack", variants: ["vertical_steps", "numbered_cards", "progress_path"], required: ["steps"], optional: ["accent"], identitySlots: []},
  {id: "number_proof", variants: ["hero_number", "metric_grid", "chart_callout"], required: ["proof"], optional: ["evidence"], identitySlots: []},
  {id: "quote_reversal", variants: ["diagonal_statement", "strike_reveal", "question_answer"], required: ["quote"], optional: ["evidence"], identitySlots: []},
  {id: "method_timeline", variants: ["horizontal_timeline", "vertical_milestones", "chapter_route"], required: ["steps"], optional: ["accent"], identitySlots: []},
  {id: "cta_offer", variants: ["offer_card", "qr_placeholder", "action_steps"], required: ["message"], optional: ["accent"], identitySlots: []},
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
  return Object.fromEntries(layout.required.map((slot) => {
    if (slot === "steps") return [slot, {items: ["准备", "执行", "复盘"]}];
    if (slot === "proof") return [slot, {label: "增长率", value: "38%"}];
    if (slot === "quote") return [slot, {text: "真正的增长来自长期价值"}];
    if (slot === "message") return [slot, {text: "立即预约体验"}];
    return [slot, {id: `${layout.id}_${slot}_01`, kind: slot === "speaker" ? "video" : "image", relativePath: `media/${layout.id}-${slot}.${slot === "speaker" ? "mp4" : "png"}`}];
  }));
}

function compileCase(layout, variantId, ratio, slots = requiredSlots(layout)) {
  return resolveV2(layout.id, variantId, ratio).compile({
    idPrefix: `v2_${layout.id}_${variantId.replaceAll("_", "")}`,
    durationMs: 3000,
    slots,
    designTokens: {"--hf-accent": "#315b8a", "--hf-surface": "#ffffff"},
  });
}

test("layout v2 publishes the complete twelve-layout contract matrix", () => {
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
    assert.deepEqual(contract.requiredSlots, expected.required);
    assert.deepEqual(contract.optionalSlots, expected.optional);
    assert.deepEqual(contract.identitySlots, expected.identitySlots);
    assert.equal(contract.fallback, "no_optional_media");
    for (const ratio of RATIOS) {
      assert.deepEqual(Object.keys(contract.safeAreas[ratio]).sort(), ["center", "left_panel", "lower_third", "right_panel", "subtitle_safe", "title_safe"]);
      const [width, height] = ratio === "16:9" ? [1920, 1080] : [1080, 1920];
      for (const [placement, box] of Object.entries(contract.safeAreas[ratio])) {
        assert.deepEqual(Object.keys(box).sort(), ["height", "width", "x", "y"], `${expected.id}/${ratio}/${placement} geometry contract`);
        assert.ok(box.x >= 0 && box.y >= 0 && box.width > 0 && box.height > 0 && box.x + box.width <= width && box.y + box.height <= height);
      }
    }
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
  const manifest = registry.getRegistrySourceManifest();
  assert.ok(manifest.some((entry) => entry.path === "layouts/product_hero.mjs"));
  assert.ok(manifest.some((entry) => entry.path === "layouts/layout-v2-primitives.mjs"));
  const stale = path.join(await mkdtemp(path.join(os.tmpdir(), "v3-registry-drift-")), "registry-sha256.txt");
  await writeFile(stale, "sha256:" + "0".repeat(64) + "\n", "utf8");
  await assert.rejects(checkRegistryHash(stale), /registry_hash_drift/);
});

test("registry source manifest sorts slash-normalized paths by Unicode code point", async () => {
  const root = await mkdtemp(path.join(os.tmpdir(), "v3-registry-source-"));
  await mkdir(path.join(root, "nested"));
  await Promise.all(["z.mjs", "ä.mjs", "nested/a.mjs"].map((file) => writeFile(path.join(root, file), "export {};", "utf8")));
  assert.deepEqual(registry.getRegistrySourceManifest(root).map((entry) => entry.path), ["nested/a.mjs", "z.mjs", "ä.mjs"]);
});

test("layout v2 compiles all thirty-six variants for both ratios with auditable structural contracts", () => {
  const signatures = new Map();
  const geometrySignatures = new Map();
  for (const layout of CASES) for (const ratio of RATIOS) for (const variantId of layout.variants) {
    const compiled = compileCase(layout, variantId, ratio);
    assert.deepEqual(Object.keys(compiled).sort(), ["geometryAudit", "html", "identitySlots", "publicTargets"]);
    assert.equal(typeof compiled.html, "string");
    assert.match(compiled.html, new RegExp(`data-layout-v2="${layout.id}"`));
    assert.match(compiled.html, new RegExp(`data-layout-variant="${variantId}"`));
    assert.match(compiled.html, /data-fallback="no_optional_media"/);
    assert.match(compiled.html, /data-fallback-state="rendered"/);
    assertStrictTimelineStructure(compiled.html, `${layout.id}/${variantId}/${ratio}`);
    const signature = treeSignature(compiled.html);
    const variantKey = `${layout.id}/${variantId}`;
    if (signatures.has(variantKey)) assert.equal(signatures.get(variantKey), signature, "ratio changes geometry, not the variant DOM structure");
    else assert.equal([...signatures.values()].includes(signature), false, `each variant has a distinct DOM structure: ${signature}`);
    signatures.set(variantKey, signature);
    assert.deepEqual(compiled.identitySlots, layout.identitySlots);
    assert.deepEqual(Object.keys(compiled.publicTargets).sort(), ["root", "safeAreas", "slots"]);
    assert.deepEqual(Object.keys(compiled.publicTargets.safeAreas).sort(), ["center", "left_panel", "lower_third", "right_panel", "subtitle_safe", "title_safe"]);
    for (const target of [compiled.publicTargets.root, ...Object.values(compiled.publicTargets.safeAreas), ...Object.values(compiled.publicTargets.slots)]) {
      assert.match(target, /^#[a-z][a-z0-9_]*$/u);
      assert.match(compiled.html, new RegExp(`id="${target.slice(1)}"`));
    }
    const expectedSize = ratio === "16:9" ? [1920, 1080] : [1080, 1920];
    assert.deepEqual([compiled.geometryAudit.width, compiled.geometryAudit.height], expectedSize);
    for (const box of Object.values(compiled.geometryAudit.safeAreas)) assertBox(box, compiled.geometryAudit);
    for (const [region, box] of Object.entries(compiled.geometryAudit.criticalRegions)) {
      assertBox(box, compiled.geometryAudit);
      assert.match(compiled.html, new RegExp(`data-v2-region="${region}"`));
      assert.match(compiled.html, new RegExp(`\\[data-v2-region="${region}"\\]\\{position:absolute;left:${box.x}px;top:${box.y}px;width:${box.width}px;height:${box.height}px`));
    }
    if (!["speaker_fullscreen", "product_hero", "steps_stack"].includes(layout.id)) {
      const geometryKey = `${layout.id}/${ratio}`;
      const geometry = JSON.stringify(compiled.geometryAudit.criticalRegions);
      const siblings = geometrySignatures.get(geometryKey) ?? new Set();
      assert.equal(siblings.has(geometry), false, `${layout.id}/${ratio}/${variantId} must have variant-specific visible geometry`);
      siblings.add(geometry);
      geometrySignatures.set(geometryKey, siblings);
    }
  }
  assert.equal(signatures.size, 36);
});

test("layout v2 emits byte-identical styles for semantically identical token objects", () => {
  const layout = CASES.find(({id}) => id === "product_hero");
  const first = compileCase(layout, "center_pedestal", "16:9", requiredSlots(layout));
  const second = resolveV2(layout.id, "center_pedestal", "16:9").compile({
    idPrefix: "v2_product_hero_centerpedestal", durationMs: 3000, slots: requiredSlots(layout),
    designTokens: {"--hf-surface": "#ffffff", "--hf-accent": "#315b8a"},
  });
  assert.equal(first.html, second.html);
});

test("layout v2 fails closed for required slots and renders a nonblank optional-slot fallback", () => {
  for (const layout of CASES) {
    assert.throws(() => compileCase(layout, layout.variants[0], "16:9", {}), /layout_required_slot_missing/);
    const compiled = compileCase(layout, layout.variants[0], "9:16");
    for (const optional of layout.optional) assert.match(compiled.html, new RegExp(`data-slot="${optional}"`));
    assert.match(compiled.html, /data-fallback="no_optional_media"/);
    assert.match(compiled.html, /data-fallback-state="rendered"/);
  }
});

test("layout v2 visible copy and counter regions are nonempty and geometry-bounded in both ratios", () => {
  for (const ratio of RATIOS) {
    const product = compileCase(CASES.find(({id}) => id === "product_hero"), "split_copy", ratio);
    const copyBox = product.geometryAudit.criticalRegions.copy;
    assert.ok(copyBox, "split_copy must audit its visible copy region");
    assertBox(copyBox, product.geometryAudit);
    assert.ok(copyBox.width * copyBox.height / (product.geometryAudit.width * product.geometryAudit.height) <= 0.25);
    const copy = product.html.match(/<section\b[^>]*data-v2-region="copy"[^>]*>([\s\S]*?)<\/section>/u)?.[1] ?? "";
    assert.match(copy, /<(?:svg|span)\b/u, "split_copy must render authoritative copy content or an explicit graphic fallback");

    const steps = compileCase(CASES.find(({id}) => id === "steps_stack"), "numbered_cards", ratio);
    const counterBox = steps.geometryAudit.criticalRegions.counter;
    assert.ok(counterBox, "numbered_cards must audit its visible counter region");
    assertBox(counterBox, steps.geometryAudit);
    assert.ok(counterBox.width * counterBox.height / (steps.geometryAudit.width * steps.geometryAudit.height) <= 0.25);
    const counter = steps.html.match(/<header\b[^>]*data-v2-region="counter"[^>]*>[\s\S]*?<\/header>/u)?.[0] ?? "";
    assert.match(counter, /data-safe-text="3"[^>]*><span><\/span>/u, "numbered_cards must expose a deterministic hydrated step count");
  }
});

test("layout v2 unit-two visible regions contain authoritative content or an explicit graphic fallback", () => {
  const unitTwo = CASES.filter(({id}) => ["editorial_collage", "comparison_split", "number_proof", "quote_reversal", "method_timeline", "cta_offer"].includes(id));
  for (const layout of unitTwo) for (const ratio of RATIOS) for (const variantId of layout.variants) {
    const compiled = compileCase(layout, variantId, ratio);
    for (const region of Object.keys(compiled.geometryAudit.criticalRegions)) {
      const body = regionBody(compiled.html, region);
      assert.match(body, /<(?:img|video|svg|span|ol|ul|strong|b)\b/u, `${layout.id}/${variantId}/${ratio}/${region} must render content or a graphic fallback`);
    }
    const markup = compiled.html.split("<style", 1)[0];
    assert.doesNotMatch(markup, /data-v2-region="[^"]+"[^>]*>\s*<\/[^>]+>/u, `${layout.id}/${variantId}/${ratio} cannot publish an empty visible region`);
  }
});

test("cta offer requires and renders the caller-supplied authoritative message", () => {
  const layout = CASES.find(({id}) => id === "cta_offer");
  assert.throws(() => compileCase(layout, "offer_card", "16:9", {}), /layout_required_slot_missing/);
  const compiled = compileCase(layout, "offer_card", "9:16", {message: {text: "权威 CTA 文案"}});
  assert.match(compiled.html, /data-safe-text="权威 CTA 文案"[^>]*><span><\/span>/u);
  assert.doesNotMatch(compiled.html, /立即购买|点击下单|默认优惠/u);
});

function assertBox(box, audit) {
  assert.ok(box.x >= 0 && box.y >= 0 && box.width > 0 && box.height > 0);
  assert.ok(box.x + box.width <= audit.width && box.y + box.height <= audit.height);
}

function regionBody(html, region) {
  const pattern = new RegExp(`<([a-z][a-z0-9-]*)\\b[^>]*data-v2-region="${region}"[^>]*>([\\s\\S]*?)<\\/\\1>`, "u");
  return html.match(pattern)?.[2] ?? "";
}

function assertStrictTimelineStructure(html, label) {
  const markup = html.replace(/<style\b[\s\S]*?<\/style>/gu, "");
  const stack = [];
  for (const match of markup.matchAll(/<\/?([a-z][a-z0-9-]*)\b[^>]*>/giu)) {
    const [token, rawTag] = match;
    const tag = rawTag.toLowerCase();
    if (token.startsWith("</")) {
      assert.equal(stack.pop()?.tag, tag, `${label} closes ${tag} in document order`);
      continue;
    }
    const timed = /\sdata-start="/u.test(token);
    if (timed) assert.match(token, /\sid="[a-z][a-z0-9_]*"/u, `${label} gives every timeline-visible element a stable id`);
    if (tag === "video" && timed) {
      assert.equal(stack.some((node) => node.timed), false, `${label} does not nest a timed video in another timed element`);
      assert.match(token, /\sclass="[^"]*\bclip\b[^"]*"/u, `${label} initializes every timed video as a managed clip`);
    }
    if (!token.endsWith("/>") && tag !== "img") stack.push({tag, timed});
  }
  assert.equal(stack.length, 0, `${label} has balanced timeline markup`);
}

function treeSignature(html) {
  const root = {tag: "root", children: []};
  const stack = [root];
  for (const match of html.matchAll(/<\/?([a-z][a-z0-9-]*)\b[^>]*>/giu)) {
    const [token, tag] = match;
    if (token.startsWith("</")) {
      assert.equal(stack.pop().tag, tag.toLowerCase(), `balanced HTML tree closes ${tag}`);
    } else {
      const node = {tag: tag.toLowerCase(), children: []};
      stack.at(-1).children.push(node);
      if (!token.endsWith("/>") && node.tag !== "img") stack.push(node);
    }
  }
  assert.equal(stack.length, 1, "balanced HTML tree");
  return serialize(root);
}

function serialize(node) {
  return `${node.tag}(${node.children.map(serialize).join(",")})`;
}
