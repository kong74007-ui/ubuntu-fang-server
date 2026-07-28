import json
import os
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

from server.content_domains import ai_edit_v2_billing as billing
from server.content_domains import ai_edit_v2_store as store
from server.content_domains import ai_edit_v2_pipeline as pipeline
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


class AlwaysUnknownSettlementPoints(FakePoints):
    def refund_points(self, username, amount, reason="", transaction_key=None):
        self.calls.append(("refund", username, amount, transaction_key))
        if transaction_key and transaction_key.endswith(":settlement"):
            raise points.AuthPointsError(502, "provider result unknown")
        return super().refund_points(username, amount, reason, transaction_key)


class SelectiveUnknownDeductPoints(FakePoints):
    def __init__(self, unknown_key, balance=500):
        super().__init__(balance)
        self.unknown_key = unknown_key

    def deduct_points(self, username, amount, reason="", transaction_key=None):
        if transaction_key == self.unknown_key:
            self.calls.append(("deduct", username, amount, transaction_key))
            raise points.AuthPointsError(502, "provider result unknown")
        return super().deduct_points(username, amount, reason, transaction_key)


class MigrateOnDeductPoints(FakePoints):
    def __init__(self, trigger_key, migrate, balance=500):
        super().__init__(balance)
        self.trigger_key = trigger_key
        self.migrate = migrate
        self.migrated = False
        self.lock = threading.Lock()

    def deduct_points(self, username, amount, reason="", transaction_key=None):
        with self.lock:
            result = super().deduct_points(
                username, amount, reason, transaction_key
            )
            if transaction_key == self.trigger_key and not self.migrated:
                self.migrated = True
                self.migrate()
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

    def _prepare_pending_migration_race(self):
        with closing(store.open_store(self.db_path)) as conn:
            conn.execute("DROP INDEX idx_edit_v2_jobs_successor")
            conn.execute("UPDATE edit_v2_schema_meta SET version=8 WHERE id=1")
            for job_id, predecessor, status, created_at in (
                ("predecessor", None, "render_failed", 0),
                ("winner", "predecessor", "completed", 1),
                ("pending-loser", "predecessor", "precharging", 2),
                ("good-second", None, "precharging", 3),
            ):
                conn.execute(
                    """INSERT INTO edit_v2_jobs(
                           id,owner,idempotency_key,quote_id,predecessor_job_id,
                           status,payload_json,checkpoint_json,created_at,updated_at
                       ) VALUES(?,?,?,?,?,?,'{}','[]',?,?)""",
                    (job_id, "alice", f"key-{job_id}", f"quote-{job_id}",
                     predecessor, status, created_at, created_at),
                )
            for job_id in ("pending-loser", "good-second"):
                conn.execute(
                    """INSERT INTO edit_v2_billing(
                           job_id,transaction_key,operation,amount,status,
                           created_at,updated_at
                       ) VALUES(?,?,'hold',40,'pending',1,1)""",
                    (job_id, f"ai-edit-v2:{job_id}:hold"),
                )

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

    def test_migration_refund_pending_is_reconciled_by_existing_failure_refund(self):
        fake_points = FakePoints()
        job = store.create_job(
            "alice", {"draft": draft()}, "quote", "duplicate-loser", 1,
            uuid_factory=lambda: "123e4567-e89b-42d3-a456-426614174089",
        )
        with closing(store.open_store(self.db_path)) as conn:
            conn.execute(
                "UPDATE edit_v2_jobs SET status='storage_failed' WHERE id=?",
                (job["id"],),
            )
            conn.execute(
                """INSERT INTO edit_v2_billing(
                       job_id,transaction_key,operation,amount,status,created_at,updated_at
                   ) VALUES(?,?,'hold',40,'refund_pending',1,1)""",
                (job["id"], "hold-duplicate-loser"),
            )

        recovered = pipeline.reconcile_terminal_refunds(
            now=2, db_path=self.db_path, points_client=fake_points
        )

        self.assertEqual(recovered, 1)
        self.assertEqual(fake_points.balance, 540)
        with closing(store.open_store(self.db_path)) as conn:
            status = conn.execute(
                "SELECT status FROM edit_v2_billing WHERE job_id=?",
                (job["id"],),
            ).fetchone()["status"]
        self.assertEqual(status, "refunded")

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
            quote["expires_at"] + 1,
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

    def test_settlement_intent_is_durable_before_lost_provider_response(self):
        quote = billing.create_quote(
            "alice", draft(), now=100, uuid_factory=lambda: "quote-intent"
        )
        fake_points = LoseFirstResponsePoints()
        billing.precharge_and_create_job(
            "alice", {"draft": draft()}, quote["id"], "request-intent", 101,
            points_client=FakePoints(), uuid_factory=lambda: "job-intent",
        )
        actual = quote["min_points"]

        with self.assertRaises(points.AuthPointsError):
            billing.settle_success(
                "job-intent", actual, 200, points_client=fake_points
            )

        with closing(store.open_store(self.db_path)) as conn:
            row = conn.execute(
                "SELECT status,response_json FROM edit_v2_billing WHERE job_id='job-intent'"
            ).fetchone()
        intent = json.loads(row["response_json"])
        self.assertEqual(row["status"], "settling")
        self.assertEqual(intent["operation"], "settlement")
        self.assertEqual(intent["transaction_key"], "ai-edit-v2:job-intent:settlement")
        self.assertEqual(intent["actual_points"], actual)
        self.assertEqual(intent["refunded_points"], quote["max_points"] - actual)
        self.assertEqual(intent["provider_operation_status"], "pending")

    def test_quarantined_settling_loser_replays_then_compensates_exact_charge_once(self):
        fake_points = LoseFirstResponsePoints(balance=400)
        job = store.create_job(
            "alice", {"draft": draft()}, "quote", "settling-loser", 1,
            uuid_factory=lambda: "job-settling-loser",
        )
        intent = {
            "operation": "settlement",
            "transaction_key": "ai-edit-v2:job-settling-loser:settlement",
            "held_points": 100,
            "actual_points": 70,
            "refunded_points": 30,
            "provider_operation_status": "pending",
        }
        with closing(store.open_store(self.db_path)) as conn:
            conn.execute(
                """UPDATE edit_v2_jobs
                   SET status='storage_failed',error_code='duplicate_successor_quarantined'
                   WHERE id=?""",
                (job["id"],),
            )
            conn.execute(
                """INSERT INTO edit_v2_billing(
                       job_id,transaction_key,operation,amount,status,response_json,
                       created_at,updated_at
                   ) VALUES(?,?,'hold',100,'settling',?,1,1)""",
                (job["id"], "ai-edit-v2:job-settling-loser:hold", json.dumps(intent)),
            )

        # Simulate the settlement-difference refund succeeding remotely while its
        # response is lost. Reconciliation must replay that key before compensating.
        with self.assertRaises(points.AuthPointsError):
            fake_points.refund_points(
                "alice", 30, transaction_key="ai-edit-v2:job-settling-loser:settlement"
            )
        self.assertEqual(fake_points.balance, 430)

        self.assertEqual(pipeline.reconcile_terminal_refunds(
            now=2, db_path=self.db_path, points_client=fake_points
        ), 0)
        self.assertEqual(pipeline.reconcile_terminal_refunds(
            now=3, db_path=self.db_path, points_client=fake_points
        ), 1)
        self.assertEqual(pipeline.reconcile_terminal_refunds(
            now=4, db_path=self.db_path, points_client=fake_points
        ), 0)
        self.assertEqual(fake_points.balance, 500)
        refund_keys = [call[3] for call in fake_points.calls if call[0] == "refund"]
        self.assertEqual(set(refund_keys), {
            "ai-edit-v2:job-settling-loser:settlement",
            "ai-edit-v2:job-settling-loser:failure-refund",
        })
        with closing(store.open_store(self.db_path)) as conn:
            row = conn.execute(
                "SELECT status,response_json FROM edit_v2_billing WHERE job_id=?",
                (job["id"],),
            ).fetchone()
        self.assertEqual(row["status"], "refunded")
        self.assertEqual(json.loads(row["response_json"])["refunded_points"], 100)

    def test_unknown_settlement_loser_does_not_block_other_terminal_refunds(self):
        fake_points = AlwaysUnknownSettlementPoints(balance=400)
        for job_id, status, response in (
            ("unknown-settlement", "settling", {
                "operation": "settlement",
                "transaction_key": "ai-edit-v2:unknown-settlement:settlement",
                "held_points": 100, "actual_points": 70, "refunded_points": 30,
                "provider_operation_status": "pending",
            }),
            ("ordinary-refund", "refund_pending", None),
        ):
            store.create_job(
                "alice", {"draft": draft()}, "quote", job_id, 1,
                uuid_factory=lambda value=job_id: value,
            )
            with closing(store.open_store(self.db_path)) as conn:
                conn.execute(
                    """UPDATE edit_v2_jobs
                       SET status='storage_failed',error_code='duplicate_successor_quarantined'
                       WHERE id=?""",
                    (job_id,),
                )
                conn.execute(
                    """INSERT INTO edit_v2_billing(
                           job_id,transaction_key,operation,amount,status,response_json,
                           created_at,updated_at
                       ) VALUES(?,?,'hold',100,?,?,1,1)""",
                    (job_id, f"ai-edit-v2:{job_id}:hold", status,
                     json.dumps(response) if response else None),
                )

        self.assertEqual(pipeline.reconcile_terminal_refunds(
            now=2, db_path=self.db_path, points_client=fake_points
        ), 1)
        with closing(store.open_store(self.db_path)) as conn:
            statuses = dict(conn.execute(
                "SELECT job_id,status FROM edit_v2_billing ORDER BY job_id"
            ).fetchall())
        self.assertEqual(statuses["unknown-settlement"], "settling")
        self.assertEqual(statuses["ordinary-refund"], "refunded")
        self.assertIn(
            "ai-edit-v2:unknown-settlement:settlement",
            [call[3] for call in fake_points.calls],
        )

    def test_quarantined_already_settled_loser_refunds_only_recorded_actual_charge(self):
        fake_points = FakePoints(balance=425)
        job = store.create_job(
            "alice", {"draft": draft()}, "quote", "settled-loser", 1,
            uuid_factory=lambda: "job-settled-loser",
        )
        with closing(store.open_store(self.db_path)) as conn:
            conn.execute(
                """UPDATE edit_v2_jobs
                   SET status='storage_failed',error_code='duplicate_successor_quarantined'
                   WHERE id=?""",
                (job["id"],),
            )
            conn.execute(
                """INSERT INTO edit_v2_billing(
                       job_id,transaction_key,operation,amount,status,response_json,
                       created_at,updated_at
                   ) VALUES(?,?,'hold',100,'settled',?,1,1)""",
                (job["id"], "ai-edit-v2:job-settled-loser:hold", json.dumps({
                    "held_points": 100, "actual_points": 75,
                    "refunded_points": 25, "points_after": 425,
                })),
            )

        self.assertEqual(pipeline.reconcile_terminal_refunds(
            now=2, db_path=self.db_path, points_client=fake_points
        ), 1)
        self.assertEqual(pipeline.reconcile_terminal_refunds(
            now=3, db_path=self.db_path, points_client=fake_points
        ), 0)
        self.assertEqual(fake_points.balance, 500)
        self.assertEqual(
            [call[2:] for call in fake_points.calls if call[0] == "refund"],
            [(75, "ai-edit-v2:job-settled-loser:failure-refund")],
        )

    def test_unknown_pending_loser_does_not_block_other_precharge_reconciliation(self):
        unknown_key = "ai-edit-v2:unknown-pending:hold"
        fake_points = SelectiveUnknownDeductPoints(unknown_key, balance=500)
        for job_id in ("unknown-pending", "known-pending"):
            store.create_job(
                "alice", {"draft": draft()}, "quote", job_id, 1,
                uuid_factory=lambda value=job_id: value,
            )
            with closing(store.open_store(self.db_path)) as conn:
                conn.execute(
                    """UPDATE edit_v2_jobs
                       SET status='storage_failed',error_code='duplicate_successor_quarantined'
                       WHERE id=?""",
                    (job_id,),
                )
                conn.execute(
                    """INSERT INTO edit_v2_billing(
                           job_id,transaction_key,operation,amount,status,created_at,updated_at
                       ) VALUES(?,?,'hold',40,'pending',1,1)""",
                    (job_id, f"ai-edit-v2:{job_id}:hold"),
                )

        self.assertEqual(billing.reconcile_pending_precharges(
            2, db_path=self.db_path, points_client=fake_points
        ), 1)
        with closing(store.open_store(self.db_path)) as conn:
            statuses = dict(conn.execute(
                "SELECT job_id,status FROM edit_v2_billing ORDER BY job_id"
            ).fetchall())
        self.assertEqual(statuses["unknown-pending"], "pending")
        self.assertEqual(statuses["known-pending"], "refund_pending")
        self.assertEqual(pipeline.reconcile_terminal_refunds(
            now=3, db_path=self.db_path, points_client=fake_points
        ), 1)
        self.assertEqual(fake_points.balance, 500)

    def test_migration_during_pending_deduct_refunds_loser_and_processes_good_second(self):
        self._prepare_pending_migration_race()
        fake_points = MigrateOnDeductPoints(
            "ai-edit-v2:pending-loser:hold",
            lambda: store.init_db(self.db_path),
        )

        recovered = billing.reconcile_pending_precharges(
            2, db_path=self.db_path, points_client=fake_points
        )

        self.assertEqual(recovered, 2)
        with closing(store.open_store(self.db_path)) as conn:
            jobs = dict(conn.execute(
                "SELECT id,status FROM edit_v2_jobs WHERE id IN (?,?)",
                ("pending-loser", "good-second"),
            ).fetchall())
            bills = dict(conn.execute(
                "SELECT job_id,status FROM edit_v2_billing ORDER BY id"
            ).fetchall())
        self.assertEqual(jobs, {
            "pending-loser": "storage_failed",
            "good-second": "queued",
        })
        self.assertEqual(bills, {
            "pending-loser": "refund_pending",
            "good-second": "held",
        })
        self.assertEqual(
            billing.reconcile_pending_precharges(
                3, db_path=self.db_path, points_client=fake_points
            ),
            0,
        )
        self.assertEqual(pipeline.reconcile_terminal_refunds(
            now=4, db_path=self.db_path, points_client=fake_points
        ), 1)
        self.assertEqual(fake_points.balance, 460)

    def test_concurrent_repeated_pending_reconcile_does_not_double_charge_or_miss_refund(self):
        self._prepare_pending_migration_race()
        fake_points = MigrateOnDeductPoints(
            "ai-edit-v2:pending-loser:hold",
            lambda: store.init_db(self.db_path),
        )
        barrier = threading.Barrier(2)

        def reconcile():
            barrier.wait(timeout=5)
            return billing.reconcile_pending_precharges(
                2, db_path=self.db_path, points_client=fake_points
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(reconcile) for _ in range(2)]
            recovered = [future.result(timeout=15) for future in futures]

        self.assertEqual(sum(recovered), 2)
        self.assertEqual(billing.reconcile_pending_precharges(
            3, db_path=self.db_path, points_client=fake_points
        ), 0)
        self.assertEqual(pipeline.reconcile_terminal_refunds(
            now=4, db_path=self.db_path, points_client=fake_points
        ), 1)
        self.assertEqual(pipeline.reconcile_terminal_refunds(
            now=5, db_path=self.db_path, points_client=fake_points
        ), 0)
        self.assertEqual(fake_points.balance, 460)
        self.assertEqual(set(fake_points.transactions), {
            "ai-edit-v2:pending-loser:hold",
            "ai-edit-v2:good-second:hold",
            "ai-edit-v2:pending-loser:failure-refund",
        })

    def test_queue_state_race_is_isolated_and_good_second_still_queues(self):
        self._prepare_pending_migration_race()
        fake_points = FakePoints()
        original_queue = billing._queue_held_job

        def migrate_before_queue(job_id, now, db_path=None):
            if job_id == "pending-loser":
                store.init_db(self.db_path)
            return original_queue(job_id, now, db_path)

        with patch.object(
            billing, "_queue_held_job", side_effect=migrate_before_queue
        ):
            recovered = billing.reconcile_pending_precharges(
                2, db_path=self.db_path, points_client=fake_points
            )

        self.assertEqual(recovered, 1)
        with closing(store.open_store(self.db_path)) as conn:
            jobs = dict(conn.execute(
                "SELECT id,status FROM edit_v2_jobs WHERE id IN (?,?)",
                ("pending-loser", "good-second"),
            ).fetchall())
            bills = dict(conn.execute(
                "SELECT job_id,status FROM edit_v2_billing ORDER BY id"
            ).fetchall())
        self.assertEqual(jobs["pending-loser"], "storage_failed")
        self.assertEqual(jobs["good-second"], "queued")
        self.assertEqual(bills["pending-loser"], "refund_pending")
        self.assertEqual(bills["good-second"], "held")

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

    def test_concurrent_settlement_and_failure_refund_choose_one_terminal_operation(self):
        quote = billing.create_quote(
            "alice", draft(), now=100, uuid_factory=lambda: "quote-race"
        )
        fake_points = FakePoints()
        billing.precharge_and_create_job(
            "alice", {"draft": draft()}, quote["id"], "request-race", 101,
            points_client=fake_points, uuid_factory=lambda: "job-race",
        )
        barrier = threading.Barrier(2)

        def settle():
            barrier.wait(timeout=5)
            try:
                return ("settled", billing.settle_success(
                    "job-race", quote["min_points"], 200, points_client=fake_points
                ))
            except billing.BillingError as exc:
                return (exc.code, None)

        def refund():
            barrier.wait(timeout=5)
            try:
                return ("refunded", billing.refund_failure(
                    "job-race", 200, points_client=fake_points
                ))
            except billing.BillingError as exc:
                return (exc.code, None)

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = [executor.submit(settle), executor.submit(refund)]
            outcomes = [future.result(timeout=10) for future in outcomes]

        with closing(store.open_store(self.db_path)) as conn:
            status = conn.execute(
                "SELECT status FROM edit_v2_billing WHERE job_id='job-race'"
            ).fetchone()[0]
        self.assertIn(status, {"settled", "refunded"})
        self.assertIn("billing_operation_conflict", {item[0] for item in outcomes})
        terminal_keys = {
            call[3] for call in fake_points.calls
            if call[0] == "refund" and call[3] in {
                "ai-edit-v2:job-race:settlement",
                "ai-edit-v2:job-race:failure-refund",
            }
        }
        self.assertEqual(len(terminal_keys), 1)

    def test_invalid_actual_cost_does_not_strand_the_hold_in_settling(self):
        quote = billing.create_quote(
            "alice", draft(), now=100, uuid_factory=lambda: "quote-invalid-actual"
        )
        fake_points = FakePoints()
        billing.precharge_and_create_job(
            "alice", {"draft": draft()}, quote["id"], "request-invalid-actual", 101,
            points_client=fake_points, uuid_factory=lambda: "job-invalid-actual",
        )

        with self.assertRaisesRegex(billing.BillingError, "actual_points_invalid"):
            billing.settle_success(
                "job-invalid-actual", quote["max_points"] + 1, 200,
                points_client=fake_points,
            )

        with closing(store.open_store(self.db_path)) as conn:
            status = conn.execute(
                "SELECT status FROM edit_v2_billing WHERE job_id='job-invalid-actual'"
            ).fetchone()[0]
        self.assertEqual(status, "held")


if __name__ == "__main__":
    unittest.main()
