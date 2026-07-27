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

from content_domains import audio, cos, video


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


if __name__ == "__main__":
    unittest.main()
