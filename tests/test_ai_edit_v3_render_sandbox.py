from __future__ import annotations

import errno
import hashlib
import importlib.machinery
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
RENDER_UNIT = ROOT / "deploy/systemd/huangque-ai-edit-v3-render@.service"
WORKER_UNIT = ROOT / "deploy/systemd/huangque-ai-edit-v3.service"
API_UNIT = ROOT / "deploy/systemd/huangque-ai-edit-v3-api.service"
API_ROLE_ENV = ROOT / "deploy/ai-edit-v3-api.env.example"
CONTENT_DROPIN = ROOT / "deploy/systemd/huangque-content.service.d/ai-edit-v3.conf"
V2_ASSET_DROPIN = ROOT / "deploy/systemd/huangque-ai-edit-v2.service.d/ai-edit-v3-assets.conf"
HELPER = ROOT / "deploy/libexec/huangque-ai-edit-v3-renderctl"
SUDOERS = ROOT / "deploy/sudoers.d/huangque-ai-edit-v3-render"
TMPFILES = ROOT / "deploy/tmpfiles.d/huangque-ai-edit-v3.conf"
FANG_LOCATIONS = ROOT / "deploy/nginx-fang-locations.conf"


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
            "CPUQuota=200%", "MemoryMax=3G", "TasksMax=512", "LimitFSIZE=8G",
            "TemporaryFileSystem=/work:rw,nodev,nosuid,size=8G",
            "BindReadOnlyPaths=/opt/huangque/ai-edit-v3-renderer/releases:/renderer-releases",
            "BindReadOnlyPaths=/var/lib/huangque-ai-edit-v3-render/%i/input:/work/input",
            "BindPaths=/var/lib/huangque-ai-edit-v3-render/%i/output:/work/output",
            "Environment=PUPPETEER_EXECUTABLE_PATH=/usr/bin/google-chrome-stable",
            "ExecStart=/usr/local/libexec/huangque-ai-edit-v3-renderctl run %i",
        )
        for directive in required:
            with self.subTest(directive=directive):
                self.assertIn(directive, unit)
        self.assertNotIn("EnvironmentFile", unit)
        self.assertNotIn("API_KEY", unit)
        self.assertNotIn("--no-sandbox", unit)
        self.assertNotIn("/current:", unit)

    def test_worker_sudoers_and_tmpfiles_are_narrow(self):
        worker = WORKER_UNIT.read_text(encoding="utf-8")
        self.assertTrue(API_UNIT.is_file())
        self.assertTrue(API_ROLE_ENV.is_file())
        api_unit = API_UNIT.read_text(encoding="utf-8")
        api_role = API_ROLE_ENV.read_text(encoding="utf-8")
        content = CONTENT_DROPIN.read_text(encoding="utf-8")
        v2_assets = V2_ASSET_DROPIN.read_text(encoding="utf-8")
        sudoers = SUDOERS.read_text(encoding="utf-8")
        tmpfiles = TMPFILES.read_text(encoding="utf-8")
        self.assertIn("User=huangque-ai-edit-v3", worker)
        self.assertIn("WantedBy=multi-user.target", worker)
        self.assertIn("NoNewPrivileges=no", worker)
        self.assertIn("SupplementaryGroups=ubuntu", worker)
        self.assertIn("EnvironmentFile=-/home/ubuntu/auth-service/auth.env", worker)
        self.assertIn("EnvironmentFile=/etc/huangque/ai-edit-v3.env", worker)
        self.assertNotIn("EnvironmentFile=/etc/huangque/ai-edit-v3.env", content)
        self.assertIn("User=huangque-ai-edit-v3", api_unit)
        self.assertIn("WantedBy=multi-user.target", api_unit)
        self.assertIn("EnvironmentFile=/etc/huangque/ai-edit-v3.env", api_unit)
        self.assertIn("EnvironmentFile=/etc/huangque/ai-edit-v3-api.env", api_unit)
        self.assertIn("AI_EDIT_V3_API_PORT=8113", api_role)
        self.assertIn(
            "AI_EDIT_V2_DB=/run/huangque-ai-edit-v3-api/ai_edit_v2.db",
            api_role,
        )
        self.assertIn("SupplementaryGroups=huangque-ai-edit-v3", content)
        shared_db = "/var/lib/huangque-ai-edit-v3/shared-assets.db"
        host_v2_db = "/home/ubuntu/content-api/ai_edit_v2.db"
        worker_v2_db = "/run/huangque-ai-edit-v3/ai_edit_v2.db"
        self.assertIn(f"Environment=CONTENT_ASSET_DB={shared_db}", worker)
        self.assertIn(f"Environment=AI_EDIT_V2_ASSET_DB={shared_db}", worker)
        self.assertIn(f"Environment=AI_EDIT_V2_ASSET_DB={shared_db}", api_unit)
        self.assertIn(f"Environment=CONTENT_ASSET_DB={shared_db}", content)
        self.assertIn(f"Environment=AI_EDIT_V2_ASSET_DB={shared_db}", v2_assets)
        self.assertNotIn("Environment=AI_EDIT_V2_DB=", worker)
        self.assertNotIn("Environment=AI_EDIT_V2_DB=", content)
        self.assertIn("RuntimeDirectory=huangque-ai-edit-v3", worker)
        self.assertIn("RuntimeDirectoryMode=0700", worker)
        self.assertIn(
            "ReadWritePaths=/var/lib/huangque-ai-edit-v3-private /var/lib/huangque-ai-edit-v3 /var/lib/huangque-ai-edit-v3-render /var/spool/huangque-ai-edit-v3",
            worker,
        )
        self.assertIn(
            f"BindReadOnlyPaths={host_v2_db}:{worker_v2_db}", worker
        )
        self.assertIn("RuntimeDirectory=huangque-ai-edit-v3-api", api_unit)
        self.assertIn("RuntimeDirectoryMode=0700", api_unit)
        self.assertIn(
            f"BindReadOnlyPaths={host_v2_db}:/run/huangque-ai-edit-v3-api/ai_edit_v2.db",
            api_unit,
        )
        self.assertIn(
            "ExecStart=/home/ubuntu/content-api/venv/bin/python /home/ubuntu/content-api/ai_edit_v3_api.py",
            api_unit,
        )
        self.assertNotIn("Environment=AI_EDIT_V2_DB=", v2_assets)
        self.assertIn("SupplementaryGroups=huangque-ai-edit-v3", v2_assets)
        self.assertIn("UMask=0007", worker)
        self.assertIn("UMask=0007", content)
        self.assertIn("UMask=0007", v2_assets)
        self.assertIn("/usr/local/libexec/huangque-ai-edit-v3-renderctl start *", sudoers)
        self.assertIn("query *", sudoers)
        self.assertIn("stop *", sudoers)
        self.assertNotIn("systemctl", sudoers)
        self.assertIn("/var/spool/huangque-ai-edit-v3/incoming", tmpfiles)
        self.assertIn("/var/spool/huangque-ai-edit-v3/results", tmpfiles)
        self.assertIn("d /var/lib/huangque-ai-edit-v3 2770", tmpfiles)
        self.assertIn(
            "d /var/lib/huangque-ai-edit-v3-private 0700 huangque-ai-edit-v3 huangque-ai-edit-v3",
            tmpfiles,
        )

    def test_nginx_routes_v3_to_the_content_api(self):
        locations = FANG_LOCATIONS.read_text(encoding="utf-8")
        block = locations.split("location ^~ /api/v3/edit/", 1)[1].split("}", 1)[0]
        self.assertIn("proxy_pass http://127.0.0.1:8113", block)
        self.assertIn("proxy_buffering off", block)


class RenderCtlTests(unittest.TestCase):
    def test_run_command_resolves_exact_content_addressed_release(self):
        helper = load_helper()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            releases = root / "releases"
            releases.mkdir()
            source = ROOT / "server/ai_edit_v3_renderer"
            lock = json.loads((source / "renderer-release.lock.json").read_text(encoding="utf-8"))
            release = releases / lock["renderer_build_id"].removeprefix("sha256:")
            shutil.copytree(source, release, ignore=shutil.ignore_patterns("node_modules"))
            request = root / "request.json"
            request.write_text(
                json.dumps({"renderer_build_id": lock["renderer_build_id"]}),
                encoding="utf-8",
            )

            command = helper.resolve_render_command(
                request_path=request,
                releases_root=releases,
                node_path=Path("/usr/bin/node"),
                input_root=Path("/work/input/assets"),
                output_root=Path("/work/output"),
            )

            self.assertEqual(command[0], "/usr/bin/node")
            self.assertEqual(Path(command[1]), release / "src/render.mjs")
            self.assertNotIn("current", command[1])
            (release / "src/unlisted-runtime.mjs").write_text("export {};\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "render_release_tree_incomplete"):
                helper.resolve_render_command(
                    request_path=request,
                    releases_root=releases,
                    node_path=Path("/usr/bin/node"),
                    input_root=Path("/work/input/assets"),
                    output_root=Path("/work/output"),
                )

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
            with patch.object(helper.os, "chown", create=True):
                result = controller.query("job_1")
            self.assertEqual(result["state"], "failed")
            controller.stop("job_1")
            self.assertEqual(calls[-1], ("/usr/bin/systemctl", "stop", "huangque-ai-edit-v3-render@job_1.service"))

    def test_query_parses_named_systemd_properties_independent_of_output_order(self):
        helper = load_helper()
        output = "Result=success\nExecMainStatus=0\nSubState=running\nActiveState=active\n"
        controller = helper.RenderController(
            systemctl=lambda _argv: (0, output, ""),
        )

        result = controller.query("job_1")

        self.assertEqual("running", result["state"])
        self.assertFalse(result["result_ready"])
        self.assertIsNone(result["error_code"])

    def test_start_handoff_does_not_rename_across_mount_boundaries(self):
        helper = load_helper()
        with tempfile.TemporaryDirectory() as folder:
            controller = helper.RenderController(
                spool_root=Path(folder) / "spool",
                state_root=Path(folder) / "state",
                systemctl=lambda _argv: (0, "", ""),
            )
            incoming = controller.incoming_root / "job_1"
            (incoming / "assets").mkdir(parents=True)
            (incoming / "request.json").write_text("{}", encoding="utf-8")
            (incoming / "assets/render-manifest.json").write_text("{}", encoding="utf-8")
            destination = controller.state_root / "job_1" / "input"
            original_replace = helper.os.replace

            def reject_cross_mount(source, target):
                if Path(source) == incoming and Path(target) == destination:
                    raise OSError(errno.EXDEV, "cross-device link")
                return original_replace(source, target)

            with patch.object(helper.os, "replace", side_effect=reject_cross_mount):
                result = controller.start("job_1")

            self.assertEqual("running", result["state"])
            self.assertFalse(incoming.exists())
            self.assertTrue((destination / "request.json").is_file())
            self.assertTrue((destination / "assets/render-manifest.json").is_file())

    def test_publish_result_copies_only_reported_delivery_artifacts(self):
        helper = load_helper()
        with tempfile.TemporaryDirectory() as folder:
            controller = helper.RenderController(
                spool_root=Path(folder) / "spool",
                state_root=Path(folder) / "state",
                systemctl=lambda _argv: (
                    0,
                    "Result=success\nExecMainStatus=0\nSubState=dead\nActiveState=inactive\n",
                    "",
                ),
            )
            output = controller.state_root / "job_1" / "output"
            snapshots = output / "snapshots"
            snapshots.mkdir(parents=True)
            video = output / "silent.mp4"
            video.write_bytes(b"video")
            frame = snapshots / "frame.png"
            frame.write_bytes(b"frame")
            report = {
                "status": "done",
                "output": {
                    "path": "silent.mp4",
                    "size_bytes": video.stat().st_size,
                    "sha256": hashlib.sha256(video.read_bytes()).hexdigest(),
                    "silent": True,
                },
                "snapshots": [
                    {
                        "path": "frame.png",
                        "size_bytes": frame.stat().st_size,
                        "sha256": hashlib.sha256(frame.read_bytes()).hexdigest(),
                    }
                ],
            }
            (output / "silent.report.json").write_text(json.dumps(report), encoding="utf-8")
            browser_cache = output / "home" / ".config" / "chrome"
            browser_cache.mkdir(parents=True)
            (browser_cache / "cache.bin").write_bytes(b"must-not-publish")

            with patch.object(helper.os, "chown", create=True):
                result = controller.query("job_1")

            published = controller.results_root / "job_1"
            self.assertEqual("succeeded", result["state"])
            self.assertTrue((published / "silent.mp4").is_file())
            self.assertTrue((published / "silent.report.json").is_file())
            self.assertTrue((published / "snapshots/frame.png").is_file())
            self.assertFalse((published / "home").exists())


if __name__ == "__main__":
    unittest.main()
