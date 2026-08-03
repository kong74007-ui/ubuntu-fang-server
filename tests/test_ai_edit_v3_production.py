from __future__ import annotations

import json
import hashlib
from pathlib import Path
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from server.content_domains.ai_edit_v2_providers.base import ProviderResult as V2Result
from server.content_domains.ai_edit_v3.director import validate_edit_plan
from server.content_domains.ai_edit_v3.production import QwenCompiledDirector
from server.content_domains.ai_edit_v3.providers.qwen_compatible import (
    DashScopeCompatibleQwenClient,
)
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
    def test_default_client_uses_v3_compatible_qwen_transport(self):
        provider = QwenCompiledDirector()

        self.assertIsInstance(provider.client, DashScopeCompatibleQwenClient)

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

    def test_talking_head_captions_compile_to_multiple_speaker_safe_scenes(self):
        provider = QwenCompiledDirector(_Qwen())
        capabilities = {
            "layout_capabilities": [
                "speaker_fullscreen",
                "speaker_left_info_right",
                "speaker_right_evidence_left",
                "material_fullscreen_speaker_pip",
                "product_hero",
            ],
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
        captions = [
            {"id": f"caption_{index:03d}", "start_ms": start, "end_ms": end, "text": text}
            for index, (start, end, text) in enumerate(
                (
                    (0, 4200, "Introduce the platform"),
                    (4200, 8200, "Explain image generation"),
                    (8200, 12400, "Explain digital presenters"),
                    (12400, 16600, "Explain voice generation"),
                    (16600, 21200, "Explain poster workflows"),
                    (21200, 26178, "Conclude with delivery efficiency"),
                ),
                start=1,
            )
        ]
        request = {
            "timeline": {"duration_ms": 26178, "captions": captions, "source_segments": []},
            "source": {"input_type": "platform_talking_head"},
            "current_materials": [],
            "generate_missing_material": True,
            "capabilities": capabilities,
            "ratio": "9:16",
            "user_direction": "ai_auto",
        }

        plan = json.loads(provider.generate_plan(
            request, purpose="initial", idempotency_key="x", deadline_at=9999999999
        ).payload["content"])

        timeline = SimpleNamespace(
            duration_ms=26178,
            captions=tuple(
                Caption(item["id"], item["text"], item["start_ms"], item["end_ms"])
                for item in captions
            ),
        )

        self.assertEqual(plan, validate_edit_plan(plan, timeline=timeline, capabilities=capabilities))
        self.assertGreaterEqual(len(plan["scenes"]), 4)
        self.assertEqual(0, plan["scenes"][0]["start_ms"])
        self.assertEqual(26178, plan["scenes"][-1]["end_ms"])
        self.assertEqual(
            [scene["end_ms"] for scene in plan["scenes"][:-1]],
            [scene["start_ms"] for scene in plan["scenes"][1:]],
        )
        layouts = [scene["layout_id"] for scene in plan["scenes"]]
        self.assertEqual("speaker_fullscreen", layouts[0])
        self.assertNotIn("product_hero", layouts)
        self.assertGreater(len(set(layouts)), 1)
        self.assertGreaterEqual(len(plan["materials"]), 2)

    def test_invalid_optional_layout_sequence_keeps_multi_scene_fallback(self):
        class InvalidSequenceQwen(_Qwen):
            def generate_edit_plan(self, system_prompt, user_prompt):
                result = super().generate_edit_plan(system_prompt, user_prompt)
                result.payload["content"] = json.dumps({
                    "creative_concept": "Safe fallback",
                    "layout_sequence": ["unknown_layout"],
                    "motion_energy": "high",
                })
                return result

        provider = QwenCompiledDirector(InvalidSequenceQwen())
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
        captions = [
            {"id": f"caption_{index:03d}", "start_ms": start, "end_ms": end, "text": text}
            for index, (start, end, text) in enumerate(
                ((0, 3000, "One"), (3000, 6000, "Two"), (6000, 9000, "Three")),
                start=1,
            )
        ]

        plan = json.loads(provider.generate_plan({
            "timeline": {"duration_ms": 9000, "captions": captions, "source_segments": []},
            "source": {"input_type": "platform_talking_head"},
            "current_materials": [],
            "generate_missing_material": False,
            "capabilities": capabilities,
            "ratio": "9:16",
        }, purpose="initial", idempotency_key="x", deadline_at=9999999999).payload["content"])

        self.assertEqual(3, len(plan["scenes"]))
        self.assertTrue(all(scene["layout_id"] in capabilities["layout_capabilities"] for scene in plan["scenes"]))


class ProductionStageCoordinatorTests(unittest.TestCase):
    def test_visual_inspector_blocks_single_card_talking_head_failure_pattern(self):
        from server.content_domains.ai_edit_v3.production import DeterministicVisualInspector

        manifest = {
            "duration_ms": 26178,
            "source_video": {"path": "media/source.mp4", "silent": True},
            "assets": [{"id": "material_01", "kind": "image"}],
            "compositions": [{
                "id": "composition_001", "start_ms": 0, "end_ms": 26178,
                "layout_id": "product_hero", "asset_ids": ["material_01"],
            }],
            "captions": [
                {"id": f"caption_{index:03d}", "start_ms": start, "end_ms": end, "text": text}
                for index, (start, end, text) in enumerate(
                    (
                        (0, 6045, "Platform introduction"),
                        (6045, 10205, "Image and presenter tools"),
                        (10205, 16460, "Marketing content"),
                        (16460, 18938, "One workbench"),
                        (18938, 25090, "Deliver final videos"),
                        (25090, 26178, "More efficient"),
                    ),
                    start=1,
                )
            ],
        }

        verdict = DeterministicVisualInspector().inspect(manifest=manifest, render_report={})
        checks = {item["check_id"]: item for item in verdict["checks"]}

        self.assertEqual("fail", checks["safe_area_and_text_visibility"]["result"])
        self.assertEqual("fail", checks["face_product_obstruction"]["result"])
        self.assertEqual("fail", checks["material_semantic_identity"]["result"])
        self.assertEqual("fail", checks["opening_hook_visual_consistency"]["result"])

    def test_visual_inspector_accepts_varied_scene_bound_talking_head_manifest(self):
        from server.content_domains.ai_edit_v3.production import DeterministicVisualInspector

        boundaries = [0, 4000, 8000, 12000, 16000, 21000, 26000]
        material_ids = ["material_01", "material_02", "material_03"]
        compositions = []
        for index in range(6):
            material_id = material_ids[index // 2] if index % 2 else None
            compositions.append({
                "id": f"composition_{index + 1:03d}",
                "start_ms": boundaries[index],
                "end_ms": boundaries[index + 1],
                "layout_id": "speaker_left_info_right" if material_id else "speaker_fullscreen",
                "asset_ids": [material_id] if material_id else [],
            })
        manifest = {
            "duration_ms": 26000,
            "source_video": {"path": "media/source.mp4", "silent": True},
            "assets": [{"id": material_id, "kind": "image"} for material_id in material_ids],
            "compositions": compositions,
            "captions": [
                {
                    "id": f"caption_{index + 1:03d}",
                    "start_ms": boundaries[index],
                    "end_ms": boundaries[index + 1],
                    "text": f"Caption {index + 1}",
                }
                for index in range(6)
            ],
        }

        verdict = DeterministicVisualInspector().inspect(manifest=manifest, render_report={})
        checks = {item["check_id"]: item for item in verdict["checks"]}

        for check_id in (
            "caption_fact_accuracy",
            "safe_area_and_text_visibility",
            "face_product_obstruction",
            "material_semantic_identity",
            "opening_hook_visual_consistency",
        ):
            self.assertEqual("pass", checks[check_id]["result"], check_id)

    def test_visual_inspector_blocks_repetitive_long_form_layouts(self):
        from server.content_domains.ai_edit_v3.production import DeterministicVisualInspector

        boundaries = [0, 4000, 8000, 12000, 16000, 20000, 24000]
        manifest = {
            "duration_ms": 24000,
            "source_video": {"path": "media/source.mp4", "silent": True},
            "assets": [],
            "compositions": [
                {
                    "id": f"composition_{index + 1:03d}",
                    "start_ms": boundaries[index],
                    "end_ms": boundaries[index + 1],
                    "layout_id": "speaker_fullscreen",
                    "asset_ids": [],
                }
                for index in range(6)
            ],
            "captions": [
                {
                    "id": f"caption_{index + 1:03d}",
                    "start_ms": boundaries[index],
                    "end_ms": boundaries[index + 1],
                    "text": f"Caption {index + 1}",
                }
                for index in range(6)
            ],
        }

        verdict = DeterministicVisualInspector().inspect(manifest=manifest, render_report={})
        checks = {item["check_id"]: item for item in verdict["checks"]}

        self.assertEqual("fail", checks["safe_area_and_text_visibility"]["result"])
        self.assertTrue(checks["safe_area_and_text_visibility"]["blocking"])

    def test_render_compositions_bind_only_scene_requested_materials(self):
        from server.content_domains.ai_edit_v3.production import _scene_asset_ids

        known = ["material_01", "material_02"]
        scenes = [
            {"id": "scene_01", "material_slots": [{"id": "material_01"}]},
            {"id": "scene_02", "material_slots": []},
            {"id": "scene_03", "material_slots": [{"id": "material_02"}]},
        ]

        self.assertEqual(
            [["material_01"], [], ["material_02"]],
            [_scene_asset_ids(scene, known) for scene in scenes],
        )

    def test_deterministic_visual_inspector_emits_complete_quality_schema(self):
        from server.content_domains.ai_edit_v3.contracts import validate_quality_verdict
        from server.content_domains.ai_edit_v3.production import DeterministicVisualInspector

        verdict = DeterministicVisualInspector().inspect()

        self.assertEqual(verdict, validate_quality_verdict(verdict))
        self.assertEqual(12, len(verdict["checks"]))

    def test_quality_owner_evidence_uses_frozen_material_hashes(self):
        from server.content_domains.ai_edit_v3.production import _material_asset_hashes

        digest = hashlib.sha256(b"verified-generated-image").hexdigest()
        manifest = {
            "assets": [{
                "id": "material_01",
                "kind": "image",
                "path": "media/material-01.png",
                "sha256": digest,
                "size_bytes": 24,
            }]
        }
        materials = {
            "items": [{
                "material_id": "generated_01",
                "relative_path": "materials/generated-01.png",
                "sha256": digest,
            }]
        }

        self.assertEqual(
            {"material_01": digest},
            _material_asset_hashes(manifest, materials),
        )

    def test_render_captions_strip_director_only_emphasis_metadata(self):
        from server.content_domains.ai_edit_v3.production import _render_captions

        self.assertEqual(
            [{"id": "caption_001", "start_ms": 0, "end_ms": 1000, "text": "准确字幕"}],
            _render_captions([
                {
                    "id": "caption_001",
                    "start_ms": 0,
                    "end_ms": 1000,
                    "text": "准确字幕",
                    "emphasis": "primary",
                }
            ]),
        )

    def test_full_source_audio_timeline_compiles_output_mapping(self):
        from server.content_domains.ai_edit_v3.production import (
            _timeline_with_full_source_map,
        )
        from server.content_domains.ai_edit_v3.transcript import SourceSegment, TextTimeline

        timeline = TextTimeline(
            duration_ms=4200,
            captions=(Caption("caption_001", "完整口播", 0, 4000),),
            source_segments=(
                SourceSegment("segment_001", 0, 1800, False, "完整", None, None),
                SourceSegment("segment_002", 1800, 4000, True, "口播", None, None),
            ),
            authoritative_text_sha256=None,
            alignment_coverage=1.0,
        )

        compiled = _timeline_with_full_source_map(timeline)

        self.assertEqual(
            [(0, 1800), (1800, 4000)],
            [
                (item.output_start_ms, item.output_end_ms)
                for item in compiled.source_segments
            ],
        )

    def test_generating_images_probes_with_a_bounded_timeout(self):
        from server.content_domains.ai_edit_v3.production import ProductionStageCoordinator

        class Generator:
            def generate(self, *, output_path, **kwargs):
                Path(output_path).write_bytes(b"generated-image")
                return None

        class Cos:
            def put_file(self, *args, **kwargs):
                return None

        with tempfile.TemporaryDirectory() as directory:
            coordinator = object.__new__(ProductionStageCoordinator)
            coordinator.store = type("Store", (), {"environment": "test"})()
            coordinator.work_root = Path(directory)
            coordinator.owner_hmac_secret = b"0123456789abcdef"
            coordinator.image_generator = Generator()
            coordinator.cos = Cos()
            root = coordinator._root("job-image")
            (root / "materials").mkdir()
            (root / "materials.json").write_text('{"items":[]}', encoding="utf-8")
            (root / "plan.json").write_text(
                json.dumps({
                    "ratio": "9:16",
                    "materials": [{
                        "request_id": "material_01",
                        "semantic": "通用行业方法视觉",
                    }],
                }),
                encoding="utf-8",
            )
            with patch(
                "server.content_domains.ai_edit_v3.production._probe_image",
                autospec=True,
                return_value=SimpleNamespace(width=1024, height=1536),
            ) as probe:
                outcome = coordinator._stage(
                    "generating_images",
                    {
                        "job_id": "job-image",
                        "owner_id": "alice",
                        "stage_input_sha256": "0" * 64,
                        "normalized_request_json": '{"input_type":"platform_talking_head"}',
                    },
                    SimpleNamespace(deadline_at=time.time() + 60),
                )

        self.assertEqual("generating_audio", outcome.next_state)
        timeout = probe.call_args.kwargs["timeout_seconds"]
        self.assertGreater(timeout, 0)
        self.assertLessEqual(timeout, 30)

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
