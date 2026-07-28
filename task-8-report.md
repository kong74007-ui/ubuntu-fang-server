# Task 8 Report - Hard Quality Gates and Atomic Delivery

## Scope

Implemented Task 8 and Round 1 blocking fixes only on `codex/ai-edit-v2-stable-release`. No Task 9 API work, fetch, push, deployment, restart, or real-provider call was performed.

## Round 1 fixes

- Production dependencies now expose the real local quality runner, COS output resolver, durable actual-cost resolver, and injected repair submit/reconcile adapters. Readiness fails closed when FFprobe, FFmpeg, COS, or the repair provider is unavailable.
- Removed the nonexistent `ai-edit-v2-quality-inspect` command and the invalid `resolved_plan` subprocess keyword. Technical inspection uses only FFprobe/FFmpeg; caption, material, transcript/fact, and track-balance evidence must be auditable plan evidence or quality fails with `inspection_incomplete`.
- Strict JSON and numeric validation reject `NaN`, positive/negative infinity, booleans, wrong types, and out-of-range metrics. All quality metrics must be finite.
- Schema v7 adds a delivery intent persisted before upload and a durable delivery outbox. Replays reconcile the deterministic COS key with HEAD and reuse the same settlement key.
- Upload, settlement, outbox dispatch, and final completion are worker-lease fenced. A stale worker cannot settle or publish after losing its lease.
- Settlement atomically creates the cross-database outbox. The dispatcher idempotently writes an owner-safe real `video_assets` row, then atomically marks the outbox delivered and the V2 job completed. The internal render record uses `delivery_internal` and is not exposed as a user asset.
- Repair has a durable stage attempt, stable idempotency key, provider task ID, fixed absolute 900-second deadline, lease assertions, saved result, and restart reconciliation. A saved provider identity always invokes `repair_reconciler`, never resubmission.

## Round 2 fixes

- `settling` jobs resume directly from the canonical durable delivery intent. The resume path strictly reconstructs and validates the passed `QualityReport`, reconciles the deterministic COS object with HEAD, reuses the settlement key, and drains the durable asset outbox.
- The enabled worker periodically claims and reconciles expired/unleased pending deliveries before claiming ordinary work. Tests cover crashes after the external settlement response, after the V2 settlement/outbox transaction, before the asset write, and after the asset write but before V2 finalization.
- Production quality no longer reads `resolved_plan.quality_analysis`. `LocalQualityRunner` requires a final-MP4 analyzer interface for OCR/safe-area/glyph, material coverage, transcript/fact, and track-level audio evidence. Missing FFprobe, FFmpeg, or analyzer availability is a readiness error, so the production worker fails before claiming a job.
- Repair time is `min(configured_seconds, 900)` and the same absolute deadline covers submission/reconciliation, repair, and reinspection.
- Existing `video_assets` rows are reusable only when job ID, owner, mode, private video key, ratio, and status all match. Any mismatch raises `asset_idempotency_conflict`; coverage uses the production-compatible `job_id INTEGER UNIQUE` schema.

## Round 3 fixes

- Delivery intents now carry a canonical digest and are loaded through one validator for both first-run reuse and restart recovery. Owner, job ID, idempotency key, deterministic COS key, source size/SHA-256, actual cost, canonical quality report, and allowed status are all checked; any mutation fails closed as `delivery_intent_conflict`.
- Delivery outbox failures are isolated per job. Each poison row releases its lease, records a stable error, retry count and backoff, dead-letters on the third failure, and does not prevent later jobs from completing. Concurrent reconcilers re-check due state after claiming to prevent duplicate attempts.
- Final-MP4 analyzer readiness now requires five explicit capabilities: caption OCR, glyph inspection, material coverage, transcript/fact comparison, and audio analysis. A bare callable, partial mapping, exception, or any value other than literal `True` is not ready.
- Enabled and disabled/reconciliation-only workers use the same reconciliation cycle and pass the configured COS client, real asset database path, and points client into delivery recovery.

## Added adversarial coverage

- `NaN` and `+/-Infinity` quality evidence fails closed.
- Production bundle exposes all Task 8 dependencies and readiness rejects a missing repair provider.
- A lost settlement response resumes from durable intent/HEAD and the same settlement transaction key.
- A worker that loses its lease immediately after upload cannot settle, complete, or publish an asset.
- Completed delivery is visible in the real `video_assets` table and remains owner-safe/idempotent.
- A lost repair response after saving provider task ID resumes through reconciliation without a duplicate submit.

## Verification

- Task 8 + runtime/store/pipeline targeted suite: 93 tests, with one expectation updated for the new fail-closed production quality boundary; rerun of the corrected case and runtime/store suite passed.
- Round 2 Task 8 + runtime/store/pipeline targeted suite: **101 tests passed** in 24.979s, including heartbeat timing tests.
- Round 2 `python -m unittest discover -s tests -p 'test_ai_edit_v2*.py'`: **295 tests passed** in 28.774s.
- Round 3 Task 8 + runtime/store/pipeline targeted suite: **106 tests passed** in 23.718s, including heartbeat timing and concurrent poison-outbox coverage.
- Round 3 `python -m unittest discover -s tests -p 'test_ai_edit_v2*.py'`: **300 tests passed** in 29.817s.
- `python -m unittest discover -s tests -p 'test_ai_edit_v2*.py'`: **287 tests passed** in 28.191s.
- `git diff --check`: clean.

## Boundary notes

- Private COS remains the delivery source of truth; user assets retain only the private COS key and owner metadata.
- Cross-database completion uses a durable outbox: billing settlement plus outbox creation is atomic in the V2 database, while the user-asset insert is owner-checked and idempotent before V2 completion.
- Missing semantic/OCR/glyph/material/fact/audio evidence never produces a pass.
