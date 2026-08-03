import tempfile
import unittest
from pathlib import Path


class _AmbiguousCosClient:
    def __init__(self, *, stored: bytes | None = None) -> None:
        self.stored = stored
        self.content_type = "video/mp4"
        self.put_calls = 0

    def head_object(self, **kwargs):
        if self.stored is None:
            raise FileNotFoundError("missing")
        return {
            "Content-Length": len(self.stored),
            "Content-Type": self.content_type,
            "ETag": '"verified-etag"',
        }

    def put_object(self, **kwargs):
        self.put_calls += 1
        self.stored = kwargs["Body"].read()
        self.content_type = kwargs["ContentType"]
        raise TimeoutError("provider response lost after acceptance")

    def download_file(self, **kwargs):
        Path(kwargs["DestFilePath"]).write_bytes(self.stored)


class V3CosImmutableUploadTests(unittest.TestCase):
    @staticmethod
    def _cos(client):
        from server.content_domains.ai_edit_v3.cos import V3Cos

        cos = V3Cos(environment="test")
        cos._bucket = "bucket"
        cos._prefix = ""
        cos._client_instance = client
        return cos

    def test_immutable_upload_reconciles_accepted_timeout_by_full_sha256(self):
        payload = b"verified-final-video"
        client = _AmbiguousCosClient()
        cos = self._cos(client)

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "final.mp4"
            source.write_bytes(payload)
            result = cos.put_file(
                source,
                "test/ai-edit-v3/owner/job/delivery/final.mp4",
                "video/mp4",
                private=True,
                if_none_match="*",
            )

        self.assertEqual(1, client.put_calls)
        self.assertEqual(len(payload), result["content_length"])
        self.assertEqual("verified-etag", result["etag"])

    def test_immutable_existing_object_with_different_sha256_fails_closed(self):
        client = _AmbiguousCosClient(stored=b"different-object")
        cos = self._cos(client)

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "final.mp4"
            source.write_bytes(b"expected-object")
            with self.assertRaisesRegex(RuntimeError, "cos_immutable_object_conflict"):
                cos.put_file(
                    source,
                    "test/ai-edit-v3/owner/job/delivery/final.mp4",
                    "video/mp4",
                    private=True,
                    if_none_match="*",
                )

        self.assertEqual(0, client.put_calls)


if __name__ == "__main__":
    unittest.main()
