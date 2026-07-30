import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from content_domains import video_asset_publish as publish


class VideoAssetPublishCoreIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(__file__).resolve().parents[1]

    def tearDown(self):
        self.temp.cleanup()

    def run_isolated(self, script, *, content_asset_db):
        env = dict(os.environ)
        env["CONTENT_OUT"] = str(Path(self.temp.name) / "content-out")
        if content_asset_db is None:
            env.pop("CONTENT_ASSET_DB", None)
        else:
            env["CONTENT_ASSET_DB"] = str(content_asset_db)
        return subprocess.run(
            [sys.executable, "-c", script],
            cwd=self.root,
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
        )

    def test_default_asset_database_path_is_unchanged_without_environment(self):
        result = self.run_isolated(
            """
import pathlib
import sys
sys.path.insert(0, str(pathlib.Path.cwd() / "server"))
from content_domains import core
expected = str(core.BASE / "audio_assets.db")
assert core.AUDIO_DB == expected, (core.AUDIO_DB, expected)
""",
            content_asset_db=None,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_core_and_assets_store_publish_through_content_asset_database(self):
        asset_db = Path(self.temp.name) / "shared-assets.db"
        result = self.run_isolated(
            """
import pathlib
import sys
from contextlib import closing
sys.path.insert(0, str(pathlib.Path.cwd() / "server"))
from content_domains import assets_store, core

target = pathlib.Path(__import__("os").environ["CONTENT_ASSET_DB"]).resolve()
assert pathlib.Path(core.AUDIO_DB).resolve() == target, core.AUDIO_DB
assert pathlib.Path(assets_store.ASSET_DB).resolve() == target, assets_store.ASSET_DB

class NoopVideoDomain:
    def backfill_audio_assets(self):
        pass

core._domains = lambda: (NoopVideoDomain(),)
core.init_audio_db()
decision = core.asset_publisher.register_generation(
    "ai_edit_v3", "same-db-job", 1, "same-db-register"
)
assert decision.status == "accepted", decision
with closing(core.adb()) as conn:
    core_path = pathlib.Path(conn.execute("PRAGMA database_list").fetchone()[2]).resolve()
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(video_assets)")}
with closing(assets_store.adb()) as conn:
    store_path = pathlib.Path(conn.execute("PRAGMA database_list").fetchone()[2]).resolve()
    publication_count = conn.execute(
        "SELECT COUNT(*) FROM video_asset_publications WHERE source_job_id='same-db-job'"
    ).fetchone()[0]
assert core_path == target
assert store_path == target
assert {"source_job_id", "publication_generation", "published_at"} <= columns
assert publication_count == 1
""",
            content_asset_db=asset_db,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(asset_db.is_file())


class VideoAssetPublishSchemaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / "assets.db")
        self.conn = self.connect()
        self.publisher = publish.AssetPublicationService(self.connect)

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def connect(self):
        conn = sqlite3.connect(self.db, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def create_legacy_video_assets_table(self):
        self.conn.execute(
            """CREATE TABLE video_assets(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER UNIQUE,
                username TEXT NOT NULL,
                mode TEXT NOT NULL,
                image_file TEXT,
                audio_file TEXT,
                reference_video_file TEXT,
                video_file TEXT,
                video_url TEXT,
                text TEXT,
                voice_key TEXT,
                resolution TEXT,
                ratio TEXT,
                motion TEXT,
                phase TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                error TEXT,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            )"""
        )

    def scalar(self, sql, params=()):
        return self.conn.execute(sql, params).fetchone()[0]

    def test_publication_schema_keeps_hidden_rows_out_of_video_assets(self):
        self.create_legacy_video_assets_table()
        publish.init_schema(self.conn)
        self.publisher.prepare_hidden(
            "ai_edit_v3",
            "job-1",
            "alice",
            "test/ai-edit-v3/o/job-1/delivery/a.mp4",
            3,
            "ai-edit-v3:job-1:publish:prepare:3",
        )
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM video_assets"), 0)
        self.assertEqual(
            self.scalar("SELECT COUNT(*) FROM video_asset_publications"), 1
        )
        index_sql = self.scalar(
            "SELECT sql FROM sqlite_master "
            "WHERE name='uq_video_assets_ai_edit_v3_source_job'"
        )
        normalized = " ".join(index_sql.lower().split())
        self.assertIn(
            "where mode='ai_edit_v3' and source_job_id is not null",
            normalized,
        )


class VideoAssetPublishArbitrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / "assets.db")
        self.conn = self.connect()
        VideoAssetPublishSchemaTests.create_legacy_video_assets_table(self)
        publish.init_schema(self.conn)
        self.publisher = publish.AssetPublicationService(self.connect)
        self.key = "test/ai-edit-v3/o/job-1/delivery/final.mp4"
        self.key2 = "test/ai-edit-v3/o/job-2/delivery/final.mp4"

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def connect(self):
        conn = sqlite3.connect(self.db, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def visible_assets(self, source_job_id):
        return self.conn.execute(
            "SELECT * FROM video_assets WHERE source_job_id=?",
            (source_job_id,),
        ).fetchall()

    def operation_count(self, idempotency_key):
        return self.conn.execute(
            "SELECT COUNT(*) FROM video_asset_publication_ops "
            "WHERE idempotency_key=?",
            (idempotency_key,),
        ).fetchone()[0]

    def publication(self, source_job_id):
        return self.conn.execute(
            """SELECT * FROM video_asset_publications
               WHERE mode='ai_edit_v3' AND source_job_id=?""",
            (source_job_id,),
        ).fetchone()

    def test_identical_replay_after_response_loss_persists_each_operation_once(self):
        calls = [
            (
                "register",
                lambda: self.publisher.register_generation(
                    "ai_edit_v3", "job-loss", 9, "loss-register"
                ),
            ),
            (
                "prepare",
                lambda: self.publisher.prepare_hidden(
                    "ai_edit_v3",
                    "job-loss",
                    "alice",
                    "test/ai-edit-v3/o/job-loss/delivery/final.mp4",
                    9,
                    "loss-prepare",
                ),
            ),
            (
                "query",
                lambda: self.publisher.query_decision(
                    "ai_edit_v3", "job-loss", "loss-query"
                ),
            ),
            (
                "commit",
                lambda: self.publisher.commit_publish(
                    "ai_edit_v3", "job-loss", 9, "loss-commit"
                ),
            ),
            (
                "cancel",
                lambda: self.publisher.cancel_publish(
                    "ai_edit_v3", "job-loss", 9, "loss-cancel"
                ),
            ),
        ]
        for name, call in calls:
            with self.subTest(operation=name):
                first = call()  # Simulate a committed response that the caller lost.
                self.publisher = publish.AssetPublicationService(self.connect)
                replay = call()
                self.assertEqual(replay, first)
                self.assertEqual(self.operation_count(f"loss-{name}"), 1)
        self.assertEqual(len(self.visible_assets("job-loss")), 1)

    def test_conflicting_idempotency_key_reuse_fails_closed(self):
        self.publisher.register_generation(
            "ai_edit_v3", "job-conflict", 4, "conflict-key"
        )
        conflicting_calls = [
            lambda: self.publisher.register_generation(
                "ai_edit_v3", "job-conflict", 5, "conflict-key"
            ),
            lambda: self.publisher.register_generation(
                "ai_edit_v3", "other-job", 4, "conflict-key"
            ),
            lambda: self.publisher.query_decision(
                "ai_edit_v3", "job-conflict", "conflict-key"
            ),
        ]
        for call in conflicting_calls:
            with self.subTest(call=call):
                with self.assertRaisesRegex(ValueError, "idempotency_conflict"):
                    call()
        self.assertEqual(self.operation_count("conflict-key"), 1)
        self.assertIsNone(self.publication("other-job"))

    def test_prepare_key_reuse_with_different_immutable_payload_fails_closed(self):
        self.publisher.register_generation(
            "ai_edit_v3", "job-prepare-conflict", 3, "pc-register"
        )
        self.publisher.prepare_hidden(
            "ai_edit_v3",
            "job-prepare-conflict",
            "alice",
            "test/ai-edit-v3/o/job-prepare-conflict/delivery/final.mp4",
            3,
            "pc-prepare",
        )
        with self.assertRaisesRegex(ValueError, "idempotency_conflict"):
            self.publisher.prepare_hidden(
                "ai_edit_v3",
                "job-prepare-conflict",
                "mallory",
                "test/ai-edit-v3/o/job-prepare-conflict/delivery/forged.mp4",
                3,
                "pc-prepare",
            )
        row = self.publication("job-prepare-conflict")
        self.assertEqual(row["owner"], "alice")
        self.assertEqual(
            row["object_key"],
            "test/ai-edit-v3/o/job-prepare-conflict/delivery/final.mp4",
        )

    def test_generation_advance_requires_fresh_prepare_before_commit(self):
        key7 = "test/ai-edit-v3/o/job-reprepare/delivery/gen-7.mp4"
        key8 = "test/ai-edit-v3/o/job-reprepare/delivery/gen-8.mp4"
        self.publisher.register_generation(
            "ai_edit_v3", "job-reprepare", 7, "reprepare-register-7"
        )
        self.publisher.prepare_hidden(
            "ai_edit_v3",
            "job-reprepare",
            "alice",
            key7,
            7,
            "reprepare-prepare-7",
        )
        advanced = self.publisher.register_generation(
            "ai_edit_v3", "job-reprepare", 8, "reprepare-register-8"
        )
        premature = self.publisher.commit_publish(
            "ai_edit_v3", "job-reprepare", 8, "reprepare-commit-before-prepare-8"
        )

        self.assertEqual(advanced.status, "accepted")
        self.assertEqual(advanced.current_generation, 8)
        self.assertEqual(premature.status, "accepted")
        self.assertEqual(premature.current_generation, 8)
        self.assertIsNone(premature.asset_id)
        self.assertEqual(self.visible_assets("job-reprepare"), [])
        row = self.publication("job-reprepare")
        self.assertIsNone(row["prepared_generation"])
        self.assertIsNone(row["owner"])
        self.assertIsNone(row["object_key"])

        prepared = self.publisher.prepare_hidden(
            "ai_edit_v3",
            "job-reprepare",
            "bob",
            key8,
            8,
            "reprepare-prepare-8",
        )
        won = self.publisher.commit_publish(
            "ai_edit_v3", "job-reprepare", 8, "reprepare-commit-after-prepare-8"
        )
        self.assertEqual(prepared.status, "accepted")
        self.assertEqual(won.status, "publish_won")
        visible = self.visible_assets("job-reprepare")
        self.assertEqual(len(visible), 1)
        self.assertEqual(visible[0]["username"], "bob")
        self.assertEqual(visible[0]["video_file"], key8)
        self.assertEqual(visible[0]["publication_generation"], 8)

    def test_same_generation_new_key_rejects_different_prepared_payload(self):
        original_key = "test/ai-edit-v3/o/job-frozen/delivery/final.mp4"
        self.publisher.register_generation(
            "ai_edit_v3", "job-frozen", 4, "frozen-register"
        )
        self.publisher.prepare_hidden(
            "ai_edit_v3",
            "job-frozen",
            "alice",
            original_key,
            4,
            "frozen-prepare-original",
        )

        with self.assertRaisesRegex(ValueError, "prepared_payload_conflict"):
            self.publisher.prepare_hidden(
                "ai_edit_v3",
                "job-frozen",
                "mallory",
                "test/ai-edit-v3/o/job-frozen/delivery/forged.mp4",
                4,
                "frozen-prepare-conflict",
            )

        row = self.publication("job-frozen")
        self.assertEqual(row["prepared_generation"], 4)
        self.assertEqual(row["owner"], "alice")
        self.assertEqual(row["object_key"], original_key)
        self.assertEqual(self.operation_count("frozen-prepare-conflict"), 0)
        self.assertEqual(self.visible_assets("job-frozen"), [])

    def test_same_generation_new_key_accepts_identical_prepared_payload(self):
        object_key = "test/ai-edit-v3/o/job-identical/delivery/final.mp4"
        self.publisher.register_generation(
            "ai_edit_v3", "job-identical", 5, "identical-register"
        )
        first = self.publisher.prepare_hidden(
            "ai_edit_v3",
            "job-identical",
            "alice",
            object_key,
            5,
            "identical-prepare-1",
        )
        second = self.publisher.prepare_hidden(
            "ai_edit_v3",
            "job-identical",
            "alice",
            object_key,
            5,
            "identical-prepare-2",
        )

        self.assertEqual(second, first)
        self.assertEqual(self.operation_count("identical-prepare-2"), 1)
        row = self.publication("job-identical")
        self.assertEqual(row["prepared_generation"], 5)
        self.assertEqual(row["owner"], "alice")
        self.assertEqual(row["object_key"], object_key)
        self.assertEqual(self.visible_assets("job-identical"), [])

    def test_higher_generation_fences_every_stale_mutation(self):
        original_key = "test/ai-edit-v3/o/job-fence/delivery/final.mp4"
        self.publisher.register_generation(
            "ai_edit_v3", "job-fence", 7, "fence-register-7"
        )
        self.publisher.prepare_hidden(
            "ai_edit_v3",
            "job-fence",
            "alice",
            original_key,
            7,
            "fence-prepare-7",
        )
        advanced = self.publisher.register_generation(
            "ai_edit_v3", "job-fence", 8, "fence-register-8"
        )
        self.assertEqual(advanced.current_generation, 8)

        stale_register = self.publisher.register_generation(
            "ai_edit_v3", "job-fence", 6, "fence-register-6"
        )
        stale_prepare = self.publisher.prepare_hidden(
            "ai_edit_v3",
            "job-fence",
            "mallory",
            "test/ai-edit-v3/o/job-fence/delivery/stale.mp4",
            7,
            "fence-stale-prepare",
        )
        stale_commit = self.publisher.commit_publish(
            "ai_edit_v3", "job-fence", 7, "fence-stale-commit"
        )
        stale_cancel = self.publisher.cancel_publish(
            "ai_edit_v3", "job-fence", 7, "fence-stale-cancel"
        )
        for decision in (
            stale_register,
            stale_prepare,
            stale_commit,
            stale_cancel,
        ):
            self.assertEqual(decision.status, "stale_generation")
            self.assertEqual(decision.current_generation, 8)
            self.assertIsNone(decision.asset_id)
        row = self.publication("job-fence")
        self.assertIsNone(row["prepared_generation"])
        self.assertIsNone(row["object_key"])
        self.assertIsNone(row["owner"])
        self.assertIsNone(row["verdict"])
        self.assertEqual(self.visible_assets("job-fence"), [])

    def test_query_key_replay_is_deterministic_and_read_only(self):
        self.publisher.register_generation(
            "ai_edit_v3", "job-query", 5, "query-register"
        )
        before = self.publisher.query_decision(
            "ai_edit_v3", "job-query", "query-before-final"
        )
        self.publisher.prepare_hidden(
            "ai_edit_v3",
            "job-query",
            "alice",
            "test/ai-edit-v3/o/job-query/delivery/final.mp4",
            5,
            "query-prepare",
        )
        won = self.publisher.commit_publish(
            "ai_edit_v3", "job-query", 5, "query-commit"
        )
        replay = self.publisher.query_decision(
            "ai_edit_v3", "job-query", "query-before-final"
        )
        current = self.publisher.query_decision(
            "ai_edit_v3", "job-query", "query-after-final"
        )
        self.assertEqual(before.status, "accepted")
        self.assertEqual(replay, before)
        self.assertEqual(current, won)
        self.assertEqual(self.operation_count("query-before-final"), 1)
        row = self.publication("job-query")
        self.assertEqual(row["current_generation"], 5)
        self.assertEqual(row["verdict"], "publish_won")

    def test_final_verdict_is_immutable_across_higher_generations(self):
        publish_key = "test/ai-edit-v3/o/job-final-publish/delivery/final.mp4"
        self.publisher.register_generation(
            "ai_edit_v3", "job-final-publish", 4, "fp-register-4"
        )
        self.publisher.prepare_hidden(
            "ai_edit_v3",
            "job-final-publish",
            "alice",
            publish_key,
            4,
            "fp-prepare-4",
        )
        publish_won = self.publisher.commit_publish(
            "ai_edit_v3", "job-final-publish", 4, "fp-commit-4"
        )
        publish_late = [
            self.publisher.register_generation(
                "ai_edit_v3", "job-final-publish", 5, "fp-register-5"
            ),
            self.publisher.prepare_hidden(
                "ai_edit_v3",
                "job-final-publish",
                "mallory",
                "test/ai-edit-v3/o/job-final-publish/delivery/forged.mp4",
                5,
                "fp-prepare-5",
            ),
            self.publisher.cancel_publish(
                "ai_edit_v3", "job-final-publish", 5, "fp-cancel-5"
            ),
        ]
        self.assertTrue(all(item == publish_won for item in publish_late))
        publish_row = self.publication("job-final-publish")
        self.assertEqual(publish_row["current_generation"], 4)
        self.assertEqual(publish_row["object_key"], publish_key)
        self.assertEqual(len(self.visible_assets("job-final-publish")), 1)

        self.publisher.register_generation(
            "ai_edit_v3", "job-final-cancel", 6, "fc-register-6"
        )
        self.publisher.prepare_hidden(
            "ai_edit_v3",
            "job-final-cancel",
            "alice",
            "test/ai-edit-v3/o/job-final-cancel/delivery/final.mp4",
            6,
            "fc-prepare-6",
        )
        cancel_won = self.publisher.cancel_publish(
            "ai_edit_v3", "job-final-cancel", 6, "fc-cancel-6"
        )
        cancel_late = [
            self.publisher.register_generation(
                "ai_edit_v3", "job-final-cancel", 7, "fc-register-7"
            ),
            self.publisher.prepare_hidden(
                "ai_edit_v3",
                "job-final-cancel",
                "alice",
                "test/ai-edit-v3/o/job-final-cancel/delivery/late.mp4",
                7,
                "fc-prepare-7",
            ),
            self.publisher.commit_publish(
                "ai_edit_v3", "job-final-cancel", 7, "fc-commit-7"
            ),
        ]
        self.assertTrue(all(item == cancel_won for item in cancel_late))
        cancel_row = self.publication("job-final-cancel")
        self.assertEqual(cancel_row["current_generation"], 6)
        self.assertEqual(cancel_row["verdict"], "cancel_won")
        self.assertEqual(self.visible_assets("job-final-cancel"), [])

    def test_missing_query_replay_stays_none_after_publication_is_created(self):
        first = self.publisher.query_decision(
            "ai_edit_v3", "job-query-missing", "query-missing"
        )
        self.publisher.register_generation(
            "ai_edit_v3", "job-query-missing", 2, "query-missing-register"
        )
        replay = self.publisher.query_decision(
            "ai_edit_v3", "job-query-missing", "query-missing"
        )
        self.assertIsNone(first)
        self.assertIsNone(replay)
        self.assertEqual(self.operation_count("query-missing"), 1)
        self.assertEqual(self.publication("job-query-missing")["current_generation"], 2)

    def test_commit_writes_exactly_one_immutable_v3_asset(self):
        object_key = "test/ai-edit-v3/o/job-fields/delivery/final.mp4"
        self.publisher.register_generation(
            "ai_edit_v3", "job-fields", 12, "fields-register"
        )
        self.publisher.prepare_hidden(
            "ai_edit_v3",
            "job-fields",
            "alice",
            object_key,
            12,
            "fields-prepare",
        )
        won = self.publisher.commit_publish(
            "ai_edit_v3", "job-fields", 12, "fields-commit"
        )
        replay = self.publisher.commit_publish(
            "ai_edit_v3", "job-fields", 12, "fields-commit-replay-key"
        )
        rows = self.visible_assets("job-fields")
        self.assertEqual(len(rows), 1)
        self.assertEqual(replay, won)
        self.assertEqual(
            dict(rows[0]),
            {
                "id": int(won.asset_id),
                "job_id": None,
                "username": "alice",
                "mode": "ai_edit_v3",
                "image_file": None,
                "audio_file": None,
                "reference_video_file": None,
                "video_file": object_key,
                "video_url": None,
                "text": None,
                "voice_key": None,
                "resolution": None,
                "ratio": None,
                "motion": None,
                "phase": "completed",
                "status": "done",
                "error": None,
                "created_at": rows[0]["created_at"],
                "updated_at": rows[0]["updated_at"],
                "source_job_id": "job-fields",
                "publication_generation": 12,
                "published_at": rows[0]["published_at"],
            },
        )

    def test_partial_index_isolates_v3_without_constraining_legacy_modes(self):
        base_values = (
            None,
            "alice",
            "legacy-a",
            "legacy-source",
            "done",
            "completed",
            1,
            1,
        )
        self.conn.execute(
            """INSERT INTO video_assets(
                   job_id,username,mode,source_job_id,status,phase,
                   created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?)""",
            base_values,
        )
        self.conn.execute(
            """INSERT INTO video_assets(
                   job_id,username,mode,source_job_id,status,phase,
                   created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (None, "bob", "legacy-b", "legacy-source", "done", "completed", 1, 1),
        )
        self.conn.execute(
            """INSERT INTO video_assets(
                   job_id,username,mode,source_job_id,status,phase,
                   created_at,updated_at
               ) VALUES(NULL,'alice','ai_edit_v3','v3-source','done',
                        'completed',1,1)"""
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                """INSERT INTO video_assets(
                       job_id,username,mode,source_job_id,status,phase,
                       created_at,updated_at
                   ) VALUES(NULL,'bob','ai_edit_v3','v3-source','done',
                            'completed',1,1)"""
            )

    def test_concurrent_commit_cancel_has_one_authoritative_winner(self):
        for round_index in range(8):
            source_job_id = f"job-race-{round_index}"
            generation = round_index + 1
            self.publisher.register_generation(
                "ai_edit_v3",
                source_job_id,
                generation,
                f"race-register-{round_index}",
            )
            self.publisher.prepare_hidden(
                "ai_edit_v3",
                source_job_id,
                "alice",
                f"test/ai-edit-v3/o/{source_job_id}/delivery/final.mp4",
                generation,
                f"race-prepare-{round_index}",
            )
            barrier = threading.Barrier(2)

            def commit():
                barrier.wait()
                return self.publisher.commit_publish(
                    "ai_edit_v3",
                    source_job_id,
                    generation,
                    f"race-commit-{round_index}",
                )

            def cancel():
                barrier.wait()
                return self.publisher.cancel_publish(
                    "ai_edit_v3",
                    source_job_id,
                    generation,
                    f"race-cancel-{round_index}",
                )

            with ThreadPoolExecutor(max_workers=2) as executor:
                commit_future = executor.submit(commit)
                cancel_future = executor.submit(cancel)
                decisions = [
                    commit_future.result(timeout=5),
                    cancel_future.result(timeout=5),
                ]
            self.assertEqual(decisions[0], decisions[1])
            self.assertIn(decisions[0].status, {"publish_won", "cancel_won"})
            visible_count = len(self.visible_assets(source_job_id))
            self.assertEqual(
                visible_count,
                1 if decisions[0].status == "publish_won" else 0,
            )
            row = self.publication(source_job_id)
            self.assertEqual(row["verdict"], decisions[0].status)

    def test_external_idempotency_key_is_required_and_mode_is_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "idempotency_key_required"):
            self.publisher.register_generation("ai_edit_v3", "job-key", 1, "")
        with self.assertRaisesRegex(ValueError, "unsupported_publication_mode"):
            self.publisher.register_generation("ai_edit_v2", "job-mode", 1, "mode-key")
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) FROM video_asset_publications"
            ).fetchone()[0],
            0,
        )

    def test_injected_connection_factory_needs_no_row_factory_configuration(self):
        plain = publish.AssetPublicationService(
            lambda: sqlite3.connect(self.db, timeout=10, isolation_level=None)
        )
        registered = plain.register_generation(
            "ai_edit_v3", "job-plain-connection", 1, "plain-register"
        )
        queried = plain.query_decision(
            "ai_edit_v3", "job-plain-connection", "plain-query"
        )
        self.assertEqual(registered.status, "accepted")
        self.assertEqual(queried, registered)

    def test_cancel_tombstone_beats_late_commit(self):
        self.publisher.register_generation("ai_edit_v3", "job-1", 7, "reg-7")
        self.publisher.prepare_hidden(
            "ai_edit_v3", "job-1", "alice", self.key, 7, "prep-7"
        )
        cancelled = self.publisher.cancel_publish(
            "ai_edit_v3", "job-1", 7, "cancel-7"
        )
        late = self.publisher.commit_publish(
            "ai_edit_v3", "job-1", 7, "commit-7"
        )
        self.assertEqual(cancelled.status, "cancel_won")
        self.assertEqual(late.status, "cancel_won")
        self.assertEqual(self.visible_assets("job-1"), [])

    def test_publish_winner_returns_stable_asset_and_blocks_refund_side(self):
        self.publisher.register_generation("ai_edit_v3", "job-2", 4, "reg-4")
        self.publisher.prepare_hidden(
            "ai_edit_v3", "job-2", "alice", self.key2, 4, "prep-4"
        )
        won = self.publisher.commit_publish(
            "ai_edit_v3", "job-2", 4, "commit-4"
        )
        late_cancel = self.publisher.cancel_publish(
            "ai_edit_v3", "job-2", 4, "cancel-4"
        )
        self.assertEqual(won.status, "publish_won")
        self.assertEqual(late_cancel.status, "publish_won")
        self.assertEqual(late_cancel.asset_id, won.asset_id)

    def test_query_decision_replays_the_same_persisted_external_key(self):
        self.publisher.register_generation("ai_edit_v3", "job-3", 5, "reg-5")
        first = self.publisher.query_decision(
            "ai_edit_v3", "job-3", "ai-edit-v3:job-3:publish:query:5"
        )
        second = self.publisher.query_decision(
            "ai_edit_v3", "job-3", "ai-edit-v3:job-3:publish:query:5"
        )
        self.assertEqual(second, first)
        self.assertEqual(
            self.operation_count("ai-edit-v3:job-3:publish:query:5"), 1
        )


if __name__ == "__main__":
    unittest.main()
