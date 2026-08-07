#!/usr/bin/env bash
set -euo pipefail

UPSTREAM_URL="https://github.com/AIDC-AI/Pixelle-Video.git"
UPSTREAM_COMMIT="848b054e4fae40dabc62ec58e960b573e83793ac"
RUNTIME_ROOT="/opt/huangque/pixelle-video"
SOURCE_DIR="${RUNTIME_ROOT}/source"
BROWSER_DIR="${RUNTIME_ROOT}/browsers"
CONFIG_PATH="/etc/huangque/pixelle-video.yaml"
SERVICE_NAME="huangque-pixelle-video.service"
DEPLOY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

if [[ "${RUNTIME_ROOT}" != "/opt/huangque/pixelle-video" ]]; then
  echo "refusing unexpected runtime root: ${RUNTIME_ROOT}" >&2
  exit 2
fi
if [[ ! -s "${CONFIG_PATH}" ]]; then
  echo "missing ${CONFIG_PATH}; run scripts/render_pixelle_config.py first" >&2
  exit 2
fi
if [[ "$(id -u)" -ne 0 ]]; then
  echo "run this installer as root" >&2
  exit 2
fi

chown root:admin "${CONFIG_PATH}"
chmod 0640 "${CONFIG_PATH}"

install -d -o admin -g admin -m 0755 "${RUNTIME_ROOT}" "${BROWSER_DIR}"

if [[ ! -d "${SOURCE_DIR}/.git" ]]; then
  sudo -u admin git clone "${UPSTREAM_URL}" "${SOURCE_DIR}"
fi
sudo -u admin git -C "${SOURCE_DIR}" fetch --prune origin
sudo -u admin git -C "${SOURCE_DIR}" checkout --detach "${UPSTREAM_COMMIT}"
sudo -u admin git -C "${SOURCE_DIR}" reset --hard "${UPSTREAM_COMMIT}"
sudo -u admin git -C "${SOURCE_DIR}" clean -fdx

install -o admin -g admin -m 0644 \
  "${DEPLOY_ROOT}"/deploy/pixelle-video/templates/1080x1920/*.html \
  "${SOURCE_DIR}/templates/1080x1920/"

# The public API keeps an in-memory task registry. Limit it to one task on this
# 2-core, low-memory host; RunningHub is also configured with concurrency one.
sed -i 's/max_concurrent_tasks: int = 5/max_concurrent_tasks: int = 1/' "${SOURCE_DIR}/api/config.py"
ln -sfn "${CONFIG_PATH}" "${SOURCE_DIR}/config.yaml"
chown -h admin:admin "${SOURCE_DIR}/config.yaml"

if [[ ! -x "${RUNTIME_ROOT}/bin/uv" ]]; then
  install -d -o admin -g admin -m 0755 "${RUNTIME_ROOT}/bin"
  sudo -u admin env UV_INSTALL_DIR="${RUNTIME_ROOT}/bin" \
    sh -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'
fi

sudo -u admin env UV_PYTHON_INSTALL_DIR="${RUNTIME_ROOT}/python" \
  "${RUNTIME_ROOT}/bin/uv" python install 3.11
sudo -u admin env UV_PYTHON_INSTALL_DIR="${RUNTIME_ROOT}/python" \
  "${RUNTIME_ROOT}/bin/uv" --directory "${SOURCE_DIR}" sync --frozen --python 3.11
sudo -u admin env PLAYWRIGHT_BROWSERS_PATH="${BROWSER_DIR}" \
  "${SOURCE_DIR}/.venv/bin/python" -m playwright install chromium

install -o root -g root -m 0644 \
  "${DEPLOY_ROOT}/deploy/systemd/${SERVICE_NAME}" \
  "/etc/systemd/system/${SERVICE_NAME}"
systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}"

for _ in $(seq 1 60); do
  if curl --fail --silent --show-error http://127.0.0.1:8103/health >/dev/null; then
    curl --fail --silent --show-error http://127.0.0.1:8103/health
    echo
    exit 0
  fi
  sleep 2
done

systemctl --no-pager --full status "${SERVICE_NAME}" || true
journalctl -u "${SERVICE_NAME}" -n 100 --no-pager || true
exit 1
