import json
import os
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from unittest.mock import patch

from server.content_domains import ai_edit_v2_store as store
from server.content_domains.ai_edit_v2_providers.openai_image import OpenAIImageProvider


EXPECTED_TABLES = {
    "edit_v2_jobs",
    "edit_v2_materials",
    "edit_v2_job_materials",
    "edit_v2_stage_attempts",
    "edit_v2_provider_jobs",
    "edit_v2_provider_events",
    "edit_v2_quotes",
    "edit_v2_billing",
    "edit_v2_render_artifacts",
    "edit_v2_schema_meta",
}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "ai_edit_v2.db")
        self.env = patch.dict(os.environ, {"AI_EDIT_V2_DB": self.db_path})
        self.env.start()
        store.init_db(self.db_path)

    def tearDown(self):
        self.env.stop()
        self.temp_dir.cleanup()

    def _create_job(self, *, owner="user-a", key="request-1", now=100):
        return store.create_job(
            owner,
            {"creation_mode": "natural_brief", "brief": "测试"},
            "quote-1",
            key,
            now,
        )

    def _create_v1_jobs_table(self, path, *, wal=False):
        with closing(sqlite3.connect(path)) as conn:
            if wal:
                conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """CREATE TABLE edit_v2_jobs(
                       id TEXT PRIMARY KEY, owner TEXT NOT NULL,
                       idempotency_key TEXT NOT NULL, quote_id TEXT NOT NULL,
                       status TEXT NOT NULL, payload_json TEXT NOT NULL,
                       director_plan_json TEXT,
                       checkpoint_json TEXT NOT NULL DEFAULT '[]',
                       lease_owner TEXT, lease_until INTEGER, error_code TEXT,
                       output_cos_key TEXT,
                       created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
                       UNIQUE(owner,idempotency_key)
                   )"""
            )

    def _create_v2_generated_materials(self, path):
        job_id = "123e4567-e89b-12d3-a456-426614174000"
        owner = "user-a"
        with closing(sqlite3.connect(path)) as conn:
            conn.executescript(
                """CREATE TABLE edit_v2_schema_meta(
                       id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL,
                       updated_at INTEGER NOT NULL
                   );
                   CREATE TABLE edit_v2_jobs(
                       id TEXT PRIMARY KEY, owner TEXT NOT NULL,
                       idempotency_key TEXT NOT NULL, quote_id TEXT NOT NULL,
                       predecessor_job_id TEXT REFERENCES edit_v2_jobs(id),
                       status TEXT NOT NULL, payload_json TEXT NOT NULL,
                       director_plan_json TEXT,
                       checkpoint_json TEXT NOT NULL DEFAULT '[]',
                       lease_owner TEXT, lease_until INTEGER, error_code TEXT,
                       output_cos_key TEXT,
                       created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
                       UNIQUE(owner,idempotency_key)
                   );
                   CREATE TABLE edit_v2_materials(
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       upload_id TEXT UNIQUE, owner TEXT NOT NULL, kind TEXT NOT NULL,
                       purpose TEXT NOT NULL, reference_mode TEXT,
                       semantic_label TEXT, source TEXT, cos_key TEXT NOT NULL,
                       filename TEXT, declared_content_type TEXT, mime_type TEXT,
                       etag TEXT, size_bytes INTEGER, duration_ms INTEGER,
                       width INTEGER, height INTEGER, reference_analysis_json TEXT,
                       status TEXT NOT NULL DEFAULT 'ready',
                       created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
                   );
                   CREATE UNIQUE INDEX idx_edit_v2_generated_idempotency
                       ON edit_v2_materials(owner,semantic_label)
                       WHERE source='gpt_image';"""
            )
            conn.execute(
                "INSERT INTO edit_v2_schema_meta(id,version,updated_at) VALUES(1,2,1)"
            )
            conn.execute(
                """INSERT INTO edit_v2_jobs(
                       id,owner,idempotency_key,quote_id,status,payload_json,
                       created_at,updated_at
                   ) VALUES(?,?,?,'quote-1','resolving_assets','{}',1,1)""",
                (job_id, owner, "job-key"),
            )
            for material_id, key, status in (
                (1, "legacy-ready", "ready"),
                (2, "legacy-pending", "pending"),
                (3, "legacy-failed", "failed"),
            ):
                conn.execute(
                    """INSERT INTO edit_v2_materials(
                           id,owner,kind,purpose,semantic_label,source,cos_key,
                           mime_type,etag,size_bytes,width,height,status,
                           created_at,updated_at
                       ) VALUES(?,?,'image','generated',?,'gpt_image',?,
                                'image/png','etag-v2',8,1536,1024,?,1,1)""",
                    (
                        material_id,
                        owner,
                        store._generation_label(job_id, key),
                        f"ai-edit-v2/fc95297aa4f56781/{job_id}/generated/{key}.png",
                        status,
                    ),
                )
            conn.commit()
        return owner, job_id

    def _queue(self, job_id):
        with closing(store.open_store(self.db_path)) as conn:
            conn.execute(
                "UPDATE edit_v2_jobs SET status='queued' WHERE id=?", (job_id,)
            )
            conn.commit()

    def _row(self, table, row_id):
        with closing(store.open_store(self.db_path)) as conn:
            return conn.execute(
                f"SELECT * FROM {table} WHERE id=?", (row_id,)
            ).fetchone()

    def test_init_db_creates_only_the_v2_domain_tables(self):
        with closing(store.open_store(self.db_path)) as conn:
            actual = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
                if not row[0].startswith("sqlite_")
            }

        self.assertEqual(actual, EXPECTED_TABLES)

    def test_init_db_migrates_existing_jobs_to_explicit_predecessor_links(self):
        legacy_path = os.path.join(self.temp_dir.name, "legacy-v1.db")
        self._create_v1_jobs_table(legacy_path)

        store.init_db(legacy_path)

        with closing(store.open_store(legacy_path)) as conn:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(edit_v2_jobs)")}
            version = conn.execute(
                "SELECT version FROM edit_v2_schema_meta WHERE id=1"
            ).fetchone()["version"]
        self.assertIn("predecessor_job_id", columns)
        self.assertEqual(version, 3)

    def test_concurrent_init_db_serializes_the_schema_migration(self):
        for scenario in ("new", "delete_v1", "wal_v1"):
            for attempt in range(50):
                legacy_path = os.path.join(
                    self.temp_dir.name, f"concurrent-{scenario}-{attempt}.db"
                )
                if scenario != "new":
                    self._create_v1_jobs_table(
                        legacy_path, wal=scenario == "wal_v1"
                    )
                barrier = threading.Barrier(2)

                def initialize_together():
                    barrier.wait(timeout=5)
                    store.init_db(legacy_path)

                with ThreadPoolExecutor(max_workers=2) as executor:
                    futures = [
                        executor.submit(initialize_together) for _ in range(2)
                    ]
                    for future in futures:
                        future.result(timeout=15)

                with closing(store.open_store(legacy_path)) as conn:
                    columns = [
                        row["name"]
                        for row in conn.execute("PRAGMA table_info(edit_v2_jobs)")
                    ]
                    version = conn.execute(
                        "SELECT version FROM edit_v2_schema_meta WHERE id=1"
                    ).fetchone()["version"]
                self.assertEqual(columns.count("predecessor_job_id"), 1)
                self.assertEqual(version, 3)

    def test_v2_generated_rows_upgrade_with_safe_ready_pending_and_failed_semantics(self):
        legacy_path = os.path.join(self.temp_dir.name, "legacy-v2-materials.db")
        owner, job_id = self._create_v2_generated_materials(legacy_path)

        store.init_db(legacy_path)

        with patch.dict(
            os.environ,
            {"AI_EDIT_V2_OPENAI_IMAGE_IDEMPOTENCY_ACCEPTED": "0"},
        ):
            legacy_ready_replay = OpenAIImageProvider(
                owner=owner,
                job_id=job_id,
                api_key="",
                asset_store=store,
                http_request=lambda *args: self.fail(
                    "ready replay must not call provider"
                ),
                db_path=legacy_path,
            ).generate(
                {"semantic_query": "legacy ready", "ratio": "16:9"},
                "legacy-ready",
            )
        self.assertEqual(legacy_ready_replay.payload["asset_id"], 1)

        def reserve(key, digest, worker, now):
            return store.reserve_generated_material(
                owner=owner,
                job_id=job_id,
                idempotency_key=key,
                cos_key=(
                    f"ai-edit-v2/fc95297aa4f56781/{job_id}/generated/{key}.png"
                ),
                request_digest=digest,
                lease_owner=worker,
                lease_seconds=10,
                now=now,
                db_path=legacy_path,
            )

        ready = reserve("legacy-ready", "ready-body", "worker-a", 100)
        pending = reserve("legacy-pending", "pending-body", "worker-a", 100)
        failed = reserve("legacy-failed", "failed-body", "worker-a", 100)

        self.assertFalse(ready["claimed"])
        self.assertEqual(ready["reason"], "ready")
        self.assertEqual(ready["material"]["generation_state"], "ready")
        self.assertTrue(pending["claimed"])
        self.assertEqual(
            pending["material"]["generation_state"], "unknown_submission"
        )
        self.assertEqual(
            pending["material"]["generation_request_digest"], "pending-body"
        )
        self.assertEqual(pending["material"]["generation_job_id"], job_id)
        self.assertEqual(
            pending["material"]["generation_idempotency_key"], "legacy-pending"
        )
        self.assertFalse(failed["claimed"])
        self.assertEqual(failed["reason"], "terminal_failed")
        self.assertEqual(failed["material"]["generation_state"], "terminal_failed")

        with self.assertRaisesRegex(ValueError, "generated_request_conflict"):
            reserve("legacy-pending", "changed-body", "worker-b", 111)

        replay = reserve("legacy-pending", "pending-body", "worker-b", 111)
        self.assertTrue(replay["claimed"])
        self.assertEqual(replay["material"]["id"], pending["material"]["id"])

    def test_open_store_enables_wal_foreign_keys_and_busy_timeout(self):
        with closing(store.open_store(self.db_path)) as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(conn.execute("PRAGMA busy_timeout").fetchone()[0], 10_000)

    def test_create_job_is_idempotent_for_owner_and_request_key(self):
        first = self._create_job()
        second = self._create_job(now=101)

        self.assertEqual(second["id"], first["id"])
        with closing(store.open_store(self.db_path)) as conn:
            count = conn.execute("SELECT COUNT(*) FROM edit_v2_jobs").fetchone()[0]
        self.assertEqual(count, 1)

    def test_create_job_rejects_idempotency_key_reuse_with_changed_request(self):
        with closing(store.open_store(self.db_path)) as conn:
            for material_id in (1, 2):
                conn.execute(
                    """INSERT INTO edit_v2_materials(
                           id,owner,kind,purpose,source,cos_key,filename,mime_type,
                           size_bytes,status,created_at,updated_at
                       ) VALUES(?, 'user-a','image','required','user_upload',?,?,
                                'image/png',100,'ready',1,1)""",
                    (material_id, f"private/{material_id}.png", f"{material_id}.png"),
                )
        first = store.create_job(
            "user-a", {"draft": {"brief": "first"}}, "quote-1", "same-key", 100,
            material_bindings=[{"material_id": 1, "purpose": "required"}],
        )
        cases = (
            ({"draft": {"brief": "first"}}, "quote-2", [{"material_id": 1, "purpose": "required"}]),
            ({"draft": {"brief": "second"}}, "quote-1", [{"material_id": 1, "purpose": "required"}]),
            ({"draft": {"brief": "first"}}, "quote-1", [{"material_id": 2, "purpose": "required"}]),
        )
        for payload, quote_id, bindings in cases:
            with self.subTest(quote_id=quote_id, payload=payload, bindings=bindings):
                with self.assertRaisesRegex(ValueError, "idempotency_conflict"):
                    store.create_job(
                        "user-a", payload, quote_id, "same-key", 101,
                        material_bindings=bindings,
                    )
        with closing(store.open_store(self.db_path)) as conn:
            actual = conn.execute(
                "SELECT material_id FROM edit_v2_job_materials WHERE job_id=?",
                (first["id"],),
            ).fetchall()
        self.assertEqual([row["material_id"] for row in actual], [1])

    def test_two_workers_cannot_claim_the_same_job(self):
        job = self._create_job()
        self._queue(job["id"])

        first = store.claim_next_job("worker-a", lease_seconds=30, now=200)
        second = store.claim_next_job("worker-b", lease_seconds=30, now=200)

        self.assertEqual(first["id"], job["id"])
        self.assertEqual(first["lease_owner"], "worker-a")
        self.assertIsNone(second)

    def test_expired_lease_is_recoverable_but_active_lease_is_not_stolen(self):
        job = self._create_job()
        self._queue(job["id"])
        store.claim_next_job("worker-a", lease_seconds=30, now=200)

        self.assertIsNone(
            store.claim_next_job("worker-b", lease_seconds=30, now=229)
        )
        recovered = store.claim_next_job("worker-b", lease_seconds=30, now=230)

        self.assertEqual(recovered["id"], job["id"])
        self.assertEqual(recovered["lease_owner"], "worker-b")
        self.assertEqual(recovered["lease_until"], 260)

    def test_only_the_lease_owner_can_renew_an_active_lease(self):
        job = self._create_job()
        self._queue(job["id"])
        store.claim_next_job("worker-a", lease_seconds=30, now=200)

        self.assertFalse(
            store.renew_lease(job["id"], "worker-b", lease_seconds=30, now=210)
        )
        self.assertTrue(
            store.renew_lease(job["id"], "worker-a", lease_seconds=30, now=210)
        )
        self.assertEqual(self._row("edit_v2_jobs", job["id"])["lease_until"], 240)

    def test_terminal_job_is_never_claimed(self):
        job = self._create_job()
        self._queue(job["id"])
        with closing(store.open_store(self.db_path)) as conn:
            conn.execute(
                "UPDATE edit_v2_jobs SET status='completed' WHERE id=?", (job["id"],)
            )
            conn.commit()

        self.assertIsNone(store.claim_next_job("worker-a", lease_seconds=30, now=200))

    def test_terminal_job_cannot_transition_to_another_failure(self):
        job = self._create_job()
        with closing(store.open_store(self.db_path)) as conn:
            conn.execute(
                "UPDATE edit_v2_jobs SET status='completed' WHERE id=?", (job["id"],)
            )
            conn.commit()

        self.assertFalse(
            store.transition(
                job["id"], "completed", "storage_failed", {"reason": "late error"}, 300
            )
        )
        self.assertEqual(self._row("edit_v2_jobs", job["id"])["status"], "completed")

    def test_transition_is_compare_and_swap_and_appends_checkpoint_versions(self):
        job = self._create_job()
        original_plan = {"version": "2.0", "scenes": [{"id": "scene_01"}]}
        with closing(store.open_store(self.db_path)) as conn:
            conn.execute(
                "UPDATE edit_v2_jobs SET status='directing', director_plan_json=? WHERE id=?",
                (json.dumps(original_plan), job["id"]),
            )
            conn.commit()

        self.assertTrue(
            store.transition(
                job["id"],
                "directing",
                "resolving_assets",
                {"provider_task_id": "qwen-123"},
                300,
            )
        )
        self.assertFalse(
            store.transition(
                job["id"],
                "directing",
                "resolving_assets",
                {"provider_task_id": "duplicate"},
                301,
            )
        )

        row = self._row("edit_v2_jobs", job["id"])
        self.assertEqual(json.loads(row["director_plan_json"]), original_plan)
        self.assertEqual(
            json.loads(row["checkpoint_json"]),
            [
                {
                    "version": 1,
                    "state": "resolving_assets",
                    "at": 300,
                    "data": {"provider_task_id": "qwen-123"},
                }
            ],
        )

    def test_provider_event_fingerprint_is_recorded_once(self):
        job = self._create_job()

        self.assertTrue(
            store.record_provider_event(
                job["id"], "shotstack", "render-1", "completed", "fingerprint-1", 400
            )
        )
        self.assertFalse(
            store.record_provider_event(
                job["id"], "shotstack", "render-1", "completed", "fingerprint-1", 401
            )
        )

    def test_generated_material_does_not_require_a_user_upload_id(self):
        with closing(store.open_store(self.db_path)) as conn:
            material_id = conn.execute(
                """INSERT INTO edit_v2_materials(
                       owner,kind,purpose,source,cos_key,status,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    "user-a",
                    "image",
                    "required",
                    "generated",
                    "ai-edit-v2/owner/task/generated/image.png",
                    "ready",
                    100,
                    100,
                ),
            ).lastrowid

        self.assertGreater(material_id, 0)

    def test_generated_material_creation_is_idempotent_and_job_scoped(self):
        job = self._create_job()
        fields = {
            "owner": "user-a",
            "job_id": job["id"],
            "idempotency_key": "image-slot-1",
            "cos_key": f"ai-edit-v2/fc95297aa4f56781/{job['id']}/generated/image.png",
            "mime_type": "image/png",
            "etag": "etag-1",
            "size_bytes": 8,
            "width": 1536,
            "height": 1024,
        }

        first = store.create_generated_material(**fields, now=100, db_path=self.db_path)
        second = store.create_generated_material(**fields, now=101, db_path=self.db_path)

        self.assertEqual(second["id"], first["id"])
        found = store.find_generated_material(
            "user-a", job["id"], "image-slot-1", db_path=self.db_path
        )
        self.assertEqual(found["id"], first["id"])
        self.assertNotIn("url", json.dumps(dict(found)).lower())
        with self.assertRaisesRegex(ValueError, "job_scope"):
            store.create_generated_material(
                **{**fields, "job_id": "223e4567-e89b-12d3-a456-426614174000"},
                now=102,
                db_path=self.db_path,
            )

    def test_generated_material_reservation_has_one_winner_and_terminal_states_do_not_reverse(self):
        job = self._create_job()
        fields = {
            "owner": "user-a",
            "job_id": job["id"],
            "idempotency_key": "reserved-image",
            "cos_key": f"ai-edit-v2/fc95297aa4f56781/{job['id']}/generated/image.png",
            "now": 100,
            "request_digest": "digest-reserved",
            "lease_owner": "worker-a",
            "lease_seconds": 30,
            "db_path": self.db_path,
        }

        winner = store.reserve_generated_material(**fields)
        loser = store.reserve_generated_material(
            **{**fields, "now": 101, "lease_owner": "worker-b"}
        )

        self.assertTrue(winner["claimed"])
        self.assertFalse(loser["claimed"])
        self.assertEqual(loser["material"]["status"], "pending")
        self.assertEqual(loser["reason"], "in_progress")

        self.assertTrue(
            store.mark_generated_material_submitting(
                owner="user-a",
                job_id=job["id"],
                idempotency_key="reserved-image",
                lease_owner="worker-a",
                now=101,
                db_path=self.db_path,
            )
        )
        self.assertTrue(
            store.mark_generated_material_provider_confirmed(
                owner="user-a",
                job_id=job["id"],
                idempotency_key="reserved-image",
                lease_owner="worker-a",
                provider_request_id="provider-request-1",
                now=102,
                db_path=self.db_path,
            )
        )

        ready = store.complete_generated_material(
            owner="user-a",
            job_id=job["id"],
            idempotency_key="reserved-image",
            cos_key=fields["cos_key"],
            mime_type="image/png",
            etag="etag-1",
            size_bytes=8,
            width=1536,
            height=1024,
            lease_owner="worker-a",
            now=103,
            db_path=self.db_path,
        )
        self.assertEqual(ready["status"], "ready")
        self.assertFalse(
            store.fail_generated_material(
                "user-a",
                job["id"],
                "reserved-image",
                lease_owner="worker-a",
                now=104,
                db_path=self.db_path,
            )
        )
        replay = store.reserve_generated_material(**{**fields, "now": 105})
        self.assertFalse(replay["claimed"])
        self.assertEqual(replay["material"]["status"], "ready")

    def test_terminal_generated_material_reservation_cannot_return_to_pending_or_ready(self):
        job = self._create_job()
        fields = {
            "owner": "user-a",
            "job_id": job["id"],
            "idempotency_key": "failed-image",
            "cos_key": f"ai-edit-v2/fc95297aa4f56781/{job['id']}/generated/image.png",
            "now": 100,
            "request_digest": "digest-failed",
            "lease_owner": "worker-a",
            "lease_seconds": 30,
            "db_path": self.db_path,
        }
        self.assertTrue(store.reserve_generated_material(**fields)["claimed"])
        store.mark_generated_material_submitting(
            owner="user-a",
            job_id=job["id"],
            idempotency_key="failed-image",
            lease_owner="worker-a",
            now=100,
            db_path=self.db_path,
        )
        self.assertTrue(
            store.fail_generated_material(
                "user-a", job["id"], "failed-image", lease_owner="worker-a",
                now=101, db_path=self.db_path
            )
        )

        replay = store.reserve_generated_material(
            **{**fields, "now": 102, "lease_owner": "worker-b"}
        )
        self.assertFalse(replay["claimed"])
        self.assertEqual(replay["material"]["status"], "failed")
        self.assertEqual(replay["material"]["generation_state"], "terminal_failed")
        with self.assertRaisesRegex(ValueError, "generated_material_not_pending"):
            store.complete_generated_material(
                owner="user-a",
                job_id=job["id"],
                idempotency_key="failed-image",
                cos_key=fields["cos_key"],
                mime_type="image/png",
                etag="etag-1",
                size_bytes=8,
                width=1536,
                height=1024,
                lease_owner="worker-b",
                now=103,
                db_path=self.db_path,
            )

    def test_generated_pre_submit_lease_can_be_reclaimed_only_after_expiry(self):
        job = self._create_job()
        fields = {
            "owner": "user-a",
            "job_id": job["id"],
            "idempotency_key": "crash-before-submit",
            "cos_key": f"ai-edit-v2/fc95297aa4f56781/{job['id']}/generated/image.png",
            "request_digest": "digest-crash",
            "lease_seconds": 10,
            "db_path": self.db_path,
        }

        first = store.reserve_generated_material(
            **fields, lease_owner="worker-a", now=100
        )
        active = store.reserve_generated_material(
            **fields, lease_owner="worker-b", now=109
        )
        reclaimed = store.reserve_generated_material(
            **fields, lease_owner="worker-b", now=110
        )

        self.assertTrue(first["claimed"])
        self.assertFalse(active["claimed"])
        self.assertEqual(active["reason"], "in_progress")
        self.assertTrue(reclaimed["claimed"])
        self.assertEqual(reclaimed["material"]["id"], first["material"]["id"])
        self.assertEqual(reclaimed["material"]["generation_state"], "pre_submit")
        self.assertEqual(reclaimed["material"]["generation_lease_owner"], "worker-b")

    def test_generated_retryable_state_obeys_backoff_then_reclaims(self):
        job = self._create_job()
        fields = {
            "owner": "user-a",
            "job_id": job["id"],
            "idempotency_key": "retryable-image",
            "cos_key": f"ai-edit-v2/fc95297aa4f56781/{job['id']}/generated/image.png",
            "request_digest": "digest-retryable",
            "lease_seconds": 10,
            "db_path": self.db_path,
        }
        store.reserve_generated_material(**fields, lease_owner="worker-a", now=100)
        store.mark_generated_material_submitting(
            owner="user-a", job_id=job["id"], idempotency_key="retryable-image",
            lease_owner="worker-a", now=101, db_path=self.db_path,
        )
        self.assertTrue(
            store.mark_generated_material_recoverable(
                owner="user-a", job_id=job["id"],
                idempotency_key="retryable-image", lease_owner="worker-a",
                state="retryable", retry_at=110, now=102, db_path=self.db_path,
            )
        )

        backing_off = store.reserve_generated_material(
            **fields, lease_owner="worker-b", now=109
        )
        recovered = store.reserve_generated_material(
            **fields, lease_owner="worker-b", now=110
        )

        self.assertFalse(backing_off["claimed"])
        self.assertEqual(backing_off["reason"], "retry_backoff")
        self.assertTrue(recovered["claimed"])
        self.assertEqual(recovered["material"]["generation_state"], "retryable")

    def test_expired_submitting_lease_is_reclassified_as_unknown_submission(self):
        job = self._create_job()
        fields = {
            "owner": "user-a",
            "job_id": job["id"],
            "idempotency_key": "crash-during-submit",
            "cos_key": f"ai-edit-v2/fc95297aa4f56781/{job['id']}/generated/image.png",
            "request_digest": "digest-submit-crash",
            "lease_seconds": 10,
            "db_path": self.db_path,
        }
        store.reserve_generated_material(
            **fields, lease_owner="worker-a", now=100
        )
        store.mark_generated_material_submitting(
            owner="user-a", job_id=job["id"],
            idempotency_key="crash-during-submit", lease_owner="worker-a",
            now=101, db_path=self.db_path,
        )

        recovered = store.reserve_generated_material(
            **fields, lease_owner="worker-b", now=110
        )

        self.assertTrue(recovered["claimed"])
        self.assertEqual(
            recovered["material"]["generation_state"], "unknown_submission"
        )
        self.assertEqual(
            recovered["material"]["generation_lease_owner"], "worker-b"
        )

    def test_generated_idempotency_key_rejects_a_changed_canonical_body(self):
        job = self._create_job()
        fields = {
            "owner": "user-a",
            "job_id": job["id"],
            "idempotency_key": "same-key-changed-body",
            "cos_key": f"ai-edit-v2/fc95297aa4f56781/{job['id']}/generated/image.png",
            "request_digest": "digest-original",
            "lease_owner": "worker-a",
            "lease_seconds": 10,
            "now": 100,
            "db_path": self.db_path,
        }
        store.reserve_generated_material(**fields)

        with self.assertRaisesRegex(ValueError, "generated_request_conflict"):
            store.reserve_generated_material(
                **{**fields, "request_digest": "digest-changed", "now": 111}
            )

    def test_material_resolution_records_are_idempotent_and_reject_urls(self):
        job = self._create_job()
        records = [
            {
                "slot_id": "slot_product_1",
                "semantic_query": "product close-up",
                "time_range": {"start_ms": 0, "end_ms": 2_000},
                "ratio": "16:9",
                "dimensions": {"width": 1920, "height": 1080},
                "source": "current_upload",
                "asset_id": "asset-1",
                "cos_key": "ai-edit-v2/safe/material.png",
                "required": True,
                "selected_score": 0.9,
                "exclusion_code": None,
            }
        ]

        first = store.save_material_resolution_records(
            job["id"], records, now=100, db_path=self.db_path
        )
        second = store.save_material_resolution_records(
            job["id"], records, now=101, db_path=self.db_path
        )

        self.assertEqual(second, first)
        row = self._row("edit_v2_stage_attempts", first)
        persisted = json.loads(row["output_summary_json"])["material_resolutions"]
        self.assertEqual(persisted, records)
        with self.assertRaisesRegex(ValueError, "unsafe_resolution_record"):
            store.save_material_resolution_records(
                job["id"],
                [{**records[0], "provider_url": "https://signed.example/x"}],
                now=102,
                db_path=self.db_path,
            )
        second_job = self._create_job(key="request-unsafe-url", now=103)
        with self.assertRaisesRegex(ValueError, "unsafe_resolution_record"):
            store.save_material_resolution_records(
                second_job["id"],
                [{**records[0], "cos_key": "https://provider.example/image.png?sig=x"}],
                now=104,
                db_path=self.db_path,
            )

    def test_material_resolution_completes_an_existing_stage_attempt(self):
        job = self._create_job()
        attempt_id = store.record_stage_attempt(
            job["id"],
            "resolving_assets",
            1,
            "running",
            90,
            db_path=self.db_path,
        )
        records = [
            {
                "slot_id": "slot_1",
                "semantic_query": "safe query",
                "time_range": {"start_ms": 0, "end_ms": 100},
                "ratio": "16:9",
                "dimensions": {"width": 1920, "height": 1080},
                "source": "platform_public",
                "asset_id": "asset-1",
                "cos_key": "ai-edit-v2/safe/material.png",
                "required": False,
                "selected_score": 0.8,
                "exclusion_code": None,
            }
        ]

        saved_id = store.save_material_resolution_records(
            job["id"], records, now=100, attempt=1, db_path=self.db_path
        )

        row = self._row("edit_v2_stage_attempts", attempt_id)
        self.assertEqual(saved_id, attempt_id)
        self.assertEqual(row["status"], "succeeded")
        self.assertEqual(row["finished_at"], 100)
        self.assertEqual(
            json.loads(row["output_summary_json"]),
            {"material_resolutions": records},
        )

    def test_failed_material_resolution_is_recorded_and_retry_uses_next_attempt(self):
        job = self._create_job()
        failed_records = [
            {
                "slot_id": "slot_1",
                "semantic_query": "product",
                "time_range": {"start_ms": 0, "end_ms": 100},
                "ratio": "16:9",
                "dimensions": {"width": 1920, "height": 1080},
                "source": "current_upload",
                "asset_id": "required-1",
                "cos_key": None,
                "required": True,
                "selected_score": None,
                "exclusion_code": "blurred",
            }
        ]
        succeeded_records = [
            {**failed_records[0], "cos_key": "ai-edit-v2/safe/product.png", "exclusion_code": None}
        ]

        failed_id = store.save_material_resolution_records(
            job["id"],
            failed_records,
            now=100,
            status="failed",
            error_code="required_material_unavailable",
            db_path=self.db_path,
        )
        succeeded_id = store.save_material_resolution_records(
            job["id"],
            succeeded_records,
            now=110,
            status="succeeded",
            db_path=self.db_path,
        )

        failed = self._row("edit_v2_stage_attempts", failed_id)
        succeeded = self._row("edit_v2_stage_attempts", succeeded_id)
        self.assertNotEqual(failed_id, succeeded_id)
        self.assertEqual((failed["attempt"], failed["status"]), (1, "failed"))
        self.assertEqual(failed["error_code"], "required_material_unavailable")
        self.assertEqual((succeeded["attempt"], succeeded["status"]), (2, "succeeded"))
        self.assertIsNone(succeeded["error_code"])
    def test_terminal_transition_clears_style_analysis_but_keeps_cos_and_audit(self):
        job = self._create_job()
        with closing(store.open_store(self.db_path)) as conn:
            conn.execute(
                "UPDATE edit_v2_jobs SET status='quality_check' WHERE id=?", (job["id"],)
            )
            material_id = conn.execute(
                """INSERT INTO edit_v2_materials(
                       upload_id, owner, kind, purpose, reference_mode, cos_key,
                       reference_analysis_json, created_at, updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    "123e4567-e89b-42d3-a456-426614174000",
                    "user-a",
                    "video",
                    "reference",
                    "style_only",
                    "ai-edit-v2/owner/material/reference.mp4",
                    '{"tempo":"fast"}',
                    100,
                    100,
                ),
            ).lastrowid
            conn.execute(
                "INSERT INTO edit_v2_job_materials(job_id,material_id,purpose,created_at) VALUES(?,?,?,?)",
                (job["id"], material_id, "reference", 100),
            )
            conn.execute(
                """INSERT INTO edit_v2_stage_attempts(
                       job_id,stage,attempt,status,started_at,finished_at
                   ) VALUES(?,?,?,?,?,?)""",
                (job["id"], "quality_check", 1, "failed", 200, 210),
            )
            conn.commit()

        self.assertTrue(
            store.transition(
                job["id"], "quality_check", "quality_failed", {"reason": "unsafe_crop"}, 220
            )
        )

        with closing(store.open_store(self.db_path)) as conn:
            material = conn.execute(
                "SELECT cos_key,reference_analysis_json FROM edit_v2_materials WHERE id=?",
                (material_id,),
            ).fetchone()
            attempts = conn.execute(
                "SELECT COUNT(*) FROM edit_v2_stage_attempts WHERE job_id=?",
                (job["id"],),
            ).fetchone()[0]
        self.assertEqual(material["cos_key"], "ai-edit-v2/owner/material/reference.mp4")
        self.assertIsNone(material["reference_analysis_json"])
        self.assertEqual(attempts, 1)


if __name__ == "__main__":
    unittest.main()
