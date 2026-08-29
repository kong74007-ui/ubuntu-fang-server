from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from server import material_library_permissions as permissions


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "posix", "requires POSIX ownership and mode semantics")
class MaterialLibraryPermissionTests(unittest.TestCase):
    def _publish_root_owned_style(self, root: Path, generation: int) -> dict[str, bytes]:
        expected = {}
        for name in permissions.METADATA_NAMES:
            content = f"generation={generation};name={name}\n".encode()
            descriptor, temporary = tempfile.mkstemp(prefix=f".{name}.", dir=root)
            try:
                os.write(descriptor, content)
                os.fsync(descriptor)
                os.fchmod(descriptor, 0o600)
            finally:
                os.close(descriptor)
            os.replace(temporary, root / name)
            expected[name] = content
        return expected

    def test_two_atomic_rebuilds_keep_content_and_restore_readable_permissions(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for generation in (1, 2):
                expected = self._publish_root_owned_style(root, generation)
                before = {
                    name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                    for name in permissions.METADATA_NAMES
                }
                result = permissions.normalize_metadata_permissions(
                    root, os.getuid(), os.getgid()
                )
                self.assertEqual(list(permissions.METADATA_NAMES), [item["name"] for item in result])
                self.assertTrue(all(item["changed"] for item in result))
                for name in permissions.METADATA_NAMES:
                    path = root / name
                    self.assertEqual(expected[name], path.read_bytes())
                    self.assertEqual(before[name], hashlib.sha256(path.read_bytes()).hexdigest())
                    self.assertEqual(0o644, stat.S_IMODE(path.stat().st_mode))
                    self.assertEqual(os.getuid(), path.stat().st_uid)
                    self.assertEqual(os.getgid(), path.stat().st_gid)

                before_ctime = {
                    name: (root / name).stat().st_ctime_ns
                    for name in permissions.METADATA_NAMES
                }
                repeated = permissions.normalize_metadata_permissions(
                    root, os.getuid(), os.getgid()
                )
                self.assertTrue(all(not item["changed"] for item in repeated))
                self.assertEqual(before_ctime, {
                    name: (root / name).stat().st_ctime_ns
                    for name in permissions.METADATA_NAMES
                })

    def test_symbolic_link_is_rejected_without_touching_target(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            external = root.parent / (root.name + "-external")
            external.write_bytes(b"do-not-touch")
            external.chmod(0o600)
            try:
                (root / "index.jsonl").symlink_to(external)
                (root / "index.csv").write_bytes(b"csv")
                (root / "stats.json").write_bytes(b"{}")
                with self.assertRaises(permissions.MetadataPermissionError):
                    permissions.normalize_metadata_permissions(root, os.getuid(), os.getgid())
                self.assertEqual(b"do-not-touch", external.read_bytes())
                self.assertEqual(0o600, stat.S_IMODE(external.stat().st_mode))
            finally:
                external.unlink(missing_ok=True)


class MaterialLibraryPermissionDeploymentTests(unittest.TestCase):
    def test_path_unit_watches_only_canonical_metadata(self):
        unit = (ROOT / "deploy/systemd/huangque-material-library-index-permissions.path").read_text()
        for name in permissions.METADATA_NAMES:
            self.assertIn(
                f"PathChanged=/home/ubuntu/material-libraries/huangque-media/{name}",
                unit,
            )
        self.assertNotIn("PathChanged=/home/ubuntu/material-libraries/huangque-media/files", unit)

    def test_service_is_root_scoped_and_link_safe_helper_is_installed(self):
        service = (ROOT / "deploy/systemd/huangque-material-library-index-permissions.service").read_text()
        installer = (ROOT / "deploy/material-library/install-index-permission-guard.sh").read_text()
        self.assertIn("User=root", service)
        self.assertIn(
            "CapabilityBoundingSet=CAP_CHOWN CAP_DAC_READ_SEARCH CAP_FOWNER",
            service,
        )
        self.assertIn("ReadWritePaths=/home/ubuntu/material-libraries/huangque-media", service)
        self.assertIn("huangque-material-library-index-permissions", service)
        self.assertIn('systemctl enable --now "${PATH_UNIT}"', installer)
        self.assertIn('actual="$(stat -c \'%U:%G:%a\'', installer)
        self.assertIn('[[ -L "${target}" ]]', installer)


@unittest.skipUnless(os.name == "posix", "requires POSIX installer semantics")
class MaterialLibraryPermissionInstallerTests(unittest.TestCase):
    def _executable(self, path: Path, content: str) -> None:
        path.write_text(content, encoding="utf-8", newline="\n")
        path.chmod(0o755)

    def _fixture(self, root: Path):
        import grp
        import pwd

        library = root / "library"
        library.mkdir()
        expected = {}
        for name in permissions.METADATA_NAMES:
            content = ("fixture:" + name).encode()
            path = library / name
            path.write_bytes(content)
            path.chmod(0o600)
            expected[name] = content
        targets = root / "targets"
        targets.mkdir()
        state = root / "state"
        state.mkdir()
        (state / "active").write_text("0")
        (state / "enabled").write_text("0")
        (state / "trace").write_text("")
        fake = root / "bin"
        fake.mkdir()
        self._executable(fake / "install", r'''#!/bin/bash
set -e
directory=0
mode=""
args=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    -d) directory=1; shift ;;
    -o|-g) shift 2 ;;
    -m) mode="$2"; shift 2 ;;
    *) args+=("$1"); shift ;;
  esac
done
if [[ "$directory" -eq 1 ]]; then
  for path in "${args[@]}"; do mkdir -p "$path"; [[ -n "$mode" ]] && chmod "$mode" "$path"; done
else
  src="${args[0]}"; dst="${args[1]}"; mkdir -p "$(dirname "$dst")"; cp -p "$src" "$dst"
  [[ -n "$mode" ]] && chmod "$mode" "$dst"
fi
''')
        self._executable(fake / "systemctl", r'''#!/bin/bash
echo "$*" >> "$TEST_STATE_DIR/trace"
case "$1" in
  is-active) [[ "$(cat "$TEST_STATE_DIR/active")" = 1 ]] ;;
  is-enabled) [[ "$(cat "$TEST_STATE_DIR/enabled")" = 1 ]] ;;
  daemon-reload) exit 0 ;;
  start)
    [[ "$2" == *.path ]] && echo 1 > "$TEST_STATE_DIR/active"
    exit 0
    ;;
  enable)
    [[ "${TEST_FAIL_ENABLE:-0}" = 1 ]] && exit 9
    echo 1 > "$TEST_STATE_DIR/enabled"
    [[ " $* " == *" --now "* ]] && echo 1 > "$TEST_STATE_DIR/active"
    exit 0
    ;;
  disable)
    echo 0 > "$TEST_STATE_DIR/enabled"
    [[ " $* " == *" --now "* ]] && echo 0 > "$TEST_STATE_DIR/active"
    exit 0
    ;;
  stop) echo 0 > "$TEST_STATE_DIR/active"; exit 0 ;;
  *) exit 2 ;;
esac
''')
        owner = pwd.getpwuid(os.getuid()).pw_name
        group = grp.getgrgid(os.getgid()).gr_name
        env = os.environ.copy()
        env.update({
            "PATH": str(fake) + os.pathsep + env["PATH"],
            "SOURCE_ROOT": str(ROOT),
            "MATERIAL_LIBRARY_ROOT": str(library),
            "MATERIAL_LIBRARY_OWNER": owner,
            "MATERIAL_LIBRARY_GROUP": group,
            "MATERIAL_LIBRARY_PERMISSION_HELPER_TARGET": str(targets / "helper"),
            "MATERIAL_LIBRARY_PERMISSION_SERVICE_TARGET": str(targets / "service"),
            "MATERIAL_LIBRARY_PERMISSION_PATH_TARGET": str(targets / "path"),
            "MATERIAL_LIBRARY_INSTALL_TEST_MODE": "1",
            "MATERIAL_LIBRARY_BACKUP_ROOT": str(root),
            "TEST_STATE_DIR": str(state),
        })
        return library, expected, targets, state, env

    def test_installer_repairs_metadata_and_enables_path_unit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            library, expected, targets, state, env = self._fixture(root)
            result = subprocess.run(
                ["bash", str(ROOT / "deploy/material-library/install-index-permission-guard.sh")],
                env=env, capture_output=True, text=True,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            for name in permissions.METADATA_NAMES:
                path = library / name
                self.assertEqual(expected[name], path.read_bytes())
                self.assertEqual(0o644, stat.S_IMODE(path.stat().st_mode))
            self.assertTrue((targets / "helper").is_file())
            self.assertTrue((targets / "service").is_file())
            self.assertTrue((targets / "path").is_file())
            trace = (state / "trace").read_text()
            self.assertIn("start huangque-material-library-index-permissions.service", trace)
            self.assertIn("enable --now huangque-material-library-index-permissions.path", trace)

    def test_failed_enable_restores_existing_targets(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            _library, _expected, targets, state, env = self._fixture(root)
            originals = {"helper": b"old-helper", "service": b"old-service", "path": b"old-path"}
            for name, content in originals.items():
                (targets / name).write_bytes(content)
            env["TEST_FAIL_ENABLE"] = "1"
            result = subprocess.run(
                ["bash", str(ROOT / "deploy/material-library/install-index-permission-guard.sh")],
                env=env, capture_output=True, text=True,
            )
            self.assertNotEqual(0, result.returncode)
            for name, content in originals.items():
                self.assertEqual(content, (targets / name).read_bytes())
            self.assertEqual("0", (state / "active").read_text().strip())
            self.assertEqual("0", (state / "enabled").read_text().strip())


if __name__ == "__main__":
    unittest.main()
