const FORBIDDEN = new Set(["html", "css", "javascript", "script", "expression", "plugin", "url", "output_path", "command", "env", "systemd_property"]);


function inspect(value) {
  if (Array.isArray(value)) { for (const item of value) inspect(item); return; }
  if (!value || typeof value !== "object") return;
  for (const [key, child] of Object.entries(value)) {
    if (FORBIDDEN.has(key.toLowerCase())) throw new Error("manifest_executable_field_forbidden");
    inspect(child);
  }
}


function deepCopyFreeze(value) {
  if (Array.isArray(value)) return Object.freeze(value.map(deepCopyFreeze));
  if (value && typeof value === "object") {
    const result = Object.create(null);
    for (const key of Object.keys(value)) result[key] = deepCopyFreeze(value[key]);
    return Object.freeze(result);
  }
  return value;
}


export function validateManifest(document, expected) {
  if (!document || typeof document !== "object" || Array.isArray(document)) throw new Error("manifest_invalid");
  inspect(document);
  const schemaByVersion = expected.schemaSha256ByVersion ?? {"1.0": expected.schemaSha256};
  if (typeof document.version !== "string" || typeof schemaByVersion[document.version] !== "string") throw new Error("manifest_version_invalid");
  if (document.renderer_environment?.renderer_build_id !== expected.rendererBuildId) throw new Error("manifest_renderer_build_mismatch");
  if (`sha256:${document.registry_sha256}` !== expected.registrySha256) throw new Error("manifest_registry_mismatch");
  if (document.schema_sha256 !== schemaByVersion[document.version]) throw new Error("manifest_schema_mismatch");
  const output = document.output_spec;
  if (!output || output.fps_num !== 30 || output.fps_den !== 1) throw new Error("manifest_output_invalid");
  const dimensions = output.ratio === "16:9" ? [1920, 1080] : output.ratio === "9:16" ? [1080, 1920] : null;
  if (!dimensions || output.width !== dimensions[0] || output.height !== dimensions[1]) throw new Error("manifest_output_invalid");
  if (document.source_video !== null && document.source_video?.silent !== true) throw new Error("manifest_source_audio_forbidden");
  if (!Number.isInteger(document.duration_ms) || document.duration_ms < 1) throw new Error("manifest_duration_invalid");
  if (!Array.isArray(document.compositions) || document.compositions.length < 1) throw new Error("manifest_compositions_invalid");
  const ids = new Set();
  let cursor = 0;
  for (const composition of document.compositions) {
    if (!composition || typeof composition.id !== "string" || ids.has(composition.id)) throw new Error("manifest_composition_id_invalid");
    ids.add(composition.id);
    if (composition.start_ms !== cursor || !Number.isInteger(composition.end_ms) || composition.end_ms <= cursor) throw new Error("manifest_composition_timeline_invalid");
    cursor = composition.end_ms;
    if (document.version === "2.0") {
      const instances = composition.overlay_instances;
      if (!Array.isArray(composition.overlay_ids) || !Array.isArray(instances)
        || JSON.stringify(composition.overlay_ids) !== JSON.stringify(instances.map((item) => item?.component_id))) {
        throw new Error("manifest_component_projection_invalid");
      }
    }
  }
  if (cursor !== document.duration_ms) throw new Error("manifest_composition_timeline_invalid");
  return deepCopyFreeze(document);
}
