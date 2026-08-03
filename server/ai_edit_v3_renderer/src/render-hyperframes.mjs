import {createHash} from "node:crypto";
import {spawn} from "node:child_process";
import {readdir, readFile, stat} from "node:fs/promises";
import path from "node:path";
import {fileURLToPath} from "node:url";

const MODULE_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const CLI = path.join(MODULE_ROOT, "node_modules", "hyperframes", "dist", "cli.js");
const SAFE_ENV = new Set(["PATH", "HOME", "TMPDIR", "TMP", "TEMP", "SYSTEMROOT", "WINDIR"]);

function sha256(bytes) { return createHash("sha256").update(bytes).digest("hex"); }

function safeEnvironment(environment, chromiumPath) {
  const result = {};
  for (const [key, value] of Object.entries(environment ?? {})) {
    if (SAFE_ENV.has(key) && typeof value === "string" && value && !value.includes("\0")) result[key] = value;
  }
  return Object.freeze({
    ...result,
    LANG: "C.UTF-8", LC_ALL: "C.UTF-8", TZ: "UTC",
    PUPPETEER_EXECUTABLE_PATH: chromiumPath,
    HYPERFRAMES_BROWSER_PATH: chromiumPath,
    HYPERFRAMES_TELEMETRY_DISABLED: "1", DO_NOT_TRACK: "1",
  });
}

function absoluteLocal(value, code) {
  if (typeof value !== "string" || !path.isAbsolute(value) || value.includes("\0") || value.includes("://")) throw new Error(code);
  return path.resolve(value);
}

export function buildRenderCommand({projectRoot, outputPath}) {
  return [CLI, "render", "--strict-all", "--no-best-effort", "--workers", "2", "--fps", "30", "--quality", "high", "--crf", "18", "--no-browser-gpu", "--frames-cache-dir", "off", "--quiet", "--format", "mp4", "--output", path.resolve(outputPath), path.resolve(projectRoot)];
}

function buildSnapshotCommand({projectRoot, snapshotRoot}) {
  return [CLI, "snapshot", "--frames", "5", "--describe", "false", "--timeout", "30000", "--output", path.resolve(snapshotRoot), path.resolve(projectRoot)];
}

async function defaultCommandRunner({argv, timeoutMs, environment, signal}) {
  return await new Promise((resolve, reject) => {
    const started = performance.now();
    const child = spawn(process.execPath, argv, {
      shell: false, windowsHide: true, detached: process.platform !== "win32",
      stdio: ["ignore", "pipe", "pipe"], env: environment,
    });
    const stdout = [];
    const stderr = [];
    let total = 0;
    let settled = false;
    const finish = (error, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
      error ? reject(error) : resolve(value);
    };
    const stop = () => {
      try { process.platform === "win32" ? child.kill("SIGKILL") : process.kill(-child.pid, "SIGKILL"); } catch { try { child.kill("SIGKILL"); } catch {} }
    };
    const abort = () => { stop(); finish(new Error("render_aborted")); };
    const timer = setTimeout(() => { stop(); finish(new Error("render_timeout")); }, timeoutMs);
    const collect = (target) => (chunk) => {
      total += chunk.length;
      if (total > 4 * 1024 * 1024) { stop(); finish(new Error("render_output_exceeded")); return; }
      target.push(chunk);
    };
    child.stdout.on("data", collect(stdout));
    child.stderr.on("data", collect(stderr));
    child.once("error", (error) => finish(error));
    child.once("close", (returncode) => finish(null, {returncode, stdout: Buffer.concat(stdout), stderr: Buffer.concat(stderr), elapsedMs: Math.round(performance.now() - started)}));
    signal?.addEventListener("abort", abort, {once: true});
  });
}

export async function renderHyperframes({
  projectRoot, outputPath, chromiumPath, timeoutMs, environment, signal,
  commandRunner = defaultCommandRunner,
}) {
  const project = absoluteLocal(projectRoot, "render_project_path_invalid");
  const output = absoluteLocal(outputPath, "render_output_path_invalid");
  const chromium = absoluteLocal(chromiumPath, "render_chromium_path_invalid");
  if (!Number.isInteger(timeoutMs) || timeoutMs < 1_000 || timeoutMs > 3_300_000) throw new Error("render_timeout_invalid");
  const env = safeEnvironment(environment, chromium);
  const argv = buildRenderCommand({projectRoot: project, outputPath: output});
  const execution = await commandRunner({argv, timeoutMs, environment: env, signal});
  if (!execution || execution.returncode !== 0) throw new Error("hyperframes_render_failed");
  const metadata = await stat(output);
  if (!metadata.isFile() || metadata.size < 1) throw new Error("hyperframes_output_missing");
  const outputBytes = await readFile(output);
  const snapshotRoot = path.join(path.dirname(output), "snapshots");
  const snapshotArgv = buildSnapshotCommand({projectRoot: project, snapshotRoot});
  const snapshotExecution = await commandRunner({argv: snapshotArgv, timeoutMs: Math.min(timeoutMs, 120_000), environment: env, signal});
  if (!snapshotExecution || snapshotExecution.returncode !== 0) throw new Error("hyperframes_snapshot_failed");
  const names = (await readdir(snapshotRoot)).filter((name) => name.endsWith(".png")).sort();
  if (names.length < 1 || names.length > 6) throw new Error("hyperframes_snapshots_invalid");
  const snapshots = await Promise.all(names.map(async (name) => {
    const snapshotPath = path.join(snapshotRoot, name);
    const bytes = await readFile(snapshotPath);
    return Object.freeze({path: snapshotPath, sha256: sha256(bytes), size: bytes.length});
  }));
  return Object.freeze({
    outputPath: output, outputSha256: sha256(outputBytes), outputSize: metadata.size,
    elapsedMs: execution.elapsedMs, stdoutSha256: sha256(execution.stdout ?? Buffer.alloc(0)),
    stderrSha256: sha256(execution.stderr ?? Buffer.alloc(0)), commandSha256: sha256(Buffer.from(JSON.stringify(argv))),
    snapshots: Object.freeze(snapshots),
  });
}
