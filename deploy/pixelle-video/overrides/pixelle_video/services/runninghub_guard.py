from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, Optional

from comfykit.comfyui.models import ExecuteResult
from comfykit.comfyui.runninghub_executor import RunningHubExecutor
from comfykit.logger import logger


DEFAULT_TASK_TIMEOUT_SECONDS = 15 * 60
STATUS_QUERY_TIMEOUT_SECONDS = 60
CHECK_INTERVAL_SECONDS = 2
TERMINAL_ERROR_MARKERS = ("APIKEY_TASK_NOT_FOUND",)


def _is_terminal_error(exc: Exception) -> bool:
    message = str(exc).upper()
    return any(marker in message for marker in TERMINAL_ERROR_MARKERS)


def _error_result(task_id: str, message: str) -> ExecuteResult:
    return ExecuteResult(status="error", prompt_id=task_id, msg=message)


def _resolve_task_timeout(
    executor_timeout: Optional[float],
    requested_timeout: Optional[float],
) -> float:
    configured_timeout = (
        requested_timeout if requested_timeout is not None else executor_timeout
    )
    if configured_timeout is None or configured_timeout <= 0:
        return DEFAULT_TASK_TIMEOUT_SECONDS
    return min(configured_timeout, DEFAULT_TASK_TIMEOUT_SECONDS)


async def guarded_wait_for_task_completion(
    self: RunningHubExecutor,
    task_id: str,
    output_id_2_var: Optional[Dict[str, str]] = None,
    max_wait_time: Optional[float] = None,
) -> ExecuteResult:
    """Wait for RunningHub without allowing missing tasks to poll forever."""
    task_timeout = _resolve_task_timeout(self.timeout, max_wait_time)
    deadline = time.monotonic() + task_timeout
    logger.info(
        f"Waiting for RunningHub task completion: {task_id} "
        f"(timeout: {task_timeout}s)"
    )

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            message = f"RunningHub task {task_id} timeout after {task_timeout} seconds"
            logger.error(message)
            return _error_result(task_id, message)

        try:
            status_info = await asyncio.wait_for(
                self.client.query_task_status(task_id),
                timeout=min(STATUS_QUERY_TIMEOUT_SECONDS, remaining),
            )
            task_status = status_info["status"]
            status_msg = status_info.get("msg", "")

            if task_status == "SUCCESS":
                result_data = await asyncio.wait_for(
                    self.client.query_task_result(task_id),
                    timeout=min(STATUS_QUERY_TIMEOUT_SECONDS, max(0.001, deadline - time.monotonic())),
                )
                return await self._process_task_result(
                    task_id,
                    result_data,
                    output_id_2_var,
                )

            if task_status == "FAILED":
                message = f"RunningHub task {task_id} failed"
                if status_msg:
                    message += f": {status_msg}"
                logger.error(message)
                return _error_result(task_id, message)

            if task_status not in {"QUEUED", "RUNNING"}:
                logger.warning(
                    f"RunningHub task {task_id} returned unknown status: {task_status}"
                )
            else:
                logger.info(f"Task {task_id} status: {task_status}, waiting...")

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if _is_terminal_error(exc):
                message = f"RunningHub task {task_id} cannot be queried: {exc}"
                logger.error(message)
                return _error_result(task_id, message)
            logger.warning(f"Transient RunningHub status error for task {task_id}: {exc}")

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            continue
        await asyncio.sleep(min(CHECK_INTERVAL_SECONDS, remaining))


def install_runninghub_guard() -> None:
    if getattr(RunningHubExecutor, "_huangque_poll_guard_installed", False):
        return
    RunningHubExecutor._wait_for_task_completion = guarded_wait_for_task_completion
    RunningHubExecutor._huangque_poll_guard_installed = True
