import json
import os
import threading
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from unittest.mock import patch
from pathlib import Path

from server.content_domains import ai_edit_v2_billing as billing
from server.content_domains import ai_edit_v2_asr as asr
from server.content_domains import ai_edit_v2_pipeline as pipeline
from server.content_domains import ai_edit_v2_store as store
from server.content_domains.ai_edit_v2_providers.base import UnknownSubmissionError


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
            "alice", draft(), 100, uuid_factory=lambda: "quote-1",
            db_path=self.db_path, pricing_db_path=self.pricing_path,
        )
        return billing.precharge_and_create_job(
            "alice", {"draft": draft()}, quote["id"], "request-1", 101,
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


if __name__ == "__main__":
    unittest.main()
