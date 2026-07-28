# AI Edit V2 Final Review Fix Report

## Scope and boundary

This final-fix change addresses the five release-review findings only. It does
not call real providers, push, deploy, restart services, or perform a nested
review.

## Delivered fixes

1. **One fail-closed production readiness decision**
   - Feature capability, API writes, and the worker now evaluate the same
     `production_dependencies` bundle and its concrete `readiness_errors`.
   - Readiness includes OpenAI image idempotency acceptance, all five final
     media analyzer capabilities, repair handler and reconciler, callback,
     private COS, provider credentials, ffmpeg, and ffprobe.
   - Missing dependencies keep writes at HTTP 503 and workers in
     reconciliation-only mode.
   - Explicit `module:factory` configuration is available for the production
     quality analyzer, repair handler, and repair reconciler without importing
     feature/API modules into runtime construction.

2. **Correct audio-only routing**
   - The public input mode is `audio_only`; source audio is retained as
     `primary_media` and is never compiled as a video clip.
   - The original voice is always mastered into one `audio_bed`, including
     `BGM/SFX = none` and optional-audio degradation.
   - Audio-only plans inject a deterministic static-visual slot when the
     director supplied no visual slot. Rendering requires a resolved image or
     video visual and a mastered audio track.

3. **Published templates and precharge-safe input contracts**
   - `GET /api/v2/edit/templates` reads the published template repository and
     returns each current version.
   - The UI explicitly selects `platform_video`, `external_video`, or
     `audio_only`, and submits `template_id` plus `template_version` for template
     mode.
   - Quote/job validation rejects missing input mode or unpublished templates
     before billing. Platform video text is loaded from the durable material
     record; client-supplied `original_text` is not trusted.

4. **Fast, untrusted Shotstack webhook wakeups**
   - Callback authentication is unchanged.
   - `id` and `status` remain required; bounded official `type`, `action`,
     `owner`, `url`, `error`, and `completed` fields are accepted.
   - The callback stores a deduplicated pending reconcile hint and returns 202;
     it does not perform a synchronous provider GET and does not trust callback
     status, URL, or error fields.

5. **Durable actual provider cost**
   - Schema v10 persists one auditable usage row per confirmed billable
     operation, keyed for retry/restart replay safety.
   - Aggregation uses the quote's frozen `price_version`, caps at the hold, and
     feeds the existing durable delivery/settlement path so `actual < hold`
     refunds the difference.
   - Opaque or missing `ProviderResult.cost_units` are explicitly recorded as
     fallback-priced, never replaced by the hold. Legacy published price
     versions receive the frozen v1 fallback schedule during validation.

## TDD evidence

- RED reproduced missing production-bundle readiness, unsupported
  `audio_only`, template identity not required, audio compiled as video, empty
  catalog, synchronous/over-strict webhook behavior, missing durable cost, and
  missing UI input mode.
- GREEN targeted backend suites: 184 tests, 0 failures.
- Added explicit none/degraded audio mastering and usage restart/once tests.
- Full AI Edit V2 discovery: 349 tests, exit 0.
- UI: 6/6 passed.
- Secret scanner tests: 3/3 passed; repository scan reported
  `secret_scan=clean`.
- `scripts/ci_validate.py`: 711 Python files and 24 HTML pages passed.
- `scripts/stamp_assets.py --check`: passed.
- Changed Python files compiled with `python -m py_compile`.
- `git diff --check`: clean.

## Operational note

This is code/test readiness only. Production provider acceptance, configuration,
push, deployment, and service restart remain separately authorized operations.
