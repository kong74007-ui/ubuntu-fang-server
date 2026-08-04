# Task 6a report — representative V2 layout slices

## Status

Completed through Fix Round 4. This report preserves the mandatory RED evidence and the fresh GREEN verification for every repair round.

## RED evidence

The first change was `server/ai_edit_v3_renderer/test/layouts-v2.test.mjs`. Before creating `layouts-v2.mjs`, any module under `src/registry/layouts/`, or changing `src/registry/index.mjs`, this command ran from `server/ai_edit_v3_renderer`:

```powershell
npm test -- --test-name-pattern="layout v2"
```

Result: exit 1; 44 existing tests passed and exactly 4 new `layout v2` tests failed.

| Failing test | Observed failure | Feature root cause |
| --- | --- | --- |
| `layout v2 publishes exactly the three independent representative module contracts` | `getLayoutV2Contracts` was `undefined` | No independent V2 registry contract or per-layout V2 module existed. |
| `layout v2 dispatch leaves legacy V1 layout resolution unchanged` | `resolveLayoutV2` was `undefined` | No V2 dispatch path existed alongside the V1 resolver. |
| `layout v2 compiles all nine variants for both ratios with auditable structural contracts` | `resolveLayoutV2` was `undefined` | The nine requested V2 variant compilers, ratio audits, public targets, identity slots, fallbacks, and structural signatures did not exist. |
| `layout v2 fails closed for required slots and renders a nonblank optional-slot fallback` | The required-slot assertion reached the missing V2 dispatch assertion | The prerequisite V2 compiler was absent, so its required-slot and fallback branch could not yet be exercised. |

These were assertion failures against an already-imported existing registry namespace. There was no missing test fixture, missing import, or Node module-resolution error.

## Why the existing generic V1 layout cannot satisfy this unit

`src/registry/layouts.mjs` defines all twelve layout IDs with the same two variants (`balanced_a`, `emphasis_b`) and emits one generic DOM shape: background, frame, speaker zone, materials container, and safe area. Layout and variant differences are primarily class names and a generic geometry helper; it cannot provide the requested nine exact variants or genuinely distinct regional DOM structures.

Its input is an optional `assets` array. It has a generic no-asset fallback but no per-layout required-slot enforcement, no explicit optional-slot inventory, and no per-slot identity mapping. The generic geometry is not an auditable V2 result with ratio-specific safe-area rectangles, public animation targets, and critical-region audit data. Replacing this path would also risk legacy V1 resolution, so Task 6a adds a separate V2 dispatch and leaves V1 untouched.

## GREEN and verification

Implemented three isolated V2 modules under `server/ai_edit_v3_renderer/src/registry/layouts/`:

- `speaker_fullscreen`: `clean_center`, `headline_top`, `caption_sidebar`
- `product_hero`: `center_pedestal`, `split_copy`, `detail_gallery`
- `steps_stack`: `vertical_steps`, `numbered_cards`, `progress_path`

Each module publishes an immutable `2.0.0` contract with its own `moduleId`, ratios, safe-area declaration, required/optional/identity slot inventory, and fallback declaration. `layouts-v2.mjs` provides the separate V2 dispatcher. `resolveLayout` remains the legacy V1 resolver; the V2 test proves it continues to resolve only `balanced_a` and rejects `clean_center`.

The V2 compiler returns exactly `{html, publicTargets, identitySlots, geometryAudit}`. Every variant has a distinct `data-layout-structure` value and a different nested DOM region arrangement. Required slots throw `layout_required_slot_missing`; absent optional slots render an inline SVG fallback with `data-fallback-state="rendered"`, rather than an empty media region. The V2 contract set is included in the canonical registry hash through `layouts_v2`.

Fresh verification after implementation:

| Command | Result |
| --- | --- |
| `npm test -- --test-name-pattern="layout v2"` | PASS, 49/49 Node tests. |
| `npm run registry:hash` | Wrote `sha256:a8c2164c76c845db386f2328ceb41b09a43e3c8e6af6308e022091e34b2aeacd`. |
| `npm test` | PASS, 49/49 Node tests. |
| `npm run release:lock` | Wrote renderer build ID `sha256:dd71bcdca53b55d3e91fdc6b24c440b12cc8dbd5f62e698f21bba2f2034e5a32`. |
| `npm run release:lock:check` | PASS. |
| selected V3 schema, render-manifest, renderer-release, release-resolution, contracts, and schema-history Python suites | PASS, 99 tests. |

The first Python run intentionally preceded writing the changed renderer release lock: 98 tests passed and the release-tree verification detected `renderer_release_tree_hash_mismatch`. After `npm run release:lock`, the same 99-test command passed.

## Risks / follow-up

- This is deliberately only the Task 6a vertical slice: no other nine layout IDs, overlay work, animation work, or PR-C behavior was added.
- The V2 dispatcher is isolated from the legacy compiler. A later unit must explicitly choose it from the visual-program rendering path; this unit does not change V1 manifest/runtime resolution.
- No deployment, external gate, PR, or push was performed.

## Fix Round 1

### RED

Before changing production code, added real `render-manifest`-shaped V2 composition tests. `compileProjectV2` with `speaker_fullscreen/clean_center` and `steps_stack/numbered_cards` failed with `layout_variant_unknown` from legacy `resolveLayout`, proving the V2 modules were exported but not selected by the renderer path.

Added independent behavior tests that also failed before the repair:

- normalized element-tree signatures (not `data-layout-structure`) found no auditable V2 region positioning;
- semantic-equivalent design-token objects emitted different style-attribute byte order;
- a compiled V2 steps scene could not provide parent `data-safe-text` nodes for the actual generated hydration loop;
- a final focused RED caught duplicate composition/V2 layout root IDs.

### GREEN

- `compileProject` now accepts internal scene options while the default V1 resolver, compiler input, source-video compiler, and emitted V1 path remain unchanged.
- `compileProjectV2` selects `resolveLayoutV2` for the three new V2 contracts and explicitly falls back to the original resolver for existing `balanced_a`/`emphasis_b` V2 manifests. V2 inputs bind composition assets to `primary`/`detail`/`accent`, source video to `speaker`, and scene captions to `steps`.
- V2 layout results are unwrapped into scene HTML; V2 optional fallbacks and overlay nodes render inside their safe area.
- Every V2 output carries ratio/variant-scoped CSS that gives the actual `data-v2-region` element its geometry-audit pixel bounds. The test validates those CSS bounds for all nine variants in both ratios.
- `stepsSlot` now puts `data-safe-text` on each `li` with a descendant `span`; the end-to-end test executes the emitted hydration loop and observes `Prepare`, `Execute`, and `Review` as nonempty text.
- Design-token serialization is key-sorted. V2 layout and composition roots now have separate IDs.

### Fix Round 1 verification

| Command | Result |
| --- | --- |
| `npm exec --package=node@22 -- node --version` | `v22.23.2` |
| `npm exec --package=node@22 -- node --test "test/*.test.mjs"` | PASS, 52/52 after lock refresh |
| `npm exec --package=node@22 -- node src/write-registry-hash.mjs` | PASS, registry hash unchanged at `sha256:a8c2164c76c845db386f2328ceb41b09a43e3c8e6af6308e022091e34b2aeacd` |
| `npm exec --package=node@22 -- node src/release-manifest.mjs --write --release-root .` | wrote renderer build `sha256:17f53e75b0938f27d4d309cab053d9bacd6bd76424fc5d1138f56138b1cafb10` |
| `npm exec --package=node@22 -- node src/release-manifest.mjs --check --release-root .` | PASS |
| selected V3 Python schema/manifest/release/contracts/history suites | PASS, 99 tests |

## Fix Round 2

### RED

Added focused renderer tests before changing implementation. A two-composition V2 manifest with a source segment beginning at 1000 ms and the speaker composition beginning at 4000 ms rendered the speaker video without the intersected local duration or media offset. A product manifest with the evidence asset first in `asset_ids` rendered that evidence asset as the primary product despite an explicit primary binding. A duplicate primary/detail binding was also accepted.

### GREEN

- Source clips are now calculated once as output/composition intersections and V2 speaker media receives the exact local start, duration, and playback start. V2 suppresses the legacy appended source clip, so the source is rendered once.
- `layout_slot_bindings` is a closed V2 manifest field with only `primary`, `detail`, `evidence`, and `accent` semantics. Production emits deterministic product bindings from frozen scene material slots. Node resolves V2 media only through these bindings; missing primary, unknown binding assets, duplicate slot names, and optional reuse of the primary asset fail closed.
- The V2 safe-area host now uses its audited ratio-specific caption rectangle instead of `inset:0`. All three representative layout families emit variant- and ratio-specific region positioning for their actual semantic hosts and speaker media has explicit width, height, and `object-fit` CSS.
- Registry identity now includes a deterministic content-addressed source manifest for all registry `.mjs` implementations. `registry:hash:check` rejects stale hash files; tests cover the implementation manifest and a stale attestation.

### Fix Round 2 verification

| Command | Result |
| --- | --- |
| `npm test` in `server/ai_edit_v3_renderer` | PASS, 54/54 Node tests |
| `npm exec --package=node@22 -- node src/write-registry-hash.mjs --check` | PASS, `sha256:6920791b3ae3579ba6ff0d10a6186c2cd9c7db568cd89a22340f5a85ee6d505f` |
| `npm exec --package=node@22 -- node src/release-manifest.mjs --check --release-root .` | PASS |
| `python -m unittest discover -s tests -p test_ai_edit_v3_schemas.py -v` | PASS, 64 tests |
| `python -m unittest discover -s tests -p test_ai_edit_v3_production.py -v` | PASS, 44 tests |

No push, PR, deployment, or gate execution was performed.

## Fix Round 3

- V2 speaker source media now renders every intersecting source segment as a unique, timed child video inside the speaker host; no legacy source-video append occurs for V2 layouts.
- Semantic bindings are derived from each scene material slot's explicit id, purpose, and priority, never material array order. Required product maps only to primary; evidence, context, decoration, and steps map to their own semantic slots and duplicates or invalid semantics fail closed.
- V2 overlay placement routes `safe_top` and `safe_bottom` to separate title/caption hosts whose CSS boxes match the audited safe areas.
- Registry source manifests use slash-normalized Unicode code-point path ordering and raw file-byte hashes. Renderer V2 schema identity is computed from the schema bytes; runtime capability auditing uses the same live schema digest.

Verification: Node renderer `npm test` 57/57; V3 schema 64/64; V3 production 44/44; director-decision 10/10; registry and release-lock checks passed. No push, PR, deployment, or gate execution.

## Fix Round 4

### RED

Focused tests were added before each production repair and failed against the real path rather than a mock or missing fixture:

- Reusing one `component_id` for two distinct overlay `instance_id` values produced duplicate DOM because routing searched the compiled HTML by component id. The real `compileProjectV2` assertion observed two copies where exactly one instance belonged in each host.
- Copying only `release_tree_files` into an isolated directory and importing the renderer failed with `ENOENT` for `content_domains/ai_edit_v3/schemas/render-manifest-v1.schema.json`. The release therefore depended on schema bytes outside its locked tree.
- A Python director decision using canonical `title_safe` and `subtitle_safe` placements survived into the edit plan and frozen manifest, but the production Node compiler rejected it with `manifest_overlay_placement_invalid` because it only recognized legacy `safe_top` and `safe_bottom` names.
- An explicit director material `slot_id="primary"` was discarded during edit-plan compilation. Product evidence-only input then failed at the wrong point with `scene_layout_binding_invalid`, showing that required layout semantics were inferred from asset order/purpose instead of preserved protocol data.
- `product_hero/split_copy` and `steps_stack/numbered_cards` emitted empty visible copy/counter regions. The ratio-aware structure test found no audited copy region and no nonempty counter content.
- Runtime capability tests failed because `CapabilityReport` exposed no separate `current_schema_hashes`; this also made it impossible to distinguish the hash used for new writes from the allowlist used to read historical plans.

### GREEN

- Overlay compilation now carries structured `{instanceId, componentId, html}` entries. V2 routing binds an exact manifest instance to exactly one title/caption host and validates its component id; it does not slice or regex-match serialized HTML.
- `manifest-schema-digests.mjs` is a generated literal module inside the release lock. A repository generator hashes the authoritative Python-owned schema files, supports write/check modes, and CI rejects stale output. The isolated locked-tree test imports and validates both manifest V1 and V2 without repository-external files. Historical V1 identity remains unchanged.
- The placement schema and Node router share one closed protocol: canonical `title_safe`/`subtitle_safe` plus the explicitly supported aliases. A cross-language regression runs a real Python director decision through edit-plan compilation and production manifest freeze, then compiles it with Node and verifies distinct safe hosts.
- Director material slot ids are preserved as `layout_slot_id` in the strict edit-plan schema. Production emits only bindings consumed by the selected layout, preserves semantic bindings independently of asset order, and requires product `primary` before either production freeze or manifest validation.
- Product split-copy and numbered-card counter regions now contain real hydrated content or an explicit SVG graphic fallback. Both ratios expose geometry-audit boxes and tests prove the visible regions are nonempty and bounded.
- Runtime capabilities publish immutable `current_schema_hashes` and `historical_schema_hashes`. New schema identities use current bytes, while the historical edit-plan allowlist retains both prior hashes and the historical V1 manifest remains readable.

### Fix Round 4 verification

| Command | Result |
| --- | --- |
| `npm exec --package=node@22 -- node --test "test/*.test.mjs"` | PASS, 61/61 Node tests. |
| selected V3 director, production, schema, feature, schema-history, Round 4, and service suites | PASS, 221/221 Python tests. |
| `npm exec --package=node@22 -- node scripts/write-manifest-schema-digests.mjs --check` | PASS. |
| `npm exec --package=node@22 -- node src/write-registry-hash.mjs --check` | PASS, `sha256:94b9c745f37a77d72dc40120be48dc2e7fc18235923707592e3ba740acdece6c`. |
| `npm exec --package=node@22 -- node src/release-manifest.mjs --check --release-root .` | PASS, renderer build `sha256:df9c90b7abecd4b570b99ed9cf3604c000c8e3c65a48d96d80b7105c9a97c7e8`. |
| `git diff --check` | PASS. |

No push, PR, deployment, service restart, external gate, or Task 6b/7/8/PR-C work was performed.
