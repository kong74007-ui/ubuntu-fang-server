from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class FakeExecuteResult:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class FakeLogger:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


def load_guard_module():
    comfykit = types.ModuleType("comfykit")
    comfyui = types.ModuleType("comfykit.comfyui")
    models = types.ModuleType("comfykit.comfyui.models")
    models.ExecuteResult = FakeExecuteResult
    executor_module = types.ModuleType("comfykit.comfyui.runninghub_executor")

    class FakeRunningHubExecutor:
        async def _wait_for_task_completion(self, *_args, **_kwargs):
            raise AssertionError("original waiter should be replaced")

    executor_module.RunningHubExecutor = FakeRunningHubExecutor
    logger_module = types.ModuleType("comfykit.logger")
    logger_module.logger = FakeLogger()

    module_names = {
        "comfykit": comfykit,
        "comfykit.comfyui": comfyui,
        "comfykit.comfyui.models": models,
        "comfykit.comfyui.runninghub_executor": executor_module,
        "comfykit.logger": logger_module,
    }
    previous = {name: sys.modules.get(name) for name in module_names}
    sys.modules.update(module_names)
    try:
        path = (
            ROOT
            / "deploy"
            / "pixelle-video"
            / "overrides"
            / "pixelle_video"
            / "services"
            / "runninghub_guard.py"
        )
        spec = importlib.util.spec_from_file_location("pixelle_runninghub_guard", path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module, FakeRunningHubExecutor
    finally:
        for name, value in previous.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


class PixelleRunningHubGuardTests(unittest.IsolatedAsyncioTestCase):
    def test_task_timeout_is_always_capped(self):
        guard, _ = load_guard_module()

        self.assertEqual(900, guard._resolve_task_timeout(None, None))
        self.assertEqual(300, guard._resolve_task_timeout(300, None))
        self.assertEqual(900, guard._resolve_task_timeout(1800, None))
        self.assertEqual(120, guard._resolve_task_timeout(1800, 120))

    async def test_task_not_found_is_terminal_after_one_status_query(self):
        guard, _ = load_guard_module()

        class Client:
            calls = 0

            async def query_task_status(self, _task_id):
                self.calls += 1
                raise RuntimeError("RunningHub API error: APIKEY_TASK_NOT_FOUND")

        executor = types.SimpleNamespace(client=Client(), timeout=None)
        result = await guard.guarded_wait_for_task_completion(executor, "missing-task")

        self.assertEqual("error", result.status)
        self.assertEqual("missing-task", result.prompt_id)
        self.assertIn("APIKEY_TASK_NOT_FOUND", result.msg)
        self.assertEqual(1, executor.client.calls)

    async def test_transient_status_error_can_recover(self):
        guard, _ = load_guard_module()
        statuses = [RuntimeError("HTTP 521"), {"status": "SUCCESS", "msg": ""}]

        class Client:
            async def query_task_status(self, _task_id):
                value = statuses.pop(0)
                if isinstance(value, Exception):
                    raise value
                return value

            async def query_task_result(self, _task_id):
                return [{"fileUrl": "https://example.invalid/image.png"}]

        async def process_result(task_id, result_data, output_mapping):
            return (task_id, result_data, output_mapping)

        executor = types.SimpleNamespace(
            client=Client(),
            timeout=None,
            _process_task_result=process_result,
        )

        original_sleep = guard.asyncio.sleep

        async def no_sleep(_delay):
            return None

        guard.asyncio.sleep = no_sleep
        try:
            result = await guard.guarded_wait_for_task_completion(
                executor,
                "recovering-task",
                {"1": "image"},
            )
        finally:
            guard.asyncio.sleep = original_sleep

        self.assertEqual("recovering-task", result[0])
        self.assertEqual([], statuses)

    async def test_cancellation_is_never_converted_to_retry(self):
        guard, _ = load_guard_module()

        class Client:
            calls = 0

            async def query_task_status(self, _task_id):
                self.calls += 1
                raise asyncio.CancelledError()

        executor = types.SimpleNamespace(client=Client(), timeout=None)
        with self.assertRaises(asyncio.CancelledError):
            await guard.guarded_wait_for_task_completion(executor, "cancelled-task")

        self.assertEqual(1, executor.client.calls)

    async def test_hung_status_request_is_bounded_by_total_timeout(self):
        guard, _ = load_guard_module()

        class Client:
            async def query_task_status(self, _task_id):
                await asyncio.Event().wait()

        executor = types.SimpleNamespace(client=Client(), timeout=None)
        result = await asyncio.wait_for(
            guard.guarded_wait_for_task_completion(
                executor,
                "hung-task",
                max_wait_time=0.01,
            ),
            timeout=0.2,
        )

        self.assertEqual("error", result.status)
        self.assertIn("timeout", result.msg.lower())

    def test_install_replaces_comfykit_waiter(self):
        guard, executor_class = load_guard_module()

        guard.install_runninghub_guard()

        self.assertIs(
            guard.guarded_wait_for_task_completion,
            executor_class._wait_for_task_completion,
        )


if __name__ == "__main__":
    unittest.main()
