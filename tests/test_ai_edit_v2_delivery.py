import hashlib
import json
import os
import tempfile
import threading
import unittest
import uuid
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from server.content_domains import ai_edit_v2_billing as billing
from server.content_domains import ai_edit_v2_delivery as delivery
from server.content_domains import ai_edit_v2_store as store
from server.content_domains.ai_edit_v2_quality import QualityReport


PASS_REPORT = QualityReport(True, (), (), False, False)


class FakePoints:
    def __init__(self):
        self.balance = 900
        self.transactions = {}
        self.calls = []
        self.lock = threading.Lock()

    def refund_points(self, username, amount, reason="", transaction_key=None):
        with self.lock:
            self.calls.append(transaction_key)
            if transaction_key not in self.transactions:
                self.balance += amount
                self.transactions[transaction_key] = self.balance
            return self.transactions[transaction_key]


class LoseSettlementResponse(FakePoints):
    def __init__(self):
        super().__init__()
        self.lost = False

    def refund_points(self, *args, **kwargs):
        value = super().refund_points(*args, **kwargs)
        if not self.lost and str(kwargs.get("transaction_key", "")).endswith(":settlement"):
            self.lost = True
            raise RuntimeError("settlement response lost")
        return value


class FakeCos:
    def __init__(self, *, bad_head=False):
        self.objects = {}
        self.bad_head = bad_head
        self.lock = threading.Lock()

    def put_file(self, path, key, content_type, private=True):
        with self.lock:
            self.objects[key] = Path(path).read_bytes()
        return {"ETag": '"uploaded"'}

    def head_object(self, key):
        body = self.objects[key]
        return {"content_length": len(body) + (1 if self.bad_head else 0),
                "content_type": "video/mp4", "etag": "uploaded"}


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.temp.name, "v2.db")
        self.video = os.path.join(self.temp.name, "final.mp4")
        self.assets_db = os.path.join(self.temp.name, "assets.db")
        Path(self.video).write_bytes(b"playable-final-mp4")
        with closing(sqlite3.connect(self.assets_db)) as conn:
            conn.execute("""CREATE TABLE video_assets(
                id INTEGER PRIMARY KEY AUTOINCREMENT,job_id INTEGER UNIQUE,
                username TEXT NOT NULL,mode TEXT NOT NULL,video_file TEXT,
                video_url TEXT,resolution TEXT,ratio TEXT,phase TEXT,
                status TEXT NOT NULL,created_at INTEGER,updated_at INTEGER)""")
        self.env = patch.dict(os.environ, {"AI_EDIT_V2_ASSET_DB": self.assets_db})
        self.env.start()
        store.init_db(self.db)
        self.job_id = str(uuid.uuid4())
        with closing(store.open_store(self.db)) as conn:
            conn.execute(
                "INSERT INTO edit_v2_jobs(id,owner,idempotency_key,quote_id,status,payload_json,checkpoint_json,created_at,updated_at) VALUES(?,?,?,?,?,'{}','[]',1,1)",
                (self.job_id, "alice", "request", "quote", "quality_check"),
            )
            conn.execute(
                "INSERT INTO edit_v2_billing(job_id,transaction_key,operation,amount,status,created_at,updated_at) VALUES(?,?,'hold',100,'held',1,1)",
                (self.job_id, f"ai-edit-v2:{self.job_id}:hold"),
            )
        self.points = FakePoints()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_delivery_settles_and_inserts_asset_once(self):
        fake_cos = FakeCos()
        with patch.object(delivery, "cos", fake_cos), patch.object(billing, "points", self.points):
            first = delivery.deliver(self.job_id, self.video, PASS_REPORT, 42, db_path=self.db)
            replay = delivery.deliver(self.job_id, self.video, PASS_REPORT, 42, db_path=self.db)

        self.assertEqual(replay, first)
        with closing(store.open_store(self.db)) as conn:
            job = conn.execute("SELECT status,output_cos_key FROM edit_v2_jobs WHERE id=?", (self.job_id,)).fetchone()
            assets = conn.execute("SELECT COUNT(*) FROM edit_v2_render_artifacts WHERE job_id=? AND kind='delivery_internal'", (self.job_id,)).fetchone()[0]
            bill = conn.execute("SELECT status,response_json FROM edit_v2_billing WHERE job_id=?", (self.job_id,)).fetchone()
        self.assertEqual(job["status"], "completed")
        self.assertEqual(assets, 1)
        with closing(sqlite3.connect(self.assets_db)) as conn:
            visible = conn.execute("SELECT username,mode,status FROM video_assets WHERE job_id=?", (self.job_id,)).fetchone()
        self.assertEqual(visible, ("alice", "ai_edit_v2", "done"))
        self.assertEqual(bill["status"], "settled")
        self.assertEqual(json.loads(bill["response_json"])["actual_points"], 42)
        self.assertEqual(self.points.calls, [f"ai-edit-v2:{self.job_id}:settlement"])
        owner_hash = hashlib.sha256(b"alice").hexdigest()[:16]
        self.assertTrue(job["output_cos_key"].startswith(f"ai-edit-v2/{owner_hash}/{self.job_id}/delivery/"))

    def test_concurrent_deliver_has_one_asset_and_one_actual_settlement(self):
        fake_cos = FakeCos()
        with patch.object(delivery, "cos", fake_cos), patch.object(billing, "points", self.points):
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(
                    lambda _n: delivery.deliver(self.job_id, self.video, PASS_REPORT, 42, db_path=self.db),
                    range(2),
                ))
        self.assertEqual(results[0], results[1])
        with closing(store.open_store(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM edit_v2_render_artifacts WHERE job_id=? AND kind='delivery_internal'", (self.job_id,)).fetchone()[0], 1)
        self.assertEqual(self.points.calls.count(f"ai-edit-v2:{self.job_id}:settlement"), 1)

    def test_storage_verification_failure_never_settles_success_and_refunds_once(self):
        with patch.object(delivery, "cos", FakeCos(bad_head=True)), patch.object(billing, "points", self.points):
            with self.assertRaisesRegex(delivery.DeliveryError, "storage_verification_failed"):
                delivery.deliver(self.job_id, self.video, PASS_REPORT, 42, db_path=self.db)
            with self.assertRaisesRegex(delivery.DeliveryError, "storage_failed"):
                delivery.deliver(self.job_id, self.video, PASS_REPORT, 42, db_path=self.db)
        with closing(store.open_store(self.db)) as conn:
            job = conn.execute("SELECT status FROM edit_v2_jobs WHERE id=?", (self.job_id,)).fetchone()[0]
            bill = conn.execute("SELECT status FROM edit_v2_billing WHERE job_id=?", (self.job_id,)).fetchone()[0]
        self.assertEqual(job, "storage_failed")
        self.assertEqual(bill, "refunded")
        self.assertEqual(self.points.calls.count(f"ai-edit-v2:{self.job_id}:failure-refund"), 1)

    def test_settling_crash_reconciles_head_and_same_settlement_key(self):
        fake_cos = FakeCos()
        points = LoseSettlementResponse()
        with patch.object(delivery, "cos", fake_cos), patch.object(billing, "points", points):
            with self.assertRaisesRegex(RuntimeError, "settlement response lost"):
                delivery.deliver(self.job_id, self.video, PASS_REPORT, 42,
                                 db_path=self.db, now_fn=lambda: 10)
            self.assertEqual(delivery.reconcile_pending_deliveries(
                11, db_path=self.db, asset_db_path=self.assets_db, cos_api=fake_cos,
            ), 1)
        self.assertEqual(delivery._load(self.job_id, self.db)["status"], "completed")
        self.assertEqual(points.calls.count(f"ai-edit-v2:{self.job_id}:settlement"), 2)
        self.assertEqual(len(fake_cos.objects), 1)

    def test_worker_losing_lease_after_upload_cannot_settle_or_publish_asset(self):
        test = self

        class LeaseStealingCos(FakeCos):
            def put_file(self, *args, **kwargs):
                value = super().put_file(*args, **kwargs)
                with closing(store.open_store(test.db)) as conn:
                    conn.execute(
                        "UPDATE edit_v2_jobs SET lease_owner='new-worker',lease_until=100 WHERE id=?",
                        (test.job_id,),
                    )
                return value

        with closing(store.open_store(self.db)) as conn:
            conn.execute(
                "UPDATE edit_v2_jobs SET lease_owner='old-worker',lease_until=100 WHERE id=?",
                (self.job_id,),
            )
        with patch.object(billing, "points", self.points):
            with self.assertRaisesRegex(delivery.DeliveryError, "delivery_lease_lost"):
                delivery.deliver(
                    self.job_id, self.video, PASS_REPORT, 42, db_path=self.db,
                    worker_id="old-worker", now_fn=lambda: 10,
                    cos_api=LeaseStealingCos(),
                )
        with closing(store.open_store(self.db)) as conn:
            job = conn.execute("SELECT status FROM edit_v2_jobs WHERE id=?", (self.job_id,)).fetchone()[0]
            bill = conn.execute("SELECT status FROM edit_v2_billing WHERE job_id=?", (self.job_id,)).fetchone()[0]
            outbox = conn.execute("SELECT COUNT(*) FROM edit_v2_delivery_outbox WHERE job_id=?", (self.job_id,)).fetchone()[0]
        self.assertEqual((job, bill, outbox), ("quality_check", "held", 0))

    def test_pending_outbox_reconciler_recovers_after_v2_commit_before_asset_write(self):
        fake_cos = FakeCos()
        with patch.object(delivery, "cos", fake_cos), patch.object(billing, "points", self.points), \
             patch.object(delivery, "_dispatch_and_complete", side_effect=BaseException("crash")):
            with self.assertRaisesRegex(BaseException, "crash"):
                delivery.deliver(self.job_id, self.video, PASS_REPORT, 42,
                                 db_path=self.db, now_fn=lambda: 10)
        with closing(store.open_store(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT status FROM edit_v2_jobs WHERE id=?", (self.job_id,)).fetchone()[0], "settling")
            self.assertEqual(conn.execute("SELECT status FROM edit_v2_delivery_outbox WHERE job_id=?", (self.job_id,)).fetchone()[0], "pending")
        with patch.object(delivery, "cos", fake_cos), patch.object(billing, "points", self.points):
            count = delivery.reconcile_pending_deliveries(
                11, db_path=self.db, asset_db_path=self.assets_db, cos_api=fake_cos,
            )
        self.assertEqual(count, 1)
        self.assertEqual(delivery._load(self.job_id, self.db)["status"], "completed")

    def test_reconciler_recovers_when_asset_insert_succeeded_before_v2_finalize(self):
        fake_cos = FakeCos()
        real_write = delivery._write_user_asset

        def write_then_crash(payload, path):
            real_write(payload, path)
            raise BaseException("crash after asset")

        with patch.object(delivery, "cos", fake_cos), patch.object(billing, "points", self.points), \
             patch.object(delivery, "_write_user_asset", side_effect=write_then_crash):
            with self.assertRaisesRegex(BaseException, "crash after asset"):
                delivery.deliver(self.job_id, self.video, PASS_REPORT, 42,
                                 db_path=self.db, now_fn=lambda: 10)
        with patch.object(delivery, "cos", fake_cos), patch.object(billing, "points", self.points):
            self.assertEqual(delivery.reconcile_pending_deliveries(
                11, db_path=self.db, asset_db_path=self.assets_db, cos_api=fake_cos,
            ), 1)
        with closing(sqlite3.connect(self.assets_db)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM video_assets WHERE job_id=?", (self.job_id,)).fetchone()[0], 1)

    def test_video_asset_reuse_rejects_any_immutable_field_conflict_with_integer_schema(self):
        payload = {"job_id": 123, "username": "alice", "video_file": "private/final.mp4",
                   "ratio": "16:9", "created_at": 10}
        asset_id = delivery._write_user_asset(payload, self.assets_db)
        self.assertGreater(asset_id, 0)
        for field, value in (("username", "bob"), ("video_file", "private/other.mp4"),
                             ("ratio", "9:16")):
            with self.subTest(field=field):
                changed = {**payload, field: value}
                with self.assertRaisesRegex(delivery.DeliveryError, "asset_idempotency_conflict"):
                    delivery._write_user_asset(changed, self.assets_db)
        with closing(sqlite3.connect(self.assets_db)) as conn:
            conn.execute("UPDATE video_assets SET mode='legacy' WHERE job_id=123")
            conn.commit()
        with self.assertRaisesRegex(delivery.DeliveryError, "asset_idempotency_conflict"):
            delivery._write_user_asset(payload, self.assets_db)
        with closing(sqlite3.connect(self.assets_db)) as conn:
            conn.execute("UPDATE video_assets SET mode='ai_edit_v2',status='deleted' WHERE job_id=123")
            conn.commit()
        with self.assertRaisesRegex(delivery.DeliveryError, "asset_idempotency_conflict"):
            delivery._write_user_asset(payload, self.assets_db)
        with closing(sqlite3.connect(self.assets_db)) as conn:
            self.assertEqual(conn.execute("SELECT typeof(job_id) FROM video_assets WHERE job_id=123").fetchone()[0], "integer")

    def test_reconciler_rejects_tampered_durable_quality_report(self):
        fake_cos = FakeCos()
        with patch.object(delivery, "cos", fake_cos), patch.object(billing, "points", self.points), \
             patch.object(delivery, "_dispatch_and_complete", side_effect=BaseException("crash")):
            with self.assertRaises(BaseException):
                delivery.deliver(self.job_id, self.video, PASS_REPORT, 42,
                                 db_path=self.db, now_fn=lambda: 10)
        with closing(store.open_store(self.db)) as conn:
            conn.execute(
                "UPDATE edit_v2_delivery_intents SET quality_json=? WHERE job_id=?",
                ('{"passed":true,"error_codes":[],"failing_layers":[],"repairable":false,"terminal":NaN}', self.job_id),
            )
        with self.assertRaisesRegex(delivery.DeliveryError, "delivery_quality_report_invalid"):
            delivery.reconcile_pending_deliveries(
                11, db_path=self.db, asset_db_path=self.assets_db, cos_api=fake_cos,
            )
        self.assertEqual(delivery._load(self.job_id, self.db)["status"], "settling")


if __name__ == "__main__":
    unittest.main()
