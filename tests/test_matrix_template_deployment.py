from __future__ import annotations

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
        self.assertIn(
            'git -C "${RELEASE}/upstream" apply --check --directory=script-to-matrix-video',
            installer,
        )
        self.assertIn(
            'git -C "${RELEASE}/upstream" apply --directory=script-to-matrix-video',
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
        self.assertIn('d.get("max_batch_size")==5', installer)
        self.assertIn('d.get("hyperframes_concurrency")==2', installer)
        self.assertIn('d.get("hyperframes_total_timeout_seconds")==900', installer)
        self.assertIn('d.get("hyperframes_slot_timeout_seconds")==600', installer)
        self.assertNotIn("MATRIX_TEMPLATE_API_TOKEN=sk-", installer)
        self.assertIn("MATRIX_TEMPLATE_RETENTION_SECONDS=259200", installer)
        self.assertIn("MATRIX_TEMPLATE_DELIVERY_GRACE_SECONDS=3600", installer)
        self.assertIn("MATRIX_TEMPLATE_CLEANUP_INTERVAL_SECONDS=900", installer)
        self.assertIn("MATRIX_TEMPLATE_CLEANUP_BATCH_SIZE=10", installer)
        self.assertIn("MATRIX_TEMPLATE_DISK_HIGH_WATER_PERCENT=95", installer)

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
