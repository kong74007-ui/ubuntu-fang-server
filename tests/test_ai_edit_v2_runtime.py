import unittest
from unittest.mock import patch

from server.content_domains import ai_edit_v2_runtime as runtime


class RuntimeTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
