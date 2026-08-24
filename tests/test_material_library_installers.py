from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BASH = "D:/Git/bin/bash.exe" if os.name == "nt" else "/bin/bash"


def executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8", newline="\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


@unittest.skipIf(os.name == "nt", "requires Unix installer semantics")
class MaterialLibraryInstallerTests(unittest.TestCase):
    def _fixture(self, root: Path):
        library = root / "library"
        (library / "files").mkdir(parents=True)
        payload = b"approved"
        sha = hashlib.sha256(payload).hexdigest()
        (library / "files" / "asset.jpg").write_bytes(payload)
        (library / "index.jsonl").write_text(json.dumps({
            "record_id": "asset", "sha256": sha, "素材名称": "asset",
            "状态": "可使用", "画面方向": "竖屏",
            "server_relative_path": "files/asset.jpg",
        }, ensure_ascii=False) + "\n", encoding="utf-8")

        runtime = root / "runtime"
        source = runtime / "source"
        (source / "server").mkdir(parents=True)
        (source / "BUILD_ID").write_text("0" * 64, encoding="ascii")
        unit = root / "service.unit"
        unit.write_text("old-unit", encoding="utf-8")
        env_file = root / "service.env"
        env_file.write_text(
            f"MATERIAL_LIBRARY_ROOT={library.as_posix()}\nMATERIAL_LIBRARY_API_TOKEN=test\n",
            encoding="utf-8",
        )
        state = root / "state"
        state.mkdir()
        (state / "active").write_text("1")
        (state / "enabled").write_text("1")
        (state / "pid").write_text("100")
        (state / "trace").write_text("")
        return library, runtime, source, unit, env_file, state

    def _fake_bin(self, root: Path) -> Path:
        fake = root / "bin"
        fake.mkdir()
        executable(fake / "python3", f'#!/bin/bash\nexec "{Path(sys.executable).as_posix()}" "$@"\n')
        executable(fake / "install", r'''#!/bin/bash
set -e
directory=0
args=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    -d) directory=1; shift ;;
    -o|-g|-m) shift 2 ;;
    *) args+=("$1"); shift ;;
  esac
done
if [[ "$directory" -eq 1 ]]; then
  for path in "${args[@]}"; do mkdir -p "$path"; done
else
  src="${args[0]}"; dst="${args[1]}"; mkdir -p "$(dirname "$dst")"; cp -p "$src" "$dst"
fi
''')
        executable(fake / "systemctl", r'''#!/bin/bash
set -e
echo "$*" >> "$TEST_STATE_DIR/trace"
case "$1" in
  is-active) [[ "$(cat "$TEST_STATE_DIR/active")" = 1 ]] ;;
  is-enabled) [[ "$(cat "$TEST_STATE_DIR/enabled")" = 1 ]] ;;
  show) cat "$TEST_STATE_DIR/pid" ;;
  stop) echo 0 > "$TEST_STATE_DIR/active" ;;
  start|restart)
    echo 1 > "$TEST_STATE_DIR/active"
    value=$(cat "$TEST_STATE_DIR/pid"); echo $((value + 1)) > "$TEST_STATE_DIR/pid"
    ;;
  enable) echo 1 > "$TEST_STATE_DIR/enabled" ;;
  disable) echo 0 > "$TEST_STATE_DIR/enabled" ;;
  daemon-reload|status) ;;
  *) exit 2 ;;
esac
''')
        executable(fake / "curl", r'''#!/bin/bash
set -e
[[ "$(cat "$TEST_STATE_DIR/active")" = 1 ]]
build=$(cat "$TEST_SOURCE_LINK/BUILD_ID")
if [[ "${TEST_FAIL_NEW_BUILD:-0}" = 1 && "$build" != "$TEST_OLD_BUILD_ID" ]]; then exit 22; fi
printf '{"ok":true,"build_id":"%s","records":1}\n' "$build"
''')
        return fake

    def _environment(self, root, library, runtime, unit, env_file, state, fake):
        env = os.environ.copy()
        env.update({
            "PATH": str(fake) + os.pathsep + env["PATH"],
            "SOURCE_ROOT": str(ROOT),
            "RUNTIME_ROOT": str(runtime),
            "MATERIAL_LIBRARY_ROOT": str(library),
            "ENV_FILE": str(env_file),
            "UNIT_PATH": str(unit),
            "MATERIAL_LIBRARY_INSTALL_TEST_MODE": "1",
            "MATERIAL_LIBRARY_BACKUP_ROOT": str(root),
            "MATERIAL_LIBRARY_HEALTH_ATTEMPTS": "2",
            "MATERIAL_LIBRARY_HEALTH_SLEEP": "0",
            "TEST_STATE_DIR": str(state),
            "TEST_SOURCE_LINK": str(runtime / "source"),
            "TEST_OLD_BUILD_ID": "0" * 64,
        })
        return env

    def test_running_upgrade_loads_new_pid_and_exact_build(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            library, runtime, _, unit, env_file, state = self._fixture(root)
            fake = self._fake_bin(root)
            env = self._environment(root, library, runtime, unit, env_file, state, fake)
            result = subprocess.run(
                [BASH, str(ROOT / "deploy/material-library/install.sh")],
                env=env, capture_output=True, text=True,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertNotEqual("0" * 64, (runtime / "source" / "BUILD_ID").read_text().strip())
            self.assertEqual("101", (state / "pid").read_text().strip())
            self.assertIn("stop huangque-material-library.service", (state / "trace").read_text())
            self.assertIn("start huangque-material-library.service", (state / "trace").read_text())

    def test_failed_new_build_restores_old_release_unit_and_service_state(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            library, runtime, _, unit, env_file, state = self._fixture(root)
            fake = self._fake_bin(root)
            env = self._environment(root, library, runtime, unit, env_file, state, fake)
            env["TEST_FAIL_NEW_BUILD"] = "1"
            result = subprocess.run(
                [BASH, str(ROOT / "deploy/material-library/install.sh")],
                env=env, capture_output=True, text=True,
            )
            self.assertNotEqual(0, result.returncode)
            self.assertEqual("0" * 64, (runtime / "source" / "BUILD_ID").read_text().strip())
            self.assertEqual("old-unit", unit.read_text())
            self.assertEqual("1", (state / "active").read_text().strip())
            self.assertEqual("1", (state / "enabled").read_text().strip())

    def test_early_preflight_failure_does_not_call_systemctl_or_change_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            library, runtime, source, unit, env_file, state = self._fixture(root)
            fake = self._fake_bin(root)
            env = self._environment(root, library, runtime, unit, env_file, state, fake)
            env["SOURCE_ROOT"] = str(root / "missing-source")
            before = (source / "BUILD_ID").read_bytes(), unit.read_bytes(), env_file.read_bytes()
            result = subprocess.run(
                [BASH, str(ROOT / "deploy/material-library/install.sh")],
                env=env, capture_output=True, text=True,
            )
            self.assertNotEqual(0, result.returncode)
            self.assertEqual("", (state / "trace").read_text())
            self.assertEqual(before, ((source / "BUILD_ID").read_bytes(), unit.read_bytes(), env_file.read_bytes()))

    def test_forwarding_installer_early_gates_leave_existing_config_and_key_untouched(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = root / "sshd.conf"
            config.write_text("existing-config", encoding="utf-8")
            home = root / "home"
            ssh_dir = home / ".ssh"
            ssh_dir.mkdir(parents=True)
            key = ssh_dir / "authorized_keys"
            key.write_text("existing-key", encoding="utf-8")
            env = os.environ.copy()
            env.update({
                "MATERIAL_LIBRARY_INSTALL_TEST_MODE": "1",
                "MATERIAL_LIBRARY_TEST_CONFIG_TARGET": str(config),
                "MATERIAL_LIBRARY_TEST_TUNNEL_HOME": str(home),
                "MATERIAL_TUNNEL_SOURCE_ADDRESS": "203.0.113.10",
            })
            result = subprocess.run(
                [BASH, str(ROOT / "deploy/material-library/install-forwarding-account.sh"), str(root / "missing.pub")],
                env=env, capture_output=True, text=True,
            )
            self.assertNotEqual(0, result.returncode)
            self.assertEqual("existing-config", config.read_text())
            self.assertEqual("existing-key", key.read_text())


if __name__ == "__main__":
    unittest.main()
