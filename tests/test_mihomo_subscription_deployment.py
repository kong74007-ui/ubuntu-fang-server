from __future__ import annotations

import importlib.util
import os
import stat
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


class MihomoSubscriptionDeploymentTests(unittest.TestCase):
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
        validate_index = installer.index("mihomo -t")
        activate_index = installer.index('mv -f "${NEXT_CONFIG}" "${CONFIG_PATH}"')
        restart_index = installer.index(
            "systemctl restart mihomo-new.service", activate_index
        )

        self.assertLess(render_index, validate_index)
        self.assertLess(validate_index, activate_index)
        self.assertLess(activate_index, restart_index)
        self.assertIn("/etc/huangque/mihomo-new.env", installer)
        self.assertIn("chmod 0600", installer)
        self.assertIn("curl --proxy http://127.0.0.1:7999", installer)
        self.assertIn('[[ "${OPENAI_STATUS}" != "401" ]]', installer)
        self.assertIn("rollback_config", installer)

    def test_repository_contains_no_real_subscription_token(self):
        example = (ROOT / "deploy/mihomo-new/env.example").read_text(
            encoding="utf-8"
        )
        self.assertIn("GRAYFOX_SUBSCRIPTION_URL=<", example)
        self.assertNotIn("token=", example)


if __name__ == "__main__":
    unittest.main()
