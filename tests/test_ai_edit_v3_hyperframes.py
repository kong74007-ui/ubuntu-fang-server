from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest

from server.content_domains.ai_edit_v3.renderers import RenderRequest
from server.content_domains.ai_edit_v3.renderers.hyperframes import HyperframesRenderer, HyperframesRendererError


class _Runner:
    def __init__(self, spool: Path) -> None:
        self.spool = spool
        self.calls = []
        self.queries = ["queued", "running", "succeeded"]

    def run(self, argv, *, timeout_seconds, environment):
        self.calls.append((tuple(str(item) for item in argv), dict(environment)))
        action, instance = argv[-2], argv[-1]
        if action == "query":
            state = self.queries.pop(0)
            if state == "succeeded":
                result = self.spool / "results" / instance
                (result / "snapshots").mkdir(parents=True, exist_ok=True)
                video = result / "silent.mp4"
                video.write_bytes(b"video")
                (result / "snapshots/frame.png").write_bytes(b"png")
                report = {
                    "status": "done",
                    "output": {"path": "silent.mp4", "size_bytes": 5, "sha256": hashlib.sha256(b"video").hexdigest(), "silent": True},
                    "snapshots": [{"path": "frame.png", "size_bytes": 3, "sha256": hashlib.sha256(b"png").hexdigest()}],
                    "performance": {"elapsed_ms": 10},
                }
                (result / "silent.report.json").write_text(json.dumps(report), encoding="utf-8")
            payload = {"state": state, "result_ready": state == "succeeded", "exit_status": 0, "error_code": None}
        else:
            payload = {"state": "running", "result_ready": False, "exit_status": None, "error_code": None}
        return type("Result", (), {"returncode": 0, "stdout": json.dumps(payload).encode(), "stderr": b"", "elapsed_ms": 1})()


class _ScriptedRunner:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def run(self, argv, *, timeout_seconds, environment):
        action = str(argv[-2])
        self.calls.append(action)
        expected_action, returncode, payload = self.responses.pop(0)
        if action != expected_action:
            raise AssertionError((action, expected_action))
        return type(
            "Result",
            (),
            {
                "returncode": returncode,
                "stdout": json.dumps(payload).encode(),
                "stderr": b"transient control failure" if returncode else b"",
                "elapsed_ms": 1,
            },
        )()


class HyperframesRendererTests(unittest.TestCase):
    def test_spools_fixed_request_polls_and_verifies_result(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            input_root = root / "input"
            output_root = root / "output"
            input_root.mkdir()
            output_root.mkdir()
            media = input_root / "media/source.mp4"
            media.parent.mkdir()
            media.write_bytes(b"video")
            digest = hashlib.sha256(b"video").hexdigest()
            manifest = {
                "version": "1.0", "renderer_environment": {"renderer_build_id": "sha256:" + "1" * 64, "node_version": "v22.14.0"},
                "source_video": {"path": "media/source.mp4", "sha256": digest, "size_bytes": 5},
                "master_audio": None, "assets": [],
            }
            manifest_path = input_root / "render-manifest.json"
            manifest_path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")), encoding="utf-8")
            spool = root / "spool"
            runner = _Runner(spool)
            renderer = HyperframesRenderer(
                renderctl_path=Path("/usr/local/libexec/huangque-ai-edit-v3-renderctl"),
                spool_root=spool,
                renderer_build_id="sha256:" + "1" * 64,
                registry_sha256="sha256:" + "2" * 64,
                schema_sha256="3" * 64,
                command_runner=runner,
                clock=time.time,
                sleeper=lambda _seconds: None,
            )
            request = RenderRequest("job_1", "job-1", 1, manifest_path, input_root, output_root, hashlib.sha256(manifest_path.read_bytes()).hexdigest(), "sha256:" + "1" * 64, time.time() + 30)
            result = renderer.render(request)
            self.assertEqual(result.sha256, hashlib.sha256(b"video").hexdigest())
            self.assertTrue((output_root / "silent.mp4").is_file())
            self.assertEqual([call[0][1] for call in runner.calls], ["start", "query", "query", "query"])
            self.assertTrue(all(call[1] == {"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TZ": "UTC"} for call in runner.calls))

    def test_deadline_or_lease_loss_stops_fixed_instance_once(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            helper = Path("/usr/local/libexec/huangque-ai-edit-v3-renderctl")
            renderer = HyperframesRenderer(renderctl_path=helper, spool_root=root / "spool", renderer_build_id="sha256:" + "1" * 64, registry_sha256="sha256:" + "2" * 64, schema_sha256="3" * 64, command_runner=_Runner(root / "spool"), clock=lambda: 100.0, sleeper=lambda _seconds: None)
            with self.assertRaisesRegex(HyperframesRendererError, "render_deadline_exceeded"):
                renderer._poll("job_1", 99.0, lambda: None)
            self.assertEqual(renderer._command_runner.calls[-1][0][1], "stop")

    def test_start_retries_same_instance_after_unaccepted_transient_control_failure(self):
        failed = {"state": "failed", "result_ready": False, "exit_status": None, "error_code": "render_status_failed"}
        running = {"state": "running", "result_ready": False, "exit_status": None, "error_code": None}
        runner = _ScriptedRunner(
            [
                ("start", 1, failed),
                ("query", 0, failed),
                ("start", 0, running),
            ]
        )
        renderer = HyperframesRenderer(
            renderctl_path=Path("/usr/local/libexec/huangque-ai-edit-v3-renderctl"),
            spool_root=Path("/var/spool/huangque-ai-edit-v3"),
            renderer_build_id="sha256:" + "1" * 64,
            registry_sha256="sha256:" + "2" * 64,
            schema_sha256="3" * 64,
            command_runner=runner,
            sleeper=lambda _seconds: None,
        )

        renderer._start_with_reconciliation("job_1")

        self.assertEqual(["start", "query", "start"], runner.calls)

    def test_start_does_not_resubmit_when_query_confirms_first_attempt(self):
        failed = {"state": "failed", "result_ready": False, "exit_status": None, "error_code": "render_control_failed"}
        running = {"state": "running", "result_ready": False, "exit_status": None, "error_code": None}
        runner = _ScriptedRunner(
            [
                ("start", 1, failed),
                ("query", 0, running),
            ]
        )
        renderer = HyperframesRenderer(
            renderctl_path=Path("/usr/local/libexec/huangque-ai-edit-v3-renderctl"),
            spool_root=Path("/var/spool/huangque-ai-edit-v3"),
            renderer_build_id="sha256:" + "1" * 64,
            registry_sha256="sha256:" + "2" * 64,
            schema_sha256="3" * 64,
            command_runner=runner,
            sleeper=lambda _seconds: None,
        )

        renderer._start_with_reconciliation("job_1")

        self.assertEqual(["start", "query"], runner.calls)


if __name__ == "__main__":
    unittest.main()
