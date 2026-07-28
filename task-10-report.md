# Task 10 Report

## Scope

Implemented Task 10 only on `codex/ai-edit-v2-stable-release`. No real provider
call, push, deployment, service restart, Task 11 work or nested review was
performed.

## Delivered

- Added a fail-closed provider smoke CLI for `dashscope-asr`,
  `dashscope-qwen`, `openai-image`, `elevenlabs-music`, `elevenlabs-sfx`,
  `shotstack` and `cos`.
- The CLI requires an explicit provider and complete provider-specific
  environment before invoking an operation. Exit codes are stable: `0`
  success, `2` usage, `3` not ready, `4` timeout and `5` provider failure.
- CLI output is restricted to `stage=<stage> request_id=<redacted>`; provider
  stdout/stderr is suppressed and request IDs expose at most the last four safe
  characters. Headers, bodies, signed URLs and credentials are never emitted.
- Each provider operation runs in its own subprocess. The parent enforces the
  wall clock timeout with terminate/kill/wait and never forwards child output.
- Added fixture-driven `platform_video`, `external_video` and `audio_only`
  fake-provider E2E coverage. Each flow uses real quote, hold, job store,
  normalization, transcript/alignment, director, material resolution, OpenAI
  image adapter, ElevenLabs music/SFX adapters, Shotstack adapter, quality
  inspection, private COS delivery, settlement and `video_assets` insertion.
- Each completed job is replayed. The tests verify every externally billable
  fake operation and final COS delivery occur exactly once, owner identity is
  preserved, and the actual charge plus refunded hold difference equals the
  final points balance.
- Production alignment now distinguishes platform video, external video and
  external audio from durable input metadata. Platform text remains authoritative
  while ASR supplies timestamps; external inputs use ASR text.
- Added a Git-aware secret scanner covering tracked and untracked files (including
  fixtures) while honoring `--exclude-standard`. Findings contain path/type only.
- Added a true restart E2E that exits after ASR provider identity persistence,
  rebuilds services/dependencies from the same database, reconciles, and completes
  without duplicate provider calls, hold, settlement or refund.

## TDD evidence

- RED: `python -m unittest tests.test_ai_edit_v2_e2e -v` failed because all
  three E2E fixtures and the smoke module were absent.
- Additional RED: the noisy-provider test demonstrated provider stdout leakage
  before suppression was added.
- Fix-round GREEN: Task 10 E2E plus scanner suite passed 12/12.

## Verification

- `python -m unittest tests.test_ai_edit_v2_e2e tests.test_ai_edit_v2_secret_scan -v`: 12/12 passed.
- `python -m unittest discover -s tests -p 'test_ai_edit_v2*.py'`: 341/341
  passed.
- `node --test tests/test_ai_edit_v2_ui.js`: 6/6 passed.
- `python scripts/ci_validate.py`: passed.
- `python scripts/stamp_assets.py --check`: passed.
- Python compilation of the new script and test: passed.
- `git diff --check`: passed.
- Repository-wide `python -m unittest discover -s tests -v`: 1330/1346 passed
  on Windows. The remaining 1 failure and 15 errors are unrelated existing
  platform-only tests involving Windows file locking/default encoding, POSIX
  `/tmp` path spelling and unavailable Bash. WSL is not installed, so the
  Linux CI-equivalent rerun was unavailable locally.
- Secret validation passed. The narrow historical grep matched nine existing
  test-fixture files and no Task 10 file; no matching content was printed.

## Risk and follow-up

Task 10 provides fake-provider and safe smoke tooling only. Real provider smoke,
test-environment samples, deployment evidence and failure drills remain Task 11
and require separate authorization.
