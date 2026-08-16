#!/usr/bin/env bash
set -euo pipefail

DEPLOY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_PATH="/etc/huangque/mihomo-new.env"
CONFIG_DIR="/home/ubuntu/.config/mihomo-new"
CONFIG_PATH="${CONFIG_DIR}/config.yaml"
RENDERER="${DEPLOY_ROOT}/scripts/render_mihomo_subscription_config.py"
UNIT_SOURCE="${DEPLOY_ROOT}/deploy/systemd/mihomo-new.service"
UNIT_TARGET="/etc/systemd/system/mihomo-new.service"
CANDIDATE_DIR=""
NEXT_CONFIG="${CONFIG_DIR}/.config.yaml.next.$$"
BACKUP_CONFIG="${CONFIG_DIR}/.config.yaml.backup.$$"
CONFIG_REPLACED=0
DEPLOY_SUCCEEDED=0

rollback_config() {
  if [[ -s "${BACKUP_CONFIG}" ]]; then
    mv -f "${BACKUP_CONFIG}" "${CONFIG_PATH}"
  else
    rm -f "${CONFIG_PATH}"
  fi
  systemctl restart mihomo-new.service || true
}

cleanup() {
  local exit_code=$?
  set +e
  if [[ "${exit_code}" -ne 0 && "${CONFIG_REPLACED}" -eq 1 && "${DEPLOY_SUCCEEDED}" -ne 1 ]]; then
    rollback_config
  fi
  rm -f "${NEXT_CONFIG}" "${BACKUP_CONFIG}"
  case "${CANDIDATE_DIR}" in
    "${CONFIG_DIR}"/.candidate.*) rm -rf -- "${CANDIDATE_DIR}" ;;
  esac
  return "${exit_code}"
}

trap cleanup EXIT

if [[ "$(id -u)" -ne 0 ]]; then
  echo "run this installer as root" >&2
  exit 2
fi
if [[ ! -x /usr/local/bin/mihomo ]]; then
  echo "missing /usr/local/bin/mihomo" >&2
  exit 2
fi
if [[ ! -s "${ENV_PATH}" ]]; then
  echo "missing ${ENV_PATH}; copy env.example and set the rotated subscription URL" >&2
  exit 2
fi
if [[ ! -s "${RENDERER}" || ! -s "${UNIT_SOURCE}" ]]; then
  echo "missing Mihomo deployment files" >&2
  exit 2
fi

chown root:root "${ENV_PATH}"
chmod 0600 "${ENV_PATH}"
install -d -o ubuntu -g ubuntu -m 0700 "${CONFIG_DIR}" "${CONFIG_DIR}/providers"
CANDIDATE_DIR="$(mktemp -d "${CONFIG_DIR}/.candidate.XXXXXX")"
install -d -o ubuntu -g ubuntu -m 0700 "${CANDIDATE_DIR}/providers"
python3 "${RENDERER}" --env "${ENV_PATH}" --output "${CANDIDATE_DIR}/config.yaml"
chown ubuntu:ubuntu "${CANDIDATE_DIR}/config.yaml"
chmod 0600 "${CANDIDATE_DIR}/config.yaml"

sudo -u ubuntu /usr/local/bin/mihomo -t -d "${CANDIDATE_DIR}"

if [[ -s "${CONFIG_PATH}" ]]; then
  install -o ubuntu -g ubuntu -m 0600 "${CONFIG_PATH}" "${BACKUP_CONFIG}"
fi
install -o ubuntu -g ubuntu -m 0600 "${CANDIDATE_DIR}/config.yaml" "${NEXT_CONFIG}"
mv -f "${NEXT_CONFIG}" "${CONFIG_PATH}"
CONFIG_REPLACED=1

install -o root -g root -m 0644 "${UNIT_SOURCE}" "${UNIT_TARGET}"
systemctl daemon-reload
systemctl enable mihomo-new.service
systemctl restart mihomo-new.service
systemctl is-active --quiet mihomo-new.service

OPENAI_STATUS=""
for _ in 1 2 3; do
  OPENAI_STATUS="$(curl --proxy http://127.0.0.1:7999 \
    --silent --output /dev/null --write-out '%{http_code}' \
    --connect-timeout 15 --max-time 30 \
    https://api.openai.com/v1/models || true)"
  [[ "${OPENAI_STATUS}" == "401" ]] && break
  sleep 2
done
if [[ "${OPENAI_STATUS}" != "401" ]]; then
  echo "Mihomo cannot reach the OpenAI API through 127.0.0.1:7999 (HTTP ${OPENAI_STATUS:-none})" >&2
  exit 1
fi

DEPLOY_SUCCEEDED=1
