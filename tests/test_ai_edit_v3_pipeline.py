from __future__ import annotations

import unittest
import tempfile
import time
import threading
from pathlib import Path
from unittest.mock import patch

from server.content_domains.ai_edit_v3 import contracts
from server.content_domains.ai_edit_v3.runtime import (
    RuntimeDependencies,
    StageOutcome,
)
from server.content_domains.ai_edit_v3.store import (
    LeaseLost,
    StoreConfigurationError,
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

    def seed_publish_job(self, job_id):
        self.seed_job(job_id, "publishing")
        self.store._write(
            lambda connection: connection.execute(
                """UPDATE edit_v3_jobs
                   SET confirmed_preheld_total=1,
                       delivery_object_key=?
                   WHERE job_id=?""",
                (
                    f"test/ai-edit-v3/owner/{job_id}/delivery/final.mp4",
                    job_id,
                ),
            )
        )

    def publish_rows(self, job_id):
        return self.store._read(
            lambda connection: tuple(
                dict(row)
                for row in connection.execute(
                    """SELECT * FROM edit_v3_publish_intents
                       WHERE job_id=? ORDER BY publish_generation,operation""",
                    (job_id,),
                )
            )
        )

    def billing_rows(self, job_id):
        return self.store._read(
            lambda connection: tuple(
                dict(row)
                for row in connection.execute(
                    """SELECT * FROM edit_v3_billing_intents
                       WHERE job_id=? ORDER BY operation,id""",
                    (job_id,),
                )
            )
        )

    def seed_unknown_publish_generation(self, job_id, *, transition=True):
        from server.content_domains.ai_edit_v3.delivery import advance_publish
        from server.content_domains.ai_edit_v3.providers import SubmissionUnknown
        from server.content_domains.video_asset_publish import PublicationDecision

        class Publisher:
            def register_generation(
                self, mode, source_job_id, generation, idempotency_key
            ):
                return PublicationDecision("accepted", generation, None)

            def prepare_hidden(
                self,
                mode,
                source_job_id,
                owner,
                object_key,
                generation,
                idempotency_key,
            ):
                return PublicationDecision("accepted", generation, None)

            def commit_publish(self, *args):
                raise SubmissionUnknown("response_lost")

            def cancel_publish(self, *args):
                raise AssertionError("unknown publish must not cancel")

            def query_decision(self, *args):
                raise AssertionError("setup stops at the unknown commit")

        self.seed_publish_job(job_id)
        setup_claim = self.store.claim_job(
            job_id,
            f"worker-{job_id}-setup",
            30,
            100_000,
            expected_states={"publishing"},
        )
        progress = advance_publish(
            setup_claim,
            metadata_sha256="8" * 64,
            now=100_001,
            store=self.store,
            publisher=Publisher(),
        )
        self.assertEqual(progress.next_state, "asset_decision_reconciling")
        if transition:
            self.assertTrue(
                self.store.transition_leased(
                    setup_claim,
                    {"publishing"},
                    progress.next_state,
                    100_002,
                    lease_seconds=30,
                )
            )
        self.assertTrue(self.store.release_lease(setup_claim, 100_003))
        return setup_claim

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
        self.assertEqual(refund.state, "refunded")

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

    def test_pending_predebit_pass_moves_real_job_from_created_draft_to_queued(self):
        from server.content_domains.ai_edit_v3.billing import (
            LedgerResult,
            LedgerTransaction,
        )
        from server.content_domains.ai_edit_v3.pipeline import (
            run_reconciliation_pass,
        )

        created = self.store.create_job_with_predebit(
            "alice",
            "job-pending-predebit",
            "quote-1",
            "key-pending-predebit",
            {},
            now_ms=100,
            intent_id="intent-pending-predebit",
        )

        class Clock:
            def now(self):
                return 0.102

        class Ledger:
            def __init__(self):
                self.deduct_calls = []

            def deduct(self, owner, amount, transaction_key, reason):
                self.deduct_calls.append(
                    (owner, amount, transaction_key, reason)
                )
                return LedgerResult(
                    True,
                    LedgerTransaction(
                        transaction_key,
                        "deduct",
                        owner,
                        amount,
                        99,
                        101,
                    ),
                    None,
                )

            def refund(self, owner, amount, transaction_key, reason):
                raise AssertionError("pre-debit pass must not refund")

            def query_transaction(self, owner, transaction_key):
                raise AssertionError("pending intent must transmit before query")

        ledger = Ledger()
        runtime = RuntimeDependencies(
            store=self.store,
            clock=Clock(),
            points=ledger,
            assets=object(),
            cos=None,
            tts=None,
            asr=None,
            director=None,
            image_generator=None,
            audio_generator=None,
            renderer=None,
            process_supervisor=object(),
            stage_handlers={},
        )

        counts = run_reconciliation_pass(
            runtime,
            worker_id="worker-predebit",
            lease_seconds=30,
            limit=10,
        )

        job = self.row(created["job"]["job_id"])
        intent = self.store._read(
            lambda connection: dict(
                connection.execute(
                    "SELECT * FROM edit_v3_billing_intents WHERE id=?",
                    (created["intent"]["id"],),
                ).fetchone()
            )
        )
        self.assertEqual(counts["billing"], 1)
        self.assertEqual(job["state"], "queued")
        self.assertIsNone(job["worker_id"])
        self.assertEqual(intent["status"], "completed")
        self.assertEqual(len(ledger.deduct_calls), 1)

    def test_service_created_job_reaches_queue_through_real_predebit_outbox(self):
        from server.content_domains.ai_edit_v3.billing import (
            LedgerResult,
            LedgerTransaction,
        )
        from server.content_domains.ai_edit_v3.feature import (
            CapabilityItem,
            CapabilityReport,
        )
        from server.content_domains.ai_edit_v3.pipeline import (
            run_reconciliation_pass,
        )
        from server.content_domains.ai_edit_v3.service import (
            CapacityDecision,
            EditV3Service,
        )

        service_store = V3Store(
            self.db.parent / "service-ai-edit-v3.db",
            v2_db_path=self.v2,
            environment="test",
        )
        part_names = (
            "base_task",
            "duration_tier",
            "tts_ceiling",
            "qwen_ceiling",
            "image_ceiling",
            "bgm_sfx_ceiling",
            "render_complexity",
            "one_repair_reserve",
        )
        parts = {
            name: {
                "ceiling_quantity": 100 if name == "tts_ceiling" else 1,
                "min_rate": 1,
                "max_rate": 2,
                **({"unit_size": 100} if name == "tts_ceiling" else {}),
            }
            for name in part_names
        }
        service_store.insert_pricing_version(
            "service-price-v1",
            {"parts": parts},
            status="published",
            created_at=1,
            published_at=2,
        )

        class Ids:
            def __init__(self):
                self.value = 0

            def __call__(self, prefix):
                self.value += 1
                return f"{prefix}-service-{self.value}"

        class Catalog:
            def resolve_voice(self, owner, voice_id):
                return {
                    "voice_id": voice_id,
                    "status": "ready",
                    "version": "voice-v1",
                }

        class Capacity:
            def check(self, request):
                return CapacityDecision(True, 1, 1_024, None)

        report = CapabilityReport(
            items={
                "common": CapabilityItem(
                    "configured_and_wired", "capability_ready", "ready"
                )
            },
            runtime_versions={"python": "3.12"},
            allows_existing_reads=True,
            accepts_uploads=True,
            accepts_new_jobs=True,
        )
        service = EditV3Service(
            service_store,
            owner_hmac_secret=b"task-ten-service-secret",
            enabled=True,
            id_factory=Ids(),
            source_catalog=Catalog(),
            capacity_gate=Capacity(),
            capability_report=report,
        )
        request = {
            "input_type": "script_to_audio_video",
            "tts_input": {"text": "hello", "voice_id": "voice-1"},
            "ratio": "16:9",
            "creation_mode": "ai_auto",
            "material_asset_ids": [],
        }
        quote = service.quote("alice", request, now=1_000)
        created = service.create_job(
            "alice",
            request,
            quote["quote_id"],
            "service-job-key",
            now=1_001,
        )
        self.assertEqual(created["state"], "created_draft")

        class Clock:
            def now(self):
                return 1.003

        class Ledger:
            def __init__(self):
                self.calls = []

            def deduct(self, owner, amount, transaction_key, reason):
                self.calls.append(transaction_key)
                return LedgerResult(
                    True,
                    LedgerTransaction(
                        transaction_key,
                        "deduct",
                        owner,
                        amount,
                        100,
                        1_002,
                    ),
                    None,
                )

            def refund(self, *args):
                raise AssertionError("pre-debit integration must not refund")

            def query_transaction(self, *args):
                raise AssertionError("pending pre-debit must transmit first")

        ledger = Ledger()
        runtime = RuntimeDependencies(
            store=service_store,
            clock=Clock(),
            points=ledger,
            assets=object(),
            cos=None,
            tts=None,
            asr=None,
            director=None,
            image_generator=None,
            audio_generator=None,
            renderer=None,
            process_supervisor=object(),
            stage_handlers={},
        )

        counts = run_reconciliation_pass(runtime, limit=10)

        queued = service_store.get_job_for_owner("alice", created["job_id"])
        self.assertEqual(counts["billing"], 1)
        self.assertEqual(queued["state"], "queued")
        self.assertEqual(queued["queued_at"], 1_002)
        self.assertEqual(len(ledger.calls), 1)

    def test_missing_processing_deadline_fails_without_running_attempt(self):
        from server.content_domains.ai_edit_v3.pipeline import run_job

        class Clock:
            def now(self):
                return 100.1

        class Supervisor:
            def __init__(self):
                self.terminated = []

            def terminate_job(self, job_id):
                self.terminated.append(job_id)

        self.seed_job("job-missing-deadline", "generating_voice", deadline=None)
        claim = self.store.claim_job(
            "job-missing-deadline",
            "worker-missing-deadline",
            30,
            100_000,
            expected_states={"generating_voice"},
        )
        supervisor = Supervisor()
        handler_calls = []
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
                "generating_voice": lambda job, context: handler_calls.append(job)
            },
        )
        error = None
        result = None
        try:
            result = run_job(claim, runtime, db_path=self.db)
        except Exception as exc:  # RED captures the current leaked TypeError.
            error = exc

        connection = self.store._connect()
        try:
            running = connection.execute(
                """SELECT COUNT(*) FROM edit_v3_stage_attempts
                   WHERE job_id=? AND status='running'""",
                (claim.job_id,),
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertIsNone(error)
        self.assertEqual(result.state, "failed")
        self.assertEqual(result.error_code, "processing_deadline_missing")
        self.assertEqual(running, 0)
        self.assertEqual(handler_calls, [])
        self.assertEqual(supervisor.terminated, [claim.job_id])
        self.assertEqual(self.row(claim.job_id)["state"], "failed")

        refund_claim = self.store.claim_job(
            claim.job_id,
            "worker-missing-deadline-refund",
            30,
            100_101,
            expected_states={"failed"},
        )
        refund = run_job(refund_claim, runtime, db_path=self.db)
        self.assertEqual(refund.state, "refunded")

    def test_media_failure_creates_and_processes_one_full_refund(self):
        from server.content_domains.ai_edit_v3.billing import (
            LedgerResult,
            LedgerTransaction,
        )
        from server.content_domains.ai_edit_v3.pipeline import (
            run_job,
            run_reconciliation_pass,
        )

        class Clock:
            def __init__(self):
                self.value = 100.1

            def now(self):
                return self.value

        class Ledger:
            def __init__(self):
                self.refund_calls = []

            def deduct(self, owner, amount, transaction_key, reason):
                raise AssertionError("already-preheld job must not debit")

            def refund(self, owner, amount, transaction_key, reason):
                self.refund_calls.append(
                    (owner, amount, transaction_key, reason)
                )
                return LedgerResult(
                    True,
                    LedgerTransaction(
                        transaction_key,
                        "refund",
                        owner,
                        amount,
                        100,
                        100_103,
                    ),
                    None,
                )

            def query_transaction(self, owner, transaction_key):
                raise AssertionError("pending refund must transmit before query")

        class Supervisor:
            def __init__(self):
                self.terminated = []

            def terminate_job(self, job_id):
                self.terminated.append(job_id)

        self.seed_job("job-full-refund", "queued")
        self.store._write(
            lambda connection: connection.execute(
                """UPDATE edit_v3_jobs SET confirmed_preheld_total=1
                   WHERE job_id='job-full-refund'"""
            )
        )
        first = self.store.claim_job(
            "job-full-refund",
            "worker-media-failure",
            30,
            100_000,
            expected_states={"queued"},
        )
        clock = Clock()
        ledger = Ledger()
        supervisor = Supervisor()
        runtime = RuntimeDependencies(
            store=self.store,
            clock=clock,
            points=ledger,
            assets=object(),
            cos=None,
            tts=None,
            asr=None,
            director=None,
            image_generator=None,
            audio_generator=None,
            renderer=None,
            process_supervisor=supervisor,
            stage_handlers={"queued": lambda job, context: 1 / 0},
        )

        failed = run_job(first, runtime, db_path=self.db)
        self.assertEqual(failed.state, "failed")
        clock.value = 100.102
        refund_claim = self.store.claim_job(
            first.job_id,
            "worker-refund-request",
            30,
            100_101,
            expected_states={"failed"},
        )
        requested = run_job(refund_claim, runtime, db_path=self.db)
        self.assertEqual(requested.state, "refund_pending")
        clock.value = 100.103
        counts = run_reconciliation_pass(
            runtime,
            worker_id="worker-refund-outbox",
            lease_seconds=30,
            limit=10,
        )

        job = self.row(first.job_id)
        intents = self.store._read(
            lambda connection: tuple(
                dict(row)
                for row in connection.execute(
                    """SELECT * FROM edit_v3_billing_intents
                       WHERE job_id=? AND operation='refund_full'""",
                    (first.job_id,),
                )
            )
        )
        self.assertEqual(counts["billing"], 1)
        self.assertEqual(job["state"], "refunded")
        self.assertEqual(job["confirmed_refunded_total"], 1)
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0]["status"], "completed")
        self.assertEqual(len(ledger.refund_calls), 1)
        self.assertEqual(supervisor.terminated, [first.job_id])

    def test_zero_prehold_full_refund_converges_without_outbox_poll(self):
        from server.content_domains.ai_edit_v3.pipeline import run_job

        class Clock:
            def now(self):
                return 100.1

        self.seed_job("job-zero-refund", "failed")
        claim = self.store.claim_job(
            "job-zero-refund",
            "worker-zero-refund",
            30,
            100_000,
            expected_states={"failed"},
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
            process_supervisor=object(),
            stage_handlers={},
        )

        result = run_job(claim, runtime, db_path=self.db)

        intent = self.store._read(
            lambda connection: dict(
                connection.execute(
                    """SELECT * FROM edit_v3_billing_intents
                       WHERE job_id=? AND operation='refund_full'""",
                    (claim.job_id,),
                ).fetchone()
            )
        )
        self.assertEqual(result.state, "refunded")
        self.assertEqual(self.row(claim.job_id)["state"], "refunded")
        self.assertEqual(intent["status"], "completed")
        self.assertEqual(intent["request_amount"], 0)

    def test_settling_without_durable_actual_charge_stays_fail_closed(self):
        from server.content_domains.ai_edit_v3.pipeline import run_job

        class Clock:
            def now(self):
                return 100.1

        self.seed_job("job-settling-no-charge", "settling")
        self.store._write(
            lambda connection: connection.execute(
                """UPDATE edit_v3_jobs SET confirmed_preheld_total=1
                   WHERE job_id='job-settling-no-charge'"""
            )
        )
        claim = self.store.claim_job(
            "job-settling-no-charge",
            "worker-settling",
            30,
            100_000,
            expected_states={"settling"},
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
            process_supervisor=object(),
            stage_handlers={},
        )

        result = run_job(claim, runtime, db_path=self.db)

        refund_count = self.store._read(
            lambda connection: connection.execute(
                """SELECT COUNT(*) FROM edit_v3_billing_intents
                   WHERE job_id=? AND operation='refund_delta'""",
                (claim.job_id,),
            ).fetchone()[0]
        )
        self.assertEqual(result.state, "settling")
        self.assertEqual(result.status, "safety_pending")
        self.assertEqual(result.error_code, "actual_charge_unavailable")
        self.assertEqual(refund_count, 0)
        self.assertIsNone(self.row(claim.job_id)["worker_id"])

    def test_pending_publication_is_advanced_before_asset_reconciliation(self):
        from server.content_domains.ai_edit_v3.delivery import (
            create_publish_intent,
        )
        from server.content_domains.ai_edit_v3.pipeline import (
            run_reconciliation_pass,
        )
        from server.content_domains.video_asset_publish import (
            PublicationDecision,
        )

        class Clock:
            def now(self):
                return 100.01

        class Publisher:
            def __init__(self):
                self.calls = []

            def register_generation(
                self, mode, source_job_id, generation, idempotency_key
            ):
                self.calls.append("register_generation")
                return PublicationDecision("accepted", generation, None)

            def prepare_hidden(
                self,
                mode,
                source_job_id,
                owner,
                object_key,
                generation,
                idempotency_key,
            ):
                self.calls.append("prepare_hidden")
                return PublicationDecision("accepted", generation, None)

            def commit_publish(
                self, mode, source_job_id, generation, idempotency_key
            ):
                self.calls.append("commit_publish")
                return PublicationDecision("publish_won", generation, "asset-42")

            def cancel_publish(self, *args):
                raise AssertionError("cancel must not run")

            def query_decision(self, *args):
                raise AssertionError("known pending work must advance before query")

        self.seed_publish_job("job-publish-pending")
        setup_claim = self.store.claim_job(
            "job-publish-pending",
            "worker-publish-setup",
            30,
            100_000,
            expected_states={"publishing"},
        )
        create_publish_intent(
            setup_claim,
            metadata_sha256="7" * 64,
            now=100_001,
            store=self.store,
        )
        self.store.begin_publish_operation(
            setup_claim, "register_generation", now_ms=100_002
        )
        self.store.record_publish_operation(
            setup_claim,
            "register_generation",
            "pending",
            {
                "outcome": "definitive_not_accepted",
                "reason_code": "definitive_not_accepted",
            },
            now_ms=100_003,
        )
        self.store.release_lease(setup_claim, 100_004)
        publisher = Publisher()
        runtime = RuntimeDependencies(
            store=self.store,
            clock=Clock(),
            points=object(),
            assets=publisher,
            cos=None,
            tts=None,
            asr=None,
            director=None,
            image_generator=None,
            audio_generator=None,
            renderer=None,
            process_supervisor=object(),
            stage_handlers={},
        )

        counts = run_reconciliation_pass(
            runtime,
            worker_id="worker-publish-pass",
            lease_seconds=30,
            limit=10,
        )

        self.assertEqual(counts["assets"], 1)
        self.assertEqual(self.row("job-publish-pending")["state"], "completed")
        self.assertEqual(
            self.row("job-publish-pending")["asset_id"], "asset-42"
        )
        self.assertEqual(
            publisher.calls,
            ["register_generation", "prepare_hidden", "commit_publish"],
        )

    def test_unknown_publication_uses_authoritative_decision_reconciliation(self):
        from server.content_domains.ai_edit_v3.delivery import advance_publish
        from server.content_domains.ai_edit_v3.pipeline import (
            run_reconciliation_pass,
        )
        from server.content_domains.ai_edit_v3.providers import SubmissionUnknown
        from server.content_domains.video_asset_publish import (
            PublicationDecision,
        )

        class Clock:
            def now(self):
                return 100.02

        class Publisher:
            def __init__(self):
                self.decision = None
                self.calls = []

            def register_generation(
                self, mode, source_job_id, generation, idempotency_key
            ):
                self.calls.append("register_generation")
                return PublicationDecision("accepted", generation, None)

            def prepare_hidden(
                self,
                mode,
                source_job_id,
                owner,
                object_key,
                generation,
                idempotency_key,
            ):
                self.calls.append("prepare_hidden")
                return PublicationDecision("accepted", generation, None)

            def commit_publish(
                self, mode, source_job_id, generation, idempotency_key
            ):
                self.calls.append("commit_publish")
                self.decision = PublicationDecision(
                    "publish_won", generation, "asset-unknown-recovered"
                )
                raise SubmissionUnknown("response_lost")

            def cancel_publish(self, *args):
                raise AssertionError("cancel must not run")

            def query_decision(self, mode, source_job_id, idempotency_key):
                self.calls.append("query_decision")
                return self.decision

        self.seed_publish_job("job-publish-unknown")
        setup_claim = self.store.claim_job(
            "job-publish-unknown",
            "worker-publish-unknown-setup",
            30,
            100_000,
            expected_states={"publishing"},
        )
        publisher = Publisher()
        progress = advance_publish(
            setup_claim,
            metadata_sha256="8" * 64,
            now=100_001,
            store=self.store,
            publisher=publisher,
        )
        self.assertEqual(progress.next_state, "asset_decision_reconciling")
        self.assertTrue(
            self.store.transition_leased(
                setup_claim,
                {"publishing"},
                progress.next_state,
                100_002,
                lease_seconds=30,
            )
        )
        self.store.release_lease(setup_claim, 100_003)
        runtime = RuntimeDependencies(
            store=self.store,
            clock=Clock(),
            points=object(),
            assets=publisher,
            cos=None,
            tts=None,
            asr=None,
            director=None,
            image_generator=None,
            audio_generator=None,
            renderer=None,
            process_supervisor=object(),
            stage_handlers={},
        )

        counts = run_reconciliation_pass(
            runtime,
            worker_id="worker-publish-unknown-pass",
            lease_seconds=30,
            limit=10,
        )

        job = self.row("job-publish-unknown")
        self.assertEqual(counts["assets"], 1)
        self.assertEqual(job["state"], "completed")
        self.assertEqual(job["asset_id"], "asset-unknown-recovered")
        self.assertEqual(publisher.calls.count("commit_publish"), 1)
        self.assertEqual(publisher.calls.count("query_decision"), 1)

    def test_disabled_reconciliation_completes_from_frozen_historical_authority(self):
        from server.content_domains.ai_edit_v3.pipeline import run_reconciliation_pass
        from server.content_domains.video_asset_publish import PublicationDecision

        job_id = "job-disabled-historical-publish-won"
        setup_claim = self.seed_unknown_publish_generation(job_id)
        frozen_key = (
            f"ai-edit-v3:{job_id}:publish:query:{setup_claim.fencing_token}"
        )

        class Clock:
            def now(self):
                return 100.02

        class Publisher:
            def __init__(self):
                self.calls = []

            def query_decision(self, mode, source_job_id, idempotency_key):
                self.calls.append(
                    (mode, source_job_id, idempotency_key)
                )
                return PublicationDecision(
                    "publish_won",
                    setup_claim.fencing_token,
                    "asset-historical-authority",
                )

            def __getattr__(self, name):
                raise AssertionError(f"disabled reconciliation called {name}")

        publisher = Publisher()
        runtime = RuntimeDependencies(
            store=self.store,
            clock=Clock(),
            points=object(),
            assets=publisher,
            cos=None,
            tts=None,
            asr=None,
            director=None,
            image_generator=None,
            audio_generator=None,
            renderer=None,
            process_supervisor=object(),
            stage_handlers={},
        )

        counts = run_reconciliation_pass(
            runtime,
            worker_id="worker-disabled-historical-publish",
            lease_seconds=30,
            limit=10,
            allow_new_work=False,
        )

        job = self.row(job_id)
        rows = self.publish_rows(job_id)
        self.assertEqual(counts["assets"], 1)
        self.assertEqual(job["state"], "completed")
        self.assertEqual(job["asset_id"], "asset-historical-authority")
        self.assertEqual(
            publisher.calls,
            [("ai_edit_v3", job_id, frozen_key)],
        )
        self.assertEqual(len(rows), 5)
        self.assertEqual(
            {row["publish_generation"] for row in rows},
            {setup_claim.fencing_token},
        )
        self.assertEqual({row["status"] for row in rows}, {"publish_won"})
        self.assertEqual(self.billing_rows(job_id), ())

    def test_disabled_reconciliation_recovers_unknown_before_state_transition(self):
        from server.content_domains.ai_edit_v3.pipeline import run_reconciliation_pass
        from server.content_domains.video_asset_publish import PublicationDecision

        job_id = "job-disabled-publishing-crash-window"
        setup_claim = self.seed_unknown_publish_generation(
            job_id, transition=False
        )
        frozen_key = (
            f"ai-edit-v3:{job_id}:publish:query:{setup_claim.fencing_token}"
        )

        class Clock:
            def now(self):
                return 100.02

        class Publisher:
            def __init__(self):
                self.calls = []

            def query_decision(self, mode, source_job_id, idempotency_key):
                self.calls.append((mode, source_job_id, idempotency_key))
                return PublicationDecision(
                    "publish_won",
                    setup_claim.fencing_token,
                    "asset-crash-window-recovered",
                )

            def __getattr__(self, name):
                raise AssertionError(f"disabled reconciliation called {name}")

        publisher = Publisher()
        runtime = RuntimeDependencies(
            store=self.store,
            clock=Clock(),
            points=object(),
            assets=publisher,
            cos=None,
            tts=None,
            asr=None,
            director=None,
            image_generator=None,
            audio_generator=None,
            renderer=None,
            process_supervisor=object(),
            stage_handlers={},
        )

        counts = run_reconciliation_pass(
            runtime,
            worker_id="worker-disabled-publishing-crash-window",
            lease_seconds=30,
            limit=10,
            allow_new_work=False,
        )

        job = self.row(job_id)
        rows = self.publish_rows(job_id)
        self.assertEqual(counts["assets"], 1)
        self.assertEqual(job["state"], "completed")
        self.assertEqual(job["asset_id"], "asset-crash-window-recovered")
        self.assertEqual(
            publisher.calls,
            [("ai_edit_v3", job_id, frozen_key)],
        )
        self.assertEqual(len(rows), 5)
        self.assertEqual(
            {row["publish_generation"] for row in rows},
            {setup_claim.fencing_token},
        )

    def test_disabled_reconciliation_fails_and_refunds_from_historical_cancel(self):
        from server.content_domains.ai_edit_v3.pipeline import run_reconciliation_pass
        from server.content_domains.video_asset_publish import PublicationDecision

        job_id = "job-disabled-historical-cancel-won"
        setup_claim = self.seed_unknown_publish_generation(job_id)
        frozen_key = (
            f"ai-edit-v3:{job_id}:publish:query:{setup_claim.fencing_token}"
        )

        class Clock:
            def now(self):
                return 100.02

        class Publisher:
            def __init__(self):
                self.calls = []

            def query_decision(self, mode, source_job_id, idempotency_key):
                self.calls.append(
                    (mode, source_job_id, idempotency_key)
                )
                return PublicationDecision(
                    "cancel_won", setup_claim.fencing_token, None
                )

            def __getattr__(self, name):
                raise AssertionError(f"disabled reconciliation called {name}")

        publisher = Publisher()
        runtime = RuntimeDependencies(
            store=self.store,
            clock=Clock(),
            points=object(),
            assets=publisher,
            cos=None,
            tts=None,
            asr=None,
            director=None,
            image_generator=None,
            audio_generator=None,
            renderer=None,
            process_supervisor=object(),
            stage_handlers={},
        )

        counts = run_reconciliation_pass(
            runtime,
            worker_id="worker-disabled-historical-cancel",
            lease_seconds=30,
            limit=10,
            allow_new_work=False,
        )

        job = self.row(job_id)
        rows = self.publish_rows(job_id)
        refunds = self.billing_rows(job_id)
        self.assertEqual(counts["assets"], 1)
        self.assertEqual(job["state"], "failed")
        self.assertIsNone(job["asset_id"])
        self.assertEqual(
            publisher.calls,
            [("ai_edit_v3", job_id, frozen_key)],
        )
        self.assertEqual(len(rows), 5)
        self.assertEqual(
            {row["publish_generation"] for row in rows},
            {setup_claim.fencing_token},
        )
        self.assertEqual({row["status"] for row in rows}, {"cancel_won"})
        self.assertEqual(len(refunds), 1)
        self.assertEqual(refunds[0]["operation"], "refund_full")
        self.assertEqual(refunds[0]["request_amount"], 1)
        self.assertEqual(refunds[0]["status"], "pending")

    def test_historical_publish_authority_rejects_stale_claim_write(self):
        job_id = "job-historical-authority-stale-claim"
        setup_claim = self.seed_unknown_publish_generation(job_id)
        claim = self.store.claim_job(
            job_id,
            "worker-historical-authority-first",
            30,
            100_004,
            expected_states={"asset_decision_reconciling"},
        )
        authority = self.store.get_historical_publish_authority_for_claim(
            claim, setup_claim.fencing_token, 100_005
        )
        self.assertEqual(
            authority["query"]["external_idempotency_key"],
            f"ai-edit-v3:{job_id}:publish:query:{setup_claim.fencing_token}",
        )
        before = self.publish_rows(job_id)
        self.assertTrue(self.store.release_lease(claim, 100_006))
        replacement = self.store.claim_job(
            job_id,
            "worker-historical-authority-replacement",
            30,
            100_007,
            expected_states={"asset_decision_reconciling"},
        )
        self.assertGreater(replacement.fencing_token, claim.fencing_token)

        with self.assertRaises(LeaseLost):
            self.store.record_historical_publish_authority(
                claim,
                setup_claim.fencing_token,
                {
                    "asset_id": None,
                    "current_generation": setup_claim.fencing_token,
                    "status": "accepted",
                },
                now_ms=100_008,
            )

        self.assertEqual(self.publish_rows(job_id), before)

    def test_historical_publish_authority_validates_frozen_row_identity(self):
        job_id = "job-historical-authority-corrupt-row"
        setup_claim = self.seed_unknown_publish_generation(job_id)
        claim = self.store.claim_job(
            job_id,
            "worker-historical-authority-corrupt",
            30,
            100_004,
            expected_states={"asset_decision_reconciling"},
        )
        self.store._write(
            lambda connection: connection.execute(
                """UPDATE edit_v3_publish_intents
                   SET external_idempotency_key=?
                   WHERE job_id=? AND publish_generation=?
                     AND operation='query_decision'""",
                (
                    f"ai-edit-v3:{job_id}:publish:query:corrupt",
                    job_id,
                    setup_claim.fencing_token,
                ),
            )
        )

        with self.assertRaises(StoreConflictError):
            self.store.get_historical_publish_authority_for_claim(
                claim, setup_claim.fencing_token, 100_005
            )

    def test_staging_checkpoint_crash_replay_converges_through_settlement_and_publish(self):
        from server.content_domains.ai_edit_v3.billing import (
            LedgerResult,
            LedgerTransaction,
        )
        from server.content_domains.ai_edit_v3.pipeline import (
            run_job,
            run_reconciliation_pass,
        )
        from server.content_domains.video_asset_publish import (
            PublicationDecision,
        )

        class Clock:
            def __init__(self):
                self.value = 100.1

            def now(self):
                return self.value

        class Ledger:
            def __init__(self):
                self.refund_calls = []

            def deduct(self, *args):
                raise AssertionError("settlement must not debit")

            def refund(self, owner, amount, transaction_key, reason):
                self.refund_calls.append(transaction_key)
                return LedgerResult(
                    True,
                    LedgerTransaction(
                        transaction_key,
                        "refund",
                        owner,
                        amount,
                        100,
                        100_301,
                    ),
                    None,
                )

            def query_transaction(self, *args):
                raise AssertionError("pending settlement refund transmits first")

        class Publisher:
            def __init__(self):
                self.calls = []

            def register_generation(
                self, mode, source_job_id, generation, idempotency_key
            ):
                self.calls.append("register_generation")
                return PublicationDecision("accepted", generation, None)

            def prepare_hidden(
                self,
                mode,
                source_job_id,
                owner,
                object_key,
                generation,
                idempotency_key,
            ):
                self.calls.append("prepare_hidden")
                return PublicationDecision("accepted", generation, None)

            def commit_publish(
                self, mode, source_job_id, generation, idempotency_key
            ):
                self.calls.append("commit_publish")
                return PublicationDecision(
                    "publish_won", generation, "asset-staging-replay"
                )

            def cancel_publish(self, *args):
                raise AssertionError("successful staging path must not cancel")

            def query_decision(self, *args):
                raise AssertionError("known publication must not query")

        class Supervisor:
            def __init__(self):
                self.terminated = []

            def terminate_job(self, job_id):
                self.terminated.append(job_id)

        self.seed_job("job-staging-replay", "staging_delivery")
        self.store._write(
            lambda connection: connection.execute(
                """UPDATE edit_v3_jobs SET confirmed_preheld_total=1
                   WHERE job_id='job-staging-replay'"""
            )
        )
        first = self.store.claim_job(
            "job-staging-replay",
            "worker-staging-first",
            30,
            100_000,
            expected_states={"staging_delivery"},
        )
        clock = Clock()
        ledger = Ledger()
        publisher = Publisher()
        supervisor = Supervisor()
        handler_calls = []
        checkpoint = {
            "actual_charge": 0,
            "metadata_sha256": "9" * 64,
            "delivery_object_key": (
                "test/ai-edit-v3/owner/job-staging-replay/delivery/final.mp4"
            ),
        }

        def staging_handler(job, context):
            handler_calls.append(context.claim.fencing_token)
            return StageOutcome("settling", checkpoint, "0" * 64)

        runtime = RuntimeDependencies(
            store=self.store,
            clock=clock,
            points=ledger,
            assets=publisher,
            cos=None,
            tts=None,
            asr=None,
            director=None,
            image_generator=None,
            audio_generator=None,
            renderer=None,
            process_supervisor=supervisor,
            stage_handlers={"staging_delivery": staging_handler},
        )
        transition = self.store.transition_leased
        interrupted = False

        def fail_once(claim, expected, target, now_ms, *, lease_seconds):
            nonlocal interrupted
            if not interrupted and target == "settling":
                interrupted = True
                return False
            return transition(
                claim, expected, target, now_ms, lease_seconds=lease_seconds
            )

        self.store.transition_leased = fail_once
        with self.assertRaises(LeaseLost):
            run_job(first, runtime, db_path=self.db)
        self.store.transition_leased = transition
        clock.value = 100.3
        second = self.store.claim_job(
            first.job_id,
            "worker-staging-replay",
            30,
            100_200,
            expected_states={"staging_delivery"},
        )

        replay = run_job(second, runtime, db_path=self.db)
        counts = run_reconciliation_pass(
            runtime,
            worker_id="worker-staging-converge",
            lease_seconds=30,
            limit=20,
        )

        job = self.row(first.job_id)
        checkpoint_count, running_count, delta_count = self.store._read(
            lambda connection: (
                connection.execute(
                    """SELECT COUNT(*) FROM edit_v3_checkpoints
                       WHERE job_id=? AND stage='staging_delivery'""",
                    (first.job_id,),
                ).fetchone()[0],
                connection.execute(
                    """SELECT COUNT(*) FROM edit_v3_stage_attempts
                       WHERE job_id=? AND status='running'""",
                    (first.job_id,),
                ).fetchone()[0],
                connection.execute(
                    """SELECT COUNT(*) FROM edit_v3_billing_intents
                       WHERE job_id=? AND operation='refund_delta'""",
                    (first.job_id,),
                ).fetchone()[0],
            )
        )
        self.assertIn(replay.state, {"settling", "publishing"})
        self.assertEqual(job["state"], "completed")
        self.assertEqual(job["asset_id"], "asset-staging-replay")
        self.assertEqual(job["delivery_object_key"], checkpoint["delivery_object_key"])
        self.assertEqual(handler_calls, [first.fencing_token])
        self.assertEqual(checkpoint_count, 1)
        self.assertEqual(running_count, 0)
        self.assertEqual(delta_count, 1)
        self.assertEqual(len(ledger.refund_calls), 1)
        self.assertEqual(counts["billing"], 1)
        self.assertEqual(counts["assets"], 1)
        self.assertEqual(
            publisher.calls,
            ["register_generation", "prepare_hidden", "commit_publish"],
        )

    def test_post_transition_settlement_error_reports_real_safety_state(self):
        from server.content_domains.ai_edit_v3.pipeline import run_job

        class Clock:
            def now(self):
                return 100.1

        class Supervisor:
            def __init__(self):
                self.terminated = []

            def terminate_job(self, job_id):
                self.terminated.append(job_id)

        self.seed_job("job-settlement-error", "staging_delivery")
        self.store._write(
            lambda connection: connection.execute(
                """UPDATE edit_v3_jobs SET confirmed_preheld_total=1
                   WHERE job_id='job-settlement-error'"""
            )
        )
        claim = self.store.claim_job(
            "job-settlement-error",
            "worker-settlement-error",
            30,
            100_000,
            expected_states={"staging_delivery"},
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
                "staging_delivery": lambda job, context: StageOutcome(
                    "settling",
                    {
                        "actual_charge": 0,
                        "metadata_sha256": "a" * 64,
                        "delivery_object_key": (
                            "test/ai-edit-v3/owner/job-settlement-error/"
                            "delivery/final.mp4"
                        ),
                    },
                    "0" * 64,
                )
            },
        )

        with patch(
            "server.content_domains.ai_edit_v3.pipeline.request_delta_refund",
            side_effect=RuntimeError("injected settlement failure"),
        ):
            result = run_job(claim, runtime, db_path=self.db)

        attempt = self.store._read(
            lambda connection: dict(
                connection.execute(
                    """SELECT * FROM edit_v3_stage_attempts
                       WHERE job_id=? AND stage='staging_delivery'""",
                    (claim.job_id,),
                ).fetchone()
            )
        )
        self.assertEqual(result.state, "settling")
        self.assertEqual(result.status, "safety_pending")
        self.assertEqual(result.error_code, "settlement_failed")
        self.assertEqual(self.row(claim.job_id)["state"], "settling")
        self.assertEqual(attempt["status"], "completed")
        self.assertEqual(supervisor.terminated, [])

    def test_delivery_key_freeze_rejects_cross_scope_and_path_syntax(self):
        invalid_keys = (
            "production/ai-edit-v3/owner/job/delivery/final.mp4",
            "test/ai-edit-v3/../other/final.mp4",
            "test/ai-edit-v3/owner\\job\\final.mp4",
            "test/ai-edit-v3/owner/job/final.mp4?token=opaque",
            "test/ai-edit-v3/owner/job/final.mp4#fragment",
        )
        for index, object_key in enumerate(invalid_keys):
            with self.subTest(object_key=object_key):
                job_id = f"job-invalid-delivery-key-{index}"
                self.seed_job(job_id, "staging_delivery")
                claim = self.store.claim_job(
                    job_id,
                    f"worker-invalid-delivery-key-{index}",
                    30,
                    100_000,
                    expected_states={"staging_delivery"},
                )
                with self.assertRaises(StoreConfigurationError):
                    self.store.freeze_delivery_object_key(
                        claim, object_key, 100_001
                    )
                self.assertIsNone(self.row(job_id)["delivery_object_key"])

    def atomic_failed_refund(self, claim, now_ms):
        operation = getattr(self.store, "freeze_failed_full_refund", None)
        self.assertTrue(
            callable(operation),
            "failed transition and full-refund intent must share one transaction",
        )
        return operation(claim, now_ms=now_ms)

    def test_zero_full_refund_intent_and_terminal_state_commit_atomically(self):
        self.seed_job("job-atomic-zero-refund", "failed")
        claim = self.store.claim_job(
            "job-atomic-zero-refund",
            "worker-atomic-zero-refund",
            30,
            100_000,
            expected_states={"failed"},
        )

        result = self.atomic_failed_refund(claim, 100_001)

        intent = self.store._read(
            lambda connection: dict(
                connection.execute(
                    """SELECT * FROM edit_v3_billing_intents
                       WHERE job_id=? AND operation='refund_full'""",
                    (claim.job_id,),
                ).fetchone()
            )
        )
        self.assertEqual(result["job"]["state"], "refunded")
        self.assertEqual(self.row(claim.job_id)["state"], "refunded")
        self.assertEqual(intent["status"], "completed")
        self.assertEqual(intent["request_amount"], 0)
        self.assertIsNone(self.row(claim.job_id)["worker_id"])

    def test_stale_claim_cannot_freeze_failed_full_refund(self):
        self.seed_job("job-atomic-stale-refund", "failed")
        self.store._write(
            lambda connection: connection.execute(
                """UPDATE edit_v3_jobs SET confirmed_preheld_total=1
                   WHERE job_id='job-atomic-stale-refund'"""
            )
        )
        stale = self.store.claim_job(
            "job-atomic-stale-refund",
            "worker-atomic-stale",
            30,
            100_000,
            expected_states={"failed"},
        )
        self.store.release_lease(stale, 100_001)
        current = self.store.claim_job(
            stale.job_id,
            "worker-atomic-current",
            30,
            100_002,
            expected_states={"failed"},
        )

        with self.assertRaises(LeaseLost):
            self.atomic_failed_refund(stale, 100_003)

        count = self.store._read(
            lambda connection: connection.execute(
                """SELECT COUNT(*) FROM edit_v3_billing_intents
                   WHERE job_id=? AND operation='refund_full'""",
                (stale.job_id,),
            ).fetchone()[0]
        )
        self.assertEqual(count, 0)
        self.assertEqual(self.row(stale.job_id)["state"], "failed")
        self.assertEqual(self.row(stale.job_id)["worker_id"], current.worker_id)

    def test_crash_before_atomic_refund_commit_rolls_back_state_and_intent(self):
        self.seed_job("job-atomic-refund-rollback", "failed")
        self.store._write(
            lambda connection: connection.execute(
                """UPDATE edit_v3_jobs SET confirmed_preheld_total=1
                   WHERE job_id='job-atomic-refund-rollback'"""
            )
        )
        claim = self.store.claim_job(
            "job-atomic-refund-rollback",
            "worker-atomic-refund-rollback",
            30,
            100_000,
            expected_states={"failed"},
        )
        operation = getattr(self.store, "freeze_failed_full_refund", None)
        self.assertTrue(callable(operation), "atomic refund operation is absent")
        original_write = self.store._write

        def inject_before_commit(write):
            def crash(connection):
                write(connection)
                raise RuntimeError("injected crash before atomic commit")

            return original_write(crash)

        with patch.object(self.store, "_write", side_effect=inject_before_commit):
            with self.assertRaisesRegex(RuntimeError, "injected crash"):
                operation(claim, now_ms=100_001)

        count = self.store._read(
            lambda connection: connection.execute(
                """SELECT COUNT(*) FROM edit_v3_billing_intents
                   WHERE job_id=? AND operation='refund_full'""",
                (claim.job_id,),
            ).fetchone()[0]
        )
        self.assertEqual(self.row(claim.job_id)["state"], "failed")
        self.assertEqual(count, 0)
        self.assertTrue(self.store.lease_owned(claim, 100_002))

    def test_committed_atomic_refund_restarts_and_refunds_exactly_once(self):
        from server.content_domains.ai_edit_v3.billing import (
            LedgerResult,
            LedgerTransaction,
        )
        from server.content_domains.ai_edit_v3.pipeline import (
            run_reconciliation_pass,
        )

        class Clock:
            def __init__(self):
                self.value = 31.0

            def now(self):
                return self.value

        class Ledger:
            def __init__(self):
                self.refund_calls = []

            def deduct(self, *args):
                raise AssertionError("failed recovery must not deduct")

            def refund(self, owner, amount, transaction_key, reason):
                self.refund_calls.append(transaction_key)
                return LedgerResult(
                    True,
                    LedgerTransaction(
                        transaction_key, "refund", owner, amount, 100, 31_001
                    ),
                    None,
                )

            def query_transaction(self, *args):
                raise AssertionError("pending refund must transmit before query")

        self.seed_job("job-atomic-refund-restart", "failed")
        self.store._write(
            lambda connection: connection.execute(
                """UPDATE edit_v3_jobs SET confirmed_preheld_total=1
                   WHERE job_id='job-atomic-refund-restart'"""
            )
        )
        crashed_claim = self.store.claim_job(
            "job-atomic-refund-restart",
            "worker-atomic-refund-crashed",
            30,
            100,
            expected_states={"failed"},
        )
        frozen = self.atomic_failed_refund(crashed_claim, 101)
        self.assertEqual(frozen["job"]["state"], "refund_pending")

        clock = Clock()
        ledger = Ledger()
        runtime = RuntimeDependencies(
            store=self.store,
            clock=clock,
            points=ledger,
            assets=object(),
            cos=None,
            tts=None,
            asr=None,
            director=None,
            image_generator=None,
            audio_generator=None,
            renderer=None,
            process_supervisor=object(),
            stage_handlers={},
        )
        first = run_reconciliation_pass(runtime, limit=10)
        clock.value = 31.1
        second = run_reconciliation_pass(runtime, limit=10)

        intents = self.store._read(
            lambda connection: tuple(
                dict(row)
                for row in connection.execute(
                    """SELECT * FROM edit_v3_billing_intents
                       WHERE job_id=? AND operation='refund_full'""",
                    (crashed_claim.job_id,),
                )
            )
        )
        self.assertEqual(first["billing"], 1)
        self.assertEqual(second["billing"], 0)
        self.assertEqual(self.row(crashed_claim.job_id)["state"], "refunded")
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0]["status"], "completed")
        self.assertEqual(len(ledger.refund_calls), 1)

    def test_failed_pipeline_uses_only_atomic_refund_store_operation(self):
        from server.content_domains.ai_edit_v3.pipeline import run_job

        class Clock:
            def now(self):
                return 100.1

        self.seed_job("job-pipeline-atomic-refund", "failed")
        self.store._write(
            lambda connection: connection.execute(
                """UPDATE edit_v3_jobs SET confirmed_preheld_total=1
                   WHERE job_id='job-pipeline-atomic-refund'"""
            )
        )
        claim = self.store.claim_job(
            "job-pipeline-atomic-refund",
            "worker-pipeline-atomic-refund",
            30,
            100_000,
            expected_states={"failed"},
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
            process_supervisor=object(),
            stage_handlers={},
        )

        with patch.object(
            self.store,
            "transition_leased",
            side_effect=AssertionError("split failed transition is forbidden"),
        ), patch(
            "server.content_domains.ai_edit_v3.pipeline.request_full_refund",
            create=True,
            side_effect=AssertionError("split full-refund creation is forbidden"),
        ):
            result = run_job(claim, runtime, db_path=self.db)

        count = self.store._read(
            lambda connection: connection.execute(
                """SELECT COUNT(*) FROM edit_v3_billing_intents
                   WHERE job_id=? AND operation='refund_full'""",
                (claim.job_id,),
            ).fetchone()[0]
        )
        self.assertEqual(result.state, "refund_pending")
        self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
