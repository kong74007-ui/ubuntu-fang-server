# Template video GPU rendering

## Scope and evidence

The backend consumes the existing prepared HTML/GSAP composition, not a second
set of template designs. `deploy/matrix-gpu/contract.json` enumerates the 22
supported public template IDs. Unknown catalog IDs prevent GPU-only startup.
No Skill repository or original material file is modified.

The 17 reference templates, nine-grid, triple-strip, yellow-banner, fan-whip and
brush-panel templates are tested as real 1080x1920/30fps renders on an NVIDIA
RTX 4080 Laptop. Acceptance includes nonempty media, exact frame counts, source
provenance, hardware adapter evidence, HLG Main 10 output and visual inspection
of opening/middle/ending frames. It is not a promise of pixel-identical results
across different encoders, displays or GPU drivers. Other workers still need
their own hardware preflight before activation.

The sanitized [22-template validation receipt](matrix-gpu-validation-20260919.json)
records the exact runtime fingerprint, per-video hashes and frame counts.
Render-stage timings exclude source preparation, downloads, queueing and upload.

## Rendering path

1. The pinned official HyperFrames compiler inlines nested compositions and
   scopes their CSS/scripts. Browser geometry is sampled at the authored GSAP
   time, including nested start offsets, opacity, transforms and SVG clip paths.
   The yellow-banner canvas uses a native 24-sample radial shader with the
   original template's exported pose function. Unrecognized canvas compositors
   are rejected instead of silently producing background-only video.
2. Only sampled source-frame intervals decode to 16-bit RGBA. Overlapping ranges
   share a cache window; disjoint ranges seek independently instead of decoding
   the unused prefix or gap. Native HLG/PQ matching the output signal
   bypasses transfer conversion. Mixed primaries/transfers use explicit FFmpeg
   zscale conversion. No arbitrary brightness multiplier is applied.
3. Dawn/WebGPU runs the perspective placement, clipping, texture sampling,
   alpha composition, blur and output packing on the hardware adapter.
4. NVENC encodes H.264 SDR or HEVC Main 10 HLG/PQ. Audio is muxed separately.
   Existing main-site voiceover continues to copy the video stream.

Typography is rasterized in Chrome; unchanged overlays are cached. Animated
decorations such as the triple-strip chevrons refresh the overlay cache instead
of being frozen. Native footage is never replaced by an 8-bit Chrome screenshot.
Material fetching, CPU decoding, browser geometry/typography, audio and host/GPU
transfers still exist: this is GPU composition and encoding, not zero CPU work.
Authored template backplates/gradients are retained, not silently removed.
Dolby Vision dynamic metadata is not preserved.

SDR-only jobs stay SDR; HLG/PQ jobs retain ten-bit output. Mixed HLG/PQ currently
selects HLG and converts other transfers explicitly. Eight-bit footage tagged
HDR and high-bit-depth footage without transfer metadata fail closed. Untagged
eight-bit SDR video uses the conventional BT.709 assumption; untagged SDR image
data uses sRGB. These assumptions are not a substitute for correctly tagged
source media. Unsupported masks/filters fail rather than disappearing or
silently returning to a CPU renderer.

## Worker configuration

For a Windows-native worker, `windows-node-launcher.py --config <private.json>`
loads only the worker-specific environment and executes the checked-in renderer
or poller entry point without a shell. Keep the version-1 configuration private
with fields `mode` (`renderer` or `poller`), `program`, `env`, and `log_dir`.
Restrict its NTFS ACL to the service identity and administrators; never commit
tokens. The renderer binds only 127.0.0.1:8212. Use a supervised scheduled task
or service with restart-on-failure, and disable the prior poller only after
draining its jobs. Preserve the existing database, user uploads and completed
outputs when relocating the data directory. Verify hardware under the actual
Windows service identity before enabling claims. Driver installation/reboot is
a separate operational gate; do not auto-reboot as part of worker activation.

Keep the GPU runtime in an immutable release directory with its pinned lockfile.
Install dependencies using `npm ci --ignore-scripts --no-audit --no-fund` there.
Requirements: Node >=22, Chrome, Python/Pillow, FFmpeg/ffprobe with NVENC and
zscale, and an NVIDIA driver supporting the chosen backend. The tested toolchain
uses FFmpeg 8.1.1. Windows uses D3D12; Linux uses Vulkan and must pass preflight
on that machine. The central CPU service can remain the metadata/preflight
upstream; it is not eligible to claim GPU-required jobs.

Install `server/matrix_template_api.py`, `server/matrix_gpu_runtime.py` and
`server/matrix_gpu_supervisor.py` together. Linux requires procfs, child-subreaper
support and pidfds; startup checks these prerequisites before claiming readiness.
Set these only on the GPU worker's rendering service:

```text
MATRIX_TEMPLATE_GPU_MODE=required
MATRIX_TEMPLATE_GPU_RUNTIME=<immutable-runtime-directory>
MATRIX_TEMPLATE_GPU_NODE=<node-executable>
MATRIX_TEMPLATE_HYPERFRAMES_BROWSER=<chrome-executable>
# Existing workers using the operator-configured HTTPS material gateway:
MATRIX_TEMPLATE_ALLOW_REMOTE_LIBRARY=1
# Optional exact Dawn adapter selection:
MATRIX_TEMPLATE_GPU_ADAPTER=<adapter-name>
```

`stage-runtime.ps1` stages and verifies a new Windows runtime directory without
stopping, restarting or switching services. It deliberately does not copy
credentials or edit active environment files. Start conservatively with one
render slot on low-VRAM nodes, then measure before increasing concurrency.

Remote material-library access is disabled by default. When explicitly enabled
by the operator, `PIXELLE_MATERIAL_LIBRARY_URL` may use a remote HTTPS endpoint
with a path prefix, without embedded credentials, query parameters or fragments.
The token stays in the existing private environment file. Loopback HTTP rules
and public-material account restrictions are unchanged. This opt-in replaces
the untracked HTTPS adaptation previously present on rendering workers.
Set `MATRIX_TEMPLATE_HYPERFRAMES_CONCURRENCY=1` and `NODE_CONCURRENCY=1` for the
initial GPU rollout. After hardware preflight, GPU-required mode permits 1–5
render slots; legacy mode retains its 1–2 limit. This is a configurable ceiling,
not a claim that every GPU sustains five renders. To exercise five real jobs,
set `MATRIX_TEMPLATE_CONCURRENCY=5`,
`MATRIX_TEMPLATE_HYPERFRAMES_CONCURRENCY=5`, and `NODE_CONCURRENCY=5` together.
For that test, set `MATRIX_TEMPLATE_SLOT_CUT_WORKERS=1` so per-job clip pools do
not multiply five jobs into twenty simultaneous hardware encoders. Isolate
test jobs from website billing, drain existing jobs before switching, measure
actual overlapping render processes and successful outputs, and restore the
previous settings on memory/encoder failures or unacceptable latency. Existing
driver, disk, timeout and child-process containment checks remain mandatory.

Startup performs a real 16-bit GPU composition and a ten-bit NVENC encode probe.
Missing dependencies, software adapters, changed runtime files and failed probes
block GPU readiness. `/health.gpu_render` reports the supported IDs, hardware
adapter, contract version and source/lock fingerprint. Completed jobs return
`gpu_render` evidence. The GPU mode does not fall back to the old renderer.

The prior path remains available only when GPU mode is explicitly disabled,
for staged rollout and central metadata-service compatibility. Website-wide
GPU-only operation additionally requires the matching main-site relay/poller
change and `RELAY_REQUIRE_GPU=1`; enabling only a worker is insufficient.

## Release and rollback gates

1. Deploy the compatible relay and node poller with enforcement initially off.
2. Stage and probe every worker; wait for its in-flight jobs to finish before
   switching its immutable service release. Do not interrupt paid renders.
3. Verify each worker advertises the expected runtime hash and all 22 IDs, then
   enable relay GPU enforcement. No capable node means preflight rejection
   before admission; busy capable nodes use a bounded queue.
4. Verify real website/CLI jobs, color format, voiceover/BGM and output delivery.

For rollback, stop admitting new GPU jobs and drain/fail/refund outstanding jobs
through the existing job workflow before reverting old relay code. Do not erase
GPU claim requirements or regenerate completed jobs. This PR does not itself
deploy, restart services, or verify remote workers.

## Tests and local CLI

```text
node deploy/matrix-gpu/probe.mjs
node --test deploy/matrix-gpu/geometry.test.mjs
node deploy/matrix-gpu/compositor.test.mjs
python -m pytest tests/test_matrix_gpu_runtime.py tests/test_matrix_hdr_color.py
node deploy/matrix-gpu/render.mjs --project <prepared-project> --variables <variables.json> --output <new-output.mp4> --browser <chrome>
```

The CLI only accepts project-local assets, blocks browser network requests, and
never overwrites a final output. A total deadline and process cleanup bound work.
By default raw-frame scratch is per-job and removed after completion/failure.
The Python parent also removes that owned scratch after the renderer tree exits,
including timeout, cancellation and nonzero exit. Windows Job Objects contain
descendant writers even if Node crashes. Linux starts a dedicated per-render
subreaper supervisor: detached Chrome processes and double-forked descendants
are adopted, killed with identity-checked pidfds, and reaped. The parent does not
set `stopped` or remove scratch without that supervisor's all-children-reaped
receipt. An abrupt supervisor crash or failed drain produces no success proof.
Linked/junction scratch paths are rejected. If tree termination
cannot be confirmed, scratch is retained rather than deleted under a writer.
`--cache <directory>` is an explicit local-benchmark option; operators own its
retention and must not point it at original media. Rendering receipts contain
source hashes and actual encoder/adapter evidence; do not publish private media.

## PR 201 regression cases

`node --test deploy/matrix-gpu/decode-plan.test.mjs` covers late source offsets,
disjoint/overlapping ranges and fractional composition starts. The opt-in test
`tests/test_matrix_gpu_windows_integration.py` creates a synthetic 30-second
2160x3840 source, then renders a 20s+3s selection and two disjoint selections with
cold caches. Both decode only 90 frames (5.56 GiB), validate actual selected frame
colors, and retain the 32 GiB per-source limit. Set `MATRIX_GPU_INTEGRATION=1`
and optionally `MATRIX_GPU_TEST_BROWSER` to run it on an NVIDIA worker; it makes
no external material/provider requests.

`tests/test_matrix_gpu_cleanup.py` launches real Node parent/child processes,
forces termination or crashes the renderer, and checks parent-owned cleanup,
external-file preservation, active-job protection and symlink/junction rejection.

`tests/test_matrix_gpu_posix.py` additionally covers detached children and
grandchildren, cancellation, missing/invalid completion proof and unrelated
process preservation. CI also launches the pinned Puppeteer with a real Chrome
using its default independent process group, kills Node with SIGKILL, and checks
that Chrome and its observed descendants no longer exist. This does not require
a GPU or any production worker access.
