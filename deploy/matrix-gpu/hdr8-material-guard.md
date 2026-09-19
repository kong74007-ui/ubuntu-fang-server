# Matrix library color selection guard

This fixes automatic selection of H.264 `yuv420p` files carrying HLG/PQ tags
which the existing GPU renderer correctly refuses. It neither relabels footage
nor upconverts eight-bit data to pretend that native HDR precision was restored.

## Contract

- New Matrix tasks freeze `_video_color_contract: 1` server-side. Their visual
  scenes request `video_color_contract: 1`; BGM scenes do not. Old persisted task
  payloads omit the scene field to preserve pre-upgrade remote receipt hashes.
- The library probes only verified local video candidates, skips incompatible
  HDR candidates, and preserves semantic ranking, exclusions and diversity.
- SDR and native high-depth BT.2020 HDR remain eligible. Missing/failed probes
  fail closed; no healthy candidates produce the existing shortage response.
- Probe results are cached by SHA and file identity. Index/file changes
  invalidate the cache. There is a 32-probe / 15-second per-request budget;
  each subprocess is limited to five seconds and file/pipe protocols.
- The requested contract is part of the existing scene/request receipt hash.
  Completed old selections replay unchanged; an ID reused with a changed
  contract conflicts. No database or receipt schema migration is required.
- User-uploaded materials and already frozen selections are NOT automatically
  replaced. Existing renderer validation remains their fail-closed boundary.

## Rollout order (separate deployment authorization required)

1. Back up and deploy only `server/material_library.py` to the actual material
   library service. Confirm `ffprobe` is installed and its service PATH resolves
   it. Restart only after the existing selection/request drain gate.
2. Require its authenticated health/ping to report `video_color_contract: 1`.
   Use isolated synthetic HLG8 + SDR files for selection validation; do not
   rotate production usage state or create paid tasks merely to test this.
3. Deploy `server/matrix_template_api.py` to each renderer's actual import path,
   preserving five-primary-slot configurations and the HY one-slot standby.
   Wait for zero in-flight work, back up configs/data, and restart only the
   necessary renderer/poller services. Do not alter the immutable GPU runtime.
4. Verify health, hashes, template count, concurrency, logs and routing.

Old library servers ignore new scene hints, so upgrading only renderer nodes
does not fix selection. The library capability check above is a rollout gate.
Rollback consumers first, then library; leave receipts and approved originals
intact. Do not retry refunded jobs or submit the postponed 100-job batch.

## Tests

`python -m unittest tests.test_matrix_material_color_guard -v`

The regression includes real FFmpeg-generated H.264 eight-bit HLG bytes,
HTTP selection/exclusion, native HDR/SDR acceptance, shortage, cache invalidation,
probe failure/budget, and old/new receipt replay. The existing GPU HDR assertion
and output HDR validation are unchanged.
