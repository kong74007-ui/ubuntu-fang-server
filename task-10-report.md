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
- Added fixture-driven `platform_video`, `external_video` and `audio_only`
  fake-provider E2E coverage. Each flow uses real quote, hold, job store,
  normalization, transcript/alignment, director, material resolution, OpenAI
  image adapter, ElevenLabs music/SFX adapters, Shotstack adapter, quality
  inspection, private COS delivery, settlement and `video_assets` insertion.
- Each completed job is replayed. The tests verify every externally billable
  fake operation and final COS delivery occur exactly once, owner identity is
  preserved, and the actual charge plus refunded hold difference equals the
  final points balance.

## TDD evidence

- RED: `python -m unittest tests.test_ai_edit_v2_e2e -v` failed because all
  three E2E fixtures and the smoke module were absent.
- Additional RED: the noisy-provider test demonstrated provider stdout leakage
  before suppression was added.
- GREEN: final targeted suite passed 8/8.

## Verification

- `python -m unittest tests.test_ai_edit_v2_e2e -v`: 8/8 passed.
- `python -m unittest discover -s tests -p 'test_ai_edit_v2*.py' -v`: 336/336
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
