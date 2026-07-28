import hashlib
import json
import os
import tempfile
import threading
import unittest
import uuid
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
        Path(self.video).write_bytes(b"playable-final-mp4")
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
        self.temp.cleanup()

    def test_delivery_settles_and_inserts_asset_once(self):
        fake_cos = FakeCos()
        with patch.object(delivery, "cos", fake_cos), patch.object(billing, "points", self.points):
            first = delivery.deliver(self.job_id, self.video, PASS_REPORT, 42, db_path=self.db)
            replay = delivery.deliver(self.job_id, self.video, PASS_REPORT, 42, db_path=self.db)

        self.assertEqual(replay, first)
        with closing(store.open_store(self.db)) as conn:
            job = conn.execute("SELECT status,output_cos_key FROM edit_v2_jobs WHERE id=?", (self.job_id,)).fetchone()
            assets = conn.execute("SELECT COUNT(*) FROM edit_v2_render_artifacts WHERE job_id=? AND kind='delivery'", (self.job_id,)).fetchone()[0]
            bill = conn.execute("SELECT status,response_json FROM edit_v2_billing WHERE job_id=?", (self.job_id,)).fetchone()
        self.assertEqual(job["status"], "completed")
        self.assertEqual(assets, 1)
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
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM edit_v2_render_artifacts WHERE job_id=? AND kind='delivery'", (self.job_id,)).fetchone()[0], 1)
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


if __name__ == "__main__":
    unittest.main()
