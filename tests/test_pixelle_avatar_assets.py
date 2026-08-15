from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import os
import stat
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
except ModuleNotFoundError:
    FastAPI = None
    TestClient = None


ROOT = Path(__file__).resolve().parents[1]
OVERRIDES_ROOT = ROOT / "deploy" / "pixelle-video" / "overrides"
MODULE_PATH = OVERRIDES_ROOT / "api" / "avatar_assets.py"
ROUTER_PATH = OVERRIDES_ROOT / "api" / "routers" / "avatar_assets.py"
PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR"
    b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00"
    b"\x90wS\xde"
)

if str(OVERRIDES_ROOT) not in sys.path:
    sys.path.insert(0, str(OVERRIDES_ROOT))


def load_module(name: str, path: Path):
    if not path.is_file():
        raise AssertionError(f"missing avatar assets module: {path}")
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class AvatarAssetRegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.module = load_module("pixelle_avatar_assets_test", MODULE_PATH)
        self.module.AVATAR_ROOT = self.root

    def tearDown(self):
        sys.modules.pop("pixelle_avatar_assets_test", None)
        self.tmp.cleanup()

    def test_store_and_lease_png(self):
        record = self.module.store_avatar_asset(PNG_BYTES, "image/png", "request-1")

        self.assertRegex(record["asset_id"], r"^avatar_[0-9a-f]{32}$")
        self.assertEqual(
            {
                "asset_id": record["asset_id"],
                "content_type": "image/png",
                "size": len(PNG_BYTES),
                "sha256": hashlib.sha256(PNG_BYTES).hexdigest(),
            },
            record,
        )

        leased = self.module.lease_avatar_assets([record["asset_id"]], "task-1")
        leased_path = leased[record["asset_id"]]
        self.assertEqual(PNG_BYTES, leased_path.read_bytes())
        if os.name != "nt":
            self.assertEqual(0o600, stat.S_IMODE(leased_path.stat().st_mode))

    def test_duplicate_lease_is_rejected(self):
        record = self.module.store_avatar_asset(PNG_BYTES, "image/png", "request-1")

        self.module.lease_avatar_assets([record["asset_id"]], "task-1")

        with self.assertRaises(self.module.AvatarLeaseError):
            self.module.lease_avatar_assets([record["asset_id"]], "task-2")

    def test_cleanup_releases_only_unleased_expired_assets(self):
        old = self.module.store_avatar_asset(PNG_BYTES, "image/png", "request-old")
        leased = self.module.store_avatar_asset(PNG_BYTES, "image/png", "request-leased")
        self.module.lease_avatar_assets([leased["asset_id"]], "task-1")

        for record in (old, leased):
            metadata = self.root / f'{record["asset_id"]}.json'
            payload = self.module._read_metadata(metadata)
            payload["created_at"] = 100
            self.module._write_metadata(metadata, payload)

        removed = self.module.cleanup_expired_avatar_assets(
            now=100 + self.module.AVATAR_TTL_SECONDS + 1
        )

        self.assertEqual(1, removed)
        self.assertFalse(any(self.root.glob(f'{old["asset_id"]}.*')))
        self.assertTrue((self.root / f'{leased["asset_id"]}.png').exists())

    def test_release_removes_files(self):
        first = self.module.store_avatar_asset(PNG_BYTES, "image/png", "request-1")
        second = self.module.store_avatar_asset(PNG_BYTES, "image/png", "request-2")
        self.module.lease_avatar_assets([first["asset_id"], second["asset_id"]], "task-1")

        self.module.release_avatar_assets([first["asset_id"], second["asset_id"]])

        self.assertFalse(any(self.root.glob(f'{first["asset_id"]}.*')))
        self.assertFalse(any(self.root.glob(f'{second["asset_id"]}.*')))


class AvatarAssetRouterSourceTests(unittest.TestCase):
    def test_router_contract_is_present_without_runtime_imports(self):
        source = ROUTER_PATH.read_text(encoding="utf-8")

        self.assertIn('@router.post("/avatar-assets"', source)
        self.assertIn('alias="X-Request-Id"', source)
        self.assertIn('status_code=status.HTTP_201_CREATED', source)
        self.assertIn("avatar_assets.store_avatar_asset", source)
        self.assertIn("request.stream()", source)
        self.assertNotIn("request.body()", source)


@unittest.skipIf(FastAPI is None or TestClient is None, "FastAPI deployment dependency is not installed")
class AvatarAssetRouterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        api_package = types.ModuleType("api")
        api_package.__path__ = []
        self.avatar_assets = load_module("api.avatar_assets", MODULE_PATH)
        self.avatar_assets.AVATAR_ROOT = self.root
        api_package.avatar_assets = self.avatar_assets
        self.modules_patch = patch.dict(
            sys.modules,
            {"api": api_package, "api.avatar_assets": self.avatar_assets},
        )
        self.modules_patch.start()
        self.router_module = load_module("pixelle_avatar_assets_router_test", ROUTER_PATH)
        app = FastAPI()
        app.include_router(self.router_module.router, prefix="/api")
        self.client = TestClient(app)

    def tearDown(self):
        self.modules_patch.stop()
        sys.modules.pop("pixelle_avatar_assets_router_test", None)
        sys.modules.pop("api.avatar_assets", None)
        self.tmp.cleanup()

    def test_upload_contract_and_errors(self):
        response = self.client.post(
            "/api/avatar-assets",
            content=PNG_BYTES,
            headers={"Content-Type": "image/png", "X-Request-Id": "request-1"},
        )

        self.assertEqual(201, response.status_code)
        self.assertEqual(
            {
                "asset_id",
                "content_type",
                "size",
                "sha256",
            },
            set(response.json()),
        )
        self.assertEqual(
            400,
            self.client.post(
                "/api/avatar-assets",
                content=PNG_BYTES,
                headers={"Content-Type": "image/png"},
            ).status_code,
        )
        self.assertEqual(
            400,
            self.client.post(
                "/api/avatar-assets",
                content=b"bad",
                headers={"Content-Type": "image/png", "X-Request-Id": "request-2"},
            ).status_code,
        )
        with patch.object(self.avatar_assets, "MAX_AVATAR_BYTES", 1):
            self.assertEqual(
                413,
                self.client.post(
                    "/api/avatar-assets",
                    content=PNG_BYTES,
                    headers={"Content-Type": "image/png", "X-Request-Id": "request-3"},
                ).status_code,
            )

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
                {"type": "http", "method": "POST", "path": "/api/avatar-assets", "headers": headers},
                receive,
            )
            return await self.router_module.upload_avatar_asset(request, "stream-test")

        base = [(b"content-type", b"image/png")]
        with patch.object(self.avatar_assets, "MAX_AVATAR_BYTES", len(PNG_BYTES) - 1):
            for headers in (base, base + [(b"content-length", b"1")]):
                with self.subTest(headers=headers), self.assertRaises(self.router_module.HTTPException) as caught:
                    asyncio.run(invoke(headers, [PNG_BYTES[:10], PNG_BYTES[10:]]))
                self.assertEqual(413, caught.exception.status_code)
        self.assertEqual([], list(self.root.glob("avatar_*")))


if __name__ == "__main__":
    unittest.main()
