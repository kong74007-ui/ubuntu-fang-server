# Pixelle video generation service

This deployment installs a pinned upstream Pixelle-Video checkout as an
isolated, loopback-only service. The repository owns the systemd unit,
configuration renderer, deployment procedure, and tested template overrides;
third-party source remains in its upstream Git repository.

## Runtime layout

- Active source link: `/opt/huangque/pixelle-video/source`
- Immutable source releases and virtual environments: `/opt/huangque/pixelle-video/releases`
- Playwright browsers: `/opt/huangque/pixelle-video/browsers`
- Persistent outputs: `/var/lib/huangque-pixelle-video/output`
- Persistent task data: `/var/lib/huangque-pixelle-video/data`
- Secret config: `/etc/huangque/pixelle-video.yaml` (`root:admin`, `0640`)
- Service: `huangque-pixelle-video.service`
- Address: `http://127.0.0.1:8103`
- Health: `GET /health`
- Egress: local `huangque-egress-tunnel.service` proxy on `127.0.0.1:7999`

The API is not exposed as a public browser API. The generated nginx config
provides `/internal/pixelle/` as a backend-only bridge, restricted to the
production website server IP. Website requests still pass through the Huangque
content backend for authentication, point charging, rate limits, and output
publication before that backend calls Pixelle.

## Deploy

Run from a clean checkout of the exact merged commit:

```bash
sudo python3 scripts/render_pixelle_config.py \
  --llm-env /home/ubuntu/content-api/content.env \
  --runninghub-env /etc/huangque/runninghub.env \
  --output /etc/huangque/pixelle-video.yaml
sudo bash deploy/pixelle-video/install.sh
curl --fail --silent http://127.0.0.1:8103/health
```

The installer is idempotent and refuses an unexpected runtime path. It pins
Pixelle-Video to the commit declared in `install.sh`, verifies and applies the
reviewed video-capacity patch, installs Python 3.11 with uv, rewrites only the
locked PyPI package host to the byte-identical Aliyun mirror, syncs dependencies
with the upstream SHA256 checks intact, overlays the reviewed templates,
installs Chromium for Playwright, and restarts the service.
Existing output and task data are migrated to `/var/lib` on first deployment
of this layout and survive later source releases and service redeployments.
Every candidate release is patched, dependency-synced, and compile-checked in
an isolated release directory before the service is stopped. The installer then
confirms the service is inactive before switching the `source` link to that
release. Startup or health-check failure restores the previous source only
after the service is again confirmed inactive.

## Capacity and persistence

This host has limited memory, so the video API permits exactly one running
video-generation task and up to 20 waiting requests across both synchronous and
asynchronous routes. Further submissions receive HTTP 429 with code
`task_queue_full`. RunningHub scene concurrency also remains one. Pixelle's task
registry and waiting queue are process-local; clients must treat service
restarts as task loss and retain their own job records. Generated files remain
under the runtime output directory until the website publication/retention
worker removes them. The runtime `output` and `data` paths are links to the
persistent directories under `/var/lib`.

Each image scene is attempted up to three times with bounded exponential
backoff (2 seconds, then 4 seconds). Retries are scoped to the failed image
provider call, so completed sibling scenes and narration audio are not
generated again. Task cancellation is never retried; after the third failed
attempt, the last provider error remains the task failure reason.

## External narration audio

Trusted backend callers can list Pixelle's sanitized public voice catalog with
`GET /api/voices/public` and upload a synthesized MP3 with
`POST /api/audio-assets`. Uploads require `Content-Type: audio/mpeg` and an
`X-Request-Id`, are limited to 20 MiB, and return an opaque `audio_*` asset ID.
The files are stored privately under
`/var/lib/huangque-pixelle-video/data/external_audio`; they are not served by
the public files router.

A fixed-mode video request may provide one to 20 `narration_segments`, each
containing `text` and an uploaded `audio_asset_id`. External narration cannot
be combined with Pixelle TTS, voice, or reference-audio parameters. Assets are
leased exclusively to one task, removed on every terminal task path, and
removed after 24 hours as crash recovery if they were uploaded but never used.
Cleanup runs once at service startup and every 15 minutes, and always skips
assets with an active task lease. Startup first reclaims leases left by the
previous process because Pixelle's task registry is in memory.
