import assert from "node:assert/strict";
import test from "node:test";

import {compilePrimitiveLayout} from "../src/registry/layout-primitives.mjs";
import {compileLayout, getLayoutContract, LAYOUT_CONTRACTS} from "../src/registry/layouts.mjs";

const RATIOS = ["16:9", "9:16"];
const VARIANTS = ["balanced_a", "emphasis_b"];

test("all 48 layout variant ratio combinations have safe bounded geometry", () => {
  assert.equal(LAYOUT_CONTRACTS.length, 12);
  for (const contract of LAYOUT_CONTRACTS) {
    assert.deepEqual(contract.variants, VARIANTS);
    assert.deepEqual(contract.supportedRatios, RATIOS);
    for (const ratio of RATIOS) for (const variantId of VARIANTS) {
      const resolved = getLayoutContract(contract.id, variantId, ratio);
      const {width, height, boxes} = resolved.geometry;
      assert.deepEqual([width, height], ratio === "16:9" ? [1920, 1080] : [1080, 1920]);
      for (const box of Object.values(boxes)) {
        assert(box.x >= 0 && box.y >= 0 && box.width > 0 && box.height > 0);
        assert(box.x + box.width <= width && box.y + box.height <= height);
      }
      if (boxes.face_critical && boxes.text_safe) assert.equal(overlaps(boxes.face_critical, boxes.text_safe), false);
      if (boxes.product_critical && boxes.text_safe) assert.equal(overlaps(boxes.product_critical, boxes.text_safe), false);
      const html = compileLayout({
        layoutId: contract.id, variantId, ratio, idPrefix: `case_${contract.id}`,
        durationMs: 3000, scene: {}, assets: [], overlays: "<div>overlay</div>", hasVideo: false,
      });
      assert.match(html, new RegExp(`data-layout-id="${contract.id}"`));
      assert.match(html, new RegExp(`data-layout-variant="${variantId}"`));
      assert.match(html, /data-fallback="no_optional_media"/);
    }
  }
});

test("layout compiler distinguishes no, one and multiple optional media slots", () => {
  const base = {layoutId: "editorial_collage", variantId: "emphasis_b", ratio: "9:16", idPrefix: "collage", durationMs: 3000, scene: {}, overlays: "", hasVideo: false};
  assert.match(compileLayout({...base, assets: []}), /data-fallback="no_optional_media"/);
  const one = compileLayout({...base, assets: [{id: "one", kind: "image", relativePath: "media/one.png"}]});
  assert.equal((one.match(/class="hf-asset/g) ?? []).length, 1);
  assert.match(one, /class="hf-materials hf-material-count-1"/);
  assert.equal((compileLayout({...base, assets: [
    {id: "one", kind: "image", relativePath: "media/one.png"},
    {id: "two", kind: "image", relativePath: "media/two.png"},
    {id: "three", kind: "image", relativePath: "media/three.png"},
  ]}).match(/class="hf-asset/g) ?? []).length, 3);
});

test("layout compilers never expose internal placeholder copy", () => {
  const compiled = [
    compileLayout({
      layoutId: "speaker_left_info_right", variantId: "balanced_a", ratio: "9:16",
      idPrefix: "talking_head", durationMs: 3000, scene: {}, assets: [], overlays: "", hasVideo: true,
    }),
    compilePrimitiveLayout({idPrefix: "primitive", durationMs: 3000, hasVideo: true}),
    compilePrimitiveLayout({idPrefix: "faceless", durationMs: 3000, hasVideo: false}),
  ].join("\n");

  for (const forbidden of ["主体画面", "主体视频", "智能剪辑画面", "AI 视觉节奏"]) {
    assert.equal(compiled.includes(forbidden), false, forbidden);
  }
  assert.match(compiled, /class="hf-fallback clip"/);
  assert.match(compiled, /class="hf-speaker-zone clip"/);
  assert.match(compiled, /class="hf-media clip/);
});

function overlaps(a, b) {
  return a.x < b.x + b.width && a.x + a.width > b.x && a.y < b.y + b.height && a.y + a.height > b.y;
}
