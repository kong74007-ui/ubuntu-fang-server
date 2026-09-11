# Huangque material library service

This deploys a read-only, loopback-only API over an approved `index.jsonl` library.

Root-run material synchronization must not leave atomically replaced metadata
private to root. Install the permission guard once on the material host; it
normalizes only `index.jsonl`, `index.csv`, and `stats.json` to
`ubuntu:ubuntu 0644` after each replacement without changing their contents:

```bash
sudo bash deploy/material-library/install-index-permission-guard.sh
```
It never generates AI media and never returns absolute server paths.

The index accepts the canonical `sha256` and `主体` fields as well as the
existing export aliases `SHA256` and `画面主体`. Conflicting lowercase and
uppercase SHA values fail closed instead of selecting an ambiguous asset.

## Install

Run from a reviewed repository checkout on the material server:

```bash
sudo MATERIAL_LIBRARY_ROOT=/home/ubuntu/material-libraries/huangque-media \
  bash deploy/material-library/install.sh
```

The installer creates `/etc/huangque/material-library.env` with mode `0600` and a
random bearer token. The service listens only on `127.0.0.1:8110`; consumers must
use a separately reviewed restricted tunnel or private transport.

## Restricted generation-server tunnel

Create a dedicated ed25519 key on the generation server. Install only its public
key on the material server:

```bash
sudo MATERIAL_TUNNEL_SOURCE_ADDRESS=<generation-server-source-ip> \
  bash deploy/material-library/install-forwarding-account.sh /path/to/id_ed25519.pub
```

The account cannot run commands, allocate a TTY, forward agents, open remote
listeners, or connect anywhere except `127.0.0.1:8110`. On the generation server,
store the private key and pinned `known_hosts` under
`/etc/huangque/pixelle-material-tunnel/`, then create these root-owned `0600`
files (values are examples, not committed credentials):

```text
# /etc/huangque/pixelle-material-tunnel.env
PIXELLE_MATERIAL_SSH_TARGET=material_tunnel@material-host.example
PIXELLE_MATERIAL_SSH_KEY=/etc/huangque/pixelle-material-tunnel/id_ed25519
PIXELLE_MATERIAL_SSH_KNOWN_HOSTS=/etc/huangque/pixelle-material-tunnel/known_hosts

# /etc/huangque/pixelle-material-library.env
PIXELLE_MATERIAL_LIBRARY_URL=http://127.0.0.1:8111
PIXELLE_MATERIAL_LIBRARY_TOKEN=<same random API token as the material server>
```

Then run `sudo bash deploy/pixelle-video/install-material-library-tunnel.sh`.

## API

`POST /v1/select` defaults to semantic ranking. Callers may send
`"selection_mode":"random"` ignores query scores and chooses a stable random
candidate from all orientations of the requested media type. Random selection
remains deterministic for one seed, excludes `used_sha256`, and skips files
whose live checksum no longer matches the approved index.

`"selection_mode":"round_robin"` uses the same all-orientation candidate pool.
For image and video assets it first rotates the least-recently-used source batch
and scene group, avoids groups already used by the current request or batch when
an alternative exists, then chooses the least-used and least-recently-used file
inside that group. The most recent nine source-scene groups, 50 source videos,
and 200 virtual clips are temporarily deprioritized. A two-use fairness window
keeps newly imported groups from monopolizing every output while still bringing
new files into rotation. If the library does not contain enough distinct groups,
selection falls back to unique files instead of failing. Counts are atomically
persisted outside the read-only approved library at
`/var/lib/huangque-material-library/usage.json`; a state write failure rejects
selection instead of silently losing fairness.

`POST /v1/select` selects one unique approved asset per scene using
`exact -> loose -> random`. `GET /v1/assets/{sha256}` downloads a selected asset
after verifying its checksum. Both endpoints require the bearer token.

`GET /health` is unauthenticated and returns counts only. `GET /v1/ping`
requires the bearer token and is used for pre-charge readiness checks.
Both responses expose `selection_contract_version=2` and
`clip_contract_version=2`; generation-server tunnel readiness rejects older
material-library releases before accepting template jobs.

Round-robin callers may provide a stable `selection_id`. Source/clip counters
and the complete response are protected by a write-ahead receipt stored under
`usage.json.receipts-v1/`. The receipt is atomically persisted before the flat
usage file; startup and request retries reconcile its expected counters using
monotonic maxima before replaying the same result. This closes both a lost HTTP
response and a crash between receipt and usage replacement without double
counting. Reusing a key for a different request returns a conflict.

`usage.json` remains the flat SHA-to-count mapping accepted by the previous
production reader, including its 20,000-record and 4 MiB limits. A successful
upgrade can therefore roll back to the old source against the live state, and
the installer never restores a stale usage snapshot over selections confirmed
while services are switching.

Video scenes may provide `clip_duration_seconds` from `2` through `4`. Ordinary
templates continue to request `2` through `3` seconds; authored fixed templates
may request longer frame-accurate slots up to `4` seconds. Every eligible source
is expanded into deterministic, non-overlapping virtual candidates across its
full duration using a slot span at least as long as the requested clip. Sources
that cannot cover one clip plus the safety margin are excluded. Each candidate
has its own persisted usage key, so later portions participate in selection
immediately instead of waiting for the whole source to cycle. The response freezes `clip_id`,
`clip_start_seconds`, `clip_duration_seconds`, `clip_slot_index`, and
`clip_slot_count` while downloads continue to use the approved source SHA.

Explicit index durations must be finite, non-boolean, non-negative, and at most
30 minutes. One source may expose at most 600 slots, and one selection request
may inspect at most 20,000 unique virtual candidates. Invalid metadata or a cap
breach fails closed before a material is returned.
