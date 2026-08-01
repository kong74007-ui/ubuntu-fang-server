import importlib
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from server.content_domains.ai_edit_v3.contracts import LeaseClaim, request_fingerprint
from server.content_domains.ai_edit_v3.store import LeaseLost, V3Store


PART_NAMES = (
    "base_task",
    "duration_tier",
    "tts_ceiling",
    "qwen_ceiling",
    "image_ceiling",
    "bgm_sfx_ceiling",
    "render_complexity",
    "one_repair_reserve",
)


def pricing_parameters():
    return {
        "parts": {
            "base_task": {
                "ceiling_quantity": 1,
                "min_rate": 10,
                "max_rate": 10,
            },
            "duration_tier": {
                "ceiling_quantity": 2,
                "min_rate": 1,
                "max_rate": 2,
            },
            "tts_ceiling": {
                "ceiling_quantity": 40,
                "unit_size": 100,
                "min_rate": 1,
                "max_rate": 2,
            },
            "qwen_ceiling": {
                "ceiling_quantity": 2,
                "min_rate": 3,
                "max_rate": 3,
            },
            "image_ceiling": {
                "ceiling_quantity": 3,
                "min_rate": 2,
                "max_rate": 4,
            },
            "bgm_sfx_ceiling": {
                "ceiling_quantity": 2,
                "min_rate": 1,
                "max_rate": 1,
            },
            "render_complexity": {
                "ceiling_quantity": 2,
                "min_rate": 2,
                "max_rate": 3,
            },
            "one_repair_reserve": {
                "ceiling_quantity": 1,
                "min_rate": 5,
                "max_rate": 5,
            },
        }
    }


def video_request(**overrides):
    request = {
        "input_type": "uploaded_video",
        "source_upload_id": "upload-video-1",
        "ratio": "auto",
        "creation_mode": "ai_auto",
        "material_asset_ids": [],
    }
    request.update(overrides)
    return request


def tts_request(text="a" * 101, **overrides):
    request = {
        "input_type": "script_to_audio_video",
        "tts_input": {"text": text, "voice_id": "voice-1"},
        "ratio": "16:9",
        "creation_mode": "ai_auto",
        "material_asset_ids": [],
    }
    request.update(overrides)
    return request


class FakeLedger:
    def __init__(self, billing, *, created_at=3_000):
        self.billing = billing
        self.created_at = created_at
        self.transactions = {}
        self.deduct_calls = []
        self.refund_calls = []
        self.query_calls = []
        self.deduct_behavior = "success"
        self.refund_behavior = "success"
        self.query_behavior = "stored"
        self.query_override = None

    def _transaction(self, owner, amount, key, operation):
        return self.billing.LedgerTransaction(
            transaction_key=key,
            operation=operation,
            owner=owner,
            amount=amount,
            points_after=1_000 - amount,
            created_at=self.created_at,
        )

    def deduct(self, owner, amount, transaction_key, reason):
        self.deduct_calls.append((owner, amount, transaction_key, reason))
        if self.deduct_behavior == "transport_before_effect":
            raise ConnectionError("deduct transport failed before effect")
        transaction = self.transactions.get(transaction_key)
        if transaction is None:
            transaction = self._transaction(
                owner, amount, transaction_key, "deduct"
            )
            self.transactions[transaction_key] = transaction
        if self.deduct_behavior == "apply_then_raise":
            raise ConnectionError("deduct response lost after effect")
        if self.deduct_behavior == "malformed_result":
            malformed = self._transaction("mallory", amount, transaction_key, "deduct")
            return self.billing.LedgerResult(True, malformed, None)
        return self.billing.LedgerResult(True, transaction, None)

    def refund(self, owner, amount, transaction_key, reason):
        self.refund_calls.append((owner, amount, transaction_key, reason))
        if self.refund_behavior == "transport_before_effect":
            raise ConnectionError("refund transport failed before effect")
        transaction = self.transactions.get(transaction_key)
        if transaction is None:
            transaction = self._transaction(
                owner, amount, transaction_key, "refund"
            )
            self.transactions[transaction_key] = transaction
        if self.refund_behavior == "apply_then_raise":
            raise ConnectionError("refund response lost after effect")
        if self.refund_behavior == "malformed_result":
            malformed = self._transaction(
                owner, amount + 1, transaction_key, "refund"
            )
            return self.billing.LedgerResult(True, malformed, None)
        return self.billing.LedgerResult(True, transaction, None)

    def query_transaction(self, owner, transaction_key):
        self.query_calls.append((owner, transaction_key))
        if self.query_behavior == "transport":
            raise ConnectionError("query transport failed")
        if self.query_behavior == "override":
            return self.query_override
        if self.query_behavior == "absent":
            return None
        return self.transactions.get(transaction_key)


class BillingTestCase(unittest.TestCase):
    def setUp(self):
        self.billing = importlib.import_module(
            "server.content_domains.ai_edit_v3.billing"
        )
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        self.db = root / "ai_edit_v3.db"
        self.v2 = root / "ai_edit_v2.db"
        self.v2.write_bytes(b"V2 identity marker; never open")
        self.store = V3Store(self.db, v2_db_path=self.v2, environment="test")

    def publish_pricing(self, version="price-v1", parameters=None):
        return self.store.insert_pricing_version(
            version,
            pricing_parameters() if parameters is None else parameters,
            status="published",
            created_at=100,
            published_at=101,
        )

    def insert_template(
        self,
        template_id="template-1",
        version="template-v1",
        status="published",
        ratios=("16:9", "9:16"),
    ):
        def write(connection):
            connection.execute(
                """INSERT INTO edit_v3_template_versions(
                       template_id,version,status,preview_cos_key,
                       supported_ratios_json,capability_contract_json,sha256,
                       created_at,published_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    template_id,
                    version,
                    status,
                    f"templates/{template_id}/{version}/preview.jpg",
                    json.dumps(list(ratios), separators=(",", ":")),
                    "{}",
                    "a" * 64,
                    100,
                    101 if status == "published" else None,
                ),
            )

        self.store._write(write)


class V3QuoteTests(BillingTestCase):
    def test_quote_freezes_all_eight_parts_request_version_and_exact_ttl(self):
        self.publish_pricing()
        request = video_request()

        quote = self.billing.create_quote(
            "alice", request, now=1_000, store=self.store
        )

        self.assertEqual(quote.request_sha256, request_fingerprint(request))
        self.assertEqual(quote.pricing_version, "price-v1")
        self.assertEqual(quote.expires_at, 901_000)
        self.assertEqual(tuple(quote.parts), PART_NAMES)
        self.assertEqual((quote.min_points, quote.max_points), (35, 45))
        self.assertEqual(quote.parts["tts_ceiling"]["quantity"], 0)
        self.assertEqual(
            self.store.get_quote("alice", quote.quote_id)["max_points"], 45
        )
        with self.assertRaises(FrozenInstanceError):
            quote.max_points = 1

    def test_tts_quantity_is_deterministic_and_bounded_by_published_ceiling(self):
        self.publish_pricing()

        quote = self.billing.create_quote(
            "alice", tts_request(), now=1_000, store=self.store
        )

        self.assertEqual(quote.parts["tts_ceiling"]["quantity"], 2)
        self.assertEqual(quote.parts["tts_ceiling"]["quantity_source"], "tts_text_units")
        self.assertEqual((quote.min_points, quote.max_points), (37, 49))

    def test_publishing_a_later_version_does_not_mutate_frozen_quote(self):
        first = pricing_parameters()
        self.publish_pricing(parameters=first)
        quote = self.billing.create_quote(
            "alice", video_request(), now=1_000, store=self.store
        )
        self.store._write(
            lambda connection: connection.execute(
                "UPDATE edit_v3_pricing_versions SET status='retired' WHERE version='price-v1'"
            )
        )
        more = pricing_parameters()
        more["parts"]["base_task"]["min_rate"] = 100
        more["parts"]["base_task"]["max_rate"] = 100
        self.publish_pricing("price-v2", more)

        frozen = self.store.get_quote("alice", quote.quote_id)

        self.assertEqual((frozen["min_points"], frozen["max_points"]), (35, 45))
        self.assertEqual(frozen["pricing_version"], "price-v1")

    def test_template_quote_freezes_published_version_and_supported_ratio(self):
        self.publish_pricing()
        self.insert_template(ratios=("16:9",))
        request = tts_request(
            creation_mode="template_reference", template_id="template-1"
        )

        quote = self.billing.create_quote(
            "alice", request, now=1_000, store=self.store
        )

        self.assertEqual(quote.template_id, "template-1")
        self.assertEqual(quote.template_version, "template-v1")

    def test_absent_unpublished_and_ratio_mismatched_templates_are_rejected(self):
        self.publish_pricing()
        request = tts_request(
            creation_mode="template_reference", template_id="template-1"
        )
        for status, ratios, expected in (
            (None, (), "template_not_found"),
            ("draft", ("16:9",), "template_unpublished"),
            ("published", ("9:16",), "template_ratio_unsupported"),
        ):
            with self.subTest(status=status, ratios=ratios):
                if status is not None:
                    self.insert_template(status=status, ratios=ratios)
                with self.assertRaises(self.billing.BillingError) as caught:
                    self.billing.create_quote(
                        "alice", request, now=1_000, store=self.store
                    )
                self.assertEqual(caught.exception.error_code, expected)
                if status is not None:
                    self.store._write(
                        lambda connection: connection.execute(
                            "DELETE FROM edit_v3_template_versions"
                        )
                    )

    def test_missing_unknown_invalid_or_overflow_pricing_is_rejected(self):
        cases = []
        missing = pricing_parameters()
        del missing["parts"]["image_ceiling"]
        cases.append((missing, "pricing_parts_invalid"))
        unknown = pricing_parameters()
        unknown["parts"]["surprise"] = unknown["parts"]["base_task"].copy()
        cases.append((unknown, "pricing_parts_invalid"))
        boolean = pricing_parameters()
        boolean["parts"]["base_task"]["min_rate"] = True
        cases.append((boolean, "pricing_integer_invalid"))
        negative = pricing_parameters()
        negative["parts"]["base_task"]["max_rate"] = -1
        cases.append((negative, "pricing_integer_invalid"))
        invalid_range = pricing_parameters()
        invalid_range["parts"]["base_task"]["min_rate"] = 11
        cases.append((invalid_range, "pricing_rate_invalid"))
        overflow = pricing_parameters()
        overflow["parts"]["duration_tier"]["ceiling_quantity"] = (1 << 63) - 1
        cases.append((overflow, "pricing_overflow"))

        for index, (parameters, expected) in enumerate(cases):
            with self.subTest(expected=expected):
                self.publish_pricing(f"price-{index}", parameters)
                with self.assertRaises(self.billing.BillingError) as caught:
                    self.billing.create_quote(
                        "alice", video_request(), now=1_000, store=self.store
                    )
                self.assertEqual(caught.exception.error_code, expected)
                self.store._write(
                    lambda connection: connection.execute(
                        "DELETE FROM edit_v3_pricing_versions"
                    )
                )

    def test_absent_published_pricing_and_int64_time_overflow_are_stable_errors(self):
        with self.assertRaises(self.billing.BillingError) as caught:
            self.billing.create_quote(
                "alice", video_request(), now=1_000, store=self.store
            )
        self.assertEqual(caught.exception.error_code, "pricing_unavailable")

        self.publish_pricing()
        with self.assertRaises(self.billing.BillingError) as caught:
            self.billing.create_quote(
                "alice", video_request(), now=(1 << 63) - 1, store=self.store
            )
        self.assertEqual(caught.exception.error_code, "quote_expiry_overflow")


class V3PreDebitTests(BillingTestCase):
    def setUp(self):
        super().setUp()
        self.publish_pricing()
        self.request = video_request()
        self.quote = self.billing.create_quote(
            "alice", self.request, now=1_000, store=self.store
        )

    def row_counts(self):
        return self.store._read(
            lambda connection: (
                connection.execute("SELECT count(*) FROM edit_v3_jobs").fetchone()[0],
                connection.execute(
                    "SELECT count(*) FROM edit_v3_billing_intents"
                ).fetchone()[0],
            )
        )

    def test_job_and_predebit_intent_rollback_together_after_job_insert(self):
        with self.assertRaises(self.billing.InjectedCommitFailure):
            self.billing.create_job_with_predebit(
                "alice",
                self.request,
                self.quote.quote_id,
                "client-key-1",
                now=2_000,
                store=self.store,
                failpoint="after_job_before_intent",
            )

        self.assertEqual(self.row_counts(), (0, 0))

    def test_atomic_create_freezes_job_and_predebit_intent_without_ledger(self):
        outcome = self.billing.create_job_with_predebit(
            "alice",
            self.request,
            self.quote.quote_id,
            "client-key-1",
            now=2_000,
            store=self.store,
        )

        self.assertEqual(outcome.next_state, "created_draft")
        self.assertFalse(outcome.confirmed)
        self.assertEqual(outcome.intent.operation, "pre_debit")
        self.assertEqual(
            outcome.intent.external_idempotency_key,
            f"ai-edit-v3:{outcome.job_id}:pre_debit",
        )
        self.assertEqual(outcome.intent.request_amount, self.quote.max_points)
        self.assertEqual(outcome.intent.refund_target_total, 0)
        self.assertEqual(outcome.intent.request_sha256, self.quote.request_sha256)
        job = self.store.get_job_for_owner("alice", outcome.job_id)
        self.assertEqual(job["normalized_request_json"], json.dumps(
            self.request, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ))
        self.assertEqual(self.row_counts(), (1, 1))

    def test_exact_replay_returns_same_job_and_divergent_key_reuse_conflicts(self):
        first = self.billing.create_job_with_predebit(
            "alice", self.request, self.quote.quote_id, "same-key", now=2_000,
            store=self.store,
        )
        replay = self.billing.create_job_with_predebit(
            "alice", self.request, self.quote.quote_id, "same-key", now=3_000,
            store=self.store,
        )

        self.assertEqual(replay, first)
        for changed_request, changed_quote in (
            (video_request(source_upload_id="other-upload"), self.quote.quote_id),
            (
                self.request,
                self.billing.create_quote(
                    "alice", self.request, now=1_100, store=self.store
                ).quote_id,
            ),
        ):
            with self.subTest(changed_quote=changed_quote):
                with self.assertRaises(self.billing.BillingError) as caught:
                    self.billing.create_job_with_predebit(
                        "alice", changed_request, changed_quote, "same-key",
                        now=3_000, store=self.store,
                    )
                self.assertEqual(caught.exception.error_code, "idempotency_conflict")
        self.assertEqual(self.row_counts(), (1, 1))

    def test_replay_rejects_corrupted_predebit_amount(self):
        created = self.billing.create_job_with_predebit(
            "alice", self.request, self.quote.quote_id, "corrupt-key", now=2_000,
            store=self.store,
        )
        self.store._write(
            lambda connection: connection.execute(
                """UPDATE edit_v3_billing_intents SET request_amount=request_amount+1
                   WHERE id=?""",
                (created.intent.intent_id,),
            )
        )

        with self.assertRaises(self.billing.BillingError) as caught:
            self.billing.create_job_with_predebit(
                "alice", self.request, self.quote.quote_id, "corrupt-key", now=2_100,
                store=self.store,
            )

        self.assertEqual(caught.exception.error_code, "billing_intent_conflict")

    def test_quote_authority_request_environment_and_expiry_are_checked_before_insert(self):
        cases = (
            (
                dict(owner_id="bob", request=self.request, environment=None, now=2_000),
                "quote_not_found",
            ),
            (
                dict(
                    owner_id="alice",
                    request=video_request(source_upload_id="other-upload"),
                    environment=None,
                    now=2_000,
                ),
                "quote_request_mismatch",
            ),
            (
                dict(owner_id="alice", request=self.request, environment="production", now=2_000),
                "quote_environment_mismatch",
            ),
            (
                dict(
                    owner_id="alice",
                    request=self.request,
                    environment=None,
                    now=self.quote.expires_at,
                ),
                "quote_expired",
            ),
        )
        for index, (arguments, expected) in enumerate(cases):
            with self.subTest(expected=expected):
                with self.assertRaises(self.billing.BillingError) as caught:
                    self.billing.create_job_with_predebit(
                        arguments["owner_id"],
                        arguments["request"],
                        self.quote.quote_id,
                        f"invalid-{index}",
                        now=arguments["now"],
                        store=self.store,
                        environment=arguments["environment"],
                    )
                self.assertEqual(caught.exception.error_code, expected)
        self.assertEqual(self.row_counts(), (0, 0))

    def test_two_thread_barrier_exact_create_has_one_job_and_one_intent(self):
        barrier = Barrier(2)

        def create():
            barrier.wait()
            return self.billing.create_job_with_predebit(
                "alice", self.request, self.quote.quote_id, "race-key", now=2_000,
                store=self.store,
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _index: create(), range(2)))

        self.assertEqual(results[0], results[1])
        self.assertEqual(self.row_counts(), (1, 1))


class V3BillingRecoveryTests(BillingTestCase):
    def setUp(self):
        super().setUp()
        self.publish_pricing()
        self.request = video_request()
        self.quote = self.billing.create_quote(
            "alice", self.request, now=1_000, store=self.store
        )
        self.created = self.billing.create_job_with_predebit(
            "alice", self.request, self.quote.quote_id, "client-key", now=2_000,
            store=self.store,
        )

    def claim(self, *, worker="worker-1", now=2_500, lease_seconds=1_000, states=None):
        claim = self.store.claim_job(
            self.created.job_id,
            worker,
            lease_seconds,
            now,
            expected_states=set(states or {"created_draft"}),
        )
        self.assertIsNotNone(claim)
        return claim

    def rows(self):
        return self.store._read(
            lambda connection: (
                dict(
                    connection.execute(
                        "SELECT * FROM edit_v3_jobs WHERE job_id=?",
                        (self.created.job_id,),
                    ).fetchone()
                ),
                dict(
                    connection.execute(
                        "SELECT * FROM edit_v3_billing_intents WHERE id=?",
                        (self.created.intent.intent_id,),
                    ).fetchone()
                ),
            )
        )

    def test_successful_predebit_queues_at_authority_time_and_sets_absolute_deadline(self):
        claim = self.claim()
        ledger = FakeLedger(self.billing, created_at=3_100)

        outcome = self.billing.process_pending_intent(
            self.created.intent.intent_id,
            claim=claim,
            ledger=ledger,
            now=3_200,
            store=self.store,
        )

        job, intent = self.rows()
        self.assertEqual(outcome.next_state, "queued")
        self.assertTrue(outcome.confirmed)
        self.assertEqual(job["state"], "queued")
        self.assertEqual(job["confirmed_preheld_total"], self.quote.max_points)
        self.assertEqual(job["queued_at"], 3_100)
        self.assertEqual(job["processing_deadline_at"], 2_703_100)
        self.assertEqual(intent["status"], "completed")
        self.assertEqual(len(ledger.deduct_calls), 1)

    def test_response_loss_restarts_with_authoritative_query_and_never_rededucts(self):
        claim = self.claim()
        ledger = FakeLedger(self.billing)
        ledger.deduct_behavior = "apply_then_raise"

        unknown = self.billing.process_pending_intent(
            self.created.intent.intent_id,
            claim=claim,
            ledger=ledger,
            now=3_000,
            store=self.store,
        )
        recovered = self.billing.process_pending_intent(
            self.created.intent.intent_id,
            claim=claim,
            ledger=ledger,
            now=3_500,
            store=self.store,
        )

        self.assertEqual(unknown.next_state, "billing_reconciling")
        self.assertEqual(recovered.next_state, "queued")
        self.assertTrue(recovered.confirmed)
        self.assertEqual(len(ledger.deduct_calls), 1)
        self.assertEqual(len(ledger.query_calls), 1)

    def test_crash_after_intent_commit_queries_absence_then_reuses_same_key(self):
        claim = self.claim()
        ledger = FakeLedger(self.billing)
        with self.assertRaises(self.billing.InjectedCommitFailure):
            self.billing.process_pending_intent(
                self.created.intent.intent_id,
                claim=claim,
                ledger=ledger,
                now=3_000,
                store=self.store,
                failpoint="after_intent_commit_before_ledger",
            )
        self.assertEqual(ledger.deduct_calls, [])
        ledger.query_behavior = "absent"

        absent = self.billing.process_pending_intent(
            self.created.intent.intent_id,
            claim=claim,
            ledger=ledger,
            now=3_500,
            store=self.store,
        )
        ledger.query_behavior = "stored"
        submitted = self.billing.process_pending_intent(
            self.created.intent.intent_id,
            claim=claim,
            ledger=ledger,
            now=4_000,
            store=self.store,
        )

        self.assertEqual(absent.next_state, "preholding")
        self.assertEqual(absent.intent.status, "retryable_absent")
        self.assertEqual(submitted.next_state, "queued")
        self.assertEqual(len(ledger.deduct_calls), 1)
        self.assertEqual(ledger.deduct_calls[0][2], self.created.intent.external_idempotency_key)

    def test_writeahead_predebit_persists_its_recovery_context(self):
        claim = self.claim()
        ledger = FakeLedger(self.billing)

        with self.assertRaises(self.billing.InjectedCommitFailure):
            self.billing.process_pending_intent(
                self.created.intent.intent_id,
                claim=claim,
                ledger=ledger,
                now=3_000,
                store=self.store,
                failpoint="after_intent_commit_before_ledger",
            )

        job, intent = self.rows()
        self.assertEqual(job["state"], "preholding")
        self.assertEqual((intent["reason"], intent["resume_state"]), ("prehold", "preholding"))
        self.assertEqual(ledger.deduct_calls, [])

    def test_crash_after_local_confirmation_replays_without_external_call(self):
        claim = self.claim()
        ledger = FakeLedger(self.billing)
        with self.assertRaises(self.billing.InjectedCommitFailure):
            self.billing.process_pending_intent(
                self.created.intent.intent_id,
                claim=claim,
                ledger=ledger,
                now=3_000,
                store=self.store,
                failpoint="after_local_confirmation",
            )

        replay = self.billing.process_pending_intent(
            self.created.intent.intent_id,
            claim=claim,
            ledger=ledger,
            now=3_500,
            store=self.store,
        )

        self.assertEqual(replay.next_state, "queued")
        self.assertEqual(len(ledger.deduct_calls), 1)
        self.assertEqual(ledger.query_calls, [])

    def test_unknown_transport_is_live_at_299999_and_times_out_at_300000(self):
        claim = self.claim()
        ledger = FakeLedger(self.billing)
        ledger.deduct_behavior = "transport_before_effect"
        self.billing.process_pending_intent(
            self.created.intent.intent_id,
            claim=claim,
            ledger=ledger,
            now=3_000,
            store=self.store,
        )
        ledger.query_behavior = "transport"

        live = self.billing.reconcile_unknown_intent(
            self.created.intent.intent_id,
            claim=claim,
            ledger=ledger,
            now=302_999,
            store=self.store,
        )
        timed_out = self.billing.reconcile_unknown_intent(
            self.created.intent.intent_id,
            claim=claim,
            ledger=ledger,
            now=303_000,
            store=self.store,
        )

        job, intent = self.rows()
        self.assertEqual(live.next_state, "billing_reconciling")
        self.assertEqual(timed_out.next_state, "failed_reconciliation_pending")
        self.assertEqual(job["state"], "failed_reconciliation_pending")
        self.assertIsNone(job["worker_id"])
        self.assertEqual(intent["first_unknown_at"], 3_000)

    def test_created_draft_prehold_admission_is_open_at_299999(self):
        claim = self.claim()
        ledger = FakeLedger(self.billing, created_at=301_999)

        outcome = self.billing.process_pending_intent(
            self.created.intent.intent_id,
            claim=claim,
            ledger=ledger,
            now=301_999,
            store=self.store,
        )

        job, intent = self.rows()
        self.assertEqual(outcome.next_state, "queued")
        self.assertEqual(job["state"], "queued")
        self.assertEqual(intent["status"], "completed")
        self.assertEqual(len(ledger.deduct_calls), 1)
        self.assertEqual(ledger.refund_calls, [])
        self.assertEqual(ledger.query_calls, [])

    def test_created_draft_prehold_admission_expires_at_exact_300000(self):
        claim = self.claim()
        ledger = FakeLedger(self.billing)

        outcome = self.billing.process_pending_intent(
            self.created.intent.intent_id,
            claim=claim,
            ledger=ledger,
            now=302_000,
            store=self.store,
        )

        job, intent = self.rows()
        self.assertEqual(outcome.next_state, "failed_reconciliation_pending")
        self.assertEqual(outcome.error_code, "prehold_admission_timeout")
        self.assertFalse(outcome.confirmed)
        self.assertEqual(job["state"], "failed_reconciliation_pending")
        self.assertEqual(
            (job["reconciliation_reason"], job["resume_state"]),
            ("prehold", "preholding"),
        )
        self.assertIsNone(job["worker_id"])
        self.assertIsNone(job["lease_until"])
        self.assertEqual(intent["status"], "reconciliation_pending")
        self.assertEqual(intent["first_unknown_at"], 2_000)
        self.assertEqual(intent["last_checked_at"], 302_000)
        self.assertEqual((intent["reason"], intent["resume_state"]), ("prehold", "preholding"))
        self.assertEqual(
            json.loads(intent["authority_evidence_json"]),
            {
                "admission_deadline_at": 302_000,
                "observed_at": 302_000,
                "transmission": "not_started",
            },
        )
        self.assertEqual(ledger.deduct_calls, [])
        self.assertEqual(ledger.refund_calls, [])
        self.assertEqual(ledger.query_calls, [])

    def test_created_draft_admission_timeout_commit_crash_replays_without_ledger(self):
        claim = self.claim()
        ledger = FakeLedger(self.billing)

        with self.assertRaises(self.billing.InjectedCommitFailure):
            self.billing.process_pending_intent(
                self.created.intent.intent_id,
                claim=claim,
                ledger=ledger,
                now=302_000,
                store=self.store,
                failpoint="after_admission_timeout_commit",
            )
        durable_after_crash = self.rows()
        replay_claim = self.store.claim_job(
            self.created.job_id,
            "admission-replay",
            100,
            302_001,
            expected_states={"failed_reconciliation_pending"},
        )
        self.assertIsNotNone(replay_claim)

        replay = self.billing.process_pending_intent(
            self.created.intent.intent_id,
            claim=replay_claim,
            ledger=ledger,
            now=302_002,
            store=self.store,
        )

        replayed_job, replayed_intent = self.rows()
        self.assertEqual(durable_after_crash[0]["state"], "failed_reconciliation_pending")
        self.assertEqual(durable_after_crash[1]["first_unknown_at"], 2_000)
        self.assertEqual(replay.next_state, "failed_reconciliation_pending")
        self.assertEqual(replay.error_code, "prehold_admission_timeout")
        self.assertEqual(replayed_job["state"], "failed_reconciliation_pending")
        self.assertEqual(replayed_intent["id"], self.created.intent.intent_id)
        self.assertEqual(
            replayed_intent["external_idempotency_key"],
            self.created.intent.external_idempotency_key,
        )
        self.assertEqual(ledger.deduct_calls, [])
        self.assertEqual(ledger.refund_calls, [])
        self.assertEqual(ledger.query_calls, [])

    def test_created_draft_admission_timeout_is_int64_safe_and_stale_fenced(self):
        claim = self.claim(lease_seconds=1)
        int64_max = (1 << 63) - 1
        created_at = int64_max - self.billing.UNKNOWN_TIMEOUT_MS - 1
        observed_at = int64_max - 1
        self.store._write(
            lambda connection: connection.execute(
                """UPDATE edit_v3_jobs
                   SET created_at=?,lease_until=? WHERE job_id=?""",
                (created_at, int64_max, self.created.job_id),
            )
        )
        ledger = FakeLedger(self.billing)

        outcome = self.billing.process_pending_intent(
            self.created.intent.intent_id,
            claim=claim,
            ledger=ledger,
            now=observed_at,
            store=self.store,
        )

        job, intent = self.rows()
        self.assertEqual(outcome.next_state, "failed_reconciliation_pending")
        self.assertEqual(intent["first_unknown_at"], created_at)
        self.assertEqual(
            json.loads(intent["authority_evidence_json"])["admission_deadline_at"],
            observed_at,
        )
        self.assertIsNone(job["worker_id"])
        self.assertEqual(ledger.deduct_calls, [])
        self.assertEqual(ledger.refund_calls, [])
        self.assertEqual(ledger.query_calls, [])

        successor = LeaseClaim(
            self.created.job_id,
            "int64-successor",
            claim.fencing_token + 1,
            int64_max,
        )
        self.store._write(
            lambda connection: connection.execute(
                """UPDATE edit_v3_jobs
                   SET worker_id=?,fencing_token=?,lease_until=? WHERE job_id=?""",
                (
                    successor.worker_id,
                    successor.fencing_token,
                    successor.lease_until,
                    self.created.job_id,
                ),
            )
        )
        before = json.dumps(self.rows(), sort_keys=True, separators=(",", ":"))
        with self.assertRaises(LeaseLost):
            self.billing.process_pending_intent(
                self.created.intent.intent_id,
                claim=claim,
                ledger=ledger,
                now=observed_at,
                store=self.store,
            )
        after = json.dumps(self.rows(), sort_keys=True, separators=(",", ":"))
        self.assertEqual(after, before)
        self.assertEqual(ledger.deduct_calls, [])
        self.assertEqual(ledger.refund_calls, [])
        self.assertEqual(ledger.query_calls, [])

    def test_predebit_restart_after_deadline_never_rededucts_after_authority_absence(self):
        claim = self.claim()
        ledger = FakeLedger(self.billing)
        with self.assertRaises(self.billing.InjectedCommitFailure):
            self.billing.process_pending_intent(
                self.created.intent.intent_id,
                claim=claim,
                ledger=ledger,
                now=3_000,
                store=self.store,
                failpoint="after_intent_commit_before_ledger",
            )
        ledger.query_behavior = "absent"
        absent = self.billing.reconcile_unknown_intent(
            self.created.intent.intent_id,
            claim=claim,
            ledger=ledger,
            now=302_000,
            store=self.store,
        )

        expired = self.billing.process_pending_intent(
            self.created.intent.intent_id,
            claim=claim,
            ledger=ledger,
            now=302_001,
            store=self.store,
        )

        job, intent = self.rows()
        self.assertEqual(absent.intent.status, "retryable_absent")
        self.assertEqual(expired.next_state, "failed_reconciliation_pending")
        self.assertEqual(expired.error_code, "prehold_admission_timeout")
        self.assertEqual(job["state"], "failed_reconciliation_pending")
        self.assertIsNone(job["worker_id"])
        self.assertEqual(intent["status"], "reconciliation_pending")
        self.assertEqual(intent["first_unknown_at"], 2_000)
        self.assertEqual(
            json.loads(intent["authority_evidence_json"]),
            {
                "admission_deadline_at": 302_000,
                "observed_at": 302_001,
                "transmission": "not_started",
            },
        )
        self.assertEqual(ledger.deduct_calls, [])
        self.assertEqual(ledger.refund_calls, [])
        self.assertEqual(len(ledger.query_calls), 1)

        replay_claim = self.store.claim_job(
            self.created.job_id,
            "deadline-restart",
            100,
            302_002,
            expected_states={"failed_reconciliation_pending"},
        )
        self.assertIsNotNone(replay_claim)
        replay = self.billing.process_pending_intent(
            self.created.intent.intent_id,
            claim=replay_claim,
            ledger=ledger,
            now=302_003,
            store=self.store,
        )
        self.assertEqual(replay.next_state, "failed_reconciliation_pending")
        self.assertEqual(ledger.deduct_calls, [])
        self.assertEqual(len(ledger.query_calls), 1)

    def test_predebit_retry_is_still_admitted_one_millisecond_before_job_deadline(self):
        claim = self.claim()
        ledger = FakeLedger(self.billing, created_at=301_999)
        with self.assertRaises(self.billing.InjectedCommitFailure):
            self.billing.process_pending_intent(
                self.created.intent.intent_id,
                claim=claim,
                ledger=ledger,
                now=3_000,
                store=self.store,
                failpoint="after_intent_commit_before_ledger",
            )
        ledger.query_behavior = "absent"
        self.billing.reconcile_unknown_intent(
            self.created.intent.intent_id,
            claim=claim,
            ledger=ledger,
            now=301_998,
            store=self.store,
        )

        outcome = self.billing.process_pending_intent(
            self.created.intent.intent_id,
            claim=claim,
            ledger=ledger,
            now=301_999,
            store=self.store,
        )

        self.assertEqual(outcome.next_state, "queued")
        self.assertEqual(len(ledger.deduct_calls), 1)
        self.assertEqual(
            ledger.deduct_calls[0][2], self.created.intent.external_idempotency_key
        )

    def test_created_draft_admission_timeout_uses_only_legal_graph_edges(self):
        claim = self.claim()
        ledger = FakeLedger(self.billing)
        store_module = importlib.import_module(
            "server.content_domains.ai_edit_v3.store"
        )
        original_transition = store_module._transition_leased_tx
        transitions = []

        def record_transition(*args, **kwargs):
            transitions.append(
                (frozenset(args[2]), args[3], kwargs.get("preserve_current_lease"))
            )
            return original_transition(*args, **kwargs)

        with patch.object(
            store_module, "_transition_leased_tx", side_effect=record_transition
        ):
            outcome = self.billing.process_pending_intent(
                self.created.intent.intent_id,
                claim=claim,
                ledger=ledger,
                now=302_000,
                store=self.store,
            )

        self.assertEqual(outcome.next_state, "failed_reconciliation_pending")
        self.assertEqual(
            transitions,
            [
                (frozenset({"created_draft"}), "preholding", True),
                (frozenset({"preholding"}), "billing_reconciling", True),
                (
                    frozenset({"billing_reconciling"}),
                    "failed_reconciliation_pending",
                    True,
                ),
            ],
        )
        self.assertEqual(ledger.deduct_calls, [])

    def test_preholding_admission_timeout_skips_only_completed_legal_edge(self):
        claim = self.claim()
        ledger = FakeLedger(self.billing)
        with self.assertRaises(self.billing.InjectedCommitFailure):
            self.billing.process_pending_intent(
                self.created.intent.intent_id,
                claim=claim,
                ledger=ledger,
                now=3_000,
                store=self.store,
                failpoint="after_intent_commit_before_ledger",
            )
        ledger.query_behavior = "absent"
        self.billing.reconcile_unknown_intent(
            self.created.intent.intent_id,
            claim=claim,
            ledger=ledger,
            now=302_000,
            store=self.store,
        )
        store_module = importlib.import_module(
            "server.content_domains.ai_edit_v3.store"
        )
        original_transition = store_module._transition_leased_tx
        transitions = []

        def record_transition(*args, **kwargs):
            transitions.append(
                (frozenset(args[2]), args[3], kwargs.get("preserve_current_lease"))
            )
            return original_transition(*args, **kwargs)

        with patch.object(
            store_module, "_transition_leased_tx", side_effect=record_transition
        ):
            outcome = self.billing.process_pending_intent(
                self.created.intent.intent_id,
                claim=claim,
                ledger=ledger,
                now=302_001,
                store=self.store,
            )

        self.assertEqual(outcome.next_state, "failed_reconciliation_pending")
        self.assertEqual(
            transitions,
            [
                (frozenset({"preholding"}), "billing_reconciling", True),
                (
                    frozenset({"billing_reconciling"}),
                    "failed_reconciliation_pending",
                    True,
                ),
            ],
        )
        self.assertEqual(ledger.deduct_calls, [])

    def test_admission_timeout_intermediate_failure_rolls_back_every_edge_and_row(self):
        claim = self.claim()
        ledger = FakeLedger(self.billing)
        before = json.dumps(self.rows(), sort_keys=True, separators=(",", ":"))
        store_module = importlib.import_module(
            "server.content_domains.ai_edit_v3.store"
        )
        original_transition = store_module._transition_leased_tx
        call_count = 0

        def fail_before_final_edge(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 3:
                raise self.billing.InjectedCommitFailure(
                    "injected admission transition failure"
                )
            return original_transition(*args, **kwargs)

        with patch.object(
            store_module,
            "_transition_leased_tx",
            side_effect=fail_before_final_edge,
        ):
            with self.assertRaises(self.billing.InjectedCommitFailure):
                self.billing.process_pending_intent(
                    self.created.intent.intent_id,
                    claim=claim,
                    ledger=ledger,
                    now=302_000,
                    store=self.store,
                )

        after = json.dumps(self.rows(), sort_keys=True, separators=(",", ":"))
        self.assertEqual(call_count, 3)
        self.assertEqual(after, before)
        self.assertEqual(ledger.deduct_calls, [])

    def test_malformed_or_conflicting_authority_never_confirms(self):
        claim = self.claim()
        ledger = FakeLedger(self.billing)
        ledger.deduct_behavior = "malformed_result"

        outcome = self.billing.process_pending_intent(
            self.created.intent.intent_id,
            claim=claim,
            ledger=ledger,
            now=3_000,
            store=self.store,
        )
        job, intent = self.rows()

        self.assertEqual(outcome.next_state, "billing_reconciling")
        self.assertFalse(outcome.confirmed)
        self.assertEqual(job["confirmed_preheld_total"], 0)
        self.assertEqual(intent["status"], "unknown")

    def test_stale_worker_cannot_query_or_change_billing_rows(self):
        old_claim = self.claim(lease_seconds=10)
        ledger = FakeLedger(self.billing)
        ledger.deduct_behavior = "transport_before_effect"
        self.billing.process_pending_intent(
            self.created.intent.intent_id,
            claim=old_claim,
            ledger=ledger,
            now=3_000,
            store=self.store,
        )
        new_claim = self.claim(
            worker="worker-2",
            now=20_000,
            lease_seconds=100,
            states={"billing_reconciling"},
        )
        before = json.dumps(self.rows(), sort_keys=True, separators=(",", ":"))
        ledger.query_calls.clear()

        with self.assertRaises(LeaseLost):
            self.billing.reconcile_unknown_intent(
                self.created.intent.intent_id,
                claim=old_claim,
                ledger=ledger,
                now=20_001,
                store=self.store,
            )

        after = json.dumps(self.rows(), sort_keys=True, separators=(",", ":"))
        self.assertEqual(after, before)
        self.assertEqual(ledger.query_calls, [])
        self.assertEqual(new_claim.fencing_token, old_claim.fencing_token + 1)

    def test_due_list_tracks_pending_unknown_and_excludes_completed(self):
        due = self.billing.list_due_billing_intents(now=2_100, store=self.store)
        self.assertEqual([item.intent_id for item in due], [self.created.intent.intent_id])
        claim = self.claim()
        ledger = FakeLedger(self.billing)
        ledger.deduct_behavior = "transport_before_effect"
        self.billing.process_pending_intent(
            self.created.intent.intent_id,
            claim=claim,
            ledger=ledger,
            now=3_000,
            store=self.store,
        )
        self.assertEqual(
            [item.intent_id for item in self.billing.list_due_billing_intents(now=3_001, store=self.store)],
            [self.created.intent.intent_id],
        )
        ledger.query_behavior = "absent"
        absent = self.billing.reconcile_unknown_intent(
            self.created.intent.intent_id,
            claim=claim,
            ledger=ledger,
            now=3_002,
            store=self.store,
        )
        self.assertEqual(absent.next_state, "prehold_absent")
        self.assertEqual(absent.intent.first_unknown_at, 3_000)
        self.assertEqual(self.billing.list_due_billing_intents(now=3_003, store=self.store), ())

    def test_authority_deadline_int64_overflow_remains_reconcilable(self):
        claim = self.claim()
        ledger = FakeLedger(
            self.billing,
            created_at=(1 << 63) - self.billing.PROCESSING_DEADLINE_MS,
        )
        ledger.created_at += 1

        outcome = self.billing.process_pending_intent(
            self.created.intent.intent_id,
            claim=claim,
            ledger=ledger,
            now=3_000,
            store=self.store,
        )

        job, intent = self.rows()
        self.assertEqual(outcome.next_state, "billing_reconciling")
        self.assertEqual(outcome.error_code, "processing_deadline_overflow")
        self.assertEqual(job["confirmed_preheld_total"], 0)
        self.assertEqual(intent["status"], "unknown")

    def test_authority_deadline_exact_int64_max_is_accepted(self):
        claim = self.claim()
        ledger = FakeLedger(
            self.billing,
            created_at=(1 << 63) - 1 - self.billing.PROCESSING_DEADLINE_MS,
        )

        outcome = self.billing.process_pending_intent(
            self.created.intent.intent_id,
            claim=claim,
            ledger=ledger,
            now=3_000,
            store=self.store,
        )

        self.assertEqual(outcome.next_state, "queued")
        self.assertEqual(self.rows()[0]["processing_deadline_at"], (1 << 63) - 1)

    def test_nonboolean_accepted_marker_is_malformed_authority(self):
        claim = self.claim()
        ledger = FakeLedger(self.billing)

        def malformed_deduct(owner, amount, transaction_key, reason):
            ledger.deduct_calls.append((owner, amount, transaction_key, reason))
            transaction = ledger._transaction(owner, amount, transaction_key, "deduct")
            return self.billing.LedgerResult(1, transaction, None)

        ledger.deduct = malformed_deduct
        outcome = self.billing.process_pending_intent(
            self.created.intent.intent_id,
            claim=claim,
            ledger=ledger,
            now=3_000,
            store=self.store,
        )

        self.assertEqual(outcome.next_state, "billing_reconciling")
        self.assertEqual(outcome.error_code, "billing_response_unknown")
        self.assertEqual(self.rows()[0]["confirmed_preheld_total"], 0)

    def test_late_predebit_authority_after_timeout_routes_to_refund_pending(self):
        claim = self.claim()
        ledger = FakeLedger(self.billing)
        ledger.deduct_behavior = "transport_before_effect"
        self.billing.process_pending_intent(
            self.created.intent.intent_id,
            claim=claim,
            ledger=ledger,
            now=3_000,
            store=self.store,
        )
        ledger.query_behavior = "transport"
        self.billing.reconcile_unknown_intent(
            self.created.intent.intent_id,
            claim=claim,
            ledger=ledger,
            now=303_000,
            store=self.store,
        )
        recovered_claim = self.store.claim_job(
            self.created.job_id,
            "recovery-worker",
            100,
            303_001,
            expected_states={"failed_reconciliation_pending"},
        )
        self.assertIsNotNone(recovered_claim)
        ledger.query_behavior = "stored"
        ledger.transactions[self.created.intent.external_idempotency_key] = (
            ledger._transaction(
                "alice",
                self.quote.max_points,
                self.created.intent.external_idempotency_key,
                "deduct",
            )
        )

        outcome = self.billing.reconcile_unknown_intent(
            self.created.intent.intent_id,
            claim=recovered_claim,
            ledger=ledger,
            now=303_002,
            store=self.store,
        )

        self.assertEqual(outcome.next_state, "refund_pending")
        self.assertEqual(self.rows()[0]["state"], "refund_pending")
        self.assertEqual(self.rows()[0]["confirmed_preheld_total"], self.quote.max_points)


class V3RefundTests(BillingTestCase):
    def setUp(self):
        super().setUp()
        self.publish_pricing()
        self.request = video_request()
        self.quote = self.billing.create_quote(
            "alice", self.request, now=1_000, store=self.store
        )
        self.created = self.billing.create_job_with_predebit(
            "alice", self.request, self.quote.quote_id, "refund-job", now=2_000,
            store=self.store,
        )
        self.claim = self.store.claim_job(
            self.created.job_id,
            "worker-1",
            1_000,
            2_500,
            expected_states={"created_draft"},
        )
        self.assertIsNotNone(self.claim)
        predebit_ledger = FakeLedger(self.billing, created_at=3_000)
        self.billing.process_pending_intent(
            self.created.intent.intent_id,
            claim=self.claim,
            ledger=predebit_ledger,
            now=3_100,
            store=self.store,
        )
        self.set_state("settling")

    def set_state(self, state):
        self.store._write(
            lambda connection: connection.execute(
                """UPDATE edit_v3_jobs
                   SET state=?,reconciliation_reason=NULL,resume_state=NULL
                   WHERE job_id=?""",
                (state, self.created.job_id),
            )
        )

    def job(self):
        return self.store.get_job_for_owner("alice", self.created.job_id)

    def intent_rows(self):
        return self.store._read(
            lambda connection: [
                dict(row)
                for row in connection.execute(
                    """SELECT * FROM edit_v3_billing_intents
                       WHERE job_id=? ORDER BY operation""",
                    (self.created.job_id,),
                )
            ]
        )

    def test_zero_delta_is_completed_without_ledger_call(self):
        requested = self.billing.request_delta_refund(
            self.claim,
            actual_charge=self.quote.max_points,
            now=4_000,
            store=self.store,
        )
        ledger = FakeLedger(self.billing)

        replay = self.billing.process_pending_intent(
            requested.intent.intent_id,
            claim=self.claim,
            ledger=ledger,
            now=4_100,
            store=self.store,
        )

        self.assertEqual(requested.intent.refund_target_total, 0)
        self.assertEqual(requested.intent.request_amount, 0)
        self.assertEqual(requested.intent.status, "completed")
        self.assertEqual(requested.next_state, "publishing")
        self.assertEqual(replay.next_state, "publishing")
        self.assertEqual(ledger.refund_calls, [])

    def test_delta_then_full_refunds_only_remaining_cumulative_amount(self):
        delta = self.billing.request_delta_refund(
            self.claim,
            actual_charge=25,
            now=4_000,
            store=self.store,
        )
        ledger = FakeLedger(self.billing, created_at=4_100)
        delta_done = self.billing.process_pending_intent(
            delta.intent.intent_id,
            claim=self.claim,
            ledger=ledger,
            now=4_200,
            store=self.store,
        )
        self.set_state("refund_pending")
        full = self.billing.request_full_refund(
            self.claim,
            now=4_300,
            store=self.store,
        )
        full_done = self.billing.process_pending_intent(
            full.intent.intent_id,
            claim=self.claim,
            ledger=ledger,
            now=4_400,
            store=self.store,
        )

        self.assertEqual(delta.intent.external_idempotency_key, f"ai-edit-v3:{self.created.job_id}:refund_delta")
        self.assertEqual((delta.intent.refund_target_total, delta.intent.request_amount), (20, 20))
        self.assertEqual(delta_done.next_state, "publishing")
        self.assertEqual((full.intent.refund_target_total, full.intent.request_amount), (45, 25))
        self.assertEqual(full.intent.external_idempotency_key, f"ai-edit-v3:{self.created.job_id}:refund_full")
        self.assertEqual(full_done.next_state, "refunded")
        self.assertEqual(self.job()["confirmed_refunded_total"], 45)
        self.assertEqual([call[1] for call in ledger.refund_calls], [20, 25])

    def test_unknown_delta_blocks_overlapping_full_until_authority_converges(self):
        delta = self.billing.request_delta_refund(
            self.claim, actual_charge=25, now=4_000, store=self.store
        )
        ledger = FakeLedger(self.billing)
        ledger.refund_behavior = "apply_then_raise"
        unknown = self.billing.process_pending_intent(
            delta.intent.intent_id,
            claim=self.claim,
            ledger=ledger,
            now=4_100,
            store=self.store,
        )

        with self.assertRaises(self.billing.BillingError) as caught:
            self.billing.request_full_refund(
                self.claim, now=4_200, store=self.store
            )
        self.assertEqual(caught.exception.error_code, "overlapping_refund_intent")
        recovered = self.billing.process_pending_intent(
            delta.intent.intent_id,
            claim=self.claim,
            ledger=ledger,
            now=4_300,
            store=self.store,
        )

        self.assertEqual(unknown.next_state, "billing_reconciling")
        self.assertEqual(recovered.next_state, "publishing")
        self.assertEqual(len(ledger.refund_calls), 1)
        self.assertEqual(len(ledger.query_calls), 1)
        self.assertEqual(len(self.intent_rows()), 2)

    def test_exact_refund_replay_is_idempotent_but_divergent_target_conflicts(self):
        first = self.billing.request_delta_refund(
            self.claim, actual_charge=25, now=4_000, store=self.store
        )
        replay = self.billing.request_delta_refund(
            self.claim, actual_charge=25, now=4_100, store=self.store
        )
        self.assertEqual(replay, first)

        with self.assertRaises(self.billing.BillingError) as caught:
            self.billing.request_delta_refund(
                self.claim, actual_charge=24, now=4_200, store=self.store
            )
        self.assertEqual(caught.exception.error_code, "billing_intent_conflict")
        self.assertEqual(len(self.intent_rows()), 2)

    def test_conflicting_refund_authority_never_increases_cumulative_total(self):
        delta = self.billing.request_delta_refund(
            self.claim, actual_charge=25, now=4_000, store=self.store
        )
        ledger = FakeLedger(self.billing)
        ledger.refund_behavior = "malformed_result"

        outcome = self.billing.process_pending_intent(
            delta.intent.intent_id,
            claim=self.claim,
            ledger=ledger,
            now=4_100,
            store=self.store,
        )

        self.assertEqual(outcome.next_state, "billing_reconciling")
        self.assertEqual(self.job()["confirmed_refunded_total"], 0)
        self.assertEqual(len(ledger.refund_calls), 1)

    def test_invalid_actual_charge_and_database_over_refund_are_rejected(self):
        for value in (-1, True, 46, 1.5):
            with self.subTest(value=value):
                with self.assertRaises(self.billing.BillingError) as caught:
                    self.billing.request_delta_refund(
                        self.claim, actual_charge=value, now=4_000, store=self.store
                    )
                self.assertEqual(caught.exception.error_code, "actual_charge_invalid")
        self.assertEqual(len(self.intent_rows()), 1)

        with self.assertRaises(sqlite3.IntegrityError):
            self.store._write(
                lambda connection: connection.execute(
                    """UPDATE edit_v3_jobs
                       SET confirmed_refunded_total=confirmed_preheld_total+1
                       WHERE job_id=?""",
                    (self.created.job_id,),
                )
            )
        self.assertEqual(self.job()["confirmed_refunded_total"], 0)

    def test_stale_claim_cannot_create_refund_intent_or_change_job(self):
        old_claim = self.claim
        new_claim = self.store.claim_job(
            self.created.job_id,
            "worker-2",
            100,
            1_003_000,
            expected_states={"settling"},
        )
        self.assertIsNotNone(new_claim)
        before = json.dumps(
            (self.job(), self.intent_rows()), sort_keys=True, separators=(",", ":")
        )

        with self.assertRaises(LeaseLost):
            self.billing.request_delta_refund(
                old_claim,
                actual_charge=25,
                now=1_003_001,
                store=self.store,
            )

        after = json.dumps(
            (self.job(), self.intent_rows()), sort_keys=True, separators=(",", ":")
        )
        self.assertEqual(after, before)

    def test_refund_crash_before_call_queries_absence_then_reuses_same_key(self):
        delta = self.billing.request_delta_refund(
            self.claim, actual_charge=25, now=4_000, store=self.store
        )
        ledger = FakeLedger(self.billing)
        with self.assertRaises(self.billing.InjectedCommitFailure):
            self.billing.process_pending_intent(
                delta.intent.intent_id,
                claim=self.claim,
                ledger=ledger,
                now=4_100,
                store=self.store,
                failpoint="after_intent_commit_before_ledger",
            )
        ledger.query_behavior = "absent"
        absent = self.billing.reconcile_unknown_intent(
            delta.intent.intent_id,
            claim=self.claim,
            ledger=ledger,
            now=4_200,
            store=self.store,
        )
        ledger.query_behavior = "stored"
        completed = self.billing.process_pending_intent(
            delta.intent.intent_id,
            claim=self.claim,
            ledger=ledger,
            now=4_300,
            store=self.store,
        )

        self.assertEqual(absent.intent.status, "retryable_absent")
        self.assertEqual(completed.next_state, "publishing")
        self.assertEqual(len(ledger.refund_calls), 1)
        self.assertEqual(ledger.refund_calls[0][2], delta.intent.external_idempotency_key)

    def test_late_delta_authority_after_timeout_routes_to_full_refund_path(self):
        delta = self.billing.request_delta_refund(
            self.claim, actual_charge=25, now=4_000, store=self.store
        )
        ledger = FakeLedger(self.billing)
        ledger.refund_behavior = "transport_before_effect"
        self.billing.process_pending_intent(
            delta.intent.intent_id,
            claim=self.claim,
            ledger=ledger,
            now=4_100,
            store=self.store,
        )
        ledger.query_behavior = "transport"
        self.billing.reconcile_unknown_intent(
            delta.intent.intent_id,
            claim=self.claim,
            ledger=ledger,
            now=304_100,
            store=self.store,
        )
        recovered_claim = self.store.claim_job(
            self.created.job_id,
            "refund-recovery",
            100,
            304_101,
            expected_states={"failed_reconciliation_pending"},
        )
        self.assertIsNotNone(recovered_claim)
        ledger.transactions[delta.intent.external_idempotency_key] = ledger._transaction(
            "alice",
            delta.intent.request_amount,
            delta.intent.external_idempotency_key,
            "refund",
        )
        ledger.query_behavior = "stored"

        outcome = self.billing.reconcile_unknown_intent(
            delta.intent.intent_id,
            claim=recovered_claim,
            ledger=ledger,
            now=304_102,
            store=self.store,
        )

        self.assertEqual(outcome.next_state, "refund_pending")
        self.assertEqual(self.job()["confirmed_refunded_total"], 20)
        self.assertEqual(self.job()["state"], "failed_reconciliation_pending")


class V3LateBillingRecoveryTests(BillingTestCase):
    def setUp(self):
        super().setUp()
        self.publish_pricing()
        self.request = video_request()
        self.quote = self.billing.create_quote(
            "alice", self.request, now=1_000, store=self.store
        )

    def raw_snapshot(self, job_id):
        return self.store._read(
            lambda connection: {
                "job": dict(
                    connection.execute(
                        "SELECT * FROM edit_v3_jobs WHERE job_id=?", (job_id,)
                    ).fetchone()
                ),
                "intents": [
                    dict(row)
                    for row in connection.execute(
                        """SELECT * FROM edit_v3_billing_intents
                           WHERE job_id=? ORDER BY operation""",
                        (job_id,),
                    )
                ],
            }
        )

    def failed_pending_scenario(self, operation, suffix):
        created = self.billing.create_job_with_predebit(
            "alice",
            self.request,
            self.quote.quote_id,
            f"late-{operation}-{suffix}",
            now=2_000,
            store=self.store,
        )
        claim = self.store.claim_job(
            created.job_id,
            f"initial-{operation}-{suffix}",
            1_000,
            2_500,
            expected_states={"created_draft"},
        )
        self.assertIsNotNone(claim)
        if operation == "pre_debit":
            intent = created.intent
            ledger = FakeLedger(self.billing)
            ledger.deduct_behavior = "transport_before_effect"
            first_unknown_at = 3_000
        else:
            predebit_ledger = FakeLedger(self.billing, created_at=3_000)
            self.billing.process_pending_intent(
                created.intent.intent_id,
                claim=claim,
                ledger=predebit_ledger,
                now=3_100,
                store=self.store,
            )
            source_state = "settling" if operation == "refund_delta" else "refund_pending"
            self.store._write(
                lambda connection: connection.execute(
                    """UPDATE edit_v3_jobs
                       SET state=?,reconciliation_reason=NULL,resume_state=NULL
                       WHERE job_id=?""",
                    (source_state, created.job_id),
                )
            )
            if operation == "refund_delta":
                requested = self.billing.request_delta_refund(
                    claim, actual_charge=25, now=4_000, store=self.store
                )
            else:
                requested = self.billing.request_full_refund(
                    claim, now=4_000, store=self.store
                )
            intent = requested.intent
            ledger = FakeLedger(self.billing)
            ledger.refund_behavior = "transport_before_effect"
            first_unknown_at = 4_100
        self.billing.process_pending_intent(
            intent.intent_id,
            claim=claim,
            ledger=ledger,
            now=first_unknown_at,
            store=self.store,
        )
        ledger.query_behavior = "transport"
        self.billing.reconcile_unknown_intent(
            intent.intent_id,
            claim=claim,
            ledger=ledger,
            now=first_unknown_at + self.billing.UNKNOWN_TIMEOUT_MS,
            store=self.store,
        )
        recovery_now = first_unknown_at + self.billing.UNKNOWN_TIMEOUT_MS + 1
        recovery_claim = self.store.claim_job(
            created.job_id,
            f"recovery-{operation}-{suffix}",
            1_000,
            recovery_now,
            expected_states={"failed_reconciliation_pending"},
        )
        self.assertIsNotNone(recovery_claim)
        self.assertEqual(
            self.raw_snapshot(created.job_id)["job"]["state"],
            "failed_reconciliation_pending",
        )
        ledger.query_calls.clear()
        return created, intent, ledger, recovery_claim, recovery_now

    def test_failed_pending_late_authority_matrix_is_durable_and_fenced(self):
        expected_reason = {
            "pre_debit": ("prehold", "preholding"),
            "refund_delta": ("settlement", "settling"),
            "refund_full": ("refund", "refund_pending"),
        }
        for operation in ("pre_debit", "refund_delta", "refund_full"):
            for result_kind in ("absent", "transport", "conflict", "found"):
                with self.subTest(operation=operation, result=result_kind):
                    created, intent, ledger, claim, recovery_now = (
                        self.failed_pending_scenario(operation, result_kind)
                    )
                    before = self.raw_snapshot(created.job_id)
                    keys_before = {
                        row["external_idempotency_key"] for row in before["intents"]
                    }
                    if result_kind == "absent":
                        ledger.query_behavior = "absent"
                    elif result_kind == "transport":
                        ledger.query_behavior = "transport"
                    else:
                        ledger.query_behavior = "override"
                        ledger.query_override = ledger._transaction(
                            "mallory" if result_kind == "conflict" else "alice",
                            intent.request_amount,
                            intent.external_idempotency_key,
                            "deduct" if operation == "pre_debit" else "refund",
                        )

                    outcome = self.billing.reconcile_unknown_intent(
                        intent.intent_id,
                        claim=claim,
                        ledger=ledger,
                        now=recovery_now + 1,
                        store=self.store,
                    )

                    after = self.raw_snapshot(created.job_id)
                    job = after["job"]
                    recovered_intent = next(
                        row for row in after["intents"] if row["id"] == intent.intent_id
                    )
                    self.assertEqual(len(ledger.query_calls), 1)
                    self.assertEqual(
                        len(ledger.deduct_calls), 1 if operation == "pre_debit" else 0
                    )
                    self.assertEqual(
                        len(ledger.refund_calls), 0 if operation == "pre_debit" else 1
                    )
                    self.assertEqual(
                        {row["external_idempotency_key"] for row in after["intents"]},
                        keys_before,
                    )
                    self.assertEqual(recovered_intent["external_idempotency_key"], intent.external_idempotency_key)
                    if result_kind in {"transport", "conflict"}:
                        self.assertEqual(job["state"], "failed_reconciliation_pending")
                        self.assertEqual(recovered_intent["status"], "reconciliation_pending")
                        self.assertEqual(recovered_intent["last_checked_at"], recovery_now + 1)
                        self.assertEqual(
                            (job["reconciliation_reason"], job["resume_state"]),
                            expected_reason[operation],
                        )
                        self.assertEqual(job["worker_id"], claim.worker_id)
                        self.assertEqual(job["fencing_token"], claim.fencing_token)
                        self.assertFalse(outcome.confirmed)
                        self.assertEqual(
                            outcome.error_code,
                            "billing_query_unknown"
                            if result_kind == "transport"
                            else "billing_authority_conflict",
                        )
                    elif result_kind == "absent":
                        expected_state = (
                            "prehold_absent"
                            if operation == "pre_debit"
                            else "refund_pending"
                        )
                        self.assertEqual(job["state"], expected_state)
                        self.assertEqual(
                            recovered_intent["status"],
                            "absent" if operation == "pre_debit" else "retryable_absent",
                        )
                        self.assertEqual(
                            (job["reconciliation_reason"], job["resume_state"]),
                            (None, None),
                        )
                        if operation == "pre_debit":
                            self.assertIsNone(job["worker_id"])
                            self.assertIsNone(job["lease_until"])
                        else:
                            self.assertEqual(job["worker_id"], claim.worker_id)
                        self.assertFalse(outcome.confirmed)
                        self.assertEqual(outcome.error_code, "billing_authority_absent")
                        if operation != "pre_debit":
                            ledger.refund_behavior = "success"
                            continued = self.billing.process_pending_intent(
                                intent.intent_id,
                                claim=claim,
                                ledger=ledger,
                                now=recovery_now + 2,
                                store=self.store,
                            )
                            continued_snapshot = self.raw_snapshot(created.job_id)
                            continued_intent = next(
                                row
                                for row in continued_snapshot["intents"]
                                if row["id"] == intent.intent_id
                            )
                            self.assertTrue(continued.confirmed)
                            self.assertEqual(
                                continued.next_state,
                                "refund_pending"
                                if operation == "refund_delta"
                                else "refunded",
                            )
                            self.assertEqual(continued_intent["status"], "completed")
                            self.assertEqual(
                                len(continued_snapshot["intents"]), len(after["intents"])
                            )
                            self.assertEqual(len(ledger.refund_calls), 2)
                            self.assertEqual(
                                {call[2] for call in ledger.refund_calls},
                                {intent.external_idempotency_key},
                            )
                    else:
                        expected_job_state = (
                            "refund_pending"
                            if operation == "pre_debit"
                            else "failed_reconciliation_pending"
                        )
                        expected_total = {
                            "pre_debit": (self.quote.max_points, 0),
                            "refund_delta": (self.quote.max_points, 20),
                            "refund_full": (self.quote.max_points, self.quote.max_points),
                        }[operation]
                        self.assertEqual(job["state"], expected_job_state)
                        self.assertEqual(
                            (job["confirmed_preheld_total"], job["confirmed_refunded_total"]),
                            expected_total,
                        )
                        self.assertEqual(recovered_intent["status"], "completed")
                        self.assertEqual(job["worker_id"], claim.worker_id)
                        self.assertTrue(outcome.confirmed)

    def late_delta_absent_scenario(self, suffix):
        created, intent, ledger, claim, recovery_now = self.failed_pending_scenario(
            "refund_delta", suffix
        )
        ledger.query_behavior = "absent"
        absent = self.billing.reconcile_unknown_intent(
            intent.intent_id,
            claim=claim,
            ledger=ledger,
            now=recovery_now + 1,
            store=self.store,
        )
        self.assertEqual(absent.next_state, "refund_pending")
        self.assertEqual(absent.intent.status, "retryable_absent")
        ledger.query_calls.clear()
        return created, intent, ledger, claim, recovery_now + 2

    def test_late_delta_absence_atomically_rebases_persisted_context(self):
        created, intent, _ledger, claim, _retry_at = self.late_delta_absent_scenario(
            "context"
        )

        snapshot = self.raw_snapshot(created.job_id)
        persisted = next(
            row for row in snapshot["intents"] if row["id"] == intent.intent_id
        )

        self.assertEqual(snapshot["job"]["state"], "refund_pending")
        self.assertEqual(
            (persisted["reason"], persisted["resume_state"]),
            ("refund", "refund_pending"),
        )
        self.assertEqual(snapshot["job"]["worker_id"], claim.worker_id)
        self.assertEqual(len(snapshot["intents"]), 2)

    def test_repeated_late_delta_recovery_matrix_reuses_one_intent_and_key(self):
        for result_kind in (
            "transport",
            "absent",
            "conflict",
            "found",
            "timeout",
            "stale",
        ):
            with self.subTest(result=result_kind):
                created, intent, ledger, claim, retry_at = (
                    self.late_delta_absent_scenario(result_kind)
                )
                before = self.raw_snapshot(created.job_id)
                keys_before = {
                    row["external_idempotency_key"] for row in before["intents"]
                }
                original_refund_calls = len(ledger.refund_calls)

                if result_kind == "absent":
                    with self.assertRaises(self.billing.InjectedCommitFailure):
                        self.billing.process_pending_intent(
                            intent.intent_id,
                            claim=claim,
                            ledger=ledger,
                            now=retry_at,
                            store=self.store,
                            failpoint="after_intent_commit_before_ledger",
                        )
                    ledger.query_behavior = "absent"
                    outcome = self.billing.reconcile_unknown_intent(
                        intent.intent_id,
                        claim=claim,
                        ledger=ledger,
                        now=retry_at + 1,
                        store=self.store,
                    )
                    self.assertEqual(outcome.next_state, "refund_pending")
                    self.assertEqual(outcome.intent.status, "retryable_absent")
                    self.assertEqual(len(ledger.refund_calls), original_refund_calls)
                elif result_kind == "found":
                    ledger.refund_behavior = "success"
                    outcome = self.billing.process_pending_intent(
                        intent.intent_id,
                        claim=claim,
                        ledger=ledger,
                        now=retry_at,
                        store=self.store,
                    )
                    self.assertTrue(outcome.confirmed)
                    self.assertEqual(outcome.next_state, "refund_pending")
                    self.assertEqual(len(ledger.refund_calls), original_refund_calls + 1)
                else:
                    ledger.refund_behavior = (
                        "malformed_result"
                        if result_kind == "conflict"
                        else "transport_before_effect"
                    )
                    outcome = self.billing.process_pending_intent(
                        intent.intent_id,
                        claim=claim,
                        ledger=ledger,
                        now=retry_at,
                        store=self.store,
                    )
                    self.assertEqual(len(ledger.refund_calls), original_refund_calls + 1)
                    self.assertEqual(outcome.next_state, "billing_reconciling")
                    self.assertEqual(
                        outcome.error_code,
                        "billing_authority_conflict"
                        if result_kind == "conflict"
                        else "billing_transport_unknown",
                    )
                    if result_kind == "timeout":
                        ledger.query_behavior = "transport"
                        live = self.billing.reconcile_unknown_intent(
                            intent.intent_id,
                            claim=claim,
                            ledger=ledger,
                            now=retry_at + self.billing.UNKNOWN_TIMEOUT_MS - 1,
                            store=self.store,
                        )
                        timed_out = self.billing.reconcile_unknown_intent(
                            intent.intent_id,
                            claim=claim,
                            ledger=ledger,
                            now=retry_at + self.billing.UNKNOWN_TIMEOUT_MS,
                            store=self.store,
                        )
                        self.assertEqual(live.next_state, "billing_reconciling")
                        self.assertEqual(
                            timed_out.next_state, "failed_reconciliation_pending"
                        )
                    elif result_kind == "stale":
                        successor_now = retry_at + 1_000_001
                        successor = self.store.claim_job(
                            created.job_id,
                            "late-delta-successor",
                            100,
                            successor_now,
                            expected_states={"billing_reconciling"},
                        )
                        self.assertIsNotNone(successor)
                        stale_before = json.dumps(
                            self.raw_snapshot(created.job_id),
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        ledger.query_calls.clear()
                        with self.assertRaises(LeaseLost):
                            self.billing.reconcile_unknown_intent(
                                intent.intent_id,
                                claim=claim,
                                ledger=ledger,
                                now=successor_now + 1,
                                store=self.store,
                            )
                        stale_after = json.dumps(
                            self.raw_snapshot(created.job_id),
                            sort_keys=True,
                            separators=(",", ":"),
                        )
                        self.assertEqual(stale_after, stale_before)
                        self.assertEqual(ledger.query_calls, [])

                after = self.raw_snapshot(created.job_id)
                persisted = next(
                    row for row in after["intents"] if row["id"] == intent.intent_id
                )
                self.assertEqual(
                    {row["external_idempotency_key"] for row in after["intents"]},
                    keys_before,
                )
                self.assertEqual(len(after["intents"]), 2)
                self.assertEqual(
                    (persisted["reason"], persisted["resume_state"]),
                    ("refund", "refund_pending"),
                )
                self.assertLessEqual(
                    after["job"]["confirmed_refunded_total"],
                    after["job"]["confirmed_preheld_total"],
                )

    def test_failed_pending_stale_tokens_are_zero_side_effect_for_all_operations(self):
        for operation in ("pre_debit", "refund_delta", "refund_full"):
            with self.subTest(operation=operation):
                created, intent, ledger, stale_claim, recovery_now = (
                    self.failed_pending_scenario(operation, "stale")
                )
                successor_now = recovery_now + 1_000_001
                successor = self.store.claim_job(
                    created.job_id,
                    f"successor-{operation}",
                    100,
                    successor_now,
                    expected_states={"failed_reconciliation_pending"},
                )
                self.assertIsNotNone(successor)
                before = json.dumps(
                    self.raw_snapshot(created.job_id),
                    sort_keys=True,
                    separators=(",", ":"),
                )
                ledger.query_calls.clear()

                with self.assertRaises(LeaseLost):
                    self.billing.reconcile_unknown_intent(
                        intent.intent_id,
                        claim=stale_claim,
                        ledger=ledger,
                        now=successor_now + 1,
                        store=self.store,
                    )

                after = json.dumps(
                    self.raw_snapshot(created.job_id),
                    sort_keys=True,
                    separators=(",", ":"),
                )
                self.assertEqual(after, before)
                self.assertEqual(ledger.query_calls, [])


if __name__ == "__main__":
    unittest.main()
