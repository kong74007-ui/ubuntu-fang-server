import {createHash} from "node:crypto";
import {mkdir, open, readFile, writeFile} from "node:fs/promises";
import path from "node:path";
import {fileURLToPath} from "node:url";

import {compileProject} from "./compile-project.mjs";
import {parseCanonicalJson} from "./parse-canonical-json.mjs";
import {getRegistrySha256} from "./registry/index.mjs";
import {validateRendererRelease} from "./release-manifest.mjs";
import {buildRenderReport, canonicalReportBytes} from "./report.mjs";
import {renderHyperframes} from "./render-hyperframes.mjs";
import {verifyInputFiles} from "./validate-files.mjs";
import {validateManifest} from "./validate-manifest.mjs";

const MODULE_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const SHA256 = /^[0-9a-f]{64}$/;
const BUILD_ID = /^sha256:[0-9a-f]{64}$/;

function hash(bytes) { return createHash("sha256").update(bytes).digest("hex"); }

export function selectManifestCompiler(version) {
  if (version === "1.0") return "legacy";
  if (version === "2.0") return "component";
  throw new Error("render_manifest_version_unknown");
}

function absolute(value, code) {
  if (typeof value !== "string" || !path.isAbsolute(value) || value.includes("\0") || value.includes("://")) throw new Error(code);
  return path.resolve(value);
}

function validateRequest(request) {
  const keys = Object.keys(request).sort();
  const expected = ["manifest_path", "manifest_sha256", "registry_sha256", "renderer_build_id", "schema_sha256", "version"];
  if (JSON.stringify(keys) !== JSON.stringify(expected)) throw new Error("render_request_fields_invalid");
  if (request.version !== "1.0" || request.manifest_path !== "render-manifest.json") throw new Error("render_request_version_invalid");
  if (!SHA256.test(request.manifest_sha256) || !SHA256.test(request.schema_sha256)) throw new Error("render_request_sha_invalid");
  if (!BUILD_ID.test(request.renderer_build_id) || !BUILD_ID.test(request.registry_sha256)) throw new Error("render_request_build_invalid");
  return request;
}

async function copyVerifiedFile(item, destinationRoot) {
  const destination = path.resolve(destinationRoot, item.relativePath);
  const relative = path.relative(destinationRoot, destination);
  if (relative.startsWith(`..${path.sep}`) || relative === ".." || path.isAbsolute(relative)) throw new Error("render_copy_path_invalid");
  await mkdir(path.dirname(destination), {recursive: true});
  const output = await open(destination, "wx", 0o400);
  try {
    const buffer = Buffer.allocUnsafe(Math.min(4 * 1024 * 1024, Math.max(1, item.size)));
    let offset = 0;
    while (offset < item.size) {
      const length = Math.min(buffer.length, item.size - offset);
      const {bytesRead} = await item.handle.read(buffer, 0, length, offset);
      if (bytesRead < 1) throw new Error("render_copy_short_read");
      await output.write(buffer, 0, bytesRead, offset);
      offset += bytesRead;
    }
    await output.sync();
  } finally {
    await output.close();
  }
}

export async function runRenderRequest({requestPath, inputRoot, outputRoot, chromiumPath, environment = process.env, commandRunner}) {
  const requestFile = absolute(requestPath, "render_request_path_invalid");
  const inputs = absolute(inputRoot, "render_input_root_invalid");
  const outputs = absolute(outputRoot, "render_output_root_invalid");
  const requestBytes = await readFile(requestFile);
  const request = validateRequest(parseCanonicalJson(requestBytes, {maxBytes: 16 * 1024, maxDepth: 4, maxItems: 32, maxStringChars: 256}));
  const release = validateRendererRelease(parseCanonicalJson(await readFile(path.join(MODULE_ROOT, "renderer-release.lock.json")), {maxBytes: 64 * 1024, maxDepth: 8, maxItems: 256, maxStringChars: 1024}));
  const registrySha256 = getRegistrySha256();
  if (request.renderer_build_id !== release.renderer_build_id) throw new Error("render_request_release_mismatch");
  if (request.registry_sha256 !== registrySha256) throw new Error("render_request_registry_mismatch");
  const manifestPath = path.join(inputs, request.manifest_path);
  const manifestBytes = await readFile(manifestPath);
  if (hash(manifestBytes) !== request.manifest_sha256) throw new Error("render_manifest_hash_mismatch");
  const manifest = validateManifest(parseCanonicalJson(manifestBytes, {maxBytes: 512 * 1024, maxDepth: 24, maxItems: 5000, maxStringChars: 4000}), {
    rendererBuildId: release.renderer_build_id,
    registrySha256,
    schemaSha256: request.schema_sha256,
    schemaSha256ByVersion: {"1.0": request.schema_sha256, "2.0": "de674b53f0864bdeca3192e96d0fe05d8364ba4761341bf158efb1df2bd907fd"},
  });
  selectManifestCompiler(manifest.version);
  const verifiedFiles = await verifyInputFiles({manifest, inputRoot: inputs});
  try {
    await mkdir(outputs, {recursive: true});
    const projectRoot = path.join(outputs, "project");
    const compiledProject = await compileProject({manifest, outputRoot: projectRoot});
    await Promise.all(verifiedFiles.map((item) => copyVerifiedFile(item, projectRoot)));
    const outputPath = path.join(outputs, "silent.mp4");
    const execution = await renderHyperframes({
      projectRoot, outputPath,
      chromiumPath: chromiumPath ?? environment.PUPPETEER_EXECUTABLE_PATH,
      timeoutMs: 2_600_000, environment, commandRunner,
    });
    const report = await buildRenderReport({
      manifest, verifiedFiles, compiledProject, execution,
      snapshots: execution.snapshots, outputPath,
    });
    return Object.freeze({outputPath, reportPath: outputPath.replace(/\.[^.]+$/, ".report.json"), report});
  } finally {
    await Promise.allSettled(verifiedFiles.map((item) => item.handle.close()));
  }
}

function parseArguments(argv) {
  if (argv.length !== 6) throw new Error("render_arguments_invalid");
  const result = {};
  for (let index = 0; index < argv.length; index += 2) {
    const key = argv[index];
    if (!["--request", "--input-root", "--output-root"].includes(key) || result[key]) throw new Error("render_arguments_invalid");
    result[key] = argv[index + 1];
  }
  if (Object.keys(result).length !== 3) throw new Error("render_arguments_invalid");
  return result;
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  try {
    const args = parseArguments(process.argv.slice(2));
    const result = await runRenderRequest({requestPath: args["--request"], inputRoot: args["--input-root"], outputRoot: args["--output-root"]});
    process.stdout.write(Buffer.concat([canonicalReportBytes({status: "done", output: result.outputPath, report: result.reportPath}), Buffer.from("\n")]));
  } catch (error) {
    process.stderr.write(`${error instanceof Error && /^[a-z0-9_]+$/.test(error.message) ? error.message : "render_failed"}\n`);
    process.exitCode = 1;
  }
}
