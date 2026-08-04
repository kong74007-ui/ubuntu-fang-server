import {writeFile} from "node:fs/promises";
import {fileURLToPath} from "node:url";

import {getRegistrySha256} from "./registry/index.mjs";

export async function writeRegistryHash(destination = new URL("../registry-sha256.txt", import.meta.url)) {
  const hash = `${getRegistrySha256()}\n`;
  await writeFile(destination, hash, "utf8");
  return hash;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) process.stdout.write(await writeRegistryHash());
