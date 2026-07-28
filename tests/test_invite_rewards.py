import importlib
import http.cookiejar
import json
import os
import tempfile
import threading
import types
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch


class InviteRewardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = os.environ.get("HQ_TEST_AUTH_DB")
        os.environ["HQ_TEST_AUTH_DB"] = os.path.join(self.tmp.name, "users.db")
        import server.auth_server as auth_server

        self.auth = importlib.reload(auth_server)
        self.auth.DB = os.environ["HQ_TEST_AUTH_DB"]
        self.auth.AUTH_COOKIE_SECURE = False
        self.auth.init_db()
        self.auth.create_user("admin", "secret123", 0, "admin")
        self.auth.create_user("inviter", "secret123", 88)
        self.now = 1800000000
        user, err = self.auth.set_membership_admin(
            "admin", "inviter", "partner", "测试邀请人", now=self.now,
        )
        self.assertIsNone(err)
        self.assertEqual(user["membership_tier"], "partner")

    def tearDown(self):
        if self.old_db is None:
            os.environ.pop("HQ_TEST_AUTH_DB", None)
        else:
            os.environ["HQ_TEST_AUTH_DB"] = self.old_db
        self.tmp.cleanup()

    def _connect(self):
        c = self.auth.db()
        c.row_factory = __import__("sqlite3").Row
        return c

    def _user_id(self, c, username):
        return c.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()[0]

    def _invite_code(self):
        c = self._connect()
        try:
            row = self.auth.invites.ensure_user_code(c, self._user_id(c, "inviter"), now=self.now)
            c.commit()
            return row["code"]
        finally:
            c.close()

    def _register_bound(self, username):
        created, err = self.auth.register_account(
            username, "secret123", invite_code=self._invite_code())
        self.assertIsNone(err)
        return created

    def _set_inviter_state(self, *, expires_at=None, account_status=None):
        assignments = []
        params = []
        if expires_at is not None:
            assignments.append("membership_expires_at=?")
            params.append(int(expires_at))
        if account_status is not None:
            assignments.append("account_status=?")
            params.append(str(account_status))
        c = self._connect()
        try:
            c.execute(
                "UPDATE users SET %s WHERE username='inviter'" % ",".join(assignments),
                tuple(params))
            c.commit()
        finally:
            c.close()

    def _assert_membership_upgrade_was_not_written(self, username):
        c = self._connect()
        try:
            row = c.execute(
                "SELECT id,membership_tier FROM users WHERE username=?", (username,)).fetchone()
            self.assertEqual("", row["membership_tier"] or "")
            self.assertEqual(0, c.execute(
                "SELECT COUNT(*) FROM membership_audit WHERE username=?", (username,)).fetchone()[0])
            self.assertEqual(0, c.execute(
                "SELECT COUNT(*) FROM membership_upgrade_records WHERE user_id=?", (row["id"],)).fetchone()[0])
            self.assertEqual(0, c.execute(
                """SELECT COUNT(*) FROM invite_reward_point_records rewards
                     JOIN membership_upgrade_records upgrades ON upgrades.id=rewards.upgrade_record_id
                    WHERE upgrades.user_id=?""", (row["id"],)).fetchone()[0])
        finally:
            c.close()

    def test_partner_rewards_are_non_stacking_and_do_not_change_consumable_points(self):
        code = self._invite_code()
        first, err = self.auth.register_account("first", "secret123", invite_code=code)
        self.assertIsNone(err)
        first_points = first["user"]["points"]

        _, err = self.auth.set_membership_admin(
            "admin", "first", "experience", "先升体验官", now=self.now + 1,
        )
        self.assertIsNone(err)
        _, err = self.auth.set_membership_admin(
            "admin", "first", "partner", "再升合伙人", now=self.now + 2,
        )
        self.assertIsNone(err)
        _, err = self.auth.set_membership_admin(
            "admin", "first", "partner", "重复设置", now=self.now + 3,
        )
        self.assertIsNone(err)

        second, err = self.auth.register_account("second", "secret123", invite_code=code)
        self.assertIsNone(err)
        _, err = self.auth.set_membership_admin(
            "admin", "second", "partner", "直接升合伙人", now=self.now + 4,
        )
        self.assertIsNone(err)

        c = self._connect()
        try:
            inviter_id = self._user_id(c, "inviter")
            rewards = self.auth.invites.reward_points(c, inviter_id)
            self.assertEqual(rewards["total_reward_points"], 3000)
            self.assertEqual(rewards["total"], 3)
            first_records = [r for r in rewards["records"] if r["invitee_username"] == "first"]
            self.assertEqual(sorted(r["reward_points"] for r in first_records), [240, 1260])
            self.assertEqual(max(r["reward_total_after"] for r in first_records), 1500)
            second_record = next(r for r in rewards["records"] if r["invitee_username"] == "second")
            self.assertEqual(second_record["reward_points"], 1500)
            self.assertEqual(
                c.execute("SELECT points FROM users WHERE username='first'").fetchone()[0],
                first_points,
            )
            self.assertEqual(
                c.execute("SELECT COUNT(*) FROM points_audit WHERE username IN ('first','inviter')").fetchone()[0],
                0,
            )
        finally:
            c.close()

    def test_reward_schema_and_matrix_exist(self):
        c = self._connect()
        try:
            tables = {row[0] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("membership_upgrade_records", tables)
            self.assertIn("invite_reward_point_records", tables)
            self.assertEqual(self.auth.invites.INVITE_REWARD_TOTALS["partner"]["partner"], 1500)
            self.assertEqual(self.auth.invites.INVITE_REWARD_TOTALS["initiator"]["initiator"], 15000)
        finally:
            c.close()

    def test_invited_user_cannot_exceed_direct_inviter_tier(self):
        code = self._invite_code()
        _, err = self.auth.register_account("limited", "secret123", invite_code=code)
        self.assertIsNone(err)
        user, err = self.auth.set_membership_admin(
            "admin", "limited", "partner", "允许同级", now=self.now + 1,
        )
        self.assertIsNone(err)
        self.assertEqual(user["membership_tier"], "partner")
        with self.assertRaises(self.auth.invites.InviteError) as caught:
            self.auth.set_membership_admin(
                "admin", "limited", "initiator", "不允许越级", now=self.now + 2,
            )
        self.assertEqual(caught.exception.code, "invite_membership_limit")

    def test_expired_inviter_cannot_authorize_membership_upgrade(self):
        self._register_bound("expired-limited")
        self._set_inviter_state(expires_at=self.now)

        with self.assertRaises(self.auth.invites.InviteError) as caught:
            self.auth.set_membership_admin(
                "admin", "expired-limited", "experience", "过期邀请人不得授权", now=self.now + 1)

        self.assertEqual("invite_membership_limit", caught.exception.code)
        self.assertEqual(409, caught.exception.http_status)
        self._assert_membership_upgrade_was_not_written("expired-limited")

    def test_banned_inviter_cannot_authorize_membership_upgrade(self):
        self._register_bound("banned-limited")
        self._set_inviter_state(account_status="banned")

        with self.assertRaises(self.auth.invites.InviteError) as caught:
            self.auth.set_membership_admin(
                "admin", "banned-limited", "experience", "封禁邀请人不得授权", now=self.now + 1)

        self.assertEqual("invite_membership_limit", caught.exception.code)
        self.assertEqual(409, caught.exception.http_status)
        self._assert_membership_upgrade_was_not_written("banned-limited")

    def test_invalid_inviter_blocks_all_online_membership_orders_before_payment(self):
        self._register_bound("online-limited")
        self._set_inviter_state(expires_at=1)
        server = ThreadingHTTPServer(("127.0.0.1", 0), self.auth.H)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:%d" % server.server_address[1]
        jar = http.cookiejar.CookieJar()
        client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        try:
            login = urllib.request.Request(
                base + "/api/auth/login",
                data=json.dumps({"username": "online-limited", "password": "secret123"}).encode(),
                headers={"Content-Type": "application/json"}, method="POST")
            client.open(login, timeout=3).close()

            cases = [
                ("/api/auth/recharge/order", {}),
                ("/api/auth/wxpay/native", {}),
                ("/api/auth/wxpay/jsapi", {"js_code": "not-used"}),
            ]
            fake_wxpay = types.SimpleNamespace(configured=lambda: True)
            with patch.object(self.auth, "wxpay", fake_wxpay):
                for path, extra in cases:
                    with self.subTest(path=path):
                        request = urllib.request.Request(
                            base + path,
                            data=json.dumps({
                                "amount": 499, "product_type": "membership_experience", **extra,
                            }).encode(),
                            headers={"Content-Type": "application/json"}, method="POST")
                        with self.assertRaises(urllib.error.HTTPError) as caught:
                            client.open(request, timeout=3)
                        self.assertEqual(409, caught.exception.code)
                        payload = json.loads(caught.exception.read())
                        self.assertEqual("invite_membership_limit", payload.get("code"))
            c = self._connect()
            try:
                self.assertEqual(0, c.execute(
                    "SELECT COUNT(*) FROM recharge_orders WHERE username='online-limited'").fetchone()[0])
            finally:
                c.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_admin_reward_ledger_can_void_and_restore_without_changing_user_points(self):
        code = self._invite_code()
        created, err = self.auth.register_account("ledger-user", "secret123", invite_code=code)
        self.assertIsNone(err)
        before_points = created["user"]["points"]
        _, err = self.auth.set_membership_admin(
            "admin", "ledger-user", "experience", "生成奖励", now=self.now + 1,
        )
        self.assertIsNone(err)
        c = self._connect()
        try:
            ledger = self.auth.invites.admin_reward_points(c)
            self.assertEqual(ledger["recorded_points"], 240)
            reward_id = ledger["items"][0]["id"]
            self.auth.invites.admin_reward_action(c, reward_id, "void", "测试作废", "admin", self.now + 2)
            c.commit()
            ledger = self.auth.invites.admin_reward_points(c)
            self.assertEqual(ledger["recorded_points"], 0)
            self.assertEqual(ledger["voided_points"], 240)
            self.auth.invites.admin_reward_action(c, reward_id, "restore", "测试恢复", "admin", self.now + 3)
            c.commit()
            ledger = self.auth.invites.admin_reward_points(c)
            self.assertEqual(ledger["recorded_points"], 240)
            self.assertEqual(
                c.execute("SELECT points FROM users WHERE username='ledger-user'").fetchone()[0],
                before_points,
            )
        finally:
            c.close()


if __name__ == "__main__":
    unittest.main()
