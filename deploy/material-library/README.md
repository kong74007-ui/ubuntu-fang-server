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
`"selection_mode":"random"` to ignore query scores and choose a stable random
candidate from the requested media type/orientation. Random selection remains
deterministic for one seed, excludes `used_sha256`, and skips files whose live
checksum no longer matches the approved index.

`POST /v1/select` selects one unique approved asset per scene using
`exact -> loose -> random`. `GET /v1/assets/{sha256}` downloads a selected asset
after verifying its checksum. Both endpoints require the bearer token.

`GET /health` is unauthenticated and returns counts only. `GET /v1/ping`
requires the bearer token and is used for pre-charge readiness checks.
