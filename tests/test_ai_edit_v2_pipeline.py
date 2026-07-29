import json
import os
import uuid
import copy
import threading
import tempfile
import time
import unittest
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from unittest.mock import patch
from pathlib import Path

from server.content_domains import ai_edit_v2_billing as billing
from server.content_domains import ai_edit_v2_asr as asr
from server.content_domains import ai_edit_v2_pipeline as pipeline
from server.content_domains import ai_edit_v2_runtime as runtime
from server.content_domains import ai_edit_v2_store as store
from server.content_domains import ai_edit_v2_delivery as delivery
from tests.test_ai_edit_v2_delivery import FakeCos as DeliveryCos
from tests.test_ai_edit_v2_quality import EvidenceRunner, passing_evidence
from server.content_domains.ai_edit_v2_providers.base import (
    ProviderError,
    RetryableProviderError,
    UnknownSubmissionError,
)
from server.content_domains.ai_edit_v2_schema import TERMINAL_STATES
from tests.test_ai_edit_v2_director import VALID_PLAN
from tests.test_ai_edit_v2_openai_image import png_bytes


def draft():
    return {
        "creation_mode": "natural_brief",
        "brief": "测试可恢复的 AI 剪辑流水线",
        "language": "zh-CN",
        "aspect_ratio": "16:9",
        "target_duration_ms": 60_000,
        "input_mode": "external_video",
        "main_input": {
            "asset_id": "main",
            "kind": "video",
            "size_bytes": 10_000,
            "duration_ms": 90_000,
        },
        "required_materials": [],
        "reference_materials": [],
    }

def _stable_resolved_plan(duration_ms, timeline):
    plan = copy.deepcopy(VALID_PLAN)
    plan["duration_ms"] = duration_ms
    plan["target_duration_ms"] = duration_ms
    plan["scenes"][0]["end_ms"] = duration_ms
    plan.update({
        "materials": {},
        "material_resolution_status": "resolved",
        "text_timeline": copy.deepcopy(timeline),
        "primary_video": {
            "cos_key": "private/source.mp4",
            "media_type": "video",
            "metadata": {"duration_ms": duration_ms},
        },
    })
    return plan


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
        self.assets_path = os.path.join(self.temp_dir.name, "assets.db")
        with closing(sqlite3.connect(self.assets_path)) as conn:
            conn.execute("""CREATE TABLE video_assets(
                id INTEGER PRIMARY KEY AUTOINCREMENT,job_id TEXT UNIQUE,
                username TEXT NOT NULL,mode TEXT NOT NULL,video_file TEXT,
                video_url TEXT,resolution TEXT,ratio TEXT,phase TEXT,status TEXT NOT NULL,
                created_at INTEGER,updated_at INTEGER)""")
        self.env = patch.dict(
            os.environ,
            {
                "AI_EDIT_V2_DB": self.db_path,
                "AI_EDIT_V2_PRICING_DB": self.pricing_path,
                "AI_EDIT_V2_ASSET_DB": self.assets_path,
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

    def test_every_terminal_state_has_zero_remaining_budget(self):
        for terminal in sorted(TERMINAL_STATES):
            with self.subTest(terminal=terminal):
                job = self._precharged_job(job_id=f"job-{terminal}")
                self._set_state(
                    job["id"], terminal,
                    [{"version": 1, "state": "normalizing", "at": 100, "data": {}}],
                )
                timing = pipeline.timing_status(job["id"], 5000, db_path=self.db_path)
                self.assertEqual(timing["remaining_seconds"], 0)

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
             patch.object(worker, "reconcile_provider_events") as provider_events, \
             patch.object(worker.pipeline, "reconcile_terminal_refunds") as refunds, \
             patch.object(worker.store, "claim_next_job") as claim:
            worker.run_worker(stop_event, config=config)

        pending.assert_called_once()
        provider_events.assert_called_once()
        refunds.assert_called_once()
        claim.assert_not_called()

    def test_dependency_incomplete_worker_reconciles_everything_without_claiming(self):
        from server import ai_edit_v2_worker as worker
        from server.content_domains import ai_edit_v2_feature as feature

        config = {"enabled": True, "workers": 1, "lease_seconds": 30,
                  "poll_seconds": 0.01, "normal_timeout_seconds": 2700,
                  "repair_timeout_seconds": 900, "db_path": self.db_path}
        stop_event = threading.Event()
        dependencies = {
            "readiness_errors": lambda: ["quality_dependency"],
            "services": None,
        }

        def reconciled(*_args, **_kwargs):
            stop_event.set()
            return 0

        with patch.object(feature, "capability", return_value={
                 "enabled": True,
                 "stable_runtime_ready": False,
                 "accepts_submissions": False,
             }), patch.object(
                 worker.billing, "reconcile_pending_precharges"
             ) as pending, patch.object(
                 worker.pipeline, "reconcile_terminal_refunds"
             ) as refunds, patch.object(
                 worker.delivery, "reconcile_pending_deliveries",
                 side_effect=reconciled,
             ) as deliveries, patch.object(
                 worker.store, "claim_next_job"
             ) as claim:
            worker.run_worker(stop_event, config=config, handlers=dependencies)

        pending.assert_called_once()
        refunds.assert_called_once()
        deliveries.assert_called_once()
        claim.assert_not_called()

    def test_enabled_worker_periodically_reconciles_pending_delivery_outbox(self):
        from server import ai_edit_v2_worker as worker

        config = {"enabled": True, "workers": 1, "lease_seconds": 30,
                  "poll_seconds": 0.01, "normal_timeout_seconds": 2700,
                  "repair_timeout_seconds": 900, "db_path": self.db_path}
        stop_event = threading.Event()

        def reconciled(*_args, **_kwargs):
            stop_event.set()
            return 0

        dependencies = {"readiness_errors": lambda: [], "services": None}
        with patch.object(worker.feature, "capability", return_value={
                 "stable_runtime_ready": True, "accepts_submissions": True,
             }), patch.object(worker.billing, "reconcile_pending_precharges"), \
             patch.object(worker.pipeline, "reconcile_terminal_refunds"), \
             patch.object(worker.delivery, "reconcile_pending_deliveries", side_effect=reconciled) as delivery_reconcile, \
             patch.object(worker.store, "claim_next_job") as claim:
            worker.run_worker(stop_event, config=config, handlers=dependencies)
        delivery_reconcile.assert_called_once()
        claim.assert_not_called()

    def test_disabled_worker_uses_same_delivery_reconciliation_dependencies(self):
        from server import ai_edit_v2_worker as worker

        config = {"enabled": False, "workers": 1, "lease_seconds": 30,
                  "poll_seconds": 0.01, "normal_timeout_seconds": 2700,
                  "repair_timeout_seconds": 900, "db_path": self.db_path}
        stop_event = threading.Event()
        fake_cos, fake_points = object(), object()
        services = type("Services", (), {"cos": fake_cos})()
        dependencies = {"services": services, "asset_db_path": self.assets_path,
                        "points_client": fake_points}
        captured = []

        def reconciled(*_args, **kwargs):
            captured.append(kwargs)
            stop_event.set()
            return 0

        with patch.object(worker.billing, "reconcile_pending_precharges"), \
             patch.object(worker.pipeline, "reconcile_terminal_refunds"), \
             patch.object(worker.delivery, "reconcile_pending_deliveries", side_effect=reconciled):
            worker.run_worker(stop_event, config=config, handlers=dependencies)
        self.assertEqual(len(captured), 1)
        self.assertIs(captured[0]["cos_api"], fake_cos)
        self.assertEqual(captured[0]["asset_db_path"], self.assets_path)
        self.assertIs(captured[0]["points_client"], fake_points)

    def test_shared_webhook_queue_gets_authoritative_status_then_marks_duplicate_processed(self):
        from server import ai_edit_v2_worker as worker
        from server.content_domains import ai_edit_v2_shotstack as shotstack

        job = self._precharged_job("job-webhook-queue")
        self._set_state(job["id"], "rendering", [])
        attempt_id = store.record_stage_attempt(
            job["id"], "rendering", 1, "submitted", 110,
            provider_task_id="render-async-1", db_path=self.db_path,
        )
        with closing(store.open_store(self.db_path)) as conn:
            conn.execute(
                "UPDATE edit_v2_stage_attempts SET provider_reference=? WHERE id=?",
                ("job-webhook-queue:render:1", attempt_id),
            )
        calls = []

        def request(method, url, _headers, _body, _timeout):
            calls.append((method, url))
            return {"success": True, "message": "OK", "response": {
                "id": "render-async-1", "status": "done",
                "url": "https://cdn.example.invalid/render-async-1.mp4",
            }}

        event = {"id": "render-async-1", "status": "done"}
        with patch.dict(os.environ, {
            "AI_EDIT_V2_WEBHOOK_SECRET": "worker-webhook-secret",
            "SHOTSTACK_API_KEY": "test-shotstack-key",
        }):
            self.assertTrue(shotstack.enqueue_webhook(
                job["id"], event, received_at=120, db_path=self.db_path
            ))
            services = runtime.ProductionServices(self.db_path, shotstack_http=request)
            processed = worker.reconcile_provider_events(
                "shared-worker", {"db_path": self.db_path, "lease_seconds": 30},
                {"services": services}, now=121,
            )
            self.assertEqual(processed, 1)
            self.assertFalse(shotstack.enqueue_webhook(
                job["id"], event, received_at=122, db_path=self.db_path
            ))
            self.assertEqual(worker.reconcile_provider_events(
                "shared-worker", {"db_path": self.db_path, "lease_seconds": 30},
                {"services": services}, now=123,
            ), 0)

        self.assertEqual(calls, [
            ("GET", "https://api.shotstack.io/edit/stage/render/render-async-1")
        ])
        with closing(store.open_store(self.db_path)) as conn:
            row = conn.execute(
                "SELECT normalized_status,attempt_count FROM edit_v2_provider_events"
            ).fetchone()
        self.assertEqual((row["normalized_status"], row["attempt_count"]), ("processed", 0))

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
    def test_retryable_provider_waits_for_backoff_before_retrying(self):
        job = self._precharged_job(str(uuid.uuid4()))
        calls = []
        dependencies = self._dependencies(calls)
        attempts = []
        sleeps = []
        original = dependencies["handlers"]["resolving_materials"]

        def rate_limited_once(job_row, context, stage_input):
            attempts.append("openai-image")
            if len(attempts) == 1:
                raise RetryableProviderError(
                    "openai_image_unavailable", retry_after_seconds=2
                )
            return original(job_row, context, stage_input)

        dependencies["handlers"]["resolving_materials"] = rate_limited_once
        dependencies["retry_sleep"] = sleeps.append
        result = pipeline.run_job(job["id"], dependencies, db_path=self.db_path)

        self.assertEqual(result["state"], "quality_checking")
        self.assertEqual(attempts, ["openai-image", "openai-image"])
        self.assertEqual(sleeps, [2])

    def test_unknown_openai_image_submission_fails_once_and_refunds_full_hold(self):
        job = self._precharged_job(str(uuid.uuid4()))
        calls = []
        dependencies = self._dependencies(calls)
        provider_calls = []

        def fail_unknown(_job, _context, _stage_input):
            provider_calls.append("openai-image")
            raise ProviderError("openai_image_submission_unknown")

        dependencies["handlers"]["resolving_materials"] = fail_unknown
        result = pipeline.run_job(job["id"], dependencies, db_path=self.db_path)

        self.assertEqual(result["state"], "asset_failed")
        self.assertEqual(result["error_code"], "openai_image_submission_unknown")
        self.assertEqual(provider_calls, ["openai-image"])
        self.assertEqual(self.points.balance, 1_000)

    def test_quality_boundary_delivers_after_task7_quality_checking(self):
        job = self._precharged_job(str(uuid.uuid4()))
        calls = []
        dependencies = self._dependencies(calls)
        pipeline.run_job(job["id"], dependencies, db_path=self.db_path)
        output = os.path.join(self.temp_dir.name, "quality-final.mp4")
        Path(output).write_bytes(b"playable-final-mp4")
        quality_evidence = passing_evidence()
        quality_evidence["probe"]["duration_ms"] = 1_000
        dependencies.update({
            "quality_runner": EvidenceRunner(quality_evidence),
            "quality_output_path": output,
            "actual_cost": 20,
            "now": lambda: int(time.time()),
        })

        with patch.object(delivery, "cos", DeliveryCos()), patch.object(billing, "points", self.points):
            result = pipeline.run_job(job["id"], dependencies, db_path=self.db_path)

        self.assertEqual(result["state"], "completed")

    def test_repair_receives_only_failing_layer_and_uses_900_second_deadline(self):
        job = self._precharged_job(str(uuid.uuid4()))
        dependencies = self._dependencies([])
        pipeline.run_job(job["id"], dependencies, db_path=self.db_path)
        output = os.path.join(self.temp_dir.name, "repair-final.mp4")
        Path(output).write_bytes(b"playable-final-mp4")
        evidence = passing_evidence()
        evidence["probe"]["duration_ms"] = 1_000
        bad_captions = {"safe_area": False, "tofu_count": 0, "missing_glyphs": []}
        probe_count = [0]

        def runner(check, **_kwargs):
            if check == "probe":
                probe_count[0] += 1
            if check == "captions" and probe_count[0] == 1:
                return bad_captions
            return evidence[check]

        repaired = []
        def repair(_job, context):
            repaired.append(context)
            return {"output_path": output}

        now = int(time.time())
        dependencies.update({
            "quality_runner": runner,
            "quality_output_path": output,
            "repair_layer": repair,
            "actual_cost": 20,
            "now": lambda: now,
        })
        with patch.object(delivery, "cos", DeliveryCos()), patch.object(billing, "points", self.points):
            result = pipeline.run_job(job["id"], dependencies, db_path=self.db_path)

        self.assertEqual(result["state"], "completed")
        self.assertEqual(repaired[0]["failing_layers"], ("captions",))
        self.assertEqual(repaired[0]["deadline_at"], now + 900)
        self.assertEqual(probe_count[0], 2)

    def test_rehydrated_repair_refreshes_qwen_url_inside_repair_budget(self):
        job = self._precharged_job(str(uuid.uuid4()))
        dependencies = self._dependencies([])
        pipeline.run_job(job["id"], dependencies, db_path=self.db_path)
        clock = [100]

        class RepairCos:
            def __init__(self):
                self.objects = {
                    "private/postprocessing.json": b"original-final",
                    "private/repaired.mp4": b"rehydrated-repair",
                }
                self.presigns = []

            def download_file(self, key, path):
                Path(path).write_bytes(self.objects[key])

            def presign_get(self, key, expires=300):
                self.presigns.append((key, expires, clock[0]))
                return (
                    f"https://cos.test/{key}?expires_at={clock[0] + expires}"
                    f"&nonce={len(self.presigns)}"
                )

            def put_file(self, path, key, _content_type, private=True):
                self.objects[key] = Path(path).read_bytes()
                return {"ETag": '"uploaded"'}

            def head_object(self, key):
                return {
                    "content_length": len(self.objects[key]),
                    "content_type": "video/mp4",
                    "etag": "uploaded",
                }

        analyzer = lambda *_args, **_kwargs: {}
        analyzer.capabilities = lambda: {
            "captions_ocr": True, "glyphs": True, "materials": True,
            "transcript_facts": True, "audio": True,
        }
        repair_cos = RepairCos()
        services = runtime.ProductionServices(
            self.db_path, cos_api=repair_cos,
            repair_handler=lambda *_args: {},
            repair_reconciler=lambda *_args: {},
            quality_analyzer=analyzer,
            quality_binary_finder=lambda name: name,
        )
        production = runtime.production_dependencies(self.db_path, services=services)
        semantic_urls = []
        evidence = passing_evidence()
        evidence["probe"]["duration_ms"] = 1_000
        probe_count = [0]

        def runner(check, *, path, resolved_plan):
            del resolved_plan
            if check == "probe":
                probe_count[0] += 1
            if check in {"captions", "materials", "transcript"}:
                url = services._quality_video_url(path)
                expires_at = int(url.split("expires_at=", 1)[1].split("&", 1)[0])
                if expires_at <= clock[0]:
                    raise RuntimeError("quality URL expired")
                semantic_urls.append((Path(path).parent.name, url))
            if check == "captions" and probe_count[0] == 1:
                return {"safe_area": False, "tofu_count": 0, "missing_glyphs": []}
            return evidence[check]

        missing_path = os.path.join(self.temp_dir.name, "missing-repair.mp4")

        def repair(_job, _context):
            clock[0] = 500  # Original 300-second URL is expired; repair budget is not.
            return {"cos_key": "private/repaired.mp4", "output_path": missing_path}

        dependencies.update({
            "quality_runner": runner,
            "quality_output_path": services.resolve_quality_output,
            "repair_layer": repair,
            "actual_cost": 20,
            "now": lambda: clock[0],
            "lease_seconds": 1_000,
            "services": services,
        })
        if callable(production.get("register_quality_output")):
            dependencies["register_quality_output"] = production["register_quality_output"]

        with patch.object(delivery, "cos", DeliveryCos()), patch.object(
            billing, "points", self.points
        ):
            result = pipeline.run_job(job["id"], dependencies, db_path=self.db_path)

        self.assertEqual(result["state"], "completed", result)
        self.assertEqual(probe_count[0], 2)
        self.assertEqual([item[0] for item in repair_cos.presigns], [
            "private/postprocessing.json", "private/postprocessing.json",
            "private/postprocessing.json", "private/repaired.mp4",
            "private/repaired.mp4", "private/repaired.mp4",
        ])
        self.assertTrue(all(expires == 300 for _key, expires, _at in repair_cos.presigns))
        self.assertEqual(len({url for _directory, url in semantic_urls}), 6)

    def test_restart_rebuilds_services_and_rehydrates_saved_repair_for_qwen(self):
        class LostResponse(BaseException):
            pass

        job = self._precharged_job(str(uuid.uuid4()))
        base = self._dependencies([])
        pipeline.run_job(job["id"], base, db_path=self.db_path)

        class RestartCos:
            def __init__(self):
                self.objects = {
                    "private/postprocessing.json": b"original-final",
                    "private/restart-repair.mp4": b"restart-repair",
                }
                self.presigns = []

            def download_file(self, key, path):
                Path(path).write_bytes(self.objects[key])

            def presign_get(self, key, expires=300):
                self.presigns.append((key, expires))
                return (
                    f"https://cos.test/{key}?expires={expires}"
                    f"&nonce={len(self.presigns)}"
                )

            def put_file(self, path, key, _content_type, private=True):
                self.objects[key] = Path(path).read_bytes()
                return {"ETag": '"uploaded"'}

            def head_object(self, key):
                return {
                    "content_length": len(self.objects[key]),
                    "content_type": "video/mp4",
                    "etag": "uploaded",
                }

        analyzer = lambda *_args, **_kwargs: {}
        analyzer.capabilities = lambda: {
            "captions_ocr": True, "glyphs": True, "materials": True,
            "transcript_facts": True, "audio": True,
        }
        restart_cos = RestartCos()
        evidence = passing_evidence()
        evidence["probe"]["duration_ms"] = 1_000

        def quality_runner(services, *, bad_captions):
            def run(check, *, path, resolved_plan):
                del resolved_plan
                if check in {"captions", "materials", "transcript"}:
                    services._quality_video_url(path)
                if check == "captions" and bad_captions:
                    return {
                        "safe_area": False,
                        "tofu_count": 0,
                        "missing_glyphs": [],
                    }
                return evidence[check]
            return run

        submitted = []

        def submit(_job, context):
            submitted.append(context["idempotency_key"])
            context["save_provider_task_id"]("restart-repair-task-1")
            raise LostResponse("provider accepted before process restart")

        services_before = runtime.ProductionServices(
            self.db_path, cos_api=restart_cos,
            repair_handler=submit,
            repair_reconciler=lambda *_args: self.fail(
                "first process must not reconcile"
            ),
            quality_analyzer=analyzer,
            quality_binary_finder=lambda name: name,
        )
        dependencies_before = {
            **base,
            **runtime.production_dependencies(
                self.db_path, services=services_before
            ),
            "quality_runner": quality_runner(
                services_before, bad_captions=True
            ),
            "actual_cost": 20,
            "points_client": self.points,
        }
        with self.assertRaises(LostResponse):
            pipeline.run_job(
                job["id"], dependencies_before, db_path=self.db_path
            )

        with closing(store.open_store(self.db_path)) as conn:
            attempt = conn.execute(
                """SELECT status,provider_task_id FROM edit_v2_stage_attempts
                   WHERE job_id=? AND stage='repairing'""",
                (job["id"],),
            ).fetchone()
            conn.execute(
                "UPDATE edit_v2_jobs SET lease_until=0 WHERE id=?",
                (job["id"],),
            )
        self.assertEqual(
            (attempt["status"], attempt["provider_task_id"]),
            ("running", "restart-repair-task-1"),
        )
        restart_cos.presigns.clear()
        reconciled = []
        missing_path = os.path.join(
            self.temp_dir.name, "restart-missing-repair.mp4"
        )

        def reconcile(_job, context):
            reconciled.append(context["provider_task_id"])
            return {
                "provider": "shotstack",
                "provider_task_id": context["provider_task_id"],
                "request_id": "restart-repair-request-1",
                "cost_units": 1,
                "cos_key": "private/restart-repair.mp4",
                "output_path": missing_path,
            }

        services_after = runtime.ProductionServices(
            self.db_path, cos_api=restart_cos,
            repair_handler=lambda *_args: self.fail(
                "restart must reconcile the saved provider id"
            ),
            repair_reconciler=reconcile,
            quality_analyzer=analyzer,
            quality_binary_finder=lambda name: name,
        )
        dependencies_after = {
            **base,
            **runtime.production_dependencies(
                self.db_path, services=services_after
            ),
            "quality_runner": quality_runner(
                services_after, bad_captions=False
            ),
            "actual_cost": 20,
            "points_client": self.points,
        }
        result = pipeline.run_job(
            job["id"], dependencies_after, db_path=self.db_path
        )

        self.assertEqual(result["state"], "completed", result)
        self.assertEqual(len(submitted), 1)
        self.assertEqual(reconciled, ["restart-repair-task-1"])
        self.assertEqual(restart_cos.presigns, [
            ("private/restart-repair.mp4", 300),
            ("private/restart-repair.mp4", 300),
            ("private/restart-repair.mp4", 300),
        ])

    def test_repair_lost_response_reconciles_saved_provider_id_without_resubmit(self):
        class LostResponse(BaseException):
            pass

        job = self._precharged_job(str(uuid.uuid4()))
        base = self._dependencies([])
        pipeline.run_job(job["id"], base, db_path=self.db_path)
        output = os.path.join(self.temp_dir.name, "repair-reconcile.mp4")
        Path(output).write_bytes(b"repair-reconcile")
        bad = passing_evidence(); bad["probe"]["duration_ms"] = 1_000
        bad["captions"]["safe_area"] = False
        submitted, reconciled = [], []
        now = [200]

        def submit(_job, context):
            submitted.append(context["idempotency_key"])
            context["save_provider_task_id"]("repair-provider-1")
            raise LostResponse("provider accepted; response lost")

        deps = {
            **base, "quality_runner": EvidenceRunner(bad),
            "quality_output_path": output, "actual_cost": 20,
            "repair_layer": submit, "now": lambda: now[0],
        }
        with patch.object(delivery, "cos", DeliveryCos()), patch.object(billing, "points", self.points):
            with self.assertRaises(LostResponse):
                pipeline.run_job(job["id"], deps, db_path=self.db_path)
            with closing(store.open_store(self.db_path)) as conn:
                attempt = conn.execute(
                    "SELECT status,provider_task_id FROM edit_v2_stage_attempts WHERE job_id=? AND stage='repairing'",
                    (job["id"],),
                ).fetchone()
                conn.execute("UPDATE edit_v2_jobs SET lease_until=0 WHERE id=?", (job["id"],))
            self.assertEqual((attempt["status"], attempt["provider_task_id"]), ("running", "repair-provider-1"))
            good = passing_evidence(); good["probe"]["duration_ms"] = 1_000
            now[0] = 201

            def reconcile(_job, context):
                reconciled.append(context["provider_task_id"])
                return {"output_path": output}

            recovered = pipeline.run_job(
                job["id"], {**deps, "quality_runner": EvidenceRunner(good),
                            "repair_reconciler": reconcile}, db_path=self.db_path,
            )
        self.assertEqual(recovered["state"], "completed")
        self.assertEqual(len(submitted), 1)
        self.assertEqual(reconciled, ["repair-provider-1"])

    def test_repair_deadline_is_capped_at_900_seconds_when_env_is_1800(self):
        with patch.dict(os.environ, {"AI_EDIT_V2_REPAIR_BUDGET_SECONDS": "1800"}):
            self.assertEqual(pipeline._repair_budget(), 900)

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
        calls = {"openai": 0, "elevenlabs": 0, "quality": []}
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
            if command[-1] not in {"NUL", "-"}: Path(command[-1]).write_bytes(b"mastered-audio")
            loudness = b'{"input_i":"-20","input_tp":"-2","input_lra":"5","input_thresh":"-30","target_offset":"0"}'
            return type("Completed", (), {"returncode": 0, "stdout": b"", "stderr": loudness})()
        def quality_analyzer(check, *, path, expected):
            calls["quality"].append((check, Path(path).name))
            if check == "materials":
                return {"covered_asset_ids": expected["required_asset_ids"]}
            return {
                "captions": {"safe_area": True, "tofu_count": 0, "missing_glyphs": []},
                "transcript": {"source_matches": True, "facts_match": True},
                "audio": {"silence_ratio": 0.0, "true_peak_dbfs": -1.0,
                          "dialogue_to_bgm_db": 8.0, "dialogue_to_sfx_db": 8.0},
            }[check]
        quality_analyzer.capabilities = lambda: {
            "captions_ocr": True, "glyphs": True, "materials": True,
            "transcript_facts": True, "audio": True,
        }
        fake_cos = FakeCos()
        services = runtime.ProductionServices(self.db_path, cos_api=fake_cos, runner=runner,
                                              dashscope_http=dashscope_http, shotstack_http=shotstack_http,
                                              openai_http=openai_http, openai_downloader=openai_download,
                                              elevenlabs_http=eleven_http,
                                              downloader=lambda _url: b"rendered-mp4",
                                              repair_handler=lambda *_args: {},
                                              repair_reconciler=lambda *_args: {},
                                              quality_analyzer=quality_analyzer,
                                              quality_binary_finder=lambda name: name)
        dependencies = runtime.production_dependencies(self.db_path, services=services)
        dependencies["points_client"] = self.points
        current_time = int(time.time())
        dependencies["now"] = lambda: current_time
        config = {"db_path": self.db_path, "lease_seconds": 180}
        claimed = store.claim_job(job_id, "integration-lease", 180, current_time, db_path=self.db_path)
        with patch.dict(os.environ, {"DASHSCOPE_API_KEY": "fake", "SHOTSTACK_API_KEY": "fake", "OPENAI_API_KEY": "fake", "ELEVENLABS_API_KEY": "fake", "AI_EDIT_V2_SHOTSTACK_CALLBACK_URL": "https://callback.test/v2", "AI_EDIT_V2_WEBHOOK_SECRET": "fake-secret"}, clear=False):
            runtime.assert_production_ready(dependencies)
            result = worker._process_claimed(claimed, "integration-lease", config, dependencies)
        self.assertEqual(result["state"], "completed", (result, calls))
        self.assertIn(b"rendered-mp4", fake_cos.objects.values())
        self.assertEqual(calls["openai"], 1)
        self.assertEqual(calls["elevenlabs"], 1)
        self.assertEqual([item[0] for item in calls["quality"]],
                         ["captions", "materials", "transcript", "audio"])

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
                resolved_plan = _stable_resolved_plan(1000, timeline)
                outputs = {
                    "normalizing": {"normalized_media": {"cos_key": "private/source.mp4", "media_type": "video", "metadata": {"duration_ms": 1000}}, "artifact": artifact},
                    "transcribing": {"asr_result": {"provider_task_id": "asr-1", "duration_ms": 1000, "words": [item], "sentences": [item]}},
                    "aligning": {"text_timeline": timeline},
                    "directing": {"edit_plan": copy.deepcopy(VALID_PLAN)},
                    "resolving_materials": {"resolved_plan": copy.deepcopy(resolved_plan)},
                    "generating_media": {"resolved_plan": copy.deepcopy(resolved_plan), "audio_plan": {"bgm": None, "sfx": [], "degradations": []}, "generated_audio": {"bgm": None, "sfx": [], "degradations": []}},
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
