# -*- coding: utf-8 -*-
import base64
import hashlib
import http.client
import importlib
import json
import os
import re
import shutil
import sqlite3
import stat
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
                      image_asset_id=None, internal=False,
                      internal_output_file=None):
        image_path = tmp_path / image_file
        audio_path = tmp_path / audio_file
        assert image_path.read_bytes() == IMAGE_BYTES
        assert audio_path.read_bytes() == AUDIO_BYTES
        seen.extend([image_path, audio_path])
        assert (resolution, ratio, motion, image_asset_id, internal) == (
            "1080p", "9:16", "medium", "image-asset-1", True)
        output_path = tmp_path / internal_output_file
        output_path.write_bytes(b"mp4")
        return {
            "video_id": "video-1",
            "image_asset_id": "image-asset-1",
            "video_file": internal_output_file,
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
        if path.parent.name == ".pixelle-talking-private" and not failed_path:
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
        if path.parent.name == ".pixelle-talking-private":
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
        if path.parent.name == ".pixelle-talking-private":
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
    monkeypatch.setattr(bridge, "OUT_DIR", tmp_path)
    monkeypatch.setattr(bridge, "DEFERRED_CLEANUP_MAX", 2)
    monkeypatch.setattr(Path, "unlink", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("locked")))
    paths = [bridge._write_private_temp(bytes([index]), ".tmp") for index in range(3)]
    bridge._best_effort_cleanup(paths, "bounded-cleanup")
    assert list(bridge._DEFERRED_CLEANUP) == [str(paths[1]), str(paths[2])]
    assert all(path.parent == bridge._private_dir() for path in paths)


def test_private_input_cleanup_survives_overflow_and_restart(tmp_path, monkeypatch):
    bridge = _bridge()
    monkeypatch.setattr(bridge, "OUT_DIR", tmp_path)
    monkeypatch.setattr(bridge, "DEFERRED_CLEANUP_MAX", 2)
    monkeypatch.setattr(bridge, "PRIVATE_SWEEP_MIN_AGE_SECONDS", 10)
    monkeypatch.setattr(bridge, "PRIVATE_SWEEP_BATCH", 2)
    paths = [bridge._write_private_temp(bytes([index]), ".tmp") for index in range(3)]
    real_unlink = Path.unlink

    monkeypatch.setattr(
        Path, "unlink",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("locked")))
    bridge._best_effort_cleanup(paths, "overflow")
    assert len(bridge._DEFERRED_CLEANUP) == 2
    assert all(path.exists() for path in paths)

    bridge._DEFERRED_CLEANUP.clear()
    old = time.time() - 20
    for path in paths:
        os.utime(path, (old, old))
    monkeypatch.setattr(Path, "unlink", real_unlink)
    assert bridge._sweep_private_cleanup("restart") == 2
    assert bridge._sweep_private_cleanup("restart") == 1
    assert all(not path.exists() for path in paths)


def test_cleanup_journal_bounds_work_without_directory_enumeration(
        tmp_path, monkeypatch):
    bridge = _bridge()
    monkeypatch.setattr(bridge, "OUT_DIR", tmp_path)
    monkeypatch.setattr(bridge, "PRIVATE_SWEEP_BATCH", 2)
    paths = [bridge._write_private_temp(bytes([index]), ".tmp")
             for index in range(5)]
    real_unlink = Path.unlink

    monkeypatch.setattr(
        Path, "unlink",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("locked")))
    bridge._best_effort_cleanup(paths, "journal-pending")
    monkeypatch.setattr(Path, "unlink", real_unlink)
    monkeypatch.setattr(
        Path, "iterdir",
        lambda *_args, **_kwargs: pytest.fail("cleanup must not enumerate the directory"))

    assert bridge._sweep_private_cleanup("journal-batch") == 2
    assert sum(path.exists() for path in paths) == 3
    with sqlite3.connect(bridge._cleanup_db_path()) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM bridge_artifacts").fetchone()[0] == 3


def test_cleanup_db_is_private_regular_file_before_sqlite_connect(
        tmp_path, monkeypatch):
    bridge = _bridge()
    monkeypatch.setattr(bridge, "OUT_DIR", tmp_path)
    real_connect = sqlite3.connect
    observed = []

    def guarded_connect(path, *args, **kwargs):
        database = Path(path)
        assert database.exists()
        assert not database.is_symlink()
        metadata = database.lstat()
        assert stat.S_ISREG(metadata.st_mode)
        if os.name != "nt":
            assert stat.S_IMODE(metadata.st_mode) == 0o600
        observed.append(database)
        return real_connect(path, *args, **kwargs)

    monkeypatch.setattr(bridge.sqlite3, "connect", guarded_connect)
    bridge._open_cleanup_db().close()

    assert observed == [bridge._cleanup_db_path()]


def test_cleanup_db_rejects_non_regular_file_before_sqlite_connect(
        tmp_path, monkeypatch):
    bridge = _bridge()
    monkeypatch.setattr(bridge, "OUT_DIR", tmp_path)
    database = bridge._cleanup_db_path()
    database.mkdir()

    with patch.object(bridge.sqlite3, "connect", wraps=sqlite3.connect) as connect:
        with pytest.raises(Exception):
            bridge._open_cleanup_db()

    connect.assert_not_called()


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not portable to Windows")
def test_cleanup_db_rejects_wrong_mode_before_sqlite_connect(
        tmp_path, monkeypatch):
    bridge = _bridge()
    monkeypatch.setattr(bridge, "OUT_DIR", tmp_path)
    database = bridge._cleanup_db_path()
    database.write_bytes(b"")
    database.chmod(0o644)

    with patch.object(bridge.sqlite3, "connect", wraps=sqlite3.connect) as connect:
        with pytest.raises(PermissionError):
            bridge._open_cleanup_db()

    connect.assert_not_called()


def test_cleanup_db_rejects_wrong_owner_before_sqlite_connect(
        tmp_path, monkeypatch):
    bridge = _bridge()
    monkeypatch.setattr(bridge, "OUT_DIR", tmp_path)
    database = bridge._cleanup_db_path()
    database.write_bytes(b"")
    database.chmod(0o600)
    real_fstat = os.fstat
    expected_uid = getattr(os, "geteuid", lambda: 1000)()
    monkeypatch.setattr(bridge.os, "geteuid", lambda: expected_uid, raising=False)

    def wrong_owner(descriptor):
        metadata = real_fstat(descriptor)
        values = {
            name: getattr(metadata, name)
            for name in dir(metadata)
            if name.startswith("st_")
        }
        values["st_uid"] = expected_uid + 1
        return type("WrongOwnerStat", (), values)()

    monkeypatch.setattr(bridge.os, "fstat", wrong_owner)
    with patch.object(bridge.sqlite3, "connect", wraps=sqlite3.connect) as connect:
        with pytest.raises(PermissionError):
            bridge._open_cleanup_db()

    connect.assert_not_called()


def test_cleanup_db_connect_boundary_swap_fails_before_provider_and_preserves_target(
        tmp_path, monkeypatch):
    bridge = _bridge()
    output_root = tmp_path / "output"
    monkeypatch.setattr(bridge, "OUT_DIR", output_root)
    database = bridge._cleanup_db_path()
    bridge._open_cleanup_db().close()

    external = tmp_path / "external.sqlite3"
    with sqlite3.connect(external) as connection:
        connection.execute("CREATE TABLE sentinel(value TEXT NOT NULL)")
        connection.execute("INSERT INTO sentinel(value) VALUES ('unchanged')")
    before = external.read_bytes()

    real_secure_descriptor = bridge._secure_cleanup_db_descriptor
    real_connect = sqlite3.connect
    real_close = os.close
    real_fstat = os.fstat
    descriptor = []
    descriptor_metadata = {}
    swapped = []

    def capture_descriptor(path):
        value = real_secure_descriptor(path)
        descriptor.append(value)
        descriptor_metadata[value] = real_fstat(value)
        return value

    def preserve_validated_identity(value):
        if value in descriptor_metadata:
            return descriptor_metadata[value]
        return real_fstat(value)

    def swap_at_connect(path, *args, **kwargs):
        if Path(path) == database and not swapped:
            real_close(descriptor[-1])
            database.unlink()
            shutil.copyfile(external, database)
            swapped.append(True)
        return real_connect(path, *args, **kwargs)

    def tolerate_closed_descriptor(value):
        try:
            real_close(value)
        except OSError:
            if value not in descriptor:
                raise

    monkeypatch.setattr(
        bridge, "_secure_cleanup_db_descriptor", capture_descriptor)
    monkeypatch.setattr(bridge.sqlite3, "connect", swap_at_connect)
    monkeypatch.setattr(bridge.os, "fstat", preserve_validated_identity)
    monkeypatch.setattr(bridge.os, "close", tolerate_closed_descriptor)
    provider_calls = []
    monkeypatch.setattr(
        video, "upload_heygen_image_asset",
        lambda _image: provider_calls.append("upload"))

    with pytest.raises(bridge.TalkingBridgeBackpressureError):
        bridge.generate_clip(_payload(request_id="cleanup-db-connect-swap"))

    assert swapped == [True]
    assert external.read_bytes() == before
    assert database.read_bytes() == before
    assert provider_calls == []


@pytest.mark.parametrize("tampered_path", ["absolute", "traversal"])
def test_cleanup_journal_discards_external_rows_without_touching_targets(
        tmp_path, monkeypatch, tampered_path):
    bridge = _bridge()
    output_root = tmp_path / "output"
    monkeypatch.setattr(bridge, "OUT_DIR", output_root)
    external = tmp_path / ("%s-sentinel.txt" % tampered_path)
    external.write_bytes(b"do-not-delete")
    database = bridge._cleanup_db_path()
    bridge._open_cleanup_db().close()
    if tampered_path == "absolute":
        row_path = str(external.resolve())
    else:
        row_path = "../../%s" % external.name
    with sqlite3.connect(database) as connection:
        connection.execute("""
            INSERT INTO bridge_artifacts(
                path, created_at, status, cleanup_requested_at, next_attempt_at
            ) VALUES (?, 0, 'pending', 0, 0)
        """, (row_path,))

    with pytest.raises(bridge.TalkingBridgeBackpressureError):
        bridge._sweep_private_cleanup("tampered-row")
    assert external.read_bytes() == b"do-not-delete"
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM bridge_artifacts WHERE path=?",
            (row_path,),
        ).fetchone()[0] == 0


def test_cleanup_journal_discards_database_and_sidecar_rows(
        tmp_path, monkeypatch):
    bridge = _bridge()
    monkeypatch.setattr(bridge, "OUT_DIR", tmp_path)
    database = bridge._cleanup_db_path()
    bridge._open_cleanup_db().close()
    reserved = [
        bridge.PRIVATE_CLEANUP_DB_NAME,
        bridge.PRIVATE_CLEANUP_DB_NAME + "-journal",
        bridge.PRIVATE_CLEANUP_DB_NAME + "-wal",
        bridge.PRIVATE_CLEANUP_DB_NAME + "-shm",
    ]
    with sqlite3.connect(database) as connection:
        connection.executemany("""
            INSERT INTO bridge_artifacts(
                path, created_at, status, cleanup_requested_at, next_attempt_at
            ) VALUES (?, 0, 'pending', 0, 0)
        """, [(name,) for name in reserved])

    with pytest.raises(bridge.CleanupJournalIntegrityError):
        bridge._journal_cleanup_candidates(time.time())
    assert database.is_file()
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM bridge_artifacts").fetchone()[0] == 0


@pytest.mark.parametrize("alias", [
    "CLEANUP.SQLITE3",
    "Cleanup.SQLite3-JOURNAL",
    "cleanup.sqlite3-WAL",
    "CLEANUP.SQLITE3-shm",
    "cleanup.sqlite3.",
    "cleanup.sqlite3 ",
    "cleanup.sqlite3-wal.",
    "cleanup.sqlite3-shm ",
    "artifact.tmp:alternate-stream",
    "CON",
    "con.txt",
    "NUL.log",
    "COM1.data",
    "LPT9",
])
def test_cleanup_journal_rejects_windows_filename_aliases(
        tmp_path, monkeypatch, alias):
    bridge = _bridge()
    monkeypatch.setattr(bridge, "OUT_DIR", tmp_path)
    database = bridge._cleanup_db_path()
    bridge._open_cleanup_db().close()
    with sqlite3.connect(database) as connection:
        connection.execute("""
            INSERT INTO bridge_artifacts(
                path, created_at, status, cleanup_requested_at, next_attempt_at
            ) VALUES (?, 0, 'pending', 0, 0)
        """, (alias,))

    with pytest.raises(bridge.CleanupJournalIntegrityError):
        bridge._journal_cleanup_candidates(time.time())

    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM bridge_artifacts WHERE path=?",
            (alias,),
        ).fetchone()[0] == 0


def test_tampered_cleanup_row_backpressures_before_artifact_or_provider_use(
        tmp_path, monkeypatch):
    bridge = _bridge()
    output_root = tmp_path / "output"
    monkeypatch.setattr(bridge, "OUT_DIR", output_root)
    external = tmp_path / "tampered-row-sentinel.txt"
    external.write_bytes(b"do-not-delete")
    database = bridge._cleanup_db_path()
    bridge._open_cleanup_db().close()
    row_path = str(external.resolve())
    with sqlite3.connect(database) as connection:
        connection.execute("""
            INSERT INTO bridge_artifacts(
                path, created_at, status, cleanup_requested_at, next_attempt_at
            ) VALUES (?, 0, 'pending', 0, 0)
        """, (row_path,))
    provider_calls = []
    monkeypatch.setattr(
        bridge, "_allocate_private_file",
        lambda *_args, **_kwargs: pytest.fail(
            "private artifacts must not be allocated after journal tampering"))
    monkeypatch.setattr(
        video, "upload_heygen_image_asset",
        lambda _image: provider_calls.append("upload"))

    with pytest.raises(bridge.TalkingBridgeBackpressureError):
        bridge.generate_clip(_payload(request_id="tampered-cleanup-row"))

    assert external.read_bytes() == b"do-not-delete"
    assert provider_calls == []
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM bridge_artifacts WHERE path=?",
            (row_path,),
        ).fetchone()[0] == 0


def test_symlinked_cleanup_db_fails_before_provider_and_preserves_target(
        tmp_path, monkeypatch):
    bridge = _bridge()
    output_root = tmp_path / "output"
    monkeypatch.setattr(bridge, "OUT_DIR", output_root)
    external = tmp_path / "external.sqlite3"
    with sqlite3.connect(external) as connection:
        connection.execute("CREATE TABLE sentinel(value TEXT NOT NULL)")
        connection.execute("INSERT INTO sentinel(value) VALUES ('unchanged')")
    before = external.read_bytes()
    database = bridge._cleanup_db_path()
    try:
        database.symlink_to(external)
    except OSError as error:
        pytest.skip("symlinks unavailable: %s" % error)
    provider_calls = []

    def provider_upload(_image):
        provider_calls.append("upload")
        raise AssertionError("provider work must not be reached")

    monkeypatch.setattr(video, "upload_heygen_image_asset", provider_upload)
    caught = None
    try:
        bridge.generate_clip(_payload(request_id="symlinked-cleanup-db"))
    except Exception as error:
        caught = error

    assert provider_calls == []
    assert external.read_bytes() == before
    assert isinstance(caught, bridge.TalkingBridgeBackpressureError)


def test_cleanup_journal_candidate_query_has_bounded_scan_cost(
        tmp_path, monkeypatch):
    bridge = _bridge()
    monkeypatch.setattr(bridge, "OUT_DIR", tmp_path)
    monkeypatch.setattr(bridge, "PRIVATE_SWEEP_BATCH", 2)
    database = bridge._cleanup_db_path()
    bridge._open_cleanup_db().close()
    with sqlite3.connect(database) as connection:
        connection.executemany("""
            INSERT INTO bridge_artifacts(
                path, created_at, status, cleanup_requested_at, next_attempt_at
            ) VALUES (?, ?, 'pending', ?, 0)
        """, [
            ("artifact-%05d.tmp" % index, float(index), float(index))
            for index in range(2000)
        ])

    real_connect = sqlite3.connect
    steps = []

    class CountingConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if "SELECT path FROM bridge_artifacts" not in sql:
                return super().execute(sql, parameters)
            count = [0]
            self.set_progress_handler(
                lambda: count.__setitem__(0, count[0] + 1) or 0, 1)
            try:
                return super().execute(sql, parameters)
            finally:
                self.set_progress_handler(None, 0)
                steps.append(count[0])

    monkeypatch.setattr(
        bridge.sqlite3, "connect",
        lambda *args, **kwargs: real_connect(
            *args, factory=CountingConnection, **kwargs))

    assert len(bridge._journal_cleanup_candidates(time.time())) == 2
    assert steps and steps[0] < 500


def test_cleanup_journal_serializes_concurrent_passes(tmp_path, monkeypatch):
    bridge = _bridge()
    monkeypatch.setattr(bridge, "OUT_DIR", tmp_path)
    path = bridge._write_private_temp(b"x", ".tmp")
    real_unlink = Path.unlink
    monkeypatch.setattr(
        Path, "unlink",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("locked")))
    bridge._best_effort_cleanup([path], "journal-pending")

    entered = threading.Event()
    release = threading.Event()
    attempts = []

    def blocking_unlink(target, *args, **kwargs):
        attempts.append(target)
        entered.set()
        assert release.wait(2)
        return real_unlink(target, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", blocking_unlink)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(bridge._sweep_private_cleanup, "first-pass")
        assert entered.wait(2)
        second = pool.submit(bridge._sweep_private_cleanup, "second-pass")
        assert second.result(timeout=2) == 0
        release.set()
        assert first.result(timeout=2) == 1

    assert attempts == [path]
    assert not path.exists()


def test_private_backlog_applies_backpressure_before_provider_use(
        tmp_path, monkeypatch):
    bridge = _bridge()
    monkeypatch.setattr(bridge, "OUT_DIR", tmp_path)
    monkeypatch.setattr(bridge, "PRIVATE_BACKLOG_MAX", 2)
    paths = [bridge._write_private_temp(bytes([index]), ".tmp")
             for index in range(2)]
    provider_calls = []

    monkeypatch.setattr(
        Path, "unlink",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("locked")))
    bridge._best_effort_cleanup(paths, "stuck-backlog")
    monkeypatch.setattr(
        video, "upload_heygen_image_asset",
        lambda _image: provider_calls.append("upload"))

    with pytest.raises(bridge.TalkingBridgeBackpressureError):
        bridge.generate_clip(_payload(request_id="backpressure"))

    assert provider_calls == []
    error = bridge.classify_error(bridge.TalkingBridgeBackpressureError("full"))
    assert error == {
        "code": "talking_bridge_backpressure",
        "detail": "talking clip bridge is temporarily unavailable",
        "retryable": True,
        "billed": False,
    }


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
    assert all(path.parent == bridge._private_dir() for path in derivatives)
    assert all(core._sensitive_output_file(
        path.relative_to(tmp_path).as_posix()) for path in derivatives)
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


def test_provider_output_is_created_directly_in_registered_private_namespace(
        tmp_path, monkeypatch):
    bridge = _bridge()
    monkeypatch.setattr(bridge, "OUT_DIR", tmp_path)
    monkeypatch.setattr(core, "OUT_DIR", tmp_path)
    monkeypatch.setattr(video, "OUT_DIR", tmp_path)
    monkeypatch.setattr(video, "upload_heygen_image_asset", lambda _image: "asset-1")
    monkeypatch.setattr(
        os, "replace",
        lambda *_args, **_kwargs: pytest.fail(
            "private provider output must not require adoption"))

    def generate(*_args, internal_output_file=None, **_kwargs):
        assert internal_output_file is not None
        output = tmp_path / internal_output_file
        assert output.parent == bridge._private_dir()
        assert output.exists()
        with sqlite3.connect(bridge._cleanup_db_path()) as connection:
            row = connection.execute(
                "SELECT status FROM bridge_artifacts WHERE path=?",
                (output.name,),
            ).fetchone()
        assert row == ("active",)
        output.write_bytes(b"mp4")
        assert not (tmp_path / "video").exists()
        return {
            "video_id": "video-1",
            "image_asset_id": "asset-1",
            "video_file": internal_output_file,
        }

    monkeypatch.setattr(video, "generate_heygen_video", generate)
    result = bridge.generate_clip(_payload(request_id="private-provider-output"))

    output = tmp_path / result["video_file"]
    assert output.parent == bridge._private_dir()
    assert output.read_bytes() == b"mp4"
    assert core._sensitive_output_file(result["video_file"])
    bridge._cleanup_result_artifacts(result, request_id="private-provider-output")
    assert not output.exists()
    with sqlite3.connect(bridge._cleanup_db_path()) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM bridge_artifacts").fetchone()[0] == 0


def test_adopted_mp4_cleanup_survives_simulated_restart(tmp_path, monkeypatch):
    bridge = _bridge()
    monkeypatch.setattr(bridge, "OUT_DIR", tmp_path)
    monkeypatch.setattr(core, "OUT_DIR", tmp_path)
    monkeypatch.setattr(bridge, "DEFERRED_CLEANUP_MAX", 2)
    monkeypatch.setattr(bridge, "PRIVATE_SWEEP_MIN_AGE_SECONDS", 10)
    provider_dir = tmp_path / "video"
    provider_dir.mkdir()
    provider_outputs = []

    def generate(*_args, **_kwargs):
        provider_output = provider_dir / ("provider-%d.mp4" % len(provider_outputs))
        provider_output.write_bytes(b"mp4")
        provider_outputs.append(provider_output)
        return {
            "video_id": "video-%d" % len(provider_outputs),
            "image_asset_id": "asset-1",
            "video_file": provider_output.relative_to(tmp_path).as_posix(),
        }

    monkeypatch.setattr(video, "upload_heygen_image_asset", lambda _image: "asset-1")
    monkeypatch.setattr(video, "generate_heygen_video", generate)

    results = [bridge.generate_clip(_payload(request_id="adopt-result-%d" % index))
               for index in range(3)]
    adopted = [tmp_path / result["video_file"] for result in results]
    assert all(path.parent == bridge._private_dir() for path in adopted)
    assert all(path.name.startswith("result-video-") for path in adopted)
    assert all(path.read_bytes() == b"mp4" for path in adopted)
    assert all(not path.exists() for path in provider_outputs)

    real_unlink = Path.unlink
    monkeypatch.setattr(
        Path, "unlink",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("locked")))
    for result in results:
        bridge._cleanup_result_artifacts(result, request_id="adopt-result")
    assert len(bridge._DEFERRED_CLEANUP) == 2
    assert all(path.exists() for path in adopted)
    bridge._DEFERRED_CLEANUP.clear()

    old = time.time() - 20
    for path in adopted:
        os.utime(path, (old, old))
    monkeypatch.setattr(Path, "unlink", real_unlink)
    assert bridge._sweep_private_cleanup("restart") == 3
    assert all(not path.exists() for path in adopted)


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
    private_dir = tmp_path / ".pixelle-talking-private"
    private_dir.mkdir()
    derivative = private_dir / "derivative-secret.jpg"
    derivative.write_bytes(b"private")
    monkeypatch.setattr(core, "OUT_DIR", tmp_path)
    status, _raw = _get(
        port, "/api/gen/file/.pixelle-talking-private/%s" % derivative.name)
    assert status == 404
    database = private_dir / "cleanup.sqlite3"
    database.write_bytes(b"private-db")
    status, _raw = _get(
        port, "/api/gen/file/.pixelle-talking-private/cleanup.sqlite3")
    assert status == 404


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
    monkeypatch.setattr(
        bridge, "_log_failure",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("logger failed")))
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


def test_handler_logger_failure_preserves_billed_generation_response(bridge_server):
    port, token = bridge_server
    with patch("content_domains.pixelle_talking.generate_clip",
               side_effect=video.HeyGenBilledError("created video-1")), \
            patch("content_domains.pixelle_talking._log_failure",
                  side_effect=OSError("logger failed")):
        status, _headers, raw = _post(port, _payload(), token)
    assert status == 502
    assert json.loads(raw) == {
        "code": "heygen_billed",
        "detail": "provider video was created but delivery failed",
        "retryable": False,
        "billed": True,
    }


def test_handler_logger_failure_cannot_skip_billed_header_cleanup(
        tmp_path, bridge_server):
    port, token = bridge_server
    output = tmp_path / "clip.mp4"
    output.write_bytes(b"mp4")
    result = {
        "video_id": "provider-video-1",
        "image_asset_id": "",
        "video_file": "video/clip.mp4",
    }
    with patch("content_domains.pixelle_talking.generate_clip", return_value=result), \
            patch("content_domains.pixelle_talking.resolve_video_path",
                  return_value=output), \
            patch("content_domains.pixelle_talking._resolve_result_artifact",
                  return_value=output), \
            patch("content_domains.pixelle_talking._log_failure",
                  side_effect=OSError("logger failed")):
        status, _headers, raw = _post(port, _payload(), token)
    assert status == 502
    assert json.loads(raw)["code"] == "heygen_billed"
    assert json.loads(raw)["retryable"] is False
    assert not output.exists()


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
