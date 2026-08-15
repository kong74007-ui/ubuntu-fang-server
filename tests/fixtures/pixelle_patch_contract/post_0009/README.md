This fixture contains the minimal post-`0009` Pixelle source files needed to verify
`deploy/pixelle-video/patches/0010-support-talking-material-assets.patch` with the
same `git apply --unidiff-zero --check` / `git apply --unidiff-zero` contract used by
the installer.

Provenance:

- upstream source commit: `848b054e4fae40dabc62ec58e960b573e83793ac`
- ordered prelude applied before snapshotting:
  - `0001-enforce-video-task-capacity.patch`
  - `0002-remove-video-template-branding.patch`
  - `0003-support-external-narration-audio.patch`
  - `0004-disable-deepseek-v4-thinking.patch`
  - `0005-retry-image-generation.patch`
  - `0006-guard-runninghub-polling.patch`
  - `0007-fail-fast-parallel-frames.patch`
  - `0008-support-tts-speed-api.patch`
  - `0009-support-single-line-caption-cues.patch`

Integrity:

- the exact source bytes are stored in `post_0009.zip`, not as tracked text files
- the archive SHA-256 and each extracted file's SHA-256 are recorded in `manifest.json`
- the archive is deterministic: entries are lexicographically ordered, uncompressed,
  timestamped `1980-01-01T00:00:00`, and stored with mode `100644`
- the deployment test verifies the immutable archive hash, exact member allowlist, and
  each extracted file hash before running `git apply` in a temporary repository
