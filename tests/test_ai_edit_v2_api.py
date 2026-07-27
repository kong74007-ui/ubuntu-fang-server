import os
import json
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

server_dir = str(Path(__file__).resolve().parents[1] / "server")
if server_dir not in sys.path:
    sys.path.insert(0, server_dir)

from server.content_domains import ai_edit_v2_api as api
from server.content_domains import ai_edit_v2_store as store


MB = 1024 * 1024


def valid_api_draft():
    return {
        "creation_mode": "natural_brief",
        "brief": "保留原任务输入",
        "language": "zh-CN",
        "aspect_ratio": "16:9",
        "target_duration_ms": 30_000,
        "main_input": {
            "asset_id": "main",
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


class FakeHandler:
    def __init__(self, body=None, token="token"):
        self.body = body or {}
        self.token = token
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
        self.head = {
            "content_length": 12 * MB,
            "content_type": "video/mp4",
            "etag": "verified-etag",
        }

    def presign_put(self, object_key, content_type, expires=900):
        return f"https://upload.example/{object_key}?signature=private"

    def head_object(self, object_key):
        return dict(self.head)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "ai_edit_v2.db")
        self.env = patch.dict(os.environ, {"AI_EDIT_V2_DB": self.db_path})
        self.env.start()
        store.init_db(self.db_path)
        self.fake_cos = FakeCos()
        self.cos_patch = patch.object(api, "cos", self.fake_cos)
        self.cos_patch.start()
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
        self.points_patch.stop()
        self.uuid_patch.stop()
        self.cos_patch.stop()
        self.env.stop()
        self.temp_dir.cleanup()

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
        with closing(store.open_store(self.db_path)) as conn:
            count = conn.execute("SELECT COUNT(*) FROM edit_v2_materials").fetchone()[0]
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

    def test_capabilities_and_empty_templates_are_provider_neutral(self):
        status, capabilities = self._dispatch(
            "GET", "/api/v2/edit/capabilities"
        )
        templates_status, templates = self._dispatch(
            "GET", "/api/v2/edit/templates"
        )

        self.assertEqual(status, 200)
        self.assertEqual(templates_status, 200)
        self.assertEqual(templates, {"items": []})
        self.assertEqual(capabilities["version"], "2.0")
        self.assertNotIn("provider", str(capabilities).lower())

    def test_quote_route_creates_dynamic_quote(self):
        draft = {
            "creation_mode": "open_generation",
            "brief": "中文测试",
            "language": "zh-CN",
            "aspect_ratio": "9:16",
            "target_duration_ms": 30_000,
            "main_input": {
                "asset_id": "main",
                "kind": "audio",
                "size_bytes": MB,
                "duration_ms": 10_000,
            },
            "required_materials": [],
            "reference_materials": [],
        }

        status, payload = self._dispatch(
            "POST", "/api/v2/edit/quotes", {"draft": draft}
        )

        self.assertEqual(status, 201)
        self.assertGreater(payload["quote"]["max_points"], 0)

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
        self.assertEqual(payload["status"], "created")
        self.assertEqual(payload["held_points"], quote_payload["quote"]["max_points"])

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
        self.assertEqual(other_status, 404)

    def test_retry_creates_an_idempotent_successor_without_reviving_old_job(self):
        old = store.create_job(
            "alice",
            {"draft": valid_api_draft()},
            "old-quote",
            "old-request",
            100,
            uuid_factory=lambda: "123e4567-e89b-42d3-a456-426614174099",
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
                "SELECT status,payload_json,quote_id FROM edit_v2_jobs WHERE id=?",
                (first["job_id"],),
            ).fetchone()
        self.assertEqual(old_row["status"], "render_failed")
        self.assertEqual(successor["status"], "created")
        self.assertEqual(json.loads(successor["payload_json"]), {"draft": valid_api_draft()})
        self.assertNotEqual(successor["quote_id"], "old-quote")

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


if __name__ == "__main__":
    unittest.main()
