# Task 8 Report — Hard Quality Gates and Atomic Delivery

## Scope

Implemented only Task 8 on `codex/ai-edit-v2-stable-release`. No Task 9 API work, fetch, push, deployment, service restart, or real-provider call was performed.

## Delivered

- Added fail-closed `inspect_output(path, resolved_plan, runner) -> QualityReport`.
- Added stable quality codes and repairable/terminal classification for:
  - video/audio presence and decode;
  - 1080p target dimensions, rotation, duration tolerance;
  - black and blank/frozen frames;
  - caption safe area, tofu blocks, and missing glyphs;
  - required-material coverage;
  - subtitle source/fact fidelity;
  - silence, clipping, dialogue/BGM balance, and dialogue/SFX balance.
- Added subprocess-compatible FFprobe/FFmpeg checks plus a fail-closed injected semantic inspector boundary for caption/material/transcript/audio evidence.
- Added `deliver(job_id, output_path, report, actual_cost, db_path=None)`:
  - uploads only a passed MP4 to the owner-hashed private COS delivery prefix;
  - verifies HEAD content length, content type, ETag, and non-empty source;
  - creates one durable delivery video artifact;
  - records actual-cost settlement, artifact, output key, and `completed` in one SQLite transaction after the external idempotent settlement response;
  - replays completed delivery idempotently;
  - refuses success settlement on storage/upload/HEAD failure and triggers one idempotent full refund.
- Hardened billing settlement/refund races with durable `settling` / `refunding` claims, stable conflict/in-progress errors, lost-response replay, and terminal refund reconciliation.
- Continued Task 7 from `quality_checking` through targeted repair and delivery when Task 8 dependencies are provided. Repair receives only failing layers and a fixed 900-second deadline. Existing Task 7 callers without Task 8 dependencies continue to stop safely at `quality_checking`.

## TDD Evidence

RED was observed before implementation:

- quality and delivery modules failed import because they did not exist;
- settlement/refund concurrency failed with `billing_not_held` rather than a single winner;
- Task 7 pipeline remained at `quality_checking`;
- invalid actual cost stranded billing in `settling`;
- subprocess-style quality runner initially failed all gates.

Each case was made GREEN with the minimum production change, followed by regression runs.

## Verification

- `python -m unittest tests.test_ai_edit_v2_quality tests.test_ai_edit_v2_delivery tests.test_ai_edit_v2_billing tests.test_collect_cos_and_refund`
  - 65 tests passed.
- `python -m unittest tests.test_ai_edit_v2_pipeline tests.test_ai_edit_v2_store -v`
  - 70 tests passed.
- `python -m unittest discover -s tests -p "test_ai_edit_v2_*.py" -v`
  - 283 tests passed.
- `python -m py_compile ...`
  - quality, delivery, billing, and pipeline modules compiled successfully.
- `git diff --check`
  - clean.

## Review Notes

- Delivery completion, the single delivery artifact, output COS key, and settled billing record share one database transaction.
- External point operations retain unique transaction keys, so a lost response can be replayed without double settlement/refund.
- Storage verification occurs before settlement; a failed upload or HEAD mismatch cannot reach successful settlement.
- No schema, store migration, public API, UI, deployment, or provider configuration was changed.
