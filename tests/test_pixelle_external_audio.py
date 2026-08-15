from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import stat
import sys
import tempfile
import time
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

    def test_cleanup_preserves_expired_leased_assets(self):
        leased = self.store("request-leased")
        abandoned = self.store("request-abandoned")
        self.module.lease_audio_assets([leased["asset_id"]], "task-running")
        for record in (leased, abandoned):
            metadata = self.root / f'{record["asset_id"]}.json'
            payload = json.loads(metadata.read_text(encoding="utf-8"))
            payload["created_at"] = 100
            metadata.write_text(json.dumps(payload), encoding="utf-8")

        removed = self.module.cleanup_expired_audio_assets(
            now=100 + self.module.AUDIO_TTL_SECONDS + 1
        )

        self.assertEqual(1, removed)
        self.assertTrue((self.root / f'{leased["asset_id"]}.mp3').exists())
        self.assertFalse((self.root / f'{abandoned["asset_id"]}.mp3').exists())

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

    def test_nested_cues_are_leased_in_scene_and_cue_order(self):
        first = self.store("nested-first")
        second = self.store("nested-second")
        segments = [
            {
                "text": "第一句，第二句。",
                "cues": [
                    {"text": "第一句，", "audio_asset_id": first["asset_id"]},
                    {"text": "第二句。", "audio_asset_id": second["asset_id"]},
                ],
            }
        ]

        asset_ids, resolved = self.module.lease_narration_segments(segments, "task-nested")

        self.assertEqual([first["asset_id"], second["asset_id"]], asset_ids)
        self.assertEqual("第一句，第二句。", resolved[0]["text"])
        self.assertEqual(["第一句，", "第二句。"], [cue["text"] for cue in resolved[0]["cues"]])
        self.assertEqual(
            [
                str(self.root / f'{first["asset_id"]}.mp3'),
                str(self.root / f'{second["asset_id"]}.mp3'),
            ],
            [cue["audio_path"] for cue in resolved[0]["cues"]],
        )

    def test_legacy_audio_asset_becomes_one_resolved_cue(self):
        record = self.store("legacy-scene")
        asset_ids, resolved = self.module.lease_narration_segments(
            [{"text": "短旁白", "audio_asset_id": record["asset_id"]}],
            "task-legacy",
        )

        self.assertEqual([record["asset_id"]], asset_ids)
        self.assertEqual(
            [{"text": "短旁白", "audio_path": str(self.root / f'{record["asset_id"]}.mp3')}],
            resolved[0]["cues"],
        )

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


class ExternalAudioAsyncLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.module = load_module("pixelle_external_audio_async_test", MODULE_PATH)
        self.module.EXTERNAL_AUDIO_ROOT = self.root
        self.probe = patch.object(
            self.module.subprocess,
            "run",
            return_value=Mock(returncode=0, stdout="3.25\n", stderr=""),
        )
        self.probe.start()

    async def asyncTearDown(self):
        await self.module.stop_cleanup_scheduler()
        self.probe.stop()
        sys.modules.pop("pixelle_external_audio_async_test", None)
        self.tmp.cleanup()

    async def test_prepare_failure_releases_capacity_before_task_creation(self):
        class Reservation:
            released = False

            async def release(self):
                self.released = True

        class Capacity:
            def __init__(self):
                self.reservations = []

            async def reserve(self):
                reservation = Reservation()
                self.reservations.append(reservation)
                return reservation

        missing = "audio_" + "f" * 32
        expired = self.module.store_audio_asset(MP3, "audio/mpeg", "expired")
        expired_meta = self.root / f'{expired["asset_id"]}.json'
        payload = json.loads(expired_meta.read_text(encoding="utf-8"))
        payload["created_at"] = 1
        expired_meta.write_text(json.dumps(payload), encoding="utf-8")
        leased = self.module.store_audio_asset(MP3, "audio/mpeg", "leased")
        self.module.lease_audio_assets([leased["asset_id"]], "other-task")

        for asset_id, error in (
            (missing, FileNotFoundError),
            (expired["asset_id"], FileNotFoundError),
            (leased["asset_id"], self.module.AudioLeaseError),
        ):
            capacity = Capacity()
            created_tasks = []
            with self.subTest(asset_id=asset_id), self.assertRaises(error):
                await self.module.prepare_async_audio_submission(
                    [{"text": "line", "audio_asset_id": asset_id}],
                    capacity.reserve,
                    lambda: created_tasks.append("task"),
                )
            self.assertEqual([], created_tasks)
            self.assertEqual(1, len(capacity.reservations))
            self.assertTrue(capacity.reservations[0].released)

    async def test_scheduler_cleans_without_new_upload(self):
        calls = []
        with patch.object(
            self.module,
            "cleanup_expired_audio_assets",
            side_effect=lambda: calls.append(time.time()) or 0,
        ), patch.object(self.module, "CLEANUP_INTERVAL_SECONDS", 0.01):
            await self.module.start_cleanup_scheduler()
            for _ in range(20):
                if len(calls) >= 2:
                    break
                await asyncio.sleep(0.01)
            await self.module.stop_cleanup_scheduler()

        self.assertGreaterEqual(len(calls), 2)

    async def test_startup_reclaims_crash_lease_before_cleanup(self):
        record = self.module.store_audio_asset(MP3, "audio/mpeg", "crash-lease")
        self.module.lease_audio_assets([record["asset_id"]], "dead-task")
        metadata = self.root / f'{record["asset_id"]}.json'
        payload = json.loads(metadata.read_text(encoding="utf-8"))
        payload["created_at"] = 1
        metadata.write_text(json.dumps(payload), encoding="utf-8")

        with patch.object(self.module.time, "time", return_value=self.module.AUDIO_TTL_SECONDS + 2):
            await self.module.start_cleanup_scheduler()
            await self.module.stop_cleanup_scheduler()

        self.assertFalse((self.root / f'{record["asset_id"]}.mp3').exists())

    async def test_task_creation_failure_releases_lease_and_capacity(self):
        class Reservation:
            released = False

            async def release(self):
                self.released = True

        reservation = Reservation()
        record = self.module.store_audio_asset(MP3, "audio/mpeg", "create-fails")

        async def reserve():
            return reservation

        with self.assertRaisesRegex(RuntimeError, "create failed"):
            await self.module.prepare_async_audio_submission(
                [{"text": "line", "audio_asset_id": record["asset_id"]}],
                reserve,
                lambda: (_ for _ in ()).throw(RuntimeError("create failed")),
            )

        self.assertTrue(reservation.released)
        self.assertFalse((self.root / f'{record["asset_id"]}.mp3').exists())

    async def test_nested_task_creation_failure_releases_every_cue_asset(self):
        class Reservation:
            released = False

            async def release(self):
                self.released = True

        reservation = Reservation()
        first = self.module.store_audio_asset(MP3, "audio/mpeg", "nested-fail-first")
        second = self.module.store_audio_asset(MP3, "audio/mpeg", "nested-fail-second")
        segments = [{
            "text": "第一句，第二句。",
            "cues": [
                {"text": "第一句，", "audio_asset_id": first["asset_id"]},
                {"text": "第二句。", "audio_asset_id": second["asset_id"]},
            ],
        }]

        async def reserve():
            return reservation

        with self.assertRaisesRegex(RuntimeError, "create failed"):
            await self.module.prepare_async_audio_submission(
                segments,
                reserve,
                lambda: (_ for _ in ()).throw(RuntimeError("create failed")),
            )

        self.assertTrue(reservation.released)
        self.assertFalse((self.root / f'{first["asset_id"]}.mp3').exists())
        self.assertFalse((self.root / f'{second["asset_id"]}.mp3').exists())


class VoiceAssetRouterSourceTests(unittest.TestCase):
    def test_router_contract_is_present_without_requiring_deployment_dependencies(self):
        source = ROUTER_PATH.read_text(encoding="utf-8")
        self.assertIn('@router.get("/voices/public")', source)
        self.assertIn('@router.post("/audio-assets"', source)
        self.assertIn('alias="X-Request-Id"', source)
        self.assertIn('status_code=status.HTTP_201_CREATED', source)
        self.assertIn('external_audio.store_audio_asset', source)
        self.assertIn('request.stream()', source)
        self.assertNotIn('request.body()', source)


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

    def test_oversized_content_length_and_stream_never_write_assets(self):
        headers = {
            "Content-Type": "audio/mpeg",
            "X-Request-Id": "oversized",
            "Content-Length": str(self.external.MAX_AUDIO_BYTES + 1),
        }
        response = self.client.post("/api/audio-assets", content=MP3, headers=headers)
        self.assertEqual(413, response.status_code)
        self.assertEqual([], list(self.root.glob("audio_*")))

        with patch.object(self.external, "MAX_AUDIO_BYTES", len(MP3) - 1):
            response = self.client.post(
                "/api/audio-assets",
                content=MP3,
                headers={"Content-Type": "audio/mpeg", "X-Request-Id": "chunked"},
            )
        self.assertEqual(413, response.status_code)
        self.assertEqual([], list(self.root.glob("audio_*")))

    def test_missing_and_forged_content_length_use_stream_boundary(self):
        async def invoke(headers, chunks):
            sent = iter(chunks)

            async def receive():
                try:
                    body = next(sent)
                except StopIteration:
                    return {"type": "http.request", "body": b"", "more_body": False}
                return {"type": "http.request", "body": body, "more_body": True}

            request = self.router_module.Request(
                {"type": "http", "method": "POST", "path": "/api/audio-assets", "headers": headers},
                receive,
            )
            return await self.router_module.upload_audio_asset(request, "stream-test")

        base = [(b"content-type", b"audio/mpeg")]
        with patch.object(self.external, "MAX_AUDIO_BYTES", len(MP3) - 1):
            for headers in (base, base + [(b"content-length", b"1")]):
                with self.subTest(headers=headers), self.assertRaises(self.router_module.HTTPException) as caught:
                    asyncio.run(invoke(headers, [MP3[:10], MP3[10:]]))
                self.assertEqual(413, caught.exception.status_code)
        self.assertEqual([], list(self.root.glob("audio_*")))

    def test_concurrent_chunked_oversize_requests_are_bounded(self):
        async def invoke(index):
            chunks = iter((b"ID3" + b"a" * 4, b"b" * 8))

            async def receive():
                try:
                    body = next(chunks)
                except StopIteration:
                    return {"type": "http.request", "body": b"", "more_body": False}
                return {"type": "http.request", "body": body, "more_body": True}

            request = self.router_module.Request(
                {
                    "type": "http",
                    "method": "POST",
                    "path": "/api/audio-assets",
                    "headers": [(b"content-type", b"audio/mpeg")],
                },
                receive,
            )
            try:
                await self.router_module.upload_audio_asset(request, f"concurrent-{index}")
            except self.router_module.HTTPException as exc:
                return exc.status_code
            return 201

        async def run_all():
            return await asyncio.gather(*(invoke(index) for index in range(8)))

        with patch.object(self.external, "MAX_AUDIO_BYTES", 10):
            statuses = asyncio.run(run_all())

        self.assertEqual([413] * 8, statuses)
        self.assertEqual([], list(self.root.glob("audio_*")))


if __name__ == "__main__":
    unittest.main()
