from __future__ import annotations

import importlib.util
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_renderer():
    path = ROOT / "scripts" / "render_pixelle_config.py"
    spec = importlib.util.spec_from_file_location("render_pixelle_config", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PixelleDeploymentTests(unittest.TestCase):
    def test_service_is_loopback_only_and_resource_limited(self):
        unit = (ROOT / "deploy/systemd/huangque-pixelle-video.service").read_text(encoding="utf-8")
        self.assertIn("--host 127.0.0.1 --port 8103", unit)
        self.assertIn("MemoryMax=1800M", unit)
        self.assertIn("CPUQuota=150%", unit)
        self.assertIn("After=network-online.target huangque-egress-tunnel.service", unit)
        self.assertIn("Environment=HTTPS_PROXY=http://127.0.0.1:7999", unit)
        self.assertIn("Environment=NO_PROXY=127.0.0.1,localhost", unit)
        self.assertNotIn("Environment=OPENAI_API_KEY", unit)
        self.assertNotIn("Environment=RUNNINGHUB_API_KEY", unit)

    def test_installer_pins_upstream_and_refuses_missing_config(self):
        installer = (ROOT / "deploy/pixelle-video/install.sh").read_text(encoding="utf-8")
        self.assertIn("848b054e4fae40dabc62ec58e960b573e83793ac", installer)
        self.assertIn("https://mirrors.aliyun.com/pypi/simple", installer)
        self.assertIn('UV_DEFAULT_INDEX="${PYPI_INDEX}"', installer)
        self.assertIn("https://files.pythonhosted.org/packages/", installer)
        self.assertIn("https://mirrors.aliyun.com/pypi/packages/", installer)
        self.assertIn("unexpected uv.lock", installer)
        self.assertIn('if [[ ! -s "${CONFIG_PATH}" ]]', installer)
        self.assertNotIn("sed -i 's/max_concurrent_tasks", installer)
        self.assertIn('TASK_CAPACITY_OVERRIDE=', installer)
        self.assertIn('TASK_CAPACITY_PATCH=', installer)
        self.assertIn('RELEASES_DIR=', installer)
        self.assertIn('RELEASE_DIR="$(mktemp -d', installer)
        self.assertIn('install -o admin -g admin -m 0644 "${TASK_CAPACITY_OVERRIDE}"', installer)
        self.assertIn(
            'git -C "${RELEASE_DIR}" apply --unidiff-zero --check "${TASK_CAPACITY_PATCH}"',
            installer,
        )
        self.assertIn(
            'git -C "${RELEASE_DIR}" apply --unidiff-zero "${TASK_CAPACITY_PATCH}"',
            installer,
        )
        self.assertNotIn('git -C "${SOURCE_DIR}" reset --hard', installer)
        self.assertNotIn('git -C "${SOURCE_DIR}" clean -fdx', installer)
        self.assertNotIn('git -C "${SOURCE_DIR}" apply', installer)
        self.assertIn('mv -Tf "${NEXT_SOURCE_LINK}" "${SOURCE_DIR}"', installer)
        self.assertIn('STATE_ROOT="/var/lib/huangque-pixelle-video"', installer)
        self.assertIn('cp -a "${SOURCE_DIR}/output/." "${OUTPUT_DIR}/"', installer)
        self.assertIn('ln -s "${OUTPUT_DIR}" "${RELEASE_DIR}/output"', installer)
        self.assertIn('ln -s "${DATA_DIR}" "${RELEASE_DIR}/data"', installer)
        self.assertIn(
            '"${RELEASE_DIR}/.venv/bin/python" -m compileall -q "${RELEASE_DIR}/api"',
            installer,
        )

    def test_capacity_patch_covers_sync_and_async_video_execution(self):
        patch = (
            ROOT
            / "deploy"
            / "pixelle-video"
            / "patches"
            / "0001-enforce-video-task-capacity.patch"
        ).read_text(encoding="utf-8")
        self.assertIn("video_task_capacity.slot()", patch)
        self.assertIn("generate_video_sync", patch)
        self.assertIn("execute_video_generation", patch)
        self.assertIn("TaskQueueFullError", patch)
        self.assertIn("status_code=429", patch)

    def test_installer_validates_release_before_live_source_switch(self):
        installer = (ROOT / "deploy/pixelle-video/install.sh").read_text(encoding="utf-8")
        patch_check = installer.index(
            'git -C "${RELEASE_DIR}" apply --unidiff-zero --check "${TASK_CAPACITY_PATCH}"'
        )
        dependency_sync = installer.index('"${RUNTIME_ROOT}/bin/uv" --directory "${RELEASE_DIR}" sync')
        compile_check = installer.index(
            '"${RELEASE_DIR}/.venv/bin/python" -m compileall -q "${RELEASE_DIR}/api"'
        )
        stop_service = installer.rindex('systemctl stop "${SERVICE_NAME}" || true')
        switch_source = installer.index('mv -Tf "${NEXT_SOURCE_LINK}" "${SOURCE_DIR}"')

        self.assertLess(patch_check, stop_service)
        self.assertLess(dependency_sync, stop_service)
        self.assertLess(compile_check, stop_service)
        self.assertLess(stop_service, switch_source)
        self.assertIn('trap cleanup EXIT', installer)
        self.assertIn('mv "${LEGACY_SOURCE_BACKUP}" "${SOURCE_DIR}"', installer)

    def test_config_renderer_requires_keys_and_does_not_eval_env(self):
        renderer = load_renderer()
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / "provider.env"
            env_path.write_text("OPENAI_API_KEY=$(touch should-not-run)\nOPENAI_BASE=https://example.test/v1\n", encoding="utf-8")
            values = renderer.parse_env(env_path)
            self.assertEqual(values["OPENAI_API_KEY"], "$(touch should-not-run)")
            rendered = renderer.render(values, {"RUNNINGHUB_API_KEY": "rh-secret"})
            self.assertIn('api_key: "$(touch should-not-run)"', rendered)
            self.assertIn('model: "gpt-4o-mini"', rendered)
        with self.assertRaisesRegex(ValueError, "OPENAI_API_KEY"):
            renderer.render({}, {"RUNNINGHUB_API_KEY": "rh-secret"})

    def test_template_overrides_have_no_pixelle_branding(self):
        templates = sorted((ROOT / "deploy/pixelle-video/templates/1080x1920").glob("*.html"))
        self.assertEqual(20, len(templates))
        markers = ("@Pixelle", "Pixelle-Video", "Pixelle.AI")
        for template in templates:
            text = template.read_text(encoding="utf-8")
            self.assertFalse(any(marker in text for marker in markers), template.name)

    def test_rendered_config_is_owner_only(self):
        renderer = load_renderer()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            llm = root / "llm.env"
            runninghub = root / "runninghub.env"
            output = root / "config.yaml"
            llm.write_text("OPENAI_API_KEY=openai-secret\n", encoding="utf-8")
            runninghub.write_text("RUNNINGHUB_API_KEY=rh-secret\n", encoding="utf-8")
            old_argv = sys.argv
            try:
                sys.argv = [
                    "render_pixelle_config.py",
                    "--llm-env", str(llm),
                    "--runninghub-env", str(runninghub),
                    "--output", str(output),
                ]
                self.assertEqual(0, renderer.main())
            finally:
                sys.argv = old_argv
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)

    def test_nginx_bridge_is_backend_only_and_strips_private_prefix(self):
        nginx = (ROOT / "deploy/nginx-fang-locations.conf").read_text(encoding="utf-8")
        self.assertIn("location ^~ /internal/pixelle/", nginx)
        self.assertIn("allow 129.204.166.13;", nginx)
        self.assertIn("deny all;", nginx)
        self.assertIn("proxy_pass http://127.0.0.1:8103/;", nginx)
        self.assertIn("proxy_read_timeout 1800s;", nginx)
        self.assertIn("proxy_request_buffering off;", nginx)


if __name__ == "__main__":
    unittest.main()
