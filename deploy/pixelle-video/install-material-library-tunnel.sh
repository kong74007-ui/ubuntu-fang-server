#!/usr/bin/env bash
set -euo pipefail

DEPLOY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUNNER_SOURCE="${DEPLOY_ROOT}/deploy/pixelle-video/bin/run-material-library-tunnel"
CHECK_SOURCE="${DEPLOY_ROOT}/deploy/pixelle-video/bin/check-material-library-tunnel"
UNIT_SOURCE="${DEPLOY_ROOT}/deploy/systemd/huangque-pixelle-material-tunnel.service"
RUNNER_TARGET="/usr/local/libexec/huangque/run-pixelle-material-tunnel"
CHECK_TARGET="/usr/local/libexec/huangque/check-pixelle-material-tunnel"
UNIT_TARGET="/etc/systemd/system/huangque-pixelle-material-tunnel.service"
TUNNEL_ENV="/etc/huangque/pixelle-material-tunnel.env"
LIBRARY_ENV="/etc/huangque/pixelle-material-library.env"
SERVICE="huangque-pixelle-material-tunnel.service"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "run this installer as root" >&2
  exit 2
fi
for source in "${RUNNER_SOURCE}" "${CHECK_SOURCE}" "${UNIT_SOURCE}"; do
  if [[ ! -s "${source}" || -L "${source}" ]]; then
    echo "missing or unsafe material tunnel deployment file: ${source}" >&2
    exit 2
  fi
done
for env_file in "${TUNNEL_ENV}" "${LIBRARY_ENV}"; do
  if [[ ! -f "${env_file}" || -L "${env_file}" || \
        "$(stat -c '%U:%G' "${env_file}")" != "root:root" || \
        "$(stat -c '%a' "${env_file}")" != "600" ]]; then
    echo "${env_file} must be a root:root mode 600 regular file" >&2
    exit 2
  fi
done

set -a
source "${TUNNEL_ENV}"
source "${LIBRARY_ENV}"
set +a
: "${PIXELLE_MATERIAL_SSH_KEY:?missing PIXELLE_MATERIAL_SSH_KEY}"
: "${PIXELLE_MATERIAL_SSH_KNOWN_HOSTS:?missing PIXELLE_MATERIAL_SSH_KNOWN_HOSTS}"
: "${PIXELLE_MATERIAL_LIBRARY_TOKEN:?missing PIXELLE_MATERIAL_LIBRARY_TOKEN}"
for path in "${PIXELLE_MATERIAL_SSH_KEY}" "${PIXELLE_MATERIAL_SSH_KNOWN_HOSTS}"; do
  case "${path}" in
    /etc/huangque/pixelle-material-tunnel/*) ;;
    *) echo "material tunnel credentials must stay under /etc/huangque/pixelle-material-tunnel" >&2; exit 2 ;;
  esac
  if [[ ! -f "${path}" || -L "${path}" || ! -r "${path}" ]]; then
    echo "missing or unsafe material tunnel credential: ${path}" >&2
    exit 2
  fi
done
KEY_OWNER="$(stat -c '%U:%G' "${PIXELLE_MATERIAL_SSH_KEY}")"
KEY_MODE="$(stat -c '%a' "${PIXELLE_MATERIAL_SSH_KEY}")"
KNOWN_OWNER="$(stat -c '%U:%G' "${PIXELLE_MATERIAL_SSH_KNOWN_HOSTS}")"
KNOWN_MODE="$(stat -c '%a' "${PIXELLE_MATERIAL_SSH_KNOWN_HOSTS}")"
if [[ "${KEY_OWNER}" != "root:admin" && "${KEY_OWNER}" != "admin:admin" ]] ||
   [[ "${KEY_MODE}" != "600" && "${KEY_MODE}" != "640" ]]; then
  echo "material tunnel private key has unsafe ownership or mode" >&2
  exit 2
fi
if [[ "${KNOWN_OWNER}" != "root:root" && "${KNOWN_OWNER}" != "root:admin" && "${KNOWN_OWNER}" != "admin:admin" ]] ||
   [[ "${KNOWN_MODE}" != "600" && "${KNOWN_MODE}" != "640" && "${KNOWN_MODE}" != "644" ]]; then
  echo "material tunnel known_hosts has unsafe ownership or mode" >&2
  exit 2
fi

install -d -o root -g root -m 0755 /usr/local/libexec/huangque
install -o root -g root -m 0755 "${RUNNER_SOURCE}" "${RUNNER_TARGET}"
install -o root -g root -m 0755 "${CHECK_SOURCE}" "${CHECK_TARGET}"
install -o root -g root -m 0644 "${UNIT_SOURCE}" "${UNIT_TARGET}"
systemctl daemon-reload
systemctl enable --now "${SERVICE}"
systemctl is-active --quiet "${SERVICE}"
"${CHECK_TARGET}"
echo "${SERVICE} is active and the material library is healthy"
