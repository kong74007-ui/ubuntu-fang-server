import {readFile, writeFile} from "node:fs/promises";
import {fileURLToPath} from "node:url";

import {visualCapabilitiesBytes} from "./registry/visual-capabilities.mjs";


const DEFAULT_DESTINATION = new URL("./registry/visual-capabilities-v1.json", import.meta.url);


export async function writeVisualCapabilities(destination = DEFAULT_DESTINATION) {
  const expected = visualCapabilitiesBytes();
  await writeFile(destination, expected, "utf8");
  return expected;
}


export async function checkVisualCapabilities(destination = DEFAULT_DESTINATION) {
  const expected = visualCapabilitiesBytes();
  let actual = "";
  try {
    actual = await readFile(destination, "utf8");
  } catch (error) {
    if (error?.code === "ENOENT") throw new Error("visual_capabilities_missing");
    throw error;
  }
  if (actual !== expected) throw new Error("visual_capabilities_drift");
  return expected;
}


if (process.argv[1] === fileURLToPath(import.meta.url)) {
  if (process.argv.includes("--check")) process.stdout.write(await checkVisualCapabilities());
  else process.stdout.write(await writeVisualCapabilities());
}
