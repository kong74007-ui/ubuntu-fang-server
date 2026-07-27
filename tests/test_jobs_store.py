# -*- coding: utf-8 -*-
"""content_domains/jobs_store.py —— 三个服务共用的 jobs 状态机与退点幂等。

这段逻辑此前在 core.py / leadgen_api.py / imggen_api.py 里各抄了一份，
同一个资金 bug 因此依次踩过三次（#187、jobs 1170、jobs 1356 那批）。

不变量：
1. 终态 CAS：谁先抢到谁定终态，败者不写状态、不做副作用
2. 认领 CAS：只有 pending 能被接管，防同一 job 跑两遍
3. 退点幂等：最多退一次
4. 退点失败保持 refunded=2 待确认，scanner 用同一个键继续确认
"""
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))
from content_domains import jobs_store  # noqa: E402


class JobsStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "jobs.db")
        with closing(self._conn()) as c:
            c.execute("""CREATE TABLE jobs(
                id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, username TEXT, cost INTEGER,
                status TEXT DEFAULT 'pending', payload TEXT, result TEXT, error TEXT,
                created_at INTEGER, updated_at INTEGER, deleted INTEGER DEFAULT 0, refunded INTEGER DEFAULT 0,
                owner TEXT)""")
            c.commit()
        self.refunds = []

    def tearDown(self):
        self.tmp.cleanup()

    def _conn(self):
        c = sqlite3.connect(self.db, timeout=10)
        c.row_factory = sqlite3.Row
        return c

    def _jdb(self):
        return self._conn()

    def _insert(self, cost=10, status="running"):
        now = int(time.time())
        with closing(self._conn()) as c:
            cur = c.execute("INSERT INTO jobs(kind,username,cost,status,created_at,updated_at) "
                            "VALUES('collect','u',?,?,?,?)", (cost, status, now, now))
            c.commit()
            return cur.lastrowid

    def _row(self, jid):
        with closing(self._conn()) as c:
            return c.execute("SELECT status, refunded, result, error FROM jobs WHERE id=?", (jid,)).fetchone()

    def _ok_refund(self, u, c):
        self.refunds.append((u, c))
        return True

    def _bad_refund(self, u, c):
        return False

    # --- 1. 终态 CAS ---
    def test_set_terminal_requires_running_by_default(self):
        jid = self._insert(status="pending")
        self.assertFalse(jobs_store.set_terminal(self._jdb, jid, "done", result={"x": 1}))
        self.assertEqual(self._row(jid)["status"], "pending")

    def test_set_terminal_from_pending_when_allowed(self):
        jid = self._insert(status="pending")
        self.assertTrue(jobs_store.set_terminal(self._jdb, jid, "error", error="db locked",
                                                from_states=("pending", "running")))
        self.assertEqual(self._row(jid)["status"], "error")
        self.assertEqual(self._row(jid)["refunded"], 2)

    def test_loser_does_not_overwrite_terminal(self):
        """reaper 先判 error，worker 随后成功 → 结果必须被丢弃。这就是 21 条僵尸记录的成因。"""
        jid = self._insert()
        self.assertTrue(jobs_store.set_terminal(self._jdb, jid, "error", error="超时"))
        self.assertFalse(jobs_store.set_terminal(self._jdb, jid, "done", result={"text": "拿到了"}))
        row = self._row(jid)
        self.assertEqual(row["status"], "error")
        self.assertIsNone(row["result"])

    def test_error_message_truncated_to_300(self):
        jid = self._insert()
        jobs_store.set_terminal(self._jdb, jid, "error", error="x" * 500)
        self.assertEqual(len(self._row(jid)["error"]), 300)

    # --- 2. 认领 CAS ---
    def test_claim_running_only_from_pending(self):
        jid = self._insert(status="pending")
        self.assertTrue(jobs_store.claim_running(self._jdb, jid))
        self.assertEqual(self._row(jid)["status"], "running")
        self.assertFalse(jobs_store.claim_running(self._jdb, jid), "同一 job 不该被认领两次")

    def test_claim_running_refuses_terminal_job(self):
        jid = self._insert(status="pending")
        jobs_store.claim_running(self._jdb, jid)
        jobs_store.set_terminal(self._jdb, jid, "error", error="boom")
        self.assertFalse(jobs_store.claim_running(self._jdb, jid))
        self.assertEqual(self._row(jid)["status"], "error")

    # --- 3. 退点幂等 ---
    def test_refund_once_only_once(self):
        jid = self._insert(10)
        jobs_store.set_terminal(self._jdb, jid, "error", error="boom")
        self.assertTrue(jobs_store.refund_once(self._jdb, jid, "u", 10, self._ok_refund))
        self.assertFalse(jobs_store.refund_once(self._jdb, jid, "u", 10, self._ok_refund))
        self.assertEqual(self.refunds, [("u", 10)])
        self.assertEqual(self._row(jid)["refunded"], 1)

    def test_refund_requires_error_terminal(self):
        jid = self._insert(10)
        jobs_store.set_terminal(self._jdb, jid, "done", result={"ok": 1})
        self.assertFalse(jobs_store.refund_once(self._jdb, jid, "u", 10, self._ok_refund))
        self.assertEqual(self.refunds, [], "done 的任务不该退点")

    def test_zero_or_bad_cost_never_refunds(self):
        jid = self._insert(0)
        jobs_store.set_terminal(self._jdb, jid, "error", error="boom")
        self.assertFalse(jobs_store.refund_once(self._jdb, jid, "u", 0, self._ok_refund))
        self.assertFalse(jobs_store.refund_once(self._jdb, jid, "u", None, self._ok_refund))
        self.assertFalse(jobs_store.refund_once(self._jdb, jid, "u", "abc", self._ok_refund))
        self.assertEqual(self.refunds, [])

    # --- 4. 退点失败保持待确认，恢复后安全重试 ---
    def test_refund_failure_stays_pending(self):
        jid = self._insert(10)
        jobs_store.set_terminal(self._jdb, jid, "error", error="boom")
        self.assertFalse(jobs_store.refund_once(self._jdb, jid, "u", 10, self._bad_refund))
        self.assertEqual(self._row(jid)["refunded"], 2)
        # 恢复后重试应能成功退一次
        self.assertTrue(jobs_store.refund_once(self._jdb, jid, "u", 10, self._ok_refund))
        self.assertEqual(self.refunds, [("u", 10)])
        self.assertEqual(self._row(jid)["refunded"], 1)

    def test_refund_exception_rolls_back_flag_for_retry(self):
        jid = self._insert(10)
        jobs_store.set_terminal(self._jdb, jid, "error", error="boom")

        self.assertFalse(jobs_store.refund_once(
            self._jdb, jid, "u", 10,
            lambda *_: (_ for _ in ()).throw(ConnectionError("response lost")),
        ))
        self.assertEqual(self._row(jid)["refunded"], 2)
        self.assertTrue(jobs_store.refund_once(self._jdb, jid, "u", 10, self._ok_refund))

    def test_scanner_ignores_ambiguous_historical_error(self):
        jid = self._insert(10, status="error")
        self.assertEqual(jobs_store.retry_failed_refunds(
            self._jdb, lambda *_: self.fail("historical row must not refund")), 0)

    def test_batch_insert_failure_compensates_total_once(self):
        with closing(self._conn()) as c:
            c.execute("""CREATE TRIGGER fail_pending BEFORE INSERT ON jobs
                         WHEN NEW.status='pending' BEGIN SELECT RAISE(FAIL, 'insert failed'); END""")
            c.commit()
        refund_calls = []

        def refund(username, amount, reason="", transaction_key=""):
            refund_calls.append((username, amount, transaction_key))
            return True

        with self.assertRaises(jobs_store.PaidJobInsertError) as ctx:
            jobs_store.create_paid_jobs(
                self._jdb, lambda *_args: 60, refund, "video", "u",
                [(20, {"n": 1}), (20, {"n": 2})], "content", "video_batch")
        self.assertEqual(ctx.exception.compensation, "refunded")
        self.assertEqual(1, len(refund_calls))
        self.assertEqual(("u", 40), refund_calls[0][:2])
        with closing(self._conn()) as c:
            row = c.execute("SELECT status,cost,refunded FROM jobs").fetchone()
        self.assertEqual(("error", 40, 1), tuple(row))

    def test_paid_job_uses_explicit_stable_submission_and_deduct_keys(self):
        deductions = []

        def deduct(username, amount, reason="", transaction_key=None):
            deductions.append((username, amount, reason, transaction_key))
            return 90

        job_id, points_left = jobs_store.create_paid_job(
            self._jdb, deduct, lambda *_args, **_kwargs: True,
            "ai_edit", "u", 10, {"source": 1}, "content",
            submission_ref="stable-submission-ref",
            deduct_transaction_key="stable-deduct-key",
        )

        self.assertGreater(job_id, 0)
        self.assertEqual(points_left, 90)
        self.assertEqual(deductions, [(
            "u", 10, "job:ai_edit submit:stable-submission-ref", "stable-deduct-key")])

    def test_explicit_job_id_replays_the_matching_database_winner(self):
        balance = {"points": 100}
        transactions = {}

        def deduct(username, amount, reason="", transaction_key=None):
            if transaction_key not in transactions:
                balance["points"] -= amount
                transactions[transaction_key] = balance["points"]
            return transactions[transaction_key]

        first = jobs_store.create_paid_job(
            self._jdb, deduct, lambda *_args, **_kwargs: True,
            "ai_edit", "u", 10, {"source": 1}, "content",
            submission_ref="stable-winner", deduct_transaction_key="stable-hold",
            job_id=-9001,
        )
        second = jobs_store.create_paid_job(
            self._jdb, deduct, lambda *_args, **_kwargs: True,
            "ai_edit", "u", 10, {"source": 1}, "content",
            submission_ref="stable-winner", deduct_transaction_key="stable-hold",
            job_id=-9001,
        )
        replay_with_state = jobs_store.create_paid_job(
            self._jdb, deduct, lambda *_args, **_kwargs: True,
            "ai_edit", "u", 10, {"source": 1}, "content",
            submission_ref="stable-winner", deduct_transaction_key="stable-hold",
            job_id=-9001, return_created=True,
        )

        self.assertEqual(first, (-9001, 90))
        self.assertEqual(second, first)
        self.assertEqual(replay_with_state, (-9001, 90, False))
        self.assertEqual(balance["points"], 90)
        with closing(self._conn()) as c:
            rows = c.execute("SELECT id,payload FROM jobs").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], -9001)
        self.assertEqual(json.loads(rows[0]["payload"])["_submission_ref"], "stable-winner")

    def test_explicit_job_id_conflict_does_not_charge_or_replace_the_winner(self):
        balance = {"points": 100}
        transactions = {}

        def deduct(username, amount, reason="", transaction_key=None):
            if transaction_key not in transactions:
                balance["points"] -= amount
                transactions[transaction_key] = balance["points"]
            return transactions[transaction_key]

        jobs_store.create_paid_job(
            self._jdb, deduct, lambda *_args, **_kwargs: True,
            "ai_edit", "u", 10, {"source": 1}, "content",
            submission_ref="winner-a", deduct_transaction_key="hold-a", job_id=-9002,
        )

        with self.assertRaisesRegex(Exception, "explicit job_id conflict"):
            jobs_store.create_paid_job(
                self._jdb, deduct, lambda *_args, **_kwargs: True,
                "ai_edit", "u", 10, {"source": 2}, "content",
                submission_ref="winner-b", deduct_transaction_key="hold-b", job_id=-9002,
            )

        self.assertEqual(balance["points"], 90)
        with closing(self._conn()) as c:
            row = c.execute("SELECT payload FROM jobs WHERE id=-9002").fetchone()
        self.assertEqual(json.loads(row["payload"])["_submission_ref"], "winner-a")

    def test_explicit_batch_replays_matching_database_winner_once(self):
        balance = {"points": 100}
        transactions = {}

        def deduct(username, amount, reason="", transaction_key=None):
            if transaction_key not in transactions:
                balance["points"] -= amount
                transactions[transaction_key] = balance["points"]
            return transactions[transaction_key]

        kwargs = dict(
            jdb=self._jdb, deduct=deduct,
            refund=lambda *_args, **_kwargs: self.fail("winner replay must not refund"),
            kind="video", username="u", items=[(20, {"n": 1}), (20, {"n": 2})],
            owner="content", job_ids=[-9101, -9102], submission_ref="batch-winner",
            deduct_transaction_key="batch-hold", submission_state="initializing:owner",
            return_created=True,
        )
        first = jobs_store.create_explicit_paid_jobs(**kwargs)
        second = jobs_store.create_explicit_paid_jobs(**kwargs)

        self.assertEqual(([-9101, -9102], 60, True), first)
        self.assertEqual(([-9101, -9102], 60, False), second)
        self.assertEqual(60, balance["points"])
        self.assertEqual({"batch-hold": 60}, transactions)
        with closing(self._conn()) as c:
            rows = c.execute("SELECT id,payload FROM jobs ORDER BY id").fetchall()
        self.assertEqual([-9102, -9101], [row["id"] for row in rows])
        self.assertTrue(all(
            json.loads(row["payload"])["_submission_ref"] == "batch-winner" for row in rows))

    def test_explicit_batch_concurrent_loser_never_refunds_winner(self):
        balance = {"points": 100}
        transactions = {}
        transaction_lock = threading.Lock()
        deduct_barrier = threading.Barrier(2)
        refunds = []
        results = []
        errors = []

        def deduct(username, amount, reason="", transaction_key=None):
            deduct_barrier.wait(timeout=5)
            with transaction_lock:
                if transaction_key not in transactions:
                    balance["points"] -= amount
                    transactions[transaction_key] = balance["points"]
                return transactions[transaction_key]

        def refund(*args, **kwargs):
            refunds.append((args, kwargs))
            return True

        def submit():
            try:
                results.append(jobs_store.create_explicit_paid_jobs(
                    self._jdb, deduct, refund, "video", "u",
                    [(20, {"n": 1}), (20, {"n": 2})], "content",
                    job_ids=[-9201, -9202], submission_ref="batch-race",
                    deduct_transaction_key="batch-race-hold",
                    submission_state="initializing:owner", return_created=True))
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        threads = [threading.Thread(target=submit) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual([], errors)
        self.assertEqual(2, len(results))
        self.assertEqual([False, True], sorted(result[2] for result in results))
        self.assertEqual(60, balance["points"])
        self.assertEqual([], refunds)
        with closing(self._conn()) as c:
            self.assertEqual(2, c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])

    def test_explicit_batch_conflict_does_not_charge_or_replace_rows(self):
        with closing(self._conn()) as c:
            c.execute(
                "INSERT INTO jobs(id,kind,username,cost,payload,created_at,updated_at,owner) "
                "VALUES(-9301,'video','other',20,'{}',1,1,'content')")
            c.commit()
        deductions = []
        with self.assertRaises(jobs_store.PaidJobConflictError):
            jobs_store.create_explicit_paid_jobs(
                self._jdb, lambda *args, **kwargs: deductions.append((args, kwargs)),
                lambda *_args, **_kwargs: True, "video", "u",
                [(20, {"n": 1}), (20, {"n": 2})], "content",
                job_ids=[-9301, -9302], submission_ref="batch-conflict",
                deduct_transaction_key="batch-conflict-hold")
        self.assertEqual([], deductions)
        with closing(self._conn()) as c:
            rows = c.execute("SELECT id,username FROM jobs ORDER BY id").fetchall()
        self.assertEqual([(-9301, "other")], [tuple(row) for row in rows])

    def test_explicit_batch_owner_transition_is_all_or_none(self):
        jobs_store.create_explicit_paid_jobs(
            self._jdb, lambda *_args, **_kwargs: 60,
            lambda *_args, **_kwargs: True, "video", "u",
            [(20, {"n": 1}), (20, {"n": 2})], "content",
            job_ids=[-9401, -9402], submission_ref="batch-state",
            deduct_transaction_key="batch-state-hold", submission_state="initializing:a")
        self.assertTrue(jobs_store.set_explicit_jobs_state(
            self._jdb, [-9401, -9402], "ready", expected_states=("initializing:a",)))
        with closing(self._conn()) as c:
            c.execute("UPDATE jobs SET status='running' WHERE id=-9402")
            c.commit()
        self.assertFalse(jobs_store.set_explicit_jobs_state(
            self._jdb, [-9401, -9402], "initializing:b", expected_states=("ready",)))
        states = jobs_store.explicit_jobs_state(self._jdb, [-9401, -9402])
        self.assertEqual("ready", states[-9401][1])
        self.assertEqual("ready", states[-9402][1])

    def test_publication_marks_the_whole_explicit_batch_ready_after_worker_claim(self):
        jobs_store.create_explicit_paid_jobs(
            self._jdb, lambda *_args, **_kwargs: 60,
            lambda *_args, **_kwargs: True, "video", "u",
            [(20, {"n": 1}), (20, {"n": 2})], "content",
            job_ids=[-9451, -9452], submission_ref="batch-publish",
            deduct_transaction_key="batch-publish-hold", submission_state="initializing:a")
        with closing(self._conn()) as c:
            c.execute("UPDATE jobs SET status='running' WHERE id=-9452")
            c.commit()

        self.assertTrue(jobs_store.publish_explicit_jobs_ready(
            self._jdb, [-9451, -9452], expected_state="initializing:a"))
        states = jobs_store.explicit_jobs_state(self._jdb, [-9451, -9452])
        self.assertEqual("ready", states[-9451][1])
        self.assertEqual("ready", states[-9452][1])

    # --- 端到端：reaper 与 worker 交错，钱只退一次，结果不覆写 ---
    def test_reaper_wins_race_money_is_correct(self):
        jid = self._insert(10)
        if jobs_store.set_terminal(self._jdb, jid, "error", error="生成超时自动结束，已退点"):
            jobs_store.refund_once(self._jdb, jid, "u", 10, self._ok_refund)
        self.assertFalse(jobs_store.set_terminal(self._jdb, jid, "done", result={"text": "结果"}))
        self.assertEqual(len(self.refunds), 1)
        self.assertIsNone(self._row(jid)["result"])

    def test_worker_wins_race_no_refund(self):
        jid = self._insert(10)
        self.assertTrue(jobs_store.set_terminal(self._jdb, jid, "done", result={"text": "结果"}))
        if jobs_store.set_terminal(self._jdb, jid, "error", error="超时"):
            jobs_store.refund_once(self._jdb, jid, "u", 10, self._ok_refund)
        self.assertEqual(self.refunds, [], "任务成功了不该退点")
        self.assertEqual(self._row(jid)["status"], "done")


class WrappersDelegateTests(unittest.TestCase):
    """三个服务的 _set_terminal/_refund_once 必须是薄包装，签名一致。"""

    def setUp(self):
        server_dir = str(Path(__file__).resolve().parents[1] / "server")
        if server_dir not in sys.path:
            sys.path.insert(0, server_dir)
        os.environ.setdefault("CONTENT_OUT", tempfile.mkdtemp(prefix="hq-jobsstore-"))

    def test_all_three_expose_from_states(self):
        import importlib, inspect
        for mod_name in ("content_domains.core", "leadgen_api", "imggen_api"):
            m = importlib.import_module(mod_name)
            sig = inspect.signature(m._set_terminal)
            self.assertIn("from_states", sig.parameters, mod_name)

    def test_no_duplicate_cas_sql_left_behind(self):
        """三处的裸 SQL 必须已经删干净，否则改一处漏两处的老问题会复发。"""
        for rel in ("server/content_domains/core.py", "server/leadgen_api.py", "server/imggen_api.py"):
            text = (Path(__file__).resolve().parents[1] / rel).read_text(encoding="utf-8")
            self.assertNotIn("refunded=1 WHERE id=? AND refunded=0", text, rel)


if __name__ == "__main__":
    unittest.main()
