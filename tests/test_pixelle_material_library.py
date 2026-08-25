from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest import mock
from pathlib import Path
from unittest.mock import patch

from server.material_library_api import build_server


ROOT = Path(__file__).resolve().parents[1]
CLIENT_PATH = (
    ROOT
    / "deploy/pixelle-video/overrides/pixelle_video/services/material_library_client.py"
)


def load_client():
    spec = importlib.util.spec_from_file_location("material_library_client", CLIENT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PixelleMaterialLibraryClientTests(unittest.TestCase):
    def setUp(self):
        self.client = load_client()
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "library"
        self.task = Path(self.temp.name) / "task"
        (self.root / "files").mkdir(parents=True)
        self.rows = []
        self._add("scene-a", ".jpg", "image/jpeg", ["医美", "抗衰"])
        self._add("scene-b", ".mp4", "video/mp4", ["门店", "服务"])
        self._add("music", ".mp3", "audio/mpeg", ["背景音乐"])
        (self.root / "index.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in self.rows),
            encoding="utf-8",
        )
        self.server = build_server("127.0.0.1", 0, self.root, "library-token")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.env = patch.dict(os.environ, {
            "PIXELLE_MATERIAL_LIBRARY_URL": f"http://127.0.0.1:{self.server.server_port}",
            "PIXELLE_MATERIAL_LIBRARY_TOKEN": "library-token",
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def _add(self, name, suffix, content_type, tags):
        payload = (name + content_type).encode()
        sha256 = hashlib.sha256(payload).hexdigest()
        relative = Path("files") / (name + suffix)
        (self.root / relative).write_bytes(payload)
        media_type = "bgm" if content_type.startswith("audio/") else content_type.split("/", 1)[0]
        self.rows.append({
            "record_id": name,
            "sha256": sha256,
            "素材名称": name,
            "状态": "可使用",
            "画面方向": "竖屏",
            "标签": tags,
            "server_relative_path": relative.as_posix(),
            "_expected_media_type": media_type,
        })

    def test_downloads_unique_task_owned_visuals_and_bgm(self):
        health = asyncio.run(self.client.check_library_health())
        self.assertEqual({"ready": True, "records": 3}, health)
        result = asyncio.run(self.client.prepare_library_materials(
            ["医美抗衰方案", "门店服务流程"],
            task_id="task-1",
            task_dir=str(self.task),
            width=1080,
            height=1920,
            frame_template="1080x1920/image_default.html",
        ))
        self.assertEqual(2, len(result["visuals"]))
        self.assertTrue(Path(result["bgm_path"]).is_file())
        self.assertEqual(3, len(result["manifest"]))
        self.assertEqual(3, len({item["sha256"] for item in result["manifest"]}))
        for item in result["visuals"]:
            path = Path(item["path"])
            self.assertTrue(path.is_file())
            self.assertEqual((self.task / "library_materials").resolve(), path.parent.resolve())
        self.assertNotIn("path", result["manifest"][0])
        probe = asyncio.run(self.client.probe_library_capacity(2, "portrait"))
        self.assertEqual({"ready": True, "scene_count": 2, "selected_count": 3}, probe)
        with self.assertRaises(self.client.MaterialLibraryClientError):
            asyncio.run(self.client.probe_library_capacity(3, "portrait"))

    def test_portrait_canvas_overrides_square_internal_media_slot(self):
        result = asyncio.run(self.client.prepare_library_materials(
            ["医美抗衰方案", "门店服务流程"],
            task_id="task-square-slot",
            task_dir=str(self.task),
            width=1024,
            height=1024,
            frame_template="1080x1920/image_blur_card.html",
        ))
        self.assertEqual(2, len(result["visuals"]))
        self.assertEqual("portrait", self.client._canvas_orientation(
            "1080x1920/image_blur_card.html", 1024, 1024
        ))

    def test_selection_shortage_returns_actionable_error_without_httpx_url(self):
        with self.assertRaises(self.client.MaterialLibraryClientError) as rejected:
            asyncio.run(self.client.prepare_library_materials(
                ["第一幕", "第二幕", "第三幕"],
                task_id="task-shortage",
                task_dir=str(self.task),
                width=1024,
                height=1024,
                frame_template="1080x1920/image_default.html",
            ))
        self.assertIn("没有足够", str(rejected.exception))
        self.assertNotIn("developer.mozilla.org", str(rejected.exception))

    def test_cross_orientation_media_uses_blurred_background_fit(self):
        source = self.task / "library_materials" / "wide.jpg"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"source")

        def fake_run(command, output, _timeout, _cancel_event):
            Path(output).write_bytes(b"adapted")

        with mock.patch.object(self.client, "_run_managed_process", side_effect=fake_run) as run:
            adapted = self.client._adapt_fallback_media(
                str(source),
                {"media_type": "image", "orientation_match": "fallback"},
                1024,
                1024,
            )
        self.assertTrue(adapted.endswith("_fit.jpg"))
        command = run.call_args.args[0]
        self.assertIn("boxblur=20:5", command[command.index("-filter_complex") + 1])
        self.assertIn("force_original_aspect_ratio=decrease", command[command.index("-filter_complex") + 1])

    def test_same_orientation_media_skips_adaptation(self):
        source = self.task / "same.jpg"
        with mock.patch.object(self.client, "_run_managed_process") as run:
            result = self.client._adapt_fallback_media(
                str(source),
                {"media_type": "image", "orientation_match": "same"},
                1024,
                1024,
            )
        self.assertEqual(str(source), result)
        run.assert_not_called()

    def test_selection_binding_rejects_order_drift_duplicates_unknown_and_type_mismatch(self):
        valid = [
            {"scene_id": "scene_02", "sha256": "b" * 64, "media_type": "video", "orientation": "landscape", "orientation_match": "fallback"},
            {"scene_id": "bgm", "sha256": "c" * 64, "media_type": "bgm", "orientation": "unknown", "orientation_match": "not_applicable"},
            {"scene_id": "scene_01", "sha256": "a" * 64, "media_type": "image", "orientation": "portrait", "orientation_match": "same"},
        ]
        ordered = self.client._validate_selection(
            valid, ["scene_01", "scene_02", "bgm"], "portrait"
        )
        self.assertEqual(["scene_01", "scene_02", "bgm"], [item["scene_id"] for item in ordered])
        invalid_values = [
            valid[:-1],
            [valid[0], valid[0], valid[2]],
            [valid[0], valid[1], {**valid[2], "scene_id": "unknown"}],
            [{**valid[2], "media_type": "bgm"}, valid[0], valid[1]],
            [valid[2], valid[0], {**valid[1], "media_type": "image"}],
            [{**valid[2], "sha256": "bad"}, valid[0], valid[1]],
            [{key: value for key, value in valid[2].items() if key != "orientation_match"}, valid[0], valid[1]],
            [{**valid[2], "orientation_match": "unknown"}, valid[0], valid[1]],
            [{**valid[2], "orientation": "landscape", "orientation_match": "same"}, valid[0], valid[1]],
            [valid[2], valid[0], {**valid[1], "orientation_match": "fallback"}],
        ]
        for value in invalid_values:
            with self.subTest(value=value), self.assertRaises(self.client.MaterialLibraryClientError):
                self.client._validate_selection(
                    value, ["scene_01", "scene_02", "bgm"], "portrait"
                )

    def test_cancel_waits_for_managed_adaptation_cleanup_and_prevents_late_output(self):
        source = self.task / "library_materials" / "cancel.jpg"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"source")
        output = source.with_name(source.stem + "_fit.jpg")
        started = threading.Event()
        finished = threading.Event()

        def blocked_runner(_command, output_path, _timeout, cancel_event):
            started.set()
            cancel_event.wait(5)
            Path(output_path).write_bytes(b"partial")
            Path(output_path).unlink(missing_ok=True)
            finished.set()
            raise RuntimeError("cancelled")

        async def scenario():
            with mock.patch.object(
                self.client, "_run_managed_process", side_effect=blocked_runner
            ):
                task = asyncio.create_task(
                    self.client._adapt_fallback_media_cancellable(
                        str(source),
                        {"media_type": "image", "orientation_match": "fallback"},
                        320,
                        320,
                    )
                )
                await asyncio.to_thread(started.wait, 2)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertTrue(finished.is_set())
                self.assertFalse(output.exists())
                await asyncio.sleep(0.05)
                self.assertFalse(output.exists())

        asyncio.run(scenario())

    def test_adaptation_timeout_removes_partial_output(self):
        source = self.task / "library_materials" / "timeout.mp4"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"source")
        output = source.with_name(source.stem + "_fit.mp4")

        def timeout_runner(_command, output_path, _timeout, _cancel_event):
            Path(output_path).write_bytes(b"partial")
            raise RuntimeError("timed out")

        with mock.patch.object(
            self.client, "_run_managed_process", side_effect=timeout_runner
        ), self.assertRaises(self.client.MaterialLibraryClientError):
            self.client._adapt_fallback_media(
                str(source),
                {"media_type": "video", "orientation_match": "fallback"},
                320,
                320,
            )
        self.assertFalse(output.exists())

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required")
    def test_real_image_and_video_adaptation_outputs_expected_contract(self):
        self.task.mkdir(parents=True, exist_ok=True)
        source_image = self.task / "wide.png"
        source_video = self.task / "wide.mp4"
        subprocess.run([
            "ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
            "color=c=blue:s=640x360", "-frames:v", "1", str(source_image),
        ], check=True)
        subprocess.run([
            "ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
            "testsrc=size=640x360:rate=24:duration=1", "-f", "lavfi", "-i",
            "sine=frequency=440:duration=1", "-shortest", "-c:v", "libx264",
            "-pix_fmt", "yuv420p", "-c:a", "aac", str(source_video),
        ], check=True)

        def real_runner(command, _output, timeout, _cancel_event):
            subprocess.run(command, check=True, timeout=timeout)

        with mock.patch.object(self.client, "_run_managed_process", side_effect=real_runner):
            fitted_image = self.client._adapt_fallback_media(
                str(source_image), {"media_type": "image", "orientation_match": "fallback"}, 320, 320
            )
            fitted_video = self.client._adapt_fallback_media(
                str(source_video), {"media_type": "video", "orientation_match": "fallback"}, 320, 320
            )
        for path, expected_streams in ((fitted_image, ["video"]), (fitted_video, ["video"])):
            probe = subprocess.run([
                "ffprobe", "-v", "error", "-show_entries",
                "stream=codec_type,codec_name,width,height", "-of", "json", path,
            ], check=True, capture_output=True, text=True)
            streams = json.loads(probe.stdout)["streams"]
            self.assertEqual(expected_streams, [stream["codec_type"] for stream in streams])
            self.assertEqual((320, 320), (streams[0]["width"], streams[0]["height"]))
        self.assertEqual("h264", json.loads(subprocess.run([
            "ffprobe", "-v", "error", "-show_entries", "stream=codec_name",
            "-of", "json", fitted_video,
        ], check=True, capture_output=True, text=True).stdout)["streams"][0]["codec_name"])

    def test_external_or_credential_bearing_urls_are_rejected(self):
        for value in (
            "https://example.com",
            "http://example.com",
            "http://user:pass@127.0.0.1:8111",
            "http://127.0.0.1:8111/path",
        ):
            with self.subTest(value=value), patch.dict(os.environ, {
                "PIXELLE_MATERIAL_LIBRARY_URL": value,
                "PIXELLE_MATERIAL_LIBRARY_TOKEN": "token",
            }):
                with self.assertRaises(self.client.MaterialLibraryClientError):
                    self.client._settings()

    def test_library_mode_patch_skips_prompt_and_media_generation(self):
        patch_text = (
            ROOT
            / "deploy/pixelle-video/patches/0018-support-strict-material-library.patch"
        ).read_text(encoding="utf-8")
        self.assertIn('ctx.image_prompts = [None] * len(ctx.narrations)', patch_text)
        self.assertIn('frame.image_path = visual["path"]', patch_text)
        self.assertIn('frame.video_path = visual["path"]', patch_text)
        self.assertIn('material_source: Literal["ai", "library"]', patch_text)
        self.assertIn('frame_template=ctx.params.get("frame_template") or ""', patch_text)
        self.assertIn('@router.get("/material-library/health")', patch_text)
        self.assertIn('@router.post("/material-library/probe")', patch_text)
        self.assertNotIn("AI fallback", patch_text)


if __name__ == "__main__":
    unittest.main()
