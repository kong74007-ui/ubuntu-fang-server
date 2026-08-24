#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -ne 0 ]]; then
  echo "run this installer as root on the production host" >&2
  exit 2
fi
if [[ "$#" -ne 1 ]]; then
  echo "usage: install-novix-production-sshd.sh PUBLIC_KEY_FILE" >&2
  exit 2
fi

DEPLOY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RENDERER="${DEPLOY_ROOT}/deploy/pixelle-video/bin/render-novix-authorized-key"
CONFIG_SOURCE="${DEPLOY_ROOT}/deploy/sshd/60-huangque-pixelle-novix.conf"
CONFIG_TARGET="/etc/ssh/sshd_config.d/60-huangque-pixelle-novix.conf"
PUBLIC_KEY_FILE="$1"
TUNNEL_USER="pixelle_tunnel"
TUNNEL_HOME="/var/lib/huangque-pixelle-tunnel"
SSH_DIR="${TUNNEL_HOME}/.ssh"
AUTHORIZED_KEYS="${SSH_DIR}/authorized_keys"
BACKUP_DIR="$(mktemp -d /var/tmp/pixelle-novix-sshd.XXXXXX)"
CONFIG_EXISTED=0
KEYS_EXISTED=0
USER_CREATED=0
SUCCEEDED=0

cleanup() {
  local status=$?
  set +e
  if [[ "${SUCCEEDED}" -ne 1 ]]; then
    if [[ "${CONFIG_EXISTED}" -eq 1 ]]; then
      install -o root -g root -m 0644 "${BACKUP_DIR}/sshd.conf" "${CONFIG_TARGET}"
    else
      rm -f "${CONFIG_TARGET}"
    fi
    if [[ "${KEYS_EXISTED}" -eq 1 ]]; then
      install -o "${TUNNEL_USER}" -g "${TUNNEL_USER}" -m 0600 \
        "${BACKUP_DIR}/authorized_keys" "${AUTHORIZED_KEYS}"
    elif [[ "${USER_CREATED}" -eq 0 ]]; then
      rm -f "${AUTHORIZED_KEYS}"
    fi
    /usr/sbin/sshd -t || true
    systemctl reload ssh.service || true
    if [[ "${USER_CREATED}" -eq 1 ]]; then
      userdel -r "${TUNNEL_USER}" || true
    fi
  fi
  rm -rf "${BACKUP_DIR}"
  return "${status}"
}
trap cleanup EXIT

for source in "${RENDERER}" "${CONFIG_SOURCE}" "${PUBLIC_KEY_FILE}"; do
  if [[ ! -f "${source}" || -L "${source}" || ! -r "${source}" ]]; then
    echo "missing or unsafe production SSH source: ${source}" >&2
    exit 2
  fi
done

if id "${TUNNEL_USER}" >/dev/null 2>&1; then
  entry="$(getent passwd "${TUNNEL_USER}")"
  IFS=: read -r _ _ _ _ _ actual_home actual_shell <<<"${entry}"
  if [[ "${actual_home}" != "${TUNNEL_HOME}" || "${actual_shell}" != "/usr/bin/false" ]]; then
    echo "existing ${TUNNEL_USER} account has an unexpected home or shell" >&2
    exit 2
  fi
else
  useradd --system --create-home --home-dir "${TUNNEL_HOME}" \
    --shell /usr/bin/false "${TUNNEL_USER}"
  USER_CREATED=1
fi

install -d -o "${TUNNEL_USER}" -g "${TUNNEL_USER}" -m 0700 "${SSH_DIR}"
if [[ -f "${AUTHORIZED_KEYS}" ]]; then
  managed_lines="$(grep -Ev '^[[:space:]]*(#|$)' "${AUTHORIZED_KEYS}" || true)"
  if [[ -n "${managed_lines}" ]] && { [[ "$(wc -l <<<"${managed_lines}")" -ne 1 ]] ||
       ! grep -q ' pixelle-novix-tunnel$' <<<"${managed_lines}"; }; then
    echo "refusing to replace non-managed ${AUTHORIZED_KEYS}" >&2
    exit 2
  fi
  cp -a "${AUTHORIZED_KEYS}" "${BACKUP_DIR}/authorized_keys"
  KEYS_EXISTED=1
fi
"${RENDERER}" "${PUBLIC_KEY_FILE}" > "${BACKUP_DIR}/authorized_keys.next"
install -o "${TUNNEL_USER}" -g "${TUNNEL_USER}" -m 0600 \
  "${BACKUP_DIR}/authorized_keys.next" "${AUTHORIZED_KEYS}"

if [[ -f "${CONFIG_TARGET}" ]]; then
  cp -a "${CONFIG_TARGET}" "${BACKUP_DIR}/sshd.conf"
  CONFIG_EXISTED=1
fi
install -o root -g root -m 0644 "${CONFIG_SOURCE}" "${CONFIG_TARGET}"
/usr/sbin/sshd -t

effective="$(/usr/sbin/sshd -T -C user=${TUNNEL_USER},host=localhost,addr=8.134.216.162)"
grep -Fxq "allowtcpforwarding local" <<<"${effective}"
grep -Fxq "permitopen 127.0.0.1:10810" <<<"${effective}"
grep -Fxq "permitlisten none" <<<"${effective}"
grep -Fxq "forcecommand /usr/bin/false" <<<"${effective}"
grep -Fxq "permittty no" <<<"${effective}"
grep -Fxq "allowagentforwarding no" <<<"${effective}"
grep -Fxq "x11forwarding no" <<<"${effective}"

systemctl reload ssh.service
SUCCEEDED=1
echo "production SSH forwarding-only account installed and sshd reloaded"
