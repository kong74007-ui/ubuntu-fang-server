from __future__ import annotations

import importlib.util
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def load_renderer():
    path = ROOT / "scripts" / "render_mihomo_subscription_config.py"
    spec = importlib.util.spec_from_file_location(
        "render_mihomo_subscription_config", path
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_idle_checker():
    path = ROOT / "scripts" / "check_pixelle_idle.py"
    spec = importlib.util.spec_from_file_location("check_pixelle_idle", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def find_bash() -> str:
    bash = shutil.which("bash")
    if bash:
        return bash
    candidate = Path("D:/Git/bin/bash.exe")
    if candidate.exists():
        return str(candidate)
    raise unittest.SkipTest("bash is required for Mihomo deployment tests")


class MihomoSubscriptionDeploymentTests(unittest.TestCase):
    def test_pixelle_unit_requires_ready_mihomo_proxy(self):
        unit = (ROOT / "deploy/systemd/huangque-pixelle-video.service").read_text(
            encoding="utf-8"
        )
        self.assertIn("After=network-online.target mihomo-new.service", unit)
        self.assertIn("Requires=mihomo-new.service", unit)
        self.assertNotIn("huangque-egress-tunnel.service", unit)

    def test_mihomo_unit_waits_for_openai_proxy_readiness(self):
        unit = (ROOT / "deploy/systemd/mihomo-new.service").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "ExecStartPost=/usr/local/libexec/huangque/check-mihomo-openai-proxy",
            unit,
        )

    def test_idle_checker_allows_only_terminal_task_history(self):
        checker = load_idle_checker()
        checker.require_idle(
            [
                {"task_id": "one", "status": "completed"},
                {"task_id": "two", "status": "failed"},
                {"task_id": "three", "status": "cancelled"},
            ]
        )

    def test_idle_checker_rejects_active_unknown_and_invalid_payloads(self):
        checker = load_idle_checker()
        for payload in (
            [{"task_id": "one", "status": "running"}],
            [{"task_id": "two", "status": "new-provider-state"}],
            {"tasks": []},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(RuntimeError):
                    checker.require_idle(payload)

    def test_renderer_requires_subscription_url(self):
        renderer = load_renderer()
        with self.assertRaisesRegex(ValueError, "GRAYFOX_SUBSCRIPTION_URL"):
            renderer.render({})

    def test_renderer_rejects_non_https_subscription_url(self):
        renderer = load_renderer()
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            renderer.render(
                {"GRAYFOX_SUBSCRIPTION_URL": "http://proxy.example.test/sub?token=x"}
            )

    def test_renderer_builds_loopback_subscription_proxy(self):
        renderer = load_renderer()
        rendered = renderer.render(
            {"GRAYFOX_SUBSCRIPTION_URL": "https://proxy.example.test/sub?token=x"}
        )

        self.assertIn("mixed-port: 7999", rendered)
        self.assertIn('bind-address: "127.0.0.1"', rendered)
        self.assertIn('url: "https://proxy.example.test/sub?token=x"', rendered)
        self.assertIn('User-Agent: ["clash.meta"]', rendered)
        self.assertIn("type: url-test", rendered)
        self.assertIn("MATCH,GRAYFOX_AUTO", rendered)

    def test_main_writes_private_config_atomically(self):
        renderer = load_renderer()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            env_path = root / "mihomo.env"
            output = root / "config.yaml"
            env_path.write_text(
                "GRAYFOX_SUBSCRIPTION_URL=https://proxy.example.test/sub?token=x\n",
                encoding="utf-8",
            )

            with patch(
                "sys.argv",
                [
                    "render_mihomo_subscription_config.py",
                    "--env",
                    str(env_path),
                    "--output",
                    str(output),
                ],
            ):
                self.assertEqual(0, renderer.main())

            self.assertTrue(output.is_file())
            self.assertFalse(output.with_name("config.yaml.tmp").exists())
            if os.name != "nt":
                self.assertEqual(0o600, stat.S_IMODE(output.stat().st_mode))

    def test_install_contract_validates_before_restart(self):
        installer = (ROOT / "deploy/mihomo-new/install.sh").read_text(
            encoding="utf-8"
        )
        render_index = installer.index("render_mihomo_subscription_config.py")
        idle_index = installer.index("check_pixelle_idle.py")
        stop_index = installer.index("systemctl stop huangque-pixelle-video.service")
        validate_index = installer.index("mihomo -t")
        activate_index = installer.index('mv -f "${NEXT_CONFIG}" "${CONFIG_PATH}"')
        restart_index = installer.index(
            "systemctl restart mihomo-new.service", activate_index
        )

        self.assertLess(idle_index, stop_index)
        self.assertLess(stop_index, activate_index)
        self.assertLess(render_index, validate_index)
        self.assertLess(validate_index, activate_index)
        self.assertLess(activate_index, restart_index)
        self.assertIn("/etc/huangque/mihomo-new.env", installer)
        self.assertIn("chmod 0600", installer)
        probe = (ROOT / "deploy/mihomo-new/check_openai_proxy.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("curl --proxy http://127.0.0.1:7999", probe)
        self.assertIn('[[ "${OPENAI_STATUS}" == "401" ]]', probe)
        self.assertIn("rollback_config", installer)
        self.assertIn("backup_managed_files", installer)
        self.assertIn("restore_managed_files", installer)
        self.assertIn("systemctl daemon-reload", installer)
        self.assertIn("systemctl start huangque-pixelle-video.service", installer)

    def test_failed_install_restores_all_managed_files(self):
        installer = (ROOT / "deploy/mihomo-new/install.sh").as_posix()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            script = f'''source "{installer}"
CONFIG_PATH="{(root / "config.yaml").as_posix()}"
UNIT_TARGET="{(root / "mihomo.service").as_posix()}"
PIXELLE_UNIT_TARGET="{(root / "pixelle.service").as_posix()}"
CHECK_TARGET="{(root / "check-proxy").as_posix()}"
BACKUP_DIR="{(root / "backup").as_posix()}"
mkdir -p "$BACKUP_DIR"
printf old-config > "$BACKUP_DIR/config.yaml"
printf old-unit > "$BACKUP_DIR/mihomo-new.service"
printf old-pixelle > "$BACKUP_DIR/huangque-pixelle-video.service"
printf old-check > "$BACKUP_DIR/check-mihomo-openai-proxy"
printf new-config > "$CONFIG_PATH"
printf new-unit > "$UNIT_TARGET"
printf new-pixelle > "$PIXELLE_UNIT_TARGET"
printf new-check > "$CHECK_TARGET"
CONFIG_EXISTED=1
UNIT_EXISTED=1
PIXELLE_UNIT_EXISTED=1
CHECK_EXISTED=1
systemctl() {{ :; }}
restore_managed_files
printf '%s|' "$(cat "$CONFIG_PATH")" "$(cat "$UNIT_TARGET")" \
  "$(cat "$PIXELLE_UNIT_TARGET")" "$(cat "$CHECK_TARGET")"
'''
            result = subprocess.run(
                [find_bash(), "-c", script],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(
                "old-config|old-unit|old-pixelle|old-check|", result.stdout
            )

    def test_successful_cleanup_keeps_new_managed_files(self):
        installer = (ROOT / "deploy/mihomo-new/install.sh").as_posix()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            script = f'''source "{installer}"
CONFIG_PATH="{(root / "config.yaml").as_posix()}"
UNIT_TARGET="{(root / "mihomo.service").as_posix()}"
PIXELLE_UNIT_TARGET="{(root / "pixelle.service").as_posix()}"
CHECK_TARGET="{(root / "check-proxy").as_posix()}"
BACKUP_DIR="{(root / "backup").as_posix()}"
mkdir -p "$BACKUP_DIR"
printf old-config > "$BACKUP_DIR/config.yaml"
printf old-unit > "$BACKUP_DIR/mihomo-new.service"
printf old-pixelle > "$BACKUP_DIR/huangque-pixelle-video.service"
printf old-check > "$BACKUP_DIR/check-mihomo-openai-proxy"
printf new-config > "$CONFIG_PATH"
printf new-unit > "$UNIT_TARGET"
printf new-pixelle > "$PIXELLE_UNIT_TARGET"
printf new-check > "$CHECK_TARGET"
CONFIG_EXISTED=1
UNIT_EXISTED=1
PIXELLE_UNIT_EXISTED=1
CHECK_EXISTED=1
MANAGED_FILES_BACKED_UP=1
DEPLOY_SUCCEEDED=1
systemctl() {{ :; }}
cleanup
printf '%s|' "$(cat "$CONFIG_PATH")" "$(cat "$UNIT_TARGET")" \
  "$(cat "$PIXELLE_UNIT_TARGET")" "$(cat "$CHECK_TARGET")"
'''
            result = subprocess.run(
                [find_bash(), "-c", script],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(
                "new-config|new-unit|new-pixelle|new-check|", result.stdout
            )

    def test_repository_contains_no_real_subscription_token(self):
        example = (ROOT / "deploy/mihomo-new/env.example").read_text(
            encoding="utf-8"
        )
        self.assertIn("GRAYFOX_SUBSCRIPTION_URL=<", example)
        self.assertNotIn("token=", example)


if __name__ == "__main__":
    unittest.main()
