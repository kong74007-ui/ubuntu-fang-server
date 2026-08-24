#!/usr/bin/env bash
set -euo pipefail

SOURCE_ROOT="${SOURCE_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
ROLLBACK_LIB="${SOURCE_ROOT}/deploy/material-library/lib/rollback.sh"
if [[ ! -f "${ROLLBACK_LIB}" || -L "${ROLLBACK_LIB}" ]]; then
  echo "missing or unsafe rollback library" >&2
  exit 2
fi
source "${ROLLBACK_LIB}"
RUNTIME_ROOT="${RUNTIME_ROOT:-/opt/huangque/material-library}"
SOURCE_LINK="${RUNTIME_ROOT}/source"
RELEASES_DIR="${RUNTIME_ROOT}/releases"
ENV_FILE="${ENV_FILE:-/etc/huangque/material-library.env}"
UNIT_PATH="${UNIT_PATH:-/etc/systemd/system/huangque-material-library.service}"
UNIT_SOURCE="${SOURCE_ROOT}/deploy/systemd/huangque-material-library.service"
LIBRARY_ROOT="${MATERIAL_LIBRARY_ROOT:-/home/ubuntu/material-libraries/huangque-media}"
SERVICE="huangque-material-library.service"
BACKUP="$(mktemp -d /var/tmp/material-library-install.XXXXXX)"
RELEASE_DIR=""
NEXT_LINK=""
PREVIOUS_SOURCE=""
LEGACY_SOURCE=""
SOURCE_SWITCHED=0
UNIT_EXISTED=0
ENV_CREATED=0
WAS_ACTIVE=0
WAS_ENABLED=0
SUCCEEDED=0

cleanup() {
  local status=$?
  set +e
  if [[ "${SUCCEEDED}" -ne 1 ]]; then
    systemctl stop "${SERVICE}" >/dev/null 2>&1 || true
    if [[ "${SOURCE_SWITCHED}" -eq 1 ]]; then
      material_restore_release \
        "${SOURCE_LINK}" "${PREVIOUS_SOURCE}" "${LEGACY_SOURCE}" \
        "${UNIT_EXISTED}" "${BACKUP}/unit" "${UNIT_PATH}" \
        "${ENV_CREATED}" "${ENV_FILE}" "${RELEASE_DIR}"
      RELEASE_DIR=""
    elif [[ "${ENV_CREATED}" -eq 1 ]]; then
      rm -f "${ENV_FILE}"
    fi
    systemctl daemon-reload >/dev/null 2>&1 || true
    if [[ "${WAS_ENABLED}" -eq 1 ]]; then
      systemctl enable "${SERVICE}" >/dev/null 2>&1 || true
    else
      systemctl disable "${SERVICE}" >/dev/null 2>&1 || true
    fi
    if [[ "${WAS_ACTIVE}" -eq 1 ]]; then
      systemctl restart "${SERVICE}" >/dev/null 2>&1 || true
    fi
    if [[ -n "${RELEASE_DIR}" && -d "${RELEASE_DIR}" ]]; then
      rm -rf "${RELEASE_DIR}"
    fi
  fi
  [[ -n "${NEXT_LINK}" ]] && rm -f "${NEXT_LINK}"
  rm -rf "${BACKUP}"
  return "${status}"
}
trap cleanup EXIT

if [[ "${EUID}" -ne 0 ]]; then
  echo "run as root" >&2
  exit 1
fi
if [[ "${RUNTIME_ROOT}" != "/opt/huangque/material-library" ]]; then
  echo "refusing unexpected runtime root" >&2
  exit 2
fi
for source in "${SOURCE_ROOT}/server/material_library.py" \
              "${SOURCE_ROOT}/server/material_library_api.py" "${UNIT_SOURCE}"; do
  if [[ ! -f "${source}" || -L "${source}" || ! -r "${source}" ]]; then
    echo "material library deployment source is missing or unsafe: ${source}" >&2
    exit 1
  fi
done
if [[ ! -f "${LIBRARY_ROOT}/index.jsonl" ]]; then
  echo "material library index is missing: ${LIBRARY_ROOT}/index.jsonl" >&2
  exit 1
fi

systemctl is-active --quiet "${SERVICE}" && WAS_ACTIVE=1 || true
systemctl is-enabled --quiet "${SERVICE}" && WAS_ENABLED=1 || true
if [[ -f "${UNIT_PATH}" ]]; then
  cp -a "${UNIT_PATH}" "${BACKUP}/unit"
  UNIT_EXISTED=1
fi

install -d -o root -g root -m 0755 "${RUNTIME_ROOT}" "${RELEASES_DIR}"
RELEASE_DIR="$(mktemp -d "${RELEASES_DIR}/release.XXXXXX")"
install -d -o root -g root -m 0755 "${RELEASE_DIR}/server"
install -o root -g root -m 0644 "${SOURCE_ROOT}/server/material_library.py" "${RELEASE_DIR}/server/material_library.py"
install -o root -g root -m 0644 "${SOURCE_ROOT}/server/material_library_api.py" "${RELEASE_DIR}/server/material_library_api.py"
python3 -m py_compile "${RELEASE_DIR}/server/material_library.py" "${RELEASE_DIR}/server/material_library_api.py"
PYTHONPATH="${RELEASE_DIR}/server" MATERIAL_LIBRARY_ROOT="${LIBRARY_ROOT}" python3 - <<'PY'
import os
from material_library import MaterialLibrary

stats = MaterialLibrary(os.environ["MATERIAL_LIBRARY_ROOT"]).stats()
if stats["records"] < 1:
    raise SystemExit("material library has no approved records")
PY

install -d -o root -g root -m 0755 "$(dirname "${ENV_FILE}")"
if [[ -e "${ENV_FILE}" ]]; then
  if [[ ! -f "${ENV_FILE}" || -L "${ENV_FILE}" || \
        "$(stat -c '%U:%G' "${ENV_FILE}")" != "root:root" || \
        "$(stat -c '%a' "${ENV_FILE}")" != "600" ]]; then
    echo "${ENV_FILE} must be root:root mode 600" >&2
    exit 2
  fi
else
  umask 077
  token="$(python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(48))
PY
)"
  printf 'MATERIAL_LIBRARY_ROOT=%s\nMATERIAL_LIBRARY_API_TOKEN=%s\n' "${LIBRARY_ROOT}" "${token}" > "${ENV_FILE}"
  ENV_CREATED=1
fi

NEXT_LINK="${RUNTIME_ROOT}/.source.next.$$"
ln -s "${RELEASE_DIR}" "${NEXT_LINK}"
if [[ -L "${SOURCE_LINK}" ]]; then
  PREVIOUS_SOURCE="$(readlink -f "${SOURCE_LINK}")"
  mv -Tf "${NEXT_LINK}" "${SOURCE_LINK}"
elif [[ -e "${SOURCE_LINK}" ]]; then
  LEGACY_SOURCE="${RUNTIME_ROOT}/source.pre-release.$(date +%s)"
  mv "${SOURCE_LINK}" "${LEGACY_SOURCE}"
  mv -T "${NEXT_LINK}" "${SOURCE_LINK}"
else
  mv -T "${NEXT_LINK}" "${SOURCE_LINK}"
fi
NEXT_LINK=""
SOURCE_SWITCHED=1

install -o root -g root -m 0644 "${UNIT_SOURCE}" "${UNIT_PATH}"
systemctl daemon-reload
systemctl enable --now "${SERVICE}"
for _ in $(seq 1 30); do
  if curl --fail --silent --show-error --max-time 2 http://127.0.0.1:8110/health >/dev/null; then
    SUCCEEDED=1
    [[ -n "${LEGACY_SOURCE}" && -d "${LEGACY_SOURCE}" ]] && rm -rf "${LEGACY_SOURCE}"
    echo "${SERVICE} deployed and healthy"
    exit 0
  fi
  sleep 0.5
done
systemctl status "${SERVICE}" --no-pager >&2 || true
echo "material library health check failed; rolling back" >&2
exit 1
