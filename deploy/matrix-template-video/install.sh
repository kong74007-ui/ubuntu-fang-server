#!/usr/bin/env bash
set -euo pipefail

UPSTREAM_URL="https://github.com/kong74007-ui/script-to-matrix-video.git"
UPSTREAM_COMMIT="243d5c168d9ab2d95daf04fef5c5e75924114eb8"
LAYOUT_PATCH_SHA256="3b1e68d990f00a578fcbb9c0078ce5ca6fe87c7a7cc8190735323740e6666377"
DEPLOY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUNTIME_ROOT="/opt/huangque/matrix-template-video"
SOURCE_LINK="${RUNTIME_ROOT}/source"
RELEASES_DIR="${RUNTIME_ROOT}/releases"
STATE_ROOT="/var/lib/huangque-matrix-template"
PRIVATE_FONT_ROOT="${STATE_ROOT}/private-fonts"
ENV_FILE="/etc/huangque/matrix-template.env"
UNIT_SOURCE="${DEPLOY_ROOT}/deploy/systemd/huangque-matrix-template.service"
UNIT_TARGET="/etc/systemd/system/huangque-matrix-template.service"
API_SOURCE="${DEPLOY_ROOT}/server/matrix_template_api.py"
LAYOUT_PATCH_SOURCE="${DEPLOY_ROOT}/deploy/matrix-template-video/private-domain-layouts.patch"
ROLLBACK_LIB="${DEPLOY_ROOT}/deploy/material-library/lib/rollback.sh"
SERVICE="huangque-matrix-template.service"

BACKUP=""
RELEASE=""
NEXT_LINK=""
PREVIOUS_SOURCE=""
LEGACY_SOURCE=""
SOURCE_MUTATED=0
UNIT_MUTATED=0
SERVICE_MUTATED=0
ENV_CREATED=0
UNIT_EXISTED=0
WAS_ACTIVE=0
WAS_ENABLED=0
OLD_PID=0
SUCCEEDED=0

cleanup() {
  local status=$?
  set +e
  if [[ "${SUCCEEDED}" -ne 1 ]]; then
    [[ "${SERVICE_MUTATED}" -eq 1 ]] && systemctl stop "${SERVICE}" >/dev/null 2>&1 || true
    if [[ "${SOURCE_MUTATED}" -eq 1 ]]; then
      material_restore_release \
        "${SOURCE_LINK}" "${PREVIOUS_SOURCE}" "${LEGACY_SOURCE}" \
        "${UNIT_EXISTED}" "${BACKUP}/unit" "${UNIT_TARGET}" \
        "${ENV_CREATED}" "${ENV_FILE}" "${RELEASE}"
      RELEASE=""
    elif [[ "${ENV_CREATED}" -eq 1 ]]; then
      rm -f "${ENV_FILE}"
    fi
    [[ "${UNIT_MUTATED}" -eq 1 ]] && systemctl daemon-reload >/dev/null 2>&1 || true
    if [[ "${SERVICE_MUTATED}" -eq 1 ]]; then
      if [[ "${WAS_ENABLED}" -eq 1 ]]; then systemctl enable "${SERVICE}" >/dev/null 2>&1 || true
      else systemctl disable "${SERVICE}" >/dev/null 2>&1 || true; fi
      [[ "${WAS_ACTIVE}" -eq 1 ]] && systemctl start "${SERVICE}" >/dev/null 2>&1 || true
    fi
    [[ -n "${RELEASE}" && -d "${RELEASE}" ]] && rm -rf "${RELEASE}"
  fi
  [[ -n "${NEXT_LINK}" ]] && rm -f "${NEXT_LINK}"
  [[ -n "${BACKUP}" ]] && rm -rf "${BACKUP}"
  return "${status}"
}

if [[ "$(id -u)" -ne 0 ]]; then echo "run as root" >&2; exit 2; fi
for source in "${UNIT_SOURCE}" "${API_SOURCE}" "${LAYOUT_PATCH_SOURCE}" "${ROLLBACK_LIB}"; do
  if [[ ! -f "${source}" || -L "${source}" || ! -r "${source}" ]]; then
    echo "missing or unsafe deployment source: ${source}" >&2; exit 2
  fi
done
if [[ ! -f /etc/huangque/pixelle-material-library.env || -L /etc/huangque/pixelle-material-library.env ]]; then
  echo "material library client environment is missing" >&2; exit 2
fi
source "${ROLLBACK_LIB}"
systemctl is-active --quiet "${SERVICE}" && WAS_ACTIVE=1 || true
systemctl is-enabled --quiet "${SERVICE}" && WAS_ENABLED=1 || true
if [[ "${WAS_ACTIVE}" -eq 1 ]]; then
  OLD_PID="$(systemctl show --property=MainPID --value "${SERVICE}")"
  [[ "${OLD_PID}" =~ ^[0-9]+$ ]] || OLD_PID=0
fi
if [[ -e "${ENV_FILE}" ]] && {
  [[ ! -f "${ENV_FILE}" ]] || [[ -L "${ENV_FILE}" ]] ||
  [[ "$(stat -c '%U:%G' "${ENV_FILE}")" != "root:admin" ]] ||
  [[ "$(stat -c '%a' "${ENV_FILE}")" != "640" ]];
}; then
  echo "${ENV_FILE} must be root:admin mode 640" >&2; exit 2
fi

BACKUP="$(mktemp -d /var/tmp/matrix-template-install.XXXXXX)"
trap cleanup EXIT
if [[ -f "${UNIT_TARGET}" ]]; then cp -a "${UNIT_TARGET}" "${BACKUP}/unit"; UNIT_EXISTED=1; fi
install -d -o root -g root -m 0755 "${RUNTIME_ROOT}" "${RELEASES_DIR}"
install -d -o admin -g admin -m 0750 "${STATE_ROOT}"
install -d -o root -g admin -m 0750 "${PRIVATE_FONT_ROOT}"
RELEASE="$(mktemp -d "${RELEASES_DIR}/${UPSTREAM_COMMIT}.XXXXXX")"
chmod 0755 "${RELEASE}"
git clone --no-checkout "${UPSTREAM_URL}" "${RELEASE}/upstream"
git -C "${RELEASE}/upstream" checkout --detach "${UPSTREAM_COMMIT}"
git -C "${RELEASE}/upstream" reset --hard "${UPSTREAM_COMMIT}"
git -C "${RELEASE}/upstream" clean -fdx
if [[ "$(git -C "${RELEASE}/upstream" rev-parse HEAD)" != "${UPSTREAM_COMMIT}" ]]; then
  echo "upstream commit mismatch" >&2; exit 1
fi
if [[ "$(sha256sum "${LAYOUT_PATCH_SOURCE}" | awk '{print $1}')" != "${LAYOUT_PATCH_SHA256}" ]]; then
  echo "private-domain layout patch hash mismatch" >&2; exit 1
fi
git -C "${RELEASE}/upstream" apply --check --directory=script-to-matrix-video "${LAYOUT_PATCH_SOURCE}"
git -C "${RELEASE}/upstream" apply --directory=script-to-matrix-video "${LAYOUT_PATCH_SOURCE}"
install -o root -g root -m 0644 "${API_SOURCE}" "${RELEASE}/api.py"
SKILL_ROOT="${RELEASE}/upstream/script-to-matrix-video"
python3 -m py_compile "${RELEASE}/api.py" "${SKILL_ROOT}/scripts/render_video.py"
python3 "${SKILL_ROOT}/scripts/check_environment.py"
python3 "${SKILL_ROOT}/scripts/test_template_catalog.py"
python3 "${SKILL_ROOT}/scripts/test_private_domain_layouts.py"
BUILD_ID="$(printf '%s\n' "${UPSTREAM_COMMIT}" "${LAYOUT_PATCH_SHA256}" "$(sha256sum "${RELEASE}/api.py" | awk '{print $1}')" | sha256sum | awk '{print $1}')"
printf '%s\n' "${BUILD_ID}" > "${RELEASE}/BUILD_ID"

if [[ ! -e "${ENV_FILE}" ]]; then
  token="$(python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(48))
PY
)"
  umask 027
  cat > "${ENV_FILE}" <<EOF
MATRIX_TEMPLATE_API_TOKEN=${token}
MATRIX_TEMPLATE_DATA_ROOT=${STATE_ROOT}
MATRIX_TEMPLATE_SKILL_ROOT=${SOURCE_LINK}/upstream/script-to-matrix-video
MATRIX_TEMPLATE_PYTHON=/usr/bin/python3
MATRIX_TEMPLATE_PRIVATE_FONT_ROOT=${PRIVATE_FONT_ROOT}
MATRIX_TEMPLATE_RETENTION_SECONDS=259200
MATRIX_TEMPLATE_DELIVERY_GRACE_SECONDS=3600
MATRIX_TEMPLATE_CLEANUP_INTERVAL_SECONDS=900
MATRIX_TEMPLATE_CLEANUP_BATCH_SIZE=10
MATRIX_TEMPLATE_DISK_HIGH_WATER_PERCENT=95
EOF
  chown root:admin "${ENV_FILE}"
  chmod 0640 "${ENV_FILE}"
  ENV_CREATED=1
fi

SERVICE_MUTATED=1
systemctl stop "${SERVICE}" >/dev/null 2>&1 || true
NEXT_LINK="${RUNTIME_ROOT}/.source.next.$$"
ln -s "${RELEASE}" "${NEXT_LINK}"
SOURCE_MUTATED=1
if [[ -L "${SOURCE_LINK}" ]]; then
  PREVIOUS_SOURCE="$(readlink -f "${SOURCE_LINK}")"; mv -Tf "${NEXT_LINK}" "${SOURCE_LINK}"
elif [[ -e "${SOURCE_LINK}" ]]; then
  LEGACY_SOURCE="${RUNTIME_ROOT}/source.pre-release.$(date +%s)"; mv "${SOURCE_LINK}" "${LEGACY_SOURCE}"; mv -T "${NEXT_LINK}" "${SOURCE_LINK}"
else mv -T "${NEXT_LINK}" "${SOURCE_LINK}"; fi
NEXT_LINK=""

UNIT_MUTATED=1
install -o root -g root -m 0644 "${UNIT_SOURCE}" "${UNIT_TARGET}"
systemctl daemon-reload
systemctl enable "${SERVICE}"
systemctl start "${SERVICE}"
NEW_PID="$(systemctl show --property=MainPID --value "${SERVICE}")"
if [[ ! "${NEW_PID}" =~ ^[1-9][0-9]*$ ]] || { [[ "${OLD_PID}" -gt 0 ]] && [[ "${NEW_PID}" -eq "${OLD_PID}" ]]; }; then
  echo "matrix template service did not start a new process" >&2; exit 1
fi
for _ in $(seq 1 30); do
  response="$(curl --fail --silent --max-time 2 http://127.0.0.1:8112/health 2>/dev/null || true)"
  if EXPECTED_BUILD_ID="${BUILD_ID}" python3 -c \
      'import json,os,sys; d=json.load(sys.stdin); raise SystemExit(0 if d.get("ok") is True and d.get("build_id")==os.environ["EXPECTED_BUILD_ID"] and d.get("templates")==15 else 1)' \
      <<<"${response}"; then
    SUCCEEDED=1
    [[ -n "${LEGACY_SOURCE}" && -d "${LEGACY_SOURCE}" ]] && rm -rf "${LEGACY_SOURCE}"
    echo "${SERVICE} deployed with build ${BUILD_ID} and PID ${NEW_PID}"
    exit 0
  fi
  sleep 0.5
done
echo "matrix template service health check failed; rolling back" >&2
exit 1
