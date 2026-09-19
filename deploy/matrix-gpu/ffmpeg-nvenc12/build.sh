#!/usr/bin/env bash
# Build only. Never installs drivers, changes services, or replaces a live binary.
set -euo pipefail
recipe=$(cd -- "$(dirname -- "$0")" && pwd)
sources=$(realpath -- "${1:?offline source archive directory required}")
work=$(realpath -m -- "${2:?new build directory required}")
jobs=${3:-4}
[[ "$jobs" =~ ^[1-8]$ ]] || { echo 'jobs must be 1..8' >&2; exit 2; }
[[ ! -e "$work" && ! -L "$work" ]] || { echo 'build directory must not exist' >&2; exit 2; }
python3 "$recipe/verify_sources.py" "$sources"
mkdir -p "$work/src" "$work/prefix" "$work/output"
prefix="$work/prefix"
export LC_ALL=C TZ=UTC SOURCE_DATE_EPOCH=1789689600
export PKG_CONFIG_LIBDIR="$prefix/lib/pkgconfig"
export PKG_CONFIG_PATH="$PKG_CONFIG_LIBDIR"
export CC=x86_64-w64-mingw32-gcc CXX=x86_64-w64-mingw32-g++
export AR=x86_64-w64-mingw32-ar RANLIB=x86_64-w64-mingw32-ranlib
extract() { mkdir "$work/src/$1"; tar -xf "$sources/$2" --strip-components=1 -C "$work/src/$1"; }
extract nv-codec-headers nv-codec-headers-12.2.72.0-src.tar.gz
extract zlib zlib-1.3.2-src.tar.gz
extract zimg zimg-3.0.6-src.tar.gz
extract x264 x264-src.tar.gz
extract ffmpeg ffmpeg-8.1.2-release.tar.xz
make -C "$work/src/nv-codec-headers" PREFIX="$prefix" install
grep -Eq '^#define NVENCAPI_MAJOR_VERSION[[:space:]]+12' "$prefix/include/ffnvcodec/nvEncodeAPI.h"
grep -Eq '^#define NVENCAPI_MINOR_VERSION[[:space:]]+2' "$prefix/include/ffnvcodec/nvEncodeAPI.h"
(
 cd "$work/src/zlib"
 make -f win32/Makefile.gcc PREFIX=x86_64-w64-mingw32- -j"$jobs" libz.a
 mkdir -p "$prefix/lib" "$prefix/include"
 cp libz.a "$prefix/lib/"
 cp zlib.h zconf.h "$prefix/include/"
)
(
 cd "$work/src/zimg"
 ./autogen.sh
 ./configure --host=x86_64-w64-mingw32 --prefix="$prefix" --enable-static --disable-shared --disable-test-apps --disable-tests CXXFLAGS='-O2 -static-libgcc -static-libstdc++' LDFLAGS='-static'
 make -j"$jobs"
 make install
)
(
 cd "$work/src/x264"
 ./configure --host=x86_64-w64-mingw32 --cross-prefix=x86_64-w64-mingw32- --prefix="$prefix" --enable-static --disable-cli --disable-opencl --disable-avs --disable-lavf --disable-swscale --extra-ldflags='-static'
 make -j"$jobs"
 make install
)
(
 cd "$work/src/ffmpeg"
 ./configure --target-os=mingw32 --arch=x86_64 --enable-cross-compile --cross-prefix=x86_64-w64-mingw32- --prefix="$work/output" --pkg-config=pkg-config --pkg-config-flags=--static --disable-autodetect --disable-shared --enable-static --disable-debug --disable-doc --disable-ffplay --disable-network --enable-gpl --enable-libx264 --enable-libzimg --enable-zlib --enable-ffnvcodec --enable-nvenc --enable-nvdec --enable-cuvid --extra-cflags="-I$prefix/include" --extra-ldflags="-L$prefix/lib -static -static-libgcc -static-libstdc++" --extra-libs='-lstdc++ -lpthread'
 make -j"$jobs"
 make install
 cp COPYING.GPLv2 LICENSE.md "$work/output/"
)
cp "$recipe/sources.json" "$work/output/sources.json"
cp "$recipe/build.sh" "$work/output/build.sh"
cp "$recipe/verify_sources.py" "$work/output/verify_sources.py"
"$CC" --version > "$work/output/compiler-version.txt"
for exe in ffmpeg ffprobe; do
 x86_64-w64-mingw32-objdump -p "$work/output/bin/$exe.exe" | grep 'DLL Name:' > "$work/output/$exe-imports.txt"
 if grep -Eiq 'libgcc|libstdc|libwinpthread|zlib1|libzimg|x264' "$work/output/$exe-imports.txt"; then
  echo 'Unexpected non-system dynamic dependency' >&2; exit 1
 fi
done
(cd "$work/output" && sha256sum bin/ffmpeg.exe bin/ffprobe.exe sources.json build.sh verify_sources.py > SHA256SUMS)
echo "Build complete: $work/output"
