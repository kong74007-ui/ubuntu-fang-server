# Matrix template video service

Internal generation-server API for the `text-media-text` mode from the pinned
`kong74007-ui/script-to-matrix-video` Skill. It binds to `127.0.0.1:8112`, runs
up to five renders at a time, and uses the existing material-library tunnel at
`127.0.0.1:8111`. It never calls an AI image or video provider.

The installer clones and verifies commit
`243d5c168d9ab2d95daf04fef5c5e75924114eb8`, verifies and applies the
generation-server-owned private-domain layout patch, validates the 15-template
catalog, atomically switches releases, and checks the exact runtime build id.
The patch adds `full-overlay-bold` and `poster-split`; its SHA-256 is locked in
`install.sh`, so a missing or changed patch fails before the active release is
switched. The public Skill repository remains unchanged until the generation
server contract is accepted and the same patch is deliberately upstreamed.

```bash
sudo bash deploy/matrix-template-video/install.sh
```

Secrets are created or loaded from root-owned environment files and are never
committed. Deploy only after the material-library tunnel is healthy.

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
- Top copy is balanced and frozen when the job is created. The service keeps English runs, number classifiers, and common Chinese modal pairs together, prefers punctuation boundaries, and preserves the untouched source copy for audit.
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
