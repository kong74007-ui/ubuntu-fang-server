import unittest
import copy
import os
import tempfile
import threading
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from server.content_domains import ai_edit_v2_runtime as runtime
from server.content_domains import ai_edit_v2_billing as billing
from server.content_domains import ai_edit_v2_store as store
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
    def test_audio_only_always_masters_original_voice_when_optional_audio_is_none_or_degraded(self):
        class Cos:
            def __init__(self): self.objects = {}
            def download_file(self, _key, path): Path(path).write_bytes(b"voice")
            def put_file(self, path, key, _content_type, private=True): self.objects[key] = Path(path).read_bytes()
            def head_object(self, key): return {"content_length": len(self.objects[key]), "etag": "master-etag"}

        plan = _resolved_plan()
        plan["primary_media"] = {"cos_key": "private/voice.m4a", "media_type": "audio",
                                 "metadata": {"duration_ms": 1800}}
        plan.pop("primary_video")
        for degradations in ([], ["music_generation_degraded", "sfx_generation_degraded"]):
            cos = Cos()
            service = runtime.ProductionServices("unused.db", cos_api=cos)
            generated = {"bgm": None, "sfx": [], "degradations": degradations}
            def fake_mix(_video, _voice, _bgm, _sfx, output, _runner):
                Path(output).write_bytes(b"master")
                return output
            with patch("server.content_domains.ai_edit_v2_audio.build_audio_plan",
                       return_value={"bgm": None, "sfx": [], "degradations": []}), \
                 patch("server.content_domains.ai_edit_v2_audio.generate_audio_assets",
                       return_value=copy.deepcopy(generated)), \
                 patch("server.content_domains.ai_edit_v2_audio.mix_audio", side_effect=fake_mix), \
                 patch("server.content_domains.ai_edit_v2_providers.elevenlabs.ElevenLabsProvider",
                       return_value=object()):
                output = service.generating_media(
                    {"id": "job-audio", "owner": "alice"}, {},
                    {"previous": {"resolved_plan": copy.deepcopy(plan)}},
                )
            self.assertEqual(output["resolved_plan"]["mastered_audio"]["source"], "mix_audio")
            self.assertEqual(output["generated_audio"]["degradations"], degradations)

    def test_provider_usage_survives_service_restart_and_replay_without_double_cost(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = os.path.join(directory, "v2.db")
            store.init_db(db_path)
            draft = {"creation_mode": "natural_brief", "brief": "x", "language": "zh-CN",
                     "aspect_ratio": "16:9", "target_duration_ms": 1000,
                     "input_mode": "external_video",
                     "main_input": {"asset_id": "m", "kind": "video", "size_bytes": 1,
                                    "duration_ms": 1000},
                     "required_materials": [], "reference_materials": []}
            quote = billing.create_quote("alice", draft, 1, db_path=db_path)
            job = store.create_job("alice", {"draft": draft}, quote["id"], "cost-once", 2,
                                   uuid_factory=lambda: "job-cost", db_path=db_path)
            with closing(store.open_store(db_path)) as conn:
                conn.execute("""INSERT INTO edit_v2_billing(
                    job_id,transaction_key,operation,amount,status,created_at,updated_at
                ) VALUES('job-cost','hold-cost','hold',50,'held',2,2)""")
            runtime.ProductionServices(db_path)._record_usage(
                "job-cost", "image:one", "openai", "image_generation", "request-one", None
            )
            restarted = runtime.ProductionServices(db_path)
            restarted._record_usage(
                "job-cost", "image:one", "openai", "image_generation", "replay-request", None
            )
            self.assertEqual(restarted.actual_cost(job, {}), 4)
            class Points:
                def __init__(self):
                    self.refunds = []
                def refund_points(self, owner, amount, reason="", transaction_key=None):
                    self.refunds.append((owner, amount, transaction_key))
                    return 500 + amount
            points = Points()
            settlement = billing.settle_success(
                job["id"], restarted.actual_cost(job, {}), 10,
                points_client=points, db_path=db_path,
            )
            self.assertEqual(settlement["refunded_points"], 46)
            self.assertEqual(points.refunds[0][1], 46)
            with closing(store.open_store(db_path)) as conn:
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM edit_v2_provider_usage WHERE job_id='job-cost'"
                ).fetchone()[0], 1)

    def test_repair_usage_is_recorded_once_across_unknown_restart_and_reconcile(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = os.path.join(directory, "v2.db")
            store.init_db(db_path)
            draft = {"creation_mode": "natural_brief", "brief": "x", "language": "zh-CN",
                     "aspect_ratio": "16:9", "target_duration_ms": 1000,
                     "input_mode": "external_video",
                     "main_input": {"asset_id": "m", "kind": "video", "size_bytes": 1,
                                    "duration_ms": 1000},
                     "required_materials": [], "reference_materials": []}
            quote = billing.create_quote("alice", draft, 1, db_path=db_path)
            job = store.create_job("alice", {"draft": draft}, quote["id"], "repair-cost", 2,
                                   uuid_factory=lambda: "job-repair-cost", db_path=db_path)
            with closing(store.open_store(db_path)) as conn:
                conn.execute("""INSERT INTO edit_v2_billing(
                    job_id,transaction_key,operation,amount,status,created_at,updated_at
                ) VALUES('job-repair-cost','hold-repair','hold',50,'held',2,2)""")

            context = {"idempotency_key": "ai-edit-v2:job-repair-cost:repair:1",
                       "attempt_id": 7, "provider_task_id": "repair-task-1"}
            confirmed = {"provider": "repairco", "provider_task_id": "repair-task-1",
                         "request_id": "repair-request-1", "cost_units": 3,
                         "output_path": "repaired.mp4"}
            runtime.ProductionServices(
                db_path, repair_handler=lambda *_args: confirmed,
                repair_reconciler=lambda *_args: confirmed,
            ).repair_layer(job, context)
            restarted = runtime.ProductionServices(
                db_path, repair_handler=lambda *_args: confirmed,
                repair_reconciler=lambda *_args: confirmed,
            )
            restarted.repair_reconciler(job, context)

            self.assertEqual(restarted.actual_cost(job, {}), 4)
            with closing(store.open_store(db_path)) as conn:
                rows = conn.execute(
                    "SELECT capability,request_id,effective_points FROM edit_v2_provider_usage WHERE job_id=?",
                    (job["id"],),
                ).fetchall()
            self.assertEqual([tuple(row) for row in rows], [("repair", "repair-request-1", 4)])
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
        for name in ("quality_runner", "quality_output_path", "actual_cost",
                     "repair_layer", "repair_reconciler"):
            self.assertTrue(callable(bundle[name]), name)
        self.assertNotIn("AI_EDIT_V2_REPAIR_PROVIDER", bundle["readiness_errors"]())
        self.assertIn("AI_EDIT_V2_QUALITY_FINAL_MEDIA_ANALYZER_CAPTIONS_OCR", bundle["readiness_errors"]())

    def test_production_bundle_builds_real_quality_and_repair_factories(self):
        class Cos:
            @staticmethod
            def enabled(): return True
            @staticmethod
            def presign_get(key): return f"https://cos.example/{key}"
            @staticmethod
            def download_file(_key, _path): return None

        transport = lambda *_args, **_kwargs: {}
        with patch.dict(os.environ, {}, clear=True):
            service = runtime.ProductionServices(
                "v2.db", cos_api=Cos(), dashscope_http=transport,
                shotstack_http=transport, quality_binary_finder=lambda name: name,
            )
            errors = service.readiness_errors()

        self.assertTrue(callable(service.repair_handler))
        self.assertTrue(callable(service.repair_reconciler_handler))
        self.assertEqual(service.quality_runner.analyzer.capabilities(), {
            "captions_ocr": True, "glyphs": True, "materials": True,
            "transcript_facts": True, "audio": True,
        })
        self.assertFalse(any(
            error == "AI_EDIT_V2_REPAIR_PROVIDER"
            or error.startswith("AI_EDIT_V2_QUALITY_FINAL_MEDIA_ANALYZER_")
            for error in errors
        ), errors)

    def test_enabled_worker_fails_readiness_before_claiming(self):
        from server import ai_edit_v2_worker as worker
        with tempfile.TemporaryDirectory() as directory:
            config = {"enabled": True, "workers": 1, "lease_seconds": 30,
                      "poll_seconds": .1, "db_path": directory + "/v2.db"}
            dependencies = {"readiness_errors": lambda: ["DASHSCOPE_API_KEY"]}
            with patch.object(worker.feature, "capability", return_value={
                     "stable_runtime_ready": True, "accepts_submissions": True,
                 }), patch.object(worker.runtime, "production_dependencies", return_value=dependencies), \
                 patch.object(worker.store, "claim_next_job") as claim:
                with self.assertRaisesRegex(RuntimeError, "ai_edit_v2_not_ready"):
                    worker.run_worker(threading.Event(), config=config)
            claim.assert_not_called()


if __name__ == "__main__":
    unittest.main()
