# Matrix GPU rendering: draft, not ready for production

## Target

All 22 website template-video templates must eventually use a verified GPU
composition and encoding path. Retain each template's typography, timeline,
transitions, material selection, narration/BGM, and HDR/SDR contract. Merely
adding `--gpu` to HyperFrames or reporting an online NVIDIA card is insufficient.

## What this draft actually contains

An isolated nine-grid HLG renderer, static-copy capture helper, and unit tests.
It is NOT invoked by `matrix_template_api.py`, installed by deployment scripts,
or advertised in health. Production behavior and scheduling are unchanged.

The experimental CLI accepts only a reviewed HTML SHA and exact source/timeline
mapping. It rejects SDR, mixed inputs, PQ, missing color metadata and changed
template revisions rather than pretending to preserve their color or falling
back to CPU. This means even the production nine-grid bundle is not automatically
supported: its adapted/frozen project must pass a separately reviewed contract.

Native HLG pixel signals bypass extra transfer/tone mapping. Intermediate
linear tags prevent reapplying the HLG display transform during spatial work;
the unchanged encoded HLG signal is labelled correctly before encoding. Text
is converted separately. There is no arbitrary brightness multiplier.
The one-frame enlarged/blurred impact and nine-grid reveal times are retained.

Vulkan/libplacebo performs spatial composition and NVENC encodes Main 10.
Decode, one-time browser typography and host/GPU transfers still exist; this
is not a zero-copy, all-GPU pipeline. It does not retain Dolby Vision metadata.
No media, fonts, binaries, private paths, credentials, or generated videos are
included. Dependencies must be supplied explicitly.

## Explicit local CLI

```text
python -m server.matrix_gpu_nine_grid --project <reviewed-prepared-project> \
  --output-dir <new-empty-run-directory> --browser <chrome-executable> \
  --puppeteer-module <installed-puppeteer-core-directory> --device NVIDIA
```

Requires Node.js, Chrome, Python/Pillow, FFmpeg with libplacebo/Vulkan and
hevc_nvenc, and a compatible NVIDIA driver. Tested locally with FFmpeg 8.1.1.
The project must already contain nine prepared 10-bit HLG clips, the reviewed
HTML/fonts/GSAP, variables.json, and template-bound BGM. Never use untrusted
HTML as input. A run has a total deadline; a partial output is never returned
as a completed result. Output directories are not overwritten.

Run unit tests with `python -m unittest tests.test_matrix_gpu_nine_grid -v`.
No GPU or network is required for these unit tests; real GPU acceptance remains
a separate gate. Library reference: https://ffmpeg.org/ffmpeg-filters.html#libplacebo

## Local evidence, not a production throughput guarantee

On one i9-13900HX/RTX 4080 Laptop workstation, a 12-second 1080p/30 nine-grid
sample took about 194 seconds via software HDR, 185 seconds with NVENC-only,
and 203 seconds with browser GPU plus NVENC. The separate Vulkan composition
prototype rendered in about 12-16 seconds, depending on inputs. These exclude
material preparation, uploads and queueing. Encoder settings and the compositor
differ; this is not proof of equal quality or a universal speed multiplier.
The final all-HLG signal-preserving prototype was user-approved for a PR.

## Remaining merge blockers for the all-template goal

- [ ] Adapt and visually validate all 17 reference templates, including random
  motion, transitions, masks, static backgrounds and text layers.
- [ ] Adapt all four fixed motion templates, including fan stills/affine
  geometry and brush clipping; do not replace their effects with generic cuts.
- [ ] Validate production nine-grid bundle/frozen font/layout contracts and
  narration/BGM paths; remove the test-only template SHA only with a reviewed
  replacement contract.
- [ ] Establish SDR, mixed HDR/SDR and PQ color policies without hidden gain,
  source relabelling, lossy bit-depth downgrades, or unsupported-template fallback.
- [ ] Integrate runtime checks, lifecycle cancellation, CPU/memory/GPU limits,
  concurrency, atomic deployment and build hashing in the generation service.
- [ ] Add capability/version/template-set reporting to each rendering worker.
- [ ] Update the main-site relay to claim only compatible GPU jobs, maintain
  bounded queues, reject unavailable capabilities before charging, and avoid
  cooldown/load-balancing starvation from incapable nodes.
- [ ] Verify every GPU node and the corresponding main-site contract together.

Do not merge/deploy this draft as fulfillment of the all-template requirement.
The standalone Skill repository is outside this change.
