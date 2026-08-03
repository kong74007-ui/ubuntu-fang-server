from __future__ import annotations

import subprocess
from pathlib import Path
import tempfile
import time
import unittest

from server.content_domains.ai_edit_v3.media import mux_master_audio, probe_media


class FinalMuxTests(unittest.TestCase):
    def test_mux_copies_video_and_emits_one_48k_stereo_aac_track(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            silent = root / "silent.mp4"
            master = root / "master.wav"
            output = root / "final.mp4"
            subprocess.run([
                "ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                "color=c=blue:s=1920x1080:r=30:d=1", "-an", "-c:v", "libx264",
                "-pix_fmt", "yuv420p", str(silent),
            ], check=True)
            subprocess.run([
                "ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                "sine=frequency=440:sample_rate=48000:duration=1", "-ac", "2", str(master),
            ], check=True)

            result = mux_master_audio(silent, master, output, duration_ms=1000, deadline_at=time.time() + 60)

            self.assertEqual((result.video_codec, result.audio_codec), ("h264", "aac"))
            self.assertEqual((result.width, result.height), (1920, 1080))
            self.assertEqual((result.fps_num, result.fps_den), (30, 1))
            self.assertEqual((result.sample_rate, result.channels), (48000, 2))
            self.assertLessEqual(abs(result.duration_ms - 1000), 40)
            self.assertEqual(probe_media(output).codecs, ("h264", "aac"))
            command = result.audit["command"]
            self.assertIn("-c:v", command)
            self.assertEqual(command[command.index("-c:v") + 1], "copy")
            self.assertIn("+faststart", command)

    def test_mux_rejects_non_silent_video(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.mp4"
            subprocess.run([
                "ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=s=1080x1920:r=30:d=1",
                "-f", "lavfi", "-i", "sine=duration=1", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-shortest", str(source),
            ], check=True)
            with self.assertRaisesRegex(ValueError, "mux_silent_video_invalid"):
                mux_master_audio(source, source, root / "final.mp4", duration_ms=1000, deadline_at=time.time() + 60)


if __name__ == "__main__":
    unittest.main()
