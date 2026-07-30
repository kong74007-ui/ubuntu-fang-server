import os
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

server_dir = str(Path(__file__).resolve().parents[1] / "server")
if server_dir not in sys.path:
    sys.path.insert(0, server_dir)

from server.content_domains import ai_edit_v2_api as api
from server.content_domains import ai_edit_v2_store as store
from server.content_domains import points


MB = 1024 * 1024


def valid_api_draft():
    return {
        "creation_mode": "natural_brief",
        "brief": "保留原任务输入",
        "language": "zh-CN",
        "aspect_ratio": "16:9",
        "target_duration_ms": 30_000,
        "input_mode": "external_video",
        "main_input": {
            "asset_id": "1",
            "kind": "video",
            "size_bytes": MB,
            "duration_ms": 30_000,
        },
        "required_materials": [],
        "reference_materials": [],
    }


class FakePoints:
    def __init__(self):
        self.balance = 500
        self.transactions = {}

    def deduct_points(self, username, amount, reason="", transaction_key=None):
        if transaction_key not in self.transactions:
            self.balance -= amount
            self.transactions[transaction_key] = self.balance
        return self.transactions[transaction_key]

    def refund_points(self, username, amount, reason="", transaction_key=None):
        if transaction_key not in self.transactions:
            self.balance += amount
            self.transactions[transaction_key] = self.balance
        return self.transactions[transaction_key]


class PendingPoints(FakePoints):
    def deduct_points(self, username, amount, reason="", transaction_key=None):
        raise points.AuthPointsError(502, "auth response unavailable")


class RejectingPoints(FakePoints):
    def __init__(self, status, detail):
        super().__init__()
        self.status = status
        self.detail = detail

    def deduct_points(self, username, amount, reason="", transaction_key=None):
        raise points.AuthPointsError(self.status, self.detail)


class FakeHandler:
    def __init__(self, body=None, token="token", headers=None):
        self.body = body or {}
        self.token = token
        self.headers = headers or {}
        self.responses = []

    def _send(self, status, payload):
        self.responses.append((status, payload))
        return payload

    def _json_body(self):
        return self.body

    def _token(self):
        return self.token


class FakeCos:
    def __init__(self):
        self.puts = []
        self.head = {
            "content_length": 12 * MB,
            "content_type": "video/mp4",
            "etag": "verified-etag",
        }

    def presign_put(self, object_key, content_type, expires=900):
        return f"https://upload.example/{object_key}?signature=private"

    def presign_get(self, object_key, expires=300):
        return f"https://download.example/{object_key}?signature=private"

    def head_object(self, object_key):
        return dict(self.head)

    def put_file(self, path, object_key, content_type, private=True):
        self.puts.append((path, object_key, content_type, private))


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "ai_edit_v2.db")
        self.env = patch.dict(
            os.environ,
            {
                "AI_EDIT_V2_DB": self.db_path,
                "AI_EDIT_V2_ENABLED": "1",
                "AI_EDIT_V2_WEBHOOK_SECRET": "task9-test-secret",
            },
        )
        self.env.start()
        self.ready_patch = patch.object(
            api.feature.runtime, "production_dependencies",
            return_value={"readiness_errors": lambda: []},
        )
        self.ready_patch.start()
        self.components_patch = patch.object(api.feature, "runtime_ready", return_value=True)
        self.components_patch.start()
        store.init_db(self.db_path)
        with closing(store.open_store(self.db_path)) as conn:
            conn.execute(
                """INSERT INTO edit_v2_materials(
                       id,owner,kind,purpose,source,cos_key,filename,mime_type,
                       size_bytes,duration_ms,status,created_at,updated_at
                   ) VALUES(1,'alice','video','primary','user_upload','private/main.mp4',
                            'main.mp4','video/mp4',?,?, 'ready',1,1)""",
                (MB, 30_000),
            )
        self.fake_cos = FakeCos()
        self.cos_patch = patch.object(api, "cos", self.fake_cos)
        self.cos_patch.start()
        self.probe_patch = patch.object(
            api.media,
            "probe_media",
            return_value={"duration_ms": 42_000, "width": 1920, "height": 1080},
        )
        self.probe_patch.start()
        self.uuid_patch = patch.object(
            api,
            "_new_uuid",
            side_effect=[
                "123e4567-e89b-42d3-a456-426614174000",
                "123e4567-e89b-42d3-a456-426614174001",
                "123e4567-e89b-42d3-a456-426614174002",
            ],
        )
        self.uuid_patch.start()
        self.points_patch = patch.object(api, "_points_client", FakePoints())
        self.points_patch.start()
        self.user = {"username": "alice"}

    def tearDown(self):
        self.probe_patch.stop()
        self.components_patch.stop()
        self.ready_patch.stop()
        self.points_patch.stop()
        self.uuid_patch.stop()
        self.cos_patch.stop()
        self.env.stop()
        self.temp_dir.cleanup()

    def test_canonical_draft_preserves_platform_input_mode_and_original_text(self):
        with closing(store.open_store(self.db_path)) as conn:
            conn.execute(
                "UPDATE edit_v2_materials SET source='platform_video',original_text=? WHERE id=1",
                ("平台持久原文：品牌价格是29元",),
            )
        draft = valid_api_draft()
        draft.update({
            "creation_mode": "platform_template",
            "input_mode": "platform_video",
            "original_text": "客户端伪造原文",
            "template_id": "business_diagnostic",
            "template_version": "1.0",
        })
        canonical, _bindings = api.canonicalize_job_draft("alice", draft)
        self.assertEqual(canonical["input_mode"], "platform_video")
        self.assertEqual(canonical["original_text"], "平台持久原文：品牌价格是29元")

    def test_platform_asset_is_owner_scoped_and_imports_authoritative_text_without_client_truth(self):
        asset_db = os.path.join(self.temp_dir.name, "platform-assets.db")
        jobs_db = os.path.join(self.temp_dir.name, "content-jobs.db")
        source = os.path.join(self.temp_dir.name, "platform-video.mp4")
        Path(source).write_bytes(b"authoritative-platform-video")
        with closing(sqlite3.connect(asset_db)) as conn:
            conn.execute("""CREATE TABLE video_assets(
                id INTEGER PRIMARY KEY,job_id TEXT,username TEXT,mode TEXT,
                image_file TEXT,video_file TEXT,provider_video_id TEXT,text TEXT,ratio TEXT,status TEXT,
                created_at INTEGER,updated_at INTEGER)""")
            conn.executemany(
                "INSERT INTO video_assets VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (31, "source-job-31", "alice", "text", "image/cover 31.jpg",
                     "platform-video.mp4", "provider-31",
                     "authoritative script 29", "16:9", "done", 1, 2),
                    (32, "source-job-32", "bob", "text", "image/cover-32.jpg",
                     "platform-video.mp4", "provider-32",
                     "other owner secret", "16:9", "done", 1, 2),
                ],
            )
            conn.commit()
        with closing(sqlite3.connect(jobs_db)) as conn:
            conn.execute("""CREATE TABLE jobs(
                id TEXT PRIMARY KEY,username TEXT,kind TEXT,status TEXT,payload TEXT,
                result TEXT,deleted INTEGER)""")
            conn.executemany(
                "INSERT INTO jobs VALUES(?,?,?,?,?,?,?)",
                [
                    ("source-job-31", "alice", "video", "done",
                     json.dumps({"mode": "text", "text": "script", "voice": "v", "image_data": "img"}),
                     json.dumps({"type": "video", "mode": "text", "status": "done"}), 0),
                    ("source-job-32", "bob", "video", "done",
                     json.dumps({"mode": "text", "text": "secret", "voice": "v", "image_data": "img"}),
                     json.dumps({"type": "video", "mode": "text", "status": "done"}), 0),
                ],
            )
            conn.commit()

        with patch.dict(os.environ, {
            "AI_EDIT_V2_ASSET_DB": asset_db,
            "AI_EDIT_V2_JOB_DB": jobs_db,
            "AI_EDIT_V2_PLATFORM_OUT": self.temp_dir.name,
        }):
            status, listed = self._dispatch("GET", "/api/v2/edit/platform-assets")
            self.assertEqual(status, 200)
            self.assertEqual(listed["items"], [{
                "id": 31, "reference_id": "31", "filename": "platform-video.mp4",
                "summary": "authoritative script 29", "ratio": "16:9",
                "status": "done", "created_at": 1, "asset_type": "digital_ip",
                "preview_url": "/api/gen/file/platform-video.mp4",
                "thumbnail_url": "/api/gen/file/image/cover%2031.jpg",
            }])
            for private_field in (
                "original_text", "username", "video_file", "image_file", "cos_key"
            ):
                self.assertNotIn(private_field, listed["items"][0])
            status, imported = self._dispatch(
                "POST", "/api/v2/edit/platform-assets/31/import",
                {"original_text": "forged client text", "source": "platform_video"},
            )
            self.assertEqual(status, 201)
            material_id = imported["material"]["id"]
            self.assertNotIn("original_text", imported["material"])
            status, _forbidden = self._dispatch(
                "POST", "/api/v2/edit/platform-assets/32/import", {}
            )
            self.assertEqual(status, 404)

        with closing(store.open_store(self.db_path)) as conn:
            row = conn.execute(
                "SELECT source,platform_asset_id,original_text FROM edit_v2_materials WHERE id=?",
                (material_id,),
            ).fetchone()
        self.assertEqual(tuple(row), ("platform_video", 31, "authoritative script 29"))

        status, upload = self._dispatch("POST", "/api/v2/edit/uploads", {
            "kind": "video", "purpose": "primary", "content_type": "video/mp4",
            "filename": "spoof.mp4", "source": "platform_video",
            "original_text": "forged platform script",
        })
        self.assertEqual(status, 201)
        with closing(store.open_store(self.db_path)) as conn:
            spoof = conn.execute(
                "SELECT source,original_text FROM edit_v2_materials WHERE upload_id=?",
                (upload["upload_id"],),
            ).fetchone()
        self.assertEqual(tuple(spoof), ("user_upload", None))

    def test_platform_assets_require_verified_digital_ip_job_provenance(self):
        asset_db = os.path.join(self.temp_dir.name, "platform-assets.db")
        jobs_db = os.path.join(self.temp_dir.name, "content-jobs.db")
        source = os.path.join(self.temp_dir.name, "platform-video.mp4")
        Path(source).write_bytes(b"verified-digital-ip-video")
        with closing(sqlite3.connect(asset_db)) as conn:
            conn.execute("""CREATE TABLE video_assets(
                id INTEGER PRIMARY KEY,job_id TEXT,username TEXT,mode TEXT,
                image_file TEXT,video_file TEXT,provider_video_id TEXT,text TEXT,ratio TEXT,status TEXT,
                created_at INTEGER,updated_at INTEGER)""")
            conn.executemany(
                "INSERT INTO video_assets VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (41, "job-41", "alice", "text", None, "platform-video.mp4", "provider-41",
                     "valid text", "9:16", "done", 1, 9),
                    (42, "job-42", "alice", "audio", None, "platform-video.mp4", "provider-42",
                     "valid audio", "9:16", "completed", 1, 8),
                    (43, "job-43", "alice", "grok", None, "platform-video.mp4", "provider-43",
                     "AI video", "9:16", "done", 1, 7),
                    (44, "job-44", "alice", "text", None, "platform-video.mp4", "provider-44",
                     "wrong task kind", "9:16", "done", 1, 6),
                    (45, "job-45", "alice", "text", None, "platform-video.mp4", "provider-45",
                     "payload mismatch", "9:16", "done", 1, 5),
                    (46, "job-46", "alice", "text", None, "platform-video.mp4", "provider-46",
                     "unfinished", "9:16", "running", 1, 4),
                    (47, "job-47", "alice", "text", None, "", "provider-47",
                     "missing file", "9:16", "done", 1, 3),
                    (48, "job-48", "bob", "text", None, "platform-video.mp4", "provider-48",
                     "other owner", "9:16", "done", 1, 2),
                    (49, "missing-job", "alice", "text", None, "platform-video.mp4", "provider-49",
                     "orphan", "9:16", "done", 1, 1),
                    (50, "job-50", "alice", "text", None, "platform-video.mp4", "provider-50",
                     "failed job", "9:16", "done", 1, 0),
                    (51, "job-51", "alice", "text", None, "platform-video.mp4", "provider-51",
                     "deleted job", "9:16", "done", 1, 0),
                    (52, "job-52", "alice", "text", None, "platform-video.mp4", "provider-52",
                     "bad result", "9:16", "done", 1, 0),
                    (53, "job-53", "alice", "text", None, "platform-video.mp4", "",
                     "missing provider", "9:16", "done", 1, 0),
                ],
            )
            conn.commit()
        with closing(sqlite3.connect(jobs_db)) as conn:
            conn.execute("""CREATE TABLE jobs(
                id TEXT PRIMARY KEY,username TEXT,kind TEXT,status TEXT,payload TEXT,
                result TEXT,deleted INTEGER)""")
            def job(job_id, owner, kind, mode, status="done", deleted=0, result_mode=None):
                payload = {"mode": mode, "image_data": "img"}
                payload.update({"text": "script", "voice": "v"} if mode == "text" else {"audio_data": "audio"})
                result = {"type": "video", "mode": result_mode or mode, "status": "done"}
                return (job_id, owner, kind, status, json.dumps(payload), json.dumps(result), deleted)
            conn.executemany(
                "INSERT INTO jobs VALUES(?,?,?,?,?,?,?)",
                [
                    job("job-41", "alice", "video", "text"),
                    job("job-42", "alice", "video", "audio"),
                    job("job-43", "alice", "xiaole_video", "grok"),
                    job("job-44", "alice", "ai_edit", "text"),
                    job("job-45", "alice", "video", "audio"),
                    job("job-46", "alice", "video", "text"),
                    job("job-47", "alice", "video", "text"),
                    job("job-48", "bob", "video", "text"),
                    job("job-50", "alice", "video", "text", status="error"),
                    job("job-51", "alice", "video", "text", deleted=1),
                    job("job-52", "alice", "video", "text", result_mode="audio"),
                    job("job-53", "alice", "video", "text"),
                ],
            )
            conn.commit()

        with patch.dict(os.environ, {
            "AI_EDIT_V2_ASSET_DB": asset_db,
            "AI_EDIT_V2_JOB_DB": jobs_db,
            "AI_EDIT_V2_PLATFORM_OUT": self.temp_dir.name,
        }):
            status, listed = self._dispatch("GET", "/api/v2/edit/platform-assets")
            self.assertEqual(status, 200)
            self.assertEqual([item["id"] for item in listed["items"]], [41, 42])
            self.assertEqual(
                [item.get("asset_type") for item in listed["items"]],
                ["digital_ip", "digital_ip"],
            )
            status, rejected = self._dispatch(
                "POST", "/api/v2/edit/platform-assets/44/import", {}
            )
            self.assertEqual(status, 409)
            self.assertEqual(rejected["detail"], "platform_asset_not_digital_ip")

    def _dispatch(self, method, path, body=None, user=None):
        handler = FakeHandler(body)
        handled = api.dispatch(
            handler,
            method,
            path,
            self.user if user is None else user,
        )
        self.assertTrue(handled)
        self.assertEqual(len(handler.responses), 1)
        return handler.responses[0]

    def _create_upload(self):
        status, payload = self._dispatch(
            "POST",
            "/api/v2/edit/uploads",
            {
                "kind": "video",
                "purpose": "required",
                "content_type": "video/mp4",
                "filename": "product-demo.mp4",
                "reference_mode": None,
            },
        )
        self.assertEqual(status, 201)
        return payload

    def _rendering_attempt(self, provider_task_id=None):
        job = store.create_job(
            "alice", {"draft": valid_api_draft()}, "quote", "webhook-request", 100,
            uuid_factory=lambda: "123e4567-e89b-42d3-a456-426614174095",
        )
        with closing(store.open_store(self.db_path)) as conn:
            conn.execute("UPDATE edit_v2_jobs SET status='rendering' WHERE id=?", (job["id"],))
            conn.commit()
        attempt_id = store.record_stage_attempt(
            job["id"], "rendering", 1, "submitted", 101,
            provider_task_id=provider_task_id,
            db_path=self.db_path,
        )
        with closing(store.open_store(self.db_path)) as conn:
            conn.execute(
                "UPDATE edit_v2_stage_attempts SET provider_reference=? WHERE id=?",
                ("stable-render-reference", attempt_id),
            )
            conn.commit()
        client = api.shotstack.ShotstackClient(
            job_id=job["id"], attempt_id=attempt_id, db_path=self.db_path
        )
        return job, attempt_id, client.callback_token("stable-render-reference")

    def test_prefix_requires_login(self):
        handler = FakeHandler()

        self.assertTrue(
            api.dispatch(handler, "GET", "/api/v2/edit/capabilities", None)
        )
        self.assertEqual(handler.responses, [(401, {"detail": "未登录"})])

    def test_non_v2_path_is_not_claimed(self):
        handler = FakeHandler()

        self.assertFalse(api.dispatch(handler, "GET", "/api/gen/assets", self.user))
        self.assertEqual(handler.responses, [])

    def test_disabled_feature_reports_capability_and_rejects_new_work(self):
        with patch.dict(os.environ, {"AI_EDIT_V2_ENABLED": "0"}):
            status, capability = self._dispatch(
                "GET", "/api/v2/edit/capabilities"
            )
            self.assertEqual(status, 200)
            self.assertFalse(capability["enabled"])
            self.assertFalse(capability["accepts_submissions"])

            status, payload = self._dispatch(
                "POST",
                "/api/v2/edit/quotes",
                {"draft": valid_api_draft()},
            )
            self.assertEqual(status, 503)
            self.assertEqual(payload["code"], "ai_edit_v2_disabled")

    def test_missing_stable_dependency_rejects_every_write_but_keeps_reads_and_webhook(self):
        missing = {
            "dashscope": True, "openai_image": True, "elevenlabs": True,
            "shotstack": True, "cos": False, "ffmpeg": True, "ffprobe": True,
        }
        job, attempt_id, token = self._rendering_attempt(provider_task_id="render-bound")
        with patch.object(api.feature.runtime, "production_dependencies", return_value={
            "readiness_errors": lambda: ["AI_EDIT_V2_REPAIR_PROVIDER"]
        }):
            status, capability = self._dispatch("GET", "/api/v2/edit/capabilities")
            self.assertEqual(status, 200)
            self.assertFalse(capability["accepts_submissions"])
            self.assertFalse(capability["stable_workflow"]["ready"])

            for path in (
                "/api/v2/edit/uploads",
                "/api/v2/edit/uploads/123e4567-e89b-42d3-a456-426614174000/complete",
                "/api/v2/edit/quote",
                "/api/v2/edit/jobs",
                f"/api/v2/edit/jobs/{job['id']}/retry",
            ):
                with self.subTest(path=path):
                    write_status, payload = self._dispatch("POST", path, {})
                    self.assertEqual(write_status, 503)
                    self.assertEqual(payload["code"], "ai_edit_v2_not_ready")

            self.assertEqual(self._dispatch("GET", "/api/v2/edit/templates")[0], 200)
            self.assertEqual(self._dispatch("GET", "/api/v2/edit/materials")[0], 200)
            self.assertEqual(
                self._dispatch("GET", f"/api/v2/edit/jobs/{job['id']}")[0], 200
            )
            body = {"id": "render-bound", "status": "done"}
            headers = {"Content-Length": str(len(json.dumps(body).encode("utf-8")))}
            handler = FakeHandler(body, headers=headers)
            route = (
                "/api/v2/edit/webhooks/shotstack"
                f"?attempt_id={attempt_id}&token={token}"
            )
            with patch.object(api.shotstack.ShotstackClient, "reconcile", return_value=object()):
                api.dispatch(handler, "POST", route, None)
            self.assertEqual(handler.responses[0][0], 202)

    def test_quote_rejects_cross_account_material_before_precharge(self):
        with closing(store.open_store(self.db_path)) as conn:
            conn.execute(
                """INSERT INTO edit_v2_materials(
                       id,owner,kind,purpose,source,cos_key,filename,mime_type,
                       size_bytes,duration_ms,status,created_at,updated_at
                   ) VALUES(99,'bob','video','primary','user_upload','private/bob.mp4',
                            'bob.mp4','video/mp4',?,?, 'ready',1,1)""",
                (MB, 30_000),
            )
        changed = valid_api_draft()
        changed["main_input"]["asset_id"] = "99"

        status, payload = self._dispatch(
            "POST", "/api/v2/edit/quotes", {"draft": changed}
        )

        self.assertEqual(status, 400)
        self.assertEqual(payload["detail"], "material_not_available")

    def test_upload_completion_uses_cos_head_and_is_idempotent(self):
        upload = self._create_upload()
        self.assertNotIn("cos_key", upload)
        self.assertIn("upload_url", upload)

        first_status, first = self._dispatch(
            "POST", f"/api/v2/edit/uploads/{upload['upload_id']}/complete"
        )
        second_status, second = self._dispatch(
            "POST", f"/api/v2/edit/uploads/{upload['upload_id']}/complete"
        )

        self.assertEqual(first_status, 200)
        self.assertEqual(second_status, 200)
        self.assertEqual(second["material"], first["material"])
        self.assertEqual(first["material"]["size_bytes"], 12 * MB)
        self.assertEqual(first["material"]["content_type"], "video/mp4")
        self.assertEqual(first["material"]["etag"], "verified-etag")
        self.assertEqual(first["material"]["duration_ms"], 42_000)
        self.assertEqual(first["material"]["width"], 1920)
        self.assertEqual(first["material"]["height"], 1080)
        with closing(store.open_store(self.db_path)) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM edit_v2_materials WHERE upload_id=?",
                (upload["upload_id"],),
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_material_owner_mismatch_returns_not_found(self):
        upload = self._create_upload()
        _, completed = self._dispatch(
            "POST", f"/api/v2/edit/uploads/{upload['upload_id']}/complete"
        )
        material_id = completed["material"]["id"]

        status, payload = self._dispatch(
            "GET",
            f"/api/v2/edit/materials/{material_id}",
            user={"username": "bob"},
        )

        self.assertEqual(status, 404)
        self.assertEqual(payload, {"detail": "素材不存在"})

    def test_complete_rejects_verified_type_mismatch(self):
        upload = self._create_upload()
        self.fake_cos.head["content_type"] = "image/png"

        status, payload = self._dispatch(
            "POST", f"/api/v2/edit/uploads/{upload['upload_id']}/complete"
        )

        self.assertEqual(status, 400)
        self.assertIn("类型", payload["detail"])

    def test_job_draft_rejects_material_count_and_total_capacity_before_quote(self):
        base = {
            "creation_mode": "natural_brief",
            "brief": "中文测试",
            "language": "zh-CN",
            "aspect_ratio": "16:9",
            "target_duration_ms": None,
            "main_input": {
                "asset_id": "main",
                "kind": "video",
                "size_bytes": 500 * MB,
                "duration_ms": 1_000,
            },
            "reference_materials": [],
        }
        too_many = dict(
            base,
            required_materials=[
                {"asset_id": str(i), "kind": "image", "size_bytes": MB}
                for i in range(11)
            ],
        )
        over_total = dict(
            base,
            required_materials=[
                {
                    "asset_id": str(i),
                    "kind": "video",
                    "size_bytes": 200 * MB,
                }
                for i in range(3)
            ],
        )

        for draft in (too_many, over_total):
            status, payload = self._dispatch(
                "POST", "/api/v2/edit/jobs", {"draft": draft, "quote_id": "quote"}
            )
            self.assertEqual(status, 400)
            self.assertIn("detail", payload)

    def test_capabilities_and_published_templates_are_provider_neutral(self):
        status, capabilities = self._dispatch(
            "GET", "/api/v2/edit/capabilities"
        )
        templates_status, templates = self._dispatch(
            "GET", "/api/v2/edit/templates"
        )

        self.assertEqual(status, 200)
        self.assertEqual(templates_status, 200)
        self.assertGreaterEqual(len(templates["items"]), 1)
        self.assertTrue(all(item["status"] == "published" for item in templates["items"]))
        self.assertTrue(all(item["version"] for item in templates["items"]))
        self.assertEqual(capabilities["version"], "2.0")
        self.assertNotIn("provider", str(capabilities).lower())

    def test_quote_route_creates_dynamic_quote(self):
        draft = valid_api_draft()
        draft.update(creation_mode="open_generation", aspect_ratio="9:16")

        status, payload = self._dispatch(
            "POST", "/api/v2/edit/quotes", {"draft": draft}
        )

        self.assertEqual(status, 201)
        self.assertGreater(payload["quote"]["max_points"], 0)

    def test_missing_input_or_unpublished_template_is_rejected_before_precharge(self):
        starting = api._points_client.balance
        missing = valid_api_draft(); missing.pop("input_mode")
        status, _payload = self._dispatch("POST", "/api/v2/edit/quote", {"draft": missing})
        self.assertEqual(status, 400)

        invalid = valid_api_draft()
        invalid.update(creation_mode="platform_template", template_id="business_diagnostic",
                       template_version="unpublished")
        status, _payload = self._dispatch(
            "POST", "/api/v2/edit/jobs",
            {"draft": invalid, "quote_id": "never", "idempotency_key": "never-charge"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(api._points_client.balance, starting)
        with closing(store.open_store(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM edit_v2_billing").fetchone()[0], 0)

    def test_stable_quote_route_exposes_range_and_maximum_hold(self):
        status, payload = self._dispatch(
            "POST", "/api/v2/edit/quote", {"draft": valid_api_draft()}
        )

        self.assertEqual(status, 201)
        self.assertGreater(payload["quote"]["minimum_points"], 0)
        self.assertGreaterEqual(
            payload["quote"]["maximum_points"], payload["quote"]["minimum_points"]
        )
        self.assertEqual(
            payload["quote"]["held_points"], payload["quote"]["maximum_points"]
        )

    def test_confirmed_quote_precharges_and_creates_job(self):
        draft = valid_api_draft()
        quote_status, quote_payload = self._dispatch(
            "POST", "/api/v2/edit/quotes", {"draft": draft}
        )

        status, payload = self._dispatch(
            "POST",
            "/api/v2/edit/jobs",
            {
                "draft": draft,
                "quote_id": quote_payload["quote"]["id"],
                "idempotency_key": "confirm-1",
            },
        )

        self.assertEqual(quote_status, 201)
        self.assertEqual(status, 201)
        self.assertEqual(payload["status"], "queued")
        self.assertEqual(payload["held_points"], quote_payload["quote"]["max_points"])
        with closing(store.open_store(self.db_path)) as conn:
            bound = conn.execute(
                "SELECT material_id,purpose FROM edit_v2_job_materials WHERE job_id=?",
                (payload["job_id"],),
            ).fetchall()
        self.assertEqual([(row["material_id"], row["purpose"]) for row in bound], [(1, "primary")])

    def test_job_creation_preserves_membership_required_status(self):
        draft = valid_api_draft()
        _, quote_payload = self._dispatch(
            "POST", "/api/v2/edit/quotes", {"draft": draft}
        )

        with patch.object(
            api, "_points_client", RejectingPoints(403, "membership required")
        ):
            status, payload = self._dispatch(
                "POST",
                "/api/v2/edit/jobs",
                {
                    "draft": draft,
                    "quote_id": quote_payload["quote"]["id"],
                    "idempotency_key": "membership-required",
                },
            )

        self.assertEqual(status, 403)
        self.assertEqual(payload, {"detail": "membership required"})

    def test_reusing_idempotency_key_with_another_quote_returns_409(self):
        draft = valid_api_draft()
        _, first_quote = self._dispatch("POST", "/api/v2/edit/quotes", {"draft": draft})
        first_status, first = self._dispatch(
            "POST", "/api/v2/edit/jobs",
            {"draft": draft, "quote_id": first_quote["quote"]["id"], "idempotency_key": "same-key"},
        )
        _, second_quote = self._dispatch("POST", "/api/v2/edit/quotes", {"draft": draft})
        replay_status, replay = self._dispatch(
            "POST", "/api/v2/edit/jobs",
            {"draft": draft, "quote_id": second_quote["quote"]["id"], "idempotency_key": "same-key"},
        )

        self.assertEqual(first_status, 201)
        self.assertTrue(first["job_id"])
        self.assertEqual(replay_status, 409)
        self.assertEqual(replay["code"], "idempotency_conflict")

    def test_normal_job_creation_rejects_reserved_retry_namespace(self):
        draft = valid_api_draft()
        _, quote = self._dispatch("POST", "/api/v2/edit/quotes", {"draft": draft})
        points_before = api._points_client.balance

        status, payload = self._dispatch(
            "POST",
            "/api/v2/edit/jobs",
            {
                "draft": draft,
                "quote_id": quote["quote"]["id"],
                "idempotency_key": "retry:server-owned:forged",
            },
        )

        self.assertEqual(status, 400)
        self.assertEqual(payload["detail"], "idempotency_key_reserved")
        self.assertEqual(api._points_client.balance, points_before)
        with closing(store.open_store(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM edit_v2_jobs").fetchone()[0], 0)

    def test_ambiguous_precharge_returns_queryable_pending_job(self):
        draft = valid_api_draft()
        _, quote_payload = self._dispatch(
            "POST", "/api/v2/edit/quotes", {"draft": draft}
        )
        with patch.object(api, "_points_client", PendingPoints()):
            status, payload = self._dispatch(
                "POST",
                "/api/v2/edit/jobs",
                {
                    "draft": draft,
                    "quote_id": quote_payload["quote"]["id"],
                    "idempotency_key": "pending-1",
                },
            )

        self.assertEqual(status, 202)
        self.assertEqual(payload["status"], "billing_pending")
        self.assertEqual(payload["billing_status"], "pending")
        self.assertTrue(payload["job_id"])

    def test_job_status_is_owner_scoped(self):
        job = store.create_job(
            "alice",
            {"draft": {"brief": "测试"}},
            "quote",
            "request",
            100,
            uuid_factory=lambda: "123e4567-e89b-42d3-a456-426614174099",
        )

        status, payload = self._dispatch(
            "GET", f"/api/v2/edit/jobs/{job['id']}"
        )
        other_status, _ = self._dispatch(
            "GET",
            f"/api/v2/edit/jobs/{job['id']}",
            user={"username": "bob"},
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload["job"]["id"], job["id"])
        self.assertNotIn("payload_json", payload["job"])
        self.assertEqual(payload["timing"]["current_stage"], "created")
        self.assertIn("queue_seconds", payload["timing"])
        self.assertIn("processing_seconds", payload["timing"])
        self.assertIn("repair_seconds", payload["timing"])
        self.assertIn("remaining_seconds", payload["timing"])
        self.assertEqual(other_status, 404)

    def test_job_response_is_provider_neutral_and_exposes_stable_progress(self):
        job = store.create_job(
            "alice", {"draft": valid_api_draft()}, "quote", "progress-request", 100,
            uuid_factory=lambda: "123e4567-e89b-42d3-a456-426614174097",
        )
        with closing(store.open_store(self.db_path)) as conn:
            conn.execute(
                "UPDATE edit_v2_jobs SET status='rendering',created_at=90,updated_at=100 WHERE id=?",
                (job["id"],),
            )
            conn.execute(
                """INSERT INTO edit_v2_billing(
                       job_id,transaction_key,operation,amount,status,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?)""",
                (job["id"], "hold-progress", "hold", 88, "held", 90, 90),
            )
            conn.commit()

        with patch.object(api, "_now", return_value=110):
            status, payload = self._dispatch("GET", f"/api/v2/edit/jobs/{job['id']}")

        self.assertEqual(status, 200)
        self.assertEqual(payload["stage"], "rendering")
        self.assertEqual(payload["elapsed_seconds"], 20)
        self.assertIn("estimated_remaining_seconds", payload)
        self.assertEqual(payload["degradations"], [])
        self.assertEqual(payload["billing"]["held_points"], 88)
        serialized = json.dumps(payload, ensure_ascii=False).lower()
        for forbidden in (
            "provider", "shotstack", "cos_key", "signed", "signature",
            "traceback", "stack", "supplier",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_completed_job_returns_quality_settlement_and_short_lived_output_links(self):
        job = store.create_job(
            "alice", {"draft": valid_api_draft()}, "quote", "completed-request", 100,
            uuid_factory=lambda: "123e4567-e89b-42d3-a456-426614174096",
        )
        quality = {
            "passed": True,
            "error_codes": [],
            "failing_layers": [],
            "repairable": False,
            "terminal": True,
        }
        with closing(store.open_store(self.db_path)) as conn:
            conn.execute(
                """UPDATE edit_v2_jobs
                   SET status='completed',output_cos_key=?,created_at=90,updated_at=120
                   WHERE id=?""",
                ("ai-edit-v2/0123456789abcdef/123e4567-e89b-42d3-a456-426614174096/delivery/final.mp4", job["id"]),
            )
            conn.execute(
                """INSERT INTO edit_v2_billing(
                       job_id,transaction_key,operation,amount,status,response_json,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (job["id"], "hold-completed", "hold", 88, "settled",
                 json.dumps({"held_points": 88, "actual_points": 61, "refunded_points": 27}), 90, 120),
            )
            conn.execute(
                """INSERT INTO edit_v2_delivery_intents(
                       job_id,owner,idempotency_key,cos_key,source_size_bytes,source_sha256,
                       quality_json,actual_cost,canonical_digest,status,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (job["id"], "alice", "delivery-completed",
                 "ai-edit-v2/0123456789abcdef/123e4567-e89b-42d3-a456-426614174096/delivery/final.mp4",
                 100, "a" * 64, json.dumps(quality, sort_keys=True, separators=(",", ":")),
                 61, "b" * 64, "completed", 110, 120),
            )
            conn.execute(
                """INSERT INTO edit_v2_delivery_outbox(
                       job_id,owner,payload_json,status,asset_id,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?)""",
                (job["id"], "alice", "{}", "delivered", 321, 110, 120),
            )
            conn.commit()

        status, payload = self._dispatch("GET", f"/api/v2/edit/jobs/{job['id']}")

        self.assertEqual(status, 200)
        self.assertEqual(payload["quality"]["status"], "passed")
        self.assertEqual(payload["billing"]["actual_charge_points"], 61)
        self.assertEqual(payload["billing"]["refunded_difference_points"], 27)
        self.assertEqual(payload["output"]["asset_id"], 321)
        self.assertTrue(payload["output"]["play_url"].startswith("https://download.example/"))
        self.assertTrue(payload["output"]["download_url"].startswith("https://download.example/"))
        self.assertEqual(payload["output"]["asset_url"], "assets.html?cat=video&asset=321")

    def test_webhook_requires_authenticated_callback_instead_of_user_session(self):
        handler = FakeHandler(
            {"id": "render-123", "status": "done"},
            headers={"Content-Length": "36"},
        )

        handled = api.dispatch(
            handler,
            "POST",
            "/api/v2/edit/webhooks/shotstack?attempt_id=7&token=wrong",
            None,
        )

        self.assertTrue(handled)
        self.assertEqual(handler.responses[0][0], 401)

    def test_authenticated_webhook_accepts_official_fields_and_only_queues_reconcile(self):
        job, attempt_id, token = self._rendering_attempt()
        body = json.loads((Path(__file__).parent / "fixtures" / "ai_edit_v2" /
                           "provider_responses" / "shotstack_webhook_complete.json").read_text(
                               encoding="utf-8"))
        route = f"/api/v2/edit/webhooks/shotstack?attempt_id={attempt_id}&token={token}"
        headers = {"Content-Length": str(len(json.dumps(body).encode("utf-8")))}

        with patch.object(api.shotstack.ShotstackClient, "reconcile", side_effect=AssertionError("webhook must not GET")) as reconcile:
            first_handler = FakeHandler(body, headers=headers)
            second_handler = FakeHandler(body, headers=headers)
            api.dispatch(first_handler, "POST", route, None)
            api.dispatch(second_handler, "POST", route, None)

        self.assertEqual(first_handler.responses, [(202, {"accepted": True, "duplicate": False})])
        self.assertEqual(second_handler.responses, [(202, {"accepted": True, "duplicate": True})])
        reconcile.assert_not_called()

        from server import ai_edit_v2_worker as worker
        from server.content_domains import ai_edit_v2_runtime as runtime
        authoritative = json.loads((Path(__file__).parent / "fixtures" / "ai_edit_v2" /
                                    "provider_responses" / "shotstack_render_success.json").read_text(
                                        encoding="utf-8"))
        calls = []

        def request(method, url, _headers, _body, _timeout):
            calls.append((method, url))
            return authoritative

        with patch.dict(os.environ, {"SHOTSTACK_API_KEY": "test-shotstack-key"}):
            services = runtime.ProductionServices(self.db_path, shotstack_http=request)
            self.assertEqual(worker.reconcile_provider_events(
                "api-test-worker", {"db_path": self.db_path, "lease_seconds": 30},
                {"services": services}, now=200,
            ), 1)
        self.assertEqual(calls, [(
            "GET", "https://api.shotstack.io/edit/stage/render/render-123"
        )])
        with closing(store.open_store(self.db_path)) as conn:
            queued = conn.execute(
                "SELECT normalized_status FROM edit_v2_provider_events WHERE job_id=?",
                (job["id"],),
            ).fetchone()
        self.assertEqual(queued["normalized_status"], "processed")

    def test_webhook_rejects_oversize_malformed_and_mismatched_events(self):
        _job, attempt_id, token = self._rendering_attempt(provider_task_id="render-bound")
        route = f"/api/v2/edit/webhooks/shotstack?attempt_id={attempt_id}&token={token}"
        cases = [
            (FakeHandler({"id": "render-bound", "status": "done"}, headers={"Content-Length": str(64 * 1024 + 1)}), 413),
            (FakeHandler({"id": "render-bound", "status": {"nested": True}}, headers={"Content-Length": "50"}), 400),
            (FakeHandler({"id": "render-other", "status": "done"}, headers={"Content-Length": "40"}), 401),
        ]
        for handler, expected_status in cases:
            with self.subTest(expected_status=expected_status):
                api.dispatch(handler, "POST", route, None)
                self.assertEqual(handler.responses[0][0], expected_status)

    def test_retry_creates_an_idempotent_successor_without_reviving_old_job(self):
        old = store.create_job(
            "alice",
            {"draft": valid_api_draft()},
            "old-quote",
            "old-request",
            100,
            uuid_factory=lambda: "123e4567-e89b-42d3-a456-426614174099",
            material_bindings=[{"material_id": 1, "purpose": "primary"}],
        )
        with closing(store.open_store(self.db_path)) as conn:
            conn.execute(
                "UPDATE edit_v2_jobs SET status='render_failed' WHERE id=?", (old["id"],)
            )
            conn.commit()

        body = {"idempotency_key": "retry-request-1"}
        first_status, first = self._dispatch(
            "POST", f"/api/v2/edit/jobs/{old['id']}/retry", body
        )
        second_status, second = self._dispatch(
            "POST", f"/api/v2/edit/jobs/{old['id']}/retry", body
        )

        self.assertEqual(first_status, 201)
        self.assertEqual(second_status, 201)
        self.assertEqual(second["job_id"], first["job_id"])
        self.assertNotEqual(first["job_id"], old["id"])
        with closing(store.open_store(self.db_path)) as conn:
            old_row = conn.execute(
                "SELECT status FROM edit_v2_jobs WHERE id=?", (old["id"],)
            ).fetchone()
            successor = conn.execute(
                """SELECT status,payload_json,quote_id,predecessor_job_id
                   FROM edit_v2_jobs WHERE id=?""",
                (first["job_id"],),
            ).fetchone()
            successor_bindings = conn.execute(
                "SELECT material_id,purpose FROM edit_v2_job_materials WHERE job_id=?",
                (first["job_id"],),
            ).fetchall()
        self.assertEqual(old_row["status"], "render_failed")
        self.assertEqual(successor["status"], "queued")
        self.assertEqual(json.loads(successor["payload_json"]), {"draft": valid_api_draft()})
        self.assertNotEqual(successor["quote_id"], "old-quote")
        self.assertEqual(successor["predecessor_job_id"], old["id"])
        self.assertEqual(
            [(row["material_id"], row["purpose"]) for row in successor_bindings],
            [(1, "primary")],
        )

    def test_retry_preserves_transaction_conflict_status(self):
        old = store.create_job(
            "alice", {"draft": valid_api_draft()}, "old-quote", "old-request", 100,
            uuid_factory=lambda: "123e4567-e89b-42d3-a456-426614174099",
            material_bindings=[{"material_id": 1, "purpose": "primary"}],
        )
        with closing(store.open_store(self.db_path)) as conn:
            conn.execute(
                "UPDATE edit_v2_jobs SET status='render_failed' WHERE id=?", (old["id"],)
            )
            conn.commit()

        with patch.object(
            api, "_points_client", RejectingPoints(409, "transaction_key conflict")
        ):
            status, payload = self._dispatch(
                "POST",
                f"/api/v2/edit/jobs/{old['id']}/retry",
                {"idempotency_key": "retry-conflict"},
            )

        self.assertEqual(status, 409)
        self.assertEqual(payload, {"detail": "transaction_key conflict"})

    def test_retry_rejects_unavailable_material_before_precharge(self):
        old = store.create_job(
            "alice", {"draft": valid_api_draft()}, "old-quote", "old-request", 100,
            uuid_factory=lambda: "123e4567-e89b-42d3-a456-426614174099",
            material_bindings=[{"material_id": 1, "purpose": "primary"}],
        )
        with closing(store.open_store(self.db_path)) as conn:
            conn.execute("UPDATE edit_v2_jobs SET status='render_failed' WHERE id=?", (old["id"],))
            conn.execute("UPDATE edit_v2_materials SET status='deleted' WHERE id=1")
            conn.commit()
        points_before = api._points_client.balance

        status, payload = self._dispatch(
            "POST", f"/api/v2/edit/jobs/{old['id']}/retry",
            {"idempotency_key": "retry-unavailable"},
        )

        self.assertEqual(status, 400)
        self.assertEqual(payload["detail"], "material_not_available")
        self.assertEqual(api._points_client.balance, points_before)
        with closing(store.open_store(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM edit_v2_jobs").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM edit_v2_billing").fetchone()[0], 0)

    def test_concurrent_different_key_retries_return_one_successor_and_charge_once(self):
        old = store.create_job(
            "alice", {"draft": valid_api_draft()}, "old-quote", "old-request", 100,
            uuid_factory=lambda: "123e4567-e89b-42d3-a456-426614174099",
            material_bindings=[{"material_id": 1, "purpose": "primary"}],
        )
        with closing(store.open_store(self.db_path)) as conn:
            conn.execute("UPDATE edit_v2_jobs SET status='render_failed' WHERE id=?", (old["id"],))
            conn.commit()
        barrier = threading.Barrier(2)
        original_create_quote = api.billing.create_quote

        def synchronized_create_quote(*args, **kwargs):
            barrier.wait(timeout=5)
            return original_create_quote(*args, **kwargs)

        with patch.object(api.billing, "create_quote", side_effect=synchronized_create_quote):
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [
                    executor.submit(
                        self._dispatch,
                        "POST",
                        f"/api/v2/edit/jobs/{old['id']}/retry",
                        {"idempotency_key": f"retry-client-{index}"},
                    )
                    for index in range(2)
                ]
                responses = [future.result(timeout=10) for future in futures]

        self.assertEqual([status for status, _payload in responses], [201, 201])
        job_ids = {payload["job_id"] for _status, payload in responses}
        self.assertEqual(len(job_ids), 1)
        with closing(store.open_store(self.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM edit_v2_jobs").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM edit_v2_billing").fetchone()[0], 1)
            charged = conn.execute(
                "SELECT amount FROM edit_v2_billing WHERE operation='hold'"
            ).fetchone()["amount"]
        self.assertEqual(len(api._points_client.transactions), 1)
        self.assertEqual(api._points_client.balance, 500 - charged)

    def test_retry_response_loss_with_new_client_key_replays_winner_after_material_is_deleted(self):
        old = store.create_job(
            "alice", {"draft": valid_api_draft()}, "old-quote", "old-request", 100,
            uuid_factory=lambda: "123e4567-e89b-42d3-a456-426614174099",
            material_bindings=[{"material_id": 1, "purpose": "primary"}],
        )
        with closing(store.open_store(self.db_path)) as conn:
            conn.execute("UPDATE edit_v2_jobs SET status='render_failed' WHERE id=?", (old["id"],))
            conn.commit()
        body = {"idempotency_key": "replay-after-delete"}
        first_status, first = self._dispatch(
            "POST", f"/api/v2/edit/jobs/{old['id']}/retry", body
        )
        with closing(store.open_store(self.db_path)) as conn:
            conn.execute("UPDATE edit_v2_materials SET status='deleted' WHERE id=1")
            conn.commit()

        replay_status, replay = self._dispatch(
            "POST", f"/api/v2/edit/jobs/{old['id']}/retry",
            {"idempotency_key": "replacement-after-response-loss"},
        )

        self.assertEqual(first_status, 201)
        self.assertEqual(replay_status, 201)
        self.assertEqual(replay["job_id"], first["job_id"])

    def test_terminal_job_response_reports_zero_remaining_time(self):
        for index, terminal in enumerate((
            "completed", "validation_failed", "transcription_failed", "director_failed",
            "asset_failed", "render_failed", "quality_failed", "settlement_failed",
            "storage_failed",
        )):
            with self.subTest(terminal=terminal):
                job = store.create_job(
                    "alice", {"draft": valid_api_draft()}, "quote",
                    f"terminal-{terminal}", 100 + index,
                )
                with closing(store.open_store(self.db_path)) as conn:
                    conn.execute(
                        "UPDATE edit_v2_jobs SET status=?,created_at=100,updated_at=120 WHERE id=?",
                        (terminal, job["id"]),
                    )
                status, payload = self._dispatch("GET", f"/api/v2/edit/jobs/{job['id']}")
                self.assertEqual(status, 200)
                self.assertEqual(payload["estimated_remaining_seconds"], 0)
                self.assertEqual(payload["timing"]["remaining_seconds"], 0)

    def test_retry_rejects_non_terminal_job(self):
        old = store.create_job(
            "alice",
            {"draft": {}},
            "old-quote",
            "old-request",
            100,
            uuid_factory=lambda: "123e4567-e89b-42d3-a456-426614174099",
        )

        status, payload = self._dispatch(
            "POST",
            f"/api/v2/edit/jobs/{old['id']}/retry",
            {"idempotency_key": "retry-request-1"},
        )

        self.assertEqual(status, 409)
        self.assertIn("终态失败", payload["detail"])

    def test_retry_does_not_accept_forged_successor_for_non_failed_job(self):
        old = store.create_job(
            "alice", {"draft": valid_api_draft()}, "old-quote", "old-request", 100,
            uuid_factory=lambda: "123e4567-e89b-42d3-a456-426614174099",
        )
        store.create_job(
            "alice",
            {"draft": valid_api_draft()},
            "forged-quote",
            f"retry:{old['id']}:forged",
            101,
            uuid_factory=lambda: "123e4567-e89b-42d3-a456-426614174098",
            material_bindings=[{"material_id": 1, "purpose": "primary"}],
        )

        status, payload = self._dispatch(
            "POST",
            f"/api/v2/edit/jobs/{old['id']}/retry",
            {"idempotency_key": "forged"},
        )

        self.assertEqual(status, 409)
        self.assertIn("终态失败", payload["detail"])


class CoreDispatchTests(unittest.TestCase):
    def test_core_forwards_v2_get_and_post_without_changing_legacy_handlers(self):
        server_dir = str(Path(__file__).resolve().parents[1] / "server")
        if server_dir not in sys.path:
            sys.path.insert(0, server_dir)
        from content_domains import core

        calls = []

        def fake_dispatch(handler, method, path, user):
            calls.append((method, path, user))
            return True

        for method_name, http_method in (("do_GET", "GET"), ("do_POST", "POST")):
            handler = object.__new__(core.H)
            handler.path = "/api/v2/edit/capabilities"
            handler.headers = {}
            with patch.object(core, "verify", return_value={"username": "alice"}), patch.object(
                core.ai_edit_v2_api, "dispatch", side_effect=fake_dispatch
            ):
                getattr(handler, method_name)()

            self.assertEqual(calls[-1], (http_method, handler.path, {"username": "alice"}))

        self.assertNotIn("ai_edit_v2", core.HANDLERS)

    def test_core_preserves_webhook_query_for_independent_authentication(self):
        server_dir = str(Path(__file__).resolve().parents[1] / "server")
        if server_dir not in sys.path:
            sys.path.insert(0, server_dir)
        from content_domains import core

        calls = []
        handler = object.__new__(core.H)
        handler.path = "/api/v2/edit/webhooks/shotstack?attempt_id=7&token=callback-token"
        handler.headers = {}
        with patch.object(core, "verify") as verify, patch.object(
            core.ai_edit_v2_api,
            "dispatch",
            side_effect=lambda _handler, method, path, user: calls.append((method, path, user)) or True,
        ):
            handler.do_POST()

        verify.assert_not_called()
        self.assertEqual(calls, [("POST", handler.path, None)])


if __name__ == "__main__":
    unittest.main()
