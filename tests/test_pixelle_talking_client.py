from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import subprocess
import threading
import urllib.error
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = (
    ROOT
    / "deploy"
    / "pixelle-video"
    / "overrides"
    / "pixelle_video"
    / "services"
    / "talking_client.py"
)


def load_module():
    spec = importlib.util.spec_from_file_location("pixelle_talking_client", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeResponse:
    def __init__(self, body: bytes, headers: dict[str, str] | None = None):
        self._body = body
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return self._body


def http_error(status: int, payload: dict) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "http://127.0.0.1:8096/api/internal/pixelle/talking-clip",
        status,
        "bridge error",
        {},
        io.BytesIO(json.dumps(payload).encode("utf-8")),
    )


def make_files(tmp_path: Path) -> tuple[Path, Path, Path]:
    image = tmp_path / "person.jpg"
    audio = tmp_path / "cue.mp3"
    output = tmp_path / "silent.mp4"
    image.write_bytes(b"jpeg-image")
    audio.write_bytes(b"mp3-audio")
    return image, audio, output


def fake_ffmpeg(command, **kwargs):
    assert kwargs == {"check": True, "capture_output": True}
    assert command[-8:] == [
        "-map",
        "0:v:0",
        "-c:v",
        "copy",
        "-an",
        "-movflags",
        "+faststart",
        command[-1],
    ]
    Path(command[-1]).write_bytes(b"silent-video")
    return subprocess.CompletedProcess(command, 0)


def test_client_retries_only_unbilled_retryable_failures(tmp_path):
    asyncio.run(_assert_client_retries_only_unbilled_retryable_failures(tmp_path))


async def _assert_client_retries_only_unbilled_retryable_failures(tmp_path):
    module = load_module()
    image, audio, output = make_files(tmp_path)
    responses = [
        http_error(503, {"retryable": True, "billed": False, "code": "busy"}),
        http_error(502, {"retryable": True, "billed": False, "code": "upstream"}),
        FakeResponse(b"provider-video", {"X-Provider-Video-Id": "video-123"}),
    ]
    requests = []
    timeouts = []
    sleeps = []

    def opener(request, timeout):
        requests.append(request)
        timeouts.append(timeout)
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    async def sleeper(seconds):
        sleeps.append(seconds)

    client = module.TalkingClient(
        token="internal-secret",
        opener=opener,
        sleeper=sleeper,
        process_runner=fake_ffmpeg,
    )
    result = await client.generate(
        str(image), str(audio), str(output), "req-1", "9:16"
    )

    assert result.video_path == str(output)
    assert result.provider_video_id == "video-123"
    assert result.attempts == 3
    assert result.warnings == []
    assert sleeps == [2, 5]
    assert timeouts == [1200, 1200, 1200]
    assert output.read_bytes() == b"silent-video"
    assert len(requests) == 3
    assert requests[0].full_url == "http://127.0.0.1:8096/api/internal/pixelle/talking-clip"
    assert requests[0].get_header("X-hq-pixelle-token") == "internal-secret"
    payload = json.loads(requests[0].data)
    assert payload["request_id"] == "req-1"
    assert payload["ratio"] == "9:16"
    assert payload["resolution"] == "1080p"
    assert payload["motion"] == "medium"
    assert payload["image_data"].startswith("data:image/jpeg;base64,")
    assert payload["audio_data"].startswith("data:audio/mpeg;base64,")
    assert len(payload["image_sha256"]) == 64


@pytest.mark.parametrize(
    "payload",
    [
        {"retryable": False, "billed": True, "code": "heygen_billed"},
        {"retryable": False, "billed": False, "code": "invalid_request"},
        {"retryable": True, "billed": True, "code": "ambiguous_billed"},
    ],
)
def test_client_does_not_retry_billed_or_terminal_failures(tmp_path, payload):
    asyncio.run(_assert_client_does_not_retry_billed_or_terminal_failures(tmp_path, payload))


async def _assert_client_does_not_retry_billed_or_terminal_failures(tmp_path, payload):
    module = load_module()
    image, audio, output = make_files(tmp_path)
    calls = 0

    def opener(_request, timeout):
        assert timeout == 1200
        nonlocal calls
        calls += 1
        raise http_error(502, payload)

    async def fail_sleep(_seconds):
        raise AssertionError("terminal failures must not sleep")

    client = module.TalkingClient(
        token="internal-secret",
        opener=opener,
        sleeper=fail_sleep,
        process_runner=fake_ffmpeg,
    )
    with pytest.raises(module.TalkingClipError) as exc:
        await client.generate(str(image), str(audio), str(output), "req-2", "9:16")

    assert exc.value.attempts == 1
    assert exc.value.code == payload["code"]
    assert exc.value.billed is payload["billed"]
    assert calls == 1
    assert not output.exists()


def test_client_stops_after_three_retryable_failures(tmp_path):
    asyncio.run(_assert_client_stops_after_three_retryable_failures(tmp_path))


async def _assert_client_stops_after_three_retryable_failures(tmp_path):
    module = load_module()
    image, audio, output = make_files(tmp_path)
    sleeps = []
    calls = 0

    def opener(_request, timeout):
        assert timeout == 1200
        nonlocal calls
        calls += 1
        raise http_error(
            503,
            {"retryable": True, "billed": False, "code": "bridge_busy"},
        )

    async def sleeper(seconds):
        sleeps.append(seconds)

    client = module.TalkingClient(
        token="internal-secret",
        opener=opener,
        sleeper=sleeper,
        process_runner=fake_ffmpeg,
    )
    with pytest.raises(module.TalkingClipError) as exc:
        await client.generate(str(image), str(audio), str(output), "req-3", "9:16")

    assert exc.value.attempts == 3
    assert exc.value.retryable is True
    assert calls == 3
    assert sleeps == [2, 5]


def test_client_requires_internal_token(monkeypatch):
    module = load_module()
    monkeypatch.delenv("PIXELLE_TALKING_INTERNAL_TOKEN", raising=False)
    with pytest.raises(ValueError, match="internal token"):
        module.TalkingClient()


def test_client_cancellation_waits_for_ffmpeg_and_never_revives_output(tmp_path):
    asyncio.run(_assert_client_cancellation_waits_for_ffmpeg_and_never_revives_output(tmp_path))


async def _assert_client_cancellation_waits_for_ffmpeg_and_never_revives_output(tmp_path):
    module = load_module()
    image, audio, output = make_files(tmp_path)
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def opener(_request, timeout):
        assert timeout == 1200
        return FakeResponse(
            b"provider-video",
            {"X-Provider-Video-Id": "video-cancelled"},
        )

    def blocking_ffmpeg(command, **kwargs):
        assert kwargs == {"check": True, "capture_output": True}
        started.set()
        assert release.wait(timeout=5), "test did not release the ffmpeg runner"
        Path(command[-1]).write_bytes(b"late-silent-video")
        finished.set()
        return subprocess.CompletedProcess(command, 0)

    client = module.TalkingClient(
        token="internal-secret",
        opener=opener,
        process_runner=blocking_ffmpeg,
    )
    task = asyncio.create_task(
        client.generate(str(image), str(audio), str(output), "req-cancel", "9:16")
    )
    assert await asyncio.to_thread(started.wait, 5)

    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.to_thread(finished.wait, 5)
    await asyncio.sleep(0.05)

    assert not output.exists()
    assert not list(tmp_path.glob(".*.provider.mp4"))
    assert not list(tmp_path.glob(".*.silent.mp4"))
    assert not list(tmp_path.glob(".*.staged.mp4"))
