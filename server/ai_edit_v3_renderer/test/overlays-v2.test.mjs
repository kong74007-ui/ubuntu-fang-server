import assert from "node:assert/strict";
import {mkdtemp, readFile} from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import test from "node:test";

import {compileProjectV2} from "../src/compile-project-v2.mjs";
import {compileProject} from "../src/compile-project.mjs";
import {getRegistrySha256} from "../src/registry/index.mjs";

const REPRESENTATIVE_OVERLAYS = Object.freeze([
  Object.freeze({
    componentId: "headline_block",
    expectedTargets: Object.freeze(["root", "headline", "underline"]),
    expectedStructure: Object.freeze(["header", "h1", "span"]),
  }),
  Object.freeze({
    componentId: "standard_caption",
    expectedTargets: Object.freeze(["root", "caption", "emphasis"]),
    expectedStructure: Object.freeze(["div", "p", "span"]),
  }),
  Object.freeze({
    componentId: "info_card",
    expectedTargets: Object.freeze(["root", "label", "body", "accent"]),
    expectedStructure: Object.freeze(["article", "header", "p"]),
  }),
]);

const REMAINING_OVERLAYS = Object.freeze([
  Object.freeze({componentId: "emphasis_caption", targets: ["root", "caption", "highlight"], tags: ["div", "p", "mark"]}),
  Object.freeze({componentId: "chapter_label", targets: ["root", "chapter", "rule"], tags: ["header", "span", "i"]}),
  Object.freeze({componentId: "lower_third", targets: ["root", "name", "role", "accent"], tags: ["aside", "strong", "small"]}),
  Object.freeze({componentId: "bullet_list", targets: ["root", "items", "bullets"], tags: ["section", "ul", "li"]}),
  Object.freeze({componentId: "number_proof", targets: ["root", "metric_value", "unit", "label"], tags: ["dl", "dt", "dd"]}),
  Object.freeze({componentId: "quote_card", targets: ["root", "quote", "accent", "attribution"], tags: ["blockquote", "p", "footer"]}),
  Object.freeze({componentId: "step_indicator", targets: ["root", "progress", "current", "total"], tags: ["nav", "ol", "li"]}),
  Object.freeze({componentId: "product_tag", targets: ["root", "product", "label", "price"], tags: ["aside", "strong", "span"]}),
  Object.freeze({componentId: "cta_hold", targets: ["root", "action", "support", "accent"], tags: ["section", "strong", "small"]}),
]);

const LEGAL_PLACEMENTS = Object.freeze({
  headline_block: ["title_safe"], standard_caption: ["subtitle_safe"], emphasis_caption: ["title_safe", "subtitle_safe"],
  chapter_label: ["title_safe", "lower_third"], lower_third: ["lower_third"],
  info_card: ["left_panel", "right_panel", "center"], bullet_list: ["left_panel", "right_panel", "center"],
  number_proof: ["left_panel", "right_panel", "center"], quote_card: ["left_panel", "right_panel", "center"],
  step_indicator: ["left_panel", "right_panel", "center"], product_tag: ["left_panel", "right_panel", "center"], cta_hold: ["center"],
});

test("Task 7a exposes three real overlay modules with distinct structures and public targets", async () => {
  const overlayRegistry = await import("../src/registry/overlays.mjs");
  assert.equal(typeof overlayRegistry.compileOverlayV2, "function");

  const signatures = [];
  for (const definition of REPRESENTATIVE_OVERLAYS) {
    const module = await import(`../src/registry/overlays/${definition.componentId}.mjs`);
    assert.equal(typeof module.compileOverlayComponent, "function", definition.componentId);
    const output = overlayRegistry.compileOverlayV2({
      componentId: definition.componentId,
      instanceId: `${definition.componentId}_01`,
      content: {text: "真实权威内容 42%"},
      placement: LEGAL_PLACEMENTS[definition.componentId][0],
      ratio: "16:9",
      durationMs: 3000,
      trackIndex: 21,
    });
    assert.deepEqual([...output.publicTargets].sort(), [...definition.expectedTargets].sort());
    assert.equal(output.textAudit.authoritativeText, "真实权威内容 42%");
    assert.equal(output.textAudit.truncated, false);
    assert.match(output.html, /data-overlay-v2=/u);
    assert.match(output.html, new RegExp(`data-placement="${LEGAL_PLACEMENTS[definition.componentId][0]}"`, "u"));
    assert.match(output.html, /data-text-fit-step=/u);
    for (const tag of definition.expectedStructure) assert.match(output.html, new RegExp(`<${tag}\\b`, "u"));
    signatures.push(normalizeElementTree(output.html));
  }
  assert.equal(new Set(signatures).size, REPRESENTATIVE_OVERLAYS.length, "representative overlays require distinct real DOM trees");
});

test("Task 7a text fitting is deterministic for Unicode without rewriting or truncating facts", async () => {
  const {fitOverlayText} = await import("../src/registry/text-fit.mjs");
  const cases = [
    "售价 499 元，赠送 1000 积分，活动截止 8 月 31 日。",
    "A deterministic English headline keeps PRODUCT-X and 42.5% unchanged.",
  ];
  for (const ratio of ["16:9", "9:16"]) {
    for (const text of cases) {
      const input = {text, ratio, bounds: {width: ratio === "16:9" ? 880 : 760, height: 180}, fontSizeSteps: [56, 50, 44, 38], lineHeight: 1.2, maxLines: 3};
      const first = fitOverlayText(input);
      const second = fitOverlayText({...input, bounds: {...input.bounds}, fontSizeSteps: [...input.fontSizeSteps]});
      assert.deepEqual(first, second);
      assert.equal(first.text, text);
      assert.equal(first.truncated, false);
      assert.ok(input.fontSizeSteps.includes(first.fontSize));
      assert.ok(first.estimatedLines >= 1 && first.estimatedLines <= input.maxLines);
    }
  }
  assert.throws(() => fitOverlayText({text: "", ratio: "16:9", bounds: {width: 800, height: 180}, fontSizeSteps: [48], lineHeight: 1.2, maxLines: 2}), /overlay_authoritative_text_empty/);
});

test("Task 7 semantic splitting preserves Chinese punctuation and metric units without mojibake", async () => {
  const {boundedClauses, metricParts} = await import("../src/registry/overlays/overlay-v2-primitives.mjs");
  assert.deepEqual(boundedClauses("第一句。第二句！第三句？第四句；第五句;", 5), ["第一句。", "第二句！", "第三句？", "第四句；", "第五句;"]);
  for (const unit of ["元", "个", "人", "件", "万", "亿", "倍", "年", "天", "积分", "%"]) {
    const text = `累计 42.5${unit} 已完成`;
    const parts = metricParts(text);
    assert.equal(parts.value, "42.5");
    assert.equal(parts.unit, unit);
    assert.equal(parts.label, "累计  已完成");
  }
});

test("Task 7 overlay placement matrix fits every legal component content box and rejects unsafe combinations", async () => {
  const {compileOverlayV2} = await import("../src/registry/overlays.mjs");
  const {OVERLAY_PLACEMENT_CATALOG, OVERLAY_PLACEMENTS_BY_COMPONENT} = await import("../src/registry/overlays/overlay-placement-contract.mjs");
  const {overlayPlacementBox} = await import("../src/registry/layouts/layout-v2-primitives.mjs");
  for (const budget of OVERLAY_PLACEMENT_CATALOG.entries) {
    const {component_id: componentId, placement, ratio} = budget;
    const actualHost = overlayPlacementBox(ratio, placement);
    assert.deepEqual(budget.host_box, {width: actualHost.width, height: actualHost.height}, `${componentId}:${placement}:${ratio} must bind the actual safe host`);
    for (const text of ["权".repeat(budget.max_chars), "A".repeat(budget.max_chars)]) {
      const output = compileOverlayV2({componentId, instanceId: `${componentId}_${placement}_${ratio.replace(":", "_")}`, content: {text}, placement, ratio, durationMs: 4000, trackIndex: 21});
      assert.equal(output.textAudit.authoritativeText, text);
      assert.equal(output.textAudit.truncated, false);
      assert.ok(budget.font_size_steps.includes(output.textAudit.fontSize), `${componentId}:${placement}:${ratio} registry font step violated`);
      assert.ok(output.geometryAudit.contentHeight + output.geometryAudit.chromeHeight <= output.geometryAudit.hostHeight, `${componentId}:${placement}:${ratio} height overflow`);
      assert.ok(output.geometryAudit.contentWidth + output.geometryAudit.chromeWidth <= output.geometryAudit.hostWidth, `${componentId}:${placement}:${ratio} width overflow`);
      assert.match(output.html, /data-host-box="\d+,\d+" data-content-box="\d+,\d+"/u);
    }
    assert.throws(() => compileOverlayV2({componentId, instanceId: `${componentId}_overflow`, content: {text: "权".repeat(budget.max_chars + 1)}, placement, ratio, durationMs: 4000, trackIndex: 21}), /manifest_overlay_text_budget_exceeded/);
  }
  assert.deepEqual(Object.fromEntries(Object.entries(OVERLAY_PLACEMENTS_BY_COMPONENT).map(([key, value]) => [key, [...value]])), LEGAL_PLACEMENTS);
  assert.throws(() => compileOverlayV2({componentId: "info_card", instanceId: "unsafe_info", content: {text: "权威事实"}, placement: "title_safe", ratio: "16:9", durationMs: 4000, trackIndex: 21}), /manifest_overlay_placement_invalid/);
});

test("Task 7 placement catalog compares actual host dimensions independent of JSON key order", async () => {
  const {OVERLAY_PLACEMENT_CATALOG, validateOverlayPlacementCatalog} = await import("../src/registry/overlays/overlay-placement-contract.mjs");
  const reordered = structuredClone(OVERLAY_PLACEMENT_CATALOG);
  const first = reordered.entries[0];
  first.host_box = {height: first.host_box.height, width: first.host_box.width};
  assert.doesNotThrow(() => validateOverlayPlacementCatalog(reordered));
  const drifted = structuredClone(reordered);
  drifted.entries[0].host_box.width += 1;
  assert.throws(() => validateOverlayPlacementCatalog(drifted), /overlay_placement_catalog_invalid/);
});

test("Task 7a routes six placements to six independent ratio-aware safe hosts", async () => {
  for (const ratio of ["16:9", "9:16"]) {
    const outputRoot = path.join(await mkdtemp(path.join(os.tmpdir(), `v3-overlays-six-hosts-${ratio.replace(":", "-")}-`)), "project");
    const manifest = fixtureManifest(ratio);
    const placements = ["title_safe", "subtitle_safe", "left_panel", "right_panel", "center", "lower_third"];
    const components = ["headline_block", "standard_caption", "info_card", "info_card", "cta_hold", "lower_third"];
    manifest.compositions[0].overlay_ids = components;
    manifest.compositions[0].overlay_instances = placements.map((placement, index) => ({
      instance_id: `overlay_${index + 1}`,
      component_id: components[index],
      content_ref: index % 2 === 0 ? "headline" : "highlight",
      placement,
    }));

    await compileProjectV2({manifest, outputRoot});
    const scene = await readFile(path.join(outputRoot, "compositions", "composition_01.html"), "utf8");
    const hosts = [...scene.matchAll(/<aside\b[^>]*data-overlay-host="([a-z_]+)"[^>]*data-safe-box="([^"]+)"[^>]*>([\s\S]*?)<\/aside>/gu)];
    assert.deepEqual(hosts.map((match) => match[1]).sort(), placements.toSorted());
    assert.equal(new Set(hosts.map((match) => match[2])).size, placements.length, `${ratio} placements require independent geometry`);
    for (let index = 0; index < placements.length; index += 1) {
      const host = hosts.find((match) => match[1] === placements[index]);
      assert.ok(host, `${placements[index]} host missing`);
      assert.match(host[3], new RegExp(`id="composition_01_overlay_${index + 1}_${components[index]}"`, "u"));
    }
  }
});

test("Task 7a resolves content_ref from frozen authoritative content and rejects untrusted projection fields", async () => {
  const outputRoot = path.join(await mkdtemp(path.join(os.tmpdir(), "v3-overlays-authoritative-")), "project");
  const manifest = fixtureManifest("16:9");
  manifest.captions[0].text = "字幕正文不应覆盖标题";
  manifest.compositions[0].authoritative_content = {
    headline: {text: "品牌名 PRODUCT-X 售价 499 元", source_caption_ids: ["caption_01"]},
    highlight: {text: "限量 42 份", source_caption_ids: ["caption_01"]},
  };
  manifest.compositions[0].overlay_ids = ["headline_block", "info_card"];
  manifest.compositions[0].overlay_instances = [
    {instance_id: "headline_01", component_id: "headline_block", content_ref: "headline", placement: "title_safe"},
    {instance_id: "info_01", component_id: "info_card", content_ref: "highlight", placement: "right_panel"},
  ];
  await compileProjectV2({manifest, outputRoot});
  const scene = await readFile(path.join(outputRoot, "compositions", "composition_01.html"), "utf8");
  assert.match(scene, /data-safe-text="品牌名 PRODUCT-X 售价 499 元"/u);
  assert.match(scene, /data-safe-text="限量 42 份"/u);
  assert.doesNotMatch(scene, /data-safe-text="字幕正文不应覆盖标题"/u);

  for (const mutate of [
    (item) => ({...item, content_ref: "invented_fact"}),
    (item) => ({...item, placement: "floating_anywhere"}),
    (item) => ({...item, html: "<script>alert(1)</script>"}),
  ]) {
    const invalid = fixtureManifest("16:9");
    invalid.compositions[0].overlay_ids = ["headline_block"];
    invalid.compositions[0].authoritative_content = {headline: {text: "权威标题", source_caption_ids: ["caption_01"]}, highlight: {text: "权威高亮", source_caption_ids: ["caption_01"]}};
    invalid.compositions[0].overlay_instances = [mutate({instance_id: "headline_01", component_id: "headline_block", content_ref: "headline", placement: "title_safe"})];
    const invalidRoot = await awaitTempRoot();
    await assert.rejects(() => compileProjectV2({manifest: invalid, outputRoot: path.join(invalidRoot, "project")}), /manifest_overlay_(?:content_ref|placement|instance)_invalid/);
  }
});

for (const definition of REMAINING_OVERLAYS) {
  test(`Task 7b compiles ${definition.componentId} as an independent semantic component`, async () => {
    const module = await import(`../src/registry/overlays/${definition.componentId}.mjs`);
    assert.equal(typeof module.compileOverlayComponent, "function");
    const {compileOverlayV2} = await import("../src/registry/overlays.mjs");
    for (const ratio of ["16:9", "9:16"]) {
      const authoritative = "品牌 PRODUCT-X 售价 499 元，限量 42 份；现在立即了解详情。";
      const output = compileOverlayV2({
        componentId: definition.componentId, instanceId: `${definition.componentId}_${ratio.replace(":", "_")}`,
        content: {text: authoritative}, placement: placementFor(definition.componentId), ratio, durationMs: 3600, trackIndex: 25,
      });
      assert.deepEqual([...output.publicTargets].sort(), [...definition.targets].sort());
      assert.equal(output.textAudit.authoritativeText, authoritative);
      assert.equal(output.textAudit.truncated, false);
      assert.match(output.html, new RegExp(`data-overlay-v2="${definition.componentId}"`, "u"));
      assert.match(output.html, /data-text-fit-step="[0-9]+"/u);
      for (const tag of definition.tags) assert.match(output.html, new RegExp(`<${tag}\\b`, "u"));
      assert.doesNotMatch(output.html, />品牌 PRODUCT-X/u, "dynamic facts must not be interpolated into executable markup");
      assert.ok([...output.html.matchAll(/data-safe-text="[^"]+"/gu)].length >= 1, "authoritative facts must hydrate through textContent");
    }
  });
}

test("Task 7b all six placement boxes are bounded and pairwise non-overlapping in both ratios", async () => {
  for (const ratio of ["16:9", "9:16"]) {
    const outputRoot = path.join(await mkdtemp(path.join(os.tmpdir(), `v3-overlay-box-audit-${ratio.replace(":", "-")}-`)), "project");
    const manifest = fixtureManifest(ratio);
    const placements = ["title_safe", "subtitle_safe", "left_panel", "right_panel", "center", "lower_third"];
    manifest.compositions[0].overlay_ids = [];
    manifest.compositions[0].overlay_instances = [];
    await compileProjectV2({manifest, outputRoot});
    const scene = await readFile(path.join(outputRoot, "compositions", "composition_01.html"), "utf8");
    const boxes = [...scene.matchAll(/data-overlay-host="([a-z_]+)"[^>]*data-safe-box="(\d+),(\d+),(\d+),(\d+)"/gu)].map((match) => ({placement: match[1], x: Number(match[2]), y: Number(match[3]), width: Number(match[4]), height: Number(match[5])}));
    assert.equal(boxes.length, 6);
    const [canvasWidth, canvasHeight] = ratio === "16:9" ? [1920, 1080] : [1080, 1920];
    for (const box of boxes) {
      assert.ok(box.x >= 0 && box.y >= 0 && box.x + box.width <= canvasWidth && box.y + box.height <= canvasHeight, `${ratio}:${box.placement} overflows`);
    }
    for (let left = 0; left < boxes.length; left += 1) {
      for (let right = left + 1; right < boxes.length; right += 1) assert.equal(overlaps(boxes[left], boxes[right]), false, `${ratio}:${boxes[left].placement}/${boxes[right].placement} overlap`);
    }
  }
});

test("Task 7 compiles all twelve semantic overlays through the real V2 project entry", async () => {
  const components = [...REPRESENTATIVE_OVERLAYS.map(({componentId}) => componentId), ...REMAINING_OVERLAYS.map(({componentId}) => componentId)];
  for (const ratio of ["16:9", "9:16"]) {
    const outputRoot = path.join(await mkdtemp(path.join(os.tmpdir(), `v3-overlays-e2e-${ratio.replace(":", "-")}-`)), "project");
    const manifest = multiSceneManifest(ratio, components);
    await compileProjectV2({manifest, outputRoot});
    const structureSignatures = [];
    for (let index = 0; index < components.length; index += 1) {
      const componentId = components[index];
      const compositionId = `composition_${String(index + 1).padStart(2, "0")}`;
      const scene = await readFile(path.join(outputRoot, "compositions", `${compositionId}.html`), "utf8");
      assert.match(scene, new RegExp(`data-overlay-v2="${componentId}"`, "u"));
      const expectedTargets = (REPRESENTATIVE_OVERLAYS.find((item) => item.componentId === componentId)?.expectedTargets ?? REMAINING_OVERLAYS.find((item) => item.componentId === componentId).targets).filter((target) => target !== "root");
      for (const target of expectedTargets) assert.match(scene, new RegExp(`data-public-target="${target}"`, "u"));
      const overlay = scene.match(new RegExp(`<((?:header|div|article|aside|section|dl|blockquote|nav))\\b[^>]*data-overlay-v2="${componentId}"[\\s\\S]*?<\\/\\1>`, "u"))?.[0] ?? "";
      assert.ok(overlay, `${componentId} missing from final scene`);
      assertEverySafeTextHasHydrationTarget(overlay, componentId);
      assert.doesNotMatch(overlay, /主体画面|辅助画面|内容占位|placeholder/iu);
      assert.ok((scene.match(new RegExp(`\\.hf-overlay-v2-${cssClassFor(componentId)}\\b`, "gu")) ?? []).length >= 1, `${componentId} lacks component CSS`);
      structureSignatures.push(normalizeElementTree(overlay));
    }
    assert.equal(new Set(structureSignatures).size, components.length, `${ratio} components must retain twelve distinct final DOM trees`);
  }
});

test("Task 7 repeated component instances preserve exact instance, authoritative ref and placement", async () => {
  const outputRoot = path.join(await mkdtemp(path.join(os.tmpdir(), "v3-overlays-repeat-e2e-")), "project");
  const manifest = fixtureManifest("9:16");
  manifest.compositions[0].authoritative_content = {
    headline: {text: "中文长标题保持品牌 PRODUCT-X 与价格 499 元，不得重写或截断。", source_caption_ids: ["caption_01"]},
    highlight: {text: "A separate English fact keeps 42.5% and PRODUCT-X exactly unchanged.", source_caption_ids: ["caption_01"]},
  };
  manifest.compositions[0].overlay_ids = ["emphasis_caption", "emphasis_caption"];
  manifest.compositions[0].overlay_instances = [
    {instance_id: "headline_cn", component_id: "emphasis_caption", content_ref: "headline", placement: "title_safe"},
    {instance_id: "headline_en", component_id: "emphasis_caption", content_ref: "highlight", placement: "subtitle_safe"},
  ];
  manifest.compositions[0].animations = [
    {target: "headline_cn", preset: "fade", direction: "in", duration_ms: 400, delay_ms: 0},
    {target: "headline_en", preset: "scale", direction: "in", duration_ms: 400, delay_ms: 50},
  ];
  await compileProjectV2({manifest, outputRoot});
  const scene = await readFile(path.join(outputRoot, "compositions", "composition_01.html"), "utf8");
  const title = scene.match(/data-overlay-host="title_safe"[\s\S]*?<\/aside>/u)?.[0] ?? "";
  const lower = scene.match(/data-overlay-host="subtitle_safe"[\s\S]*?<\/aside>/u)?.[0] ?? "";
  assert.match(title, /id="composition_01_headline_cn_emphasis_caption"[\s\S]*data-safe-text="中文长标题保持品牌 PRODUCT-X 与价格 499 元，不得重写或截断。"/u);
  assert.doesNotMatch(title, /headline_en/u);
  assert.match(lower, /id="composition_01_headline_en_emphasis_caption"[\s\S]*data-safe-text="A separate English fact keeps 42\.5% and PRODUCT-X exactly unchanged\."/u);
  assert.doesNotMatch(lower, /headline_cn/u);
  const selectors = [...scene.matchAll(/tl\.fromTo\("(#[a-z][a-z0-9_]*)"/gu)].map((match) => match[1]);
  assert.deepEqual(selectors, ["#composition_01_headline_cn_emphasis_caption", "#composition_01_headline_en_emphasis_caption"]);
  for (const selector of selectors) {
    assert.equal((scene.match(new RegExp(`\\bid="${selector.slice(1)}"`, "gu")) ?? []).length, 1, `${selector} must resolve exactly once`);
  }
});

test("Task 7 leaves the V1 compiler on its legacy overlay path", async () => {
  const outputRoot = path.join(await mkdtemp(path.join(os.tmpdir(), "v3-overlays-v1-regression-")), "project");
  const manifest = fixtureManifest("16:9");
  delete manifest.version;
  delete manifest.compositions[0].overlay_instances;
  delete manifest.compositions[0].layout_slot_bindings;
  delete manifest.compositions[0].authoritative_content;
  manifest.compositions[0].layout_variant = "balanced_a";
  manifest.compositions[0].overlay_ids = ["standard_caption"];
  await compileProject({manifest, outputRoot});
  const scene = await readFile(path.join(outputRoot, "compositions", "composition_01.html"), "utf8");
  assert.match(scene, /class="hf-overlay hf-overlay-standard_caption clip"/u);
  assert.doesNotMatch(scene, /data-overlay-v2=|data-overlay-host=/u);
});

function fixtureManifest(ratio) {
  const [width, height] = ratio === "16:9" ? [1920, 1080] : [1080, 1920];
  return {
    version: "2.0",
    registry_sha256: getRegistrySha256(),
    duration_ms: 4000,
    output_spec: {ratio, width, height, fps_num: 30, fps_den: 1},
    theme: {palette_id: "midnight_gold", typography_id: "editorial_sans", density: "balanced", motion_energy: "medium", image_fit: "cover"},
    source_video: {path: "media/source.mp4", silent: true},
    source_segments: [{id: "segment_01", source_path: "media/source.mp4", source_start_ms: 0, source_end_ms: 4000, output_start_ms: 0, output_end_ms: 4000}],
    assets: [],
    compositions: [{
      id: "composition_01", scene_id: "scene_01", start_ms: 0, end_ms: 4000,
      layout_id: "speaker_fullscreen", layout_variant: "clean_center",
      overlay_ids: [], overlay_instances: [], animations: [], transition: "hard_cut", asset_ids: [], layout_slot_bindings: [],
      authoritative_content: {headline: {text: "权威标题", source_caption_ids: ["caption_01"]}, highlight: {text: "权威高亮", source_caption_ids: ["caption_01"]}},
    }],
    captions: [{id: "caption_01", start_ms: 0, end_ms: 4000, text: "权威字幕"}],
  };
}

function normalizeElementTree(html) {
  return [...html.matchAll(/<([a-z][a-z0-9-]*)\b/giu)].map((match) => match[1].toLowerCase()).join(">");
}

async function awaitTempRoot() {
  return mkdtemp(path.join(os.tmpdir(), "v3-overlays-invalid-"));
}

function placementFor(componentId) {
  return ({headline_block: "title_safe", standard_caption: "subtitle_safe", info_card: "right_panel", emphasis_caption: "subtitle_safe", chapter_label: "title_safe", lower_third: "lower_third", bullet_list: "left_panel", number_proof: "center", quote_card: "right_panel", step_indicator: "left_panel", product_tag: "right_panel", cta_hold: "center"})[componentId];
}

function overlaps(left, right) {
  return left.x < right.x + right.width && left.x + left.width > right.x && left.y < right.y + right.height && left.y + left.height > right.y;
}

function multiSceneManifest(ratio, components) {
  const manifest = fixtureManifest(ratio);
  const durationMs = components.length * 4000;
  manifest.duration_ms = durationMs;
  manifest.source_video = {...manifest.source_video, duration_ms: durationMs};
  manifest.source_segments = [{id: "segment_01", source_path: "media/source.mp4", source_start_ms: 0, source_end_ms: durationMs, output_start_ms: 0, output_end_ms: durationMs}];
  manifest.captions = [];
  manifest.compositions = components.map((componentId, index) => {
    const startMs = index * 4000; const endMs = startMs + 4000; const id = `composition_${String(index + 1).padStart(2, "0")}`;
    manifest.captions.push({id: `caption_${String(index + 1).padStart(2, "0")}`, start_ms: startMs, end_ms: endMs, text: `权威字幕 ${index + 1}`});
    return {
      id, scene_id: `scene_${String(index + 1).padStart(2, "0")}`, start_ms: startMs, end_ms: endMs,
      layout_id: "speaker_fullscreen", layout_variant: "clean_center", overlay_ids: [componentId],
      overlay_instances: [{instance_id: `overlay_${index + 1}`, component_id: componentId, content_ref: index % 2 ? "highlight" : "headline", placement: placementFor(componentId) ?? "title_safe"}],
      authoritative_content: {headline: {text: `品牌 PRODUCT-X 第 ${index + 1} 条，售价 499 元。`, source_caption_ids: [`caption_${String(index + 1).padStart(2, "0")}`]}, highlight: {text: `Evidence ${index + 1} preserves 42.5% exactly.`, source_caption_ids: [`caption_${String(index + 1).padStart(2, "0")}`]}},
      animations: [], transition: "hard_cut", asset_ids: [], layout_slot_bindings: [],
    };
  });
  return manifest;
}

function assertEverySafeTextHasHydrationTarget(html, componentId) {
  const safeTextCount = [...html.matchAll(/\bdata-safe-text="[^"]+"/gu)].length;
  const hydratableCount = [...html.matchAll(/\bdata-safe-text="[^"]+"[^>]*><span><\/span>/gu)].length;
  assert.ok(safeTextCount >= 1, `${componentId} has no hydratable authoritative text`);
  assert.equal(hydratableCount, safeTextCount, `${componentId} has a safe-text node without its direct hydration span`);
}

function cssClassFor(componentId) {
  return ({headline_block: "headline", standard_caption: "caption", info_card: "info-card", emphasis_caption: "emphasis", chapter_label: "chapter", lower_third: "lower-third", bullet_list: "bullets", number_proof: "number-proof", quote_card: "quote", step_indicator: "steps", product_tag: "product-tag", cta_hold: "cta"})[componentId];
}
