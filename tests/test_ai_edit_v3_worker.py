from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from server.content_domains.ai_edit_v3.feature import FeatureConfig
from server.content_domains.ai_edit_v3.runtime import Runtime, RuntimeDependencies
from server.content_domains.ai_edit_v3.store import StoreConflictError


class FakeClock:
    def now(self):
        return 101.0


class FakeStore:
    def __init__(self):
        self.billing_queries = 0
        self.asset_queries = 0
        self.claim_calls = 0
        self.events = []

    def list_due_billing_intents(self, now, *, limit):
        self.billing_queries += 1
        self.events.append("billing")
        return []

    def list_due_publish_intents(self, now, *, limit, cursor=None):
        self.asset_queries += 1
        self.events.append("assets")
        return []

    def claim_next_job(self, worker_id, lease_seconds, now_ms):
        self.claim_calls += 1
        self.events.append("claim")
        return None


class StopAfterOneLoop:
    def __init__(self):
        self.waits = 0

    def is_set(self):
        return self.waits > 0

    def wait(self, timeout):
        self.waits += 1
        return True


class V3WorkerTests(unittest.TestCase):
    def test_worker_config_rejects_boolean_and_non_numeric_bounds(self):
        from server.ai_edit_v3_worker import WorkerConfig

        invalid = (
            {"lease_seconds": True},
            {"lease_seconds": 1.5},
            {"reconciliation_limit": False},
            {"reconciliation_limit": 1.5},
            {"queue_timeout_seconds": True},
            {"queue_timeout_seconds": 1.5},
            {"poll_interval_seconds": True},
            {"poll_interval_seconds": "0.1"},
        )
        failures = []
        for values in invalid:
            with self.subTest(values=values):
                try:
                    WorkerConfig(**values)
                except (TypeError, ValueError):
                    continue
                failures.append(values)
        self.assertEqual(failures, [])

    def test_disabled_worker_reconciles_but_never_claims_media(self):
        try:
            from server.ai_edit_v3_worker import run_worker, worker_config
        except ImportError as exc:
            self.fail(f"V3 worker entry point is absent: {exc}")

        store = FakeStore()
        dependencies = RuntimeDependencies(
            store=store,
            clock=FakeClock(),
            points=object(),
            assets=object(),
            cos=None,
            tts=None,
            asr=None,
            director=None,
            image_generator=None,
            audio_generator=None,
            renderer=None,
            process_supervisor=object(),
            stage_handlers={},
        )
        runtime = Runtime(
            config=FeatureConfig(False, None, None, "test", None, 1, 1, 1),
            dependencies=dependencies,
        )

        run_worker(StopAfterOneLoop(), config=worker_config(), runtime=runtime)

        self.assertEqual(store.billing_queries, 1)
        self.assertEqual(store.asset_queries, 1)
        self.assertEqual(store.claim_calls, 0)

    def test_ready_worker_reconciles_billing_and_assets_before_media_claim(self):
        from server.ai_edit_v3_worker import run_worker, worker_config

        store = FakeStore()
        dependencies = RuntimeDependencies(
            store=store,
            clock=FakeClock(),
            points=object(),
            assets=object(),
            cos=None,
            tts=None,
            asr=None,
            director=None,
            image_generator=None,
            audio_generator=None,
            renderer=None,
            process_supervisor=object(),
            stage_handlers={},
        )
        runtime = Runtime(
            config=FeatureConfig(True, None, None, "test", None, 3, 3, 1),
            dependencies=dependencies,
        )

        with patch(
            "server.ai_edit_v3_worker.preflight",
            return_value=SimpleNamespace(accepts_new_jobs=True),
        ):
            run_worker(StopAfterOneLoop(), config=worker_config(), runtime=runtime)

        self.assertEqual(store.events, ["billing", "assets", "claim"])
        self.assertEqual(store.claim_calls, 1)

    def test_typed_reconciliation_error_retries_before_any_media_claim(self):
        from server.ai_edit_v3_worker import run_worker, worker_config

        class StopAfterTwoLoops:
            def __init__(self):
                self.waits = 0

            def is_set(self):
                return self.waits > 1

            def wait(self, timeout):
                self.waits += 1
                return self.is_set()

        store = FakeStore()
        dependencies = RuntimeDependencies(
            store=store,
            clock=FakeClock(),
            points=object(),
            assets=object(),
            cos=None,
            tts=None,
            asr=None,
            director=None,
            image_generator=None,
            audio_generator=None,
            renderer=None,
            process_supervisor=object(),
            stage_handlers={},
        )
        runtime = Runtime(
            config=FeatureConfig(True, None, None, "test", None, 1, 1, 1),
            dependencies=dependencies,
        )
        reconciliation_error = StoreConflictError(
            "reconciliation_conflict", "injected typed conflict"
        )
        with patch(
            "server.ai_edit_v3_worker.run_reconciliation_pass",
            side_effect=[reconciliation_error, {"billing": 0, "assets": 0}],
        ) as reconcile, patch(
            "server.ai_edit_v3_worker.preflight",
            return_value=SimpleNamespace(accepts_new_jobs=True),
        ):
            run_worker(StopAfterTwoLoops(), config=worker_config(), runtime=runtime)

        self.assertEqual(reconcile.call_count, 2)
        self.assertEqual(store.claim_calls, 1)


if __name__ == "__main__":
    unittest.main()
