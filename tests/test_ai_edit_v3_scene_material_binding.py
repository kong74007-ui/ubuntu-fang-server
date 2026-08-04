from __future__ import annotations

import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from server.content_domains.ai_edit_v3.director_compiler import compile_edit_plan
from server.content_domains.ai_edit_v3.director_decision import validate_director_decision
from server.content_domains.ai_edit_v3 import production as production_module
from server.content_domains.ai_edit_v3.materials import (
    MaterialError,
    bind_scene_materials,
    validate_generated_material_review,
)
from server.content_domains.ai_edit_v3.production import ProductionStageCoordinator
from server.content_domains.ai_edit_v3.providers.base import ProviderResult


ROOT = Path(__file__).resolve().parents[1]


class SceneMaterialBindingTests(unittest.TestCase):
    def test_generated_material_review_rejects_urls_and_signed_parameters(self) -> None:
        cases = (
            {
                "result": "pass",
                "reason": "review image https://private.example/object.png",
                "evidence": [{"semantic_match": True, "forbidden_subjects": []}],
            },
            {
                "result": "pass",
                "reason": "q-sign-algorithm=sha1&q-signature=secret",
                "evidence": [{"semantic_match": True, "forbidden_subjects": []}],
            },
            {
                "result": "pass",
                "reason": "semantic match",
                "evidence": [{
                    "semantic_match": True,
                    "forbidden_subjects": [],
                    "preview_url": "https://private.example/object.png?q-signature=secret",
                }],
            },
        )

        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(MaterialError, "generated_material_review_invalid"):
                    validate_generated_material_review(payload, required=False)

    def test_generated_material_review_rejects_oversized_and_unknown_fields(self) -> None:
        cases = (
            {
                "result": "pass",
                "reason": "x" * 501,
                "evidence": [{"semantic_match": True, "forbidden_subjects": []}],
            },
            {
                "result": "pass",
                "reason": "valid reason",
                "evidence": [
                    {"semantic_match": index == 0, "forbidden_subjects": []}
                    for index in range(9)
                ],
            },
            {
                "result": "pass",
                "reason": "valid reason",
                "evidence": [{
                    "semantic_match": True,
                    "forbidden_subjects": [],
                    "confidence": 0.99,
                }],
            },
        )

        for payload in cases:
            with self.subTest(payload=payload):
                with self.assertRaisesRegex(MaterialError, "generated_material_review_invalid"):
                    validate_generated_material_review(payload, required=False)

    def test_generated_material_review_accepts_exact_size_boundaries(self) -> None:
        payload = {
            "result": "pass",
            "reason": "x" * 500,
            "evidence": [
                {"semantic_match": index == 0, "forbidden_subjects": []}
                for index in range(8)
            ],
        }

        self.assertEqual(payload, validate_generated_material_review(payload, required=False))

    def test_generated_material_review_rejects_whitespace_only_reason(self) -> None:
        with self.assertRaisesRegex(MaterialError, "generated_material_review_invalid"):
            validate_generated_material_review(
                {
                    "result": "pass",
                    "reason": " \n\t ",
                    "evidence": [{"semantic_match": True, "forbidden_subjects": []}],
                },
                required=False,
            )

    def test_inferred_renderer_slot_uses_raw_request_slot_for_asset_lookup(self) -> None:
        scene = {
            "id": "scene_01",
            "layout_id": "product_hero",
            "material_slots": [
                {
                    "id": "product_visual",
                    "layout_slot_id": "primary",
                    "purpose": "product",
                    "priority": "required",
                },
                {
                    "id": "airflow_visual",
                    "purpose": "context",
                    "priority": "required",
                },
            ],
        }
        known = ["material_01", "material_02"]
        frozen = {
            ("scene_01", "primary"): "material_01",
            ("scene_01", "airflow_visual"): "material_02",
        }

        self.assertEqual(
            known,
            production_module._scene_asset_ids(scene, known, frozen),
        )
        self.assertEqual(
            [
                {"slot_id": "primary", "asset_id": "material_01"},
                {"slot_id": "detail", "asset_id": "material_02"},
            ],
            production_module._layout_slot_bindings(scene, known, frozen),
        )

    def test_post_upload_reviewer_failures_are_audited_and_deleted(self) -> None:
        generated = b"generated-image-requiring-review"

        class Store:
            environment = "test"

            def resolve_request_uploads_for_owner(self, _owner, *, source_upload_id, material_ids, environment):
                return {"source_upload": None, "materials": []}

        class Cos:
            def __init__(self) -> None:
                self.deletes = []

            def put_file(self, _source, _key, _content_type, **_kwargs):
                return None

            def delete_object(self, key):
                self.deletes.append(key)

        class Generator:
            def generate(self, *, output_path, **_kwargs):
                Path(output_path).write_bytes(generated)
                return ProviderResult("openai", "image_generation", "request-review-edge", {}, {}, 1)

        class RaisingReviewer:
            def inspect_material(self, **_request):
                raise RuntimeError("review_provider_unavailable")

        class InvalidReviewer:
            def inspect_material(self, **_request):
                return {"result": "pass", "reason": "missing evidence"}

        cases = (
            ("missing", object(), "generated_material_reviewer_unavailable"),
            ("raised", RaisingReviewer(), "review_provider_unavailable"),
            ("schema_invalid", InvalidReviewer(), "generated_material_review_invalid"),
        )
        for label, reviewer, expected_error in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                coordinator = object.__new__(ProductionStageCoordinator)
                coordinator.store = Store()
                coordinator.work_root = Path(directory)
                coordinator.owner_hmac_secret = b"0123456789abcdef"
                coordinator.cos = Cos()
                coordinator.image_generator = Generator()
                coordinator.visual_inspector = reviewer
                root = coordinator._root(f"job-review-{label}")
                (root / "plan.json").write_text(json.dumps({
                    "visual_program_version": "1.0",
                    "ratio": "9:16",
                    "materials": [{"request_id": "detail", "semantic": "airflow diagram", "purpose": "context", "priority": "required", "ratio": "9:16"}],
                    "scenes": [{"id": "scene_01", "material_slots": [{"id": "detail", "semantic": "airflow diagram", "purpose": "context", "priority": "required", "ratio": "9:16"}]}],
                }), encoding="utf-8")
                job = {
                    "job_id": f"job-review-{label}",
                    "owner_id": "owner",
                    "stage_input_sha256": "0" * 64,
                    "normalized_request_json": json.dumps({"input_type": "platform_talking_head", "material_asset_ids": []}),
                }
                coordinator._stage("resolving_materials", job, SimpleNamespace(deadline_at=time.time() + 60))
                with patch("server.content_domains.ai_edit_v3.production._probe_image", autospec=True, return_value=SimpleNamespace(width=1024, height=1536)):
                    with self.assertRaisesRegex((MaterialError, RuntimeError), expected_error):
                        coordinator._stage("generating_images", job, SimpleNamespace(deadline_at=time.time() + 60))

                rejection_path = root / "material-rejections.json"
                self.assertTrue(rejection_path.exists(), f"{label} must persist a rejection audit")
                rejection = json.loads(rejection_path.read_text(encoding="utf-8"))["items"]
                self.assertEqual(1, len(rejection))
                self.assertEqual("deleted", rejection[0]["cleanup_status"])
                self.assertEqual([rejection[0]["cos_key"]], coordinator.cos.deletes)
                materials = json.loads((root / "materials.json").read_text(encoding="utf-8"))
                self.assertEqual([], materials["items"])

    def test_cleanup_failed_rejection_persists_retry_contract_without_secret_error(self) -> None:
        class FailingCos:
            def delete_object(self, _key):
                raise RuntimeError("https://private.example/object?q-signature=must-not-persist")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            production_module._record_material_rejection(
                root,
                material_request={
                    "scene_id": "scene_02",
                    "slot_id": "detail",
                    "request_id": "request_02",
                    "semantic": "product detail",
                },
                cos_key="private/owner/job/generated-detail.png",
                source_metadata={"sha256": "a" * 64, "provider": "openai"},
                review={
                    "result": "fail",
                    "reason": "wrong product",
                    "evidence": [{"semantic_match": False, "forbidden_subjects": ["wrong_product"]}],
                },
                cos=FailingCos(),
            )
            audit = json.loads((root / "material-rejections.json").read_text(encoding="utf-8"))["items"][0]

        self.assertEqual("cleanup_failed", audit["cleanup_status"])
        self.assertIs(True, audit["cleanup_required"])
        self.assertEqual(1, audit["cleanup_attempt"]["attempt_count"])
        self.assertEqual("cos_delete_failed", audit["cleanup_attempt"]["last_error_code"])
        serialized = json.dumps(audit, ensure_ascii=False).casefold()
        self.assertNotIn("http://", serialized)
        self.assertNotIn("https://", serialized)
        self.assertNotIn("q-signature", serialized)

    def test_cleanup_retry_scanner_returns_deterministic_pending_contracts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "material-rejections.json").write_text(json.dumps({
                "items": [
                    {
                        "scene_id": "scene_02",
                        "slot_id": "detail",
                        "request_id": "request_b",
                        "cos_key": "private/owner/job/b.png",
                        "cleanup_status": "cleanup_failed",
                        "cleanup_required": True,
                        "cleanup_attempt": {"attempt_count": 2, "last_error_code": "cos_delete_failed"},
                    },
                    {
                        "scene_id": "scene_01",
                        "slot_id": "primary",
                        "request_id": "request_a",
                        "cos_key": "private/owner/job/a.png",
                        "cleanup_status": "cleanup_failed",
                        "cleanup_required": True,
                        "cleanup_attempt": {"attempt_count": 1, "last_error_code": "cos_delete_failed"},
                    },
                    {
                        "scene_id": "scene_03",
                        "slot_id": "accent",
                        "request_id": "request_c",
                        "cos_key": "private/owner/job/c.png",
                        "cleanup_status": "deleted",
                        "cleanup_required": False,
                        "cleanup_attempt": {"attempt_count": 1, "last_error_code": None},
                    },
                ],
            }), encoding="utf-8")
            scanner = getattr(production_module, "scan_material_cleanup_retries", None)

            self.assertTrue(callable(scanner), "deterministic cleanup retry scanner is missing")
            retries = scanner(root)

        self.assertEqual(
            [
                {
                    "audit_path": "material-rejections.json",
                    "scene_id": "scene_01",
                    "slot_id": "primary",
                    "request_id": "request_a",
                    "cos_key": "private/owner/job/a.png",
                    "attempt_count": 1,
                    "last_error_code": "cos_delete_failed",
                },
                {
                    "audit_path": "material-rejections.json",
                    "scene_id": "scene_02",
                    "slot_id": "detail",
                    "request_id": "request_b",
                    "cos_key": "private/owner/job/b.png",
                    "attempt_count": 2,
                    "last_error_code": "cos_delete_failed",
                },
            ],
            retries,
        )

    def test_compiling_maps_frozen_scene_slots_to_final_manifest_asset_ids(self) -> None:
        primary = b"current-primary-product"
        detail = b"reviewed-generated-detail"
        master = b"frozen-master-audio"
        plan = {
            "version": "2.0",
            "visual_program_version": "1.0",
            "duration_ms": 4000,
            "ratio": "9:16",
            "theme": {
                "palette_id": "midnight_gold",
                "typography_id": "editorial_sans",
                "density": "balanced",
                "motion_energy": "medium",
                "image_fit": "smart_crop",
            },
            "captions": [{
                "id": "caption_001",
                "start_ms": 0,
                "end_ms": 4000,
                "text": "The product uses a measurable energy-saving method.",
                "emphasis": "primary",
            }],
            "source_segments": [{
                "id": "segment_01",
                "source_start_ms": 0,
                "source_end_ms": 4000,
                "output_start_ms": 0,
                "output_end_ms": 4000,
                "caption_ids": ["caption_001"],
                "keep_reason": "authoritative full timeline",
            }],
            "materials": [
                {"request_id": "mat_product", "semantic": "green product package", "purpose": "product", "priority": "required", "ratio": "9:16", "time_range": {"start_ms": 0, "end_ms": 4000}},
                {"request_id": "detail", "semantic": "energy-saving airflow diagram", "purpose": "context", "priority": "required", "ratio": "9:16", "time_range": {"start_ms": 0, "end_ms": 4000}},
            ],
            "scenes": [{
                "id": "scene_01",
                "start_ms": 0,
                "end_ms": 4000,
                "intent": "Show the real product and its method.",
                "layout_id": "product_hero",
                "layout_variant": "center_pedestal",
                "visual_type": "director_program",
                "headline": {"text": "The product uses a measurable energy-saving method.", "text_kind": "verbatim", "source_caption_ids": ["caption_001"]},
                "highlight": {"text_kind": "ui_label", "ui_label_id": "chapter"},
                "overlay_ids": [],
                "overlay_instances": [],
                "material_slots": [
                    {"id": "mat_product", "layout_slot_id": "primary", "semantic": "green product package", "purpose": "product", "priority": "required", "ratio": "9:16", "start_ms": 0, "end_ms": 4000},
                    {"id": "detail", "layout_slot_id": "detail", "semantic": "energy-saving airflow diagram", "purpose": "context", "priority": "required", "ratio": "9:16", "start_ms": 0, "end_ms": 4000},
                ],
                "animations": [],
                "transition": "hard_cut",
            }],
        }

        with tempfile.TemporaryDirectory() as directory:
            coordinator = object.__new__(ProductionStageCoordinator)
            coordinator.store = SimpleNamespace(environment="test")
            coordinator.work_root = Path(directory)
            coordinator.renderer_root = ROOT / "server" / "ai_edit_v3_renderer"
            release_lock = json.loads(
                (coordinator.renderer_root / "renderer-release.lock.json").read_text(encoding="utf-8")
            )
            coordinator.renderer = SimpleNamespace(
                renderer_build_id=release_lock["renderer_build_id"],
                registry_sha256="sha256:" + "d" * 64,
            )
            root = coordinator._root("job-compile-materials")
            (root / "materials").mkdir(parents=True)
            (root / "materials" / "primary.png").write_bytes(primary)
            (root / "materials" / "detail.png").write_bytes(detail)
            (root / "master.wav").write_bytes(master)
            (root / "normalized.json").write_text(json.dumps({
                "media_type": "audio",
                "relative_path": "source.wav",
                "duration_ms": 4000,
                "ratio": "9:16",
            }), encoding="utf-8")
            (root / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
            (root / "master.json").write_text(json.dumps({
                "relative_path": "master.wav",
                "sha256": hashlib.sha256(master).hexdigest(),
                "duration_ms": 4000,
                "sample_rate": 48000,
                "channels": 2,
            }), encoding="utf-8")
            (root / "visual-program.json").write_text(json.dumps({
                "theme_profile_id": "editorial_clean",
                "design_intent": {"density": "balanced", "motion_energy": "medium", "image_fit": "smart_crop", "decoration_intensity": "medium"},
                "variation_seed": "0123456789abcdef",
            }), encoding="utf-8")
            (root / "materials.json").write_text(json.dumps({
                "items": [
                    {"material_id": "mat_product", "scene_id": "scene_01", "slot_id": "primary", "request_id": "mat_product", "relative_path": "materials/primary.png", "mime_type": "image/png", "size_bytes": len(primary), "sha256": hashlib.sha256(primary).hexdigest(), "source": "current_upload", "reason": "current_task_semantic_match"},
                    {"material_id": "generated_01", "scene_id": "scene_01", "slot_id": "detail", "request_id": "detail", "relative_path": "materials/detail.png", "mime_type": "image/png", "size_bytes": len(detail), "sha256": hashlib.sha256(detail).hexdigest(), "source": "generated", "reason": "required_slot_generated", "visual_review": {"result": "pass", "reason": "reviewed", "evidence": [{"semantic_match": True, "forbidden_subjects": []}]}},
                ],
                "unresolved": [],
                "omitted": [],
            }), encoding="utf-8")
            try:
                outcome = coordinator._stage(
                    "compiling",
                    {
                        "job_id": "job-compile-materials",
                        "owner_id": "owner",
                        "repair_count": 0,
                        "stage_input_sha256": "0" * 64,
                        "normalized_request_json": json.dumps({"input_type": "uploaded_audio"}),
                    },
                    SimpleNamespace(deadline_at=time.time() + 60),
                )
            except ValueError as exc:
                self.fail(f"compiling rejected frozen scene-slot bindings: {exc}")
            manifest = json.loads(
                (root / outcome.checkpoint["input_root"] / "render-manifest.json").read_text(encoding="utf-8")
            )

        asset_ids = [item["id"] for item in manifest["assets"]]
        self.assertEqual(2, len(asset_ids))
        self.assertEqual(asset_ids, manifest["compositions"][0]["asset_ids"])
        self.assertEqual(
            [{"slot_id": "primary", "asset_id": asset_ids[0]}, {"slot_id": "detail", "asset_id": asset_ids[1]}],
            manifest["compositions"][0]["layout_slot_bindings"],
        )

    def test_required_rejection_is_audited_deleted_and_never_renderable(self) -> None:
        generated = b"generated-image-with-wrong-person"

        class Store:
            environment = "test"

            def resolve_request_uploads_for_owner(self, _owner, *, source_upload_id, material_ids, environment):
                self.call = (source_upload_id, tuple(material_ids), environment)
                return {"source_upload": None, "materials": []}

        class Cos:
            def __init__(self) -> None:
                self.puts = []
                self.deletes = []

            def put_file(self, source, key, content_type, **kwargs):
                self.puts.append((key, content_type, kwargs.get("private"), hashlib.sha256(Path(source).read_bytes()).hexdigest()))

            def delete_object(self, key):
                self.deletes.append(key)

        class Generator:
            def generate(self, *, output_path, **_kwargs):
                Path(output_path).write_bytes(generated)
                return ProviderResult("openai", "image_generation", "request-rejected", {}, {"tokens": 1}, 1)

        class Reviewer:
            def inspect_material(self, **_request):
                return {
                    "result": "fail",
                    "reason": "unrelated presenter detected",
                    "evidence": [{"semantic_match": False, "forbidden_subjects": ["person"]}],
                }

        with tempfile.TemporaryDirectory() as directory:
            coordinator = object.__new__(ProductionStageCoordinator)
            coordinator.store = Store()
            coordinator.work_root = Path(directory)
            coordinator.owner_hmac_secret = b"0123456789abcdef"
            coordinator.cos = Cos()
            coordinator.image_generator = Generator()
            coordinator.visual_inspector = Reviewer()
            root = coordinator._root("job-rejected-material")
            (root / "plan.json").write_text(json.dumps({
                "visual_program_version": "1.0",
                "ratio": "9:16",
                "materials": [{"request_id": "detail", "semantic": "energy-saving airflow diagram", "purpose": "context", "priority": "required", "ratio": "9:16"}],
                "scenes": [{"id": "scene_01", "material_slots": [{"id": "detail", "semantic": "energy-saving airflow diagram", "purpose": "context", "priority": "required", "ratio": "9:16"}]}],
            }), encoding="utf-8")
            job = {"job_id": "job-rejected-material", "owner_id": "owner", "stage_input_sha256": "0" * 64, "normalized_request_json": json.dumps({"input_type": "platform_talking_head", "material_asset_ids": []})}
            coordinator._stage("resolving_materials", job, SimpleNamespace(deadline_at=time.time() + 60))
            with patch("server.content_domains.ai_edit_v3.production._probe_image", autospec=True, return_value=SimpleNamespace(width=1024, height=1536)):
                with self.assertRaisesRegex(MaterialError, "generated_required_material_review_failed"):
                    coordinator._stage("generating_images", job, SimpleNamespace(deadline_at=time.time() + 60))
            materials = json.loads((root / "materials.json").read_text(encoding="utf-8"))
            rejection_path = root / "material-rejections.json"
            self.assertTrue(rejection_path.exists(), "required rejection must be persisted before the stage fails")
            rejection = json.loads(rejection_path.read_text(encoding="utf-8"))

        self.assertEqual([], materials["items"])
        self.assertEqual(1, len(rejection["items"]))
        audit = rejection["items"][0]
        self.assertEqual("scene_01", audit["scene_id"])
        self.assertEqual("detail", audit["slot_id"])
        self.assertEqual("unrelated presenter detected", audit["reason"])
        self.assertEqual([{"semantic_match": False, "forbidden_subjects": ["person"]}], audit["evidence"])
        self.assertEqual(coordinator.cos.puts[0][0], audit["cos_key"])
        self.assertEqual(hashlib.sha256(generated).hexdigest(), audit["source_metadata"]["sha256"])
        self.assertEqual("deleted", audit["cleanup_status"])
        self.assertEqual([audit["cos_key"]], coordinator.cos.deletes)
        serialized = json.dumps(rejection, ensure_ascii=False).casefold()
        self.assertNotIn("signed_url", serialized)
        self.assertNotIn("http://", serialized)
        self.assertNotIn("https://", serialized)

    def test_optional_unresolved_scene_slot_is_audited_and_never_generated(self) -> None:
        result = bind_scene_materials(
            {
                "visual_program_version": "1.0",
                "scenes": [{
                    "id": "scene_01",
                    "material_slots": [{
                        "id": "accent",
                        "semantic": "subtle abstract accent",
                        "purpose": "decoration",
                        "priority": "optional",
                        "ratio": "9:16",
                    }],
                }],
            },
            [],
        )

        self.assertEqual([], result["items"])
        self.assertEqual([], result["unresolved"])
        self.assertEqual(
            [{
                "scene_id": "scene_01",
                "slot_id": "accent",
                "request_id": "accent",
                "semantic": "subtle abstract accent",
                "purpose": "decoration",
                "priority": "optional",
                "ratio": "9:16",
                "reason": "no_qualified_current_upload",
                "source": "omitted_optional",
                "status": "omitted_optional",
            }],
            result["omitted"],
        )

    def test_required_generated_material_review_failure_is_terminal(self) -> None:
        with self.assertRaisesRegex(
            MaterialError,
            "generated_required_material_review_failed",
        ):
            validate_generated_material_review(
                {
                    "result": "pass",
                    "reason": "provider claimed success but reviewer found a person",
                    "evidence": [{
                        "semantic_match": True,
                        "forbidden_subjects": ["person"],
                    }],
                },
                required=True,
            )

    def test_generated_material_review_rejects_malformed_forbidden_subjects(self) -> None:
        with self.assertRaisesRegex(
            MaterialError,
            "generated_material_review_invalid",
        ):
            validate_generated_material_review(
                {
                    "result": "pass",
                    "reason": "malformed reviewer payload must not bypass the gate",
                    "evidence": [{
                        "semantic_match": True,
                        "forbidden_subjects": "person",
                    }],
                },
                required=True,
            )

    def test_required_generated_detail_is_scene_bound_private_and_visually_reviewed(self) -> None:
        candidate = {
            "id": "candidate_01",
            "start_ms": 0,
            "end_ms": 4000,
            "caption_ids": ["caption_001"],
            "caption_texts": [["caption_001", "The product uses a measurable energy-saving method."]],
            "authoritative_text": "The product uses a measurable energy-saving method.",
            "protected_fact_ids": [],
            "available_material_ids": ["mat_product"],
            "speaker_available": True,
        }
        capabilities = {
            "layout_capabilities": ["product_hero"],
            "layout_variants": {"product_hero": ["center_pedestal"]},
            "overlay_capabilities": [],
            "animation_capabilities": [],
            "transition_capabilities": ["hard_cut"],
            "overlay_variants": {},
            "overlay_animation_targets": {},
            "layout_animation_targets": {"product_hero": []},
            "theme_profile_ids": ["editorial_clean"],
            "theme_capabilities": {
                "palette_id": ["midnight_gold"],
                "typography_id": ["editorial_sans"],
                "density": ["balanced"],
                "motion_energy": ["medium"],
                "image_fit": ["smart_crop"],
            },
            "output_ratio": "9:16",
        }
        decision = validate_director_decision(
            {
                "version": "1.0",
                "creative_concept": "Use the real product plus a supporting detail illustration.",
                "narrative_pattern": "product_proof",
                "theme_profile_id": "editorial_clean",
                "design_intent": {
                    "density": "balanced",
                    "motion_energy": "medium",
                    "image_fit": "smart_crop",
                    "decoration_intensity": "medium",
                },
                "scene_directives": [{
                    "scene_id": "candidate_01",
                    "narrative_role": "proof",
                    "layout_id": "product_hero",
                    "layout_variant": "center_pedestal",
                    "overlay_instances": [],
                    "material_bindings": [{
                        "slot_id": "primary",
                        "material_id": "mat_product",
                        "required": True,
                    }],
                    "material_slot_directives": [{
                        "slot_id": "detail",
                        "semantic": "abstract energy-saving airflow diagram without people or text",
                        "purpose": "context",
                        "priority": "required",
                        "ratio": "9:16",
                    }],
                    "animations": [],
                    "transition": "hard_cut",
                    "sound_events": [],
                }],
                "audio_intent": {
                    "bgm_description": "restrained instrumental bed",
                    "energy": "medium",
                    "dialogue_priority": True,
                },
            },
            candidates=[candidate],
            capabilities=capabilities,
        )
        plan = compile_edit_plan(
            decision,
            candidates=[candidate],
            timeline={
                "duration_ms": 4000,
                "ratio": "9:16",
                "captions": [{
                    "id": "caption_001",
                    "start_ms": 0,
                    "end_ms": 4000,
                    "text": "The product uses a measurable energy-saving method.",
                }],
            },
            materials=[{
                "material_id": "mat_product",
                "semantic": ["green product package"],
                "subject_type": "product",
                "composition": "center",
                "supported_ratios": ["9:16"],
                "risk_labels": [],
                "sha256": "a" * 64,
            }],
            capabilities=capabilities,
            variation_seed=1,
        )

        upload = b"verified-current-task-product"
        generated = b"generated-supporting-detail"

        class Store:
            environment = "test"

            def resolve_request_uploads_for_owner(self, owner, *, source_upload_id, material_ids, environment):
                self.call = (owner, source_upload_id, tuple(material_ids), environment)
                return {
                    "source_upload": None,
                    "materials": [{
                        "material_id": "mat_product",
                        "cos_key": "test/ai-edit-v3/owner/uploads/product.png",
                        "mime_type": "image/png",
                        "size_bytes": len(upload),
                        "sha256": hashlib.sha256(upload).hexdigest(),
                        "metadata_json": json.dumps({
                            "semantic": ["green product package"],
                            "subject_type": "product",
                            "composition": "center",
                        }),
                    }],
                }

        class Cos:
            def __init__(self) -> None:
                self.puts = []

            def download_file(self, key, target):
                self.downloaded_key = key
                Path(target).write_bytes(upload)

            def put_file(self, source, key, content_type, **kwargs):
                self.puts.append({
                    "key": key,
                    "content_type": content_type,
                    "private": kwargs.get("private"),
                    "sha256": hashlib.sha256(Path(source).read_bytes()).hexdigest(),
                })

        class Generator:
            def __init__(self) -> None:
                self.prompts = []

            def generate(self, *, prompt, output_path, **_kwargs):
                self.prompts.append(prompt)
                Path(output_path).write_bytes(generated)
                return ProviderResult(
                    provider="openai",
                    capability="image_generation",
                    request_id="image-request-1",
                    payload={"sha256": hashlib.sha256(generated).hexdigest()},
                    usage={"tokens": 1},
                    elapsed_ms=1,
                )

        class Reviewer:
            def __init__(self) -> None:
                self.calls = []

            def inspect_material(self, **request):
                self.calls.append(request)
                return {
                    "result": "pass",
                    "reason": "semantic_match_without_forbidden_subjects",
                    "evidence": [{"semantic_match": True, "forbidden_subjects": []}],
                }

        with tempfile.TemporaryDirectory() as directory:
            coordinator = object.__new__(ProductionStageCoordinator)
            coordinator.store = Store()
            coordinator.work_root = Path(directory)
            coordinator.owner_hmac_secret = b"0123456789abcdef"
            coordinator.cos = Cos()
            coordinator.image_generator = Generator()
            coordinator.visual_inspector = Reviewer()
            root = coordinator._root("job-scene-material")
            root.mkdir(parents=True, exist_ok=True)
            (root / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
            job = {
                "job_id": "job-scene-material",
                "owner_id": "owner",
                "stage_input_sha256": "0" * 64,
                "normalized_request_json": json.dumps({
                    "input_type": "platform_talking_head",
                    "material_asset_ids": ["mat_product"],
                }),
            }

            resolved = coordinator._stage(
                "resolving_materials",
                job,
                SimpleNamespace(deadline_at=time.time() + 60),
            )
            self.assertEqual("generating_images", resolved.next_state)
            with patch(
                "server.content_domains.ai_edit_v3.production._probe_image",
                autospec=True,
                return_value=SimpleNamespace(width=1024, height=1536),
            ):
                generated_outcome = coordinator._stage(
                    "generating_images",
                    job,
                    SimpleNamespace(deadline_at=time.time() + 60),
                )
            frozen = json.loads((root / "materials.json").read_text(encoding="utf-8"))

        self.assertEqual("generating_audio", generated_outcome.next_state)
        self.assertEqual(1, len(coordinator.image_generator.prompts))
        self.assertIn("energy-saving airflow diagram", coordinator.image_generator.prompts[0])
        self.assertNotIn("green product package", coordinator.image_generator.prompts[0])
        self.assertEqual(
            [("scene_01", "primary", "mat_product", "current_upload"),
             ("scene_01", "detail", "generated_01", "generated")],
            [(item.get("scene_id"), item.get("slot_id"), item.get("material_id"), item.get("source"))
             for item in frozen["items"]],
        )
        self.assertEqual(hashlib.sha256(generated).hexdigest(), frozen["items"][1]["sha256"])
        self.assertTrue(coordinator.cos.puts[0]["private"])
        self.assertEqual(1, len(coordinator.visual_inspector.calls))
        review_request = coordinator.visual_inspector.calls[0]
        self.assertEqual("detail", review_request["slot_id"])
        self.assertEqual(
            "abstract energy-saving airflow diagram without people or text",
            review_request["semantic"],
        )
        self.assertEqual(
            ("person", "face", "wrong_product", "wrong_store", "fabricated_real_world_evidence"),
            tuple(review_request["forbidden_subjects"]),
        )
        self.assertTrue(review_request["cos_key"].startswith("test/ai-edit-v3/"))
        self.assertNotIn("url", review_request)
        self.assertNotIn("signed_url", review_request)
        self.assertEqual("pass", frozen["items"][1]["visual_review"]["result"])


if __name__ == "__main__":
    unittest.main()
