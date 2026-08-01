from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

from server.content_domains.ai_edit_v3 import contracts
from server.content_domains.ai_edit_v3.store import (
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


if __name__ == "__main__":
    unittest.main()
