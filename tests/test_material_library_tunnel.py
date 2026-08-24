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
        self.assertIn("check-pixelle-material-command-denied", unit)
        self.assertIn("check-pixelle-material-remote-forward-denied", unit)

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
        self.assertIn("MATERIAL_TUNNEL_SOURCE_ADDRESS", material)
        checker = (ROOT / "deploy/pixelle-video/bin/check-material-library-account").read_text(encoding="utf-8")
        for rule in ("allowstreamlocalforwarding no", "gatewayports no", "permittunnel no", "permituserrc no"):
            self.assertIn(rule, checker)
        self.assertIn("root:root mode 600", generation)
        self.assertIn("pixelle-material-tunnel", generation)
        for script in (
            "deploy/material-library/install-forwarding-account.sh",
            "deploy/pixelle-video/install-material-library-tunnel.sh",
            "deploy/pixelle-video/bin/run-material-library-tunnel",
            "deploy/pixelle-video/bin/check-material-library-tunnel",
            "deploy/pixelle-video/bin/render-material-authorized-key",
            "deploy/pixelle-video/bin/check-material-library-account",
            "deploy/pixelle-video/bin/check-material-command-denied",
            "deploy/pixelle-video/bin/check-material-remote-forward-denied",
            "deploy/material-library/lib/rollback.sh",
        ):
            subprocess.run([bash_path(), "-n", str(ROOT / script)], check=True)

    def test_existing_account_without_key_removes_new_key_on_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            ssh_dir = Path(temp) / ".ssh"
            ssh_dir.mkdir()
            authorized = ssh_dir / "authorized_keys"
            authorized.write_text("new-key", encoding="utf-8")
            command = f'''source "{(ROOT / "deploy/material-library/lib/rollback.sh").as_posix()}"
material_restore_authorized_key "{authorized.as_posix()}" 0 0 0 "{ssh_dir.as_posix()}" "{(Path(temp) / "missing").as_posix()}" ignored
[[ ! -e "{authorized.as_posix()}" && ! -d "{ssh_dir.as_posix()}" ]]
'''
            subprocess.run([bash_path(), "-c", command], check=True)

    def test_failed_release_restores_source_unit_and_cleans_first_install_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            source.write_text("new", encoding="utf-8")
            legacy = root / "legacy"
            legacy.mkdir()
            (legacy / "version").write_text("old", encoding="utf-8")
            unit = root / "service.unit"
            unit.write_text("new-unit", encoding="utf-8")
            backup = root / "old.unit"
            backup.write_text("old-unit", encoding="utf-8")
            env_file = root / "created.env"
            env_file.write_text("token", encoding="utf-8")
            release = root / "release"
            release.mkdir()
            command = f'''source "{(ROOT / "deploy/material-library/lib/rollback.sh").as_posix()}"
material_restore_release "{source.as_posix()}" "" "{legacy.as_posix()}" 1 "{backup.as_posix()}" "{unit.as_posix()}" 1 "{env_file.as_posix()}" "{release.as_posix()}"
[[ "$(cat "{source.as_posix()}/version")" = old ]]
[[ "$(cat "{unit.as_posix()}")" = old-unit ]]
[[ ! -e "{env_file.as_posix()}" && ! -e "{release.as_posix()}" ]]
'''
            subprocess.run([bash_path(), "-c", command], check=True)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source"
            source.write_text("new", encoding="utf-8")
            unit = root / "service.unit"
            unit.write_text("new-unit", encoding="utf-8")
            env_file = root / "created.env"
            env_file.write_text("token", encoding="utf-8")
            release = root / "release"
            release.mkdir()
            command = f'''source "{(ROOT / "deploy/material-library/lib/rollback.sh").as_posix()}"
material_restore_release "{source.as_posix()}" "" "" 0 "{(root / "missing-unit").as_posix()}" "{unit.as_posix()}" 1 "{env_file.as_posix()}" "{release.as_posix()}"
[[ ! -e "{source.as_posix()}" && ! -e "{unit.as_posix()}" ]]
[[ ! -e "{env_file.as_posix()}" && ! -e "{release.as_posix()}" ]]
'''
            subprocess.run([bash_path(), "-c", command], check=True)


if __name__ == "__main__":
    unittest.main()
