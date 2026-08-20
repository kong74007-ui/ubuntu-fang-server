from __future__ import annotations

import importlib.util
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = (
    ROOT
    / "deploy"
    / "pixelle-video"
    / "overrides"
    / "pixelle_video"
    / "services"
    / "video_concat.py"
)


def load_module():
    spec = importlib.util.spec_from_file_location("pixelle_video_concat", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PixelleVideoConcatTests(unittest.TestCase):
    def test_filter_normalizes_mixed_frame_rates_and_timestamps(self):
        module = load_module()
        expression = module.build_concat_filter(2)

        for marker in (
            "[0:v]fps=30,settb=AVTB,setpts=PTS-STARTPTS[v0]",
            "[1:v]fps=30,settb=AVTB,setpts=PTS-STARTPTS[v1]",
            "aresample=44100:async=1:first_pts=0,asetpts=PTS-STARTPTS",
            "[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]",
        ):
            self.assertIn(marker, expression)

    def test_timeout_removes_partial_output_and_reports_terminal_failure(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "partial.mp4"
            output.write_bytes(b"partial")
            with patch.object(
                module.subprocess,
                "run",
                side_effect=subprocess.TimeoutExpired(["ffmpeg"], 600),
            ):
                with self.assertRaisesRegex(RuntimeError, "timed out after 600 seconds"):
                    module.concat_with_normalized_streams(
                        ["one.mp4", "two.mp4"],
                        str(output),
                    )
            self.assertFalse(output.exists())

    def test_real_mixed_frame_rate_concat_finishes_with_bounded_duration(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inputs = []
            for index, fps in enumerate((25, 30)):
                path = root / f"input-{fps}.mp4"
                subprocess.run(
                    [
                        "ffmpeg",
                        "-v",
                        "error",
                        "-f",
                        "lavfi",
                        "-i",
                        f"color=c=0x336699:s=160x90:r={fps}:d=1",
                        "-f",
                        "lavfi",
                        "-i",
                        "sine=frequency=440:sample_rate=44100:duration=1",
                        "-shortest",
                        "-c:v",
                        "libx264",
                        "-pix_fmt",
                        "yuv420p",
                        "-c:a",
                        "aac",
                        "-y",
                        str(path),
                    ],
                    check=True,
                    capture_output=True,
                )
                inputs.append(str(path))

            output = root / "final.mp4"
            module.concat_with_normalized_streams(inputs, str(output), timeout_seconds=30)
            probe = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=avg_frame_rate,nb_frames:format=duration",
                    "-of",
                    "default=noprint_wrappers=1",
                    str(output),
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            self.assertIn("avg_frame_rate=30/1", probe)
            duration = float(
                next(line.split("=", 1)[1] for line in probe.splitlines() if line.startswith("duration="))
            )
            self.assertGreater(duration, 1.8)
            self.assertLess(duration, 2.3)


if __name__ == "__main__":
    unittest.main()
