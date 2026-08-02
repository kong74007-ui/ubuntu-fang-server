from __future__ import annotations

import importlib
import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import MappingProxyType

from server.content_domains.ai_edit_v3.providers import (
    DefinitiveNotAccepted,
    SubmissionUnknown,
)
from server.content_domains.ai_edit_v3.store import (
    LeaseLost,
    StoreConfigurationError,
    StoreConflictError,
    V3Store,
    is_valid_publish_asset_id,
    request_fingerprint,
)
from server.content_domains import video_asset_publish as shared_publish
from server.content_domains.video_asset_publish import PublicationDecision


METADATA_SHA256 = "7" * 64
OBJECT_KEY = "test/ai-edit-v3/owner/job-publish/delivery/final.mp4"


class EffectThenLostPublisher:
    def __init__(self) -> None:
        self.decision: PublicationDecision | None = None
        self.query_keys: list[str] = []

    def register_generation(self, mode, source_job_id, generation, idempotency_key):
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

    def commit_publish(self, mode, source_job_id, generation, idempotency_key):
        self.decision = PublicationDecision("publish_won", generation, "asset-42")
        raise SubmissionUnknown("response_lost")

    def cancel_publish(self, mode, source_job_id, generation, idempotency_key):
        raise AssertionError("cancel must not run on the publish-winner path")

    def query_decision(self, mode, source_job_id, idempotency_key):
        self.query_keys.append(idempotency_key)
        return self.decision


class ScriptedPublisher:
    def __init__(
        self,
        *,
        lose_once: tuple[str, ...] = (),
        unknown_always: tuple[str, ...] = (),
        definitive_once: tuple[str, ...] = (),
    ) -> None:
        self.lose_once = set(lose_once)
        self.unknown_always = set(unknown_always)
        self.definitive_once = set(definitive_once)
        self.calls: dict[str, list[str]] = {
            operation: []
            for operation in (
                "register_generation",
                "prepare_hidden",
                "commit_publish",
                "cancel_publish",
                "query_decision",
            )
        }
        self.order: list[str] = []
        self.generation: int | None = None
        self.prepared = False
        self.verdict: PublicationDecision | None = None

    def _finish(self, operation, key, decision):
        self.calls[operation].append(key)
        self.order.append(operation)
        if operation in self.definitive_once:
            self.definitive_once.remove(operation)
            raise DefinitiveNotAccepted("not_accepted")
        if operation in self.unknown_always:
            raise SubmissionUnknown("response_lost")
        if operation in self.lose_once:
            self.lose_once.remove(operation)
            raise SubmissionUnknown("response_lost")
        return decision

    def register_generation(self, mode, source_job_id, generation, idempotency_key):
        self.generation = generation
        return self._finish(
            "register_generation",
            idempotency_key,
            self.verdict or PublicationDecision("accepted", generation, None),
        )

    def prepare_hidden(
        self,
        mode,
        source_job_id,
        owner,
        object_key,
        generation,
        idempotency_key,
    ):
        self.prepared = True
        return self._finish(
            "prepare_hidden",
            idempotency_key,
            self.verdict or PublicationDecision("accepted", generation, None),
        )

    def commit_publish(self, mode, source_job_id, generation, idempotency_key):
        if self.prepared and self.verdict is None:
            self.verdict = PublicationDecision("publish_won", generation, "asset-42")
        decision = self.verdict or PublicationDecision("accepted", generation, None)
        return self._finish("commit_publish", idempotency_key, decision)

    def cancel_publish(self, mode, source_job_id, generation, idempotency_key):
        if self.verdict is None:
            self.verdict = PublicationDecision("cancel_won", generation, None)
        return self._finish("cancel_publish", idempotency_key, self.verdict)

    def query_decision(self, mode, source_job_id, idempotency_key):
        decision = self.verdict
        if decision is None and self.generation is not None:
            decision = PublicationDecision("accepted", self.generation, None)
        return self._finish("query_decision", idempotency_key, decision)


class LossySharedPublisher:
    def __init__(self, backend, *, lose_once=()):
        self.backend = backend
        self.lose_once = set(lose_once)
        self.calls = {name: [] for name in (
            "register_generation", "prepare_hidden", "commit_publish",
            "cancel_publish", "query_decision",
        )}

    def _call(self, operation, key, function):
        self.calls[operation].append(key)
        decision = function()
        if operation in self.lose_once:
            self.lose_once.remove(operation)
            raise SubmissionUnknown("response_lost")
        return decision

    def register_generation(self, mode, source_job_id, generation, idempotency_key):
        return self._call(
            "register_generation",
            idempotency_key,
            lambda: self.backend.register_generation(
                mode, source_job_id, generation, idempotency_key
            ),
        )

    def prepare_hidden(self, mode, source_job_id, owner, object_key, generation, idempotency_key):
        return self._call(
            "prepare_hidden",
            idempotency_key,
            lambda: self.backend.prepare_hidden(
                mode, source_job_id, owner, object_key, generation, idempotency_key
            ),
        )

    def commit_publish(self, mode, source_job_id, generation, idempotency_key):
        return self._call(
            "commit_publish",
            idempotency_key,
            lambda: self.backend.commit_publish(
                mode, source_job_id, generation, idempotency_key
            ),
        )

    def cancel_publish(self, mode, source_job_id, generation, idempotency_key):
        return self._call(
            "cancel_publish",
            idempotency_key,
            lambda: self.backend.cancel_publish(
                mode, source_job_id, generation, idempotency_key
            ),
        )

    def query_decision(self, mode, source_job_id, idempotency_key):
        return self._call(
            "query_decision",
            idempotency_key,
            lambda: self.backend.query_decision(mode, source_job_id, idempotency_key),
        )


class OrderedSharedPublisher(LossySharedPublisher):
    def __init__(self, backend, winner):
        super().__init__(backend)
        self.winner = winner
        self.commit_done = threading.Event()
        self.cancel_done = threading.Event()

    def commit_publish(self, mode, source_job_id, generation, idempotency_key):
        if self.winner == "cancel_won":
            if not self.cancel_done.wait(5):
                raise TimeoutError("cancel did not reach arbitration")
        decision = super().commit_publish(
            mode, source_job_id, generation, idempotency_key
        )
        self.commit_done.set()
        return decision

    def cancel_publish(self, mode, source_job_id, generation, idempotency_key):
        if self.winner == "publish_won":
            if not self.commit_done.wait(5):
                raise TimeoutError("commit did not reach arbitration")
        decision = super().cancel_publish(
            mode, source_job_id, generation, idempotency_key
        )
        self.cancel_done.set()
        return decision


class V3PublicationRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        self.root = root
        self.db = root / "ai-edit-v3.db"
        self.v2 = root / "ai-edit-v2.db"
        self.v2.write_bytes(b"V2 identity marker; never open")
        self.store = V3Store(self.db, v2_db_path=self.v2, environment="test")
        self.publisher = EffectThenLostPublisher()

    def real_shared_publisher(self):
        shared_db = self.root / "shared-assets.db"

        def connect():
            connection = sqlite3.connect(
                shared_db, timeout=10, isolation_level=None
            )
            connection.row_factory = sqlite3.Row
            return connection

        connection = connect()
        connection.execute(
            """CREATE TABLE video_assets(
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   job_id INTEGER UNIQUE,
                   username TEXT NOT NULL,
                   mode TEXT NOT NULL,
                   video_file TEXT,
                   phase TEXT,
                   status TEXT NOT NULL DEFAULT 'pending',
                   created_at INTEGER NOT NULL,
                   updated_at INTEGER NOT NULL
               )"""
        )
        shared_publish.init_schema(connection)
        connection.close()
        return shared_publish.AssetPublicationService(connect), connect

    def delivery_module(self):
        try:
            return importlib.import_module(
                "server.content_domains.ai_edit_v3.delivery"
            )
        except ModuleNotFoundError as exc:
            self.fail(f"delivery module missing for publication behavior: {exc}")

    def seed_publish_job(self, *, fencing_token: int = 0):
        request = {"input_type": "uploaded_video"}
        self.store.insert_pricing_version(
            "price-v1",
            {"base": 45},
            status="published",
            created_at=100,
            published_at=101,
        )
        self.store.insert_quote(
            "alice",
            "quote-1",
            request,
            pricing_version="price-v1",
            min_points=45,
            max_points=45,
            breakdown={"base": 45},
            expires_at=10_000,
            created_at=102,
        )
        self.store._write(
            lambda connection: connection.execute(
                """INSERT INTO edit_v3_jobs(
                       job_id,environment,owner_id,state,normalized_request_json,
                       request_sha256,quote_id,idempotency_key,fencing_token,
                       confirmed_preheld_total,confirmed_refunded_total,
                       delivery_object_key,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "job-publish",
                    "test",
                    "alice",
                    "publishing",
                    '{"input_type":"uploaded_video"}',
                    request_fingerprint(request),
                    "quote-1",
                    "job-key",
                    fencing_token,
                    45,
                    0,
                    OBJECT_KEY,
                    200,
                    200,
                ),
            )
        )
        claim = self.store.claim_job(
            "job-publish",
            "worker-1",
            10,
            900,
            expected_states={"publishing"},
        )
        self.assertIsNotNone(claim)
        return claim

    def test_publish_intent_freezes_all_five_keys_and_exact_replay(self):
        delivery = self.delivery_module()
        claim = self.seed_publish_job(fencing_token=4)

        first = delivery.create_publish_intent(
            claim,
            metadata_sha256=METADATA_SHA256,
            now=1_000,
            store=self.store,
        )
        replay = delivery.create_publish_intent(
            claim,
            metadata_sha256=METADATA_SHA256,
            now=1_001,
            store=self.store,
        )

        self.assertEqual(replay, first)
        self.assertEqual(
            [row["operation"] for row in first],
            [
                "register_generation",
                "prepare_hidden",
                "commit_publish",
                "cancel_publish",
                "query_decision",
            ],
        )
        self.assertEqual(
            [row["external_idempotency_key"] for row in first],
            [
                f"ai-edit-v3:{claim.job_id}:publish:register:{claim.fencing_token}",
                f"ai-edit-v3:{claim.job_id}:publish:prepare:{claim.fencing_token}",
                f"ai-edit-v3:{claim.job_id}:publish:commit:{claim.fencing_token}",
                f"ai-edit-v3:{claim.job_id}:publish:cancel:{claim.fencing_token}",
                f"ai-edit-v3:{claim.job_id}:publish:query:{claim.fencing_token}",
            ],
        )
        self.assertTrue(all(row["object_key"] == OBJECT_KEY for row in first))
        self.assertTrue(
            all(row["metadata_sha256"] == METADATA_SHA256 for row in first)
        )
        self.assertTrue(
            all(row["publish_generation"] == claim.fencing_token for row in first)
        )

        with self.assertRaises(StoreConflictError):
            delivery.create_publish_intent(
                claim,
                metadata_sha256="8" * 64,
                now=1_002,
                store=self.store,
            )
        persisted = self.store._read(
            lambda connection: connection.execute(
                """SELECT count(*),min(metadata_sha256),max(metadata_sha256)
                   FROM edit_v3_publish_intents WHERE job_id=?""",
                (claim.job_id,),
            ).fetchone()
        )
        self.assertEqual(tuple(persisted), (5, METADATA_SHA256, METADATA_SHA256))

    def test_publication_progress_is_slotted_immutable_and_deep_frozen(self):
        delivery = self.delivery_module()
        source = {"nested": {"items": [1, {"ok": True}]}}

        progress = delivery.PublicationProgress("publishing", source)
        source["nested"]["items"].append(2)

        self.assertIsInstance(progress.checkpoint, MappingProxyType)
        self.assertIsInstance(progress.checkpoint["nested"], MappingProxyType)
        self.assertEqual(progress.checkpoint["nested"]["items"], (1, MappingProxyType({"ok": True})))
        with self.assertRaises(TypeError):
            progress.checkpoint["new"] = "value"
        with self.assertRaises(FrozenInstanceError):
            progress.next_state = "completed"

    def test_lost_commit_response_recovers_publish_winner_without_refund(self):
        claim = self.seed_publish_job(fencing_token=8)
        delivery = self.delivery_module()

        outcome = delivery.advance_publish(
            claim,
            metadata_sha256=METADATA_SHA256,
            now=1_000,
            store=self.store,
            publisher=self.publisher,
        )
        self.assertEqual(outcome.next_state, "asset_decision_reconciling")

        recovered = delivery.reconcile_asset_decision(
            claim,
            now=1_010,
            store=self.store,
            publisher=self.publisher,
        )

        self.assertEqual(recovered.next_state, "completed")
        self.assertEqual(
            self.store.get_job_for_owner("alice", claim.job_id)["asset_id"],
            "asset-42",
        )
        self.assertEqual(
            self.store._read(
                lambda connection: connection.execute(
                    """SELECT count(*) FROM edit_v3_billing_intents
                       WHERE job_id=? AND operation='refund_full'""",
                    (claim.job_id,),
                ).fetchone()[0]
            ),
            0,
        )
        self.assertEqual(
            self.publisher.query_keys,
            [f"ai-edit-v3:{claim.job_id}:publish:query:{claim.fencing_token}"],
        )

    def test_register_and_prepare_effect_then_loss_resume_only_original_key(self):
        delivery = self.delivery_module()
        for operation in ("register_generation", "prepare_hidden"):
            with self.subTest(operation=operation):
                self.setUp()
                claim = self.seed_publish_job(fencing_token=2)
                publisher = ScriptedPublisher(lose_once=(operation,))

                unknown = delivery.advance_publish(
                    claim,
                    metadata_sha256=METADATA_SHA256,
                    now=1_000,
                    store=self.store,
                    publisher=publisher,
                )
                self.assertEqual(unknown.next_state, "asset_decision_reconciling")
                recovered = delivery.reconcile_asset_decision(
                    claim,
                    now=1_010,
                    store=self.store,
                    publisher=publisher,
                )

                self.assertEqual(recovered.next_state, "publishing")
                self.assertEqual(len(set(publisher.calls[operation])), 1)
                completed = delivery.advance_publish(
                    claim,
                    metadata_sha256=METADATA_SHA256,
                    now=1_020,
                    store=self.store,
                    publisher=publisher,
                )
                self.assertEqual(completed.next_state, "completed")

    def test_definitive_not_accepted_retries_only_the_frozen_key(self):
        delivery = self.delivery_module()
        claim = self.seed_publish_job(fencing_token=3)
        publisher = ScriptedPublisher(definitive_once=("register_generation",))

        absent = delivery.advance_publish(
            claim,
            metadata_sha256=METADATA_SHA256,
            now=1_000,
            store=self.store,
            publisher=publisher,
        )
        completed = delivery.advance_publish(
            claim,
            metadata_sha256=METADATA_SHA256,
            now=1_010,
            store=self.store,
            publisher=publisher,
        )

        self.assertEqual(absent.checkpoint["outcome"], "definitive_not_accepted")
        self.assertEqual(completed.next_state, "completed")
        self.assertEqual(len(publisher.calls["register_generation"]), 2)
        self.assertEqual(len(set(publisher.calls["register_generation"])), 1)

    def test_lost_query_response_stops_before_replaying_unknown_commit(self):
        delivery = self.delivery_module()
        claim = self.seed_publish_job(fencing_token=5)
        publisher = ScriptedPublisher(
            lose_once=("commit_publish", "query_decision")
        )
        delivery.advance_publish(
            claim,
            metadata_sha256=METADATA_SHA256,
            now=1_000,
            store=self.store,
            publisher=publisher,
        )

        first_recovery = delivery.reconcile_asset_decision(
            claim,
            now=1_010,
            store=self.store,
            publisher=publisher,
        )

        self.assertEqual(first_recovery.next_state, "asset_decision_reconciling")
        self.assertEqual(len(publisher.calls["commit_publish"]), 1)
        second_recovery = delivery.reconcile_asset_decision(
            claim,
            now=1_020,
            store=self.store,
            publisher=publisher,
        )
        self.assertEqual(second_recovery.next_state, "completed")
        self.assertEqual(len(set(publisher.calls["query_decision"])), 1)

    def billing_intent(self, operation):
        return self.store._read(
            lambda connection: connection.execute(
                """SELECT * FROM edit_v3_billing_intents
                   WHERE job_id='job-publish' AND operation=?""",
                (operation,),
            ).fetchone()
        )

    def test_cancel_winner_atomically_freezes_one_full_refund_without_state_change(self):
        delivery = self.delivery_module()
        claim = self.seed_publish_job(fencing_token=6)
        publisher = ScriptedPublisher()

        cancelled = delivery.request_cancel(
            claim,
            metadata_sha256=METADATA_SHA256,
            now=1_000,
            store=self.store,
            publisher=publisher,
        )
        replay = delivery.request_cancel(
            claim,
            metadata_sha256=METADATA_SHA256,
            now=1_010,
            store=self.store,
            publisher=publisher,
        )

        self.assertEqual(cancelled.next_state, "failed")
        self.assertEqual(replay.next_state, "failed")
        self.assertEqual(publisher.order[:2], ["register_generation", "cancel_publish"])
        self.assertEqual(len(publisher.calls["cancel_publish"]), 1)
        intent = self.billing_intent("refund_full")
        self.assertEqual(intent["external_idempotency_key"], "ai-edit-v3:job-publish:refund_full")
        self.assertEqual((intent["refund_target_total"], intent["request_amount"]), (45, 45))
        self.assertEqual((intent["reason"], intent["resume_state"]), ("refund", "refund_pending"))
        self.assertEqual(
            self.store.get_job_for_owner("alice", claim.job_id)["state"],
            "publishing",
        )

    def test_publish_winner_on_cancel_path_completes_and_never_refunds(self):
        delivery = self.delivery_module()
        claim = self.seed_publish_job(fencing_token=7)
        publisher = ScriptedPublisher()
        publisher.verdict = PublicationDecision("publish_won", 6, "asset-old")

        outcome = delivery.request_cancel(
            claim,
            metadata_sha256=METADATA_SHA256,
            now=1_000,
            store=self.store,
            publisher=publisher,
        )

        self.assertEqual(outcome.next_state, "completed")
        self.assertEqual(
            self.store.get_job_for_owner("alice", claim.job_id)["asset_id"],
            "asset-old",
        )
        self.assertIsNone(self.billing_intent("refund_full"))
        self.assertEqual(publisher.calls["cancel_publish"], [])

    def test_cancel_winner_refunds_only_remaining_delta_and_zero_is_completed(self):
        delivery = self.delivery_module()
        for already_refunded, expected_amount, expected_status in (
            (20, 25, "pending"),
            (45, 0, "completed"),
        ):
            with self.subTest(already_refunded=already_refunded):
                self.setUp()
                claim = self.seed_publish_job(fencing_token=4)
                if already_refunded:
                    self.store._write(
                        lambda connection: (
                            connection.execute(
                                """UPDATE edit_v3_jobs
                                   SET confirmed_refunded_total=? WHERE job_id='job-publish'""",
                                (already_refunded,),
                            ),
                            connection.execute(
                                """INSERT INTO edit_v3_billing_intents(
                                       id,environment,owner_id,job_id,operation,
                                       external_idempotency_key,request_sha256,
                                       refund_target_total,request_amount,status,
                                       authority_evidence_json,reason,resume_state,
                                       created_at,updated_at,completed_at
                                   )
                                   SELECT 'delta-id',environment,owner_id,job_id,
                                          'refund_delta','ai-edit-v3:job-publish:refund_delta',
                                          request_sha256,?,?, 'completed','{\"confirmed\":true}',
                                          'settlement','settling',300,300,300
                                   FROM edit_v3_jobs WHERE job_id='job-publish'""",
                                (already_refunded, already_refunded),
                            ),
                        )
                    )
                outcome = delivery.request_cancel(
                    claim,
                    metadata_sha256=METADATA_SHA256,
                    now=1_000,
                    store=self.store,
                    publisher=ScriptedPublisher(),
                )

                self.assertEqual(outcome.next_state, "failed")
                intent = self.billing_intent("refund_full")
                self.assertEqual(intent["refund_target_total"], 45)
                self.assertEqual(intent["request_amount"], expected_amount)
                self.assertEqual(intent["status"], expected_status)

    def test_unknown_cancel_does_not_create_refund_then_query_converges(self):
        delivery = self.delivery_module()
        claim = self.seed_publish_job(fencing_token=9)
        publisher = ScriptedPublisher(lose_once=("cancel_publish",))

        unknown = delivery.request_cancel(
            claim,
            metadata_sha256=METADATA_SHA256,
            now=1_000,
            store=self.store,
            publisher=publisher,
        )
        self.assertEqual(unknown.next_state, "asset_decision_reconciling")
        self.assertIsNone(self.billing_intent("refund_full"))

        recovered = delivery.reconcile_asset_decision(
            claim,
            now=1_010,
            store=self.store,
            publisher=publisher,
        )
        self.assertEqual(recovered.next_state, "failed")
        self.assertIsNotNone(self.billing_intent("refund_full"))

    def test_repeated_unknown_uses_original_time_at_299999_and_300000_ms(self):
        delivery = self.delivery_module()
        claim = self.seed_publish_job(fencing_token=10)
        publisher = ScriptedPublisher(
            unknown_always=("commit_publish", "query_decision")
        )
        first = delivery.advance_publish(
            claim,
            metadata_sha256=METADATA_SHA256,
            now=1_000,
            store=self.store,
            publisher=publisher,
        )
        self.assertTrue(self.store.renew_lease(claim, 1_000, 1_001))
        before = delivery.reconcile_asset_decision(
            claim,
            now=300_999,
            store=self.store,
            publisher=publisher,
        )
        at_limit = delivery.reconcile_asset_decision(
            claim,
            now=301_000,
            store=self.store,
            publisher=publisher,
        )

        self.assertEqual(first.checkpoint["first_unknown_at"], 1_000)
        self.assertEqual(before.next_state, "asset_decision_reconciling")
        self.assertEqual(at_limit.next_state, "failed_asset_decision_pending")
        commit_row = self.store._read(
            lambda connection: connection.execute(
                """SELECT first_unknown_at FROM edit_v3_publish_intents
                   WHERE job_id='job-publish' AND operation='commit_publish'"""
            ).fetchone()
        )
        self.assertEqual(commit_row["first_unknown_at"], 1_000)
        publisher.unknown_always.clear()
        late = delivery.reconcile_asset_decision(
            claim,
            now=301_010,
            store=self.store,
            publisher=publisher,
        )
        self.assertEqual(late.next_state, "completed")

    def test_timeout_with_only_accepted_query_never_replays_media_operation(self):
        delivery = self.delivery_module()
        claim = self.seed_publish_job(fencing_token=10)

        class NoVerdictPublisher(ScriptedPublisher):
            def commit_publish(
                self, mode, source_job_id, generation, idempotency_key
            ):
                self.calls["commit_publish"].append(idempotency_key)
                self.order.append("commit_publish")
                raise SubmissionUnknown("response_lost")

        publisher = NoVerdictPublisher()
        first = delivery.advance_publish(
            claim,
            metadata_sha256=METADATA_SHA256,
            now=1_000,
            store=self.store,
            publisher=publisher,
        )
        self.assertTrue(self.store.renew_lease(claim, 1_000, 1_001))

        timed_out = delivery.reconcile_asset_decision(
            claim,
            now=301_000,
            store=self.store,
            publisher=publisher,
        )

        self.assertEqual(first.next_state, "asset_decision_reconciling")
        self.assertEqual(timed_out.next_state, "failed_asset_decision_pending")
        self.assertEqual(len(publisher.calls["commit_publish"]), 1)
        self.assertEqual(len(publisher.calls["query_decision"]), 1)

        publisher.verdict = PublicationDecision("publish_won", 10, "asset-late")
        late = delivery.reconcile_asset_decision(
            claim,
            now=301_010,
            store=self.store,
            publisher=publisher,
        )
        self.assertEqual(late.next_state, "completed")
        self.assertEqual(len(publisher.calls["commit_publish"]), 1)

    def test_definitive_publish_retry_at_timeout_never_calls_shared_service(self):
        delivery = self.delivery_module()
        claim = self.seed_publish_job(fencing_token=20)

        class UnknownThenDefinitivePublish(ScriptedPublisher):
            def commit_publish(
                self, mode, source_job_id, generation, idempotency_key
            ):
                self.calls["commit_publish"].append(idempotency_key)
                self.order.append("commit_publish")
                if len(self.calls["commit_publish"]) == 1:
                    raise SubmissionUnknown("response_lost")
                if len(self.calls["commit_publish"]) == 2:
                    raise DefinitiveNotAccepted("not_accepted")
                raise AssertionError("timed-out publish must not be replayed")

        publisher = UnknownThenDefinitivePublish()
        first = delivery.advance_publish(
            claim,
            metadata_sha256=METADATA_SHA256,
            now=1_000,
            store=self.store,
            publisher=publisher,
        )
        self.assertTrue(self.store.renew_lease(claim, 400, 1_001))
        definitive = delivery.reconcile_asset_decision(
            claim,
            now=1_010,
            store=self.store,
            publisher=publisher,
        )
        timed_out = delivery.advance_publish(
            claim,
            metadata_sha256=METADATA_SHA256,
            now=301_000,
            store=self.store,
            publisher=publisher,
        )

        self.assertEqual(first.next_state, "asset_decision_reconciling")
        self.assertEqual(definitive.checkpoint["outcome"], "definitive_not_accepted")
        self.assertEqual(timed_out.next_state, "failed_asset_decision_pending")
        self.assertEqual(len(publisher.calls["commit_publish"]), 2)

    def test_definitive_cancel_retry_at_timeout_never_calls_shared_service(self):
        delivery = self.delivery_module()
        claim = self.seed_publish_job(fencing_token=21)

        class UnknownThenDefinitiveCancel(ScriptedPublisher):
            def cancel_publish(
                self, mode, source_job_id, generation, idempotency_key
            ):
                self.calls["cancel_publish"].append(idempotency_key)
                self.order.append("cancel_publish")
                if len(self.calls["cancel_publish"]) == 1:
                    raise SubmissionUnknown("response_lost")
                if len(self.calls["cancel_publish"]) == 2:
                    raise DefinitiveNotAccepted("not_accepted")
                raise AssertionError("timed-out cancel must not be replayed")

        publisher = UnknownThenDefinitiveCancel()
        first = delivery.request_cancel(
            claim,
            metadata_sha256=METADATA_SHA256,
            now=1_000,
            store=self.store,
            publisher=publisher,
        )
        self.assertTrue(self.store.renew_lease(claim, 400, 1_001))
        definitive = delivery.reconcile_asset_decision(
            claim,
            now=1_010,
            store=self.store,
            publisher=publisher,
        )
        timed_out = delivery.request_cancel(
            claim,
            metadata_sha256=METADATA_SHA256,
            now=301_000,
            store=self.store,
            publisher=publisher,
        )

        self.assertEqual(first.next_state, "asset_decision_reconciling")
        self.assertEqual(definitive.checkpoint["outcome"], "definitive_not_accepted")
        self.assertEqual(timed_out.next_state, "failed_asset_decision_pending")
        self.assertEqual(len(publisher.calls["cancel_publish"]), 2)
        self.assertIsNone(self.billing_intent("refund_full"))

    def test_earlier_register_unknown_blocks_later_planned_work_at_timeout(self):
        delivery = self.delivery_module()
        claim = self.seed_publish_job(fencing_token=23)
        publisher = ScriptedPublisher(lose_once=("register_generation",))

        first = delivery.advance_publish(
            claim,
            metadata_sha256=METADATA_SHA256,
            now=1_000,
            store=self.store,
            publisher=publisher,
        )
        self.assertTrue(self.store.renew_lease(claim, 400, 1_001))
        accepted = delivery.reconcile_asset_decision(
            claim,
            now=1_010,
            store=self.store,
            publisher=publisher,
        )
        timed_out = delivery.advance_publish(
            claim,
            metadata_sha256=METADATA_SHA256,
            now=301_000,
            store=self.store,
            publisher=publisher,
        )

        self.assertEqual(first.next_state, "asset_decision_reconciling")
        self.assertEqual(accepted.checkpoint["outcome"], "accepted")
        self.assertEqual(timed_out.next_state, "failed_asset_decision_pending")
        self.assertEqual(publisher.calls["prepare_hidden"], [])
        self.assertEqual(publisher.calls["commit_publish"], [])

    def test_pure_definitive_not_accepted_has_no_synthetic_timeout(self):
        delivery = self.delivery_module()
        claim = self.seed_publish_job(fencing_token=24)

        class DefinitiveThenPublish(ScriptedPublisher):
            def commit_publish(
                self, mode, source_job_id, generation, idempotency_key
            ):
                self.calls["commit_publish"].append(idempotency_key)
                self.order.append("commit_publish")
                if len(self.calls["commit_publish"]) == 1:
                    raise DefinitiveNotAccepted("not_accepted")
                return PublicationDecision("publish_won", generation, "asset-24")

        publisher = DefinitiveThenPublish()
        first = delivery.advance_publish(
            claim,
            metadata_sha256=METADATA_SHA256,
            now=1_000,
            store=self.store,
            publisher=publisher,
        )
        self.assertTrue(self.store.renew_lease(claim, 400, 1_001))
        row = self.store._read(
            lambda connection: connection.execute(
                """SELECT first_unknown_at FROM edit_v3_publish_intents
                   WHERE job_id='job-publish' AND operation='commit_publish'"""
            ).fetchone()
        )
        replay = delivery.advance_publish(
            claim,
            metadata_sha256=METADATA_SHA256,
            now=301_000,
            store=self.store,
            publisher=publisher,
        )

        self.assertEqual(first.checkpoint["outcome"], "definitive_not_accepted")
        self.assertIsNone(row["first_unknown_at"])
        self.assertEqual(replay.next_state, "completed")
        self.assertEqual(len(publisher.calls["commit_publish"]), 2)

    def test_stale_claims_before_and_after_intent_never_call_shared_service(self):
        delivery = self.delivery_module()
        for after_intent in (False, True):
            with self.subTest(after_intent=after_intent):
                self.setUp()
                old = self.seed_publish_job(fencing_token=1)
                if after_intent:
                    delivery.create_publish_intent(
                        old,
                        metadata_sha256=METADATA_SHA256,
                        now=950,
                        store=self.store,
                    )
                self.store._write(
                    lambda connection: connection.execute(
                        """UPDATE edit_v3_jobs SET lease_until=1000
                           WHERE job_id='job-publish'"""
                    )
                )
                successor = self.store.claim_job(
                    old.job_id,
                    "worker-2",
                    10,
                    1_000,
                    expected_states={"publishing"},
                )
                self.assertIsNotNone(successor)
                publisher = ScriptedPublisher()

                with self.assertRaises(LeaseLost):
                    delivery.advance_publish(
                        old,
                        metadata_sha256=METADATA_SHA256,
                        now=1_001,
                        store=self.store,
                        publisher=publisher,
                    )

                self.assertTrue(all(not calls for calls in publisher.calls.values()))
                self.assertIsNone(self.billing_intent("refund_full"))
                rows = self.store._read(
                    lambda connection: tuple(
                        connection.execute(
                            """SELECT status,last_decision_json
                               FROM edit_v3_publish_intents"""
                        )
                    )
                )
                self.assertTrue(
                    not rows
                    or all(
                        row["status"] == "planned"
                        and row["last_decision_json"] is None
                        for row in rows
                    )
                )

    def test_process_restart_recovers_real_shared_commit_with_original_query_key(self):
        delivery = self.delivery_module()
        claim = self.seed_publish_job(fencing_token=2)
        backend, connect = self.real_shared_publisher()
        first_process = LossySharedPublisher(
            backend, lose_once=("commit_publish",)
        )

        unknown = delivery.advance_publish(
            claim,
            metadata_sha256=METADATA_SHA256,
            now=1_000,
            store=self.store,
            publisher=first_process,
        )
        second_process = LossySharedPublisher(backend)
        recovered = delivery.reconcile_asset_decision(
            claim,
            now=1_010,
            store=self.store,
            publisher=second_process,
        )

        self.assertEqual(unknown.next_state, "asset_decision_reconciling")
        self.assertEqual(recovered.next_state, "completed")
        expected_query = f"ai-edit-v3:{claim.job_id}:publish:query:{claim.fencing_token}"
        expected_commit = f"ai-edit-v3:{claim.job_id}:publish:commit:{claim.fencing_token}"
        self.assertEqual(second_process.calls["query_decision"], [expected_query])
        with closing(connect()) as connection:
            self.assertEqual(
                connection.execute(
                    """SELECT count(*) FROM video_assets
                       WHERE source_job_id='job-publish'"""
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    """SELECT count(*) FROM video_asset_publication_ops
                       WHERE idempotency_key=?""",
                    (expected_commit,),
                ).fetchone()[0],
                1,
            )

    def test_old_worker_publish_won_before_takeover_converges_without_refund(self):
        delivery = self.delivery_module()
        old = self.seed_publish_job(fencing_token=3)
        backend, _connect = self.real_shared_publisher()
        first_process = LossySharedPublisher(
            backend, lose_once=("commit_publish",)
        )
        delivery.advance_publish(
            old,
            metadata_sha256=METADATA_SHA256,
            now=1_000,
            store=self.store,
            publisher=first_process,
        )
        self.store._write(
            lambda connection: connection.execute(
                """UPDATE edit_v3_jobs
                   SET state='asset_decision_reconciling',lease_until=1010
                   WHERE job_id='job-publish'"""
            )
        )
        successor = self.store.claim_job(
            old.job_id,
            "worker-2",
            10,
            1_010,
            expected_states={"asset_decision_reconciling"},
        )
        self.assertIsNotNone(successor)
        delivery.create_publish_intent(
            successor,
            metadata_sha256=METADATA_SHA256,
            now=1_011,
            store=self.store,
        )

        recovered = delivery.reconcile_asset_decision(
            successor,
            now=1_012,
            store=self.store,
            publisher=LossySharedPublisher(backend),
        )

        self.assertEqual(recovered.next_state, "completed")
        self.assertIsNotNone(
            self.store.get_job_for_owner("alice", old.job_id)["asset_id"]
        )
        self.assertIsNone(self.billing_intent("refund_full"))

    def test_frozen_query_key_refreshes_late_cancel_verdict_after_timeout(self):
        delivery = self.delivery_module()
        claim = self.seed_publish_job(fencing_token=22)
        backend, connect = self.real_shared_publisher()

        class CancelUnknownBeforeEffect(LossySharedPublisher):
            def cancel_publish(
                self, mode, source_job_id, generation, idempotency_key
            ):
                self.calls["cancel_publish"].append(idempotency_key)
                raise SubmissionUnknown("response_lost")

        cancel_unknown = CancelUnknownBeforeEffect(backend)
        first = delivery.request_cancel(
            claim,
            metadata_sha256=METADATA_SHA256,
            now=1_000,
            store=self.store,
            publisher=cancel_unknown,
        )
        self.assertTrue(self.store.renew_lease(claim, 400, 1_001))

        first_query = LossySharedPublisher(
            backend, lose_once=("query_decision",)
        )
        query_lost = delivery.reconcile_asset_decision(
            claim,
            now=1_010,
            store=self.store,
            publisher=first_query,
        )
        cancel_key = cancel_unknown.calls["cancel_publish"][0]
        external_winner = backend.cancel_publish(
            "ai_edit_v3",
            claim.job_id,
            claim.fencing_token,
            cancel_key,
        )

        late_query = LossySharedPublisher(backend)
        late = delivery.reconcile_asset_decision(
            claim,
            now=301_000,
            store=self.store,
            publisher=late_query,
        )

        expected_query_key = (
            f"ai-edit-v3:{claim.job_id}:publish:query:{claim.fencing_token}"
        )
        self.assertEqual(first.next_state, "asset_decision_reconciling")
        self.assertEqual(query_lost.next_state, "asset_decision_reconciling")
        self.assertEqual(external_winner.status, "cancel_won")
        self.assertEqual(first_query.calls["query_decision"], [expected_query_key])
        self.assertEqual(late_query.calls["query_decision"], [expected_query_key])
        self.assertEqual(late.next_state, "failed")
        self.assertIsNotNone(self.billing_intent("refund_full"))
        with closing(connect()) as connection:
            self.assertEqual(
                connection.execute(
                    """SELECT count(*) FROM video_asset_publication_ops
                       WHERE idempotency_key=?""",
                    (expected_query_key,),
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    """SELECT count(*) FROM video_assets
                       WHERE source_job_id='job-publish'"""
                ).fetchone()[0],
                0,
            )

    def test_real_shared_sqlite_concurrent_publish_cancel_has_one_local_outcome(self):
        delivery = self.delivery_module()
        for winner, expected_state in (
            ("publish_won", "completed"),
            ("cancel_won", "failed"),
        ):
            with self.subTest(winner=winner):
                self.setUp()
                claim = self.seed_publish_job(fencing_token=5)
                backend, connect = self.real_shared_publisher()
                publisher = OrderedSharedPublisher(backend, winner)
                prepared = delivery.prepare_hidden(
                    claim,
                    metadata_sha256=METADATA_SHA256,
                    now=999,
                    store=self.store,
                    publisher=publisher,
                )
                self.assertEqual(prepared.checkpoint["outcome"], "accepted")
                barrier = threading.Barrier(2)

                def publish_call():
                    barrier.wait()
                    return delivery.advance_publish(
                        claim,
                        metadata_sha256=METADATA_SHA256,
                        now=1_000,
                        store=self.store,
                        publisher=publisher,
                    )

                def cancel_call():
                    barrier.wait()
                    return delivery.request_cancel(
                        claim,
                        metadata_sha256=METADATA_SHA256,
                        now=1_000,
                        store=self.store,
                        publisher=publisher,
                    )

                with ThreadPoolExecutor(max_workers=2) as executor:
                    publish_future = executor.submit(publish_call)
                    cancel_future = executor.submit(cancel_call)
                    outcomes = (
                        publish_future.result(timeout=10),
                        cancel_future.result(timeout=10),
                    )

                converged = tuple(
                    outcome
                    if outcome.next_state == expected_state
                    else delivery.reconcile_asset_decision(
                        claim,
                        now=1_010,
                        store=self.store,
                        publisher=publisher,
                    )
                    for outcome in outcomes
                )
                self.assertTrue(
                    all(outcome.next_state == expected_state for outcome in converged),
                    converged,
                )
                with closing(connect()) as connection:
                    visible = connection.execute(
                        """SELECT count(*) FROM video_assets
                           WHERE source_job_id='job-publish'"""
                    ).fetchone()[0]
                self.assertEqual(visible, 1 if winner == "publish_won" else 0)
                self.assertEqual(
                    self.billing_intent("refund_full") is not None,
                    winner == "cancel_won",
                )

    def test_ambiguous_error_is_redacted_and_process_control_propagates(self):
        delivery = self.delivery_module()
        claim = self.seed_publish_job(fencing_token=11)

        class SecretFailure(ScriptedPublisher):
            def register_generation(self, *args):
                raise RuntimeError("https://signed.invalid/?token=TOP-SECRET")

        unknown = delivery.advance_publish(
            claim,
            metadata_sha256=METADATA_SHA256,
            now=1_000,
            store=self.store,
            publisher=SecretFailure(),
        )
        self.assertEqual(unknown.next_state, "asset_decision_reconciling")
        dump = self.store._read(
            lambda connection: "\n".join(connection.iterdump())
        )
        self.assertNotIn("TOP-SECRET", dump)
        self.assertNotIn("signed.invalid", dump)

        self.setUp()
        claim = self.seed_publish_job(fencing_token=12)

        class StopProcess(ScriptedPublisher):
            def register_generation(self, *args):
                raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            delivery.advance_publish(
                claim,
                metadata_sha256=METADATA_SHA256,
                now=1_000,
                store=self.store,
                publisher=StopProcess(),
            )
        row = self.store._read(
            lambda connection: connection.execute(
                """SELECT status,first_unknown_at,last_decision_json
                   FROM edit_v3_publish_intents
                   WHERE operation='register_generation'"""
            ).fetchone()
        )
        self.assertEqual(tuple(row), ("pending", None, None))

    def test_corrupted_existing_full_refund_is_rejected_on_cancel_replay(self):
        delivery = self.delivery_module()
        claim = self.seed_publish_job(fencing_token=13)
        publisher = ScriptedPublisher()
        delivery.request_cancel(
            claim,
            metadata_sha256=METADATA_SHA256,
            now=1_000,
            store=self.store,
            publisher=publisher,
        )
        self.store._write(
            lambda connection: connection.execute(
                """UPDATE edit_v3_billing_intents
                   SET request_amount=request_amount+1
                   WHERE operation='refund_full'"""
            )
        )

        with self.assertRaises(StoreConflictError):
            delivery.request_cancel(
                claim,
                metadata_sha256=METADATA_SHA256,
                now=1_010,
                store=self.store,
                publisher=publisher,
            )

    def test_completed_full_refund_replay_keeps_original_request_amount(self):
        delivery = self.delivery_module()
        claim = self.seed_publish_job(fencing_token=14)
        publisher = ScriptedPublisher()
        first = delivery.request_cancel(
            claim,
            metadata_sha256=METADATA_SHA256,
            now=1_000,
            store=self.store,
            publisher=publisher,
        )
        self.store._write(
            lambda connection: (
                connection.execute(
                    """UPDATE edit_v3_jobs SET confirmed_refunded_total=45
                       WHERE job_id='job-publish'"""
                ),
                connection.execute(
                    """UPDATE edit_v3_billing_intents
                       SET status='completed',completed_at=1005
                       WHERE operation='refund_full'"""
                ),
            )
        )

        replay = delivery.request_cancel(
            claim,
            metadata_sha256=METADATA_SHA256,
            now=1_010,
            store=self.store,
            publisher=publisher,
        )

        self.assertEqual((first.next_state, replay.next_state), ("failed", "failed"))
        self.assertEqual(self.billing_intent("refund_full")["request_amount"], 45)

    def test_higher_generation_response_stops_without_saving_or_side_effects(self):
        delivery = self.delivery_module()
        claim = self.seed_publish_job(fencing_token=15)

        class HigherGeneration(ScriptedPublisher):
            def register_generation(self, mode, source_job_id, generation, key):
                self.calls["register_generation"].append(key)
                self.order.append("register_generation")
                return PublicationDecision("stale_generation", generation + 1, None)

        publisher = HigherGeneration()
        outcome = delivery.advance_publish(
            claim,
            metadata_sha256=METADATA_SHA256,
            now=1_000,
            store=self.store,
            publisher=publisher,
        )

        self.assertEqual(outcome.checkpoint["outcome"], "stale_generation")
        self.assertEqual(publisher.calls["prepare_hidden"], [])
        self.assertEqual(publisher.calls["commit_publish"], [])
        row = self.store._read(
            lambda connection: connection.execute(
                """SELECT status,last_decision_json FROM edit_v3_publish_intents
                   WHERE operation='register_generation'"""
            ).fetchone()
        )
        self.assertEqual(tuple(row), ("pending", None))
        self.assertIsNone(self.billing_intent("refund_full"))

    def test_takeover_during_response_persistence_blocks_old_local_writes(self):
        delivery = self.delivery_module()
        old = self.seed_publish_job(fencing_token=16)
        backend, connect = self.real_shared_publisher()
        successor_box = []

        class TakeoverAfterCommit(LossySharedPublisher):
            def commit_publish(inner_self, mode, source_job_id, generation, key):
                decision = super(TakeoverAfterCommit, inner_self).commit_publish(
                    mode, source_job_id, generation, key
                )
                self.store._write(
                    lambda connection: connection.execute(
                        """UPDATE edit_v3_jobs SET lease_until=1000
                           WHERE job_id='job-publish'"""
                    )
                )
                successor_box.append(
                    self.store.claim_job(
                        old.job_id,
                        "worker-2",
                        10,
                        1_000,
                        expected_states={"publishing"},
                    )
                )
                return decision

        outcome = delivery.advance_publish(
            old,
            metadata_sha256=METADATA_SHA256,
            now=1_000,
            store=self.store,
            publisher=TakeoverAfterCommit(backend),
        )

        self.assertEqual(outcome.checkpoint["outcome"], "stale_claim")
        self.assertIsNotNone(successor_box[0])
        self.assertIsNone(
            self.store.get_job_for_owner("alice", old.job_id)["asset_id"]
        )
        self.assertIsNone(self.billing_intent("refund_full"))
        with closing(connect()) as connection:
            self.assertEqual(
                connection.execute(
                    """SELECT count(*) FROM video_assets
                       WHERE source_job_id='job-publish'"""
                ).fetchone()[0],
                1,
            )

    def test_strict_inputs_invalid_decisions_and_protocol_boundary(self):
        delivery = self.delivery_module()
        claim = self.seed_publish_job(fencing_token=17)
        backend, _connect = self.real_shared_publisher()
        self.assertIsInstance(backend, delivery.SharedAssetPublisher)

        for bad_now in (True, 1.5, -1):
            with self.subTest(bad_now=bad_now):
                with self.assertRaises(ValueError):
                    delivery.create_publish_intent(
                        claim,
                        metadata_sha256=METADATA_SHA256,
                        now=bad_now,
                        store=self.store,
                    )
        for bad_sha in (True, "A" * 64, "0" * 63):
            with self.subTest(bad_sha=bad_sha):
                with self.assertRaises(ValueError):
                    delivery.create_publish_intent(
                        claim,
                        metadata_sha256=bad_sha,
                        now=1_000,
                        store=self.store,
                    )

        class BadDecision(ScriptedPublisher):
            def register_generation(self, mode, source_job_id, generation, key):
                return type(
                    "Decision",
                    (),
                    {
                        "status": "publish_won",
                        "current_generation": generation,
                        "asset_id": " ",
                    },
                )()

        unknown = delivery.advance_publish(
            claim,
            metadata_sha256=METADATA_SHA256,
            now=1_000,
            store=self.store,
            publisher=BadDecision(),
        )
        self.assertEqual(unknown.next_state, "asset_decision_reconciling")
        dump = self.store._read(
            lambda connection: "\n".join(connection.iterdump())
        )
        self.assertNotIn("publication_asset_id_invalid", dump)

    def test_asset_id_is_opaque_at_delivery_and_store_write_boundaries(self):
        delivery = self.delivery_module()
        claim = self.seed_publish_job(fencing_token=18)
        secret_url = "https://signed.invalid/video.mp4?token=TOP-SECRET"

        for invalid in (
            secret_url,
            "asset?token=TOP-SECRET",
            "asset#fragment",
            "user:password@host",
            "token=TOP-SECRET",
            "asset/42",
            "asset\\42",
            "asset\x00id",
            "asset\x7fid",
        ):
            with self.subTest(invalid=repr(invalid)):
                self.assertFalse(is_valid_publish_asset_id(invalid))
        for valid in ("1", "asset-42", "video_123", "asset.42"):
            with self.subTest(valid=valid):
                self.assertTrue(is_valid_publish_asset_id(valid))

        class SignedUrlDecision(ScriptedPublisher):
            def register_generation(self, mode, source_job_id, generation, key):
                return PublicationDecision("publish_won", generation, secret_url)

        progress = delivery.advance_publish(
            claim,
            metadata_sha256=METADATA_SHA256,
            now=1_000,
            store=self.store,
            publisher=SignedUrlDecision(),
        )
        self.assertEqual(progress.next_state, "asset_decision_reconciling")
        dump = self.store._read(lambda connection: "\n".join(connection.iterdump()))
        self.assertNotIn("TOP-SECRET", dump)
        self.assertNotIn("signed.invalid", dump)

        self.setUp()
        claim = self.seed_publish_job(fencing_token=19)
        delivery.create_publish_intent(
            claim,
            metadata_sha256=METADATA_SHA256,
            now=1_000,
            store=self.store,
        )
        with self.assertRaises(StoreConfigurationError) as caught:
            self.store.record_publish_winner(
                claim,
                "register_generation",
                secret_url,
                {
                    "asset_id": secret_url,
                    "current_generation": claim.fencing_token,
                    "status": "publish_won",
                },
                now_ms=1_001,
            )
        self.assertEqual(caught.exception.error_code, "asset_id_invalid")
        dump = self.store._read(lambda connection: "\n".join(connection.iterdump()))
        self.assertNotIn("TOP-SECRET", dump)
        self.assertNotIn("signed.invalid", dump)

    def test_store_rejects_secret_bearing_evidence_at_all_write_entries(self):
        delivery = self.delivery_module()
        secret_url = "https://signed.invalid/result?token=TOP-SECRET"

        for entry in ("publish_winner", "cancel_winner", "operation"):
            with self.subTest(entry=entry):
                self.setUp()
                claim = self.seed_publish_job(fencing_token=25)
                delivery.create_publish_intent(
                    claim,
                    metadata_sha256=METADATA_SHA256,
                    now=1_000,
                    store=self.store,
                )
                caught = None
                try:
                    if entry == "publish_winner":
                        self.store.record_publish_winner(
                            claim,
                            "commit_publish",
                            "asset-25",
                            {
                                "asset_id": "asset-25",
                                "current_generation": 25,
                                "signed_url": secret_url,
                                "status": "publish_won",
                            },
                            now_ms=1_001,
                        )
                    elif entry == "cancel_winner":
                        self.store.record_cancel_winner_and_refund(
                            claim,
                            "cancel_publish",
                            {
                                "asset_id": None,
                                "current_generation": 25,
                                "signed_url": secret_url,
                                "status": "cancel_won",
                            },
                            now_ms=1_001,
                        )
                    else:
                        self.store.record_publish_operation(
                            claim,
                            "commit_publish",
                            "unknown",
                            {
                                "outcome": "unknown",
                                "reason_code": "response_lost",
                                "signed_url": secret_url,
                            },
                            now_ms=1_001,
                        )
                except StoreConfigurationError as exc:
                    caught = exc
                dump = self.store._read(
                    lambda connection: "\n".join(connection.iterdump())
                )
                self.assertNotIn("TOP-SECRET", dump)
                self.assertNotIn("signed.invalid", dump)
                self.assertIsNotNone(caught)
                self.assertEqual(caught.error_code, "publish_evidence_invalid")

    def test_store_requires_exact_canonical_decision_and_safe_evidence(self):
        delivery = self.delivery_module()
        cases = (
            (
                "publish_status",
                "publish_winner",
                {
                    "asset_id": None,
                    "current_generation": 26,
                    "status": "cancel_won",
                },
            ),
            (
                "publish_asset_mismatch",
                "publish_winner",
                {
                    "asset_id": "asset-other",
                    "current_generation": 26,
                    "status": "publish_won",
                },
            ),
            (
                "publish_bool_generation",
                "publish_winner",
                {
                    "asset_id": "asset-26",
                    "current_generation": True,
                    "status": "publish_won",
                },
            ),
            (
                "publish_overflow_generation",
                "publish_winner",
                {
                    "asset_id": "asset-26",
                    "current_generation": 1 << 63,
                    "status": "publish_won",
                },
            ),
            (
                "publish_negative_generation",
                "publish_winner",
                {
                    "asset_id": "asset-26",
                    "current_generation": -1,
                    "status": "publish_won",
                },
            ),
            (
                "cancel_status_and_asset",
                "cancel_winner",
                {
                    "asset_id": "asset-26",
                    "current_generation": 26,
                    "status": "publish_won",
                },
            ),
            (
                "cancel_bool_generation",
                "cancel_winner",
                {
                    "asset_id": None,
                    "current_generation": True,
                    "status": "cancel_won",
                },
            ),
            (
                "operation_unknown_status",
                "operation",
                {
                    "asset_id": None,
                    "current_generation": 26,
                    "status": "winner",
                },
            ),
            (
                "operation_missing_asset_id",
                "operation",
                {
                    "current_generation": 26,
                    "status": "accepted",
                },
            ),
            (
                "operation_nonpublish_asset",
                "operation",
                {
                    "asset_id": "asset-26",
                    "current_generation": 26,
                    "status": "accepted",
                },
            ),
            (
                "operation_unsafe_reason",
                "operation",
                {
                    "outcome": "unknown",
                    "reason_code": "https://signed.invalid/secret",
                },
            ),
            (
                "operation_unknown_outcome",
                "operation",
                {"outcome": "provider_payload", "reason_code": "response_lost"},
            ),
        )
        for name, entry, evidence in cases:
            with self.subTest(name=name):
                self.setUp()
                claim = self.seed_publish_job(fencing_token=26)
                delivery.create_publish_intent(
                    claim,
                    metadata_sha256=METADATA_SHA256,
                    now=1_000,
                    store=self.store,
                )
                with self.assertRaises(StoreConfigurationError) as caught:
                    if entry == "publish_winner":
                        self.store.record_publish_winner(
                            claim,
                            "commit_publish",
                            "asset-26",
                            evidence,
                            now_ms=1_001,
                        )
                    elif entry == "cancel_winner":
                        self.store.record_cancel_winner_and_refund(
                            claim,
                            "cancel_publish",
                            evidence,
                            now_ms=1_001,
                        )
                    else:
                        self.store.record_publish_operation(
                            claim,
                            "commit_publish",
                            "unknown",
                            evidence,
                            now_ms=1_001,
                        )
                self.assertEqual(
                    caught.exception.error_code, "publish_evidence_invalid"
                )

    def test_store_binds_operation_status_to_evidence_semantics(self):
        delivery = self.delivery_module()
        cases = (
            (
                "accepted_with_unknown",
                "accepted",
                {"outcome": "unknown", "reason_code": "response_lost"},
            ),
            (
                "unknown_with_definitive",
                "unknown",
                {
                    "outcome": "definitive_not_accepted",
                    "reason_code": "definitive_not_accepted",
                },
            ),
            (
                "pending_with_accepted",
                "pending",
                {
                    "asset_id": None,
                    "current_generation": 27,
                    "status": "accepted",
                },
            ),
            (
                "unknown_with_winner",
                "unknown",
                {
                    "asset_id": "asset-27",
                    "current_generation": 27,
                    "status": "publish_won",
                },
            ),
        )
        for name, status, evidence in cases:
            with self.subTest(name=name):
                self.setUp()
                claim = self.seed_publish_job(fencing_token=27)
                delivery.create_publish_intent(
                    claim,
                    metadata_sha256=METADATA_SHA256,
                    now=1_000,
                    store=self.store,
                )
                with self.assertRaises(StoreConfigurationError) as caught:
                    self.store.record_publish_operation(
                        claim,
                        "commit_publish",
                        status,
                        evidence,
                        now_ms=1_001,
                    )
                self.assertEqual(
                    caught.exception.error_code, "publish_evidence_invalid"
                )

    def test_store_rejects_stale_accepted_generation_before_delivery_order(self):
        delivery = self.delivery_module()
        claim = self.seed_publish_job(fencing_token=59)
        self.assertEqual(claim.fencing_token, 60)
        delivery.create_publish_intent(
            claim,
            metadata_sha256=METADATA_SHA256,
            now=1_000,
            store=self.store,
        )

        caught = None
        try:
            self.store.record_publish_operation(
                claim,
                "register_generation",
                "accepted",
                {
                    "asset_id": None,
                    "current_generation": 59,
                    "status": "accepted",
                },
                now_ms=1_001,
            )
        except StoreConfigurationError as exc:
            caught = exc

        publisher = ScriptedPublisher()
        completed = delivery.advance_publish(
            claim,
            metadata_sha256=METADATA_SHA256,
            now=1_002,
            store=self.store,
            publisher=publisher,
        )

        self.assertEqual(
            (
                getattr(caught, "error_code", None),
                completed.next_state,
                publisher.order[:3],
            ),
            (
                "publish_evidence_invalid",
                "completed",
                ["register_generation", "prepare_hidden", "commit_publish"],
            ),
        )

    def test_store_rejects_crossed_operation_and_reason_semantics(self):
        delivery = self.delivery_module()
        cases = (
            (
                "register_unknown_accepted",
                "register_generation",
                "unknown",
                {
                    "asset_id": None,
                    "current_generation": 61,
                    "status": "accepted",
                },
            ),
            (
                "commit_accepted_accepted",
                "commit_publish",
                "accepted",
                {
                    "asset_id": None,
                    "current_generation": 61,
                    "status": "accepted",
                },
            ),
            (
                "unknown_reserved_definitive_reason",
                "commit_publish",
                "unknown",
                {
                    "outcome": "unknown",
                    "reason_code": "definitive_not_accepted",
                },
            ),
            (
                "definitive_with_unknown_reason",
                "commit_publish",
                "pending",
                {
                    "outcome": "definitive_not_accepted",
                    "reason_code": "response_lost",
                },
            ),
        )
        for name, operation, status, evidence in cases:
            with self.subTest(name=name):
                self.setUp()
                claim = self.seed_publish_job(fencing_token=60)
                self.assertEqual(claim.fencing_token, 61)
                delivery.create_publish_intent(
                    claim,
                    metadata_sha256=METADATA_SHA256,
                    now=1_000,
                    store=self.store,
                )
                with self.assertRaises(StoreConfigurationError) as caught:
                    self.store.record_publish_operation(
                        claim,
                        operation,
                        status,
                        evidence,
                        now_ms=1_001,
                    )
                row = self.store._read(
                    lambda connection: connection.execute(
                        """SELECT status,last_decision_json
                           FROM edit_v3_publish_intents
                           WHERE job_id=? AND publish_generation=?
                             AND operation=?""",
                        (claim.job_id, claim.fencing_token, operation),
                    ).fetchone()
                )
                self.assertEqual(
                    caught.exception.error_code, "publish_evidence_invalid"
                )
                self.assertEqual(
                    (row["status"], row["last_decision_json"]),
                    ("planned", None),
                )

    def test_store_accepts_exact_five_operation_canonical_matrix(self):
        delivery = self.delivery_module()
        claim = self.seed_publish_job(fencing_token=61)
        self.assertEqual(claim.fencing_token, 62)
        delivery.create_publish_intent(
            claim,
            metadata_sha256=METADATA_SHA256,
            now=1_000,
            store=self.store,
        )
        expected = (
            ("register_generation", "accepted"),
            ("prepare_hidden", "accepted"),
            ("commit_publish", "unknown"),
            ("cancel_publish", "unknown"),
            ("query_decision", "unknown"),
        )
        evidence = {
            "asset_id": None,
            "current_generation": claim.fencing_token,
            "status": "accepted",
        }

        persisted = []
        for offset, (operation, status) in enumerate(expected, start=1):
            row = self.store.record_publish_operation(
                claim,
                operation,
                status,
                evidence,
                now_ms=1_000 + offset,
            )
            persisted.append(
                (
                    row["operation"],
                    row["status"],
                    json.loads(row["last_decision_json"]),
                )
            )

        self.assertEqual(
            persisted,
            [
                (
                    "register_generation",
                    "accepted",
                    {
                        "asset_id": None,
                        "current_generation": 62,
                        "status": "accepted",
                    },
                ),
                (
                    "prepare_hidden",
                    "accepted",
                    {
                        "asset_id": None,
                        "current_generation": 62,
                        "status": "accepted",
                    },
                ),
                (
                    "commit_publish",
                    "unknown",
                    {
                        "asset_id": None,
                        "current_generation": 62,
                        "status": "accepted",
                    },
                ),
                (
                    "cancel_publish",
                    "unknown",
                    {
                        "asset_id": None,
                        "current_generation": 62,
                        "status": "accepted",
                    },
                ),
                (
                    "query_decision",
                    "unknown",
                    {
                        "asset_id": None,
                        "current_generation": 62,
                        "status": "accepted",
                    },
                ),
            ],
        )

    def test_due_listing_is_read_only_bounded_ordered_and_strict(self):
        delivery = self.delivery_module()
        claim = self.seed_publish_job(fencing_token=1)
        delivery.create_publish_intent(
            claim,
            metadata_sha256=METADATA_SHA256,
            now=900,
            store=self.store,
        )
        self.store.begin_publish_operation(
            claim, "register_generation", now_ms=910
        )
        self.store.begin_publish_operation(claim, "commit_publish", now_ms=920)
        self.store.record_publish_operation(
            claim,
            "commit_publish",
            "unknown",
            {"outcome": "unknown", "reason_code": "response_lost"},
            now_ms=930,
        )
        before = self.store._read(
            lambda connection: tuple(
                connection.execute(
                    """SELECT operation,status,updated_at FROM edit_v3_publish_intents
                       ORDER BY operation"""
                )
            )
        )

        first_page = delivery.list_due_publish_intents(
            now=1_000, store=self.store, limit=1
        )
        self.assertEqual(len(first_page), 1)
        cursor = (first_page[0]["due_at"], first_page[0]["id"])
        second_page = delivery.list_due_publish_intents(
            now=1_000, store=self.store, limit=1, cursor=cursor
        )
        self.assertEqual(len(second_page), 1)
        self.assertNotEqual(first_page[0]["id"], second_page[0]["id"])
        self.assertEqual(
            [first_page[0]["operation"], second_page[0]["operation"]],
            ["register_generation", "commit_publish"],
        )
        after = self.store._read(
            lambda connection: tuple(
                connection.execute(
                    """SELECT operation,status,updated_at FROM edit_v3_publish_intents
                       ORDER BY operation"""
                )
            )
        )
        self.assertEqual(after, before)

        for bad_limit in (True, 0, 101):
            with self.subTest(bad_limit=bad_limit):
                with self.assertRaises(ValueError):
                    delivery.list_due_publish_intents(
                        now=1_000, store=self.store, limit=bad_limit
                    )
        for bad_cursor in (
            "opaque",
            (1,),
            (True, "id"),
            (1 << 63, "id"),
            (1, ""),
        ):
            with self.subTest(bad_cursor=bad_cursor):
                with self.assertRaisesRegex(ValueError, "publish_cursor_invalid"):
                    delivery.list_due_publish_intents(
                        now=1_000,
                        store=self.store,
                        limit=1,
                        cursor=bad_cursor,
                    )


if __name__ == "__main__":
    unittest.main()
