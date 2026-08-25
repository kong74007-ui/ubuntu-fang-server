# Matrix template video service

Internal generation-server API for the `text-media-text` mode from the pinned
`kong74007-ui/script-to-matrix-video` Skill. It binds to `127.0.0.1:8112`, runs
one render at a time, and uses the existing material-library tunnel at
`127.0.0.1:8111`. It never calls an AI image or video provider.

The installer clones and verifies commit
`243d5c168d9ab2d95daf04fef5c5e75924114eb8`, validates the 13-template catalog,
atomically switches releases, and checks the exact runtime build id.

```bash
sudo bash deploy/matrix-template-video/install.sh
```

Secrets are created or loaded from root-owned environment files and are never
committed. Deploy only after the material-library tunnel is healthy.
