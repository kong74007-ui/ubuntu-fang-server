# -*- coding: utf-8 -*-
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from content_domains import ai_edit_store


class AiEditStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = pathlib.Path(self.tmp.name) / "ai_edit.db"
        ai_edit_store.init_db(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_creates_all_tables_and_indexes(self):
        with closing(sqlite3.connect(self.db)) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            indexes = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                )
            }
        self.assertTrue(
            {"edit_jobs", "edit_materials", "edit_job_assets", "billing_holds"}
            <= tables
        )
        self.assertIn("idx_edit_jobs_username_created", indexes)

    def test_job_reads_are_owner_scoped_and_stage_updates_are_observable(self):
        created = ai_edit_store.create_edit_job(
            self.db, 40, "fang", "knowledge_dynamic", "shotstack", 30
        )
        self.assertEqual("fang", created["username"])
        self.assertIsNone(ai_edit_store.get_owned_job(self.db, "other", 40))
        self.assertTrue(
            ai_edit_store.update_stage(
                self.db, 40, "transcribing", "asr_pending", "provider queued"
            )
        )
        updated = ai_edit_store.get_owned_job(self.db, "fang", 40)
        self.assertEqual("transcribing", updated["stage"])
        self.assertEqual("asr_pending", updated["error_code"])
        self.assertEqual("provider queued", updated["error_detail"])
        self.assertFalse(ai_edit_store.update_stage(self.db, 999, "missing"))

    def test_provider_job_is_idempotent(self):
        ai_edit_store.create_edit_job(
            self.db, 41, "fang", "knowledge_dynamic", "shotstack", 30
        )
        self.assertTrue(
            ai_edit_store.set_provider_job(self.db, 41, "render-1", "queued")
        )
        self.assertTrue(
            ai_edit_store.set_provider_job(self.db, 41, "render-1", "rendering")
        )
        row = ai_edit_store.get_owned_job(self.db, "fang", 41)
        self.assertEqual("render-1", row["provider_job_id"])
        self.assertEqual("rendering", row["provider_status"])
        with self.assertRaises(ValueError):
            ai_edit_store.set_provider_job(self.db, 41, "render-2", "queued")

    def test_material_completion_is_owned_and_attachment_is_idempotent(self):
        ai_edit_store.create_edit_job(
            self.db, 43, "fang", "product_story", "shotstack", 30
        )
        material = ai_edit_store.create_material(
            self.db,
            "m43",
            "fang",
            "image",
            "product",
            "uploaded",
            "edit-input/fang/m43.png",
            "image/png",
            120,
        )
        self.assertEqual("pending", material["status"])
        self.assertFalse(ai_edit_store.complete_material(self.db, "m43", "other", 120))
        self.assertTrue(ai_edit_store.complete_material(self.db, "m43", "fang", 120))
        self.assertFalse(ai_edit_store.complete_material(self.db, "m43", "fang", 121))
        self.assertTrue(ai_edit_store.attach_material(self.db, 43, "m43", "product"))
        self.assertFalse(ai_edit_store.attach_material(self.db, 43, "m43", "product"))

    def test_hold_can_only_reach_one_terminal_state(self):
        ai_edit_store.create_edit_job(
            self.db, 42, "fang", "product_story", "shotstack", 30
        )
        self.assertTrue(ai_edit_store.confirm_hold(self.db, 42))
        self.assertFalse(ai_edit_store.release_hold(self.db, 42))

    def test_additive_migration_keeps_legacy_rows(self):
        legacy = pathlib.Path(self.tmp.name) / "legacy.db"
        with closing(sqlite3.connect(legacy)) as connection:
            connection.execute(
                "CREATE TABLE edit_materials("
                "id TEXT PRIMARY KEY, username TEXT, kind TEXT, role TEXT, cos_key TEXT)"
            )
            connection.execute(
                "INSERT INTO edit_materials VALUES('m1','fang','image','product','old/key.png')"
            )
            connection.commit()
        ai_edit_store.init_db(legacy)
        with closing(sqlite3.connect(legacy)) as connection:
            cols = {
                row[1] for row in connection.execute("PRAGMA table_info(edit_materials)")
            }
            kept = connection.execute(
                "SELECT cos_key FROM edit_materials WHERE id='m1'"
            ).fetchone()[0]
            status = connection.execute(
                "SELECT status FROM edit_materials WHERE id='m1'"
            ).fetchone()[0]
        self.assertIn("origin", cols)
        self.assertEqual("old/key.png", kept)
        self.assertEqual("pending", status)


if __name__ == "__main__":
    unittest.main()
