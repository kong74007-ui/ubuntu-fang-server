from __future__ import annotations

import json
import hashlib
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

from server.content_domains.ai_edit_v2_providers.base import ProviderResult as V2Result
from server.content_domains.ai_edit_v3.director import validate_edit_plan
from server.content_domains.ai_edit_v3.production import QwenCompiledDirector
from server.content_domains.ai_edit_v3.transcript import Caption


class _Qwen:
    def generate_edit_plan(self, system_prompt, user_prompt):
        self.system_prompt = system_prompt
        self.user_prompt = user_prompt
        return V2Result(
            provider="dashscope",
            capability="director",
            request_id="qwen-request-1",
            payload={"content": json.dumps({
                "creative_concept": "先给结论，再解释方法",
                "layout_id": "speaker_fullscreen",
                "motion_energy": "high",
            }, ensure_ascii=False)},
            cost_units=17,
            elapsed_ms=12,
        )


class ProductionDirectorTests(unittest.TestCase):
    def test_qwen_creativity_is_compiled_to_the_strict_plan(self):
        client = _Qwen()
        provider = QwenCompiledDirector(client)
        capabilities = {
            "layout_capabilities": ["speaker_fullscreen"],
            "overlay_capabilities": ["standard_caption"],
            "animation_capabilities": ["subtitle_pop"],
            "transition_capabilities": ["hard_cut"],
            "theme_capabilities": {
                "palette_id": ["midnight_gold"],
                "typography_id": ["editorial_sans"],
                "density": ["balanced"],
                "motion_energy": ["medium", "high"],
                "image_fit": ["cover"],
            },
        }
        request = {
            "timeline": {
                "duration_ms": 4000,
                "captions": [
                    {"id": "caption_001", "start_ms": 0, "end_ms": 2000, "text": "真实方法"},
                    {"id": "caption_002", "start_ms": 2000, "end_ms": 4000, "text": "提升效率"},
                ],
                "source_segments": [],
            },
            "capabilities": capabilities,
            "ratio": "9:16",
            "user_direction": "节奏清晰",
        }

        result = provider.generate_plan(request, purpose="initial", idempotency_key="x", deadline_at=9999999999)
        plan = json.loads(result.payload["content"])
        timeline = SimpleNamespace(
            duration_ms=4000,
            captions=(
                Caption("caption_001", "真实方法", 0, 2000),
                Caption("caption_002", "提升效率", 2000, 4000),
            ),
        )

        self.assertEqual(validate_edit_plan(plan, timeline=timeline, capabilities=capabilities), plan)
        self.assertEqual(plan["creative_concept"], "先给结论，再解释方法")
        self.assertEqual(plan["theme"]["motion_energy"], "high")
        self.assertEqual(result.usage, {"tokens": 17})
        self.assertNotIn("Shotstack", client.system_prompt)

    def test_uploaded_materials_become_bound_slots_and_visible_layout(self):
        client = _Qwen()
        provider = QwenCompiledDirector(client)
        capabilities = {
            "layout_capabilities": ["speaker_fullscreen", "speaker_left_info_right"],
            "overlay_capabilities": ["standard_caption"],
            "animation_capabilities": ["subtitle_pop"],
            "transition_capabilities": ["hard_cut"],
            "theme_capabilities": {
                "palette_id": ["midnight_gold"],
                "typography_id": ["editorial_sans"],
                "density": ["balanced"],
                "motion_energy": ["medium", "high"],
                "image_fit": ["cover"],
            },
        }
        request = {
            "timeline": {
                "duration_ms": 4000,
                "captions": [
                    {"id": "caption_001", "start_ms": 0, "end_ms": 4000, "text": "真实产品介绍"},
                ],
                "source_segments": [],
            },
            "current_materials": [
                {"material_id": "mat-1", "semantic": "用户上传的产品图"},
            ],
            "capabilities": capabilities,
            "ratio": "9:16",
            "user_direction": "突出产品",
        }

        result = provider.generate_plan(
            request, purpose="initial", idempotency_key="x", deadline_at=9999999999
        )
        plan = json.loads(result.payload["content"])
        timeline = SimpleNamespace(
            duration_ms=4000,
            captions=(Caption("caption_001", "真实产品介绍", 0, 4000),),
        )

        self.assertEqual(validate_edit_plan(plan, timeline=timeline, capabilities=capabilities), plan)
        self.assertEqual(plan["scenes"][0]["layout_id"], "speaker_left_info_right")
        self.assertEqual(plan["scenes"][0]["material_slots"][0]["id"], "material_01")
        self.assertEqual(plan["materials"][0]["request_id"], "material_01")

    def test_missing_material_requests_one_generic_generated_visual(self):
        provider = QwenCompiledDirector(_Qwen())
        capabilities = {
            "layout_capabilities": ["speaker_fullscreen", "product_hero"],
            "overlay_capabilities": ["standard_caption"],
            "animation_capabilities": ["subtitle_pop"],
            "transition_capabilities": ["hard_cut"],
            "theme_capabilities": {
                "palette_id": ["midnight_gold"],
                "typography_id": ["editorial_sans"],
                "density": ["balanced"],
                "motion_energy": ["medium", "high"],
                "image_fit": ["cover"],
            },
        }
        request = {
            "timeline": {
                "duration_ms": 4000,
                "captions": [{"id": "caption_001", "start_ms": 0, "end_ms": 4000, "text": "讲解行业方法"}],
                "source_segments": [],
            },
            "source": {"input_type": "uploaded_audio"},
            "current_materials": [],
            "generate_missing_material": True,
            "capabilities": capabilities,
            "ratio": "16:9",
            "user_direction": "知识讲解",
        }
        result = provider.generate_plan(request, purpose="initial", idempotency_key="x", deadline_at=9999999999)
        plan = json.loads(result.payload["content"])
        timeline = SimpleNamespace(duration_ms=4000, captions=(Caption("caption_001", "讲解行业方法", 0, 4000),))

        self.assertEqual(validate_edit_plan(plan, timeline=timeline, capabilities=capabilities), plan)
        self.assertEqual("product_hero", plan["scenes"][0]["layout_id"])
        self.assertEqual("context", plan["materials"][0]["purpose"])


class ProductionStageCoordinatorTests(unittest.TestCase):
    def test_queued_stage_enters_media_pipeline(self):
        from server.content_domains.ai_edit_v3.production import ProductionStageCoordinator

        with tempfile.TemporaryDirectory() as directory:
            coordinator = object.__new__(ProductionStageCoordinator)
            coordinator.store = type("Store", (), {"environment": "test"})()
            coordinator.work_root = Path(directory)

            outcome = coordinator._stage(
                "queued",
                {
                    "job_id": "job-queued",
                    "owner_id": "alice",
                    "stage_input_sha256": "0" * 64,
                    "normalized_request_json": '{"input_type":"uploaded_audio"}',
                },
                object(),
            )

        self.assertEqual("generating_voice", outcome.next_state)
        self.assertTrue(outcome.checkpoint["admitted"])

    def test_transcribing_persists_deeply_frozen_provider_payload_as_json(self):
        from server.content_domains.ai_edit_v3.production import ProductionStageCoordinator
        from server.content_domains.ai_edit_v3.providers.base import ProviderResult

        class Cos:
            def put_file(self, *args, **kwargs):
                return None

            def presign_get(self, key, *, expires):
                return "https://example.invalid/source.mp4"

        class Asr:
            def transcribe(self, signed_url, reference, *, deadline_at):
                return ProviderResult(
                    provider="dashscope",
                    capability="asr",
                    request_id="request-1",
                    payload={
                        "status": "succeeded",
                        "provider_task_id": "task-1",
                        "duration_ms": 1000,
                        "words": [
                            {"text": "测试", "start_ms": 0, "end_ms": 1000}
                        ],
                        "sentences": [
                            {"text": "测试", "start_ms": 0, "end_ms": 1000}
                        ],
                    },
                    usage={},
                    elapsed_ms=10,
                )

        with tempfile.TemporaryDirectory() as directory:
            coordinator = object.__new__(ProductionStageCoordinator)
            coordinator.store = type("Store", (), {"environment": "test"})()
            coordinator.work_root = Path(directory)
            coordinator.owner_hmac_secret = b"0123456789abcdef"
            coordinator.cos = Cos()
            coordinator.asr = Asr()
            root = coordinator._root("job-transcribe")
            (root / "media").mkdir()
            (root / "media/source.mp4").write_bytes(b"source")
            (root / "normalized.json").write_text(
                json.dumps(
                    {
                        "relative_path": "media/source.mp4",
                        "media_type": "video",
                    }
                ),
                encoding="utf-8",
            )

            outcome = coordinator._stage(
                "transcribing",
                {
                    "job_id": "job-transcribe",
                    "owner_id": "alice",
                    "stage_input_sha256": "0" * 64,
                    "normalized_request_json": '{"input_type":"uploaded_video"}',
                },
                SimpleNamespace(deadline_at=9999999999),
            )
            persisted = json.loads((root / "asr.json").read_text(encoding="utf-8"))

        self.assertEqual("aligning", outcome.next_state)
        self.assertEqual("task-1", outcome.checkpoint["provider_task_id"])
        self.assertEqual("测试", persisted["words"][0]["text"])

    def test_resolving_materials_freezes_only_job_bound_uploads(self):
        from server.content_domains.ai_edit_v3.production import ProductionStageCoordinator

        image = b"verified-user-image"

        class Store:
            environment = "test"

            def resolve_request_uploads_for_owner(self, owner, *, source_upload_id, material_ids, environment):
                self.call = (owner, source_upload_id, material_ids, environment)
                return {
                    "source_upload": None,
                    "materials": [{
                        "material_id": "mat-1",
                        "cos_key": "test/ai-edit-v3/alice/uploads/material.png",
                        "mime_type": "image/png",
                        "size_bytes": len(image),
                        "sha256": hashlib.sha256(image).hexdigest(),
                        "metadata_json": '{"width":1080,"height":1920}',
                    }],
                }

        class Cos:
            def download_file(self, key, target):
                self.key = key
                Path(target).write_bytes(image)

        with tempfile.TemporaryDirectory() as directory:
            coordinator = object.__new__(ProductionStageCoordinator)
            coordinator.store = Store()
            coordinator.cos = Cos()
            coordinator.work_root = Path(directory)
            outcome = coordinator._stage(
                "resolving_materials",
                {
                    "job_id": "job-material",
                    "owner_id": "alice",
                    "stage_input_sha256": "0" * 64,
                    "normalized_request_json": json.dumps({
                        "input_type": "platform_talking_head",
                        "material_asset_ids": ["mat-1"],
                    }),
                },
                object(),
            )
            frozen = json.loads((coordinator._root("job-material") / "materials.json").read_text("utf-8"))

        self.assertEqual("generating_images", outcome.next_state)
        self.assertEqual(1, outcome.checkpoint["material_count"])
        self.assertEqual("mat-1", frozen["items"][0]["material_id"])
        self.assertEqual(("alice", None, ["mat-1"], "test"), coordinator.store.call)


if __name__ == "__main__":
    unittest.main()
