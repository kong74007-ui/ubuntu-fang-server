#!/usr/bin/env bash
# Provision the ten authorized private fonts for the matrix template video
# service from a delivery directory into the service-private font directory.
#
# Font binaries are never committed to Git. This script writes nothing into
# the repository: every delivered file is matched against
# deploy/matrix-template-video/private-fonts.manifest.example.json by SHA-256,
# copied under its manifest filename, re-verified, and only then is the
# manifest installed as sources.json. The matrix service verifies the same
# hashes again at startup and fails closed on any mismatch.
#
# Usage:
#   sudo bash deploy/matrix-template-video/provision-private-fonts.sh \
#       <delivery-dir> [target-dir]
#
# delivery-dir  directory that contains the ten delivered font files
#               (any filenames; matching is done by content hash)
# target-dir    defaults to /var/lib/huangque-matrix-template/private-fonts
#
# Restart huangque-matrix-template.service after provisioning so the service
# verifies the bundle and exposes the new fingerprint in /health.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$BASH_SOURCE")" && pwd)"
MANIFEST="$SCRIPT_DIR/private-fonts.manifest.example.json"
DELIVERY_DIR="$1"
TARGET_DIR="$2"
if [[ -z "$TARGET_DIR" ]]; then
  TARGET_DIR="/var/lib/huangque-matrix-template/private-fonts"
fi
INSTALLED_LIST="$(mktemp /tmp/matrix-private-fonts-installed.XXXXXX)"
ROWS_FILE="$(mktemp /tmp/matrix-private-fonts-rows.XXXXXX)"
trap 'rm -f "$INSTALLED_LIST" "$ROWS_FILE"' EXIT

usage() {
  echo "usage: $0 <delivery-dir> [target-dir]" >&2
  echo "  delivery-dir: directory with the ten delivered font files (any filenames)" >&2
  echo "  target-dir:   defaults to /var/lib/huangque-matrix-template/private-fonts" >&2
}
if [[ -z "$DELIVERY_DIR" || ! -d "$DELIVERY_DIR" ]]; then
  usage
  exit 2
fi
if [[ ! -f "$MANIFEST" || -L "$MANIFEST" ]]; then
  echo "missing or unsafe manifest: $MANIFEST" >&2
  exit 2
fi
if ! command -v sha256sum >/dev/null 2>&1; then
  echo "sha256sum is required" >&2
  exit 2
fi
PYTHON_BIN=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys' >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v "$candidate")"
    break
  fi
done
if [[ -z "$PYTHON_BIN" ]]; then
  echo "a working python3 (or python) is required to read the manifest" >&2
  exit 2
fi
if [[ -e "$TARGET_DIR" && ( -L "$TARGET_DIR" || ! -d "$TARGET_DIR" ) ]]; then
  echo "target must be a real directory, not a symlink: $TARGET_DIR" >&2
  exit 2
fi
if [[ -e "$TARGET_DIR/sources.json" && -L "$TARGET_DIR/sources.json" ]]; then
  echo "refusing to overwrite symlinked manifest: $TARGET_DIR/sources.json" >&2
  exit 2
fi
mkdir -p "$TARGET_DIR"
if [[ "$(id -u)" -eq 0 ]]; then
  chown root:admin "$TARGET_DIR" 2>/dev/null || true
  chmod 0750 "$TARGET_DIR"
fi

# Emit one manifest row per line: family<TAB>file<TAB>sha256.
"$PYTHON_BIN" - "$MANIFEST" > "$ROWS_FILE" <<'PY'
import json
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(newline="\n")
manifest = json.load(open(sys.argv[1], encoding="utf-8"))
if manifest.get("schema_version") != 1 or not isinstance(manifest.get("fonts"), list):
    raise SystemExit("invalid manifest schema")
for item in manifest["fonts"]:
    print("\t".join([
        str(item.get("family") or ""),
        str(item.get("file") or ""),
        str(item.get("sha256") or "").lower(),
    ]))
PY

# Phase 1: match every manifest entry against the delivery directory by
# SHA-256 and copy it under its manifest filename.
while IFS=$'\t' read -r family file expected; do
  [[ -n "$family" && -n "$file" && -n "$expected" ]] || {
    echo "invalid manifest row" >&2
    exit 2
  }
  found=""
  for candidate in "$DELIVERY_DIR"/*; do
    [[ -f "$candidate" ]] || continue
    actual="$(sha256sum -- "$candidate" | awk '{print $1}')"
    if [[ "$actual" == "$expected" ]]; then
      found="$candidate"
      break
    fi
  done
  if [[ -z "$found" ]]; then
    echo "no delivered file matches sha256 $expected for $family ($file)" >&2
    exit 2
  fi
  rm -f -- "$TARGET_DIR/$file"
  cp -f -- "$found" "$TARGET_DIR/$file"
  staged="$(sha256sum -- "$TARGET_DIR/$file" | awk '{print $1}')"
  if [[ "$staged" != "$expected" ]]; then
    echo "copied font failed verification: $file" >&2
    exit 2
  fi
  chmod 0644 -- "$TARGET_DIR/$file"
  if [[ "$(id -u)" -eq 0 ]]; then
    chown root:admin -- "$TARGET_DIR/$file" 2>/dev/null || true
  fi
  echo "$file" >> "$INSTALLED_LIST"
  echo "installed $file ($family)"
done < "$ROWS_FILE"

# Phase 2: the manifest must have produced exactly the declared entries.
EXPECTED_COUNT="$("$PYTHON_BIN" -c 'import json,sys; print(len(json.load(open(sys.argv[1], encoding="utf-8"))["fonts"]))' "$MANIFEST")"
INSTALLED_COUNT="$(wc -l < "$INSTALLED_LIST")"
if [[ "$INSTALLED_COUNT" -ne "$EXPECTED_COUNT" ]]; then
  echo "installed $INSTALLED_COUNT fonts but manifest declares $EXPECTED_COUNT" >&2
  exit 2
fi

# Phase 3: final integrity sweep against the manifest before publishing it.
while IFS=$'\t' read -r family file expected; do
  actual="$(sha256sum -- "$TARGET_DIR/$file" | awk '{print $1}')"
  if [[ "$actual" != "$expected" ]]; then
    echo "final verification failed: $file" >&2
    exit 2
  fi
done < "$ROWS_FILE"

cp -f -- "$MANIFEST" "$TARGET_DIR/sources.json"
chmod 0640 -- "$TARGET_DIR/sources.json"
if [[ "$(id -u)" -eq 0 ]]; then
  chown root:admin -- "$TARGET_DIR/sources.json" 2>/dev/null || true
fi
echo "private font bundle provisioned into $TARGET_DIR"
echo "restart huangque-matrix-template.service to verify and activate the bundle"
