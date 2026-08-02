import {createHash} from "node:crypto";
import {constants} from "node:fs";
import {lstat, open, realpath} from "node:fs/promises";
import {isAbsolute, relative, resolve, sep} from "node:path";


function declarationPath(value) {
  if (typeof value !== "string" || !value || value.includes("\0") || value.includes("\\") || isAbsolute(value) || /^[a-zA-Z]+:/.test(value)) throw new Error("render_input_path_invalid");
  const parts = value.split("/");
  if (parts.some((part) => !part || part === "." || part === "..")) throw new Error("render_input_path_invalid");
  return value;
}


function declarations(manifest) {
  return [manifest.source_video, manifest.master_audio, ...(manifest.assets || [])].filter(Boolean);
}


export async function verifyInputFiles({manifest, inputRoot}) {
  const root = await realpath(inputRoot);
  const unique = new Map();
  const folded = new Set();
  for (const declaration of declarations(manifest)) {
    const path = declarationPath(declaration.path);
    const caseKey = path.toLowerCase();
    if (folded.has(caseKey) && !unique.has(path)) throw new Error("render_input_case_collision");
    folded.add(caseKey);
    if (unique.has(path)) {
      const prior = unique.get(path);
      if (prior.sha256 !== declaration.sha256 || prior.size_bytes !== declaration.size_bytes) throw new Error("render_input_declaration_conflict");
    } else unique.set(path, declaration);
  }
  const verified = [];
  try {
    for (const [relativePath, declaration] of unique) {
      const candidate = resolve(root, relativePath);
      const contained = relative(root, candidate);
      if (contained.startsWith(`..${sep}`) || contained === ".." || isAbsolute(contained)) throw new Error("render_input_path_invalid");
      const before = await lstat(candidate);
      if (before.isSymbolicLink()) throw new Error("render_input_symlink");
      if (!before.isFile()) throw new Error("render_input_not_regular");
      if (before.nlink !== 1) throw new Error("render_input_hardlink");
      const resolved = await realpath(candidate);
      const realContained = relative(root, resolved);
      if (realContained.startsWith(`..${sep}`) || realContained === ".." || isAbsolute(realContained)) throw new Error("render_input_path_invalid");
      const flags = constants.O_RDONLY | (constants.O_NOFOLLOW || 0);
      const handle = await open(candidate, flags);
      let keep = false;
      try {
        const after = await handle.stat();
        if (!after.isFile() || after.nlink !== 1 || after.size !== declaration.size_bytes || before.dev !== after.dev || before.ino !== after.ino) throw new Error("render_input_identity_mismatch");
        const bytes = await handle.readFile();
        const sha256 = createHash("sha256").update(bytes).digest("hex");
        if (sha256 !== declaration.sha256) throw new Error("render_input_hash_mismatch");
        verified.push(Object.freeze({relativePath, realPath: resolved, size: after.size, sha256, mode: after.mode, nlink: after.nlink, handle}));
        keep = true;
      } finally {
        if (!keep) await handle.close();
      }
    }
    return Object.freeze(verified);
  } catch (error) {
    await Promise.allSettled(verified.map((item) => item.handle.close()));
    throw error;
  }
}
