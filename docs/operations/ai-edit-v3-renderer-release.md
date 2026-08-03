# AI Edit V3 renderer release

The renderer is an immutable, content-addressed release. Build artifacts live under:

```text
/opt/huangque/ai-edit-v3-renderer/releases/<renderer_build_id>/
/opt/huangque/ai-edit-v3-renderer/current -> releases/<renderer_build_id>/
```

`renderer-release.lock.json` freezes Node 22.x, Chromium, FFmpeg, FFprobe, HyperFrames 0.7.84, GSAP 3.15.0, fonts, encoder arguments, locale and timezone. `renderer_build_id` is the SHA-256 of the canonical release inputs excluding the ID and archive attestation. The release archive receives a separate adjacent attestation so its hash is not self-referential.

Release builds may run `npm ci --ignore-scripts`; production rendering must never run a package manager or download plugins, fonts, Chromium or other code. Generate the lock only from the approved binaries and exact Git commit:

```bash
node src/release-manifest.mjs \
  --release-root . \
  --node /approved/node \
  --chromium /approved/chromium \
  --chromium-version 149.0.0.0 \
  --ffmpeg /approved/ffmpeg \
  --ffprobe /approved/ffprobe \
  --git-commit <40-hex>
```

Activation is a separate deployment operation. Build the archive, verify every lock/file hash, write the external archive attestation, extract into a new content-addressed directory, and atomically replace the `current` symlink. Never modify an activated release in place. Rollback switches the symlink to a previously verified release; it does not rebuild dependencies.
