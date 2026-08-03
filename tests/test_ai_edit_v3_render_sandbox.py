from __future__ import annotations

import importlib.machinery
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
RENDER_UNIT = ROOT / "deploy/systemd/huangque-ai-edit-v3-render@.service"
WORKER_UNIT = ROOT / "deploy/systemd/huangque-ai-edit-v3.service"
HELPER = ROOT / "deploy/libexec/huangque-ai-edit-v3-renderctl"
SUDOERS = ROOT / "deploy/sudoers.d/huangque-ai-edit-v3-render"
TMPFILES = ROOT / "deploy/tmpfiles.d/huangque-ai-edit-v3.conf"


def load_helper():
    return importlib.machinery.SourceFileLoader("v3_renderctl", str(HELPER)).load_module()


class RenderSandboxStaticTests(unittest.TestCase):
    def test_render_unit_has_complete_frozen_sandbox(self):
        unit = RENDER_UNIT.read_text(encoding="utf-8")
        required = (
            "DynamicUser=yes", "PrivateNetwork=yes", "PrivateUsers=yes", "PrivateMounts=yes",
            "PrivateTmp=yes", "NoNewPrivileges=yes", "ProtectSystem=strict", "ProtectHome=yes",
            "ProtectKernelTunables=yes", "ProtectKernelModules=yes", "ProtectControlGroups=yes",
            "RestrictSUIDSGID=yes", "CapabilityBoundingSet=", "AmbientCapabilities=",
            "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6", "UMask=0077",
            "KillMode=control-group", "TimeoutStopSec=30", "RuntimeMaxSec=3300",
            "CPUQuota=200%", "MemoryMax=3G", "TasksMax=64", "LimitFSIZE=8G",
            "TemporaryFileSystem=/work:rw,nodev,nosuid,size=8G",
            "BindReadOnlyPaths=/opt/huangque/ai-edit-v3-renderer/current:/work/release",
            "BindReadOnlyPaths=/var/lib/huangque-ai-edit-v3-render/%i/input:/work/input",
            "BindPaths=/var/lib/huangque-ai-edit-v3-render/%i/output:/work/output",
            "ExecStart=/usr/bin/node /work/release/src/render.mjs --request /work/input/request.json --input-root /work/input/assets --output-root /work/output",
        )
        for directive in required:
            with self.subTest(directive=directive):
                self.assertIn(directive, unit)
        self.assertNotIn("EnvironmentFile", unit)
        self.assertNotIn("API_KEY", unit)
        self.assertNotIn("--no-sandbox", unit)

    def test_worker_sudoers_and_tmpfiles_are_narrow(self):
        worker = WORKER_UNIT.read_text(encoding="utf-8")
        sudoers = SUDOERS.read_text(encoding="utf-8")
        tmpfiles = TMPFILES.read_text(encoding="utf-8")
        self.assertIn("User=huangque-ai-edit-v3", worker)
        self.assertIn("EnvironmentFile=/etc/huangque/ai-edit-v3.env", worker)
        self.assertIn("/usr/local/libexec/huangque-ai-edit-v3-renderctl start *", sudoers)
        self.assertIn("query *", sudoers)
        self.assertIn("stop *", sudoers)
        self.assertNotIn("systemctl", sudoers)
        self.assertIn("/var/spool/huangque-ai-edit-v3/incoming", tmpfiles)
        self.assertIn("/var/spool/huangque-ai-edit-v3/results", tmpfiles)


class RenderCtlTests(unittest.TestCase):
    def test_instance_id_is_exact_and_rejects_injection(self):
        helper = load_helper()
        for value in ("job_1", "a", "a-b_c", "9" * 64):
            self.assertEqual(helper.validate_instance_id(value), value)
        for value in ("", "A", "../x", "a/b", "a b", "a\nb", "*", "-x", ".", "a" * 65):
            with self.subTest(value=value), self.assertRaises(ValueError):
                helper.validate_instance_id(value)

    def test_controller_uses_fixed_unit_and_bounded_json(self):
        helper = load_helper()
        with tempfile.TemporaryDirectory() as folder:
            calls = []
            controller = helper.RenderController(
                spool_root=Path(folder) / "spool",
                state_root=Path(folder) / "state",
                systemctl=lambda argv: calls.append(tuple(argv)) or (0, "inactive\ndead\nsuccess\n0\n", ""),
            )
            incoming = controller.incoming_root / "job_1"
            (incoming / "assets").mkdir(parents=True)
            (incoming / "request.json").write_text("{}", encoding="utf-8")
            (incoming / "assets/render-manifest.json").write_text("{}", encoding="utf-8")
            controller.start("job_1")
            self.assertEqual(calls[0], ("/usr/bin/systemctl", "start", "huangque-ai-edit-v3-render@job_1.service"))
            result = controller.query("job_1")
            self.assertEqual(result["state"], "failed")
            controller.stop("job_1")
            self.assertEqual(calls[-1], ("/usr/bin/systemctl", "stop", "huangque-ai-edit-v3-render@job_1.service"))


if __name__ == "__main__":
    unittest.main()
