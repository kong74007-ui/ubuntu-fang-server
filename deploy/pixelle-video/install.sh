#!/usr/bin/env bash
set -euo pipefail

UPSTREAM_URL="https://github.com/AIDC-AI/Pixelle-Video.git"
UPSTREAM_COMMIT="848b054e4fae40dabc62ec58e960b573e83793ac"
RUNTIME_ROOT="/opt/huangque/pixelle-video"
SOURCE_DIR="${RUNTIME_ROOT}/source"
RELEASES_DIR="${RUNTIME_ROOT}/releases"
BROWSER_DIR="${RUNTIME_ROOT}/browsers"
STATE_ROOT="/var/lib/huangque-pixelle-video"
OUTPUT_DIR="${STATE_ROOT}/output"
DATA_DIR="${STATE_ROOT}/data"
EXTERNAL_AUDIO_DIR="${DATA_DIR}/external_audio"
AVATAR_ASSET_ROOT="${DATA_DIR}/avatar_assets"
CONFIG_PATH="/etc/huangque/pixelle-video.yaml"
SERVICE_NAME="huangque-pixelle-video.service"
DEPLOY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SERVICE_UNIT_SOURCE="${DEPLOY_ROOT}/deploy/systemd/${SERVICE_NAME}"
SERVICE_UNIT_TARGET="/etc/systemd/system/${SERVICE_NAME}"
PROXY_SERVICE="mihomo-new.service"
PROXY_READINESS_CHECK="/usr/local/libexec/huangque/check-mihomo-openai-proxy"
PYPI_INDEX="https://mirrors.aliyun.com/pypi/simple"
TASK_CAPACITY_OVERRIDE="${DEPLOY_ROOT}/deploy/pixelle-video/overrides/api/task_capacity.py"
TASK_CAPACITY_PATCH="${DEPLOY_ROOT}/deploy/pixelle-video/patches/0001-enforce-video-task-capacity.patch"
VIDEO_TEMPLATE_BRANDING_PATCH="${DEPLOY_ROOT}/deploy/pixelle-video/patches/0002-remove-video-template-branding.patch"
EXTERNAL_NARRATION_PATCH="${DEPLOY_ROOT}/deploy/pixelle-video/patches/0003-support-external-narration-audio.patch"
DEEPSEEK_V4_PATCH="${DEPLOY_ROOT}/deploy/pixelle-video/patches/0004-disable-deepseek-v4-thinking.patch"
IMAGE_RETRY_PATCH="${DEPLOY_ROOT}/deploy/pixelle-video/patches/0005-retry-image-generation.patch"
RUNNINGHUB_GUARD_PATCH="${DEPLOY_ROOT}/deploy/pixelle-video/patches/0006-guard-runninghub-polling.patch"
PARALLEL_FAIL_FAST_PATCH="${DEPLOY_ROOT}/deploy/pixelle-video/patches/0007-fail-fast-parallel-frames.patch"
TTS_SPEED_PATCH="${DEPLOY_ROOT}/deploy/pixelle-video/patches/0008-support-tts-speed-api.patch"
CAPTION_CUES_PATCH="${DEPLOY_ROOT}/deploy/pixelle-video/patches/0009-support-single-line-caption-cues.patch"
TALKING_MATERIAL_ASSETS_PATCH="${DEPLOY_ROOT}/deploy/pixelle-video/patches/0010-support-talking-material-assets.patch"
TALKING_SCENES_PATCH="${DEPLOY_ROOT}/deploy/pixelle-video/patches/0011-render-talking-material-scenes.patch"
CONTINUOUS_NARRATION_PATCH="${DEPLOY_ROOT}/deploy/pixelle-video/patches/0012-preserve-continuous-narration.patch"
TALKING_MATERIAL_OVERRIDE="${DEPLOY_ROOT}/deploy/pixelle-video/overrides/pixelle_video/services/talking_material.py"
TALKING_CLIENT_OVERRIDE="${DEPLOY_ROOT}/deploy/pixelle-video/overrides/pixelle_video/services/talking_client.py"
PIXELLE_DISCONNECT_OVERRIDE="${DEPLOY_ROOT}/deploy/pixelle-video/overrides/api/disconnect.py"
EXTERNAL_AUDIO_OVERRIDE="${DEPLOY_ROOT}/deploy/pixelle-video/overrides/api/external_audio.py"
VOICE_ASSETS_ROUTER_OVERRIDE="${DEPLOY_ROOT}/deploy/pixelle-video/overrides/api/routers/voice_assets.py"
AVATAR_ASSETS_OVERRIDE="${DEPLOY_ROOT}/deploy/pixelle-video/overrides/api/avatar_assets.py"
AVATAR_ASSETS_ROUTER_OVERRIDE="${DEPLOY_ROOT}/deploy/pixelle-video/overrides/api/routers/avatar_assets.py"
MEDIA_RETRY_OVERRIDE="${DEPLOY_ROOT}/deploy/pixelle-video/overrides/pixelle_video/services/media_retry.py"
RUNNINGHUB_GUARD_OVERRIDE="${DEPLOY_ROOT}/deploy/pixelle-video/overrides/pixelle_video/services/runninghub_guard.py"
FAIL_FAST_OVERRIDE="${DEPLOY_ROOT}/deploy/pixelle-video/overrides/pixelle_video/services/fail_fast.py"
CAPTION_CUES_OVERRIDE="${DEPLOY_ROOT}/deploy/pixelle-video/overrides/pixelle_video/services/caption_cues.py"
SERVICE_CONTROL_LIB="${DEPLOY_ROOT}/deploy/pixelle-video/lib/service_control.sh"
RELEASE_DIR=""
NEXT_SOURCE_LINK=""
PREVIOUS_SOURCE_TARGET=""
LEGACY_SOURCE_BACKUP=""
UNIT_BACKUP_DIR=""
UNIT_EXISTED=0
UNIT_BACKED_UP=0
SOURCE_SWITCHED=0
DEPLOY_SUCCEEDED=0

if [[ ! -s "${SERVICE_CONTROL_LIB}" ]]; then
  echo "missing Pixelle service control library" >&2
  exit 2
fi
source "${SERVICE_CONTROL_LIB}"

backup_pixelle_unit() {
  UNIT_BACKUP_DIR="$(mktemp -d /var/tmp/huangque-pixelle-unit.XXXXXX)"
  chmod 0700 "${UNIT_BACKUP_DIR}"
  pixelle_backup_managed_file \
    "${SERVICE_UNIT_TARGET}" \
    "${UNIT_BACKUP_DIR}/${SERVICE_NAME}" \
    UNIT_EXISTED
  UNIT_BACKED_UP=1
}

restore_pixelle_unit() {
  pixelle_restore_managed_file \
    "${SERVICE_UNIT_TARGET}" \
    "${UNIT_BACKUP_DIR}/${SERVICE_NAME}" \
    "${UNIT_EXISTED}" || return 1
  systemctl daemon-reload || return 1
}

rollback_release() {
  if [[ -n "${PREVIOUS_SOURCE_TARGET}" ]]; then
    local rollback_link="${RUNTIME_ROOT}/.source.rollback.$$"
    ln -s "${PREVIOUS_SOURCE_TARGET}" "${rollback_link}"
    mv -Tf "${rollback_link}" "${SOURCE_DIR}"
  elif [[ -n "${LEGACY_SOURCE_BACKUP}" && -d "${LEGACY_SOURCE_BACKUP}" ]]; then
    rm -f "${SOURCE_DIR}"
    mv "${LEGACY_SOURCE_BACKUP}" "${SOURCE_DIR}"
  else
    rm -f "${SOURCE_DIR}"
  fi
}

rollback_release_and_unit() {
  rollback_release || return 1
  restore_pixelle_unit || return 1
}

activate_release() {
  NEXT_SOURCE_LINK="${RUNTIME_ROOT}/.source.next.$$"
  ln -s "${RELEASE_DIR}" "${NEXT_SOURCE_LINK}"
  if [[ -L "${SOURCE_DIR}" ]]; then
    PREVIOUS_SOURCE_TARGET="$(readlink -f "${SOURCE_DIR}")"
    SOURCE_SWITCHED=1
    mv -Tf "${NEXT_SOURCE_LINK}" "${SOURCE_DIR}"
  elif [[ -e "${SOURCE_DIR}" ]]; then
    LEGACY_SOURCE_BACKUP="${RUNTIME_ROOT}/source.pre-release.$(date +%s)"
    mv "${SOURCE_DIR}" "${LEGACY_SOURCE_BACKUP}"
    SOURCE_SWITCHED=1
    mv -T "${NEXT_SOURCE_LINK}" "${SOURCE_DIR}"
  else
    SOURCE_SWITCHED=1
    mv -T "${NEXT_SOURCE_LINK}" "${SOURCE_DIR}"
  fi
}

cleanup() {
  local exit_code=$?
  set +e

  if [[ "${SOURCE_SWITCHED}" -eq 1 && "${DEPLOY_SUCCEEDED}" -ne 1 ]]; then
    if pixelle_run_with_service_stopped "${SERVICE_NAME}" rollback_release_and_unit; then
      systemctl start "${SERVICE_NAME}" || true
    else
      restore_pixelle_unit || true
      echo "source rollback skipped because ${SERVICE_NAME} could not be confirmed inactive" >&2
    fi
  elif [[ "${UNIT_BACKED_UP}" -eq 1 && "${DEPLOY_SUCCEEDED}" -ne 1 ]]; then
    restore_pixelle_unit || true
  fi

  if [[ -n "${NEXT_SOURCE_LINK}" && -L "${NEXT_SOURCE_LINK}" ]]; then
    rm -f "${NEXT_SOURCE_LINK}"
  fi
  if [[ "${SOURCE_SWITCHED}" -ne 1 && -n "${RELEASE_DIR}" && -d "${RELEASE_DIR}" ]]; then
    rm -rf "${RELEASE_DIR}"
  fi
  case "${UNIT_BACKUP_DIR}" in
    /var/tmp/huangque-pixelle-unit.*) rm -rf -- "${UNIT_BACKUP_DIR}" ;;
  esac
  return "${exit_code}"
}

trap cleanup EXIT

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
if [[ "$(systemctl show --property=LoadState --value "${PROXY_SERVICE}")" != "loaded" ]]; then
  echo "${PROXY_SERVICE} is not installed; provision the Pixelle proxy first" >&2
  exit 2
fi
if ! systemctl is-active --quiet "${PROXY_SERVICE}"; then
  echo "${PROXY_SERVICE} is not active; refusing Pixelle deployment" >&2
  exit 2
fi
if [[ ! -x "${PROXY_READINESS_CHECK}" ]]; then
  echo "missing ${PROXY_READINESS_CHECK}; provision the Pixelle proxy first" >&2
  exit 2
fi
if ! "${PROXY_READINESS_CHECK}"; then
  echo "${PROXY_SERVICE} is not ready on 127.0.0.1:7999" >&2
  exit 2
fi
if [[ ! -s "${TASK_CAPACITY_OVERRIDE}" || ! -s "${TASK_CAPACITY_PATCH}" || ! -s "${VIDEO_TEMPLATE_BRANDING_PATCH}" || ! -s "${EXTERNAL_NARRATION_PATCH}" || ! -s "${DEEPSEEK_V4_PATCH}" || ! -s "${IMAGE_RETRY_PATCH}" || ! -s "${RUNNINGHUB_GUARD_PATCH}" || ! -s "${PARALLEL_FAIL_FAST_PATCH}" || ! -s "${TTS_SPEED_PATCH}" || ! -s "${CAPTION_CUES_PATCH}" || ! -s "${TALKING_MATERIAL_ASSETS_PATCH}" || ! -s "${TALKING_SCENES_PATCH}" || ! -s "${CONTINUOUS_NARRATION_PATCH}" || ! -s "${TALKING_MATERIAL_OVERRIDE}" || ! -s "${TALKING_CLIENT_OVERRIDE}" || ! -s "${PIXELLE_DISCONNECT_OVERRIDE}" || ! -s "${EXTERNAL_AUDIO_OVERRIDE}" || ! -s "${VOICE_ASSETS_ROUTER_OVERRIDE}" || ! -s "${AVATAR_ASSETS_OVERRIDE}" || ! -s "${AVATAR_ASSETS_ROUTER_OVERRIDE}" || ! -s "${MEDIA_RETRY_OVERRIDE}" || ! -s "${RUNNINGHUB_GUARD_OVERRIDE}" || ! -s "${FAIL_FAST_OVERRIDE}" || ! -s "${CAPTION_CUES_OVERRIDE}" ]]; then
  echo "missing Pixelle deployment files" >&2
  exit 2
fi

chown root:admin "${CONFIG_PATH}"
chmod 0640 "${CONFIG_PATH}"

install -d -o admin -g admin -m 0755 "${RUNTIME_ROOT}" "${RELEASES_DIR}" "${BROWSER_DIR}"
install -d -o admin -g admin -m 0750 "${STATE_ROOT}" "${OUTPUT_DIR}" "${DATA_DIR}"
install -d -o admin -g admin -m 0700 "${EXTERNAL_AUDIO_DIR}"
install -d -o admin -g admin -m 0700 "${AVATAR_ASSET_ROOT}"

# Preserve outputs created by deployments that predate the persistent state
# layout before preparing the replacement release.
if [[ -d "${SOURCE_DIR}/output" && ! -L "${SOURCE_DIR}/output" ]]; then
  cp -a "${SOURCE_DIR}/output/." "${OUTPUT_DIR}/"
fi
if [[ -d "${SOURCE_DIR}/data" && ! -L "${SOURCE_DIR}/data" ]]; then
  cp -a "${SOURCE_DIR}/data/." "${DATA_DIR}/"
fi

RELEASE_DIR="$(mktemp -d "${RELEASES_DIR}/${UPSTREAM_COMMIT}.XXXXXX")"
chown admin:admin "${RELEASE_DIR}"
sudo -u admin git clone --no-checkout "${UPSTREAM_URL}" "${RELEASE_DIR}"
sudo -u admin git -C "${RELEASE_DIR}" checkout --detach "${UPSTREAM_COMMIT}"
sudo -u admin git -C "${RELEASE_DIR}" reset --hard "${UPSTREAM_COMMIT}"
sudo -u admin git -C "${RELEASE_DIR}" clean -fdx

rm -rf "${RELEASE_DIR}/output" "${RELEASE_DIR}/data"
ln -s "${OUTPUT_DIR}" "${RELEASE_DIR}/output"
ln -s "${DATA_DIR}" "${RELEASE_DIR}/data"
chown -h admin:admin "${RELEASE_DIR}/output" "${RELEASE_DIR}/data"

install -o admin -g admin -m 0644 \
  "${DEPLOY_ROOT}"/deploy/pixelle-video/templates/1080x1920/*.html \
  "${RELEASE_DIR}/templates/1080x1920/"

# Fail closed if the pinned upstream source no longer matches the reviewed
# concurrency patch. The override permits one running task plus 20 waiters.
sudo -u admin git -C "${RELEASE_DIR}" apply --unidiff-zero --check "${TASK_CAPACITY_PATCH}"
sudo -u admin git -C "${RELEASE_DIR}" apply --unidiff-zero "${TASK_CAPACITY_PATCH}"
sudo -u admin git -C "${RELEASE_DIR}" apply --unidiff-zero --check "${VIDEO_TEMPLATE_BRANDING_PATCH}"
sudo -u admin git -C "${RELEASE_DIR}" apply --unidiff-zero "${VIDEO_TEMPLATE_BRANDING_PATCH}"
sudo -u admin git -C "${RELEASE_DIR}" apply --unidiff-zero --check "${EXTERNAL_NARRATION_PATCH}"
sudo -u admin git -C "${RELEASE_DIR}" apply --unidiff-zero "${EXTERNAL_NARRATION_PATCH}"
sudo -u admin git -C "${RELEASE_DIR}" apply --check "${DEEPSEEK_V4_PATCH}"
sudo -u admin git -C "${RELEASE_DIR}" apply "${DEEPSEEK_V4_PATCH}"
sudo -u admin git -C "${RELEASE_DIR}" apply --unidiff-zero --check "${IMAGE_RETRY_PATCH}"
sudo -u admin git -C "${RELEASE_DIR}" apply --unidiff-zero "${IMAGE_RETRY_PATCH}"
sudo -u admin git -C "${RELEASE_DIR}" apply --check "${RUNNINGHUB_GUARD_PATCH}"
sudo -u admin git -C "${RELEASE_DIR}" apply "${RUNNINGHUB_GUARD_PATCH}"
sudo -u admin git -C "${RELEASE_DIR}" apply --unidiff-zero --check "${PARALLEL_FAIL_FAST_PATCH}"
sudo -u admin git -C "${RELEASE_DIR}" apply --unidiff-zero "${PARALLEL_FAIL_FAST_PATCH}"
sudo -u admin git -C "${RELEASE_DIR}" apply --check "${TTS_SPEED_PATCH}"
sudo -u admin git -C "${RELEASE_DIR}" apply "${TTS_SPEED_PATCH}"
sudo -u admin git -C "${RELEASE_DIR}" apply --unidiff-zero --check "${CAPTION_CUES_PATCH}"
sudo -u admin git -C "${RELEASE_DIR}" apply --unidiff-zero "${CAPTION_CUES_PATCH}"
sudo -u admin git -C "${RELEASE_DIR}" apply --unidiff-zero --check "${TALKING_MATERIAL_ASSETS_PATCH}"
sudo -u admin git -C "${RELEASE_DIR}" apply --unidiff-zero "${TALKING_MATERIAL_ASSETS_PATCH}"
sudo -u admin git -C "${RELEASE_DIR}" apply --unidiff-zero --check "${TALKING_SCENES_PATCH}"
sudo -u admin git -C "${RELEASE_DIR}" apply --unidiff-zero "${TALKING_SCENES_PATCH}"
sudo -u admin git -C "${RELEASE_DIR}" apply --unidiff-zero --check "${CONTINUOUS_NARRATION_PATCH}"
sudo -u admin git -C "${RELEASE_DIR}" apply --unidiff-zero "${CONTINUOUS_NARRATION_PATCH}"
install -o admin -g admin -m 0644 "${TASK_CAPACITY_OVERRIDE}" \
  "${RELEASE_DIR}/api/task_capacity.py"
install -o admin -g admin -m 0644 "${PIXELLE_DISCONNECT_OVERRIDE}" \
  "${RELEASE_DIR}/api/disconnect.py"
install -o admin -g admin -m 0644 "${EXTERNAL_AUDIO_OVERRIDE}" \
  "${RELEASE_DIR}/api/external_audio.py"
install -o admin -g admin -m 0644 "${VOICE_ASSETS_ROUTER_OVERRIDE}" \
  "${RELEASE_DIR}/api/routers/voice_assets.py"
install -o admin -g admin -m 0644 "${AVATAR_ASSETS_OVERRIDE}" \
  "${RELEASE_DIR}/api/avatar_assets.py"
install -o admin -g admin -m 0644 "${AVATAR_ASSETS_ROUTER_OVERRIDE}" \
  "${RELEASE_DIR}/api/routers/avatar_assets.py"
install -o admin -g admin -m 0644 "${MEDIA_RETRY_OVERRIDE}" \
  "${RELEASE_DIR}/pixelle_video/services/media_retry.py"
install -o admin -g admin -m 0644 "${RUNNINGHUB_GUARD_OVERRIDE}" \
  "${RELEASE_DIR}/pixelle_video/services/runninghub_guard.py"
install -o admin -g admin -m 0644 "${FAIL_FAST_OVERRIDE}" \
  "${RELEASE_DIR}/pixelle_video/services/fail_fast.py"
install -o admin -g admin -m 0644 "${CAPTION_CUES_OVERRIDE}" \
  "${RELEASE_DIR}/pixelle_video/services/caption_cues.py"
install -o admin -g admin -m 0644 "${TALKING_MATERIAL_OVERRIDE}" \
  "${RELEASE_DIR}/pixelle_video/services/talking_material.py"
install -o admin -g admin -m 0644 "${TALKING_CLIENT_OVERRIDE}" \
  "${RELEASE_DIR}/pixelle_video/services/talking_client.py"
rm -f "${RELEASE_DIR}/config.yaml"
ln -s "${CONFIG_PATH}" "${RELEASE_DIR}/config.yaml"
chown -h admin:admin "${RELEASE_DIR}/config.yaml"

if [[ ! -x "${RUNTIME_ROOT}/bin/uv" ]]; then
  install -d -o admin -g admin -m 0755 "${RUNTIME_ROOT}/bin"
  sudo -u admin env UV_INSTALL_DIR="${RUNTIME_ROOT}/bin" \
    sh -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'
fi

sudo -u admin env UV_PYTHON_INSTALL_DIR="${RUNTIME_ROOT}/python" \
  "${RUNTIME_ROOT}/bin/uv" python install 3.11

# uv.lock records immutable wheel URLs. Rewrite only the package CDN host to
# the byte-identical regional mirror; uv still verifies every locked SHA256.
LOCK_FILE="${RELEASE_DIR}/uv.lock"
LOCKED_WHEEL_COUNT="$(grep -c 'https://files.pythonhosted.org/packages/' "${LOCK_FILE}" || true)"
if [[ "${LOCKED_WHEEL_COUNT}" -lt 1 ]]; then
  echo "unexpected uv.lock: no files.pythonhosted.org package URLs" >&2
  exit 2
fi
sed -i 's#https://files.pythonhosted.org/packages/#https://mirrors.aliyun.com/pypi/packages/#g' "${LOCK_FILE}"

sudo -u admin env UV_PYTHON_INSTALL_DIR="${RUNTIME_ROOT}/python" UV_DEFAULT_INDEX="${PYPI_INDEX}" \
  "${RUNTIME_ROOT}/bin/uv" --directory "${RELEASE_DIR}" sync --frozen --python 3.11
sudo -u admin env PLAYWRIGHT_BROWSERS_PATH="${BROWSER_DIR}" \
  "${RELEASE_DIR}/.venv/bin/python" -m playwright install chromium
sudo -u admin "${RELEASE_DIR}/.venv/bin/python" -m compileall -q "${RELEASE_DIR}/api" "${RELEASE_DIR}/pixelle_video"

backup_pixelle_unit
install -o root -g root -m 0644 "${SERVICE_UNIT_SOURCE}" "${SERVICE_UNIT_TARGET}"
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"

pixelle_run_with_service_stopped "${SERVICE_NAME}" activate_release
systemctl start "${SERVICE_NAME}"

for _ in $(seq 1 60); do
  if curl --fail --silent --show-error http://127.0.0.1:8103/health >/dev/null; then
    curl --fail --silent --show-error http://127.0.0.1:8103/health
    echo
    DEPLOY_SUCCEEDED=1
    if [[ -n "${LEGACY_SOURCE_BACKUP}" && -d "${LEGACY_SOURCE_BACKUP}" ]]; then
      rm -rf "${LEGACY_SOURCE_BACKUP}"
    fi
    exit 0
  fi
  sleep 2
done

systemctl --no-pager --full status "${SERVICE_NAME}" || true
journalctl -u "${SERVICE_NAME}" -n 100 --no-pager || true
exit 1
