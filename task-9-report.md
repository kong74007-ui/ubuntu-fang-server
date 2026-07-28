# Task 9 Report - Stable V2 API and Test Workbench

## Scope

Implemented Task 9 only on `codex/ai-edit-v2-stable-release`. No Task 10 work,
push, deployment, service restart, or real-provider request was performed.

## Fix Round 1

- Submission readiness now uses the single strict expression
  `enabled && stable_runtime_ready`. Missing any stable provider, private COS,
  FFmpeg/FFprobe, or runtime quality dependency disables capabilities and makes
  every authenticated write route return 503. Capability, material/template/job
  reads and authenticated render reconciliation remain available.
- Schema version 9 adds a partial unique index on
  `(owner, predecessor_job_id)` for non-null predecessors. Retry identity is
  server-owned per predecessor, so concurrent retries with different client
  keys and response-loss retries replay the same successor and hold points once.
- The browser persists a retry key before sending, reuses it for the same
  predecessor after transport failure, and clears it only after an accepted
  response.
- Every completed or failed terminal job reports both
  `estimated_remaining_seconds` and `timing.remaining_seconds` as zero.

## API

- Added the stable singular `POST /api/v2/edit/quote` route while retaining the
  existing plural route for compatibility. Quotes expose the price range and the
  maximum held amount.
- Kept job create/get/retry owner-scoped. Retry creates and replays one successor
  with a new deterministic quote and hold, while leaving the failed predecessor
  terminal and unchanged.
- Job responses expose only user-facing stage, elapsed/estimated time,
  allowlisted degradations, quality summary, held/actual/refunded points, and
  short-lived play/download plus asset-library links.
- Public capabilities use provider-neutral workflow names and explicitly list
  disabled advanced motion graphics, AI video generation, and free-code
  rendering. Supplier names, provider IDs, private COS keys, signed source
  inputs, stack traces, and internal error codes are not returned.

## Authenticated render callback

- Added the callback HTTP adapter with a 64 KiB body ceiling, strict two-field
  event schema, positive attempt binding, active rendering-job binding, and
  constant-time HMAC token comparison.
- The callback does not trust the supplied status or output. It deduplicates the
  event and only wakes the existing authoritative active reconciliation path.
- `core.py` preserves the callback query for independent callback
  authentication and does not invoke user-session authentication on that route.

## UI and old-flow compatibility

- Completed the V2 page for platform assets, external video/audio, the three
  creation modes, required/reference image/video/audio uploads, ratio, optional
  duration, quote confirmation, polling, degradation/quality display,
  settlement/refund display, playback, download, asset link, and failed-job
  retry.
- Added shared task tracking and resume support for V2 jobs.
- The legacy video page retains its existing controls and submission flow; it
  only gains a link to the capability-gated V2 page.
- The UI does not expose renderer selection or disabled advanced controls.

## TDD evidence

- API RED: four initial contract tests failed with the expected missing route,
  fields, and disabled callback; all turned green after implementation.
- UI RED: four workflow/compatibility contracts failed on missing result fields,
  retry/tracking, legacy entry, and V2 task routing; all turned green after
  implementation.
- Callback follow-up covered authenticated deduplication, authoritative
  reconciliation, oversized/malformed payloads, event mismatch, and raw query
  forwarding.
- Round 1 RED reproduced all three defects: incomplete dependency gates still
  accepted writes, different retry keys created two successors, and terminal
  jobs exposed 2699/2700 seconds remaining. The UI RED also proved retry keys
  were not durable before transport.

## Verification

- Task 9 API/admin/legacy-video compatibility: 45 tests passed.
- Task 9 UI: 6 tests passed.
- Full AI Edit V2 Python suite: 315 tests passed in 29.685s.
- Changed Python files passed `python -m py_compile`.
- `python scripts/stamp_assets.py --check`: cache stamps OK.
- `python scripts/ci_validate.py`: 703 Python files and 24 HTML pages passed.
- `git diff --check`: clean.
- Full repository Python discovery executed 1320 tests on Windows. The Task 9
  line-count regression found by that run was corrected and its targeted test
  passed. Remaining unrelated host-specific failures are 15 `ship` test errors
  because Bash is unavailable (`WinError 2`) and one pre-existing POSIX/Windows
  path-separator assertion in `test_motion_audio`.

## Boundary notes

- Test environment only; no production authorization.
- Real provider behavior and Task 10 smoke/E2E acceptance remain out of scope.
