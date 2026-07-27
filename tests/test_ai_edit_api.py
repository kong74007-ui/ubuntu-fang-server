# -*- coding: utf-8 -*-
import json
import os
import pathlib
import queue
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from content_domains import ai_edit_api, ai_edit_store, audio, core, video


class FakeHandler:
    def __init__(self, path, body=None, token="token", headers=None):
        self.path = path
        self._body = body or {}
        self._request_token = token
        self.headers = headers or {}
        self.response = None

    def _token(self):
        return self._request_token

    def _json_body_strict(self):
        return self._body

    def _send(self, status, body):
        self.response = (status, body)
        return self.response


class FakePointsError(Exception):
    def __init__(self, status, detail):
        self.status = status
        self.detail = detail


class FakePoints:
    AuthPointsError = FakePointsError

    def __init__(self):
        self.deductions = []
        self.refunds = []

    def cost_of(self, kind, body):
        return 30

    def deduct_points(self, username, cost, reason):
        self.deductions.append((username, cost, reason))
        return 970

    def safe_refund_points(self, username, cost, reason):
        self.refunds.append((username, cost, reason))
        return 1000


class AiEditApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.temp.name)
        self.job_db = str(root / "jobs.db")
        self.asset_db = str(root / "assets.db")
        self.edit_db = str(root / "edit.db")
        self.points = FakePoints()
        self.originals = {
            "JOB_DB": core.JOB_DB,
            "AUDIO_DB": core.AUDIO_DB,
            "domains": core._domains,
            "verify": core.verify,
            "queue": getattr(core, "_ai_edit_job_queue", None),
            "queued": core._queued_job_ids,
        }
        core.JOB_DB = self.job_db
        core.AUDIO_DB = self.asset_db
        core.verify = lambda token: {"username": "fang", "must_change": False} if token else None
        core._domains = lambda: (audio, self.points, video)
        core._queued_job_ids = set()
        core.init_db()
        core.init_audio_db()
        ai_edit_store.init_db(self.edit_db)
        self.env = mock.patch.dict(
            os.environ,
            {"AI_EDIT_ENABLED": "1", "AI_EDIT_DB": self.edit_db},
        )
        self.env.start()
        if hasattr(core, "_ai_edit_job_queue"):
            core._ai_edit_job_queue = queue.Queue(maxsize=8)

    def tearDown(self):
        self.env.stop()
        core.JOB_DB = self.originals["JOB_DB"]
        core.AUDIO_DB = self.originals["AUDIO_DB"]
        core._domains = self.originals["domains"]
        core.verify = self.originals["verify"]
        core._queued_job_ids = self.originals["queued"]
        if self.originals["queue"] is not None:
            core._ai_edit_job_queue = self.originals["queue"]
        self.temp.cleanup()

    def request(self, method, path, body=None, token="token", headers=None):
        handler = FakeHandler(path, body, token, headers)
        handled = ai_edit_api.handle_get(handler) if method == "GET" else ai_edit_api.handle_post(handler)
        self.assertTrue(handled)
        return handler.response

    def add_video(self, username="fang"):
        video.record_video_asset(101, username, {
            "mode": "text", "video_file": "video/source.mp4",
            "video_url": "https://cdn.example/source.mp4", "status": "done",
        })
        with closing(sqlite3.connect(self.asset_db)) as db:
            return db.execute("SELECT id FROM video_assets WHERE job_id=101").fetchone()[0]

    def valid_submit(self, asset_id):
        return {"source_video_asset_id": asset_id, "style": "knowledge_dynamic", "ratio": "9:16"}

    def add_core_edit_job(self, handler):
        now = 1
        with closing(sqlite3.connect(self.job_db)) as db:
            cursor = db.execute(
                "INSERT INTO jobs(kind,username,cost,status,payload,created_at,updated_at,owner) VALUES(?,?,?,?,?,?,?,?)",
                ("ai_edit", "fang", 30, "pending", "{}", now, now, core.SERVICE_OWNER),
            )
            job_id = cursor.lastrowid
            db.commit()
        ai_edit_store.create_edit_job(self.edit_db, job_id, "fang", "knowledge_dynamic", "shotstack", 30)
        core.HANDLERS["ai_edit"] = handler
        return job_id

    def test_styles_are_public_to_logged_in_user(self):
        ai_edit_store.create_material(
            self.edit_db, "material-1", "fang", "image", "product", "generated",
            "edit/fang/generated/product.png", "image/png", 123,
        )
        ai_edit_store.complete_material(self.edit_db, "material-1", "fang", 123)
        status, body = self.request("GET", "/api/v1/edit/styles")
        self.assertEqual(200, status)
        self.assertEqual(3, len(body["styles"]))
        self.assertEqual(30, body["cost"])
        self.assertEqual(["material-1"], [item["id"] for item in body["materials"]])

    def test_disabled_feature_rejects_before_points_change(self):
        asset_id = self.add_video()
        with mock.patch.dict(os.environ, {"AI_EDIT_ENABLED": "0"}):
            status, body = self.request(
                "POST", "/api/v1/edit/jobs", self.valid_submit(asset_id),
                headers={"Idempotency-Key": "edit-submit-disabled"},
            )
        self.assertEqual(503, status)
        self.assertEqual("ai_edit_disabled", body["code"])
        self.assertEqual([], self.points.deductions)
        with closing(sqlite3.connect(self.job_db)) as db:
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])

    def test_create_job_requires_idempotency_and_owned_source(self):
        asset_id = self.add_video()
        status, body = self.request("POST", "/api/v1/edit/jobs", self.valid_submit(asset_id), headers={})
        self.assertEqual(400, status)
        self.assertEqual("idempotency_required", body["code"])
        core.verify = lambda token: {"username": "other", "must_change": False}
        status, _ = self.request(
            "POST", "/api/v1/edit/jobs", self.valid_submit(asset_id),
            headers={"Idempotency-Key": "edit-submit-0001"},
        )
        self.assertEqual(404, status)

    def test_duplicate_submission_returns_same_job_without_second_charge(self):
        asset_id = self.add_video()
        headers = {"Idempotency-Key": "edit-submit-0002"}
        with mock.patch.object(core, "enqueue_job", return_value=True):
            first = self.request("POST", "/api/v1/edit/jobs", self.valid_submit(asset_id), headers=headers)
            second = self.request("POST", "/api/v1/edit/jobs", self.valid_submit(asset_id), headers=headers)
        self.assertEqual(first[1]["job_id"], second[1]["job_id"])
        self.assertEqual(1, len(self.points.deductions))

    def test_upload_complete_verifies_declared_object_size(self):
        with mock.patch("content_domains.ai_edit_api.cos.create_presigned_put", return_value={"url": "https://put.example", "headers": {}}):
            status, created = self.request("POST", "/api/v1/edit/uploads", {"content_type": "video/mp4", "size_bytes": 123})
        self.assertEqual(200, status)
        with mock.patch("content_domains.ai_edit_api.cos.head", return_value={"size_bytes": 122, "content_type": "video/mp4"}):
            status, body = self.request("POST", "/api/v1/edit/uploads/%s/complete" % created["upload_id"], {})
        self.assertEqual(409, status)
        self.assertEqual("upload_size_mismatch", body["code"])

    def test_shotstack_webhook_requeries_provider(self):
        ai_edit_store.create_edit_job(self.edit_db, 88, "fang", "knowledge_dynamic", "shotstack", 30)
        ai_edit_store.set_provider_job(self.edit_db, 88, "render-1", "queued")
        renderer = mock.Mock()
        renderer.get_status.return_value = {"id": "render-1", "status": "done", "url": "https://cdn.example/out.mp4"}
        with mock.patch("content_domains.ai_edit_api.ShotstackRenderer", return_value=renderer):
            status, _ = self.request("POST", "/api/v1/edit/webhooks/shotstack", {"id": "render-1", "status": "done"}, token="")
        self.assertEqual(200, status)
        renderer.get_status.assert_called_once_with("render-1")
        with closing(sqlite3.connect(self.job_db)) as db:
            self.assertIsNone(db.execute("SELECT status FROM jobs WHERE id=88").fetchone())

    def test_success_confirms_hold_and_records_video_asset(self):
        result = {"mode": "ai_edit", "video_file": "edit-output/fang/1.mp4", "video_url": "https://cdn.example/out.mp4",
                  "ratio": "9:16", "resolution": "1080p", "status": "done", "phase": "done"}
        old_handler = core.HANDLERS.get("ai_edit")
        try:
            job_id = self.add_core_edit_job(lambda payload: result)
            with mock.patch.object(core, "_start_job_heartbeat", return_value=lambda: None):
                core.run_job(job_id)
        finally:
            if old_handler is None:
                core.HANDLERS.pop("ai_edit", None)
            else:
                core.HANDLERS["ai_edit"] = old_handler
        with closing(sqlite3.connect(self.edit_db)) as db:
            self.assertEqual("confirmed", db.execute("SELECT status FROM billing_holds WHERE job_id=?", (job_id,)).fetchone()[0])
        with closing(sqlite3.connect(self.asset_db)) as db:
            self.assertEqual("done", db.execute("SELECT status FROM video_assets WHERE job_id=?", (job_id,)).fetchone()[0])

    def test_failure_refunds_and_releases_hold(self):
        old_handler = core.HANDLERS.get("ai_edit")
        try:
            job_id = self.add_core_edit_job(lambda payload: (_ for _ in ()).throw(RuntimeError("render failed")))
            with mock.patch.object(core, "_start_job_heartbeat", return_value=lambda: None):
                core.run_job(job_id)
        finally:
            if old_handler is None:
                core.HANDLERS.pop("ai_edit", None)
            else:
                core.HANDLERS["ai_edit"] = old_handler
        with closing(sqlite3.connect(self.edit_db)) as db:
            self.assertEqual("released", db.execute("SELECT status FROM billing_holds WHERE job_id=?", (job_id,)).fetchone()[0])
        self.assertEqual(1, len(self.points.refunds))


class AiEditRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self.temp.name)
        self.job_db = str(root / "jobs.db")
        self.edit_db = str(root / "edit.db")
        self.old_job_db = core.JOB_DB
        core.JOB_DB = self.job_db
        core.init_db()
        self.env = mock.patch.dict(os.environ, {"AI_EDIT_DB": self.edit_db})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        core.JOB_DB = self.old_job_db
        self.temp.cleanup()

    def insert_running(self, provider_job_id=None):
        with closing(sqlite3.connect(self.job_db)) as db:
            cursor = db.execute(
                "INSERT INTO jobs(kind,username,cost,status,payload,created_at,updated_at,owner) VALUES(?,?,?,?,?,?,?,?)",
                ("ai_edit", "fang", 30, "running", "{}", 1, 1, core.SERVICE_OWNER),
            )
            job_id = cursor.lastrowid
            db.commit()
        ai_edit_store.create_edit_job(self.edit_db, job_id, "fang", "knowledge_dynamic", "shotstack", 30)
        if provider_job_id:
            ai_edit_store.set_provider_job(self.edit_db, job_id, provider_job_id, "rendering")
        return job_id

    def test_orphaned_provider_job_is_requeued_not_refunded(self):
        job_id = self.insert_running("render-1")
        with mock.patch.object(core, "enqueue_job", return_value=True) as enqueue, \
                mock.patch.object(core, "_refund_once") as refund:
            core.reclaim_orphaned_running()
        with closing(sqlite3.connect(self.job_db)) as db:
            status = db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()[0]
        self.assertEqual("pending", status)
        refund.assert_not_called()
        enqueue.assert_called_once_with(job_id, "ai_edit")

    def test_orphaned_job_without_provider_id_uses_existing_refund_path(self):
        job_id = self.insert_running()
        with mock.patch.object(core, "_refund_once") as refund, \
                mock.patch.object(core, "_mark_video_asset_failed"):
            core.reclaim_orphaned_running()
        with closing(sqlite3.connect(self.job_db)) as db:
            status = db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()[0]
        self.assertEqual("error", status)
        refund.assert_called_once_with(job_id, "fang", 30)


if __name__ == "__main__":
    unittest.main()
