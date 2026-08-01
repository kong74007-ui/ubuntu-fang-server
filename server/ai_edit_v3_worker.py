"""Reconciliation-first polling Worker for the isolated AI Edit V3 store."""

from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from server.content_domains.ai_edit_v3.feature import FeatureConfig
from server.content_domains.ai_edit_v3.pipeline import run_job, run_reconciliation_pass
from server.content_domains.ai_edit_v3.runtime import Runtime, preflight
from server.content_domains.ai_edit_v3.store import LeaseLost, StoreConflictError


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    worker_id: str = "ai-edit-v3-worker"
    lease_seconds: int = 30
    poll_interval_seconds: float = 0.05
    reconciliation_limit: int = 100
    queue_timeout_seconds: int = 300

    def __post_init__(self):
        if not self.worker_id or self.worker_id != self.worker_id.strip():
            raise ValueError("worker_id_invalid")
        if (
            isinstance(self.lease_seconds, bool)
            or not isinstance(self.lease_seconds, int)
            or not 1 <= self.lease_seconds <= 3600
        ):
            raise ValueError("worker_lease_invalid")
        if (
            isinstance(self.poll_interval_seconds, bool)
            or not isinstance(self.poll_interval_seconds, (int, float))
            or not math.isfinite(self.poll_interval_seconds)
            or not 0 < self.poll_interval_seconds <= 60
        ):
            raise ValueError("worker_poll_interval_invalid")
        if (
            isinstance(self.reconciliation_limit, bool)
            or not isinstance(self.reconciliation_limit, int)
            or not 1 <= self.reconciliation_limit <= 1000
        ):
            raise ValueError("worker_reconciliation_limit_invalid")
        if (
            isinstance(self.queue_timeout_seconds, bool)
            or not isinstance(self.queue_timeout_seconds, int)
            or not 1 <= self.queue_timeout_seconds <= 86_400
        ):
            raise ValueError("worker_queue_timeout_invalid")


def worker_config() -> WorkerConfig:
    return WorkerConfig()


def _ready(config: FeatureConfig, runtime: Runtime) -> bool:
    return bool(
        config.enabled
        and config.worker_concurrency is not None
        and config.worker_concurrency > 0
        and preflight(runtime).accepts_new_jobs
    )


def run_worker(stop_event, *, config=None, runtime=None) -> None:
    if config is None:
        config = worker_config()
    if not isinstance(config, WorkerConfig):
        raise TypeError("worker_config_invalid")
    if not isinstance(runtime, Runtime):
        raise TypeError("worker_runtime_required")
    dependencies = runtime.dependencies
    if dependencies is None:
        while not stop_event.is_set():
            stop_event.wait(config.poll_interval_seconds)
        return

    while not stop_event.is_set():
        try:
            run_reconciliation_pass(
                dependencies,
                worker_id=f"{config.worker_id}:reconcile",
                lease_seconds=config.lease_seconds,
                limit=config.reconciliation_limit,
            )
        except (LeaseLost, StoreConflictError):
            stop_event.wait(config.poll_interval_seconds)
            continue
        if _ready(runtime.config, runtime):
            concurrency = runtime.config.worker_concurrency or 0
            claims = []
            for _ in range(concurrency):
                if stop_event.is_set():
                    break
                now_ms = int(dependencies.clock.now() * 1000)
                claim = dependencies.store.claim_next_job(
                    config.worker_id, config.lease_seconds, now_ms
                )
                if claim is None:
                    break
                claims.append(claim)
            if claims:
                with ThreadPoolExecutor(
                    max_workers=concurrency,
                    thread_name_prefix="ai-edit-v3-pipeline",
                ) as executor:
                    futures = [
                        executor.submit(
                            run_job,
                            claim,
                            dependencies,
                            stop_event=stop_event,
                            lease_seconds=config.lease_seconds,
                            queue_timeout_ms=config.queue_timeout_seconds * 1000,
                        )
                        for claim in claims
                    ]
                    for future in futures:
                        future.result()
        stop_event.wait(config.poll_interval_seconds)


__all__ = ("WorkerConfig", "run_worker", "worker_config")
