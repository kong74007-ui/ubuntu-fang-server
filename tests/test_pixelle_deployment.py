from __future__ import annotations

import importlib.util
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def find_bash() -> str:
    bash = shutil.which("bash")
    if bash:
        return bash
    for candidate in (Path("D:/Git/bin/bash.exe"), Path("C:/Program Files/Git/bin/bash.exe")):
        if candidate.exists():
            return str(candidate)
    raise unittest.SkipTest("bash is required for Pixelle installer tests")


def run_stop_helper(fake_systemctl: str, trace: Path) -> subprocess.CompletedProcess[str]:
    helper = ROOT / "deploy" / "pixelle-video" / "lib" / "service_control.sh"
    command = f'''source "{helper.as_posix()}"
systemctl() {{
{fake_systemctl}
}}
mark_source_switch() {{
  printf "switch\\n" >> "$TRACE"
}}
pixelle_run_with_service_stopped huangque-pixelle-video.service mark_source_switch
'''
    env = os.environ.copy()
    env["TRACE"] = trace.as_posix()
    return subprocess.run(
        [find_bash(), "-c", command],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def load_renderer():
    path = ROOT / "scripts" / "render_pixelle_config.py"
    spec = importlib.util.spec_from_file_location("render_pixelle_config", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PixelleDeploymentTests(unittest.TestCase):
    def test_single_line_caption_patch_is_fail_closed_and_applied_last(self):
        installer = (ROOT / "deploy/pixelle-video/install.sh").read_text(encoding="utf-8")
        patch_path = (
            ROOT
            / "deploy"
            / "pixelle-video"
            / "patches"
            / "0009-support-single-line-caption-cues.patch"
        )
        self.assertTrue(patch_path.is_file())
        patch = patch_path.read_text(encoding="utf-8")
        self.assertIn('CAPTION_CUES_PATCH=', installer)
        self.assertIn('CAPTION_CUES_OVERRIDE=', installer)
        self.assertIn(
            'git -C "${RELEASE_DIR}" apply --unidiff-zero --check "${CAPTION_CUES_PATCH}"',
            installer,
        )
        self.assertGreater(
            installer.index('"${CAPTION_CUES_PATCH}"'),
            installer.index('"${TTS_SPEED_PATCH}"'),
        )
        self.assertIn("class CaptionCue", patch)
        self.assertIn("caption_cues", patch)
        self.assertIn("pixelle_video/services/frame_html.py", patch)
        self.assertIn("validate_caption_cue_text(cue.text)", patch)
        self.assertIn("ensure_video_duration(", patch)
        self.assertLess(
            patch.index("ensure_video_duration("),
            patch.index("extract_video_clip("),
        )
        for line in patch.splitlines():
            self.assertEqual(line.rstrip(), line)
        self.assertFalse(patch.endswith("\n\n"))

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
        stop_service = installer.rindex(
            'pixelle_run_with_service_stopped "${SERVICE_NAME}" activate_release'
        )

        self.assertLess(patch_check, stop_service)
        self.assertLess(dependency_sync, stop_service)
        self.assertLess(compile_check, stop_service)
        self.assertIn('trap cleanup EXIT', installer)
        self.assertIn('mv "${LEGACY_SOURCE_BACKUP}" "${SOURCE_DIR}"', installer)

    def test_installer_confirms_service_is_inactive_before_switch(self):
        installer = (ROOT / "deploy/pixelle-video/install.sh").read_text(encoding="utf-8")
        stop_call = 'pixelle_run_with_service_stopped "${SERVICE_NAME}" activate_release'
        if stop_call not in installer:
            self.fail("installer must require confirmed service stop before switching source")

        self.assertIn('source "${SERVICE_CONTROL_LIB}"', installer)
        self.assertIn('activate_release() {', installer)
        self.assertIn(
            'pixelle_run_with_service_stopped "${SERVICE_NAME}" rollback_release',
            installer,
        )
        self.assertNotIn('systemctl stop "${SERVICE_NAME}" || true', installer)

    def test_stop_failure_prevents_service_stop_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory) / "systemctl.log"
            trace.touch()
            result = run_stop_helper(
                'printf "%s\\n" "$*" >> "$TRACE"\n'
                'if [[ "$1" == "stop" ]]; then return 1; fi',
                trace,
            )

            self.assertNotEqual(0, result.returncode)
            self.assertEqual(["stop huangque-pixelle-video.service"], trace.read_text().splitlines())

    def test_active_service_prevents_service_stop_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory) / "systemctl.log"
            trace.touch()
            result = run_stop_helper(
                'printf "%s\\n" "$*" >> "$TRACE"\n'
                'if [[ "$1" == "show" ]]; then printf "active\\n"; fi\n'
                'return 0',
                trace,
            )

            self.assertNotEqual(0, result.returncode)
            self.assertEqual(
                [
                    "stop huangque-pixelle-video.service",
                    "show --property=ActiveState --value huangque-pixelle-video.service",
                ],
                trace.read_text().splitlines(),
            )

    def test_inactive_service_allows_source_switch_callback(self):
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory) / "systemctl.log"
            trace.touch()
            result = run_stop_helper(
                'printf "%s\\n" "$*" >> "$TRACE"\n'
                'if [[ "$1" == "show" ]]; then printf "inactive\\n"; fi\n'
                'return 0',
                trace,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(
                [
                    "stop huangque-pixelle-video.service",
                    "show --property=ActiveState --value huangque-pixelle-video.service",
                    "switch",
                ],
                trace.read_text().splitlines(),
            )

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
            self.assertIn("runninghub_concurrent_limit: 5", rendered)
        with self.assertRaisesRegex(ValueError, "OPENAI_API_KEY"):
            renderer.render({}, {"RUNNINGHUB_API_KEY": "rh-secret"})

    def test_rendered_media_prompts_default_people_to_chinese_or_east_asian(self):
        renderer = load_renderer()
        rendered = renderer.render(
            {"OPENAI_API_KEY": "openai-secret"},
            {"RUNNINGHUB_API_KEY": "rh-secret"},
        )
        people_context = (
            "When people appear, depict contemporary Chinese or East Asian people "
            "unless the user text explicitly specifies another ethnicity, nationality, or region."
        )
        self.assertEqual(2, rendered.count(people_context))

    def test_pixelle_config_allows_five_runninghub_scenes_per_video(self):
        example = (ROOT / "deploy/pixelle-video/config.yaml.example").read_text(
            encoding="utf-8"
        )
        readme = (ROOT / "deploy/pixelle-video/README.md").read_text(encoding="utf-8")

        self.assertIn("runninghub_concurrent_limit: 5", example)
        self.assertIn("up to five RunningHub scenes", readme)

    def test_template_overrides_have_no_pixelle_branding(self):
        templates = sorted((ROOT / "deploy/pixelle-video/templates/1080x1920").glob("*.html"))
        self.assertEqual(20, len(templates))
        markers = ("@Pixelle", "Pixelle-Video", "Pixelle.AI")
        for template in templates:
            text = template.read_text(encoding="utf-8")
            self.assertFalse(any(marker in text for marker in markers), template.name)

    def test_video_templates_remove_upstream_branding_during_install(self):
        installer = (ROOT / "deploy/pixelle-video/install.sh").read_text(encoding="utf-8")
        patch_path = (
            ROOT
            / "deploy"
            / "pixelle-video"
            / "patches"
            / "0002-remove-video-template-branding.patch"
        )
        patch = patch_path.read_text(encoding="utf-8")

        self.assertIn("VIDEO_TEMPLATE_BRANDING_PATCH=", installer)
        self.assertIn(
            'git -C "${RELEASE_DIR}" apply --unidiff-zero --check "${VIDEO_TEMPLATE_BRANDING_PATCH}"',
            installer,
        )
        self.assertIn(
            'git -C "${RELEASE_DIR}" apply --unidiff-zero "${VIDEO_TEMPLATE_BRANDING_PATCH}"',
            installer,
        )
        self.assertIn("templates/1080x1920/video_default.html", patch)
        self.assertIn("templates/1080x1920/video_healing.html", patch)
        for marker in ("@Pixelle.AI", "Pixelle-Video", "Open Source Omnimodal AI Creative Agent"):
            self.assertTrue(
                any(line.startswith("-") and marker in line for line in patch.splitlines()),
                marker,
            )

    def test_external_narration_patch_and_overrides_are_fail_closed(self):
        installer = (ROOT / "deploy/pixelle-video/install.sh").read_text(encoding="utf-8")
        unit = (ROOT / "deploy/systemd/huangque-pixelle-video.service").read_text(encoding="utf-8")
        patch_path = ROOT / "deploy/pixelle-video/patches/0003-support-external-narration-audio.patch"
        patch = patch_path.read_text(encoding="utf-8")

        for marker in (
            "EXTERNAL_AUDIO_OVERRIDE=",
            "VOICE_ASSETS_ROUTER_OVERRIDE=",
            "EXTERNAL_NARRATION_PATCH=",
        ):
            self.assertIn(marker, installer)
        self.assertIn(
            'git -C "${RELEASE_DIR}" apply --unidiff-zero --check "${EXTERNAL_NARRATION_PATCH}"',
            installer,
        )
        self.assertIn(
            'git -C "${RELEASE_DIR}" apply --unidiff-zero "${EXTERNAL_NARRATION_PATCH}"',
            installer,
        )
        self.assertIn('"${RELEASE_DIR}/api/external_audio.py"', installer)
        self.assertIn('"${RELEASE_DIR}/api/routers/voice_assets.py"', installer)
        self.assertIn(
            "Environment=PIXELLE_EXTERNAL_AUDIO_ROOT=/var/lib/huangque-pixelle-video/data/external_audio",
            unit,
        )
        self.assertIn("narration_segments", patch)
        self.assertIn("external_narration_segments", patch)
        self.assertIn("release_audio_assets", patch)
        self.assertIn("start_cleanup_scheduler", patch)
        self.assertIn("stop_cleanup_scheduler", patch)
        self.assertIn('status_code=404, detail="narration audio asset not found"', patch)
        self.assertIn('status_code=409, detail="narration audio asset is already in use"', patch)
        added = "\n".join(
            line[1:] for line in patch.splitlines()
            if line.startswith("+") and not line.startswith("+++")
        )
        self.assertLess(
            added.index("prepare_async_audio_submission"),
            added.index("task_manager.create_task"),
        )
        patch_check = installer.index(
            'git -C "${RELEASE_DIR}" apply --unidiff-zero --check "${EXTERNAL_NARRATION_PATCH}"'
        )
        source_switch = installer.rindex(
            'pixelle_run_with_service_stopped "${SERVICE_NAME}" activate_release'
        )
        self.assertLess(patch_check, source_switch)

    def test_deepseek_v4_patch_disables_thinking_for_json_compatible_output(self):
        installer = (ROOT / "deploy/pixelle-video/install.sh").read_text(encoding="utf-8")
        patch_path = (
            ROOT
            / "deploy/pixelle-video/patches/0004-disable-deepseek-v4-thinking.patch"
        )
        patch = patch_path.read_text(encoding="utf-8")

        self.assertIn("DEEPSEEK_V4_PATCH=", installer)
        self.assertIn(
            'git -C "${RELEASE_DIR}" apply --check "${DEEPSEEK_V4_PATCH}"',
            installer,
        )
        self.assertIn(
            'git -C "${RELEASE_DIR}" apply "${DEEPSEEK_V4_PATCH}"',
            installer,
        )
        self.assertIn("pixelle_video/services/llm_service.py", patch)
        self.assertIn('hostname == "api.deepseek.com"', patch)
        self.assertIn('model.startswith("deepseek-v4-")', patch)
        self.assertIn(
            'extra_body.setdefault("thinking", {"type": "disabled"})',
            patch,
        )
        self.assertGreaterEqual(
            patch.count("request_kwargs = self._prepare_request_kwargs"), 2
        )

        deepseek_patch_check = installer.index(
            'git -C "${RELEASE_DIR}" apply --check "${DEEPSEEK_V4_PATCH}"'
        )
        source_switch = installer.rindex(
            'pixelle_run_with_service_stopped "${SERVICE_NAME}" activate_release'
        )
        self.assertLess(deepseek_patch_check, source_switch)

    def test_image_generation_retry_is_installed_and_scoped_to_images(self):
        installer = (ROOT / "deploy/pixelle-video/install.sh").read_text(encoding="utf-8")
        patch_path = ROOT / "deploy/pixelle-video/patches/0005-retry-image-generation.patch"
        patch = patch_path.read_text(encoding="utf-8")

        self.assertIn("MEDIA_RETRY_OVERRIDE=", installer)
        self.assertIn("IMAGE_RETRY_PATCH=", installer)
        self.assertIn(
            'git -C "${RELEASE_DIR}" apply --unidiff-zero --check "${IMAGE_RETRY_PATCH}"',
            installer,
        )
        self.assertIn(
            'install -o admin -g admin -m 0644 "${MEDIA_RETRY_OVERRIDE}"',
            installer,
        )
        self.assertIn("pixelle_video/services/frame_processor.py", patch)
        self.assertIn("retry_async", patch)
        self.assertIn("max_attempts=4", patch)
        self.assertIn("attempt_timeout=180 if media_type == \"image\" else 600", patch)
        self.assertIn("RetryBudget(max_retries=10)", patch)

        retry_check = installer.index(
            'git -C "${RELEASE_DIR}" apply --unidiff-zero --check "${IMAGE_RETRY_PATCH}"'
        )
        source_switch = installer.rindex(
            'pixelle_run_with_service_stopped "${SERVICE_NAME}" activate_release'
        )
        self.assertLess(retry_check, source_switch)

    def test_runninghub_poll_guard_is_installed_before_service_activation(self):
        installer = (ROOT / "deploy/pixelle-video/install.sh").read_text(encoding="utf-8")
        patch_path = ROOT / "deploy/pixelle-video/patches/0006-guard-runninghub-polling.patch"
        guard_path = (
            ROOT
            / "deploy/pixelle-video/overrides/pixelle_video/services/runninghub_guard.py"
        )
        disconnect_path = ROOT / "deploy/pixelle-video/overrides/api/disconnect.py"

        self.assertTrue(patch_path.is_file())
        self.assertTrue(guard_path.is_file())
        self.assertTrue(disconnect_path.is_file())
        self.assertIn("RUNNINGHUB_GUARD_PATCH=", installer)
        self.assertIn("RUNNINGHUB_GUARD_OVERRIDE=", installer)
        self.assertIn("PIXELLE_DISCONNECT_OVERRIDE=", installer)
        self.assertIn(
            'git -C "${RELEASE_DIR}" apply --check "${RUNNINGHUB_GUARD_PATCH}"',
            installer,
        )
        self.assertIn(
            'install -o admin -g admin -m 0644 "${RUNNINGHUB_GUARD_OVERRIDE}"',
            installer,
        )
        self.assertIn('"${RELEASE_DIR}/api/disconnect.py"', installer)
        self.assertIn("install_runninghub_guard", patch_path.read_text(encoding="utf-8"))
        self.assertIn("await_with_disconnect", patch_path.read_text(encoding="utf-8"))

        disconnect_install = installer.index('"${RELEASE_DIR}/api/disconnect.py"')
        guard_install = installer.index(
            'install -o admin -g admin -m 0644 "${RUNNINGHUB_GUARD_OVERRIDE}"'
        )
        source_switch = installer.rindex(
            'pixelle_run_with_service_stopped "${SERVICE_NAME}" activate_release'
        )
        self.assertLess(disconnect_install, source_switch)
        self.assertLess(guard_install, source_switch)

    def test_parallel_frame_fail_fast_is_installed_before_service_activation(self):
        installer = (ROOT / "deploy/pixelle-video/install.sh").read_text(encoding="utf-8")
        patch_path = ROOT / "deploy/pixelle-video/patches/0007-fail-fast-parallel-frames.patch"
        helper_path = (
            ROOT
            / "deploy/pixelle-video/overrides/pixelle_video/services/fail_fast.py"
        )

        self.assertTrue(patch_path.is_file())
        self.assertTrue(helper_path.is_file())
        self.assertIn("PARALLEL_FAIL_FAST_PATCH=", installer)
        self.assertIn("FAIL_FAST_OVERRIDE=", installer)
        self.assertIn("gather_cancel_on_error", patch_path.read_text(encoding="utf-8"))
        self.assertIn(
            'git -C "${RELEASE_DIR}" apply --unidiff-zero --check "${PARALLEL_FAIL_FAST_PATCH}"',
            installer,
        )
        self.assertIn(
            'install -o admin -g admin -m 0644 "${FAIL_FAST_OVERRIDE}"',
            installer,
        )

        patch_check = installer.index(
            'git -C "${RELEASE_DIR}" apply --unidiff-zero --check "${PARALLEL_FAIL_FAST_PATCH}"'
        )
        helper_install = installer.index(
            'install -o admin -g admin -m 0644 "${FAIL_FAST_OVERRIDE}"'
        )
        source_switch = installer.rindex(
            'pixelle_run_with_service_stopped "${SERVICE_NAME}" activate_release'
        )
        self.assertLess(patch_check, source_switch)
        self.assertLess(helper_install, source_switch)

    def test_tts_speed_patch_is_fail_closed_and_covers_sync_and_async(self):
        installer = (ROOT / "deploy/pixelle-video/install.sh").read_text(encoding="utf-8")
        patch_path = ROOT / "deploy/pixelle-video/patches/0008-support-tts-speed-api.patch"

        self.assertTrue(patch_path.is_file())
        self.assertIn("TTS_SPEED_PATCH=", installer)
        self.assertIn(
            'git -C "${RELEASE_DIR}" apply --check "${TTS_SPEED_PATCH}"',
            installer,
        )
        self.assertIn(
            'git -C "${RELEASE_DIR}" apply "${TTS_SPEED_PATCH}"',
            installer,
        )

        patch = patch_path.read_text(encoding="utf-8")
        self.assertIn("api/schemas/video.py", patch)
        self.assertIn("api/routers/video.py", patch)
        self.assertIn(
            "tts_speed: Optional[float] = Field(default=None, ge=0.5, le=2.0",
            patch,
        )
        self.assertEqual(2, patch.count("if request_body.tts_speed is not None:"))
        self.assertEqual(
            2,
            patch.count('video_params["tts_speed"] = request_body.tts_speed'),
        )
        self.assertNotIn('"tts_speed": request_body.tts_speed', patch)

        patch_check = installer.index(
            'git -C "${RELEASE_DIR}" apply --check "${TTS_SPEED_PATCH}"'
        )
        source_switch = installer.rindex(
            'pixelle_run_with_service_stopped "${SERVICE_NAME}" activate_release'
        )
        self.assertLess(patch_check, source_switch)

    def test_talking_material_override_is_fail_closed_and_installed(self):
        installer = (ROOT / "deploy/pixelle-video/install.sh").read_text(encoding="utf-8")

        self.assertIn("TALKING_MATERIAL_OVERRIDE=", installer)
        self.assertIn('! -s "${TALKING_MATERIAL_OVERRIDE}"', installer)
        self.assertIn(
            'install -o admin -g admin -m 0644 "${TALKING_MATERIAL_OVERRIDE}"',
            installer,
        )
        self.assertIn(
            '"${RELEASE_DIR}/pixelle_video/services/talking_material.py"',
            installer,
        )
        self.assertIn(
            '"${RELEASE_DIR}/pixelle_video"',
            installer,
        )

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
