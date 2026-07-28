import json
import os
import uuid
import copy
import threading
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from unittest.mock import patch
from pathlib import Path

from server.content_domains import ai_edit_v2_billing as billing
from server.content_domains import ai_edit_v2_asr as asr
from server.content_domains import ai_edit_v2_pipeline as pipeline
from server.content_domains import ai_edit_v2_runtime as runtime
from server.content_domains import ai_edit_v2_store as store
from server.content_domains.ai_edit_v2_providers.base import UnknownSubmissionError
from tests.test_ai_edit_v2_director import VALID_PLAN
from tests.test_ai_edit_v2_openai_image import png_bytes


def draft():
    return {
        "creation_mode": "natural_brief",
        "brief": "测试可恢复的 AI 剪辑流水线",
        "language": "zh-CN",
        "aspect_ratio": "16:9",
        "target_duration_ms": 60_000,
        "main_input": {
            "asset_id": "main",
            "kind": "video",
            "size_bytes": 10_000,
            "duration_ms": 90_000,
        },
        "required_materials": [],
        "reference_materials": [],
    }


class FakePoints:
    def __init__(self):
        self.balance = 1_000
        self.transactions = {}

    def deduct_points(self, username, amount, reason="", transaction_key=None):
        if transaction_key not in self.transactions:
            self.balance -= amount
            self.transactions[transaction_key] = self.balance
        return self.transactions[transaction_key]

    def refund_points(self, username, amount, reason="", transaction_key=None):
        if transaction_key not in self.transactions:
            self.balance += amount
            self.transactions[transaction_key] = self.balance
        return self.transactions[transaction_key]


class LoseFirstRefundResponse(FakePoints):
    def __init__(self):
        super().__init__()
        self.lost = False

    def refund_points(self, username, amount, reason="", transaction_key=None):
        result = super().refund_points(username, amount, reason, transaction_key)
        if not self.lost:
            self.lost = True
            raise RuntimeError("refund response lost")
        return result

class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "v2.db")
        self.pricing_path = os.path.join(self.temp_dir.name, "pricing.db")
        self.env = patch.dict(
            os.environ,
            {
                "AI_EDIT_V2_DB": self.db_path,
                "AI_EDIT_V2_PRICING_DB": self.pricing_path,
                "AI_EDIT_V2_NORMAL_BUDGET_SECONDS": "2700",
                "AI_EDIT_V2_REPAIR_BUDGET_SECONDS": "900",
            },
        )
        self.env.start()
        store.init_db(self.db_path)
        self.points = FakePoints()

    def tearDown(self):
        self.env.stop()
        self.temp_dir.cleanup()

    def _precharged_job(self, job_id="job-1"):
        quote = billing.create_quote(
            "alice", draft(), 100, uuid_factory=lambda: f"quote-{job_id}",
            db_path=self.db_path, pricing_db_path=self.pricing_path,
        )
        return billing.precharge_and_create_job(
            "alice", {"draft": draft()}, quote["id"], f"request-{job_id}", 101,
            points_client=self.points, uuid_factory=lambda: job_id,
            db_path=self.db_path,
        )["job"]

    def _set_state(self, job_id, status, checkpoints):
        with closing(store.open_store(self.db_path)) as conn:
            conn.execute(
                "UPDATE edit_v2_jobs SET status=?,checkpoint_json=? WHERE id=?",
                (status, json.dumps(checkpoints), job_id),
            )

    def test_precharged_job_is_queued_and_processing_clock_starts_at_normalizing(self):
        job = self._precharged_job()
        self.assertEqual(job["status"], "queued")
        queued = pipeline.timing_status(job["id"], 131, db_path=self.db_path)
        self.assertEqual(queued["queue_seconds"], 30)
        self.assertEqual(queued["processing_seconds"], 0)

        result = pipeline.run_stage(job["id"], "queued", now=141, db_path=self.db_path)
        self.assertEqual(result.next_state, "normalizing")
        active = pipeline.timing_status(job["id"], 151, db_path=self.db_path)
        self.assertEqual(active["queue_seconds"], 40)
        self.assertEqual(active["processing_seconds"], 10)
        self.assertEqual(active["repair_seconds"], 0)

    def test_normal_budget_without_render_fails_and_refunds_full_hold(self):
        job = self._precharged_job()
        self._set_state(
            job["id"], "directing",
            [{"version": 1, "state": "normalizing", "at": 100, "data": {}}],
        )
        held_balance = self.points.balance
        result = pipeline.run_stage(
            job["id"], "directing", now=2801, db_path=self.db_path,
            points_client=self.points,
        )
        self.assertEqual(result.next_state, "director_failed")
        self.assertEqual(result.error_code, "normal_budget_exceeded")
        self.assertGreater(self.points.balance, held_balance)
        self.assertEqual(self.points.balance, 1_000)

    def test_repair_requires_render_and_explicit_qc_issue_then_has_fifteen_minutes(self):
        job = self._precharged_job()
        checkpoints = [
            {"version": 1, "state": "normalizing", "at": 100, "data": {}},
            {
                "version": 2,
                "state": "quality_check",
                "at": 2750,
                "data": {"qc": {"passed": False, "repairable": True, "issues": ["audio_peak"]}},
            },
        ]
        self._set_state(job["id"], "quality_check", checkpoints)
        with closing(store.open_store(self.db_path)) as conn:
            conn.execute(
                """INSERT INTO edit_v2_render_artifacts(
                       job_id,kind,version,cos_key,created_at
                   ) VALUES(?,?,?,?,?)""",
                (job["id"], "assembled", 1, "v2/alice/job-1/assembled.mp4", 2740),
            )

        repair = pipeline.run_stage(
            job["id"], "quality_check", now=2801, db_path=self.db_path,
            points_client=self.points,
        )
        self.assertEqual(repair.next_state, "repairing")
        timing = pipeline.timing_status(job["id"], 3000, db_path=self.db_path)
        self.assertEqual(timing["repair_seconds"], 199)
        self.assertEqual(timing["remaining_seconds"], 700)

        failed = pipeline.run_stage(
            job["id"], "repairing", now=3701, db_path=self.db_path,
            points_client=self.points,
        )
        self.assertEqual(failed.next_state, "quality_failed")
        self.assertEqual(failed.error_code, "repair_budget_exceeded")
        self.assertEqual(self.points.balance, 1_000)

    def test_recovery_passes_existing_provider_task_id_without_new_attempt(self):
        job = self._precharged_job()
        self._set_state(
            job["id"], "transcribing",
            [{"version": 1, "state": "normalizing", "at": 100, "data": {}}],
        )
        store.record_stage_attempt(
            job["id"], "transcribing", 1, "waiting", 120,
            provider_task_id="fun-asr-existing", db_path=self.db_path,
        )
        seen = []

        def resume(_job, context):
            seen.append(context["provider_task_id"])
            return pipeline.StageResult(
                next_state="aligning_transcript",
                checkpoint={"transcript": "ready"},
            )

        result = pipeline.run_stage(
            job["id"], "transcribing", handlers={"transcribing": resume},
            now=130, db_path=self.db_path, points_client=self.points,
        )
        self.assertEqual(result.next_state, "aligning_transcript")
        self.assertEqual(seen, ["fun-asr-existing"])
        with closing(store.open_store(self.db_path)) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM edit_v2_stage_attempts WHERE job_id=? AND stage='transcribing'",
                (job["id"],),
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_restart_recovery_exposes_saved_unknown_submission_intent_without_resubmitting(self):
        job = self._precharged_job()
        self._set_state(
            job["id"], "transcribing",
            [{"version": 1, "state": "normalizing", "at": 100, "data": {}}],
        )
        store.record_stage_attempt(
            job["id"], "transcribing", 1, "waiting", 120,
            input_summary={
                "submission_intent": {
                    "provider": "dashscope", "capability": "asr",
                    "reference": "job-1:transcribing:1", "status": "unknown",
                }
            },
            db_path=self.db_path,
        )
        submitted = []

        def resume(_job, context):
            self.assertEqual(context["submission_intent"], {
                "provider": "dashscope", "capability": "asr",
                "reference": "job-1:transcribing:1", "status": "unknown",
            })
            self.assertIsNone(context["provider_task_id"])
            return pipeline.StageResult("transcription_failed", {"submitted": submitted})

        result = pipeline.run_stage(
            job["id"], "transcribing", handlers={"transcribing": resume},
            now=130, db_path=self.db_path, points_client=self.points,
        )

        self.assertEqual(result.next_state, "transcription_failed")
        self.assertEqual(submitted, [])
        self.assertIsNone(result.error_code)
        self.assertEqual(result.checkpoint, {"submitted": []})

    def test_submission_intent_is_durable_before_a_transcription_submit(self):
        job = self._precharged_job()
        self._set_state(
            job["id"], "transcribing",
            [{"version": 1, "state": "normalizing", "at": 100, "data": {}}],
        )

        def submit(_job, context):
            context["save_submission_intent"]("dashscope", "asr", "job-1:transcribing:1")
            return pipeline.StageResult("aligning_transcript", {"transcript": "ready"})

        result = pipeline.run_stage(
            job["id"], "transcribing", handlers={"transcribing": submit},
            now=130, db_path=self.db_path, points_client=self.points,
        )

        self.assertEqual(result.next_state, "aligning_transcript")
        with closing(store.open_store(self.db_path)) as conn:
            saved = conn.execute(
                "SELECT input_summary_json FROM edit_v2_stage_attempts WHERE job_id=? AND stage='transcribing'",
                (job["id"],),
            ).fetchone()["input_summary_json"]
        self.assertEqual(json.loads(saved), {"submission_intent": {
            "provider": "dashscope", "capability": "asr",
            "reference": "job-1:transcribing:1", "status": "pending",
        }})

    def test_concurrent_unknown_submission_is_claimed_once_and_never_retried(self):
        job = self._precharged_job()
        self._set_state(
            job["id"], "transcribing",
            [{"version": 1, "state": "normalizing", "at": 100, "data": {}}],
        )
        store.record_stage_attempt(
            job["id"], "transcribing", 1, "waiting", 120, db_path=self.db_path,
        )
        barrier = threading.Barrier(2)
        latest_barrier = threading.Barrier(2)
        submitted = []
        claims = []
        submitted_lock = threading.Lock()

        class UnknownAfterSendClient:
            def submit_asr(self, cos_url, reference):
                with submitted_lock:
                    submitted.append((cos_url, reference))
                raise UnknownSubmissionError("connection lost after provider accepted")

        def transcribe(_job, context):
            def claim(reference):
                may_submit = context["save_submission_intent"]("dashscope", "asr", reference)
                with submitted_lock:
                    claims.append(may_submit)
                barrier.wait(timeout=5)
                return may_submit

            try:
                asr.transcribe(
                    "https://media.example.invalid/source.mp4", UnknownAfterSendClient(), deadline_at=500,
                    reference="job-1:transcribing:1", provider_task_id=context["provider_task_id"],
                    submission_intent=context["submission_intent"],
                    save_submission_intent=claim,
                    mark_submission_unknown=context["mark_submission_unknown"],
                    now_fn=lambda: 130, sleep_fn=lambda _: None,
                )
            except UnknownSubmissionError:
                return pipeline.StageResult("transcription_failed", {"unknown": True})
            raise AssertionError("unknown provider submission must not succeed")

        def runner():
            try:
                result = pipeline.run_stage(
                    job["id"], "transcribing", handlers={"transcribing": transcribe},
                    now=130, db_path=self.db_path, points_client=self.points,
                )
                return ("result", result)
            except Exception as exc:
                return ("error", type(exc).__name__, str(exc))

        original_latest_attempt = pipeline._latest_attempt

        def synchronized_latest_attempt(*args, **kwargs):
            attempt = original_latest_attempt(*args, **kwargs)
            latest_barrier.wait(timeout=5)
            return attempt

        with patch.object(pipeline, "_latest_attempt", side_effect=synchronized_latest_attempt):
            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = [future.result(timeout=10) for future in (executor.submit(runner), executor.submit(runner))]

        self.assertEqual(len(claims), 2, outcomes)
        self.assertEqual(sorted(claims), [False, True])
        self.assertEqual(submitted, [
            ("https://media.example.invalid/source.mp4", "job-1:transcribing:1")
        ])
        with closing(store.open_store(self.db_path)) as conn:
            intent = json.loads(conn.execute(
                "SELECT input_summary_json FROM edit_v2_stage_attempts WHERE job_id=? AND stage='transcribing'",
                (job["id"],),
            ).fetchone()["input_summary_json"])["submission_intent"]
        self.assertEqual(intent, {
            "provider": "dashscope", "capability": "asr",
            "reference": "job-1:transcribing:1", "status": "unknown",
        })

    def test_provider_bound_intent_is_not_reclaimed_by_a_restart(self):
        job = self._precharged_job()
        self._set_state(
            job["id"], "transcribing",
            [{"version": 1, "state": "normalizing", "at": 100, "data": {}}],
        )
        store.record_stage_attempt(
            job["id"], "transcribing", 1, "waiting", 120,
            provider_task_id="fun-asr-1",
            input_summary={"submission_intent": {
                "provider": "dashscope", "capability": "asr",
                "reference": "job-1:transcribing:1", "status": "provider_bound",
            }},
            db_path=self.db_path,
        )

        def resume(_job, context):
            self.assertIs(
                context["save_submission_intent"]("dashscope", "asr", "job-1:transcribing:1"),
                False,
            )
            return pipeline.StageResult("transcription_failed", {"recovered": True})

        result = pipeline.run_stage(
            job["id"], "transcribing", handlers={"transcribing": resume},
            now=130, db_path=self.db_path, points_client=self.points,
        )
        self.assertIsNone(result.error_code)

    def test_worker_and_service_are_isolated_disabled_by_default_and_graceful(self):
        from server import ai_edit_v2_worker as worker

        with patch.dict(os.environ, {}, clear=True):
            config = worker.worker_config()
        self.assertFalse(config["enabled"])
        self.assertEqual(config["workers"], 5)
        self.assertEqual(config["normal_timeout_seconds"], 2700)
        self.assertEqual(config["repair_timeout_seconds"], 900)

        root = Path(__file__).resolve().parents[1]
        service = (root / "deploy/systemd/huangque-ai-edit-v2.service").read_text(encoding="utf-8")
        env_example = (root / "deploy/huangque-secrets.env.example").read_text(encoding="utf-8")
        self.assertIn("ExecStart=/usr/bin/python3 /home/ubuntu/content-api/ai_edit_v2_worker.py", service)
        self.assertIn("EnvironmentFile=-/home/ubuntu/auth-service/auth.env", service)
        self.assertIn("EnvironmentFile=/etc/huangque/ai-edit-v2.env", service)
        self.assertIn("TimeoutStopSec=120", service)
        drop_in = (
            root / "deploy/systemd/huangque-content.service.d/ai-edit-v2.conf"
        ).read_text(encoding="utf-8")
        self.assertEqual(drop_in.strip(), "[Service]\nEnvironmentFile=/etc/huangque/ai-edit-v2.env")
        self.assertIn("AI_EDIT_V2_ENABLED=0", env_example)
        self.assertIn("AI_EDIT_V2_WORKERS=5", env_example)
        self.assertIn("AI_EDIT_V2_NORMAL_TIMEOUT_SECONDS=2700", env_example)
        self.assertIn("AI_EDIT_V2_REPAIR_TIMEOUT_SECONDS=900", env_example)

    def test_disabled_worker_reconciles_billing_without_claiming_jobs(self):
        from server import ai_edit_v2_worker as worker

        config = {
            "enabled": False,
            "workers": 1,
            "lease_seconds": 30,
            "poll_seconds": 0.1,
            "normal_timeout_seconds": 2700,
            "repair_timeout_seconds": 900,
            "db_path": self.db_path,
        }
        stop_event = threading.Event()

        def finish_after_reconciliation(*_args, **_kwargs):
            stop_event.set()

        with patch.object(
             worker.billing, "reconcile_pending_precharges",
             side_effect=finish_after_reconciliation,
        ) as pending, \
             patch.object(worker.pipeline, "reconcile_terminal_refunds") as refunds, \
             patch.object(worker.store, "claim_next_job") as claim:
            worker.run_worker(stop_event, config=config)

        pending.assert_called_once()
        refunds.assert_called_once()
        claim.assert_not_called()

    def test_terminal_failure_refund_is_reconciled_after_lost_response(self):
        lost_points = LoseFirstRefundResponse()
        self.points = lost_points
        job = self._precharged_job()
        self._set_state(
            job["id"], "directing",
            [{"version": 1, "state": "normalizing", "at": 100, "data": {}}],
        )
        with self.assertRaisesRegex(RuntimeError, "refund response lost"):
            pipeline.run_stage(
                job["id"], "directing", now=2801, db_path=self.db_path,
                points_client=lost_points,
            )
        recovered = pipeline.reconcile_terminal_refunds(
            now=2802, db_path=self.db_path, points_client=lost_points
        )
        self.assertEqual(recovered, 1)
        self.assertEqual(lost_points.balance, 1_000)
        with closing(store.open_store(self.db_path)) as conn:
            status = conn.execute(
                "SELECT status FROM edit_v2_billing WHERE job_id=?", (job["id"],)
            ).fetchone()[0]
        self.assertEqual(status, "refunded")

    def test_phase_a_fake_provider_reaches_directing_with_alignment_hold_and_checkpoint(self):
        job = self._precharged_job()
        submitted = []

        def normalized(_job, _context):
            return pipeline.StageResult(
                "transcribing", {"normalized_cos_key": "private/normalized.mp4"}
            )

        def transcribed(_job, context):
            self.assertIsNone(context["provider_task_id"])
            context["save_provider_task_id"]("fake-asr-task-1")
            submitted.append("fake-asr-task-1")
            return pipeline.StageResult(
                "aligning_transcript",
                {"words": [{"text": "黄", "start_ms": 0, "end_ms": 100}]},
                provider_task_id="fake-asr-task-1",
            )

        def aligned(_job, _context):
            return pipeline.StageResult(
                "directing",
                {
                    "aligned_transcript": {
                        "text": "黄雀传媒",
                        "coverage": 1.0,
                        "monotonic": True,
                    }
                },
            )

        handlers = {
            "normalizing": normalized,
            "transcribing": transcribed,
            "aligning_transcript": aligned,
        }
        pipeline.run_stage(job["id"], "queued", now=102, db_path=self.db_path)
        pipeline.run_stage(
            job["id"], "normalizing", handlers=handlers, now=103,
            db_path=self.db_path, points_client=self.points,
        )
        pipeline.run_stage(
            job["id"], "transcribing", handlers=handlers, now=104,
            db_path=self.db_path, points_client=self.points,
        )
        pipeline.run_stage(
            job["id"], "aligning_transcript", handlers=handlers, now=105,
            db_path=self.db_path, points_client=self.points,
        )

        with closing(store.open_store(self.db_path)) as conn:
            final = conn.execute(
                "SELECT status,checkpoint_json FROM edit_v2_jobs WHERE id=?", (job["id"],)
            ).fetchone()
            bill = conn.execute(
                "SELECT status FROM edit_v2_billing WHERE job_id=?", (job["id"],)
            ).fetchone()
            attempt = conn.execute(
                "SELECT provider_task_id FROM edit_v2_stage_attempts WHERE job_id=? AND stage='transcribing'",
                (job["id"],),
            ).fetchone()
        checkpoints = json.loads(final["checkpoint_json"])
        self.assertEqual(final["status"], "directing")
        self.assertEqual(bill["status"], "held")
        self.assertEqual(attempt["provider_task_id"], "fake-asr-task-1")
        self.assertEqual(submitted, ["fake-asr-task-1"])
        self.assertTrue(checkpoints[-1]["data"]["aligned_transcript"]["monotonic"])


class StableRunJobTests(PipelineTests):
    def test_real_worker_and_concrete_production_services_reach_quality_check(self):
        from server import ai_edit_v2_worker as worker

        job_id = str(uuid.uuid4())
        job = self._precharged_job(job_id)
        payload = json.loads(job["payload_json"])
        payload["draft"].update({"target_duration_ms": 3000, "style_text": "clean editorial"})
        payload["draft"]["main_input"].update({"cos_key": "ai-edit-v2/source/input.mp4", "duration_ms": 3000})
        with closing(store.open_store(self.db_path)) as conn:
            conn.execute("UPDATE edit_v2_jobs SET payload_json=? WHERE id=?", (json.dumps(payload), job_id))

        class FakeCos:
            def __init__(self): self.objects = {"ai-edit-v2/source/input.mp4": b"source"}; self.types = {"ai-edit-v2/source/input.mp4": "video/mp4"}
            def enabled(self): return True
            def download_file(self, key, path): Path(path).write_bytes(self.objects[key])
            def put_bytes(self, content, key, content_type, private=True): self.objects[key] = content; self.types[key] = content_type
            def put_file(self, path, key, content_type, private=True): self.objects[key] = Path(path).read_bytes(); self.types[key] = content_type
            def head_object(self, key): return {"content_length": len(self.objects[key]), "etag": "etag-" + str(len(self.objects[key])), "content_type": self.types[key]}
            def presign_get(self, key, expires=300): return "https://cos.test/" + key

        transcript = json.loads((Path(__file__).parent / "fixtures/ai_edit_v2/provider_responses/fun_asr_success.json").read_text(encoding="utf-8"))
        transcript["properties"]["original_duration_in_milliseconds"] = 3000
        plan = json.loads(json.loads((Path(__file__).parent / "fixtures/ai_edit_v2/provider_responses/qwen_edit_plan_success.json").read_text(encoding="utf-8"))["output"]["choices"][0]["message"]["content"])
        plan["scenes"][0]["material_slots"] = ["slot_1"]
        plan["duration_ms"] = plan["target_duration_ms"] = 3000
        plan["scenes"][0]["end_ms"] = 3000
        plan["audio_plan"].update({"music_policy": "duck_under_speech", "sfx_policy": "semantic_only"})
        calls = {"openai": 0, "elevenlabs": 0}
        def openai_http(*_args): calls["openai"] += 1; return {"id": "img-1", "data": [{"url": "https://image.test/x.png"}], "usage": {"total_tokens": 1}}
        def openai_download(*_args): return {"content": png_bytes(), "content_type": "image/png"}
        def eleven_http(*_args): calls["elevenlabs"] += 1; return {"content": b"ID3-audio", "content_type": "audio/mpeg", "headers": {"request-id": "audio-1", "character-cost": "1", "song-id": "song-1"}}

        def dashscope_http(method, url, _headers, body, _timeout):
            if "text-generation" in url:
                return {"request_id": "qwen-1", "output": {"choices": [{"message": {"role": "assistant", "content": json.dumps(plan, ensure_ascii=False)}}]}, "usage": {"input_tokens": 1, "output_tokens": 1}}
            if method == "POST":
                return {"request_id": "submit-1", "output": {"task_id": "asr-1", "task_status": "PENDING"}}
            if url.endswith("/tasks/asr-1"):
                return {"request_id": "query-1", "output": {"task_id": "asr-1", "task_status": "SUCCEEDED", "results": [{"subtask_status": "SUCCEEDED", "transcription_url": "https://result.test/asr.json"}]}}
            return transcript

        def shotstack_http(method, _url, _headers, _body, _timeout):
            if method == "POST":
                return {"success": True, "response": {"id": "render-1", "status": "queued"}}
            return {"success": True, "response": {"id": "render-1", "status": "done", "url": "https://render.test/final.mp4"}}

        probe = {"format": {"duration": "3.0", "format_name": "mov,mp4"}, "streams": [{"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080, "r_frame_rate": "30/1"}, {"codec_type": "audio", "codec_name": "aac", "sample_rate": "48000", "channels": 2}]}
        def runner(command, **_kw):
            if command[0] == "ffprobe": return type("Completed", (), {"returncode": 0, "stdout": json.dumps(probe), "stderr": b""})()
            if command[-1] != "NUL": Path(command[-1]).write_bytes(b"mastered-audio")
            loudness = b'{"input_i":"-20","input_tp":"-2","input_lra":"5","input_thresh":"-30","target_offset":"0"}'
            return type("Completed", (), {"returncode": 0, "stdout": b"", "stderr": loudness})()
        fake_cos = FakeCos()
        services = runtime.ProductionServices(self.db_path, cos_api=fake_cos, runner=runner,
                                              dashscope_http=dashscope_http, shotstack_http=shotstack_http,
                                              openai_http=openai_http, openai_downloader=openai_download,
                                              elevenlabs_http=eleven_http,
                                              downloader=lambda _url: b"rendered-mp4")
        dependencies = runtime.production_dependencies(self.db_path, services=services)
        dependencies["points_client"] = self.points
        current_time = int(time.time())
        dependencies["now"] = lambda: current_time
        config = {"db_path": self.db_path, "lease_seconds": 180}
        claimed = store.claim_job(job_id, "integration-lease", 180, current_time, db_path=self.db_path)
        with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "fake", "SHOTSTACK_API_KEY": "fake", "OPENAI_API_KEY": "fake", "AI_EDIT_V2_OPENAI_IMAGE_IDEMPOTENCY_ACCEPTED": "1", "ELEVENLABS_API_KEY": "fake", "AI_EDIT_V2_SHOTSTACK_CALLBACK_URL": "https://callback.test/v2", "AI_EDIT_V2_WEBHOOK_SECRET": "fake-secret"}, clear=False):
            runtime.assert_production_ready(dependencies)
            result = worker._process_claimed(claimed, "integration-lease", config, dependencies)
        self.assertEqual(result["state"], "quality_checking", result)
        self.assertIn(b"rendered-mp4", fake_cos.objects.values())
        self.assertEqual(calls, {"openai": 1, "elevenlabs": 1})

    def _dependencies(self, calls, *, render_started=None, render_release=None):
        def handler(name):
            def run(_job, context, stage_input):
                calls.append(name)
                if name == "resolving_materials":
                    context["save_provider_task_id"]("openai-image-1")
                elif name == "generating_media":
                    context["save_provider_task_id"]("elevenlabs-1")
                elif name == "rendering":
                    context["save_provider_task_id"]("shotstack-1")
                    if render_started is not None:
                        render_started.set()
                    if render_release is not None:
                        render_release.wait(5)
                artifact = {
                        "cos_key": f"private/{name}.json",
                        "etag": name,
                        "size_bytes": 10,
                }
                item = {"text": "ok", "start_ms": 0, "end_ms": 1000}
                timeline = {"text": "ok", "words": [item], "sentences": [item],
                            "alignment_status": "aligned", "duration_ms": 1000}
                outputs = {
                    "normalizing": {"normalized_media": {"cos_key": "private/source.mp4", "media_type": "video", "metadata": {"duration_ms": 1000}}, "artifact": artifact},
                    "transcribing": {"asr_result": {"provider_task_id": "asr-1", "duration_ms": 1000, "words": [item], "sentences": [item]}},
                    "aligning": {"text_timeline": timeline},
                    "directing": {"edit_plan": copy.deepcopy(VALID_PLAN)},
                    "resolving_materials": {"resolved_plan": {"duration_ms": 1000, "scenes": [{"start_ms": 0, "end_ms": 1000}], "materials": {}}},
                    "generating_media": {"resolved_plan": {}, "audio_plan": {"bgm": None, "sfx": [], "degradations": []}, "generated_audio": {"bgm": None, "sfx": [], "degradations": []}},
                    "rendering": {"provider_task_id": "shotstack-1", "provider_status": "succeeded", "render_url": "https://render.test/final.mp4"},
                    "postprocessing": {"artifact": artifact, "output_available": True},
                }
                return outputs[name]
            return run

        return {
            "handlers": {name: handler(name) for name in runtime.STABLE_STAGE_SEQUENCE},
            "verify_artifact": lambda _stage, _artifact: True,
            "worker_id": "stable-worker",
            "lease_seconds": 30,
            "now": lambda: 200,
            "points_client": self.points,
        }

    def test_platform_job_reaches_quality_checking_once(self):
        job = self._precharged_job()
        calls = []

        result = pipeline.run_job(
            job["id"], self._dependencies(calls), db_path=self.db_path
        )

        self.assertEqual(result["state"], "quality_checking")
        self.assertEqual(calls, list(runtime.STABLE_STAGE_SEQUENCE))
        with closing(store.open_store(self.db_path)) as conn:
            self.assertEqual(
                conn.execute(
                    "SELECT status FROM edit_v2_jobs WHERE id=?", (job["id"],)
                ).fetchone()[0],
                "quality_check",
            )

    def test_duplicate_workers_submit_each_billable_provider_once(self):
        job = self._precharged_job()
        calls = []
        render_started = threading.Event()
        render_release = threading.Event()
        dependencies = self._dependencies(
            calls, render_started=render_started, render_release=render_release
        )

        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(
                pipeline.run_job, job["id"], dependencies, db_path=self.db_path
            )
            self.assertTrue(render_started.wait(5))
            second = executor.submit(
                pipeline.run_job, job["id"], dependencies, db_path=self.db_path
            )
            second.result(timeout=5)
            render_release.set()
            first.result(timeout=5)

        self.assertEqual(calls.count("resolving_materials"), 1)
        self.assertEqual(calls.count("generating_media"), 1)
        self.assertEqual(calls.count("rendering"), 1)

    def test_restart_reconciles_saved_provider_id_before_any_resubmit(self):
        job = self._precharged_job()
        calls = []
        dependencies = self._dependencies(calls)

        class Crash(BaseException):
            pass

        original_render = dependencies["handlers"]["rendering"]

        def crash_after_submit(current_job, context, stage_input):
            original_render(current_job, context, stage_input)
            raise Crash()

        dependencies["handlers"]["rendering"] = crash_after_submit
        with self.assertRaises(Crash):
            pipeline.run_job(job["id"], dependencies, db_path=self.db_path)

        reconciled = []

        def reconcile_render(_job, context, _stage_input):
            reconciled.append(context["provider_task_id"])
            return {
                "provider_task_id": context["provider_task_id"],
                "provider_status": "succeeded",
                "render_url": "https://render.test/final.mp4",
            }

        dependencies["reconcilers"] = {"rendering": reconcile_render}
        dependencies["handlers"]["rendering"] = lambda *_args: self.fail(
            "a durable provider id must be reconciled before submit"
        )
        dependencies["now"] = lambda: 231
        result = pipeline.run_job(job["id"], dependencies, db_path=self.db_path)

        self.assertEqual(result["state"], "quality_checking")
        self.assertEqual(reconciled, ["shotstack-1"])
        self.assertEqual(calls.count("resolving_materials"), 1)
        self.assertEqual(calls.count("generating_media"), 1)
        self.assertEqual(calls.count("rendering"), 1)

    def test_completed_checkpoint_reuse_requires_matching_fingerprint_and_artifact(self):
        job = self._precharged_job()
        calls = []
        dependencies = self._dependencies(calls)
        original_transition = store.transition_leased
        crashed = False

        def crash_before_normalizing_transition(*args, **kwargs):
            nonlocal crashed
            if not crashed and args[1] == "normalizing":
                crashed = True
                raise RuntimeError("crash after checkpoint persistence")
            return original_transition(*args, **kwargs)

        with patch.object(store, "transition_leased", side_effect=crash_before_normalizing_transition):
            with self.assertRaisesRegex(RuntimeError, "checkpoint persistence"):
                pipeline.run_job(job["id"], dependencies, db_path=self.db_path)
        self.assertEqual(calls.count("normalizing"), 1)

        dependencies["now"] = lambda: 231
        pipeline.run_job(job["id"], dependencies, db_path=self.db_path)
        self.assertEqual(calls.count("normalizing"), 1)

        second = self._precharged_job("job-2")
        dependencies = self._dependencies(calls)
        crashed = False
        with patch.object(store, "transition_leased", side_effect=crash_before_normalizing_transition):
            with self.assertRaisesRegex(RuntimeError, "checkpoint persistence"):
                pipeline.run_job(second["id"], dependencies, db_path=self.db_path)
        dependencies["verify_artifact"] = lambda _stage, _artifact: False
        dependencies["now"] = lambda: 231
        pipeline.run_job(second["id"], dependencies, db_path=self.db_path)
        self.assertEqual(calls.count("normalizing"), 3)

    def test_provider_stage_stops_after_two_retries(self):
        job = self._precharged_job()
        calls = []
        dependencies = self._dependencies(calls)
        attempts = []

        def unavailable(*_args):
            attempts.append(1)
            raise pipeline.RetryableStageError("provider_unavailable")

        dependencies["handlers"]["transcribing"] = unavailable
        result = pipeline.run_job(job["id"], dependencies, db_path=self.db_path)

        self.assertEqual(len(attempts), 3)
        self.assertEqual(result["state"], "transcription_failed")

    def test_provider_retry_budget_survives_process_restart(self):
        job = self._precharged_job()
        calls = []
        dependencies = self._dependencies(calls)
        attempts = []

        class Crash(BaseException):
            pass

        def exhaust_then_crash(*_args):
            attempts.append(1)
            if len(attempts) < 3:
                raise pipeline.RetryableStageError("provider_unavailable")
            raise Crash()

        dependencies["handlers"]["transcribing"] = exhaust_then_crash
        with self.assertRaises(Crash):
            pipeline.run_job(job["id"], dependencies, db_path=self.db_path)
        dependencies["now"] = lambda: 231
        dependencies["handlers"]["transcribing"] = lambda *_args: attempts.append(1) or {}

        result = pipeline.run_job(job["id"], dependencies, db_path=self.db_path)

        self.assertEqual(result["state"], "transcription_failed")
        self.assertEqual(result["error_code"], "provider_retry_exhausted")
        self.assertEqual(len(attempts), 3)

    def test_long_provider_polling_renews_the_job_lease(self):
        job = self._precharged_job()
        calls = []
        dependencies = self._dependencies(calls)
        dependencies["now"] = lambda: int(time.time())
        dependencies["lease_seconds"] = 3
        entered = threading.Event()
        release = threading.Event()
        original = dependencies["handlers"]["normalizing"]

        def long_poll(current_job, context, stage_input):
            entered.set()
            release.wait(3)
            return original(current_job, context, stage_input)

        dependencies["handlers"]["normalizing"] = long_poll
        with patch.object(store, "renew_lease", wraps=store.renew_lease) as renew:
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    pipeline.run_job, job["id"], dependencies, db_path=self.db_path
                )
                self.assertTrue(entered.wait(2))
                threading.Event().wait(1.2)
                release.set()
                future.result(timeout=5)
            self.assertGreaterEqual(renew.call_count, 1)

    def test_required_artifact_metadata_and_nested_verification_fail_closed(self):
        job = self._precharged_job()
        calls = []
        dependencies = self._dependencies(calls)
        dependencies["handlers"]["normalizing"] = lambda *_args: {
            "normalized_media": {"cos_key": "private/source.mp4", "media_type": "video", "metadata": {"duration_ms": 1000}},
            "nested": {
                "artifacts": [
                    {"cos_key": "private/source.mp4", "etag": "bad", "size_bytes": 10}
                ]
            }
        }
        dependencies["verify_artifact"] = (
            lambda _stage, artifact: artifact["etag"] == "good"
        )

        result = pipeline.run_job(job["id"], dependencies, db_path=self.db_path)

        self.assertEqual(result["state"], "validation_failed")
        self.assertEqual(result["error_code"], "stage_artifact_invalid")

        other = self._precharged_job("job-missing-artifact")
        dependencies = self._dependencies([])
        dependencies["handlers"]["normalizing"] = lambda *_args: {"normalized_media": {"cos_key": "private/source.mp4", "media_type": "video", "metadata": {"duration_ms": 1000}}}
        result = pipeline.run_job(other["id"], dependencies, db_path=self.db_path)
        self.assertEqual(result["error_code"], "stage_artifact_missing")

    def test_late_handler_result_never_completes_checkpoint_or_advances(self):
        job = self._precharged_job()
        calls = []
        clock = [100]
        dependencies = self._dependencies(calls)
        dependencies["now"] = lambda: clock[0]
        dependencies["lease_seconds"] = 5_000

        def late(*_args):
            clock[0] = 2_901
            return {
                "artifact": {
                    "cos_key": "private/late.mp4", "etag": "late", "size_bytes": 10
                }
            }

        dependencies["handlers"]["normalizing"] = late
        result = pipeline.run_job(job["id"], dependencies, db_path=self.db_path)

        self.assertEqual(result["state"], "validation_failed")
        self.assertEqual(result["error_code"], "normal_budget_exceeded")
        with closing(store.open_store(self.db_path)) as conn:
            checkpoint = conn.execute(
                "SELECT status FROM edit_v2_pipeline_checkpoints WHERE job_id=? AND stage='normalizing'",
                (job["id"],),
            ).fetchone()
        self.assertEqual(checkpoint["status"], "running")

    def test_lost_heartbeat_fences_old_worker_before_checkpoint_write(self):
        job = self._precharged_job()
        calls = []
        dependencies = self._dependencies(calls)
        dependencies["lease_seconds"] = 1
        dependencies["now"] = lambda: int(time.time())
        entered = threading.Event()
        release = threading.Event()

        def blocked(_job, context, _stage_input):
            entered.set()
            release.wait(3)
            context["save_provider_task_id"]("late-provider-id")
            return {
                "artifact": {
                    "cos_key": "private/old.mp4", "etag": "old", "size_bytes": 10
                }
            }

        dependencies["handlers"]["normalizing"] = blocked
        real_renew = store.renew_lease
        renewals = []
        lease_lost = threading.Event()

        def lose_once(*args, **kwargs):
            renewals.append(1)
            if len(renewals) == 1:
                lease_lost.set()
                return False
            return real_renew(*args, **kwargs)

        with patch.object(store, "renew_lease", side_effect=lose_once):
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    pipeline.run_job, job["id"], dependencies, db_path=self.db_path
                )
                self.assertTrue(entered.wait(2))
                self.assertTrue(lease_lost.wait(2))
                release.set()
                with self.assertRaisesRegex(pipeline.PipelineError, "job_lease_lost"):
                    future.result(timeout=5)
        with closing(store.open_store(self.db_path)) as conn:
            row = conn.execute(
                "SELECT status,output_json,provider_task_id FROM edit_v2_pipeline_checkpoints WHERE job_id=? AND stage='normalizing'",
                (job["id"],),
            ).fetchone()
        self.assertEqual(row["status"], "running")
        self.assertIsNone(row["output_json"])
        self.assertIsNone(row["provider_task_id"])

    def test_terminal_failure_persists_error_code_and_success_clears_stale_code(self):
        job = self._precharged_job()
        dependencies = self._dependencies([])
        dependencies["handlers"]["normalizing"] = lambda *_args: (_ for _ in ()).throw(
            pipeline.PipelineError("normalize_failed")
        )
        pipeline.run_job(job["id"], dependencies, db_path=self.db_path)
        with closing(store.open_store(self.db_path)) as conn:
            failed = conn.execute(
                "SELECT error_code FROM edit_v2_jobs WHERE id=?", (job["id"],)
            ).fetchone()[0]
        self.assertEqual(failed, "normalize_failed")

        successful = self._precharged_job("job-success-error-clear")
        with closing(store.open_store(self.db_path)) as conn:
            conn.execute(
                "UPDATE edit_v2_jobs SET error_code='stale' WHERE id=?", (successful["id"],)
            )
        pipeline.run_job(successful["id"], self._dependencies([]), db_path=self.db_path)
        with closing(store.open_store(self.db_path)) as conn:
            row = conn.execute(
                "SELECT status,error_code FROM edit_v2_jobs WHERE id=?", (successful["id"],)
            ).fetchone()
        self.assertEqual(row["status"], "quality_check")
        self.assertIsNone(row["error_code"])


if __name__ == "__main__":
    unittest.main()
