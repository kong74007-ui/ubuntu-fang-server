import {createHash} from "node:crypto";
import {readFileSync} from "node:fs";

function digest(relativePath) {
  return createHash("sha256").update(readFileSync(new URL(relativePath, import.meta.url))).digest("hex");
}

export const MANIFEST_SCHEMA_SHA256_BY_VERSION = Object.freeze({
  "1.0": digest("../../content_domains/ai_edit_v3/schemas/render-manifest-v1.schema.json"),
  "2.0": digest("../../content_domains/ai_edit_v3/schemas/render-manifest-v2.schema.json"),
});
