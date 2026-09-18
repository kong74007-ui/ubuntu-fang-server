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
records the exact runtime fingerprint, per-video hashes and frame counts. The
final local warm-cache runs took 8.99-32.35 seconds per template (12.25 seconds
mean); this excludes source preparation, downloads, queueing and upload.

## Rendering path

1. The pinned official HyperFrames compiler inlines nested compositions and
   scopes their CSS/scripts. Browser geometry is sampled at the authored GSAP
   time, including nested start offsets, opacity, transforms and SVG clip paths.
   The yellow-banner canvas uses a native 24-sample radial shader with the
   original template's exported pose function. Unrecognized canvas compositors
   are rejected instead of silently producing background-only video.
2. Source frames decode to 16-bit RGBA. Native HLG/PQ matching the output signal
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

Keep the GPU runtime in an immutable release directory with its pinned lockfile.
Install dependencies using `npm ci --ignore-scripts --no-audit --no-fund` there.
Requirements: Node >=22, Chrome, Python/Pillow, FFmpeg/ffprobe with NVENC and
zscale, and an NVIDIA driver supporting the chosen backend. The tested toolchain
uses FFmpeg 8.1.1. Windows uses D3D12; Linux uses Vulkan and must pass preflight
on that machine. The central CPU service can remain the metadata/preflight
upstream; it is not eligible to claim GPU-required jobs.

Install both `server/matrix_template_api.py` and `server/matrix_gpu_runtime.py`.
Set these only on the GPU worker's rendering service:

```text
MATRIX_TEMPLATE_GPU_MODE=required
MATRIX_TEMPLATE_GPU_RUNTIME=<immutable-runtime-directory>
MATRIX_TEMPLATE_GPU_NODE=<node-executable>
MATRIX_TEMPLATE_HYPERFRAMES_BROWSER=<chrome-executable>
# Optional exact Dawn adapter selection:
MATRIX_TEMPLATE_GPU_ADAPTER=<adapter-name>
```

`stage-runtime.ps1` stages and verifies a new Windows runtime directory without
stopping, restarting or switching services. It deliberately does not copy
credentials or edit active environment files. Start conservatively with one
render slot on low-VRAM nodes, then measure before increasing concurrency.

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
`--cache <directory>` is an explicit local-benchmark option; operators own its
retention and must not point it at original media. Rendering receipts contain
source hashes and actual encoder/adapter evidence; do not publish private media.
