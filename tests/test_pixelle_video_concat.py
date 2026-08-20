from __future__ import annotations

import asyncio
import importlib.util
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


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
            child = (
                "from pathlib import Path; import time; "
                f"p=Path({str(output)!r}); p.write_bytes(b'partial'); "
                "time.sleep(30); p.write_bytes(b'late')"
            )
            started = time.monotonic()
            with self.assertRaisesRegex(RuntimeError, "timed out after 0.2 seconds"):
                module.run_cancellable_process(
                    [sys.executable, "-c", child],
                    str(output),
                    timeout_seconds=0.2,
                )
            self.assertLess(time.monotonic() - started, 5)
            self.assertFalse(output.exists())
            time.sleep(0.3)
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


class PixelleVideoConcatCancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_task_cancellation_kills_worker_and_keeps_event_loop_responsive(self):
        module = load_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "cancelled.mp4"
            started_file = root / "started.txt"

            def fake_concat(*, videos, output, cancel_event=None):
                child = (
                    "from pathlib import Path; import os,time; "
                    f"Path({str(started_file)!r}).write_text(str(os.getpid())); "
                    f"p=Path({str(output)!r}); p.write_bytes(b'partial'); "
                    "time.sleep(30); p.write_bytes(b'late')"
                )
                module.run_cancellable_process(
                    [sys.executable, "-c", child],
                    output,
                    timeout_seconds=30,
                    cancel_event=cancel_event,
                )
                return output

            pulses = 0

            async def pulse():
                nonlocal pulses
                while True:
                    pulses += 1
                    await asyncio.sleep(0.01)

            pulse_task = asyncio.create_task(pulse())
            task = asyncio.create_task(
                module.concat_videos_cancellable(
                    fake_concat,
                    videos=["one.mp4", "two.mp4"],
                    output=str(output),
                )
            )
            for _ in range(200):
                if started_file.exists():
                    break
                await asyncio.sleep(0.01)
            self.assertTrue(started_file.exists())
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=5)
            pulse_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pulse_task

            self.assertGreater(pulses, 2)
            self.assertFalse(output.exists())
            await asyncio.sleep(0.3)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
