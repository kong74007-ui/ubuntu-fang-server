from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
except ModuleNotFoundError:
    FastAPI = None
    TestClient = None


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "deploy" / "pixelle-video" / "overrides" / "api" / "external_audio.py"
ROUTER_PATH = ROOT / "deploy" / "pixelle-video" / "overrides" / "api" / "routers" / "voice_assets.py"
MP3 = b"ID3\x04\x00\x00\x00\x00\x00\x00" + (b"audio" * 20)


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class ExternalAudioRegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.module = load_module("pixelle_external_audio_test", MODULE_PATH)
        self.module.EXTERNAL_AUDIO_ROOT = self.root
        self.probe = patch.object(
            self.module.subprocess,
            "run",
            return_value=Mock(returncode=0, stdout="3.25\n", stderr=""),
        )
        self.probe.start()

    def tearDown(self):
        self.probe.stop()
        sys.modules.pop("pixelle_external_audio_test", None)
        self.tmp.cleanup()

    def store(self, request_id="request-1"):
        return self.module.store_audio_asset(MP3, "audio/mpeg", request_id)

    def test_store_writes_private_opaque_mp3_and_reports_duration(self):
        record = self.store()
        self.assertRegex(record["asset_id"], r"^audio_[0-9a-f]{32}$")
        self.assertEqual("audio/mpeg", record["content_type"])
        self.assertEqual(len(MP3), record["size"])
        self.assertEqual(3.25, record["duration"])
        path = self.root / f'{record["asset_id"]}.mp3'
        self.assertEqual(MP3, path.read_bytes())
        if os.name != "nt":
            self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))

    def test_store_rejects_invalid_uploads_and_probe_failure(self):
        invalid = [
            (b"", "audio/mpeg", "request-1"),
            (b"not-mp3", "audio/mpeg", "request-1"),
            (MP3, "audio/wav", "request-1"),
            (MP3, "audio/mpeg", "../bad"),
        ]
        for content, content_type, request_id in invalid:
            with self.subTest(content_type=content_type, request_id=request_id), self.assertRaises(ValueError):
                self.module.store_audio_asset(content, content_type, request_id)
        with patch.object(self.module, "MAX_AUDIO_BYTES", len(MP3) - 1), self.assertRaises(self.module.AudioTooLargeError):
            self.store()
        with patch.object(self.module.subprocess, "run", return_value=Mock(returncode=1, stdout="", stderr="bad")), \
             self.assertRaises(self.module.AudioProbeError):
            self.store("request-2")

    def test_resolve_rejects_path_missing_and_expired_assets(self):
        record = self.store()
        self.assertEqual(self.root / f'{record["asset_id"]}.mp3', self.module.resolve_audio_asset(record["asset_id"]))
        for asset_id in ("../secret", "audio_bad", "audio_" + "f" * 32):
            with self.subTest(asset_id=asset_id), self.assertRaises((ValueError, FileNotFoundError)):
                self.module.resolve_audio_asset(asset_id)
        metadata = self.root / f'{record["asset_id"]}.json'
        payload = json.loads(metadata.read_text(encoding="utf-8"))
        payload["created_at"] = 1
        metadata.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(FileNotFoundError):
            self.module.resolve_audio_asset(record["asset_id"])

    def test_cleanup_only_removes_expired_assets(self):
        old = self.store("request-old")
        fresh = self.store("request-fresh")
        old_meta = self.root / f'{old["asset_id"]}.json'
        payload = json.loads(old_meta.read_text(encoding="utf-8"))
        payload["created_at"] = 100
        old_meta.write_text(json.dumps(payload), encoding="utf-8")
        self.assertEqual(1, self.module.cleanup_expired_audio_assets(now=100 + self.module.AUDIO_TTL_SECONDS + 1))
        self.assertFalse((self.root / f'{old["asset_id"]}.mp3').exists())
        self.assertTrue((self.root / f'{fresh["asset_id"]}.mp3').exists())

    def test_lease_is_exclusive_atomic_and_release_is_idempotent(self):
        first = self.store("request-first")
        second = self.store("request-second")
        paths = self.module.lease_audio_assets([first["asset_id"], second["asset_id"]], "task-1")
        self.assertEqual(2, len(paths))
        with self.assertRaises(self.module.AudioLeaseError):
            self.module.lease_audio_assets([first["asset_id"]], "task-2")
        self.assertEqual(2, self.module.release_audio_assets([first["asset_id"], second["asset_id"]]))
        self.assertEqual(0, self.module.release_audio_assets([first["asset_id"], second["asset_id"]]))
        self.assertFalse(paths[0].exists())

    def test_public_catalog_is_sanitized(self):
        voices = types.ModuleType("pixelle_video.tts_voices")
        voices.EDGE_TTS_VOICES = [{
            "id": "zh-CN-TestNeural", "name": "测试音色", "gender": "female",
            "locale": "zh-CN", "workflow": "/secret/workflow.json", "token": "secret",
        }]
        package = types.ModuleType("pixelle_video")
        with patch.dict(sys.modules, {"pixelle_video": package, "pixelle_video.tts_voices": voices}):
            self.assertEqual([{
                "id": "zh-CN-TestNeural", "name": "测试音色", "gender": "female", "locale": "zh-CN",
            }], self.module.public_voice_catalog())


class VoiceAssetRouterSourceTests(unittest.TestCase):
    def test_router_contract_is_present_without_requiring_deployment_dependencies(self):
        source = ROUTER_PATH.read_text(encoding="utf-8")
        self.assertIn('@router.get("/voices/public")', source)
        self.assertIn('@router.post("/audio-assets"', source)
        self.assertIn('alias="X-Request-Id"', source)
        self.assertIn('status_code=status.HTTP_201_CREATED', source)
        self.assertIn('external_audio.store_audio_asset', source)


@unittest.skipIf(FastAPI is None or TestClient is None, "FastAPI deployment dependency is not installed")
class VoiceAssetRouterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        api_package = types.ModuleType("api")
        api_package.__path__ = []
        self.external = load_module("api.external_audio", MODULE_PATH)
        self.external.EXTERNAL_AUDIO_ROOT = self.root
        api_package.external_audio = self.external
        self.modules_patch = patch.dict(sys.modules, {"api": api_package, "api.external_audio": self.external})
        self.modules_patch.start()
        self.router_module = load_module("pixelle_voice_assets_test", ROUTER_PATH)
        app = FastAPI()
        app.include_router(self.router_module.router, prefix="/api")
        self.client = TestClient(app)

    def tearDown(self):
        self.modules_patch.stop()
        sys.modules.pop("pixelle_voice_assets_test", None)
        sys.modules.pop("api.external_audio", None)
        self.tmp.cleanup()

    def test_upload_contract_and_errors(self):
        with patch.object(self.external.subprocess, "run", return_value=Mock(returncode=0, stdout="2.5\n", stderr="")):
            response = self.client.post(
                "/api/audio-assets", content=MP3,
                headers={"Content-Type": "audio/mpeg", "X-Request-Id": "request-1"},
            )
        self.assertEqual(201, response.status_code)
        self.assertEqual({"asset_id", "content_type", "size", "duration"}, set(response.json()))
        self.assertEqual(400, self.client.post("/api/audio-assets", content=MP3, headers={"Content-Type": "audio/mpeg"}).status_code)
        self.assertEqual(400, self.client.post("/api/audio-assets", content=b"bad", headers={"Content-Type": "audio/mpeg", "X-Request-Id": "r2"}).status_code)
        with patch.object(self.external, "MAX_AUDIO_BYTES", 1):
            self.assertEqual(413, self.client.post("/api/audio-assets", content=MP3, headers={"Content-Type": "audio/mpeg", "X-Request-Id": "r3"}).status_code)

    def test_public_voice_response_shape(self):
        with patch.object(self.external, "public_voice_catalog", return_value=[{"id": "v", "name": "n", "gender": "male", "locale": "zh-CN"}]):
            response = self.client.get("/api/voices/public")
        self.assertEqual(200, response.status_code)
        self.assertEqual({"items": [{"id": "v", "name": "n", "gender": "male", "locale": "zh-CN"}]}, response.json())


if __name__ == "__main__":
    unittest.main()
