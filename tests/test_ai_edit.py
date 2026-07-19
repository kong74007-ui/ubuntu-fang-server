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
ai_edit_store = importlib.import_module("content_domains.ai_edit_store")
core = importlib.import_module("content_domains.core")
feature_flags = importlib.import_module("content_domains.feature_flags")
points = importlib.import_module("content_domains.points")
registry = importlib.import_module("content_domains.registry")

PAGE = (ROOT / "site/workbench/ai-edit.html").read_text(encoding="utf-8")
SHELL = (ROOT / "site/workbench/cloud-shell.js").read_text(encoding="utf-8")
CORE = (ROOT / "server/content_domains/core.py").read_text(encoding="utf-8")
API = (ROOT / "server/content_domains/ai_edit_api.py").read_text(encoding="utf-8")
FANG_NGINX = (ROOT / "deploy/nginx-fang-locations.conf").read_text(encoding="utf-8")


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

    def test_versioned_routes_are_delegated_out_of_core(self):
        self.assertIn("ai_edit_api.handle_post", CORE)
        self.assertIn("ai_edit_api.handle_get", CORE)
        self.assertIn('/api/v1/edit-assets', API)
        self.assertIn('/api/v1/edit-jobs', API)
        self.assertIn('billing_state', API)
        self.assertIn('payload["_retry_from_job_id"]', API)
        self.assertIn('return handler._send(202, {"job_id": new_id', API)
        self.assertIn('self._send(ai_edit_api.submission_status(self, kind), response)', CORE)

    def test_test_domain_proxies_versioned_routes_to_content_api(self):
        block = FANG_NGINX.split("location ^~ /api/v1/", 1)[1].split("}", 1)[0]
        self.assertIn("proxy_pass http://127.0.0.1:8096", block)
        self.assertIn("proxy_buffering off", block)
        self.assertIn("proxy_request_buffering off", block)


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
        self.assertIn('window.__timelines["ai-edit-main"]=tl', page)
        self.assertIn('gsap.timeline({paused:true})', page)
        self.assertIn('data-width="1080"', page)
        self.assertIn('data-height="1920"', page)
        self.assertIn('data-duration="12.000"', page)
        self.assertIn('<video id="source-scene-1" class="clip source source-full" src="input-video.mp4"', page)
        self.assertIn('<audio id="source-audio" src="input-video.mp4"', page)
        self.assertIn("muted playsinline", page)
        self.assertNotIn("http://", page)
        self.assertNotIn("https://", page)
        self.assertIn('class="clip scene-shell', page)

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

    def test_styles_change_scene_pacing_and_motion_profile(self):
        transcript = {"words": [{"start": 0, "end": 30, "text": "口播"}]}
        fast = ai_edit._beat_windows(transcript, 30, style_id="promo_fast")
        premium = ai_edit._beat_windows(transcript, 30, style_id="brand_premium")
        self.assertGreater(len(fast), len(premium))
        fast_page = ai_edit.build_hyperframes_html("限时优惠", 6, 1080, 1920, "promo_fast")
        premium_page = ai_edit.build_hyperframes_html("品牌故事", 6, 1080, 1920, "brand_premium")
        self.assertIn("duration:0.18", fast_page)
        self.assertIn("duration:0.52", premium_page)

    def test_project_zip_contains_entry_media_and_frozen_font(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            source, font, target = temp / "input.mp4", temp / "font.woff2", temp / "project.zip"
            source.write_bytes(b"mp4-bytes")
            font.write_bytes(b"font-bytes")
            ai_edit._write_project_zip(target, "<html></html>", source, font)
            with zipfile.ZipFile(str(target)) as archive:
                self.assertEqual(set(archive.namelist()), {
                    "index.html", "input-video.mp4", "assets/noto-sans-sc-700.woff2",
                    "assets/gsap.min.js",
                })
                self.assertEqual(archive.read("input-video.mp4"), b"mp4-bytes")

    def test_timeline_requires_full_non_overlapping_coverage(self):
        valid = {"duration": 4, "scenes": [
            {"start": 0, "end": 2, "layout": "talking_full"},
            {"start": 2, "end": 4, "layout": "split_product"},
        ]}
        self.assertEqual(ai_edit._validate_timeline(valid)["scenes"][1]["duration"], 2)
        invalid = {"duration": 4, "scenes": [
            {"start": 0, "end": 1.8, "layout": "talking_full"},
            {"start": 2, "end": 4, "layout": "talking_full"},
        ]}
        with self.assertRaisesRegex(RuntimeError, "空隙或重叠"):
            ai_edit._validate_timeline(invalid)

    def test_must_use_material_is_forced_into_director_timeline(self):
        windows = [{"id": "scene-01", "start": 0, "end": 4, "text": "开场"}]
        materials = [{"id": 7, "usage": "must_use", "kind": "image",
                      "analysis": {"safe": True, "ocr": ["产品实拍"]}}]
        with patch.object(ai_edit, "_qwen_json", return_value={"assignments": [
            {"id": "scene-01", "layout": "talking_full", "material_id": None,
             "headline": "开场"}
        ]}), patch.object(ai_edit, "_ensure_not_cancelled"):
            scenes = ai_edit._direct_timeline(windows, materials, {}, "auto", 1)
        self.assertEqual(scenes[0]["material_id"], 7)
        self.assertEqual(scenes[0]["layout"], "split_product")

    def test_talking_full_keeps_moving_source_instead_of_still_material(self):
        windows = [{"id": "scene-01", "start": 0, "end": 4, "text": "开场"}]
        materials = [{"id": -1, "usage": "auto", "kind": "image", "source": "source_frame"}]
        with patch.object(ai_edit, "_qwen_json", return_value={"assignments": [
            {"id": "scene-01", "layout": "talking_full", "material_id": -1,
             "motion": "none", "transition": "cut", "headline": "开场"}
        ]}), patch.object(ai_edit, "_ensure_not_cancelled"):
            scenes = ai_edit._direct_timeline(windows, materials, {}, "auto", 1)
        self.assertIsNone(scenes[0]["material_id"])
        self.assertEqual(scenes[0]["asset_source"], "source_video")
        self.assertEqual(scenes[0]["motion"], "none")

    def test_legacy_talking_full_timeline_does_not_overlay_static_material(self):
        timeline = {
            "duration": 4,
            "transcript": {"words": []},
            "scenes": [{"id": "scene-01", "start": 0, "end": 4,
                        "layout": "talking_full", "material_id": -1,
                        "motion": "none", "transition": "cut"}],
        }
        page = ai_edit.build_hyperframes_html(
            "开场", 4, 1080, 1920, timeline=timeline,
            material_files={-1: {"name": "assets/source-frame.jpg", "kind": "image"}},
        )
        self.assertIn('id="source-scene-1"', page)
        self.assertNotIn('id="material-1"', page)

    def test_director_never_silently_drops_excess_must_use_materials(self):
        windows = [{"id": "scene-01", "start": 0, "end": 4, "text": "开场"}]
        materials = [
            {"id": 7, "usage": "must_use", "kind": "image", "analysis": {"safe": True}},
            {"id": 8, "usage": "must_use", "kind": "image", "analysis": {"safe": True}},
        ]
        with patch.object(ai_edit, "_qwen_json", return_value={"assignments": []}), \
             patch.object(ai_edit, "_ensure_not_cancelled"):
            with self.assertRaisesRegex(RuntimeError, "必用素材数量"):
                ai_edit._direct_timeline(windows, materials, {}, "auto", 1)

    def test_material_priority_is_uploaded_then_source_frame_then_reused_then_generated(self):
        transcript = {"segments": [{"text": "产品介绍"}]}
        materials = [
            {"id": 4, "source": "ai_generated", "analysis": {"summary": "产品", "quality": 90}},
            {"id": 3, "source": "reused", "analysis": {"summary": "产品", "quality": 90}},
            {"id": 2, "source": "source_frame", "analysis": {"summary": "产品", "quality": 90}},
            {"id": 1, "source": "uploaded", "analysis": {"summary": "产品", "quality": 90}},
        ]
        ranked = ai_edit._rank_materials(materials, transcript, {})
        self.assertEqual([item["id"] for item in ranked], [1, 2, 3, 4])

    def test_cached_material_analysis_is_reused(self):
        materials = [{"id": 7, "usage": "auto", "kind": "image", "filename": "a.jpg",
                      "analysis_json": {"summary": "缓存分析", "safe": True, "quality": 80}}]
        with tempfile.TemporaryDirectory() as temp, \
             patch.object(ai_edit, "_preview_image", return_value=Path(temp) / "preview.jpg"), \
             patch.object(ai_edit, "_qwen_json") as qwen, \
             patch.object(ai_edit.store, "set_material_analysis"):
            result = ai_edit._analyze_materials(materials, Path(temp), 9)
        self.assertEqual(result[0]["analysis"]["summary"], "缓存分析")
        qwen.assert_not_called()

    def test_retry_reuses_generated_assets_that_still_exist(self):
        timeline = {"materials": [{
            "source": "ai_generated", "generated_file": "image/generated.png",
            "generation_model": "seedream", "generation_prompt": "生活场景",
            "analysis": {"summary": "已生成场景", "safe": True, "quality": 80},
        }]}
        with tempfile.TemporaryDirectory() as temp:
            image = Path(temp) / "generated.png"
            image.write_bytes(b"png")
            with patch.object(ai_edit, "_resolve_out_file", return_value=image), \
                 patch.object(ai_edit.store, "_probe", return_value=(1080, 1920, 0)):
                result = ai_edit._reusable_generated_materials(timeline, 1)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["source"], "reused")
        self.assertEqual(result[0]["generation_prompt"], "生活场景")

    def test_technical_qc_enforces_delivery_contract(self):
        valid = {"width": 1080, "height": 1920, "video_codec": "h264", "pix_fmt": "yuv420p",
                 "audio_codec": "aac", "sample_rate": 48000, "fps": 30.0, "r_fps": 30.0,
                 "duration": 6.0, "video_duration": 6.0, "audio_duration": 6.0,
                 "video_start": 0.0, "audio_start": 0.0, "format_start": 0.0}
        with tempfile.TemporaryDirectory() as temp:
            media = Path(temp) / "output.mp4"
            media.write_bytes(b"ftyp....moov....mdat")
            with patch.object(ai_edit, "_stream_info", return_value=valid):
                self.assertTrue(ai_edit._technical_qc(media, 6.0)["passed"])
            invalid = dict(valid, duration=6.2)
            with patch.object(ai_edit, "_stream_info", return_value=invalid):
                with self.assertRaisesRegex(RuntimeError, "技术质检"):
                    ai_edit._technical_qc(media, 6.0)


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
        self.assertIn("/api/v1/edit-jobs", PAGE)
        self.assertIn("source_video_asset_id:selectedSource.id", PAGE)
        self.assertIn("'Idempotency-Key':key", PAGE)

    def test_asset_cards_load_the_protected_source_image_as_cover(self):
        self.assertIn("item.image_file?'/api/gen/file/'+item.image_file", PAGE)
        self.assertIn('data-cover="', PAGE)
        self.assertIn("blobUrl(cover)", PAGE)
        self.assertIn("Authorization:'Bearer '+token", PAGE)

    def test_completed_result_binds_generated_cover_as_video_poster(self):
        self.assertIn("job.result.image_url", PAGE)
        self.assertIn("showResult(resultUrl,false,resultCoverUrl)", PAGE)
        self.assertIn("video.poster=src", PAGE)
        self.assertIn("showResult(resultUrl,true,resultCoverUrl)", PAGE)

    def test_latest_completed_edit_is_restored_after_page_reload(self):
        self.assertIn("item.mode==='ai_edit'", PAGE)
        self.assertIn("restoreLatestResult(items)", PAGE)
        self.assertIn("resultCoverUrl=sourceCover(latest)", PAGE)
        self.assertIn("loadResultDetails(latest.job_id)", PAGE)

    def test_price_and_fixed_output_are_visible(self):
        self.assertIn("生成一键剪辑 · 30 点", PAGE)
        self.assertIn("功能单价：<b>30 点 / 条</b>", PAGE)
        self.assertIn("1080 × 1920", PAGE)
        self.assertIn("原声保留", PAGE)
        self.assertIn("AI 自动推荐", PAGE)
        self.assertIn("产品种草", PAGE)
        self.assertIn("style_id:styleId", PAGE)

    def test_material_library_and_versioned_job_controls_are_visible(self):
        self.assertIn('id="materialInput"', PAGE)
        self.assertIn("/api/v1/edit-assets", PAGE)
        self.assertIn("data-usage=", PAGE)
        self.assertIn("product_facts:productFacts()", PAGE)
        self.assertNotIn('id="cancelBtn"', PAGE)
        self.assertNotIn("function cancelJob", PAGE)
        self.assertNotIn('id="downloadTimeline"', PAGE)
        self.assertNotIn("timelineUrl", PAGE)
        self.assertIn("/retry", PAGE)
        self.assertIn("HELD", PAGE)
        self.assertIn("CAPTURED", PAGE)
        self.assertIn("RELEASED", PAGE)
        self.assertIn('id="resultMeta"', PAGE)
        self.assertIn("loadResultDetails(finishedJob)", PAGE)
        self.assertIn("material_breakdown", PAGE)


class AiEditStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_db = ai_edit_store.EDIT_DB
        self.old_material_dir = ai_edit_store.MATERIAL_DIR
        ai_edit_store.EDIT_DB = Path(self.temp.name) / "ai-edit.db"
        ai_edit_store.MATERIAL_DIR = Path(self.temp.name) / "materials"
        ai_edit_store.init_db()

    def tearDown(self):
        ai_edit_store.EDIT_DB = self.old_db
        ai_edit_store.MATERIAL_DIR = self.old_material_dir
        self.temp.cleanup()

    def test_billing_hold_capture_and_release_are_idempotent(self):
        payload = {"source_video_asset_id": 4, "style_id": "auto",
                   "materials": [], "product_facts": {"name": "测试产品"}}
        created = ai_edit_store.create_job(101, "fang", payload, 30)
        self.assertEqual(created["billing"]["state"], "HELD")
        self.assertTrue(ai_edit_store.capture_hold(101))
        self.assertFalse(ai_edit_store.capture_hold(101))
        self.assertFalse(ai_edit_store.release_hold(101))
        self.assertEqual(ai_edit_store.public_job(101, "fang")["billing"]["state"], "CAPTURED")

        ai_edit_store.create_job(102, "fang", payload, 30)
        self.assertTrue(ai_edit_store.release_hold(102))
        self.assertFalse(ai_edit_store.release_hold(102))
        self.assertEqual(ai_edit_store.public_job(102, "fang")["billing"]["state"], "RELEASED")

    def test_job_status_exposes_timeline_only_when_requested(self):
        payload = {"source_video_asset_id": 5, "style_id": "auto", "materials": []}
        ai_edit_store.create_job(103, "fang", payload, 30)
        ai_edit_store.set_timeline(103, {"version": "2.0", "scenes": [{"id": "scene-01"}]})
        self.assertNotIn("timeline", ai_edit_store.public_job(103, "fang"))
        self.assertEqual(ai_edit_store.public_job(103, "fang", include_timeline=True)["timeline"]["version"], "2.0")

    def test_stage_progress_exposes_eta_and_stage_timings(self):
        payload = {"source_video_asset_id": 6, "style_id": "auto", "materials": []}
        ai_edit_store.create_job(104, "fang", payload, 30)
        ai_edit_store.update_stage(104, "transcribing", 30, "识别语音")
        job = ai_edit_store.public_job(104, "fang")
        self.assertEqual(job["stage"], "transcribing")
        self.assertGreaterEqual(job["eta_seconds"], 0)
        self.assertIn("queued", job["stage_timings"])
        self.assertIn("transcribing", job["stage_timings"])
        ai_edit_store.set_usage(104, {"asr": {"audio_seconds": 12}}, {"billable_points": 30})
        job = ai_edit_store.public_job(104, "fang")
        self.assertEqual(job["provider_usage"]["asr"]["audio_seconds"], 12)
        self.assertEqual(job["cost_breakdown"]["billable_points"], 30)


if __name__ == "__main__":
    unittest.main()
