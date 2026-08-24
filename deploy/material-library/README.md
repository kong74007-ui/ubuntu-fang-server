# Huangque material library service

This deploys a read-only, loopback-only API over an approved `index.jsonl` library.
It never generates AI media and never returns absolute server paths.

## Install

Run from a reviewed repository checkout on the material server:

```bash
sudo MATERIAL_LIBRARY_ROOT=/home/ubuntu/material-libraries/huangque-media \
  bash deploy/material-library/install.sh
```

The installer creates `/etc/huangque/material-library.env` with mode `0600` and a
random bearer token. The service listens only on `127.0.0.1:8110`; consumers must
use a separately reviewed restricted tunnel or private transport.

## API

`POST /v1/select` selects one unique approved asset per scene using
`exact -> loose -> random`. `GET /v1/assets/{sha256}` downloads a selected asset
after verifying its checksum. Both endpoints require the bearer token.

`GET /health` is unauthenticated and returns counts only.
