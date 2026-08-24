from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from server.material_library_api import build_server


class MaterialLibraryApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "files").mkdir()
        payload = b"approved-image"
        self.sha = hashlib.sha256(payload).hexdigest()
        (self.root / "files" / "approved.jpg").write_bytes(payload)
        row = {
            "record_id": "asset-1",
            "sha256": self.sha,
            "素材名称": "approved",
            "状态": "可使用",
            "画面方向": "竖屏",
            "标签": ["医美", "抗衰"],
            "server_relative_path": "files/approved.jpg",
        }
        (self.root / "index.jsonl").write_text(
            json.dumps(row, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        self.server = build_server("127.0.0.1", 0, self.root, "test-token")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def request(self, path, *, method="GET", payload=None, token=None):
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(self.base + path, data=data, method=method)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        if token:
            request.add_header("Authorization", f"Bearer {token}")
        return urllib.request.urlopen(request, timeout=3)

    def test_health_is_public_but_does_not_expose_paths(self):
        with self.request("/health") as response:
            payload = json.load(response)
        self.assertTrue(payload["ok"])
        self.assertEqual(1, payload["records"])
        self.assertNotIn(str(self.root), json.dumps(payload))

    def test_select_requires_bearer_token_and_hides_server_path(self):
        body = {
            "scenes": [{"scene_id": "s1", "query": "医美 抗衰", "media_type": "image"}],
            "orientation": "portrait",
        }
        with self.assertRaises(urllib.error.HTTPError) as denied:
            self.request("/v1/select", method="POST", payload=body)
        self.assertEqual(401, denied.exception.code)

        with self.request("/v1/select", method="POST", payload=body, token="test-token") as response:
            payload = json.load(response)
        self.assertEqual(self.sha, payload["materials"][0]["sha256"])
        serialized = json.dumps(payload)
        self.assertNotIn("relative_path", serialized)
        self.assertNotIn(str(self.root), serialized)

    def test_asset_download_requires_token_and_valid_checksum(self):
        with self.assertRaises(urllib.error.HTTPError) as denied:
            self.request(f"/v1/assets/{self.sha}")
        self.assertEqual(401, denied.exception.code)

        with self.request(f"/v1/assets/{self.sha}", token="test-token") as response:
            self.assertEqual(b"approved-image", response.read())
            self.assertEqual("image/jpeg", response.headers.get_content_type())

        (self.root / "files" / "approved.jpg").write_bytes(b"tampered")
        with self.assertRaises(urllib.error.HTTPError) as corrupted:
            self.request(f"/v1/assets/{self.sha}", token="test-token")
        self.assertEqual(409, corrupted.exception.code)


if __name__ == "__main__":
    unittest.main()
