from __future__ import annotations

import unittest
import tempfile
import time
import threading
from pathlib import Path

from server.content_domains.ai_edit_v3 import contracts
from server.content_domains.ai_edit_v3.runtime import (
    RuntimeDependencies,
    StageOutcome,
)
from server.content_domains.ai_edit_v3.store import (
    LeaseLost,
    StoreConflictError,
    V3Store,
)


class V3StateContractTests(unittest.TestCase):
    def test_every_state_has_an_explicit_transition_contract(self):
        self.assertTrue(hasattr(contracts, "ALL_STATES"))
        self.assertTrue(hasattr(contracts, "QUEUE_CLAIMABLE_STATES"))
        self.assertEqual(set(contracts.ALLOWED_TRANSITIONS), contracts.ALL_STATES)
        self.assertEqual(
            {
                target
                for targets in contracts.ALLOWED_TRANSITIONS.values()
                for target in targets
            },
            contracts.ALL_STATES - {"created_draft"},
        )
        self.assertEqual(
            {
                state
                for state, targets in contracts.ALLOWED_TRANSITIONS.items()
                if not targets
            },
            contracts.TERMINAL_STATES,
        )

    def test_queue_claimable_states_are_only_media_plus_failed(self):
        expected = frozenset((*contracts.MEDIA_STATES, "failed"))
        claimable = getattr(contracts, "QUEUE_CLAIMABLE_STATES", None)
        self.assertEqual(claimable, expected)
        self.assertTrue(
            claimable.isdisjoint(
                contracts.TERMINAL_STATES | contracts.RECONCILIATION_STATES
            )
        )


class V3StateCASTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        self.db = root / "ai_edit_v3.db"
        self.v2 = root / "ai_edit_v2.db"
        self.v2.write_bytes(b"V2 identity marker; never open")
        self.store = V3Store(self.db, v2_db_path=self.v2, environment="test")
        self.store.insert_pricing_version(
            "price-v1", {"base": 1}, status="published", created_at=1,
            published_at=1,
        )
        self.store.insert_quote(
            "alice", "quote-1", {}, pricing_version="price-v1",
            min_points=1, max_points=1, breakdown={"base": 1},
            expires_at=9_999_999, created_at=1,
        )

    def seed_job(self, job_id, state, *, deadline=5_000_000):
        connection = self.store._connect()
        try:
            connection.execute(
                """INSERT INTO edit_v3_jobs(
                       job_id,environment,owner_id,state,normalized_request_json,
                       request_sha256,quote_id,idempotency_key,queued_at,
                       processing_deadline_at,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    job_id, "test", "alice", state, "{}", "0" * 64,
                    "quote-1", f"key-{job_id}", 1, deadline, 1, 1,
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def row(self, job_id):
        connection = self.store._connect()
        try:
            return dict(
                connection.execute(
                    "SELECT * FROM edit_v3_jobs WHERE job_id=?", (job_id,)
                ).fetchone()
            )
        finally:
            connection.close()

    def test_every_graph_edge_uses_fenced_actual_state_cas(self):
        index = 0
        for source, targets in contracts.ALLOWED_TRANSITIONS.items():
            for target in sorted(targets):
                with self.subTest(source=source, target=target):
                    index += 1
                    job_id = f"edge-{index}"
                    self.seed_job(job_id, source)
                    claim = self.store.claim_job(
                        job_id,
                        f"worker-{index}",
                        30,
                        100_000,
                        expected_states={source},
                    )
                    self.assertIsNotNone(claim)
                    self.assertTrue(
                        self.store.transition_leased(
                            claim,
                            {source},
                            target,
                            101_000,
                            lease_seconds=30,
                        )
                    )
                    row = self.row(job_id)
                    self.assertEqual(row["state"], target)
                    expected_deadline = (
                        5_600_000
                        if (source, target)
                        == ("quality_checking", "repair_planning")
                        else 5_000_000
                    )
                    self.assertEqual(row["processing_deadline_at"], expected_deadline)
                    clears = target in contracts.TERMINAL_STATES or target in {
                        "failed_reconciliation_pending",
                        "failed_asset_decision_pending",
                    }
                    self.assertEqual(row["worker_id"] is None, clears)

    def test_actual_state_mismatch_is_false_without_mutation(self):
        self.seed_job("job-1", "queued")
        claim = self.store.claim_next_job("worker-a", 30, 100_000)
        before = self.row("job-1")
        self.assertFalse(
            self.store.transition_leased(
                claim,
                {"generating_voice"},
                "normalizing",
                101_000,
                lease_seconds=30,
            )
        )
        self.assertEqual(self.row("job-1"), before)

    def test_claim_bound_job_read_requires_environment_owner_and_live_token(self):
        self.seed_job("job-claim-read", "queued")
        first = self.store.claim_next_job("worker-first", 30, 100_000)
        row = self.store.get_job_for_claim(first, 100_001, environment="test")
        self.assertEqual(row["job_id"], first.job_id)
        self.assertEqual(row["worker_id"], first.worker_id)
        self.assertEqual(row["fencing_token"], first.fencing_token)

        with self.assertRaises(LeaseLost):
            self.store.get_job_for_claim(
                first, 100_001, environment="production"
            )
        self.assertTrue(self.store.release_lease(first, 100_002))
        second = self.store.claim_job(
            first.job_id,
            "worker-second",
            30,
            100_003,
            expected_states={"queued"},
        )
        self.assertGreater(second.fencing_token, first.fencing_token)
        with self.assertRaises(LeaseLost):
            self.store.get_job_for_claim(first, 100_004, environment="test")

    def test_running_attempt_blocks_transition_and_skipped_requires_checkpoint(self):
        self.seed_job("job-1", "queued")
        claim = self.store.claim_next_job("worker-a", 30, 100_000)
        attempt = self.store.start_stage_attempt(
            claim, "queued", "a" * 64, 101_000
        )
        self.assertFalse(
            self.store.transition_leased(
                claim,
                {"queued"},
                "generating_voice",
                102_000,
                lease_seconds=30,
            )
        )
        with self.assertRaises(StoreConflictError):
            self.store.finish_stage_attempt(
                claim, attempt["id"], "skipped", 103_000
            )
        self.store.save_checkpoint(
            claim, attempt["id"], "a" * 64, {"skipped": True}, 103_000
        )
        self.store.finish_stage_attempt(
            claim, attempt["id"], "skipped", 104_000
        )
        self.assertTrue(
            self.store.transition_leased(
                claim,
                {"queued"},
                "generating_voice",
                105_000,
                lease_seconds=30,
            )
        )

    def test_repair_without_existing_deadline_is_rejected(self):
        self.seed_job("job-1", "quality_checking", deadline=None)
        claim = self.store.claim_next_job("worker-a", 30, 100_000)
        self.assertFalse(
            self.store.transition_leased(
                claim,
                {"quality_checking"},
                "repair_planning",
                101_000,
                lease_seconds=30,
            )
        )
        row = self.row("job-1")
        self.assertIsNone(row["processing_deadline_at"])
        self.assertEqual(row["repair_count"], 0)

    def test_no_work_stage_records_skipped_before_transition(self):
        try:
            from server.content_domains.ai_edit_v3.pipeline import run_job
        except ImportError as exc:
            self.fail(f"run_job is absent: {exc}")

        class Clock:
            def now(self):
                return 101.0

        class Supervisor:
            def __init__(self):
                self.terminated = []

            def terminate_job(self, job_id):
                self.terminated.append(job_id)

        self.seed_job("job-skipped", "generating_voice")
        claim = self.store.claim_job(
            "job-skipped",
            "worker-skipped",
            30,
            100_000,
            expected_states={"generating_voice"},
        )
        supervisor = Supervisor()

        def skip_handler(job, context):
            context.assert_active()
            return StageOutcome("normalizing", {"skipped": True}, "0" * 64)

        runtime = RuntimeDependencies(
            store=self.store,
            clock=Clock(),
            points=object(),
            assets=object(),
            cos=None,
            tts=None,
            asr=None,
            director=None,
            image_generator=None,
            audio_generator=None,
            renderer=None,
            process_supervisor=supervisor,
            stage_handlers={"generating_voice": skip_handler},
        )
        result = run_job(claim, runtime, db_path=self.db)

        connection = self.store._connect()
        try:
            attempt = dict(
                connection.execute(
                    """SELECT * FROM edit_v3_stage_attempts
                       WHERE job_id=? AND stage=? ORDER BY attempt DESC LIMIT 1""",
                    (claim.job_id, "generating_voice"),
                ).fetchone()
            )
        finally:
            connection.close()
        self.assertEqual(attempt["status"], "skipped")
        self.assertEqual(result.state, "normalizing")
        self.assertEqual(supervisor.terminated, [])

    def test_failed_heartbeat_invalidates_claim_and_closes_running_attempt(self):
        from server.content_domains.ai_edit_v3.pipeline import run_job

        class Clock:
            def now(self):
                return 100.1

        class Supervisor:
            def __init__(self):
                self.terminated = []

            def terminate_job(self, job_id):
                self.terminated.append(job_id)

        self.seed_job("job-lease-loss", "generating_voice")
        claim = self.store.claim_job(
            "job-lease-loss",
            "worker-old",
            1,
            100_000,
            expected_states={"generating_voice"},
        )
        renewals = []

        def fail_renewal(active_claim, lease_seconds, now_ms):
            renewals.append((active_claim, lease_seconds, now_ms))
            return False

        self.store.renew_lease = fail_renewal
        supervisor = Supervisor()

        def blocking_handler(job, context):
            context.assert_active()
            time.sleep(0.45)
            return StageOutcome("normalizing", {"done": True}, "0" * 64)

        runtime = RuntimeDependencies(
            store=self.store,
            clock=Clock(),
            points=object(),
            assets=object(),
            cos=None,
            tts=None,
            asr=None,
            director=None,
            image_generator=None,
            audio_generator=None,
            renderer=None,
            process_supervisor=supervisor,
            stage_handlers={"generating_voice": blocking_handler},
        )

        with self.assertRaises(LeaseLost):
            run_job(claim, runtime, db_path=self.db, lease_seconds=1)

        connection = self.store._connect()
        try:
            attempt = dict(
                connection.execute(
                    "SELECT * FROM edit_v3_stage_attempts WHERE job_id=?",
                    (claim.job_id,),
                ).fetchone()
            )
            checkpoint_count = connection.execute(
                "SELECT COUNT(*) FROM edit_v3_checkpoints WHERE job_id=?",
                (claim.job_id,),
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(len(renewals), 1)
        self.assertEqual(supervisor.terminated, [claim.job_id])
        self.assertNotEqual(attempt["status"], "running")
        self.assertEqual(checkpoint_count, 0)
        self.assertEqual(self.row(claim.job_id)["state"], "generating_voice")

    def test_committed_checkpoint_replays_after_fenced_transition_interruption(self):
        from server.content_domains.ai_edit_v3.pipeline import run_job

        class Clock:
            def now(self):
                return 100.1

        class Supervisor:
            def __init__(self):
                self.terminated = []

            def terminate_job(self, job_id):
                self.terminated.append(job_id)

        self.seed_job("job-checkpoint-replay", "generating_voice")
        first = self.store.claim_job(
            "job-checkpoint-replay",
            "worker-first",
            30,
            100_000,
            expected_states={"generating_voice"},
        )
        calls = []
        supervisor = Supervisor()

        def handler(job, context):
            calls.append(context.claim.fencing_token)
            return StageOutcome("normalizing", {"done": True}, "0" * 64)

        runtime = RuntimeDependencies(
            store=self.store,
            clock=Clock(),
            points=object(),
            assets=object(),
            cos=None,
            tts=None,
            asr=None,
            director=None,
            image_generator=None,
            audio_generator=None,
            renderer=None,
            process_supervisor=supervisor,
            stage_handlers={"generating_voice": handler},
        )
        transition = self.store.transition_leased
        interrupted = False

        def fail_once(claim, expected, target, now_ms, *, lease_seconds):
            nonlocal interrupted
            if not interrupted and target == "normalizing":
                interrupted = True
                return False
            return transition(
                claim, expected, target, now_ms, lease_seconds=lease_seconds
            )

        self.store.transition_leased = fail_once
        with self.assertRaises(LeaseLost):
            run_job(first, runtime, db_path=self.db)
        self.store.transition_leased = transition
        second = self.store.claim_job(
            "job-checkpoint-replay",
            "worker-second",
            30,
            100_200,
            expected_states={"generating_voice"},
        )
        result = run_job(second, runtime, db_path=self.db)

        connection = self.store._connect()
        try:
            checkpoint_count = connection.execute(
                "SELECT COUNT(*) FROM edit_v3_checkpoints WHERE job_id=?",
                (first.job_id,),
            ).fetchone()[0]
            running_count = connection.execute(
                """SELECT COUNT(*) FROM edit_v3_stage_attempts
                   WHERE job_id=? AND status='running'""",
                (first.job_id,),
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(calls, [first.fencing_token])
        self.assertGreater(second.fencing_token, first.fencing_token)
        self.assertEqual(checkpoint_count, 1)
        self.assertEqual(running_count, 0)
        self.assertEqual(result.state, "normalizing")

    def test_missing_handler_fails_closed_without_checkpoint(self):
        from server.content_domains.ai_edit_v3.pipeline import run_job

        class Clock:
            def now(self):
                return 100.1

        class Supervisor:
            def terminate_job(self, job_id):
                raise AssertionError("no process was started")

        self.seed_job("job-missing-handler", "generating_voice")
        claim = self.store.claim_job(
            "job-missing-handler",
            "worker-missing",
            30,
            100_000,
            expected_states={"generating_voice"},
        )
        runtime = RuntimeDependencies(
            store=self.store,
            clock=Clock(),
            points=object(),
            assets=object(),
            cos=None,
            tts=None,
            asr=None,
            director=None,
            image_generator=None,
            audio_generator=None,
            renderer=None,
            process_supervisor=Supervisor(),
            stage_handlers={},
        )

        result = run_job(claim, runtime, db_path=self.db)

        self.assertEqual(result.state, "failed")
        self.assertEqual(result.error_code, "capability_unavailable")
        connection = self.store._connect()
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM edit_v3_checkpoints WHERE job_id=?",
                    (claim.job_id,),
                ).fetchone()[0],
                0,
            )
        finally:
            connection.close()

    def test_stop_before_handler_terminates_once_without_starting_attempt(self):
        from server.content_domains.ai_edit_v3.pipeline import run_job

        class Clock:
            def now(self):
                return 100.1

        class Supervisor:
            def __init__(self):
                self.terminated = []

            def terminate_job(self, job_id):
                self.terminated.append(job_id)

        self.seed_job("job-stopped", "generating_voice")
        claim = self.store.claim_job(
            "job-stopped",
            "worker-stopped",
            30,
            100_000,
            expected_states={"generating_voice"},
        )
        supervisor = Supervisor()

        def forbidden_handler(job, context):
            raise AssertionError("stopped job must not invoke media")

        runtime = RuntimeDependencies(
            store=self.store,
            clock=Clock(),
            points=object(),
            assets=object(),
            cos=None,
            tts=None,
            asr=None,
            director=None,
            image_generator=None,
            audio_generator=None,
            renderer=None,
            process_supervisor=supervisor,
            stage_handlers={"generating_voice": forbidden_handler},
        )
        stopped = threading.Event()
        stopped.set()

        result = run_job(claim, runtime, db_path=self.db, stop_event=stopped)

        self.assertEqual(result.state, "generating_voice")
        self.assertEqual(result.error_code, "pipeline_stopped")
        self.assertEqual(supervisor.terminated, [claim.job_id])
        connection = self.store._connect()
        try:
            attempt_count = connection.execute(
                "SELECT COUNT(*) FROM edit_v3_stage_attempts WHERE job_id=?",
                (claim.job_id,),
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(attempt_count, 0)

    def test_expired_queue_wait_stops_before_media_handler(self):
        from server.content_domains.ai_edit_v3.pipeline import run_job

        class Clock:
            def now(self):
                return 400.0

        class Supervisor:
            def __init__(self):
                self.terminated = []

            def terminate_job(self, job_id):
                self.terminated.append(job_id)

        self.seed_job("job-queue-timeout", "queued")
        claim = self.store.claim_job(
            "job-queue-timeout",
            "worker-timeout",
            30,
            399_000,
            expected_states={"queued"},
        )
        supervisor = Supervisor()
        runtime = RuntimeDependencies(
            store=self.store,
            clock=Clock(),
            points=object(),
            assets=object(),
            cos=None,
            tts=None,
            asr=None,
            director=None,
            image_generator=None,
            audio_generator=None,
            renderer=None,
            process_supervisor=supervisor,
            stage_handlers={"queued": lambda job, context: self.fail("media called")},
        )

        result = run_job(
            claim,
            runtime,
            db_path=self.db,
            queue_timeout_ms=300_000,
        )

        self.assertEqual(result.state, "failed")
        self.assertEqual(result.error_code, "pipeline_queue_timeout")
        self.assertEqual(supervisor.terminated, [claim.job_id])
        second = self.store.claim_job(
            claim.job_id,
            "worker-refund",
            30,
            400_001,
            expected_states={"failed"},
        )
        refund = run_job(second, runtime, db_path=self.db)
        self.assertEqual(refund.state, "refund_pending")

    def test_invalid_handler_next_state_fails_without_checkpoint(self):
        from server.content_domains.ai_edit_v3.pipeline import run_job

        class Clock:
            def now(self):
                return 100.1

        class Supervisor:
            def __init__(self):
                self.terminated = []

            def terminate_job(self, job_id):
                self.terminated.append(job_id)

        self.seed_job("job-invalid-edge", "generating_voice")
        claim = self.store.claim_job(
            "job-invalid-edge",
            "worker-invalid-edge",
            30,
            100_000,
            expected_states={"generating_voice"},
        )
        supervisor = Supervisor()
        runtime = RuntimeDependencies(
            store=self.store,
            clock=Clock(),
            points=object(),
            assets=object(),
            cos=None,
            tts=None,
            asr=None,
            director=None,
            image_generator=None,
            audio_generator=None,
            renderer=None,
            process_supervisor=supervisor,
            stage_handlers={
                "generating_voice": lambda job, context: StageOutcome(
                    "publishing", {"bad": True}, "0" * 64
                )
            },
        )

        result = run_job(claim, runtime, db_path=self.db)

        connection = self.store._connect()
        try:
            attempt = dict(
                connection.execute(
                    "SELECT * FROM edit_v3_stage_attempts WHERE job_id=?",
                    (claim.job_id,),
                ).fetchone()
            )
            checkpoints = connection.execute(
                "SELECT COUNT(*) FROM edit_v3_checkpoints WHERE job_id=?",
                (claim.job_id,),
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(result.state, "failed")
        self.assertEqual(result.error_code, "invalid_stage_transition")
        self.assertEqual(attempt["status"], "failed")
        self.assertEqual(checkpoints, 0)
        self.assertEqual(supervisor.terminated, [claim.job_id])


if __name__ == "__main__":
    unittest.main()
