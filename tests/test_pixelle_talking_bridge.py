# -*- coding: utf-8 -*-
import base64
import hashlib
import http.client
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import OrderedDict
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


def _converted_payload(image_bytes=IMAGE_BYTES, request_id="request-1"):
    payload = _payload(image_bytes=image_bytes, request_id=request_id)
    payload["image_data"] = _data_url("image/webp", image_bytes)
    payload["audio_data"] = _data_url("audio/wav", AUDIO_BYTES)
    return payload


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
        "detail": "provider video was created but delivery failed",
        "retryable": False,
        "billed": True,
    }


def test_bridge_uses_two_slots():
    bridge = _bridge()
    assert bridge.TALKING_CLIP_CONCURRENCY == 2


@pytest.fixture(autouse=True)
def reset_bridge_cache():
    bridge = _bridge()
    for name in ("_IMAGE_ASSET_CACHE", "_IMAGE_UPLOADS", "_IMAGE_HASH_LOCKS",
                 "_DEFERRED_CLEANUP"):
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

    monkeypatch.setattr(video, "upload_heygen_image_asset", lambda _image: "image-asset-1")

    def fake_generate(image_file, audio_file, resolution, ratio, motion,
                      image_asset_id=None, internal=False):
        image_path = tmp_path / image_file
        audio_path = tmp_path / audio_file
        assert image_path.read_bytes() == IMAGE_BYTES
        assert audio_path.read_bytes() == AUDIO_BYTES
        seen.extend([image_path, audio_path])
        assert (resolution, ratio, motion, image_asset_id, internal) == (
            "1080p", "9:16", "medium", "image-asset-1", True)
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

    monkeypatch.setattr(video, "upload_heygen_image_asset", lambda _image: "image-asset-1")
    monkeypatch.setattr(video, "generate_heygen_video", fail)
    with pytest.raises(RuntimeError, match="provider failed"):
        bridge.generate_clip(_payload())
    assert seen and all(not path.exists() for path in seen)


def test_input_cleanup_failure_cannot_mask_success_and_is_retried(
        tmp_path, monkeypatch, capsys):
    bridge = _bridge()
    monkeypatch.setattr(bridge, "OUT_DIR", tmp_path)
    monkeypatch.setattr(video, "upload_heygen_image_asset", lambda _image: "asset-1")
    monkeypatch.setattr(video, "generate_heygen_video", lambda *_args, **_kwargs: {
        "video_id": "video-1", "image_asset_id": "asset-1",
        "video_file": "video/result.mp4"})
    real_unlink = Path.unlink
    failed_path = []

    def fail_once(path, *args, **kwargs):
        if path.name.startswith(".pixelle-talking-") and not failed_path:
            failed_path.append(path)
            raise OSError("locked C:\\secret\\input.tmp")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_once)
    result = bridge.generate_clip(_payload(request_id="cleanup-success"))
    assert result["video_id"] == "video-1"
    assert failed_path[0].exists()
    assert list(bridge._DEFERRED_CLEANUP) == [str(failed_path[0])]
    logged = capsys.readouterr().out
    assert "request_id=cleanup-success" in logged
    assert "secret" not in logged

    bridge.generate_clip(_payload(request_id="cleanup-retry"))
    assert not failed_path[0].exists()
    assert bridge._DEFERRED_CLEANUP == OrderedDict()


def test_input_cleanup_failure_preserves_existing_billed_error(tmp_path, monkeypatch):
    bridge = _bridge()
    monkeypatch.setattr(bridge, "OUT_DIR", tmp_path)
    monkeypatch.setattr(video, "upload_heygen_image_asset", lambda _image: "asset-1")
    billed = video.HeyGenBilledError("video-1 created")
    monkeypatch.setattr(
        video, "generate_heygen_video",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(billed))
    real_unlink = Path.unlink

    def fail_inputs(path, *args, **kwargs):
        if path.name.startswith(".pixelle-talking-"):
            raise OSError("cleanup failed")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_inputs)
    with pytest.raises(video.HeyGenBilledError) as raised:
        bridge.generate_clip(_payload(request_id="cleanup-billed"))
    assert raised.value is billed
    assert bridge.classify_error(raised.value)["retryable"] is False
    assert bridge._DEFERRED_CLEANUP


def test_cleanup_logging_failure_cannot_mask_completed_result(tmp_path, monkeypatch):
    bridge = _bridge()
    monkeypatch.setattr(bridge, "OUT_DIR", tmp_path)
    monkeypatch.setattr(video, "upload_heygen_image_asset", lambda _image: "asset-1")
    monkeypatch.setattr(video, "generate_heygen_video", lambda *_args, **_kwargs: {
        "video_id": "video-1", "image_asset_id": "asset-1",
        "video_file": "video/result.mp4"})
    real_unlink = Path.unlink

    def fail_inputs(path, *args, **kwargs):
        if path.name.startswith(".pixelle-talking-"):
            raise OSError("cleanup failed")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_inputs)
    monkeypatch.setattr(
        bridge, "_log_failure",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("log closed")))
    result = bridge.generate_clip(_payload(request_id="cleanup-log-failed"))
    assert result["video_id"] == "video-1"


def test_deferred_cleanup_retention_is_bounded(tmp_path, monkeypatch):
    bridge = _bridge()
    monkeypatch.setattr(bridge, "DEFERRED_CLEANUP_MAX", 2)
    monkeypatch.setattr(Path, "unlink", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("locked")))
    paths = []
    for index in range(3):
        path = tmp_path / ("private-%d.tmp" % index)
        path.write_bytes(b"x")
        paths.append(path)
    bridge._best_effort_cleanup(paths, "bounded-cleanup")
    assert list(bridge._DEFERRED_CLEANUP) == [str(paths[1]), str(paths[2])]


def test_converted_derivatives_are_unique_private_and_cleaned(tmp_path, monkeypatch):
    bridge = _bridge()
    monkeypatch.setattr(bridge, "OUT_DIR", tmp_path)
    derivatives = []
    lock = threading.Lock()

    def convert(source, output_path=None):
        assert output_path is not None
        if os.name != "nt":
            assert (output_path.stat().st_mode & 0o777) == 0o600
        output_path.write_bytes(b"converted:" + source.read_bytes())
        with lock:
            derivatives.append(output_path)
        return output_path

    monkeypatch.setattr(video, "_ensure_heygen_image_jpg", convert)
    monkeypatch.setattr(video, "_ensure_heygen_audio_mp3", convert)
    monkeypatch.setattr(video, "upload_heygen_image_asset", lambda _image: "asset-shared")
    monkeypatch.setattr(video, "generate_heygen_video", lambda *_args, **_kwargs: {
        "video_id": "video", "image_asset_id": "asset-shared", "video_file": "video/result.mp4"})

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(bridge.generate_clip, [
            _converted_payload(request_id="a"),
            _converted_payload(request_id="b"),
        ]))

    assert len(derivatives) == 3
    assert len({path.name for path in derivatives}) == 3
    assert all(path.name.startswith(".pixelle-talking-") for path in derivatives)
    assert all(core._sensitive_output_file(path.name) for path in derivatives)
    assert all(not path.exists() for path in derivatives)


def test_concurrent_same_hash_coalesces_only_image_upload(tmp_path, monkeypatch):
    bridge = _bridge()
    monkeypatch.setattr(bridge, "OUT_DIR", tmp_path)
    upload_started = threading.Event()
    release_upload = threading.Event()
    uploads = []
    generations = []

    def upload(_image_file):
        uploads.append("upload")
        upload_started.set()
        assert release_upload.wait(2)
        return "image-asset-shared"

    def fake_generate(*_args, image_asset_id=None, **_kwargs):
        generations.append(image_asset_id)
        return {
            "video_id": "video-%d" % len(generations),
            "image_asset_id": image_asset_id,
            "video_file": "video/result.mp4",
        }

    monkeypatch.setattr(video, "upload_heygen_image_asset", upload)
    monkeypatch.setattr(video, "generate_heygen_video", fake_generate)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(bridge.generate_clip, _payload(request_id=value)) for value in ("a", "b")]
        assert upload_started.wait(2)
        time.sleep(0.05)
        assert generations == []
        release_upload.set()
        results = [future.result(timeout=2) for future in futures]
    assert len(results) == 2
    assert uploads == ["upload"]
    assert generations == ["image-asset-shared", "image-asset-shared"]
    assert bridge._IMAGE_UPLOADS == {}


def test_same_hash_requests_acquire_generation_slots_after_upload_release(
        tmp_path, monkeypatch):
    bridge = _bridge()
    monkeypatch.setattr(bridge, "OUT_DIR", tmp_path)
    upload_started = threading.Event()
    release_upload = threading.Event()
    two_generations = threading.Event()
    release_generation = threading.Event()
    active_generations = 0
    lock = threading.Lock()

    def upload(_image_file):
        upload_started.set()
        assert release_upload.wait(2)
        return "shared"

    def generate(*_args, image_asset_id=None, **_kwargs):
        nonlocal active_generations
        with lock:
            active_generations += 1
            if active_generations == 2:
                two_generations.set()
        assert release_generation.wait(2)
        with lock:
            active_generations -= 1
        return {"video_id": "v", "image_asset_id": image_asset_id,
                "video_file": "video/v.mp4"}

    monkeypatch.setattr(video, "upload_heygen_image_asset", upload)
    monkeypatch.setattr(video, "generate_heygen_video", generate)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(bridge.generate_clip, _payload(request_id="a"))
        assert upload_started.wait(2)
        second = pool.submit(bridge.generate_clip, _payload(request_id="b"))
        release_upload.set()
        assert two_generations.wait(2)
        release_generation.set()
        first.result(timeout=2)
        second.result(timeout=2)


def test_failed_image_upload_releases_waiters_and_cleans_coordination(tmp_path, monkeypatch):
    bridge = _bridge()
    monkeypatch.setattr(bridge, "OUT_DIR", tmp_path)
    upload_started = threading.Event()
    release_upload = threading.Event()

    def fail_upload(_image_file):
        upload_started.set()
        assert release_upload.wait(2)
        raise RuntimeError("upload failed")

    monkeypatch.setattr(video, "upload_heygen_image_asset", fail_upload)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(bridge.generate_clip, _payload(request_id="a"))
        assert upload_started.wait(2)
        second = pool.submit(bridge.generate_clip, _payload(request_id="b"))
        time.sleep(0.05)
        release_upload.set()
        for future in (first, second):
            with pytest.raises(RuntimeError, match="upload failed"):
                future.result(timeout=2)
    assert bridge._IMAGE_UPLOADS == {}


def test_same_hash_waiter_does_not_consume_generation_slot(tmp_path, monkeypatch):
    bridge = _bridge()
    monkeypatch.setattr(bridge, "OUT_DIR", tmp_path)
    upload_started = threading.Event()
    release_upload = threading.Event()
    unrelated_generated = threading.Event()
    upload_count = 0
    upload_lock = threading.Lock()

    def upload(_image_file):
        nonlocal upload_count
        with upload_lock:
            upload_count += 1
            call = upload_count
        if call == 1:
            upload_started.set()
            assert release_upload.wait(2)
            return "shared"
        return "unrelated"

    def fake_generate(*_args, image_asset_id=None, **_kwargs):
        if image_asset_id == "unrelated":
            unrelated_generated.set()
        return {"video_id": "v", "image_asset_id": image_asset_id, "video_file": "video/v.mp4"}

    monkeypatch.setattr(video, "upload_heygen_image_asset", upload)
    monkeypatch.setattr(video, "generate_heygen_video", fake_generate)
    unrelated = _payload(image_bytes=IMAGE_BYTES + b"other", request_id="other")
    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(bridge.generate_clip, _payload(request_id="a"))
        assert upload_started.wait(2)
        waiter = pool.submit(bridge.generate_clip, _payload(request_id="b"))
        third = pool.submit(bridge.generate_clip, unrelated)
        assert unrelated_generated.wait(1)
        release_upload.set()
        first.result(timeout=2)
        waiter.result(timeout=2)
        third.result(timeout=2)


def test_image_asset_remains_coalesced_when_first_generation_fails(tmp_path, monkeypatch):
    bridge = _bridge()
    monkeypatch.setattr(bridge, "OUT_DIR", tmp_path)
    uploads = []
    generation_calls = 0
    lock = threading.Lock()
    upload_started = threading.Event()
    release_upload = threading.Event()

    def generate(*_args, image_asset_id=None, **_kwargs):
        nonlocal generation_calls
        with lock:
            generation_calls += 1
            call = generation_calls
        if call == 1:
            raise RuntimeError("pre-video failure")
        return {"video_id": "v2", "image_asset_id": image_asset_id, "video_file": "video/v.mp4"}

    def upload(_image):
        uploads.append(1)
        upload_started.set()
        assert release_upload.wait(2)
        return "shared"

    monkeypatch.setattr(video, "upload_heygen_image_asset", upload)
    monkeypatch.setattr(video, "generate_heygen_video", generate)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(bridge.generate_clip, _payload(request_id="a"))
        assert upload_started.wait(2)
        second = pool.submit(bridge.generate_clip, _payload(request_id="b"))
        time.sleep(0.05)
        release_upload.set()
        futures = [first, second]
        outcomes = []
        for future in futures:
            try:
                outcomes.append(future.result(timeout=2))
            except RuntimeError:
                outcomes.append("failed")
    assert uploads == [1]
    assert "failed" in outcomes
    assert any(isinstance(value, dict) for value in outcomes)


def test_bridge_never_runs_more_than_two_conversion_upload_or_generation_calls(
        tmp_path, monkeypatch):
    bridge = _bridge()
    monkeypatch.setattr(bridge, "OUT_DIR", tmp_path)
    active = 0
    maximum = 0
    lock = threading.Lock()
    two_entered = threading.Event()
    three_entered = threading.Event()
    release = threading.Event()

    def bounded_step():
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
            if active == 2:
                two_entered.set()
            if active == 3:
                three_entered.set()
        assert release.wait(2)
        with lock:
            active -= 1

    def convert(source, output_path=None):
        bounded_step()
        output_path.write_bytes(b"converted:" + source.read_bytes())
        return output_path

    def upload(image):
        bounded_step()
        return Path(image).stem

    def fake_generate(*_args, image_asset_id=None, **_kwargs):
        bounded_step()
        return {
            "video_id": "video",
            "image_asset_id": image_asset_id,
            "video_file": "video/result.mp4",
        }

    monkeypatch.setattr(video, "_ensure_heygen_image_jpg", convert)
    monkeypatch.setattr(video, "_ensure_heygen_audio_mp3", convert)
    monkeypatch.setattr(video, "upload_heygen_image_asset", upload)
    monkeypatch.setattr(video, "generate_heygen_video", fake_generate)
    payloads = [
        _converted_payload(image_bytes=IMAGE_BYTES + bytes([i]), request_id=str(i))
        for i in range(3)
    ]
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(bridge.generate_clip, payload) for payload in payloads]
        assert two_entered.wait(2)
        time.sleep(0.05)
        assert not three_entered.is_set()
        assert maximum == 2
        release.set()
        [future.result(timeout=2) for future in futures]
    assert maximum == 2


def test_missing_result_image_asset_deletes_completed_mp4(tmp_path, monkeypatch):
    bridge = _bridge()
    monkeypatch.setattr(bridge, "OUT_DIR", tmp_path)
    output = tmp_path / "completed.mp4"
    output.write_bytes(b"mp4")
    monkeypatch.setattr(video, "upload_heygen_image_asset", lambda _image: "asset-1")
    monkeypatch.setattr(video, "generate_heygen_video", lambda *_args, **_kwargs: {
        "video_id": "video-1", "video_file": "video/completed.mp4"})
    monkeypatch.setattr(
        bridge, "_resolve_result_artifact",
        lambda value: output if value == "video/completed.mp4" else None)

    with pytest.raises(video.HeyGenBilledError):
        bridge.generate_clip(_payload(request_id="missing-asset"))
    assert not output.exists()


def test_image_asset_cache_is_ttl_lru_bounded_and_cleans_upload_state(monkeypatch):
    bridge = _bridge()
    clock = [100.0]
    monkeypatch.setattr(bridge.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(bridge, "IMAGE_ASSET_CACHE_MAX", 2)
    monkeypatch.setattr(bridge, "IMAGE_ASSET_CACHE_TTL_SECONDS", 10)
    bridge._IMAGE_ASSET_CACHE = OrderedDict()

    bridge._cache_image_asset("a", "asset-a")
    clock[0] += 1
    bridge._cache_image_asset("b", "asset-b")
    assert bridge._get_cached_image_asset("a") == "asset-a"
    bridge._cache_image_asset("c", "asset-c")
    assert bridge._get_cached_image_asset("b") is None
    assert list(bridge._IMAGE_ASSET_CACHE) == ["a", "c"]

    clock[0] += 11
    assert bridge._get_cached_image_asset("a") is None
    assert bridge._get_cached_image_asset("c") is None
    assert bridge._IMAGE_ASSET_CACHE == OrderedDict()
    assert bridge._IMAGE_UPLOADS == {}


def test_non_billed_failure_evicts_asset_but_billed_failure_preserves_it(tmp_path, monkeypatch):
    bridge = _bridge()
    monkeypatch.setattr(bridge, "OUT_DIR", tmp_path)
    image_hash = _payload()["image_sha256"]
    bridge._cache_image_asset(image_hash, "cached")
    monkeypatch.setattr(video, "upload_heygen_image_asset", lambda _image: pytest.fail("must use cache"))
    monkeypatch.setattr(video, "generate_heygen_video", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("pre-video")))
    with pytest.raises(RuntimeError):
        bridge.generate_clip(_payload())
    assert bridge._get_cached_image_asset(image_hash) is None

    bridge._cache_image_asset(image_hash, "cached")
    monkeypatch.setattr(video, "generate_heygen_video", lambda *_args, **_kwargs: (_ for _ in ()).throw(video.HeyGenBilledError("video created")))
    with pytest.raises(video.HeyGenBilledError):
        bridge.generate_clip(_payload())
    assert bridge._get_cached_image_asset(image_hash) == "cached"


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


def _get(port, path):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
    connection.request("GET", path)
    response = connection.getresponse()
    body = response.read()
    connection.close()
    return response.status, body


def test_internal_route_rejects_missing_token(bridge_server):
    port, _token = bridge_server
    with patch("content_domains.pixelle_talking.generate_clip") as generate:
        status, headers, raw = _post(port, _payload())
    assert status == 401
    assert headers["Content-Type"].startswith("application/json")
    assert json.loads(raw) == {
        "code": "internal_auth",
        "detail": "internal authentication failed",
        "retryable": False,
        "billed": False,
    }
    generate.assert_not_called()


def test_bridge_derivative_is_not_publicly_retrievable(
        tmp_path, bridge_server, monkeypatch):
    port, _token = bridge_server
    derivative = tmp_path / ".pixelle-talking-secret.jpg"
    derivative.write_bytes(b"private")
    monkeypatch.setattr(core, "OUT_DIR", tmp_path)
    status, _raw = _get(port, "/api/gen/file/%s" % derivative.name)
    assert status == 401


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
    for _ in range(50):
        if not output.exists():
            break
        time.sleep(0.01)
    assert not output.exists()


def test_internal_stream_deletes_video_and_cover_after_disconnect(tmp_path, monkeypatch):
    bridge = _bridge()
    video_path = tmp_path / "clip.mp4"
    cover_path = tmp_path / "cover.jpg"
    video_path.write_bytes(b"mp4-result")
    cover_path.write_bytes(b"cover")
    result = {
        "video_id": "provider-video-1",
        "image_asset_id": "provider-image-1",
        "video_file": "video/clip.mp4",
        "image_file": "image/cover.jpg",
    }

    class DisconnectingWriter:
        def write(self, _data):
            raise BrokenPipeError("client disconnected")

    class Handler:
        client_address = ("127.0.0.1", 12345)
        headers = {"X-HQ-Pixelle-Token": "secret"}
        wfile = DisconnectingWriter()

        def _json_body_strict(self):
            return _payload()

        def send_response(self, _status):
            pass

        def send_header(self, _name, _value):
            pass

        def end_headers(self):
            pass

    monkeypatch.setenv("PIXELLE_TALKING_INTERNAL_TOKEN", "secret")
    monkeypatch.setattr(bridge, "generate_clip", lambda _payload: result)
    monkeypatch.setattr(bridge, "resolve_video_path", lambda _result: video_path)
    monkeypatch.setattr(bridge, "_resolve_result_artifact", lambda value: {
        "video/clip.mp4": video_path,
        "image/cover.jpg": cover_path,
    }.get(value))
    with pytest.raises(BrokenPipeError):
        bridge.handle_http_request(Handler())
    assert not video_path.exists()
    assert not cover_path.exists()


def test_internal_route_reports_billed_failure_as_non_retryable(bridge_server):
    port, token = bridge_server
    with patch("content_domains.pixelle_talking.generate_clip",
               side_effect=video.HeyGenBilledError("created video-1")):
        status, _headers, raw = _post(port, _payload(), token)
    assert status == 502
    assert json.loads(raw) == {
        "code": "heygen_billed",
        "detail": "provider video was created but delivery failed",
        "retryable": False,
        "billed": True,
    }


def test_error_response_is_sanitized_and_server_log_has_redacted_request_id(
        bridge_server, capsys):
    port, token = bridge_server
    raw_detail = "provider failed at C:\\secrets\\token.txt https://provider.invalid/job/abc"
    with patch("content_domains.pixelle_talking.generate_clip",
               side_effect=RuntimeError(raw_detail)):
        status, _headers, raw = _post(port, _payload(request_id="request-redact"), token)
    body = json.loads(raw)
    assert status == 502
    assert body == {
        "code": "talking_bridge_error",
        "detail": "talking clip generation failed",
        "retryable": True,
        "billed": False,
    }
    logged = capsys.readouterr().out
    assert "request_id=request-redact" in logged
    assert "token.txt" not in logged
    assert "provider.invalid" not in logged
    assert "<path>" in logged
    assert "<url>" in logged


def test_shared_root_only_systemd_token_file_is_wired_without_token_logging():
    env_path = "/etc/huangque/pixelle-talking.env"
    for unit_name in ("huangque-content.service", "huangque-pixelle-video.service"):
        unit = (ROOT / "deploy/systemd" / unit_name).read_text(encoding="utf-8")
        assert "EnvironmentFile=%s" % env_path in unit

    setup = (ROOT / "deploy/setup-dev-server.sh").read_text(encoding="utf-8")
    assert env_path in setup
    assert "openssl rand -hex 48" in setup
    assert "set -eu" in setup
    assert "mktemp" in setup
    assert 'ln "$tmp" "$target"' in setup
    assert '${#token}' in setup
    assert '*[!0-9a-f]*' in setup
    assert 'wc -l < "$target"' in setup
    assert "chown root:root" in setup
    assert "chmod 600" in setup
    assert 'echo "$token"' not in setup.lower()


def _git_posix_shell():
    candidates = [shutil.which("dash"), shutil.which("sh")]
    git = shutil.which("git")
    if git:
        git_root = Path(git).resolve().parent.parent
        candidates.extend([
            git_root / "usr" / "bin" / "dash.exe",
            git_root / "bin" / "sh.exe",
        ])
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(candidate)
    raise AssertionError("POSIX sh/dash runtime not found")


def _run_token_block(shell, body, cwd, target, prefix=""):
    script = "PATH=/usr/bin:/bin:$PATH\nexport PATH\n" + prefix + body
    return subprocess.run(
        [shell, "-s", "--", target], input=script.encode("utf-8"),
        cwd=cwd, capture_output=True)


def test_pixelle_token_creation_is_atomic_no_clobber_and_fail_closed(tmp_path):
    shell = _git_posix_shell()
    setup = (ROOT / "deploy/setup-dev-server.sh").read_text(encoding="utf-8")
    marker_start = "# PIXELLE_TOKEN_CREATE_BEGIN"
    marker_end = "# PIXELLE_TOKEN_CREATE_END"
    body = setup.split(marker_start, 1)[1].split(marker_end, 1)[0]
    target = Path("pixelle-talking.env")
    created = _run_token_block(
        shell, body, tmp_path, str(target),
        prefix=('chmod() { printf "%s\\n" "$*" >> chmod.trace; '
                'command chmod "$@"; }\n'))
    assert created.returncode == 0, created.stderr
    target = tmp_path / target
    original = target.read_text(encoding="utf-8")
    assert re.fullmatch(r"PIXELLE_TALKING_INTERNAL_TOKEN=[0-9a-f]{96}\n", original)
    metadata = subprocess.run(
        [shell, "-c",
         'PATH=/usr/bin:/bin:$PATH; export PATH; stat -c "%a:%u" "$1"; id -u',
         "sh", target.name],
        cwd=tmp_path, text=True, capture_output=True, check=True).stdout.splitlines()
    mode, owner = metadata[0].split(":")
    if os.name == "nt":
        assert (tmp_path / "chmod.trace").read_text(encoding="utf-8").split()[0] == "600"
    else:
        assert mode == "600"
    assert owner == metadata[1]

    existing = _run_token_block(
        shell, body, tmp_path, target.name,
        prefix="openssl() { return 1; }\n")
    assert existing.returncode == 0, existing.stderr
    assert target.read_text(encoding="utf-8") == original

    target.write_text("PIXELLE_TALKING_INTERNAL_TOKEN=broken\n", encoding="utf-8")
    failed = _run_token_block(shell, body, tmp_path, target.name)
    assert failed.returncode != 0
    assert target.read_text(encoding="utf-8") == "PIXELLE_TALKING_INTERNAL_TOKEN=broken\n"

    target.unlink()
    random_failed = _run_token_block(
        shell, body, tmp_path, target.name,
        prefix="openssl() { return 1; }\n")
    assert random_failed.returncode != 0
    assert not target.exists()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda _index: _run_token_block(shell, body, tmp_path, target.name),
            range(2)))
    assert [result.returncode for result in results] == [0, 0]
    assert re.fullmatch(
        r"PIXELLE_TALKING_INTERNAL_TOKEN=[0-9a-f]{96}\n",
        target.read_text(encoding="utf-8"))
