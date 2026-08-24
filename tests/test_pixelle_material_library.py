from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import tempfile
import threading
import unittest
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

    def test_selection_binding_rejects_order_drift_duplicates_unknown_and_type_mismatch(self):
        valid = [
            {"scene_id": "scene_02", "sha256": "b" * 64, "media_type": "video"},
            {"scene_id": "bgm", "sha256": "c" * 64, "media_type": "bgm"},
            {"scene_id": "scene_01", "sha256": "a" * 64, "media_type": "image"},
        ]
        ordered = self.client._validate_selection(valid, ["scene_01", "scene_02", "bgm"])
        self.assertEqual(["scene_01", "scene_02", "bgm"], [item["scene_id"] for item in ordered])
        invalid_values = [
            valid[:-1],
            [valid[0], valid[0], valid[2]],
            [valid[0], valid[1], {**valid[2], "scene_id": "unknown"}],
            [{**valid[2], "media_type": "bgm"}, valid[0], valid[1]],
            [valid[2], valid[0], {**valid[1], "media_type": "image"}],
            [{**valid[2], "sha256": "bad"}, valid[0], valid[1]],
        ]
        for value in invalid_values:
            with self.subTest(value=value), self.assertRaises(self.client.MaterialLibraryClientError):
                self.client._validate_selection(value, ["scene_01", "scene_02", "bgm"])

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
        self.assertIn('@router.get("/material-library/health")', patch_text)
        self.assertIn('@router.post("/material-library/probe")', patch_text)
        self.assertNotIn("AI fallback", patch_text)


if __name__ == "__main__":
    unittest.main()
