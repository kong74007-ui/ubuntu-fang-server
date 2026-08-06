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
    @staticmethod
    def _freeze_material_descriptors(
        coordinator: ProductionStageCoordinator,
        job: dict,
        descriptors: list[dict] | None = None,
    ) -> None:
        trusted = coordinator._frozen_bound_materials(job)
        safe_descriptors = list(descriptors or ())
        if len(trusted) != len(safe_descriptors):
            raise AssertionError("one descriptor is required for every bound upload")
        items = []
        for index, (material, descriptor) in enumerate(
            zip(trusted, safe_descriptors, strict=True), 1,
        ):
            items.append({
                "upload_alias": f"upload_{index:02d}",
                "material_id": material["material_id"],
                "sha256": material["sha256"],
                **descriptor,
            })
        root = coordinator._root(str(job["job_id"]))
        (root / "material-descriptors.json").write_text(
            json.dumps({
                "contract": "ai-edit-v3-material-descriptors-v1",
                "version": "1.0",
                "input_sha256": production_module._material_descriptor_input_sha256(
                    str(job["owner_id"]), trusted,
                ),
                "items": items,
            }, ensure_ascii=False),
            encoding="utf-8",
        )

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
                self.puts = []
                self.deletes = []

            def put_file(self, _source, key, _content_type, **_kwargs):
                self.puts.append(key)

            def delete_object(self, key):
                self.deletes.append(key)

        class Generator:
            def __init__(self) -> None:
                self.calls = 0

            def generate(self, *, output_path, **_kwargs):
                self.calls += 1
                Path(output_path).write_bytes(generated)
                return ProviderResult("openai", "image_generation", "request-review-edge", {}, {}, 1)

        class RaisingReviewer:
            def inspect_material(self, **_request):
                raise RuntimeError("review_provider_unavailable")

        class InvalidReviewer:
            def inspect_material(self, **_request):
                return {"result": "pass", "reason": "missing evidence"}

        class PassingReviewer:
            def inspect_material(self, **_request):
                return {
                    "result": "pass",
                    "reason": "same candidate passed after reviewer recovery",
                    "evidence": [{"semantic_match": True, "forbidden_subjects": []}],
                }

        cases = (
            ("missing", object(), "generated_material_reviewer_unavailable", "reviewer_unavailable"),
            ("raised", RaisingReviewer(), "generated_material_review_pending", "reviewer_failed"),
            ("schema_invalid", InvalidReviewer(), "generated_material_review_schema_retry", "review_schema_invalid"),
        )
        for label, reviewer, expected_error, audit_error in cases:
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
                self._freeze_material_descriptors(coordinator, job)
                coordinator._stage("resolving_materials", job, SimpleNamespace(deadline_at=time.time() + 60))
                with patch("server.content_domains.ai_edit_v3.production._probe_image", autospec=True, return_value=SimpleNamespace(width=1024, height=1536)):
                    with self.assertRaisesRegex((MaterialError, RuntimeError), expected_error):
                        coordinator._stage("generating_images", job, SimpleNamespace(deadline_at=time.time() + 60))

                self.assertFalse((root / "material-rejections.json").exists())
                error_path = root / "material-review-errors.json"
                self.assertTrue(error_path.exists(), f"{label} must persist a technical review audit")
                errors = json.loads(error_path.read_text(encoding="utf-8"))["items"]
                self.assertEqual(1, len(errors))
                self.assertEqual(audit_error, errors[0]["error_code"])
                self.assertEqual((1, 1), (
                    errors[0]["generation_attempt"], errors[0]["review_attempt"],
                ))
                self.assertEqual("deleted", errors[0]["cleanup_status"])
                self.assertEqual([errors[0]["cos_key"]], coordinator.cos.deletes)
                materials = json.loads((root / "materials.json").read_text(encoding="utf-8"))
                self.assertEqual([], materials["items"])
                coordinator.visual_inspector = PassingReviewer()
                with patch("server.content_domains.ai_edit_v3.production._probe_image", autospec=True, return_value=SimpleNamespace(width=1024, height=1536)):
                    outcome = coordinator._stage(
                        "generating_images", job, SimpleNamespace(deadline_at=time.time() + 60),
                    )
                materials = json.loads((root / "materials.json").read_text(encoding="utf-8"))
                candidates = json.loads(
                    (root / "material-generation-candidates.json").read_text(encoding="utf-8")
                )["items"]
                self.assertEqual("generating_audio", outcome.next_state)
                self.assertEqual(1, coordinator.image_generator.calls)
                self.assertEqual(2, len(coordinator.cos.puts))
                self.assertEqual(coordinator.cos.puts[0], coordinator.cos.puts[1])
                self.assertEqual(1, len(materials["items"]))
                self.assertEqual("materials/generated-01.png", materials["items"][0]["relative_path"])
                self.assertEqual("request-review-edge", candidates[0]["provider_request_id"])
                self.assertEqual((1, "accepted"), (
                    candidates[0]["generation_attempt"], candidates[0]["status"],
                ))

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
                        "scene_id": "scene_00",
                        "slot_id": "detail",
                        "request_id": "request_pending",
                        "cos_key": "private/owner/job/pending.png",
                        "cleanup_status": "pending",
                        "cleanup_required": True,
                        "cleanup_attempt": {"attempt_count": 1, "last_error_code": None},
                    },
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
            deleted = []
            production_module._recover_material_cleanup(
                root,
                type("Cos", (), {"delete_object": staticmethod(deleted.append)})(),
            )
            recovered = json.loads(
                (root / "material-rejections.json").read_text(encoding="utf-8")
            )["items"]

        self.assertEqual(
            [
                {
                    "audit_path": "material-rejections.json",
                    "scene_id": "scene_00",
                    "slot_id": "detail",
                    "request_id": "request_pending",
                    "cos_key": "private/owner/job/pending.png",
                    "attempt_count": 1,
                    "last_error_code": None,
                },
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
        self.assertEqual(
            [
                "private/owner/job/pending.png",
                "private/owner/job/a.png",
                "private/owner/job/b.png",
            ],
            deleted,
        )
        self.assertTrue(all(
            item["cleanup_status"] == "deleted" and item["cleanup_required"] is False
            for item in recovered
        ))

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

    def test_required_rejection_retries_once_then_is_audited_deleted_and_terminal(self) -> None:
        generated = [b"generated-image-semantic-mismatch", b"generated-image-with-wrong-person"]

        class Store:
            environment = "test"

            record_provider_intent = staticmethod(lambda *_args, **_kwargs: None)
            get_provider_task_for_claim = staticmethod(lambda *_args, **_kwargs: None)
            claim_provider_submission = staticmethod(lambda *_args, **_kwargs: None)
            bind_provider_result = staticmethod(lambda *_args, **_kwargs: None)

            def resolve_request_uploads_for_owner(self, _owner, *, source_upload_id, material_ids, environment):
                self.call = (source_upload_id, tuple(material_ids), environment)
                return {"source_upload": None, "materials": []}

        class Cos:
            def __init__(self) -> None:
                self.puts = []
                self.deletes = []
                self.fail_deletes = True

            def put_file(self, source, key, content_type, **kwargs):
                self.puts.append((key, content_type, kwargs.get("private"), hashlib.sha256(Path(source).read_bytes()).hexdigest()))

            def delete_object(self, key):
                if self.fail_deletes:
                    raise RuntimeError("cos delete temporarily unavailable")
                self.deletes.append(key)

        class Generator:
            def __init__(self) -> None:
                self.calls = []

            def generate(self, *, output_path, **kwargs):
                call_index = len(self.calls)
                self.calls.append({"output_path": Path(output_path), **kwargs})
                Path(output_path).write_bytes(generated[call_index])
                return ProviderResult(
                    "openai", "image_generation", f"request-rejected-{call_index + 1}",
                    {}, {"tokens": 1}, 1,
                )

        class Reviewer:
            def __init__(self) -> None:
                self.calls = []

            def inspect_material(self, **request):
                self.calls.append(request)
                if len(self.calls) == 1:
                    return {
                        "result": "fail",
                        "reason": "physical workshop does not match the software platform",
                        "evidence": [{"semantic_match": False, "forbidden_subjects": []}],
                    }
                return {
                    "result": "fail",
                    "reason": "unrelated presenter detected",
                    "evidence": [{"semantic_match": True, "forbidden_subjects": ["person"]}],
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
                "creative_concept": "AI 内容创作软件平台的一体化数字流程",
                "materials": [{"request_id": "detail", "semantic": "非人物环境素材，展示多工具集成工作台", "purpose": "context", "priority": "required", "ratio": "9:16"}],
                "scenes": [{"id": "scene_01", "intent": "展示软件平台内的工具协同流程", "headline": {"text": "一个工作台串联多种软件工具"}, "material_slots": [{"id": "detail", "semantic": "非人物环境素材，展示多工具集成工作台", "purpose": "context", "priority": "required", "ratio": "9:16"}]}],
            }), encoding="utf-8")
            job = {"job_id": "job-rejected-material", "owner_id": "owner", "stage_input_sha256": "0" * 64, "normalized_request_json": json.dumps({"input_type": "platform_talking_head", "material_asset_ids": []})}
            self._freeze_material_descriptors(coordinator, job)
            coordinator._stage("resolving_materials", job, SimpleNamespace(deadline_at=time.time() + 60))
            review_operations = []

            def invoke_review(**kwargs):
                review_operations.append(kwargs["operation_key"])
                return kwargs["call"]()

            context = SimpleNamespace(
                claim=object(),
                stage_attempt_id="stage-attempt-1",
                deadline_at=time.time() + 60,
            )
            with patch("server.content_domains.ai_edit_v3.production._probe_image", autospec=True, return_value=SimpleNamespace(width=1024, height=1536)), patch(
                "server.content_domains.ai_edit_v3.production.invoke_provider_once",
                autospec=True,
                side_effect=invoke_review,
            ):
                with self.assertRaisesRegex(RuntimeError, "generated_material_cleanup_pending"):
                    coordinator._stage("generating_images", job, context)
                self.assertEqual(1, len(coordinator.image_generator.calls))
                self.assertEqual(1, len(coordinator.visual_inspector.calls))
                coordinator.cos.fail_deletes = False
                with self.assertRaisesRegex(MaterialError, "generated_required_material_review_failed"):
                    coordinator._stage("generating_images", job, context)
            materials = json.loads((root / "materials.json").read_text(encoding="utf-8"))
            rejection_path = root / "material-rejections.json"
            self.assertTrue(rejection_path.exists(), "required rejection must be persisted before the stage fails")
            rejection = json.loads(rejection_path.read_text(encoding="utf-8"))

        self.assertEqual([], materials["items"])
        self.assertEqual(2, len(rejection["items"]))
        first, second = rejection["items"]
        self.assertEqual([1, 2], [first["generation_attempt"], second["generation_attempt"]])
        self.assertEqual([True, False], [first["retry_allowed"], second["retry_allowed"]])
        self.assertEqual(["physical workshop does not match the software platform", "unrelated presenter detected"], [first["reason"], second["reason"]])
        self.assertEqual(["deleted", "deleted"], [first["cleanup_status"], second["cleanup_status"]])
        self.assertEqual(2, first["cleanup_attempt"]["attempt_count"])
        self.assertEqual([item[0] for item in coordinator.cos.puts], coordinator.cos.deletes)
        self.assertEqual(2, len(set(item[0] for item in coordinator.cos.puts)))
        self.assertEqual(2, len({call["output_path"] for call in coordinator.image_generator.calls}))
        self.assertEqual(2, len({call["idempotency_key"] for call in coordinator.image_generator.calls}))
        self.assertEqual(2, len(set(review_operations)))
        self.assertTrue(all(":material-review:v3:" in key for key in review_operations))
        self.assertTrue(review_operations[0].endswith(":attempt:1:review:1"))
        self.assertTrue(review_operations[1].endswith(":attempt:2:review:1"))
        self.assertTrue(all(
            tuple(call["forbidden_subjects"]) == production_module._GENERATED_MATERIAL_FORBIDDEN_SUBJECTS
            for call in coordinator.visual_inspector.calls
        ))
        self.assertEqual(
            [hashlib.sha256(item).hexdigest() for item in generated],
            [first["source_metadata"]["sha256"], second["source_metadata"]["sha256"]],
        )
        serialized = json.dumps(rejection, ensure_ascii=False).casefold()
        self.assertNotIn("signed_url", serialized)
        self.assertNotIn("http://", serialized)
        self.assertNotIn("https://", serialized)

    def test_review_guided_regeneration_disambiguates_digital_workbench_and_accepts_retry(self) -> None:
        generated = [b"physical-workshop", b"digital-software-workbench"]

        class Store:
            environment = "test"

            def resolve_request_uploads_for_owner(self, _owner, *, source_upload_id, material_ids, environment):
                return {"source_upload": None, "materials": []}

        class Cos:
            def __init__(self) -> None:
                self.puts = []
                self.deletes = []

            def put_file(self, source, key, content_type, **kwargs):
                self.puts.append((Path(source), key, content_type, kwargs))

            def delete_object(self, key):
                self.deletes.append(key)

        class Generator:
            def __init__(self) -> None:
                self.calls = []

            def generate(self, *, prompt, output_path, idempotency_key, **_kwargs):
                call_index = len(self.calls)
                self.calls.append((prompt, Path(output_path), idempotency_key))
                Path(output_path).write_bytes(generated[call_index])
                return ProviderResult(
                    "openai", "image_generation", f"image-request-{call_index + 1}",
                    {}, {"tokens": 1}, 1,
                )

        class Reviewer:
            def __init__(self) -> None:
                self.calls = []

            def inspect_material(self, **request):
                self.calls.append(request)
                if len(self.calls) == 1:
                    return {
                        "result": "fail",
                        "reason": "generated a physical tool workshop",
                        "evidence": [{"semantic_match": False, "forbidden_subjects": []}],
                    }
                return {
                    "result": "pass",
                    "reason": "digital software workbench matches the scene",
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
            root = coordinator._root("job-review-guided-retry")
            (root / "plan.json").write_text(json.dumps({
                "visual_program_version": "1.0",
                "ratio": "9:16",
                "creative_concept": "AI 内容创作软件平台的一体化数字流程",
                "materials": [{"request_id": "detail", "semantic": "非人物环境素材，展示多工具集成工作台", "purpose": "context", "priority": "required", "ratio": "9:16"}],
                "scenes": [{"id": "scene_01", "intent": "展示软件平台内的工具协同流程", "headline": {"text": "一个工作台串联多种软件工具"}, "material_slots": [{"id": "detail", "semantic": "非人物环境素材，展示多工具集成工作台", "purpose": "context", "priority": "required", "ratio": "9:16"}]}],
            }), encoding="utf-8")
            job = {"job_id": "job-review-guided-retry", "owner_id": "owner", "stage_input_sha256": "0" * 64, "normalized_request_json": json.dumps({"input_type": "platform_talking_head", "material_asset_ids": []})}
            self._freeze_material_descriptors(coordinator, job)
            coordinator._stage("resolving_materials", job, SimpleNamespace(deadline_at=time.time() + 60))
            with patch("server.content_domains.ai_edit_v3.production._probe_image", autospec=True, return_value=SimpleNamespace(width=1024, height=1536)):
                outcome = coordinator._stage(
                    "generating_images", job, SimpleNamespace(deadline_at=time.time() + 60),
                )
            materials = json.loads((root / "materials.json").read_text(encoding="utf-8"))
            rejection = json.loads((root / "material-rejections.json").read_text(encoding="utf-8"))

        self.assertEqual("generating_audio", outcome.next_state)
        self.assertEqual(2, len(coordinator.image_generator.calls))
        first_prompt, first_path, first_key = coordinator.image_generator.calls[0]
        retry_prompt, retry_path, retry_key = coordinator.image_generator.calls[1]
        self.assertIn("数字软件界面", first_prompt)
        self.assertIn("不是扳手、锤子等实体工具", first_prompt)
        self.assertNotIn("上一版未通过语义匹配", first_prompt)
        self.assertIn("上一版未通过语义匹配", retry_prompt)
        self.assertIn("明显不同的构图", retry_prompt)
        physical_prompt = production_module._generated_material_prompt({
            "semantic": "木工车间里的实体工作台与锤子",
        })
        self.assertNotIn("数字软件界面", physical_prompt)
        unrelated_prompt = production_module._generated_material_prompt(
            {"semantic": "山野旅行中的地方美食和自然风景"},
            scene_context={"creative_concept": "AI 内容创作的温暖旅行故事"},
        )
        self.assertNotIn("数字软件界面", unrelated_prompt)
        self.assertNotEqual(first_path, retry_path)
        self.assertEqual("generated-01.png", first_path.name)
        self.assertEqual("generated-01-attempt-02.png", retry_path.name)
        self.assertNotEqual(first_key, retry_key)
        self.assertTrue(retry_key.endswith(":attempt:2"))
        self.assertEqual(2, len({item[1] for item in coordinator.cos.puts}))
        self.assertEqual([coordinator.cos.puts[0][1]], coordinator.cos.deletes)
        self.assertEqual(1, len(rejection["items"]))
        self.assertEqual((1, True, "deleted"), (
            rejection["items"][0]["generation_attempt"],
            rejection["items"][0]["retry_allowed"],
            rejection["items"][0]["cleanup_status"],
        ))
        self.assertEqual(1, len(materials["items"]))
        accepted = materials["items"][0]
        self.assertEqual("materials/generated-01-attempt-02.png", accepted["relative_path"])
        self.assertEqual("pass", accepted["visual_review"]["result"])
        self.assertTrue(all(
            tuple(call["forbidden_subjects"]) == production_module._GENERATED_MATERIAL_FORBIDDEN_SUBJECTS
            for call in coordinator.visual_inspector.calls
        ))

    def test_review_guided_prompt_is_deterministically_bounded_for_image_provider(self) -> None:
        request = {"semantic": "软件平台的数字工作台" + "界面模块" * 1000}
        scene_context = {
            "creative_concept": "数字软件平台" * 1000,
            "scene_intent": "展示应用程序工作流" * 1000,
            "headline": "多工具集成工作台" * 1000,
        }
        prior_review = {
            "result": "fail",
            "reason": "bounded structured review guidance",
            "evidence": [{
                "semantic_match": False,
                "forbidden_subjects": ["person", "wrong_product"],
            }],
        }

        first = production_module._generated_material_prompt(
            request, scene_context=scene_context, prior_review=prior_review,
        )
        second = production_module._generated_material_prompt(
            request, scene_context=scene_context, prior_review=prior_review,
        )

        self.assertEqual(first, second)
        self.assertEqual(production_module._GENERATED_MATERIAL_PROMPT_MAX_CHARS, len(first))
        self.assertIn("数字软件界面", first)
        self.assertIn("上一版未通过语义匹配", first)
        self.assertIn("禁止人物、脸、手", first)
        self.assertIn("禁止产品包装", first)

    def test_later_required_slot_failure_keeps_prior_accepted_candidate_owned(self) -> None:
        class Store:
            environment = "test"

            def resolve_request_uploads_for_owner(self, _owner, *, source_upload_id, material_ids, environment):
                return {"source_upload": None, "materials": []}

        class Cos:
            def __init__(self) -> None:
                self.puts = []
                self.deletes = []
                self.root = None
                self.sidecars_seen_before_put = []

            def put_file(self, source, key, _content_type, **_kwargs):
                sidecars = json.loads(
                    (self.root / "material-generation-candidates.json").read_text(encoding="utf-8")
                )["items"]
                candidate = next(item for item in sidecars if item["cos_key"] == key)
                if candidate["status"] != "local_ready":
                    raise AssertionError("candidate sidecar must precede COS upload")
                self.sidecars_seen_before_put.append(dict(candidate))
                self.puts.append((key, hashlib.sha256(Path(source).read_bytes()).hexdigest()))

            def delete_object(self, key):
                self.deletes.append(key)

        class Generator:
            def __init__(self) -> None:
                self.calls = []

            def generate(self, *, output_path, idempotency_key, **_kwargs):
                call_number = len(self.calls) + 1
                self.calls.append((Path(output_path), idempotency_key))
                Path(output_path).write_bytes(f"candidate-{call_number}".encode())
                return ProviderResult(
                    "openai", "image_generation", f"provider-image-{call_number}",
                    {}, {}, 1,
                )

        class Reviewer:
            def __init__(self) -> None:
                self.calls = []

            def inspect_material(self, **request):
                self.calls.append(request)
                if request["slot_id"] == "primary_visual":
                    return {
                        "result": "pass",
                        "reason": "first required slot accepted",
                        "evidence": [{"semantic_match": True, "forbidden_subjects": []}],
                    }
                return {
                    "result": "fail",
                    "reason": "second required slot remains unrelated",
                    "evidence": [{"semantic_match": False, "forbidden_subjects": []}],
                }

        with tempfile.TemporaryDirectory() as directory:
            coordinator = object.__new__(ProductionStageCoordinator)
            coordinator.store = Store()
            coordinator.work_root = Path(directory)
            coordinator.owner_hmac_secret = b"0123456789abcdef"
            coordinator.cos = Cos()
            coordinator.image_generator = Generator()
            coordinator.visual_inspector = Reviewer()
            root = coordinator._root("job-two-required-slots")
            coordinator.cos.root = root
            requests = [
                {"request_id": "primary_visual", "semantic": "abstract blue gradient", "purpose": "context", "priority": "required", "ratio": "9:16"},
                {"request_id": "detail_visual", "semantic": "abstract green workflow", "purpose": "context", "priority": "required", "ratio": "9:16"},
            ]
            (root / "plan.json").write_text(json.dumps({
                "visual_program_version": "1.0",
                "ratio": "9:16",
                "materials": requests,
                "scenes": [{
                    "id": "scene_01",
                    "material_slots": [
                        {"id": item["request_id"], **{key: item[key] for key in ("semantic", "purpose", "priority", "ratio")}}
                        for item in requests
                    ],
                }],
            }), encoding="utf-8")
            job = {
                "job_id": "job-two-required-slots",
                "owner_id": "owner",
                "stage_input_sha256": "0" * 64,
                "normalized_request_json": json.dumps({
                    "input_type": "platform_talking_head", "material_asset_ids": [],
                }),
            }
            self._freeze_material_descriptors(coordinator, job)
            coordinator._stage(
                "resolving_materials", job, SimpleNamespace(deadline_at=time.time() + 60),
            )
            with patch(
                "server.content_domains.ai_edit_v3.production._probe_image",
                autospec=True,
                return_value=SimpleNamespace(width=1024, height=1536),
            ):
                with self.assertRaisesRegex(
                    MaterialError, "generated_required_material_review_failed",
                ):
                    coordinator._stage(
                        "generating_images", job, SimpleNamespace(deadline_at=time.time() + 60),
                    )
                counts_after_failure = (
                    len(coordinator.image_generator.calls),
                    len(coordinator.cos.puts),
                    len(coordinator.visual_inspector.calls),
                )
                with self.assertRaisesRegex(
                    MaterialError, "generated_required_material_review_failed",
                ):
                    coordinator._stage(
                        "generating_images", job, SimpleNamespace(deadline_at=time.time() + 60),
                    )
            materials = json.loads((root / "materials.json").read_text(encoding="utf-8"))
            candidates = json.loads(
                (root / "material-generation-candidates.json").read_text(encoding="utf-8")
            )["items"]

        self.assertEqual((3, 3, 3), counts_after_failure)
        self.assertEqual(counts_after_failure, (
            len(coordinator.image_generator.calls),
            len(coordinator.cos.puts),
            len(coordinator.visual_inspector.calls),
        ))
        self.assertEqual(1, len(materials["items"]))
        self.assertEqual("primary_visual", materials["items"][0]["slot_id"])
        self.assertEqual(["detail_visual"], [item["slot_id"] for item in materials["unresolved"]])
        accepted_key = materials["items"][0]["object_key"]
        rejected_keys = [key for key, _digest in coordinator.cos.puts if key != accepted_key]
        self.assertNotIn(accepted_key, coordinator.cos.deletes)
        self.assertEqual(rejected_keys, coordinator.cos.deletes)
        self.assertEqual(["accepted", "rejected", "rejected"], [
            item["status"] for item in candidates
        ])
        self.assertEqual(3, len(coordinator.cos.sidecars_seen_before_put))
        self.assertTrue(all(item["status"] == "local_ready" for item in coordinator.cos.sidecars_seen_before_put))
        self.assertTrue(all(
            item.get("provider_request_id")
            and item.get("sha256")
            and item.get("cos_key")
            and item.get("idempotency_key")
            for item in candidates
        ))

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
                "semantic": "green product package",
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
                            "semantic": "green product package",
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
            self._freeze_material_descriptors(coordinator, job, [{
                "semantic": "green product package",
                "subject_type": "product",
                "composition": "center",
                "supported_ratios": ["9:16"],
                "risk_labels": [],
            }])

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
