from __future__ import annotations

import hashlib
from pathlib import Path
import tempfile
import unittest

from server.content_domains.ai_edit_v3.delivery import stage_private_delivery
from server.content_domains.ai_edit_v3.media import FinalMux


class _Cos:
    def __init__(self):
        self.environment = "test"
        self.objects = {}
        self.puts = []
        self.ranges = []

    def put_file(self, path, key, content_type, *, private, if_none_match):
        body = Path(path).read_bytes()
        self.puts.append((key, content_type, private, if_none_match))
        existing = self.objects.get(key)
        if existing is not None and existing != body:
            raise RuntimeError("object exists")
        self.objects[key] = body
        return {"etag": '"etag-1"'}

    def head_object(self, key):
        body = self.objects[key]
        return {"size_bytes": len(body), "sha256": hashlib.sha256(body).hexdigest(), "etag": '"etag-1"', "private": True}

    def presign_get(self, key, *, expires):
        self.assert_key = key
        self.assert_expires = expires
        return "https://signed.invalid/private?secret=redacted"

    def range_get(self, url, *, range_header):
        self.ranges.append((url, range_header))
        body = self.objects[self.assert_key]
        return {"status": 206, "headers": {"Content-Range": f"bytes 0-0/{len(body)}"}, "body": body[:1]}


class PrivateDeliveryTests(unittest.TestCase):
    def test_stages_immutable_private_object_and_verifies_one_byte_range(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "final.mp4"
            path.write_bytes(b"final-video")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            mux = FinalMux(path.name, digest, 1000, "h264", "aac", 1920, 1080, 30, 1, 48000, 2, {})
            cos = _Cos()

            result = stage_private_delivery("owner", "a" * 24, "job-1", 1, mux, environment="test", cos=cos, source_path=path)

            self.assertEqual(result.object_key, f"test/ai-edit-v3/{'a' * 24}/job-1/delivery/1-{digest}.mp4")
            self.assertEqual(cos.puts, [(result.object_key, "video/mp4", True, "*")])
            self.assertEqual(cos.assert_expires, 300)
            self.assertEqual(cos.ranges[0][1], "bytes=0-0")
            self.assertEqual((result.range_status, result.content_range), (206, f"bytes 0-0/{path.stat().st_size}"))

    def test_environment_and_content_identity_fail_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "final.mp4"
            path.write_bytes(b"final-video")
            mux = FinalMux(path.name, "0" * 64, 1000, "h264", "aac", 1920, 1080, 30, 1, 48000, 2, {})
            with self.assertRaisesRegex(ValueError, "delivery_content_hash_mismatch"):
                stage_private_delivery("owner", "a" * 24, "job-1", 1, mux, environment="test", cos=_Cos(), source_path=path)
            with self.assertRaisesRegex(ValueError, "delivery_environment_invalid"):
                stage_private_delivery("owner", "a" * 24, "job-1", 1, mux, environment="production", cos=_Cos(), source_path=path)


if __name__ == "__main__":
    unittest.main()
