import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from server.content_domains import ai_edit_v2_media as media


def video_probe(*, duration=12.5, codec="h264", fps="30/1", audio_codec="aac"):
    streams = [
        {
            "codec_type": "video",
            "codec_name": codec,
            "width": 1080,
            "height": 1920,
            "r_frame_rate": fps,
        }
    ]
    if audio_codec:
        streams.append(
            {
                "codec_type": "audio",
                "codec_name": audio_codec,
                "sample_rate": "48000",
                "channels": 2,
            }
        )
    return {"format": {"duration": str(duration), "format_name": "mov,mp4"}, "streams": streams}


def audio_probe(*, duration=12.5, codec="aac", sample_rate="48000"):
    return {
        "format": {"duration": str(duration), "format_name": "mov,mp4"},
        "streams": [
            {
                "codec_type": "audio",
                "codec_name": codec,
                "sample_rate": sample_rate,
                "channels": 1,
            }
        ],
    }


class Result:
    def __init__(self, payload=None, returncode=0, stderr=b""):
        self.stdout = json.dumps(payload or {}).encode("utf-8")
        self.stderr = stderr
        self.returncode = returncode


class SequenceRunner:
    def __init__(self, results, create_output=False):
        self.results = list(results)
        self.calls = []
        self.create_output = create_output

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        if command[0] == "ffmpeg" and self.create_output:
            Path(command[-1]).write_bytes(b"normalized media")
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class ProbeTests(unittest.TestCase):
    def test_probe_uses_json_and_returns_normalized_video_metadata(self):
        runner = SequenceRunner([Result(video_probe())])

        result = media.probe_media("source.mp4", runner=runner, media_type="video")

        command, kwargs = runner.calls[0]
        self.assertEqual(command[0], "ffprobe")
        self.assertIn("-show_streams", command)
        self.assertIn("-show_format", command)
        self.assertEqual(command[command.index("-of") + 1], "json")
        self.assertNotIn("shell", kwargs)
        self.assertEqual(
            result,
            {
                "duration_ms": 12_500,
                "width": 1080,
                "height": 1920,
                "fps": 30.0,
                "video_codec": "h264",
                "audio_codec": "aac",
                "sample_rate": 48_000,
                "channels": 2,
                "container": "mov,mp4",
            },
        )

    def test_probe_rejects_zero_over_ten_minutes_and_declared_type_mismatch(self):
        cases = (
            (video_probe(duration=0), "video", "media_invalid_duration"),
            (video_probe(duration=600.001), "video", "media_invalid_duration"),
            (audio_probe(), "video", "media_type_mismatch"),
            (video_probe(audio_codec=None), "audio", "media_type_mismatch"),
        )

        for payload, media_type, code in cases:
            with self.subTest(code=code):
                runner = SequenceRunner([Result(payload)])
                with self.assertRaises(media.MediaError) as caught:
                    media.probe_media("source", runner=runner, media_type=media_type)
                self.assertEqual(caught.exception.code, code)

    def test_probe_maps_missing_binary_timeout_and_bad_json_to_stable_codes(self):
        cases = (
            (FileNotFoundError("ffprobe"), "ffprobe_missing"),
            (subprocess.TimeoutExpired("ffprobe", 30), "ffprobe_timeout"),
            (Result(returncode=1, stderr=b"corrupt"), "media_probe_failed"),
            (Result(), "media_probe_failed"),
        )
        cases[-1][0].stdout = b"not-json"

        for outcome, code in cases:
            with self.subTest(code=code):
                runner = SequenceRunner([outcome])
                with self.assertRaises(media.MediaError) as caught:
                    media.probe_media("source", runner=runner, media_type="video")
                self.assertEqual(caught.exception.code, code)

    def test_probe_rejects_unreadable_stream_parameters_with_stable_code(self):
        malformed = video_probe()
        malformed["streams"][0]["width"] = "not-a-number"

        with self.assertRaises(media.MediaError) as caught:
            media.probe_media(
                "source.mp4",
                runner=SequenceRunner([Result(malformed)]),
                media_type="video",
            )

        self.assertEqual(caught.exception.code, "media_probe_failed")


class NormalizeTests(unittest.TestCase):
    def test_video_normalization_targets_h264_aac_30fps_and_48khz_without_shell(self):
        runner = SequenceRunner(
            [Result(video_probe(codec="hevc", fps="25/1")), Result(), Result(video_probe())],
            create_output=True,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = os.path.join(temp_dir, "normalized.mp4")

            result = media.normalize_media(
                "source.mov", destination, "video", runner=runner
            )

        command, kwargs = runner.calls[1]
        self.assertIsInstance(command, list)
        self.assertEqual(command[0], "ffmpeg")
        self.assertEqual(command[command.index("-c:v") + 1], "libx264")
        self.assertEqual(command[command.index("-r") + 1], "30")
        self.assertEqual(command[command.index("-c:a") + 1], "aac")
        self.assertEqual(command[command.index("-ar") + 1], "48000")
        self.assertNotIn("shell", kwargs)
        self.assertEqual(result["duration_ms"], 12_500)

    def test_audio_normalization_targets_m4a_aac_48khz(self):
        runner = SequenceRunner(
            [Result(audio_probe(codec="mp3", sample_rate="44100")), Result(), Result(audio_probe())],
            create_output=True,
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = os.path.join(temp_dir, "normalized.m4a")
            media.normalize_media("source.mp3", destination, "audio", runner=runner)

        command = runner.calls[1][0]
        self.assertIn("-vn", command)
        self.assertEqual(command[command.index("-c:a") + 1], "aac")
        self.assertEqual(command[command.index("-ar") + 1], "48000")
        self.assertEqual(command[command.index("-f") + 1], "ipod")

    def test_normalize_rejects_missing_ffmpeg_timeout_empty_output_and_duration_drift(self):
        errors = (
            (FileNotFoundError("ffmpeg"), False, None, "ffmpeg_missing"),
            (subprocess.TimeoutExpired("ffmpeg", 600), False, None, "ffmpeg_timeout"),
            (Result(returncode=1), False, None, "ffmpeg_failed"),
            (Result(), False, None, "normalized_output_missing"),
            (Result(), True, video_probe(duration=20), "duration_drift"),
        )
        for ffmpeg_result, create_output, after, code in errors:
            outcomes = [Result(video_probe()), ffmpeg_result]
            if after is not None:
                outcomes.append(Result(after))
            runner = SequenceRunner(outcomes, create_output=create_output)
            with tempfile.TemporaryDirectory() as temp_dir:
                destination = os.path.join(temp_dir, "normalized.mp4")
                with self.subTest(code=code), self.assertRaises(media.MediaError) as caught:
                    media.normalize_media(
                        "source.mov", destination, "video", runner=runner
                    )
            self.assertEqual(caught.exception.code, code)


class FakeCos:
    def __init__(self):
        self.deleted = []
        self.uploaded = []

    def download_file(self, key, destination):
        Path(destination).write_bytes(b"source")

    def put_file(self, path, key, content_type=None, private=False):
        self.uploaded.append((key, content_type, private, Path(path).read_bytes()))

    def delete_object(self, key):
        self.deleted.append(key)


class PrepareTests(unittest.TestCase):
    def test_prepare_uses_task_temp_directory_cleans_it_and_preserves_original_cos_object(self):
        fake_cos = FakeCos()
        seen_paths = []

        def runner(command, **kwargs):
            seen_paths.extend(arg for arg in command if isinstance(arg, str) and "ai-edit-v2-" in arg)
            if command[0] == "ffprobe":
                payload = video_probe(codec="hevc", fps="25/1") if len(seen_paths) < 2 else video_probe()
                return Result(payload)
            Path(command[-1]).write_bytes(b"normalized")
            return Result()

        result = media.prepare_cos_media(
            "ai-edit-v2/owner/task/source.mov",
            "ai-edit-v2/owner/task/normalized.mp4",
            "video",
            cos_api=fake_cos,
            runner=runner,
        )

        self.assertEqual(result["cos_key"], "ai-edit-v2/owner/task/normalized.mp4")
        self.assertEqual(fake_cos.deleted, [])
        self.assertEqual(fake_cos.uploaded[0][:3], (
            "ai-edit-v2/owner/task/normalized.mp4", "video/mp4", True
        ))
        self.assertTrue(seen_paths)
        self.assertTrue(all(not Path(path).exists() for path in seen_paths))


if __name__ == "__main__":
    unittest.main()
