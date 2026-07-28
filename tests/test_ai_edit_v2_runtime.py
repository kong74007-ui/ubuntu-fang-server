import unittest
import copy
import tempfile
import threading
from unittest.mock import patch

from server.content_domains import ai_edit_v2_runtime as runtime
from tests.test_ai_edit_v2_director import VALID_PLAN


def _timeline(duration_ms=1800):
    item = {"text": "x", "start_ms": 0, "end_ms": duration_ms}
    return {
        "text": "x",
        "words": [copy.deepcopy(item)],
        "sentences": [copy.deepcopy(item)],
        "alignment_status": "aligned",
        "duration_ms": duration_ms,
    }


def _resolved_plan(duration_ms=1800):
    plan = copy.deepcopy(VALID_PLAN)
    plan["duration_ms"] = duration_ms
    plan["target_duration_ms"] = duration_ms
    plan["scenes"][0]["end_ms"] = duration_ms
    plan.update(
        {
            "materials": {},
            "material_resolution_status": "resolved",
            "text_timeline": _timeline(duration_ms),
            "primary_video": {
                "cos_key": "private/source.mp4",
                "media_type": "video",
                "metadata": {"duration_ms": duration_ms},
            },
        }
    )
    return plan


class RuntimeTests(unittest.TestCase):
    def test_each_stage_schema_rejects_missing_field_and_wrong_type(self):
        artifact = {"cos_key": "k", "etag": "e", "size_bytes": 1}
        item = {"text": "x", "start_ms": 0, "end_ms": 1}
        valid = {
            "normalizing": {"normalized_media": {"cos_key": "k", "media_type": "video", "metadata": {"duration_ms": 1}}, "artifact": artifact},
            "transcribing": {"asr_result": {"provider_task_id": "p", "duration_ms": 1, "words": [item], "sentences": [item]}},
            "aligning": {"text_timeline": {"text": "x", "words": [item], "sentences": [item], "alignment_status": "aligned", "duration_ms": 1}},
            "directing": {"edit_plan": copy.deepcopy(VALID_PLAN)},
            "resolving_materials": {"resolved_plan": _resolved_plan()},
            "generating_media": {"resolved_plan": _resolved_plan(), "audio_plan": {"bgm": None, "sfx": [], "degradations": []}, "generated_audio": {"bgm": None, "sfx": [], "degradations": []}},
            "rendering": {"provider_task_id": "p", "provider_status": "succeeded", "render_url": "https://x/y.mp4"},
            "postprocessing": {"artifact": artifact, "output_available": True},
        }
        required = {"normalizing": "normalized_media", "transcribing": "asr_result", "aligning": "text_timeline", "directing": "edit_plan", "resolving_materials": "resolved_plan", "generating_media": "audio_plan", "rendering": "render_url", "postprocessing": "output_available"}
        for stage, output in valid.items():
            with self.subTest(stage=stage):
                self.assertEqual(runtime.validate_stage_output(stage, output, lambda *_: True), (True, None))
                missing = copy.deepcopy(output); missing.pop(required[stage])
                self.assertEqual(runtime.validate_stage_output(stage, missing, lambda *_: True)[1], "stage_output_schema_invalid")
                wrong = copy.deepcopy(output); wrong[required[stage]] = 7
                self.assertEqual(runtime.validate_stage_output(stage, wrong, lambda *_: True)[1], "stage_output_schema_invalid")

        self.assertEqual(runtime.validate_stage_output("transcribing", {"garbage": 1}, None)[1], "stage_output_schema_invalid")
        self.assertEqual(runtime.validate_stage_output("normalizing", {"artifact": artifact}, lambda *_: True)[1], "stage_output_schema_invalid")

    def test_nested_semantics_and_every_artifact_boundary_fail_closed(self):
        base = {"asr_result": {"provider_task_id": "p", "duration_ms": 10,
                "words": [{"text": "x", "start_ms": 0, "end_ms": 10}],
                "sentences": [{"text": "x", "start_ms": 0, "end_ms": 10}]}}
        for malformed in (None, "key", [], {"cos_key": "k"}):
            output = copy.deepcopy(base); output["nested"] = {"artifact": malformed}
            self.assertEqual(runtime.validate_stage_output("transcribing", output, lambda *_: True)[1], "stage_artifact_metadata_invalid")
        output = copy.deepcopy(base); output["nested"] = {"artifacts": ["bad"]}
        self.assertEqual(runtime.validate_stage_output("transcribing", output, lambda *_: True)[1], "stage_artifact_metadata_invalid")
        garbage = copy.deepcopy(base); garbage["asr_result"]["words"] = [{}]
        self.assertEqual(runtime.validate_stage_output("transcribing", garbage, None)[1], "stage_output_schema_invalid")

    def test_checkpoint_output_rejects_nested_non_json_containers_and_hidden_artifacts(self):
        class CustomList(list):
            pass

        class CustomDict(dict):
            pass

        class CustomContainer:
            pass

        base = {"asr_result": {
            "provider_task_id": "p", "duration_ms": 10,
            "words": [{"text": "x", "start_ms": 0, "end_ms": 10}],
            "sentences": [{"text": "x", "start_ms": 0, "end_ms": 10}],
        }}
        malformed_values = (
            ({"artifact": "hidden-malformed"},),
            CustomList([1]),
            CustomDict({"safe": 1}),
            CustomContainer(),
        )
        for value in malformed_values:
            output = copy.deepcopy(base)
            output["nested"] = value
            with self.subTest(container=type(value).__name__):
                self.assertEqual(
                    runtime.validate_stage_output("transcribing", output, lambda *_: True)[1],
                    "stage_output_invalid",
                )

        malformed_artifact = copy.deepcopy(base)
        malformed_artifact["nested"] = [{"artifact": {"cos_key": "k"}}]
        self.assertEqual(
            runtime.validate_stage_output("transcribing", malformed_artifact, lambda *_: True)[1],
            "stage_artifact_metadata_invalid",
        )

    def test_timed_outputs_require_positive_monotonic_ranges_within_duration(self):
        transcribing = {"asr_result": {
            "provider_task_id": "asr-1", "duration_ms": 100,
            "words": [{"text": "x", "start_ms": 0, "end_ms": 100}],
            "sentences": [{"text": "x", "start_ms": 0, "end_ms": 100}],
        }}
        aligning = {"text_timeline": _timeline(100)}
        for stage, output, root in (
            ("transcribing", transcribing, "asr_result"),
            ("aligning", aligning, "text_timeline"),
        ):
            with self.subTest(stage=stage, defect="past_duration"):
                invalid = copy.deepcopy(output)
                invalid[root]["words"][0]["end_ms"] = 101
                self.assertEqual(
                    runtime.validate_stage_output(stage, invalid, None)[1],
                    "stage_output_schema_invalid",
                )
            with self.subTest(stage=stage, defect="empty_range"):
                invalid = copy.deepcopy(output)
                invalid[root]["words"][0].update({"start_ms": 50, "end_ms": 50})
                self.assertEqual(
                    runtime.validate_stage_output(stage, invalid, None)[1],
                    "stage_output_schema_invalid",
                )
            with self.subTest(stage=stage, defect="end_order"):
                invalid = copy.deepcopy(output)
                invalid[root]["words"] = [
                    {"text": "a", "start_ms": 0, "end_ms": 80},
                    {"text": "b", "start_ms": 20, "end_ms": 60},
                ]
                self.assertEqual(
                    runtime.validate_stage_output(stage, invalid, None)[1],
                    "stage_output_schema_invalid",
                )

    def test_resolved_plan_reuses_strict_scene_and_material_contracts(self):
        valid = {"resolved_plan": _resolved_plan()}
        self.assertEqual(
            runtime.validate_stage_output("resolving_materials", valid, None),
            (True, None),
        )

        reversed_scene = copy.deepcopy(valid)
        reversed_scene["resolved_plan"]["scenes"][0].update(
            {"start_ms": 1000, "end_ms": 500}
        )
        unresolved_slot = copy.deepcopy(valid)
        unresolved_slot["resolved_plan"]["scenes"][0]["material_slots"] = ["slot_1"]
        malformed_material = copy.deepcopy(unresolved_slot)
        malformed_material["resolved_plan"]["materials"] = {
            "slot_1": {"asset_id": "asset-1", "cos_key": "", "kind": "document"}
        }
        for defect, output in (
            ("reversed_scene", reversed_scene),
            ("unresolved_slot", unresolved_slot),
            ("malformed_material", malformed_material),
        ):
            with self.subTest(defect=defect):
                self.assertEqual(
                    runtime.validate_stage_output("resolving_materials", output, None)[1],
                    "stage_output_schema_invalid",
                )

    def test_generating_media_rejects_empty_or_malformed_recursive_audio(self):
        valid = {
            "resolved_plan": _resolved_plan(),
            "audio_plan": {"bgm": None, "sfx": [], "degradations": []},
            "generated_audio": {"bgm": None, "sfx": [], "degradations": []},
        }
        self.assertEqual(
            runtime.validate_stage_output("generating_media", valid, None),
            (True, None),
        )
        invalid_outputs = []
        empty_plan = copy.deepcopy(valid); empty_plan["resolved_plan"] = {}; invalid_outputs.append(empty_plan)
        empty_bgm = copy.deepcopy(valid); empty_bgm["audio_plan"]["bgm"] = {}; invalid_outputs.append(empty_bgm)
        empty_sfx = copy.deepcopy(valid); empty_sfx["audio_plan"]["sfx"] = [{}]; invalid_outputs.append(empty_sfx)
        malformed_degradation = copy.deepcopy(valid); malformed_degradation["generated_audio"]["degradations"] = [{}]; invalid_outputs.append(malformed_degradation)
        malformed_generated = copy.deepcopy(valid); malformed_generated["generated_audio"]["sfx"] = [{}]; invalid_outputs.append(malformed_generated)
        for index, output in enumerate(invalid_outputs):
            with self.subTest(index=index):
                self.assertEqual(
                    runtime.validate_stage_output("generating_media", output, None)[1],
                    "stage_output_schema_invalid",
                )

    def test_stable_sequence_stops_before_task_8_quality_implementation(self):
        self.assertEqual(
            runtime.STABLE_STAGE_SEQUENCE,
            (
                "normalizing",
                "transcribing",
                "aligning",
                "directing",
                "resolving_materials",
                "generating_media",
                "rendering",
                "postprocessing",
            ),
        )
        self.assertEqual(runtime.public_state("quality_check"), "quality_checking")

    def test_worker_processes_claim_with_run_job_and_production_dependencies(self):
        from server import ai_edit_v2_worker as worker

        job = {"id": "job-1", "status": "normalizing"}
        config = {"db_path": "v2.db", "lease_seconds": 180}
        dependencies = {"handlers": {}}
        with patch.object(worker.pipeline, "run_job", return_value={"state": "quality_checking"}) as run, \
             patch.object(worker.pipeline, "run_stage") as old:
            result = worker._process_claimed(job, "lease-token", config, dependencies)
        self.assertEqual(result["state"], "quality_checking")
        passed = run.call_args.args[1]
        self.assertEqual(passed["handlers"], dependencies["handlers"])
        self.assertEqual(passed["lease_owner"], "lease-token")
        self.assertEqual(passed["lease_seconds"], 180)
        old.assert_not_called()

    def test_production_bundle_exposes_real_adapter_backed_handlers_and_reconcilers(self):
        bundle = runtime.production_dependencies("v2.db")
        self.assertEqual(set(bundle["handlers"]), set(runtime.STABLE_STAGE_SEQUENCE))
        for stage in runtime.PROVIDER_STAGES:
            self.assertIn(stage, bundle["reconcilers"])
        self.assertTrue(bundle["production"])
        self.assertIsInstance(bundle["services"], runtime.ProductionServices)

    def test_enabled_worker_fails_readiness_before_claiming(self):
        from server import ai_edit_v2_worker as worker
        with tempfile.TemporaryDirectory() as directory:
            config = {"enabled": True, "workers": 1, "lease_seconds": 30,
                      "poll_seconds": .1, "db_path": directory + "/v2.db"}
            dependencies = {"readiness_errors": lambda: ["DASHSCOPE_API_KEY"]}
            with patch.object(worker.runtime, "production_dependencies", return_value=dependencies), \
                 patch.object(worker.store, "claim_next_job") as claim:
                with self.assertRaisesRegex(RuntimeError, "ai_edit_v2_not_ready"):
                    worker.run_worker(threading.Event(), config=config)
            claim.assert_not_called()


if __name__ == "__main__":
    unittest.main()
