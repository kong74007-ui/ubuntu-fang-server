# AI Edit V2 Final Fix Round 2

## Scope

This commit closes the three requested P1 release gaps without pushing,
deploying, restarting services, or calling real providers.

## Fixes

1. Durable Shotstack webhook reconciliation
   - Added a shared provider-event queue consumer used in both normal and
     reconciliation-only worker modes.
   - Added atomic leases, expired-lease reclaim, bounded exponential retry,
     dead-letter isolation, and duplicate suppression.
   - The worker authenticates the durable rendering attempt and performs the
     authoritative Shotstack GET before marking the hint processed.

2. Durable repair provider usage
   - Repair submit and reconcile results now record one `repair` usage row with
     a stable operation key derived from the repair idempotency key.
   - Restart/reconcile replay cannot double count the operation.
   - `actual_cost` includes the frozen repair fallback and remains capped by
     the held points; settlement refunds only the unused difference.

3. Authoritative platform-video import
   - Added owner-scoped listing and controlled import endpoints for completed
     first-party talking-video assets.
   - Import copies the server-side authoritative script and media into a V2
     material; client-supplied `source` and `original_text` are ignored.
   - Added an idempotent `(owner, platform_asset_id)` mapping and a path boundary
     for source files.
   - The UI enumerates platform assets by reference ID and imports them without
     receiving or transmitting the authoritative text.

## Verification

- Round 2 focused RED tests: initially 4 expected failures, then 4/4 passing.
- Relevant store/API/runtime/worker tests: 40/40 passing.
- Full AI Edit V2 Python discovery: 354 tests passing in 49.942s.
- UI Node tests: 6/6 passing.
- Secret scanner: 3/3 passing.
- Python compile check: passing for all changed production Python modules.
- `git diff --check`: passing.

The first full-suite attempt exposed two pre-existing timing-sensitive tests
(delivery concurrency and lost-heartbeat fencing); both passed immediately in
targeted rerun, and the complete 354-test suite then passed cleanly.
