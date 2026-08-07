# Pixelle video generation service

This deployment installs a pinned upstream Pixelle-Video checkout as an
isolated, loopback-only service. The repository owns the systemd unit,
configuration renderer, deployment procedure, and tested template overrides;
third-party source remains in its upstream Git repository.

## Runtime layout

- Source and virtual environment: `/opt/huangque/pixelle-video/source`
- Playwright browsers: `/opt/huangque/pixelle-video/browsers`
- Secret config: `/etc/huangque/pixelle-video.yaml` (`root:admin`, `0640`)
- Service: `huangque-pixelle-video.service`
- Address: `http://127.0.0.1:8103`
- Health: `GET /health`

The API is intentionally not routed through nginx. Website integration must
use a Huangque backend proxy with authentication, point charging, rate limits,
and output publication rather than exposing Pixelle directly to browsers.

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
Pixelle-Video to the commit declared in `install.sh`, installs Python 3.11 with
uv, syncs the locked dependencies, overlays the reviewed templates, installs
Chromium for Playwright, and restarts the service.

## Capacity and persistence

This host has limited memory, so RunningHub concurrency and local API task
concurrency are both set to one. Pixelle's task registry is process-local;
clients must treat service restarts as task loss and retain their own job
records. Generated files remain under the runtime output directory until the
website publication/retention worker removes them.
