from __future__ import annotations

import hashlib
from pathlib import Path
import socket
from tempfile import TemporaryDirectory
import time
import unittest

from server.content_domains.ai_edit_v3.providers.base import (
    DefinitiveNotAccepted,
    SecretValue,
    SubmissionUnknown,
)
from server.content_domains.ai_edit_v3.providers.elevenlabs import (
    ElevenLabsAudioGenerator,
    MusicGenerationRequest,
    SfxGenerationRequest,
)
from server.content_domains.ai_edit_v3.runtime import build_runtime, preflight


class FakeResponse:
    def __init__(self, body=b"ID3audio", *, status=200, headers=None):
        self.status = status
        self.headers = headers or {
            "content-type": "audio/mpeg",
            "request-id": "req_audio_1",
            "x-usage-credits": "12",
        }
        self._body = body
        self._offset = 0

    def read(self, size):
        chunk = self._body[self._offset:self._offset + size]
        self._offset += len(chunk)
        return chunk

    def close(self):
        return None


class FakeTransport:
    def __init__(self, response=None, error=None):
        self.response = response or FakeResponse()
        self.error = error
        self.calls = []

    def open(self, **request):
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        return self.response


class TransportError(OSError):
    def __init__(self, *, body_sent):
        self.body_sent = body_sent
        super().__init__("secret prompt and signed-url must never be surfaced")


class ElevenLabsTests(unittest.TestCase):
    def test_exact_music_and_sfx_requests_are_fixed_and_bounded(self):
        with TemporaryDirectory() as temporary:
            transport = FakeTransport()
            provider = self._provider(transport)
            music = Path(temporary) / "music.mp3"
            provider.generate_music(
                MusicGenerationRequest("克制、现代、无歌词的商业背景音乐", 30_000),
                output_path=music,
                idempotency_key="ai-edit-v3:job:audio:bgm",
                deadline_at=time.time() + 60,
            )
            call = transport.calls[0]
            self.assertEqual("https://api.elevenlabs.io/v1/music", call["url"])
            self.assertEqual("music_v2", call["json_body"]["model_id"])
            self.assertIs(True, call["json_body"]["force_instrumental"])
            self.assertEqual(30_000, call["json_body"]["music_length_ms"])
            self.assertEqual("ai-edit-v3:job:audio:bgm", call["headers"]["Idempotency-Key"])
            self.assertEqual("test-secret", call["headers"]["xi-api-key"])

            transport.response = FakeResponse(headers={"content-type": "audio/wav", "request-id": "req_sfx"}, body=b"RIFFxxxxWAVEaudio")
            provider.generate_sfx(
                SfxGenerationRequest("轻微数字强调音", 800, "cue_01", False),
                output_path=Path(temporary) / "sfx.wav",
                idempotency_key="ai-edit-v3:job:audio:sfx:cue_01",
                deadline_at=time.time() + 60,
            )
            call = transport.calls[1]
            self.assertEqual("https://api.elevenlabs.io/v1/sound-generation", call["url"])
            self.assertEqual("eleven_text_to_sound_v2", call["json_body"]["model_id"])
            self.assertEqual(.8, call["json_body"]["duration_seconds"])
            for request in [MusicGenerationRequest("x", 2_999), MusicGenerationRequest("x", 600_001)]:
                with self.assertRaises(ValueError):
                    provider.generate_music(request, output_path=Path(temporary) / "bad.mp3", idempotency_key="key", deadline_at=time.time() + 60)
            with self.assertRaises(ValueError):
                provider.generate_sfx(SfxGenerationRequest("x", 30_001, "cue", True), output_path=Path(temporary) / "bad.wav", idempotency_key="key", deadline_at=time.time() + 60)

    def test_streamed_result_has_hash_usage_and_no_audio_or_secret_payload(self):
        with TemporaryDirectory() as temporary:
            body = b"ID3" + b"a" * 32_000
            transport = FakeTransport(FakeResponse(body=body))
            provider = self._provider(transport)
            output = Path(temporary) / "music.mp3"
            result = provider.generate_music(MusicGenerationRequest("品牌音乐", 3_000), output_path=output, idempotency_key="one", deadline_at=time.time() + 60)
            self.assertEqual(body, output.read_bytes())
            self.assertEqual(hashlib.sha256(body).hexdigest(), result.payload["sha256"])
            self.assertEqual(len(body), result.payload["size_bytes"])
            self.assertEqual("audio/mpeg", result.payload["content_type"])
            self.assertEqual("music_v2", result.payload["model"])
            self.assertEqual("req_audio_1", result.request_id)
            self.assertEqual(12, result.usage["credits"])
            rendered = repr(result)
            self.assertNotIn("test-secret", rendered)
            self.assertNotIn("品牌音乐", rendered)
            self.assertNotIn("ID3", rendered)

    def test_empty_json_html_and_oversized_responses_fail_without_output(self):
        cases = [
            FakeResponse(b"", headers={"content-type": "audio/mpeg"}),
            FakeResponse(b'{"error":"bad"}', headers={"content-type": "application/json"}),
            FakeResponse(b"<html>bad</html>", headers={"content-type": "audio/mpeg"}),
            FakeResponse(b"ID3" + b"x" * (16 * 1024 * 1024 + 1), headers={"content-type": "audio/mpeg"}),
        ]
        with TemporaryDirectory() as temporary:
            for index, response in enumerate(cases):
                output = Path(temporary) / f"bad-{index}.mp3"
                with self.assertRaises(ValueError):
                    self._provider(FakeTransport(response)).generate_sfx(
                        SfxGenerationRequest("音效", 500, "cue", True), output_path=output,
                        idempotency_key=f"bad-{index}", deadline_at=time.time() + 60,
                    )
                self.assertFalse(output.exists())

    def test_submission_outcome_classification_never_retries_inside_adapter(self):
        with TemporaryDirectory() as temporary:
            dns = socket.gaierror(socket.EAI_NONAME, "hidden")
            dns.body_sent = False
            transport = FakeTransport(error=dns)
            with self.assertRaises(DefinitiveNotAccepted):
                self._provider(transport).generate_music(MusicGenerationRequest("x", 3_000), output_path=Path(temporary) / "a.mp3", idempotency_key="dns", deadline_at=time.time() + 60)
            self.assertEqual(1, len(transport.calls))

            for status in [429, 503]:
                headers = {"content-type": "application/json", "x-request-accepted": "false"}
                transport = FakeTransport(FakeResponse(b"{}", status=status, headers=headers))
                with self.assertRaises(DefinitiveNotAccepted):
                    self._provider(transport).generate_music(MusicGenerationRequest("x", 3_000), output_path=Path(temporary) / f"{status}.mp3", idempotency_key=str(status), deadline_at=time.time() + 60)
                self.assertEqual(1, len(transport.calls))

            transport = FakeTransport(error=TransportError(body_sent=True))
            with self.assertRaises(SubmissionUnknown) as raised:
                self._provider(transport).generate_music(MusicGenerationRequest("full secret prompt", 3_000), output_path=Path(temporary) / "unknown.mp3", idempotency_key="unknown", deadline_at=time.time() + 60)
            self.assertEqual("elevenlabs_submission_unknown", str(raised.exception))
            self.assertEqual(1, len(transport.calls))

    def test_preflight_distinguishes_implemented_configured_and_missing(self):
        report = preflight(build_runtime(env={}))
        self.assertEqual("implemented", report.items["elevenlabs_audio"].status)
        self.assertFalse(report.accepts_new_jobs)
        configured = self._provider(FakeTransport()).probe_capability("audio_generator", environment="test")
        self.assertEqual({"available": True, "environment": "test", "provider": "elevenlabs"}, configured)
        missing = ElevenLabsAudioGenerator(api_key=None, transport=FakeTransport()).probe_capability("audio_generator", environment="test")
        self.assertEqual("elevenlabs_api_key_missing", missing["reason_code"])

    @staticmethod
    def _provider(transport):
        return ElevenLabsAudioGenerator(api_key=SecretValue("test-secret"), transport=transport)


if __name__ == "__main__":
    unittest.main()
