import json
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from unittest.mock import patch

from server.content_domains import ai_edit_v2_store as store


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
        with closing(store.open_store(legacy_path)) as conn:
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

        store.init_db(legacy_path)

        with closing(store.open_store(legacy_path)) as conn:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(edit_v2_jobs)")}
            version = conn.execute(
                "SELECT version FROM edit_v2_schema_meta WHERE id=1"
            ).fetchone()["version"]
        self.assertIn("predecessor_job_id", columns)
        self.assertEqual(version, 2)

    def test_concurrent_init_db_serializes_the_schema_migration(self):
        for attempt in range(50):
            legacy_path = os.path.join(
                self.temp_dir.name, f"concurrent-v1-{attempt}.db"
            )
            with closing(store.open_store(legacy_path)) as conn:
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
            barrier = threading.Barrier(2)

            def initialize_together():
                barrier.wait(timeout=5)
                store.init_db(legacy_path)

            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [executor.submit(initialize_together) for _ in range(2)]
                for future in futures:
                    future.result(timeout=10)

            with closing(store.open_store(legacy_path)) as conn:
                columns = [
                    row["name"]
                    for row in conn.execute("PRAGMA table_info(edit_v2_jobs)")
                ]
                version = conn.execute(
                    "SELECT version FROM edit_v2_schema_meta WHERE id=1"
                ).fetchone()["version"]
            self.assertEqual(columns.count("predecessor_job_id"), 1)
            self.assertEqual(version, 2)

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
