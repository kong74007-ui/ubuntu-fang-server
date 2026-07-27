import base64
import json
import pathlib
import queue
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from contextlib import closing
from http.server import ThreadingHTTPServer
from unittest.mock import patch


ROOT = pathlib.Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from content_domains import video


def _data_url(seed):
    raw = b"\x89PNG\r\n\x1a\n" + seed.encode("ascii")
    return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")


class _RecoverablePoints:
    class AuthPointsError(Exception):
        def __init__(self, status, detail):
            self.status, self.detail = status, detail

    def __init__(self, balance=500):
        self.balance = balance
        self.calls = []
        self.transactions = {}
        self.actual_deductions = 0
        self.drop_next_response = False
        self.reject_status = None

    def cost_of(self, kind, body):
        return 30 if kind == "ai_edit" else 20

    def deduct_points(self, username, cost, reason, transaction_key=None):
        self.calls.append((username, cost, reason, transaction_key))
        if self.reject_status:
            raise self.AuthPointsError(self.reject_status, "definitive rejection")
        if transaction_key and transaction_key in self.transactions:
            return self.transactions[transaction_key]
        self.balance -= int(cost)
        self.actual_deductions += 1
        if transaction_key:
            self.transactions[transaction_key] = self.balance
        if self.drop_next_response:
            self.drop_next_response = False
            raise self.AuthPointsError(502, "auth committed but response was lost")
        return self.balance

    def refund_points(self, username, cost, reason, transaction_key=None):
        self.balance += int(cost)
        return self.balance

    def safe_refund_points(self, username, cost, reason):
        self.balance += int(cost)
        return self.balance


class _NoopSubmissionLock:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _accept_enqueue(*_args, **kwargs):
    publish = kwargs.get("before_enqueue")
    return True if publish is None else bool(publish())


class _NoopAudioDomain:
    @staticmethod
    def backfill_audio_assets():
        return None


class VideoBatchValidationTests(unittest.TestCase):
    def _payload(self, avatars=None):
        return {
            "mode": "text",
            "text": "同一份口播文案",
            "voice": "voice-demo",
            "resolution": "1080p",
            "ratio": "9:16",
            "motion": "medium",
            "avatars": avatars or [
                {"image_data": _data_url("one"), "label": "形象一"},
                {"image_data": _data_url("two"), "label": "形象二"},
            ],
        }

    def test_batch_expands_common_settings_into_individual_video_jobs(self):
        items = video.validate_video_batch_payload(self._payload(), username="fang")
        self.assertEqual(2, len(items))
        self.assertEqual([1, 2], [item["batch_index"] for item in items])
        self.assertEqual([2, 2], [item["batch_size"] for item in items])
        self.assertEqual(["形象一", "形象二"], [item["batch_label"] for item in items])
        self.assertTrue(all(item["text"] == "同一份口播文案" for item in items))
        self.assertTrue(all(item["voice"] == "voice-demo" for item in items))

    def test_batch_accepts_owned_saved_avatars_without_embedding_images(self):
        avatars = [{"avatar_id": 11, "label": "门店主理人"}, {"avatar_id": 12, "label": "护理师"}]
        with patch.object(video, "get_video_avatar", return_value={"id": 11, "image_file": "image/avatar.jpg"}) as get_avatar:
            items = video.validate_video_batch_payload(self._payload(avatars), username="fang")
        self.assertEqual(["11", "12"], [item["avatar_id"] for item in items])
        self.assertTrue(all(not item["image_data"] for item in items))
        self.assertEqual([("fang", "11"), ("fang", "12")], [call.args for call in get_avatar.call_args_list])

    def test_batch_rejects_wrong_count_duplicate_or_non_text_mode(self):
        with self.assertRaisesRegex(ValueError, "至少选择 2"):
            video.validate_video_batch_payload(self._payload([{"image_data": _data_url("one")}]))
        with self.assertRaisesRegex(ValueError, "最多选择 3"):
            video.validate_video_batch_payload(self._payload([
                {"image_data": _data_url(str(i))} for i in range(4)
            ]), max_items=3)
        with self.assertRaisesRegex(ValueError, "不能重复"):
            image = _data_url("same")
            video.validate_video_batch_payload(self._payload([{"image_data": image}, {"image_data": image}]))
        payload = self._payload()
        payload["mode"] = "audio"
        with self.assertRaisesRegex(ValueError, "仅支持文案配音"):
            video.validate_video_batch_payload(payload)

    def test_text_and_avatar_ownership_are_checked_before_generation(self):
        payload = {"mode": "text", "avatar_id": "9", "voice": "v", "text": ""}
        with self.assertRaisesRegex(ValueError, "text 必填"):
            video.validate_video_payload(payload, username="fang")
        payload["text"] = "hello"
        with patch.object(video, "get_video_avatar", side_effect=ValueError("形象不存在")):
            with self.assertRaisesRegex(ValueError, "形象不存在"):
                video.validate_video_payload(payload, username="fang")

    def test_audio_mode_accepts_owned_audio_file_and_normalizes_relative_path(self):
        payload = {"mode": "audio", "image_data": _data_url("hero"), "audio_file": "voice.mp3"}
        audio_fp = video.OUT_DIR / "audio" / "voice.mp3"
        with patch.object(video, "_resolve_out_file", return_value=audio_fp), \
                patch.object(video, "_user_owns_output_file", return_value=True):
            cleaned = video.validate_video_payload(payload, username="fang")
        self.assertEqual("audio/voice.mp3", cleaned["audio_file"])

    def test_audio_mode_rejects_unowned_or_unsupported_audio_file(self):
        payload = {"mode": "audio", "image_data": _data_url("hero"), "audio_file": "audio/voice.mp3"}
        audio_fp = video.OUT_DIR / "audio" / "voice.mp3"
        with patch.object(video, "_resolve_out_file", return_value=audio_fp), \
                patch.object(video, "_user_owns_output_file", return_value=False):
            with self.assertRaisesRegex(ValueError, "不属于当前账号"):
                video.validate_video_payload(payload, username="fang")
        bad_fp = video.OUT_DIR / "video" / "voice.txt"
        with patch.object(video, "_resolve_out_file", return_value=bad_fp):
            with self.assertRaisesRegex(ValueError, "仅支持 mp3、wav、m4a"):
                video.validate_video_payload(payload, username=None)

    def test_audio_job_can_reuse_owned_audio_file_without_resaving(self):
        payload = {"_username": "fang", "_job_id": 8, "mode": "audio", "image_data": _data_url("hero"),
                   "audio_file": "audio/voice.mp3", "resolution": "1080p", "ratio": "9:16", "motion": "medium"}
        save_calls = []
        def fake_save(data_url, prefix, allowed_ext):
            save_calls.append(prefix)
            if prefix == "vid_img":
                return "image/avatar.jpg"
            raise AssertionError("audio_file 已复用时不应再次落盘音频")
        with patch.object(video, "HEYGEN_API_KEY", "configured"), \
                patch.object(video, "_save_data_file", side_effect=fake_save), \
                patch.object(video, "_resolve_out_file", return_value=video.OUT_DIR / "audio" / "voice.mp3"), \
                patch.object(video, "_user_owns_output_file", return_value=True), \
                patch.object(video, "generate_heygen_video", return_value={"video_file": "video/out.mp4", "duration": 12}) as generate, \
                patch.object(video, "public_url", return_value="https://cdn.example/out.mp4"), \
                patch.object(video, "_file_url", side_effect=lambda value: "/api/gen/file/" + str(value or "")):
            result = video.gen_video(payload)
        self.assertEqual(["vid_img"], save_calls)
        generate.assert_called_once_with("image/avatar.jpg", "audio/voice.mp3", "1080p", "9:16", "medium")
        self.assertEqual("audio/voice.mp3", result["audio_file"])
        self.assertEqual("/api/gen/file/audio/voice.mp3", result["audio_url"])

    def test_talking_job_can_reuse_owned_avatar_image(self):
        payload = {"_username": "fang", "_job_id": 8, "mode": "text", "avatar_id": "9",
                   "text": "hello", "voice": "v", "resolution": "1080p", "ratio": "9:16", "motion": "medium"}
        with patch.object(video, "HEYGEN_API_KEY", "configured"), \
                patch.object(video, "get_video_avatar", return_value={"id": 9, "image_file": "image/avatar.jpg"}), \
                patch.object(video, "gen_audio", return_value={"file": "audio/voice.mp3", "url": "/audio.mp3"}), \
                patch.object(video, "generate_heygen_video", return_value={"video_file": "video/out.mp4", "duration": 12}) as generate, \
                patch.object(video, "public_url", return_value="https://cdn.example/out.mp4"), \
                patch.object(video, "_file_url", side_effect=lambda value: "/api/gen/file/" + str(value or "")):
            result = video.gen_video(payload)
        generate.assert_called_once_with("image/avatar.jpg", "audio/voice.mp3", "1080p", "9:16", "medium")
        self.assertEqual(9, result["avatar_id"])


class VideoBatchOpenApiTests(unittest.TestCase):
    def test_published_specs_require_the_paid_submission_key(self):
        for relative in ("site/api-docs/openapi.json", "docs/api/openapi.json"):
            with self.subTest(spec=relative):
                spec = json.loads((ROOT / relative).read_text(encoding="utf-8"))
                operation = spec["paths"]["/api/gen/video/batch"]["post"]
                header = next(
                    item for item in operation.get("parameters", [])
                    if item.get("in") == "header" and item.get("name") == "Idempotency-Key")
                self.assertTrue(header.get("required"))
                self.assertEqual(header["schema"].get("minLength"), 8)
                self.assertEqual(header["schema"].get("maxLength"), 128)
                self.assertEqual(header["schema"].get("pattern"), r"^[A-Za-z0-9._:-]+$")
                self.assertIn("409", operation["responses"])
                self.assertIn("502", operation["responses"])


class VideoBatchIntegrationGuardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.core = (ROOT / "server/content_domains/core.py").read_text(encoding="utf-8")
        cls.html = (ROOT / "site/workbench/video.html").read_text(encoding="utf-8")

    def test_batch_route_checks_slots_before_deduct_and_enqueues_atomically(self):
        start = self.core.index('if p == "/api/gen/video/batch":')
        end = self.core.index('if p.startswith("/api/gen/")', start)
        route = self.core[start:end]
        self.assertLess(
            route.index("validate_video_batch_payload"),
            route.index("costs = [points_domain.cost_of"))
        self.assertLess(
            route.index("costs = [points_domain.cost_of"),
            route.index("_idempotency_prepare("))
        self.assertLess(route.index("active_jobs + len(payloads)"), route.index("deduct_points"))
        self.assertIn("queued = enqueue_jobs(", route)
        self.assertIn('job_ids, "video", "text"', route)
        self.assertIn('"available_slots"', route)

    def test_workbench_ui_exposes_single_and_batch_avatar_talking_video(self):
        for needle in ('data-talking-shape="single"', 'data-talking-shape="batch"',
                       'id="imageFile"', 'id="batchImageFile"', 'id="batchTalkingImages"',
                       '选择多个形象', "fetch('/api/gen/video'", "fetch('/api/gen/video/batch'",
                       'talkingBatchItems', 'function renderTalkingBatchImages()',
                       'body.avatars=talkingBatchItems.map', 'jobs.forEach(function(job)'):
            self.assertIn(needle, self.html)

    def test_inline_javascript_parses_as_utf8(self):
        scripts = re.findall(r"<script(?:\s[^>]*)?>([\s\S]*?)</script>", self.html)
        self.assertTrue(scripts)
        checked = subprocess.run(["node", "--check", "-"], input=scripts[-1], text=True,
                                 encoding="utf-8", capture_output=True)
        self.assertEqual(0, checked.returncode, checked.stderr)

    def test_batch_http_route_accepts_all_jobs_and_rejects_slot_overflow_before_deduct(self):
        from content_domains import core

        class FakePointsError(Exception):
            def __init__(self, status, detail):
                self.status, self.detail = status, detail

        class FakePoints:
            AuthPointsError = FakePointsError

            def __init__(self):
                self.deductions = []
                self.refunds = []

            def cost_of(self, kind, body):
                return 20

            def deduct_points(self, username, cost, reason, transaction_key=None):
                self.deductions.append((username, cost, reason, transaction_key))
                return 100 - cost

            def safe_refund_points(self, username, cost, reason):
                return 100

            def refund_points(self, username, cost, reason, transaction_key=""):
                self.refunds.append((username, cost, reason, transaction_key))
                return 100

        originals = {
            "JOB_DB": core.JOB_DB, "AUDIO_DB": core.AUDIO_DB, "_domains": core._domains,
            "verify": core.verify, "require_enabled": core.feature_flags.require_enabled,
            "queue": core._talking_job_queue, "ids": core._queued_job_ids,
            "max_active": core.MAX_USER_ACTIVE_JOBS,
        }
        fake = FakePoints()
        server = None
        with tempfile.TemporaryDirectory() as td:
            core.JOB_DB = str(pathlib.Path(td) / "jobs.db")
            core.AUDIO_DB = str(pathlib.Path(td) / "assets.db")
            core.verify = lambda token: {"username": "fang", "must_change": False}
            core.feature_flags.require_enabled = lambda kind: None
            core._talking_job_queue = queue.Queue(maxsize=8)
            core._queued_job_ids = set()
            core.MAX_USER_ACTIVE_JOBS = 3
            try:
                with closing(sqlite3.connect(core.JOB_DB)) as db:
                    db.execute("""CREATE TABLE jobs(id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT,username TEXT,cost INTEGER,
                        status TEXT DEFAULT 'pending',payload TEXT,result TEXT,error TEXT,created_at INTEGER,updated_at INTEGER,
                        deleted INTEGER DEFAULT 0,refunded INTEGER DEFAULT 0,owner TEXT)""")
                    db.commit()
                core.init_audio_db()
                core._domains = lambda: (None, fake, video)
                server = ThreadingHTTPServer(("127.0.0.1", 0), core.H)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                url = "http://127.0.0.1:%d/api/gen/video/batch" % server.server_address[1]
                data = json.dumps({
                    "mode": "text", "text": "batch", "voice": "voice-demo",
                    "avatars": [{"image_data": _data_url("http-one")}, {"image_data": _data_url("http-two")}],
                }).encode("utf-8")
                request = urllib.request.Request(url, data=data, method="POST", headers={
                    "Authorization": "Bearer test", "Content-Type": "application/json",
                    "Idempotency-Key": "batch-submit-001",
                })
                with urllib.request.urlopen(request, timeout=5) as response:
                    accepted = json.loads(response.read())
                self.assertEqual(2, accepted["count"])
                self.assertEqual(40, accepted["cost"])
                self.assertEqual(("fang", 40), fake.deductions[0][:2])
                self.assertTrue(fake.deductions[0][2].startswith("job:video_batch submit:"))
                with closing(core.jdb()) as db:
                    rows = db.execute("SELECT status,cost,payload FROM jobs ORDER BY id").fetchall()
                self.assertEqual(["pending", "pending"], [row["status"] for row in rows])
                self.assertEqual([20, 20], [row["cost"] for row in rows])
                self.assertEqual(2, core._talking_job_queue.qsize())

                with urllib.request.urlopen(request, timeout=5) as response:
                    replayed = json.loads(response.read())
                self.assertEqual(accepted, replayed)
                self.assertEqual(1, len(fake.deductions))
                with closing(core.jdb()) as db:
                    self.assertEqual(2, db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])

                changed = urllib.request.Request(url, data=data.replace(b'"batch"', b'"changed"'), method="POST", headers={
                    "Authorization": "Bearer test", "Content-Type": "application/json",
                    "Idempotency-Key": "batch-submit-001",
                })
                with self.assertRaises(urllib.error.HTTPError) as conflict:
                    urllib.request.urlopen(changed, timeout=5)
                self.assertEqual(409, conflict.exception.code)
                self.assertEqual("idempotency_conflict", json.loads(conflict.exception.read())["code"])
            finally:
                if server:
                    server.shutdown()
                    server.server_close()
                core.JOB_DB, core.AUDIO_DB = originals["JOB_DB"], originals["AUDIO_DB"]
                core._domains, core.verify = originals["_domains"], originals["verify"]
                core.feature_flags.require_enabled = originals["require_enabled"]
                core._talking_job_queue, core._queued_job_ids = originals["queue"], originals["ids"]
                core.MAX_USER_ACTIVE_JOBS = originals["max_active"]


class VideoPaidSubmissionRecoveryTests(unittest.TestCase):
    def setUp(self):
        from content_domains import ai_edit, ai_edit_api, core, upstream_guard

        self.core = core
        self.upstream_guard = upstream_guard
        self.points = _RecoverablePoints()
        self.temp = tempfile.TemporaryDirectory()
        self.job_db = str(pathlib.Path(self.temp.name) / "jobs.db")
        self.audio_db = str(pathlib.Path(self.temp.name) / "assets.db")
        self.patches = [
            patch.object(core, "JOB_DB", self.job_db),
            patch.object(core, "AUDIO_DB", self.audio_db),
            patch.object(core, "verify", lambda _token: {"username": "fang", "must_change": False}),
            patch.object(core.feature_flags, "require_enabled", lambda _kind: None),
            patch.object(core, "_domains", lambda: (_NoopAudioDomain(), self.points, video)),
            patch.object(core, "HANDLERS", {
                "video": lambda body: body,
                "ai_edit": lambda body: body,
            }),
            patch.object(core, "_submission_lock", _NoopSubmissionLock()),
            patch.object(core, "MAX_USER_ACTIVE_JOBS", 5),
            patch.object(core, "enqueue_jobs", _accept_enqueue),
            patch.object(core, "enqueue_job", _accept_enqueue),
            patch.object(core, "is_shutting_down", lambda: False),
            patch.object(core.miniprogram_security, "check_payload", lambda _body: None),
            patch.object(upstream_guard, "exhausted_reason", lambda _kind, _body: None),
            patch.object(video, "record_video_pending_asset", lambda *_args, **_kwargs: None),
            patch.object(ai_edit, "validate_ai_edit_payload", lambda body, _username: dict(body)),
            patch.object(ai_edit_api, "prepare_submission", lambda *_args, **_kwargs: False),
        ]
        for item in self.patches:
            item.start()
        with closing(sqlite3.connect(self.job_db)) as db:
            db.execute("""CREATE TABLE jobs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT,username TEXT,cost INTEGER,
                status TEXT DEFAULT 'pending',payload TEXT,result TEXT,error TEXT,
                created_at INTEGER,updated_at INTEGER,deleted INTEGER DEFAULT 0,
                refunded INTEGER DEFAULT 0,owner TEXT)""")
            db.commit()
        core.init_audio_db()
        self.server = None
        self.thread = None
        self._start_server()

    def tearDown(self):
        self._stop_server()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def _start_server(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.core.H)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

    def _stop_server(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=3)
            self.server = None
            self.thread = None

    def _restart_process_boundary(self):
        self._stop_server()
        self.core._queued_job_ids = set()
        self._start_server()

    def _post(self, path, body, key):
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": "Bearer test",
                "Content-Type": "application/json",
                "Idempotency-Key": key,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read() or b"{}")

    def _batch_body(self, text="batch"):
        return {
            "mode": "text", "text": text, "voice": "voice-demo",
            "avatars": [
                {"image_data": _data_url("guard-one")},
                {"image_data": _data_url("guard-two")},
            ],
        }

    def _clear_submission_state(self):
        with closing(self.core.jdb()) as db:
            db.execute("DELETE FROM jobs")
            if db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='submission_idempotency'").fetchone():
                db.execute("DELETE FROM submission_idempotency")
            db.commit()
        self.points.calls.clear()
        self.points.transactions.clear()
        self.points.actual_deductions = 0

    def _assert_rejected_before_charge(self, status, payload, expected_status, expected_code):
        self.assertEqual(expected_status, status)
        self.assertEqual(expected_code, payload.get("code"))
        self.assertEqual(0, self.points.actual_deductions)
        with closing(self.core.jdb()) as db:
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])

    def test_batch_shares_content_shutdown_and_upstream_precharge_guards(self):
        cases = [
            ("content", patch.object(
                self.core.miniprogram_security, "check_payload",
                side_effect=self.core.miniprogram_security.ContentRejected("risky")),
             400, "content_rejected"),
            ("security-unavailable", patch.object(
                self.core.miniprogram_security, "check_payload",
                side_effect=self.core.miniprogram_security.SecurityUnavailable("offline")),
             503, "content_security_unavailable"),
            ("shutdown", patch.object(self.core, "is_shutting_down", return_value=True),
             503, "shutting_down"),
            ("upstream", patch.object(
                self.upstream_guard, "exhausted_reason", return_value="quota exhausted"),
             503, "upstream_exhausted"),
        ]
        for name, guard, expected_status, expected_code in cases:
            with self.subTest(guard=name):
                self._clear_submission_state()
                with guard:
                    status, payload = self._post(
                        "/api/gen/video/batch", self._batch_body(), "batch-guard-" + name)
                self._assert_rejected_before_charge(
                    status, payload, expected_status, expected_code)

    def test_batch_auth_response_loss_recovers_same_batch_after_process_restart(self):
        body = self._batch_body()
        self.points.drop_next_response = True

        first_status, first = self._post(
            "/api/gen/video/batch", body, "batch-auth-loss-001")

        self.assertEqual(502, first_status)
        self.assertEqual("points_result_unknown", first.get("code"))
        self.assertRegex(first.get("batch_id", ""), r"^batch-[0-9a-f]{32}$")
        self.assertEqual(2, len(first.get("job_ids") or []))
        self._restart_process_boundary()

        second_status, second = self._post(
            "/api/gen/video/batch", body, "batch-auth-loss-001")
        replay_status, replay = self._post(
            "/api/gen/video/batch", body, "batch-auth-loss-001")

        self.assertEqual(200, second_status)
        self.assertEqual((second_status, second), (replay_status, replay))
        self.assertEqual(first["batch_id"], second["batch_id"])
        self.assertEqual(first["job_ids"], second["job_ids"])
        self.assertTrue(all(int(job_id) < 0 for job_id in second["job_ids"]))
        self.assertEqual(1, self.points.actual_deductions)
        self.assertEqual(1, len(self.points.transactions))
        with closing(self.core.jdb()) as db:
            rows = db.execute("SELECT id,payload FROM jobs ORDER BY id").fetchall()
        self.assertEqual(sorted(second["job_ids"]), sorted(row["id"] for row in rows))
        self.assertTrue(all(json.loads(row["payload"])["batch_id"] == second["batch_id"] for row in rows))

        conflict_status, conflict = self._post(
            "/api/gen/video/batch", self._batch_body("changed"), "batch-auth-loss-001")
        self.assertEqual(409, conflict_status)
        self.assertEqual("idempotency_conflict", conflict.get("code"))
        self.assertEqual(1, self.points.actual_deductions)

    def test_initial_ai_edit_auth_response_loss_recovers_same_job_after_process_restart(self):
        body = {"source_video_asset_id": 7, "style_id": "auto", "materials": []}
        self.points.drop_next_response = True

        first_status, first = self._post(
            "/api/gen/ai-edit/jobs", body, "ai-edit-auth-loss-001")

        self.assertEqual(502, first_status)
        self.assertEqual("points_result_unknown", first.get("code"))
        self.assertLess(int(first.get("job_id") or 0), 0)
        self._restart_process_boundary()

        second_status, second = self._post(
            "/api/gen/ai-edit/jobs", body, "ai-edit-auth-loss-001")
        replay_status, replay = self._post(
            "/api/gen/ai-edit/jobs", body, "ai-edit-auth-loss-001")

        self.assertEqual(202, second_status)
        self.assertEqual((second_status, second), (replay_status, replay))
        self.assertEqual(first["job_id"], second["job_id"])
        self.assertEqual(1, self.points.actual_deductions)
        self.assertEqual(1, len(self.points.transactions))
        with closing(self.core.jdb()) as db:
            rows = db.execute(
                "SELECT id,kind,username FROM jobs WHERE kind='ai_edit'").fetchall()
        self.assertEqual([(second["job_id"], "ai_edit", "fang")], [tuple(row) for row in rows])

    def test_initial_ai_edit_recovers_inserted_job_after_complete_crash(self):
        from content_domains import jobs_store, submission_idempotency

        body = {"source_material_ids": [1]}
        key = "ai-edit-inserted-before-complete"
        route = "/api/gen/ai_edit"
        state, _response = self.core._idempotency_begin("fang", route, key, body)
        self.assertEqual("new", state)
        identity = submission_idempotency.paid_submission_identity("fang", route, key, 1)
        job_id = identity["job_ids"][0]
        created = jobs_store.create_paid_job(
            self.core.jdb, self.points.deduct_points, self.points.refund_points,
            "ai_edit", "fang", 30, dict(body), self.core.SERVICE_OWNER,
            submission_ref=identity["submission_ref"],
            deduct_transaction_key=identity["deduct_transaction_key"],
            job_id=job_id, return_created=True,
            submission_state="initializing:dead-process")
        self.assertEqual((job_id, 470, True), created)
        self._restart_process_boundary()

        status, payload = self._post("/api/gen/ai-edit/jobs", body, key)
        replay_status, replay = self._post("/api/gen/ai-edit/jobs", body, key)

        self.assertEqual(202, status)
        self.assertEqual(job_id, payload["job_id"])
        self.assertEqual((status, payload), (replay_status, replay))
        self.assertEqual(1, self.points.actual_deductions)
        with closing(self.core.jdb()) as db:
            self.assertEqual(1, db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])

    def test_batch_publishes_ready_before_a_worker_can_claim_the_first_job(self):
        claimed = []

        def claim_after_publication(job_ids, *_args, **kwargs):
            publish = kwargs.get("before_enqueue")
            if publish is not None:
                self.assertTrue(publish())
            with closing(self.core.jdb()) as db:
                db.execute("UPDATE jobs SET status='running' WHERE id=?", (job_ids[0],))
                db.commit()
            claimed.append(job_ids[0])
            return True

        with patch.object(self.core, "enqueue_jobs", side_effect=claim_after_publication):
            status, payload = self._post(
                "/api/gen/video/batch", self._batch_body(), "batch-publish-before-claim")

        self.assertEqual(200, status)
        self.assertEqual([payload["job_ids"][0]], claimed)
        with closing(self.core.jdb()) as db:
            rows = db.execute("SELECT status,payload FROM jobs ORDER BY id").fetchall()
        self.assertEqual({"pending", "running"}, {row["status"] for row in rows})
        self.assertTrue(all(
            json.loads(row["payload"])["_submission_state"] == "ready" for row in rows))

    def test_initial_ai_edit_publishes_ready_before_a_worker_can_claim_the_job(self):
        claimed = []

        def claim_after_publication(job_id, *_args, **kwargs):
            publish = kwargs.get("before_enqueue")
            if publish is not None:
                self.assertTrue(publish())
            with closing(self.core.jdb()) as db:
                db.execute("UPDATE jobs SET status='running' WHERE id=?", (job_id,))
                db.commit()
            claimed.append(job_id)
            return True

        body = {"source_video_asset_id": 7, "style_id": "auto", "materials": []}
        with patch.object(self.core, "enqueue_job", side_effect=claim_after_publication):
            status, payload = self._post(
                "/api/gen/ai-edit/jobs", body, "ai-edit-publish-before-claim")

        self.assertEqual(202, status)
        self.assertEqual([payload["job_id"]], claimed)
        with closing(self.core.jdb()) as db:
            row = db.execute("SELECT status,payload FROM jobs WHERE id=?", (payload["job_id"],)).fetchone()
        self.assertEqual("running", row["status"])
        self.assertEqual("ready", json.loads(row["payload"])["_submission_state"])

    def test_batch_ready_receipt_wins_over_a_later_worker_error_on_replay(self):
        real_complete = self.core._idempotency_complete
        body = self._batch_body()
        key = "batch-ready-before-worker-error"

        with patch.object(
                self.core, "_idempotency_complete",
                side_effect=lambda _username, _route, _key, response: response):
            first_status, first = self._post("/api/gen/video/batch", body, key)
        self.assertEqual(200, first_status)
        with closing(self.core.jdb()) as db:
            db.execute("UPDATE jobs SET status='error',error='worker failed' WHERE id=?", (first["job_ids"][0],))
            db.commit()

        with patch.object(self.core, "_idempotency_complete", side_effect=real_complete):
            replay_status, replay = self._post("/api/gen/video/batch", body, key)

        self.assertEqual(200, replay_status)
        self.assertEqual(first["batch_id"], replay["batch_id"])
        self.assertEqual(first["job_ids"], replay["job_ids"])
        self.assertEqual(1, self.points.actual_deductions)

    def test_initial_ready_receipt_wins_over_a_later_worker_error_on_replay(self):
        real_complete = self.core._idempotency_complete
        body = {"source_video_asset_id": 7, "style_id": "auto", "materials": []}
        key = "ai-edit-ready-before-worker-error"

        with patch.object(
                self.core, "_idempotency_complete",
                side_effect=lambda _username, _route, _key, response: response):
            first_status, first = self._post("/api/gen/ai-edit/jobs", body, key)
        self.assertEqual(202, first_status)
        with closing(self.core.jdb()) as db:
            db.execute("UPDATE jobs SET status='error',error='worker failed' WHERE id=?", (first["job_id"],))
            db.commit()

        with patch.object(self.core, "_idempotency_complete", side_effect=real_complete):
            replay_status, replay = self._post("/api/gen/ai-edit/jobs", body, key)

        self.assertEqual(202, replay_status)
        self.assertEqual(first["job_id"], replay["job_id"])
        self.assertEqual(1, self.points.actual_deductions)

    def test_batch_auth_loss_recovery_is_not_blocked_by_new_active_jobs(self):
        body = self._batch_body()
        key = "batch-auth-loss-cap-recovery"
        self.points.drop_next_response = True
        first_status, _first = self._post("/api/gen/video/batch", body, key)
        self.assertEqual(502, first_status)
        with closing(self.core.jdb()) as db:
            for index in range(self.core.MAX_USER_ACTIVE_JOBS):
                db.execute(
                    "INSERT INTO jobs(kind,username,cost,status,payload,created_at,updated_at,owner) "
                    "VALUES('video','fang',0,'pending','{}',1,1,'content')")
            db.commit()

        status, payload = self._post("/api/gen/video/batch", body, key)

        self.assertEqual(200, status)
        self.assertEqual(2, len(payload["job_ids"]))
        self.assertEqual(1, self.points.actual_deductions)

    def test_initial_auth_loss_recovery_is_not_blocked_by_new_active_jobs(self):
        body = {"source_video_asset_id": 7, "style_id": "auto", "materials": []}
        key = "ai-edit-auth-loss-cap-recovery"
        self.points.drop_next_response = True
        first_status, _first = self._post("/api/gen/ai-edit/jobs", body, key)
        self.assertEqual(502, first_status)
        with closing(self.core.jdb()) as db:
            for index in range(self.core.MAX_USER_ACTIVE_JOBS):
                db.execute(
                    "INSERT INTO jobs(kind,username,cost,status,payload,created_at,updated_at,owner) "
                    "VALUES('video','fang',0,'pending','{}',1,1,'content')")
            db.commit()

        status, payload = self._post("/api/gen/ai-edit/jobs", body, key)

        self.assertEqual(202, status)
        self.assertIsNotNone(payload.get("job_id"))
        self.assertEqual(1, self.points.actual_deductions)

    def test_batch_auth_loss_recovery_uses_precharge_snapshot_after_validation_changes(self):
        body = self._batch_body()
        key = "batch-auth-loss-validation-recovery"
        self.points.drop_next_response = True
        first_status, _first = self._post("/api/gen/video/batch", body, key)
        self.assertEqual(502, first_status)

        with patch.object(
                video, "validate_video_batch_payload",
                side_effect=ValueError("avatar was deleted after the first attempt")):
            status, payload = self._post("/api/gen/video/batch", body, key)

        self.assertEqual(200, status)
        self.assertEqual(2, len(payload["job_ids"]))
        self.assertEqual(1, self.points.actual_deductions)

    def test_initial_auth_loss_recovery_uses_precharge_snapshot_after_validation_changes(self):
        from content_domains import ai_edit

        body = {"source_video_asset_id": 7, "style_id": "auto", "materials": []}
        key = "ai-edit-auth-loss-validation-recovery"
        self.points.drop_next_response = True
        first_status, _first = self._post("/api/gen/ai-edit/jobs", body, key)
        self.assertEqual(502, first_status)

        with patch.object(
                ai_edit, "validate_ai_edit_payload",
                side_effect=ValueError("source was deleted after the first attempt")):
            status, payload = self._post("/api/gen/ai-edit/jobs", body, key)

        self.assertEqual(202, status)
        self.assertIsNotNone(payload.get("job_id"))
        self.assertEqual(1, self.points.actual_deductions)

    def test_batch_recovers_after_crash_between_begin_and_preparation(self):
        body = self._batch_body()
        key = "batch-crash-before-preparation"
        state, _response = self.core._idempotency_begin(
            "fang", "/api/gen/video/batch", key, body)
        self.assertEqual("new", state)

        status, payload = self._post("/api/gen/video/batch", body, key)

        self.assertEqual(200, status)
        self.assertEqual(2, len(payload["job_ids"]))
        self.assertEqual(1, self.points.actual_deductions)

    def test_initial_recovers_after_crash_between_begin_and_preparation(self):
        body = {"source_video_asset_id": 7, "style_id": "auto", "materials": []}
        key = "ai-edit-crash-before-preparation"
        state, _response = self.core._idempotency_begin(
            "fang", "/api/gen/ai_edit", key, body)
        self.assertEqual("new", state)

        status, payload = self._post("/api/gen/ai-edit/jobs", body, key)

        self.assertEqual(202, status)
        self.assertIsNotNone(payload.get("job_id"))
        self.assertEqual(1, self.points.actual_deductions)

    def test_batch_preparation_crash_still_applies_capacity_before_auth(self):
        body = self._batch_body()
        key = "batch-crash-before-preparation-full-cap"
        state, _response = self.core._idempotency_begin(
            "fang", "/api/gen/video/batch", key, body)
        self.assertEqual("new", state)
        with closing(self.core.jdb()) as db:
            for _index in range(self.core.MAX_USER_ACTIVE_JOBS):
                db.execute(
                    "INSERT INTO jobs(kind,username,cost,status,payload,created_at,updated_at,owner) "
                    "VALUES('video','fang',0,'pending','{}',1,1,'content')")
            db.commit()

        status, _payload = self._post("/api/gen/video/batch", body, key)

        self.assertEqual(429, status)
        self.assertEqual(0, self.points.actual_deductions)

    def test_initial_preparation_crash_still_applies_capacity_before_auth(self):
        body = {"source_video_asset_id": 7, "style_id": "auto", "materials": []}
        key = "ai-edit-crash-before-preparation-full-cap"
        state, _response = self.core._idempotency_begin(
            "fang", "/api/gen/ai_edit", key, body)
        self.assertEqual("new", state)
        with closing(self.core.jdb()) as db:
            for _index in range(self.core.MAX_USER_ACTIVE_JOBS):
                db.execute(
                    "INSERT INTO jobs(kind,username,cost,status,payload,created_at,updated_at,owner) "
                    "VALUES('video','fang',0,'pending','{}',1,1,'content')")
            db.commit()

        status, _payload = self._post("/api/gen/ai-edit/jobs", body, key)

        self.assertEqual(429, status)
        self.assertEqual(0, self.points.actual_deductions)

    def test_batch_uses_the_persisted_preparation_winner_for_charge_and_rows(self):
        def alternate_winner(_username, _route, _key, _body, preparation, **_kwargs):
            winner = json.loads(json.dumps(preparation))
            winner["common"]["text"] = "persisted winner"
            winner["costs"] = [7, 7]
            return winner

        with patch.object(self.core, "_idempotency_prepare", side_effect=alternate_winner):
            status, payload = self._post(
                "/api/gen/video/batch", self._batch_body("local contender"),
                "batch-preparation-winner")

        self.assertEqual(200, status)
        self.assertEqual(14, payload["cost"])
        self.assertEqual(14, self.points.calls[0][1])
        with closing(self.core.jdb()) as db:
            rows = db.execute("SELECT cost,payload FROM jobs ORDER BY id").fetchall()
        self.assertEqual([7, 7], [row["cost"] for row in rows])
        self.assertTrue(all(json.loads(row["payload"])["text"] == "persisted winner" for row in rows))

    def test_initial_uses_the_persisted_preparation_winner_for_charge_and_row(self):
        def alternate_winner(_username, _route, _key, _body, preparation, **_kwargs):
            winner = json.loads(json.dumps(preparation))
            winner["payload"]["style_id"] = "persisted-winner"
            winner["cost"] = 7
            return winner

        body = {"source_video_asset_id": 7, "style_id": "local-contender", "materials": []}
        with patch.object(self.core, "_idempotency_prepare", side_effect=alternate_winner):
            status, payload = self._post(
                "/api/gen/ai-edit/jobs", body, "ai-edit-preparation-winner")

        self.assertEqual(202, status)
        self.assertEqual(7, payload["cost"])
        self.assertEqual(7, self.points.calls[0][1])
        with closing(self.core.jdb()) as db:
            row = db.execute("SELECT cost,payload FROM jobs WHERE id=?", (payload["job_id"],)).fetchone()
        self.assertEqual(7, row["cost"])
        self.assertEqual("persisted-winner", json.loads(row["payload"])["style_id"])

    def test_batch_preparation_factors_shared_media_once(self):
        bgm_data = "data:audio/mpeg;base64," + base64.b64encode(b"shared-bgm" * 200).decode("ascii")
        body = self._batch_body()
        body["bgm_data"] = bgm_data
        payloads = video.validate_video_batch_payload(body, "fang", 5)

        prepared = self.core._compact_video_batch_preparation(payloads, [20, 20])
        encoded = json.dumps(prepared, ensure_ascii=False)
        expanded, costs = self.core._expand_video_batch_preparation(prepared, 2)

        self.assertEqual(1, encoded.count(bgm_data))
        self.assertEqual(payloads, expanded)
        self.assertEqual([20, 20], costs)

    def test_oversized_batch_preparation_is_rejected_before_charge(self):
        from content_domains import submission_idempotency

        with patch.object(submission_idempotency, "MAX_PREPARATION_BYTES", 128):
            status, payload = self._post(
                "/api/gen/video/batch", self._batch_body(),
                "batch-preparation-size-limit")

        self.assertEqual(413, status)
        self.assertEqual("submission_snapshot_too_large", payload.get("code"))
        self.assertEqual(0, self.points.actual_deductions)

    def test_tracked_batch_compensation_releases_preparation_quota(self):
        from content_domains import jobs_store

        with patch.object(
                jobs_store, "create_explicit_paid_jobs",
                side_effect=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    jobs_store.PaidJobInsertError("refunded", "tracked-batch"))):
            for index in range(6):
                status, payload = self._post(
                    "/api/gen/video/batch", self._batch_body(),
                    "batch-tracked-comp-%02d" % index)
                self.assertEqual(500, status)
                self.assertEqual("submission_compensated", payload.get("code"))

        with closing(self.core.jdb()) as db:
            pending = db.execute(
                "SELECT COUNT(*) FROM submission_idempotency WHERE response_json LIKE ?",
                ('{"_paid_submission_preparation"%',)).fetchone()[0]
        self.assertEqual(0, pending)

    def test_tracked_initial_compensation_releases_preparation_quota(self):
        from content_domains import jobs_store

        body = {"source_video_asset_id": 7, "style_id": "auto", "materials": []}
        with patch.object(
                jobs_store, "create_paid_job",
                side_effect=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    jobs_store.PaidJobInsertError("queued", "tracked-ai-edit"))):
            for index in range(6):
                status, payload = self._post(
                    "/api/gen/ai-edit/jobs", body,
                    "ai-edit-tracked-comp-%02d" % index)
                self.assertEqual(500, status)
                self.assertEqual("submission_compensated", payload.get("code"))

        with closing(self.core.jdb()) as db:
            pending = db.execute(
                "SELECT COUNT(*) FROM submission_idempotency WHERE response_json LIKE ?",
                ('{"_paid_submission_preparation"%',)).fetchone()[0]
        self.assertEqual(0, pending)

    def test_batch_admission_lease_blocks_a_stale_parallel_validator(self):
        entered = threading.Event()
        release = threading.Event()
        validation_calls = []
        first_result = []

        def slow_rejection(*args, **kwargs):
            validation_calls.append(1)
            entered.set()
            release.wait(timeout=3)
            raise ValueError("stale validator rejected")

        def first_request():
            first_result.append(self._post(
                "/api/gen/video/batch", self._batch_body(),
                "batch-admission-lease-race"))

        with patch.object(video, "validate_video_batch_payload", side_effect=slow_rejection):
            thread = threading.Thread(target=first_request)
            thread.start()
            self.assertTrue(entered.wait(timeout=2))
            contender_status, contender = self._post(
                "/api/gen/video/batch", self._batch_body(),
                "batch-admission-lease-race")
            release.set()
            thread.join(timeout=3)

        self.assertEqual(409, contender_status)
        self.assertEqual("idempotency_in_progress", contender.get("code"))
        self.assertEqual(400, first_result[0][0])
        self.assertEqual(1, len(validation_calls))
        self.assertEqual(0, self.points.actual_deductions)

    def test_expired_batch_admission_rejection_releases_the_key_without_auth(self):
        from content_domains import submission_idempotency

        body = self._batch_body()
        route = "/api/gen/video/batch"
        key = "batch-expired-admission-reject"
        state, _response = self.core._idempotency_begin("fang", route, key, body)
        self.assertEqual("new", state)
        self.assertTrue(submission_idempotency.claim_admission(
            self.core.jdb, "fang", route, key, body, "dead-owner"))
        with closing(self.core.jdb()) as db:
            row = db.execute(
                "SELECT response_json FROM submission_idempotency WHERE idem_key=?", (key,)).fetchone()
            marker = json.loads(row["response_json"])
            marker["_paid_submission_admission"]["expires_at"] = 0
            db.execute(
                "UPDATE submission_idempotency SET response_json=? WHERE idem_key=?",
                (json.dumps(marker), key))
            db.commit()

        with patch.object(
                video, "validate_video_batch_payload",
                side_effect=ValueError("asset disappeared before Auth")):
            status, _payload = self._post("/api/gen/video/batch", body, key)

        self.assertEqual(400, status)
        self.assertEqual(0, self.points.actual_deductions)
        with closing(self.core.jdb()) as db:
            self.assertEqual(0, db.execute(
                "SELECT COUNT(*) FROM submission_idempotency WHERE idem_key=?", (key,)).fetchone()[0])

    def test_expired_batch_validator_cannot_resurrect_after_takeover_rejects(self):
        from content_domains import submission_idempotency

        body = self._batch_body()
        key = "batch-expired-owner-resurrection"
        entered = threading.Event()
        release = threading.Event()
        call_lock = threading.Lock()
        call_count = []
        first_result = []
        real_validate = video.validate_video_batch_payload

        def staged_validation(*args, **kwargs):
            with call_lock:
                call_count.append(1)
                call_number = len(call_count)
            if call_number == 1:
                entered.set()
                release.wait(timeout=3)
                return real_validate(*args, **kwargs)
            raise ValueError("takeover owner rejected mutable input")

        def first_request():
            first_result.append(self._post(
                "/api/gen/video/batch", body, key))

        with patch.object(
                video, "validate_video_batch_payload",
                side_effect=staged_validation):
            thread = threading.Thread(target=first_request)
            thread.start()
            self.assertTrue(entered.wait(timeout=2))
            with closing(self.core.jdb()) as db:
                row = db.execute(
                    "SELECT response_json FROM submission_idempotency WHERE idem_key=?",
                    (key,)).fetchone()
                marker = json.loads(row["response_json"])
                marker["_paid_submission_admission"]["expires_at"] = 0
                db.execute(
                    "UPDATE submission_idempotency SET response_json=? WHERE idem_key=?",
                    (json.dumps(marker), key))
                db.commit()
            try:
                takeover_status, _payload = self._post(
                    "/api/gen/video/batch", body, key)
            finally:
                release.set()
            thread.join(timeout=3)

        self.assertFalse(thread.is_alive())
        self.assertEqual(400, takeover_status)
        self.assertEqual(409, first_result[0][0])
        self.assertEqual(0, self.points.actual_deductions)
        self.assertEqual(0, len(self.points.calls))
        with closing(self.core.jdb()) as db:
            row = db.execute(
                "SELECT response_json FROM submission_idempotency WHERE idem_key=?",
                (key,)).fetchone()
            self.assertIsNotNone(row)
            self.assertIsNone(row["response_json"])
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])

    def test_stale_admission_owner_cannot_delete_recreated_processing_row(self):
        from content_domains import submission_idempotency

        body = self._batch_body()
        route = "/api/gen/video/batch"
        key = "batch-stale-owner-cleanup"
        state, _response = self.core._idempotency_begin("fang", route, key, body)
        self.assertEqual("new", state)
        self.assertTrue(submission_idempotency.claim_admission(
            self.core.jdb, "fang", route, key, body, "stale-owner"))
        with closing(self.core.jdb()) as db:
            row = db.execute(
                "SELECT response_json FROM submission_idempotency WHERE idem_key=?",
                (key,)).fetchone()
            marker = json.loads(row["response_json"])
            marker["_paid_submission_admission"]["expires_at"] = 0
            db.execute(
                "UPDATE submission_idempotency SET response_json=? WHERE idem_key=?",
                (json.dumps(marker), key))
            db.commit()
        self.assertTrue(submission_idempotency.claim_admission(
            self.core.jdb, "fang", route, key, body, "takeover-owner"))
        self.assertTrue(submission_idempotency.abort_unprepared(
            self.core.jdb, "fang", route, key, owner="takeover-owner"))
        state, _response = self.core._idempotency_begin("fang", route, key, body)
        self.assertEqual("new", state)

        self.assertFalse(submission_idempotency.abort_unprepared(
            self.core.jdb, "fang", route, key, owner="stale-owner"))
        with closing(self.core.jdb()) as db:
            row = db.execute(
                "SELECT response_json FROM submission_idempotency WHERE idem_key=?",
                (key,)).fetchone()
        self.assertIsNotNone(row)
        self.assertIsNone(row["response_json"])

    def test_batch_waiter_discards_snapshot_aborted_by_definitive_auth_rejection(self):
        class SignalingLock:
            def __init__(self):
                self.lock = threading.Lock()
                self.contender_waiting = threading.Event()

            def __enter__(self):
                if self.lock.locked():
                    self.contender_waiting.set()
                self.lock.acquire()
                return self

            def __exit__(self, *_args):
                self.lock.release()
                return False

        gate = SignalingLock()
        auth_entered = threading.Event()
        first_result = []
        waiter_result = []
        body = self._batch_body()
        key = "batch-stale-preparation-after-auth-reject"

        def definitive_rejection(*_args, **_kwargs):
            auth_entered.set()
            gate.contender_waiting.wait(timeout=3)
            raise self.points.AuthPointsError(402, "definitive rejection")

        with patch.object(self.core, "_submission_lock", gate), patch.object(
                self.points, "deduct_points",
                side_effect=definitive_rejection) as deduct:
            first = threading.Thread(target=lambda: first_result.append(
                self._post("/api/gen/video/batch", body, key)))
            first.start()
            self.assertTrue(auth_entered.wait(timeout=2))
            waiter = threading.Thread(target=lambda: waiter_result.append(
                self._post("/api/gen/video/batch", body, key)))
            waiter.start()
            self.assertTrue(gate.contender_waiting.wait(timeout=2))
            first.join(timeout=3)
            waiter.join(timeout=3)

        self.assertFalse(first.is_alive())
        self.assertFalse(waiter.is_alive())
        self.assertEqual(402, first_result[0][0])
        self.assertEqual(409, waiter_result[0][0])
        self.assertEqual(1, deduct.call_count)
        self.assertEqual(0, self.points.actual_deductions)
        with closing(self.core.jdb()) as db:
            row = db.execute(
                "SELECT response_json FROM submission_idempotency WHERE idem_key=?",
                (key,)).fetchone()
            self.assertIsNotNone(row)
            self.assertIsNone(row["response_json"])
            self.assertEqual(0, db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])

    def test_changed_body_conflicts_while_auth_result_is_unknown(self):
        self.points.drop_next_response = True
        first_status, _first = self._post(
            "/api/gen/video/batch", self._batch_body("original"),
            "batch-auth-unknown-conflict")
        self.assertEqual(502, first_status)
        calls_before_conflict = len(self.points.calls)

        status, payload = self._post(
            "/api/gen/video/batch", self._batch_body("changed"),
            "batch-auth-unknown-conflict")
        self.assertEqual(409, status)
        self.assertEqual("idempotency_conflict", payload.get("code"))
        self.assertEqual(calls_before_conflict, len(self.points.calls))

    def test_auth_transaction_conflict_is_409_for_initial_and_batch(self):
        cases = [
            ("/api/gen/video/batch", self._batch_body(), "batch-auth-409"),
            ("/api/gen/ai-edit/jobs", {"source_material_ids": [1]}, "ai-edit-auth-409"),
        ]
        for path, body, key in cases:
            with self.subTest(path=path):
                self._clear_submission_state()
                self.points.reject_status = 409
                status, payload = self._post(path, body, key)
                self.assertEqual(409, status)
                self.assertEqual("points_transaction_conflict", payload.get("code"))
                self.assertEqual(0, self.points.actual_deductions)
                with closing(self.core.jdb()) as db:
                    self.assertEqual(0, db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
                    self.assertEqual(
                        0, db.execute("SELECT COUNT(*) FROM submission_idempotency").fetchone()[0])
                self.points.reject_status = None

    def test_definitive_auth_rejection_aborts_without_jobs_or_idempotency_state(self):
        for path, body, key in (
                ("/api/gen/video/batch", self._batch_body(), "batch-auth-402"),
                ("/api/gen/ai-edit/jobs",
                 {"source_video_asset_id": 7, "style_id": "auto", "materials": []},
                 "ai-edit-auth-402")):
            with self.subTest(path=path):
                self._clear_submission_state()
                self.points.reject_status = 402
                status, payload = self._post(path, body, key)
                self.points.reject_status = None
                self.assertEqual(402, status)
                self.assertIn("definitive rejection", payload.get("detail", ""))
                with closing(self.core.jdb()) as db:
                    self.assertEqual(0, db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
                    self.assertEqual(
                        0, db.execute("SELECT COUNT(*) FROM submission_idempotency").fetchone()[0])
                self.assertEqual(0, self.points.actual_deductions)


class VideoSingleRouteSubLimitTests(unittest.TestCase):
    def test_single_video_routes_use_kind_specific_caps_before_deduct(self):
        from content_domains import core

        class FakePointsError(Exception):
            def __init__(self, status, detail):
                self.status, self.detail = status, detail

        class FakePoints:
            AuthPointsError = FakePointsError

            def __init__(self):
                self.deductions = []

            def cost_of(self, kind, body):
                return 20

            def deduct_points(self, username, cost, reason):
                self.deductions.append((username, cost, reason))
                return 100 - cost

            def safe_refund_points(self, username, cost, reason):
                return 100

            def refund_points(self, username, cost, reason, transaction_key=""):
                return 100

        originals = {
            "JOB_DB": core.JOB_DB,
            "AUDIO_DB": core.AUDIO_DB,
            "_domains": core._domains,
            "verify": core.verify,
            "require_enabled": core.feature_flags.require_enabled,
            "max_active": core.MAX_USER_ACTIVE_JOBS,
            "max_xiaole": core.MAX_USER_ACTIVE_XIAOLE_VIDEO,
            "max_tryon": core.MAX_USER_ACTIVE_TRYON,
            "handlers": core.HANDLERS,
            "enqueue": core.enqueue_job,
            "validate_video": video.validate_video_payload,
            "validate_tryon": video.validate_tryon_payload,
            "validate_xiaole": video.validate_xiaole_video_payload,
            "record_pending": video.record_video_pending_asset,
        }
        fake = FakePoints()
        server = None
        with tempfile.TemporaryDirectory() as td:
            core.JOB_DB = str(pathlib.Path(td) / "jobs.db")
            core.AUDIO_DB = str(pathlib.Path(td) / "assets.db")
            core.verify = lambda token: {"username": "fang", "must_change": False}
            core.feature_flags.require_enabled = lambda kind: None
            core.MAX_USER_ACTIVE_JOBS = 5
            core.MAX_USER_ACTIVE_XIAOLE_VIDEO = 2
            core.MAX_USER_ACTIVE_TRYON = 1
            core.HANDLERS = {"video": lambda body: body, "tryon": lambda body: body, "xiaole_video": lambda body: body}
            video.validate_video_payload = lambda body, username: body
            video.validate_tryon_payload = lambda body: body
            video.validate_xiaole_video_payload = lambda body: body
            try:
                with closing(sqlite3.connect(core.JOB_DB)) as db:
                    db.execute("""CREATE TABLE jobs(id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT,username TEXT,cost INTEGER,
                        status TEXT DEFAULT 'pending',payload TEXT,result TEXT,error TEXT,created_at INTEGER,updated_at INTEGER,
                        deleted INTEGER DEFAULT 0, refunded INTEGER DEFAULT 0, owner TEXT)""")
                    db.commit()
                core.init_audio_db()
                core._domains = lambda: (None, fake, video)
                server = ThreadingHTTPServer(("127.0.0.1", 0), core.H)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                base = "http://127.0.0.1:%d" % server.server_address[1]

                cases = [
                    {
                        "seed": [
                            ("xiaole_video", "pending", '{"channel":"omni"}'),
                            ("xiaole_video", "running", '{"channel":"grok"}'),
                        ],
                        "path": "/api/gen/xiaole_video",
                        "body": {"channel": "micro", "prompt": "商品展示"},
                        "detail": "当前果肉/豆姐/欧米视频最多同时排队或生成 2 个任务，请等待部分完成后再继续",
                        "code": "xiaole_active_cap",
                    },
                    {
                        "seed": [
                            ("tryon", "running", '{"line":"2"}'),
                        ],
                        "path": "/api/gen/tryon",
                        "body": {"line": "2", "text": "换装"},
                        "detail": "当前换装视频最多同时排队或生成 1 个任务，请等待任务完成后再继续",
                        "code": "tryon_active_cap",
                    },
                ]

                for case in cases:
                    with self.subTest(path=case["path"]):
                        with closing(core.jdb()) as db:
                            db.execute("DELETE FROM jobs")
                            for idx, (kind, status, payload) in enumerate(case["seed"], start=1):
                                db.execute("INSERT INTO jobs(id,kind,username,cost,status,payload,created_at,updated_at,deleted,refunded) VALUES(?,?,?,?,?,?,?,?,0,0)",
                                           (idx, kind, "fang", 20, status, payload, 1, 1))
                            db.commit()
                        before = list(fake.deductions)
                        req = urllib.request.Request(base + case["path"], data=json.dumps(case["body"]).encode("utf-8"), method="POST", headers={
                            "Authorization": "Bearer test", "Content-Type": "application/json",
                        })
                        with self.assertRaises(urllib.error.HTTPError) as rejected:
                            urllib.request.urlopen(req, timeout=5)
                        self.assertEqual(429, rejected.exception.code)
                        payload = json.loads(rejected.exception.read().decode("utf-8"))
                        self.assertEqual(case["detail"], payload["detail"])
                        self.assertEqual(case["code"], payload["code"])
                        self.assertEqual(before, fake.deductions)

                enqueued = []
                core.enqueue_job = lambda *args: (enqueued.append(args), True)[1]
                video.record_video_pending_asset = lambda *args: (_ for _ in ()).throw(RuntimeError("asset db locked"))
                request = urllib.request.Request(base + "/api/gen/video", data=json.dumps({
                    "mode": "text", "text": "商品口播", "voice": "demo",
                }).encode("utf-8"), method="POST", headers={
                    "Authorization": "Bearer test", "Content-Type": "application/json",
                })
                with self.assertRaises(urllib.error.HTTPError) as failed:
                    urllib.request.urlopen(request, timeout=5)
                self.assertEqual(500, failed.exception.code)
                with closing(core.jdb()) as db:
                    row = db.execute("SELECT status,refunded FROM jobs ORDER BY id DESC LIMIT 1").fetchone()
                self.assertEqual(("error", 1), (row["status"], row["refunded"]))
                self.assertEqual([], enqueued, "资产登记失败的付费任务绝不能继续入队")

                with urllib.request.urlopen(base + "/api/gen/health", timeout=5) as response:
                    health = json.loads(response.read())
                self.assertEqual(core.JOB_QUEUE_MAX, health["job_queue_max"])
                self.assertEqual(core.TALKING_JOB_QUEUE_MAX, health["talking_job_queue_max"])
                self.assertEqual(2, health["max_user_active_xiaole_video"])
                self.assertEqual(1, health["max_user_active_tryon"])
            finally:
                if server:
                    server.shutdown()
                    server.server_close()
                core.JOB_DB = originals["JOB_DB"]
                core.AUDIO_DB = originals["AUDIO_DB"]
                core._domains = originals["_domains"]
                core.verify = originals["verify"]
                core.feature_flags.require_enabled = originals["require_enabled"]
                core.MAX_USER_ACTIVE_JOBS = originals["max_active"]
                core.MAX_USER_ACTIVE_XIAOLE_VIDEO = originals["max_xiaole"]
                core.MAX_USER_ACTIVE_TRYON = originals["max_tryon"]
                core.HANDLERS = originals["handlers"]
                core.enqueue_job = originals["enqueue"]
                video.validate_video_payload = originals["validate_video"]
                video.validate_tryon_payload = originals["validate_tryon"]
                video.validate_xiaole_video_payload = originals["validate_xiaole"]
                video.record_video_pending_asset = originals["record_pending"]


if __name__ == "__main__":
    unittest.main()
