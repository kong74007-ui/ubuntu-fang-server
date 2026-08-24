#!/usr/bin/env bash
set -euo pipefail

DEPLOY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUNNER_SOURCE="${DEPLOY_ROOT}/deploy/pixelle-video/bin/run-novix-tunnel"
CHECK_SOURCE="${DEPLOY_ROOT}/deploy/pixelle-video/bin/check-novix-openai-proxy"
COMMAND_CHECK_SOURCE="${DEPLOY_ROOT}/deploy/pixelle-video/bin/check-novix-command-denied"
UNIT_SOURCE="${DEPLOY_ROOT}/deploy/systemd/huangque-pixelle-novix-tunnel.service"
RUNNER_TARGET="/usr/local/libexec/huangque/run-pixelle-novix-tunnel"
CHECK_TARGET="/usr/local/libexec/huangque/check-pixelle-novix-openai"
COMMAND_CHECK_TARGET="/usr/local/libexec/huangque/check-pixelle-novix-command-denied"
UNIT_TARGET="/etc/systemd/system/huangque-pixelle-novix-tunnel.service"
ENV_FILE="/etc/huangque/pixelle-novix-tunnel.env"
SERVICE="huangque-pixelle-novix-tunnel.service"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "run this installer as root" >&2
  exit 2
fi
for source in "${RUNNER_SOURCE}" "${CHECK_SOURCE}" "${COMMAND_CHECK_SOURCE}" "${UNIT_SOURCE}"; do
  if [[ ! -s "${source}" || -L "${source}" ]]; then
    echo "missing or unsafe Novix tunnel deployment file: ${source}" >&2
    exit 2
  fi
done
if [[ ! -f "${ENV_FILE}" || -L "${ENV_FILE}" ]]; then
  echo "missing or unsafe ${ENV_FILE}; provision credentials first" >&2
  exit 2
fi
if [[ "$(stat -c '%U:%G' "${ENV_FILE}")" != "root:root" || "$(stat -c '%a' "${ENV_FILE}")" != "600" ]]; then
  echo "${ENV_FILE} must be root:root mode 600" >&2
  exit 2
fi

set -a
source "${ENV_FILE}"
set +a
: "${PIXELLE_NOVIX_SSH_KEY:?missing PIXELLE_NOVIX_SSH_KEY}"
: "${PIXELLE_NOVIX_SSH_KNOWN_HOSTS:?missing PIXELLE_NOVIX_SSH_KNOWN_HOSTS}"
for path in "${PIXELLE_NOVIX_SSH_KEY}" "${PIXELLE_NOVIX_SSH_KNOWN_HOSTS}"; do
  case "${path}" in
    /etc/huangque/pixelle-novix-tunnel/*) ;;
    *) echo "Novix tunnel credential paths must stay under /etc/huangque/pixelle-novix-tunnel" >&2; exit 2 ;;
  esac
  if [[ ! -f "${path}" || -L "${path}" ]]; then
    echo "missing or unsafe Novix tunnel credential: ${path}" >&2
    exit 2
  fi
done
KEY_OWNER="$(stat -c '%U:%G' "${PIXELLE_NOVIX_SSH_KEY}")"
KEY_MODE="$(stat -c '%a' "${PIXELLE_NOVIX_SSH_KEY}")"
KNOWN_HOSTS_OWNER="$(stat -c '%U:%G' "${PIXELLE_NOVIX_SSH_KNOWN_HOSTS}")"
KNOWN_HOSTS_MODE="$(stat -c '%a' "${PIXELLE_NOVIX_SSH_KNOWN_HOSTS}")"
if [[ "${KEY_OWNER}" != "root:admin" && "${KEY_OWNER}" != "admin:admin" ]]; then
  echo "Novix tunnel private key must be owned by root:admin or admin:admin" >&2
  exit 2
fi
if [[ "${KEY_MODE}" != "600" && "${KEY_MODE}" != "640" ]]; then
  echo "Novix tunnel private key mode must be 600 or 640" >&2
  exit 2
fi
if [[ "${KNOWN_HOSTS_OWNER}" != "root:root" && "${KNOWN_HOSTS_OWNER}" != "root:admin" && "${KNOWN_HOSTS_OWNER}" != "admin:admin" ]]; then
  echo "Novix tunnel known_hosts has an unexpected owner" >&2
  exit 2
fi
if [[ "${KNOWN_HOSTS_MODE}" != "600" && "${KNOWN_HOSTS_MODE}" != "640" && "${KNOWN_HOSTS_MODE}" != "644" ]]; then
  echo "Novix tunnel known_hosts mode must be 600, 640, or 644" >&2
  exit 2
fi

install -d -o root -g root -m 0755 /usr/local/libexec/huangque
install -o root -g root -m 0755 "${RUNNER_SOURCE}" "${RUNNER_TARGET}"
install -o root -g root -m 0755 "${CHECK_SOURCE}" "${CHECK_TARGET}"
install -o root -g root -m 0755 "${COMMAND_CHECK_SOURCE}" "${COMMAND_CHECK_TARGET}"
install -o root -g root -m 0644 "${UNIT_SOURCE}" "${UNIT_TARGET}"
systemctl daemon-reload
systemctl enable --now "${SERVICE}"
systemctl is-active --quiet "${SERVICE}"
"${CHECK_TARGET}"
"${COMMAND_CHECK_TARGET}"
echo "${SERVICE} is active and OpenAI connectivity is healthy"
