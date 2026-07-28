#!/usr/bin/env python3
"""Independent leased worker process for AI Edit V2."""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor, wait
from typing import Any

try:
    from content_domains import ai_edit_v2_billing as billing
    from content_domains import ai_edit_v2_delivery as delivery
    from content_domains import ai_edit_v2_feature as feature
    from content_domains import ai_edit_v2_pipeline as pipeline
    from content_domains import ai_edit_v2_store as store
    from content_domains import ai_edit_v2_runtime as runtime
except ImportError:
    from .content_domains import ai_edit_v2_billing as billing
    from .content_domains import ai_edit_v2_delivery as delivery
    from .content_domains import ai_edit_v2_feature as feature
    from .content_domains import ai_edit_v2_pipeline as pipeline
    from .content_domains import ai_edit_v2_store as store
    from .content_domains import ai_edit_v2_runtime as runtime


LOG = logging.getLogger("ai-edit-v2")
STAGE_HANDLERS = runtime.STAGE_HANDLERS


def _enabled(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def worker_config() -> dict[str, Any]:
    return {
        "enabled": _enabled(os.environ.get("AI_EDIT_V2_ENABLED", "0")),
        "workers": max(1, min(10, int(os.environ.get("AI_EDIT_V2_WORKERS", "5")))),
        "lease_seconds": max(30, int(os.environ.get("AI_EDIT_V2_LEASE_SECONDS", "180"))),
        "poll_seconds": max(0.1, float(os.environ.get("AI_EDIT_V2_POLL_SECONDS", "1"))),
        "normal_timeout_seconds": int(os.environ.get("AI_EDIT_V2_NORMAL_TIMEOUT_SECONDS", "2700")),
        "repair_timeout_seconds": int(os.environ.get("AI_EDIT_V2_REPAIR_TIMEOUT_SECONDS", "900")),
        "db_path": store._db_path(None),
    }


def _heartbeat(
    job_id: str,
    worker_id: str,
    lease_seconds: int,
    finished: threading.Event,
    db_path: str,
) -> None:
    interval = max(10, lease_seconds // 3)
    while not finished.wait(interval):
        if not store.renew_lease(
            job_id, worker_id, lease_seconds, int(time.time()), db_path=db_path
        ):
            LOG.error("[ai-edit-v2] lease renewal lost job=%s", job_id)
            return


def _process_claimed(
    job: dict[str, Any], worker_id: str, config: dict[str, Any], dependencies: dict[str, Any]
) -> dict[str, Any]:
    bundle = dict(dependencies)
    bundle.update(
        {
            "lease_owner": worker_id,
            "lease_seconds": config["lease_seconds"],
        }
    )
    return pipeline.run_job(job["id"], bundle, db_path=config["db_path"])


def run_worker(
    stop_event: threading.Event,
    *,
    config: dict[str, Any] | None = None,
    handlers: dict[str, Any] | None = None,
) -> None:
    config = dict(config or worker_config())
    store.init_db(config["db_path"])
    dependencies = handlers or runtime.production_dependencies(config["db_path"])
    capability = feature.capability()
    accepts_submissions = bool(config["enabled"]) and bool(
        capability.get("accepts_submissions")
    )

    def reconcile_once() -> None:
        billing.reconcile_pending_precharges(
            int(time.time()), db_path=config["db_path"]
        )
        pipeline.reconcile_terminal_refunds(db_path=config["db_path"])
        services = runtime.option(dependencies, "services")
        delivery.reconcile_pending_deliveries(
            int(time.time()), db_path=config["db_path"],
            lease_seconds=config["lease_seconds"],
            cos_api=getattr(services, "cos", None),
            asset_db_path=(runtime.option(dependencies, "asset_db_path")
                           or delivery._asset_db_path()),
            points_client=runtime.option(dependencies, "points_client", billing.points),
        )

    if not accepts_submissions:
        LOG.warning("[ai-edit-v2] submissions disabled; reconciliation-only mode")
        while not stop_event.is_set():
            try:
                reconcile_once()
            except Exception:
                LOG.exception("[ai-edit-v2] reconciliation failed")
            stop_event.wait(config["poll_seconds"])
        return
    worker_id = f"{os.getpid()}-{uuid.uuid4().hex[:10]}"
    runtime.assert_production_ready(dependencies)
    active: set[Future] = set()
    with ThreadPoolExecutor(
        max_workers=config["workers"], thread_name_prefix="ai-edit-v2"
    ) as executor:
        while not stop_event.is_set():
            try:
                reconcile_once()
            except Exception:
                LOG.exception("[ai-edit-v2] reconciliation failed")
            finished = {future for future in active if future.done()}
            for future in finished:
                active.remove(future)
                try:
                    future.result()
                except Exception:
                    LOG.exception("[ai-edit-v2] stage execution failed")
            while len(active) < config["workers"] and not stop_event.is_set():
                claim_owner = f"{worker_id}-{uuid.uuid4().hex}"
                job = store.claim_next_job(
                    claim_owner, config["lease_seconds"], int(time.time()),
                    db_path=config["db_path"],
                )
                if job is None:
                    break
                active.add(
                    executor.submit(
                        _process_claimed, job, claim_owner, config, dependencies
                    )
                )
            stop_event.wait(config["poll_seconds"])
        if active:
            LOG.info("[ai-edit-v2] stop requested; waiting for %d active stage(s)", len(active))
            wait(active)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    stop_event = threading.Event()

    def stop(_signum, _frame):
        stop_event.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    run_worker(stop_event)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
