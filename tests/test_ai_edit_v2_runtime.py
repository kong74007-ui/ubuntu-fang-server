import unittest
import copy
import tempfile
import threading
from unittest.mock import patch

from server.content_domains import ai_edit_v2_runtime as runtime


class RuntimeTests(unittest.TestCase):
    def test_each_stage_schema_rejects_missing_field_and_wrong_type(self):
        artifact = {"cos_key": "k", "etag": "e", "size_bytes": 1}
        valid = {
            "normalizing": {"normalized_media": {"cos_key": "k", "media_type": "video", "metadata": {"duration_ms": 1}}, "artifact": artifact},
            "transcribing": {"asr_result": {"provider_task_id": "p", "duration_ms": 1, "words": [{}], "sentences": [{}]}},
            "aligning": {"text_timeline": {"text": "x", "words": [{}], "sentences": [{}], "alignment_status": "aligned", "duration_ms": 1}},
            "directing": {"edit_plan": {"version": "2.0", "duration_ms": 1, "scenes": [{}]}},
            "resolving_materials": {"resolved_plan": {"materials": {}, "scenes": [{}], "duration_ms": 1}},
            "generating_media": {"resolved_plan": {}, "audio_plan": {}, "generated_audio": {}},
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
