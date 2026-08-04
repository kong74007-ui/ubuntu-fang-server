import {createHash} from "node:crypto";
import {readdirSync, readFileSync} from "node:fs";
import path from "node:path";
import {fileURLToPath} from "node:url";

const REGISTRY_ROOT = path.dirname(fileURLToPath(import.meta.url));

/** A sorted, content-addressed inventory of every registry implementation module. */
export function getRegistrySourceManifest(root = REGISTRY_ROOT) {
  return Object.freeze(walk(root).map((file) => ({file, path: path.relative(root, file).replaceAll("\\", "/")}))
    .sort((left, right) => left.path < right.path ? -1 : left.path > right.path ? 1 : 0)
    .map(({file, path: relativePath}) => Object.freeze({
      path: relativePath,
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
      return entry.isFile() && (entry.name.endsWith(".mjs") || entry.name.endsWith(".json")) ? [child] : [];
    })
    .sort((left, right) => left < right ? -1 : left > right ? 1 : 0);
}
