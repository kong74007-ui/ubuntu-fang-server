import assert from "node:assert/strict";
import test from "node:test";
import {validateManifest} from "../src/validate-manifest.mjs";


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
