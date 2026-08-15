# Pixelle Single-Line Caption Carousel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep every existing Pixelle scene, material and template style unchanged while rendering its narration as one timed caption line at a time.

**Architecture:** A scene remains one `StoryboardFrame` and owns an ordered list of `CaptionCue` values. Pixelle splits public-voice narration into cues, synthesizes or accepts one audio file per cue, probes real durations, generates media once per frame, and renders cue clips against that shared media before concatenating them into the existing frame segment. The main site uses the same splitter for personal voices and submits nested cue assets through a backward-compatible API.

**Tech Stack:** Python 3.11/3.12, FastAPI/Pydantic, Pixelle dataclasses and async pipeline, ffmpeg-python, Playwright HTML templates, `unittest`, deployment patches against Pixelle commit `848b054e4fae40dabc62ec58e960b573e83793ac`.

## Global Constraints

- Every visible body caption is one line and rotates at real audio boundaries.
- Keep existing font, size, weight, color, stroke, shadow, position, title, material, layout and non-caption animation unchanged.
- Do not add scenes or image/video generation requests.
- Do not shrink, truncate, horizontally compress or scroll long captions.
- Split at 28 display units; CJK/full-width characters count as 2 and ASCII characters count as 1.
- Preserve all narration characters and ordering.
- Legacy `{text, audio_asset_id}` narration segments remain valid.
- A frame accepts at most 20 cues.
- Push PRs only; do not merge or deploy.

---

### Task 1: Caption splitting and cue model

**Files:**
- Create: `deploy/pixelle-video/overrides/pixelle_video/services/caption_cues.py`
- Modify: `deploy/pixelle-video/patches/0009-support-single-line-caption-cues.patch`
- Modify: `deploy/pixelle-video/install.sh`
- Test: `tests/test_pixelle_caption_cues.py`
- Test: `tests/test_pixelle_deployment.py`

**Interfaces:**
- Produces: `split_caption_text(text: str, max_units: int = 28) -> list[str]`.
- Produces upstream model `CaptionCue(text: str, audio_path: Optional[str], duration: float, start_time: float, end_time: float)` and `StoryboardFrame.caption_cues: list[CaptionCue]`.
- Consumes: the pinned Pixelle upstream source and existing patch sequence `0001` through `0008`.

- [ ] **Step 1: Write failing splitter tests**

```python
def test_splitter_preserves_chinese_text_and_limits_display_width(self):
    text = "所以轩和堂做这件事，并不是为了追风口，是为了让门店效果可验证。"
    cues = split_caption_text(text)
    self.assertEqual(text, "".join(cues))
    self.assertTrue(all(display_units(cue) <= 28 for cue in cues))

def test_splitter_handles_unpunctuated_mixed_text(self):
    text = "AI培训2026帮助门店建立standard workflow持续增长"
    cues = split_caption_text(text)
    self.assertEqual(text, "".join(cues))
    self.assertTrue(all(display_units(cue) <= 28 for cue in cues))
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m unittest tests.test_pixelle_caption_cues`

Expected: FAIL because `caption_cues.py` does not exist.

- [ ] **Step 3: Implement the minimal splitter**

```python
MAX_CAPTION_UNITS = 28

def display_units(text: str) -> int:
    return sum(1 if ord(char) < 128 else 2 for char in text)

def split_caption_text(text: str, max_units: int = MAX_CAPTION_UNITS) -> list[str]:
    # Split in priority order: sentence punctuation, clause punctuation,
    # then a lossless width-boundary split. Merge only when the result stays
    # within max_units and validate that the joined output equals input.
```

- [ ] **Step 4: Run splitter tests and verify GREEN**

Run: `python -m unittest tests.test_pixelle_caption_cues`

Expected: all caption splitter tests pass.

- [ ] **Step 5: Add model/install patch assertions**

```python
def test_caption_cue_patch_is_applied_after_existing_patches(self):
    self.assertIn("0009-support-single-line-caption-cues.patch", installer)
    self.assertIn("class CaptionCue", patch)
    self.assertIn("caption_cues", patch)
```

- [ ] **Step 6: Run deployment tests and verify RED, then add patch/install entries**

Run: `python -m unittest tests.test_pixelle_deployment`

Expected before implementation: FAIL because patch `0009` is absent. Add a fail-closed installer variable, existence check and `git apply --check` followed by `git apply` after patch `0008`, then rerun to PASS.

- [ ] **Step 7: Commit Task 1**

```bash
git add deploy/pixelle-video/overrides/pixelle_video/services/caption_cues.py deploy/pixelle-video/patches/0009-support-single-line-caption-cues.patch deploy/pixelle-video/install.sh tests/test_pixelle_caption_cues.py tests/test_pixelle_deployment.py
git commit -m "feat(pixelle): add caption cue model and splitter"
```

### Task 2: Backward-compatible nested external narration

**Files:**
- Modify: `deploy/pixelle-video/patches/0009-support-single-line-caption-cues.patch`
- Modify: `deploy/pixelle-video/overrides/api/external_audio.py`
- Test: `tests/test_pixelle_external_audio.py`
- Test: `tests/test_pixelle_deployment.py`

**Interfaces:**
- Consumes request shape `NarrationSegment(text, audio_asset_id=None, cues=None)`.
- Produces resolved scene shape `{text, cues: [{text, audio_path}]}`.
- Preserves legacy scene shape `{text, audio_path}` by normalizing it to one cue inside the pipeline.

- [ ] **Step 1: Write failing nested lease tests**

```python
def test_nested_cues_are_leased_in_scene_and_cue_order(self):
    segments = [{"text": "完整旁白", "cues": [
        {"text": "第一句，", "audio_asset_id": first_id},
        {"text": "第二句。", "audio_asset_id": second_id},
    ]}]
    asset_ids, resolved = module.lease_narration_segments(segments, "task-1")
    self.assertEqual(asset_ids, [first_id, second_id])
    self.assertEqual([cue["text"] for cue in resolved[0]["cues"]], ["第一句，", "第二句。"])

def test_legacy_audio_asset_becomes_one_resolved_cue(self):
    segments = [{"text": "短旁白", "audio_asset_id": asset_id}]
    _, resolved = module.lease_narration_segments(segments, "task-1")
    self.assertEqual(len(resolved[0]["cues"]), 1)
```

- [ ] **Step 2: Run external-audio tests and verify RED**

Run: `python -m unittest tests.test_pixelle_external_audio`

Expected: FAIL because nested `cues` are not flattened or resolved.

- [ ] **Step 3: Implement flatten, validation, leasing and release**

```python
def narration_asset_ids(segments):
    ids = []
    for scene in segments or []:
        if scene.get("cues"):
            ids.extend(cue["audio_asset_id"] for cue in scene["cues"])
        else:
            ids.append(scene["audio_asset_id"])
    return ids
```

The Pydantic validator rejects both `audio_asset_id` and `cues`, empty cues, more than 20 cues, cue text/audio omissions, and external narration combined with TTS fields.

- [ ] **Step 4: Run tests and verify GREEN**

Run: `python -m unittest tests.test_pixelle_external_audio tests.test_pixelle_deployment`

Expected: nested and legacy lease lifecycle tests pass, including async failure release.

- [ ] **Step 5: Commit Task 2**

```bash
git add deploy/pixelle-video/overrides/api/external_audio.py deploy/pixelle-video/patches/0009-support-single-line-caption-cues.patch tests/test_pixelle_external_audio.py tests/test_pixelle_deployment.py
git commit -m "feat(pixelle): accept nested narration cue audio"
```

### Task 3: Public voice timing and one-media cue rendering

**Files:**
- Modify: `deploy/pixelle-video/patches/0009-support-single-line-caption-cues.patch`
- Modify: `deploy/pixelle-video/overrides/pixelle_video/services/caption_cues.py`
- Test: `tests/test_pixelle_caption_cues.py`
- Test: `tests/test_pixelle_deployment.py`

**Interfaces:**
- Produces `prepare_caption_cues(frame, tts_callable, duration_probe, concat_audio) -> None`.
- Produces frame duration as the sum of real cue durations and one combined `frame.audio_path` for media workflows.
- Produces one media generation call per `StoryboardFrame` and one final `video_segment_path` per frame.

- [ ] **Step 1: Write failing timing and render-plan tests**

```python
async def test_real_audio_durations_define_cue_boundaries(self):
    frame.caption_cues = [CaptionCue("第一句"), CaptionCue("第二句")]
    await prepare_caption_cues(frame, fake_tts, probe_duration, concat_audio)
    self.assertEqual([(c.start_time, c.end_time) for c in frame.caption_cues], [(0.0, 1.25), (1.25, 3.75)])
    self.assertEqual(frame.duration, 3.75)

def test_render_plan_reuses_one_media_and_advances_video_offset(self):
    plan = build_caption_render_plan(frame)
    self.assertEqual([item.media_path for item in plan], [frame.video_path, frame.video_path])
    self.assertEqual([item.start_time for item in plan], [0.0, 1.25])
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m unittest tests.test_pixelle_caption_cues`

Expected: FAIL because timing and render-plan functions do not exist.

- [ ] **Step 3: Implement cue preparation and rendering patch**

The patch must:

```python
# FrameProcessor order
await self._prepare_caption_audio(frame, config)  # synthesize/probe/concat
await self._step_generate_media(frame, config)    # exactly once
await self._step_create_caption_video_segment(frame, storyboard, config)
```

For each cue, `_compose_frame_html` receives only `cue.text`. Image scenes reuse `frame.image_path`; video scenes extract consecutive `[start_time, end_time]` clips from the same `frame.video_path`. Cue clips are concatenated into the original frame segment path. A one-cue frame follows the same output contract and does not duplicate work.

`HTMLFrameGenerator` wraps the `{{text}}` replacement in `<span data-pixelle-caption>` and injects only `white-space: nowrap` for that span, preserving inherited template styles.

Audio probing must raise on failure instead of estimating from file size for caption cues.

- [ ] **Step 4: Apply patches to a disposable pinned-upstream checkout and run focused upstream tests**

Run:

```bash
git checkout --detach 848b054e4fae40dabc62ec58e960b573e83793ac
git apply --check <repo>/deploy/pixelle-video/patches/0001-*.patch
# apply 0001 through 0009 in installer order, install overrides, then:
python -m unittest <repo>/tests/test_pixelle_caption_cues.py
python -m compileall -q api pixelle_video
```

Expected: all patches apply cleanly, focused tests pass, compileall exits 0.

- [ ] **Step 5: Run generation repository regression tests**

Run: `python -m unittest tests.test_pixelle_caption_cues tests.test_pixelle_external_audio tests.test_pixelle_deployment`

Expected: all tests pass.

- [ ] **Step 6: Commit Task 3**

```bash
git add deploy/pixelle-video/overrides/pixelle_video/services/caption_cues.py deploy/pixelle-video/patches/0009-support-single-line-caption-cues.patch tests/test_pixelle_caption_cues.py tests/test_pixelle_deployment.py
git commit -m "feat(pixelle): render timed single-line caption cues"
```

### Task 4: Main-site personal voice cue submission

**Files:**
- Modify: `server/content_domains/pixelle_video.py`
- Test: `tests/test_pixelle_video.py`
- Test: `tests/test_text_video_personal_audio.py`

**Interfaces:**
- Adds private helper `_split_caption_text(text: str, max_units: int = 28) -> list[str]` matching the generation-server contract.
- Changes `_personal_narration_segments(payload)` to return one scene per original narration with nested cue audio assets.
- Keeps public voice request behavior unchanged.

- [ ] **Step 1: Write failing personal-voice tests**

```python
def test_personal_voice_splits_one_scene_into_nested_cues_without_adding_scenes(self):
    result = pixelle._personal_narration_segments(payload)
    self.assertEqual(len(result), 1)
    self.assertEqual("".join(cue["text"] for cue in result[0]["cues"]), original_text)
    self.assertTrue(all("audio_asset_id" in cue for cue in result[0]["cues"]))

def test_submit_uses_original_scene_count_for_nested_cues(self):
    result = pixelle._submit(payload)
    self.assertEqual(video_body["n_scenes"], 2)
    self.assertEqual(len(video_body["narration_segments"]), 2)
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m unittest tests.test_pixelle_video tests.test_text_video_personal_audio`

Expected: FAIL because the current adapter emits one flat audio asset per narration.

- [ ] **Step 3: Implement per-cue personal TTS and nested payload**

```python
for scene_text in _personal_narrations(payload):
    cues = []
    for cue_text in _split_caption_text(scene_text):
        audio_bytes = _synthesize_personal_audio(cue_text, voice_key, speed=speech_rate)
        asset_id = _upload_personal_audio(audio_bytes)
        cues.append({"text": cue_text, "audio_asset_id": asset_id})
    scenes.append({"text": scene_text, "cues": cues})
```

Set request `text` from original scene texts, `n_scenes` from scene count and `narration_segments` from nested scenes. Do not expose cue count as scene count.

- [ ] **Step 4: Run main-site tests and verify GREEN**

Run: `python -m unittest tests.test_pixelle_video tests.test_text_video_personal_audio tests.test_text_video_page`

Expected: all related tests pass and public voice assertions remain unchanged.

- [ ] **Step 5: Commit Task 4 in the main-site worktree**

```bash
git add server/content_domains/pixelle_video.py tests/test_pixelle_video.py tests/test_text_video_personal_audio.py
git commit -m "feat(text-video): submit personal voice caption cues"
```

### Task 5: Documentation, full verification and PR publication

**Files:**
- Modify: `deploy/pixelle-video/README.md`
- Modify: `docs/superpowers/specs/2026-08-15-pixelle-single-line-caption-carousel-design.md` only if implementation reveals a factual mismatch.

**Interfaces:**
- Documents nested and legacy API shapes, one-media behavior, 28-unit split and dependency order.

- [ ] **Step 1: Update API/deployment documentation**

Document a nested `cues` request example, compatibility with flat `audio_asset_id`, maximum 20 cues per scene, and the requirement to make generation-server capability available before main-site nested requests are released.

- [ ] **Step 2: Run generation-server verification**

Run:

```bash
python -m unittest tests.test_pixelle_caption_cues tests.test_pixelle_external_audio tests.test_pixelle_deployment
git diff --check origin/main...HEAD
```

Expected: zero failures and no whitespace errors.

- [ ] **Step 3: Run main-site verification**

Run:

```bash
python -m unittest tests.test_pixelle_video tests.test_text_video_personal_audio tests.test_text_video_page
git diff --check origin/main...HEAD
```

Expected: zero failures and no whitespace errors.

- [ ] **Step 4: Perform pre-push review**

Confirm the diff changes only the files listed in this plan, no secrets or generated assets are present, public voice remains backward-compatible, personal voice scene count is unchanged, all nested assets are released, and no merge/deployment command exists in the changes.

- [ ] **Step 5: Commit documentation**

```bash
git add deploy/pixelle-video/README.md
git commit -m "docs(pixelle): document caption cue requests"
```

- [ ] **Step 6: Push and create two PRs**

Push `codex/pixelle-caption-segmentation` to `kong74007-ui/ubuntu-fang-server` and create the generation-server PR first. Push `codex/text-video-caption-segmentation` to `tang730125633/huangque-main-site` and create the main-site PR with an explicit dependency on the generation-server PR.

Do not merge or deploy either PR.
