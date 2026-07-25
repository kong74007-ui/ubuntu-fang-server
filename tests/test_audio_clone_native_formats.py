import base64
import json
import pathlib
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch


ROOT = pathlib.Path(__file__).resolve().parents[1]

import sys

sys.path.insert(0, str(ROOT / "server"))

from content_domains import audio


class NativeCloneAudioTest(unittest.TestCase):
    def _probe(self, codec, sample_rate=24000, bits=0, duration=12.5):
        return SimpleNamespace(stdout=json.dumps({
            "streams": [{
                "codec_name": codec,
                "sample_rate": str(sample_rate),
                "bits_per_sample": bits,
            }],
            "format": {
                "duration": str(duration),
                "format_name": "test",
            },
        }).encode())

    def _prepare(self, audio_format, probe):
        raw = (audio_format + "-original-bytes").encode()
        with tempfile.TemporaryDirectory() as td:
            probe_path = pathlib.Path(td) / ("probe." + audio_format)
            with patch.object(audio, "_out_path", return_value=probe_path), \
                    patch.object(audio.subprocess, "run", return_value=probe) as run:
                result_b64, result_format = audio.prepare_clone_audio(
                    base64.b64encode(raw).decode(), audio_format,
                )
            self.assertFalse(probe_path.exists())
        self.assertEqual(raw, base64.b64decode(result_b64))
        self.assertEqual(audio_format, result_format)
        self.assertEqual("ffprobe", run.call_args.args[0][0])
        self.assertNotIn("ffmpeg", run.call_args.args[0])

    def test_supported_formats_keep_original_bytes(self):
        self._prepare("mp3", self._probe("mp3"))
        self._prepare("wav", self._probe("pcm_s16le", bits=16))
        self._prepare("m4a", self._probe("aac"))

    def test_rejects_non_official_formats(self):
        raw = base64.b64encode(b"audio").decode()
        for audio_format in ("aac", "ogg", "flac"):
            with self.subTest(audio_format=audio_format):
                with self.assertRaisesRegex(ValueError, "仅支持"):
                    audio.prepare_clone_audio(raw, audio_format)

    def test_rejects_invalid_duration_sample_rate_and_wav_depth(self):
        cases = [
            ("mp3", self._probe("mp3", duration=4.9), "至少需要 5 秒"),
            ("mp3", self._probe("mp3", sample_rate=8000), "不能低于 16kHz"),
            ("wav", self._probe("pcm_s24le", bits=24), "必须为 16-bit PCM"),
        ]
        for audio_format, probe, message in cases:
            with self.subTest(audio_format=audio_format, message=message):
                raw = base64.b64encode(b"sample").decode()
                with tempfile.TemporaryDirectory() as td, \
                        patch.object(audio, "_out_path",
                                     return_value=pathlib.Path(td) / ("probe." + audio_format)), \
                        patch.object(audio.subprocess, "run", return_value=probe):
                    with self.assertRaisesRegex(ValueError, message):
                        audio.prepare_clone_audio(raw, audio_format)

    def test_long_audio_is_stream_copied_to_59_seconds(self):
        raw = base64.b64encode(b"long-original-mp3").decode()
        trimmed_bytes = b"trimmed-mp3-without-reencoding"
        calls = []
        probes = iter([
            self._probe("mp3", duration=77),
            self._probe("mp3", duration=59),
        ])
        with tempfile.TemporaryDirectory() as td:
            src = pathlib.Path(td) / "source.mp3"
            trimmed = pathlib.Path(td) / "trimmed.mp3"

            def run(cmd, **kwargs):
                calls.append(cmd)
                if cmd[0] == "ffprobe":
                    return next(probes)
                trimmed.write_bytes(trimmed_bytes)
                return SimpleNamespace(stdout=b"")

            with patch.object(audio, "_out_path", side_effect=[src, trimmed]), \
                    patch.object(audio.subprocess, "run", side_effect=run):
                result_b64, result_format = audio.prepare_clone_audio(raw, "mp3")

            self.assertFalse(src.exists())
            self.assertFalse(trimmed.exists())
        self.assertEqual(trimmed_bytes, base64.b64decode(result_b64))
        self.assertEqual("mp3", result_format)
        ffmpeg_cmd = next(cmd for cmd in calls if cmd[0] == "ffmpeg")
        self.assertEqual("59", ffmpeg_cmd[ffmpeg_cmd.index("-t") + 1])
        self.assertEqual("copy", ffmpeg_cmd[ffmpeg_cmd.index("-c:a") + 1])
        self.assertTrue(ffmpeg_cmd[-1].endswith(".mp3"))

    def test_upload_uses_original_extension(self):
        raw = base64.b64encode(b"original-m4a").decode()
        with tempfile.TemporaryDirectory() as td, \
                patch.object(audio, "_out_path",
                             return_value=pathlib.Path(td) / "reference.m4a"), \
                patch.object(audio.cos, "enabled", return_value=True), \
                patch.object(audio.cos, "upload") as upload, \
                patch.object(audio.cos, "object_url", return_value="https://example/ref.m4a"), \
                patch.object(audio.cosyvoice, "create_voice",
                             return_value="cosyvoice-v3.5-plus-hq-test"), \
                patch.object(audio.cosyvoice, "voice_status", return_value=("OK", {})), \
                patch.object(audio, "adb") as adb:
            # The database portion is outside this test's file-extension contract.
            adb.side_effect = RuntimeError("stop after provider upload")
            with self.assertRaisesRegex(RuntimeError, "stop after provider upload"):
                audio._clone_via_cosyvoice("fang", "slot", "name", raw, "m4a")
        uploaded_path, object_key = upload.call_args.args
        self.assertTrue(str(uploaded_path).endswith(".m4a"))
        self.assertTrue(object_key.endswith(".m4a"))


class NativeCloneAudioFrontendTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "site" / "workbench" / "assets.html").read_text(
            encoding="utf-8",
        )

    def test_picker_only_accepts_official_formats(self):
        self.assertIn(
            'accept=".mp3,.wav,.m4a,audio/mpeg,audio/wav,audio/x-wav,'
            'audio/mp4,audio/x-m4a"',
            self.html,
        )
        self.assertIn("['mp3','wav','m4a'].indexOf(ext)<0", self.html)

    def test_submission_preserves_file_and_format(self):
        submit = self.html.split("function submitVipFile(file, ext){", 1)[1]
        submit = submit.split("vipBtn.onclick=function()", 1)[0]
        self.assertIn("reader.readAsDataURL(file);", submit)
        self.assertIn("audio_format:ext", submit)
        self.assertNotIn("trimAudioTo60(file)", submit)
        self.assertNotIn("audio_format:'wav'", submit)

    def test_duration_and_size_handling(self):
        self.assertIn("if(f.size>10*1024*1024)", self.html)
        self.assertIn("if(sec>60)", self.html)
        self.assertIn("if(sec<5)", self.html)
        over_limit = self.html.split("if(sec>60){", 1)[1]
        over_limit = over_limit.split("if(sec<5){", 1)[0]
        self.assertNotIn("return;", over_limit)
        self.assertIn(r"\u81ea\u52a8\u4fdd\u7559\u524d59\u79d2", over_limit)


if __name__ == "__main__":
    unittest.main()
