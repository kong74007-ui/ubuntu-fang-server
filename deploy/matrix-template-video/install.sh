#!/usr/bin/env bash
set -euo pipefail

UPSTREAM_URL="https://github.com/kong74007-ui/script-to-matrix-video.git"
UPSTREAM_COMMIT="243d5c168d9ab2d95daf04fef5c5e75924114eb8"
REFERENCE_UPSTREAM_COMMIT="9040a24139372f14346816cf42a97271767a0777"
NINE_GRID_UPSTREAM_COMMIT="81da5e926aad0d2166845ee0b398282a21ab09e7"
HYPERFRAMES_VERSION="0.8.16"
NINE_GRID_HYPERFRAMES_VERSION="0.8.33"
GSAP_VERSION="3.14.2"
HYPERFRAMES_CLI="/usr/local/bin/hyperframes"
HYPERFRAMES_BROWSER="/usr/bin/google-chrome-stable"
NODE_NPM="/opt/node-v22.22.0-linux-x64/bin/npm"
LAYOUT_PATCH_SHA256="33f64143e481301bcfd0f157ce1398c590d2e41512e2ea930772d739b4651329"
REFERENCE_LAYOUT_PATCH_SHA256="90454262ac629a38554a2b0155eab498c7d117289d8f2d5c4c001c526d18b5e5"
NINE_GRID_ADAPTER_SHA256="b0b60138b6d51d8b1fa672f9552dae1fbc3c96e387de2a072e6cf7eb655b75cd"
NINE_GRID_PACKAGE_SHA256="6a9f7d9900b2a7e9c451811b19f373fa2a081f3737133c5783346aeebc0be216"
NINE_GRID_LOCK_SHA256="df5d53aa4b5c3e8cf0c896649b3ea8c75c5d76d197ebc89d2923d12964423e84"
DEPLOY_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUNTIME_ROOT="/opt/huangque/matrix-template-video"
SOURCE_LINK="${RUNTIME_ROOT}/source"
RELEASES_DIR="${RUNTIME_ROOT}/releases"
STATE_ROOT="/var/lib/huangque-matrix-template"
PRIVATE_FONT_ROOT="${STATE_ROOT}/private-fonts"
ENV_FILE="/etc/huangque/matrix-template.env"
PEXELS_ENV_FILE="/etc/huangque/pexels.env"
UNIT_SOURCE="${DEPLOY_ROOT}/deploy/systemd/huangque-matrix-template.service"
UNIT_TARGET="/etc/systemd/system/huangque-matrix-template.service"
API_SOURCE="${DEPLOY_ROOT}/server/matrix_template_api.py"
LAYOUT_PATCH_SOURCE="${DEPLOY_ROOT}/deploy/matrix-template-video/private-domain-layouts.patch"
REFERENCE_LAYOUT_PATCH_SOURCE="${DEPLOY_ROOT}/deploy/matrix-template-video/reference-featured-layout.patch"
REFERENCE_V04_PREVIEW_CHECK_SOURCE="${DEPLOY_ROOT}/deploy/matrix-template-video/verify_v04_preview.py"
REFERENCE_V07_PREVIEW_CHECK_SOURCE="${DEPLOY_ROOT}/deploy/matrix-template-video/verify_v07_preview.py"
NINE_GRID_ADAPTER_SOURCE="${DEPLOY_ROOT}/deploy/matrix-template-video/prepare-nine-grid-template.py"
NINE_GRID_PACKAGE_SOURCE="${DEPLOY_ROOT}/deploy/matrix-template-video/nine-grid-runtime/package.json"
NINE_GRID_LOCK_SOURCE="${DEPLOY_ROOT}/deploy/matrix-template-video/nine-grid-runtime/package-lock.json"
ROLLBACK_LIB="${DEPLOY_ROOT}/deploy/material-library/lib/rollback.sh"
SERVICE="huangque-matrix-template.service"

BACKUP=""
RELEASE=""
NEXT_LINK=""
PREVIOUS_SOURCE=""
LEGACY_SOURCE=""
SOURCE_MUTATED=0
UNIT_MUTATED=0
SERVICE_MUTATED=0
ENV_CREATED=0
ENV_EXISTED=0
ENV_MUTATED=0
UNIT_EXISTED=0
WAS_ACTIVE=0
WAS_ENABLED=0
OLD_PID=0
SUCCEEDED=0

cleanup() {
  local status=$?
  set +e
  if [[ "${SUCCEEDED}" -ne 1 ]]; then
    [[ "${SERVICE_MUTATED}" -eq 1 ]] && systemctl stop "${SERVICE}" >/dev/null 2>&1 || true
    if [[ "${SOURCE_MUTATED}" -eq 1 ]]; then
      material_restore_release \
        "${SOURCE_LINK}" "${PREVIOUS_SOURCE}" "${LEGACY_SOURCE}" \
        "${UNIT_EXISTED}" "${BACKUP}/unit" "${UNIT_TARGET}" \
        "${ENV_CREATED}" "${ENV_FILE}" "${RELEASE}"
      RELEASE=""
    elif [[ "${ENV_CREATED}" -eq 1 ]]; then
      rm -f "${ENV_FILE}"
    fi
    if [[ "${ENV_MUTATED}" -eq 1 && "${ENV_EXISTED}" -eq 1 && -f "${BACKUP}/env" ]]; then
      install -o root -g admin -m 0640 "${BACKUP}/env" "${ENV_FILE}"
    fi
    [[ "${UNIT_MUTATED}" -eq 1 ]] && systemctl daemon-reload >/dev/null 2>&1 || true
    if [[ "${SERVICE_MUTATED}" -eq 1 ]]; then
      if [[ "${WAS_ENABLED}" -eq 1 ]]; then systemctl enable "${SERVICE}" >/dev/null 2>&1 || true
      else systemctl disable "${SERVICE}" >/dev/null 2>&1 || true; fi
      [[ "${WAS_ACTIVE}" -eq 1 ]] && systemctl start "${SERVICE}" >/dev/null 2>&1 || true
    fi
    [[ -n "${RELEASE}" && -d "${RELEASE}" ]] && rm -rf "${RELEASE}"
  fi
  [[ -n "${NEXT_LINK}" ]] && rm -f "${NEXT_LINK}"
  [[ -n "${BACKUP}" ]] && rm -rf "${BACKUP}"
  return "${status}"
}

if [[ "$(id -u)" -ne 0 ]]; then echo "run as root" >&2; exit 2; fi
for source in "${UNIT_SOURCE}" "${API_SOURCE}" "${LAYOUT_PATCH_SOURCE}" "${REFERENCE_LAYOUT_PATCH_SOURCE}" "${REFERENCE_V04_PREVIEW_CHECK_SOURCE}" "${REFERENCE_V07_PREVIEW_CHECK_SOURCE}" "${NINE_GRID_ADAPTER_SOURCE}" "${NINE_GRID_PACKAGE_SOURCE}" "${NINE_GRID_LOCK_SOURCE}" "${ROLLBACK_LIB}"; do
  if [[ ! -f "${source}" || -L "${source}" || ! -r "${source}" ]]; then
    echo "missing or unsafe deployment source: ${source}" >&2; exit 2
  fi
done
if [[ ! -f /etc/huangque/pixelle-material-library.env || -L /etc/huangque/pixelle-material-library.env ]]; then
  echo "material library client environment is missing" >&2; exit 2
fi
if [[ ! -f "${PEXELS_ENV_FILE}" || -L "${PEXELS_ENV_FILE}" ]] ||
   [[ "$(stat -c '%U:%G' "${PEXELS_ENV_FILE}")" != "root:admin" ]] ||
   [[ "$(stat -c '%a' "${PEXELS_ENV_FILE}")" != "640" ]] ||
   ! grep -Eq '^PEXELS_API_KEY=[A-Za-z0-9]+$' "${PEXELS_ENV_FILE}"; then
  echo "${PEXELS_ENV_FILE} must contain PEXELS_API_KEY and be root:admin mode 640" >&2
  exit 2
fi
source "${ROLLBACK_LIB}"
systemctl is-active --quiet "${SERVICE}" && WAS_ACTIVE=1 || true
systemctl is-enabled --quiet "${SERVICE}" && WAS_ENABLED=1 || true
if [[ "${WAS_ACTIVE}" -eq 1 ]]; then
  OLD_PID="$(systemctl show --property=MainPID --value "${SERVICE}")"
  [[ "${OLD_PID}" =~ ^[0-9]+$ ]] || OLD_PID=0
fi
if [[ -e "${ENV_FILE}" ]] && {
  [[ ! -f "${ENV_FILE}" ]] || [[ -L "${ENV_FILE}" ]] ||
  [[ "$(stat -c '%U:%G' "${ENV_FILE}")" != "root:admin" ]] ||
  [[ "$(stat -c '%a' "${ENV_FILE}")" != "640" ]];
}; then
  echo "${ENV_FILE} must be root:admin mode 640" >&2; exit 2
fi

BACKUP="$(mktemp -d /var/tmp/matrix-template-install.XXXXXX)"
trap cleanup EXIT
if [[ -f "${UNIT_TARGET}" ]]; then cp -a "${UNIT_TARGET}" "${BACKUP}/unit"; UNIT_EXISTED=1; fi
if [[ -f "${ENV_FILE}" ]]; then cp -a "${ENV_FILE}" "${BACKUP}/env"; ENV_EXISTED=1; fi
if [[ "$(nproc)" -lt 4 ]] || [[ "$(awk '/MemTotal/{print $2}' /proc/meminfo)" -lt 7340032 ]]; then
  echo "matrix template concurrency 5 requires at least 4 vCPU and 7 GiB RAM" >&2; exit 1
fi
if [[ ! -x "${HYPERFRAMES_CLI}" ]] || [[ "$("${HYPERFRAMES_CLI}" --version)" != "${HYPERFRAMES_VERSION}" ]]; then
  echo "HyperFrames ${HYPERFRAMES_VERSION} is required" >&2; exit 1
fi
for binary in "${HYPERFRAMES_BROWSER}" "${NODE_NPM}"; do
  if [[ ! -x "${binary}" ]]; then
    echo "missing or unsafe HyperFrames runtime binary: ${binary}" >&2; exit 1
  fi
done
install -d -o root -g root -m 0755 "${RUNTIME_ROOT}" "${RELEASES_DIR}"
install -d -o admin -g admin -m 0750 "${STATE_ROOT}"
install -d -o root -g admin -m 0750 "${PRIVATE_FONT_ROOT}"
RELEASE="$(mktemp -d "${RELEASES_DIR}/${UPSTREAM_COMMIT}.XXXXXX")"
chmod 0755 "${RELEASE}"
git clone --no-checkout "${UPSTREAM_URL}" "${RELEASE}/upstream"
git -C "${RELEASE}/upstream" checkout --detach "${UPSTREAM_COMMIT}"
git -C "${RELEASE}/upstream" reset --hard "${UPSTREAM_COMMIT}"
git -C "${RELEASE}/upstream" clean -fdx
if [[ "$(git -C "${RELEASE}/upstream" rev-parse HEAD)" != "${UPSTREAM_COMMIT}" ]]; then
  echo "upstream commit mismatch" >&2; exit 1
fi
if [[ "$(sha256sum "${LAYOUT_PATCH_SOURCE}" | awk '{print $1}')" != "${LAYOUT_PATCH_SHA256}" ]]; then
  echo "private-domain layout patch hash mismatch" >&2; exit 1
fi
git -C "${RELEASE}/upstream" apply --check --directory=script-to-matrix-video "${LAYOUT_PATCH_SOURCE}"
git -C "${RELEASE}/upstream" apply --directory=script-to-matrix-video "${LAYOUT_PATCH_SOURCE}"
install -o root -g root -m 0644 "${API_SOURCE}" "${RELEASE}/api.py"
SKILL_ROOT="${RELEASE}/upstream/script-to-matrix-video"
python3 -m py_compile "${RELEASE}/api.py" "${SKILL_ROOT}/scripts/render_video.py"
python3 -c 'from PIL import Image, ImageDraw, ImageFont'
python3 "${SKILL_ROOT}/scripts/check_environment.py"
python3 "${SKILL_ROOT}/scripts/test_template_catalog.py"
python3 "${SKILL_ROOT}/scripts/test_private_domain_layouts.py"
python3 "${SKILL_ROOT}/scripts/test_private_domain_catalog.py"
python3 "${SKILL_ROOT}/scripts/restrict_private_domain_catalog.py"
python3 "${SKILL_ROOT}/scripts/test_private_domain_layouts.py"

REFERENCE_UPSTREAM="${RELEASE}/reference-upstream"
git clone --filter=blob:none --no-checkout "${UPSTREAM_URL}" "${REFERENCE_UPSTREAM}"
git -C "${REFERENCE_UPSTREAM}" sparse-checkout init --cone
git -C "${REFERENCE_UPSTREAM}" sparse-checkout set \
  script-to-matrix-video/assets/templates/reference-typography-17 \
  script-to-matrix-video/assets/fonts
git -C "${REFERENCE_UPSTREAM}" checkout --detach "${REFERENCE_UPSTREAM_COMMIT}"
git -C "${REFERENCE_UPSTREAM}" reset --hard "${REFERENCE_UPSTREAM_COMMIT}"
git -C "${REFERENCE_UPSTREAM}" clean -fdx
if [[ "$(git -C "${REFERENCE_UPSTREAM}" rev-parse HEAD)" != "${REFERENCE_UPSTREAM_COMMIT}" ]]; then
  echo "reference template upstream commit mismatch" >&2; exit 1
fi
if [[ "$(sha256sum "${REFERENCE_LAYOUT_PATCH_SOURCE}" | awk '{print $1}')" != "${REFERENCE_LAYOUT_PATCH_SHA256}" ]]; then
  echo "reference featured layout patch hash mismatch" >&2; exit 1
fi
git -C "${REFERENCE_UPSTREAM}" apply --check "${REFERENCE_LAYOUT_PATCH_SOURCE}"
git -C "${REFERENCE_UPSTREAM}" apply "${REFERENCE_LAYOUT_PATCH_SOURCE}"
REFERENCE_SKILL_ROOT="${REFERENCE_UPSTREAM}/script-to-matrix-video"
REFERENCE_PACK_ROOT="${REFERENCE_SKILL_ROOT}/assets/templates/reference-typography-17"
REFERENCE_SKILL_ROOT="${REFERENCE_SKILL_ROOT}" HYPERFRAMES_VERSION="${HYPERFRAMES_VERSION}" PRIVATE_FONT_ROOT="${PRIVATE_FONT_ROOT}" python3 - <<'PY'
import json
import os
import re
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

root = Path(os.environ["REFERENCE_SKILL_ROOT"])
manifest = json.loads((root / "assets/templates/reference-typography-17/manifest.json").read_text(encoding="utf-8"))
templates = manifest.get("templates") or []
assert manifest.get("pack_id") == "reference-typography-17"
assert manifest.get("engine") == "hyperframes"
assert manifest.get("hyperframes_version") == os.environ["HYPERFRAMES_VERSION"]
assert len(templates) == 17
assert len({item.get("id") for item in templates}) == 17
index_html = (root / "assets/templates/reference-typography-17/index.html").read_text(
    encoding="utf-8"
)
top_safe_area = re.search(
    r"(?m)^\s*#root \.top\s*\{\s*top:\s*8%;\s*\}\s*$",
    index_html,
)
assert top_safe_area is not None
for name in (
    "NotoSansSC-Variable.ttf", "MaShanZheng-Regular.ttf",
    "ZCOOLKuaiLe-Regular.ttf", "ZCOOLXiaoWei-Regular.ttf",
):
    assert (root / "assets/fonts" / name).is_file()

font_path = root / "assets/fonts/NotoSansSC-Variable.ttf"
sample = "AI视频获客增长100条"
draw = ImageDraw.Draw(Image.new("L", (1, 1)))
widths = {}
for weight in (400, 900):
    font = ImageFont.truetype(str(font_path), 104)
    axes = font.get_variation_axes()
    weight_axis = next(
        index for index, axis in enumerate(axes)
        if axis["name"].decode("ascii", "ignore").lower() == "weight"
    )
    values = [int(axis["default"]) for axis in axes]
    values[weight_axis] = weight
    font.set_variation_by_axes(values)
    box = draw.textbbox((0, 0), sample, font=font, stroke_width=13)
    widths[weight] = box[2] - box[0] + (len(sample) - 1) * 104 * -0.045
assert widths[400] <= 996 < widths[900], widths

xiaowei_sample = "MMMMMMMMMMMMMM"
xiaowei = ImageFont.truetype(
    str(root / "assets/fonts/ZCOOLXiaoWei-Regular.ttf"), 80
)
xiaowei_box = draw.textbbox(
    (0, 0), xiaowei_sample, font=xiaowei, stroke_width=9
)
xiaowei_width = (
    xiaowei_box[2] - xiaowei_box[0]
    + (len(xiaowei_sample) - 1) * 80 * 0.01
)
assert 970 < xiaowei_width <= 996, xiaowei_width

def text_width(path, size, stroke, value, weight=None):
    font = ImageFont.truetype(str(path), size)
    if weight is not None:
        axes = font.get_variation_axes()
        weight_axis = next(
            index for index, axis in enumerate(axes)
            if axis["name"].decode("ascii", "ignore").lower() == "weight"
        )
        values = [int(axis["default"]) for axis in axes]
        values[weight_axis] = weight
        font.set_variation_by_axes(values)
    box = draw.textbbox((0, 0), value, font=font, stroke_width=stroke)
    return box[2] - box[0] + max(0, len(value) - 1) * size * 0.01

smiley_path = Path(os.environ["PRIVATE_FONT_ROOT"]) / "SmileySans-Oblique.ttf"
checks = (
    ("v10.top1", root / "assets/fonts/ZCOOLXiaoWei-Regular.ttf", 85, 8,
     ("我在深圳发起100场",)),
    ("v12.top1", root / "assets/fonts/MaShanZheng-Regular.ttf", 80, 10,
     ("在广州 天河",)),
    ("v12.top3", root / "assets/fonts/MaShanZheng-Regular.ttf", 70, 7,
     ("不打麻将 不逛街", "资源链接 相互成长 社交突破")),
    ("v16.top1", root / "assets/fonts/ZCOOLXiaoWei-Regular.ttf", 80, 5,
     ("我在深圳发起了共享办公", "共享创业 OPC 自媒体平台")),
    ("v16.top2", smiley_path, 68, 7, ("我有流量 共创600场地",)),
    ("v16.bottom1", smiley_path, 70, 8, ("坐标：深圳-南山",)),
    ("v16.bottom2", smiley_path, 70, 8,
     ("每周都有聚会活动", "想参加扣777 我拉你")),
)
for label, path, size, stroke, lines in checks:
    assert path.is_file(), (label, path)
    widths = [text_width(path, size, stroke, line) for line in lines]
    assert max(widths) <= 996, (label, widths)
v10_top3_lines = ("自媒体｜AI沙龙｜", "抄经｜睡眠沙龙")
v10_top3_widths = [
    text_width(
        root / "assets/fonts/NotoSansSC-Variable.ttf", 65, 6, line,
        weight=800,
    )
    for line in v10_top3_lines
]
assert max(v10_top3_widths) <= 996, ("v10.top3", v10_top3_widths)
PY
python3 "${REFERENCE_V04_PREVIEW_CHECK_SOURCE}" \
  --pack-root "${REFERENCE_PACK_ROOT}" \
  --browser "${HYPERFRAMES_BROWSER}"
python3 "${REFERENCE_V07_PREVIEW_CHECK_SOURCE}" \
  --pack-root "${REFERENCE_PACK_ROOT}" \
  --browser "${HYPERFRAMES_BROWSER}"
REFERENCE_RUNTIME="${RELEASE}/reference-runtime"
install -d -o root -g root -m 0755 "${REFERENCE_RUNTIME}"
env ONNXRUNTIME_NODE_INSTALL_CUDA=skip "${NODE_NPM}" install \
  --prefix "${REFERENCE_RUNTIME}" --no-save --ignore-scripts --no-audit --no-fund \
  "gsap@${GSAP_VERSION}"
GSAP_SOURCE="${REFERENCE_RUNTIME}/node_modules/gsap/dist/gsap.min.js"
if [[ ! -f "${GSAP_SOURCE}" ]] || [[ -L "${GSAP_SOURCE}" ]]; then
  echo "pinned GSAP runtime is missing" >&2; exit 1
fi
if [[ "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["version"])' "${REFERENCE_RUNTIME}/node_modules/gsap/package.json")" != "${GSAP_VERSION}" ]]; then
  echo "pinned GSAP runtime version mismatch" >&2; exit 1
fi

NINE_GRID_UPSTREAM="${RELEASE}/nine-grid-upstream"
git clone --filter=blob:none --no-checkout "${UPSTREAM_URL}" "${NINE_GRID_UPSTREAM}"
git -C "${NINE_GRID_UPSTREAM}" sparse-checkout init --cone
git -C "${NINE_GRID_UPSTREAM}" sparse-checkout set \
  script-to-matrix-video/assets/templates/nine-grid-reveal \
  script-to-matrix-video/assets/templates/triple-strip-shutter \
  script-to-matrix-video/assets/templates/yellow-banner-zoom \
  script-to-matrix-video/scripts
git -C "${NINE_GRID_UPSTREAM}" checkout --detach "${NINE_GRID_UPSTREAM_COMMIT}"
git -C "${NINE_GRID_UPSTREAM}" reset --hard "${NINE_GRID_UPSTREAM_COMMIT}"
git -C "${NINE_GRID_UPSTREAM}" clean -fdx
if [[ "$(git -C "${NINE_GRID_UPSTREAM}" rev-parse HEAD)" != "${NINE_GRID_UPSTREAM_COMMIT}" ]]; then
  echo "nine-grid upstream commit mismatch" >&2; exit 1
fi
if [[ "$(sha256sum "${NINE_GRID_ADAPTER_SOURCE}" | awk '{print $1}')" != "${NINE_GRID_ADAPTER_SHA256}" ]]; then
  echo "nine-grid adapter hash mismatch" >&2; exit 1
fi
NINE_GRID_ROOT="${NINE_GRID_UPSTREAM}/script-to-matrix-video/assets/templates/nine-grid-reveal"
TRIPLE_STRIP_ROOT="${NINE_GRID_UPSTREAM}/script-to-matrix-video/assets/templates/triple-strip-shutter"
YELLOW_BANNER_ROOT="${NINE_GRID_UPSTREAM}/script-to-matrix-video/assets/templates/yellow-banner-zoom"
python3 "${NINE_GRID_ADAPTER_SOURCE}" --root "${NINE_GRID_ROOT}"
NINE_GRID_ROOT="${NINE_GRID_ROOT}" NINE_GRID_VERSION="${NINE_GRID_HYPERFRAMES_VERSION}" python3 - <<'PY'
import hashlib
import json
import os
import re
from pathlib import Path

root = Path(os.environ["NINE_GRID_ROOT"])
manifest = json.loads((root / "template.json").read_text(encoding="utf-8"))
assert manifest["id"] == "nine-grid-reveal"
assert manifest["version"] == 4
assert manifest["renderer"] == "hyperframes@" + os.environ["NINE_GRID_VERSION"]
assert manifest["canvas"] == [1080, 1920]
assert manifest["fps"] == 30 and manifest["duration"] == 12
assert manifest["text_fields"] == ["top_text", "bottom_text"]
assert manifest["text_limits"] == {"top_text": 60, "bottom_text": 80}
assert manifest["text_layout"] == {
    "mode": "semantic-then-width", "semantic_layout_required": True,
    "top_max_lines": 4, "bottom_max_lines": 4,
    "hide_edge_punctuation": True, "truncate": False,
}
required = (
    "index.html", "hyperframes.json", "index.motion.json",
    "assets/audio/reference-bgm.m4a",
    "assets/fonts/NotoSansSC-Variable.ttf",
    "assets/fonts/NotoSerifSC-Variable.ttf", "assets/vendor/gsap.min.js",
)
assert all((root / path).is_file() and not (root / path).is_symlink() for path in required)
audio = root / manifest["bgm"]["path"]
digest = hashlib.sha256(audio.read_bytes()).hexdigest()
assert digest == manifest["bgm"]["sha256"] == "d9b3d892623b9dfc9dee4f8642e2844e3700c13a9795e76b48e3a96b24ac9874"
index = (root / "index.html").read_text(encoding="utf-8")
assert index.count('data-var-text="top_text"') == 1
assert index.count('data-var-text="bottom_text"') == 1
assert 'id="matrix-nine-grid-copy-layout"' in index
assert re.search(r'<audio\b[^>]*\bid="bgm"[^>]*\bdata-volume="1"', index)
assert "data-color-grading" not in index
assert "--hf-color-grading-blur" not in index
assert "filter:'blur(10px)'" in index and "filter:'blur(0px)'" in index
assert 'id="main1-visual" class="main-visual" data-layout-allow-overflow' in index
for number in range(1, 4):
    assert re.search(
        rf'<video\b[^>]*\bid="main-video{number}"[^>]*\bdata-media-start="0"',
        index,
    )
PY
python3 "${NINE_GRID_UPSTREAM}/script-to-matrix-video/scripts/test_triple_strip.py"
python3 "${NINE_GRID_UPSTREAM}/script-to-matrix-video/scripts/test_yellow_banner.py"
NINE_GRID_RUNTIME="${RELEASE}/nine-grid-runtime"
install -d -o root -g root -m 0755 "${NINE_GRID_RUNTIME}"
if [[ "$(sha256sum "${NINE_GRID_PACKAGE_SOURCE}" | awk '{print $1}')" != "${NINE_GRID_PACKAGE_SHA256}" ]] || \
   [[ "$(sha256sum "${NINE_GRID_LOCK_SOURCE}" | awk '{print $1}')" != "${NINE_GRID_LOCK_SHA256}" ]]; then
  echo "nine-grid runtime lock files changed" >&2; exit 1
fi
install -o root -g root -m 0644 "${NINE_GRID_PACKAGE_SOURCE}" "${NINE_GRID_RUNTIME}/package.json"
install -o root -g root -m 0644 "${NINE_GRID_LOCK_SOURCE}" "${NINE_GRID_RUNTIME}/package-lock.json"
env ONNXRUNTIME_NODE_INSTALL_CUDA=skip "${NODE_NPM}" ci \
  --prefix "${NINE_GRID_RUNTIME}" --ignore-scripts --no-audit --no-fund
env ONNXRUNTIME_NODE_INSTALL_CUDA=skip "${NODE_NPM}" ls \
  --prefix "${NINE_GRID_RUNTIME}" --all --json >/dev/null
if [[ "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["version"])' "${NINE_GRID_RUNTIME}/node_modules/hyperframes/package.json")" != "${NINE_GRID_HYPERFRAMES_VERSION}" ]]; then
  echo "pinned nine-grid HyperFrames package mismatch" >&2; exit 1
fi
NODE_BINARY="$(dirname "${NODE_NPM}")/node"
NINE_GRID_CLI="${NINE_GRID_RUNTIME}/hyperframes"
cat > "${NINE_GRID_CLI}" <<EOF
#!/usr/bin/env bash
exec "${NODE_BINARY}" "${NINE_GRID_RUNTIME}/node_modules/hyperframes/bin/hyperframes.mjs" "\$@"
EOF
chmod 0755 "${NINE_GRID_CLI}"
if [[ "$("${NINE_GRID_CLI}" --version)" != "${NINE_GRID_HYPERFRAMES_VERSION}" ]]; then
  echo "nine-grid HyperFrames CLI version mismatch" >&2; exit 1
fi
BUILD_ID="$(printf '%s\n' \
  "${UPSTREAM_COMMIT}" "${REFERENCE_UPSTREAM_COMMIT}" "${NINE_GRID_UPSTREAM_COMMIT}" \
  "${LAYOUT_PATCH_SHA256}" "${REFERENCE_LAYOUT_PATCH_SHA256}" "${NINE_GRID_ADAPTER_SHA256}" \
  "${NINE_GRID_PACKAGE_SHA256}" "${NINE_GRID_LOCK_SHA256}" \
  "${HYPERFRAMES_VERSION}" "${NINE_GRID_HYPERFRAMES_VERSION}" \
  "$(sha256sum "${GSAP_SOURCE}" | awk '{print $1}')" \
  "$(sha256sum "${NINE_GRID_ROOT}/index.html" | awk '{print $1}')" \
  "$(sha256sum "${NINE_GRID_ROOT}/assets/audio/reference-bgm.m4a" | awk '{print $1}')" \
  "$(sha256sum "${TRIPLE_STRIP_ROOT}/index.html" | awk '{print $1}')" \
  "$(sha256sum "${TRIPLE_STRIP_ROOT}/assets/audio/bound-bgm.m4a" | awk '{print $1}')" \
  "$(sha256sum "${YELLOW_BANNER_ROOT}/index.html" | awk '{print $1}')" \
  "$(sha256sum "${YELLOW_BANNER_ROOT}/assets/audio/bound-bgm.m4a" | awk '{print $1}')" \
  "$(sha256sum "${RELEASE}/api.py" | awk '{print $1}')" \
  | sha256sum | awk '{print $1}')"
printf '%s\n' "${BUILD_ID}" > "${RELEASE}/BUILD_ID"

if [[ ! -e "${ENV_FILE}" ]]; then
  token="$(python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(48))
PY
)"
  umask 027
  cat > "${ENV_FILE}" <<EOF
MATRIX_TEMPLATE_API_TOKEN=${token}
MATRIX_TEMPLATE_DATA_ROOT=${STATE_ROOT}
MATRIX_TEMPLATE_SKILL_ROOT=${SOURCE_LINK}/upstream/script-to-matrix-video
MATRIX_TEMPLATE_REFERENCE_SKILL_ROOT=${SOURCE_LINK}/reference-upstream/script-to-matrix-video
MATRIX_TEMPLATE_NINE_GRID_ROOT=${SOURCE_LINK}/nine-grid-upstream/script-to-matrix-video/assets/templates/nine-grid-reveal
MATRIX_TEMPLATE_TRIPLE_STRIP_ROOT=${SOURCE_LINK}/nine-grid-upstream/script-to-matrix-video/assets/templates/triple-strip-shutter
MATRIX_TEMPLATE_YELLOW_BANNER_ROOT=${SOURCE_LINK}/nine-grid-upstream/script-to-matrix-video/assets/templates/yellow-banner-zoom
MATRIX_TEMPLATE_PYTHON=/usr/bin/python3
MATRIX_TEMPLATE_PRIVATE_FONT_ROOT=${PRIVATE_FONT_ROOT}
MATRIX_TEMPLATE_HYPERFRAMES_CLI=${HYPERFRAMES_CLI}
MATRIX_TEMPLATE_NINE_GRID_HYPERFRAMES_CLI=${SOURCE_LINK}/nine-grid-runtime/hyperframes
MATRIX_TEMPLATE_HYPERFRAMES_GSAP=${SOURCE_LINK}/reference-runtime/node_modules/gsap/dist/gsap.min.js
MATRIX_TEMPLATE_HYPERFRAMES_BROWSER=${HYPERFRAMES_BROWSER}
MATRIX_TEMPLATE_HYPERFRAMES_CONCURRENCY=2
MATRIX_TEMPLATE_HYPERFRAMES_TOTAL_TIMEOUT_SECONDS=900
MATRIX_TEMPLATE_HYPERFRAMES_SLOT_TIMEOUT_SECONDS=600
MATRIX_TEMPLATE_CONCURRENCY=5
MATRIX_TEMPLATE_RETENTION_SECONDS=259200
MATRIX_TEMPLATE_DELIVERY_GRACE_SECONDS=3600
MATRIX_TEMPLATE_CLEANUP_INTERVAL_SECONDS=900
MATRIX_TEMPLATE_CLEANUP_BATCH_SIZE=10
MATRIX_TEMPLATE_DISK_HIGH_WATER_PERCENT=95
EOF
  chown root:admin "${ENV_FILE}"
  chmod 0640 "${ENV_FILE}"
  ENV_CREATED=1
else
  env_next="$(mktemp "${BACKUP}/env.next.XXXXXX")"
  ENV_INPUT="${ENV_FILE}" ENV_OUTPUT="${env_next}" \
  REFERENCE_ROOT="${SOURCE_LINK}/reference-upstream/script-to-matrix-video" \
  NINE_GRID_ROOT_VALUE="${SOURCE_LINK}/nine-grid-upstream/script-to-matrix-video/assets/templates/nine-grid-reveal" \
  TRIPLE_STRIP_ROOT_VALUE="${SOURCE_LINK}/nine-grid-upstream/script-to-matrix-video/assets/templates/triple-strip-shutter" \
  YELLOW_BANNER_ROOT_VALUE="${SOURCE_LINK}/nine-grid-upstream/script-to-matrix-video/assets/templates/yellow-banner-zoom" \
  NINE_GRID_CLI_VALUE="${SOURCE_LINK}/nine-grid-runtime/hyperframes" \
  HYPERFRAMES_CLI_VALUE="${HYPERFRAMES_CLI}" \
  HYPERFRAMES_GSAP_VALUE="${SOURCE_LINK}/reference-runtime/node_modules/gsap/dist/gsap.min.js" \
  HYPERFRAMES_BROWSER_VALUE="${HYPERFRAMES_BROWSER}" python3 - <<'PY'
import os
from pathlib import Path

source = Path(os.environ["ENV_INPUT"])
target = Path(os.environ["ENV_OUTPUT"])
settings = {
    "MATRIX_TEMPLATE_CONCURRENCY": "5",
    "MATRIX_TEMPLATE_REFERENCE_SKILL_ROOT": os.environ["REFERENCE_ROOT"],
    "MATRIX_TEMPLATE_NINE_GRID_ROOT": os.environ["NINE_GRID_ROOT_VALUE"],
    "MATRIX_TEMPLATE_TRIPLE_STRIP_ROOT": os.environ["TRIPLE_STRIP_ROOT_VALUE"],
    "MATRIX_TEMPLATE_YELLOW_BANNER_ROOT": os.environ["YELLOW_BANNER_ROOT_VALUE"],
    "MATRIX_TEMPLATE_HYPERFRAMES_CLI": os.environ["HYPERFRAMES_CLI_VALUE"],
    "MATRIX_TEMPLATE_NINE_GRID_HYPERFRAMES_CLI": os.environ["NINE_GRID_CLI_VALUE"],
    "MATRIX_TEMPLATE_HYPERFRAMES_GSAP": os.environ["HYPERFRAMES_GSAP_VALUE"],
    "MATRIX_TEMPLATE_HYPERFRAMES_BROWSER": os.environ["HYPERFRAMES_BROWSER_VALUE"],
    "MATRIX_TEMPLATE_HYPERFRAMES_CONCURRENCY": "2",
    "MATRIX_TEMPLATE_HYPERFRAMES_TOTAL_TIMEOUT_SECONDS": "900",
    "MATRIX_TEMPLATE_HYPERFRAMES_SLOT_TIMEOUT_SECONDS": "600",
}
seen = set()
output = []
for line in source.read_text(encoding="utf-8").splitlines():
    key = line.split("=", 1)[0]
    if key in settings:
        if key not in seen:
            output.append(f"{key}={settings[key]}")
            seen.add(key)
    else:
        output.append(line)
for key in settings:
    if key not in seen:
        output.append(f"{key}={settings[key]}")
target.write_text("\n".join(output) + "\n", encoding="utf-8")
PY
  install -o root -g admin -m 0640 "${env_next}" "${ENV_FILE}"
  ENV_MUTATED=1
fi

SERVICE_MUTATED=1
systemctl stop "${SERVICE}" >/dev/null 2>&1 || true
NEXT_LINK="${RUNTIME_ROOT}/.source.next.$$"
ln -s "${RELEASE}" "${NEXT_LINK}"
SOURCE_MUTATED=1
if [[ -L "${SOURCE_LINK}" ]]; then
  PREVIOUS_SOURCE="$(readlink -f "${SOURCE_LINK}")"; mv -Tf "${NEXT_LINK}" "${SOURCE_LINK}"
elif [[ -e "${SOURCE_LINK}" ]]; then
  LEGACY_SOURCE="${RUNTIME_ROOT}/source.pre-release.$(date +%s)"; mv "${SOURCE_LINK}" "${LEGACY_SOURCE}"; mv -T "${NEXT_LINK}" "${SOURCE_LINK}"
else mv -T "${NEXT_LINK}" "${SOURCE_LINK}"; fi
NEXT_LINK=""

UNIT_MUTATED=1
install -o root -g root -m 0644 "${UNIT_SOURCE}" "${UNIT_TARGET}"
systemctl daemon-reload
systemctl enable "${SERVICE}"
systemctl start "${SERVICE}"
NEW_PID="$(systemctl show --property=MainPID --value "${SERVICE}")"
if [[ ! "${NEW_PID}" =~ ^[1-9][0-9]*$ ]] || { [[ "${OLD_PID}" -gt 0 ]] && [[ "${NEW_PID}" -eq "${OLD_PID}" ]]; }; then
  echo "matrix template service did not start a new process" >&2; exit 1
fi
for _ in $(seq 1 30); do
  response="$(curl --fail --silent --max-time 2 http://127.0.0.1:8112/health 2>/dev/null || true)"
  if EXPECTED_BUILD_ID="${BUILD_ID}" python3 -c \
      'import json,os,sys; d=json.load(sys.stdin); raise SystemExit(0 if d.get("ok") is True and d.get("build_id")==os.environ["EXPECTED_BUILD_ID"] and d.get("templates")==20 and d.get("hyperframes_templates")==17 and d.get("hyperframes_version")=="0.8.16" and d.get("nine_grid_templates")==1 and d.get("nine_grid_hyperframes_version")=="0.8.33" and d.get("fixed_skill_templates")==["triple-strip-shutter","yellow-banner-zoom"] and d.get("fixed_skill_template_count")==2 and d.get("fixed_skill_hyperframes_version")=="0.8.33" and d.get("reference_top_layer_counts")=={"2":6,"3":10,"4":1} and d.get("reference_fixed_private_fonts")==["Smiley Sans Oblique"] and d.get("reference_semantic_layout_templates")==["v01","v02","v03","v04","v05","v06","v07","v08","v09","v10","v11","v12","v13","v14","v15","v16","v17"] and d.get("material_library_ready") is True and d.get("pexels_material_ready") is True and d.get("material_source_policy")=="huangque-bookends-pexels-middle-v1" and d.get("material_selection_contract_version")==2 and d.get("material_clip_contract_version")==2 and d.get("max_batch_size")==5 and d.get("engine_concurrency")=={"ffmpeg":5,"hyperframes":2} and d.get("hyperframes_concurrency")==2 and d.get("hyperframes_total_timeout_seconds")==900 and d.get("hyperframes_slot_timeout_seconds")==600 and d.get("concurrency")==5 and d.get("worker_count")==5 else 1)' \
      <<<"${response}"; then
    SUCCEEDED=1
    [[ -n "${LEGACY_SOURCE}" && -d "${LEGACY_SOURCE}" ]] && rm -rf "${LEGACY_SOURCE}"
    echo "${SERVICE} deployed with build ${BUILD_ID} and PID ${NEW_PID}"
    exit 0
  fi
  sleep 0.5
done
echo "matrix template service health check failed; rolling back" >&2
exit 1
