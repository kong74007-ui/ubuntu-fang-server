# AI Edit V3 Qualified Output Design

## Status and scope

This corrective design is approved by the user's instruction to continue optimizing until the online V3 test environment produces a qualified final video. It is limited to the isolated V3 path and does not alter V2, Shotstack, production traffic, production pricing, or production data.

The motivating production sample is test job `f05a5b85a65e44418a22a940fb661469`, asset `147`. The file was technically valid but visually unacceptable: one composition covered the full 26.178 seconds, a generated image occupied only half of an opaque panel, the speaker was obscured, and all captions were concatenated into one static paragraph. The existing visual inspector returned unconditional passes and allowed publication.

## Outcome

For a normal 20–60 second talking-head input, V3 must produce a content-driven sequence instead of a single full-duration card. A qualified output must:

- contain at least three bounded scenes when the aligned transcript contains at least three useful caption groups;
- preserve the speaker as the default visual anchor for talking-head sources;
- use generated or uploaded materials only in the scenes that request them;
- never leave an unintended empty half-panel when a scene contains one material;
- display captions according to their time ranges rather than concatenating the whole transcript;
- fail closed before publication when the manifest structurally recreates the diagnosed failure pattern;
- retain the existing security, ownership, billing, COS, renderer sandbox, and V2-isolation contracts.

## Director and deterministic compiler

Qwen remains `qwen3.7-max-2026-06-08`, but its bounded creative response may include a concept, motion energy, layout sequence, and scene visual focuses. The deterministic compiler remains the authority for exact timestamps, caption text, legal component IDs, material IDs, and continuous coverage.

Caption time boundaries define the first scene candidates. Adjacent very short captions may be grouped, while a normal caption sequence produces approximately 3–7 second scenes. If the model response is missing or malformed, deterministic content segmentation still produces multiple legal scenes; model formatting must not collapse the video back to one full-duration scene.

For talking-head video, the layout sequence starts and ends with speaker-preserving layouts. `product_hero` is not used for generic generated context material. Material scenes alternate with speaker-led scenes, with a maximum of four material requests in the first release. Audio-only inputs continue to prefer material-led layouts.

Each material request owns a stable request ID and one time range. The compiler binds a scene only to the asset IDs declared by that scene's material slots. It must not copy every task material into every composition.

## Renderer behavior

The layout compiler marks material count explicitly. A one-material container uses one column and fills its assigned material region; two or more materials retain the supported collage grid.

Each composition contains only captions overlapping that scene. With caption-bounded scenes this yields timed, concise subtitles. The overlay compiler must not concatenate unrelated captions from the rest of the video. Overlay IDs remain unique within each composition and all visible text continues to be escaped as data.

## Quality gate

The unconditional visual-pass placeholder is removed from the release path. Before visual findings can pass, a deterministic manifest inspector verifies:

- scene count and maximum scene duration for non-trivial videos;
- continuous scene coverage and useful layout variation;
- talking-head speaker visibility;
- per-scene material binding and absence of unused full-task material injection;
- caption-to-scene overlap without full-transcript static concatenation.

Technical FFmpeg checks, ownership/provenance checks, and existing fail-closed behavior remain unchanged. The structural inspector is intentionally conservative: an uncertain blocking condition is a failure, not an automatic pass. Render snapshots remain acceptance evidence, and the final test task is manually reviewed across the full timeline before it is called qualified.

## Verification and release

Development follows red-green TDD. Required regression tests reproduce the five diagnosed defects before implementation. The branch must pass targeted Python tests, all V3 Python tests, renderer Node tests, and repository CI.

After PR merge, only the V3 files from the merged commit are deployed to the Fang test server. Services are restarted with no active V3 task. A new real V3 talking-head task is created through the public API, allowed to finish, downloaded from private COS through the normal asset route, and reviewed at multiple timestamps plus full playback. A technically valid but visually repetitive, obstructed, blank, or static-caption output is a failed acceptance result and triggers another TDD iteration.
