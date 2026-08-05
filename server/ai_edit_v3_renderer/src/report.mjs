import {writeFile} from "node:fs/promises";
import path from "node:path";

function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === "object") return Object.fromEntries(Object.keys(value).sort().map((key) => [key, canonical(value[key])]));
  return value;
}

export function canonicalReportBytes(report) {
  return Buffer.from(JSON.stringify(canonical(report)), "utf8");
}

function safeSha(value, code) {
  if (typeof value !== "string" || !/^[0-9a-f]{64}$/.test(value)) throw new Error(code);
  return value;
}

function snapshotTimestampMs(snapshotPath, durationMs) {
  const match = /^frame-\d+-at-(\d+(?:\.\d+)?)s\.png$/.exec(path.basename(snapshotPath));
  const timestampMs = match ? Math.round(Number(match[1]) * 1000) : Number.NaN;
  if (!Number.isInteger(timestampMs) || timestampMs < 0 || timestampMs > durationMs) throw new Error("render_report_snapshot_timestamp_invalid");
  return timestampMs;
}

export async function buildRenderReport({manifest, verifiedFiles, compiledProject, execution, snapshots, outputPath, reportPath}) {
  if (!manifest || !compiledProject || !execution || !Array.isArray(verifiedFiles) || !Array.isArray(snapshots)) throw new Error("render_report_input_invalid");
  const output = path.resolve(outputPath);
  if (path.resolve(execution.outputPath) !== output) throw new Error("render_report_output_mismatch");
  const report = {
    version: "1.0", status: "done",
    renderer_build_id: manifest.renderer_environment.renderer_build_id,
    registry_sha256: compiledProject.registrySha256,
    duration_ms: manifest.duration_ms,
    output_spec: {
      width: manifest.output_spec.width, height: manifest.output_spec.height,
      fps_num: manifest.output_spec.fps_num, fps_den: manifest.output_spec.fps_den,
    },
    expected_frames: compiledProject.expectedFrames,
    composition_ids: [...compiledProject.compositionIds],
    verified_inputs: verifiedFiles.map((item) => ({path: item.relativePath, size_bytes: item.size, sha256: safeSha(item.sha256, "render_report_input_sha_invalid")})),
    output: {path: path.basename(output), size_bytes: execution.outputSize, sha256: safeSha(execution.outputSha256, "render_report_output_sha_invalid"), silent: true},
    snapshots: snapshots.map((item) => ({
      path: path.basename(item.path), size_bytes: item.size,
      sha256: safeSha(item.sha256, "render_report_snapshot_sha_invalid"),
      timestamp_ms: snapshotTimestampMs(item.path, manifest.duration_ms),
    })),
    performance: {elapsed_ms: execution.elapsedMs},
    audit: {
      command_sha256: safeSha(execution.commandSha256, "render_report_command_sha_invalid"),
      stdout_sha256: safeSha(execution.stdoutSha256, "render_report_stdout_sha_invalid"),
      stderr_sha256: safeSha(execution.stderrSha256, "render_report_stderr_sha_invalid"),
      network: "disabled", audio_elements: 0, audible_video_elements: 0,
    },
  };
  const destination = reportPath ? path.resolve(reportPath) : output.replace(/\.[^.]+$/, ".report.json");
  await writeFile(destination, Buffer.concat([canonicalReportBytes(report), Buffer.from("\n")]), {flag: "wx"});
  return Object.freeze(report);
}
