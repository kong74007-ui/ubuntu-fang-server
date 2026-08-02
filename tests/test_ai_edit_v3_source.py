from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from server.content_domains.ai_edit_v3.providers import SubmissionUnknown
from server.content_domains.ai_edit_v3.source import SourceError, prepare_source


class FakeVoices:
    def get_active_for_owner(self, owner: str, voice_id: str) -> object | None:
        if (owner, voice_id) == ("alice", "voice-1"):
            return SimpleNamespace(id="voice-1")
        return None


class FakeTts:
    def __init__(self, order: list[str] | None = None) -> None:
        self.submissions: list[str] = []
        self.queries: list[str] = []
        self.order = order

    def submit(self, **kwargs: object) -> SimpleNamespace:
        self.submissions.append(str(kwargs["idempotency_key"]))
        if self.order is not None:
            self.order.append("submit")
        return SimpleNamespace(
            request_id="tts-request-1",
            media=SimpleNamespace(sha256="tts-media"),
            timestamps=({"text": "价格是 298 元", "start_ms": 0, "end_ms": 1000},),
        )

    def query(self, request_id: str, **kwargs: object) -> SimpleNamespace:
        self.queries.append(request_id)
        if self.order is not None:
            self.order.append("query")
        return SimpleNamespace(
            request_id=request_id,
            media=SimpleNamespace(sha256="tts-media"),
            timestamps=(),
        )


class FakeProviderTasks:
    def __init__(self, existing: dict[str, object] | None = None, order: list[str] | None = None) -> None:
        self.existing = existing
        self.order = order
        self.intents: list[dict[str, object]] = []
        self.bindings: list[dict[str, object]] = []
        self.unknown: list[str] = []

    def record_intent(self, **kwargs: object) -> dict[str, object] | None:
        self.intents.append(dict(kwargs))
        if self.order is not None:
            self.order.append("intent")
        return self.existing

    def bind_result(self, **kwargs: object) -> None:
        self.bindings.append(dict(kwargs))
        if self.order is not None:
            self.order.append("bind")

    def mark_unknown(self, operation_key: str, **kwargs: object) -> None:
        self.unknown.append(operation_key)


class FakeRepository:
    def __init__(self, records: dict[tuple[str, str], dict[str, object]]) -> None:
        self.records = records
        self.calls: list[tuple[str, str]] = []

    def get_for_owner(self, owner: str, identifier: str) -> dict[str, object] | None:
        self.calls.append((owner, identifier))
        return self.records.get((owner, identifier))


class FakeMediaResolver:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def prepare(self, record: dict[str, object], *, input_type: str, deadline_at: float) -> object:
        self.calls.append((str(record["id"]), input_type))
        return record["media"]


class SourceContractTests(unittest.TestCase):
    def test_script_to_audio_freezes_owner_voice_and_request_identity(self) -> None:
        tts = FakeTts()
        deps = SimpleNamespace(
            voices=FakeVoices(),
            tts=tts,
            provider_tasks=FakeProviderTasks(),
        )
        context = SimpleNamespace(deadline_at=100.0)
        job = {
            "id": "j1",
            "owner": "alice",
            "input_type": "script_to_audio_video",
            "authoritative_text": "价格是 298 元",
            "voice_id": "voice-1",
        }

        source = prepare_source(job, deps, context)

        self.assertEqual(source.authoritative_text, "价格是 298 元")
        self.assertEqual(source.provider_request_id, "tts-request-1")
        self.assertEqual(tts.submissions, ["ai-edit-v3:j1:tts"])
        with self.assertRaisesRegex(SourceError, "voice_not_found"):
            prepare_source({**job, "owner": "mallory"}, deps, context)

    def test_all_non_tts_inputs_are_owner_scoped_and_keep_authoritative_text_only_for_platform(self) -> None:
        media = SimpleNamespace(sha256="media-sha")
        platform = FakeRepository({
            ("alice", "video-1"): {
                "id": "video-1", "asset_id": "video-1", "status": "completed",
                "authoritative_text": "品牌名和价格 298 元", "media": media,
            }
        })
        audio = FakeRepository({
            ("alice", "audio-1"): {
                "id": "audio-1", "asset_id": "audio-1", "status": "completed", "media": media,
            }
        })
        uploads = FakeRepository({
            ("alice", "video-up"): {
                "id": "video-up", "upload_id": "video-up", "status": "completed", "media": media,
            },
            ("alice", "audio-up"): {
                "id": "audio-up", "upload_id": "audio-up", "status": "completed", "media": media,
            },
        })
        deps = SimpleNamespace(
            platform_assets=platform,
            audio_assets=audio,
            uploads=uploads,
            media=FakeMediaResolver(),
        )
        context = SimpleNamespace(deadline_at=100.0)

        cases = (
            ("platform_talking_head", "source_asset_id", "video-1", "品牌名和价格 298 元"),
            ("uploaded_video", "source_upload_id", "video-up", None),
            ("existing_audio", "source_asset_id", "audio-1", None),
            ("uploaded_audio", "source_upload_id", "audio-up", None),
        )
        for input_type, field, identifier, expected_text in cases:
            with self.subTest(input_type=input_type):
                result = prepare_source(
                    {"id": "j1", "owner": "alice", "input_type": input_type, field: identifier},
                    deps,
                    context,
                )
                self.assertEqual(result.authoritative_text, expected_text)
                self.assertIs(result.media, media)

        with self.assertRaisesRegex(SourceError, "source_not_found"):
            prepare_source(
                {"id": "j2", "owner": "mallory", "input_type": "platform_talking_head", "source_asset_id": "video-1"},
                deps,
                context,
            )

    def test_tts_intent_precedes_submit_and_bound_request_resumes_by_query(self) -> None:
        order: list[str] = []
        tasks = FakeProviderTasks(order=order)
        tts = FakeTts(order)
        deps = SimpleNamespace(voices=FakeVoices(), tts=tts, provider_tasks=tasks)
        job = {
            "id": "j1", "owner": "alice", "input_type": "script_to_audio_video",
            "authoritative_text": "准确文案", "voice_id": "voice-1",
        }
        context = SimpleNamespace(deadline_at=100.0)

        result = prepare_source(job, deps, context)

        self.assertEqual(order, ["intent", "submit", "bind"])
        self.assertEqual(tasks.intents[0]["operation_key"], "ai-edit-v3:j1:tts")
        self.assertEqual(result.provider_request_id, "tts-request-1")

        resumed_order: list[str] = []
        resumed_tasks = FakeProviderTasks(
            existing={"external_id": "tts-existing", "status": "submitted"},
            order=resumed_order,
        )
        resumed_tts = FakeTts(resumed_order)
        resumed = prepare_source(
            job,
            SimpleNamespace(voices=FakeVoices(), tts=resumed_tts, provider_tasks=resumed_tasks),
            context,
        )
        self.assertEqual(resumed_tts.submissions, [])
        self.assertEqual(resumed_tts.queries, ["tts-existing"])
        self.assertEqual(resumed.provider_request_id, "tts-existing")

    def test_tts_unknown_is_marked_without_blind_resubmit(self) -> None:
        class UnknownTts(FakeTts):
            def submit(self, **kwargs: object) -> SimpleNamespace:
                raise SubmissionUnknown("provider_response_unknown")

        tasks = FakeProviderTasks()
        deps = SimpleNamespace(voices=FakeVoices(), tts=UnknownTts(), provider_tasks=tasks)
        with self.assertRaises(SubmissionUnknown):
            prepare_source(
                {
                    "id": "j1", "owner": "alice", "input_type": "script_to_audio_video",
                    "authoritative_text": "准确文案", "voice_id": "voice-1",
                },
                deps,
                SimpleNamespace(deadline_at=100.0),
            )
        self.assertEqual(tasks.unknown, ["ai-edit-v3:j1:tts"])
        self.assertEqual(len(tasks.intents), 1)

    def test_store_job_shape_reads_only_the_frozen_normalized_request(self) -> None:
        media = SimpleNamespace(sha256="media-sha")
        platform = FakeRepository({
            ("alice", "video-1"): {
                "id": "video-1", "asset_id": "video-1", "status": "completed",
                "authoritative_text": "平台准确原文", "media": media,
            }
        })
        deps = SimpleNamespace(
            platform_assets=platform,
            media=FakeMediaResolver(),
        )
        job = {
            "job_id": "job-from-store",
            "owner_id": "alice",
            "normalized_request_json": json.dumps({
                "input_type": "platform_talking_head",
                "source_asset_id": "video-1",
                "creation_mode": "ai_auto",
                "material_asset_ids": [],
                "ratio": "9:16",
            }),
        }

        result = prepare_source(job, deps, SimpleNamespace(deadline_at=100.0))

        self.assertEqual(result.source_asset_id, "video-1")
        self.assertEqual(result.authoritative_text, "平台准确原文")

    def test_tts_provider_binding_is_json_safe_and_contains_no_url(self) -> None:
        tasks = FakeProviderTasks()
        prepare_source(
            {
                "id": "j1", "owner": "alice", "input_type": "script_to_audio_video",
                "authoritative_text": "准确文案", "voice_id": "voice-1",
            },
            SimpleNamespace(voices=FakeVoices(), tts=FakeTts(), provider_tasks=tasks),
            SimpleNamespace(deadline_at=100.0),
        )

        serialized = json.dumps(tasks.bindings[0]["result"], ensure_ascii=False)
        self.assertNotIn("http", serialized)
        self.assertNotIn("SimpleNamespace", serialized)


if __name__ == "__main__":
    unittest.main()
