from __future__ import annotations

import copy
import functools
import json
import hashlib
import os
from pathlib import Path
import shutil
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


@functools.lru_cache(maxsize=1)
def _valid_mp3_bytes() -> bytes:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise unittest.SkipTest("ffmpeg is required for production media tests")
    with tempfile.TemporaryDirectory() as folder:
        target = Path(folder) / "voice.mp3"
        completed = subprocess.run(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=0.25",
                "-codec:a", "libmp3lame", str(target),
            ],
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0 or not target.is_file():
            raise unittest.SkipTest("ffmpeg cannot create the MP3 test fixture")
        return target.read_bytes()


def _node22_command() -> list[str]:
    configured = os.environ.get("AI_EDIT_V3_TEST_NODE22_BIN")
    if configured:
        return [str(Path(configured).resolve(strict=True))]
    system_node = shutil.which("node")
    if system_node:
        version = subprocess.run(
            [system_node, "--version"], capture_output=True, text=True, check=False,
        )
        if version.returncode == 0 and version.stdout.strip().startswith("v22."):
            return [system_node]
    npm = shutil.which("npm")
    if not npm:
        raise RuntimeError("ai_edit_v3_test_node22_missing")
    return [npm, "exec", "--yes", "--package=node@22", "--", "node"]


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
    def test_python_and_node_consume_the_same_renderer_owned_overlay_catalog(self):
        from server.content_domains.ai_edit_v3.overlay_catalog import load_overlay_placement_catalog

        renderer = Path(__file__).resolve().parents[1] / "server" / "ai_edit_v3_renderer"
        python_catalog = load_overlay_placement_catalog(renderer)
        system_node = shutil.which("node")
        self.assertIsNotNone(system_node, "system Node runtime is required for compatibility coverage")
        commands = [[str(Path(system_node).resolve(strict=True))], _node22_command()]
        for command in commands:
            probe = subprocess.run(
                [*command, "--input-type=module", "-e", "import {OVERLAY_PLACEMENT_CATALOG} from './src/registry/overlays/overlay-placement-contract.mjs';process.stdout.write(JSON.stringify(OVERLAY_PLACEMENT_CATALOG));"],
                cwd=renderer, capture_output=True, text=True, check=False,
            )
            self.assertEqual(0, probe.returncode, probe.stderr)
            self.assertEqual(python_catalog, json.loads(probe.stdout))
        self.assertEqual(52, len(python_catalog["entries"]))

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

        captured_capabilities = []
        captured_requests = []
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"AI_EDIT_V3_VISUAL_PROGRAM_ENABLED": "1"}, clear=False):
            coordinator = object.__new__(ProductionStageCoordinator)
            coordinator.work_root = Path(directory)
            coordinator.renderer_root = Path(__file__).resolve().parents[1] / "server" / "ai_edit_v3_renderer"
            coordinator.store = type("Store", (), {
                "environment": "test",
                "resolve_request_uploads_for_owner": lambda *_args, **_kwargs: {
                    "materials": []
                },
            })()
            coordinator.director = object()
            coordinator.renderer = SimpleNamespace(registry_sha256="sha256:" + "d" * 64)
            coordinator._capabilities = lambda _ratio: capabilities

            def planned(job_id, request_sha256):
                root = coordinator._root(job_id)
                (root / "normalized.json").write_text(json.dumps({"input_type": "uploaded_audio", "ratio": "9:16", "sha256": "a" * 64}), encoding="utf-8")
                (root / "timeline.json").write_text(json.dumps({"duration_ms": 4000, "captions": [{"id": "caption_001", "text": "authoritative", "start_ms": 0, "end_ms": 4000}], "source_segments": [], "authoritative_text_sha256": None, "alignment_coverage": 1.0}), encoding="utf-8")
                job = {"job_id": job_id, "owner_id": "alice", "request_sha256": request_sha256, "stage_input_sha256": "0" * 64, "normalized_request_json": '{"input_type":"uploaded_audio"}'}
                def capture_decision(context, *_args, **_kwargs):
                    captured_capabilities.append(context.request["capabilities"])
                    captured_requests.append(context.request)
                    return decision
                with patch("server.content_domains.ai_edit_v3.production.build_director_request", return_value={}), patch("server.content_domains.ai_edit_v3.production.generate_director_decision", side_effect=capture_decision), patch("server.content_domains.ai_edit_v3.production.compile_edit_plan", return_value={"version": "2.0", "visual_program_version": "1.0"}):
                    coordinator._stage("planning", job, SimpleNamespace(deadline_at=time.time() + 60, claim=None, stage_attempt_id="attempt"))
                return json.loads((root / "visual-program.json").read_text(encoding="utf-8"))["variation_seed"]

            first = planned("job-request-a", "1" * 64)
            second = planned("job-request-b", "2" * 64)
            replay = planned("job-replay", "1" * 64)
            replay_again = planned("job-replay", "1" * 64)

        self.assertNotEqual(first, second)
        self.assertEqual(first, replay)
        self.assertEqual(replay, replay_again)
        self.assertEqual(4, len(captured_capabilities))
        self.assertTrue(all(item["output_ratio"] == "9:16" for item in captured_capabilities))
        self.assertTrue(all("overlay_placement_budgets" not in item for item in captured_capabilities))
        self.assertTrue(all(
            item["overlay_placements"]["standard_caption"]
            == [{"placement": "subtitle_safe", "max_chars": 96, "max_lines": 3}]
            for item in captured_capabilities
        ))
        speaker_layouts = {
            "speaker_fullscreen", "speaker_left_info_right",
            "speaker_right_evidence_left", "material_fullscreen_speaker_pip",
        }
        self.assertTrue(all(
            not speaker_layouts.intersection(
                request["scene_candidates"][0]["allowed_layout_ids"]
            )
            for request in captured_requests
        ))
        self.assertTrue(all(
            request["scene_candidates"][0]["available_material_ids"] == []
            for request in captured_requests
        ))

    def test_visual_planning_rejects_missing_or_invalid_persisted_request_sha(self):
        from server.content_domains.ai_edit_v3.production import ProductionStageCoordinator

        decision = SimpleNamespace(value={"theme_profile_id": "editorial_clean", "design_intent": {"density": "balanced", "motion_energy": "medium", "image_fit": "cover", "decoration_intensity": "medium"}}, decision_sha256="b" * 64, provider_request_id="director-1", raw_output_sha256="c" * 64)
        capabilities = {"layout_capabilities": ["speaker_fullscreen"], "layout_variants": {"speaker_fullscreen": ["emphasis_b"]}, "overlay_capabilities": ["standard_caption"], "animation_capabilities": ["fade"], "transition_capabilities": ["hard_cut"], "theme_capabilities": {}, "theme_profile_ids": ["editorial_clean"], "overlay_variants": {}, "overlay_animation_targets": {}, "layout_animation_targets": {}}
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"AI_EDIT_V3_VISUAL_PROGRAM_ENABLED": "1"}, clear=False):
            coordinator = object.__new__(ProductionStageCoordinator)
            coordinator.work_root = Path(directory)
            coordinator.renderer_root = Path(__file__).resolve().parents[1] / "server" / "ai_edit_v3_renderer"
            coordinator.store = type("Store", (), {
                "environment": "test",
                "resolve_request_uploads_for_owner": lambda *_args, **_kwargs: {
                    "materials": []
                },
            })()
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
                    manifests.append({"version": "2.0", "schema_sha256": "schema-v2", "registry_sha256": "registry", "renderer_environment": {"renderer_build_id": "build"}, "output_spec": {"ratio": "9:16", "width": 1080, "height": 1920, "fps_num": 30, "fps_den": 1}, "duration_ms": 4000, "master_audio": {"path": "media/master.wav"}, "source_video": None, "theme_profile_id": profile, "design_intent": intent, "variation_seed": seed, "design_tokens": _resolve_design_tokens(profile, intent, seed), "compositions": [{"id": "scene_1", "start_ms": 0, "end_ms": 4000, "overlay_ids": [], "overlay_instances": [], "authoritative_content": {"headline": {"text": "authoritative headline", "source_caption_ids": []}, "highlight": {"text": "authoritative highlight", "source_caption_ids": []}}}]})
        program = "import fs from 'node:fs'; import {parseCanonicalJson} from './src/parse-canonical-json.mjs'; import {validateManifest} from './src/validate-manifest.mjs'; const values=parseCanonicalJson(fs.readFileSync(process.argv.at(-1))); for (const value of values) validateManifest(value,{rendererBuildId:'build',registrySha256:'sha256:registry',schemaSha256ByVersion:{'2.0':'schema-v2'}});"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifests.json"; path.write_text(json.dumps(manifests), encoding="utf-8")
            accepted = subprocess.run([*_node22_command(), "--input-type=module", "-e", program, str(path)], cwd=renderer, capture_output=True, text=True, check=False)
            manifests[0]["design_tokens"]["--hf-bg"] = "#tampered"
            path.write_text(json.dumps(manifests), encoding="utf-8")
            rejected = subprocess.run([*_node22_command(), "--input-type=module", "-e", program, str(path)], cwd=renderer, capture_output=True, text=True, check=False)
        self.assertEqual(0, accepted.returncode, accepted.stderr)
        self.assertNotEqual(0, rejected.returncode)
        self.assertIn("manifest_design_tokens_mismatch", rejected.stderr)

    def test_python_frozen_overlay_facts_pass_strict_node_validation_and_real_v2_compile(self):
        from server.content_domains.ai_edit_v3.contracts import schema_sha256
        from server.content_domains.ai_edit_v3.production import (
            _freeze_overlay_authoritative_content,
            _resolve_design_tokens,
        )

        renderer = Path(__file__).resolve().parents[1] / "server" / "ai_edit_v3_renderer"
        registry_probe = subprocess.run(
            [*_node22_command(), "--input-type=module", "-e", "import {getRegistrySha256} from './src/registry/index.mjs';process.stdout.write(getRegistrySha256());"],
            cwd=renderer, capture_output=True, text=True, check=False,
        )
        self.assertEqual(0, registry_probe.returncode, registry_probe.stderr)
        registry_sha = registry_probe.stdout.removeprefix("sha256:")
        intent = {"density": "balanced", "motion_energy": "medium", "image_fit": "cover", "decoration_intensity": "medium"}
        authoritative = _freeze_overlay_authoritative_content({
            "headline": {"text_kind": "verbatim", "text": "品牌 PRODUCT-X 售价 499 元", "source_caption_ids": ["caption_01"]},
            "highlight": {"text_kind": "compressed", "text": "证据保持 42.5%", "source_caption_ids": ["caption_01"]},
        })
        manifest = {
            "version": "2.0", "schema_sha256": schema_sha256("render-manifest-v2.schema.json"), "registry_sha256": registry_sha,
            "renderer_environment": {"renderer_build_id": "build"},
            "output_spec": {"ratio": "9:16", "width": 1080, "height": 1920, "fps_num": 30, "fps_den": 1},
            "duration_ms": 4000, "edit_plan_sha256": "a" * 64,
            "theme": {"palette_id": "midnight_gold", "typography_id": "editorial_sans", "density": "balanced", "motion_energy": "medium", "image_fit": "cover"},
            "seed": 7, "theme_profile_id": "editorial_clean", "design_intent": intent,
            "variation_seed": "0123456789abcdef", "design_tokens": _resolve_design_tokens("editorial_clean", intent, "0123456789abcdef"),
            "source_video": {"path": "media/source.mp4", "silent": True},
            "source_segments": [{"id": "segment_01", "source_path": "media/source.mp4", "source_start_ms": 0, "source_end_ms": 4000, "output_start_ms": 0, "output_end_ms": 4000}],
            "master_audio": {"path": "media/master.wav"}, "assets": [],
            "compositions": [{
                "id": "composition_01", "scene_id": "scene_01", "start_ms": 0, "end_ms": 4000,
                "layout_id": "speaker_fullscreen", "layout_variant": "clean_center",
                "overlay_ids": ["info_card", "info_card"],
                "overlay_instances": [
                    {"instance_id": "info_left", "component_id": "info_card", "content_ref": "headline", "placement": "left_panel"},
                    {"instance_id": "info_right", "component_id": "info_card", "content_ref": "highlight", "placement": "right_panel"},
                ],
                "authoritative_content": authoritative, "animations": [], "transition": "hard_cut", "asset_ids": [], "layout_slot_bindings": [],
            }],
            "captions": [{"id": "caption_01", "start_ms": 0, "end_ms": 4000, "text": "权威字幕"}],
        }
        program = """import fs from 'node:fs'; import path from 'node:path'; import {parseCanonicalJson} from './src/parse-canonical-json.mjs'; import {validateManifest} from './src/validate-manifest.mjs'; import {compileProjectV2} from './src/compile-project-v2.mjs'; const manifest=parseCanonicalJson(fs.readFileSync(process.argv.at(-2))); const expected={rendererBuildId:'build',registrySha256:`sha256:${manifest.registry_sha256}`,schemaSha256ByVersion:{'2.0':manifest.schema_sha256}}; const valid=validateManifest(manifest,expected); await compileProjectV2({manifest:valid,outputRoot:process.argv.at(-1)}); const html=fs.readFileSync(path.join(process.argv.at(-1),'compositions','composition_01.html'),'utf8'); if(!html.includes('info_left_info_card')||!html.includes('info_right_info_card')||!html.includes('品牌 PRODUCT-X 售价 499 元')||!html.includes('证据保持 42.5%')) throw new Error('overlay_cross_language_compile_invalid');"""
        with tempfile.TemporaryDirectory() as directory:
            manifest_path = Path(directory) / "manifest.json"
            output_path = Path(directory) / "project"
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            result = subprocess.run([*_node22_command(), "--input-type=module", "-e", program, str(manifest_path), str(output_path)], cwd=renderer, capture_output=True, text=True, check=False)
        self.assertEqual(0, result.returncode, result.stderr)

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
        from server.content_domains.ai_edit_v3.capability_catalog import (
            load_visual_capability_catalog,
            validate_visual_capability_catalog,
        )
        from server.content_domains.ai_edit_v3.overlay_catalog import (
            load_overlay_placement_catalog,
        )
        from server.content_domains.ai_edit_v3.production import visual_program_capabilities

        with self.assertRaisesRegex(ValueError, "visual_program_capabilities_incomplete"):
            visual_program_capabilities({"layout_capabilities": ["quote_reversal"]})

        renderer = Path(__file__).resolve().parents[1] / "server" / "ai_edit_v3_renderer"
        capabilities = {
            **load_visual_capability_catalog(renderer),
            "overlay_placement_budgets": load_overlay_placement_catalog(renderer),
            "output_ratio": "9:16",
        }
        for field, capability_id in (
            ("layout_variants", "speaker_fullscreen"),
            ("layout_animation_targets", "speaker_fullscreen"),
            ("overlay_variants", "standard_caption"),
            ("overlay_animation_targets", "standard_caption"),
        ):
            broken = json.loads(json.dumps(capabilities))
            del broken[field][capability_id]
            with self.subTest(field=field), self.assertRaisesRegex(
                ValueError, "visual_program_capabilities_incomplete"
            ):
                visual_program_capabilities(broken)

        frozen_catalog = load_visual_capability_catalog(renderer)
        for field, replacement in (
            ("identity_match_capability", True),
            (
                "transition_capabilities",
                [*frozen_catalog["transition_capabilities"], "card_match_cut"],
            ),
        ):
            broken = json.loads(json.dumps(frozen_catalog))
            broken[field] = replacement
            with self.subTest(field=field), self.assertRaisesRegex(
                ValueError, "visual_capability_catalog_invalid"
            ):
                validate_visual_capability_catalog(broken)
            broken.update({
                "overlay_placement_budgets": load_overlay_placement_catalog(renderer),
                "output_ratio": "9:16",
            })
            with self.subTest(field=field), self.assertRaisesRegex(
                ValueError, "visual_program_capabilities_incomplete"
            ):
                visual_program_capabilities(broken)

        for field, capability_id, unsupported in (
            ("overlay_variants", "standard_caption", "primary"),
            ("overlay_animation_targets", "standard_caption", "caption"),
            ("layout_animation_targets", "speaker_fullscreen", "root"),
        ):
            broken = json.loads(json.dumps(frozen_catalog))
            broken[field][capability_id] = [unsupported]
            with self.subTest(field=field, unsupported=unsupported), self.assertRaisesRegex(
                ValueError, "visual_capability_catalog_invalid"
            ):
                validate_visual_capability_catalog(broken)
            broken.update({
                "overlay_placement_budgets": load_overlay_placement_catalog(renderer),
                "output_ratio": "9:16",
            })
            with self.subTest(field=field, unsupported=unsupported), self.assertRaisesRegex(
                ValueError, "visual_program_capabilities_incomplete"
            ):
                visual_program_capabilities(broken)

    def test_visual_program_gate_zero_keeps_legacy_director_path(self):
        from server.content_domains.ai_edit_v3.production import ProductionStageCoordinator

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"AI_EDIT_V3_VISUAL_PROGRAM_ENABLED": "0"}, clear=False
        ):
            coordinator = object.__new__(ProductionStageCoordinator)
            coordinator.work_root = Path(directory)
            coordinator.renderer_root = Path(__file__).resolve().parents[1] / "server" / "ai_edit_v3_renderer"
            coordinator.store = type("Store", (), {
                "environment": "test",
                "resolve_request_uploads_for_owner": lambda *_args, **_kwargs: {
                    "materials": []
                },
            })()
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

    def test_visual_program_gate_one_uses_renderer_catalog_and_compiles_plan(self):
        from server.content_domains.ai_edit_v3.production import ProductionStageCoordinator

        captured_capabilities = []
        captured_requests = []
        image = b"verified-user-image"

        class Store:
            environment = "test"

            @staticmethod
            def resolve_request_uploads_for_owner(*_args, **_kwargs):
                return {
                    "materials": [{
                        "material_id": "material-1234",
                        "cos_key": "test/alice/material.png",
                        "mime_type": "image/png",
                        "size_bytes": len(image),
                        "sha256": hashlib.sha256(image).hexdigest(),
                        "metadata_json": '{"width":1080,"height":1920,"format":"png"}',
                    }],
                }

        class Cos:
            @staticmethod
            def download_file(_key, target):
                Path(target).write_bytes(image)

        class Analyzer:
            @staticmethod
            def describe_materials(images, *, deadline_at):
                del deadline_at
                assert [item["upload_alias"] for item in images] == ["upload_01"]
                return {"descriptors": [{
                    "upload_alias": "upload_01",
                    "semantic": "用户上传的产品实拍图",
                    "subject_type": "product",
                    "composition": "centered close-up",
                    "supported_ratios": ["9:16"],
                    "risk_labels": [],
                }]}

        decision = SimpleNamespace(
            value={
                "version": "1.0",
                "creative_concept": "内容驱动的商业讲解",
                "narrative_pattern": "question_proof",
                "theme_profile_id": "editorial_clean",
                "design_intent": {
                    "density": "balanced",
                    "motion_energy": "medium",
                    "image_fit": "cover",
                    "decoration_intensity": "medium",
                },
                "scene_directives": [{
                    "scene_id": "candidate_01",
                    "narrative_role": "hook",
                    "layout_id": "speaker_fullscreen",
                    "layout_variant": "clean_center",
                    "headline": {
                        "text_kind": "verbatim",
                        "source_caption_ids": ["caption_001"],
                    },
                    "overlay_instances": [{
                        "instance_id": "caption_overlay",
                        "component_id": "standard_caption",
                        "content_ref": "headline",
                        "placement": "subtitle_safe",
                    }],
                    "material_bindings": [],
                    "material_slot_directives": [],
                    "animations": [{
                        "target_id": "caption_overlay",
                        "preset": "subtitle_pop",
                        "direction": "up",
                        "duration_ms": 400,
                        "delay_ms": 0,
                    }],
                    "transition": "hard_cut",
                    "sound_events": [],
                }],
                "audio_intent": {
                    "bgm_description": "克制的无歌词电子氛围",
                    "energy": "medium",
                    "dialogue_priority": True,
                },
            },
            decision_sha256="b" * 64,
            provider_request_id="director-request-1",
            raw_output_sha256="c" * 64,
        )
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"AI_EDIT_V3_VISUAL_PROGRAM_ENABLED": "1"}, clear=False
        ):
            coordinator = object.__new__(ProductionStageCoordinator)
            coordinator.work_root = Path(directory)
            coordinator.renderer_root = Path(__file__).resolve().parents[1] / "server" / "ai_edit_v3_renderer"
            coordinator.store = Store()
            coordinator.cos = Cos()
            coordinator.material_analyzer = Analyzer()
            coordinator.director = object()
            coordinator.renderer = SimpleNamespace(registry_sha256="sha256:" + "d" * 64)
            root = coordinator._root("job-gate-one")
            (root / "normalized.json").write_text(json.dumps({"input_type": "uploaded_video", "ratio": "9:16", "sha256": "a" * 64}), encoding="utf-8")
            (root / "timeline.json").write_text(json.dumps({"duration_ms": 4000, "captions": [{"id": "caption_001", "text": "authoritative caption", "start_ms": 0, "end_ms": 4000}], "source_segments": [{"id": "segment_01", "text": "authoritative caption", "start_ms": 0, "end_ms": 4000, "protected": False, "output_start_ms": None, "output_end_ms": None}], "authoritative_text_sha256": None, "alignment_coverage": 1.0}), encoding="utf-8")
            def capture_decision(context, *_args, **_kwargs):
                captured_capabilities.append(context.capabilities)
                captured_requests.append(context.request)
                return decision
            def fake_jpeg(_source, destination, *, deadline_at):
                del deadline_at
                Path(destination).parent.mkdir(parents=True, exist_ok=True)
                Path(destination).write_bytes(b"\xff\xd8safe-jpeg")
                return {"width": 288, "height": 512}

            with patch("server.content_domains.ai_edit_v3.production._prepare_material_analysis_jpeg", side_effect=fake_jpeg), patch("server.content_domains.ai_edit_v3.production.generate_director_decision", side_effect=capture_decision) as visual_path, patch("server.content_domains.ai_edit_v3.production.generate_edit_plan") as legacy_path:
                outcome = coordinator._stage(
                    "planning",
                    {
                        "job_id": "job-gate-one",
                        "owner_id": "alice",
                        "request_sha256": "1" * 64,
                        "stage_input_sha256": "0" * 64,
                        "normalized_request_json": '{"input_type":"uploaded_video","material_asset_ids":["material-1234"]}',
                    },
                    SimpleNamespace(deadline_at=time.time() + 60, claim=None, stage_attempt_id="attempt"),
                )
            plan = json.loads((root / "plan.json").read_text(encoding="utf-8"))

        self.assertEqual("resolving_materials", outcome.next_state)
        visual_path.assert_called_once()
        legacy_path.assert_not_called()
        self.assertEqual(1, len(captured_capabilities))
        capabilities = captured_capabilities[0]
        self.assertIn("clean_center", capabilities["layout_variants"]["speaker_fullscreen"])
        self.assertEqual([], capabilities["overlay_variants"]["standard_caption"])
        self.assertEqual([], capabilities["overlay_animation_targets"]["standard_caption"])
        self.assertEqual([], capabilities["layout_animation_targets"]["speaker_fullscreen"])
        self.assertIn("editorial_clean", capabilities["theme_profile_ids"])
        self.assertFalse(capabilities["identity_match_capability"])
        self.assertNotIn("card_match_cut", capabilities["transition_capabilities"])
        self.assertEqual("overlay-placement-v1", capabilities["overlay_placement_budgets"]["version"])
        self.assertEqual("9:16", capabilities["output_ratio"])
        prompt_capabilities = captured_requests[0]["capabilities"]
        self.assertNotIn("overlay_placement_budgets", prompt_capabilities)
        self.assertEqual(
            [{"placement": "subtitle_safe", "max_chars": 96, "max_lines": 3}],
            prompt_capabilities["overlay_placements"]["standard_caption"],
        )
        self.assertEqual(
            capabilities["layout_variants"],
            prompt_capabilities["layout_variants"],
        )
        self.assertFalse(prompt_capabilities["identity_match_capability"])
        self.assertEqual("semantic_slots_only", prompt_capabilities["material_binding_mode"])
        candidate = captured_requests[0]["scene_candidates"][0]
        self.assertIn("speaker_fullscreen", candidate["allowed_layout_ids"])
        self.assertTrue(all(
            layout_id in {
                "speaker_fullscreen", "speaker_left_info_right",
                "speaker_right_evidence_left", "material_fullscreen_speaker_pip",
            }
            for layout_id in candidate["allowed_layout_ids"]
        ))
        self.assertEqual([], candidate["available_material_ids"])
        self.assertEqual(1, len(captured_requests[0]["current_materials"]))
        self.assertEqual(
            "用户上传的产品实拍图",
            captured_requests[0]["current_materials"][0]["semantic"],
        )
        self.assertNotIn("material_id", captured_requests[0]["current_materials"][0])
        self.assertNotIn("sha256", captured_requests[0]["current_materials"][0])
        prompt_json = json.dumps(captured_requests[0], ensure_ascii=False)
        self.assertNotIn("material-1234", prompt_json)
        self.assertNotIn(hashlib.sha256(image).hexdigest(), prompt_json)
        self.assertEqual(
            capabilities["layout_requirements"],
            prompt_capabilities["layout_requirements"],
        )
        self.assertEqual(6, prompt_capabilities["max_required_material_slots"])
        self.assertEqual(40, prompt_capabilities["max_total_material_slots"])
        self.assertEqual(
            {"opening_requires_speaker": True, "max_hidden_ratio": 0.4},
            prompt_capabilities["speaker_visibility_policy"],
        )
        self.assertEqual("2.0", plan["version"])
        self.assertEqual("1.0", plan["visual_program_version"])
        self.assertEqual("speaker_fullscreen", plan["scenes"][0]["layout_id"])
        self.assertEqual("clean_center", plan["scenes"][0]["layout_variant"])

    def test_visual_program_gate_one_fails_closed_before_real_director_call_when_catalog_is_incomplete(self):
        from server.content_domains.ai_edit_v3.production import ProductionStageCoordinator

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"AI_EDIT_V3_VISUAL_PROGRAM_ENABLED": "1"}, clear=False
        ):
            coordinator = object.__new__(ProductionStageCoordinator)
            coordinator.work_root = Path(directory)
            coordinator.renderer_root = Path(__file__).resolve().parents[1] / "server" / "ai_edit_v3_renderer"
            coordinator.store = type("Store", (), {"environment": "test", "resolve_request_uploads_for_owner": lambda *_args, **_kwargs: {"materials": []}})()
            coordinator.director = object()
            incomplete_capabilities = {
                "layout_capabilities": ["speaker_fullscreen"],
                "overlay_capabilities": ["standard_caption"],
                "animation_capabilities": ["subtitle_pop"],
                "transition_capabilities": ["hard_cut"],
                "theme_capabilities": {},
            }
            root = coordinator._root("job-gate-incomplete")
            (root / "normalized.json").write_text(json.dumps({"input_type": "uploaded_audio", "ratio": "9:16", "sha256": "a" * 64}), encoding="utf-8")
            (root / "timeline.json").write_text(json.dumps({"duration_ms": 4000, "captions": [{"id": "caption_001", "text": "authoritative caption", "start_ms": 0, "end_ms": 4000}], "source_segments": [{"id": "segment_01", "text": "authoritative caption", "start_ms": 0, "end_ms": 4000, "protected": False, "output_start_ms": None, "output_end_ms": None}], "authoritative_text_sha256": None, "alignment_coverage": 1.0}), encoding="utf-8")
            with patch("server.content_domains.ai_edit_v3.production.load_visual_capability_catalog", return_value=incomplete_capabilities), patch("server.content_domains.ai_edit_v3.production.generate_director_decision") as visual_path, patch("server.content_domains.ai_edit_v3.production.generate_edit_plan") as legacy_path:
                with self.assertRaisesRegex(ValueError, "visual_program_capabilities_incomplete"):
                    coordinator._stage("planning", {"job_id": "job-gate-incomplete", "owner_id": "alice", "stage_input_sha256": "0" * 64, "normalized_request_json": '{"input_type":"uploaded_audio"}'}, SimpleNamespace(deadline_at=time.time() + 60, claim=None, stage_attempt_id="attempt"))

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

    def test_visual_director_uses_strict_json_transport_and_explicit_contract(self):
        class StrictDirectorClient:
            def generate_edit_plan(self, *args, **kwargs):
                raise AssertionError("visual director must use strict JSON transport")

            def generate_director_decision(
                self, system_prompt, user_prompt, *, timeout_seconds=None
            ):
                self.system_prompt = system_prompt
                self.user_prompt = user_prompt
                self.timeout_seconds = timeout_seconds
                return V2Result(
                    provider="dashscope",
                    capability="director",
                    request_id="director-request-1",
                    payload={"content": "{}"},
                    cost_units=23,
                    elapsed_ms=19,
                )

        client = StrictDirectorClient()
        provider = QwenCompiledDirector(client, timeout_seconds=120)
        request = {
            "scene_candidates": [{
                "id": "candidate_01",
                "caption_ids": ["caption_001"],
                "authoritative_text": "权威原文",
            }],
            "capabilities": {
                "layout_capabilities": ["speaker_fullscreen"],
                "layout_variants": {"speaker_fullscreen": ["clean_center"]},
                "overlay_capabilities": ["standard_caption"],
                "animation_capabilities": ["subtitle_pop"],
                "transition_capabilities": ["hard_cut"],
                "theme_profile_ids": ["editorial_clean"],
            },
        }

        result = provider.generate_decision(
            request,
            purpose="initial",
            idempotency_key="director-1",
            deadline_at=9_999_999_999,
        )

        self.assertEqual("{}", result.payload["content"])
        self.assertEqual({"tokens": 23}, result.usage)
        self.assertEqual(120, client.timeout_seconds)
        self.assertEqual(request, json.loads(client.user_prompt))
        for required_field in (
            '"scene_directives"',
            '"material_slot_directives"',
            '"source_caption_ids"',
            '"audio_intent"',
        ):
            self.assertIn(required_field, client.system_prompt)
        self.assertIn("candidate_01", client.user_prompt)
        self.assertIn("material_bindings 必须为空", client.system_prompt)
        self.assertIn("必须等于同场景 overlay instance_id", client.system_prompt)
        self.assertIn("max_chars", client.system_prompt)
        self.assertIn("semantic 必须逐字复制", client.system_prompt)
        self.assertIn("repair.expected_constraint", client.system_prompt)
        self.assertIn("顶层只能包含", client.system_prompt)
        self.assertIn("不得包装在 output_contract", client.system_prompt)
        self.assertIn("不得输出 null、字符串、数组或 text 字段", client.system_prompt)
        self.assertIn(
            '{"text_kind":"compressed","source_caption_ids":["caption_001"]}',
            client.system_prompt,
        )
        self.assertIn("例子只展示结构", client.system_prompt)
        self.assertIn("必须使用当前 scene_candidate.caption_ids", client.system_prompt)
        self.assertIn(
            "scene_directives.length 必须等于 scene_candidates.length",
            client.system_prompt,
        )
        self.assertIn("minimum_distinct_signatures", client.system_prompt)
        self.assertIn("max_adjacent_identical", client.system_prompt)
        self.assertIn("max_hidden_ratio", client.system_prompt)
        self.assertIn(
            "scene_signatures_meet_distinct_and_adjacency_policy",
            client.system_prompt,
        )
        self.assertIn("自动生成的 required 素材槽", client.system_prompt)
        self.assertIn("不得要求人物、人脸、讲师、团队、客户", client.system_prompt)
        self.assertIn("特定品牌、真实产品包装、真实门店或事实证据", client.system_prompt)
        self.assertIn("抽象概念图、流程示意图或非人物环境素材", client.system_prompt)

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

    def test_legacy_compiler_keeps_all_ten_current_material_descriptors(self):
        capabilities = {
            "layout_capabilities": ["product_hero", "editorial_collage"],
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
            {
                "id": f"caption_{index + 1:03d}",
                "start_ms": index * 8000,
                "end_ms": (index + 1) * 8000,
                "text": f"素材说明 {index + 1}",
            }
            for index in range(10)
        ]
        plan = QwenCompiledDirector._compile(
            {
                "timeline": {
                    "duration_ms": 80000,
                    "captions": captions,
                    "source_segments": [],
                },
                "source": {"input_type": "uploaded_audio"},
                "current_materials": [
                    {
                        "upload_alias": f"upload_{index + 1:02d}",
                        "semantic": f"用户素材 {index + 1}",
                        "purpose": "context",
                        "scene_index": index,
                    }
                    for index in range(10)
                ],
                "generate_missing_material": False,
                "capabilities": capabilities,
                "ratio": "9:16",
            },
            {"motion_energy": "medium"},
        )

        self.assertEqual(10, len(plan["materials"]))
        self.assertEqual(
            [f"用户素材 {index}" for index in range(1, 11)],
            [item["semantic"] for item in plan["materials"]],
        )

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
    def test_material_review_receipt_hash_includes_policy_version(self):
        from server.content_domains.ai_edit_v3 import production

        payload = production._material_review_receipt_request(
            scene_id="scene_01",
            slot_id="evidence",
            semantic="abstract workflow diagram",
            forbidden_subjects=("person", "face"),
            cos_key="test/ai-edit-v3/owner/job/materials/generated-01.png",
            source_metadata={"sha256": "a" * 64},
        )
        self.assertEqual(
            "material-review-policy-v2",
            payload["review_policy_version"],
        )
        legacy_payload = dict(payload)
        legacy_payload.pop("review_policy_version")
        self.assertNotEqual(
            hashlib.sha256(production.canonical_json(payload)).hexdigest(),
            hashlib.sha256(production.canonical_json(legacy_payload)).hexdigest(),
        )

    def test_material_descriptor_text_rejects_secret_prefixes_and_control_characters(self):
        from server.content_domains.ai_edit_v3 import production

        for unsafe in (
            "sk-test-secret",
            "token sk_test_secret",
            "password=secret-value",
            "api_key: secret-value",
            "signature=secret-value",
            "credential=secret-value",
            "product\x00photo",
        ):
            with self.subTest(unsafe=repr(unsafe)), self.assertRaisesRegex(
                ValueError, "material_descriptor_invalid",
            ):
                production._material_descriptor_payload(
                    {"descriptors": [{
                        "upload_alias": "upload_01",
                        "semantic": unsafe,
                        "subject_type": "product",
                        "composition": "centered close-up",
                        "supported_ratios": ["9:16"],
                        "risk_labels": [],
                    }]},
                    expected_aliases=("upload_01",),
                )

    def test_material_descriptor_text_allows_sk_inside_ordinary_words(self):
        from server.content_domains.ai_edit_v3 import production

        for safe_text in (
            "desk-side product close-up",
            "mask_style visual",
            "signature pose",
            "credential document",
        ):
            with self.subTest(safe_text=safe_text):
                result = production._material_descriptor_payload(
                    {"descriptors": [{
                        "upload_alias": "upload_01",
                        "semantic": safe_text,
                        "subject_type": "product",
                        "composition": safe_text,
                        "supported_ratios": ["9:16"],
                        "risk_labels": [],
                    }]},
                    expected_aliases=("upload_01",),
                )
                self.assertEqual(safe_text, result["descriptors"][0]["semantic"])

    def test_material_analysis_reencode_is_bounded_jpeg_and_deterministic(self):
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            self.skipTest("ffmpeg and ffprobe are required")
        from server.content_domains.ai_edit_v3.production import (
            _prepare_material_analysis_jpeg,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.ppm"
            source.write_bytes(
                b"P6\n1024 768\n255\n" + bytes((18, 126, 214)) * (1024 * 768)
            )
            first = root / "first.jpg"
            second = root / "second.jpg"

            first_dimensions = _prepare_material_analysis_jpeg(
                source, first, deadline_at=time.time() + 30,
            )
            second_dimensions = _prepare_material_analysis_jpeg(
                source, second, deadline_at=time.time() + 30,
            )

            first_bytes = first.read_bytes()
            self.assertEqual({"width": 512, "height": 384}, first_dimensions)
            self.assertEqual(first_dimensions, second_dimensions)
            self.assertEqual(first_bytes, second.read_bytes())
            self.assertTrue(first_bytes.startswith(b"\xff\xd8"))
            self.assertLessEqual(len(first_bytes), 256 * 1024)

    def test_script_tts_retries_cos_persistence_without_provider_resubmit(self):
        from server.content_domains.ai_edit_v3.production import ProductionStageCoordinator
        from server.content_domains.ai_edit_v3.providers import SubmissionUnknown
        from server.content_domains.ai_edit_v3.providers.base import ProviderResult

        with tempfile.TemporaryDirectory() as folder:
            audio = _valid_mp3_bytes()

            class Tts:
                calls = 0

                def generate(self, **kwargs):
                    self.calls += 1
                    kwargs["output_path"].parent.mkdir(parents=True, exist_ok=True)
                    kwargs["output_path"].write_bytes(audio)
                    return ProviderResult(
                        provider="website-cosyvoice", capability="tts",
                        request_id="website-tts-stable",
                        payload={
                            "sha256": hashlib.sha256(audio).hexdigest(),
                            "size_bytes": len(audio), "mime_type": "audio/mpeg",
                        },
                        usage={"characters": 7}, elapsed_ms=5,
                    )

            class Store:
                environment = "test"

                def __init__(self):
                    self.task = None

                def get_provider_task_for_claim(self, *_args):
                    return self.task

                def record_provider_intent(self, *args):
                    self.task = {
                        "status": "intent_recorded", "stage": args[1],
                        "stage_attempt_id": args[2], "provider": args[3],
                        "capability": args[4], "request_sha256": args[6],
                    }

                def claim_provider_submission(self, *_args):
                    self.task = {**self.task, "status": "submitting"}
                    return True

                def bind_provider_result(self, *args):
                    self.task = {
                        **self.task, "status": "completed",
                        "external_id": args[2], "result_json": json.dumps(args[4]),
                    }

                def recover_provider_result(self, *_args):
                    raise AssertionError("completed receipt must replay directly")

            class Cos:
                calls = 0
                files = {}

                def put_file(self, path, key, *_args, **_kwargs):
                    self.calls += 1
                    self.files[key] = Path(path).read_bytes()
                    if self.calls == 1:
                        raise RuntimeError("temporary COS failure")

                def download_file(self, key, path):
                    Path(path).write_bytes(self.files[key])

            coordinator = object.__new__(ProductionStageCoordinator)
            coordinator.work_root = Path(folder) / "work"
            coordinator.owner_hmac_secret = b"production-tts-test-secret"
            coordinator.store = Store()
            coordinator.tts = Tts()
            coordinator.cos = Cos()
            job = {
                "job_id": "job-tts-cos-retry", "owner_id": "alice",
                "stage_input_sha256": "1" * 64,
                "normalized_request_json": json.dumps({
                    "input_type": "script_to_audio_video",
                    "tts_input": {"text": "content", "voice_id": "voice"},
                    "ratio": "16:9", "creation_mode": "ai_auto",
                    "material_asset_ids": [],
                }),
            }
            context = SimpleNamespace(
                claim=object(), stage_attempt_id="attempt-1",
                deadline_at=time.time() + 60,
            )
            with self.assertRaisesRegex(SubmissionUnknown, "cos_persistence_pending"):
                coordinator._stage("generating_voice", job, context)

            target = coordinator._root(job["job_id"]) / "generated-voice.mp3"
            target.write_bytes(b"corrupt-local-artifact")
            outcome = coordinator._stage("generating_voice", job, context)

            self.assertEqual("normalizing", outcome.next_state)
            self.assertEqual(1, coordinator.tts.calls)
            self.assertEqual(2, coordinator.cos.calls)
            self.assertEqual(audio, target.read_bytes())

    def test_script_tts_recovers_unbound_receipt_from_private_cos_without_resubmit(self):
        from server.content_domains.ai_edit_v3.production import ProductionStageCoordinator
        from server.content_domains.ai_edit_v3.providers.base import ProviderResult

        with tempfile.TemporaryDirectory() as folder:
            audio = _valid_mp3_bytes()

            class Tts:
                calls = 0

                def generate(self, **kwargs):
                    self.calls += 1
                    target = kwargs["output_path"]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(audio)
                    return ProviderResult(
                        provider="website-cosyvoice",
                        capability="tts",
                        request_id="website-tts-stable",
                        payload={
                            "sha256": hashlib.sha256(audio).hexdigest(),
                            "size_bytes": len(audio),
                            "mime_type": "audio/mpeg",
                            "characters": 4,
                        },
                        usage={"characters": 4},
                        elapsed_ms=5,
                    )

            class Cos:
                files = {}
                fail_download_once = False

                def put_file(self, path, key, content_type, private, if_absent):
                    self.files[key] = Path(path).read_bytes()

                def download_file(self, key, path):
                    Path(path).parent.mkdir(parents=True, exist_ok=True)
                    if self.fail_download_once:
                        self.fail_download_once = False
                        Path(path).write_bytes(b"partial-nonempty-download")
                        raise RuntimeError("interrupted COS download")
                    Path(path).write_bytes(self.files[key])

            class Store:
                environment = "test"

                def __init__(self):
                    self.task = None
                    self.bind_calls = 0
                    self.recovery_calls = 0

                def get_provider_task_for_claim(self, *_args):
                    return self.task

                def record_provider_intent(self, *args):
                    self.task = {
                        "status": "intent_recorded",
                        "stage": args[1],
                        "stage_attempt_id": args[2],
                        "provider": args[3],
                        "capability": args[4],
                        "request_sha256": args[6],
                    }
                    return self.task

                def claim_provider_submission(self, *_args):
                    self.task = {**self.task, "status": "submitting"}
                    return True

                def bind_provider_result(self, *_args):
                    self.bind_calls += 1
                    raise RuntimeError("injected receipt bind failure")

                def recover_provider_result(self, *args):
                    self.recovery_calls += 1
                    self.task = {
                        **self.task,
                        "status": "completed",
                        "stage_attempt_id": args[2],
                        "external_id": args[7],
                        "result_json": json.dumps(args[8]),
                    }
                    return self.task

            coordinator = object.__new__(ProductionStageCoordinator)
            coordinator.work_root = Path(folder) / "work"
            coordinator.owner_hmac_secret = b"production-tts-test-secret"
            coordinator.store = Store()
            coordinator.tts = Tts()
            coordinator.cos = Cos()
            job = {
                "job_id": "job-tts-recovery",
                "owner_id": "alice",
                "stage_input_sha256": "1" * 64,
                "normalized_request_json": json.dumps({
                    "input_type": "script_to_audio_video",
                    "tts_input": {"text": "content", "voice_id": "my-clone"},
                    "ratio": "16:9",
                    "creation_mode": "ai_auto",
                    "material_asset_ids": [],
                }),
            }
            first_context = SimpleNamespace(
                claim=object(),
                stage_attempt_id="attempt-1",
                deadline_at=time.time() + 60,
            )
            with self.assertRaisesRegex(RuntimeError, "receipt bind failure"):
                coordinator._stage("generating_voice", job, first_context)

            target = coordinator._root(job["job_id"]) / "generated-voice.mp3"
            object_key = (
                f"test/ai-edit-v3/{coordinator._owner_hmac('alice')}/"
                f"{job['job_id']}/working/generated-voice.mp3"
            )
            coordinator.cos.files[object_key] = target.read_bytes()
            target.unlink()

            second_context = SimpleNamespace(
                claim=object(),
                stage_attempt_id="attempt-2",
                deadline_at=time.time() + 60,
            )
            coordinator.cos.fail_download_once = True
            from server.content_domains.ai_edit_v3.providers import SubmissionUnknown
            with self.assertRaisesRegex(SubmissionUnknown, "provider_receipt_pending"):
                coordinator._stage("generating_voice", job, second_context)
            self.assertFalse(target.exists())
            self.assertEqual(0, coordinator.store.recovery_calls)
            self.assertEqual(1, coordinator.tts.calls)

            third_context = SimpleNamespace(
                claim=object(),
                stage_attempt_id="attempt-3",
                deadline_at=time.time() + 60,
            )
            outcome = coordinator._stage("generating_voice", job, third_context)

            self.assertEqual("normalizing", outcome.next_state)
            self.assertEqual(1, coordinator.tts.calls)
            self.assertEqual(1, coordinator.store.bind_calls)
            self.assertEqual(1, coordinator.store.recovery_calls)
            self.assertEqual(audio, target.read_bytes())

    def test_script_to_audio_generates_private_recoverable_voice_source(self):
        from server.content_domains.ai_edit_v3.production import ProductionStageCoordinator
        from server.content_domains.ai_edit_v3.providers.base import ProviderResult

        with tempfile.TemporaryDirectory() as folder:
            class Tts:
                calls = 0

                def generate(self, **kwargs):
                    self.calls += 1
                    target = kwargs["output_path"]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    audio = _valid_mp3_bytes()
                    target.write_bytes(audio)
                    return ProviderResult(
                        provider="website-cosyvoice", capability="tts",
                        request_id="tts-request-1",
                        payload={
                            "sha256": hashlib.sha256(audio).hexdigest(),
                            "size_bytes": len(audio), "mime_type": "audio/mpeg", "characters": 4,
                        },
                        usage={"characters": 4}, elapsed_ms=5,
                    )

            class Cos:
                files = {}

                def put_file(self, path, key, content_type, private, if_absent):
                    self.files[key] = Path(path).read_bytes()

                def download_file(self, key, path):
                    Path(path).parent.mkdir(parents=True, exist_ok=True)
                    Path(path).write_bytes(self.files[key])

            coordinator = object.__new__(ProductionStageCoordinator)
            coordinator.work_root = Path(folder) / "work"
            coordinator.owner_hmac_secret = b"production-tts-test-secret"
            coordinator.store = SimpleNamespace(environment="test")
            coordinator.tts = Tts()
            coordinator.cos = Cos()
            job = {
                "job_id": "job-tts-1", "owner_id": "alice",
                "stage_input_sha256": "1" * 64,
                "normalized_request_json": json.dumps({
                    "input_type": "script_to_audio_video",
                    "tts_input": {"text": "讲解内容", "voice_id": "my-clone"},
                    "ratio": "16:9", "creation_mode": "ai_auto",
                    "material_asset_ids": [],
                }),
            }
            context = SimpleNamespace(deadline_at=time.time() + 60)

            outcome = coordinator._stage("generating_voice", job, context)
            voice_path = coordinator._root("job-tts-1") / "generated-voice.mp3"
            voice_path.unlink()
            recovered, text = coordinator._source(job, context)

            self.assertEqual(outcome.next_state, "normalizing")
            self.assertEqual(coordinator.tts.calls, 1)
            self.assertEqual(recovered.read_bytes(), _valid_mp3_bytes())
            self.assertEqual(text, "讲解内容")

    def test_existing_audio_source_requires_owned_ready_catalog_record(self):
        from server.content_domains.ai_edit_v3.production import ProductionStageCoordinator

        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "owned.mp3"
            source.write_bytes(b"audio")

            class Catalog:
                record = {
                    "asset_id": "17",
                    "owner": "alice",
                    "status": "ready",
                    "local_path": str(source.resolve()),
                }

                def resolve_audio_asset(self, owner, asset_id):
                    return dict(self.record) if asset_id == "17" else None

            coordinator = object.__new__(ProductionStageCoordinator)
            coordinator.work_root = Path(folder) / "work"
            coordinator.source_catalog = Catalog()
            job = {
                "job_id": "job-audio-1",
                "owner_id": "alice",
                "normalized_request_json": json.dumps({
                    "input_type": "existing_audio",
                    "source_asset_id": "17",
                }),
            }

            resolved, authoritative_text = coordinator._source(
                job, SimpleNamespace(deadline_at=time.time() + 60)
            )
            self.assertEqual(resolved, source.resolve())
            self.assertIsNone(authoritative_text)

            coordinator.source_catalog.record["owner"] = "mallory"
            with self.assertRaisesRegex(ValueError, "audio_source_not_found"):
                coordinator._source(job, SimpleNamespace(deadline_at=time.time() + 60))

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

    def test_frozen_repair_instruction_changes_only_the_target_scene(self):
        from server.content_domains.ai_edit_v3.production import (
            _freeze_repair_instruction,
            _repair_render_manifest,
        )

        manifest = {
            "duration_ms": 12000,
            "source_video": {"path": "media/source.mp4", "silent": True},
            "assets": [{"id": "material_01", "kind": "image"}],
            "compositions": [
                {
                    "id": "composition_001", "scene_id": "scene_01",
                    "start_ms": 0, "end_ms": 6000,
                    "layout_id": "speaker_fullscreen", "asset_ids": ["material_01"],
                },
                {
                    "id": "composition_002", "scene_id": "scene_02",
                    "start_ms": 6000, "end_ms": 12000,
                    "layout_id": "product_hero", "asset_ids": ["material_01"],
                },
            ],
            "captions": [
                {"id": "caption_001", "start_ms": 0, "end_ms": 6000, "text": "One"},
                {"id": "caption_002", "start_ms": 6000, "end_ms": 12000, "text": "Two"},
            ],
            "renderer_environment": {"renderer_build_id": "sha256:" + "1" * 64},
            "variation_seed": "0123456789abcdef",
        }
        instruction = _freeze_repair_instruction(manifest, ({
            "scene_id": "scene_02",
            "reason_code": "material_semantic_identity",
            "allowed_action": "speaker_fallback",
        },))

        repaired = _repair_render_manifest(manifest, instruction)

        self.assertRegex(instruction["instruction_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(manifest["compositions"][0], repaired["compositions"][0])
        self.assertEqual("speaker_fullscreen", repaired["compositions"][1]["layout_id"])
        self.assertEqual([], repaired["compositions"][1]["asset_ids"])
        for protected in (
            "duration_ms", "source_video", "assets", "captions",
            "renderer_environment", "variation_seed",
        ):
            self.assertEqual(manifest[protected], repaired[protected], protected)

    def test_repair_instruction_rejects_tampered_sha_unknown_scene_or_action(self):
        from server.content_domains.ai_edit_v3.production import (
            _freeze_repair_instruction,
            _repair_render_manifest,
        )

        manifest = {
            "duration_ms": 6000,
            "source_video": {"path": "media/source.mp4", "silent": True},
            "assets": [],
            "compositions": [{
                "id": "composition_001", "scene_id": "scene_01",
                "start_ms": 0, "end_ms": 6000,
                "layout_id": "speaker_fullscreen", "asset_ids": [],
            }],
            "captions": [{
                "id": "caption_001", "start_ms": 0, "end_ms": 6000,
                "text": "One",
            }],
        }
        valid = _freeze_repair_instruction(manifest, ({
            "scene_id": "scene_01",
            "reason_code": "face_product_obstruction",
            "allowed_action": "speaker_fallback",
        },))
        tampered = copy.deepcopy(valid)
        tampered["directives"][0]["scene_id"] = "scene_02"
        with self.assertRaisesRegex(ValueError, "repair_instruction_sha_invalid"):
            _repair_render_manifest(manifest, tampered)

        for directive in (
            {"scene_id": "scene_02", "reason_code": "face_product_obstruction", "allowed_action": "speaker_fallback"},
            {"scene_id": "scene_01", "reason_code": "face_product_obstruction", "allowed_action": "https://evil.invalid"},
        ):
            with self.subTest(directive=directive):
                with self.assertRaisesRegex(ValueError, "repair_instruction_invalid"):
                    _freeze_repair_instruction(manifest, (directive,))

    def test_quality_repair_payload_persists_and_reloads_only_frozen_instruction(self):
        from server.content_domains.ai_edit_v3.production import (
            _quality_repair_payload,
            _repair_instruction_from_quality,
        )

        manifest = {
            "duration_ms": 6000,
            "source_video": {"path": "media/source.mp4", "silent": True},
            "assets": [],
            "compositions": [{
                "id": "composition_001", "scene_id": "scene_01",
                "start_ms": 0, "end_ms": 6000,
                "layout_id": "product_hero", "asset_ids": [],
            }],
            "captions": [{
                "id": "caption_001", "start_ms": 0, "end_ms": 6000,
                "text": "One",
            }],
        }
        quality = SimpleNamespace(
            can_repair=True,
            repair_directives=({
                "scene_id": "scene_01",
                "reason_code": "face_product_obstruction",
                "allowed_action": "speaker_fallback",
            },),
        )

        payload = _quality_repair_payload(manifest, quality)
        loaded = _repair_instruction_from_quality(manifest, payload)

        self.assertEqual(payload["repair_instruction_sha256"], loaded["instruction_sha256"])
        self.assertEqual("scene_01", loaded["directives"][0]["scene_id"])
        with self.assertRaisesRegex(ValueError, "repair_quality_invalid"):
            _repair_instruction_from_quality(manifest, {
                "can_repair": True,
                "repairable_ids": ["face_product_obstruction"],
            })

    def test_repair_instruction_rejects_ambiguous_duplicate_scene_identity(self):
        from server.content_domains.ai_edit_v3.production import _freeze_repair_instruction

        manifest = {
            "duration_ms": 6000,
            "compositions": [
                {"id": "composition_001", "scene_id": "scene_01", "start_ms": 0, "end_ms": 3000},
                {"id": "composition_002", "scene_id": "scene_01", "start_ms": 3000, "end_ms": 6000},
            ],
        }
        with self.assertRaisesRegex(ValueError, "repair_instruction_invalid"):
            _freeze_repair_instruction(manifest, ({
                "scene_id": "scene_01",
                "reason_code": "face_product_obstruction",
                "allowed_action": "speaker_fallback",
            },))

    def test_v2_speaker_fallback_clears_slot_bindings_and_refreezes_with_orphan_asset(self):
        from server.content_domains.ai_edit_v3.contracts import freeze_render_manifest
        from server.content_domains.ai_edit_v3.overlay_catalog import load_overlay_placement_catalog
        from server.content_domains.ai_edit_v3.production import (
            _freeze_repair_instruction,
            _repair_render_manifest,
        )

        repository = Path(__file__).resolve().parents[1]
        manifest = json.loads((
            repository / "tests/fixtures/ai_edit_v3/valid-render-manifest-v2.json"
        ).read_text(encoding="utf-8"))
        scene = manifest["compositions"][0]
        scene.update({
            "layout_id": "product_hero",
            "layout_variant": "center_pedestal",
            "asset_ids": ["asset_01"],
            "layout_slot_bindings": [{"slot_id": "primary", "asset_id": "asset_01"}],
        })
        instruction = _freeze_repair_instruction(manifest, ({
            "scene_id": "scene_01",
            "reason_code": "face_product_obstruction",
            "allowed_action": "speaker_fallback",
        },))

        repaired = _repair_render_manifest(manifest, instruction)

        self.assertEqual("speaker_fullscreen", repaired["compositions"][0]["layout_id"])
        self.assertEqual("clean_center", repaired["compositions"][0]["layout_variant"])
        self.assertEqual([], repaired["compositions"][0]["asset_ids"])
        self.assertEqual([], repaired["compositions"][0]["layout_slot_bindings"])
        self.assertEqual(manifest["assets"], repaired["assets"])
        with tempfile.TemporaryDirectory() as folder:
            sandbox = Path(folder)
            media = sandbox / "media"
            media.mkdir()
            (media / "source.mp4").write_bytes(b"video")
            (media / "master.wav").write_bytes(b"audio")
            (media / "image.png").write_bytes(b"image")
            frozen = freeze_render_manifest(
                repaired,
                sandbox / "render-manifest.json",
                sandbox_root=sandbox,
                overlay_placement_catalog=load_overlay_placement_catalog(
                    repository / "server/ai_edit_v3_renderer"
                ),
            )
        self.assertRegex(frozen.sha256, r"^[0-9a-f]{64}$")

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

    def test_visual_inspector_blocks_three_identical_adjacent_scene_structures(self):
        from server.content_domains.ai_edit_v3.production import DeterministicVisualInspector

        layouts = [
            ("speaker_fullscreen", "clean_center"),
            ("speaker_fullscreen", "clean_center"),
            ("speaker_fullscreen", "clean_center"),
            ("speaker_left_info_right", "speaker_focus"),
        ]
        manifest = {
            "duration_ms": 24000,
            "source_video": {"path": "media/source.mp4", "silent": True},
            "assets": [],
            "compositions": [{
                "id": f"composition_{index:03d}", "scene_id": f"scene_{index:02d}",
                "start_ms": (index - 1) * 6000, "end_ms": index * 6000,
                "layout_id": layout_id, "layout_variant": variant,
                "asset_ids": [],
            } for index, (layout_id, variant) in enumerate(layouts, 1)],
            "captions": [{
                "id": f"caption_{index:03d}", "start_ms": (index - 1) * 6000,
                "end_ms": index * 6000, "text": f"Point {index}",
            } for index in range(1, 5)],
        }

        checks = {item["check_id"]: item for item in DeterministicVisualInspector().inspect(
            manifest=manifest, render_report={},
        )["checks"]}

        self.assertEqual("fail", checks["safe_area_and_text_visibility"]["result"])
        self.assertIn("adjacent_scene_structure", checks["safe_area_and_text_visibility"]["reason"])
        self.assertFalse(checks["safe_area_and_text_visibility"]["repairable"])

    def test_visual_inspector_collapses_safe_split_parts_of_one_logical_scene(self):
        from server.content_domains.ai_edit_v3.production import DeterministicVisualInspector

        manifest = {
            "duration_ms": 24000,
            "source_video": {"path": "media/source.mp4", "silent": True},
            "assets": [],
            "compositions": [
                {
                    "id": f"composition_001_r{part:02d}", "scene_id": "scene_01",
                    "start_ms": (part - 1) * 6000, "end_ms": part * 6000,
                    "layout_id": "speaker_fullscreen", "layout_variant": "clean_center",
                    "asset_ids": [],
                } for part in range(1, 4)
            ] + [{
                "id": "composition_002", "scene_id": "scene_02",
                "start_ms": 18000, "end_ms": 24000,
                "layout_id": "speaker_left_info_right", "layout_variant": "speaker_focus",
                "asset_ids": [],
            }],
            "captions": [{
                "id": f"caption_{index:03d}", "start_ms": (index - 1) * 6000,
                "end_ms": index * 6000, "text": f"Point {index}",
            } for index in range(1, 5)],
        }

        checks = {item["check_id"]: item for item in DeterministicVisualInspector().inspect(
            manifest=manifest, render_report={},
        )["checks"]}

        self.assertEqual("pass", checks["safe_area_and_text_visibility"]["result"])

    def test_visual_inspector_accepts_same_layout_with_real_structural_variants(self):
        from server.content_domains.ai_edit_v3.production import DeterministicVisualInspector

        variants = ["clean_center", "headline_top", "caption_sidebar", "clean_center"]
        manifest = {
            "duration_ms": 24000,
            "source_video": {"path": "media/source.mp4", "silent": True},
            "assets": [],
            "compositions": [{
                "id": f"composition_{index:03d}", "scene_id": f"scene_{index:02d}",
                "start_ms": (index - 1) * 6000, "end_ms": index * 6000,
                "layout_id": "speaker_fullscreen", "layout_variant": variant,
                "asset_ids": [],
            } for index, variant in enumerate(variants, 1)],
            "captions": [{
                "id": f"caption_{index:03d}", "start_ms": (index - 1) * 6000,
                "end_ms": index * 6000, "text": f"Point {index}",
            } for index in range(1, 5)],
        }

        checks = {item["check_id"]: item for item in DeterministicVisualInspector().inspect(
            manifest=manifest, render_report={},
        )["checks"]}

        self.assertEqual("pass", checks["safe_area_and_text_visibility"]["result"])
        self.assertEqual("pass", checks["opening_hook_visual_consistency"]["result"])

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
            _freeze_overlay_authoritative_content,
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
        self.assertEqual(
            {
                "headline": {"text": "品牌 PRODUCT-X 售价 499 元", "source_caption_ids": ["caption_001"]},
                "highlight": {"text": "证据 42.5%", "source_caption_ids": ["caption_002"]},
            },
            _freeze_overlay_authoritative_content({
                "headline": {"text_kind": "verbatim", "text": "品牌 PRODUCT-X 售价 499 元", "source_caption_ids": ["caption_001"]},
                "highlight": {"text_kind": "compressed", "text": "证据 42.5%", "source_caption_ids": ["caption_002"]},
            }),
        )
        self.assertEqual(
            {"headline": {"text": "章节", "source_caption_ids": []}, "highlight": {"text": "行动", "source_caption_ids": []}},
            _freeze_overlay_authoritative_content({
                "headline": {"text_kind": "ui_label", "ui_label_id": "chapter"},
                "highlight": {"text_kind": "ui_label", "ui_label_id": "cta_prompt"},
            }),
        )
        with self.assertRaisesRegex(ValueError, "scene_overlay_authoritative_content_invalid"):
            _freeze_overlay_authoritative_content({"headline": {"text": "safe", "html": "<b>unsafe</b>"}, "highlight": {"text": "safe"}})
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
        from server.content_domains.ai_edit_v3 import production
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
            job = {
                "job_id": "job-material",
                "owner_id": "alice",
                "stage_input_sha256": "0" * 64,
                "normalized_request_json": json.dumps({
                    "input_type": "platform_talking_head",
                    "material_asset_ids": ["mat-1"],
                }),
            }
            trusted = coordinator._frozen_bound_materials(job)
            descriptor_input = production._material_descriptor_input_sha256(
                "alice", trusted,
            )
            (coordinator._root("job-material") / "material-descriptors.json").write_text(
                json.dumps({
                    "contract": "ai-edit-v3-material-descriptors-v1",
                    "version": "1.0",
                    "input_sha256": descriptor_input,
                    "items": [{
                        "upload_alias": "upload_01",
                        "material_id": "mat-1",
                        "sha256": hashlib.sha256(image).hexdigest(),
                        "semantic": "用户上传的产品图",
                        "subject_type": "product",
                        "composition": "centered close-up",
                        "supported_ratios": ["9:16"],
                        "risk_labels": [],
                    }],
                }, ensure_ascii=False),
                encoding="utf-8",
            )
            outcome = coordinator._stage(
                "resolving_materials",
                job,
                object(),
            )
            frozen = json.loads((coordinator._root("job-material") / "materials.json").read_text("utf-8"))

        self.assertEqual("generating_images", outcome.next_state)
        self.assertEqual(1, outcome.checkpoint["material_count"])
        self.assertEqual("mat-1", frozen["items"][0]["material_id"])
        self.assertEqual(("alice", None, ["mat-1"], "test"), coordinator.store.call)

    def test_two_uploaded_images_freeze_descriptors_match_second_and_skip_generation_on_retry(self):
        from server.content_domains.ai_edit_v3 import production
        from server.content_domains.ai_edit_v3.production import ProductionStageCoordinator

        image_bytes = {
            "test/owner/green.png": b"verified-green-product",
            "test/owner/blue.png": b"verified-blue-store",
        }
        real_ids = ("material-real-green", "material-real-blue")
        hashes = {
            key: hashlib.sha256(value).hexdigest()
            for key, value in image_bytes.items()
        }

        class Store:
            environment = "test"

            def resolve_request_uploads_for_owner(self, owner, *, source_upload_id, material_ids, environment):
                self.last_scope = (owner, source_upload_id, tuple(material_ids), environment)
                return {
                    "source_upload": None,
                    "materials": [
                        {
                            "material_id": real_ids[0],
                            "cos_key": "test/owner/green.png",
                            "mime_type": "image/png",
                            "size_bytes": len(image_bytes["test/owner/green.png"]),
                            "sha256": hashes["test/owner/green.png"],
                            "metadata_json": '{"width":1080,"height":1080,"format":"png"}',
                        },
                        {
                            "material_id": real_ids[1],
                            "cos_key": "test/owner/blue.png",
                            "mime_type": "image/png",
                            "size_bytes": len(image_bytes["test/owner/blue.png"]),
                            "sha256": hashes["test/owner/blue.png"],
                            "metadata_json": '{"width":1920,"height":1080,"format":"png"}',
                        },
                    ],
                }

        class Cos:
            def download_file(self, key, target):
                Path(target).write_bytes(image_bytes[key])

        class Analyzer:
            def __init__(self):
                self.calls = []

            def describe_materials(self, images, *, deadline_at):
                self.calls.append(copy.deepcopy(images))
                self.assert_safe(images)
                return {
                    "descriptors": [
                        {
                            "upload_alias": "upload_01",
                            "semantic": "绿色产品包装正面实拍",
                            "subject_type": "product",
                            "composition": "centered close-up",
                            "supported_ratios": ["9:16", "1:1"],
                            "risk_labels": [],
                        },
                        {
                            "upload_alias": "upload_02",
                            "semantic": "蓝色门店前台实拍",
                            "subject_type": "store",
                            "composition": "wide interior",
                            "supported_ratios": ["16:9", "9:16"],
                            "risk_labels": [],
                        },
                    ],
                }

            @staticmethod
            def assert_safe(images):
                for image in images:
                    if set(image) != {"upload_alias", "width", "height", "jpeg_bytes"}:
                        raise AssertionError(f"unsafe descriptor request keys: {set(image)}")
                    serialized = repr(image)
                    if any(value in serialized for value in (
                        *real_ids, *hashes.values(), *image_bytes.keys(),
                    )):
                        raise AssertionError("trusted material identity leaked to analyzer")

        class NoImageGenerator:
            def __init__(self):
                self.calls = 0

            def generate(self, **_kwargs):
                self.calls += 1
                raise AssertionError("matched upload must not trigger image generation")

        decision = SimpleNamespace(
            value={
                "version": "1.0",
                "creative_concept": "用门店实拍支撑讲解",
                "narrative_pattern": "question_proof",
                "theme_profile_id": "editorial_clean",
                "design_intent": {
                    "density": "balanced",
                    "motion_energy": "medium",
                    "image_fit": "cover",
                    "decoration_intensity": "medium",
                },
                "scene_directives": [{
                    "scene_id": "candidate_01",
                    "narrative_role": "proof",
                    "layout_id": "product_hero",
                    "layout_variant": "center_pedestal",
                    "headline": {
                        "text_kind": "verbatim",
                        "source_caption_ids": ["caption_001"],
                    },
                    "overlay_instances": [{
                        "instance_id": "caption_overlay",
                        "component_id": "standard_caption",
                        "content_ref": "headline",
                        "placement": "subtitle_safe",
                    }],
                    "material_bindings": [],
                    "material_slot_directives": [{
                        "slot_id": "candidate_01_product",
                        "semantic": "蓝色门店前台实拍",
                        "purpose": "product",
                        "priority": "required",
                        "ratio": "9:16",
                    }],
                    "animations": [{
                        "target_id": "caption_overlay",
                        "preset": "subtitle_pop",
                        "direction": "up",
                        "duration_ms": 400,
                        "delay_ms": 0,
                    }],
                    "transition": "hard_cut",
                    "sound_events": [],
                }],
                "audio_intent": {
                    "bgm_description": "克制的无歌词商务氛围",
                    "energy": "medium",
                    "dialogue_priority": True,
                },
            },
            decision_sha256="b" * 64,
            provider_request_id="director-request-descriptor",
            raw_output_sha256="c" * 64,
        )
        captured_requests = []
        analyzer = Analyzer()
        generator = NoImageGenerator()

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"AI_EDIT_V3_VISUAL_PROGRAM_ENABLED": "1"}, clear=False
        ):
            coordinator = object.__new__(ProductionStageCoordinator)
            coordinator.work_root = Path(directory)
            coordinator.renderer_root = Path(__file__).resolve().parents[1] / "server" / "ai_edit_v3_renderer"
            coordinator.store = Store()
            coordinator.cos = Cos()
            coordinator.material_analyzer = analyzer
            coordinator.image_generator = generator
            coordinator.director = object()
            coordinator.owner_hmac_secret = b"0123456789abcdef"
            coordinator.renderer = SimpleNamespace(registry_sha256="sha256:" + "d" * 64)
            root = coordinator._root("job-descriptors")
            (root / "normalized.json").write_text(json.dumps({
                "input_type": "uploaded_audio",
                "ratio": "9:16",
                "sha256": "a" * 64,
            }), encoding="utf-8")
            (root / "timeline.json").write_text(json.dumps({
                "duration_ms": 4000,
                "captions": [{
                    "id": "caption_001",
                    "text": "蓝色门店前台是团队服务的第一现场",
                    "start_ms": 0,
                    "end_ms": 4000,
                }],
                "source_segments": [],
                "authoritative_text_sha256": None,
                "alignment_coverage": 1.0,
            }), encoding="utf-8")
            job = {
                "job_id": "job-descriptors",
                "owner_id": "alice",
                "request_sha256": "1" * 64,
                "stage_input_sha256": "0" * 64,
                "normalized_request_json": json.dumps({
                    "input_type": "uploaded_audio",
                    "material_asset_ids": list(real_ids),
                }),
            }

            def fake_jpeg(source, destination, *, deadline_at):
                Path(destination).parent.mkdir(parents=True, exist_ok=True)
                Path(destination).write_bytes(b"safe-jpeg-" + Path(source).name.encode("ascii"))
                return {"width": 512, "height": 288}

            def capture_decision(context, *_args, **_kwargs):
                captured_requests.append(copy.deepcopy(context.request))
                return decision

            with patch(
                "server.content_domains.ai_edit_v3.production._prepare_material_analysis_jpeg",
                side_effect=fake_jpeg,
            ), patch(
                "server.content_domains.ai_edit_v3.production.generate_director_decision",
                side_effect=capture_decision,
            ):
                first = coordinator._stage(
                    "planning", job,
                    SimpleNamespace(deadline_at=time.time() + 60),
                )
                replay = coordinator._stage(
                    "planning", job,
                    SimpleNamespace(deadline_at=time.time() + 60),
                )
                resolving_job = {**job, "stage_input_sha256": "2" * 64}
                resolved = coordinator._stage(
                    "resolving_materials", resolving_job,
                    SimpleNamespace(deadline_at=time.time() + 60),
                )
                generated = coordinator._stage(
                    "generating_images", job,
                    SimpleNamespace(deadline_at=time.time() + 60),
                )

            descriptor_document = json.loads(
                (root / "material-descriptors.json").read_text(encoding="utf-8")
            )
            descriptor_sha256 = hashlib.sha256(
                (root / "material-descriptors.json").read_bytes()
            ).hexdigest()
            plan = json.loads((root / "plan.json").read_text(encoding="utf-8"))
            materials = json.loads((root / "materials.json").read_text(encoding="utf-8"))
            trusted = coordinator._frozen_bound_materials(job)
            expected_descriptor_input = production._material_descriptor_input_sha256(
                "alice", trusted,
            )
            with self.assertRaises(production.MaterialError):
                coordinator._validated_material_descriptor_document(
                    descriptor_document,
                    list(reversed(trusted)),
                    input_sha256=production._material_descriptor_input_sha256(
                        "alice", list(reversed(trusted)),
                    ),
                )

        self.assertEqual("resolving_materials", first.next_state)
        self.assertEqual("resolving_materials", replay.next_state)
        self.assertEqual(descriptor_sha256, first.checkpoint["material_descriptors_sha256"])
        self.assertEqual("ai-edit-v3-material-descriptors-v1", descriptor_document["contract"])
        self.assertEqual(expected_descriptor_input, descriptor_document["input_sha256"])
        self.assertNotEqual("0" * 64, descriptor_document["input_sha256"])
        self.assertEqual("generating_images", resolved.next_state)
        self.assertEqual("generating_audio", generated.next_state)
        self.assertEqual(1, len(analyzer.calls), "two images must share one bounded batch and replay must reuse it")
        self.assertEqual(0, generator.calls)
        self.assertEqual("primary", plan["scenes"][0]["material_slots"][0]["layout_slot_id"])
        self.assertEqual(real_ids[1], materials["items"][0]["material_id"])
        self.assertEqual("primary", materials["items"][0]["slot_id"])
        self.assertEqual([], materials["unresolved"])
        public_materials = captured_requests[0]["current_materials"]
        self.assertEqual(["upload_01", "upload_02"], [item["upload_alias"] for item in public_materials])
        self.assertEqual("蓝色门店前台实拍", public_materials[1]["semantic"])
        prompt_json = json.dumps(captured_requests, ensure_ascii=False)
        artifact_json = json.dumps(descriptor_document, ensure_ascii=False)
        for secret in (*real_ids, *hashes.values(), "test/owner/green.png", "test/owner/blue.png"):
            self.assertNotIn(secret, prompt_json)
        self.assertNotIn("data:image", artifact_json)
        self.assertNotIn("relative_path", artifact_json)

    def test_material_descriptor_receipt_closes_success_before_artifact_crash_window(self):
        from server.content_domains.ai_edit_v3 import production
        from server.content_domains.ai_edit_v3.production import ProductionStageCoordinator
        from server.content_domains.ai_edit_v3.providers.base import ProviderResult

        source = b"verified-upload"
        source_sha = hashlib.sha256(source).hexdigest()

        class Store:
            environment = "test"

            def __init__(self):
                self.task = None
                self.receipt_calls = []

            def resolve_request_uploads_for_owner(self, owner, *, source_upload_id, material_ids, environment):
                return {"materials": [{
                    "material_id": "trusted-material",
                    "cos_key": "test/owner/upload.png",
                    "mime_type": "image/png",
                    "size_bytes": len(source),
                    "sha256": source_sha,
                    "metadata_json": '{"width":1080,"height":1080}',
                }]}

            def get_provider_task_for_claim(self, claim, operation_key, now_ms):
                return copy.deepcopy(self.task)

            def record_provider_intent(self, claim, stage, stage_attempt_id, provider, capability, operation_key, request_sha256, now_ms):
                self.receipt_calls.append(("intent", stage, capability, operation_key))
                if self.task is None:
                    self.task = {
                        "status": "intent_recorded",
                        "stage": stage,
                        "provider": provider,
                        "capability": capability,
                        "request_sha256": request_sha256,
                    }

            def claim_provider_submission(self, *args):
                self.receipt_calls.append(("claim",))
                return True

            def bind_provider_result(self, claim, operation_key, external_id, status, result, now_ms):
                self.receipt_calls.append(("bind", external_id, status))
                self.task.update({
                    "status": status,
                    "external_id": external_id,
                    "result_json": json.dumps(result, ensure_ascii=False),
                })

        class Cos:
            def download_file(self, key, target):
                Path(target).write_bytes(source)

        class Analyzer:
            def __init__(self):
                self.calls = 0

            def describe_materials(self, images, *, deadline_at):
                self.calls += 1
                return ProviderResult(
                    provider="dashscope",
                    capability="material_analysis",
                    request_id="descriptor-request-1",
                    payload={"content": json.dumps({"descriptors": [{
                        "upload_alias": "upload_01",
                        "semantic": "绿色产品包装正面实拍",
                        "subject_type": "product",
                        "composition": "centered close-up",
                        "supported_ratios": ["9:16", "1:1"],
                        "risk_labels": [],
                    }]}, ensure_ascii=False)},
                    usage={},
                    elapsed_ms=1,
                )

        store = Store()
        analyzer = Analyzer()
        with tempfile.TemporaryDirectory() as directory:
            coordinator = object.__new__(ProductionStageCoordinator)
            coordinator.work_root = Path(directory)
            coordinator.store = store
            coordinator.cos = Cos()
            coordinator.material_analyzer = analyzer
            job = {
                "job_id": "job-receipt",
                "owner_id": "alice",
                "stage_input_sha256": "0" * 64,
                "normalized_request_json": json.dumps({
                    "input_type": "uploaded_audio",
                    "material_asset_ids": ["trusted-material"],
                }),
            }
            context = SimpleNamespace(
                deadline_at=time.time() + 60,
                claim=object(),
                stage_attempt_id="attempt-1",
            )

            def fake_jpeg(source_path, destination, *, deadline_at):
                Path(destination).parent.mkdir(parents=True, exist_ok=True)
                Path(destination).write_bytes(b"safe-jpeg")
                return {"width": 512, "height": 512}

            real_write_json = production._write_json
            failed_once = False

            def crash_after_receipt(path, value):
                nonlocal failed_once
                if Path(path).name == "material-descriptors.json" and not failed_once:
                    failed_once = True
                    raise OSError("simulated-artifact-crash")
                return real_write_json(path, value)

            with patch(
                "server.content_domains.ai_edit_v3.production._prepare_material_analysis_jpeg",
                side_effect=fake_jpeg,
            ), patch(
                "server.content_domains.ai_edit_v3.production._write_json",
                side_effect=crash_after_receipt,
            ):
                with self.assertRaisesRegex(OSError, "simulated-artifact-crash"):
                    coordinator._material_descriptors(job, context)
                descriptors = coordinator._material_descriptors(job, context)

        self.assertEqual(1, analyzer.calls)
        self.assertEqual("绿色产品包装正面实拍", descriptors[0]["semantic"])
        self.assertEqual(
            [("intent", "planning", "material_analysis", "ai-edit-v3:job-receipt:material-analysis:0"),
             ("claim",),
             ("bind", "descriptor-request-1", "completed")],
            store.receipt_calls,
        )

    def test_ten_uploads_use_two_bounded_batches_and_all_reach_director_as_safe_aliases(self):
        from server.content_domains.ai_edit_v3.production import ProductionStageCoordinator

        contents = [f"image-{index}".encode("ascii") for index in range(1, 11)]
        material_ids = [f"trusted-real-{index}" for index in range(1, 11)]
        cos_keys = [f"test/private/material-{index}.png" for index in range(1, 11)]

        class Store:
            environment = "test"

            def resolve_request_uploads_for_owner(self, owner, *, source_upload_id, material_ids, environment):
                return {"materials": [
                    {
                        "material_id": material_id,
                        "cos_key": cos_key,
                        "mime_type": "image/png",
                        "size_bytes": len(content),
                        "sha256": hashlib.sha256(content).hexdigest(),
                        "metadata_json": '{"width":1080,"height":1080}',
                    }
                    for material_id, cos_key, content in zip(
                        material_ids, cos_keys, contents, strict=True,
                    )
                ]}

        class Cos:
            def download_file(self, key, target):
                Path(target).write_bytes(contents[cos_keys.index(key)])

        class Analyzer:
            def __init__(self):
                self.batch_sizes = []

            def describe_materials(self, images, *, deadline_at):
                self.batch_sizes.append(len(images))
                return {"descriptors": [{
                    "upload_alias": item["upload_alias"],
                    "semantic": f"安全素材描述 {item['upload_alias']}",
                    "subject_type": "object",
                    "composition": "centered object",
                    "supported_ratios": ["9:16"],
                    "risk_labels": [],
                } for item in images]}

        decision = SimpleNamespace(
            value={
                "theme_profile_id": "editorial_clean",
                "design_intent": {
                    "density": "balanced", "motion_energy": "medium",
                    "image_fit": "cover", "decoration_intensity": "medium",
                },
            },
            decision_sha256="b" * 64,
            provider_request_id="director-ten",
            raw_output_sha256="c" * 64,
        )
        captured = []
        analyzer = Analyzer()
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"AI_EDIT_V3_VISUAL_PROGRAM_ENABLED": "1"}, clear=False,
        ):
            coordinator = object.__new__(ProductionStageCoordinator)
            coordinator.work_root = Path(directory)
            coordinator.renderer_root = Path(__file__).resolve().parents[1] / "server" / "ai_edit_v3_renderer"
            coordinator.store = Store()
            coordinator.cos = Cos()
            coordinator.material_analyzer = analyzer
            coordinator.director = object()
            coordinator.renderer = SimpleNamespace(registry_sha256="sha256:" + "d" * 64)
            root = coordinator._root("job-ten")
            (root / "normalized.json").write_text(json.dumps({
                "input_type": "uploaded_audio", "ratio": "9:16", "sha256": "a" * 64,
            }), encoding="utf-8")
            (root / "timeline.json").write_text(json.dumps({
                "duration_ms": 4000,
                "captions": [{
                    "id": "caption_001", "text": "十张素材测试",
                    "start_ms": 0, "end_ms": 4000,
                }],
                "source_segments": [], "authoritative_text_sha256": None,
                "alignment_coverage": 1.0,
            }), encoding="utf-8")
            job = {
                "job_id": "job-ten", "owner_id": "alice",
                "request_sha256": "1" * 64, "stage_input_sha256": "0" * 64,
                "normalized_request_json": json.dumps({
                    "input_type": "uploaded_audio", "material_asset_ids": material_ids,
                }),
            }

            def fake_jpeg(source, destination, *, deadline_at):
                Path(destination).parent.mkdir(parents=True, exist_ok=True)
                Path(destination).write_bytes(b"safe-jpeg")
                return {"width": 512, "height": 512}

            def capture_decision(context, *_args, **_kwargs):
                captured.append(copy.deepcopy(context.request))
                return decision

            with patch(
                "server.content_domains.ai_edit_v3.production._prepare_material_analysis_jpeg",
                side_effect=fake_jpeg,
            ), patch(
                "server.content_domains.ai_edit_v3.production.generate_director_decision",
                side_effect=capture_decision,
            ), patch(
                "server.content_domains.ai_edit_v3.production.compile_edit_plan",
                return_value={"version": "2.0", "visual_program_version": "1.0"},
            ):
                coordinator._stage(
                    "planning", job,
                    SimpleNamespace(deadline_at=time.time() + 60),
                )

        self.assertEqual([5, 5], analyzer.batch_sizes)
        self.assertEqual(10, len(captured[0]["current_materials"]))
        self.assertEqual(
            [f"upload_{index:02d}" for index in range(1, 11)],
            [item["upload_alias"] for item in captured[0]["current_materials"]],
        )
        prompt = json.dumps(captured[0], ensure_ascii=False)
        for private_value in [*material_ids, *cos_keys, *(
            hashlib.sha256(content).hexdigest() for content in contents
        )]:
            self.assertNotIn(private_value, prompt)


if __name__ == "__main__":
    unittest.main()
