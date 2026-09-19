# Windows FFmpeg with NVENC 12.2

This optional node-local toolchain keeps FFmpeg 8.1.2 while compiling its NVIDIA
integration against nv-codec-headers 12.2.72.0. An FFmpeg release number alone
does not establish the driver's required NVENC API: a prebuilt 7.1.1 executable
can still have been compiled with 13.0 headers.

It does **not** install, modify, force-match or weaken signature checking for a
display driver. It does not claim to patch vulnerabilities in an old display
driver. Operators should still obtain a supported vendor driver when possible.
GPU composition, HEVC Main10, HDR metadata and the no-CPU-fallback policy remain
mandatory. The normal renderer source/lock fingerprint is unchanged; record the
separate toolchain binary hashes alongside it.

## Build

Use an isolated Linux/WSL build directory backed by a disk with at least 5 GiB
free. The recipe does not install system packages or access the network. Required
tools are Python3, Bash, GNU make, pkg-config, autoconf, automake, libtool, nasm,
native build tools and the x86_64-w64-mingw32 GCC/G++ POSIX cross toolchain.

Download the five HTTPS archives listed in `sources.json` to a private staging
directory. Their exact SHA256 digests are verified before extraction. Keep the
source archives, recipe and build log for provenance and applicable GPL source
requirements; do not commit large binaries or distribute a binary without its
license/source materials.

```sh
python3 verify_sources.py /absolute/source-archives
nice -n 15 bash build.sh /absolute/source-archives /absolute/new-build-dir 4
```

The build directory must not exist. Output goes into `new-build-dir/output` and
includes static Windows ffmpeg.exe/ffprobe.exe, import lists, source pins,
compiler version and SHA256SUMS. Network protocols are disabled in this build:
material downloads remain the authenticated Python application's responsibility.
zscale/zlib, native media decoders, AAC, x264 and NVIDIA encoders are included;
libx265 is intentionally absent because this deployment is GPU-required only.
Do not reuse it as the legacy software-HDR toolchain.

## Activation gates

1. Verify the output SHA256SUMS and absence of non-system runtime DLL imports.
2. On the target Windows node, use an isolated toolchain directory and put its
   `bin` first in that process's PATH. Do not modify machine-wide PATH.
3. Verify `ffmpeg -version`, zscale/PNG/AAC, H.264 NVENC and HEVC Main10 encoding.
   Run the existing Matrix GPU hardware probe with the actual service identity.
4. Render an approved complete template in an isolated no-billing API instance.
   Verify real output duration/frame count, dimensions, HDR/pixel format/audio,
   absence of decoding errors, and representative visible frames.
5. Require no in-flight jobs; preserve DB/history/uploads/finished outputs and
   private credentials. Back up configuration before switching. Stop/disable the
   previous poller to prevent two claimants using the same node identity.
6. Check relay capability, delayed task/service state and logs. If another actor
   re-enables the old poller, stop activation and resolve ownership first.

Rebooting is not part of this build. Any reboot or driver change requires a
separate authorization. Rollback restores the previous node configuration/data
and starts only one renderer/poller pair, without regenerating or billing jobs.
