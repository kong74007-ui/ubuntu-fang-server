#!/usr/bin/env bash
set -euo pipefail

SOURCE_ROOT="${SOURCE_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
RUNTIME_ROOT="${RUNTIME_ROOT:-/opt/huangque/material-library}"
SOURCE_LINK="${RUNTIME_ROOT}/source"
RELEASES_DIR="${RUNTIME_ROOT}/releases"
ENV_FILE="${ENV_FILE:-/etc/huangque/material-library.env}"
UNIT_PATH="${UNIT_PATH:-/etc/systemd/system/huangque-material-library.service}"
UNIT_SOURCE="${SOURCE_ROOT}/deploy/systemd/huangque-material-library.service"
ROLLBACK_LIB="${SOURCE_ROOT}/deploy/material-library/lib/rollback.sh"
LIBRARY_ROOT="${MATERIAL_LIBRARY_ROOT:-/home/ubuntu/material-libraries/huangque-media}"
SERVICE="huangque-material-library.service"
TEST_MODE="${MATERIAL_LIBRARY_INSTALL_TEST_MODE:-0}"
BACKUP_ROOT="${MATERIAL_LIBRARY_BACKUP_ROOT:-/var/tmp}"
HEALTH_ATTEMPTS="${MATERIAL_LIBRARY_HEALTH_ATTEMPTS:-30}"
HEALTH_SLEEP="${MATERIAL_LIBRARY_HEALTH_SLEEP:-0.5}"

BACKUP=""
RELEASE_DIR=""
NEXT_LINK=""
PREVIOUS_SOURCE=""
LEGACY_SOURCE=""
SOURCE_MUTATED=0
UNIT_MUTATED=0
SERVICE_MUTATED=0
UNIT_EXISTED=0
ENV_CREATED=0
WAS_ACTIVE=0
WAS_ENABLED=0
OLD_MAIN_PID=0
RUNTIME_EXISTED=0
RELEASES_EXISTED=0
SUCCEEDED=0

cleanup() {
  local status=$?
  set +e
  if [[ "${SUCCEEDED}" -ne 1 ]]; then
    if [[ "${SERVICE_MUTATED}" -eq 1 ]]; then
      systemctl stop "${SERVICE}" >/dev/null 2>&1 || true
    fi
    if [[ "${SOURCE_MUTATED}" -eq 1 ]]; then
      material_restore_release \
        "${SOURCE_LINK}" "${PREVIOUS_SOURCE}" "${LEGACY_SOURCE}" \
        "${UNIT_EXISTED}" "${BACKUP}/unit" "${UNIT_PATH}" \
        "${ENV_CREATED}" "${ENV_FILE}" "${RELEASE_DIR}"
      RELEASE_DIR=""
    elif [[ "${ENV_CREATED}" -eq 1 ]]; then
      rm -f "${ENV_FILE}"
    fi
    if [[ "${UNIT_MUTATED}" -eq 1 ]]; then
      systemctl daemon-reload >/dev/null 2>&1 || true
    fi
    if [[ "${SERVICE_MUTATED}" -eq 1 ]]; then
      if [[ "${WAS_ENABLED}" -eq 1 ]]; then
        systemctl enable "${SERVICE}" >/dev/null 2>&1 || true
      else
        systemctl disable "${SERVICE}" >/dev/null 2>&1 || true
      fi
      if [[ "${WAS_ACTIVE}" -eq 1 ]]; then
        systemctl start "${SERVICE}" >/dev/null 2>&1 || true
        for _ in $(seq 1 "${HEALTH_ATTEMPTS}"); do
          curl --fail --silent --max-time 2 http://127.0.0.1:8110/health >/dev/null && break
          sleep "${HEALTH_SLEEP}"
        done
      fi
    fi
    if [[ -n "${RELEASE_DIR}" && -d "${RELEASE_DIR}" ]]; then
      rm -rf "${RELEASE_DIR}"
    fi
    [[ "${RELEASES_EXISTED}" -eq 0 ]] && rmdir "${RELEASES_DIR}" 2>/dev/null || true
    [[ "${RUNTIME_EXISTED}" -eq 0 ]] && rmdir "${RUNTIME_ROOT}" 2>/dev/null || true
  fi
  [[ -n "${NEXT_LINK}" ]] && rm -f "${NEXT_LINK}"
  [[ -n "${BACKUP}" ]] && rm -rf "${BACKUP}"
  return "${status}"
}

# Read-only preflight. No trap is registered and no production resource is
# touched until every input and current-state query below succeeds.
if [[ "${TEST_MODE}" == "1" && "${EUID}" -eq 0 ]]; then
  echo "test mode is forbidden for root" >&2
  exit 2
fi
if [[ "${EUID}" -ne 0 && "${TEST_MODE}" != "1" ]]; then
  echo "run as root" >&2
  exit 1
fi
if [[ "${TEST_MODE}" != "1" && "${RUNTIME_ROOT}" != "/opt/huangque/material-library" ]]; then
  echo "refusing unexpected runtime root" >&2
  exit 2
fi
for source in "${SOURCE_ROOT}/server/material_library.py" \
              "${SOURCE_ROOT}/server/material_library_api.py" \
              "${UNIT_SOURCE}" "${ROLLBACK_LIB}"; do
  if [[ ! -f "${source}" || -L "${source}" || ! -r "${source}" ]]; then
    echo "material library deployment source is missing or unsafe: ${source}" >&2
    exit 1
  fi
done
if [[ ! -f "${LIBRARY_ROOT}/index.jsonl" ]]; then
  echo "material library index is missing: ${LIBRARY_ROOT}/index.jsonl" >&2
  exit 1
fi
source "${ROLLBACK_LIB}"
[[ -d "${RUNTIME_ROOT}" ]] && RUNTIME_EXISTED=1
[[ -d "${RELEASES_DIR}" ]] && RELEASES_EXISTED=1
systemctl is-active --quiet "${SERVICE}" && WAS_ACTIVE=1 || true
systemctl is-enabled --quiet "${SERVICE}" && WAS_ENABLED=1 || true
if [[ "${WAS_ACTIVE}" -eq 1 ]]; then
  OLD_MAIN_PID="$(systemctl show --property=MainPID --value "${SERVICE}")"
  [[ "${OLD_MAIN_PID}" =~ ^[0-9]+$ ]] || OLD_MAIN_PID=0
fi
if [[ "${TEST_MODE}" != "1" && -e "${ENV_FILE}" ]] && {
  [[ ! -f "${ENV_FILE}" ]] || [[ -L "${ENV_FILE}" ]] ||
  [[ "$(stat -c '%U:%G' "${ENV_FILE}")" != "root:root" ]] ||
  [[ "$(stat -c '%a' "${ENV_FILE}")" != "600" ]];
}; then
  echo "${ENV_FILE} must be root:root mode 600" >&2
  exit 2
fi

BACKUP="$(mktemp -d "${BACKUP_ROOT%/}/material-library-install.XXXXXX")"
trap cleanup EXIT
if [[ -f "${UNIT_PATH}" ]]; then
  cp -a "${UNIT_PATH}" "${BACKUP}/unit"
  UNIT_EXISTED=1
fi

install -d -o root -g root -m 0755 "${RUNTIME_ROOT}" "${RELEASES_DIR}"
RELEASE_DIR="$(mktemp -d "${RELEASES_DIR}/release.XXXXXX")"
chmod 0755 "${RELEASE_DIR}"
install -d -o root -g root -m 0755 "${RELEASE_DIR}/server"
install -o root -g root -m 0644 "${SOURCE_ROOT}/server/material_library.py" "${RELEASE_DIR}/server/material_library.py"
install -o root -g root -m 0644 "${SOURCE_ROOT}/server/material_library_api.py" "${RELEASE_DIR}/server/material_library_api.py"
python3 -m py_compile "${RELEASE_DIR}/server/material_library.py" "${RELEASE_DIR}/server/material_library_api.py"
BUILD_ID="$(cd "${RELEASE_DIR}" && sha256sum server/material_library.py server/material_library_api.py | sha256sum | awk '{print $1}')"
printf '%s\n' "${BUILD_ID}" > "${RELEASE_DIR}/BUILD_ID"
PYTHONPATH="${RELEASE_DIR}/server" MATERIAL_LIBRARY_ROOT="${LIBRARY_ROOT}" python3 - <<'PY'
import os
from material_library import MaterialLibrary

if MaterialLibrary(os.environ["MATERIAL_LIBRARY_ROOT"]).stats()["records"] < 1:
    raise SystemExit("material library has no approved records")
PY

install -d -o root -g root -m 0755 "$(dirname "${ENV_FILE}")"
if [[ ! -e "${ENV_FILE}" ]]; then
  umask 077
  token="$(python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(48))
PY
)"
  printf 'MATERIAL_LIBRARY_ROOT=%s\nMATERIAL_LIBRARY_API_TOKEN=%s\n' "${LIBRARY_ROOT}" "${token}" > "${ENV_FILE}"
  ENV_CREATED=1
fi

# From here onward cleanup is allowed to mutate only resources marked below.
SERVICE_MUTATED=1
systemctl stop "${SERVICE}" >/dev/null 2>&1 || true
NEXT_LINK="${RUNTIME_ROOT}/.source.next.$$"
ln -s "${RELEASE_DIR}" "${NEXT_LINK}"
SOURCE_MUTATED=1
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

UNIT_MUTATED=1
install -o root -g root -m 0644 "${UNIT_SOURCE}" "${UNIT_PATH}"
systemctl daemon-reload
systemctl enable "${SERVICE}"
systemctl start "${SERVICE}"
NEW_MAIN_PID="$(systemctl show --property=MainPID --value "${SERVICE}")"
if [[ ! "${NEW_MAIN_PID}" =~ ^[1-9][0-9]*$ ]] ||
   { [[ "${OLD_MAIN_PID}" -gt 0 ]] && [[ "${NEW_MAIN_PID}" -eq "${OLD_MAIN_PID}" ]]; }; then
  echo "material library did not start a new process" >&2
  exit 1
fi

for _ in $(seq 1 "${HEALTH_ATTEMPTS}"); do
  response="$(curl --fail --silent --show-error --max-time 2 http://127.0.0.1:8110/health 2>/dev/null || true)"
  if EXPECTED_BUILD_ID="${BUILD_ID}" python3 -c \
      'import json,os,sys; d=json.load(sys.stdin); raise SystemExit(0 if d.get("ok") is True and d.get("build_id")==os.environ["EXPECTED_BUILD_ID"] and d.get("usage_state_ready") is True else 1)' \
      <<<"${response}"; then
    SUCCEEDED=1
    [[ -n "${LEGACY_SOURCE}" && -d "${LEGACY_SOURCE}" ]] && rm -rf "${LEGACY_SOURCE}"
    echo "${SERVICE} deployed with build ${BUILD_ID} and PID ${NEW_MAIN_PID}"
    exit 0
  fi
  sleep "${HEALTH_SLEEP}"
done
echo "material library build identity check failed; rolling back" >&2
exit 1
