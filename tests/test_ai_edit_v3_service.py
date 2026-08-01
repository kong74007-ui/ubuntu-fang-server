import re
import tempfile
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
from server.content_domains.ai_edit_v3.store import V3Store


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
        replay = self.service.retry_job(
            "alice", original["job_id"], "retry-client-1", now=1_005
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
