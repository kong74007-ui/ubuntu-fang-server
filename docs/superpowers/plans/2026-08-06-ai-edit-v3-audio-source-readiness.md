# AI Edit V3 Audio Source Readiness Plan

**Goal:** Make V3's two promised audio paths real: select an owned existing audio asset, or select an owned website voice plus text and generate an audio-led video.

**Boundary:** Local implementation and fake tests only. No Provider call, deployment, asset mutation, or point mutation.

## Task 1: Wire the owner-scoped website catalog

- Modify `server/content_domains/ai_edit_v3/bootstrap.py` and add focused tests.
- Map `audio.list_audio_assets(owner)` to V3 `asset_id`, title, duration, status, and owner-bound source metadata. Resolve by exact owner and ID; never trust a URL as authority.
- Map `audio.list_audio_voices(owner)` to V3 `voice_id=voice_key`, display name, preview reference, scope, and ready/unavailable status. Resolve by exact visible voice key and owner/scope.
- Resolve local audio files through the website's existing output-path resolver and probe duration server-side.

## Task 2: Normalize existing audio in ProductionStageCoordinator

- Add an `existing_audio` branch to `_source()` that resolves the owned catalog record again, rejects deleted/missing files, and returns a local path.
- Preserve no authoritative transcript for uploaded/existing audio; ASR supplies text and timestamps.
- Add owner mismatch, deleted asset, missing file, and valid source tests.

## Task 3: Add a real website CosyVoice adapter

- Create a V3 adapter around the existing website voice resolver and CosyVoice synthesis boundary.
- Inputs are owner, text, owner-visible voice key, idempotency key, deadline, and an explicit job-local output path.
- Never store audio bytes in provider receipts. Store request ID, output SHA, relative job path, media type, character usage, and elapsed time.
- Use `invoke_provider_once()` in `generating_voice`; generic/unknown submission remains pending and must not be retried as absent.
- Change bootstrap identity from `tts=placeholder` to `tts=website-cosyvoice` only when the actual adapter is injected and `DASHSCOPE_API_KEY` capability probe is ready.

## Task 4: Feed generated voice into the common pipeline

- `generating_voice` creates and freezes the job-local MP3 for `script_to_audio_video`; other input types remain a deterministic skip.
- `_source()` consumes the frozen generated voice artifact, and `normalizing` converts it through the existing audio normalization path.
- Add crash/replay tests proving no duplicate synthesis, plus audio-only scene tests proving no invented talking-head source.

## Verification

- Focused catalog, production coordinator, source, runtime, and API suites.
- Full `python -m unittest discover -s tests -p "test_ai_edit_v3_*.py" -q`.
- `git diff --check` and isolation scan; no credentials or generated media committed.
