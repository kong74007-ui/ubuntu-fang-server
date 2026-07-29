# AI Edit V2 Disable ElevenLabs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ensure the stable V1 editing path never calls ElevenLabs while preserving original-source audio and keeping the optional provider code available for a later release.

**Architecture:** Published templates and the Qwen director converge on `music_policy=none` and `sfx_policy=none`. The production media stage enforces that contract again before mixing only the source audio, while readiness and public capability reporting explicitly treat ElevenLabs as disabled instead of required.

**Tech Stack:** Python 3, `unittest`, SQLite provider-usage ledger, FFmpeg audio mastering, existing AI Edit V2 runtime and capability modules.

## Global Constraints

- The stable V1 runtime must not instantiate or call `ElevenLabsProvider`.
- Existing ElevenLabs provider implementation and stored test key remain untouched.
- Source speech/audio must continue through existing `mix_audio` mastering and quality checks.
- Public capability must return `optional_audio: false`.
- Music and SFX must create no provider-usage rows and therefore settle at zero points.
- `AI_EDIT_V2_ENABLED` remains unchanged during code implementation.

---

### Task 1: Freeze Stable Director Audio Policy

**Files:**
- Modify: `server/content_domains/ai_edit_v2_templates.py`
- Modify: `server/content_domains/ai_edit_v2_director.py`
- Test: `tests/test_ai_edit_v2_templates.py`
- Test: `tests/test_ai_edit_v2_director.py`

**Interfaces:**
- Consumes: `get_published_template(template_id, version)` and Qwen `ProviderResult` payloads.
- Produces: every accepted stable edit plan has `audio_plan.music_policy == "none"` and `audio_plan.sfx_policy == "none"`.

- [ ] **Step 1: Write failing template and director tests**

Add assertions that every published template exposes `none/none`, that the director system prompt only permits `none`, and that a natural/open-generation response requesting music or SFX is rejected and repaired before acceptance.

```python
def test_all_stable_templates_disable_optional_audio(self):
    for template in list_published_templates():
        self.assertEqual(template["sound_policy"], {
            "music_policy": "none",
            "sfx_policy": "none",
        })

def test_director_repairs_optional_audio_requests_to_none(self):
    wrong = copy.deepcopy(VALID_PLAN)
    wrong["audio_plan"] = {
        "speech_policy": "preserve_source",
        "music_policy": "duck_under_speech",
        "sfx_policy": "semantic_only",
    }
    fixed = copy.deepcopy(wrong)
    fixed["audio_plan"]["music_policy"] = "none"
    fixed["audio_plan"]["sfx_policy"] = "none"
    result = generate_edit_plan(CONTEXT, FakeClient([wrong, fixed]))
    self.assertEqual(result["audio_plan"]["music_policy"], "none")
    self.assertEqual(result["audio_plan"]["sfx_policy"], "none")
```

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```powershell
python -m unittest tests.test_ai_edit_v2_templates tests.test_ai_edit_v2_director -v
```

Expected: failures show published templates still request optional audio and non-template plans still accept those policies.

- [ ] **Step 3: Implement deterministic `none/none` policy**

Change each published template sound policy to:

```python
"sound_policy": {
    "music_policy": "none",
    "sfx_policy": "none",
}
```

Format the director prompt with only `none` for both optional policies and add the same deterministic validation for every creation mode:

```python
audio_plan = plan["audio_plan"]
if audio_plan["music_policy"] != "none":
    raise ValueError("stable_v1_music_policy_must_be_none")
if audio_plan["sfx_policy"] != "none":
    raise ValueError("stable_v1_sfx_policy_must_be_none")
```

- [ ] **Step 4: Run focused tests and verify GREEN**

Run the Step 2 command. Expected: all template and director tests pass.

- [ ] **Step 5: Commit Task 1**

```powershell
git add -- server/content_domains/ai_edit_v2_templates.py server/content_domains/ai_edit_v2_director.py tests/test_ai_edit_v2_templates.py tests/test_ai_edit_v2_director.py
git commit -m "fix(ai-edit-v2): freeze stable audio policy"
```

### Task 2: Remove ElevenLabs From Stable Runtime and Readiness

**Files:**
- Modify: `server/content_domains/ai_edit_v2_runtime.py`
- Modify: `server/content_domains/ai_edit_v2_feature.py`
- Test: `tests/test_ai_edit_v2_runtime.py`
- Test: `tests/test_ai_edit_v2_feature.py`
- Test: `tests/test_ai_edit_v2_api.py`

**Interfaces:**
- Consumes: an accepted resolved plan with `none/none` audio policy.
- Produces: `generated_audio={"bgm": None, "sfx": [], "degradations": []}`, an original-source mastered track, readiness without `ELEVENLABS_API_KEY`, and `optional_audio=false`.

- [ ] **Step 1: Write failing no-call and readiness tests**

Add tests that omit `ELEVENLABS_API_KEY`, patch `ElevenLabsProvider` to fail if constructed, and assert public capability remains explicit.

```python
def test_stable_runtime_never_constructs_elevenlabs(self):
    plan = _resolved_plan()
    plan["edit_plan"]["audio_plan"].update({
        "music_policy": "none",
        "sfx_policy": "none",
    })
    with patch(
        "server.content_domains.ai_edit_v2_providers.elevenlabs.ElevenLabsProvider",
        side_effect=AssertionError("ElevenLabs must not be constructed"),
    ):
        output = services.generating_media(job, context, {"previous": {"resolved_plan": plan}})
    self.assertEqual(output["generated_audio"], {
        "bgm": None,
        "sfx": [],
        "degradations": [],
    })

def test_elevenlabs_is_not_a_stable_readiness_dependency(self):
    with patch.dict(os.environ, complete_stable_env_without_elevenlabs, clear=True):
        errors = ProductionServices(db_path, quality_analyzer=analyzer).readiness_errors()
    self.assertNotIn("ELEVENLABS_API_KEY", errors)

def test_public_capability_reports_optional_audio_disabled(self):
    response = _public_capability()
    self.assertFalse(response["stable_workflow"]["optional_audio"])
```

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```powershell
python -m unittest tests.test_ai_edit_v2_runtime tests.test_ai_edit_v2_feature tests.test_ai_edit_v2_api -v
```

Expected: readiness still reports `ELEVENLABS_API_KEY`, capability reports it enabled when a key exists, or runtime constructs the provider.

- [ ] **Step 3: Implement the no-call runtime boundary**

Remove the ElevenLabs import and generation call from `ProductionServices.generating_media`. Fail closed if an upstream plan violates Task 1, then preserve the existing source-audio mixing path:

```python
audio_plan = build_audio_plan(plan, plan["text_timeline"])
if audio_plan.get("bgm") is not None or audio_plan.get("sfx"):
    raise RuntimeError("stable_optional_audio_disabled")
generated = {"bgm": None, "sfx": [], "degradations": []}
```

Remove `ELEVENLABS_API_KEY` from `ProductionServices.readiness_errors` and force the stable component state to disabled:

```python
"elevenlabs": False,
```

The API already maps that component to `stable_workflow.optional_audio`, so no new response field is introduced.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run the Step 2 command. Expected: all runtime, feature, and API tests pass.

- [ ] **Step 5: Commit Task 2**

```powershell
git add -- server/content_domains/ai_edit_v2_runtime.py server/content_domains/ai_edit_v2_feature.py tests/test_ai_edit_v2_runtime.py tests/test_ai_edit_v2_feature.py tests/test_ai_edit_v2_api.py
git commit -m "fix(ai-edit-v2): disable ElevenLabs in stable runtime"
```

### Task 3: Update End-to-End Cost Evidence and Verify the Release

**Files:**
- Modify: `tests/test_ai_edit_v2_e2e.py`
- Modify: `docs/superpowers/specs/2026-07-29-ai-edit-v2-disable-optional-audio-design.md`

**Interfaces:**
- Consumes: Task 1 `none/none` plans and Task 2 no-call runtime.
- Produces: end-to-end proof that platform video, external video, and audio-only jobs create no ElevenLabs calls or music/SFX usage charges.

- [ ] **Step 1: Write failing E2E assertions for zero optional-audio usage**

Change the fake-provider fixture expectations so the external call ledger excludes `elevenlabs-music` and `elevenlabs-sfx`, and assert no usage row uses those capabilities:

```python
self.assertEqual(result["external_charge_counts"].get("elevenlabs-music", 0), 0)
self.assertEqual(result["external_charge_counts"].get("elevenlabs-sfx", 0), 0)
self.assertFalse(any(
    row["capability"] in {"music", "sfx"}
    for row in result["provider_usage"]
))
```

- [ ] **Step 2: Run E2E tests and verify RED where old expectations remain**

Run:

```powershell
python -m unittest tests.test_ai_edit_v2_e2e -v
```

Expected: old fixtures or assertions expecting ElevenLabs calls fail until aligned with the stable policy.

- [ ] **Step 3: Update E2E fixtures and tighten the design wording**

Remove expected ElevenLabs calls from fake-provider charge maps. Update the design specification to state that the stable V1 execution path does not read or call the configured ElevenLabs key, while provider code and configuration may remain present for future use.

- [ ] **Step 4: Run the complete AI Edit V2 suite**

Run:

```powershell
python -m unittest discover -s tests -p 'test_ai_edit_v2*.py'
```

Expected: all tests pass with zero failures.

- [ ] **Step 5: Run static and secret checks**

Run:

```powershell
git diff --check
python -m unittest tests.test_ai_edit_v2_secret_scan -v
```

Expected: no whitespace errors and all secret-scanning tests pass.

- [ ] **Step 6: Commit Task 3 and push PR #24**

```powershell
git add -- tests/test_ai_edit_v2_e2e.py docs/superpowers/specs/2026-07-29-ai-edit-v2-disable-optional-audio-design.md
git commit -m "test(ai-edit-v2): prove optional audio stays disabled"
git push
```

### Task 4: Test-Server Acceptance Without ElevenLabs

**Files:**
- No repository file changes.

**Interfaces:**
- Consumes: merged or explicitly deployed PR branch on the test server.
- Produces: operational evidence that V2 remains healthy and no ElevenLabs request occurs.

- [ ] **Step 1: Deploy only the reviewed PR branch to the test environment**

Keep `AI_EDIT_V2_ENABLED=0` during deployment. Do not modify production.

- [ ] **Step 2: Verify process and public health**

```powershell
ssh admin@8.134.216.162 "sudo systemctl is-active huangque-ai-edit-v2; curl -sS -o /dev/null -w '%{http_code}' https://huangquechuanmei.com/api/gen/health"
```

Expected: worker is `active` and health is `200`.

- [ ] **Step 3: Verify readiness no longer includes ElevenLabs**

Run the server's capability/readiness check with the key omitted from the process environment. Expected: `ELEVENLABS_API_KEY` is absent from `readiness_errors` and `optional_audio` is false.

- [ ] **Step 4: Execute one fake-provider or controlled stable job**

Expected: the job reaches the next stage with original audio mastering, while the provider-usage ledger contains no `music` or `sfx` row.

- [ ] **Step 5: Leave the feature closed pending remaining provider gates**

Confirm `AI_EDIT_V2_ENABLED=0`. Enabling submissions is a separate release decision after OpenAI image, quality analyzer, and repair-provider acceptance.
