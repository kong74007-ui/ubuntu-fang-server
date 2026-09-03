from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from server import material_library_api as api


build_server = api.build_server


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
        self.usage_path = self.root / "state" / "usage.json"
        self.usage_path.parent.mkdir()
        self.server = build_server(
            "127.0.0.1", 0, self.root, "test-token",
            usage_path=self.usage_path,
        )
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
        self.assertEqual("development", payload["build_id"])
        self.assertTrue(payload["usage_state_ready"])
        self.assertNotIn(str(self.root), json.dumps(payload))

    def test_authenticated_ping_validates_the_runtime_token(self):
        with self.assertRaises(urllib.error.HTTPError) as denied:
            self.request("/v1/ping")
        self.assertEqual(401, denied.exception.code)
        with self.request("/v1/ping", token="test-token") as response:
            payload = json.load(response)
        self.assertTrue(payload["ok"])
        self.assertEqual(1, payload["records"])

    def test_select_requires_bearer_token_and_hides_server_path(self):
        body = {
            "scenes": [{"scene_id": "s1", "query": "医美 抗衰", "media_type": "image"}],
            "orientation": "portrait",
            "selection_mode": "random",
        }
        with self.assertRaises(urllib.error.HTTPError) as denied:
            self.request("/v1/select", method="POST", payload=body)
        self.assertEqual(401, denied.exception.code)

        with self.request("/v1/select", method="POST", payload=body, token="test-token") as response:
            payload = json.load(response)
        self.assertEqual(self.sha, payload["materials"][0]["sha256"])
        self.assertEqual("random", payload["selection_mode"])
        serialized = json.dumps(payload)
        self.assertNotIn("relative_path", serialized)
        self.assertNotIn(str(self.root), serialized)

    def test_build_server_accepts_a_separate_usage_state_path(self):
        usage_path = self.root / "state" / "usage.json"
        usage_path.parent.mkdir(exist_ok=True)
        server = build_server(
            "127.0.0.1", 0, self.root, "test-token",
            usage_path=usage_path,
        )
        try:
            self.assertEqual(usage_path.resolve(), server.library._usage_path)
            self.assertNotEqual(self.root.resolve(), usage_path.parent.resolve())
        finally:
            server.server_close()

    def test_round_robin_http_selection_persists_before_success(self):
        body = {
            "scenes": [{"scene_id": "s1", "media_type": "image"}],
            "orientation": "portrait",
            "selection_mode": "round_robin",
            "seed": "http-round-robin",
        }

        with self.request(
            "/v1/select", method="POST", payload=body, token="test-token",
        ) as response:
            payload = json.load(response)

        self.assertEqual("round_robin", payload["selection_mode"])
        self.assertEqual(
            ["round_robin_all_orientations_unique"],
            payload["fallback_policy"],
        )
        usage = json.loads(self.usage_path.read_text(encoding="utf-8"))
        self.assertEqual(1, usage[self.sha]["count"])

    def test_select_rejects_non_object_root_and_scene_entries(self):
        for payload in ([{"scene_id": "bad"}], {"scenes": ["bad"]}):
            with self.subTest(payload=payload), self.assertRaises(urllib.error.HTTPError) as rejected:
                self.request("/v1/select", method="POST", payload=payload, token="test-token")
            self.assertEqual(400, rejected.exception.code)
        with self.assertRaises(urllib.error.HTTPError) as rejected:
            self.request(
                "/v1/select", method="POST", token="test-token",
                payload={
                    "scenes": [{"scene_id": "s1"}],
                    "selection_mode": "weighted",
                },
            )
        self.assertEqual(400, rejected.exception.code)

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

    def test_atomic_path_replacement_after_validation_streams_only_snapshot(self):
        asset = self.root / "files" / "approved.jpg"
        replacement = self.root / "files" / "replacement.jpg"
        replacement.write_bytes(b"replacement-bytes")
        real_snapshot = api.verified_asset_snapshot

        @api.contextlib.contextmanager
        def replace_after_snapshot(path, expected_sha256):
            with real_snapshot(path, expected_sha256) as verified:
                os.replace(replacement, asset)
                yield verified

        with mock.patch.object(api, "verified_asset_snapshot", replace_after_snapshot):
            with self.request(f"/v1/assets/{self.sha}", token="test-token") as response:
                returned = response.read()
        self.assertEqual(b"approved-image", returned)
        self.assertEqual(b"replacement-bytes", asset.read_bytes())

    def test_source_mutation_during_snapshot_never_returns_http_200(self):
        asset = (self.root / "files" / "approved.jpg").resolve()
        original_open = Path.open

        class MutatingReader:
            def __init__(self, raw):
                self.raw = raw
                self.changed = False

            def __enter__(self):
                return self

            def __exit__(self, *_):
                self.raw.close()

            def read(self, size=-1):
                chunk = self.raw.read(size)
                if chunk and not self.changed:
                    self.changed = True
                    with original_open(asset, "r+b", buffering=0) as writer:
                        writer.seek(len(chunk))
                        writer.write(b"X" * max(0, asset.stat().st_size - len(chunk)))
                return chunk

        def controlled_open(path, mode="r", buffering=-1, encoding=None, errors=None, newline=None):
            if path.resolve() == asset and mode == "rb":
                return MutatingReader(original_open(path, mode, buffering=0))
            return original_open(path, mode, buffering, encoding, errors, newline)

        with mock.patch.object(api, "SNAPSHOT_CHUNK_BYTES", 4), \
             mock.patch.object(Path, "open", controlled_open), \
             self.assertRaises(urllib.error.HTTPError) as rejected:
            self.request(f"/v1/assets/{self.sha}", token="test-token")
        self.assertEqual(409, rejected.exception.code)


if __name__ == "__main__":
    unittest.main()
