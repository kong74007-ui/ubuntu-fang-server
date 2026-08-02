from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from server.content_domains.ai_edit_v3.billing import LedgerResult, LedgerTransaction
from server.content_domains.ai_edit_v3.delivery import (
    advance_publish,
    create_publish_intent,
)
from server.content_domains.ai_edit_v3.feature import FeatureConfig
from server.content_domains.ai_edit_v3.runtime import Runtime, RuntimeDependencies
from server.content_domains.ai_edit_v3.contracts import LeaseClaim
from server.content_domains.ai_edit_v3.providers import SubmissionUnknown
from server.content_domains.ai_edit_v3.store import (
    LeaseLost,
    StoreConflictError,
    V3Store,
)
from server.content_domains.video_asset_publish import PublicationDecision


class FakeClock:
    def now(self):
        return 101.0


class FakeStore:
    def __init__(self):
        self.billing_queries = 0
        self.asset_queries = 0
        self.claim_calls = 0
        self.events = []

    def list_due_billing_intents(self, now, *, limit):
        self.billing_queries += 1
        self.events.append("billing")
        return []

    def list_due_publish_intents(self, now, *, limit, cursor=None):
        self.asset_queries += 1
        self.events.append("assets")
        return []

    def list_publication_ready_jobs(self, now, *, limit):
        return []

    def claim_next_job(self, worker_id, lease_seconds, now_ms):
        self.claim_calls += 1
        self.events.append("claim")
        return None


class StopAfterOneLoop:
    def __init__(self):
        self.waits = 0

    def is_set(self):
        return self.waits > 0

    def wait(self, timeout):
        self.waits += 1
        return True


class StopAfterThreeLoops:
    def __init__(self):
        self.waits = 0

    def is_set(self):
        return self.waits >= 3

    def wait(self, timeout):
        self.waits += 1
        return self.is_set()


class V3WorkerTests(unittest.TestCase):
    def real_store(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name).resolve()
        v2 = root / "ai_edit_v2.db"
        v2.write_bytes(b"V2 identity marker; never open")
        store = V3Store(
            root / "ai_edit_v3.db",
            v2_db_path=v2,
            environment="test",
        )
        store.insert_pricing_version(
            "price-v1",
            {"base": 1},
            status="published",
            created_at=1,
            published_at=1,
        )
        store.insert_quote(
            "alice",
            "quote-1",
            {},
            pricing_version="price-v1",
            min_points=1,
            max_points=1,
            breakdown={"base": 1},
            expires_at=9_999_999,
            created_at=1,
        )
        return store

    def seed_real_job(self, store, job_id, state, *, preheld=0):
        connection = store._connect()
        try:
            connection.execute(
                """INSERT INTO edit_v3_jobs(
                       job_id,environment,owner_id,state,normalized_request_json,
                       request_sha256,quote_id,idempotency_key,queued_at,
                       processing_deadline_at,confirmed_preheld_total,
                       created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    job_id,
                    "test",
                    "alice",
                    state,
                    "{}",
                    "0" * 64,
                    "quote-1",
                    f"key-{job_id}",
                    1,
                    5_000_000,
                    preheld,
                    1,
                    1,
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def real_job(self, store, job_id):
        return store._read(
            lambda connection: dict(
                connection.execute(
                    "SELECT * FROM edit_v3_jobs WHERE job_id=?", (job_id,)
                ).fetchone()
            )
        )

    def runtime_for_gate(self, store, *, enabled, ledger, publisher, clock=None):
        class Clock:
            def now(self):
                return 0.2

        dependencies = RuntimeDependencies(
            store=store,
            clock=clock or Clock(),
            points=ledger,
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
        return Runtime(
            config=FeatureConfig(enabled, None, None, "test", None, 1, 1, 1),
            dependencies=dependencies,
        )

    def assert_pending_predebit_is_gated(self, *, enabled, ready):
        from server.ai_edit_v3_worker import run_worker, worker_config

        store = self.real_store()
        created = store.create_job_with_predebit(
            "alice",
            f"job-gated-predebit-{enabled}",
            "quote-1",
            f"key-gated-predebit-{enabled}",
            {},
            now_ms=100,
            intent_id=f"intent-gated-predebit-{enabled}",
        )

        class Ledger:
            def __init__(self):
                self.deduct_calls = 0

            def deduct(self, owner, amount, transaction_key, reason):
                self.deduct_calls += 1
                return LedgerResult(
                    True,
                    LedgerTransaction(
                        transaction_key, "deduct", owner, amount, 99, 101
                    ),
                    None,
                )

            def refund(self, *args):
                raise AssertionError("pending pre-debit must not refund")

            def query_transaction(self, *args):
                raise AssertionError("pending pre-debit must not query")

        class Publisher:
            pass

        ledger = Ledger()
        runtime = self.runtime_for_gate(
            store,
            enabled=enabled,
            ledger=ledger,
            publisher=Publisher(),
        )
        preflight_result = SimpleNamespace(accepts_new_jobs=ready)
        with patch(
            "server.ai_edit_v3_worker.preflight",
            return_value=preflight_result,
        ):
            run_worker(StopAfterOneLoop(), config=worker_config(), runtime=runtime)

        job = self.real_job(store, created["job"]["job_id"])
        intent = store._read(
            lambda connection: dict(
                connection.execute(
                    "SELECT * FROM edit_v3_billing_intents WHERE id=?",
                    (created["intent"]["id"],),
                ).fetchone()
            )
        )
        self.assertEqual(ledger.deduct_calls, 0)
        self.assertEqual(job["state"], "created_draft")
        self.assertEqual(intent["status"], "pending")

    def seed_publication_ready_job(self, store, job_id):
        self.seed_real_job(store, job_id, "staging_delivery")
        claim = store.claim_job(
            job_id,
            "worker-publication-ready-setup",
            30,
            100,
            expected_states={"staging_delivery"},
        )
        checkpoint = {
            "actual_charge": 0,
            "metadata_sha256": "a" * 64,
            "delivery_object_key": (
                f"test/ai-edit-v3/owner/{job_id}/delivery/final.mp4"
            ),
        }
        attempt = store.start_stage_attempt(
            claim, "staging_delivery", "0" * 64, 101
        )
        store.save_checkpoint(
            claim,
            attempt["id"],
            "0" * 64,
            {
                "next_state": "settling",
                "checkpoint": checkpoint,
                "provider_evidence": None,
            },
            102,
        )
        store.freeze_delivery_object_key(
            claim, checkpoint["delivery_object_key"], 103
        )
        create_publish_intent(
            claim,
            metadata_sha256=checkpoint["metadata_sha256"],
            now=104,
            store=store,
        )
        store.finish_stage_attempt(claim, attempt["id"], "completed", 105)
        self.assertTrue(
            store.transition_leased(
                claim,
                {"staging_delivery"},
                "settling",
                106,
                lease_seconds=30,
            )
        )
        store.release_lease(claim, 107)

    def assert_publication_ready_is_gated(self, *, enabled, ready):
        from server.ai_edit_v3_worker import run_worker, worker_config

        store = self.real_store()
        job_id = f"job-gated-publication-{enabled}"
        self.seed_publication_ready_job(store, job_id)

        class Ledger:
            def deduct(self, *args):
                raise AssertionError("publication recovery must not deduct")

            def refund(self, *args):
                raise AssertionError("gated publication must not refund")

            def query_transaction(self, *args):
                raise AssertionError("gated publication must not query billing")

        class Publisher:
            def __init__(self):
                self.calls = []

            def register_generation(self, *args):
                self.calls.append("register_generation")
                generation = args[2]
                return PublicationDecision("accepted", generation, None)

            def prepare_hidden(self, *args):
                self.calls.append("prepare_hidden")
                generation = args[4]
                return PublicationDecision("accepted", generation, None)

            def commit_publish(self, *args):
                self.calls.append("commit_publish")
                generation = args[2]
                return PublicationDecision("publish_won", generation, "asset-gated")

            def cancel_publish(self, *args):
                self.calls.append("cancel_publish")
                raise AssertionError("gated publication must not cancel")

            def query_decision(self, *args):
                self.calls.append("query_decision")
                raise AssertionError("planned publication must not query")

        publisher = Publisher()
        runtime = self.runtime_for_gate(
            store,
            enabled=enabled,
            ledger=Ledger(),
            publisher=publisher,
        )
        with patch(
            "server.ai_edit_v3_worker.preflight",
            return_value=SimpleNamespace(accepts_new_jobs=ready),
        ):
            run_worker(StopAfterOneLoop(), config=worker_config(), runtime=runtime)

        self.assertEqual(publisher.calls, [])
        self.assertEqual(self.real_job(store, job_id)["state"], "settling")

    def test_disabled_worker_does_not_submit_pending_predebit(self):
        self.assert_pending_predebit_is_gated(enabled=False, ready=False)

    def test_not_ready_worker_does_not_submit_pending_predebit(self):
        self.assert_pending_predebit_is_gated(enabled=True, ready=False)

    def test_disabled_worker_does_not_advance_publication_ready_job(self):
        self.assert_publication_ready_is_gated(enabled=False, ready=False)

    def test_not_ready_worker_does_not_advance_publication_ready_job(self):
        self.assert_publication_ready_is_gated(enabled=True, ready=False)

    def assert_disabled_worker_reuses_unknown_publication_authority(
        self, query_outcome
    ):
        from server.ai_edit_v3_worker import run_worker, worker_config

        store = self.real_store()
        job_id = "job-gated-unknown-publication"
        self.seed_real_job(store, job_id, "publishing", preheld=1)
        setup_claim = store.claim_job(
            job_id,
            "worker-unknown-setup",
            30,
            100,
            expected_states={"publishing"},
        )
        store.freeze_delivery_object_key(
            setup_claim,
            f"test/ai-edit-v3/owner/{job_id}/delivery/final.mp4",
            101,
        )

        class Publisher:
            def __init__(self):
                self.calls = []
                self.query_keys = []

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

            def commit_publish(self, *args):
                self.calls.append("commit_publish")
                raise SubmissionUnknown("response_lost")

            def cancel_publish(self, *args):
                self.calls.append("cancel_publish")
                raise AssertionError("unknown publish must not cancel")

            def query_decision(self, mode, source_job_id, idempotency_key):
                self.calls.append("query_decision")
                self.query_keys.append(idempotency_key)
                current_generation = int(idempotency_key.rsplit(":", 1)[1])
                if query_outcome == "unknown":
                    raise SubmissionUnknown("authority_query_unknown")
                if query_outcome == "none":
                    return None
                return PublicationDecision("accepted", current_generation, None)

        publisher = Publisher()
        progress = advance_publish(
            setup_claim,
            metadata_sha256="b" * 64,
            now=102,
            store=store,
            publisher=publisher,
        )
        self.assertEqual(progress.next_state, "asset_decision_reconciling")
        self.assertTrue(
            store.transition_leased(
                setup_claim,
                {"publishing"},
                progress.next_state,
                103,
                lease_seconds=30,
            )
        )
        self.assertTrue(store.release_lease(setup_claim, 104))
        publisher.calls.clear()
        publisher.query_keys.clear()
        media_claims = []
        original_claim_next = store.claim_next_job

        def count_media_claim(*args, **kwargs):
            media_claims.append(args)
            return original_claim_next(*args, **kwargs)

        store.claim_next_job = count_media_claim
        runtime = self.runtime_for_gate(
            store,
            enabled=False,
            ledger=object(),
            publisher=publisher,
        )
        run_worker(StopAfterThreeLoops(), config=worker_config(), runtime=runtime)

        self.assertEqual(publisher.calls, ["query_decision"] * 3)
        self.assertEqual(
            publisher.query_keys,
            [
                f"ai-edit-v3:{job_id}:publish:query:"
                f"{setup_claim.fencing_token}"
            ]
            * 3,
        )
        self.assertEqual(media_claims, [])
        job = self.real_job(store, job_id)
        self.assertEqual(job["state"], "asset_decision_reconciling")
        self.assertEqual(job["fencing_token"], setup_claim.fencing_token + 3)
        publish_rows = store._read(
            lambda connection: tuple(
                dict(row)
                for row in connection.execute(
                    """SELECT * FROM edit_v3_publish_intents
                       WHERE job_id=? ORDER BY publish_generation,operation""",
                    (job_id,),
                )
            )
        )
        self.assertEqual(len(publish_rows), 5)
        self.assertEqual(
            {row["publish_generation"] for row in publish_rows},
            {setup_claim.fencing_token},
        )

    def test_disabled_worker_reuses_unknown_publication_accepted_authority(self):
        self.assert_disabled_worker_reuses_unknown_publication_authority("accepted")

    def test_disabled_worker_reuses_unknown_publication_no_verdict_authority(self):
        self.assert_disabled_worker_reuses_unknown_publication_authority("none")

    def test_disabled_worker_reuses_unknown_publication_query_unknown(self):
        self.assert_disabled_worker_reuses_unknown_publication_authority("unknown")

    def test_not_ready_worker_queries_unknown_billing_without_claiming_media(self):
        from server.ai_edit_v3_worker import run_worker, worker_config
        from server.content_domains.ai_edit_v3.billing import process_pending_intent

        store = self.real_store()
        created = store.create_job_with_predebit(
            "alice",
            "job-gated-unknown-billing",
            "quote-1",
            "key-gated-unknown-billing",
            {},
            now_ms=100,
            intent_id="intent-gated-unknown-billing",
        )
        setup_claim = store.claim_job(
            created["job"]["job_id"],
            "worker-billing-unknown-setup",
            30,
            101,
            expected_states={"created_draft"},
        )

        class Ledger:
            def __init__(self):
                self.deduct_calls = 0
                self.refund_calls = 0
                self.query_calls = 0

            def deduct(self, *args):
                self.deduct_calls += 1
                raise RuntimeError("injected unknown transmission")

            def refund(self, *args):
                self.refund_calls += 1
                raise AssertionError("unknown pre-debit must not refund")

            def query_transaction(self, *args):
                self.query_calls += 1
                raise RuntimeError("injected unknown authority query")

        ledger = Ledger()
        outcome = process_pending_intent(
            created["intent"]["id"],
            claim=setup_claim,
            ledger=ledger,
            now=102,
            store=store,
        )
        self.assertEqual(outcome.next_state, "billing_reconciling")
        store.release_lease(setup_claim, 103)
        ledger.deduct_calls = 0
        media_claims = []
        original_claim_next = store.claim_next_job

        def count_media_claim(*args, **kwargs):
            media_claims.append(args)
            return original_claim_next(*args, **kwargs)

        store.claim_next_job = count_media_claim
        runtime = self.runtime_for_gate(
            store,
            enabled=True,
            ledger=ledger,
            publisher=object(),
        )
        with patch(
            "server.ai_edit_v3_worker.preflight",
            return_value=SimpleNamespace(accepts_new_jobs=False),
        ):
            run_worker(StopAfterOneLoop(), config=worker_config(), runtime=runtime)

        self.assertEqual(ledger.query_calls, 1)
        self.assertEqual(ledger.deduct_calls, 0)
        self.assertEqual(ledger.refund_calls, 0)
        self.assertEqual(media_claims, [])
        self.assertEqual(
            self.real_job(store, created["job"]["job_id"])["state"],
            "billing_reconciling",
        )

    def test_billing_authority_pending_beyond_300_seconds_converges_without_media(self):
        from server.ai_edit_v3_worker import run_worker, worker_config
        from server.content_domains.ai_edit_v3.billing import process_pending_intent

        class MutableClock:
            def __init__(self):
                self.value = 300.102

            def now(self):
                return self.value

        class RecoveringLedger:
            def __init__(self):
                self.available = False
                self.deduct_calls = 0
                self.query_calls = 0

            def deduct(self, *args):
                self.deduct_calls += 1
                raise RuntimeError("ledger unavailable")

            def refund(self, *args):
                raise AssertionError("absent pre-debit must never refund")

            def query_transaction(self, *args):
                self.query_calls += 1
                if not self.available:
                    raise RuntimeError("ledger unavailable")
                return None

        store = self.real_store()
        created = store.create_job_with_predebit(
            "alice",
            "job-pending-convergence",
            "quote-1",
            "key-pending-convergence",
            {},
            now_ms=100,
            intent_id="intent-pending-convergence",
        )
        initial_claim = store.claim_job(
            created["job"]["job_id"],
            "pending-setup",
            30,
            101,
            expected_states={"created_draft"},
        )
        ledger = RecoveringLedger()
        unknown = process_pending_intent(
            created["intent"]["id"],
            claim=initial_claim,
            ledger=ledger,
            now=102,
            store=store,
        )
        self.assertEqual(unknown.next_state, "billing_reconciling")
        self.assertTrue(store.release_lease(initial_claim, 103))
        media_claims = []
        original_claim_next = store.claim_next_job

        def count_media_claim(*args, **kwargs):
            media_claims.append(args)
            return original_claim_next(*args, **kwargs)

        store.claim_next_job = count_media_claim
        clock = MutableClock()
        runtime = self.runtime_for_gate(
            store,
            enabled=False,
            ledger=ledger,
            publisher=object(),
            clock=clock,
        )

        run_worker(StopAfterOneLoop(), config=worker_config(), runtime=runtime)

        pending = self.real_job(store, created["job"]["job_id"])
        self.assertEqual(pending["state"], "failed_reconciliation_pending")
        self.assertEqual(media_claims, [])
        self.assertEqual(ledger.deduct_calls, 1)
        self.assertEqual(ledger.query_calls, 1)

        ledger.available = True
        clock.value = 300.103
        run_worker(StopAfterOneLoop(), config=worker_config(), runtime=runtime)

        converged = self.real_job(store, created["job"]["job_id"])
        self.assertEqual(converged["state"], "prehold_absent")
        self.assertEqual(media_claims, [])
        self.assertEqual(ledger.deduct_calls, 1)
        self.assertEqual(ledger.query_calls, 2)

    def test_worker_config_rejects_boolean_and_non_numeric_bounds(self):
        from server.ai_edit_v3_worker import WorkerConfig

        invalid = (
            {"lease_seconds": True},
            {"lease_seconds": 1.5},
            {"reconciliation_limit": False},
            {"reconciliation_limit": 1.5},
            {"queue_timeout_seconds": True},
            {"queue_timeout_seconds": 1.5},
            {"poll_interval_seconds": True},
            {"poll_interval_seconds": "0.1"},
        )
        failures = []
        for values in invalid:
            with self.subTest(values=values):
                try:
                    WorkerConfig(**values)
                except (TypeError, ValueError):
                    continue
                failures.append(values)
        self.assertEqual(failures, [])

    def test_disabled_worker_reconciles_but_never_claims_media(self):
        try:
            from server.ai_edit_v3_worker import run_worker, worker_config
        except ImportError as exc:
            self.fail(f"V3 worker entry point is absent: {exc}")

        store = FakeStore()
        dependencies = RuntimeDependencies(
            store=store,
            clock=FakeClock(),
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
        runtime = Runtime(
            config=FeatureConfig(False, None, None, "test", None, 1, 1, 1),
            dependencies=dependencies,
        )

        run_worker(StopAfterOneLoop(), config=worker_config(), runtime=runtime)

        self.assertEqual(store.billing_queries, 1)
        self.assertEqual(store.asset_queries, 1)
        self.assertEqual(store.claim_calls, 0)

    def test_ready_worker_reconciles_billing_and_assets_before_media_claim(self):
        from server.ai_edit_v3_worker import run_worker, worker_config

        store = FakeStore()
        dependencies = RuntimeDependencies(
            store=store,
            clock=FakeClock(),
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
        runtime = Runtime(
            config=FeatureConfig(True, None, None, "test", None, 3, 3, 1),
            dependencies=dependencies,
        )

        with patch(
            "server.ai_edit_v3_worker.preflight",
            return_value=SimpleNamespace(accepts_new_jobs=True),
        ):
            run_worker(StopAfterOneLoop(), config=worker_config(), runtime=runtime)

        self.assertEqual(store.events, ["billing", "assets", "claim"])
        self.assertEqual(store.claim_calls, 1)

    def test_typed_reconciliation_error_retries_before_any_media_claim(self):
        from server.ai_edit_v3_worker import run_worker, worker_config

        class StopAfterTwoLoops:
            def __init__(self):
                self.waits = 0

            def is_set(self):
                return self.waits > 1

            def wait(self, timeout):
                self.waits += 1
                return self.is_set()

        store = FakeStore()
        dependencies = RuntimeDependencies(
            store=store,
            clock=FakeClock(),
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
        runtime = Runtime(
            config=FeatureConfig(True, None, None, "test", None, 1, 1, 1),
            dependencies=dependencies,
        )
        reconciliation_error = StoreConflictError(
            "reconciliation_conflict", "injected typed conflict"
        )
        with patch(
            "server.ai_edit_v3_worker.run_reconciliation_pass",
            side_effect=[reconciliation_error, {"billing": 0, "assets": 0}],
        ) as reconcile, patch(
            "server.ai_edit_v3_worker.preflight",
            return_value=SimpleNamespace(accepts_new_jobs=True),
        ):
            run_worker(StopAfterTwoLoops(), config=worker_config(), runtime=runtime)

        self.assertEqual(reconcile.call_count, 2)
        self.assertEqual(store.claim_calls, 1)

    def test_due_intent_typed_errors_emit_redacted_v3_logs_through_worker(self):
        from server.ai_edit_v3_worker import run_worker, worker_config
        from server.content_domains.ai_edit_v3.billing import BillingError

        class DueStore(FakeStore):
            def __init__(self):
                super().__init__()
                self.released = []

            def list_due_billing_intents(self, now, *, limit):
                return (
                    {
                        "id": "intent-billing-private-123456",
                        "environment": "test",
                        "owner_id": "alice",
                        "job_id": "job-billing-private-123456",
                        "operation": "pre_debit",
                        "external_idempotency_key": "private-billing-key-123456",
                        "request_sha256": "0" * 64,
                        "refund_target_total": 0,
                        "request_amount": 1,
                        "status": "pending",
                        "first_unknown_at": None,
                        "last_checked_at": None,
                        "created_at": 1,
                        "updated_at": 1,
                        "completed_at": None,
                    },
                )

            def list_due_publish_intents(self, now, *, limit, cursor=None):
                return (
                    {
                        "id": "intent-publish-private-123456",
                        "job_id": "job-publish-private-123456",
                        "operation": "commit_publish",
                        "publish_generation": 1,
                        "metadata_sha256": "1" * 64,
                        "status": "pending",
                    },
                )

            def list_publication_ready_jobs(self, now, *, limit):
                return (
                    {
                        "job_id": "job-ready-private-123456",
                        "state": "settling",
                    },
                )

            def claim_job(
                self, job_id, worker_id, lease_seconds, now_ms, *, expected_states
            ):
                return LeaseClaim(job_id, worker_id, 1, now_ms + 30_000)

            def get_job_for_claim(self, claim, now_ms):
                return {
                    "state": "settling"
                    if claim.job_id.startswith("job-ready")
                    else "publishing",
                    "request_sha256": "0" * 64,
                    "repair_count": 0,
                    "repair_budget_granted_at": None,
                    "confirmed_preheld_total": 0,
                }

            def get_checkpoint_for_claim(self, claim, stage, input_sha256, now_ms):
                raise LeaseLost(
                    "lease_lost",
                    "https://private.example/token?secret=ready",
                )

            def lease_owned(self, claim, now_ms):
                return True

            def release_lease(self, claim, now_ms):
                self.released.append(claim.job_id)
                return True

        store = DueStore()
        dependencies = RuntimeDependencies(
            store=store,
            clock=FakeClock(),
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
        runtime = Runtime(
            config=FeatureConfig(True, None, None, "test", None, 1, 1, 1),
            dependencies=dependencies,
        )
        with patch(
            "server.ai_edit_v3_worker.preflight",
            return_value=SimpleNamespace(accepts_new_jobs=True),
        ), patch(
            "server.content_domains.ai_edit_v3.pipeline.process_pending_intent",
            side_effect=BillingError(
                "ledger_unavailable",
                "https://private.example/token?secret=billing",
            ),
        ), patch(
            "server.content_domains.ai_edit_v3.pipeline.advance_publish",
            side_effect=StoreConflictError(
                "publish_conflict",
                "https://private.example/token?secret=publish",
            ),
        ), self.assertLogs("ai-edit-v3", level="ERROR") as captured:
            run_worker(
                StopAfterOneLoop(), config=worker_config(), runtime=runtime
            )

        self.assertEqual(len(captured.output), 3)
        for operation, error_type in (
            ("pre_debit", "BillingError"),
            ("commit_publish", "StoreConflictError"),
            ("publication_ready", "LeaseLost"),
        ):
            self.assertTrue(
                any(
                    f"operation={operation}" in line
                    and f"error_type={error_type}" in line
                    and "identifier=" in line
                    for line in captured.output
                ),
                captured.output,
            )
        joined = "\n".join(captured.output)
        self.assertNotIn("private.example", joined)
        self.assertNotIn("private-123456", joined)

    def test_one_claim_lease_loss_does_not_block_peer_or_next_poll(self):
        from server.ai_edit_v3_worker import run_worker, worker_config

        class StopAfterTwoLoops:
            def __init__(self):
                self.waits = 0

            def is_set(self):
                return self.waits >= 2

            def wait(self, timeout):
                self.waits += 1
                return self.is_set()

        class ClaimStore(FakeStore):
            def __init__(self):
                super().__init__()
                self.cleaned = []
                self.released = []
                self.claims = [
                    LeaseClaim("job-lost", "worker", 1, 200_000),
                    LeaseClaim("job-peer", "worker", 1, 200_000),
                    LeaseClaim("job-next-poll", "worker", 1, 200_000),
                    None,
                ]

            def claim_next_job(self, worker_id, lease_seconds, now_ms):
                self.claim_calls += 1
                self.events.append("claim")
                return self.claims.pop(0)

            def lease_owned(self, claim, now_ms):
                return claim.job_id == "job-lost"

            def close_running_attempts(self, claim, now_ms):
                self.cleaned.append(claim.job_id)
                return 1

            def release_lease(self, claim, now_ms):
                self.released.append(claim.job_id)
                return True

        store = ClaimStore()
        dependencies = RuntimeDependencies(
            store=store,
            clock=FakeClock(),
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
        runtime = Runtime(
            config=FeatureConfig(True, None, None, "test", None, 2, 2, 1),
            dependencies=dependencies,
        )
        calls = []

        def execute(claim, *args, **kwargs):
            calls.append(claim.job_id)
            if claim.job_id == "job-lost":
                raise LeaseLost("lease_lost", "injected claim loss")
            return SimpleNamespace(state="normalizing")

        error = None
        with patch(
            "server.ai_edit_v3_worker.run_reconciliation_pass",
            return_value={"billing": 0, "assets": 0},
        ) as reconcile, patch(
            "server.ai_edit_v3_worker.preflight",
            return_value=SimpleNamespace(accepts_new_jobs=True),
        ), patch("server.ai_edit_v3_worker.run_job", side_effect=execute):
            try:
                run_worker(
                    StopAfterTwoLoops(), config=worker_config(), runtime=runtime
                )
            except Exception as exc:  # RED captures the escaped future error.
                error = exc

        self.assertIsNone(error)
        self.assertCountEqual(
            calls, ["job-lost", "job-peer", "job-next-poll"]
        )
        self.assertEqual(reconcile.call_count, 2)
        self.assertEqual(store.cleaned, ["job-lost"])
        self.assertEqual(store.released, ["job-lost"])


if __name__ == "__main__":
    unittest.main()
