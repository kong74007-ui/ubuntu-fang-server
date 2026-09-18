"""Opt-in, real 4K late-source GPU regression; no library/provider requests."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.environ.get("MATRIX_GPU_INTEGRATION") == "1", "Requires NVIDIA GPU, NVENC and Chrome")
class GpuDecodeWindowsIntegrationTests(unittest.TestCase):
    def test_cold_4k_late_and_disjoint_selections_decode_only_used_frames(self):
        browser = os.environ.get("MATRIX_GPU_TEST_BROWSER", "C:/Program Files/Google/Chrome/Application/chrome.exe")
        self.assertTrue(Path(browser).is_file())
        with tempfile.TemporaryDirectory(prefix="matrix-gpu-window-") as temp:
            root = Path(temp)
            source = root / "source.mp4"
            subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                "color=red:s=2160x3840:r=30:d=30",
                "-vf", "drawbox=color=blue:t=fill:enable='gte(t,10)',format=p010le,"
                       "setparams=range=tv:color_primaries=bt2020:color_trc=arib-std-b67:colorspace=bt2020nc",
                "-an", "-c:v", "hevc_nvenc", "-preset", "p1", "-g", "30", "-pix_fmt", "p010le", str(source),
            ], check=True, timeout=180, capture_output=True)
            cases = [
                ("late", [(0, 3, 20)], [{"start": 600, "end": 690}], [(1, 2)]),
                ("disjoint", [(0, 1.5, 2), (1.5, 1.5, 20)],
                 [{"start": 60, "end": 105}, {"start": 600, "end": 645}], [(.5, 0), (2, 2)]),
            ]
            for name, clips, windows, samples in cases:
                with self.subTest(case=name):
                    project = root / name
                    project.mkdir()
                    os.link(source, project / "source.mp4")
                    media = "".join(
                        f'<video src="source.mp4" data-start="{start}" data-duration="{duration}" '
                        f'data-media-start="{offset}" style="position:absolute;inset:0;width:1080px;height:1920px;object-fit:cover"></video>'
                        for start, duration, offset in clips
                    )
                    (project / "index.html").write_text(
                        '<html><body style="margin:0"><div data-composition-id="window-test" '
                        'data-width="1080" data-height="1920" data-duration="3" '
                        'style="position:relative;width:1080px;height:1920px;overflow:hidden">'
                        + media + '</div></body></html>', encoding="utf-8",
                    )
                    cache, output = root / (name + "-cache"), root / (name + "-output/final.mp4")
                    self.assertFalse(cache.exists())
                    process = subprocess.run([
                        shutil.which("node"), str(ROOT / "deploy/matrix-gpu/render.mjs"),
                        "--project", str(project), "--output", str(output), "--browser", browser,
                        "--cache", str(cache), "--timeout", "240",
                    ], capture_output=True, text=True, encoding="utf-8", timeout=270)
                    self.assertEqual(0, process.returncode, process.stderr + process.stdout[-1200:])
                    report = json.loads(Path(str(output) + ".json").read_text())
                    self.assertEqual(90, report["frames"])
                    self.assertFalse(report["adapter"]["isFallbackAdapter"])
                    self.assertEqual(windows, report["sources"][0]["decodeWindows"])
                    self.assertEqual(90, report["sources"][0]["decodedFrames"])
                    expected = 2160 * 3840 * 8 * 90
                    self.assertEqual(expected, sum(p.stat().st_size for p in cache.glob("*.rgba16")))
                    self.assertLess(expected, 32 * 1024 ** 3)
                    for second, channel in samples:
                        frame = subprocess.run([
                            "ffmpeg", "-v", "error", "-ss", str(second), "-i", str(output),
                            "-vf", "crop=2:2:540:960,format=rgb24", "-frames:v", "1", "-f", "rawvideo", "pipe:1",
                        ], capture_output=True, check=True, timeout=20).stdout
                        self.assertGreater(frame[channel], 2 * frame[2 if channel == 0 else 0])
                    shutil.rmtree(cache)


if __name__ == "__main__":
    unittest.main()
