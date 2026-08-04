from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import subprocess
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
    def generate_edit_plan(self, system_prompt, user_prompt, *, timeout_seconds=None):
        self.system_prompt = system_prompt
        self.user_prompt = user_prompt
        self.timeout_seconds = timeout_seconds
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
    def test_visual_planning_binds_seed_to_persisted_request_sha_and_replays_exactly(self):
        from server.content_domains.ai_edit_v3.production import ProductionStageCoordinator

        decision = SimpleNamespace(
            value={
                "theme_profile_id": "editorial_clean",
                "design_intent": {"density": "balanced", "motion_energy": "medium", "image_fit": "cover", "decoration_intensity": "medium"},
            },
            decision_sha256="b" * 64,
            provider_request_id="director-1",
            raw_output_sha256="c" * 64,
        )
        capabilities = {
            "layout_capabilities": ["speaker_fullscreen"],
            "layout_variants": {"speaker_fullscreen": ["emphasis_b"]},
            "overlay_capabilities": ["standard_caption"],
            "animation_capabilities": ["fade"],
            "transition_capabilities": ["hard_cut"],
            "theme_capabilities": {},
            "theme_profile_ids": ["editorial_clean"],
            "overlay_variants": {}, "overlay_animation_targets": {}, "layout_animation_targets": {},
        }

        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"AI_EDIT_V3_VISUAL_PROGRAM_ENABLED": "1"}, clear=False):
            coordinator = object.__new__(ProductionStageCoordinator)
            coordinator.work_root = Path(directory)
            coordinator.store = type("Store", (), {"environment": "test", "resolve_request_uploads_for_owner": lambda *_args, **_kwargs: {"materials": []}})()
            coordinator.director = object()
            coordinator.renderer = SimpleNamespace(registry_sha256="sha256:" + "d" * 64)
            coordinator._capabilities = lambda _ratio: capabilities

            def planned(job_id, request_sha256):
                root = coordinator._root(job_id)
                (root / "normalized.json").write_text(json.dumps({"input_type": "uploaded_audio", "ratio": "9:16", "sha256": "a" * 64}), encoding="utf-8")
                (root / "timeline.json").write_text(json.dumps({"duration_ms": 4000, "captions": [{"id": "caption_001", "text": "authoritative", "start_ms": 0, "end_ms": 4000}], "source_segments": [], "authoritative_text_sha256": None, "alignment_coverage": 1.0}), encoding="utf-8")
                job = {"job_id": job_id, "owner_id": "alice", "request_sha256": request_sha256, "stage_input_sha256": "0" * 64, "normalized_request_json": '{"input_type":"uploaded_audio"}'}
                with patch("server.content_domains.ai_edit_v3.production.build_director_request", return_value={}), patch("server.content_domains.ai_edit_v3.production.generate_director_decision", return_value=decision), patch("server.content_domains.ai_edit_v3.production.compile_edit_plan", return_value={"version": "2.0", "visual_program_version": "1.0"}):
                    coordinator._stage("planning", job, SimpleNamespace(deadline_at=time.time() + 60, claim=None, stage_attempt_id="attempt"))
                return json.loads((root / "visual-program.json").read_text(encoding="utf-8"))["variation_seed"]

            first = planned("job-request-a", "1" * 64)
            second = planned("job-request-b", "2" * 64)
            replay = planned("job-replay", "1" * 64)
            replay_again = planned("job-replay", "1" * 64)

        self.assertNotEqual(first, second)
        self.assertEqual(first, replay)
        self.assertEqual(replay, replay_again)

    def test_visual_planning_rejects_missing_or_invalid_persisted_request_sha(self):
        from server.content_domains.ai_edit_v3.production import ProductionStageCoordinator

        decision = SimpleNamespace(value={"theme_profile_id": "editorial_clean", "design_intent": {"density": "balanced", "motion_energy": "medium", "image_fit": "cover", "decoration_intensity": "medium"}}, decision_sha256="b" * 64, provider_request_id="director-1", raw_output_sha256="c" * 64)
        capabilities = {"layout_capabilities": ["speaker_fullscreen"], "layout_variants": {"speaker_fullscreen": ["emphasis_b"]}, "overlay_capabilities": ["standard_caption"], "animation_capabilities": ["fade"], "transition_capabilities": ["hard_cut"], "theme_capabilities": {}, "theme_profile_ids": ["editorial_clean"], "overlay_variants": {}, "overlay_animation_targets": {}, "layout_animation_targets": {}}
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"AI_EDIT_V3_VISUAL_PROGRAM_ENABLED": "1"}, clear=False):
            coordinator = object.__new__(ProductionStageCoordinator)
            coordinator.work_root = Path(directory)
            coordinator.store = type("Store", (), {"environment": "test", "resolve_request_uploads_for_owner": lambda *_args, **_kwargs: {"materials": []}})()
            coordinator.director = object(); coordinator.renderer = SimpleNamespace(registry_sha256="sha256:" + "d" * 64); coordinator._capabilities = lambda _ratio: capabilities
            for index, request_sha256 in enumerate((None, "not-a-sha")):
                job_id = f"job-invalid-{index}"; root = coordinator._root(job_id)
                (root / "normalized.json").write_text(json.dumps({"input_type": "uploaded_audio", "ratio": "9:16", "sha256": "a" * 64}), encoding="utf-8")
                (root / "timeline.json").write_text(json.dumps({"duration_ms": 4000, "captions": [{"id": "caption_001", "text": "authoritative", "start_ms": 0, "end_ms": 4000}], "source_segments": [], "authoritative_text_sha256": None, "alignment_coverage": 1.0}), encoding="utf-8")
                job = {"job_id": job_id, "owner_id": "alice", "request_sha256": request_sha256, "stage_input_sha256": "0" * 64, "normalized_request_json": '{"input_type":"uploaded_audio"}'}
                with patch("server.content_domains.ai_edit_v3.production.build_director_request", return_value={}), patch("server.content_domains.ai_edit_v3.production.generate_director_decision", return_value=decision), patch("server.content_domains.ai_edit_v3.production.compile_edit_plan", return_value={"version": "2.0", "visual_program_version": "1.0"}):
                    with self.assertRaisesRegex(ValueError, "variation_seed_source_invalid"):
                        coordinator._stage("planning", job, SimpleNamespace(deadline_at=time.time() + 60, claim=None, stage_attempt_id="attempt"))

    def test_python_frozen_tokens_are_accepted_and_tampering_is_rejected_by_node(self):
        from server.content_domains.ai_edit_v3.production import _resolve_design_tokens

        renderer = Path(__file__).resolve().parents[1] / "server" / "ai_edit_v3_renderer"
        intents = ({"density": "minimal", "motion_energy": "low", "image_fit": "contain", "decoration_intensity": "low"}, {"density": "balanced", "motion_energy": "medium", "image_fit": "cover", "decoration_intensity": "medium"}, {"density": "dense", "motion_energy": "high", "image_fit": "smart_crop", "decoration_intensity": "high"})
        seeds = ("0123456789abcdef", "0000000000000000", "fedcba9876543210")
        manifests = []
        for profile in ("editorial_clean", "commercial_energy", "premium_dark", "warm_lifestyle"):
            for intent in intents:
                for seed in seeds:
                    manifests.append({"version": "2.0", "schema_sha256": "schema-v2", "registry_sha256": "registry", "renderer_environment": {"renderer_build_id": "build"}, "output_spec": {"ratio": "9:16", "width": 1080, "height": 1920, "fps_num": 30, "fps_den": 1}, "duration_ms": 4000, "master_audio": {"path": "media/master.wav"}, "source_video": None, "theme_profile_id": profile, "design_intent": intent, "variation_seed": seed, "design_tokens": _resolve_design_tokens(profile, intent, seed), "compositions": [{"id": "scene_1", "start_ms": 0, "end_ms": 4000, "overlay_ids": [], "overlay_instances": []}]})
        program = "import fs from 'node:fs'; import {parseCanonicalJson} from './src/parse-canonical-json.mjs'; import {validateManifest} from './src/validate-manifest.mjs'; const values=parseCanonicalJson(fs.readFileSync(process.argv.at(-1))); for (const value of values) validateManifest(value,{rendererBuildId:'build',registrySha256:'sha256:registry',schemaSha256ByVersion:{'2.0':'schema-v2'}});"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifests.json"; path.write_text(json.dumps(manifests), encoding="utf-8")
            accepted = subprocess.run(["node", "--input-type=module", "-e", program, str(path)], cwd=renderer, capture_output=True, text=True, check=False)
            manifests[0]["design_tokens"]["--hf-bg"] = "#tampered"
            path.write_text(json.dumps(manifests), encoding="utf-8")
            rejected = subprocess.run(["node", "--input-type=module", "-e", program, str(path)], cwd=renderer, capture_output=True, text=True, check=False)
        self.assertEqual(0, accepted.returncode, accepted.stderr)
        self.assertNotEqual(0, rejected.returncode)
        self.assertIn("manifest_design_tokens_mismatch", rejected.stderr)

    def test_variation_seed_is_a_stable_16_lowercase_hex_derivation(self):
        from server.content_domains.ai_edit_v3 import production

        derive = getattr(production, "derive_variation_seed", None)
        self.assertTrue(callable(derive), "variation seed derivation is required")
        request_sha256 = "a" * 64
        director_decision_sha256 = "b" * 64
        registry_sha256 = "c" * 64
        seed = derive(request_sha256, director_decision_sha256, registry_sha256)

        self.assertRegex(seed, r"^[0-9a-f]{16}$")
        self.assertEqual(
            seed,
            derive(request_sha256, director_decision_sha256, registry_sha256),
        )
        self.assertNotEqual(
            seed,
            derive("d" * 64, director_decision_sha256, registry_sha256),
        )

    def test_visual_program_rejects_missing_real_variant_catalog_before_provider_call(self):
        from server.content_domains.ai_edit_v3.production import visual_program_capabilities

        with self.assertRaisesRegex(ValueError, "visual_program_capabilities_incomplete"):
            visual_program_capabilities({"layout_capabilities": ["quote_reversal"]})

    def test_visual_program_gate_zero_keeps_legacy_director_path(self):
        from server.content_domains.ai_edit_v3.production import ProductionStageCoordinator

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"AI_EDIT_V3_VISUAL_PROGRAM_ENABLED": "0"}, clear=False
        ):
            coordinator = object.__new__(ProductionStageCoordinator)
            coordinator.work_root = Path(directory)
            coordinator.store = type("Store", (), {"environment": "test", "resolve_request_uploads_for_owner": lambda *_args, **_kwargs: {"materials": []}})()
            coordinator.director = object()
            root = coordinator._root("job-gate-zero")
            (root / "normalized.json").write_text(json.dumps({"input_type": "uploaded_audio", "ratio": "9:16", "sha256": "a" * 64}), encoding="utf-8")
            (root / "timeline.json").write_text(json.dumps({"duration_ms": 4000, "captions": [{"id": "caption_001", "text": "权威字幕", "start_ms": 0, "end_ms": 4000}], "source_segments": [{"id": "segment_01", "text": "权威字幕", "start_ms": 0, "end_ms": 4000, "protected": False, "output_start_ms": None, "output_end_ms": None}], "authoritative_text_sha256": None, "alignment_coverage": 1.0}), encoding="utf-8")
            legacy = SimpleNamespace(value={"version": "2.0"}, provider_request_id="legacy-request")
            with patch("server.content_domains.ai_edit_v3.production.generate_edit_plan", return_value=legacy) as old_path, patch("server.content_domains.ai_edit_v3.production.generate_director_decision") as visual_path:
                outcome = coordinator._stage("planning", {"job_id": "job-gate-zero", "owner_id": "alice", "stage_input_sha256": "0" * 64, "normalized_request_json": '{"input_type":"uploaded_audio"}'}, SimpleNamespace(deadline_at=time.time() + 60, claim=None, stage_attempt_id="attempt"))

        self.assertEqual("resolving_materials", outcome.next_state)
        old_path.assert_called_once()
        visual_path.assert_not_called()

    def test_visual_program_gate_one_fails_closed_before_real_director_call_when_catalog_is_incomplete(self):
        from server.content_domains.ai_edit_v3.production import ProductionStageCoordinator

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"AI_EDIT_V3_VISUAL_PROGRAM_ENABLED": "1"}, clear=False
        ):
            coordinator = object.__new__(ProductionStageCoordinator)
            coordinator.work_root = Path(directory)
            coordinator.store = type("Store", (), {"environment": "test", "resolve_request_uploads_for_owner": lambda *_args, **_kwargs: {"materials": []}})()
            coordinator.director = object()
            root = coordinator._root("job-gate-one")
            (root / "normalized.json").write_text(json.dumps({"input_type": "uploaded_audio", "ratio": "9:16", "sha256": "a" * 64}), encoding="utf-8")
            (root / "timeline.json").write_text(json.dumps({"duration_ms": 4000, "captions": [{"id": "caption_001", "text": "authoritative caption", "start_ms": 0, "end_ms": 4000}], "source_segments": [{"id": "segment_01", "text": "authoritative caption", "start_ms": 0, "end_ms": 4000, "protected": False, "output_start_ms": None, "output_end_ms": None}], "authoritative_text_sha256": None, "alignment_coverage": 1.0}), encoding="utf-8")
            with patch("server.content_domains.ai_edit_v3.production.generate_director_decision") as visual_path, patch("server.content_domains.ai_edit_v3.production.generate_edit_plan") as legacy_path:
                with self.assertRaisesRegex(ValueError, "visual_program_capabilities_incomplete"):
                    coordinator._stage("planning", {"job_id": "job-gate-one", "owner_id": "alice", "stage_input_sha256": "0" * 64, "normalized_request_json": '{"input_type":"uploaded_audio"}'}, SimpleNamespace(deadline_at=time.time() + 60, claim=None, stage_attempt_id="attempt"))

        visual_path.assert_not_called()
        legacy_path.assert_not_called()

    def test_scene_budget_adapts_when_twelve_scenes_cannot_hold_all_captions(self):
        from server.content_domains.ai_edit_v3.production import (
            _scene_duration_budget,
        )

        captions = [
            {
                "id": f"caption_{index:03d}",
                "start_ms": (index - 1) * 6000,
                "end_ms": index * 6000,
                "text": f"Caption {index}",
            }
            for index in range(1, 14)
        ]

        budget = _scene_duration_budget(captions)
        groups = QwenCompiledDirector._caption_groups(captions)

        self.assertEqual(12000, budget)
        self.assertLessEqual(len(groups), 12)
        self.assertLessEqual(
            max(
                int(group[-1]["end_ms"]) - int(group[0]["start_ms"])
                for group in groups
            ),
            budget,
        )

    def test_scene_budget_accepts_one_indivisible_long_caption(self):
        from server.content_domains.ai_edit_v3.production import (
            _scene_duration_budget,
        )

        captions = [
            {"id": "caption_001", "start_ms": 0, "end_ms": 10000, "text": "Long caption"},
            {"id": "caption_002", "start_ms": 10000, "end_ms": 14000, "text": "Tail"},
        ]

        self.assertEqual(10000, _scene_duration_budget(captions))

    def test_scene_budget_accounts_for_legal_caption_gaps_in_compiled_boundaries(self):
        from server.content_domains.ai_edit_v3.production import (
            DeterministicVisualInspector,
            _scene_duration_budget,
        )

        captions = [
            {"id": "caption_001", "start_ms": 0, "end_ms": 7900, "text": "One"},
            {"id": "caption_002", "start_ms": 8100, "end_ms": 10000, "text": "Two"},
            {"id": "caption_003", "start_ms": 10200, "end_ms": 18000, "text": "Three"},
        ]
        capabilities = {
            "layout_capabilities": ["speaker_fullscreen", "speaker_left_info_right"],
            "overlay_capabilities": ["standard_caption"],
            "animation_capabilities": ["subtitle_pop"],
            "transition_capabilities": ["hard_cut"],
            "theme_capabilities": {
                "palette_id": ["midnight_gold"],
                "typography_id": ["editorial_sans"],
                "density": ["balanced"],
                "motion_energy": ["medium"],
                "image_fit": ["cover"],
            },
        }
        plan = QwenCompiledDirector._compile(
            {
                "timeline": {"duration_ms": 18000, "captions": captions, "source_segments": []},
                "source": {"input_type": "platform_talking_head"},
                "current_materials": [
                    {"semantic": "Supporting evidence", "purpose": "evidence", "scene_index": 1},
                ],
                "generate_missing_material": False,
                "capabilities": capabilities,
                "ratio": "9:16",
            },
            {
                "layout_sequence": [
                    "speaker_fullscreen",
                    "speaker_left_info_right",
                    "speaker_fullscreen",
                ],
                "motion_energy": "medium",
            },
        )

        budget = _scene_duration_budget(captions, duration_ms=18000)
        self.assertEqual(8100, budget)
        self.assertLessEqual(
            max(scene["end_ms"] - scene["start_ms"] for scene in plan["scenes"]),
            budget,
        )
        manifest = {
            "duration_ms": 18000,
            "source_video": {"path": "media/source.mp4", "silent": True},
            "assets": [{"id": "material_01", "kind": "image"}],
            "compositions": [
                {
                    "id": f"composition_{index:03d}",
                    "start_ms": scene["start_ms"],
                    "end_ms": scene["end_ms"],
                    "layout_id": scene["layout_id"],
                    "asset_ids": [
                        slot["id"] for slot in scene["material_slots"]
                    ],
                }
                for index, scene in enumerate(plan["scenes"], start=1)
            ],
            "captions": captions,
        }
        checks = {
            item["check_id"]: item["result"]
            for item in DeterministicVisualInspector().inspect(
                manifest=manifest,
                render_report={},
            )["checks"]
        }
        self.assertEqual("pass", checks["safe_area_and_text_visibility"])
        self.assertEqual("pass", checks["opening_hook_visual_consistency"])

    def test_scene_budget_does_not_over_relax_when_feasibility_is_non_monotonic(self):
        from server.content_domains.ai_edit_v3.production import (
            _scene_duration_budget,
        )

        ranges = (
            (252, 2109), (2389, 9167), (9401, 10457), (10689, 15619),
            (15923, 17904), (18170, 23103), (23359, 28405), (28593, 33101),
            (33378, 35881), (35882, 38888), (38934, 40146), (40384, 44469),
            (44700, 46631), (46977, 53754), (54088, 57241),
        )
        captions = [
            {
                "id": f"caption_{index:03d}",
                "start_ms": start,
                "end_ms": end,
                "text": f"Caption {index}",
            }
            for index, (start, end) in enumerate(ranges, start=1)
        ]
        groups = QwenCompiledDirector._caption_groups(
            captions,
            duration_ms=57719,
        )
        starts = [0] + [int(group[0]["start_ms"]) for group in groups[1:]]
        ends = [int(group[0]["start_ms"]) for group in groups[1:]] + [57719]

        self.assertEqual(
            8000,
            _scene_duration_budget(captions, duration_ms=57719),
        )
        self.assertLessEqual(len(groups), 12)
        self.assertLessEqual(
            max(end - start for start, end in zip(starts, ends, strict=True)),
            8000,
        )

    def test_short_tail_stays_separate_when_merging_would_break_scene_rhythm(self):
        from server.content_domains.ai_edit_v3.production import _scene_duration_budget

        captions = [
            {"id": "caption_001", "start_ms": 0, "end_ms": 4200, "text": "One"},
            {"id": "caption_002", "start_ms": 4700, "end_ms": 12300, "text": "Two"},
            {"id": "caption_003", "start_ms": 12800, "end_ms": 13800, "text": "Three"},
        ]
        groups = QwenCompiledDirector._caption_groups(
            captions,
            duration_ms=14300,
        )

        self.assertEqual(3, len(groups))
        self.assertEqual(
            8100,
            _scene_duration_budget(captions, duration_ms=14300),
        )

    def test_default_client_uses_v3_compatible_qwen_transport(self):
        with patch.dict(os.environ, {}, clear=True):
            provider = QwenCompiledDirector()

        self.assertIsInstance(provider.client, DashScopeCompatibleQwenClient)
        self.assertEqual(provider.client._timeout_seconds, 120)

    def test_director_timeout_honors_bounded_environment_value(self):
        with patch.dict(
            os.environ,
            {"AI_EDIT_V3_DIRECTOR_TIMEOUT_SECONDS": "180"},
            clear=False,
        ):
            provider = QwenCompiledDirector()

        self.assertEqual(provider.client._timeout_seconds, 180)

    def test_director_timeout_rejects_invalid_or_unsafe_values(self):
        for raw in ("not-a-number", "29", "601", "+30", "030", " 30", "30 "):
            with self.subTest(raw=raw), patch.dict(
                os.environ,
                {"AI_EDIT_V3_DIRECTOR_TIMEOUT_SECONDS": raw},
                clear=False,
            ):
                with self.assertRaisesRegex(ValueError, "director_timeout_invalid"):
                    QwenCompiledDirector()

    def test_director_call_timeout_is_clipped_to_the_absolute_job_deadline(self):
        client = _Qwen()
        provider = QwenCompiledDirector(client, timeout_seconds=120)

        with patch(
            "server.content_domains.ai_edit_v3.production.time.time",
            return_value=1_000.2,
        ):
            provider.generate_plan(
                {
                    "timeline": {
                        "duration_ms": 1_000,
                        "captions": [
                            {"id": "caption_001", "start_ms": 0, "end_ms": 1_000, "text": "测试"}
                        ],
                        "source_segments": [],
                    },
                    "capabilities": {
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
                    },
                    "ratio": "9:16",
                },
                deadline_at=1_038.9,
            )

        self.assertEqual(client.timeout_seconds, 38)

    def test_director_does_not_call_provider_after_absolute_deadline(self):
        class NeverCalled(_Qwen):
            def generate_edit_plan(self, *args, **kwargs):
                raise AssertionError("provider must not be called")

        provider = QwenCompiledDirector(NeverCalled(), timeout_seconds=120)
        with patch(
            "server.content_domains.ai_edit_v3.production.time.time",
            return_value=1_000.0,
        ):
            with self.assertRaisesRegex(TimeoutError, "director_deadline_exceeded"):
                provider.generate_plan({}, deadline_at=1_000.9)

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
                    (0, 6045, "Introduce the platform"),
                    (6045, 10205, "Explain image generation"),
                    (10205, 16460, "Explain digital presenters"),
                    (16460, 18938, "Explain voice generation"),
                    (18938, 25090, "Explain poster workflows"),
                    (25090, 26178, "Conclude with delivery efficiency"),
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
        self.assertLessEqual(
            max(scene["end_ms"] - scene["start_ms"] for scene in plan["scenes"]),
            8000,
        )
        from server.content_domains.ai_edit_v3.production import (
            DeterministicVisualInspector,
            _scene_asset_ids,
        )

        material_ids = [item["request_id"] for item in plan["materials"]]
        manifest = {
            "duration_ms": plan["duration_ms"],
            "source_video": {"path": "media/source.mp4", "silent": True},
            "assets": [
                {"id": material_id, "kind": "image"}
                for material_id in material_ids
            ],
            "compositions": [
                {
                    "id": f"composition_{index:03d}",
                    "start_ms": scene["start_ms"],
                    "end_ms": scene["end_ms"],
                    "layout_id": scene["layout_id"],
                    "asset_ids": _scene_asset_ids(scene, material_ids),
                }
                for index, scene in enumerate(plan["scenes"], start=1)
            ],
            "captions": plan["captions"],
        }
        checks = {
            item["check_id"]: item
            for item in DeterministicVisualInspector().inspect(
                manifest=manifest,
                render_report={},
            )["checks"]
        }
        for check_id in (
            "safe_area_and_text_visibility",
            "material_semantic_identity",
            "opening_hook_visual_consistency",
        ):
            self.assertEqual("pass", checks[check_id]["result"], check_id)

    def test_material_free_talking_head_scene_ignores_split_layout_request(self):
        capabilities = {
            "layout_capabilities": [
                "speaker_fullscreen",
                "speaker_left_info_right",
                "material_fullscreen_speaker_pip",
            ],
            "overlay_capabilities": ["standard_caption"],
            "animation_capabilities": ["subtitle_pop"],
            "transition_capabilities": ["hard_cut"],
            "theme_capabilities": {
                "palette_id": ["midnight_gold"],
                "typography_id": ["editorial_sans"],
                "density": ["balanced"],
                "motion_energy": ["medium"],
                "image_fit": ["cover"],
            },
        }
        captions = [
            {"id": "caption_001", "start_ms": 0, "end_ms": 4000, "text": "Opening"},
            {"id": "caption_002", "start_ms": 4000, "end_ms": 8000, "text": "Evidence"},
            {"id": "caption_003", "start_ms": 8000, "end_ms": 12000, "text": "Close"},
        ]

        plan = QwenCompiledDirector._compile(
            {
                "timeline": {"duration_ms": 12000, "captions": captions, "source_segments": []},
                "source": {"input_type": "platform_talking_head"},
                "current_materials": [],
                "generate_missing_material": True,
                "capabilities": capabilities,
                "ratio": "9:16",
            },
            {
                "layout_sequence": [
                    "speaker_left_info_right",
                    "material_fullscreen_speaker_pip",
                    "speaker_left_info_right",
                ],
                "motion_energy": "medium",
                "visual_focuses": ["software workflow"],
            },
        )

        self.assertEqual([], plan["scenes"][0]["material_slots"])
        self.assertEqual("speaker_fullscreen", plan["scenes"][0]["layout_id"])
        self.assertTrue(plan["scenes"][1]["material_slots"])

    def test_invalid_optional_layout_sequence_keeps_multi_scene_fallback(self):
        class InvalidSequenceQwen(_Qwen):
            def generate_edit_plan(self, system_prompt, user_prompt, *, timeout_seconds=None):
                result = super().generate_edit_plan(
                    system_prompt,
                    user_prompt,
                    timeout_seconds=timeout_seconds,
                )
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
    def test_visual_inspector_rejects_material_layout_without_assets(self):
        from server.content_domains.ai_edit_v3.production import DeterministicVisualInspector

        manifest = {
            "duration_ms": 18000,
            "source_video": {"path": "media/source.mp4", "silent": True},
            "assets": [{"id": "material_01", "kind": "image"}],
            "compositions": [
                {
                    "id": "composition_001", "start_ms": 0, "end_ms": 6000,
                    "layout_id": "speaker_fullscreen", "asset_ids": [],
                },
                {
                    "id": "composition_002", "start_ms": 6000, "end_ms": 12000,
                    "layout_id": "speaker_left_info_right", "asset_ids": [],
                },
                {
                    "id": "composition_003", "start_ms": 12000, "end_ms": 18000,
                    "layout_id": "material_fullscreen_speaker_pip", "asset_ids": ["material_01"],
                },
            ],
            "captions": [
                {"id": "caption_001", "start_ms": 0, "end_ms": 6000, "text": "One"},
                {"id": "caption_002", "start_ms": 6000, "end_ms": 12000, "text": "Two"},
                {"id": "caption_003", "start_ms": 12000, "end_ms": 18000, "text": "Three"},
            ],
        }

        checks = {
            item["check_id"]: item
            for item in DeterministicVisualInspector().inspect(
                manifest=manifest,
                render_report={},
            )["checks"]
        }

        self.assertEqual("fail", checks["material_semantic_identity"]["result"])
        self.assertTrue(checks["material_semantic_identity"]["repairable"])
        self.assertEqual(
            "material_layout_requires_bound_asset_failed",
            checks["material_semantic_identity"]["reason"],
        )

    def test_repair_manifest_replaces_material_layout_without_assets(self):
        from server.content_domains.ai_edit_v3.production import (
            DeterministicVisualInspector,
            _repair_render_manifest,
        )

        manifest = {
            "duration_ms": 18000,
            "source_video": {"path": "media/source.mp4", "silent": True},
            "assets": [{"id": "material_01", "kind": "image"}],
            "compositions": [
                {
                    "id": "composition_001", "start_ms": 0, "end_ms": 6000,
                    "layout_id": "speaker_fullscreen", "asset_ids": [],
                },
                {
                    "id": "composition_002", "start_ms": 6000, "end_ms": 12000,
                    "layout_id": "speaker_left_info_right", "asset_ids": [],
                },
                {
                    "id": "composition_003", "start_ms": 12000, "end_ms": 18000,
                    "layout_id": "material_fullscreen_speaker_pip", "asset_ids": ["material_01"],
                },
            ],
            "captions": [
                {"id": "caption_001", "start_ms": 0, "end_ms": 6000, "text": "One"},
                {"id": "caption_002", "start_ms": 6000, "end_ms": 12000, "text": "Two"},
                {"id": "caption_003", "start_ms": 12000, "end_ms": 18000, "text": "Three"},
            ],
        }

        repaired = _repair_render_manifest(manifest, {"material_semantic_identity"})
        checks = {
            item["check_id"]: item
            for item in DeterministicVisualInspector().inspect(
                manifest=repaired,
                render_report={},
            )["checks"]
        }

        self.assertEqual("speaker_fullscreen", repaired["compositions"][1]["layout_id"])
        self.assertEqual([], repaired["compositions"][1]["asset_ids"])
        self.assertEqual("pass", checks["material_semantic_identity"]["result"])

    def test_repair_manifest_changes_the_failed_structure_and_passes_bounded_checks(self):
        from server.content_domains.ai_edit_v3.production import (
            DeterministicVisualInspector,
            _repair_render_manifest,
        )

        manifest = {
            "duration_ms": 26178,
            "source_video": {"path": "media/source.mp4", "silent": True},
            "assets": [
                {"id": "material_01", "kind": "image"},
                {"id": "material_02", "kind": "image"},
            ],
            "compositions": [
                {
                    "id": "composition_001", "scene_id": "scene_01",
                    "start_ms": 0, "end_ms": 6045,
                    "layout_id": "speaker_fullscreen", "asset_ids": [],
                },
                {
                    "id": "composition_002", "scene_id": "scene_02",
                    "start_ms": 6045, "end_ms": 10205,
                    "layout_id": "speaker_left_info_right", "asset_ids": ["material_01"],
                },
                {
                    "id": "composition_003", "scene_id": "scene_03",
                    "start_ms": 10205, "end_ms": 16460,
                    "layout_id": "speaker_fullscreen", "asset_ids": [],
                },
                {
                    "id": "composition_004", "scene_id": "scene_04",
                    "start_ms": 16460, "end_ms": 26178,
                    "layout_id": "material_fullscreen_speaker_pip", "asset_ids": ["material_02"],
                },
            ],
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

        repaired = _repair_render_manifest(
            manifest,
            {
                "safe_area_and_text_visibility",
                "material_semantic_identity",
                "opening_hook_visual_consistency",
            },
        )
        checks = {
            item["check_id"]: item
            for item in DeterministicVisualInspector().inspect(
                manifest=repaired,
                render_report={},
            )["checks"]
        }

        self.assertNotEqual(repaired, manifest)
        self.assertEqual(4, len(manifest["compositions"]))
        self.assertGreater(len(repaired["compositions"]), 4)
        for caption in repaired["captions"]:
            containing_scenes = [
                composition
                for composition in repaired["compositions"]
                if composition["start_ms"] <= caption["start_ms"]
                and caption["end_ms"] <= composition["end_ms"]
            ]
            self.assertEqual(1, len(containing_scenes), caption["id"])
        for check_id in (
            "safe_area_and_text_visibility",
            "material_semantic_identity",
            "opening_hook_visual_consistency",
        ):
            self.assertEqual("pass", checks[check_id]["result"], check_id)

    def test_visual_inspector_rejects_a_caption_split_across_compositions(self):
        from server.content_domains.ai_edit_v3.production import DeterministicVisualInspector

        manifest = {
            "duration_ms": 18000,
            "source_video": {"path": "media/source.mp4", "silent": True},
            "assets": [],
            "compositions": [
                {
                    "id": "composition_001", "start_ms": 0, "end_ms": 8000,
                    "layout_id": "speaker_fullscreen", "asset_ids": [],
                },
                {
                    "id": "composition_002", "start_ms": 8000, "end_ms": 14000,
                    "layout_id": "speaker_left_info_right", "asset_ids": [],
                },
                {
                    "id": "composition_003", "start_ms": 14000, "end_ms": 18000,
                    "layout_id": "speaker_fullscreen", "asset_ids": [],
                },
            ],
            "captions": [
                {"id": "caption_001", "start_ms": 0, "end_ms": 6000, "text": "One"},
                {"id": "caption_002", "start_ms": 6000, "end_ms": 10000, "text": "Split"},
                {"id": "caption_003", "start_ms": 10000, "end_ms": 18000, "text": "Three"},
            ],
        }

        checks = {
            item["check_id"]: item
            for item in DeterministicVisualInspector().inspect(
                manifest=manifest,
                render_report={},
            )["checks"]
        }

        self.assertEqual("fail", checks["caption_fact_accuracy"]["result"])
        self.assertEqual("fail", checks["safe_area_and_text_visibility"]["result"])

    def test_repair_manifest_rejects_changed_but_still_failing_structure(self):
        from server.content_domains.ai_edit_v3.production import _repair_render_manifest

        manifest = {
            "duration_ms": 18000,
            "source_video": {"path": "media/source.mp4", "silent": True},
            "assets": [],
            "compositions": [{
                "id": "composition_001", "scene_id": "scene_01",
                "start_ms": 0, "end_ms": 18000,
                "layout_id": "speaker_fullscreen", "asset_ids": [],
            }],
            "captions": [
                {"id": "caption_001", "start_ms": 0, "end_ms": 6000, "text": "One"},
                {"id": "caption_002", "start_ms": 6000, "end_ms": 12000, "text": "Two"},
                {"id": "caption_003", "start_ms": 12000, "end_ms": 18000, "text": "Three"},
            ],
        }

        with self.assertRaisesRegex(ValueError, "repair_manifest_unresolved"):
            _repair_render_manifest(manifest, {"safe_area_and_text_visibility"})

    def test_repair_manifest_rejects_unsupported_failed_check_before_render(self):
        from server.content_domains.ai_edit_v3.production import _repair_render_manifest

        manifest = {
            "duration_ms": 18000,
            "source_video": {"path": "media/source.mp4", "silent": True},
            "assets": [],
            "compositions": [
                {
                    "id": "composition_001", "scene_id": "scene_01",
                    "start_ms": 0, "end_ms": 9000,
                    "layout_id": "speaker_fullscreen", "asset_ids": [],
                },
                {
                    "id": "composition_002", "scene_id": "scene_02",
                    "start_ms": 9000, "end_ms": 18000,
                    "layout_id": "speaker_left_info_right", "asset_ids": [],
                },
            ],
            "captions": [
                {"id": "caption_001", "start_ms": 0, "end_ms": 6000, "text": "One"},
                {"id": "caption_002", "start_ms": 6000, "end_ms": 12000, "text": "Two"},
                {"id": "caption_003", "start_ms": 12000, "end_ms": 18000, "text": "Three"},
            ],
        }

        with self.assertRaisesRegex(ValueError, "repair_manifest_unsupported"):
            _repair_render_manifest(
                manifest,
                {"safe_area_and_text_visibility", "audio_integrity"},
            )

    def test_repair_manifest_keeps_split_composition_ids_schema_bounded(self):
        from server.content_domains.ai_edit_v3.production import _repair_render_manifest

        manifest = {
            "duration_ms": 18000,
            "source_video": {"path": "media/source.mp4", "silent": True},
            "assets": [{"id": "material_01", "kind": "image"}],
            "compositions": [
                {
                    "id": "a" * 64, "scene_id": "scene_01",
                    "start_ms": 0, "end_ms": 12000,
                    "layout_id": "speaker_fullscreen", "asset_ids": [],
                },
                {
                    "id": "composition_002", "scene_id": "scene_02",
                    "start_ms": 12000, "end_ms": 18000,
                    "layout_id": "speaker_left_info_right", "asset_ids": ["material_01"],
                },
            ],
            "captions": [
                {"id": "caption_001", "start_ms": 0, "end_ms": 6000, "text": "One"},
                {"id": "caption_002", "start_ms": 6000, "end_ms": 12000, "text": "Two"},
                {"id": "caption_003", "start_ms": 12000, "end_ms": 18000, "text": "Three"},
            ],
        }

        repaired = _repair_render_manifest(
            manifest,
            {"safe_area_and_text_visibility"},
        )
        ids = [item["id"] for item in repaired["compositions"]]

        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(len(item) <= 64 for item in ids))

    def test_repair_manifest_keeps_similar_long_split_ids_unique(self):
        from server.content_domains.ai_edit_v3.production import _repair_render_manifest

        manifest = {
            "duration_ms": 24000,
            "source_video": {"path": "media/source.mp4", "silent": True},
            "assets": [{"id": "material_01", "kind": "image"}],
            "compositions": [
                {
                    "id": "a" * 63 + "b", "scene_id": "scene_01",
                    "start_ms": 0, "end_ms": 12000,
                    "layout_id": "speaker_fullscreen", "asset_ids": [],
                },
                {
                    "id": "a" * 63 + "c", "scene_id": "scene_02",
                    "start_ms": 12000, "end_ms": 24000,
                    "layout_id": "speaker_left_info_right", "asset_ids": ["material_01"],
                },
            ],
            "captions": [
                {"id": "caption_001", "start_ms": 0, "end_ms": 6000, "text": "One"},
                {"id": "caption_002", "start_ms": 6000, "end_ms": 12000, "text": "Two"},
                {"id": "caption_003", "start_ms": 12000, "end_ms": 18000, "text": "Three"},
                {"id": "caption_004", "start_ms": 18000, "end_ms": 24000, "text": "Four"},
            ],
        }

        repaired = _repair_render_manifest(
            manifest,
            {"safe_area_and_text_visibility"},
        )
        ids = [item["id"] for item in repaired["compositions"]]

        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(len(item) <= 64 for item in ids))

    def test_repair_manifest_rejects_a_new_structural_regression(self):
        from server.content_domains.ai_edit_v3.production import _repair_render_manifest

        manifest = {
            "duration_ms": 18000,
            "source_video": {"path": "media/source.mp4", "silent": True},
            "assets": [{"id": "material_01", "kind": "image"}],
            "compositions": [
                {
                    "id": "composition_001", "scene_id": "scene_01",
                    "start_ms": 0, "end_ms": 6000,
                    "layout_id": "product_hero", "asset_ids": ["material_01"],
                },
                {
                    "id": "composition_002", "scene_id": "scene_02",
                    "start_ms": 6000, "end_ms": 12000,
                    "layout_id": "speaker_right_evidence_left", "asset_ids": [],
                },
                {
                    "id": "composition_003", "scene_id": "scene_03",
                    "start_ms": 12000, "end_ms": 18000,
                    "layout_id": "speaker_left_info_right", "asset_ids": [],
                },
            ],
            "captions": [
                {"id": "caption_001", "start_ms": 0, "end_ms": 6000, "text": "One"},
                {"id": "caption_002", "start_ms": 6000, "end_ms": 12000, "text": "Two"},
                {"id": "caption_003", "start_ms": 12000, "end_ms": 18000, "text": "Three"},
            ],
        }

        with self.assertRaisesRegex(ValueError, "repair_manifest_unresolved"):
            _repair_render_manifest(
                manifest,
                {"opening_hook_visual_consistency"},
            )

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
        from server.content_domains.ai_edit_v3.production import (
            _layout_slot_bindings,
            _scene_asset_ids,
            _validate_layout_authoritative_content,
            _validate_layout_source_requirements,
        )

        known = ["evidence_01", "product_01", "context_01", "decoration_01"]
        scenes = [
            {"id": "scene_01", "material_slots": [{"id": "evidence_01"}]},
            {"id": "scene_02", "material_slots": []},
            {"id": "scene_03", "material_slots": [{"id": "product_01"}]},
        ]

        self.assertEqual(
            [["evidence_01"], [], ["product_01"]],
            [_scene_asset_ids(scene, known) for scene in scenes],
        )
        self.assertEqual(
            [
                {"slot_id": "primary", "asset_id": "product_01"},
                {"slot_id": "detail", "asset_id": "context_01"},
            ],
            _layout_slot_bindings({"layout_id": "product_hero", "material_slots": [
                {"id": "evidence_01", "purpose": "evidence", "priority": "optional"},
                {"id": "product_01", "purpose": "product", "priority": "required"},
                {"id": "context_01", "purpose": "context", "priority": "optional"},
                {"id": "decoration_01", "purpose": "decoration", "priority": "optional"},
            ]}, known),
        )
        with self.assertRaisesRegex(ValueError, "scene_layout_required_slot_missing"):
            _layout_slot_bindings({"layout_id": "product_hero", "material_slots": [{"id": "evidence_01", "purpose": "evidence", "priority": "optional"}]}, known)
        self.assertEqual(
            [{"slot_id": "evidence", "asset_id": "evidence_01"}],
            _layout_slot_bindings({"layout_id": "speaker_fullscreen", "material_slots": [{"id": "evidence_01", "purpose": "evidence", "priority": "optional"}]}, known),
        )
        for layout_id in ("speaker_left_info_right", "speaker_right_evidence_left"):
            self.assertEqual(
                [{"slot_id": "evidence", "asset_id": "evidence_01"}],
                _layout_slot_bindings({"layout_id": layout_id, "material_slots": [{"id": "evidence_01", "purpose": "evidence", "priority": "optional"}]}, known),
            )
        self.assertEqual(
            [
                {"slot_id": "primary", "asset_id": "product_01"},
                {"slot_id": "detail", "asset_id": "context_01"},
            ],
            _layout_slot_bindings({"layout_id": "material_fullscreen_speaker_pip", "material_slots": [
                {"id": "context_01", "purpose": "context", "priority": "optional"},
                {"id": "product_01", "purpose": "product", "priority": "required"},
            ]}, known),
        )
        with self.assertRaisesRegex(ValueError, "scene_layout_required_slot_missing"):
            _layout_slot_bindings({"layout_id": "material_fullscreen_speaker_pip", "material_slots": [{"id": "evidence_01", "purpose": "evidence", "priority": "optional"}]}, known)
        self.assertEqual(
            [{"slot_id": "accent", "asset_id": "decoration_01"}],
            _layout_slot_bindings({"layout_id": "steps_stack", "material_slots": [{"id": "context_01", "purpose": "context", "priority": "optional"}, {"id": "decoration_01", "purpose": "decoration", "priority": "optional"}]}, known),
        )
        with self.assertRaisesRegex(ValueError, "scene_layout_binding_invalid"):
            _layout_slot_bindings({"layout_id": "product_hero", "material_slots": [{"id": "product_01", "purpose": "product", "priority": "optional"}]}, known)
        with self.assertRaisesRegex(ValueError, "scene_layout_binding_duplicate"):
            _layout_slot_bindings({"layout_id": "speaker_fullscreen", "material_slots": [{"id": "evidence_01", "purpose": "evidence", "priority": "optional"}, {"id": "context_01", "purpose": "evidence", "priority": "optional"}]}, known)
        for layout_id in ("editorial_collage", "comparison_split"):
            self.assertEqual(
                [
                    {"slot_id": "primary", "asset_id": "product_01"},
                    {"slot_id": "detail", "asset_id": "context_01"},
                ],
                _layout_slot_bindings({"layout_id": layout_id, "material_slots": [
                    {"id": "context_01", "purpose": "context", "priority": "optional"},
                    {"id": "product_01", "purpose": "product", "priority": "required"},
                ]}, known),
            )
            with self.assertRaisesRegex(ValueError, "scene_layout_required_slot_missing"):
                _layout_slot_bindings({"layout_id": layout_id, "material_slots": [],}, known)
        for layout_id, purpose, slot_id in (
            ("number_proof", "evidence", "evidence"),
            ("quote_reversal", "evidence", "evidence"),
            ("method_timeline", "decoration", "accent"),
            ("cta_offer", "decoration", "accent"),
        ):
            self.assertEqual(
                [{"slot_id": slot_id, "asset_id": "evidence_01"}],
                _layout_slot_bindings({"layout_id": layout_id, "material_slots": [{"id": "evidence_01", "purpose": purpose, "priority": "optional"}]}, known),
            )
        for layout_id in ("speaker_fullscreen", "speaker_left_info_right", "speaker_right_evidence_left", "material_fullscreen_speaker_pip"):
            with self.assertRaisesRegex(ValueError, "scene_layout_required_source_missing"):
                _validate_layout_source_requirements({"layout_id": layout_id}, source_video=None)
            _validate_layout_source_requirements({"layout_id": layout_id}, source_video={"path": "media/source.mp4"})
        _validate_layout_source_requirements({"layout_id": "product_hero"}, source_video=None)
        authoritative = [{"id": "caption_001", "start_ms": 0, "end_ms": 2000, "text": "权威文案"}]
        for layout_id in ("number_proof", "quote_reversal", "method_timeline", "cta_offer"):
            scene = {"layout_id": layout_id, "start_ms": 0, "end_ms": 2000}
            _validate_layout_authoritative_content(scene, captions=authoritative)
            with self.assertRaisesRegex(ValueError, "scene_layout_authoritative_content_missing"):
                _validate_layout_authoritative_content(scene, captions=[])
            with self.assertRaisesRegex(ValueError, "scene_layout_authoritative_content_missing"):
                _validate_layout_authoritative_content(scene, captions=[{"id": "caption_002", "start_ms": 2000, "end_ms": 3000, "text": "其他场景"}])

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
            def __init__(self):
                self.prompts = []

            def generate(self, *, output_path, **kwargs):
                self.prompts.append(kwargs["prompt"])
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
            generator = Generator()
            coordinator.image_generator = generator
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
        prompt = generator.prompts[0].lower()
        for required_phrase in (
            "supplemental b-roll or graphic",
            "no presenter",
            "no talking head",
            "no portrait",
            "no recognizable person",
        ):
            self.assertIn(required_phrase, prompt)

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
