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
- Egress: local `mihomo-new.service` proxy on `127.0.0.1:7999`

The API is not exposed as a public browser API. The generated nginx config
provides `/internal/pixelle/` as a backend-only bridge, restricted to the
production website server IP. Website requests still pass through the Huangque
content backend for authentication, point charging, rate limits, and output
publication before that backend calls Pixelle.

## Deploy

The text/storyboard LLM uses the dedicated values below when present. Keep the
key only in the root-readable LLM env file; never place it in this repository:

```bash
PIXELLE_GLM_API_KEY=replace-with-zhipu-key
PIXELLE_GLM_BASE=https://open.bigmodel.cn/api/paas/v4
PIXELLE_GLM_MODEL=glm-4.7-flash
```

These values affect only Pixelle text and storyboard generation. Existing
`OPENAI_API_KEY` / `OPENAI_BASE` values remain available to the separate OpenAI
provider and are also the backward-compatible LLM fallback when the dedicated
GLM key is absent. The legacy `PIXELLE_LLM_MODEL` override applies only to that
OpenAI fallback. A GLM base or model without `PIXELLE_GLM_API_KEY` fails closed.

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
Before preparing a release, it fails closed unless `mihomo-new.service` is
installed, active, and able to reach OpenAI through `127.0.0.1:7999`.
Existing output and task data are migrated to `/var/lib` on first deployment
of this layout and survive later source releases and service redeployments.
Every candidate release is patched, dependency-synced, and compile-checked in
an isolated release directory before the service is stopped. The installer then
confirms the service is inactive before switching the `source` link to that
release. The existing Pixelle systemd unit is backed up before replacement.
Startup or health-check failure restores both the previous source and previous
unit only after the service is again confirmed inactive, then reloads systemd.

## Capacity and persistence

This host has limited memory, so the video API permits exactly one running
video-generation task and up to 20 waiting requests across both synchronous and
asynchronous routes. Further submissions receive HTTP 429 with code
`task_queue_full`. Each accepted video can process up to five RunningHub scenes
in parallel; additional scenes wait for the next available slot. The video-task
capacity itself remains one. Pixelle's task registry and waiting queue are
process-local; clients must treat service
restarts as task loss and retain their own job records. Generated files remain
under the runtime output directory until the website publication/retention
worker removes them. The runtime `output` and `data` paths are links to the
persistent directories under `/var/lib`.

Each image or video scene gets one initial attempt plus at most three retries
with bounded exponential backoff (2 seconds, 4 seconds, then 8 seconds). An
image provider attempt is abandoned locally after 180 seconds and a video
provider attempt after 600 seconds. The initial attempt does not consume retry
budget; all failed scenes in one video task share at most ten additional
retries. When that budget is exhausted, no scene may start another retry.
Completed sibling scenes and narration audio are not generated again. Task
cancellation is never retried. The provider coroutine is cancelled on timeout,
although an upstream provider job may continue remotely when its API does not
expose cancellation.

Parallel frame generation is fail-fast. If any frame fails or the parent task
is cancelled, every running or semaphore-waiting sibling frame is cancelled
and awaited before the original exception is propagated. Cancelled siblings do
not enter provider retry handling or reserve additional task retry budget.

RunningHub status polling is bounded to 15 minutes, with each status request
bounded to 60 seconds. A missing task response (`APIKEY_TASK_NOT_FOUND`) is
terminal and returns immediately instead of entering another polling cycle.
Cancellation is propagated without conversion to a retry.
The direct image endpoint also monitors client disconnects and cancels its
provider wait, so an abandoned HTTP request cannot leave a background poller.

## External narration audio

Trusted backend callers can list Pixelle's sanitized public voice catalog with
`GET /api/voices/public` and upload a synthesized MP3 with
`POST /api/audio-assets`. Uploads require `Content-Type: audio/mpeg` and an
`X-Request-Id`, are limited to 20 MiB, and return an opaque `audio_*` asset ID.
The files are stored privately under
`/var/lib/huangque-pixelle-video/data/external_audio`; they are not served by
the public files router.

A fixed-mode video request may provide one to 20 `narration_segments`. Legacy
callers may keep sending one `audio_asset_id` per segment. For single-line
caption rotation, a segment can instead contain one to 20 ordered `cues`, each
with `text` and an uploaded `audio_asset_id`; cue text must concatenate exactly
to the segment text. Public-voice jobs synthesize one continuous Edge TTS track
and map caption changes to Edge's emitted word-boundary timestamps. If timing
metadata is absent or does not match the narration, rendering falls back to the
existing proportional timing. The segment still generates media only once and
preserves the selected template's title, typography, colors, stroke, shadow,
position, and non-caption motion.
External narration cannot be combined with Pixelle TTS, voice, or
reference-audio parameters. Assets are leased exclusively to one task,
removed on every terminal task path, and removed after 24 hours as crash
recovery if they were uploaded but never used.
Cleanup runs once at service startup and every 15 minutes, and always skips
assets with an active task lease. Startup first reclaims leases left by the
previous process because Pixelle's task registry is in memory.

## Optional talking material scenes

Talking material is opt-in. When enabled, selected storyboard scenes use an
uploaded avatar image and that scene's already-generated narration audio to
create a talking-head visual. Avatar files are private leased assets with a
24-hour avatar TTL for unused-upload crash recovery. Identical avatar content
is uploaded to the provider once and reused by its content fingerprint while
the cache entry remains valid.

Pixelle and the Huangque content service load the same internal token from
`/etc/huangque/pixelle-talking.env`. The default Pixelle endpoint remains the
loopback route `http://127.0.0.1:8096/api/internal/pixelle/talking-clip`.
`PIXELLE_TALKING_ENDPOINT` may select another loopback HTTP port, but external
hosts, HTTPS URLs, URL credentials, query strings, and fragments are rejected.
The bridge enforces a two-slot bridge limit across image conversion, provider
image upload, and talking-video generation.

When the content service and Pixelle run on different hosts, keep the provider
API and MCP OAuth credentials on the content host. Do not copy the MCP refresh
token: it is rotated by the content service and must have one owner. Instead,
forward the content host's existing loopback bridge through the restricted SSH
unit in `deploy/systemd/huangque-pixelle-talking-tunnel.service`.

Provision a dedicated SSH key owned by `admin` on the Pixelle host and authorize
only that key on the content host. The `authorized_keys` entry must use an
independently verified public key and restrict it to the bridge socket:

```text
restrict,port-forwarding,permitopen="127.0.0.1:8096" ssh-ed25519 <public-key> pixelle-talking-tunnel
```

Pin the content host key in a dedicated `known_hosts` file after verifying its
fingerprint out of band. Then create the root-owned, mode `0600` file
`/etc/huangque/pixelle-talking-tunnel.env` without placing it in Git:

```bash
PIXELLE_TALKING_SSH_TARGET=ubuntu@content-host.example
PIXELLE_TALKING_SSH_KEY=/etc/huangque/pixelle-talking-tunnel/id_ed25519
PIXELLE_TALKING_SSH_KNOWN_HOSTS=/etc/huangque/pixelle-talking-tunnel/known_hosts
PIXELLE_TALKING_LOCAL_PORT=8097
PIXELLE_TALKING_REMOTE_HOST=127.0.0.1
PIXELLE_TALKING_REMOTE_PORT=8096
```

Install the reviewed runner and unit, then enable the tunnel:

```bash
sudo install -o root -g root -m 0755 \
  deploy/pixelle-video/bin/run-talking-tunnel \
  /usr/local/libexec/huangque/run-pixelle-talking-tunnel
sudo install -o root -g root -m 0644 \
  deploy/systemd/huangque-pixelle-talking-tunnel.service \
  /etc/systemd/system/huangque-pixelle-talking-tunnel.service
sudo systemctl daemon-reload
sudo systemctl enable --now huangque-pixelle-talking-tunnel.service
```

Add the endpoint below to the existing root-managed
`/etc/huangque/pixelle-talking.env`; keep the existing internal token in that
file unchanged:

```bash
PIXELLE_TALKING_ENDPOINT=http://127.0.0.1:8097/api/internal/pixelle/talking-clip
```

After `systemctl daemon-reload`, start and verify the tunnel before restarting
Pixelle. The tunnel uses strict host-key checking, ignores inherited SSH
configuration, binds only to `127.0.0.1`, and can reach only the content host's
loopback bridge when the matching `authorized_keys` restriction is present.

Each provider attempt uses the existing 15-minute provider deadline. The
Pixelle loopback caller uses a 20-minute client timeout so the provider
deadline and cleanup grace remain authoritative. A scene makes at most three
attempts, using 2-second and 5-second delays only for explicitly retryable,
pre-billing failures. Billed outcomes are never retried. When generation or
final composition fails, ordinary visual fallback retains the scene's already
generated image or video and the parent video task continues with a warning.

Caption cues are grouped toward approximately six seconds, but this is not a
hard duration limit: a semantic cue or final remainder may be shorter or
longer. The talking path concatenates existing cue audio and never invokes a
second TTS operation. Provider audio is stripped from every returned clip;
the original narration and caption timeline remain authoritative for final
composition, including caption end times and the user's selected speech rate.
Provider video is re-encoded with regenerated timestamps and a zero-based PTS
before cue slicing, preventing non-zero MP4 edit timelines from appearing as an
initial blank frame in the final composition.

Video-backed scenes use the transparent `video_default.html` overlay for the
selected output size whenever the chosen frame template is image-only. This
keeps the talking or generated video visible instead of rendering its MP4 path
inside an image element. Multi-segment output uses FFmpeg's concat filter by
default and normalizes the result to H.264/yuv420p video plus 44.1 kHz stereo
AAC audio, preventing incompatible per-segment AAC headers from corrupting the
final narration track.

Linux validation must run the POSIX mode-bit and real symlink security tests
that are skipped on Windows. Local deterministic integration tests stub the
loopback provider boundary and do not make a billable live-provider request.
