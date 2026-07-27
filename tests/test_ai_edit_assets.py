# -*- coding: utf-8 -*-
import os
import pathlib
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from content_domains import ai_edit_assets, ai_edit_store, audio, cos, video


class _RemoteResponse:
    def __init__(self, chunks, content_type="video/mp4"):
        self._chunks = iter(chunks)
        self.headers = {"Content-Type": content_type}

    def read(self, _size):
        return next(self._chunks, b"")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class AiEditAssetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = pathlib.Path(self.tmp.name) / "assets.db"
        with closing(sqlite3.connect(self.db)) as connection:
            connection.execute(
                """CREATE TABLE video_assets(
                    id INTEGER PRIMARY KEY, username TEXT, status TEXT,
                    video_file TEXT, video_url TEXT, source_video_url TEXT)"""
            )
            connection.execute(
                """CREATE TABLE audio_assets(
                    id INTEGER PRIMARY KEY, username TEXT, deleted INTEGER DEFAULT 0,
                    file TEXT, url TEXT)"""
            )
            connection.commit()

    def tearDown(self):
        self.tmp.cleanup()

    def _db(self):
        connection = sqlite3.connect(self.db)
        connection.row_factory = sqlite3.Row
        return connection

    def test_owned_video_asset_does_not_cross_users(self):
        with closing(self._db()) as connection:
            cursor = connection.execute(
                """INSERT INTO video_assets(
                    username,status,video_file,video_url,source_video_url
                ) VALUES(?,?,?,?,?)""",
                ("fang", "done", "video/source.mp4", "/local", None),
            )
            asset_id = cursor.lastrowid
            connection.commit()
        with mock.patch.object(video, "adb", self._db):
            self.assertEqual(
                asset_id, video.get_owned_video_asset("fang", asset_id)["id"]
            )
            self.assertIsNone(video.get_owned_video_asset("other", asset_id))

    def test_owned_audio_asset_does_not_cross_users_or_return_deleted_rows(self):
        with closing(self._db()) as connection:
            cursor = connection.execute(
                "INSERT INTO audio_assets(username,deleted,file,url) VALUES(?,?,?,?)",
                ("fang", 0, "audio/source.mp3", "/local"),
            )
            asset_id = cursor.lastrowid
            deleted_id = connection.execute(
                "INSERT INTO audio_assets(username,deleted,file,url) VALUES(?,?,?,?)",
                ("fang", 1, "audio/deleted.mp3", "/deleted"),
            ).lastrowid
            connection.commit()
        with mock.patch.object(audio, "adb", self._db):
            self.assertEqual(
                asset_id, audio.get_owned_audio_asset("fang", asset_id)["id"]
            )
            self.assertIsNone(audio.get_owned_audio_asset("other", asset_id))
            self.assertIsNone(audio.get_owned_audio_asset("fang", deleted_id))

    @mock.patch.object(cos, "_client")
    def test_presigned_put_is_short_lived_and_scoped(self, client):
        client.return_value.get_presigned_url.return_value = (
            "https://put.example/signed"
        )
        with mock.patch.object(cos, "enabled", return_value=True), mock.patch.object(
            cos, "_BUCKET", "bucket-123"
        ):
            url = cos.create_presigned_put(
                "edit-input/fang/a.mp4", "video/mp4", expires=900
            )
        self.assertEqual("https://put.example/signed", url)
        kwargs = client.return_value.get_presigned_url.call_args.kwargs
        self.assertEqual("PUT", kwargs["Method"])
        self.assertEqual(900, kwargs["Expired"])
        self.assertEqual("video/mp4", kwargs["Headers"]["Content-Type"])
        self.assertEqual("edit-input/fang/a.mp4", kwargs["Key"])

    @mock.patch.object(cos, "_client")
    def test_head_and_media_info_use_scoped_cos_object_key(self, client):
        client.return_value.head_object.return_value = {
            "Content-Length": "120",
            "Content-Type": "video/mp4",
        }
        client.return_value.get_media_info.return_value = {
            "MediaInfo": {"Format": {"Duration": "20.0"}}
        }
        with mock.patch.object(cos, "enabled", return_value=True), mock.patch.object(
            cos, "_BUCKET", "bucket-123"
        ), mock.patch.object(cos, "_PREFIX", "hq"):
            metadata = cos.head("edit-output/fang/job.mp4")
            info = cos.get_media_info("edit-output/fang/job.mp4")
        self.assertEqual(120, metadata["size_bytes"])
        self.assertEqual("video/mp4", metadata["content_type"])
        self.assertEqual("20.0", info["Format"]["Duration"])
        self.assertEqual(
            "hq/edit-output/fang/job.mp4",
            client.return_value.get_media_info.call_args.kwargs["Key"],
        )

    def test_transfer_remote_streams_to_file_and_always_removes_it(self):
        response = _RemoteResponse([b"abc", b"def", b""])
        with mock.patch.object(
            cos.urllib.request, "urlopen", return_value=response
        ), mock.patch.object(cos, "put_file", return_value="https://cos.example/out") as put:
            result = cos.transfer_remote(
                "https://provider.example/out.mp4?signature=secret",
                "edit-output/fang/out.mp4",
                max_bytes=10,
            )
        self.assertEqual("https://cos.example/out", result)
        uploaded_path = pathlib.Path(put.call_args.args[0])
        self.assertFalse(uploaded_path.exists())
        self.assertEqual("edit-output/fang/out.mp4", put.call_args.args[1])
        self.assertEqual("video/mp4", put.call_args.args[2])

    def test_transfer_remote_rejects_insecure_or_oversized_content(self):
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            cos.transfer_remote("http://provider.example/out.mp4", "out.mp4", 10)
        response = _RemoteResponse([b"123456", b"789012", b""])
        with mock.patch.object(cos.urllib.request, "urlopen", return_value=response):
            with self.assertRaisesRegex(ValueError, "大小"):
                cos.transfer_remote(
                    "https://provider.example/out.mp4", "out.mp4", max_bytes=10
                )


class AiEditMaterialResolverTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = pathlib.Path(self.tmp.name) / "ai_edit.db"
        self.env = mock.patch.dict(os.environ, {"AI_EDIT_DB": str(self.db)})
        self.env.start()
        ai_edit_store.create_edit_job(
            self.db, 10, "fang", "product_story", "shotstack", 30
        )

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_vtt_uses_word_timestamps(self):
        vtt = ai_edit_assets.words_to_vtt(
            [
                {"begin_time": 100, "end_time": 500, "text": "你好"},
                {"begin_time": 500, "end_time": 900, "text": "黄雀"},
            ]
        )
        self.assertTrue(vtt.startswith("WEBVTT\n"))
        self.assertIn("00:00:00.100 --> 00:00:00.900", vtt)
        self.assertIn("你好黄雀", vtt)

    @mock.patch("content_domains.ai_edit_assets._generate_image")
    def test_user_material_wins_over_generation(self, generate):
        plan = {
            "material_requests": [
                {"role": "product", "kind": "image", "prompt": "产品展示"}
            ]
        }
        result = ai_edit_assets.resolve_materials(
            10,
            "fang",
            plan,
            [
                {
                    "id": "mine",
                    "role": "product",
                    "kind": "image",
                    "origin": "uploaded",
                    "cos_key": "edit-input/fang/product.png",
                }
            ],
            lambda _stage: None,
        )
        self.assertEqual("uploaded", result["product"]["origin"])
        generate.assert_not_called()

    @mock.patch("content_domains.ai_edit_assets._generate_image")
    def test_generates_only_missing_roles_and_persists_material(self, generate):
        generate.return_value = {
            "url": "https://cos.example/generated.png",
            "cos_key": "edit/fang/10/generated/scene.png",
            "content_type": "image/png",
            "size_bytes": 321,
        }
        heartbeat = mock.Mock()
        result = ai_edit_assets.resolve_materials(
            10,
            "fang",
            {
                "material_requests": [
                    {"role": "scene", "kind": "image", "prompt": "城市夜景"}
                ]
            },
            [],
            heartbeat,
        )
        self.assertEqual(1, generate.call_count)
        self.assertEqual("generated", result["scene"]["origin"])
        heartbeat.assert_called_once_with("generating_assets")
        with closing(sqlite3.connect(self.db)) as connection:
            row = connection.execute(
                "SELECT origin,status,role FROM edit_materials"
            ).fetchone()
        self.assertEqual(("generated", "ready", "scene"), row)

    @mock.patch("content_domains.ai_edit_assets._generate_image")
    def test_rejects_more_than_eight_generated_images(self, generate):
        requests = [
            {"role": "role-%d" % index, "kind": "image", "prompt": "画面"}
            for index in range(9)
        ]
        with self.assertRaisesRegex(ValueError, "8"):
            ai_edit_assets.resolve_materials(
                10, "fang", {"material_requests": requests}, [], lambda _stage: None
            )
        generate.assert_not_called()

    @mock.patch("content_domains.ai_edit_assets.cos.put_bytes")
    def test_uploads_word_timed_vtt_without_exposing_it_as_material_role(self, put_bytes):
        put_bytes.return_value = "https://cos.example/captions.vtt"
        result = ai_edit_assets.resolve_materials(
            10,
            "fang",
            {
                "material_requests": [],
                "_words": [
                    {"begin_time": 0, "end_time": 500, "text": "黄雀"}
                ],
            },
            [],
            lambda _stage: None,
        )
        self.assertEqual(
            "edit/fang/10/captions.vtt", result["_captions"]["cos_key"]
        )
        self.assertIn(b"WEBVTT", put_bytes.call_args.args[0])
        self.assertTrue(put_bytes.call_args.kwargs["private"])


if __name__ == "__main__":
    unittest.main()
