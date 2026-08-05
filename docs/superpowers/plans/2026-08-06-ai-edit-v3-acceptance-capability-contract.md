# AI Edit V3 Acceptance Capability Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Make the authenticated test-environment capability response authoritative enough for the Phase E runner to prove deployment identity, quiescence, feature state, upload/job admission, and real Provider wiring before any upload or point mutation.

**Architecture:** Extend the existing capability response with a test-only `acceptance` object. Deployment identity comes from validated V3 configuration; active-job count comes from the isolated V3 store; admission comes from the service's existing effective gates; Provider readiness requires both capability status and a non-placeholder injected implementation. The HTTP runner copies only this nested object and remains fail-closed.

**Tech Stack:** Python 3.12, SQLite, existing V3 feature/service/API layers, `urllib.request`, `unittest`.

## Global constraints

- Local implementation and fake tests only. Do not deploy, call Providers, upload media, or mutate points.
- The object is additive and appears only when `environment == "test"`; production must not expose deployment SHA or global active-job count.
- `AI_EDIT_V3_DEPLOYED_SHA` is exactly 40 lowercase hexadecimal characters and is required only when V3 is enabled in `test`.
- Active means `state NOT IN TERMINAL_STATES` for `V3Store.environment`.
- Required Provider items are `tts`, `asr`, `director`, `image_generator`, `audio_generator`, and `renderer`.
- A `CapabilityPlaceholder`, including today's `tts=CapabilityPlaceholder("tts")`, is never a real Provider and must force `providers_ready=false` even if its probe reports `configured_and_wired`.
- `accepts_uploads` and `accepts_new_jobs` are the service's effective gates, not copies of raw Provider status.
- Every mismatch stops before `upload_authorized_sources()`.

---

### Task 1: Validate deployment identity

**Files:**
- Modify: `server/content_domains/ai_edit_v3/feature.py`
- Modify: `tests/test_ai_edit_v3_feature.py`

- [ ] Add `AI_EDIT_V3_DEPLOYED_SHA` to `_CONFIG_NAMES` and `deployed_sha: str | None = None` to `FeatureConfig`.
- [ ] In `FeatureConfigTests.enabled_env()`, include `"AI_EDIT_V3_DEPLOYED_SHA": "a" * 40`; do the same in `RuntimeContractTests.enabled_env()` so existing enabled-test cases keep their intended subject.
- [ ] Add tests using the existing `load_config()` function: missing SHA in enabled test config raises `config_required`; uppercase, short, whitespace, or nonhex SHA raises `config_deployed_sha_invalid`; valid SHA is preserved; disabled and production config do not require it.
- [ ] Parse with `re.fullmatch(r"[0-9a-f]{40}", value)`, require it only for `enabled and environment == "test"`, and pass it into the returned `FeatureConfig`.
- [ ] Run `python -m unittest tests.test_ai_edit_v3_feature -q` and `git diff --check`.

---

### Task 2: Count active V3 jobs

**Files:**
- Modify: `server/content_domains/ai_edit_v3/store.py`
- Modify: `tests/test_ai_edit_v3_store.py`

- [ ] In `V3LeaseTests`, use its existing `seed_job(job_id, state=...)` helper to seed `queued`, `normalizing`, `rendering`, and `settling`, plus every state in imported `TERMINAL_STATES`.
- [ ] Assert `count_active_jobs()` returns only the four active rows; add empty and terminal-only cases.
- [ ] Implement with the existing `V3Store._read(callback)` helper:

```python
def count_active_jobs(self) -> int:
    terminal = tuple(sorted(TERMINAL_STATES))
    placeholders = ",".join("?" for _ in terminal)
    return self._read(lambda connection: int(connection.execute(
        f"SELECT COUNT(*) FROM edit_v3_jobs WHERE environment=? "
        f"AND state NOT IN ({placeholders})",
        (self.environment, *terminal),
    ).fetchone()[0]))
```

- [ ] Run `python -m unittest tests.test_ai_edit_v3_store.V3LeaseTests -q` and `git diff --check`.

---

### Task 3: Expose a truthful test-only acceptance object

**Files:**
- Modify: `server/content_domains/ai_edit_v3/service.py`
- Modify: `server/content_domains/ai_edit_v3/bootstrap.py`
- Modify: `tests/test_ai_edit_v3_service.py`
- Modify: `tests/test_ai_edit_v3_api.py`

**Exact response:**

```json
{
  "acceptance": {
    "environment": "test",
    "deployed_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "active_v3_jobs": 0,
    "v3_enabled": true,
    "providers_ready": false,
    "accepts_uploads": true,
    "accepts_new_jobs": false
  }
}
```

- [ ] Add keyword-only constructor inputs `deployed_sha: str | None = None` and `acceptance_provider_identities: Mapping[str, str] | None = None` to `EditV3Service`. Validate the SHA for test only; copy and freeze identities so callers cannot mutate them.
- [ ] Add service tests proving: exact object in test; no object in production; each missing/not-wired Provider fails; any identity equal to `"placeholder"` fails; all six configured non-placeholder identities pass; `accepts_uploads` and `accepts_new_jobs` match `_accepts_uploads(report)` and `_accepts_new_jobs(report)`.
- [ ] Derive readiness from both sources: every required `CapabilityReport.items[name].status == "configured_and_wired"` and every identity is a nonempty string other than `"placeholder"`. Do not infer identity from class names inside the service.
- [ ] In `bootstrap._build()`, pass `deployed_sha=config.deployed_sha` and an explicit frozen identity map. Set `tts` to `"placeholder"` while it is `CapabilityPlaceholder`; use stable names for the actual injected ASR, director, image, audio, and renderer adapters.
- [ ] Build the existing response first. Only for test add the nested object using `store.count_active_jobs()`, `self.enabled`, Provider readiness, and the two effective admission gates.
- [ ] API tests must prove authenticated serialization, production non-disclosure, and absence of secret paths/credentials.
- [ ] Run `python -m unittest tests.test_ai_edit_v3_service tests.test_ai_edit_v3_api tests.test_ai_edit_v3_api_entrypoint tests.test_ai_edit_v3_production -q`.

**Important gate:** This task intentionally exposes today's missing real TTS as `providers_ready=false`; it must not relabel the placeholder as ready. A real website-TTS adapter is a separate implementation unit and is required before live acceptance can proceed.

---

### Task 4: Normalize the real HTTP response

**Files:**
- Modify: `scripts/ai_edit_v3_acceptance.py`
- Modify: `tests/test_ai_edit_v3_acceptance_runner.py`

- [ ] Add `HttpRealRunApi` tests with an injected opener for valid nested response, timeout, HTTP 401/500, invalid JSON, >1 MiB body, missing object, malformed fields, and unsafe base URLs. Assert zero upload calls for every failure.
- [ ] Require an HTTPS origin with no path, query, fragment, username, or password. Use a 15-second timeout and an opener with proxies disabled. Read at most 1 MiB plus one sentinel byte.
- [ ] Copy only `payload["acceptance"]`; never retain the raw response, cookie, headers, signed URLs, or credentials.
- [ ] Add `accepts_uploads is True` and `accepts_new_jobs is True` to `run_real_acceptance` before source upload.
- [ ] `build_real_run_api()` may construct the adapter only when the base URL, interactive/environment session, and ignored authorized binding manifest are complete; otherwise raise `RealRunUnavailable`.
- [ ] Keep upload and case execution separate: `upload_authorized_sources()` performs the single owner/hash-validated upload phase, and later case execution must reuse those upload IDs.
- [ ] Run `python -m unittest tests.test_ai_edit_v3_acceptance_runner -q` and `git diff --check`.

---

### Task 5: Re-enter the Phase E live gate only with new authority

- [ ] Confirm the exact deployed SHA equals `git rev-parse HEAD` and the acceptance object reports test, zero active jobs, V3 enabled, all Providers genuinely ready, and both admission booleans true.
- [ ] Stop if TTS is still a placeholder or any other Provider is not actually wired.
- [ ] Obtain explicit user authority for test deployment, source upload, real Provider calls, and test-point mutation; plan approval alone is not that authority.
- [ ] Then follow Task 7 of `docs/superpowers/plans/2026-07-30-ai-edit-v3-phase-e-acceptance.md` for single, blinded review, fault, parallel-5, and qualified stress-10 evidence.

## Verification contract

- Focused suites pass after each task.
- Full `python -m unittest discover -s tests -p "test_ai_edit_v3_*.py" -q` passes before any completion claim.
- `git diff --check` is clean.
- No credentials, private media, binding manifests, signed URLs, or generated evidence directories are committed.
