import copy
import unittest

from server.content_domains.ai_edit_v2_materials import (
    MaterialResolutionError,
    resolve_materials,
)
from server.content_domains.ai_edit_v2_providers.base import ProviderResult


JOB = "123e4567-e89b-12d3-a456-426614174000"
PLAN = {
    "version": "2.0",
    "creation_mode": "natural_brief",
    "duration_ms": 2_000,
    "target_duration_ms": 2_000,
    "aspect_ratio": "16:9",
    "language": "zh-CN",
    "style_system": {},
    "scenes": [
        {
            "id": "scene_01",
            "start_ms": 0,
            "end_ms": 2_000,
            "intent": "show the real product clearly",
            "layout": "speaker_product_split",
            "visual_type": "product_hook",
            "headline": "Product close-up",
            "material_slots": ["slot_product_1"],
            "transition": "cut",
        }
    ],
    "caption_plan": {"source": "text_timeline", "style": "clean"},
    "audio_plan": {
        "speech_policy": "preserve_source",
        "music_policy": "duck_under_speech",
        "sfx_policy": "semantic_only",
    },
}


def candidate(asset_id, source, **overrides):
    value = {
        "asset_id": asset_id,
        "cos_key": f"ai-edit-v2/fc95297aa4f56781/{JOB}/materials/{asset_id}.png",
        "kind": "image",
        "owner": "user-a" if source != "platform_public" else None,
        "job_id": JOB if source == "current_upload" else None,
        "width": 1600,
        "height": 900,
        "score": 0.9,
        "relevant": True,
        "required": False,
        "is_real_product": False,
    }
    value.update(overrides)
    return value


class FakeRepositories:
    def __init__(self, by_source=None, required=None):
        self.owner = "user-a"
        self.by_source = by_source or {}
        self.required = list(required or [])
        self.records = None
        self.persisted = []
        self.search_calls = []

    def owner_for_job(self, job_id):
        if job_id != JOB:
            raise AssertionError("unexpected job")
        return self.owner

    def search(self, source, job_id, slot):
        self.assert_safe_slot(slot)
        self.search_calls.append(source)
        return copy.deepcopy(self.by_source.get(source, []))

    def required_materials(self, job_id):
        return copy.deepcopy(self.required)

    def save_resolution_records(
        self, job_id, records, *, status="succeeded", error_code=None
    ):
        self.records = copy.deepcopy(records)
        self.persisted.append(
            {
                "job_id": job_id,
                "records": copy.deepcopy(records),
                "status": status,
                "error_code": error_code,
            }
        )

    def assert_safe_slot(self, slot):
        self_test = unittest.TestCase()
        self_test.assertNotIn("url", str(slot).lower())
        self_test.assertEqual(slot["time_range"], {"start_ms": 0, "end_ms": 2_000})
        self_test.assertEqual(slot["dimensions"], {"width": 1920, "height": 1080})


class FakeImageProvider:
    def __init__(self, result=None):
        self.result = result
        self.calls = []

    def generate(self, slot, idempotency_key):
        self.calls.append((copy.deepcopy(slot), idempotency_key))
        if isinstance(self.result, Exception):
            raise self.result
        if self.result is not None:
            return self.result
        return ProviderResult(
            provider="openai",
            capability="image_generation",
            request_id="req-generated",
            payload={
                "asset_id": 901,
                "cos_key": (
                    f"ai-edit-v2/fc95297aa4f56781/{JOB}/generated/slot_product_1.png"
                ),
                "width": 1600,
                "height": 900,
                "content_type": "image/png",
            },
            cost_units=1,
            elapsed_ms=5,
        )


class MaterialResolverTests(unittest.TestCase):
    def test_material_priority_prefers_current_upload(self):
        repos = FakeRepositories(
            {
                source: [candidate(source, source)]
                for source in ("current_upload", "user_history", "platform_public")
            }
        )
        image = FakeImageProvider()

        resolved = resolve_materials(JOB, PLAN, repos, image)

        self.assertEqual(
            resolved["materials"]["slot_product_1"]["source"], "current_upload"
        )
        self.assertEqual(image.calls, [])
        self.assertEqual(repos.search_calls, ["current_upload"])

    def test_history_and_public_sources_are_never_searched(self):
        repos = FakeRepositories(
            {
                "user_history": [candidate("history", "user_history")],
                "platform_public": [candidate("public", "platform_public")],
            }
        )
        image = FakeImageProvider()

        resolved = resolve_materials(JOB, PLAN, repos, image)

        self.assertEqual(repos.search_calls, ["current_upload"])
        self.assertEqual(
            resolved["materials"]["slot_product_1"]["source"], "gpt_image"
        )
        self.assertEqual(len(image.calls), 1)

    def test_optional_current_upload_is_not_reused_across_slots(self):
        plan = copy.deepcopy(PLAN)
        plan["scenes"].append(
            {
                **copy.deepcopy(plan["scenes"][0]),
                "id": "scene_02",
                "material_slots": ["slot_product_2"],
            }
        )
        repos = FakeRepositories(
            {"current_upload": [candidate("only-upload", "current_upload")]}
        )
        image = FakeImageProvider()

        resolved = resolve_materials(JOB, plan, repos, image)

        self.assertEqual(repos.search_calls, ["current_upload", "current_upload"])
        self.assertEqual(
            resolved["materials"]["slot_product_1"]["source"], "current_upload"
        )
        self.assertEqual(
            resolved["materials"]["slot_product_2"]["source"], "gpt_image"
        )
        self.assertEqual(len(image.calls), 1)

    def test_optional_candidate_without_relevance_evidence_uses_generated_image(self):
        unverified = candidate(
            "unverified-upload",
            "current_upload",
            relevant=None,
            score=None,
            semantic_label=None,
            filename="IMG_0001.png",
        )
        repos = FakeRepositories({"current_upload": [unverified]})
        image = FakeImageProvider()

        resolved = resolve_materials(JOB, PLAN, repos, image)

        self.assertEqual(
            resolved["materials"]["slot_product_1"]["source"], "gpt_image"
        )
        record = next(
            item
            for item in repos.records
            if item.get("asset_id") == "unverified-upload"
        )
        self.assertEqual(record["exclusion_code"], "relevance_unverified")
        self.assertEqual(len(image.calls), 1)

    def test_matching_semantic_label_is_scored_for_the_current_slot(self):
        labeled = candidate(
            "labeled-upload",
            "current_upload",
            relevant=None,
            score=None,
            semantic_label="Product close-up",
            filename="IMG_0002.png",
        )
        repos = FakeRepositories({"current_upload": [labeled]})
        image = FakeImageProvider()

        resolved = resolve_materials(JOB, PLAN, repos, image)

        material = resolved["materials"]["slot_product_1"]
        self.assertEqual(material["asset_id"], "labeled-upload")
        self.assertEqual(material["source"], "current_upload")
        selected = next(
            item
            for item in repos.records
            if item.get("asset_id") == "labeled-upload"
        )
        self.assertGreaterEqual(selected["selected_score"], 0.8)
        self.assertIsNone(selected["exclusion_code"])
        self.assertEqual(image.calls, [])

    def test_required_material_must_be_used_once_or_more(self):
        plan_without_slots = copy.deepcopy(PLAN)
        plan_without_slots["scenes"][0]["material_slots"] = []
        required = candidate(
            "must-use", "current_upload", required=True, is_real_product=True
        )
        repos = FakeRepositories(required=[required])

        with self.assertRaises(MaterialResolutionError) as caught:
            resolve_materials(plan=plan_without_slots, job_id=JOB, repositories=repos,
                              image_provider=FakeImageProvider())

        self.assertEqual(caught.exception.code, "required_material_unused")
        self.assertEqual(repos.persisted[-1]["status"], "failed")
        self.assertEqual(
            repos.persisted[-1]["error_code"], "required_material_unused"
        )

    def test_rejects_bad_candidates_before_selecting_highest_qualified_score(self):
        bad = [
            candidate("duplicate", "current_upload", duplicate=True, score=1.0),
            candidate("blurred", "current_upload", blurred=True, score=0.99),
            candidate("irrelevant", "current_upload", relevant=False, score=0.98),
            candidate("bad-ratio", "current_upload", width=900, height=1600, score=0.97),
        ]
        good = candidate("good", "current_upload", score=0.8)
        repos = FakeRepositories({"current_upload": [*bad, good]})

        resolved = resolve_materials(JOB, PLAN, repos, FakeImageProvider())

        self.assertEqual(resolved["materials"]["slot_product_1"]["asset_id"], "good")
        by_asset = {record["asset_id"]: record for record in repos.records}
        self.assertEqual(by_asset["duplicate"]["exclusion_code"], "duplicate")
        self.assertEqual(by_asset["blurred"]["exclusion_code"], "blurred")
        self.assertEqual(by_asset["irrelevant"]["exclusion_code"], "irrelevant")
        self.assertEqual(by_asset["bad-ratio"]["exclusion_code"], "invalid_ratio")
        selected = by_asset["good"]
        self.assertEqual(selected["semantic_query"], "Product close-up")
        self.assertEqual(selected["time_range"], {"start_ms": 0, "end_ms": 2_000})
        self.assertEqual(selected["ratio"], "16:9")
        self.assertEqual(selected["dimensions"], {"width": 1920, "height": 1080})
        self.assertEqual(selected["selected_score"], 0.8)
        self.assertIsNone(selected["exclusion_code"])
        self.assertNotIn("url", str(repos.records).lower())

    def test_required_material_is_selected_ahead_of_optional_peer(self):
        required = candidate("required", "current_upload", required=True, score=0.5)
        optional = candidate("optional", "current_upload", score=1.0)
        repos = FakeRepositories(
            {"current_upload": [optional, required]}, required=[required]
        )

        resolved = resolve_materials(JOB, PLAN, repos, FakeImageProvider())

        self.assertEqual(
            resolved["materials"]["slot_product_1"]["asset_id"], "required"
        )

    def test_real_product_material_is_never_replaced_by_generated_image(self):
        product = candidate(
            "real-product",
            "current_upload",
            required=True,
            is_real_product=True,
            blurred=True,
        )
        repos = FakeRepositories({"current_upload": [product]}, required=[product])
        image = FakeImageProvider()

        with self.assertRaises(MaterialResolutionError) as caught:
            resolve_materials(JOB, PLAN, repos, image)

        self.assertEqual(caught.exception.code, "required_material_unavailable")
        self.assertEqual(image.calls, [])

    def test_invalid_non_required_real_product_candidate_does_not_block_generation(self):
        invalid_candidates = (
            candidate(
                "foreign-product",
                "user_history",
                owner="user-b",
                is_real_product=True,
            ),
            candidate(
                "blurred-product",
                "current_upload",
                blurred=True,
                is_real_product=True,
            ),
        )

        for invalid in invalid_candidates:
            with self.subTest(asset_id=invalid["asset_id"]):
                source = "user_history" if invalid["asset_id"] == "foreign-product" else "current_upload"
                image = FakeImageProvider()
                resolved = resolve_materials(
                    JOB,
                    PLAN,
                    FakeRepositories({source: [invalid]}),
                    image,
                )
                self.assertEqual(
                    resolved["materials"]["slot_product_1"]["source"], "gpt_image"
                )
                self.assertEqual(len(image.calls), 1)

    def test_non_required_generation_failure_is_an_explicit_degradation(self):
        repos = FakeRepositories()

        resolved = resolve_materials(
            JOB, PLAN, repos, FakeImageProvider(RuntimeError("provider down"))
        )

        self.assertEqual(resolved["material_resolution_status"], "image_generation_degraded")
        self.assertNotIn("slot_product_1", resolved["materials"])

    def test_required_slot_without_qualified_fallback_fails(self):
        required = candidate("must-use", "current_upload", required=True, blurred=True)
        repos = FakeRepositories({"current_upload": [required]}, required=[required])

        with self.assertRaises(MaterialResolutionError) as caught:
            resolve_materials(JOB, PLAN, repos, FakeImageProvider(RuntimeError("down")))

        self.assertEqual(caught.exception.code, "required_material_unavailable")
        self.assertEqual(repos.persisted[-1]["status"], "failed")
        self.assertEqual(
            repos.persisted[-1]["error_code"], "required_material_unavailable"
        )

    def test_invalid_required_material_cannot_be_hidden_by_optional_fallback(self):
        required = candidate("must-use", "current_upload", required=True, blurred=True)
        optional = candidate("optional", "current_upload", score=1.0)
        repos = FakeRepositories(
            {"current_upload": [required, optional]}, required=[required]
        )

        with self.assertRaises(MaterialResolutionError) as caught:
            resolve_materials(JOB, PLAN, repos, FakeImageProvider())

        self.assertEqual(caught.exception.code, "required_material_unavailable")

    def test_cross_job_current_upload_is_excluded_without_history_or_public_fallback(self):
        wrong_job = candidate("wrong-job", "current_upload", job_id="other-job")
        repos = FakeRepositories({"current_upload": [wrong_job]})

        resolved = resolve_materials(JOB, PLAN, repos, FakeImageProvider())

        self.assertEqual(
            resolved["materials"]["slot_product_1"]["source"], "gpt_image"
        )
        by_asset = {record["asset_id"]: record for record in repos.records}
        self.assertEqual(by_asset["wrong-job"]["exclusion_code"], "job_scope_mismatch")
        self.assertIsNone(by_asset["wrong-job"]["cos_key"])

    def test_private_candidate_cos_key_must_match_user_and_current_job_scope(self):
        wrong_cos_scope = candidate(
            "wrong-cos-scope",
            "current_upload",
            cos_key=(
                "ai-edit-v2/fc95297aa4f56781/"
                "223e4567-e89b-12d3-a456-426614174000/materials/wrong.png"
            ),
        )
        repos = FakeRepositories({"current_upload": [wrong_cos_scope]})

        resolved = resolve_materials(JOB, PLAN, repos, FakeImageProvider())

        self.assertEqual(
            resolved["materials"]["slot_product_1"]["source"], "gpt_image"
        )
        wrong_record = next(
            record for record in repos.records if record["asset_id"] == "wrong-cos-scope"
        )
        self.assertEqual(wrong_record["exclusion_code"], "cos_scope_mismatch")
        self.assertIsNone(wrong_record["cos_key"])

    def test_generation_result_must_not_expose_provider_or_signed_url(self):
        unsafe = ProviderResult(
            provider="openai",
            capability="image_generation",
            request_id="req-unsafe",
            payload={
                "asset_id": 901,
                "cos_key": "safe/key.png",
                "provider_url": "https://provider.example/generated.png?signature=secret",
            },
            cost_units=1,
            elapsed_ms=1,
        )

        with self.assertRaises(MaterialResolutionError) as caught:
            resolve_materials(JOB, PLAN, FakeRepositories(), FakeImageProvider(unsafe))

        self.assertEqual(caught.exception.code, "image_generation_result_invalid")

    def test_generation_result_must_match_current_job_cos_scope(self):
        cross_job = ProviderResult(
            provider="openai",
            capability="image_generation",
            request_id="req-cross-job",
            payload={
                "asset_id": 902,
                "cos_key": (
                    "ai-edit-v2/fc95297aa4f56781/"
                    "223e4567-e89b-12d3-a456-426614174000/generated/image.png"
                ),
            },
            cost_units=1,
            elapsed_ms=1,
        )

        with self.assertRaises(MaterialResolutionError) as caught:
            resolve_materials(
                JOB, PLAN, FakeRepositories(), FakeImageProvider(cross_job)
            )

        self.assertEqual(caught.exception.code, "image_generation_result_invalid")


if __name__ == "__main__":
    unittest.main()
