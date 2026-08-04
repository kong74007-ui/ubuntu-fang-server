import json
import re
import tempfile
import threading
import unittest
from pathlib import Path

from server.content_domains.ai_edit_v3.service import (
    CapacityDecision,
    EditV3Service,
    ServiceError,
    UploadObservation,
    build_object_key,
)
from server.content_domains.ai_edit_v3.feature import (
    CapabilityItem,
    CapabilityReport,
)
from server.content_domains.ai_edit_v3.store import StoreConflictError, V3Store


MIB = 1024 * 1024
GIB = 1024 * MIB


PART_NAMES = (
    "base_task",
    "duration_tier",
    "tts_ceiling",
    "qwen_ceiling",
    "image_ceiling",
    "bgm_sfx_ceiling",
    "render_complexity",
    "one_repair_reserve",
)


def pricing_parameters():
    parts = {}
    for name in PART_NAMES:
        part = {"ceiling_quantity": 1, "min_rate": 1, "max_rate": 2}
        if name == "tts_ceiling":
            part.update({"ceiling_quantity": 100, "unit_size": 100})
        parts[name] = part
    return {"parts": parts}


def ready_capability_report(**overrides):
    values = {
        "items": {
            "common": CapabilityItem(
                "configured_and_wired", "capability_ready", "ready"
            )
        },
        "runtime_versions": {"python": "3.12"},
        "allows_existing_reads": True,
        "accepts_uploads": True,
        "accepts_new_jobs": True,
    }
    values.update(overrides)
    return CapabilityReport(**values)


class FakeUploadObjectStore:
    def __init__(self):
        self.presign_calls = []
        self.head_calls = []
        self.delete_calls = []
        self.heads = {}

    def presign_put(self, key, content_type, expires=900):
        self.presign_calls.append((key, content_type, expires))
        return f"https://upload.invalid/put?opaque={len(self.presign_calls)}"

    def head_object(self, key):
        self.head_calls.append(key)
        return dict(self.heads[key])

    def delete_object(self, key):
        self.delete_calls.append(key)


class FakeUploadInspector:
    def __init__(self):
        self.calls = []
        self.observations = {}

    def inspect(self, key, *, upload_type, head):
        self.calls.append((key, upload_type, dict(head)))
        return self.observations[key]


class SequentialIds:
    def __init__(self):
        self.value = 0

    def __call__(self, prefix):
        self.value += 1
        return f"{prefix}-{self.value:04d}"


class FakeCapacityGate:
    def __init__(self):
        self.calls = []
        self.decision = CapacityDecision(
            accepted=True,
            queue_slots=1,
            required_temp_bytes=1024,
            retry_after=None,
        )

    def check(self, normalized_request):
        self.calls.append(dict(normalized_request))
        return self.decision


class FakeSourceCatalog:
    def __init__(self):
        self.platform = {}
        self.audio = {}
        self.voices = {}
        self.templates = {}
        self.platform_rows = []
        self.audio_rows = []
        self.voice_rows = []
        self.template_rows = []
        self.calls = []

    def resolve_platform_asset(self, owner, asset_id):
        self.calls.append(("platform", owner, asset_id))
        return self.platform.get((owner, asset_id))

    def resolve_audio_asset(self, owner, asset_id):
        self.calls.append(("audio", owner, asset_id))
        return self.audio.get((owner, asset_id))

    def resolve_voice(self, owner, voice_id):
        self.calls.append(("voice", owner, voice_id))
        return self.voices.get((owner, voice_id))

    def resolve_template(self, template_id, ratio):
        self.calls.append(("template", template_id, ratio))
        return self.templates.get((template_id, ratio))

    def list_platform_assets(self, owner):
        self.calls.append(("platform-list", owner))
        return list(self.platform_rows)

    def list_audio_assets(self, owner):
        self.calls.append(("audio-list", owner))
        return list(self.audio_rows)

    def list_voices(self, owner):
        self.calls.append(("voice-list", owner))
        return list(self.voice_rows)

    def list_templates(self, owner):
        self.calls.append(("template-list", owner))
        return list(self.template_rows)


class V3ObjectKeyTests(unittest.TestCase):
    def test_object_key_uses_environment_owner_hmac_job_and_scope(self):
        key = build_object_key(
            environment="test",
            owner="alice",
            job_id="019f-test-job",
            scope="materials/uploaded",
            filename="Image Final.WEBP",
            owner_hmac_secret=b"test-only-secret",
        )

        self.assertRegex(
            key,
            r"^test/ai-edit-v3/[0-9a-f]{24}/019f-test-job/"
            r"materials/uploaded/[a-z0-9._-]+$",
        )
        self.assertNotIn("alice", key)
        self.assertTrue(key.endswith("/image-final.webp"))

    def test_object_key_is_deterministic_and_owner_bound(self):
        arguments = {
            "environment": "production",
            "job_id": "upload-123",
            "scope": "source",
            "filename": "voice.M4A",
            "owner_hmac_secret": b"another-test-only-secret",
        }

        alice = build_object_key(owner="alice", **arguments)
        replay = build_object_key(owner="alice", **arguments)
        bob = build_object_key(owner="bob", **arguments)

        self.assertEqual(replay, alice)
        self.assertNotEqual(bob, alice)

    def test_object_key_rejects_unsafe_or_unfrozen_inputs(self):
        valid = {
            "environment": "test",
            "owner": "alice",
            "job_id": "upload-123",
            "scope": "source",
            "filename": "clip.mp4",
            "owner_hmac_secret": b"test-only-secret",
        }
        invalid = (
            ("environment", "staging"),
            ("owner", ""),
            ("owner", "alice\ud800"),
            ("job_id", "../job"),
            ("job_id", "job/child"),
            ("scope", "render"),
            ("filename", "../clip.mp4"),
            ("filename", "folder/clip.mp4"),
            ("filename", "folder\\clip.mp4"),
            ("filename", "C:clip.mp4"),
            ("filename", "clip.mp4?token=secret"),
            ("filename", "clip.mp4#fragment"),
            ("filename", "clip\x00.mp4"),
            ("filename", "x" * 256 + ".mp4"),
            ("owner_hmac_secret", b"short"),
        )

        for field, value in invalid:
            with self.subTest(field=field, value=repr(value)):
                arguments = dict(valid)
                arguments[field] = value
                with self.assertRaises(ServiceError):
                    build_object_key(**arguments)

    def test_object_key_has_only_frozen_safe_characters(self):
        key = build_object_key(
            environment="test",
            owner="owner-with-unicode-鹅",
            job_id="job_ABC-123",
            scope="materials/uploaded",
            filename="  封面 图 01.PNG  ",
            owner_hmac_secret=b"long-enough-test-secret",
        )

        self.assertIsNotNone(re.fullmatch(r"[a-zA-Z0-9_./-]+", key))
        self.assertTrue(key.endswith("/01.png"))


class V3UploadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        self.v2 = root / "ai_edit_v2.db"
        self.v2.write_bytes(b"V2 marker; do not open")
        self.store = V3Store(
            root / "ai_edit_v3.db",
            v2_db_path=self.v2,
            environment="test",
        )
        self.objects = FakeUploadObjectStore()
        self.inspector = FakeUploadInspector()
        self.ids = SequentialIds()
        self.service = EditV3Service(
            self.store,
            object_store=self.objects,
            upload_inspector=self.inspector,
            owner_hmac_secret=b"task-nine-test-secret",
            enabled=True,
            id_factory=self.ids,
            capability_report=ready_capability_report(),
        )

    def create_upload(
        self,
        upload_type="material_image",
        *,
        filename="image.png",
        content_type="image/png",
        size_bytes=100,
        now=1_000,
        owner="alice",
    ):
        return self.service.create_upload(
            owner,
            {
                "upload_type": upload_type,
                "filename": filename,
                "content_type": content_type,
                "size_bytes": size_bytes,
            },
            now=now,
        )

    def prepare_completion(
        self,
        upload,
        *,
        mime_type="image/png",
        media_kind="image",
        size_bytes=100,
        duration_ms=None,
        width=100,
        height=100,
        frame_rate=None,
    ):
        key = self.objects.presign_calls[-1][0]
        self.objects.heads[key] = {
            "content_type": "application/octet-stream",
            "size_bytes": size_bytes,
            "etag": "etag-safe-1",
        }
        self.inspector.observations[key] = UploadObservation(
            mime_type=mime_type,
            media_kind=media_kind,
            size_bytes=size_bytes,
            sha256="a" * 64,
            duration_ms=duration_ms,
            width=width,
            height=height,
            frame_rate=frame_rate,
            probe_evidence={"codec": mime_type.split("/")[-1]},
        )
        return key

    def test_upload_intent_is_persisted_before_presign_and_url_is_not_stored(self):
        observed_rows = []

        def presign(key, content_type, expires=900):
            observed_rows.append(self.store.get_upload_for_owner("alice", "upload-0001"))
            return "https://upload.invalid/private?signature=never-persist"

        self.objects.presign_put = presign

        result = self.create_upload()

        self.assertEqual(result["upload_id"], "upload-0001")
        self.assertEqual(result["put_url"], "https://upload.invalid/private?signature=never-persist")
        self.assertEqual(observed_rows[0]["status"], "pending")
        stored = self.store.get_upload_for_owner("alice", result["upload_id"])
        self.assertNotIn("https://", repr(stored))
        self.assertNotIn("signature", repr(stored))

    def test_capabilities_fail_closed_without_full_runtime_preflight_report(self):
        service = EditV3Service(
            self.store,
            object_store=self.objects,
            upload_inspector=self.inspector,
            owner_hmac_secret=b"task-nine-test-secret",
            enabled=True,
            id_factory=SequentialIds(),
            capability_report=ready_capability_report(
                items={
                    "common": CapabilityItem(
                        "missing_or_unavailable",
                        "schema_hash_mismatch",
                        "not ready",
                    )
                },
                accepts_uploads=False,
                accepts_new_jobs=False,
            ),
        )

        capabilities = service.get_capabilities("alice")

        self.assertTrue(capabilities["allows_existing_reads"])
        self.assertFalse(capabilities["accepts_uploads"])
        self.assertFalse(capabilities["accepts_new_jobs"])
        with self.assertRaises(ServiceError) as context:
            service.create_upload(
                "alice",
                {
                    "upload_type": "material_image",
                    "filename": "image.png",
                    "content_type": "image/png",
                    "size_bytes": 1,
                },
                now=1_000,
            )
        self.assertEqual(context.exception.status, 503)

    def test_foreign_completion_is_404_without_external_calls(self):
        upload = self.create_upload(owner="bob")

        with self.assertRaises(ServiceError) as context:
            self.service.complete_upload("alice", upload["upload_id"], now=1_001)

        self.assertEqual(context.exception.status, 404)
        self.assertEqual(context.exception.error_code, "not_found")
        self.assertEqual(self.objects.head_calls, [])
        self.assertEqual(self.inspector.calls, [])
        self.assertEqual(self.objects.delete_calls, [])

    def test_completion_uses_authoritative_observation_and_replay_has_no_external_work(self):
        upload = self.create_upload(content_type="image/jpeg", size_bytes=1)
        self.prepare_completion(
            upload,
            mime_type="image/webp",
            size_bytes=321,
            width=80,
            height=60,
        )

        completed = self.service.complete_upload("alice", upload["upload_id"], now=1_001)
        replay = self.service.complete_upload("alice", upload["upload_id"], now=1_999)

        self.assertEqual(replay, completed)
        self.assertEqual(completed["mime_type"], "image/webp")
        self.assertEqual(completed["size_bytes"], 321)
        self.assertEqual(len(self.objects.head_calls), 1)
        self.assertEqual(len(self.inspector.calls), 1)
        stored = self.store.get_upload_for_owner("alice", upload["upload_id"])
        self.assertEqual(stored["declared_mime"], "image/jpeg")
        self.assertEqual(stored["declared_size"], 1)
        self.assertEqual(stored["observed_mime"], "image/webp")
        self.assertEqual(stored["observed_size"], 321)

    def test_material_image_size_is_enforced_at_exact_plus_one_boundary(self):
        too_large = self.create_upload(size_bytes=25 * MIB)
        self.prepare_completion(too_large, size_bytes=25 * MIB + 1)
        with self.assertRaisesRegex(ServiceError, "input_image_size_exceeded"):
            self.service.complete_upload("alice", too_large["upload_id"], now=1_001)

    def test_completion_rejects_unsupported_image_and_expired_upload(self):
        unsupported = self.create_upload()
        self.prepare_completion(unsupported, mime_type="image/gif")
        with self.assertRaisesRegex(ServiceError, "input_image_type_unsupported"):
            self.service.complete_upload("alice", unsupported["upload_id"], now=1_001)

        expired = self.create_upload(filename="expired.png", now=2_000)
        self.prepare_completion(expired)
        with self.assertRaisesRegex(ServiceError, "upload_expired"):
            self.service.complete_upload("alice", expired["upload_id"], now=902_000)
        self.assertEqual(len(self.objects.head_calls), 1)

    def test_main_media_duration_and_kind_are_authoritative(self):
        video = self.create_upload(
            "main_video",
            filename="main.mp4",
            content_type="video/mp4",
        )
        self.prepare_completion(
            video,
            mime_type="video/mp4",
            media_kind="audio",
            duration_ms=3_000,
            width=None,
            height=None,
        )
        with self.assertRaisesRegex(ServiceError, "input_media_kind_mismatch"):
            self.service.complete_upload("alice", video["upload_id"], now=1_001)

        audio = self.create_upload(
            "main_audio",
            filename="voice.m4a",
            content_type="audio/mp4",
            now=2_000,
        )
        self.prepare_completion(
            audio,
            mime_type="audio/mp4",
            media_kind="audio",
            duration_ms=600_001,
            width=None,
            height=None,
        )
        with self.assertRaisesRegex(ServiceError, "input_duration_invalid"):
            self.service.complete_upload("alice", audio["upload_id"], now=2_001)

    def test_probe_evidence_uses_a_frozen_allowlist_and_rejects_paths(self):
        unsafe_evidence = (
            {"path": "/srv/private/probe.json"},
            {"file_path": "relative/probe.json"},
            {"cos_key": "production/ai-edit-v3/private/object"},
            {"codec": "/absolute/posix/value"},
            {"codec": "C:\\private\\probe.json"},
            {"codec": "\\\\server\\share\\probe.json"},
            {"codec": "production/ai-edit-v3/private/object"},
            {"format_name": "../private/probe.json"},
            {"codec_long_name": "..\\private\\probe.json"},
            {"container": "relative/private/probe.json"},
        )
        for index, evidence in enumerate(unsafe_evidence):
            with self.subTest(evidence=evidence):
                upload = self.create_upload(
                    filename=f"unsafe-{index}.png", now=10_000 + index * 10
                )
                key = self.prepare_completion(upload)
                observation = self.inspector.observations[key]
                self.inspector.observations[key] = UploadObservation(
                    mime_type=observation.mime_type,
                    media_kind=observation.media_kind,
                    size_bytes=observation.size_bytes,
                    sha256=observation.sha256,
                    width=observation.width,
                    height=observation.height,
                    probe_evidence=evidence,
                )
                with self.assertRaisesRegex(ServiceError, "input_probe_invalid"):
                    self.service.complete_upload(
                        "alice", upload["upload_id"], now=10_001 + index * 10
                    )

        safe = self.create_upload(filename="safe.png", now=20_000)
        key = self.prepare_completion(safe)
        observation = self.inspector.observations[key]
        self.inspector.observations[key] = UploadObservation(
            mime_type=observation.mime_type,
            media_kind=observation.media_kind,
            size_bytes=observation.size_bytes,
            sha256=observation.sha256,
            width=observation.width,
            height=observation.height,
            probe_evidence={
                "codec": "h264",
                "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
                "frame_rate": "30000/1001",
                "channel_layout": "5.1(side)",
                "bit_rate": 8000,
                "sample_rate": 48000,
                "channels": 2,
            },
        )
        self.service.complete_upload("alice", safe["upload_id"], now=20_001)
        stored = self.store.get_upload_for_owner("alice", safe["upload_id"])
        self.assertEqual(
            json.loads(stored["probe_json"]),
            {
                "bit_rate": 8000,
                "channel_layout": "5.1(side)",
                "channels": 2,
                "codec": "h264",
                "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
                "frame_rate": "30000/1001",
                "sample_rate": 48000,
            },
        )

    def test_completed_upload_replay_bypasses_runtime_readiness_but_revalidates_storage(self):
        completed_upload = self.create_upload(filename="replay.png", now=21_000)
        self.prepare_completion(completed_upload)
        completed = self.service.complete_upload(
            "alice", completed_upload["upload_id"], now=21_001
        )

        corrupt_upload = self.create_upload(filename="corrupt.png", now=21_010)
        self.prepare_completion(corrupt_upload)
        self.service.complete_upload("alice", corrupt_upload["upload_id"], now=21_011)

        corrupt_video = self.create_upload(
            "main_video",
            filename="corrupt-video.mp4",
            content_type="video/mp4",
            now=21_012,
        )
        self.prepare_completion(
            corrupt_video,
            mime_type="video/mp4",
            media_kind="video",
            duration_ms=3_000,
            width=1920,
            height=1080,
            frame_rate=30,
        )
        self.service.complete_upload("alice", corrupt_video["upload_id"], now=21_013)

        pending_upload = self.create_upload(filename="pending.png", now=21_020)
        self.prepare_completion(pending_upload)
        self.store._write(
            lambda connection: connection.execute(
                "UPDATE edit_v3_uploads SET probe_json=? WHERE upload_id=?",
                ('{"codec":"../private/probe.json"}', corrupt_upload["upload_id"]),
            )
        )
        self.store._write(
            lambda connection: connection.execute(
                "UPDATE edit_v3_uploads SET probe_json=? WHERE upload_id=?",
                ('{"codec":"h264","frame_rate":120}', corrupt_video["upload_id"]),
            )
        )

        self.service.object_store = None
        self.service.upload_inspector = None
        self.service.owner_hmac_secret = b"weak"
        self.service._capability_report_source = ready_capability_report(
            accepts_uploads=False,
            accepts_new_jobs=False,
        )

        replay = None
        replay_error = None
        try:
            replay = self.service.complete_upload(
                "alice", completed_upload["upload_id"], now=21_100
            )
        except ServiceError as exc:
            replay_error = exc
        self.assertIsNone(
            replay_error,
            f"completed replay unexpectedly required runtime readiness: {replay_error}",
        )
        self.assertEqual(replay, completed)

        with self.assertRaises(ServiceError) as corrupt:
            self.service.complete_upload(
                "alice", corrupt_upload["upload_id"], now=21_101
            )
        self.assertEqual(corrupt.exception.error_code, "upload_storage_failed")
        self.assertEqual(corrupt.exception.status, 503)

        with self.assertRaises(ServiceError) as corrupt_frame_rate:
            self.service.complete_upload(
                "alice", corrupt_video["upload_id"], now=21_101
            )
        self.assertEqual(
            corrupt_frame_rate.exception.error_code, "upload_storage_failed"
        )
        self.assertEqual(corrupt_frame_rate.exception.status, 503)

        with self.assertRaises(ServiceError) as pending:
            self.service.complete_upload(
                "alice", pending_upload["upload_id"], now=21_102
            )
        self.assertEqual(pending.exception.error_code, "upload_capability_unavailable")
        self.assertEqual(pending.exception.status, 503)

        with self.assertRaises(ServiceError) as foreign:
            self.service.complete_upload(
                "bob", completed_upload["upload_id"], now=21_103
            )
        self.assertEqual(foreign.exception.error_code, "not_found")
        self.assertEqual(foreign.exception.status, 404)

        self.service.enabled = False
        with self.assertRaises(ServiceError) as disabled:
            self.service.complete_upload(
                "alice", completed_upload["upload_id"], now=21_104
            )
        self.assertEqual(disabled.exception.error_code, "feature_disabled")

    def test_concurrent_completion_replays_first_time_but_metadata_drift_conflicts(self):
        upload = self.create_upload(filename="concurrent.png", now=30_000)
        key = self.prepare_completion(upload, size_bytes=321, width=80, height=60)
        observation = self.inspector.observations[key]
        barrier = threading.Barrier(2)

        def inspect(_key, *, upload_type, head):
            barrier.wait(timeout=5)
            return observation

        self.inspector.inspect = inspect
        results = []
        errors = []

        def complete(at):
            try:
                results.append(
                    self.service.complete_upload("alice", upload["upload_id"], now=at)
                )
            except Exception as exc:  # captured for assertion in the test thread
                errors.append(exc)

        threads = [
            threading.Thread(target=complete, args=(30_001,)),
            threading.Thread(target=complete, args=(30_002,)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0], results[1])
        stored = self.store.get_upload_for_owner("alice", upload["upload_id"])
        self.assertIn(stored["completed_at"], {30_001, 30_002})
        with self.assertRaises(StoreConflictError):
            self.store.complete_upload(
                "alice",
                upload["upload_id"],
                observed_mime="image/png",
                observed_size=321,
                observed_etag="different-etag",
                sha256="a" * 64,
                duration_ms=None,
                width=80,
                height=60,
                probe={"codec": "png"},
                completed_at=30_003,
                environment="test",
            )

    def test_zero_image_and_video_dimensions_fail_as_stable_input_errors(self):
        cases = (
            ("material_image", "zero.png", "image/png", "image/png", "image", None),
            ("main_video", "zero.mp4", "video/mp4", "video/mp4", "video", 3_000),
        )
        for index, (upload_type, filename, declared, observed, kind, duration) in enumerate(cases):
            with self.subTest(upload_type=upload_type):
                upload = self.create_upload(
                    upload_type,
                    filename=filename,
                    content_type=declared,
                    now=40_000 + index * 10,
                )
                self.prepare_completion(
                    upload,
                    mime_type=observed,
                    media_kind=kind,
                    duration_ms=duration,
                    width=0,
                    height=100,
                    frame_rate=30 if upload_type == "main_video" else None,
                )
                captured = None
                try:
                    self.service.complete_upload(
                        "alice", upload["upload_id"], now=40_001 + index * 10
                    )
                except Exception as exc:
                    captured = exc
                self.assertIsInstance(captured, ServiceError)
                self.assertTrue(captured.error_code.startswith("input_"))
                self.assertLess(captured.status, 500)

    def test_material_promotion_is_owner_bound_typed_and_idempotent(self):
        upload = self.create_upload()
        self.prepare_completion(upload)
        self.service.complete_upload("alice", upload["upload_id"], now=1_001)

        material = self.service.create_material("alice", upload["upload_id"], now=1_002)
        replay = self.service.create_material("alice", upload["upload_id"], now=1_999)

        self.assertEqual(replay, material)
        self.assertRegex(material["material_id"], r"^material-[0-9]{4}$")
        with self.assertRaises(ServiceError) as context:
            self.service.create_material("bob", upload["upload_id"], now=2_000)
        self.assertEqual(context.exception.status, 404)

        main = self.create_upload(
            "main_video",
            filename="main.mp4",
            content_type="video/mp4",
            now=3_000,
        )
        self.prepare_completion(
            main,
            mime_type="video/mp4",
            media_kind="video",
            duration_ms=3_000,
            width=1920,
            height=1080,
            frame_rate=30,
        )
        self.service.complete_upload("alice", main["upload_id"], now=3_001)
        with self.assertRaisesRegex(ServiceError, "material_upload_invalid"):
            self.service.create_material("alice", main["upload_id"], now=3_002)


class V3ApplicationServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve()
        self.v2 = root / "ai_edit_v2.db"
        self.v2.write_bytes(b"V2 marker; do not open")
        self.store = V3Store(
            root / "ai_edit_v3.db",
            v2_db_path=self.v2,
            environment="test",
        )
        self.store.insert_pricing_version(
            "price-v1",
            pricing_parameters(),
            status="published",
            created_at=1,
            published_at=2,
        )
        self.objects = FakeUploadObjectStore()
        self.inspector = FakeUploadInspector()
        self.capacity = FakeCapacityGate()
        self.catalog = FakeSourceCatalog()
        self.service = EditV3Service(
            self.store,
            object_store=self.objects,
            upload_inspector=self.inspector,
            owner_hmac_secret=b"task-nine-test-secret",
            enabled=True,
            id_factory=SequentialIds(),
            source_catalog=self.catalog,
            capacity_gate=self.capacity,
            capability_report=ready_capability_report(),
        )
        self.main_upload_id = self.completed_upload(
            "main_video",
            filename="main.mp4",
            content_type="video/mp4",
            mime_type="video/mp4",
            media_kind="video",
            size_bytes=10,
            duration_ms=3_000,
            width=1920,
            height=1080,
            frame_rate=30,
            now=100,
        )["upload_id"]

    def completed_upload(
        self,
        upload_type,
        *,
        filename,
        content_type,
        mime_type,
        media_kind,
        size_bytes,
        duration_ms=None,
        width=None,
        height=None,
        frame_rate=None,
        now,
    ):
        upload = self.service.create_upload(
            "alice",
            {
                "upload_type": upload_type,
                "filename": filename,
                "content_type": content_type,
                "size_bytes": min(size_bytes, GIB),
            },
            now=now,
        )
        key = self.objects.presign_calls[-1][0]
        self.objects.heads[key] = {
            "content_type": content_type,
            "size_bytes": size_bytes,
            "etag": f"etag-{upload['upload_id']}",
        }
        self.inspector.observations[key] = UploadObservation(
            mime_type=mime_type,
            media_kind=media_kind,
            size_bytes=size_bytes,
            sha256="b" * 64,
            duration_ms=duration_ms,
            width=width,
            height=height,
            frame_rate=frame_rate,
            probe_evidence={"codec": mime_type.split("/")[-1]},
        )
        return self.service.complete_upload("alice", upload["upload_id"], now=now + 1)

    def material(self, *, size_bytes=1, now=300):
        upload = self.completed_upload(
            "material_image",
            filename=f"material-{now}.png",
            content_type="image/png",
            mime_type="image/png",
            media_kind="image",
            size_bytes=size_bytes,
            width=1,
            height=1,
            now=now,
        )
        return self.service.create_material("alice", upload["upload_id"], now=now + 2)

    def request(self, **overrides):
        value = {
            "input_type": "uploaded_video",
            "source_upload_id": self.main_upload_id,
            "ratio": "auto",
            "creation_mode": "style_prompt",
            "style_prompt": "clean commercial edit",
            "material_asset_ids": [],
        }
        value.update(overrides)
        return value

    def test_job_requires_exact_quote_fingerprint_and_one_predebit_intent(self):
        request = self.request()
        quote = self.service.quote("alice", request, now=1_000)
        changed = dict(request, style_prompt="changed")

        with self.assertRaisesRegex(ServiceError, "quote_request_mismatch"):
            self.service.create_job(
                "alice", changed, quote["quote_id"], "client-key-1", now=1_001
            )
        job = self.service.create_job(
            "alice", request, quote["quote_id"], "client-key-1", now=1_001
        )
        replay = self.service.create_job(
            "alice", request, quote["quote_id"], "client-key-1", now=1_002
        )

        self.assertEqual(replay["job_id"], job["job_id"])
        intents = [
            row
            for row in self.store.list_due_billing_intents(10_000)
            if row["job_id"] == job["job_id"] and row["operation"] == "pre_debit"
        ]
        self.assertEqual(len(intents), 1)

    def test_real_job_detail_and_list_expose_current_stable_progress_stage(self):
        request = self.request()
        quote = self.service.quote("alice", request, now=1_000)
        created = self.service.create_job(
            "alice", request, quote["quote_id"], "progress-key-1", now=1_001
        )

        detail = self.service.get_job("alice", created["job_id"])
        listed = self.service.list_jobs("alice", cursor=None, limit=20)["items"][0]

        self.assertEqual(detail["state"], "created_draft")
        self.assertEqual(detail["stage"], detail["state"])
        self.assertEqual(listed["state"], "created_draft")
        self.assertEqual(listed["stage"], listed["state"])
        self.assertNotEqual(detail["stage"], "completed")

    def assert_authority_drift_rejected(self, request, quote, key):
        with self.assertRaisesRegex(ServiceError, "quote_authority_mismatch"):
            self.service.create_job(
                "alice", request, quote["quote_id"], key, now=1_001
            )
        self.assertEqual(self.store.list_jobs_for_owner("alice")["items"], [])
        self.assertEqual(self.store.list_due_billing_intents(10_000), [])

    def test_uploaded_source_or_material_authority_drift_invalidates_quote(self):
        request = self.request()
        quote = self.service.quote("alice", request, now=1_000)
        self.store._write(
            lambda connection: connection.execute(
                "UPDATE edit_v3_uploads SET observed_size=observed_size+1 WHERE upload_id=?",
                (self.main_upload_id,),
            )
        )
        self.assert_authority_drift_rejected(request, quote, "drift-source-1")

        self.store._write(
            lambda connection: connection.execute(
                "UPDATE edit_v3_uploads SET observed_size=observed_size-1 WHERE upload_id=?",
                (self.main_upload_id,),
            )
        )
        material = self.material(now=500)
        request = self.request(material_asset_ids=[material["material_id"]])
        quote = self.service.quote("alice", request, now=1_000)
        self.store._write(
            lambda connection: connection.execute(
                "UPDATE edit_v3_materials SET size_bytes=size_bytes+1 WHERE material_id=?",
                (material["material_id"],),
            )
        )
        self.assert_authority_drift_rejected(request, quote, "drift-material-1")

    def test_catalog_source_and_voice_authority_drift_invalidates_quote(self):
        platform_request = {
            "input_type": "platform_talking_head",
            "source_asset_id": "platform-drift",
            "ratio": "auto",
            "creation_mode": "ai_auto",
            "material_asset_ids": [],
        }
        self.catalog.platform[("alice", "platform-drift")] = {
            "asset_id": "platform-drift",
            "duration_ms": 3_000,
            "ratio": "16:9",
            "transcript_sha256": "d" * 64,
        }
        quote = self.service.quote("alice", platform_request, now=1_000)
        self.catalog.platform[("alice", "platform-drift")] = {
            "asset_id": "platform-drift",
            "duration_ms": 3_001,
            "ratio": "16:9",
            "transcript_sha256": "e" * 64,
        }
        self.assert_authority_drift_rejected(
            platform_request, quote, "drift-platform-1"
        )

        voice_request = {
            "input_type": "script_to_audio_video",
            "tts_input": {"text": "hello", "voice_id": "voice-drift"},
            "ratio": "16:9",
            "creation_mode": "ai_auto",
            "material_asset_ids": [],
        }
        self.catalog.voices[("alice", "voice-drift")] = {
            "voice_id": "voice-drift",
            "status": "ready",
            "version": "voice-v1",
        }
        quote = self.service.quote("alice", voice_request, now=1_000)
        self.catalog.voices[("alice", "voice-drift")] = {
            "voice_id": "voice-drift",
            "status": "ready",
            "version": "voice-v2",
        }
        self.assert_authority_drift_rejected(voice_request, quote, "drift-voice-1")

    def test_template_authority_drift_invalidates_quote_even_when_version_is_same(self):
        self.store._write(
            lambda connection: connection.execute(
                """INSERT INTO edit_v3_template_versions(
                       template_id,version,status,preview_cos_key,supported_ratios_json,
                       capability_contract_json,sha256,created_at,published_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    "template-drift",
                    "template-v1",
                    "published",
                    "templates/template-drift/v1/preview.jpg",
                    '["16:9","9:16"]',
                    "{}",
                    "f" * 64,
                    10,
                    11,
                ),
            )
        )
        request = self.request(
            creation_mode="template_reference",
            template_id="template-drift",
        )
        request.pop("style_prompt")
        self.catalog.templates[("template-drift", "auto")] = {
            "template_id": "template-drift",
            "version": "template-v1",
            "status": "published",
            "capability_sha256": "1" * 64,
        }
        quote = self.service.quote("alice", request, now=1_000)
        self.catalog.templates[("template-drift", "auto")] = {
            "template_id": "template-drift",
            "version": "template-v1",
            "status": "published",
            "capability_sha256": "2" * 64,
        }
        self.assert_authority_drift_rejected(request, quote, "drift-template-1")

    def test_capacity_checks_before_quote_and_again_before_atomic_job_create(self):
        request = self.request()
        self.capacity.decision = CapacityDecision(False, 0, 1024, 17)

        with self.assertRaises(ServiceError) as context:
            self.service.quote("alice", request, now=1_000)
        self.assertEqual(context.exception.error_code, "capacity_unavailable")
        self.assertEqual(context.exception.retry_after, 17)
        self.assertEqual(self.store.list_jobs_for_owner("alice")["items"], [])

        self.capacity.decision = CapacityDecision(True, 1, 1024, None)
        quote = self.service.quote("alice", request, now=2_000)
        self.capacity.decision = CapacityDecision(False, 0, 1024, 23)
        with self.assertRaises(ServiceError) as context:
            self.service.create_job(
                "alice", request, quote["quote_id"], "client-key-2", now=2_001
            )
        self.assertEqual(context.exception.retry_after, 23)
        self.assertEqual(self.store.list_jobs_for_owner("alice")["items"], [])
        self.assertEqual(self.store.list_due_billing_intents(10_000), [])
        self.assertEqual(len(self.capacity.calls), 3)

    def test_request_limits_use_only_selected_authoritative_uploads(self):
        materials = [self.material(now=300 + index * 10) for index in range(11)]
        with self.assertRaisesRegex(ServiceError, "material_asset_ids_invalid"):
            self.service.quote(
                "alice",
                self.request(
                    material_asset_ids=[item["material_id"] for item in materials]
                ),
                now=1_000,
            )

        exact_main = self.completed_upload(
            "main_video",
            filename="one-gib.mp4",
            content_type="video/mp4",
            mime_type="video/mp4",
            media_kind="video",
            size_bytes=GIB,
            duration_ms=3_000,
            width=1920,
            height=1080,
            frame_rate=30,
            now=2_000,
        )
        extra = self.material(size_bytes=1, now=2_100)
        with self.assertRaisesRegex(ServiceError, "input_upload_total_exceeded"):
            self.service.quote(
                "alice",
                self.request(
                    source_upload_id=exact_main["upload_id"],
                    material_asset_ids=[extra["material_id"]],
                ),
                now=3_000,
            )

        self.service.quote(
            "alice",
            self.request(source_upload_id=exact_main["upload_id"]),
            now=3_001,
        )

    def test_main_upload_alone_obeys_the_one_gib_authoritative_limit(self):
        oversized = self.completed_upload(
            "main_video",
            filename="oversized-main.mp4",
            content_type="video/mp4",
            mime_type="video/mp4",
            media_kind="video",
            size_bytes=GIB + 1,
            duration_ms=3_000,
            width=1920,
            height=1080,
            frame_rate=30,
            now=4_000,
        )
        with self.assertRaisesRegex(ServiceError, "input_upload_total_exceeded"):
            self.service.quote(
                "alice",
                self.request(source_upload_id=oversized["upload_id"]),
                now=5_000,
            )

        exact = self.completed_upload(
            "main_video",
            filename="exact-main.mp4",
            content_type="video/mp4",
            mime_type="video/mp4",
            media_kind="video",
            size_bytes=GIB,
            duration_ms=3_000,
            width=1920,
            height=1080,
            frame_rate=30,
            now=6_000,
        )
        quote = self.service.quote(
            "alice",
            self.request(source_upload_id=exact["upload_id"]),
            now=7_000,
        )
        self.assertEqual(quote["request_sha256"], quote["request_fingerprint"])

    def test_source_resolution_is_owner_bound_and_catalog_unready_is_503(self):
        platform_request = {
            "input_type": "platform_talking_head",
            "source_asset_id": "platform-1",
            "ratio": "auto",
            "creation_mode": "ai_auto",
            "material_asset_ids": [],
        }
        self.catalog.platform[("alice", "platform-1")] = {
            "asset_id": "platform-1",
            "duration_ms": 3_000,
            "ratio": "16:9",
            "transcript_sha256": "c" * 64,
        }

        quote = self.service.quote("alice", platform_request, now=1_000)
        self.assertEqual(quote["request_sha256"], quote["request_fingerprint"])
        with self.assertRaises(ServiceError) as context:
            self.service.quote("bob", platform_request, now=1_001)
        self.assertEqual(context.exception.status, 404)

        self.service.source_catalog = None
        with self.assertRaises(ServiceError) as context:
            self.service.quote("alice", platform_request, now=1_002)
        self.assertEqual(context.exception.status, 503)
        self.assertEqual(context.exception.error_code, "platform_assets_unavailable")

    def test_public_catalog_omits_every_raw_cover_url_and_keeps_opaque_refs(self):
        public = {
            "asset_id": "platform-cover-safe",
            "title": "Platform title",
            "cover_asset_id": "cover-asset-1",
            "cover_reference": "cover-reference-1",
            "duration_ms": 3_000,
            "ratio": "16:9",
        }
        raw_cover_urls = (
            "https://cdn.example.com/cover.jpg",
            "https://localhost./cover.jpg",
            "https://foo.localhost/cover.jpg",
            "https://2130706433/cover.jpg",
            "https://0x7f000001/cover.jpg",
            "https://0177.0.0.1/cover.jpg",
            "https://192.168.1.1/cover.jpg",
            "https://cdn.example.com/cover.jpg?token=secret",
        )
        failures = []
        for cover_url in raw_cover_urls:
            with self.subTest(cover_url=cover_url):
                self.catalog.platform_rows = [{**public, "cover_url": cover_url}]
                try:
                    returned = self.service.list_platform_assets("alice")["items"][0]
                except ServiceError as exc:
                    failures.append((cover_url, exc.error_code))
                    continue
                if returned != public:
                    failures.append((cover_url, returned))
        self.assertEqual(failures, [])

    def test_public_catalog_plan_and_result_dtos_drop_nested_private_data(self):
        malicious = {
            "transcript": "private transcript",
            "cos_key": "production/ai-edit-v3/private/object",
            "path": "/srv/private/source.mp4",
            "provider_payload": {"request_id": "provider-private"},
        }
        catalog_cases = (
            (
                "platform_rows",
                self.service.list_platform_assets,
                {
                    "asset_id": "platform-1",
                    "title": "Platform title",
                    "cover_asset_id": "cover-1",
                    "duration_ms": 3_000,
                    "ratio": "16:9",
                },
            ),
            (
                "audio_rows",
                self.service.list_audio_assets,
                {
                    "asset_id": "audio-1",
                    "title": "Audio title",
                    "duration_ms": 3_000,
                    "mime_type": "audio/mp4",
                },
            ),
            (
                "voice_rows",
                self.service.list_voices,
                {"voice_id": "voice-1", "name": "Voice", "language": "zh-CN"},
            ),
            (
                "template_rows",
                self.service.list_templates,
                {
                    "template_id": "template-1",
                    "version": "v1",
                    "title": "Template",
                    "preview_asset_id": "preview-1",
                    "supported_ratios": ["16:9"],
                },
            ),
        )
        leaks = []
        for attribute, method, public in catalog_cases:
            setattr(self.catalog, attribute, [{**public, **malicious}])
            returned = method("alice")["items"][0]
            if returned != public:
                leaks.append((attribute, returned))

        self.catalog.platform_rows = [
            {
                "asset_id": "platform-url-title",
                "title": "https://private.invalid/title?token=secret",
                "duration_ms": 3_000,
                "ratio": "16:9",
            }
        ]
        try:
            self.service.list_platform_assets("alice")
        except ServiceError:
            pass
        else:
            leaks.append(("allowed-field-url", self.catalog.platform_rows[0]))

        unsafe_catalog_values = (
            ("asset_id", "production/ai-edit-v3/private/object"),
            ("cover_asset_id", "../private/cover.jpg"),
            ("cover_reference", "relative/private/cover.jpg"),
            ("cover_url", "https://cdn.invalid/cover.jpg?token=secret"),
        )
        for field, unsafe in unsafe_catalog_values:
            with self.subTest(field=field, unsafe=unsafe):
                row = {
                    "asset_id": "platform-safe",
                    "title": "Use A versus B testing for growth",
                    "duration_ms": 3_000,
                    "ratio": "16:9",
                    field: unsafe,
                }
                self.catalog.platform_rows = [row]
                try:
                    returned = self.service.list_platform_assets("alice")
                except ServiceError:
                    continue
                if unsafe in json.dumps(returned, ensure_ascii=False, sort_keys=True):
                    leaks.append((f"catalog-{field}", unsafe))

        self.catalog.platform_rows = [
            {
                "asset_id": "platform-legitimate",
                "title": "Use A versus B testing for growth",
                "duration_ms": 3_000,
                "ratio": "16:9",
            }
        ]
        legitimate = self.service.list_platform_assets("alice")["items"][0]
        self.assertEqual(legitimate["title"], "Use A versus B testing for growth")
        self.catalog.platform_rows = [
            {
                "asset_id": "platform-slash-title",
                "title": "Use A/B testing for growth",
                "duration_ms": 3_000,
                "ratio": "16:9",
            }
        ]
        try:
            returned = self.service.list_platform_assets("alice")
        except ServiceError:
            pass
        else:
            leaks.append(("catalog-slash-title", returned))

        request = self.request()
        quote = self.service.quote("alice", request, now=8_000)
        job = self.service.create_job(
            "alice", request, quote["quote_id"], "privacy-key-1", now=8_001
        )
        nested = {
            "headline": "safe public value",
            "summary": "Use A versus B testing for Q3 growth.",
            "slash_free_chinese": "进行 A 与 B 测试",
            "semantic_slash_text": {
                "headline": "Use A/B testing for Q3 growth.",
                "title": "Use A/B testing for Q3 growth.",
                "description": "使用 A/B 测试优化转化率。",
                "intent": "开展 A/B 实验评估方案。",
                "text": "进行 A/B 对比来选择版本。",
                "label": "Use A/B testing for Q3 growth.",
                "name": "Use A/B testing for Q3 growth.",
            },
            "slash_policy_cases": {
                "summary": [
                    "private A/Btesting",
                    "private A/Bexperiment",
                    "private A/Bcomparison",
                    "private A/B testing_private",
                    "private A/B testing",
                ]
            },
            "slash_reference_cases": [
                {
                    "detail": "private A/B",
                    "note": "private A/B",
                    "reference": "private A/B",
                    "summary": "private A/B",
                },
                {
                    "detail": "folder A/B",
                    "note": "folder A/B",
                    "reference": "folder A/B",
                    "summary": "folder A/B",
                },
                {
                    "detail": "private A/B/secret.mov",
                    "note": "private A/B/secret.mov",
                    "reference": "private A/B/secret.mov",
                    "summary": "private A/B/secret.mov",
                },
            ],
            "innocuous": "Use A/B testing for Q3 growth.",
            "mime_key_cases": {
                "MIME_TYPE": "video/mp4",
                "Mime_Type": "video/mp4",
                "CONTENT_TYPE": "image/png",
                "Content_Type": "image/png",
                " mime_type": "video/mp4",
                "mime_type ": "video/mp4",
                "mіme_type": "video/mp4",
                "mime＿type": "video/mp4",
                "arbitrary": "video/private.mov",
                "invalid_mime": {
                    "mime_type": "custom/mp4",
                    "content_type": "video/private/path",
                },
                "nested_context": {
                    "mime_type": [
                        "video/mp4",
                        {
                            "MIME_TYPE": "video/mp4",
                            "mime_type": "video/mp4",
                            "arbitrary": "video/private.mov",
                        },
                    ],
                    "MIME_TYPE": [
                        "video/mp4",
                        {"content_type": "image/png"},
                    ],
                },
            },
            "nested": {
                "transcript": "private transcript",
                "cos_key": "production/ai-edit-v3/private/object",
                "path": "/srv/private/source.mp4",
                "provider_payload": {"request_id": "provider-private"},
                "signed_url": "https://private.invalid/a?token=secret",
                "reference": "production/ai-edit-v3/private/innocuous-key",
                "detail": "relative/private/source.mp4",
                "note": "../private/source.mp4",
                "more": "C:\\private\\source.mp4",
                "other": "\\\\server\\share\\source.mp4",
                "detail_one": "private dir/file.mp4",
                "detail_two": "bucket/path",
                "detail_three": "folder name/sub folder/private.mov",
                "encoded_one": "private%2fobject",
                "encoded_two": "private%2Fobject",
                "encoded_three": "private%5cobject",
                "encoded_four": "private%5Cobject",
                "mime_type": "video/mp4",
                "content_type": "image/png",
                "note_mime": "video/mp4",
                "reference_mime": "image/png",
            },
        }
        encoded = json.dumps(
            nested, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        def seed_plan(connection):
            connection.execute(
                """INSERT INTO edit_v3_model_calls(
                       id,job_id,stage_attempt_id,provider,model,purpose,prompt_version,
                       request_schema_sha256,response_schema_sha256,request_id,
                       redacted_final_output_json,validation_json,usage_json,elapsed_ms,
                       created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    "model-call-private",
                    job["job_id"],
                    None,
                    "test-provider",
                    "test-model",
                    "director",
                    "v1",
                    "a" * 64,
                    "b" * 64,
                    "request-safe",
                    "{}",
                    "{}",
                    None,
                    1,
                    8_002,
                ),
            )
            connection.execute(
                """INSERT INTO edit_v3_plans(
                       id,job_id,version,model_call_id,raw_final_output_json,
                       normalized_plan_json,plan_sha256,schema_sha256,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    "plan-privacy",
                    job["job_id"],
                    1,
                    "model-call-private",
                    encoded,
                    encoded,
                    "d" * 64,
                    "b96c059fa2e4ef7d91cd48278b474d61a34606f1cbce6963c3b65fa66f7d046c",
                    8_002,
                ),
            )
        self.store._write(seed_plan)
        self.store._write(
            lambda connection: connection.execute(
                "UPDATE edit_v3_jobs SET result_json=?,updated_at=? WHERE job_id=?",
                (encoded, 8_003, job["job_id"]),
            )
        )
        public_values = (
            self.service.get_plan("alice", job["job_id"])["plan"],
            self.service.get_result("alice", job["job_id"])["result"],
        )
        expected_semantic_slash_text = {
            "headline": "[redacted]",
            "title": "[redacted]",
            "description": "[redacted]",
            "intent": "[redacted]",
            "text": "[redacted]",
            "label": "[redacted]",
            "name": "[redacted]",
        }
        expected_slash_reference_cases = [
            {
                "detail": "[redacted]",
                "note": "[redacted]",
                "reference": "[redacted]",
                "summary": "[redacted]",
            },
            {
                "detail": "[redacted]",
                "note": "[redacted]",
                "reference": "[redacted]",
                "summary": "[redacted]",
            },
            {
                "detail": "[redacted]",
                "note": "[redacted]",
                "reference": "[redacted]",
                "summary": "[redacted]",
            },
        ]
        semantic_failures = []
        for value in public_values:
            serialized = json.dumps(value, ensure_ascii=False, sort_keys=True)
            for private in (
                "private transcript",
                "production/ai-edit-v3/private/object",
                "/srv/private/source.mp4",
                "provider-private",
                "private.invalid",
                "production/ai-edit-v3/private/innocuous-key",
                "relative/private/source.mp4",
                "../private/source.mp4",
                "C:\\private\\source.mp4",
                "\\\\server\\share\\source.mp4",
                "private dir/file.mp4",
                "bucket/path",
                "folder name/sub folder/private.mov",
                "private%2fobject",
                "private%2Fobject",
                "private%5cobject",
                "private%5Cobject",
                "private A/B",
                "folder A/B",
                "private A/Btesting",
                "private A/Bexperiment",
                "private A/Bcomparison",
                "private A/B testing_private",
                "private A/B testing",
                "Use A/B testing for Q3 growth.",
                "使用 A/B 测试优化转化率。",
                "开展 A/B 实验评估方案。",
                "进行 A/B 对比来选择版本。",
            ):
                if private in serialized:
                    leaks.append(("plan-or-result", private))
            self.assertEqual(value["headline"], "safe public value")
            self.assertEqual(value["summary"], "Use A versus B testing for Q3 growth.")
            self.assertEqual(value["slash_free_chinese"], "进行 A 与 B 测试")
            if value["semantic_slash_text"] != expected_semantic_slash_text:
                semantic_failures.append(
                    ("semantic_slash_text", value["semantic_slash_text"])
                )
            if value["slash_reference_cases"] != expected_slash_reference_cases:
                semantic_failures.append(
                    ("slash_reference_cases", value["slash_reference_cases"])
                )
            if value["slash_policy_cases"] != {
                "summary": [
                    "[redacted]",
                    "[redacted]",
                    "[redacted]",
                    "[redacted]",
                    "[redacted]",
                ]
            }:
                semantic_failures.append(
                    ("slash_policy_cases", value["slash_policy_cases"])
                )
            if value["innocuous"] != "[redacted]":
                semantic_failures.append(("innocuous", value["innocuous"]))
            expected_mime_key_cases = {
                "MIME_TYPE": "[redacted]",
                "Mime_Type": "[redacted]",
                "CONTENT_TYPE": "[redacted]",
                "Content_Type": "[redacted]",
                " mime_type": "[redacted]",
                "mime_type ": "[redacted]",
                "mіme_type": "[redacted]",
                "mime＿type": "[redacted]",
                "arbitrary": "[redacted]",
                "invalid_mime": {
                    "mime_type": "[redacted]",
                    "content_type": "[redacted]",
                },
                "nested_context": {
                    "mime_type": [
                        "video/mp4",
                        {
                            "MIME_TYPE": "[redacted]",
                            "mime_type": "video/mp4",
                            "arbitrary": "[redacted]",
                        },
                    ],
                    "MIME_TYPE": [
                        "[redacted]",
                        {"content_type": "image/png"},
                    ],
                },
            }
            if value["mime_key_cases"] != expected_mime_key_cases:
                semantic_failures.append(("mime_key_cases", value["mime_key_cases"]))
            self.assertEqual(value["nested"]["mime_type"], "video/mp4")
            self.assertEqual(value["nested"]["content_type"], "image/png")
            self.assertEqual(value["nested"]["note_mime"], "[redacted]")
            self.assertEqual(value["nested"]["reference_mime"], "[redacted]")
        self.assertEqual(leaks + semantic_failures, [])

    def test_existing_job_replay_does_not_reresolve_catalog_and_rechecks_store(self):
        request = {
            "input_type": "platform_talking_head",
            "source_asset_id": "platform-replay",
            "ratio": "auto",
            "creation_mode": "ai_auto",
            "material_asset_ids": [],
        }
        self.catalog.platform[("alice", "platform-replay")] = {
            "asset_id": "platform-replay",
            "duration_ms": 3_000,
            "ratio": "16:9",
            "transcript_sha256": "c" * 64,
        }
        quote = self.service.quote("alice", request, now=9_000)
        job = self.service.create_job(
            "alice", request, quote["quote_id"], "catalog-replay-1", now=9_001
        )
        self.service.source_catalog = None
        self.service.capacity_gate = None
        self.service.owner_hmac_secret = b"changed-replay-secret"
        self.service._capability_report_source = None
        published = self.store.list_published_pricing_versions()
        original_pricing = self.store.list_published_pricing_versions
        self.store.list_published_pricing_versions = lambda: [
            published[0],
            dict(published[0], version="ambiguous-replay-version"),
        ]

        replay = None
        replay_error = None
        try:
            replay = self.service.create_job(
                "alice", request, quote["quote_id"], "catalog-replay-1", now=9_002
            )
        except ServiceError as exc:
            replay_error = exc
        self.assertIsNone(replay_error, f"replay unexpectedly resolved catalog: {replay_error}")
        self.assertEqual(replay["job_id"], job["job_id"])
        with self.assertRaisesRegex(ServiceError, "idempotency_conflict"):
            self.service.create_job(
                "alice",
                {
                    **request,
                    "creation_mode": "style_prompt",
                    "style_prompt": "a different valid request",
                },
                quote["quote_id"],
                "catalog-replay-1",
                now=9_003,
            )
        self.store._write(
            lambda connection: connection.execute(
                """UPDATE edit_v3_billing_intents
                   SET request_amount=request_amount+1 WHERE job_id=?
                     AND operation='pre_debit'""",
                (job["job_id"],),
            )
        )
        with self.assertRaisesRegex(ServiceError, "billing_intent_conflict"):
            self.service.create_job(
                "alice", request, quote["quote_id"], "catalog-replay-1", now=9_004
            )
        self.service.enabled = False
        with self.assertRaises(ServiceError) as disabled:
            self.service.create_job(
                "alice", request, quote["quote_id"], "catalog-replay-1", now=9_005
            )
        self.assertEqual(disabled.exception.error_code, "feature_disabled")
        self.store.list_published_pricing_versions = original_pricing

    def test_capabilities_include_local_dependencies_and_usable_pricing(self):
        def service(**overrides):
            values = {
                "object_store": self.objects,
                "upload_inspector": self.inspector,
                "owner_hmac_secret": b"task-nine-test-secret",
                "enabled": True,
                "source_catalog": self.catalog,
                "capacity_gate": self.capacity,
                "capability_report": ready_capability_report(),
            }
            values.update(overrides)
            return EditV3Service(self.store, **values)

        for name, candidate in (
            ("object-store", service(object_store=None)),
            ("object-store-shape", service(object_store=object())),
            ("inspector", service(upload_inspector=None)),
            ("inspector-shape", service(upload_inspector=object())),
            ("upload-secret", service(owner_hmac_secret=b"weak")),
        ):
            with self.subTest(name=name):
                self.assertFalse(candidate.get_capabilities("alice")["accepts_uploads"])

        for name, candidate in (
            ("capacity", service(capacity_gate=None)),
            ("capacity-shape", service(capacity_gate=object())),
            ("job-secret", service(owner_hmac_secret=b"weak")),
        ):
            with self.subTest(name=name):
                self.assertFalse(candidate.get_capabilities("alice")["accepts_new_jobs"])

        original = self.store.list_published_pricing_versions
        published = original()
        try:
            for name, replacement in (
                ("missing", lambda: []),
                ("ambiguous", lambda: [published[0], dict(published[0], version="other")]),
            ):
                with self.subTest(name=name):
                    self.store.list_published_pricing_versions = replacement
                    self.assertFalse(service().get_capabilities("alice")["accepts_new_jobs"])

            def unavailable():
                raise RuntimeError("local store unavailable")

            self.store.list_published_pricing_versions = unavailable
            self.assertFalse(service().get_capabilities("alice")["accepts_new_jobs"])
        finally:
            self.store.list_published_pricing_versions = original

    def test_foreign_or_missing_quote_is_404_before_authority_drift_checks(self):
        request = {
            "input_type": "platform_talking_head",
            "source_asset_id": "shared-platform-id",
            "ratio": "auto",
            "creation_mode": "ai_auto",
            "material_asset_ids": [],
        }
        record = {
            "asset_id": "shared-platform-id",
            "duration_ms": 3_000,
            "ratio": "16:9",
            "transcript_sha256": "f" * 64,
        }
        self.catalog.platform[("alice", "shared-platform-id")] = dict(record)
        self.catalog.platform[("bob", "shared-platform-id")] = dict(record)
        quote = self.service.quote("alice", request, now=10_000)

        for quote_id in (quote["quote_id"], "quote-does-not-exist"):
            with self.subTest(quote_id=quote_id):
                with self.assertRaises(ServiceError) as context:
                    self.service.create_job(
                        "bob", request, quote_id, "bob-client-key-1", now=10_001
                    )
                self.assertEqual(context.exception.error_code, "quote_not_found")
                self.assertEqual(context.exception.status, 404)

    def test_retry_rejects_active_job_then_creates_fresh_successor_and_replays(self):
        request = self.request()
        quote = self.service.quote("alice", request, now=1_000)
        original = self.service.create_job(
            "alice", request, quote["quote_id"], "client-key-3", now=1_001
        )
        with self.assertRaisesRegex(ServiceError, "retry_not_allowed"):
            self.service.retry_job(
                "alice", original["job_id"], "retry-client-1", now=1_002
            )

        self.store._write(
            lambda connection: connection.execute(
                "UPDATE edit_v3_jobs SET state='refunded',updated_at=? WHERE job_id=?",
                (1_003, original["job_id"]),
            )
        )
        successor = self.service.retry_job(
            "alice", original["job_id"], "retry-client-1", now=1_004
        )
        self.service.capacity_gate = None
        self.service.owner_hmac_secret = b"changed-retry-replay-secret"
        self.service._capability_report_source = None
        published = self.store.list_published_pricing_versions()
        self.store.list_published_pricing_versions = lambda: [
            published[0],
            dict(published[0], version="ambiguous-retry-replay"),
        ]

        replay = None
        replay_error = None
        try:
            replay = self.service.retry_job(
                "alice", original["job_id"], "retry-client-1", now=1_005
            )
        except ServiceError as exc:
            replay_error = exc
        self.assertIsNone(
            replay_error,
            f"retry replay unexpectedly required current capabilities: {replay_error}",
        )

        self.assertNotEqual(successor["job_id"], original["job_id"])
        self.assertEqual(replay["job_id"], successor["job_id"])
        stored = self.store.get_job_for_owner("alice", successor["job_id"])
        self.assertEqual(stored["predecessor_job_id"], original["job_id"])
        intents = [
            row
            for row in self.store.list_due_billing_intents(10_000)
            if row["job_id"] == successor["job_id"]
        ]
        self.assertEqual(len(intents), 1)
        self.store._write(
            lambda connection: connection.execute(
                """UPDATE edit_v3_billing_intents
                   SET request_amount=request_amount+1 WHERE job_id=?
                     AND operation='pre_debit'""",
                (successor["job_id"],),
            )
        )
        with self.assertRaisesRegex(ServiceError, "billing_intent_conflict"):
            self.service.retry_job(
                "alice", original["job_id"], "retry-client-1", now=1_006
            )

    def test_retry_predecessor_and_predebit_are_one_crash_safe_transaction(self):
        request = self.request()
        predecessor_quote = self.service.quote("alice", request, now=1_000)
        predecessor = self.service.create_job(
            "alice",
            request,
            predecessor_quote["quote_id"],
            "client-key-4",
            now=1_001,
        )
        self.store._write(
            lambda connection: connection.execute(
                "UPDATE edit_v3_jobs SET state='refunded',updated_at=? WHERE job_id=?",
                (1_002, predecessor["job_id"]),
            )
        )
        material = self.material(now=1_010)
        request = self.request(material_asset_ids=[material["material_id"]])
        quote = self.service.quote("alice", request, now=1_020)
        arguments = {
            "owner_id": "alice",
            "job_id": "successor-atomic",
            "quote_id": quote["quote_id"],
            "idempotency_key": "retry:atomic:client-key-4",
            "normalized_request": request,
            "now_ms": 1_021,
            "intent_id": "intent-atomic",
            "predecessor_job_id": predecessor["job_id"],
            "material_bindings": [
                {
                    "material_id": material["material_id"],
                    "purpose": "supplemental",
                    "ordinal": 0,
                }
            ],
            "environment": "test",
        }

        with self.assertRaisesRegex(RuntimeError, "crash-after-job"):
            self.store.create_job_with_predebit(
                **arguments,
                fail_after_job=RuntimeError("crash-after-job"),
            )
        self.assertIsNone(self.store.get_job_for_owner("alice", "successor-atomic"))
        self.assertFalse(
            any(
                row["job_id"] == "successor-atomic"
                for row in self.store.list_due_billing_intents(10_000)
            )
        )
        binding_count = self.store._read(
            lambda connection: connection.execute(
                "SELECT COUNT(*) FROM edit_v3_job_materials WHERE job_id=?",
                ("successor-atomic",),
            ).fetchone()[0]
        )
        self.assertEqual(binding_count, 0)

        created = self.store.create_job_with_predebit(**arguments)
        self.assertEqual(
            created["job"]["predecessor_job_id"], predecessor["job_id"]
        )
        self.assertEqual(created["intent"]["operation"], "pre_debit")
        binding_count = self.store._read(
            lambda connection: connection.execute(
                "SELECT COUNT(*) FROM edit_v3_job_materials WHERE job_id=?",
                ("successor-atomic",),
            ).fetchone()[0]
        )
        self.assertEqual(binding_count, 1)

    def test_retry_recovers_quote_only_crash_in_same_or_later_ttl_generation(self):
        request = self.request()
        quote = self.service.quote("alice", request, now=1_000)
        predecessor = self.service.create_job(
            "alice", request, quote["quote_id"], "client-key-5", now=1_001
        )
        self.store._write(
            lambda connection: connection.execute(
                "UPDATE edit_v3_jobs SET state='refunded',updated_at=? WHERE job_id=?",
                (1_002, predecessor["job_id"]),
            )
        )
        original_create = self.service._create_job

        def crash_after_quote(*args, **kwargs):
            raise RuntimeError("crash-after-retry-quote")

        self.service._create_job = crash_after_quote
        try:
            with self.assertRaisesRegex(RuntimeError, "crash-after-retry-quote"):
                self.service.retry_job(
                    "alice", predecessor["job_id"], "retry-client-2", now=1_003
                )
        finally:
            self.service._create_job = original_create

        same_generation = self.service.retry_job(
            "alice", predecessor["job_id"], "retry-client-2", now=1_004
        )
        same_replay = self.service.retry_job(
            "alice", predecessor["job_id"], "retry-client-2", now=1_005
        )
        self.assertEqual(same_replay["job_id"], same_generation["job_id"])

        self.service._create_job = crash_after_quote
        try:
            with self.assertRaisesRegex(RuntimeError, "crash-after-retry-quote"):
                self.service.retry_job(
                    "alice", predecessor["job_id"], "retry-client-3", now=2_000
                )
        finally:
            self.service._create_job = original_create
        later_generation = self.service.retry_job(
            "alice", predecessor["job_id"], "retry-client-3", now=902_001
        )
        later_replay = self.service.retry_job(
            "alice", predecessor["job_id"], "retry-client-3", now=902_002
        )
        self.assertEqual(later_replay["job_id"], later_generation["job_id"])

        successors = [
            row
            for row in self.store.list_jobs_for_owner("alice", limit=100)["items"]
            if row["predecessor_job_id"] == predecessor["job_id"]
        ]
        self.assertEqual(len(successors), 2)
        successor_ids = {row["job_id"] for row in successors}
        intents = [
            row
            for row in self.store.list_due_billing_intents(2_000_000)
            if row["job_id"] in successor_ids and row["operation"] == "pre_debit"
        ]
        self.assertEqual(len(intents), 2)

    def test_client_cannot_claim_the_service_retry_namespace(self):
        quote = self.service.quote("alice", self.request(), now=1_000)
        with self.assertRaisesRegex(ServiceError, "idempotency_key_invalid"):
            self.service.create_job(
                "alice",
                self.request(),
                quote["quote_id"],
                "retry:stolen",
                now=1_001,
            )


if __name__ == "__main__":
    unittest.main()
