# Three additional website templates

Pinned Skill source: `981ecf0584d963c6e26a2f9d5cfa7fd6985758d2`.
The Skill repository itself is unchanged. The website service stages its own
copies and exposes 26 templates after the full rollout, including the independently
merged health-team-hook template.

| ID | Timeline | Account sources |
| --- | --- | --- |
| inset-flip-whip | 443 frames / 14.766667s | 7 distinct videos, authored reuse and source offsets |
| fixed-opening-whip | 519 frames / 17.3s | 2 pinned opening clips plus 4 distinct uploaded videos |
| bilingual-stagger-salon | measured narration plus 0.6s, rounded up to 30fps | at least 3 distinct videos, increasing with duration |

The two fixed templates use existing AI semantic copy layout and website safe
areas. Fixed opening subtitles and audio remain bundled; template copy stays
hidden through frame 96. Opening clips and bound audio are hash-checked. The
website adapter removes the fixed-opening template's optional SDR highlight
grading to preserve native source colors. Inset's first 1.1s intentionally uses
its original desaturated backplate; this is prepared at native bit depth, not
by relabeling HDR as SDR. Curved SVG masks, the animated inset border and timed
text visibility are supported by the native GPU compositor.

## Bilingual rules

The user explicitly chose the original bilingual rules: no additional persistent
bottom CTA, no BGM, original 128/80px phase titles and 82/38px Chinese/English
captions. Website `top_text` means main titles; `bottom_text` means subtitles in
this template. Long copy is split at the existing AI semantic boundaries into
sequential title phases, never squeezed horizontally. Other templates retain
their previous field meaning and voice/BGM behavior.

The main-site companion must synthesize the selected owned/public voice, request
real word timestamps, split and translate the unchanged narration, then persist
the timeline before provider submission. Missing timestamps, segment-level
estimates, omitted words, invalid intervals and changed audio fingerprints fail
closed. Retry reuses the persisted plan and existing TTS cache. The renderer
does not accept a bilingual job without a validated plan. Narration is limited
to 60 seconds, captions to 10 Chinese code points / 48 English characters per
cue, and title/caption overflow is rejected rather than silently truncating.
The main site copies the finished video stream when adding narration and pads
the audio tail; it does not convert HDR to SDR.

## Required rollout order

1. Preserve main's account-upload-only visual policy. The shared library remains
   BGM-only and needs no changes from this PR. Fixed opening needs at most 5.1s
   per source, plus the existing 0.1s validation margin. Upload sufficient owned
   videos before using these modes; no shared/public visual fallback is added.
2. Deploy the main-site companion. It must accept the narration catalog contract,
   force voiceover/no BGM for the bilingual template, and preserve its frozen
   measured timeline. ASR uses the existing `video_compose_asr` configuration;
   translation uses `matrix_template_semantics`. Both must be configured.
3. Stage matching generator Python files, three template directories,
   HyperFrames 0.8.38, and the updated native GPU runtime on each worker. On
   Windows, `deploy/matrix-template-video/stage-motion-v3.ps1` stages a new release
   and prints configuration without touching the running service. On Linux the
   main installer includes these resources. Keep other template runtime pins.
4. Wait for each worker's active jobs before switching it. Enable
   `MATRIX_TEMPLATE_MOTION_V3_ROOT` and `MATRIX_TEMPLATE_MOTION_V3_HYPERFRAMES_CLI`.
   GPU-required workers must advertise the three new IDs; old nodes must not
   claim them. The staged API must include the sibling matrix_motion_v3.py module.
5. Confirm the 26-ID catalog, GPU routing, real owned-voice alignment, downloads
   and refund behavior before general use. Do not regenerate completed jobs.

This PR does not deploy, restart services, modify live jobs, or call paid TTS,
ASR or translation providers. Local renders use approved local footage and
frozen test cue timings. Provider transport/recovery is covered with mocks;
production credentials and provider availability still require rollout checks.

## Local validation scope

All three additions render through account-asset validation and the service's
GPU entry point. Twenty-two previous prepared template fixtures also pass.
The separately merged health-team-hook was additionally probed but its authored
CSS video filter (saturate/contrast/brightness/blur) is rejected by the existing
GPU runtime. This PR preserves that template and does not claim all 26 templates
are GPU-ready; its filter adapter needs a separate fix before a GPU-only rollout.
