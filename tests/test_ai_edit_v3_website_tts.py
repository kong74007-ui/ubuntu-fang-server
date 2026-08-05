from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from server.content_domains.ai_edit_v3.providers import (
    DefinitiveNotAccepted,
    SubmissionUnknown,
)
from server.content_domains.ai_edit_v3.providers.website_tts import WebsiteCosyVoiceTts


class WebsiteCosyVoiceTtsTests(unittest.TestCase):
    def test_generates_owned_voice_to_explicit_job_path(self) -> None:
        calls = []
        with tempfile.TemporaryDirectory() as folder:
            generated = Path(folder) / "website-output.mp3"
            generated.write_bytes(b"mp3-bytes")

            def generate(payload):
                calls.append(dict(payload))
                return {"type": "audio", "file": "audio/generated.mp3"}

            provider = WebsiteCosyVoiceTts(
                generate_audio=generate,
                resolve_output=lambda _value: generated,
                configured=True,
            )
            output = Path(folder) / "job/source.mp3"
            result = provider.generate(
                owner="alice",
                text="团队负责人怎么做？",
                voice_id="my-clone",
                output_path=output,
                idempotency_key="ai-edit-v3:job-1:tts",
                deadline_at=time.time() + 30,
            )

            self.assertEqual(output.read_bytes(), b"mp3-bytes")
        self.assertEqual(calls[0]["_username"], "alice")
        self.assertEqual(calls[0]["voice"], "my-clone")
        self.assertEqual(result.provider, "website-cosyvoice")
        self.assertEqual(result.capability, "tts")
        self.assertEqual(result.payload["characters"], 9)
        self.assertNotIn("text", result.payload)

    def test_missing_configuration_is_definitive_and_transport_failure_is_unknown(self) -> None:
        missing = WebsiteCosyVoiceTts(
            generate_audio=lambda payload: {},
            resolve_output=lambda value: None,
            configured=False,
        )
        with tempfile.TemporaryDirectory() as folder, self.assertRaises(DefinitiveNotAccepted):
            missing.generate(
                owner="alice", text="内容", voice_id="voice",
                output_path=Path(folder) / "out.mp3", idempotency_key="key",
                deadline_at=time.time() + 30,
            )

        unknown = WebsiteCosyVoiceTts(
            generate_audio=lambda payload: (_ for _ in ()).throw(RuntimeError("network")),
            resolve_output=lambda value: None,
            configured=True,
        )
        with tempfile.TemporaryDirectory() as folder, self.assertRaises(SubmissionUnknown):
            unknown.generate(
                owner="alice", text="内容", voice_id="voice",
                output_path=Path(folder) / "out.mp3", idempotency_key="key",
                deadline_at=time.time() + 30,
            )

    def test_value_error_after_call_starts_is_submission_unknown(self) -> None:
        calls = []

        def accepted_then_invalid_response(payload):
            calls.append(dict(payload))
            raise ValueError("provider response was not valid JSON")

        provider = WebsiteCosyVoiceTts(
            generate_audio=accepted_then_invalid_response,
            resolve_output=lambda value: None,
            configured=True,
        )
        with tempfile.TemporaryDirectory() as folder, self.assertRaises(SubmissionUnknown):
            provider.generate(
                owner="alice", text="content", voice_id="voice",
                output_path=Path(folder) / "out.mp3", idempotency_key="key",
                deadline_at=time.time() + 30,
            )
        self.assertEqual(1, len(calls))

    def test_interrupted_copy_never_leaves_a_trusted_partial_target(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            generated = Path(folder) / "website-output.mp3"
            generated.write_bytes(b"complete-provider-file")
            output = Path(folder) / "job/source.mp3"
            provider = WebsiteCosyVoiceTts(
                generate_audio=lambda payload: {
                    "type": "audio", "file": "audio/generated.mp3"
                },
                resolve_output=lambda value: generated,
                configured=True,
            )

            def interrupted_copy(_source, destination):
                Path(destination).write_bytes(b"partial")
                raise OSError("disk interrupted")

            with patch(
                "server.content_domains.ai_edit_v3.providers.website_tts.shutil.copyfile",
                side_effect=interrupted_copy,
            ), self.assertRaises(SubmissionUnknown):
                provider.generate(
                    owner="alice", text="content", voice_id="voice",
                    output_path=output, idempotency_key="key",
                    deadline_at=time.time() + 30,
                )

            self.assertFalse(output.exists())
            self.assertEqual([], list(output.parent.glob("*.tmp")))


if __name__ == "__main__":
    unittest.main()
