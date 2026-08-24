#!/usr/bin/env bash
set -euo pipefail

if [[ "$(id -u)" -ne 0 || "$#" -ne 1 ]]; then
  echo "usage: sudo install-forwarding-account.sh PUBLIC_KEY_FILE" >&2
  exit 2
fi

DEPLOY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PUBLIC_KEY_FILE="$1"
RENDERER="${DEPLOY_ROOT}/deploy/pixelle-video/bin/render-material-authorized-key"
CHECKER="${DEPLOY_ROOT}/deploy/pixelle-video/bin/check-material-library-account"
CONFIG_SOURCE="${DEPLOY_ROOT}/deploy/sshd/61-huangque-material-library.conf"
CONFIG_TARGET="/etc/ssh/sshd_config.d/61-huangque-material-library.conf"
TUNNEL_USER="material_tunnel"
TUNNEL_HOME="/var/lib/huangque-material-tunnel"
SSH_DIR="${TUNNEL_HOME}/.ssh"
AUTHORIZED_KEYS="${SSH_DIR}/authorized_keys"
BACKUP="$(mktemp -d /var/tmp/material-library-sshd.XXXXXX)"
USER_CREATED=0
CONFIG_EXISTED=0
KEYS_EXISTED=0
PASSWORD_CHANGED=0
ORIGINAL_PASSWORD_HASH=""
SUCCEEDED=0

cleanup() {
  local status=$?
  set +e
  if [[ "${SUCCEEDED}" -ne 1 ]]; then
    if [[ "${CONFIG_EXISTED}" -eq 1 ]]; then
      install -o root -g root -m 0644 "${BACKUP}/sshd.conf" "${CONFIG_TARGET}"
    else
      rm -f "${CONFIG_TARGET}"
    fi
    if [[ "${KEYS_EXISTED}" -eq 1 ]]; then
      install -o "${TUNNEL_USER}" -g "${TUNNEL_USER}" -m 0600 \
        "${BACKUP}/authorized_keys" "${AUTHORIZED_KEYS}"
    fi
    if [[ "${USER_CREATED}" -eq 0 && "${PASSWORD_CHANGED}" -eq 1 ]]; then
      printf '%s:%s\n' "${TUNNEL_USER}" "${ORIGINAL_PASSWORD_HASH}" | chpasswd -e || true
    fi
    /usr/sbin/sshd -t || true
    systemctl reload ssh.service || true
    if [[ "${USER_CREATED}" -eq 1 ]]; then
      userdel -r "${TUNNEL_USER}" || true
    fi
  fi
  rm -rf "${BACKUP}"
  return "${status}"
}
trap cleanup EXIT

for source in "${PUBLIC_KEY_FILE}" "${RENDERER}" "${CHECKER}" "${CONFIG_SOURCE}"; do
  if [[ ! -f "${source}" || -L "${source}" || ! -r "${source}" ]]; then
    echo "missing or unsafe forwarding-account source: ${source}" >&2
    exit 2
  fi
done

if id "${TUNNEL_USER}" >/dev/null 2>&1; then
  entry="$(getent passwd "${TUNNEL_USER}")"
  IFS=: read -r _ _ _ _ _ actual_home actual_shell <<<"${entry}"
  if [[ "${actual_home}" != "${TUNNEL_HOME}" || "${actual_shell}" != "/usr/bin/false" ]]; then
    echo "existing ${TUNNEL_USER} account has unexpected properties" >&2
    exit 2
  fi
else
  useradd --system --create-home --home-dir "${TUNNEL_HOME}" \
    --shell /usr/bin/false "${TUNNEL_USER}"
  USER_CREATED=1
fi

if [[ "$(passwd -S "${TUNNEL_USER}" | awk '{print $2}')" != "P" ]]; then
  ORIGINAL_PASSWORD_HASH="$(getent shadow "${TUNNEL_USER}" | cut -d: -f2)"
  random_hash="$(openssl rand -base64 48 | openssl passwd -6 -stdin)"
  printf '%s:%s\n' "${TUNNEL_USER}" "${random_hash}" | chpasswd -e
  unset random_hash
  PASSWORD_CHANGED=1
fi

install -d -o "${TUNNEL_USER}" -g "${TUNNEL_USER}" -m 0700 "${SSH_DIR}"
if [[ -f "${AUTHORIZED_KEYS}" ]]; then
  managed_lines="$(grep -Ev '^[[:space:]]*(#|$)' "${AUTHORIZED_KEYS}" || true)"
  if [[ -n "${managed_lines}" ]] && { [[ "$(wc -l <<<"${managed_lines}")" -ne 1 ]] ||
       ! grep -q ' pixelle-material-library-tunnel$' <<<"${managed_lines}"; }; then
    echo "refusing to replace non-managed ${AUTHORIZED_KEYS}" >&2
    exit 2
  fi
  cp -a "${AUTHORIZED_KEYS}" "${BACKUP}/authorized_keys"
  KEYS_EXISTED=1
fi
"${RENDERER}" "${PUBLIC_KEY_FILE}" > "${BACKUP}/authorized_keys.next"
install -o "${TUNNEL_USER}" -g "${TUNNEL_USER}" -m 0600 \
  "${BACKUP}/authorized_keys.next" "${AUTHORIZED_KEYS}"

if [[ -f "${CONFIG_TARGET}" ]]; then
  cp -a "${CONFIG_TARGET}" "${BACKUP}/sshd.conf"
  CONFIG_EXISTED=1
fi
install -o root -g root -m 0644 "${CONFIG_SOURCE}" "${CONFIG_TARGET}"
/usr/sbin/sshd -t
"${CHECKER}"
systemctl reload ssh.service
"${CHECKER}"
SUCCEEDED=1
echo "material-library forwarding-only account installed"
