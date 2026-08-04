import {readFile, writeFile} from "node:fs/promises";
import {fileURLToPath} from "node:url";

import {getRegistrySha256} from "./registry/index.mjs";

export async function writeRegistryHash(destination = new URL("../registry-sha256.txt", import.meta.url)) {
  const hash = `${getRegistrySha256()}\n`;
  await writeFile(destination, hash, "utf8");
  return hash;
}

export async function checkRegistryHash(destination = new URL("../registry-sha256.txt", import.meta.url)) {
  const expected = `${getRegistrySha256()}\n`;
  let actual = "";
  try {
    actual = await readFile(destination, "utf8");
  } catch (error) {
    if (error?.code === "ENOENT") throw new Error("registry_hash_missing");
    throw error;
  }
  if (actual !== expected) throw new Error("registry_hash_drift");
  return expected;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  if (process.argv.includes("--check")) process.stdout.write(await checkRegistryHash());
  else process.stdout.write(await writeRegistryHash());
}
