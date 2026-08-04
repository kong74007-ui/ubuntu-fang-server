import assert from "node:assert/strict";
import test from "node:test";

import {parseCanonicalJson} from "../src/parse-canonical-json.mjs";
import {validateManifest} from "../src/validate-manifest.mjs";


const canonical = (value) => Buffer.from(JSON.stringify(Object.fromEntries(Object.keys(value).sort().map((key) => [key, value[key]]))));


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
    assert.equal(validateManifest({...base, version, schema_sha256: schema}, expected).version, version);
  }
  assert.throws(() => validateManifest({...base, version: "3.0", schema_sha256: "schema-v3"}, expected), /manifest_version_invalid/);
  assert.throws(() => validateManifest({...base, version: "2.0", schema_sha256: "schema-v1"}, expected), /manifest_schema_mismatch/);
});
