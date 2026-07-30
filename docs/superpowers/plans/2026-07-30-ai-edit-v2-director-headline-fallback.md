# AI Edit V2 Director Headline Fallback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent a missing or blank Qwen scene headline from failing an otherwise valid AI Edit V2 director plan.

**Architecture:** Extend the existing deterministic normalization boundary in `ai_edit_v2_director.py`. Before strict V2 validation, copy the same scene's trimmed non-empty `intent` into a missing or blank `headline`; leave every other semantic and provider boundary unchanged.

**Tech Stack:** Python 3, `unittest`, existing AI Edit V2 Director and Pipeline modules.

## Global Constraints

- Only normalize `headline` when it is missing, empty, or whitespace-only and the same scene has a non-empty string `intent`.
- Preserve an existing non-empty `headline` exactly.
- If `intent` is invalid too, preserve the existing strict Schema failure.
- Do not change captions, Schema, material resolution, rendering, quality, billing, database, API, or UI behavior.
- Do not call real providers, submit jobs, spend points, deploy, or retry the failed production-like task.

---

### Task 1: Deterministic scene headline fallback

**Files:**
- Modify: `tests/test_ai_edit_v2_director.py`
- Modify: `server/content_domains/ai_edit_v2_director.py:225-260`

**Interfaces:**
- Consumes: `_normalize_structural_fields(plan: Any) -> Any`, where each scene may contain `intent` and `headline`.
- Produces: a normalized plan whose blank scene `headline` equals `scene["intent"].strip()` when that intent is valid.

- [ ] **Step 1: Write the failing regression test**

Add these behavior tests to `DirectorTests` in `tests/test_ai_edit_v2_director.py`:

```python
def test_director_fills_blank_headline_from_scene_intent_without_retry(self):
    response = copy.deepcopy(VALID_PLAN)
    response["scenes"][0]["intent"] = "  解释价格构成  "
    response["scenes"][0]["headline"] = "   "
    client = FakeQwen([json.dumps(response, ensure_ascii=False)])

    plan = generate_edit_plan(CONTEXT, client)

    self.assertEqual(plan["scenes"][0]["headline"], "解释价格构成")
    self.assertEqual(len(client.calls), 1)

def test_director_fills_missing_or_none_headline_from_scene_intent(self):
    for headline in (None, "missing"):
        with self.subTest(headline=headline):
            response = copy.deepcopy(VALID_PLAN)
            response["scenes"][0]["intent"] = "解释价格构成"
            if headline == "missing":
                response["scenes"][0].pop("headline")
            else:
                response["scenes"][0]["headline"] = headline
            client = FakeQwen([json.dumps(response, ensure_ascii=False)])

            plan = generate_edit_plan(CONTEXT, client)

            self.assertEqual(plan["scenes"][0]["headline"], "解释价格构成")
            self.assertEqual(len(client.calls), 1)
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```powershell
python -m unittest tests.test_ai_edit_v2_director.DirectorTests.test_director_fills_blank_headline_from_scene_intent_without_retry -v
```

Expected: FAIL because the current normalizer leaves the blank headline in place and Schema raises `scenes[0].headline不能为空`.

- [ ] **Step 3: Implement the minimal normalization**

In the existing per-scene normalization loop, add:

```python
headline = normalized_scene.get("headline")
intent = normalized_scene.get("intent")
if (
    (headline is None or (isinstance(headline, str) and not headline.strip()))
    and isinstance(intent, str)
    and intent.strip()
):
    normalized_scene["headline"] = intent.strip()
```

Update the helper docstring to describe deterministic required-field normalization without claiming it only normalizes identifiers and wrapper shapes.

- [ ] **Step 4: Run targeted and related tests and verify GREEN**

Run:

```powershell
python -m unittest tests.test_ai_edit_v2_director -v
python -m unittest tests.test_ai_edit_v2_schema tests.test_ai_edit_v2_pipeline -v
```

Expected: all tests pass with zero failures and zero errors.

- [ ] **Step 5: Review the diff and commit**

Confirm only the two documentation files, Director implementation, and Director test changed, then commit:

```powershell
git add docs/superpowers/specs/2026-07-30-ai-edit-v2-director-headline-fallback-design.md docs/superpowers/plans/2026-07-30-ai-edit-v2-director-headline-fallback.md tests/test_ai_edit_v2_director.py server/content_domains/ai_edit_v2_director.py
git commit -m "fix(ai-edit-v2): normalize empty scene headlines"
```
