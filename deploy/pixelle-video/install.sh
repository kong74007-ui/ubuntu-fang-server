#!/usr/bin/env bash
set -euo pipefail

UPSTREAM_URL="https://github.com/AIDC-AI/Pixelle-Video.git"
UPSTREAM_COMMIT="848b054e4fae40dabc62ec58e960b573e83793ac"
RUNTIME_ROOT="/opt/huangque/pixelle-video"
SOURCE_DIR="${RUNTIME_ROOT}/source"
BROWSER_DIR="${RUNTIME_ROOT}/browsers"
STATE_ROOT="/var/lib/huangque-pixelle-video"
OUTPUT_DIR="${STATE_ROOT}/output"
DATA_DIR="${STATE_ROOT}/data"
CONFIG_PATH="/etc/huangque/pixelle-video.yaml"
SERVICE_NAME="huangque-pixelle-video.service"
PYPI_INDEX="https://mirrors.aliyun.com/pypi/simple"
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
install -d -o admin -g admin -m 0750 "${STATE_ROOT}" "${OUTPUT_DIR}" "${DATA_DIR}"

if [[ ! -d "${SOURCE_DIR}/.git" ]]; then
  sudo -u admin git clone "${UPSTREAM_URL}" "${SOURCE_DIR}"
fi

# Preserve outputs created by deployments that predate the persistent state
# layout before resetting the upstream checkout.
if [[ -d "${SOURCE_DIR}/output" && ! -L "${SOURCE_DIR}/output" ]]; then
  cp -a "${SOURCE_DIR}/output/." "${OUTPUT_DIR}/"
fi
if [[ -d "${SOURCE_DIR}/data" && ! -L "${SOURCE_DIR}/data" ]]; then
  cp -a "${SOURCE_DIR}/data/." "${DATA_DIR}/"
fi

sudo -u admin git -C "${SOURCE_DIR}" fetch --prune origin
sudo -u admin git -C "${SOURCE_DIR}" checkout --detach "${UPSTREAM_COMMIT}"
sudo -u admin git -C "${SOURCE_DIR}" reset --hard "${UPSTREAM_COMMIT}"
sudo -u admin git -C "${SOURCE_DIR}" clean -fdx

ln -sfn "${OUTPUT_DIR}" "${SOURCE_DIR}/output"
ln -sfn "${DATA_DIR}" "${SOURCE_DIR}/data"
chown -h admin:admin "${SOURCE_DIR}/output" "${SOURCE_DIR}/data"

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

# uv.lock records immutable wheel URLs. Rewrite only the package CDN host to
# the byte-identical regional mirror; uv still verifies every locked SHA256.
LOCK_FILE="${SOURCE_DIR}/uv.lock"
LOCKED_WHEEL_COUNT="$(grep -c 'https://files.pythonhosted.org/packages/' "${LOCK_FILE}" || true)"
if [[ "${LOCKED_WHEEL_COUNT}" -lt 1 ]]; then
  echo "unexpected uv.lock: no files.pythonhosted.org package URLs" >&2
  exit 2
fi
sed -i 's#https://files.pythonhosted.org/packages/#https://mirrors.aliyun.com/pypi/packages/#g' "${LOCK_FILE}"

sudo -u admin env UV_PYTHON_INSTALL_DIR="${RUNTIME_ROOT}/python" UV_DEFAULT_INDEX="${PYPI_INDEX}" \
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
