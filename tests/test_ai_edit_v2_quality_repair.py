import json
import os
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from server.content_domains import ai_edit_v2_billing as billing
from server.content_domains import ai_edit_v2_store as store
from server.content_domains.ai_edit_v2_providers.base import ProviderError
from server.content_domains.ai_edit_v2_quality_repair import (
    DashScopeFinalMediaAnalyzer,
    ProductionRepairProvider,
)
from tests.test_ai_edit_v2_runtime import _resolved_plan


class QualityRepairProviderTests(unittest.TestCase):
    def test_qwen_analyzer_sends_actual_video_and_strictly_validates_evidence(self):
        calls = []

        def transport(method, url, headers, body, timeout):
            calls.append((method, url, headers, json.loads(body), timeout))
            return {"output": {"choices": [{"message": {"content": json.dumps({
                "safe_area": True, "tofu_count": 0, "missing_glyphs": [],
            })}}]}}

        analyzer = DashScopeFinalMediaAnalyzer(
            cos_api=object(), video_url=lambda _path: "https://cos.example/final.mp4",
            http_request=transport, binary_finder=lambda name: name,
        )
        value = analyzer("captions", path="final.mp4", expected={
            "caption_plan": {}, "text_timeline": {"text": "verified"},
        })

        self.assertEqual(value, {
            "safe_area": True, "tofu_count": 0, "missing_glyphs": [],
        })
        self.assertTrue(calls[0][1].endswith("/services/aigc/multimodal-generation/generation"))
        content = calls[0][3]["input"]["messages"][0]["content"]
        self.assertEqual(content[0], {"video": "https://cos.example/final.mp4"})

        analyzer.http_request = lambda *_args: {
            "output": {"choices": [{"message": {"content": '{"safe_area":true}'}}]}
        }
        with self.assertRaisesRegex(ProviderError, "quality_qwen_evidence_invalid"):
            analyzer("captions", path="final.mp4", expected={})

    def test_audio_analyzer_measures_master_and_sidechain_processed_sources(self):
        class Result:
            def __init__(self, *, stdout=b"", stderr=b""):
                self.returncode, self.stdout, self.stderr = 0, stdout, stderr

        commands = []

        def runner(command, **_kwargs):
            commands.append(command)
            if command[0] == "ffprobe":
                return Result(stdout=b"10.0\n")
            if "silencedetect=n=-50dB:d=0.2,ebur128=peak=true" in command:
                return Result(stderr=b"silence_duration: 1.0\nPeak: -1.5 dBFS\n")
            if "-filter_complex" in command:
                return Result(stderr=b"I: -24.0 LUFS\n")
            return Result(stderr=b"I: -16.0 LUFS\n")

        class Cos:
            @staticmethod
            def download_file(_key, path): Path(path).write_bytes(b"audio")

        analyzer = DashScopeFinalMediaAnalyzer(
            cos_api=Cos(), video_url=lambda _path: "https://cos.example/final.mp4",
            http_request=lambda *_args: {}, process_runner=runner,
            binary_finder=lambda name: name,
        )
        value = analyzer("audio", path="final.mp4", expected={
            "quality_sources": {
                "primary_media": {"cos_key": "private/dialogue.m4a"},
                "generated_audio": {
                    "bgm": {"cos_key": "private/bgm.mp3"}, "sfx": [],
                },
            },
        })

        self.assertEqual(value, {
            "silence_ratio": 0.1, "true_peak_dbfs": -1.5,
            "dialogue_to_bgm_db": 8.0, "dialogue_to_sfx_db": 200.0,
        })
        sidechain = next(command for command in commands if "-filter_complex" in command)
        self.assertIn(
            "sidechaincompress=threshold=0.03:ratio=8",
            sidechain[sidechain.index("-filter_complex") + 1],
        )

    def test_shotstack_repair_persists_task_identity_and_reconciles_without_resubmit(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = os.path.join(directory, "v2.db")
            store.init_db(db_path)
            draft = {
                "creation_mode": "natural_brief", "brief": "x", "language": "zh-CN",
                "aspect_ratio": "16:9", "target_duration_ms": 1800,
                "input_mode": "external_video",
                "main_input": {"asset_id": "m", "kind": "video", "size_bytes": 1,
                               "duration_ms": 1800},
                "required_materials": [], "reference_materials": [],
            }
            quote = billing.create_quote("alice", draft, 1, db_path=db_path)
            job = store.create_job(
                "alice", {"draft": draft}, quote["id"], "repair-provider", 2,
                uuid_factory=lambda: "job-repair-provider", db_path=db_path,
            )
            attempt_id = store.record_stage_attempt(
                job["id"], "repairing", 1, "running", 2, db_path=db_path,
            )
            with closing(store.open_store(db_path)) as conn:
                conn.execute(
                    """INSERT INTO edit_v2_pipeline_checkpoints(
                           job_id,stage,input_fingerprint,status,output_json,attempt_count,updated_at
                       ) VALUES(?, 'resolving_materials', 'plan', 'completed', ?, 1, 2)""",
                    (job["id"], json.dumps({"resolved_plan": _resolved_plan()})),
                )

            calls = []

            def transport(method, url, _headers, _body, _timeout):
                calls.append((method, url))
                status = "queued" if method == "POST" else "done"
                response = {"id": "repair-task-1", "status": status}
                if status == "done":
                    response["url"] = "https://shotstack.example/repaired.mp4"
                return {"success": True, "response": response, "request_id": "repair-request"}

            class Cos:
                def __init__(self): self.objects = {}
                @staticmethod
                def presign_get(key): return f"https://cos.example/{key}"
                def put_bytes(self, data, key, _content_type, private=True): self.objects[key] = data

            saved = []
            context = {
                "attempt_id": attempt_id,
                "error_codes": ("black_frames_detected",),
                "failing_layers": ("video",),
                "idempotency_key": "ai-edit-v2:job-repair-provider:repair:1",
                "deadline_at": 100,
                "assert_active": lambda: None,
                "save_provider_task_id": saved.append,
            }
            provider = ProductionRepairProvider(
                db_path=db_path, cos_api=Cos(), shotstack_http=transport,
                downloader=lambda _url: b"repaired-video", now_fn=lambda: 10,
                sleep_fn=lambda _seconds: None,
            )
            configured = {
                "SHOTSTACK_API_KEY": "test-shotstack-key",
                "AI_EDIT_V2_SHOTSTACK_CALLBACK_URL": "https://example.test/shotstack",
                "AI_EDIT_V2_WEBHOOK_SECRET": "test-secret",
            }
            with patch.dict(os.environ, configured, clear=True):
                result = provider.submit(job, context)
                replay = provider.reconcile(job, {**context, "provider_task_id": saved[0]})

            self.assertEqual(saved, ["repair-task-1"])
            self.assertEqual(result["provider_task_id"], "repair-task-1")
            self.assertEqual(replay["provider_task_id"], "repair-task-1")
            self.assertEqual(sum(method == "POST" for method, _url in calls), 1)
            self.assertGreaterEqual(sum(method == "GET" for method, _url in calls), 2)

    def test_shotstack_targeted_repair_changes_graph_and_keeps_mixed_local_fix(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = os.path.join(directory, "v2.db")
            store.init_db(db_path)
            draft = {
                "creation_mode": "natural_brief", "brief": "x", "language": "zh-CN",
                "aspect_ratio": "16:9", "target_duration_ms": 1800,
                "input_mode": "external_video",
                "main_input": {"asset_id": "m", "kind": "video", "size_bytes": 1,
                               "duration_ms": 1800},
                "required_materials": [], "reference_materials": [],
            }
            quote = billing.create_quote("alice", draft, 1, db_path=db_path)
            job = store.create_job(
                "alice", {"draft": draft}, quote["id"], "targeted-repair", 2,
                uuid_factory=lambda: "job-targeted-repair", db_path=db_path,
            )
            attempt_id = store.record_stage_attempt(
                job["id"], "repairing", 1, "running", 2, db_path=db_path,
            )
            with closing(store.open_store(db_path)) as conn:
                conn.execute(
                    """INSERT INTO edit_v2_pipeline_checkpoints(
                           job_id,stage,input_fingerprint,status,output_json,attempt_count,updated_at
                       ) VALUES(?, 'resolving_materials', 'plan', 'completed', ?, 1, 2)""",
                    (job["id"], json.dumps({"resolved_plan": _resolved_plan()})),
                )

            submitted_payloads = []

            def transport(method, _url, _headers, body, _timeout):
                if method == "POST":
                    submitted_payloads.append(json.loads(body))
                    return {"success": True, "response": {
                        "id": "repair-task-mixed", "status": "done",
                        "url": "https://shotstack.example/repaired.mp4",
                    }, "request_id": "repair-request-mixed"}
                raise AssertionError("completed submit must not poll")

            class Cos:
                def __init__(self): self.objects = {}
                @staticmethod
                def presign_get(key, expires=300):
                    return f"https://cos.example/{key}?expires={expires}"
                def put_bytes(self, data, key, _content_type, private=True):
                    self.objects[key] = data
                def download_file(self, key, path):
                    Path(path).write_bytes(self.objects[key])
                def put_file(self, path, key, _content_type, private=True):
                    self.objects[key] = Path(path).read_bytes()

            ffmpeg_commands = []

            def process_runner(command, **_kwargs):
                ffmpeg_commands.append(command)
                Path(command[-1]).write_bytes(b"shotstack-then-local")
                return type("Completed", (), {"returncode": 0})()

            context = {
                "attempt_id": attempt_id,
                "error_codes": ("caption_out_of_safe_area", "audio_clipping_detected"),
                "failing_layers": ("captions", "audio"),
                "idempotency_key": "ai-edit-v2:job-targeted-repair:repair:1",
                "deadline_at": 100,
                "assert_active": lambda: None,
                "save_provider_task_id": lambda _value: None,
            }
            provider = ProductionRepairProvider(
                db_path=db_path, cos_api=Cos(), shotstack_http=transport,
                process_runner=process_runner,
                downloader=lambda _url: b"shotstack-output", now_fn=lambda: 10,
                sleep_fn=lambda _seconds: None,
            )
            configured = {
                "SHOTSTACK_API_KEY": "test-shotstack-key",
                "AI_EDIT_V2_SHOTSTACK_CALLBACK_URL": "https://example.test/shotstack",
                "AI_EDIT_V2_WEBHOOK_SECRET": "test-secret",
            }
            with patch.dict(os.environ, configured, clear=True):
                result = provider.submit(job, context)

            clips = [
                clip for track in submitted_payloads[0]["timeline"]["tracks"]
                for clip in track["clips"]
            ]
            caption = next(clip for clip in clips if clip["asset"]["type"] == "rich-text")
            with self.subTest("caption graph is deterministically corrected"):
                self.assertEqual(caption["asset"]["font"]["size"], 44)
                self.assertEqual((caption["width"], caption["height"]), (1440, 180))
            with self.subTest("local defect is retained after Shotstack repair"):
                self.assertEqual(len(ffmpeg_commands), 1)
                self.assertIn("loudnorm=I=-16:TP=-1.5:LRA=11", ffmpeg_commands[0])
                self.assertEqual(Path(result["output_path"]).read_bytes(), b"shotstack-then-local")


if __name__ == "__main__":
    unittest.main()
