import hashlib
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from server.content_domains.ai_edit_v2_providers.base import (
    ProviderError,
    UnknownSubmissionError,
)
from server.content_domains.ai_edit_v2_providers.elevenlabs import ElevenLabsProvider


FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "ai_edit_v2"
    / "provider_responses"
    / "elevenlabs_music_headers.json"
)
OWNER = "owner-a"
JOB_ID = "123e4567-e89b-12d3-a456-426614174000"


class FakeCos:
    def __init__(self):
        self.objects = {}
        self.puts = []

    def put_bytes(self, content, cos_key, content_type, private=True):
        self.puts.append((content, cos_key, content_type, private))
        self.objects[cos_key] = (content, content_type)
        return {"ETag": '"etag-audio"'}

    def head_object(self, cos_key):
        content, content_type = self.objects[cos_key]
        return {
            "content_length": len(content),
            "content_type": content_type,
            "etag": "etag-audio",
        }


class RecordingTransport:
    def __init__(self, response=None, failure=None):
        self.response = response or {
            "content": b"ID3-private-audio",
            "content_type": "audio/mpeg",
            "headers": json.loads(FIXTURE.read_text(encoding="utf-8")),
        }
        self.failure = failure
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, headers, body, timeout))
        if self.failure:
            raise self.failure
        return self.response


class ElevenLabsProviderTests(unittest.TestCase):
    def _provider(self, temp_dir, transport=None, cos=None):
        return ElevenLabsProvider(
            owner=OWNER,
            job_id=JOB_ID,
            db_path=os.path.join(temp_dir, "audio-idempotency.db"),
            cos_api=cos or FakeCos(),
            http_request=transport or RecordingTransport(),
            clock_ms=lambda: 100,
        )

    def test_music_is_forced_instrumental_and_uses_only_music_v2(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ,
            {
                "ELEVENLABS_API_KEY": "test-eleven-key",
                "ELEVENLABS_MUSIC_MODEL": "forbidden-override",
            },
            clear=False,
        ):
            transport = RecordingTransport()
            provider = self._provider(temp_dir, transport=transport)
            provider.generate_music("calm business", 30_000, "job:music")

        method, url, headers, body, _ = transport.calls[0]
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(method, "POST")
        self.assertTrue(url.endswith("/v1/music"))
        self.assertEqual(payload["model_id"], "music_v2")
        self.assertIs(payload["force_instrumental"], True)
        self.assertEqual(headers["xi-api-key"], "test-eleven-key")
        self.assertNotIn("Authorization", headers)

    def test_sfx_uses_only_eleven_text_to_sound_v2(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ, {"ELEVENLABS_API_KEY": "test-eleven-key"}, clear=False
        ):
            transport = RecordingTransport()
            self._provider(temp_dir, transport=transport).generate_sfx(
                "soft page turn", 900, "job:sfx:1"
            )

        payload = json.loads(transport.calls[0][3].decode("utf-8"))
        self.assertTrue(transport.calls[0][1].endswith("/v1/sound-generation"))
        self.assertEqual(payload["model_id"], "eleven_text_to_sound_v2")

    def test_output_is_verified_in_private_cos_before_return(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ, {"ELEVENLABS_API_KEY": "test-eleven-key"}, clear=False
        ):
            cos = FakeCos()
            result = self._provider(temp_dir, cos=cos).generate_music(
                "restrained ambient bed", 10_000, "job:music"
            )

        self.assertEqual(result.provider, "elevenlabs")
        self.assertEqual(result.capability, "music")
        self.assertEqual(result.request_id, "eleven-request-1")
        self.assertEqual(result.cost_units, 17)
        self.assertTrue(result.payload["cos_key"].startswith(
            "ai-edit-v2/" + hashlib.sha256(OWNER.encode()).hexdigest()[:16] + f"/{JOB_ID}/"
        ))
        self.assertEqual(cos.puts[0][2:], ("audio/mpeg", True))
        self.assertNotIn("content", result.payload)
        self.assertNotIn("url", json.dumps(result.payload).lower())

    def test_completed_idempotency_replays_across_instances_without_resubmission(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ, {"ELEVENLABS_API_KEY": "test-eleven-key"}, clear=False
        ):
            first_transport = RecordingTransport()
            first = self._provider(temp_dir, transport=first_transport).generate_music(
                "calm", 5_000, "same-key"
            )
            second_transport = RecordingTransport(failure=AssertionError("must not resubmit"))
            second = self._provider(temp_dir, transport=second_transport).generate_music(
                "calm", 5_000, "same-key"
            )

        self.assertEqual(first.payload, second.payload)
        self.assertEqual(second.request_id, "eleven-request-1")
        self.assertEqual(second_transport.calls, [])

    def test_unknown_submission_is_durable_and_never_blindly_resubmitted(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ, {"ELEVENLABS_API_KEY": "test-eleven-key"}, clear=False
        ):
            uncertain = RecordingTransport(failure=TimeoutError("after send"))
            with self.assertRaises(UnknownSubmissionError):
                self._provider(temp_dir, transport=uncertain).generate_music(
                    "calm", 5_000, "unknown-key"
                )
            retry = RecordingTransport(failure=AssertionError("duplicate charge"))
            with self.assertRaises(UnknownSubmissionError):
                self._provider(temp_dir, transport=retry).generate_music(
                    "calm", 5_000, "unknown-key"
                )

        self.assertEqual(len(uncertain.calls), 1)
        self.assertEqual(retry.calls, [])

    def test_same_key_with_changed_request_is_rejected_before_transport(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(
            os.environ, {"ELEVENLABS_API_KEY": "test-eleven-key"}, clear=False
        ):
            provider = self._provider(temp_dir)
            provider.generate_music("calm", 5_000, "same-key")
            with self.assertRaisesRegex(ProviderError, "request_conflict"):
                provider.generate_music("energetic", 5_000, "same-key")

    def test_empty_or_non_audio_response_never_reaches_cos(self):
        bad_responses = (
            {"content": b"", "content_type": "audio/mpeg", "headers": {}},
            {"content": b"<html>", "content_type": "text/html", "headers": {}},
        )
        for index, response in enumerate(bad_responses):
            with self.subTest(index=index), tempfile.TemporaryDirectory() as temp_dir, patch.dict(
                os.environ, {"ELEVENLABS_API_KEY": "test-eleven-key"}, clear=False
            ):
                cos = FakeCos()
                provider = self._provider(
                    temp_dir, transport=RecordingTransport(response=response), cos=cos
                )
                with self.assertRaises(ProviderError):
                    provider.generate_music("calm", 5_000, f"bad-{index}")
                self.assertEqual(cos.puts, [])


if __name__ == "__main__":
    unittest.main()
