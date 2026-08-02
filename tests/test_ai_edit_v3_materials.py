from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from server.content_domains.ai_edit_v3.director import ValidatedPlan
from server.content_domains.ai_edit_v3.materials import (
    MaterialDescriptor,
    MaterialError,
    analyze_current_images,
    resolve_uploaded_materials,
)


class FakeRepository:
    def __init__(self, records):
        self.records = records
        self.calls = []

    def get_images_for_owner(self, owner, ids):
        self.calls.append((owner, tuple(ids)))
        return self.records


class FakeProvider:
    def __init__(self):
        self.requests = []

    def analyze_images(self, request, **kwargs):
        self.requests.append((request, kwargs))
        descriptors = []
        for image in request["images"]:
            descriptors.append({
                "material_id": image["material_id"],
                "semantic": ["product"],
                "subject_type": "product",
                "composition": "center",
                "supported_ratios": ["9:16"],
                "risk_labels": [],
                "sha256": image["sha256"],
            })
        return {"descriptors": descriptors}


class MaterialContractTests(unittest.TestCase):
    def test_only_semantically_matching_current_image_binds(self) -> None:
        plan = SimpleNamespace(
            material_slots=(
                {"id": "slot_product", "semantic": ["product"], "required": True, "ratio": "9:16"},
                {"id": "slot_store", "semantic": ["store"], "required": True, "ratio": "9:16"},
            )
        )
        current = MaterialDescriptor(
            material_id="image-1",
            semantic=("product",),
            subject_type="product",
            composition="center",
            supported_ratios=("9:16",),
            risk_labels=(),
            sha256="a" * 64,
        )

        result = resolve_uploaded_materials(plan, [current])

        self.assertEqual(result.slots["slot_product"].material_id, "image-1")
        self.assertEqual(result.slots["slot_store"].status, "generation_required")
        self.assertNotIn("history", repr(result))

    def test_matching_is_deterministic_and_rejects_unrelated_images(self) -> None:
        plan = SimpleNamespace(material_slots=(
            {"id": "slot", "semantic": "green product package", "purpose": "product", "priority": "required", "ratio": "9:16"},
        ))
        images = [
            MaterialDescriptor("z-image", ("product",), "product", "center", ("9:16",), (), "b" * 64),
            MaterialDescriptor("a-image", ("green", "product", "package"), "product", "center", ("9:16",), (), "a" * 64),
            MaterialDescriptor("store", ("store",), "store", "wide", ("16:9",), (), "c" * 64),
        ]

        first = resolve_uploaded_materials(plan, images)
        second = resolve_uploaded_materials(plan, tuple(reversed(images)))

        self.assertEqual(first.slots["slot"].material_id, "a-image")
        self.assertEqual(second.slots["slot"].material_id, "a-image")

    def test_analysis_reads_only_current_declared_owner_images_in_bounded_batches(self) -> None:
        ids = [f"img-{index}" for index in range(6)]
        records = [
            {
                "asset_id": asset_id,
                "owner_id": "user-1",
                "media_type": "image",
                "status": "completed",
                "thumbnail": f"thumb:{asset_id}",
                "thumbnail_width": 640,
                "thumbnail_height": 640,
                "sha256": f"{index:064x}",
            }
            for index, asset_id in enumerate(ids)
        ]
        repository = FakeRepository(records)
        provider = FakeProvider()
        context = SimpleNamespace(
            material_repository=repository,
            deadline_at=9999999999.0,
            job_id="job-1",
        )

        result = analyze_current_images(
            {"owner_id": "user-1", "material_asset_ids": ids}, context, provider
        )

        self.assertEqual(repository.calls, [("user-1", tuple(ids))])
        self.assertEqual([len(request[0]["images"]) for request in provider.requests], [5, 1])
        self.assertEqual(len(result), 6)
        self.assertTrue(all(image["thumbnail_width"] <= 768 for image in provider.requests[0][0]["images"]))

    def test_analysis_rejects_history_other_owner_and_non_images_before_qwen(self) -> None:
        invalid_records = (
            {"asset_id": "other", "owner_id": "user-1", "media_type": "image", "status": "completed"},
            {"asset_id": "img-1", "owner_id": "user-2", "media_type": "image", "status": "completed"},
            {"asset_id": "img-1", "owner_id": "user-1", "media_type": "video", "status": "completed"},
        )
        for record in invalid_records:
            provider = FakeProvider()
            context = SimpleNamespace(
                material_repository=FakeRepository([record]),
                deadline_at=9999999999.0,
                job_id="job-1",
            )
            with self.subTest(record=record):
                with self.assertRaises(MaterialError):
                    analyze_current_images(
                        {"owner_id": "user-1", "material_asset_ids": ["img-1"]},
                        context,
                        provider,
                    )
                self.assertEqual(provider.requests, [])


if __name__ == "__main__":
    unittest.main()
