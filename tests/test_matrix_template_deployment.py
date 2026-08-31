from __future__ import annotations

import hashlib
import shutil
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class MatrixTemplateDeploymentTests(unittest.TestCase):
    def test_installer_pins_skill_and_uses_atomic_release_contract(self):
        installer = (ROOT / "deploy/matrix-template-video/install.sh").read_text(encoding="utf-8")
        self.assertIn('UPSTREAM_COMMIT="243d5c168d9ab2d95daf04fef5c5e75924114eb8"', installer)
        self.assertIn('REFERENCE_UPSTREAM_COMMIT="9040a24139372f14346816cf42a97271767a0777"', installer)
        self.assertIn('HYPERFRAMES_VERSION="0.8.16"', installer)
        self.assertIn('GSAP_VERSION="3.14.2"', installer)
        self.assertIn('LAYOUT_PATCH_SHA256="33f64143e481301bcfd0f157ce1398c590d2e41512e2ea930772d739b4651329"', installer)
        self.assertIn('REFERENCE_LAYOUT_PATCH_SHA256="9460cb37306ef3efbb0a1bb4277ca010560c903726c9c89461468840b710000e"', installer)
        self.assertIn(
            'git -C "${RELEASE}/upstream" apply --check --directory=script-to-matrix-video',
            installer,
        )
        self.assertIn(
            'git -C "${RELEASE}/upstream" apply --directory=script-to-matrix-video',
            installer,
        )
        self.assertIn(
            'git -C "${REFERENCE_UPSTREAM}" apply --check "${REFERENCE_LAYOUT_PATCH_SOURCE}"',
            installer,
        )
        self.assertIn(
            'git -C "${REFERENCE_UPSTREAM}" apply "${REFERENCE_LAYOUT_PATCH_SOURCE}"',
            installer,
        )
        self.assertIn('python3 "${SKILL_ROOT}/scripts/test_private_domain_layouts.py"', installer)
        self.assertIn('python3 "${SKILL_ROOT}/scripts/test_private_domain_catalog.py"', installer)
        self.assertIn('python3 "${SKILL_ROOT}/scripts/restrict_private_domain_catalog.py"', installer)
        self.assertIn('PRIVATE_FONT_ROOT="${STATE_ROOT}/private-fonts"', installer)
        self.assertIn('MATRIX_TEMPLATE_PRIVATE_FONT_ROOT=${PRIVATE_FONT_ROOT}', installer)
        self.assertIn('MATRIX_TEMPLATE_REFERENCE_SKILL_ROOT=${SOURCE_LINK}/reference-upstream/script-to-matrix-video', installer)
        self.assertIn('MATRIX_TEMPLATE_HYPERFRAMES_CLI=${HYPERFRAMES_CLI}', installer)
        self.assertIn('MATRIX_TEMPLATE_HYPERFRAMES_CONCURRENCY=2', installer)
        self.assertIn('MATRIX_TEMPLATE_HYPERFRAMES_TOTAL_TIMEOUT_SECONDS=900', installer)
        self.assertIn('MATRIX_TEMPLATE_HYPERFRAMES_SLOT_TIMEOUT_SECONDS=600', installer)
        self.assertIn('MATRIX_TEMPLATE_CONCURRENCY=5', installer)
        self.assertIn('d.get("concurrency")==5', installer)
        self.assertIn('d.get("worker_count")==5', installer)
        self.assertIn('requires at least 4 vCPU and 7 GiB RAM', installer)
        self.assertIn('install -o root -g admin -m 0640 "${BACKUP}/env" "${ENV_FILE}"', installer)
        self.assertIn('git -C "${RELEASE}/upstream" rev-parse HEAD', installer)
        self.assertIn('systemctl stop "${SERVICE}"', installer)
        self.assertIn('systemctl start "${SERVICE}"', installer)
        self.assertIn('d.get("build_id")==os.environ["EXPECTED_BUILD_ID"]', installer)
        self.assertIn('d.get("templates")==19', installer)
        self.assertIn('d.get("hyperframes_templates")==17', installer)
        self.assertIn('d.get("hyperframes_version")=="0.8.16"', installer)
        self.assertIn('d.get("reference_top_layer_counts")=={"2":6,"3":11}', installer)
        self.assertIn('d.get("reference_fixed_private_fonts")==["Smiley Sans Oblique"]', installer)
        self.assertIn(
            'd.get("reference_semantic_layout_templates")==["v01","v02","v03","v04","v05","v06","v07","v08","v09","v10","v11","v12","v13","v14","v15","v16","v17"]',
            installer,
        )
        self.assertIn("from PIL import Image, ImageDraw, ImageFont", installer)
        self.assertIn('sample = "AI视频获客增长100条"', installer)
        self.assertIn("font.set_variation_by_axes(values)", installer)
        self.assertIn("widths[400] <= 996 < widths[900]", installer)
        self.assertIn('xiaowei_sample = "MMMMMMMMMMMMMM"', installer)
        self.assertIn("970 < xiaowei_width <= 996", installer)
        self.assertIn('d.get("max_batch_size")==5', installer)
        self.assertIn('d.get("engine_concurrency")=={"ffmpeg":5,"hyperframes":2}', installer)
        self.assertIn('d.get("hyperframes_concurrency")==2', installer)
        self.assertIn('d.get("hyperframes_total_timeout_seconds")==900', installer)
        self.assertIn('d.get("hyperframes_slot_timeout_seconds")==600', installer)
        self.assertNotIn("MATRIX_TEMPLATE_API_TOKEN=sk-", installer)
        self.assertIn("MATRIX_TEMPLATE_RETENTION_SECONDS=259200", installer)
        self.assertIn("MATRIX_TEMPLATE_DELIVERY_GRACE_SECONDS=3600", installer)
        self.assertIn("MATRIX_TEMPLATE_CLEANUP_INTERVAL_SECONDS=900", installer)
        self.assertIn("MATRIX_TEMPLATE_CLEANUP_BATCH_SIZE=10", installer)
        self.assertIn("MATRIX_TEMPLATE_DISK_HIGH_WATER_PERCENT=95", installer)

    def test_reference_patch_is_hash_locked_and_scoped_to_v01_and_v05(self):
        patch_path = (
            ROOT / "deploy/matrix-template-video/reference-featured-layout.patch"
        )
        patch = patch_path.read_text(encoding="utf-8")
        self.assertEqual(
            "9460cb37306ef3efbb0a1bb4277ca010560c903726c9c89461468840b710000e",
            hashlib.sha256(patch_path.read_bytes()).hexdigest(),
        )
        self.assertIn(
            "script-to-matrix-video/assets/templates/reference-typography-17/index.html",
            patch,
        )
        self.assertIn('font: 900 102px/1.02 "NotoSC";', patch)
        self.assertIn("-webkit-text-stroke: 12px #203449;", patch)
        self.assertIn("background: #f4c900;", patch)
        self.assertIn('font: 400 70px/1.08 "MaShan";', patch)
        self.assertIn('font: 400 64px/1.15 "MaShan";', patch)
        self.assertIn("font-size: 52px;", patch)
        self.assertIn('font: 400 56px/1.05 "MaShan";', patch)
        self.assertIn('font: 400 74px/1.15 "MaShan";', patch)
        self.assertNotIn(".v02 .top1 {\n+", patch)
        self.assertNotIn(".v04 .top1 {\n+", patch)
        self.assertNotIn(".v06 .top1 {\n+", patch)

    def test_systemd_is_loopback_hardened_and_reuses_material_tunnel(self):
        unit = (ROOT / "deploy/systemd/huangque-matrix-template.service").read_text(encoding="utf-8")
        self.assertIn("--host 127.0.0.1 --port 8112", unit)
        self.assertIn("Requires=huangque-pixelle-material-tunnel.service", unit)
        self.assertIn("EnvironmentFile=/etc/huangque/pixelle-material-library.env", unit)
        self.assertIn("NoNewPrivileges=true", unit)
        self.assertIn("ProtectSystem=strict", unit)
        self.assertIn("ReadWritePaths=/var/lib/huangque-matrix-template", unit)
        self.assertIn("MemoryMax=6G", unit)
        self.assertIn("CPUQuota=400%", unit)

    def test_nginx_bridge_is_private_to_production_content_host(self):
        for relative in (
            "deploy/nginx-fang-locations.conf",
            "deploy/nginx-huangquechuanmei.conf",
        ):
            nginx = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIn("/internal/matrix-template/", nginx)
            self.assertIn("allow 129.204.166.13;", nginx)
            self.assertIn("deny all;", nginx)
            self.assertIn("proxy_pass http://127.0.0.1:8112/;", nginx)

    def test_installer_shell_syntax(self):
        bash = shutil.which("bash") or "D:/Git/bin/bash.exe"
        subprocess.run([
            bash, "-n", str(ROOT / "deploy/matrix-template-video/install.sh")
        ], check=True)


if __name__ == "__main__":
    unittest.main()
