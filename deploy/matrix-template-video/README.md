# Matrix template video service

Internal generation-server API for the `text-media-text` mode from the pinned
`kong74007-ui/script-to-matrix-video` Skill. It binds to `127.0.0.1:8112`, runs
one render at a time, and uses the existing material-library tunnel at
`127.0.0.1:8111`. It never calls an AI image or video provider.

The installer clones and verifies commit
`243d5c168d9ab2d95daf04fef5c5e75924114eb8`, validates the 13-template catalog,
atomically switches releases, and checks the exact runtime build id.

```bash
sudo bash deploy/matrix-template-video/install.sh
```

Secrets are created or loaded from root-owned environment files and are never
committed. Deploy only after the material-library tunnel is healthy.

## Typography variants

- The pinned public Skill remains unchanged and supplies its four baseline OFL families.
- Up to ten project-authorized fonts may be provisioned privately under `/var/lib/huangque-matrix-template/private-fonts`; font binaries are never committed to Git.
- Copy `private-fonts.manifest.example.json` to that directory as `sources.json` together with the matching font files. The service accepts only the ten named families, requires `authorized: true`, rejects symlinks and unsafe filenames, and verifies every SHA-256 at startup.
- Selection, selected file SHA-256 values, and the complete private-bundle fingerprint are frozen in the SQLite job payload in the same transaction that creates the job. Recovery and retries consume only this frozen provenance and fail closed if a selected file changes.
- Pairs are template-specific. Business and data templates stay restrained; handwritten fonts are limited to editorial, diary, portrait, and Chinese-title templates.
- The selected pair is persisted in `project.json` and returned as `font_selection` for audit and troubleshooting.
- `/health` reports `private_font_bundle_sha256`; completed job results retain the selected families, filenames, file hashes, and bundle fingerprint after staged files expire.
- For a private-font render, the service copies the four baseline fonts and only the selected private font into the job directory. FFmpeg never reads the persistent private directory directly.

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
