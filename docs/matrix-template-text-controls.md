# Per-job text controls

This is an additive contract, not a rewrite of legacy v05 `overrides` or the
template files. Existing requests with no `text_overrides` retain their payload
and render path. IP12 Agent and the Skill repository are not modified.

`GET /v1/templates` adds `text_controls` on each supported template. It describes
the actual text roles, default styles, preset auto-size ranges, installed font
families, numeric bounds, and `text_revision`. Revision binds the template HTML,
adapter, defaults, and font identity set. Unsupported legacy FFmpeg templates do
not advertise this contract; all 26 current HyperFrames templates are covered.

The main-site companion exposes this through the existing
`matrix-template-controls` action. `POST /v1/preflight` and `POST /v1/jobs` accept
`text_revision` plus `text_overrides: {layer: {properties}}`. Properties are
`font_family`, `font_size_px`, `color`, `offset_x_px`, `offset_y_px`,
`stroke_width_px`, and `stroke_color`. Consult the returned schema; unknown
roles/properties, unavailable fonts, nonfinite values, invalid colors, out-of-range
values, or stale revisions fail explicitly. No clamping or font fallback occurs.

Position means a delta from the authored placement, not an absolute canvas point.
The transform animation is preserved using independent CSS translate. Colors
affect fill/stroke only; authored shadows and backgrounds stay intact. Explicit
size is exact; otherwise the template retains its existing auto-fit rules.

Per-request copies of layout/font metrics avoid mutating shared service state.
New font files are hash-frozen and copied to a separate asset directory under
hashed aliases, never replacing the original font faces. Rendering waits for the
selected fonts and reapplies settings after authored font fitters/each seek.
The native GPU runtime detects added visible text overlap or canvas overflow
relative to the authored state. It preserves intentional animation entrances.

New style requests require GPU `text_controls_contract_version=1`. The API
advertises revision maps in the GPU health contract. The paired relay/poller
change requires protocol-v2 delivery and matching revisions (including fonts),
so unupgraded nodes cannot silently render default styles. Replay of an already
accepted request uses its saved style contract, not newly published defaults.
Results echo the style revision and overrides; the site verifies them before
delivery. This does not change legacy v05 preview behavior. New text controls are
generation-only and cannot be mixed with legacy overrides or preview IDs.

## Packaging

Install `deploy/requirements-matrix-text-controls.txt` in the service Python
environment. Linux installer and Windows staging fail before switching services
when parsers are missing. Ship the new `matrix_text_controls.py` sibling module,
matching API/GPU helper/runtime, and the paired main-site relay/poller changes.
Keep every active node's template/font bundle consistent. Switch idle nodes only;
no restart/deployment or paid provider call was performed by this change.

The separately merged `health-team-hook` has an existing native-video filter
limitation. Its text parameters are supported, but this feature does not remove
or alter that template's authored media filter. Do not treat a successful text
style audit as evidence that that unrelated render limitation is fixed.
