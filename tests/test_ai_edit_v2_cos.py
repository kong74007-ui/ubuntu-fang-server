import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server.content_domains import cos


VALID_KEY = (
    "ai-edit-v2/0123456789abcdef/"
    "123e4567-e89b-12d3-a456-426614174000/source/main.mp4"
)


class FakeCosClient:
    def __init__(self):
        self.calls = []

    def get_presigned_url(self, **kwargs):
        self.calls.append(("presign", kwargs))
        return "https://signed.example/private-put"

    def head_object(self, **kwargs):
        self.calls.append(("head", kwargs))
        return {
            "Content-Length": "12345",
            "Content-Type": "video/mp4",
            "ETag": '"abc123"',
        }

    def download_file(self, **kwargs):
        self.calls.append(("download", kwargs))
        with open(kwargs["DestFilePath"], "wb") as destination:
            destination.write(b"private video")
        return {"ETag": '"abc123"'}

    def delete_object(self, **kwargs):
        self.calls.append(("delete", kwargs))
        return {"status": 204}


class V2CosTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeCosClient()
        self.patches = (
            patch.object(cos, "enabled", return_value=True),
            patch.object(cos, "_client", return_value=self.client),
            patch.object(cos, "_BUCKET", "private-bucket-123"),
            patch.object(cos, "_PREFIX", "huangque"),
        )
        for active_patch in self.patches:
            active_patch.start()

    def tearDown(self):
        for active_patch in reversed(self.patches):
            active_patch.stop()

    def test_presign_put_is_private_scoped_and_never_exceeds_fifteen_minutes(self):
        url = cos.presign_put(VALID_KEY, "video/mp4", expires=900)

        self.assertEqual(url, "https://signed.example/private-put")
        operation, request = self.client.calls[-1]
        self.assertEqual(operation, "presign")
        self.assertEqual(request["Method"], "PUT")
        self.assertEqual(request["Bucket"], "private-bucket-123")
        self.assertEqual(request["Key"], "huangque/" + VALID_KEY)
        self.assertEqual(request["Expired"], 900)
        self.assertEqual(request["Headers"], {"Content-Type": "video/mp4"})

        with self.assertRaisesRegex(ValueError, "900"):
            cos.presign_put(VALID_KEY, "video/mp4", expires=901)

    def test_head_object_returns_normalized_private_metadata(self):
        metadata = cos.head_object(VALID_KEY)

        self.assertEqual(
            metadata,
            {
                "content_length": 12345,
                "content_type": "video/mp4",
                "etag": "abc123",
            },
        )
        self.assertNotIn("url", metadata)
        self.assertEqual(
            self.client.calls[-1],
            (
                "head",
                {"Bucket": "private-bucket-123", "Key": "huangque/" + VALID_KEY},
            ),
        )

    def test_download_and_delete_use_the_private_object_key(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = os.path.join(temp_dir, "source.mp4")
            result = cos.download_file(VALID_KEY, destination)
            self.assertEqual(Path(destination).read_bytes(), b"private video")

        self.assertEqual(result, destination)
        self.assertEqual(
            self.client.calls[-1][0], "download"
        )
        deleted = cos.delete_object(VALID_KEY)
        self.assertEqual(deleted, {"status": 204})
        self.assertEqual(
            self.client.calls[-1],
            (
                "delete",
                {"Bucket": "private-bucket-123", "Key": "huangque/" + VALID_KEY},
            ),
        )

    def test_rejects_paths_outside_the_v2_owner_and_task_scope(self):
        invalid_keys = (
            "../ai-edit-v2/0123456789abcdef/123e4567-e89b-12d3-a456-426614174000/a.mp4",
            "/ai-edit-v2/0123456789abcdef/123e4567-e89b-12d3-a456-426614174000/a.mp4",
            "C:/ai-edit-v2/0123456789abcdef/123e4567-e89b-12d3-a456-426614174000/a.mp4",
            VALID_KEY + "?token=secret",
            "video/public.mp4",
            "ai-edit-v2/not-a-hash/123e4567-e89b-12d3-a456-426614174000/a.mp4",
            "ai-edit-v2/0123456789abcdef/not-a-uuid/a.mp4",
        )

        for rel_key in invalid_keys:
            with self.subTest(rel_key=rel_key), self.assertRaises(ValueError):
                cos.head_object(rel_key)

        self.assertEqual(self.client.calls, [])


if __name__ == "__main__":
    unittest.main()
