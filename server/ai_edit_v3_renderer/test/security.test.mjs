import assert from "node:assert/strict";
import test from "node:test";
import {validateManifest} from "../src/validate-manifest.mjs";
import {readFile} from "node:fs/promises";


test("executable and provider fields are rejected recursively", () => {
  const base = {
    schema_sha256: "schema", registry_sha256: "registry",
    renderer_environment: {renderer_build_id: "build"},
    output_spec: {ratio: "16:9", width: 1920, height: 1080, fps_num: 30, fps_den: 1},
    duration_ms: 1000, master_audio: null, source_video: null,
    compositions: [{id: "scene", start_ms: 0, end_ms: 1000}],
  };
  for (const key of ["html", "css", "javascript", "script", "expression", "plugin", "url", "output_path", "command", "env", "systemd_property"]) {
    const value = structuredClone(base); value.compositions[0][key] = "bad";
    assert.throws(() => validateManifest(value, {rendererBuildId: "build", registrySha256: "registry", schemaSha256: "schema"}), /manifest_executable_field_forbidden/);
  }
});

test("production renderer has fixed request input and output paths with no provider authority", async () => {
  const source = await readFile(new URL("../src/render.mjs", import.meta.url), "utf8");
  assert.match(source, /--request/);
  assert.match(source, /--input-root/);
  assert.match(source, /--output-root/);
  assert.doesNotMatch(source, /ELEVENLABS_API_KEY|DASHSCOPE_API_KEY|Authorization|xi-api-key/);
  assert.doesNotMatch(source, /eval\(|new Function|shell:\s*true/);
});
