import assert from "node:assert/strict";
import test from "node:test";

import {parseCanonicalJson} from "../src/parse-canonical-json.mjs";
import {validateManifest} from "../src/validate-manifest.mjs";


const canonical = (value) => Buffer.from(JSON.stringify(Object.fromEntries(Object.keys(value).sort().map((key) => [key, value[key]]))));
const validVisualFields = Object.freeze({
  theme_profile_id: "editorial_clean", design_intent: {density: "balanced", motion_energy: "medium", image_fit: "cover", decoration_intensity: "medium"}, variation_seed: "0123456789abcdef",
  design_tokens: {
    "--hf-theme-profile": "editorial_clean", "--hf-bg": "#f7f4ed", "--hf-surface": "#ffffff",
    "--hf-text": "#17212b", "--hf-accent": "#315b8a", "--hf-font": '"Noto Sans SC", sans-serif',
    "--hf-type-scale": "0.960", "--hf-gap": "28px", "--hf-radius": "26px", "--hf-border": "rgba(49,91,138,.28)",
    "--hf-shadow": "0 18px 48px rgba(23,33,43,.14)", "--hf-texture": "none", "--hf-density": "balanced",
    "--hf-motion-distance": "36px", "--hf-image-fit": "cover",
  },
});
const authoritativeContent = Object.freeze({
  headline: Object.freeze({text: "权威标题", source_caption_ids: Object.freeze([])}),
  highlight: Object.freeze({text: "权威高亮", source_caption_ids: Object.freeze([])}),
});


test("strict parser rejects duplicate nonfinite trailing and prototype keys", () => {
  assert.equal(parseCanonicalJson(Buffer.from('{"a":1}'), {}).a, 1);
  for (const raw of [
    '{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}', '{}{}', '{} trailing',
    '```json\n{}\n```', '{"__proto__":{}}', '{"constructor":1}', '{"prototype":1}',
  ]) assert.throws(() => parseCanonicalJson(Buffer.from(raw), {}));
  assert.throws(() => parseCanonicalJson(Buffer.from([0xff]), {}), /json_utf8_invalid/);
  assert.throws(() => parseCanonicalJson(Buffer.from('\uFEFF{}'), {}), /json_bom_forbidden/);
});


test("strict parser enforces byte depth item and string limits", () => {
  assert.throws(() => parseCanonicalJson(Buffer.from(' '.repeat(513 * 1024)), {}), /json_bytes_exceeded/);
  assert.throws(() => parseCanonicalJson(Buffer.from('['.repeat(25) + '0' + ']'.repeat(25)), {}), /json_depth_exceeded/);
  assert.throws(() => parseCanonicalJson(Buffer.from(JSON.stringify({a: "x".repeat(4001)})), {}), /json_string_exceeded/);
  assert.throws(() => parseCanonicalJson(Buffer.from(JSON.stringify({a: Array(5001).fill(0)})), {}), /json_items_exceeded/);
});


test("manifest binds renderer registry schema and silent video", () => {
  const manifest = {
    version: "1.0",
    schema_sha256: "schema",
    registry_sha256: "registry",
    renderer_environment: {renderer_build_id: "build"},
    output_spec: {ratio: "9:16", width: 1080, height: 1920, fps_num: 30, fps_den: 1},
    duration_ms: 4000,
    master_audio: {path: "media/master.wav"},
    source_video: {path: "media/source.mp4", silent: true},
    compositions: [{id: "scene_1", start_ms: 0, end_ms: 4000}],
  };
  const expected = {rendererBuildId: "build", registrySha256: "sha256:registry", schemaSha256: "schema"};
  const valid = validateManifest(manifest, expected);
  assert(Object.isFrozen(valid));
  for (const mutate of [
    (value) => value.renderer_environment.renderer_build_id = "wrong",
    (value) => value.registry_sha256 = "wrong",
    (value) => value.source_video.silent = false,
    (value) => value.output_spec.width = 1920,
  ]) {
    const changed = structuredClone(manifest); mutate(changed);
    assert.throws(() => validateManifest(changed, expected));
  }
});

test("manifest schema identity dispatches v1 and v2 and rejects unknown versions", () => {
  const base = {
    registry_sha256: "registry", renderer_environment: {renderer_build_id: "build"},
    output_spec: {ratio: "9:16", width: 1080, height: 1920, fps_num: 30, fps_den: 1},
    duration_ms: 4000, master_audio: {path: "media/master.wav"}, source_video: null,
    compositions: [{id: "scene_1", start_ms: 0, end_ms: 4000}],
  };
  const expected = {rendererBuildId: "build", registrySha256: "sha256:registry", schemaSha256ByVersion: {"1.0": "schema-v1", "2.0": "schema-v2"}};
  for (const [version, schema] of [["1.0", "schema-v1"], ["2.0", "schema-v2"]]) {
    const compositions = version === "2.0" ? [{...base.compositions[0], overlay_ids: [], overlay_instances: [], authoritative_content: authoritativeContent}] : base.compositions;
    assert.equal(validateManifest({...base, ...(version === "2.0" ? validVisualFields : {}), compositions, version, schema_sha256: schema}, expected).version, version);
  }
  assert.throws(() => validateManifest({...base, version: "3.0", schema_sha256: "schema-v3"}, expected), /manifest_version_invalid/);
  assert.throws(() => validateManifest({...base, version: "2.0", schema_sha256: "schema-v1"}, expected), /manifest_schema_mismatch/);
});

test("v2 manifest rejects an overlay id projection that disagrees with instances", () => {
  const base = {
    version: "2.0", schema_sha256: "schema-v2", registry_sha256: "registry", renderer_environment: {renderer_build_id: "build"},
    output_spec: {ratio: "9:16", width: 1080, height: 1920, fps_num: 30, fps_den: 1}, duration_ms: 4000,
    master_audio: {path: "media/master.wav"}, source_video: null,
    compositions: [{id: "scene_1", start_ms: 0, end_ms: 4000, overlay_ids: ["headline_block"], overlay_instances: [{instance_id: "headline_01", component_id: "info_card"}]}],
  };
  const expected = {rendererBuildId: "build", registrySha256: "sha256:registry", schemaSha256ByVersion: {"2.0": "schema-v2"}};
  assert.throws(() => validateManifest(base, expected), /manifest_component_projection_invalid/);
});

test("v2 manifest rejects design tokens that do not equal the frozen profile resolution", () => {
  const manifest = {
    version: "2.0", schema_sha256: "schema-v2", registry_sha256: "registry", renderer_environment: {renderer_build_id: "build"},
    output_spec: {ratio: "9:16", width: 1080, height: 1920, fps_num: 30, fps_den: 1}, duration_ms: 4000,
    master_audio: {path: "media/master.wav"}, source_video: null,
    theme_profile_id: "editorial_clean", design_intent: {density: "balanced", motion_energy: "medium", image_fit: "cover", decoration_intensity: "medium"}, variation_seed: "0123456789abcdef",
    design_tokens: {"--hf-bg": "#untrusted"},
    compositions: [{id: "scene_1", start_ms: 0, end_ms: 4000, overlay_ids: [], overlay_instances: [], authoritative_content: authoritativeContent}],
  };
  const expected = {rendererBuildId: "build", registrySha256: "sha256:registry", schemaSha256ByVersion: {"2.0": "schema-v2"}};
  assert.throws(() => validateManifest(manifest, expected), /manifest_design_tokens_mismatch/);
});

test("v2 manifest compares frozen design tokens by value after canonical key ordering", () => {
  const manifest = {
    version: "2.0", schema_sha256: "schema-v2", registry_sha256: "registry", renderer_environment: {renderer_build_id: "build"},
    output_spec: {ratio: "9:16", width: 1080, height: 1920, fps_num: 30, fps_den: 1}, duration_ms: 4000,
    master_audio: {path: "media/master.wav"}, source_video: null,
    ...validVisualFields, design_tokens: Object.fromEntries(Object.entries(validVisualFields.design_tokens).sort(([left], [right]) => left.localeCompare(right))),
    compositions: [{id: "scene_1", start_ms: 0, end_ms: 4000, overlay_ids: [], overlay_instances: [], authoritative_content: authoritativeContent}],
  };
  const expected = {rendererBuildId: "build", registrySha256: "sha256:registry", schemaSha256ByVersion: {"2.0": "schema-v2"}};
  assert.equal(validateManifest(manifest, expected).variation_seed, "0123456789abcdef");
});

test("strict parser v2 manifest accepts Python JSON tokens with null prototypes", () => {
  const manifest = {
    version: "2.0", schema_sha256: "schema-v2", registry_sha256: "registry", renderer_environment: {renderer_build_id: "build"},
    output_spec: {ratio: "9:16", width: 1080, height: 1920, fps_num: 30, fps_den: 1}, duration_ms: 4000,
    master_audio: {path: "media/master.wav"}, source_video: null, ...validVisualFields,
    compositions: [{id: "scene_1", start_ms: 0, end_ms: 4000, overlay_ids: [], overlay_instances: [], authoritative_content: authoritativeContent}],
  };
  const parsed = parseCanonicalJson(Buffer.from(JSON.stringify(manifest)));
  const expected = {rendererBuildId: "build", registrySha256: "sha256:registry", schemaSha256ByVersion: {"2.0": "schema-v2"}};
  assert.equal(validateManifest(parsed, expected).theme_profile_id, "editorial_clean");
});

test("strict parser validates frozen overlay facts and rejects executable or unknown projection fields", () => {
  const expected = {rendererBuildId: "build", registrySha256: "sha256:registry", schemaSha256ByVersion: {"2.0": "schema-v2"}};
  const manifest = {
    version: "2.0", schema_sha256: "schema-v2", registry_sha256: "registry", renderer_environment: {renderer_build_id: "build"},
    output_spec: {ratio: "9:16", width: 1080, height: 1920, fps_num: 30, fps_den: 1}, duration_ms: 4000,
    master_audio: {path: "media/master.wav"}, source_video: null, ...validVisualFields,
    compositions: [{
      id: "scene_1", start_ms: 0, end_ms: 4000, layout_id: "speaker_fullscreen",
      overlay_ids: ["headline_block"], overlay_instances: [{instance_id: "headline_01", component_id: "headline_block", content_ref: "headline", placement: "title_safe"}],
      authoritative_content: {headline: {text: "品牌 PRODUCT-X 499 元", source_caption_ids: ["caption_01"]}, highlight: {text: "证据 42.5%", source_caption_ids: ["caption_02"]}},
    }],
  };
  assert.equal(validateManifest(parseCanonicalJson(Buffer.from(JSON.stringify(manifest))), expected).compositions[0].authoritative_content.headline.text, "品牌 PRODUCT-X 499 元");
  for (const mutate of [
    (value) => value.compositions[0].overlay_instances[0].content_ref = "invented",
    (value) => value.compositions[0].overlay_instances[0].placement = "floating",
    (value) => value.compositions[0].authoritative_content.headline.source_caption_ids = ["caption_01", "caption_01"],
    (value) => value.compositions[0].overlay_instances[0].html = "<b>unsafe</b>",
    (value) => value.compositions[0].authoritative_content.headline.url = "https://invalid.example",
    (value) => value.compositions[0].authoritative_content.headline.extra = "not allowed",
  ]) {
    const changed = structuredClone(manifest); mutate(changed);
    assert.throws(() => validateManifest(parseCanonicalJson(Buffer.from(JSON.stringify(changed))), expected), /manifest_(?:overlay|executable)/);
  }
});

test("v2 manifest rejects component placements outside the frozen semantic matrix", () => {
  const expected = {rendererBuildId: "build", registrySha256: "sha256:registry", schemaSha256ByVersion: {"2.0": "schema-v2"}};
  const manifest = {
    version: "2.0", schema_sha256: "schema-v2", registry_sha256: "registry", renderer_environment: {renderer_build_id: "build"},
    output_spec: {ratio: "16:9", width: 1920, height: 1080, fps_num: 30, fps_den: 1}, duration_ms: 4000,
    master_audio: {path: "media/master.wav"}, source_video: null, ...validVisualFields,
    compositions: [{
      id: "scene_1", start_ms: 0, end_ms: 4000, layout_id: "speaker_fullscreen",
      overlay_ids: ["info_card"], overlay_instances: [{instance_id: "info_01", component_id: "info_card", content_ref: "highlight", placement: "title_safe"}],
      authoritative_content: authoritativeContent,
    }],
  };
  assert.throws(() => validateManifest(parseCanonicalJson(Buffer.from(JSON.stringify(manifest))), expected), /manifest_overlay_placement_invalid/);
});

test("v2 manifest rejects extra executable design intent fields before token resolution", () => {
  const manifest = {
    version: "2.0", schema_sha256: "schema-v2", registry_sha256: "registry", renderer_environment: {renderer_build_id: "build"},
    output_spec: {ratio: "9:16", width: 1080, height: 1920, fps_num: 30, fps_den: 1}, duration_ms: 4000,
    master_audio: {path: "media/master.wav"}, source_video: null, ...validVisualFields,
    design_intent: {...validVisualFields.design_intent, font_url: "https://fonts.invalid/font.woff2"},
    compositions: [{id: "scene_1", start_ms: 0, end_ms: 4000, overlay_ids: [], overlay_instances: [], authoritative_content: authoritativeContent}],
  };
  const expected = {rendererBuildId: "build", registrySha256: "sha256:registry", schemaSha256ByVersion: {"2.0": "schema-v2"}};
  assert.throws(() => validateManifest(parseCanonicalJson(Buffer.from(JSON.stringify(manifest))), expected), /manifest_design_intent_invalid/);
});

test("v2 manifest rejects URL external-font and animation token values", () => {
  const expected = {rendererBuildId: "build", registrySha256: "sha256:registry", schemaSha256ByVersion: {"2.0": "schema-v2"}};
  for (const poisoned of ["https://fonts.invalid/font.woff2", "url(https://fonts.invalid/font.woff2)", "@font-face", "animation: spin 1s"]) {
    const manifest = {
      version: "2.0", schema_sha256: "schema-v2", registry_sha256: "registry", renderer_environment: {renderer_build_id: "build"},
      output_spec: {ratio: "9:16", width: 1080, height: 1920, fps_num: 30, fps_den: 1}, duration_ms: 4000,
      master_audio: {path: "media/master.wav"}, source_video: null, ...validVisualFields,
      design_tokens: {...validVisualFields.design_tokens, "--hf-font": poisoned},
      compositions: [{id: "scene_1", start_ms: 0, end_ms: 4000, overlay_ids: [], overlay_instances: [], authoritative_content: authoritativeContent}],
    };
    assert.throws(() => validateManifest(parseCanonicalJson(Buffer.from(JSON.stringify(manifest))), expected), /manifest_visual_value_forbidden/);
  }
});
