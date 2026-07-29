import importlib
import copy
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from server.content_domains import ai_edit_v2_billing as billing
from server.content_domains import ai_edit_v2_pipeline as pipeline
from server.content_domains import ai_edit_v2_runtime as runtime
from server.content_domains import ai_edit_v2_store as store
from tests.test_ai_edit_v2_director import VALID_PLAN
from tests.test_ai_edit_v2_openai_image import png_bytes


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "ai_edit_v2" / "e2e"
SMOKE_SCRIPT = ROOT / "scripts" / "ai_edit_v2_provider_smoke.py"


class _SimulatedProcessExit(BaseException):
    pass


def run_fixture(name, *, restart_after_submit=False):
    fixture = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return _execute_fixture(fixture, restart_after_submit=restart_after_submit)


class _FakePoints:
    def __init__(self, balance=1_000):
        self.balance = balance
        self.transactions = {}
        self.calls = []

    def deduct_points(self, username, amount, reason="", transaction_key=None):
        self.calls.append(("deduct", username, amount, transaction_key))
        if transaction_key not in self.transactions:
            self.balance -= amount
            self.transactions[transaction_key] = self.balance
        return self.transactions[transaction_key]

    def refund_points(self, username, amount, reason="", transaction_key=None):
        self.calls.append(("refund", username, amount, transaction_key))
        if transaction_key not in self.transactions:
            self.balance += amount
            self.transactions[transaction_key] = self.balance
        return self.transactions[transaction_key]


class _FakeCos:
    def __init__(self, source_key, source_bytes, counters):
        self.objects = {source_key: source_bytes}
        self.types = {source_key: "application/octet-stream"}
        self.counters = counters

    def enabled(self):
        return True

    def download_file(self, key, destination):
        Path(destination).write_bytes(self.objects[key])
        return str(destination)

    def put_bytes(self, content, key, content_type, private=True):
        self.objects[key] = bytes(content)
        self.types[key] = content_type
        return {"ETag": self._etag(key)}

    def put_file(self, source, key, content_type, private=True):
        self.objects[key] = Path(source).read_bytes()
        self.types[key] = content_type
        if key.endswith("/delivery/final.mp4"):
            self.counters["cos-output"] += 1
        return {"ETag": self._etag(key)}

    def head_object(self, key):
        if key not in self.objects:
            raise KeyError(key)
        return {
            "content_length": len(self.objects[key]),
            "etag": self._etag(key),
            "content_type": self.types[key],
        }

    def presign_get(self, key, expires=300):
        return "https://private.invalid/" + key

    def _etag(self, key):
        return "etag-" + str(len(self.objects[key]))


def _edit_plan(fixture):
    plan = copy.deepcopy(VALID_PLAN)
    duration = fixture["duration_ms"]
    plan.update({
        "creation_mode": fixture["creation_mode"],
        "duration_ms": duration,
        "target_duration_ms": duration,
        "aspect_ratio": fixture["aspect_ratio"],
        "style_system": {"component_family": "editorial_business"},
    })
    if fixture["creation_mode"] == "platform_template":
        plan["style_system"].update({
            "template_id": fixture["template_id"],
            "template_version": fixture["template_version"],
        })
    plan["scenes"] = [
        {
            "id": "scene_01", "start_ms": 0, "end_ms": 2_000,
            "intent": "保留口播事实", "layout": "speaker_focus",
            "visual_type": "talking_head", "headline": "品牌价格信息",
            "material_slots": [], "transition": "cut",
        },
        {
            "id": "scene_02", "start_ms": 2_000, "end_ms": duration,
            "intent": "重点补充视觉信息", "layout": "speaker_product_split",
            "visual_type": "b_roll", "headline": "重点视觉",
            "material_slots": ["slot_1"], "transition": "dissolve",
        },
    ]
    plan["audio_plan"] = {
        "speech_policy": "preserve_source",
        "music_policy": "duck_under_speech",
        "sfx_policy": "semantic_only",
    }
    return plan


def _execute_fixture(fixture, *, restart_after_submit=False):
    counters = {name: 0 for name in (
        "dashscope-asr", "dashscope-qwen", "openai-image",
        "elevenlabs-music", "elevenlabs-sfx", "shotstack", "cos-output",
    )}
    with tempfile.TemporaryDirectory(prefix="ai-edit-v2-e2e-") as directory:
        db_path = str(Path(directory) / "v2.db")
        pricing_path = str(Path(directory) / "pricing.db")
        assets_path = str(Path(directory) / "assets.db")
        with closing(sqlite3.connect(assets_path)) as conn:
            conn.execute("""CREATE TABLE video_assets(
                id INTEGER PRIMARY KEY AUTOINCREMENT,job_id TEXT UNIQUE,
                username TEXT NOT NULL,mode TEXT NOT NULL,video_file TEXT,
                video_url TEXT,resolution TEXT,ratio TEXT,phase TEXT,status TEXT NOT NULL,
                created_at INTEGER,updated_at INTEGER)""")
        store.init_db(db_path)
        job_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "ai-edit-v2-e2e:" + fixture["name"]))
        suffix = "m4a" if fixture["main_kind"] == "audio" else "mp4"
        source_key = f"ai-edit-v2/source/{job_id}.{suffix}"
        fake_cos = _FakeCos(source_key, b"fixture-source-media", counters)
        points = _FakePoints()
        starting_balance = points.balance
        draft = {
            "creation_mode": fixture["creation_mode"],
            "brief": "稳定版假 Provider 全链路验证",
            "language": "zh-CN",
            "aspect_ratio": fixture["aspect_ratio"],
            "target_duration_ms": fixture["duration_ms"],
            "main_input": {
                "asset_id": "main", "kind": fixture["main_kind"],
                "size_bytes": 10_000, "duration_ms": fixture["duration_ms"],
                "cos_key": source_key,
            },
            "required_materials": [],
            "reference_materials": [],
            "original_text": fixture["original_text"],
            "input_mode": fixture["source_type"],
        }
        if fixture["creation_mode"] == "platform_template":
            draft.update(template_id=fixture["template_id"], template_version=fixture["template_version"])
        quote = billing.create_quote(
            fixture["owner"], draft, 100,
            uuid_factory=lambda: "quote-" + fixture["name"],
            db_path=db_path, pricing_db_path=pricing_path,
        )
        created = billing.precharge_and_create_job(
            fixture["owner"], {"draft": draft}, quote["id"],
            "request-" + fixture["name"], 101,
            points_client=points, uuid_factory=lambda: job_id, db_path=db_path,
        )

        transcript = json.loads(
            (ROOT / "tests/fixtures/ai_edit_v2/provider_responses/fun_asr_success.json")
            .read_text(encoding="utf-8")
        )
        transcript["properties"]["original_duration_in_milliseconds"] = fixture["duration_ms"]
        plan = _edit_plan(fixture)

        crash_state = {"pending": restart_after_submit, "observed": False}

        def dashscope_http(method, url, _headers, _body, _timeout):
            if "text-generation" in url:
                counters["dashscope-qwen"] += 1
                return {
                    "request_id": "qwen-request-1",
                    "output": {"choices": [{"message": {
                        "role": "assistant", "content": json.dumps(plan, ensure_ascii=False)
                    }}]},
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                }
            if method == "POST":
                counters["dashscope-asr"] += 1
                return {"request_id": "asr-submit-1", "output": {
                    "task_id": "asr-task-1", "task_status": "PENDING"
                }}
            if url.endswith("/tasks/asr-task-1"):
                if crash_state["pending"]:
                    crash_state.update(pending=False, observed=True)
                    raise _SimulatedProcessExit()
                return {"request_id": "asr-query-1", "output": {
                    "task_id": "asr-task-1", "task_status": "SUCCEEDED",
                    "results": [{"subtask_status": "SUCCEEDED",
                                 "transcription_url": "https://result.invalid/asr.json"}],
                }}
            return transcript

        def openai_http(*_args):
            counters["openai-image"] += 1
            return {
                "id": "image-request-1",
                "data": [{"url": "https://image.invalid/generated.png"}],
                "usage": {"total_tokens": 1},
            }

        image_size = (1536, 1024) if fixture["aspect_ratio"] == "16:9" else (1024, 1536)

        def openai_download(*_args):
            return {"content": png_bytes(*image_size), "content_type": "image/png"}

        def elevenlabs_http(_method, url, _headers, _body, _timeout):
            capability = "elevenlabs-music" if url.endswith("/v1/music") else "elevenlabs-sfx"
            counters[capability] += 1
            return {
                "content": b"ID3-fake-audio", "content_type": "audio/mpeg",
                "headers": {"request-id": capability + "-1", "character-cost": "1", "song-id": "song-1"},
            }

        def shotstack_http(method, _url, _headers, _body, _timeout):
            if method == "POST":
                counters["shotstack"] += 1
            return {"success": True, "request_id": "shotstack-request-1", "response": {
                "id": "render-task-1", "status": "done",
                "url": "https://render.invalid/final.mp4",
            }}

        def runner(command, **_kwargs):
            if command[0] == "ffprobe":
                target = str(command[-1]).lower()
                normalized = "normalized" in target
                video = fixture["main_kind"] == "video" or "final" in target
                streams = []
                if video:
                    streams.append({
                        "codec_type": "video", "codec_name": "h264" if normalized else "hevc",
                        "width": 1920 if fixture["aspect_ratio"] == "16:9" else 1080,
                        "height": 1080 if fixture["aspect_ratio"] == "16:9" else 1920,
                        "r_frame_rate": "30/1", "tags": {"rotate": "0"},
                    })
                streams.append({
                    "codec_type": "audio", "codec_name": "aac" if normalized else "mp3",
                    "sample_rate": "48000", "channels": 2,
                })
                payload = {"format": {"duration": "3.0", "format_name": "mov,mp4"}, "streams": streams}
                return type("Completed", (), {
                    "returncode": 0, "stdout": json.dumps(payload), "stderr": b"",
                })()
            if command[-1] not in {"NUL", "-"}:
                Path(command[-1]).write_bytes(b"fake-media-output")
            loudness = b'{"input_i":"-20","input_tp":"-2","input_lra":"5","input_thresh":"-30","target_offset":"0"}'
            return type("Completed", (), {"returncode": 0, "stdout": b"", "stderr": loudness})()

        quality_calls = []

        def quality_analyzer(check, *, path, expected):
            quality_calls.append(check)
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
        def build_runtime():
            services = runtime.ProductionServices(
                db_path, cos_api=fake_cos, runner=runner,
                dashscope_http=dashscope_http, shotstack_http=shotstack_http,
                openai_http=openai_http, openai_downloader=openai_download,
                elevenlabs_http=elevenlabs_http,
                downloader=lambda _url: b"fake-rendered-mp4",
                repair_handler=lambda *_args: {}, repair_reconciler=lambda *_args: {},
                quality_analyzer=quality_analyzer,
            )
            dependencies = runtime.production_dependencies(db_path, services=services)
            dependencies.update({
                "points_client": points,
                "asset_db_path": assets_path,
                "actual_cost": fixture["actual_points"],
                "now": lambda: int(time.time()),
            })
            return services, dependencies

        services, dependencies = build_runtime()
        env = {
            "DASHSCOPE_API_KEY": "test-placeholder",
            "SHOTSTACK_API_KEY": "test-placeholder",
            "OPENAI_API_KEY": "test-placeholder",
            "AI_EDIT_V2_OPENAI_IMAGE_IDEMPOTENCY_ACCEPTED": "1",
            "ELEVENLABS_API_KEY": "test-placeholder",
            "AI_EDIT_V2_SHOTSTACK_CALLBACK_URL": "https://callback.invalid/v2",
            "AI_EDIT_V2_WEBHOOK_SECRET": "test-placeholder",
        }
        with patch.dict(os.environ, env, clear=False):
            restart_checkpoint = None
            if restart_after_submit:
                try:
                    pipeline.run_job(created["job"]["id"], dependencies, db_path=db_path)
                except _SimulatedProcessExit:
                    pass
                else:
                    raise AssertionError("simulated process exit was not reached")
                with closing(store.open_store(db_path)) as conn:
                    restart_checkpoint = dict(conn.execute(
                        "SELECT stage,status,provider_task_id FROM edit_v2_pipeline_checkpoints WHERE job_id=? AND stage='transcribing'",
                        (job_id,),
                    ).fetchone())
                    conn.execute(
                        "UPDATE edit_v2_jobs SET lease_owner=NULL,lease_until=NULL WHERE id=?",
                        (job_id,),
                    )
                previous_services = services
                services, dependencies = build_runtime()
                if services is previous_services:
                    raise AssertionError("runtime services were not rebuilt")
            result = pipeline.run_job(created["job"]["id"], dependencies, db_path=db_path)
            replay = pipeline.run_job(created["job"]["id"], dependencies, db_path=db_path)

        with closing(store.open_store(db_path)) as conn:
            job = dict(conn.execute("SELECT * FROM edit_v2_jobs WHERE id=?", (job_id,)).fetchone())
            bill = dict(conn.execute(
                "SELECT * FROM edit_v2_billing WHERE job_id=? AND operation='hold'", (job_id,)
            ).fetchone())
            checkpoint_stages = [row[0] for row in conn.execute(
                "SELECT stage FROM edit_v2_pipeline_checkpoints WHERE job_id=? ORDER BY id", (job_id,)
            ).fetchall()]
            aligning_output = json.loads(conn.execute(
                "SELECT output_json FROM edit_v2_pipeline_checkpoints WHERE job_id=? AND stage='aligning'",
                (job_id,),
            ).fetchone()[0])
        with closing(sqlite3.connect(assets_path)) as conn:
            conn.row_factory = sqlite3.Row
            asset_row = conn.execute("SELECT * FROM video_assets WHERE job_id=?", (job_id,)).fetchone()
            if asset_row is None:
                raise AssertionError({"result": result, "job": job, "counters": counters})
            asset = dict(asset_row)
        settlement = json.loads(bill["response_json"])
        if checkpoint_stages != list(runtime.STABLE_STAGE_SEQUENCE):
            raise AssertionError(checkpoint_stages)
        if quality_calls != ["captions", "materials", "transcript", "audio"]:
            raise AssertionError(quality_calls)
        if replay["state"] != "completed":
            raise AssertionError(replay)
        if len([call for call in points.calls if call[0] == "deduct"]) != 1:
            raise AssertionError(points.calls)
        if len([call for call in points.calls if call[0] == "refund"]) != 1:
            raise AssertionError(points.calls)
        return {
            "state": result["state"],
            "owner": job["owner"],
            "asset_owner": asset["username"],
            "asset_status": asset["status"],
            "stages": [
                "quote", "hold", "job", "normalizing", "transcribing", "aligning",
                "directing", "resolving_materials", "openai-image",
                "elevenlabs-music", "elevenlabs-sfx", "shotstack", "quality",
                "cos", "settlement", "video_asset",
            ],
            "external_charge_counts": counters,
            "held_points": settlement["held_points"],
            "actual_points": settlement["actual_points"],
            "refunded_points": settlement["refunded_points"],
            "starting_balance": starting_balance,
            "ending_balance": points.balance,
            "timeline_source_type": aligning_output["text_timeline"]["source_type"],
            "timeline_text": aligning_output["text_timeline"]["text"],
            "expected_timeline_text": fixture["original_text"] if fixture["source_type"] == "platform_video" else "".join(item["text"] for item in transcript["transcripts"]),
            "restart_checkpoint": restart_checkpoint,
            "deduct_calls": len([call for call in points.calls if call[0] == "deduct"]),
            "refund_calls": len([call for call in points.calls if call[0] == "refund"]),
        }


class FakeProviderE2ETests(unittest.TestCase):
    def assert_run_fixture(self, name, expected="completed"):
        result = run_fixture(name)
        self.assertEqual(result["state"], expected, result)
        self.assertEqual(result["owner"], result["asset_owner"])
        self.assertEqual(result["asset_status"], "done")
        self.assertEqual(
            result["stages"],
            [
                "quote", "hold", "job", "normalizing", "transcribing",
                "aligning", "directing", "resolving_materials",
                "openai-image", "elevenlabs-music", "elevenlabs-sfx",
                "shotstack", "quality", "cos", "settlement", "video_asset",
            ],
        )
        self.assertEqual(result["external_charge_counts"], {
            "dashscope-asr": 1,
            "dashscope-qwen": 1,
            "openai-image": 1,
            "elevenlabs-music": 1,
            "elevenlabs-sfx": 1,
            "shotstack": 1,
            "cos-output": 1,
        })
        self.assertEqual(result["held_points"] - result["actual_points"], result["refunded_points"])
        self.assertEqual(result["ending_balance"], result["starting_balance"] - result["actual_points"])
        fixture = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
        self.assertEqual(result["timeline_source_type"], fixture["source_type"])
        self.assertEqual(result["timeline_text"], result["expected_timeline_text"])

    def test_platform_video_e2e(self):
        self.assert_run_fixture("platform_video")

    def test_external_video_e2e(self):
        self.assert_run_fixture("external_video")

    def test_audio_only_e2e(self):
        self.assert_run_fixture("audio_only")

    def test_restart_after_provider_identity_reconciles_without_duplicate_charges(self):
        result = run_fixture("external_video", restart_after_submit=True)
        self.assertEqual(result["state"], "completed")
        self.assertTrue(result["restart_checkpoint"])
        self.assertEqual(result["restart_checkpoint"]["stage"], "transcribing")
        self.assertEqual(result["restart_checkpoint"]["status"], "running")
        self.assertEqual(result["restart_checkpoint"]["provider_task_id"], "asr-task-1")
        self.assertEqual(result["deduct_calls"], 1)
        self.assertEqual(result["refund_calls"], 1)
        self.assertEqual(result["external_charge_counts"], {
            "dashscope-asr": 1, "dashscope-qwen": 1, "openai-image": 1,
            "elevenlabs-music": 1, "elevenlabs-sfx": 1, "shotstack": 1,
            "cos-output": 1,
        })


class ProviderSmokeCLITests(unittest.TestCase):
    PROVIDERS = (
        "dashscope-asr", "dashscope-qwen", "openai-image",
        "elevenlabs-music", "elevenlabs-sfx", "shotstack", "cos",
    )

    def _module(self):
        return importlib.import_module("scripts.ai_edit_v2_provider_smoke")

    def test_elevenlabs_smoke_uses_valid_uuid_job_scope(self):
        smoke = self._module()
        from server.content_domains.ai_edit_v2_providers.elevenlabs import (
            ElevenLabsProvider,
        )

        with tempfile.TemporaryDirectory() as directory:
            provider = ElevenLabsProvider(
                owner="provider-smoke",
                job_id=smoke.SMOKE_JOB_ID,
                db_path=str(Path(directory) / "provider.db"),
                api_key="test-placeholder",
            )
        self.assertEqual(provider.job_id, smoke.SMOKE_JOB_ID)

    def test_provider_import_falls_back_to_deployed_server_layout(self):
        smoke = self._module()
        deployed_module = object()
        missing_server_package = ModuleNotFoundError(
            "No module named 'server'", name="server"
        )

        with patch.object(
            smoke.importlib,
            "import_module",
            side_effect=[missing_server_package, deployed_module],
        ) as importer:
            result = smoke._import_provider_module(
                "content_domains.ai_edit_v2_providers.elevenlabs"
            )

        self.assertIs(result, deployed_module)
        self.assertEqual(
            importer.call_args_list,
            [
                unittest.mock.call(
                    "server.content_domains.ai_edit_v2_providers.elevenlabs"
                ),
                unittest.mock.call(
                    "content_domains.ai_edit_v2_providers.elevenlabs"
                ),
            ],
        )

    def test_shotstack_smoke_reports_provider_task_id_instead_of_created_message(self):
        smoke = self._module()

        class Result:
            request_id = "Created"
            payload = {
                "provider_task_id": "22222222-2222-4222-8222-222222222222"
            }

        self.assertEqual(
            smoke._request_id(Result()),
            "22222222-2222-4222-8222-222222222222",
        )

    def test_no_provider_never_calls_network_and_has_stable_usage_exit(self):
        completed = subprocess.run(
            [sys.executable, str(SMOKE_SCRIPT)],
            cwd=ROOT,
            env={"PATH": os.environ.get("PATH", "")},
            capture_output=True,
            text=True,
            timeout=5,
        )
        self.assertEqual(completed.returncode, 2)
        self.assertEqual(completed.stdout, "")

    def test_each_provider_fails_closed_when_environment_is_not_ready(self):
        smoke = self._module()
        for provider in self.PROVIDERS:
            with self.subTest(provider=provider):
                result = smoke.run_smoke(
                    provider,
                    environ={},
                    timeout_seconds=0.05,
                )
                self.assertEqual(result.exit_code, 3)
                self.assertEqual(result.stage, "not_ready")

    def test_success_output_contains_only_stage_and_redacted_request_id(self):
        smoke = self._module()
        child = "import json; print('AI_EDIT_V2_SMOKE_RESULT='+json.dumps({'request_id':'req-sensitive-prefix-12345678'}))"
        result = smoke.run_smoke(
            "dashscope-qwen",
            environ={"DASHSCOPE_API_KEY": "test-placeholder"},
            command=[sys.executable, "-c", child],
            timeout_seconds=1,
        )
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(smoke.format_result(result), "stage=completed request_id=...5678")

    def test_child_stdout_and_stderr_are_never_forwarded(self):
        smoke = self._module()
        child = "import json,sys; print('Authorization: canary-secret'); print('signed=canary-secret',file=sys.stderr); print('AI_EDIT_V2_SMOKE_RESULT='+json.dumps({'request_id':'safe-request-12345678'}))"
        result = smoke.run_smoke(
            "dashscope-qwen", environ={"DASHSCOPE_API_KEY": "test-placeholder"},
            command=[sys.executable, "-c", child], timeout_seconds=1,
        )
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(smoke.format_result(result), "stage=completed request_id=...5678")

    def test_timeout_has_stable_exit_and_does_not_leak_operation_result(self):
        smoke = self._module()

        started = time.monotonic()
        result = smoke.run_smoke(
            "dashscope-asr",
            environ={
                "DASHSCOPE_API_KEY": "test-placeholder",
                "AI_EDIT_V2_SMOKE_ASR_URL": "https://input.invalid/audio.m4a",
            },
            command=[sys.executable, "-c", "import time; print('canary-secret'); time.sleep(10)"],
            timeout_seconds=0.01,
        )
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(result.exit_code, 4)
        self.assertEqual(smoke.format_result(result), "stage=timeout request_id=none")


if __name__ == "__main__":
    unittest.main()
