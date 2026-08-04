import {createHash} from "node:crypto";
import {readdirSync, readFileSync} from "node:fs";
import path from "node:path";
import {fileURLToPath} from "node:url";

const REGISTRY_ROOT = path.dirname(fileURLToPath(import.meta.url));

/** A sorted, content-addressed inventory of every registry implementation module. */
export function getRegistrySourceManifest(root = REGISTRY_ROOT) {
  return Object.freeze(walk(root).map((file) => Object.freeze({
    path: path.relative(root, file).replaceAll("\\", "/"),
    sha256: createHash("sha256").update(readFileSync(file)).digest("hex"),
  })));
}

export function getRegistrySourceSha256(root = REGISTRY_ROOT) {
  return createHash("sha256").update(JSON.stringify(getRegistrySourceManifest(root))).digest("hex");
}

function walk(directory) {
  return readdirSync(directory, {withFileTypes: true})
    .flatMap((entry) => {
      const child = path.join(directory, entry.name);
      if (entry.isDirectory()) return walk(child);
      return entry.isFile() && entry.name.endsWith(".mjs") ? [child] : [];
    })
    .sort((left, right) => left.localeCompare(right));
}
