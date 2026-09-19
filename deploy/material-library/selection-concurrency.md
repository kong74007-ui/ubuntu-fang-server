# Bounded selection preparation and atomic commit

Round-robin selection still serializes authoritative usage and receipt commits.
It no longer holds that lock while hashing files or running FFprobe.

1. Under the usage lock, compute a tentative plan using only file identity/cache
   lookups. Collect unknown file/color checks instead of performing them.
2. If checks are needed, discard ALL tentative usage mutations and release the
   lock. Check the selected files outside it. Shared per-file locks coalesce
   duplicate checks; a four-slot limiter bounds hash/probe I/O.
3. Re-enter the lock, check for an existing receipt first, and re-plan against
   current usage. Commit only when every selected file has matching cached
   verification. Existing write-ahead receipt/save/recovery semantics remain.

Preparation is bounded: at most 33 plan attempts, 32 uncached color probes and a
25-second color preparation deadline per round-robin call. Each probe remains
limited to five seconds. Semantic selection retains its prior bounded check path.
These are guardrails, not a promised end-to-end latency under arbitrary I/O stalls.

Candidate expansion is reused for identical slot specifications within one plan;
random/round-robin skips unused semantic scoring and retains the legacy returned
match_score of zero. Semantic mode still computes its original scores. No ranking, exclusion,
diversity, HDR precision, ownership, receipt version or index schema is relaxed.

The renderer retries exactly once ONLY after a timeout on a round-robin /v1/select
request carrying a stable selection_id. It sends the same URL/body/ID, never a new
job. Definitive HTTP errors, missing IDs, readiness GETs and connection refusal do
not retry. The existing receipt is returned without another usage increment.

## Prepare synchronized assets before accepting work

Cold fairness-driven plans can repeatedly choose new unchecked files. Releasing
the lock alone therefore does not guarantee faster batch completion. Run the
following with the SAME native OS/Python and root path used by the material API:

`python scripts/prepare_material_library_cache.py --root <approved-root> --output <private-state>/prepared-v1.json --workers 4`

It checks all approved bytes and video metadata once, without changing usage or
receipts, then atomically publishes a bounded private cache outside the media root.
Configure `MATERIAL_LIBRARY_PREPARED_CACHE` to that file for the API. Its health
exposes `prepared_cache_entries` for deployment verification. Prepare before
service activation, and repeat after importing a new snapshot. Never commit the
generated cache or put it in a public asset directory.

The cache is an operator-controlled performance hint, bound to root identity and
file stat identity, with strict schema/policy validation. Changed files are checked
again. It is NOT a delivery integrity/authorization boundary: the asset endpoint
always hashes the actual transferred bytes; the GPU HDR validator remains intact.
Malformed/configured-missing caches fail closed. Running without a prepared cache
retains lazy verification, but cold-batch latency is not the optimized acceptance
configuration. No automatic cloud synchronization is added.

## Rollout

Deploy the material-library module and API to the active HY Windows-native service first,
then the renderer API to all four active workers, only from a merged/CI-approved
commit, with zero in-flight tasks, backups and health checks. Preserve LAN tunnels,
private credentials, usage/receipts and four-by-five concurrency. HY remains a
material server, not a standby renderer. Do not retry refunded jobs or submit the
paused batch as a deployment test.

Use tests/test_material_selection_concurrency.py for slow-check lock release,
20 concurrent callers, duplicate IDs, failed preparation rollback, receipt replay
while another probe stalls, bounded retries, and a real HTTP response-loss test.
