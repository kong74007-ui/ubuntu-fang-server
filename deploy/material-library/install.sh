#!/usr/bin/env bash
set -euo pipefail

SOURCE_ROOT="${SOURCE_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
INSTALL_ROOT="${INSTALL_ROOT:-/opt/huangque/material-library/source}"
ENV_FILE="${ENV_FILE:-/etc/huangque/material-library.env}"
UNIT_PATH="${UNIT_PATH:-/etc/systemd/system/huangque-material-library.service}"
LIBRARY_ROOT="${MATERIAL_LIBRARY_ROOT:-/home/ubuntu/material-libraries/huangque-media}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "run as root" >&2
  exit 1
fi
if [[ ! -f "${SOURCE_ROOT}/server/material_library.py" || ! -f "${SOURCE_ROOT}/server/material_library_api.py" ]]; then
  echo "material library source files are missing" >&2
  exit 1
fi
if [[ ! -f "${LIBRARY_ROOT}/index.jsonl" ]]; then
  echo "material library index is missing: ${LIBRARY_ROOT}/index.jsonl" >&2
  exit 1
fi

install -d -o root -g root -m 0755 "${INSTALL_ROOT}/server"
install -o root -g root -m 0644 "${SOURCE_ROOT}/server/material_library.py" "${INSTALL_ROOT}/server/material_library.py"
install -o root -g root -m 0644 "${SOURCE_ROOT}/server/material_library_api.py" "${INSTALL_ROOT}/server/material_library_api.py"
install -d -o root -g root -m 0755 "$(dirname "${ENV_FILE}")"

if [[ ! -f "${ENV_FILE}" ]]; then
  umask 077
  token="$(python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(48))
PY
)"
  printf 'MATERIAL_LIBRARY_ROOT=%s\nMATERIAL_LIBRARY_API_TOKEN=%s\n' "${LIBRARY_ROOT}" "${token}" > "${ENV_FILE}"
fi
chown root:root "${ENV_FILE}"
chmod 0600 "${ENV_FILE}"

install -o root -g root -m 0644 \
  "${SOURCE_ROOT}/deploy/systemd/huangque-material-library.service" \
  "${UNIT_PATH}"
systemctl daemon-reload
systemctl enable --now huangque-material-library.service

for _ in $(seq 1 20); do
  if curl --fail --silent --show-error http://127.0.0.1:8110/health >/dev/null; then
    exit 0
  fi
  sleep 0.5
done
systemctl status huangque-material-library.service --no-pager >&2 || true
exit 1
