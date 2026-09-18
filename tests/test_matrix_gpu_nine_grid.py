import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from server import matrix_gpu_nine_grid as gpu


class MatrixGpuNineGridTests(unittest.TestCase):
    def test_native_hlg_is_accepted(self):
        gpu.require_hlg(dict(codec_name="hevc", pix_fmt="yuv420p10le",
            color_transfer="arib-std-b67", color_primaries="bt2020", color_space="bt2020nc"))

    def test_other_formats_are_not_silently_relabelled(self):
        valid = dict(codec_name="hevc", pix_fmt="yuv420p10le", color_transfer="arib-std-b67",
                     color_primaries="bt2020", color_space="bt2020nc")
        for key, value in (("codec_name", "h264"), ("pix_fmt", "yuv420p"),
                           ("color_transfer", "bt709"), ("color_transfer", "smpte2084"),
                           ("color_primaries", "bt709"), ("color_space", None)):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                gpu.require_hlg({**valid, key: value})

    def test_timeline_and_reveal_order(self):
        self.assertEqual(360, 96 + sum(section[2] for section in gpu.SECTIONS))
        self.assertEqual([5, 4, 1, 2, 3, 6, 9, 8, 7],
            sorted(range(1, 10), key=lambda slot: gpu.REVEALS[slot-1]))
        self.assertEqual((1, 5, 9), tuple(section[1] for section in gpu.SECTIONS[1:]))

    def test_native_video_does_not_receive_transfer_conversion(self):
        self.assertEqual("[2:v]setpts=PTS-STARTPTS,sidedata=mode=delete,"
                         "setparams=color_trc=linear,hwupload[v2]", gpu.video_upload(2, "v2"))
        self.assertNotIn("custom_shader", gpu.opening_graph())
        self.assertIn("setparams=color_trc=arib-std-b67[out]", gpu.opening_graph())

    def test_impact_blurs_enlarged_rgb_before_crop(self):
        graph = gpu.main_graph(True)
        self.assertIn("w=1264:h=2246", graph)
        self.assertIn("format=gbrap16le", graph)
        self.assertIn("gblur_vulkan=sigma=10:size=61", graph)
        self.assertLess(graph.index("gblur_vulkan"), graph.index("crop_w="))
        self.assertNotIn("gblur_vulkan", gpu.main_graph(False))

    def test_unknown_template_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "index.html").write_text("other template")
            with self.assertRaisesRegex(ValueError, "Unreviewed"):
                gpu.validate_project(root)

    def test_local_asset_cannot_escape_project(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "private.txt").touch()
            (root / "project").mkdir()
            with self.assertRaises(ValueError):
                gpu.local_asset(root / "project", "../private.txt")

    def test_project_mappings_are_checked(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            raw = b"unit test template"
            (root / "index.html").write_bytes(raw)
            values = {f"grid{i}": f"assets/input/video-{i}.mp4" for i in range(1, 10)}
            for relative in values.values():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            bgm = root / "assets/audio/reference-bgm.m4a"
            bgm.parent.mkdir()
            bgm.touch()
            values.update(main1=values["grid1"], main2=values["grid5"], main3=values["grid9"],
                          top_text="title", bottom_text="cta")
            with mock.patch.object(gpu, "TEMPLATE_SHA256", hashlib.sha256(raw).hexdigest()):
                (root / "variables.json").write_text(json.dumps(values))
                gpu.validate_project(root)
                values["main2"] = values["grid6"]
                (root / "variables.json").write_text(json.dumps(values))
                with self.assertRaisesRegex(ValueError, "full-screen"):
                    gpu.validate_project(root)

    def test_timeout_stops_owned_process(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            renderer = gpu.Renderer(root, root, browser=root, puppeteer_module=root)
            process = mock.Mock()
            process.wait.side_effect = subprocess.TimeoutExpired("owned child", 1)
            with mock.patch.object(gpu.subprocess, "Popen", return_value=process), \
                 mock.patch.object(renderer, "stop") as stop:
                with self.assertRaises(subprocess.TimeoutExpired):
                    renderer.run("timeout", ["owned child"])
            stop.assert_called_once_with(process)

    def test_deadline_and_nvenc_are_explicit(self):
        root = Path(".")
        for value in (0, -1, float("inf"), float("nan")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                gpu.Renderer(root, root, browser=root, puppeteer_module=root, timeout=value)
        self.assertIn("hevc_nvenc", gpu.ENCODE)
        self.assertIn("yuv420p10le", gpu.ENCODE)


if __name__ == "__main__":
    unittest.main()
