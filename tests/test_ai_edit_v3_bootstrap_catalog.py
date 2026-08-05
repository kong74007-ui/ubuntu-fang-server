from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from server.content_domains.ai_edit_v3 import bootstrap


class ProductionCatalogAudioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = bootstrap.ProductionCatalog(())

    def test_audio_assets_are_owner_scoped_resolved_and_probed(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "owned.mp3"
            source.write_bytes(b"fake")

            def rows(owner, limit=120):
                return [{
                    "id": 17,
                    "username": owner,
                    "file": "audio/owned.mp3",
                    "text": "团队负责人方法讲解",
                    "voice_name": "杨姐",
                }]

            audio_domain = SimpleNamespace(
                list_audio_assets=rows,
                _resolve_out_file=lambda _value: source,
            )
            with patch.object(bootstrap, "_audio_domain", return_value=audio_domain), patch.object(
                bootstrap, "probe_media", return_value=SimpleNamespace(duration_ms=21_500)
            ):
                listed = self.catalog.list_audio_assets("alice")
                resolved = self.catalog.resolve_audio_asset("alice", "17")

        self.assertEqual(listed[0]["asset_id"], "17")
        self.assertEqual(listed[0]["duration_ms"], 21_500)
        self.assertEqual(listed[0]["mime_type"], "audio/mpeg")
        self.assertEqual(resolved["status"], "ready")
        self.assertEqual(resolved["owner"], "alice")
        self.assertEqual(resolved["local_path"], str(source.resolve()))
        with patch.object(bootstrap, "_audio_domain", return_value=audio_domain):
            self.assertIsNone(self.catalog.resolve_audio_asset("alice", "99"))

    def test_voices_use_owner_visible_voice_key_and_never_provider_voice_as_id(self) -> None:
        def voices(owner):
            return [
                {
                    "voice_key": "S_d21F8OR62",
                    "display_name": "温柔女声",
                    "scope": "public",
                    "username": "",
                    "provider_voice": "longwan",
                    "preview_url": "/api/gen/file/audio/public.mp3",
                },
                {
                    "voice_key": "my-clone",
                    "display_name": "我的音色",
                    "scope": "personal",
                    "username": owner,
                    "provider_voice": "cosyvoice-v3.5-plus-secret-provider-id",
                    "preview_url": "/api/gen/file/audio/personal.mp3",
                },
            ]

        with patch.object(
            bootstrap,
            "_audio_domain",
            return_value=SimpleNamespace(list_audio_voices=voices),
        ):
            listed = self.catalog.list_voices("alice")
            resolved = self.catalog.resolve_voice("alice", "my-clone")

        self.assertEqual([item["voice_id"] for item in listed], ["S_d21F8OR62", "my-clone"])
        self.assertEqual(resolved["voice_id"], "my-clone")
        self.assertEqual(resolved["status"], "ready")
        self.assertNotIn("provider_voice", repr(listed))
        with patch.object(
            bootstrap,
            "_audio_domain",
            return_value=SimpleNamespace(list_audio_voices=voices),
        ):
            self.assertIsNone(self.catalog.resolve_voice("alice", "provider-id"))


if __name__ == "__main__":
    unittest.main()
