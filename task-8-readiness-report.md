# Task 8 Readiness Fix Report (Round 5/5)

## Status and scope

- Branch: `agent/ai-edit-v2-quality-repair`
- Baseline: `7cd54ad455d6c42044ed14adff99402350b38d46`
- Scope: Task 8 production quality-analyzer and targeted-repair readiness only.
- Not performed: Task 9 work, deployment, push, service restart, real-provider call, secret write, or acceptance-flag change.

The code-side Task 8 readiness gap is implemented. Production dependencies now create concrete, fail-closed quality and repair providers without requiring external Python factory settings. This does not by itself authorize enabling the worker: the unchanged OpenAI image gate and real test-environment acceptance remain separate external gates.

## Root cause

`ProductionServices` previously loaded all three Task 8 components only from optional environment factory paths:

- `AI_EDIT_V2_QUALITY_ANALYZER_FACTORY`
- `AI_EDIT_V2_REPAIR_HANDLER_FACTORY`
- `AI_EDIT_V2_REPAIR_RECONCILER_FACTORY`

The repository did not ship implementations for those factories, so a correctly configured test worker still reported five missing final-media analyzer capabilities plus `AI_EDIT_V2_REPAIR_PROVIDER`.

## TDD evidence

The production-injection test was added before implementation and run in isolation. It failed at the expected missing production repair factory:

```text
test_production_bundle_builds_real_quality_and_repair_factories ... FAIL
AssertionError: False is not true
```

After adding the concrete factories, the same test passed. It verifies that, with callable DashScope/Shotstack transports, usable FFprobe/FFmpeg discovery and COS support, all five analyzer capabilities are literal `True` and the six Task 8 readiness errors are absent.

## Production quality analyzer

`DashScopeFinalMediaAnalyzer` uses the official DashScope multimodal-generation endpoint with Qwen-VL. The final private COS object is downloaded for local technical checks and separately exposed to Qwen-VL only through a short-lived HTTPS URL. Required image/video references are also supplied through short-lived URLs; COS keys are removed from the model prompt.

The implementation declares capabilities only when their real dependencies exist:

- caption OCR and glyph inspection require a configured/injected DashScope transport and a bound final-video URL;
- material coverage additionally requires COS presigning and rejects missing or unsupported references;
- transcript/fact comparison requires the same real Qwen-VL path;
- audio requires FFprobe, FFmpeg and downloadable COS source tracks.

Every Qwen-VL response is strict JSON with an exact per-check schema. Missing fields, extra fields, wrong types, non-finite values, malformed provider responses or unsupported material references fail closed as incomplete inspection. The implementation follows the official DashScope multimodal request shape documented for [Qwen visual/video understanding](https://help.aliyun.com/en/model-studio/qwen-api-via-dashscope).

Audio evidence is not a constant or a fabricated pass. FFmpeg measures final-master silence and true peak. Dialogue, BGM and SFX sources are downloaded from COS and measured with EBU R128; BGM measurement applies the same sidechain-compression parameters used by production mixing before the dialogue-to-BGM ratio is computed.

## Production targeted repair

`ProductionRepairProvider` accepts only enumerated, non-terminal repair codes:

- local FFmpeg repairs are limited to dimensions, rotation, duration and clipping;
- Shotstack rerender is limited to caption-render and black/blank-frame layers.

Content/source/fact errors, required-material omissions, missing audio, abnormal silence and dialogue masking are terminal. They are refunded rather than silently repaired or delivered.

Shotstack repair submission uses the durable repair idempotency key. The returned `provider_task_id` is saved before polling. Restart recovery calls the reconciler with the saved ID and never submits another render. A successful repaired MP4 is copied to a deterministic private COS key; if the local repair file disappears after restart, the pipeline rehydrates it from COS before reinspection.

## Tests

- Production injection RED then GREEN: passed after implementation.
- Provider behavior tests: Qwen-VL request/strict-schema, real FFmpeg/sidechain evidence parsing, and Shotstack task-ID/reconcile-without-resubmit tests passed.
- Task 8 plus runtime/pipeline targeted suite: **160 tests passed** in 22.595s, exit code 0.
- Full V2 suite: **367 tests passed** in 41.814s, exit code 0. One intervening run hit the existing two-second heartbeat timing assertion; the case passed immediately in isolation (0.436s) and the complete suite then passed on a fresh rerun.
- Changed Python files: `python -m py_compile` passed.
- `git diff --check`: passed.
- `python scripts/ci_validate.py`: passed (767 Python files and 27 HTML pages checked).

No test invoked a real provider. HTTP transports, provider responses, COS operations and binary discovery were controlled test boundaries.

## Remaining external blockers

Code status is complete, but enabling the real test worker remains blocked pending operator-controlled acceptance:

1. Run one authorized Qwen-VL final-media quality smoke and one targeted repair/reconcile smoke with non-sensitive test media.
2. Re-run production readiness on the test host after installing this commit; this task did not deploy or restart anything.
3. Keep `AI_EDIT_V2_OPENAI_IMAGE_IDEMPOTENCY_ACCEPTED` unset/disabled. Current site evidence shows `api.openai.com` resolves to an unusable IPv4 address and direct IPv4 port 443 times out with no proxy configured. This round does not alter that gate or claim OpenAI image readiness.

No secret or provider response body is present in this report or commit.
