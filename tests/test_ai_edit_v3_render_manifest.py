from __future__ import annotations

import json
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path

from server.content_domains.ai_edit_v3.contracts import (
    ContractError,
    freeze_render_manifest,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "ai_edit_v3" / "valid-render-manifest-v1.json"


class FrozenRenderManifestTests(unittest.TestCase):
    def test_freeze_is_canonical_atomic_and_hash_verified(self) -> None:
        document = json.loads(FIXTURE.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "media"
            media.mkdir()
            (media / "source.mp4").write_bytes(b"video")
            (media / "master.wav").write_bytes(b"audio")
            (media / "image.png").write_bytes(b"image")
            frozen = freeze_render_manifest(document, root / "manifest.json", sandbox_root=root)
            raw = frozen.path.read_bytes()

            self.assertEqual(raw, json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
            self.assertEqual(frozen.sha256, sha256(raw).hexdigest())
            self.assertEqual(frozen.document, document)
            self.assertFalse(any(path.name.endswith(".tmp") for path in root.iterdir()))

    def test_destination_must_remain_in_sandbox_and_existing_file_is_not_overwritten(self) -> None:
        document = json.loads(FIXTURE.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media = root / "media"
            media.mkdir()
            (media / "source.mp4").write_bytes(b"video")
            (media / "master.wav").write_bytes(b"audio")
            (media / "image.png").write_bytes(b"image")
            with self.assertRaisesRegex(ContractError, "render_manifest_destination_invalid"):
                freeze_render_manifest(document, root.parent / "escape.json", sandbox_root=root)
            destination = root / "manifest.json"
            destination.write_text("sentinel", encoding="utf-8")
            with self.assertRaisesRegex(ContractError, "render_manifest_exists"):
                freeze_render_manifest(document, destination, sandbox_root=root)
            self.assertEqual(destination.read_text(encoding="utf-8"), "sentinel")


if __name__ == "__main__":
    unittest.main()
