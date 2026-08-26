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

- Every template has three curated top/bottom font pairs selected from the bundled OFL fonts: Noto Sans SC, ZCOOL XiaoWei, Ma Shan Zheng, and ZCOOL KuaiLe.
- Selection is deterministic from `template_id + job_id`: a retry keeps the same typography while a new job receives a varied pair.
- Pairs are template-specific. Business and data templates stay restrained; handwritten fonts are limited to editorial, diary, portrait, and Chinese-title templates.
- The selected pair is persisted in `project.json` and returned as `font_selection` for audit and troubleshooting.

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
