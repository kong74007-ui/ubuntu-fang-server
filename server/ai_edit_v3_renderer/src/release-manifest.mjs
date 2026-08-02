import {createHash} from "node:crypto";
import {closeSync, fsyncSync, openSync} from "node:fs";
import {readdir, readFile, rename, stat, writeFile} from "node:fs/promises";
import {basename, dirname, relative, resolve} from "node:path";
import {spawn} from "node:child_process";
import {fileURLToPath} from "node:url";


const SHA256 = /^[0-9a-f]{64}$/;
const BUILD_ID = /^sha256:[0-9a-f]{64}$/;


function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.keys(value).sort().map((key) => [key, canonical(value[key])]));
  }
  return value;
}


export function canonicalReleaseBytes(release) {
  return Buffer.from(JSON.stringify(canonical(release)), "utf8");
}


export function computeRendererBuildId(release) {
  const input = {...release};
  delete input.renderer_build_id;
  delete input.release_archive_sha256;
  return `sha256:${createHash("sha256").update(canonicalReleaseBytes(input)).digest("hex")}`;
}


async function hashFile(path) {
  const bytes = await readFile(path);
  return createHash("sha256").update(bytes).digest("hex");
}


async function commandVersion(executable, args = ["--version"]) {
  return await new Promise((accept, reject) => {
    const child = spawn(executable, args, {shell: false, windowsHide: true, stdio: ["ignore", "pipe", "pipe"]});
    const stdout = [];
    const stderr = [];
    child.stdout.on("data", (chunk) => stdout.push(chunk));
    child.stderr.on("data", (chunk) => stderr.push(chunk));
    child.once("error", reject);
    child.once("close", (code) => {
      if (code !== 0) return reject(new Error("renderer_version_probe_failed"));
      const text = Buffer.concat([...stdout, ...stderr]).toString("utf8").split(/\r?\n/, 1)[0].trim();
      if (!text) return reject(new Error("renderer_version_probe_empty"));
      accept(text);
    });
  });
}


function assertSha(value, code) {
  if (typeof value !== "string" || !SHA256.test(value)) throw new Error(code);
}


export function validateRendererRelease(release) {
  if (!release || typeof release !== "object" || Array.isArray(release)) throw new Error("renderer_release_invalid");
  if (release.schema_version !== 1) throw new Error("renderer_schema_version_invalid");
  if (typeof release.git_commit !== "string" || !/^[0-9a-f]{40}$/.test(release.git_commit)) throw new Error("renderer_git_commit_invalid");
  assertSha(release.package_lock_sha256, "renderer_package_lock_invalid");
  if (!BUILD_ID.test(release.renderer_build_id || "")) throw new Error("renderer_build_id_invalid");
  if (release.renderer_build_id !== computeRendererBuildId(release)) throw new Error("renderer_build_id_mismatch");
  if (release.hyperframes_version !== "0.7.84" || release.gsap_version !== "3.15.0") throw new Error("renderer_dependency_version_invalid");
  if (!release.node?.version?.startsWith("v22.")) throw new Error("renderer_node_version_invalid");
  for (const name of ["node", "chromium", "ffmpeg", "ffprobe"]) {
    if (typeof release[name]?.version !== "string" || !release[name].version) throw new Error(`renderer_${name}_version_invalid`);
    assertSha(release[name].sha256, `renderer_${name}_sha256_invalid`);
  }
  if (release.locale !== "C.UTF-8" || release.timezone !== "UTC" || release.thread_count !== 2) throw new Error("renderer_environment_invalid");
  if (!Array.isArray(release.fonts) || release.fonts.length < 1) throw new Error("renderer_fonts_invalid");
  const sorted = [...release.fonts].sort((a, b) => a.relative_path.localeCompare(b.relative_path));
  if (JSON.stringify(sorted) !== JSON.stringify(release.fonts)) throw new Error("renderer_fonts_unsorted");
  for (const font of release.fonts) assertSha(font.sha256, "renderer_font_sha256_invalid");
  return release;
}


export async function inspectRendererRelease({
  repoRoot,
  releaseRoot,
  nodePath,
  chromiumPath,
  ffmpegPath,
  ffprobePath,
  gitCommit,
  versionProbe = commandVersion,
}) {
  const root = resolve(releaseRoot);
  const paths = {node: nodePath, chromium: chromiumPath, ffmpeg: ffmpegPath, ffprobe: ffprobePath};
  for (const path of Object.values(paths)) {
    const metadata = await stat(path);
    if (!metadata.isFile()) throw new Error("renderer_binary_invalid");
  }
  const fontsRoot = resolve(root, "assets", "fonts");
  const fontNames = (await readdir(fontsRoot)).filter((name) => name.endsWith(".woff2")).sort();
  const fonts = await Promise.all(fontNames.map(async (name) => ({
    relative_path: relative(root, resolve(fontsRoot, name)).replaceAll("\\", "/"),
    sha256: await hashFile(resolve(fontsRoot, name)),
  })));
  const versions = Object.fromEntries(await Promise.all(Object.entries(paths).map(async ([name, path]) => [name, {
    version: await versionProbe(path),
    sha256: await hashFile(path),
  }])));
  const release = {
    schema_version: 1,
    git_commit: gitCommit,
    package_lock_sha256: await hashFile(resolve(root, "package-lock.json")),
    node: versions.node,
    chromium: versions.chromium,
    ffmpeg: versions.ffmpeg,
    ffprobe: versions.ffprobe,
    hyperframes_version: "0.7.84",
    gsap_version: "3.15.0",
    locale: "C.UTF-8",
    timezone: "UTC",
    encoder_argv: ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-threads", "2"],
    thread_count: 2,
    fonts,
  };
  release.renderer_build_id = computeRendererBuildId(release);
  return validateRendererRelease(release);
}


async function atomicJsonWrite(destination, value) {
  const target = resolve(destination);
  const temporary = resolve(dirname(target), `.${basename(target)}.${process.pid}.tmp`);
  await writeFile(temporary, Buffer.concat([canonicalReleaseBytes(value), Buffer.from("\n")]));
  const file = openSync(temporary, "r");
  try {
    try { fsyncSync(file); } catch (error) {
      if (process.platform !== "win32" || error?.code !== "EPERM") throw error;
    }
  } finally { closeSync(file); }
  await rename(temporary, target);
  const directory = openSync(dirname(target), "r");
  try {
    try { fsyncSync(directory); } catch (error) {
      if (process.platform !== "win32" || error?.code !== "EPERM") throw error;
    }
  } finally { closeSync(directory); }
}


export async function writeRendererReleaseLock(release, destination) {
  validateRendererRelease(release);
  await atomicJsonWrite(destination, release);
}


export async function writeArtifactAttestation({rendererBuildId, archivePath, destination}) {
  if (!BUILD_ID.test(rendererBuildId)) throw new Error("renderer_build_id_invalid");
  await atomicJsonWrite(destination, {
    renderer_build_id: rendererBuildId,
    release_archive_sha256: await hashFile(archivePath),
  });
}


if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const args = Object.fromEntries(Array.from({length: Math.floor((process.argv.length - 2) / 2)}, (_, index) => {
    const offset = 2 + index * 2;
    return [process.argv[offset], process.argv[offset + 1]];
  }));
  const required = ["--release-root", "--node", "--chromium", "--chromium-version", "--ffmpeg", "--ffprobe", "--git-commit"];
  if (required.some((name) => !args[name])) throw new Error("renderer_release_arguments_invalid");
  const release = await inspectRendererRelease({
    repoRoot: args["--release-root"],
    releaseRoot: args["--release-root"],
    nodePath: args["--node"],
    chromiumPath: args["--chromium"],
    ffmpegPath: args["--ffmpeg"],
    ffprobePath: args["--ffprobe"],
    gitCommit: args["--git-commit"],
    versionProbe: async (path) => {
      if (path === args["--chromium"]) return `Chromium ${args["--chromium-version"]}`;
      if (path === args["--ffmpeg"] || path === args["--ffprobe"]) return commandVersion(path, ["-version"]);
      return commandVersion(path);
    },
  });
  await writeRendererReleaseLock(release, resolve(args["--release-root"], "renderer-release.lock.json"));
  process.stdout.write(`${release.renderer_build_id}\n`);
}
