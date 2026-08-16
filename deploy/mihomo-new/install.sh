#!/usr/bin/env bash
set -euo pipefail

DEPLOY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_PATH="/etc/huangque/mihomo-new.env"
CONFIG_DIR="/home/ubuntu/.config/mihomo-new"
CONFIG_PATH="${CONFIG_DIR}/config.yaml"
PROVIDER_PATH="${CONFIG_DIR}/providers/grayfox.yaml"
RENDERER="${DEPLOY_ROOT}/scripts/render_mihomo_subscription_config.py"
IDLE_CHECKER="${DEPLOY_ROOT}/scripts/check_pixelle_idle.py"
CHECK_SOURCE="${DEPLOY_ROOT}/deploy/mihomo-new/check_openai_proxy.sh"
CHECK_TARGET="/usr/local/libexec/huangque/check-mihomo-openai-proxy"
UNIT_SOURCE="${DEPLOY_ROOT}/deploy/systemd/mihomo-new.service"
UNIT_TARGET="/etc/systemd/system/mihomo-new.service"
PIXELLE_UNIT_SOURCE="${DEPLOY_ROOT}/deploy/systemd/huangque-pixelle-video.service"
PIXELLE_UNIT_TARGET="/etc/systemd/system/huangque-pixelle-video.service"
PIXELLE_SERVICE="huangque-pixelle-video.service"
PROXY_SERVICE="mihomo-new.service"
CANDIDATE_DIR=""
NEXT_CONFIG="${CONFIG_DIR}/.config.yaml.next.$$"
BACKUP_DIR=""
CONFIG_EXISTED=0
UNIT_EXISTED=0
PIXELLE_UNIT_EXISTED=0
CHECK_EXISTED=0
PROVIDER_EXISTED=0
PROXY_WAS_ACTIVE=0
PROXY_WAS_ENABLED=0
PIXELLE_WAS_ACTIVE=0
PIXELLE_STOPPED=0
MANAGED_FILES_BACKED_UP=0
DEPLOY_SUCCEEDED=0

backup_file() {
  local source_path=$1
  local backup_name=$2
  local marker_name=$3
  if [[ -e "${source_path}" || -L "${source_path}" ]]; then
    cp -a -- "${source_path}" "${BACKUP_DIR}/${backup_name}"
    printf -v "${marker_name}" '%s' 1
  fi
}

restore_file() {
  local target_path=$1
  local backup_name=$2
  local existed=$3
  if [[ "${existed}" -eq 1 ]]; then
    cp -a -- "${BACKUP_DIR}/${backup_name}" "${target_path}.restore.$$"
    mv -f "${target_path}.restore.$$" "${target_path}"
  else
    rm -f -- "${target_path}"
  fi
}

backup_managed_files() {
  BACKUP_DIR="$(mktemp -d /var/tmp/huangque-mihomo-new.XXXXXX)"
  chmod 0700 "${BACKUP_DIR}"
  backup_file "${CONFIG_PATH}" config.yaml CONFIG_EXISTED
  backup_file "${UNIT_TARGET}" mihomo-new.service UNIT_EXISTED
  backup_file "${PIXELLE_UNIT_TARGET}" huangque-pixelle-video.service PIXELLE_UNIT_EXISTED
  backup_file "${CHECK_TARGET}" check-mihomo-openai-proxy CHECK_EXISTED
  backup_file "${PROVIDER_PATH}" grayfox.yaml PROVIDER_EXISTED
  MANAGED_FILES_BACKED_UP=1
}

restore_managed_files() {
  restore_file "${CONFIG_PATH}" config.yaml "${CONFIG_EXISTED}" || return 1
  restore_file "${UNIT_TARGET}" mihomo-new.service "${UNIT_EXISTED}" || return 1
  restore_file "${PIXELLE_UNIT_TARGET}" huangque-pixelle-video.service "${PIXELLE_UNIT_EXISTED}" || return 1
  restore_file "${CHECK_TARGET}" check-mihomo-openai-proxy "${CHECK_EXISTED}" || return 1
  restore_file "${PROVIDER_PATH}" grayfox.yaml "${PROVIDER_EXISTED}" || return 1
  systemctl daemon-reload || return 1
}

rollback_config() {
  systemctl stop "${PROXY_SERVICE}" || return 1
  restore_managed_files || return 1
  if [[ "${PROXY_WAS_ENABLED}" -eq 1 ]]; then
    systemctl enable "${PROXY_SERVICE}" || return 1
  else
    systemctl disable "${PROXY_SERVICE}" || return 1
  fi
  if [[ "${PROXY_WAS_ACTIVE}" -eq 1 ]]; then
    systemctl restart "${PROXY_SERVICE}" || return 1
  fi
}

cleanup() {
  local exit_code=$?
  local rollback_ok=1
  set +e
  if [[ "${exit_code}" -ne 0 && "${PIXELLE_WAS_ACTIVE}" -eq 1 && "${PIXELLE_STOPPED}" -eq 1 ]]; then
    systemctl stop "${PIXELLE_SERVICE}" || true
  fi
  if [[ "${exit_code}" -ne 0 && "${MANAGED_FILES_BACKED_UP}" -eq 1 && "${DEPLOY_SUCCEEDED}" -ne 1 ]]; then
    if ! rollback_config; then
      rollback_ok=0
      echo "Mihomo rollback failed; leaving Pixelle stopped" >&2
    fi
  fi
  if [[ "${PIXELLE_WAS_ACTIVE}" -eq 1 && "${PIXELLE_STOPPED}" -eq 1 && "${DEPLOY_SUCCEEDED}" -ne 1 && "${rollback_ok}" -eq 1 ]]; then
    systemctl start "${PIXELLE_SERVICE}" || true
  fi
  rm -f "${NEXT_CONFIG}"
  case "${CANDIDATE_DIR}" in
    "${CONFIG_DIR}"/.candidate.*) rm -rf -- "${CANDIDATE_DIR}" ;;
  esac
  case "${BACKUP_DIR}" in
    /var/tmp/huangque-mihomo-new.*) rm -rf -- "${BACKUP_DIR}" ;;
  esac
  return "${exit_code}"
}

main() {
  if [[ "$(id -u)" -ne 0 ]]; then
    echo "run this installer as root" >&2
    return 2
  fi
  if [[ ! -x /usr/local/bin/mihomo ]]; then
    echo "missing /usr/local/bin/mihomo" >&2
    return 2
  fi
  if [[ ! -s "${ENV_PATH}" ]]; then
    echo "missing ${ENV_PATH}; copy env.example and set the rotated subscription URL" >&2
    return 2
  fi
  if [[ ! -s "${RENDERER}" || ! -s "${IDLE_CHECKER}" || ! -s "${CHECK_SOURCE}" || ! -s "${UNIT_SOURCE}" || ! -s "${PIXELLE_UNIT_SOURCE}" ]]; then
    echo "missing Mihomo deployment files" >&2
    return 2
  fi
  if ! systemctl is-active --quiet "${PIXELLE_SERVICE}"; then
    echo "${PIXELLE_SERVICE} must be active before proxy replacement" >&2
    return 2
  fi
  PIXELLE_WAS_ACTIVE=1
  systemctl is-active --quiet "${PROXY_SERVICE}" && PROXY_WAS_ACTIVE=1
  systemctl is-enabled --quiet "${PROXY_SERVICE}" && PROXY_WAS_ENABLED=1

  trap cleanup EXIT

  chown root:root "${ENV_PATH}"
  chmod 0600 "${ENV_PATH}"
  install -d -o ubuntu -g ubuntu -m 0700 "${CONFIG_DIR}" "${CONFIG_DIR}/providers"
  CANDIDATE_DIR="$(mktemp -d "${CONFIG_DIR}/.candidate.XXXXXX")"
  chown ubuntu:ubuntu "${CANDIDATE_DIR}"
  chmod 0700 "${CANDIDATE_DIR}"
  install -d -o ubuntu -g ubuntu -m 0700 "${CANDIDATE_DIR}/providers"
  python3 "${RENDERER}" --env "${ENV_PATH}" --output "${CANDIDATE_DIR}/config.yaml"
  chown ubuntu:ubuntu "${CANDIDATE_DIR}/config.yaml"
  chmod 0600 "${CANDIDATE_DIR}/config.yaml"
  sudo -u ubuntu /usr/local/bin/mihomo -t -d "${CANDIDATE_DIR}"

  python3 "${IDLE_CHECKER}" --url http://127.0.0.1:8103/api/tasks --timeout 5
  systemctl stop huangque-pixelle-video.service
  if [[ "$(systemctl show --property=ActiveState --value "${PIXELLE_SERVICE}")" != "inactive" ]]; then
    echo "could not confirm ${PIXELLE_SERVICE} is inactive" >&2
    return 1
  fi
  PIXELLE_STOPPED=1

  backup_managed_files
  install -o ubuntu -g ubuntu -m 0600 "${CANDIDATE_DIR}/config.yaml" "${NEXT_CONFIG}"
  mv -f "${NEXT_CONFIG}" "${CONFIG_PATH}"

  install -d -o root -g root -m 0755 "$(dirname "${CHECK_TARGET}")"
  install -o root -g root -m 0755 "${CHECK_SOURCE}" "${CHECK_TARGET}"
  install -o root -g root -m 0644 "${UNIT_SOURCE}" "${UNIT_TARGET}"
  install -o root -g root -m 0644 "${PIXELLE_UNIT_SOURCE}" "${PIXELLE_UNIT_TARGET}"
  systemctl daemon-reload
  systemctl enable "${PROXY_SERVICE}"
  systemctl restart mihomo-new.service
  systemctl is-active --quiet "${PROXY_SERVICE}"

  systemctl start huangque-pixelle-video.service
  for _ in 1 2 3 4 5; do
    if curl --fail --silent --show-error http://127.0.0.1:8103/health >/dev/null; then
      DEPLOY_SUCCEEDED=1
      return 0
    fi
    sleep 2
  done
  echo "Pixelle health check failed after proxy replacement" >&2
  return 1
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
