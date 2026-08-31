# Matrix template video service

Internal generation-server API for the `text-media-text` mode from the pinned
`kong74007-ui/script-to-matrix-video` Skill. It binds to `127.0.0.1:8112`, runs
up to five FFmpeg renders at a time, and uses the existing material-library
tunnel at `127.0.0.1:8111`. It never calls an AI image or video provider.

The runtime exposes 19 templates: two generation-server-owned FFmpeg layouts
and the 17-template `reference-typography-17` HyperFrames pack. HyperFrames
templates require exactly three distinct approved video assets, render with
HyperFrames `0.8.16`, and run at most two concurrent renders on the 8 GB host.
Their fonts, sizes, colors, outlines, and text hierarchy are locked by the
template. Any request `font_family` is ignored for these 17 templates; the two
FFmpeg layouts continue to support automatic or explicit font selection.
HyperFrames templates accept batches of up to five outputs. Two renders occupy
slots concurrently and additional accepted jobs wait for a slot. A persisted
900-second deadline starts at database admission; render-slot waiting is capped
at 600 seconds and consumes the same deadline. Expired jobs terminate and enter
the normal failure/refund path before the production site's 1200-second poll
deadline, so accepted work cannot continue after the caller reports a timeout.

The installer clones and verifies commit
`243d5c168d9ab2d95daf04fef5c5e75924114eb8`, verifies and applies the
generation-server-owned private-domain layout patch, restricts the runtime catalog
to the two private-domain templates, atomically switches releases, and checks the
exact runtime build id.
It separately sparse-checks out reference template commit
`9040a24139372f14346816cf42a97271767a0777`, verifies the 17-entry manifest and
four fixed OFL fonts, applies a hash-locked generation-server patch limited to
variants `v01` and `v05`, and installs pinned GSAP `3.14.2` inside the release.
Variant `v01` keeps its green-outlined handwritten treatment while its five
locked text layers increase from `68/62/50/54/72px` to
`70/64/52/56/74px`. Variant `v05` keeps the approved Noto Sans SC 900 block
style, blue-black outlines and yellow CTA treatment. Catalog order is unchanged,
including `v05` as the third item.
The private-domain patch adds `full-overlay-bold` and `poster-split`; both that
patch and the separate reference-typography patch have SHA-256 locks in
`install.sh`, so a missing or changed patch fails before the active release is
switched. The public Skill repository remains unchanged until the generation
server contract is accepted and the same change is deliberately upstreamed.

```bash
sudo bash deploy/matrix-template-video/install.sh
```

Secrets are created or loaded from root-owned environment files and are never
committed. Deploy only after the material-library tunnel is healthy. The system
Python must provide Pillow (`Image`, `ImageDraw`, and `ImageFont`); the installer
verifies it before switching releases.

The production installer sets `MATRIX_TEMPLATE_CONCURRENCY=5`, requires at
least 4 vCPU and 7 GiB RAM, and configures the service for 400% CPU and 6 GiB
memory. The upgraded 4-vCPU/8-GB host completed a five-render 1080x1920 smoke
test in 119 seconds with 5/5 valid outputs. Lower-spec hosts fail installation
instead of starting an unsafe five-worker service.

## Typography variants

- The pinned public Skill supplies its four baseline OFL families; the server-owned patch adds the two private-domain layout definitions without changing the upstream repository.
- Up to ten project-authorized fonts may be provisioned privately under `/var/lib/huangque-matrix-template/private-fonts`; font binaries are never committed to Git.
- Copy `private-fonts.manifest.example.json` to that directory as `sources.json` together with the matching font files. The service accepts only the ten named families, requires `authorized: true`, rejects symlinks and unsafe filenames, and verifies every SHA-256 at startup.
- Selection, selected file SHA-256 values, and the complete private-bundle fingerprint are frozen in the SQLite job payload in the same transaction that creates the job. Recovery and retries consume only this frozen provenance and fail closed if a selected file changes.
- Pairs are template-specific. Business and data templates stay restrained; handwritten fonts are limited to editorial, diary, portrait, and Chinese-title templates.
- The selected pair is persisted in `project.json` and returned as `font_selection` for audit and troubleshooting.
- `/health` reports `private_font_bundle_sha256`; completed job results retain the selected families, filenames, file hashes, and bundle fingerprint after staged files expire.
- For a private-font render, the service copies the four baseline fonts and only the selected private font into the job directory. FFmpeg never reads the persistent private directory directly.
- HyperFrames fixed-font overrides are server-owned template settings, not user-selectable inputs. `v02` and `v03` use the authorized `Smiley Sans Oblique` private font at `62px` for `top2`; each template keeps its original color, stroke, and line height. New jobs freeze the family/file/SHA/size mapping, stage only that extra font, and inject a task-local `@font-face`; already-admitted jobs without the frozen override keep their original template style.
- `/health` reports `reference_fixed_private_fonts` and deployment requires the expected fixed private font set, so a missing or changed private file fails before release activation.
- Top copy is balanced and frozen when the job is created. The service keeps English runs, number classifiers, and common Chinese modal pairs together, prefers punctuation boundaries, and preserves the untouched source copy for audit.
- `v02` and `v05` additionally accept versioned AI semantic-boundary indices. The service verifies the indices against the untouched source hash, rejects protected-word splits, measures candidate lines with each template's actual font, size, stroke, letter spacing, and text-box width, and freezes the shortest balanced layout. The three-layer `v05` contract distributes detail copy across `top2` and `top3` without cutting through a semantic boundary. Requests without this optional contract retain the legacy layout path for rolling-deployment compatibility.
- `GET /v1/templates` returns only fonts verified at service startup. `POST /v1/preflight` and `POST /v1/jobs` accept optional `font_family`; omitting it keeps automatic template-specific pairing, while a valid value applies that family to both title regions and freezes its file SHA in the job.

## Storage and delivery policy

- Render output is published atomically only after the H.264/AAC 1080x1920 probe passes.
- Files are downloadable only while the job is `completed` and its persisted result binds the requested URL.
- Terminal job directories expire after 72 hours, or one hour after a successful download, whichever comes first.
- Cleanup runs at startup and every 15 minutes, removes at most 10 jobs per pass, and skips active jobs and downloads.
- New jobs fail closed when the state filesystem reaches 95% usage; idempotent replay of an accepted request remains available.
- SQLite rows remain as tombstones after file cleanup so request-id idempotency and job history are preserved.

The values are configurable through `MATRIX_TEMPLATE_RETENTION_SECONDS`,
`MATRIX_TEMPLATE_DELIVERY_GRACE_SECONDS`, `MATRIX_TEMPLATE_CLEANUP_INTERVAL_SECONDS`,
`MATRIX_TEMPLATE_CLEANUP_BATCH_SIZE`, and `MATRIX_TEMPLATE_DISK_HIGH_WATER_PERCENT`.

## Batch material diversity

Requests may include one shared 32-character `batch_id` plus `batch_index` and
`batch_size` (1-5). Material selection is serialized briefly while job
preparation remains five-way concurrent. Every selected image/video SHA is
reserved in SQLite before the next batch member selects, then supplied to the
material library as `used_sha256`. This prevents visual reuse across one batch
while allowing BGM reuse. A retry or service restart reuses the frozen per-job
selection instead of choosing new assets.

The five service workers may prepare five jobs concurrently, but HyperFrames
rendering is guarded by a two-slot semaphore. Jobs beyond those two slots wait
up to 600 seconds and retain the original 900-second admission deadline.
`/health` and `/v1/templates` expose `engine_concurrency` so callers can show
five-way FFmpeg capacity separately from the two HyperFrames render slots.

Reference-template top text is packed from the actual variant CSS contract.
Variants with an explicit `.vXX .top3` rule use three top regions; variants
without that rule use only `top1` and `top2`, with `top3` frozen empty. The
service still supports up to six semantic lines by packing them `2+4` for a
two-region template or `2+2+2` for a three-region template. Startup requires
explicit `top1` and `top2` styles for every variant, and `/health` reports the
detected two-layer/three-layer counts for deployment drift checks.
Already-admitted jobs keep their frozen layer text unchanged, so an upgrade
does not rewrite or resubmit in-flight work.

Before HyperFrames starts, the service probes all three selected video files,
allocates a gap-free timeline within their real durations, and writes the final
clip, typography, audio, and GSAP timings directly into the copied HTML. This
avoids the pack's static eight-second media timeline and prevents a short clip
from leaving an uncovered interval. If the three videos cannot cover the frozen
8-15 second output, the job fails instead of publishing black frames. Completed
reference renders also run a sustained-black check over the central media area;
0.5 seconds or more fails before publication.
