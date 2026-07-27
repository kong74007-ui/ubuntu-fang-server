import os
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

server_dir = str(Path(__file__).resolve().parents[1] / "server")
if server_dir not in sys.path:
    sys.path.insert(0, server_dir)

from server.content_domains import ai_edit_v2_billing as billing
from server.content_domains import ai_edit_v2_store as store
from server.content_domains import points


def draft(**overrides):
    value = {
        "creation_mode": "natural_brief",
        "brief": "制作一条中文产品介绍",
        "language": "zh-CN",
        "aspect_ratio": "16:9",
        "target_duration_ms": 60_000,
        "main_input": {
            "asset_id": "main",
            "kind": "video",
            "size_bytes": 10_000,
            "duration_ms": 90_000,
        },
        "required_materials": [],
        "reference_materials": [],
    }
    value.update(overrides)
    return value


class FakePoints:
    def __init__(self, balance=500):
        self.balance = balance
        self.transactions = {}
        self.calls = []

    def deduct_points(self, username, amount, reason="", transaction_key=None):
        self.calls.append(("deduct", username, amount, transaction_key))
        if transaction_key in self.transactions:
            return self.transactions[transaction_key]
        if self.balance < amount:
            raise points.AuthPointsError(402, "点数不足")
        self.balance -= amount
        self.transactions[transaction_key] = self.balance
        return self.balance

    def refund_points(self, username, amount, reason="", transaction_key=None):
        self.calls.append(("refund", username, amount, transaction_key))
        if transaction_key in self.transactions:
            return self.transactions[transaction_key]
        self.balance += amount
        self.transactions[transaction_key] = self.balance
        return self.balance


class LoseFirstResponsePoints(FakePoints):
    def __init__(self, balance=500):
        super().__init__(balance)
        self.lost_operations = set()

    def deduct_points(self, username, amount, reason="", transaction_key=None):
        result = super().deduct_points(username, amount, reason, transaction_key)
        marker = ("deduct", transaction_key)
        if marker not in self.lost_operations:
            self.lost_operations.add(marker)
            raise points.AuthPointsError(502, "response lost")
        return result

    def refund_points(self, username, amount, reason="", transaction_key=None):
        result = super().refund_points(username, amount, reason, transaction_key)
        marker = ("refund", transaction_key)
        if marker not in self.lost_operations:
            self.lost_operations.add(marker)
            raise points.AuthPointsError(502, "response lost")
        return result

class BillingTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "ai_edit_v2.db")
        self.env = patch.dict(os.environ, {"AI_EDIT_V2_DB": self.db_path})
        self.env.start()
        store.init_db(self.db_path)

    def tearDown(self):
        self.env.stop()
        self.temp_dir.cleanup()

    def test_quote_contains_dynamic_range_breakdown_version_and_expiry(self):
        quote = billing.create_quote(
            "alice", draft(), now=100, uuid_factory=lambda: "quote-1"
        )

        self.assertEqual(quote["id"], "quote-1")
        self.assertGreater(quote["min_points"], 0)
        self.assertGreaterEqual(quote["max_points"], quote["min_points"])
        self.assertIn("base", quote["breakdown"])
        self.assertEqual(quote["price_version"], "ai-edit-v2-price-v1")
        self.assertGreater(quote["expires_at"], 100)

    def test_quote_rejects_expiry_owner_or_changed_draft(self):
        quote = billing.create_quote(
            "alice", draft(), now=100, uuid_factory=lambda: "quote-1"
        )
        cases = (
            ("bob", draft(), 101, "quote_owner_mismatch"),
            ("alice", draft(brief="被修改"), 101, "quote_draft_mismatch"),
            ("alice", draft(), quote["expires_at"] + 1, "quote_expired"),
        )

        for owner, changed, now, code in cases:
            with self.subTest(code=code), self.assertRaises(billing.BillingError) as caught:
                billing.validate_quote(owner, changed, quote["id"], now)
            self.assertEqual(caught.exception.code, code)

    def test_precharge_is_idempotent_and_success_settlement_refunds_difference_once(self):
        quote = billing.create_quote(
            "alice", draft(), now=100, uuid_factory=lambda: "quote-1"
        )
        fake_points = FakePoints()

        first = billing.precharge_and_create_job(
            "alice",
            {"draft": draft()},
            quote["id"],
            "request-1",
            101,
            points_client=fake_points,
            uuid_factory=lambda: "job-1",
        )
        replay = billing.precharge_and_create_job(
            "alice",
            {"draft": draft()},
            quote["id"],
            "request-1",
            102,
            points_client=fake_points,
            uuid_factory=lambda: "should-not-be-used",
        )

        self.assertEqual(first["job"]["id"], "job-1")
        self.assertEqual(replay["job"]["id"], "job-1")
        deduct_keys = {call[3] for call in fake_points.calls if call[0] == "deduct"}
        self.assertEqual(deduct_keys, {"ai-edit-v2:job-1:hold"})
        held = quote["max_points"]
        actual = quote["min_points"]
        billing.settle_success("job-1", actual, 200, points_client=fake_points)
        billing.settle_success("job-1", actual, 201, points_client=fake_points)
        self.assertEqual(fake_points.balance, 500 - actual)
        refund_calls = [call for call in fake_points.calls if call[0] == "refund"]
        self.assertTrue(refund_calls)
        self.assertEqual({call[3] for call in refund_calls}, {"ai-edit-v2:job-1:settlement"})
        with closing(store.open_store(self.db_path)) as conn:
            row = conn.execute(
                "SELECT status,amount,response_json FROM edit_v2_billing WHERE job_id='job-1'"
            ).fetchone()
        self.assertEqual(row["status"], "settled")
        self.assertEqual(row["amount"], held)

    def test_failed_job_refunds_full_hold_once(self):
        quote = billing.create_quote(
            "alice", draft(), now=100, uuid_factory=lambda: "quote-2"
        )
        fake_points = FakePoints()
        billing.precharge_and_create_job(
            "alice",
            {"draft": draft()},
            quote["id"],
            "request-2",
            101,
            points_client=fake_points,
            uuid_factory=lambda: "job-2",
        )

        billing.refund_failure("job-2", 200, points_client=fake_points)
        billing.refund_failure("job-2", 201, points_client=fake_points)

        self.assertEqual(fake_points.balance, 500)
        self.assertEqual(
            {call[3] for call in fake_points.calls if call[0] == "refund"},
            {"ai-edit-v2:job-2:failure-refund"},
        )

    def test_points_adapter_forwards_optional_transaction_key(self):
        captured = []

        def request(path, payload=None, method="POST"):
            captured.append((path, payload))
            return {"points": 88}

        with patch.object(points, "_auth_points_request", side_effect=request):
            self.assertEqual(
                points.deduct_points("alice", 12, "hold", transaction_key="tx-1"), 88
            )
            self.assertEqual(
                points.refund_points("alice", 3, "settle", transaction_key="tx-2"), 88
            )

        self.assertEqual(captured[0][1]["transaction_key"], "tx-1")
        self.assertEqual(captured[1][1]["transaction_key"], "tx-2")

    def test_lost_auth_responses_replay_without_double_deduct_or_refund(self):
        quote = billing.create_quote(
            "alice", draft(), now=100, uuid_factory=lambda: "quote-crash"
        )
        fake_points = LoseFirstResponsePoints()
        kwargs = {
            "owner": "alice",
            "payload": {"draft": draft()},
            "quote_id": quote["id"],
            "idempotency_key": "request-crash",
            "points_client": fake_points,
            "uuid_factory": lambda: "job-crash",
        }

        with self.assertRaises(billing.PrechargePending) as pending:
            billing.precharge_and_create_job(now=101, **kwargs)
        self.assertEqual(pending.exception.job_id, "job-crash")
        billing.precharge_and_create_job(now=102, **kwargs)
        self.assertEqual(fake_points.balance, 500 - quote["max_points"])

        with self.assertRaises(points.AuthPointsError):
            billing.settle_success(
                "job-crash", quote["min_points"], 200, points_client=fake_points
            )
        billing.settle_success(
            "job-crash", quote["min_points"], 201, points_client=fake_points
        )
        self.assertEqual(fake_points.balance, 500 - quote["min_points"])

    def test_definitive_precharge_rejection_is_terminal_and_never_reconciled(self):
        quote = billing.create_quote(
            "alice", draft(), now=100, uuid_factory=lambda: "quote-rejected"
        )
        fake_points = FakePoints(balance=0)
        kwargs = {
            "owner": "alice",
            "payload": {"draft": draft()},
            "quote_id": quote["id"],
            "idempotency_key": "request-rejected",
            "points_client": fake_points,
            "uuid_factory": lambda: "job-rejected",
        }

        for now in (101, 102):
            with self.assertRaises(points.AuthPointsError) as caught:
                billing.precharge_and_create_job(now=now, **kwargs)
            self.assertEqual(caught.exception.status, 402)

        with closing(store.open_store(self.db_path)) as conn:
            bill = conn.execute(
                "SELECT status FROM edit_v2_billing WHERE job_id='job-rejected'"
            ).fetchone()
            job = conn.execute(
                "SELECT status FROM edit_v2_jobs WHERE id='job-rejected'"
            ).fetchone()
        self.assertEqual(bill["status"], "rejected")
        self.assertEqual(job["status"], "validation_failed")
        self.assertEqual(
            billing.reconcile_pending_precharges(
                103, points_client=FakePoints(balance=500), db_path=self.db_path
            ),
            0,
        )

    def test_price_versions_require_preview_and_explicit_publish_confirmation(self):
        config = billing.default_price_config()
        config["base_points"] = 31
        price_draft = billing.create_price_draft(
            "operator", "price-test-v2", config, 100, pricing_db_path=self.db_path
        )
        self.assertEqual(price_draft["status"], "draft")
        preview = billing.preview_price_config(config, draft())
        self.assertEqual(preview["breakdown"]["base"], 31)
        with self.assertRaisesRegex(billing.BillingError, "publish_confirmation_required"):
            billing.publish_price_version(
                "operator", "price-test-v2", "确认", 101, pricing_db_path=self.db_path
            )
        published = billing.publish_price_version(
            "operator", "price-test-v2", "发布 price-test-v2", 102,
            pricing_db_path=self.db_path,
        )
        self.assertEqual(published["status"], "published")
        with self.assertRaisesRegex(billing.BillingError, "price_version_immutable"):
            billing.publish_price_version(
                "operator", "price-test-v2", "发布 price-test-v2", 103,
                pricing_db_path=self.db_path,
            )

    def test_quote_uses_latest_published_price_version_and_keeps_it(self):
        config = billing.default_price_config()
        config["base_points"] = 33
        billing.create_price_draft(
            "operator", "price-live-v2", config, 100, pricing_db_path=self.db_path
        )
        billing.publish_price_version(
            "operator", "price-live-v2", "发布 price-live-v2", 101,
            pricing_db_path=self.db_path,
        )
        quote = billing.create_quote(
            "alice", draft(), 102, db_path=self.db_path,
            pricing_db_path=self.db_path,
        )
        self.assertEqual(quote["price_version"], "price-live-v2")
        self.assertEqual(quote["breakdown"]["base"], 33)


if __name__ == "__main__":
    unittest.main()
