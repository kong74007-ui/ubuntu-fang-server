# -*- coding: utf-8 -*-
import importlib
import json
import sqlite3
import sys
import tempfile
import unittest
import zipfile
from contextlib import closing
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SERVER = str(ROOT / "server")
if SERVER not in sys.path:
    sys.path.insert(0, SERVER)

ai_edit = importlib.import_module("content_domains.ai_edit")
core = importlib.import_module("content_domains.core")
feature_flags = importlib.import_module("content_domains.feature_flags")
points = importlib.import_module("content_domains.points")
registry = importlib.import_module("content_domains.registry")

PAGE = (ROOT / "site/workbench/ai-edit.html").read_text(encoding="utf-8")
SHELL = (ROOT / "site/workbench/cloud-shell.js").read_text(encoding="utf-8")
CORE = (ROOT / "server/content_domains/core.py").read_text(encoding="utf-8")


class AiEditWiringTests(unittest.TestCase):
    def test_capability_is_fully_registered(self):
        self.assertIn("ai_edit", registry.HANDLERS)
        self.assertTrue(callable(registry.HANDLERS["ai_edit"]))
        self.assertEqual(points.cost_of("ai_edit", {}), 30)
        self.assertIn("ai_edit", feature_flags.CATALOG_MAP)

    def test_slow_queue_and_reaper_cover_cloud_render(self):
        self.assertIs(core._pick_job_queue("ai_edit"), core._job_queue)
        self.assertGreaterEqual(core.KIND_GRACE["ai_edit"], 1800)
        self.assertIn('"ai_edit"', CORE.split('payload["_username"] = username')[0].rsplit("if kind in", 1)[-1])

    def test_video_asset_lifecycle_includes_ai_edit(self):
        self.assertIn('{"video", "tryon", "xiaole_video", "cinematic", "ai_edit"}', CORE)
        self.assertIn('body = ai_edit_domain.validate_ai_edit_payload(body, user["username"])', CORE)


class AiEditValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "assets.db"
        self.video_path = Path(self.temp.name) / "source.mp4"
        self.video_path.write_bytes(b"video")
        with closing(sqlite3.connect(str(self.db_path))) as db:
            db.execute(
                """CREATE TABLE video_assets(
                       id INTEGER PRIMARY KEY, job_id INTEGER, username TEXT, mode TEXT,
                       video_file TEXT, video_url TEXT, text TEXT, resolution TEXT,
                       ratio TEXT, status TEXT, created_at INTEGER)"""
            )
            db.execute(
                "INSERT INTO video_assets VALUES(1,7,'fang','text','video/source.mp4','/v.mp4','测试口播','1080p','9:16','done',1)"
            )
            db.execute(
                "INSERT INTO video_assets VALUES(2,8,'other','text','video/other.mp4','/o.mp4','别人素材','1080p','9:16','done',1)"
            )
            db.execute(
                "INSERT INTO video_assets VALUES(3,9,'fang','ai_edit','video/edit.mp4','/e.mp4','剪辑成片','1080p','9:16','done',1)"
            )
            db.commit()

    def tearDown(self):
        self.temp.cleanup()

    def _db(self):
        db = sqlite3.connect(str(self.db_path))
        db.row_factory = sqlite3.Row
        return db

    def test_accepts_only_owned_completed_digital_ip_asset(self):
        with patch.object(ai_edit, "adb", self._db), patch.object(ai_edit, "_resolve_out_file", return_value=self.video_path):
            body = ai_edit.validate_ai_edit_payload({"source_video_asset_id": 1, "style_id": "product_seeding"}, "fang")
        self.assertEqual(body["source_video_asset_id"], 1)
        self.assertEqual(body["mode"], "ai_edit")
        self.assertEqual(body["ratio"], "9:16")
        self.assertEqual(body["style_id"], "product_seeding")

    def test_rejects_unknown_style(self):
        with patch.object(ai_edit, "adb", self._db), patch.object(ai_edit, "_resolve_out_file", return_value=self.video_path):
            with self.assertRaisesRegex(ValueError, "剪辑风格"):
                ai_edit.validate_ai_edit_payload({"source_video_asset_id": 1, "style_id": "unknown"}, "fang")

    def test_rejects_other_users_and_previous_edit_outputs(self):
        with patch.object(ai_edit, "adb", self._db), patch.object(ai_edit, "_resolve_out_file", return_value=self.video_path):
            with self.assertRaisesRegex(ValueError, "不属于"):
                ai_edit.validate_ai_edit_payload({"source_video_asset_id": 2}, "fang")
            with self.assertRaisesRegex(ValueError, "只支持数字化 IP"):
                ai_edit.validate_ai_edit_payload({"source_video_asset_id": 3}, "fang")

    def test_rejects_unavailable_local_source(self):
        with patch.object(ai_edit, "adb", self._db), patch.object(ai_edit, "_resolve_out_file", return_value=None):
            with self.assertRaisesRegex(ValueError, "原文件"):
                ai_edit.validate_ai_edit_payload({"source_video_asset_id": 1}, "fang")


class HyperframesProjectTests(unittest.TestCase):
    def test_composition_contract_keeps_media_at_root(self):
        page = ai_edit.build_hyperframes_html("第一句。第二句。第三句。", 12, 1080, 1920)
        self.assertIn('data-composition-id="ai-edit-main"', page)
        self.assertIn("data-no-timeline", page)
        self.assertIn('data-width="1080"', page)
        self.assertIn('data-height="1920"', page)
        self.assertIn('data-duration="12.000"', page)
        self.assertIn('<video id="source-video" src="input-video.mp4"', page)
        self.assertIn('<audio id="source-audio" src="input-video.mp4"', page)
        self.assertIn("muted playsinline", page)
        self.assertNotIn("http://", page)
        self.assertNotIn("https://", page)
        self.assertIn('class="clip topic', page)

    def test_user_text_is_escaped(self):
        page = ai_edit.build_hyperframes_html('<script>alert("x")</script>。', 8, 1080, 1920)
        self.assertNotIn('<script>alert("x")</script>', page)
        self.assertIn("&lt;script&gt;", page)

    def test_styles_change_layout_and_captions_are_timed(self):
        science = ai_edit.build_hyperframes_html("这是科普内容。第二句字幕。", 8, 1080, 1920, "content_first")
        premium = ai_edit.build_hyperframes_html("这是品牌内容。第二句字幕。", 8, 1080, 1920, "brand_premium")
        self.assertIn('class="style-content_first"', science)
        self.assertIn('class="style-brand_premium"', premium)
        self.assertIn('id="caption-1" class="clip caption"', science)
        self.assertIn("信息科普 · 竖屏原片", science)
        self.assertNotEqual(science, premium)

    def test_project_zip_contains_entry_media_and_frozen_font(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            source, font, target = temp / "input.mp4", temp / "font.woff2", temp / "project.zip"
            source.write_bytes(b"mp4-bytes")
            font.write_bytes(b"font-bytes")
            ai_edit._write_project_zip(target, "<html></html>", source, font)
            with zipfile.ZipFile(str(target)) as archive:
                self.assertEqual(set(archive.namelist()), {"index.html", "input-video.mp4", "assets/noto-sans-sc-700.woff2"})
                self.assertEqual(archive.read("input-video.mp4"), b"mp4-bytes")


class HyperframesApiTests(unittest.TestCase):
    def test_render_uses_asset_id_and_portrait_1080p(self):
        captured = {}

        def fake(method, path, body, key, what, tries=4):
            captured.update(method=method, path=path, body=body, key=key)
            return {"data": {"render_id": "render-1"}}

        with patch.object(ai_edit, "_idempotent_json", side_effect=fake):
            render_id = ai_edit._submit_render("asset-1", 42, "标题")
        self.assertEqual(render_id, "render-1")
        self.assertEqual(captured["path"], "/hyperframes/renders")
        self.assertEqual(captured["body"]["project"], {"type": "asset_id", "asset_id": "asset-1"})
        self.assertEqual(captured["body"]["aspect_ratio"], "9:16")
        self.assertEqual(captured["body"]["resolution"], "1080p")
        self.assertEqual(captured["body"]["composition"], "index.html")

    def test_poll_returns_completed_render(self):
        responses = [
            {"data": {"status": "queued"}},
            {"data": {"status": "completed", "video_url": "https://example.invalid/out.mp4"}},
        ]
        with patch.object(ai_edit.video, "_heygen_request_json", side_effect=responses), \
             patch.object(ai_edit.time, "sleep"):
            out = ai_edit._wait_render("render-1")
        self.assertEqual(out["status"], "completed")


class AiEditUiTests(unittest.TestCase):
    def test_sidebar_has_new_tab(self):
        self.assertIn("{k:'ai-edit',l:'一键剪辑'", SHELL)
        self.assertIn('data-active="ai-edit"', PAGE)

    def test_page_filters_owned_digital_ip_assets_and_submits_once(self):
        self.assertIn("/api/gen/video/assets?limit=120", PAGE)
        self.assertIn("item.mode==='text'||item.mode==='audio'", PAGE)
        self.assertIn("/api/gen/ai_edit", PAGE)
        self.assertIn("source_video_asset_id:selected.id", PAGE)
        self.assertIn("'Idempotency-Key':key", PAGE)

    def test_asset_cards_load_the_protected_source_image_as_cover(self):
        self.assertIn("item.image_file?'/api/gen/file/'+item.image_file", PAGE)
        self.assertIn('data-cover-id="', PAGE)
        self.assertIn("loadAssetCovers();", PAGE)
        self.assertIn("coverCache[id]", PAGE)

    def test_price_and_fixed_output_are_visible(self):
        self.assertIn("一键剪辑 · 30 点", PAGE)
        self.assertIn("单条固定 <b>30</b> 点", PAGE)
        self.assertIn("1080 × 1920", PAGE)
        self.assertIn("原声保留", PAGE)
        self.assertIn("AI 自动推荐", PAGE)
        self.assertIn("产品种草", PAGE)
        self.assertIn("style_id:styleId", PAGE)


if __name__ == "__main__":
    unittest.main()
