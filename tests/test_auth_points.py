import os
import json
import sqlite3
import tempfile
import threading
import urllib.error
import urllib.request
import unittest
from contextlib import closing
from http.server import ThreadingHTTPServer
from unittest.mock import patch


class AuthPointsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = os.environ.get("HQ_TEST_AUTH_DB")
        os.environ["HQ_TEST_AUTH_DB"] = os.path.join(self.tmp.name, "users.db")

        import importlib
        import server.auth_server as auth_server

        self.auth = importlib.reload(auth_server)
        self.auth.DB = os.environ["HQ_TEST_AUTH_DB"]
        self.auth.INTERNAL_TOKEN = "test-internal-token"
        self.auth.init_db()
        c = sqlite3.connect(self.auth.DB)
        try:
            c.execute(
                "INSERT INTO users(username,pw_hash,pw_salt,display_name,points,role,must_change) "
                "VALUES('fang','h','s','fang',10,'member',0)"
            )
            c.execute(
                "UPDATE users SET membership_tier='experience',membership_started_at=1,membership_expires_at=4102444800 "
                "WHERE username='fang'"
            )
            c.commit()
        finally:
            c.close()

    def tearDown(self):
        if self.old_db is None:
            os.environ.pop("HQ_TEST_AUTH_DB", None)
        else:
            os.environ["HQ_TEST_AUTH_DB"] = self.old_db
        self.tmp.cleanup()

    def test_deduct_is_atomic_and_rejects_insufficient_points(self):
        points, err = self.auth.deduct_points("fang", 7)
        self.assertIsNone(err)
        self.assertEqual(points["points"], 3)

        points, err = self.auth.deduct_points("fang", 4)
        self.assertIsNone(points)
        self.assertEqual(err, "insufficient")
        self.assertEqual(self.auth.get_points_row("fang")["points"], 3)

    def test_refund_adds_points(self):
        points, err = self.auth.refund_points("fang", 5)
        self.assertIsNone(err)
        self.assertEqual(points["points"], 15)

    def test_transaction_key_makes_deduct_and_refund_idempotent(self):
        first, err = self.auth.deduct_points("fang", 4, "v2 hold", "job-1:hold")
        replay, replay_err = self.auth.deduct_points("fang", 4, "v2 hold replay", "job-1:hold")

        self.assertIsNone(err)
        self.assertIsNone(replay_err)
        self.assertEqual(first["points"], 6)
        self.assertEqual(replay["points"], 6)
        self.assertEqual(self.auth.get_points_row("fang")["points"], 6)

        refunded, refund_err = self.auth.refund_points("fang", 4, "v2 refund", "job-1:refund")
        refund_replay, refund_replay_err = self.auth.refund_points(
            "fang", 4, "v2 refund replay", "job-1:refund"
        )
        self.assertIsNone(refund_err)
        self.assertIsNone(refund_replay_err)
        self.assertEqual(refunded["points"], 10)
        self.assertEqual(refund_replay["points"], 10)
        self.assertEqual(self.auth.get_points_row("fang")["points"], 10)

        with closing(sqlite3.connect(self.auth.DB)) as conn:
            audit_count = conn.execute(
                "SELECT COUNT(*) FROM points_audit WHERE username='fang'"
            ).fetchone()[0]
            transaction_count = conn.execute(
                "SELECT COUNT(*) FROM points_transactions WHERE username='fang'"
            ).fetchone()[0]
        self.assertEqual(audit_count, 2)
        self.assertEqual(transaction_count, 2)

    def test_transaction_key_conflict_does_not_change_points(self):
        _, err = self.auth.deduct_points("fang", 4, "hold", "same-key")
        conflict, conflict_err = self.auth.deduct_points("fang", 5, "changed", "same-key")

        self.assertIsNone(err)
        self.assertIsNone(conflict)
        self.assertEqual(conflict_err, "transaction_conflict")
        self.assertEqual(self.auth.get_points_row("fang")["points"], 6)
    def test_refund_transaction_key_is_idempotent(self):
        first, first_err = self.auth.refund_points("fang", 5, "job#42", "job-refund:42")
        replay, replay_err = self.auth.refund_points("fang", 5, "job#42", "job-refund:42")

        self.assertIsNone(first_err)
        self.assertIsNone(replay_err)
        self.assertEqual(first["points"], 15)
        self.assertEqual(replay["points"], 15)
        c = sqlite3.connect(self.auth.DB)
        try:
            self.assertEqual(c.execute(
                "SELECT COUNT(*) FROM points_audit WHERE transaction_key='job-refund:42'"
            ).fetchone()[0], 1)
        finally:
            c.close()

    def test_refund_transaction_key_rejects_different_amount(self):
        self.auth.refund_points("fang", 5, "job#42", "job-refund:42")
        points, err = self.auth.refund_points("fang", 6, "job#43", "job-refund:42")

        self.assertIsNone(points)
        self.assertEqual(err, "transaction_conflict")
        self.assertEqual(self.auth.get_points_row("fang")["points"], 15)

    def test_refund_accepts_shared_transaction_key_limit(self):
        for length in (161, 200):
            with self.subTest(length=length):
                points, err = self.auth.refund_points(
                    "fang", 1, "boundary refund", "r" * length
                )
                self.assertIsNone(err)
                self.assertIsNotNone(points)

    def test_http_refund_transaction_key_boundary_is_200_characters(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), self.auth.H)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:%d" % server.server_address[1]

        def post(key):
            request = urllib.request.Request(
                base + "/api/auth/points/refund",
                data=json.dumps({
                    "username": "fang", "amount": 1,
                    "reason": "boundary refund", "transaction_key": key,
                }).encode(),
                headers={
                    "Content-Type": "application/json",
                    "X-HQ-Internal-Token": "test-internal-token",
                }, method="POST")
            return urllib.request.urlopen(request, timeout=3)

        try:
            key = "r" * 200
            with post(key) as response:
                self.assertEqual(11, json.loads(response.read())["points"])
            with post(key) as response:
                self.assertEqual(11, json.loads(response.read())["points"])
            with self.assertRaises(urllib.error.HTTPError) as caught:
                post("r" * 201)
            self.assertEqual(400, caught.exception.code)
            self.assertEqual("invalid transaction_key", json.loads(caught.exception.read())["detail"])
            with closing(sqlite3.connect(self.auth.DB)) as conn:
                self.assertEqual(11, conn.execute(
                    "SELECT points FROM users WHERE username='fang'").fetchone()[0])
                self.assertEqual(1, conn.execute(
                    "SELECT COUNT(*) FROM points_transactions WHERE operation='refund' AND transaction_key=?",
                    (key,)).fetchone()[0])
                self.assertEqual(0, conn.execute(
                    "SELECT COUNT(*) FROM points_transactions WHERE length(transaction_key)=201").fetchone()[0])
                self.assertEqual(1, conn.execute(
                    "SELECT COUNT(*) FROM points_audit WHERE transaction_key=?", (key,)).fetchone()[0])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_wechat_transaction_can_only_approve_one_recharge_order(self):
        first, first_err = self.auth.create_recharge_order("fang", 99, 1000, "微信扫码充值")
        second, second_err = self.auth.create_recharge_order("fang", 99, 1000, "微信扫码充值")
        self.assertIsNone(first_err)
        self.assertIsNone(second_err)

        _, approve_err = self.auth.review_recharge_order(
            "wxpay", first["order_id"], "approve", "paid",
            transaction_id="wx-transaction-1", pay_channel="wxpay",
        )
        duplicate, duplicate_err = self.auth.review_recharge_order(
            "wxpay", second["order_id"], "approve", "paid",
            transaction_id="wx-transaction-1", pay_channel="wxpay",
        )

        self.assertIsNone(approve_err)
        self.assertIsNone(duplicate)
        self.assertEqual(duplicate_err, "transaction_in_use")
        self.assertEqual(self.auth.get_points_row("fang")["points"], 1010)
        c = sqlite3.connect(self.auth.DB)
        try:
            row = c.execute(
                "SELECT transaction_id,pay_channel FROM recharge_orders WHERE order_id=?",
                (first["order_id"],),
            ).fetchone()
            self.assertEqual(row, ("wx-transaction-1", "wxpay"))
        finally:
            c.close()

    def test_wechat_callback_identity_must_match_merchant_and_app(self):
        import server.wxpay as wxpay

        expected = {"appid": "wx-huangque", "mchid": "merchant-huangque"}
        with patch.object(wxpay, "_config", return_value=expected):
            self.assertTrue(wxpay.payment_identity_matches(expected))
            self.assertFalse(wxpay.payment_identity_matches({
                "appid": "wx-other", "mchid": "merchant-huangque",
            }))
            self.assertFalse(wxpay.payment_identity_matches({
                "appid": "wx-huangque", "mchid": "merchant-other",
            }))

    def test_concurrent_deduct_never_overdraws(self):
        results = []
        lock = threading.Lock()

        def worker():
            points, err = self.auth.deduct_points("fang", 1)
            with lock:
                results.append((points, err))

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        ok = [r for r in results if r[1] is None]
        insufficient = [r for r in results if r[1] == "insufficient"]
        self.assertEqual(len(ok), 10)
        self.assertEqual(len(insufficient), 10)
        self.assertEqual(self.auth.get_points_row("fang")["points"], 0)

    def test_http_points_endpoints_require_internal_token(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), self.auth.H)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:%d" % server.server_address[1]
        try:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(base + "/api/auth/points?username=fang", timeout=3)
            self.assertEqual(ctx.exception.code, 403)

            req = urllib.request.Request(
                base + "/api/auth/points/deduct",
                data=json.dumps({"username": "fang", "amount": 4}).encode(),
                headers={
                    "Content-Type": "application/json",
                    "X-HQ-Internal-Token": "test-internal-token",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=3) as r:
                data = json.loads(r.read())
            self.assertEqual(data["points"], 6)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_http_transaction_resolution_precedes_expired_membership_gate(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), self.auth.H)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = "http://127.0.0.1:%d/api/auth/points/deduct" % server.server_address[1]

        def request(amount, transaction_key="http-hold-1"):
            return urllib.request.Request(
                url,
                data=json.dumps(
                    {
                        "username": "fang",
                        "amount": amount,
                        "transaction_key": transaction_key,
                    }
                ).encode(),
                headers={
                    "Content-Type": "application/json",
                    "X-HQ-Internal-Token": "test-internal-token",
                },
                method="POST",
            )

        try:
            with urllib.request.urlopen(request(4), timeout=3) as response:
                first = json.loads(response.read())
            with closing(sqlite3.connect(self.auth.DB)) as conn:
                conn.execute(
                    "UPDATE users SET membership_expires_at=1 WHERE username='fang'"
                )
                conn.commit()
            with urllib.request.urlopen(request(4), timeout=3) as response:
                replay = json.loads(response.read())
            self.assertEqual(first["points"], 6)
            self.assertEqual(replay["points"], 6)
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request(5), timeout=3)
            self.assertEqual(caught.exception.code, 409)
            with self.assertRaises(urllib.error.HTTPError) as membership_required:
                urllib.request.urlopen(request(4, "http-hold-new"), timeout=3)
            self.assertEqual(membership_required.exception.code, 403)
            self.assertEqual(self.auth.get_points_row("fang")["points"], 6)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_login_sets_http_only_cookie_without_plaintext_token_body(self):
        self.auth.create_user("cookie_user", "secret123", 5)
        server = ThreadingHTTPServer(("127.0.0.1", 0), self.auth.H)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:%d" % server.server_address[1]
        try:
            req = urllib.request.Request(
                base + "/api/auth/login",
                data=json.dumps({"username": "cookie_user", "password": "secret123"}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=3) as r:
                data = json.loads(r.read())
                cookie = r.headers.get("Set-Cookie") or ""

            self.assertNotIn("token", data)
            self.assertEqual(data["user"]["username"], "cookie_user")
            self.assertIn("HttpOnly", cookie)
            self.assertIn(self.auth.AUTH_COOKIE_NAME + "=", cookie)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_miniprogram_login_returns_token_usable_as_bearer(self):
        self.auth.create_user("mp_user", "secret123", 5)
        server = ThreadingHTTPServer(("127.0.0.1", 0), self.auth.H)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:%d" % server.server_address[1]
        try:
            req = urllib.request.Request(
                base + "/api/auth/miniprogram-login",
                data=json.dumps({"username": "mp_user", "password": "secret123"}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=3) as r:
                data = json.loads(r.read())

            self.assertIn("token", data)
            self.assertEqual(data["user"]["username"], "mp_user")

            req2 = urllib.request.Request(
                base + "/api/auth/me",
                headers={"Authorization": "Bearer " + data["token"]},
            )
            with urllib.request.urlopen(req2, timeout=3) as r:
                me_data = json.loads(r.read())
            self.assertEqual(me_data["user"]["username"], "mp_user")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_miniprogram_register_returns_token_and_creates_user(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), self.auth.H)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:%d" % server.server_address[1]
        try:
            req = urllib.request.Request(
                base + "/api/auth/miniprogram-register",
                data=json.dumps({"username": "mp_new", "password": "secret123"}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=3) as r:
                data = json.loads(r.read())

            self.assertIn("token", data)
            self.assertEqual(data["user"]["username"], "mp_new")
            self.assertEqual(data["user"]["points"], self.auth.NEW_USER_TRIAL_POINTS)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


class PointsTransactionTests(unittest.TestCase):
    setUp = AuthPointsTests.setUp
    tearDown = AuthPointsTests.tearDown

    def scalar(self, query, params=()):
        with closing(sqlite3.connect(self.auth.DB)) as conn:
            return conn.execute(query, params).fetchone()[0]

    def test_transaction_query_is_owner_bound_and_read_only(self):
        self.auth.create_user("alice", "secret123", 20)
        self.auth.create_user("bob", "secret123", 20)
        self.auth.deduct_points("alice", 12, "v3", "ai-edit-v3:j1:pre_debit")
        before = self.scalar("SELECT COUNT(*) FROM points_audit")
        row = self.auth.get_points_transaction("alice", "ai-edit-v3:j1:pre_debit")
        self.assertEqual(row["operation"], "deduct")
        self.assertEqual(row["amount"], 12)
        self.assertIsNone(
            self.auth.get_points_transaction("bob", "ai-edit-v3:j1:pre_debit")
        )
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM points_audit"), before)


class PointsTransactionHttpTests(unittest.TestCase):
    def setUp(self):
        AuthPointsTests.setUp(self)
        self.auth.create_user("alice", "secret123", 20)
        self.auth.create_user("bob", "secret123", 20)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.auth.H)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:%d" % self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        AuthPointsTests.tearDown(self)

    def post(self, payload, token="test-internal-token"):
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["X-HQ-Internal-Token"] = token
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        request = urllib.request.Request(
            self.base + "/api/auth/points/transaction",
            data=data,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=3) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_transaction_query_requires_internal_token_before_body_parsing(self):
        status, _ = self.post(b"{", token=None)
        self.assertEqual(status, 403)

    def test_transaction_query_rejects_malformed_key(self):
        status, body = self.post({
            "username": "alice",
            "transaction_key": "x" * 201,
        })
        self.assertEqual(status, 400)
        self.assertEqual(body["detail"], "invalid transaction_key")

    def test_transaction_query_returns_not_found_for_absent_row(self):
        status, body = self.post({
            "username": "alice",
            "transaction_key": "ai-edit-v3:absent:pre_debit",
        })
        self.assertEqual(status, 200)
        self.assertEqual(body, {"found": False})

    def test_transaction_query_is_owner_bound_over_http(self):
        self.auth.deduct_points("alice", 12, "v3", "ai-edit-v3:j1:pre_debit")
        status, body = self.post({
            "username": "bob",
            "transaction_key": "ai-edit-v3:j1:pre_debit",
        })
        self.assertEqual(status, 200)
        self.assertEqual(body, {"found": False})

    def test_transaction_query_returns_found_row(self):
        self.auth.deduct_points("alice", 12, "v3", "ai-edit-v3:j1:pre_debit")
        status, body = self.post({
            "username": "alice",
            "transaction_key": "ai-edit-v3:j1:pre_debit",
        })
        self.assertEqual(status, 200)
        self.assertTrue(body["found"])
        self.assertEqual(body["transaction"]["transaction_key"], "ai-edit-v3:j1:pre_debit")
        self.assertEqual(body["transaction"]["operation"], "deduct")
        self.assertEqual(body["transaction"]["username"], "alice")
        self.assertEqual(body["transaction"]["amount"], 12)
        self.assertEqual(body["transaction"]["points_after"], 8)
        self.assertIsInstance(body["transaction"]["created_at"], int)

    def test_content_client_preserves_transport_errors(self):
        from server.content_domains import points

        with patch.object(points, "AUTH_INTERNAL_TOKEN", "test-internal-token"):
            with patch.object(
                points.urllib.request,
                "urlopen",
                side_effect=urllib.error.URLError("auth unavailable"),
            ):
                with self.assertRaises(points.AuthPointsError) as caught:
                    points.get_points_transaction(
                        "alice", "ai-edit-v3:j1:pre_debit"
                    )
        self.assertEqual(caught.exception.status, 502)


if __name__ == "__main__":
    unittest.main()
