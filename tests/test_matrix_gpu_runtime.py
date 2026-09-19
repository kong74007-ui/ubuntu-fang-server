import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from server.matrix_gpu_runtime import GpuRuntime, RUNTIME_FILES
from server.matrix_template_api import MatrixTemplateService
from server import matrix_template_api as api


def evidence():
    return {"ok": True, "contract_version": 1, "compositor": "webgpu-native",
            "encoder": "hevc_nvenc", "adapter": {"vendor": "nvidia", "isFallbackAdapter": False}}


class MatrixGpuRuntimeTests(unittest.TestCase):
    def test_gpu_clip_bookends_interpolate_matching_shapes(self):
        index = '<html><head></head><body>' + ''.join(
            f'<video id="{name}"></video>' for name in api.REFERENCE_VIDEO_IDS[:3]
        ) + api.REFERENCE_BASE_TIMELINE_JS + '</body></html>'
        plan = api._reference_editing_plan("clip-test", "ref-02-shenzhen-ai-orange", 3)
        plan["bookends"] = {"entrance": "circle_reveal", "exit": "diagonal_close"}
        gpu = api._inject_reference_editing_plan(index, plan, gpu_safe_clips=True)
        self.assertIn('polygon(0% 0%, 100% 0%, 100% 100%, 0% 100%)', gpu)
        self.assertIn('circle(100% at 50% 50%)', gpu)
        self.assertIn('timeline.fromTo(transitionLayers[lastIndex], fullClipNeutral', gpu)
        legacy = api._inject_reference_editing_plan(index, plan)
        self.assertNotIn('fullClipNeutral', legacy)

    def setup_runtime(self, root, result=None):
        for name in RUNTIME_FILES:
            (root / name).write_text(name)
        (root / "contract.json").write_text(json.dumps({"version": 1, "templates": ["ref-01", "nine-grid-reveal"]}))
        browser = root / "chrome"
        browser.touch()
        response = subprocess.CompletedProcess([], 0, json.dumps(result or evidence()), "")
        with mock.patch("server.matrix_gpu_runtime.subprocess.run", return_value=response):
            return GpuRuntime(root, browser)

    def test_verified_runtime_builds_gpu_command(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = self.setup_runtime(root)
            command = runtime.command(root, root / "final.mp4", root / "variables.json", 30)
            self.assertIn(str(root / "render.mjs"), command)
            self.assertEqual(["--timeout", "30"], command[-2:])
            self.assertNotIn("--sdr", command)
            self.assertTrue(runtime.public(["ref-01", "nine-grid-reveal"])["ready"])

    def test_changed_code_revokes_readiness_and_rendering(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = self.setup_runtime(root)
            (root / "compositor.mjs").write_text("changed")
            self.assertFalse(runtime.public(["ref-01"])["ready"])
            with self.assertRaises(ValueError):
                runtime.command(root, root / "out.mp4", root / "v.json", 20)

    def test_failed_hardware_contract_is_not_accepted(self):
        for invalid in ({"ok": False}, {"compositor": "cpu"}, {"encoder": "libx265"},
                        {"adapter": {"isFallbackAdapter": True}}):
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as temp:
                with self.assertRaises(ValueError):
                    self.setup_runtime(Path(temp), {**evidence(), **invalid})

    def test_gpu_prep_never_uses_cpu_video_encoder(self):
        for transfer in ("arib-std-b67", "smpte2084"):
            color = {"dynamic_range": "hdr", "transfer": transfer,
                     "primaries": "bt2020", "matrix": "bt2020nc", "range": "tv"}
            pixel, args = MatrixTemplateService._clip_color_encoding(color, gpu=True)
            self.assertEqual("yuv420p10le", pixel)
            self.assertIn("hevc_nvenc", args)
            self.assertNotIn("libx265", args)
            self.assertIn(transfer, args)
        pixel, args = MatrixTemplateService._clip_color_encoding({"dynamic_range": "sdr"}, gpu=True)
        self.assertEqual("yuv420p", pixel)
        self.assertIn("h264_nvenc", args)

    def test_output_evidence_must_be_hardware(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runtime = self.setup_runtime(root)
            output = root / "final.mp4"
            sidecar = Path(str(output) + ".json")
            sidecar.write_text(json.dumps({"compositor": "webgpu-native", "adapter": {
                "vendor": "nvidia", "isFallbackAdapter": False}, "mode": "hlg", "frames": 360, "renderSeconds": 12.5}))
            proof = runtime.result(output)
            self.assertEqual(runtime.fingerprint, proof["runtime_sha256"])
            self.assertEqual("hevc_nvenc", proof["encoder"])
            sidecar.write_text('{"compositor":"cpu"}')
            with self.assertRaises(ValueError):
                runtime.result(output)


if __name__ == "__main__":
    unittest.main()
