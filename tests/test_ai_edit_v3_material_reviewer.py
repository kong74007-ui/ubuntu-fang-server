from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from server.content_domains.ai_edit_v3 import production
from server.content_domains.ai_edit_v3.providers.base import ProviderResult


class _Cos:
    def __init__(self) -> None:
        self.calls = []

    def presign_get(self, cos_key, *, expires):
        self.calls.append((cos_key, expires))
        return "https://private.example/signed?secret=short-lived"


class _QwenVision:
    def __init__(self) -> None:
        self.requests = []

    def inspect_image(self, request, *, deadline_at):
        self.requests.append((request, deadline_at))
        return ProviderResult(
            provider="dashscope",
            capability="material_review",
            request_id="review-request-1",
            payload={
                "content": json.dumps({
                    "result": "pass",
                    "reason": "subject and exclusions verified",
                    "evidence": [{
                        "semantic_match": True,
                        "forbidden_subjects": [],
                    }],
                })
            },
            usage={"tokens": 9},
            elapsed_ms=3,
        )


class MaterialReviewerTests(unittest.TestCase):
    def test_real_qwen_client_result_is_accepted_by_descriptor_reviewer(self):
        from server.content_domains.ai_edit_v3.providers.qwen_compatible import (
            DashScopeCompatibleQwenClient,
        )

        def mock_http(_method, _url, _headers, _body, _timeout):
            return {
                "id": "real-compatible-descriptor-1",
                "choices": [{"message": {"content": json.dumps({
                    "descriptors": [{
                        "upload_alias": "upload_01",
                        "semantic": "绿色产品包装正面实拍",
                        "subject_type": "product",
                        "composition": "centered close-up",
                        "supported_ratios": ["9:16"],
                        "risk_labels": [],
                    }],
                }, ensure_ascii=False)}}],
                "usage": {},
            }

        client = DashScopeCompatibleQwenClient(http_request=mock_http)
        reviewer = production.QwenMaterialReviewer(cos=_Cos(), client=client)
        with patch.dict("os.environ", {"DASHSCOPE_API_KEY": "test-key"}, clear=False):
            result = reviewer.describe_materials(
                [{
                    "upload_alias": "upload_01",
                    "width": 320,
                    "height": 512,
                    "jpeg_bytes": b"\xff\xd8sanitized-pixels",
                }],
                deadline_at=10_000_000_000.0,
            )

        self.assertEqual("real-compatible-descriptor-1", result.request_id)
        self.assertEqual("绿色产品包装正面实拍", result["descriptors"][0]["semantic"])

    def test_duck_typed_descriptor_result_requires_exact_provider_contract(self):
        content = json.dumps({"descriptors": [{
            "upload_alias": "upload_01",
            "semantic": "绿色产品包装正面实拍",
            "subject_type": "product",
            "composition": "centered close-up",
            "supported_ratios": ["9:16"],
            "risk_labels": [],
        }]}, ensure_ascii=False)
        invalid_results = (
            SimpleNamespace(
                provider="openai", capability="material_analysis",
                request_id="request-1", payload={"content": content},
            ),
            SimpleNamespace(
                provider="dashscope", capability="material_review",
                request_id="request-1", payload={"content": content},
            ),
            SimpleNamespace(
                provider="dashscope", capability="material_analysis",
                request_id="", payload={"content": content},
            ),
            SimpleNamespace(
                provider="dashscope", capability="material_analysis",
                request_id="request-1", payload="not-a-mapping",
            ),
        )

        for result in invalid_results:
            with self.subTest(result=result), self.assertRaisesRegex(
                ValueError, "material_descriptor_invalid",
            ):
                production._material_descriptor_payload(
                    result,
                    expected_aliases=("upload_01",),
                )

    def test_qwen_descriptor_adapter_sends_only_alias_dimensions_and_inline_jpeg(self):
        class DescriptorQwen:
            def __init__(self):
                self.requests = []

            def inspect_image(self, *_args, **_kwargs):
                raise AssertionError("descriptor analysis must not use signed-URL review")

            def describe_images(self, request, *, deadline_at):
                self.requests.append((request, deadline_at))
                return ProviderResult(
                    provider="dashscope",
                    capability="material_analysis",
                    request_id="descriptor-request-1",
                    payload={"content": json.dumps({"descriptors": [{
                        "upload_alias": "upload_01",
                        "semantic": "绿色产品包装正面实拍",
                        "subject_type": "product",
                        "composition": "centered close-up",
                        "supported_ratios": ["9:16"],
                        "risk_labels": [],
                    }]}, ensure_ascii=False)},
                    usage={},
                    elapsed_ms=1,
                )

        qwen = DescriptorQwen()
        reviewer = production.QwenMaterialReviewer(cos=_Cos(), client=qwen)
        result = reviewer.describe_materials(
            [{
                "upload_alias": "upload_01",
                "width": 320,
                "height": 512,
                "jpeg_bytes": b"\xff\xd8sanitized-pixels",
            }],
            deadline_at=123.0,
        )

        self.assertEqual("descriptor-request-1", result.request_id)
        self.assertEqual("绿色产品包装正面实拍", result["descriptors"][0]["semantic"])
        request, deadline_at = qwen.requests[0]
        self.assertEqual(123.0, deadline_at)
        self.assertEqual("material-descriptors-v1", request["output_contract"])
        image = request["images"][0]
        self.assertEqual(
            {"upload_alias", "width", "height", "data_url"},
            set(image),
        )
        self.assertTrue(image["data_url"].startswith("data:image/jpeg;base64,"))
        serialized = json.dumps(request, ensure_ascii=False)
        self.assertNotIn("material_id", serialized)
        self.assertNotIn("sha256", serialized)
        self.assertNotIn("cos_key", serialized)

    def test_bootstrap_injects_qwen_material_reviewer_into_coordinator(self):
        from server.content_domains.ai_edit_v3 import bootstrap

        captured = {}
        cos = object()
        config = SimpleNamespace(
            enabled=True,
            db_path=Path("v3.db"),
            v2_db_path=Path("v2.db"),
            environment="test",
            owner_hmac_secret_file=Path("owner.key"),
            queue_capacity=1,
            temp_bytes_limit=1024,
            director_timeout_seconds=45,
            auto_repair_enabled=False,
        )

        def coordinator(**kwargs):
            captured.update(kwargs)
            return object()

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            "os.environ",
            {
                "AI_EDIT_V3_RENDERER_ROOT": directory,
                "AI_EDIT_V3_RENDERER_RELEASES_ROOT": directory,
                "AI_EDIT_V3_WORK_ROOT": directory,
            },
            clear=False,
        ), patch.object(bootstrap, "load_config", return_value=config), patch.object(
            bootstrap, "V3Store", return_value=SimpleNamespace()
        ), patch.object(bootstrap, "_seed", return_value=()), patch.object(
            bootstrap, "_read_secret", return_value=b"0123456789abcdef"
        ), patch.object(bootstrap, "V3Cos", return_value=cos), patch.object(
            bootstrap,
            "verify_renderer_release",
            return_value=SimpleNamespace(renderer_build_id="build-1"),
        ), patch.object(Path, "read_text", return_value="a" * 64), patch.object(
            bootstrap, "HyperframesRenderer", return_value=object()
        ), patch.object(
            bootstrap, "ProductionStageCoordinator", side_effect=coordinator
        ), patch.object(bootstrap, "build_stage_handlers", return_value={}), patch.object(
            bootstrap, "build_runtime", return_value=object()
        ), patch.object(bootstrap, "SharedPublisher", return_value=object()), patch.object(
            bootstrap, "ProductionCatalog", return_value=object()
        ), patch.object(bootstrap, "Capacity", return_value=object()), patch.object(
            bootstrap, "EditV3Service", return_value=SimpleNamespace()
        ):
            bootstrap._build()

        reviewer = captured.get("visual_inspector")
        self.assertIsNotNone(reviewer, "bootstrap must explicitly inject the reviewer")
        self.assertIs(reviewer, captured.get("material_analyzer"))
        self.assertIs(cos, reviewer.cos)
        self.assertIs(False, captured.get("auto_repair_enabled"))
        self.assertTrue(callable(getattr(reviewer, "inspect_material", None)))

    def test_qwen_reviewer_presigns_private_key_only_inside_adapter(self):
        reviewer_type = getattr(production, "QwenMaterialReviewer", None)
        self.assertTrue(callable(reviewer_type), "production Qwen-VL material reviewer is required")
        cos = _Cos()
        qwen = _QwenVision()
        reviewer = reviewer_type(cos=cos, client=qwen)

        review = reviewer.inspect_material(
            cos_key="test/ai-edit-v3/owner/job/materials/generated-01.png",
            semantic="clean product detail on a neutral background",
            forbidden_subjects=("person", "face", "wrong_product"),
            source_metadata={"source": "generated", "sha256": "a" * 64},
            deadline_at=123.0,
        )

        self.assertEqual("pass", review["result"])
        self.assertEqual(
            [("test/ai-edit-v3/owner/job/materials/generated-01.png", 300)],
            cos.calls,
        )
        request, deadline_at = qwen.requests[0]
        self.assertEqual(123.0, deadline_at)
        self.assertEqual(
            "https://private.example/signed?secret=short-lived",
            request["image_url"],
        )
        self.assertNotIn("image_url", review)
        self.assertNotIn("signed", json.dumps(review))

    def test_qwen_reviewer_rejects_echoed_signed_url_before_receipt(self):
        class EchoingQwen(_QwenVision):
            def inspect_image(self, request, *, deadline_at):
                return ProviderResult(
                    provider="dashscope",
                    capability="material_review",
                    request_id="review-request-echo",
                    payload={"content": json.dumps({
                        "result": "pass",
                        "reason": request["image_url"],
                        "evidence": [{"semantic_match": True, "forbidden_subjects": []}],
                    })},
                    usage={},
                    elapsed_ms=1,
                )

        reviewer = production.QwenMaterialReviewer(cos=_Cos(), client=EchoingQwen())
        with self.assertRaisesRegex(ValueError, "generated_material_review_invalid"):
            reviewer.inspect_material(
                cos_key="test/ai-edit-v3/owner/job/materials/generated-01.png",
                semantic="clean product detail",
                forbidden_subjects=("person",),
                source_metadata={"source": "generated", "sha256": "a" * 64},
                deadline_at=123.0,
            )

    def test_completed_receipt_replay_does_not_call_review_provider_twice(self):
        invoke_once = getattr(production, "invoke_provider_once", None)
        self.assertTrue(callable(invoke_once), "real-store provider receipt helper is required")
        completed = {
            "status": "completed",
            "stage": "generating_images",
            "provider": "dashscope",
            "capability": "material_review",
            "request_sha256": "b" * 64,
            "external_id": "review-request-1",
            "result_json": json.dumps({
                "result": "pass",
                "reason": "subject verified",
                "evidence": [{"semantic_match": True, "forbidden_subjects": []}],
            }),
        }

        class Store:
            def __init__(self):
                self.intent_calls = []
                self.get_calls = []
                self.bind_calls = []
                self.claim_calls = []

            def record_provider_intent(self, *args):
                self.intent_calls.append(args)
                return completed

            def get_provider_task_for_claim(self, *args):
                self.get_calls.append(args)
                return completed

            def bind_provider_result(self, *args):
                self.bind_calls.append(args)
                raise AssertionError("completed receipt must not be rebound")

        store = Store()
        context = SimpleNamespace(
            claim=object(), stage_attempt_id="stage-attempt-1", deadline_at=123.0
        )
        provider_calls = []
        call = lambda: provider_calls.append(True)
        kwargs = dict(
            store=store,
            context=context,
            stage="generating_images",
            provider="dashscope",
            capability="material_review",
            operation_key="ai-edit-v3:job-1:material-review:material-01",
            request_sha256="b" * 64,
            call=call,
            now_ms=1000,
        )

        first = invoke_once(**kwargs)
        second = invoke_once(**kwargs)

        self.assertEqual(first, second)
        self.assertEqual([], provider_calls)
        self.assertEqual([], store.bind_calls)
        self.assertEqual([], store.intent_calls)
        self.assertEqual(2, len(store.get_calls))
        self.assertEqual(context.claim, store.get_calls[0][0])

    def test_first_call_records_and_binds_then_replays_receipt(self):
        class Review(dict):
            request_id = "review-request-new"

        class Store:
            def __init__(self):
                self.task = None
                self.intent_calls = []
                self.bind_calls = []
                self.claim_calls = []

            def get_provider_task_for_claim(self, *_args):
                return self.task

            def claim_provider_submission(self, *args):
                self.claim_calls.append(args)
                return True

            def record_provider_intent(self, *args):
                self.intent_calls.append(args)
                self.task = {
                    "status": "intent_recorded",
                    "stage": args[1],
                    "provider": args[3],
                    "capability": args[4],
                    "request_sha256": args[6],
                }
                return self.task

            def bind_provider_result(self, *args):
                self.bind_calls.append(args)
                self.task = {
                    **self.task,
                    "status": "completed",
                    "external_id": args[2],
                    "result_json": json.dumps(args[4]),
                }
                return self.task

        store = Store()
        context = SimpleNamespace(claim=object(), stage_attempt_id="stage-attempt-1")
        calls = []

        def provider_call():
            calls.append(True)
            return Review({
                "result": "pass",
                "reason": "verified",
                "evidence": [{"semantic_match": True, "forbidden_subjects": []}],
            })

        kwargs = dict(
            store=store,
            context=context,
            stage="generating_images",
            provider="dashscope",
            capability="material_review",
            operation_key="ai-edit-v3:job-1:material-review:scene-1:detail",
            request_sha256="b" * 64,
            call=provider_call,
            now_ms=1000,
        )
        first = production.invoke_provider_once(**kwargs)
        second = production.invoke_provider_once(**kwargs)

        self.assertEqual(first, second)
        self.assertEqual([True], calls)
        self.assertEqual(1, len(store.intent_calls))
        self.assertEqual(1, len(store.bind_calls))
        self.assertEqual(1, len(store.claim_calls))

    def test_recorded_but_unclaimed_intent_is_safely_claimed_on_replay(self):
        class Review(dict):
            request_id = "review-request-recovered"

        class Store:
            def __init__(self):
                self.task = {
                    "status": "intent_recorded",
                    "stage_attempt_id": "stage-attempt-1",
                    "stage": "generating_images",
                    "provider": "dashscope",
                    "capability": "material_review",
                    "request_sha256": "b" * 64,
                }
                self.claim_calls = []
                self.intent_calls = []

            def get_provider_task_for_claim(self, *_args):
                return self.task

            def record_provider_intent(self, *args):
                self.intent_calls.append(args)
                self.task = {
                    "status": "intent_recorded",
                    "stage_attempt_id": args[2],
                    "stage": args[1],
                    "provider": args[3],
                    "capability": args[4],
                    "request_sha256": args[6],
                }
                return self.task

            def claim_provider_submission(self, *args):
                if self.task["stage_attempt_id"] != args[2]:
                    raise ValueError("provider_attempt_mismatch")
                self.claim_calls.append(args)
                self.task = {**self.task, "status": "submitting"}
                return True

            def bind_provider_result(self, *args):
                self.task = {
                    **self.task,
                    "status": "completed",
                    "external_id": args[2],
                    "result_json": json.dumps(args[4]),
                }
                return self.task

        store = Store()
        context = SimpleNamespace(claim=object(), stage_attempt_id="stage-attempt-2")
        provider_calls = []

        result = production.invoke_provider_once(
            store=store,
            context=context,
            stage="generating_images",
            provider="dashscope",
            capability="material_review",
            operation_key="ai-edit-v3:job-1:material-review:scene-1:detail",
            request_sha256="b" * 64,
            call=lambda: provider_calls.append(True) or Review({
                "result": "pass",
                "reason": "verified",
                "evidence": [{"semantic_match": True, "forbidden_subjects": []}],
            }),
            now_ms=1000,
        )

        self.assertEqual("pass", result["result"])
        self.assertEqual([True], provider_calls)
        self.assertEqual(1, len(store.intent_calls))
        self.assertEqual(1, len(store.claim_calls))

    def test_invalid_descriptor_contract_releases_receipt_without_caching_raw_response(self):
        class DescriptorQwen:
            def __init__(self):
                self.calls = 0

            def inspect_image(self, *_args, **_kwargs):
                raise AssertionError("descriptor analysis must not use signed-URL review")

            def describe_images(self, _request, *, deadline_at):
                self.calls += 1
                if self.calls == 1:
                    content = json.dumps({
                        "output_contract": {"descriptors": []},
                    })
                elif self.calls == 2:
                    content = json.dumps({"descriptors": [{
                        "upload_alias": "upload_01",
                        "semantic": "green product package",
                        "subject_type": "product",
                        "composition": "centered close-up",
                        "supported_ratios": [[]],
                        "risk_labels": [],
                    }]})
                else:
                    content = json.dumps({"descriptors": [{
                        "upload_alias": "upload_01",
                        "semantic": "green product package",
                        "subject_type": "product",
                        "composition": "centered close-up",
                        "supported_ratios": ["9:16"],
                        "risk_labels": [],
                    }]})
                return ProviderResult(
                    provider="dashscope",
                    capability="material_analysis",
                    request_id=f"descriptor-request-{self.calls}",
                    payload={"content": content},
                    usage={},
                    elapsed_ms=1,
                )

        class Store:
            def __init__(self):
                self.task = None
                self.bind_calls = []
                self.release_calls = []

            def get_provider_task_for_claim(self, *_args):
                return self.task

            def record_provider_intent(self, *args):
                if self.task is None:
                    self.task = {
                        "status": "intent_recorded",
                        "stage": args[1],
                        "provider": args[3],
                        "capability": args[4],
                        "request_sha256": args[6],
                    }
                return self.task

            def claim_provider_submission(self, *_args):
                if self.task["status"] != "intent_recorded":
                    return False
                self.task = {**self.task, "status": "submitting"}
                return True

            def release_material_analysis_submission(self, *args):
                self.release_calls.append(args)
                if self.task["status"] != "submitting":
                    raise AssertionError("only an unbound submission may be released")
                self.task = {**self.task, "status": "intent_recorded"}
                return self.task

            def bind_provider_result(self, *args):
                self.bind_calls.append(args)
                self.task = {
                    **self.task,
                    "status": args[3],
                    "external_id": args[2],
                    "result_json": json.dumps(args[4]),
                }
                return self.task

        qwen = DescriptorQwen()
        reviewer = production.QwenMaterialReviewer(cos=_Cos(), client=qwen)
        store = Store()
        context = SimpleNamespace(
            claim=object(), stage_attempt_id="stage-attempt-1"
        )
        images = [{
            "upload_alias": "upload_01",
            "width": 320,
            "height": 512,
            "jpeg_bytes": b"\xff\xd8sanitized-pixels",
        }]

        def call():
            return reviewer.describe_materials(images, deadline_at=123.0)

        kwargs = dict(
            store=store,
            context=context,
            stage="planning",
            provider="dashscope",
            capability="material_analysis",
            operation_key="ai-edit-v3:job-1:material-analysis:0",
            request_sha256="b" * 64,
            call=call,
            now_ms=1000,
        )

        with self.assertRaisesRegex(
            production.MaterialError, "material_descriptor_invalid",
        ):
            production.invoke_provider_once(**kwargs)

        self.assertEqual("intent_recorded", store.task["status"])
        self.assertNotIn("external_id", store.task)
        self.assertNotIn("result_json", store.task)
        self.assertEqual([], store.bind_calls)
        self.assertEqual(1, len(store.release_calls))
        self.assertEqual(
            (
                context.claim,
                "ai-edit-v3:job-1:material-analysis:0",
                1000,
            ),
            store.release_calls[0],
        )

        with self.assertRaisesRegex(
            production.MaterialError, "material_descriptor_invalid",
        ):
            production.invoke_provider_once(**kwargs)

        self.assertEqual("intent_recorded", store.task["status"])
        self.assertNotIn("external_id", store.task)
        self.assertNotIn("result_json", store.task)
        self.assertEqual([], store.bind_calls)
        self.assertEqual(2, len(store.release_calls))

        result = production.invoke_provider_once(**kwargs)

        self.assertEqual("green product package", result["descriptors"][0]["semantic"])
        self.assertEqual(3, qwen.calls)
        self.assertEqual(1, len(store.bind_calls))

    def test_descriptor_nested_model_values_never_escape_as_type_error(self):
        descriptor = {
            "upload_alias": "upload_01",
            "semantic": "green product package",
            "subject_type": "product",
            "composition": "centered close-up",
            "supported_ratios": ["9:16"],
            "risk_labels": [],
        }
        malformed = (
            ("subject_type_list", "subject_type", []),
            ("subject_type_number", "subject_type", 1),
            ("ratio_nested_list", "supported_ratios", [[]]),
            ("ratio_number", "supported_ratios", [1]),
            ("risk_nested_object", "risk_labels", [{}]),
            ("risk_null", "risk_labels", [None]),
        )

        for case, field, value in malformed:
            candidate = {**descriptor, field: value}
            with self.subTest(case=case), self.assertRaisesRegex(
                production.MaterialError,
                "material_descriptor_invalid",
            ):
                production._material_descriptor_payload(
                    {"descriptors": [candidate]},
                    expected_aliases=("upload_01",),
                )

        provider_result = ProviderResult(
            provider="dashscope",
            capability="material_analysis",
            request_id="descriptor-root-list",
            payload={"content": json.dumps([{}])},
            usage={},
            elapsed_ms=1,
        )
        with self.assertRaisesRegex(
            production.MaterialError,
            "material_descriptor_invalid",
        ):
            production._material_descriptor_payload(
                provider_result,
                expected_aliases=("upload_01",),
            )


if __name__ == "__main__":
    unittest.main()
