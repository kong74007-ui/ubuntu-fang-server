# Task 8 Readiness Fix 1 Report

## Status and scope

- Branch: `agent/ai-edit-v2-quality-repair`
- Review baseline: `eae65290dbbdb06e944c1490c2cd11ba1e281f64`
- Scope: the two Important Task 8 review findings covering repaired-output Qwen-VL URL lifetime and deterministic Shotstack targeted repair.
- Not performed: push, deployment, service restart, real-provider call, secret/configuration change, OpenAI acceptance change, V1 change, or feature-flag change.

## Root causes

1. `ProductionServices.resolve_quality_output` cached one 300-second signed URL against only the original local quality path. A repaired path, including the path recreated from the durable private COS repair object after restart, was not registered. Even the original path reused one URL for every Qwen-VL inspection. A valid repair finishing after the original URL expired could therefore fail the second quality inspection with `inspection_incomplete` inside the allowed 900-second repair window.
2. `ProductionRepairProvider.submit` rebuilt the original render graph without consuming `error_codes` or `failing_layers`. If any Shotstack code was present, local repair codes in the same report were skipped. The provider could therefore submit an unchanged graph and leave a mixed local defect unfixed.

## Strict TDD evidence

Both regression tests were added and run before production changes.

- `test_rehydrated_repair_refreshes_qwen_url_inside_repair_budget` advanced the clock beyond the original 300-second signature, returned only a durable repair COS key plus a missing local path, and required pipeline rehydration. It failed with `quality_failed / inspection_incomplete` instead of completing.
- `test_shotstack_targeted_repair_changes_graph_and_keeps_mixed_local_fix` supplied `caption_out_of_safe_area` plus `audio_clipping_detected`. It failed because the submitted caption remained the original `52 / 1720x240` instead of `44 / 1440x180`, and because FFmpeg was called zero times instead of once.

After the minimal implementation, both tests passed.

## Implementation

### Repaired-output registration and URL refresh

- Quality media registration now stores only `absolute local path -> private COS key`; signed URLs are not retained.
- Every Qwen-VL semantic inspection requests a fresh HTTPS COS GET URL with an explicit 300-second lifetime.
- Production dependencies expose the registration callback to the pipeline.
- After repair returns, the pipeline registers the repaired path and COS key before the second quality inspection. This occurs for both an existing local repair file and a file rehydrated from private COS.
- Missing registration capability, missing mapping, or an invalid/non-HTTPS signature fails closed.

### Deterministic targeted repair

- Every accepted repair code has an expected failing layer. Missing or inconsistent `error_codes/failing_layers` is rejected.
- Caption repair adds a schema-allowlisted safe layout profile (`44px`, `1440x180`) that is compiled into the actual Shotstack request.
- Black/blank-frame repair removes standard transitions from the repaired render graph.
- A Shotstack repair that produces no graph change is rejected with `repair_render_graph_unchanged` instead of resubmitting the original graph.
- In mixed Shotstack plus local cases, the Shotstack result is first stored at the deterministic private repair COS key, then the enumerated local FFmpeg fixes are applied to that result and written back to the same key.

The repair provider still accepts only the existing technical allowlist. Content/source/fact errors, required-material omissions, missing audio, abnormal silence, and dialogue masking remain terminal through the unchanged hard-quality policy.

## Durability, idempotency, and billing

- The Shotstack `provider_task_id` is still persisted immediately after submit and before polling.
- Restart recovery still invokes only `reconcile(provider_task_id=...)`; it does not call submit again.
- The repair reference and deterministic COS key are unchanged.
- Mixed local postprocessing introduces no provider submission and reports the original Shotstack request identity/cost, so the existing durable repair usage key remains the only charge record.

## Verification

- New RED-to-GREEN regressions: 2 passed after implementation.
- Targeted quality/repair/Shotstack/pipeline/runtime suite: **101 tests passed** in 29.819s.
- Full AI Edit V2 suite (`test_ai_edit_v2*.py`): **369 tests passed** in 65.854s.
- `python -m py_compile` for all changed Python and test files: passed.
- `python scripts/ci_validate.py`: passed; 767 Python files and 27 HTML pages checked.
- `git diff --check`: passed.

No test made a real provider call. Provider HTTP, COS, process execution, clocks, and downloaded media were controlled test boundaries.

## Remaining concerns and external gates

1. The exact Shotstack safe-caption layout and transition-free black/blank repair still require an authorized real-provider visual acceptance run with non-sensitive test media.
2. This change deliberately performs only the existing single repair attempt. If the targeted result still fails hard quality, the job fails and refunds rather than entering an unbounded repair loop.
3. OpenAI image acceptance and all test-environment enablement gates remain unchanged and must stay disabled until separately accepted.
4. The local `origin` fetch refspec still targets the removed `codex/ai-edit-v2-stable-release` branch, so `git fetch origin --prune` failed before work began. No remote configuration was changed.

## Fix round 1 addendum: glyph defects fail closed

### Remaining Important finding

The original safe-caption Shotstack repair changed only font size and frame dimensions. That is a real targeted correction for `caption_out_of_safe_area`, but it cannot supply a glyph missing from the bundled font or change tofu while preserving exact source text. Treating `caption_tofu_detected` and `caption_glyph_missing` as repairable could therefore spend points on a render that cannot correct the defect.

### RED-to-GREEN evidence

The new quality regression first failed for both defects because the reports returned `terminal=False`. The direct-provider regression first failed because both codes passed the repair allowlist and reached plan loading (`repair_plan_missing`) instead of being rejected before any Shotstack work.

The minimal implementation now:

- classifies `caption_tofu_detected` and `caption_glyph_missing` as terminal and not repairable;
- removes both codes from the Shotstack repair allowlist and code-to-layer map;
- rejects either code before plan lookup or provider submission;
- permits the generic `caption_invalid` repair code only when accompanied by the explicit repairable `caption_out_of_safe_area` code;
- leaves the existing deterministic safe-area layout repair unchanged.

### Additional adversarial coverage

- Both black-frame and blank-frame codes remove standard transitions from the repaired graph.
- A black/blank request against a graph with no transitions is rejected with `repair_render_graph_unchanged`; no original graph can be resubmitted merely to satisfy a repair request.
- A restart test now creates one `ProductionServices` instance, persists a repair `provider_task_id`, simulates process loss, constructs a new `ProductionServices` instance, reconciles only the saved ID, rehydrates the missing local repair file from private COS, registers that new path, and obtains three fresh 300-second URLs for the second Qwen-VL inspection. Submit is observed exactly once.

### Fix-round verification

- Focused new and affected regressions: **6 tests passed** in 1.227s.
- Targeted quality/repair/Shotstack/pipeline/runtime suite: **105 tests passed** in 24.572s.
- Full AI Edit V2 suite (`test_ai_edit_v2*.py`): **373 tests passed** in 55.772s.

No real provider call, deployment, restart, secret/configuration change, OpenAI acceptance change, V1 change, or feature-flag change was performed in this round.
