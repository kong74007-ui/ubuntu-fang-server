import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from content_domains import ai_edit_v2_cos
from content_domains import cos
from content_domains import video


class AiEditV2AssetLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / "assets.db")
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("""CREATE TABLE video_assets(
                id INTEGER PRIMARY KEY,job_id TEXT,username TEXT,mode TEXT,
                image_file TEXT,audio_file TEXT,reference_video_file TEXT,
                video_file TEXT,video_url TEXT,text TEXT,voice_key TEXT,
                resolution TEXT,ratio TEXT,motion TEXT,phase TEXT,
                image_asset_id TEXT,audio_asset_id TEXT,reference_asset_id TEXT,
                provider_video_id TEXT,provider_avatar_id TEXT,
                provider_avatar_group_id TEXT,source_video_url TEXT,
                background_file TEXT,tryon_mode TEXT,model TEXT,status TEXT,
                error TEXT,created_at INTEGER,updated_at INTEGER)""")
            conn.execute(
                """INSERT INTO video_assets(
                    id,job_id,username,mode,video_file,video_url,resolution,
                    ratio,phase,status,created_at,updated_at
                ) VALUES(1,?,'alice','ai_edit_v2',?,NULL,'1080p','9:16',
                         'completed','done',1,1)""",
                (
                    "57dd0813-12f0-4ab2-b56e-ebd96d8b5b13",
                    "ai-edit-v2/0123456789abcdef/57dd0813-12f0-4ab2-b56e-ebd96d8b5b13/delivery/final.mp4",
                ),
            )
            conn.commit()

    def tearDown(self):
        self.temp.cleanup()

    def test_completed_v2_asset_gets_fresh_private_playback_url(self):
        def connect_assets():
            conn = sqlite3.connect(self.db)
            conn.row_factory = sqlite3.Row
            return conn

        with patch.object(video, "adb", side_effect=connect_assets), \
                patch.object(video, "jdb", side_effect=sqlite3.OperationalError), \
                patch.object(ai_edit_v2_cos, "presign_get", return_value="https://signed.example/final.mp4") as sign, \
                patch.object(cos, "enabled", return_value=True), \
                patch.object(cos, "object_url", return_value="https://wrong-bucket.example/final.mp4") as legacy_sign:
            items = video.list_video_assets("alice")

        self.assertEqual(items[0]["video_url"], "https://signed.example/final.mp4")
        sign.assert_called_once_with(
            "ai-edit-v2/0123456789abcdef/57dd0813-12f0-4ab2-b56e-ebd96d8b5b13/delivery/final.mp4",
            expires=300,
        )
        legacy_sign.assert_not_called()

    def test_v2_asset_replaces_stale_url_instead_of_reusing_it(self):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute(
                "UPDATE video_assets SET video_url='https://expired.example/final.mp4' WHERE id=1"
            )
            conn.commit()

        def connect_assets():
            conn = sqlite3.connect(self.db)
            conn.row_factory = sqlite3.Row
            return conn

        with patch.object(video, "adb", side_effect=connect_assets), \
                patch.object(video, "jdb", side_effect=sqlite3.OperationalError), \
                patch.object(ai_edit_v2_cos, "presign_get", return_value="https://fresh.example/final.mp4"):
            items = video.list_video_assets("alice")

        self.assertEqual(items[0]["video_url"], "https://fresh.example/final.mp4")

    def test_completed_v3_asset_gets_fresh_private_playback_url(self):
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute(
                """INSERT INTO video_assets(
                    id,job_id,username,mode,video_file,video_url,resolution,
                    ratio,phase,status,created_at,updated_at
                ) VALUES(2,NULL,'alice','ai_edit_v3',?,NULL,'1080p','9:16',
                         'completed','done',2,2)""",
                ("test/ai-edit-v3/owner/job/delivery/final.mp4",),
            )
            conn.commit()

        def connect_assets():
            conn = sqlite3.connect(self.db)
            conn.row_factory = sqlite3.Row
            return conn

        signer = unittest.mock.Mock()
        signer.presign_get.return_value = "https://signed.example/v3-final.mp4"
        with patch.object(video, "adb", side_effect=connect_assets), \
                patch.object(video, "jdb", side_effect=sqlite3.OperationalError), \
                patch("content_domains.ai_edit_v3.cos.V3Cos", return_value=signer) as factory:
            items = video.list_video_assets("alice")

        self.assertEqual(items[0]["video_url"], "https://signed.example/v3-final.mp4")
        factory.assert_called_once_with(environment="test")
        signer.presign_get.assert_called_once_with(
            "test/ai-edit-v3/owner/job/delivery/final.mp4", expires=300
        )


if __name__ == "__main__":
    unittest.main()
