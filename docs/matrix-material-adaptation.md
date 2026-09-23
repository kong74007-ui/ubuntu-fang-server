# Unified API owned-media adaptation

This is an additive `material_adaptation: "auto-v1"` contract on the existing
`/v1/preflight` and `/v1/jobs` renderer APIs. Existing requests remain strict.
The user-facing unified API and account/COS ownership checks live in content-api,
not in this low-level trusted renderer service. No account name alone authorizes
access to arbitrary renderer-side SHA assets.

For auto-v1 jobs:

- Probe the uploaded, SHA-verified sources; skip unreadable sources.
- Fill slots in source order, reusing sources when there are fewer than slots.
- Select long-video windows deterministically across the available duration.
- Loop short videos; freeze sources shorter than 1.5 seconds; turn images into
  finite video clips. Template code continues to own slot geometry and crop.
- Freeze material choices in the existing job material-selection record.
- Download repeated sources once, so concurrent slots do not race on one `.part`.
- Preserve existing HDR transfer/primaries and encoder selection.
- Return `material_adaptation: "auto-v1"` and per-slot adaptation provenance.

Health GPU capability advertises `material_adaptation_contract: "auto-v1"`.
The matching relay change prevents old nodes or legacy pollers from claiming
these jobs. Main-site delivery checks the result echo rather than silently
accepting an old renderer's default behavior.

Deploy the sibling `matrix_material_adaptation.py` alongside the renderer entry
point. Linux and Windows staging scripts include it. Do not replace active nodes
or restart busy workers without separate deployment authorization.

Not included: loop crossfades, image camera moves, per-repeat visual transforms,
changes to authored typography, template timelines, or BGM/voiceover policies.
These remain explicit gaps against the full September 23 Word proposal.
