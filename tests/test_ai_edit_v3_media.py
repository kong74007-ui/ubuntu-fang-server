from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from server.content_domains.ai_edit_v3.media import (
    MediaProcessError,
    MediaProbe,
    MediaValidationError,
    decode_and_normalize_image,
    extract_director_keyframes,
    normalize_primary_media,
    probe_media,
    validate_primary_media,
)


class Completed:
    def __init__(self, payload: dict[str, object] | None = None, *, returncode: int = 0):
        self.stdout = json.dumps(payload or {}).encode("utf-8")
        self.stderr = b""
        self.returncode = returncode


def video_payload(
    *,
    duration: str = "3.000",
    width: int = 1080,
    height: int = 1920,
    fps: str = "30/1",
    rotation: int = 0,
) -> dict[str, object]:
    return {
        "format": {"duration": duration},
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "width": width,
                "height": height,
                "r_frame_rate": fps,
                "tags": {"rotate": str(rotation)},
            },
            {"codec_type": "audio", "codec_name": "aac"},
        ],
    }


def image_payload(
    *,
    codec: str = "mjpeg",
    width: int = 2,
    height: int = 3,
    rotation: int = 90,
) -> dict[str, object]:
    return {
        "format": {"format_name": "image2"},
        "streams": [
            {
                "codec_type": "video",
                "codec_name": codec,
                "width": width,
                "height": height,
                "tags": {"rotate": str(rotation)},
            }
        ],
    }


class MediaContractTests(unittest.TestCase):
    def test_uploaded_audio_rejects_video_stream(self) -> None:
        probe = MediaProbe(
            media_type="video",
            duration_ms=3_000,
            width=1080,
            height=1920,
            fps_num=30,
            fps_den=1,
            rotation=0,
            codecs=("h264", "aac"),
            streams=({"codec_type": "video"}, {"codec_type": "audio"}),
        )

        with self.assertRaisesRegex(MediaValidationError, "media_type_mismatch"):
            validate_primary_media(probe, input_type="uploaded_audio")

    def test_primary_media_limits_include_exact_duration_dimension_and_fps_boundaries(self) -> None:
        valid = MediaProbe(
            media_type="video",
            duration_ms=3_000,
            width=4096,
            height=2160,
            fps_num=60,
            fps_den=1,
            rotation=90,
            codecs=("h264", "aac"),
            streams=({"codec_type": "video"}, {"codec_type": "audio"}),
        )
        validate_primary_media(valid, input_type="uploaded_video")
        validate_primary_media(
            MediaProbe(**{**valid.__dict__, "duration_ms": 600_000}),
            input_type="platform_talking_head",
        )

        invalid = (
            ({"duration_ms": 2_999}, "media_duration_invalid"),
            ({"duration_ms": 600_001}, "media_duration_invalid"),
            ({"width": 4097}, "video_dimensions_invalid"),
            ({"fps_num": 61}, "video_fps_invalid"),
        )
        for changes, code in invalid:
            with self.subTest(code=code), self.assertRaisesRegex(MediaValidationError, code):
                validate_primary_media(
                    MediaProbe(**{**valid.__dict__, **changes}),
                    input_type="uploaded_video",
                )

    def test_probe_uses_bounded_ffprobe_and_parses_rotation_and_fractional_fps(self) -> None:
        with patch(
            "server.content_domains.ai_edit_v3.media._run_process",
            return_value=Completed(video_payload(rotation=90)),
        ) as run:
            result = probe_media(Path("source.mp4"))

        command = run.call_args.args[0]
        self.assertEqual(command[0], "ffprobe")
        self.assertNotIn("shell", run.call_args.kwargs)
        self.assertEqual(run.call_args.kwargs["timeout_seconds"], 30)
        self.assertEqual(result.rotation, 90)
        self.assertEqual((result.fps_num, result.fps_den), (30, 1))
        self.assertEqual(result.codecs, ("h264", "aac"))

    def test_normalization_builds_local_cfr_rotation_safe_command_and_audit(self) -> None:
        before = MediaProbe(
            media_type="video", duration_ms=3_000, width=1920, height=1080,
            fps_num=30, fps_den=1, rotation=90, codecs=("h264", "aac"),
            streams=({"codec_type": "video"}, {"codec_type": "audio"}),
        )
        after = MediaProbe(
            media_type="video", duration_ms=3_000, width=1080, height=1920,
            fps_num=30, fps_den=1, rotation=0, codecs=("h264", "aac"),
            streams=({"codec_type": "video"}, {"codec_type": "audio"}),
        )
        commands: list[list[str]] = []

        def run(command: list[str], *, timeout_seconds: float) -> Completed:
            commands.append(command)
            Path(command[-1]).write_bytes(b"normalized")
            return Completed()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mov"
            source.write_bytes(b"source")
            with patch(
                "server.content_domains.ai_edit_v3.media.probe_media",
                side_effect=(before, after),
            ), patch(
                "server.content_domains.ai_edit_v3.media._run_process",
                side_effect=run,
            ):
                result = normalize_primary_media(
                    source,
                    root / "out",
                    input_type="uploaded_video",
                    deadline_at=10**12,
                )

            command = commands[0]
            self.assertEqual(command[0], "ffmpeg")
            self.assertEqual(command[command.index("-c:v") + 1], "libx264")
            self.assertEqual(command[command.index("-pix_fmt") + 1], "yuv420p")
            self.assertEqual(command[command.index("-r") + 1], "30")
            self.assertIn("transpose=1", command[command.index("-vf") + 1])
            self.assertEqual(command[command.index("-protocol_whitelist") + 1], "file,pipe")
            self.assertEqual(result.ratio, "9:16")
            self.assertEqual(result.time_base_num, 1)
            self.assertEqual(result.time_base_den, 30)
            audit_json = json.dumps(result.audit)
            self.assertNotIn("token=", audit_json)
            self.assertNotIn("https://", audit_json)

    def test_image_decode_physically_applies_exif_and_rejects_unsupported_format(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "rotated.jpg"
            source.write_bytes(b"jpeg-source")

            outcomes = [
                Completed(image_payload()),
                Completed(),
                Completed(image_payload(codec="webp", width=3, height=2, rotation=0)),
            ]

            def run(command: list[str], *, timeout_seconds: float) -> Completed:
                outcome = outcomes.pop(0)
                if command[0] == "ffmpeg":
                    Path(command[-1]).write_bytes(b"webp-output")
                return outcome

            with patch(
                "server.content_domains.ai_edit_v3.media._run_process",
                side_effect=run,
            ):
                result = decode_and_normalize_image(
                    source,
                    root / "out",
                    deadline_at=time.time() + 60,
                )
            self.assertEqual((result.width, result.height), (3, 2))
            self.assertTrue((root / "out" / result.relative_path).is_file())

            unsupported = root / "bad.gif"
            unsupported.write_bytes(b"gif-source")
            with patch(
                "server.content_domains.ai_edit_v3.media._run_process",
                return_value=Completed(image_payload(codec="gif", rotation=0)),
            ), self.assertRaisesRegex(MediaValidationError, "image_format_invalid"):
                decode_and_normalize_image(unsupported, root / "out2", deadline_at=time.time() + 60)

    def test_media_module_imports_without_unpinned_site_packages(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-S",
                "-c",
                "from server.content_domains.ai_edit_v3 import media; print(media.MAX_DURATION_MS)",
            ],
            check=False,
            cwd=Path(__file__).resolve().parents[1],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "600000")

    def test_keyframes_are_bounded_deterministic_and_include_first_and_last(self) -> None:
        probe = MediaProbe(
            media_type="video", duration_ms=11_000, width=1920, height=1080,
            fps_num=30, fps_den=1, rotation=0, codecs=("h264", "aac"),
            streams=({"codec_type": "video"}, {"codec_type": "audio"}),
        )

        def run(command: list[str], *, timeout_seconds: float) -> Completed:
            Path(command[-1]).write_bytes(b"jpeg")
            return Completed()

        with tempfile.TemporaryDirectory() as directory, patch(
            "server.content_domains.ai_edit_v3.media.probe_media",
            return_value=probe,
        ), patch(
            "server.content_domains.ai_edit_v3.media._run_process",
            side_effect=run,
        ):
            root = Path(directory)
            frames = extract_director_keyframes(root / "source.mp4", root, max_frames=12)

            self.assertEqual(len(frames), 12)
            self.assertEqual(frames[0].source_ms, 0)
            self.assertEqual(frames[-1].source_ms, 10_999)
            self.assertEqual([frame.relative_path for frame in frames], sorted(frame.relative_path for frame in frames))

    def test_process_failures_use_stable_redacted_codes(self) -> None:
        with patch(
            "server.content_domains.ai_edit_v3.media._run_process",
            side_effect=TimeoutError("https://secret.example/path?token=private"),
        ):
            with self.assertRaises(MediaProcessError) as caught:
                probe_media(Path("source.mp4"))
        self.assertEqual(caught.exception.code, "ffprobe_timeout")
        self.assertNotIn("secret", str(caught.exception))

    def test_media_deadline_uses_pipeline_unix_epoch_seconds(self) -> None:
        before = MediaProbe(
            media_type="audio", duration_ms=3_000, width=None, height=None,
            fps_num=None, fps_den=None, rotation=0, codecs=("aac",),
            streams=({"codec_type": "audio"},),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.m4a"
            source.write_bytes(b"source")
            with patch(
                "server.content_domains.ai_edit_v3.media.probe_media",
                return_value=before,
            ), self.assertRaisesRegex(MediaProcessError, "media_deadline_exceeded"):
                normalize_primary_media(
                    source,
                    root / "out",
                    input_type="uploaded_audio",
                    deadline_at=time.time() - 1,
                )


if __name__ == "__main__":
    unittest.main()
