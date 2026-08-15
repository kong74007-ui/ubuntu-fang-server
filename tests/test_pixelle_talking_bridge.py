# -*- coding: utf-8 -*-
import base64
import hashlib
import http.client
import importlib
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from content_domains import core, video


IMAGE_BYTES = b"\x89PNG\r\n\x1a\n" + b"image" * 16
AUDIO_BYTES = b"ID3" + b"audio" * 16


def _data_url(mime, data):
    return "data:%s;base64,%s" % (mime, base64.b64encode(data).decode("ascii"))


def _payload(image_bytes=IMAGE_BYTES, image_sha256=None, request_id="request-1"):
    return {
        "request_id": request_id,
        "image_data": _data_url("image/png", image_bytes),
        "audio_data": _data_url("audio/mpeg", AUDIO_BYTES),
        "image_sha256": image_sha256 or hashlib.sha256(image_bytes).hexdigest(),
        "resolution": "1080p",
        "ratio": "9:16",
        "motion": "medium",
    }


def _bridge():
    return importlib.import_module("content_domains.pixelle_talking")


def test_internal_token_is_required():
    bridge = _bridge()
    with pytest.raises(bridge.InternalTalkingAuthError):
        bridge.validate_internal_token("", "expected")


def test_billed_error_is_not_retryable():
    bridge = _bridge()
    error = bridge.classify_error(video.HeyGenBilledError("billed"))
    assert error == {
        "code": "heygen_billed",
        "detail": "billed",
        "retryable": False,
        "billed": True,
    }


def test_bridge_uses_two_slots():
    bridge = _bridge()
    assert bridge.TALKING_CLIP_CONCURRENCY == 2


@pytest.fixture(autouse=True)
def reset_bridge_cache():
    bridge = _bridge()
    for name in ("_IMAGE_ASSET_CACHE", "_IMAGE_HASH_LOCKS"):
        value = getattr(bridge, name, None)
        if value is not None:
            value.clear()
    yield


def test_invalid_data_url_is_rejected_before_provider(tmp_path, monkeypatch):
    bridge = _bridge()
    payload = _payload()
    payload["image_data"] = "not-a-data-url"
    monkeypatch.setattr(bridge, "OUT_DIR", tmp_path)
    with patch.object(video, "generate_heygen_video") as generate, \
            pytest.raises(bridge.TalkingPayloadError, match="image_data"):
        bridge.generate_clip(payload)
    generate.assert_not_called()


def test_image_sha_mismatch_is_rejected_before_provider(tmp_path, monkeypatch):
    bridge = _bridge()
    monkeypatch.setattr(bridge, "OUT_DIR", tmp_path)
    with patch.object(video, "generate_heygen_video") as generate, \
            pytest.raises(bridge.TalkingPayloadError, match="image_sha256"):
        bridge.generate_clip(_payload(image_sha256="0" * 64))
    generate.assert_not_called()


def test_temporary_inputs_are_deleted_after_success(tmp_path, monkeypatch):
    bridge = _bridge()
    monkeypatch.setattr(bridge, "OUT_DIR", tmp_path)
    seen = []

    def fake_generate(image_file, audio_file, resolution, ratio, motion, image_asset_id=None):
        image_path = tmp_path / image_file
        audio_path = tmp_path / audio_file
        assert image_path.read_bytes() == IMAGE_BYTES
        assert audio_path.read_bytes() == AUDIO_BYTES
        seen.extend([image_path, audio_path])
        assert (resolution, ratio, motion, image_asset_id) == (
            "1080p", "9:16", "medium", None)
        return {
            "video_id": "video-1",
            "image_asset_id": "image-asset-1",
            "video_file": "video/result.mp4",
        }

    monkeypatch.setattr(video, "generate_heygen_video", fake_generate)
    result = bridge.generate_clip(_payload())
    assert result["video_id"] == "video-1"
    assert seen and all(not path.exists() for path in seen)


def test_temporary_inputs_are_deleted_after_provider_error(tmp_path, monkeypatch):
    bridge = _bridge()
    monkeypatch.setattr(bridge, "OUT_DIR", tmp_path)
    seen = []

    def fail(image_file, audio_file, *_args, **_kwargs):
        seen.extend([tmp_path / image_file, tmp_path / audio_file])
        raise RuntimeError("provider failed")

    monkeypatch.setattr(video, "generate_heygen_video", fail)
    with pytest.raises(RuntimeError, match="provider failed"):
        bridge.generate_clip(_payload())
    assert seen and all(not path.exists() for path in seen)


def test_concurrent_same_hash_upload_is_coalesced(tmp_path, monkeypatch):
    bridge = _bridge()
    monkeypatch.setattr(bridge, "OUT_DIR", tmp_path)
    calls = []
    calls_lock = threading.Lock()

    def fake_generate(*_args, image_asset_id=None, **_kwargs):
        with calls_lock:
            calls.append(image_asset_id)
        time.sleep(0.05)
        return {
            "video_id": "video-%d" % len(calls),
            "image_asset_id": image_asset_id or "image-asset-shared",
            "video_file": "video/result.mp4",
        }

    monkeypatch.setattr(video, "generate_heygen_video", fake_generate)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(bridge.generate_clip, [_payload(request_id="a"), _payload(request_id="b")]))
    assert len(results) == 2
    assert calls == [None, "image-asset-shared"]


def test_bridge_never_runs_more_than_two_provider_calls(tmp_path, monkeypatch):
    bridge = _bridge()
    monkeypatch.setattr(bridge, "OUT_DIR", tmp_path)
    active = 0
    maximum = 0
    lock = threading.Lock()
    two_entered = threading.Event()
    release = threading.Event()

    def fake_generate(*_args, **_kwargs):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
            if active == 2:
                two_entered.set()
        assert release.wait(2)
        with lock:
            active -= 1
        return {
            "video_id": "video",
            "image_asset_id": "image-asset",
            "video_file": "video/result.mp4",
        }

    monkeypatch.setattr(video, "generate_heygen_video", fake_generate)
    payloads = [
        _payload(image_bytes=IMAGE_BYTES + bytes([i]), request_id=str(i))
        for i in range(3)
    ]
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(bridge.generate_clip, payload) for payload in payloads]
        assert two_entered.wait(2)
        time.sleep(0.05)
        assert maximum == 2
        release.set()
        [future.result(timeout=2) for future in futures]
    assert maximum == 2


@pytest.fixture
def bridge_server(monkeypatch):
    token = "test-internal-token"
    monkeypatch.setenv("PIXELLE_TALKING_INTERNAL_TOKEN", token)
    server = ThreadingHTTPServer(("127.0.0.1", 0), core.H)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], token
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _post(port, payload, token=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["X-HQ-Pixelle-Token"] = token
    connection.request(
        "POST", "/api/internal/pixelle/talking-clip",
        body=json.dumps(payload).encode("utf-8"), headers=headers)
    response = connection.getresponse()
    body = response.read()
    response_headers = dict(response.getheaders())
    connection.close()
    return response.status, response_headers, body


def test_internal_route_rejects_missing_token(bridge_server):
    port, _token = bridge_server
    with patch("content_domains.pixelle_talking.generate_clip") as generate:
        status, headers, raw = _post(port, _payload())
    assert status == 401
    assert headers["Content-Type"].startswith("application/json")
    assert json.loads(raw) == {
        "code": "internal_auth",
        "detail": "invalid internal Pixelle token",
        "retryable": False,
        "billed": False,
    }
    generate.assert_not_called()


def test_internal_route_streams_mp4_with_provider_headers(tmp_path, bridge_server):
    port, token = bridge_server
    output = tmp_path / "clip.mp4"
    output.write_bytes(b"mp4-result")
    result = {
        "video_id": "provider-video-1",
        "image_asset_id": "provider-image-1",
        "video_file": "video/clip.mp4",
    }
    with patch("content_domains.pixelle_talking.generate_clip", return_value=result), \
            patch("content_domains.pixelle_talking.resolve_video_path", return_value=output):
        status, headers, raw = _post(port, _payload(), token)
    assert status == 200
    assert headers["Content-Type"] == "video/mp4"
    assert headers["X-Provider-Video-Id"] == "provider-video-1"
    assert headers["X-Provider-Image-Asset-Id"] == "provider-image-1"
    assert raw == b"mp4-result"


def test_internal_route_reports_billed_failure_as_non_retryable(bridge_server):
    port, token = bridge_server
    with patch("content_domains.pixelle_talking.generate_clip",
               side_effect=video.HeyGenBilledError("created video-1")):
        status, _headers, raw = _post(port, _payload(), token)
    assert status == 502
    assert json.loads(raw) == {
        "code": "heygen_billed",
        "detail": "created video-1",
        "retryable": False,
        "billed": True,
    }


def test_shared_root_only_systemd_token_file_is_wired_without_token_logging():
    env_path = "/etc/huangque/pixelle-talking.env"
    for unit_name in ("huangque-content.service", "huangque-pixelle-video.service"):
        unit = (ROOT / "deploy/systemd" / unit_name).read_text(encoding="utf-8")
        assert "EnvironmentFile=%s" % env_path in unit

    setup = (ROOT / "deploy/setup-dev-server.sh").read_text(encoding="utf-8")
    assert env_path in setup
    assert "openssl rand -hex 48" in setup
    assert "chown root:root" in setup
    assert "chmod 600" in setup
    assert 'echo "$token"' not in setup.lower()
