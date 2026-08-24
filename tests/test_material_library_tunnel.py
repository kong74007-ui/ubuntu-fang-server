from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def bash_path():
    return shutil.which("bash") or "D:/Git/bin/bash.exe"


class MaterialLibraryTunnelTests(unittest.TestCase):
    def test_forwarding_account_is_commandless_and_single_destination(self):
        config = (ROOT / "deploy/sshd/61-huangque-material-library.conf").read_text(encoding="utf-8")
        self.assertIn("Match User material_tunnel", config)
        self.assertIn("AllowTcpForwarding local", config)
        self.assertIn("PermitOpen 127.0.0.1:8110", config)
        self.assertIn("PermitListen none", config)
        self.assertIn("ForceCommand /usr/bin/false", config)
        for forbidden in ("PasswordAuthentication yes", "PermitTTY yes", "GatewayPorts yes"):
            self.assertNotIn(forbidden, config)

    def test_generation_tunnel_is_loopback_only_and_hardened(self):
        runner = (ROOT / "deploy/pixelle-video/bin/run-material-library-tunnel").read_text(encoding="utf-8")
        unit = (ROOT / "deploy/systemd/huangque-pixelle-material-tunnel.service").read_text(encoding="utf-8")
        self.assertIn('LOCAL_PORT="${PIXELLE_MATERIAL_LOCAL_PORT:-8111}"', runner)
        self.assertIn('REMOTE_PORT="${PIXELLE_MATERIAL_REMOTE_PORT:-8110}"', runner)
        self.assertIn('-L "127.0.0.1:${LOCAL_PORT}:${REMOTE_HOST}:${REMOTE_PORT}"', runner)
        self.assertIn("StrictHostKeyChecking=yes", runner)
        self.assertIn("BatchMode=yes", runner)
        self.assertIn("NoNewPrivileges=true", unit)
        self.assertIn("ProtectSystem=strict", unit)
        self.assertIn("EnvironmentFile=/etc/huangque/pixelle-material-library.env", unit)

    def test_authorized_key_renderer_restricts_real_ed25519_key(self):
        with tempfile.TemporaryDirectory() as temp:
            key = Path(temp) / "id_ed25519"
            subprocess.run(
                ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
                check=True,
            )
            result = subprocess.run(
                [bash_path(), str(ROOT / "deploy/pixelle-video/bin/render-material-authorized-key"), str(key) + ".pub"],
                check=True,
                capture_output=True,
                text=True,
            )
        line = result.stdout.strip()
        self.assertIn('command="/usr/bin/false"', line)
        self.assertIn('permitopen="127.0.0.1:8110"', line)
        self.assertIn("restrict,port-forwarding", line)
        self.assertTrue(line.endswith("pixelle-material-library-tunnel"))

    def test_installers_are_fail_closed_and_shell_valid(self):
        material = (ROOT / "deploy/material-library/install-forwarding-account.sh").read_text(encoding="utf-8")
        generation = (ROOT / "deploy/pixelle-video/install-material-library-tunnel.sh").read_text(encoding="utf-8")
        self.assertIn("refusing to replace non-managed", material)
        self.assertIn("sshd -t", material)
        self.assertIn("PASSWORD_CHANGED", material)
        self.assertIn("root:root mode 600", generation)
        self.assertIn("pixelle-material-tunnel", generation)
        for script in (
            "deploy/material-library/install-forwarding-account.sh",
            "deploy/pixelle-video/install-material-library-tunnel.sh",
            "deploy/pixelle-video/bin/run-material-library-tunnel",
            "deploy/pixelle-video/bin/check-material-library-tunnel",
            "deploy/pixelle-video/bin/render-material-authorized-key",
            "deploy/pixelle-video/bin/check-material-library-account",
        ):
            subprocess.run([bash_path(), "-n", str(ROOT / script)], check=True)


if __name__ == "__main__":
    unittest.main()
