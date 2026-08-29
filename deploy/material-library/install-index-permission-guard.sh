#!/usr/bin/env bash
set -euo pipefail

SOURCE_ROOT="${SOURCE_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
LIBRARY_ROOT="${MATERIAL_LIBRARY_ROOT:-/home/ubuntu/material-libraries/huangque-media}"
OWNER="${MATERIAL_LIBRARY_OWNER:-ubuntu}"
GROUP="${MATERIAL_LIBRARY_GROUP:-ubuntu}"
HELPER_SOURCE="${SOURCE_ROOT}/server/material_library_permissions.py"
SERVICE_SOURCE="${SOURCE_ROOT}/deploy/systemd/huangque-material-library-index-permissions.service"
PATH_SOURCE="${SOURCE_ROOT}/deploy/systemd/huangque-material-library-index-permissions.path"
HELPER_TARGET="${MATERIAL_LIBRARY_PERMISSION_HELPER_TARGET:-/usr/local/libexec/huangque-material-library-index-permissions}"
SERVICE_TARGET="${MATERIAL_LIBRARY_PERMISSION_SERVICE_TARGET:-/etc/systemd/system/huangque-material-library-index-permissions.service}"
PATH_TARGET="${MATERIAL_LIBRARY_PERMISSION_PATH_TARGET:-/etc/systemd/system/huangque-material-library-index-permissions.path}"
SERVICE_UNIT="huangque-material-library-index-permissions.service"
PATH_UNIT="huangque-material-library-index-permissions.path"
TEST_MODE="${MATERIAL_LIBRARY_INSTALL_TEST_MODE:-0}"
BACKUP_ROOT="${MATERIAL_LIBRARY_BACKUP_ROOT:-/var/tmp}"

BACKUP=""
MUTATED=0
SUCCEEDED=0
PATH_WAS_ACTIVE=0
PATH_WAS_ENABLED=0

restore_target() {
  local label="$1" target="$2"
  if [[ -e "${BACKUP}/${label}" ]]; then
    rm -f "${target}"
    cp -a "${BACKUP}/${label}" "${target}"
  else
    rm -f "${target}"
  fi
}

cleanup() {
  local status=$?
  set +e
  if [[ "${SUCCEEDED}" -ne 1 && "${MUTATED}" -eq 1 ]]; then
    systemctl disable --now "${PATH_UNIT}" >/dev/null 2>&1 || true
    restore_target helper "${HELPER_TARGET}"
    restore_target service "${SERVICE_TARGET}"
    restore_target path "${PATH_TARGET}"
    systemctl daemon-reload >/dev/null 2>&1 || true
    [[ "${PATH_WAS_ENABLED}" -eq 1 ]] && systemctl enable "${PATH_UNIT}" >/dev/null 2>&1 || true
    [[ "${PATH_WAS_ACTIVE}" -eq 1 ]] && systemctl start "${PATH_UNIT}" >/dev/null 2>&1 || true
  fi
  [[ -n "${BACKUP}" ]] && rm -rf "${BACKUP}"
  return "${status}"
}

if [[ "${TEST_MODE}" == "1" && "${EUID}" -eq 0 ]]; then
  echo "test mode is forbidden for root" >&2
  exit 2
fi
if [[ "${EUID}" -ne 0 && "${TEST_MODE}" != "1" ]]; then
  echo "run as root" >&2
  exit 1
fi
if [[ "${TEST_MODE}" != "1" && "${LIBRARY_ROOT}" != "/home/ubuntu/material-libraries/huangque-media" ]]; then
  echo "refusing unexpected material library root" >&2
  exit 2
fi
for source in "${HELPER_SOURCE}" "${SERVICE_SOURCE}" "${PATH_SOURCE}"; do
  if [[ ! -f "${source}" || -L "${source}" || ! -r "${source}" ]]; then
    echo "permission guard deployment source is missing or unsafe: ${source}" >&2
    exit 1
  fi
done
for name in index.jsonl index.csv stats.json; do
  target="${LIBRARY_ROOT}/${name}"
  if [[ ! -f "${target}" || -L "${target}" ]]; then
    echo "material metadata is missing or unsafe: ${target}" >&2
    exit 1
  fi
done
for target in "${HELPER_TARGET}" "${SERVICE_TARGET}" "${PATH_TARGET}"; do
  if [[ -e "${target}" && ! -f "${target}" ]] || [[ -L "${target}" ]]; then
    echo "refusing unsafe deployment target: ${target}" >&2
    exit 2
  fi
done

systemctl is-active --quiet "${PATH_UNIT}" && PATH_WAS_ACTIVE=1 || true
systemctl is-enabled --quiet "${PATH_UNIT}" && PATH_WAS_ENABLED=1 || true
BACKUP="$(mktemp -d "${BACKUP_ROOT%/}/material-library-permissions.XXXXXX")"
trap cleanup EXIT
[[ -e "${HELPER_TARGET}" ]] && cp -a "${HELPER_TARGET}" "${BACKUP}/helper"
[[ -e "${SERVICE_TARGET}" ]] && cp -a "${SERVICE_TARGET}" "${BACKUP}/service"
[[ -e "${PATH_TARGET}" ]] && cp -a "${PATH_TARGET}" "${BACKUP}/path"

MUTATED=1
install -d -o root -g root -m 0755 "$(dirname "${HELPER_TARGET}")" "$(dirname "${SERVICE_TARGET}")" "$(dirname "${PATH_TARGET}")"
install -o root -g root -m 0755 "${HELPER_SOURCE}" "${HELPER_TARGET}"
install -o root -g root -m 0644 "${SERVICE_SOURCE}" "${SERVICE_TARGET}"
install -o root -g root -m 0644 "${PATH_SOURCE}" "${PATH_TARGET}"
python3 -m py_compile "${HELPER_TARGET}"
"${HELPER_TARGET}" --root "${LIBRARY_ROOT}" --owner "${OWNER}" --group "${GROUP}" >/dev/null
systemctl daemon-reload
systemctl start "${SERVICE_UNIT}"
systemctl enable --now "${PATH_UNIT}"
systemctl is-active --quiet "${PATH_UNIT}"
for name in index.jsonl index.csv stats.json; do
  actual="$(stat -c '%U:%G:%a' "${LIBRARY_ROOT}/${name}")"
  if [[ "${actual}" != "${OWNER}:${GROUP}:644" ]]; then
    echo "material metadata permissions are incorrect: ${name}=${actual}" >&2
    exit 1
  fi
done

SUCCEEDED=1
echo "${PATH_UNIT} installed; metadata owner=${OWNER}:${GROUP} mode=0644"
