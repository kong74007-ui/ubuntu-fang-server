import unittest
import tempfile
import json
import hashlib
import os
import threading
import time
from pathlib import Path
from unittest.mock import patch

from scripts.ai_edit_v3_acceptance import (
    HttpCaseApi,
    HttpRealRunApi,
    RealRunConfig,
    RealRunUnavailable,
    load_authorized_bindings,
    execute_preflighted_cases,
    _refresh_acceptance_aggregate,
    main,
    run_real_acceptance,
)

from server.content_domains.ai_edit_v3.acceptance_export import (
    AcceptanceConfig,
    CaseCheckpoint,
    collect_case_evidence,
    load_test_session,
    RunManifest,
    run_cases,
    resume_or_create_case,
    terminal_result_code,
    verify_case_evidence,
    write_json_exclusive,
)
from server.content_domains.ai_edit_v3.contracts import normalize_job_request, request_fingerprint


class FakeV3Api:
    def __init__(self) -> None:
        self.created_idempotency_keys: list[str] = []
        self.fetched_job_ids: list[str] = []

    def create_job(self, idempotency_key: str) -> str:
        self.created_idempotency_keys.append(idempotency_key)
        return "unexpected-new-job"

    def get_job(self, job_id: str) -> dict[str, str]:
        self.fetched_job_ids.append(job_id)
        return {"job_id": job_id, "status": "rendering"}


class EvidenceFakeApi:
    def __init__(self, response: dict) -> None:
        self.response = response
        self.operations: list[str] = []
        self.range_url: str | None = None

    def upload_source(self, case: dict) -> dict:
        self.operations.append("upload")
        return {"upload_id": f"upload-{case['case_id']}", "owner_alias": case["source"]["owner_alias"]}

    def quote(self, case: dict, upload: dict) -> dict:
        self.operations.append("quote")
        return dict(self.response["quote"])

    def create_job(self, idempotency_key: str) -> str:
        self.operations.append(f"create:{idempotency_key}")
        return str(self.response["job_id"])

    def get_job(self, job_id: str) -> dict:
        self.operations.append("poll")
        status = self.response["status"]
        if (
            status == "failed"
            and self.response.get("settlement", {}).get("state") == "prehold_absent"
        ):
            status = "prehold_absent"
        return {"job_id": job_id, "status": status}

    def get_result(self, job_id: str) -> dict:
        self.operations.append("result")
        return json.loads(json.dumps(self.response))

    def verify_range(self, playback_url: str) -> bool:
        self.operations.append("range")
        self.range_url = playback_url
        return True


class FakeRealRunApi:
    def __init__(self, **capability_overrides) -> None:
        self.upload_calls: list[str] = []
        self._capabilities = {
            "deployed_sha": "a" * 40,
            "environment": "test",
            "v3_enabled": True,
            "providers_ready": True,
            "accepts_uploads": True,
            "accepts_new_jobs": True,
            "active_v3_jobs": 0,
        }
        self._capabilities.update(capability_overrides)

    def capabilities(self) -> dict[str, object]:
        return dict(self._capabilities)

    def upload_authorized_sources(self, *args) -> None:
        self.upload_calls.append("upload")


class AcceptanceRunnerTests(unittest.TestCase):
    def test_acceptance_aggregate_lock_recovers_stale_file_and_waits_for_competitor(self) -> None:
        import scripts.ai_edit_v3_acceptance as command

        with tempfile.TemporaryDirectory() as folder:
            run_root = Path(folder) / "lock-run-01"
            profile_dir = run_root / "parallel-5"
            profile_dir.mkdir(parents=True)
            (profile_dir / "report.json").write_text(json.dumps({
                "run_id": "lock-run-01",
                "environment": "test",
                "status": "completed",
                "case_count": 5,
                "manifest_sha256": "a" * 64,
            }), encoding="utf-8")
            (run_root / ".acceptance.lock").write_text("stale", encoding="utf-8")
            with patch(
                "scripts.ai_edit_v3_acceptance.execute_verify_command", return_value=0,
            ):
                try:
                    _refresh_acceptance_aggregate(run_root, "lock-run-01")
                except ValueError as exc:
                    self.fail(f"stale advisory lock file must not block recovery: {exc}")
                self.assertTrue((run_root / "acceptance.json").is_file())

                original_write = command._write_json_atomic
                first_writer_entered = threading.Event()
                release_first_writer = threading.Event()
                errors: list[BaseException] = []

                def blocking_write(path, payload):
                    if path.name == "acceptance.json" and not first_writer_entered.is_set():
                        first_writer_entered.set()
                        release_first_writer.wait(timeout=2)
                    return original_write(path, payload)

                def refresh() -> None:
                    try:
                        _refresh_acceptance_aggregate(run_root, "lock-run-01")
                    except BaseException as exc:
                        errors.append(exc)

                with patch(
                    "scripts.ai_edit_v3_acceptance._write_json_atomic",
                    side_effect=blocking_write,
                ):
                    first = threading.Thread(target=refresh)
                    first.start()
                    self.assertTrue(first_writer_entered.wait(timeout=2))
                    second = threading.Thread(target=refresh)
                    second.start()
                    time.sleep(0.1)
                    release_first_writer.set()
                    first.join(timeout=2)
                    second.join(timeout=2)

                self.assertFalse(first.is_alive())
                self.assertFalse(second.is_alive())
                self.assertEqual([], errors)

    def test_execute_preflighted_cases_writes_real_immutable_report_without_reupload(self) -> None:
        root = Path(__file__).resolve().parents[1]
        matrix_path = root / "tests/fixtures/ai_edit_v3/acceptance-20.json"
        matrix = json.loads(matrix_path.read_text(encoding="utf-8"))
        fixture = json.loads((
            root / "tests/fixtures/ai_edit_v3/acceptance-responses/completed.json"
        ).read_text(encoding="utf-8"))

        class Api:
            def __init__(self):
                self.case_ids = []

            def for_case(self, case):
                self.case_ids.append(case["case_id"])
                response = json.loads(json.dumps(fixture))
                response["job_id"] = "job-real-" + case["case_id"]
                response["normalized_request_sha256"] = case["source"]["sha256"]
                return EvidenceFakeApi(response)

            def expected_request_sha256(self, case):
                return case["source"]["sha256"]

        args = type("Args", (), {
            "matrix": matrix_path, "run_id": "real-run-01", "concurrency": 5,
            "subset": "parallel-5", "environment": "test",
        })()
        api = Api()
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
            "AI_EDIT_V3_ACCEPTANCE_OUTPUT_ROOT": folder,
            "AI_EDIT_V3_EXPECTED_SHA": "a" * 40,
            "AI_EDIT_V3_ACCEPTANCE_AUTHORIZATION_REF": "acceptance-approved-corpus-v1",
        }, clear=False):
            result = execute_preflighted_cases(api, args)
            run_root = Path(folder) / "real-run-01"
            run_dir = run_root / "parallel-5"
            self.assertEqual(0, result, list(run_dir.rglob("*")) if run_dir.exists() else [])
            report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
            manifest = json.loads((run_dir / "run-manifest.json").read_text(encoding="utf-8"))
            aggregate = json.loads((run_root / "acceptance.json").read_text(encoding="utf-8"))
            (run_root / "acceptance.json").unlink()
            self.assertEqual(4, execute_preflighted_cases(api, args))
            self.assertTrue((run_root / "acceptance.json").is_file())
            stress_args = type("Args", (), {
                "matrix": matrix_path, "run_id": "real-run-01", "concurrency": 10,
                "subset": "stress-10", "environment": "test",
            })()
            stress_api = Api()
            self.assertEqual(0, execute_preflighted_cases(stress_api, stress_args))
            aggregate = json.loads((run_root / "acceptance.json").read_text(encoding="utf-8"))
            self.assertEqual(0, main([
                "verify", "--report", str(run_root / "acceptance.json"), "--strict",
            ]))
            stress_manifest_path = run_root / "stress-10" / "run-manifest.json"
            stress_manifest = json.loads(stress_manifest_path.read_text(encoding="utf-8"))
            stress_manifest["profile"] = "parallel-5"
            stress_manifest_path.write_text(json.dumps(stress_manifest), encoding="utf-8")
            stress_report_path = run_root / "stress-10" / "report.json"
            stress_report = json.loads(stress_report_path.read_text(encoding="utf-8"))
            stress_report["manifest_sha256"] = hashlib.sha256(
                stress_manifest_path.read_bytes()
            ).hexdigest()
            stress_report_path.write_text(json.dumps(stress_report), encoding="utf-8")
            profile_swapped = json.loads(json.dumps(aggregate))
            profile_swapped["profiles"]["stress-10"]["manifest_sha256"] = (
                stress_report["manifest_sha256"]
            )
            profile_swapped["profiles"]["stress-10"]["report_sha256"] = hashlib.sha256(
                stress_report_path.read_bytes()
            ).hexdigest()
            (run_root / "acceptance.json").write_text(
                json.dumps(profile_swapped), encoding="utf-8"
            )
            self.assertEqual(4, main([
                "verify", "--report", str(run_root / "acceptance.json"), "--strict",
            ]))
            tampered = json.loads(json.dumps(aggregate))
            tampered["profiles"]["parallel-5"]["case_count"] = 99
            (run_root / "acceptance.json").write_text(
                json.dumps(tampered), encoding="utf-8"
            )
            self.assertEqual(4, main([
                "verify", "--report", str(run_root / "acceptance.json"), "--strict",
            ]))

        self.assertCountEqual(
            [case["case_id"] for case in matrix["cases"][:5]], api.case_ids
        )
        self.assertEqual(5, len(api.case_ids))
        self.assertEqual("test", manifest["environment"])
        self.assertEqual("a" * 40, manifest["commit_sha"])
        self.assertEqual("completed", report["status"])
        self.assertEqual(5, report["case_count"])
        self.assertEqual(["parallel-5", "stress-10"], sorted(aggregate["profiles"]))
        self.assertEqual(
            report["manifest_sha256"],
            aggregate["profiles"]["parallel-5"]["manifest_sha256"],
        )
        self.assertEqual(10, aggregate["profiles"]["stress-10"]["case_count"])

    def test_real_profile_rejects_wrong_concurrency_before_writing(self) -> None:
        root = Path(__file__).resolve().parents[1]
        matrix_path = root / "tests/fixtures/ai_edit_v3/acceptance-20.json"
        args = type("Args", (), {
            "matrix": matrix_path, "run_id": "wrong-concurrency-01", "concurrency": 1,
            "subset": "parallel-5", "environment": "test",
        })()
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
            "AI_EDIT_V3_ACCEPTANCE_OUTPUT_ROOT": folder,
            "AI_EDIT_V3_EXPECTED_SHA": "a" * 40,
            "AI_EDIT_V3_ACCEPTANCE_AUTHORIZATION_REF": "acceptance-approved-corpus-v1",
        }, clear=False):
            self.assertEqual(4, execute_preflighted_cases(object(), args))
            self.assertFalse((Path(folder) / "wrong-concurrency-01").exists())

    def test_run_cases_honors_requested_concurrency(self) -> None:
        root = Path(__file__).resolve().parents[1]
        response = json.loads((
            root / "tests/fixtures/ai_edit_v3/acceptance-responses/completed.json"
        ).read_text(encoding="utf-8"))
        matrix = json.loads((
            root / "tests/fixtures/ai_edit_v3/acceptance-20.json"
        ).read_text(encoding="utf-8"))
        cases = matrix["cases"][:2]
        barrier = threading.Barrier(2, timeout=2)

        class ConcurrentApi(EvidenceFakeApi):
            def get_job(self, job_id):
                self.operations.append("poll")
                barrier.wait()
                return {"job_id": job_id, "status": "completed"}

        def factory(case):
            payload = json.loads(json.dumps(response))
            payload["job_id"] = "job-" + case["case_id"]
            payload["normalized_request_sha256"] = case["source"]["sha256"]
            return ConcurrentApi(payload)

        with tempfile.TemporaryDirectory() as folder:
            summary = run_cases(
                AcceptanceConfig("parallel-run", Path(folder), factory),
                RunManifest(tuple(cases)), concurrency=2,
            )

        self.assertEqual(0, summary.result_code)
        self.assertEqual(["case_01", "case_02"], [item["case_id"] for item in summary.case_results])

    def test_http_case_api_uses_frozen_request_owner_session_and_test_evidence(self) -> None:
        class RangeResponse:
            status = 206
            headers = {"Content-Range": "bytes 0-0/123"}

            def read(self, size=-1):
                return b"x"

            def close(self):
                pass

        class Opener:
            def __init__(self):
                self.requests = []

            def open(self, request, timeout):
                self.requests.append((request, timeout))
                return RangeResponse()

        class Transport:
            _safe_signed_upload_url = staticmethod(HttpRealRunApi._safe_signed_upload_url)

            def __init__(self):
                self.calls = []
                self._opener = Opener()
                self.request_sha256 = None

            def _json_request(
                self, method, path, payload, *, expected_statuses, session,
                idempotency_key=None,
            ):
                self.calls.append((method, path, payload, expected_statuses, session, idempotency_key))
                if path.endswith("/quote"):
                    self.request_sha256 = request_fingerprint(normalize_job_request(payload))
                    return {
                        "quote_id": "quote-owned-1", "pricing_version": "pricing-v1",
                        "max_points": 30, "request_sha256": self.request_sha256,
                    }
                if path.endswith("/jobs"):
                    return {"job_id": "job-owned-1"}
                if path.endswith("/result"):
                    return {
                        "job_id": "job-owned-1",
                        "result": {"play_url": "https://cos.example/final.mp4?token=opaque"},
                    }
                if path.endswith("/acceptance-evidence"):
                    return {
                        "job_id": "job-owned-1", "state": "completed",
                        "evidence": {"normalized_request_sha256": self.request_sha256},
                    }
                return {"job_id": "job-owned-1", "state": "completed"}

        session = load_test_session({"AI_EDIT_V3_TEST_SESSION": "owner-session"}, lambda _: "")
        transport = Transport()
        case = {
            "case_id": "case_01", "input_type": "uploaded_audio", "ratio": "9:16",
            "creation_mode": "style_prompt", "style_prompt": "高级商业纪实",
        }
        client = HttpCaseApi(transport, case, {
            "owner_alias": "owner_a", "session": session,
            "source_fields": {"source_upload_id": "upload-owned-1"},
            "material_asset_ids": ("material-owned-1",),
        })

        upload = client.upload_source(case)
        quote = client.quote(case, upload)
        job_id = client.create_job("acceptance:run-01:case_01")
        state = client.get_job(job_id)
        result = client.get_result(job_id)

        self.assertEqual(30, quote["held_points"])
        self.assertEqual("completed", state["status"])
        self.assertEqual("completed", result["status"])
        self.assertTrue(client.verify_range(result["playback_url"]))
        quote_body = transport.calls[0][2]
        create_body = transport.calls[1][2]
        self.assertEqual({**quote_body, "quote_id": "quote-owned-1"}, create_body)
        self.assertEqual("9:16", quote_body["ratio"])
        self.assertEqual(["material-owned-1"], quote_body["material_asset_ids"])
        self.assertEqual("Bearer owner-session", "Bearer " + transport.calls[0][4].reveal())
        self.assertEqual("acceptance:run-01:case_01", transport.calls[1][5])
        self.assertEqual(
            ["/api/v3/edit/jobs/job-owned-1/acceptance-evidence",
             "/api/v3/edit/jobs/job-owned-1/result"],
            [transport.calls[3][1], transport.calls[4][1]],
        )

    def test_http_case_api_normalizes_prehold_absent_without_public_result(self) -> None:
        class Transport:
            def __init__(self):
                self.paths = []

            def _json_request(self, method, path, payload, **kwargs):
                self.paths.append(path)
                if path.endswith("/acceptance-evidence"):
                    return {
                        "job_id": "job-failed-1", "state": "prehold_absent",
                        "evidence": {"normalized_request_sha256": "a" * 64},
                    }
                raise AssertionError("failed jobs must not request public result")

        client = object.__new__(HttpCaseApi)
        client._transport = Transport()
        client._session = load_test_session(
            {"AI_EDIT_V3_TEST_SESSION": "owner-session"}, lambda _: ""
        )

        result = client.get_result("job-failed-1")

        self.assertEqual("failed", result["status"])
        self.assertNotIn("playback_url", result)
        self.assertEqual(
            ["/api/v3/edit/jobs/job-failed-1/acceptance-evidence"],
            client._transport.paths,
        )

    def test_http_case_api_rejects_malformed_or_empty_range_response(self) -> None:
        class Response:
            status = 206
            headers = {"Content-Range": "bytes 0-0/garbage", "Content-Length": "0"}

            def read(self, size=-1):
                return b""

            def close(self):
                pass

        class Opener:
            def open(self, request, timeout):
                return Response()

        transport = type("Transport", (), {
            "_safe_signed_upload_url": staticmethod(HttpRealRunApi._safe_signed_upload_url),
            "_opener": Opener(),
        })()
        client = object.__new__(HttpCaseApi)
        client._transport = transport

        self.assertFalse(client.verify_range("https://cos.example/final.mp4?token=opaque"))

    def test_bindings_v2_loads_owner_sessions_without_persisting_values(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            media = root / "source.mp3"
            media.write_bytes(b"audio-source")
            script = root / "script.txt"
            script.write_text("authorized script", encoding="utf-8")
            media_sha = __import__("hashlib").sha256(media.read_bytes()).hexdigest()
            script_sha = __import__("hashlib").sha256(script.read_bytes()).hexdigest()
            matrix = {
                "authorization_ref": "approval-test",
                "cases": [
                    {
                        "case_id": "case_01", "input_type": "uploaded_audio",
                        "authorization_ref": "approval-test",
                        "source": {
                            "alias": "bindings/case_01/source", "sha256": media_sha,
                            "owner_alias": "owner_a", "authorization_ref": "approval-test",
                            "media_type": "audio/mpeg",
                        },
                        "materials": [],
                    },
                    {
                        "case_id": "case_02", "input_type": "script_to_audio_video",
                        "authorization_ref": "approval-test",
                        "source": {
                            "alias": "bindings/case_02/source", "sha256": script_sha,
                            "owner_alias": "owner_b", "authorization_ref": "approval-test",
                            "media_type": "text/plain",
                        },
                        "materials": [],
                    },
                ],
            }
            bindings = {
                "version": "2.0",
                "owners": [
                    {"owner_alias": "owner_a", "session_env": "AI_EDIT_V3_TEST_SESSION_A"},
                    {"owner_alias": "owner_b", "session_env": "AI_EDIT_V3_TEST_SESSION_B"},
                ],
                "cases": [
                    {
                        "case_id": "case_01", "owner_alias": "owner_a",
                        "source": {
                            "kind": "upload", "alias": "bindings/case_01/source",
                            "authorization_ref": "approval-test", "path": str(media),
                            "sha256": media_sha, "upload_type": "main_audio",
                            "content_type": "audio/mpeg",
                        },
                        "materials": [],
                    },
                    {
                        "case_id": "case_02", "owner_alias": "owner_b",
                        "source": {
                            "kind": "tts", "alias": "bindings/case_02/source",
                            "authorization_ref": "approval-test", "text_path": str(script),
                            "sha256": script_sha, "voice_id": "voice-owned-b",
                        },
                        "materials": [],
                    },
                ],
            }
            matrix_path = root / "matrix.json"
            bindings_path = root / "bindings.json"
            matrix_path.write_text(json.dumps(matrix), encoding="utf-8")
            bindings_path.write_text(json.dumps(bindings), encoding="utf-8")

            loaded = load_authorized_bindings(
                matrix_path,
                bindings_path,
                environment={
                    "AI_EDIT_V3_TEST_SESSION_A": "session-a-secret",
                    "AI_EDIT_V3_TEST_SESSION_B": "session-b-secret",
                },
                authorization_ref="approval-test",
            )

            self.assertEqual({"owner_a", "owner_b"}, set(loaded.owners))
            self.assertEqual("upload", loaded.cases["case_01"]["source"]["kind"])
            self.assertEqual("tts", loaded.cases["case_02"]["source"]["kind"])
            self.assertNotIn("session-a-secret", repr(loaded))
            self.assertNotIn("session-b-secret", repr(loaded))

    def test_bindings_v2_rejects_missing_session_and_cross_owner_material(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.mp3"
            material = root / "material.jpg"
            source.write_bytes(b"source")
            material.write_bytes(b"material")
            sha = lambda path: __import__("hashlib").sha256(path.read_bytes()).hexdigest()
            matrix = {
                "authorization_ref": "approval-test",
                "cases": [{
                    "case_id": "case_01", "input_type": "uploaded_audio",
                    "authorization_ref": "approval-test",
                    "source": {
                        "alias": "bindings/case_01/source", "sha256": sha(source),
                        "owner_alias": "owner_a", "authorization_ref": "approval-test",
                        "media_type": "audio/mpeg",
                    },
                    "materials": [{
                        "alias": "bindings/case_01/material_01", "sha256": sha(material),
                        "owner_alias": "owner_a", "authorization_ref": "approval-test",
                        "media_type": "image/jpeg",
                    }],
                }],
            }
            bindings = {
                "version": "2.0",
                "owners": [{"owner_alias": "owner_a", "session_env": "AI_EDIT_V3_TEST_SESSION_A"}],
                "cases": [{
                    "case_id": "case_01", "owner_alias": "owner_a",
                    "source": {
                        "kind": "upload", "alias": "bindings/case_01/source",
                        "authorization_ref": "approval-test", "path": str(source),
                        "sha256": sha(source), "upload_type": "main_audio",
                        "content_type": "audio/mpeg",
                    },
                    "materials": [{
                        "kind": "upload", "alias": "bindings/case_01/material_01",
                        "owner_alias": "owner_b", "authorization_ref": "approval-test",
                        "path": str(material), "sha256": sha(material),
                        "content_type": "image/jpeg",
                    }],
                }],
            }
            matrix_path = root / "matrix.json"
            bindings_path = root / "bindings.json"
            matrix_path.write_text(json.dumps(matrix), encoding="utf-8")
            bindings_path.write_text(json.dumps(bindings), encoding="utf-8")

            with self.assertRaisesRegex(RealRunUnavailable, "test_session_missing"):
                load_authorized_bindings(
                    matrix_path, bindings_path, environment={},
                    authorization_ref="approval-test",
                )
            with self.assertRaisesRegex(RealRunUnavailable, "material_owner_mismatch"):
                load_authorized_bindings(
                    matrix_path, bindings_path,
                    environment={"AI_EDIT_V3_TEST_SESSION_A": "secret"},
                    authorization_ref="approval-test",
                )

    def test_http_real_api_normalizes_only_nested_acceptance_contract(self) -> None:
        class Response:
            status = 200

            def __init__(self, payload):
                self.payload = json.dumps(payload).encode("utf-8")

            def read(self, size=-1):
                value, self.payload = self.payload[:size], self.payload[size:]
                return value

            def close(self):
                pass

        class Opener:
            def __init__(self):
                self.requests = []

            def open(self, request, timeout):
                self.requests.append((request, timeout))
                return Response({
                    "items": {"ignored": "raw"},
                    "acceptance": {
                        "environment": "test",
                        "deployed_sha": "a" * 40,
                        "active_v3_jobs": 0,
                        "v3_enabled": True,
                        "providers_ready": True,
                        "accepts_uploads": True,
                        "accepts_new_jobs": True,
                    },
                })

        opener = Opener()
        session = load_test_session(
            {"AI_EDIT_V3_TEST_SESSION": "session-secret"}, lambda _: ""
        )
        with tempfile.TemporaryDirectory() as folder:
            bindings = Path(folder) / "bindings.json"
            bindings.write_text('{"version":"1.0","sources":[]}', encoding="utf-8")
            api = HttpRealRunApi(
                base_url="https://test.example",
                session=session,
                bindings_path=bindings,
                opener=opener,
            )
            capabilities = api.capabilities()

        self.assertEqual(capabilities["deployed_sha"], "a" * 40)
        self.assertNotIn("items", capabilities)
        request, timeout = opener.requests[0]
        self.assertEqual(request.full_url, "https://test.example/api/v3/edit/capabilities")
        self.assertEqual(timeout, 15)
        self.assertEqual(request.get_header("Authorization"), "Bearer session-secret")
        self.assertNotIn("session-secret", repr(api))

    def test_http_real_api_prepares_v2_upload_material_and_tts_per_owner(self) -> None:
        class Response:
            def __init__(self, status, payload=None):
                self.status = status
                self.payload = b"" if payload is None else json.dumps(payload).encode()

            def read(self, size=-1):
                value, self.payload = self.payload[:size], self.payload[size:]
                return value

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.mp3"
            material = root / "material.jpg"
            script = root / "script.txt"
            source.write_bytes(b"source-audio")
            material.write_bytes(b"image-material")
            script.write_text("authorized narration", encoding="utf-8")
            sha = lambda path: __import__("hashlib").sha256(path.read_bytes()).hexdigest()
            source_sha, material_sha, script_sha = sha(source), sha(material), sha(script)

            class Opener:
                def __init__(self):
                    self.requests = []
                    self.uploads = iter((
                        ("upload-source", source_sha, "https://cos.example/source?signed=x"),
                        ("upload-material", material_sha, "https://cos.example/material?signed=x"),
                    ))
                    self.completions = {
                        "upload-source": source_sha,
                        "upload-material": material_sha,
                    }

                def open(self, request, timeout):
                    self.requests.append((request, timeout))
                    url = request.full_url
                    if request.get_method() == "PUT":
                        return Response(200)
                    if url.endswith("/api/v3/edit/uploads"):
                        upload_id, _digest, put_url = next(self.uploads)
                        return Response(201, {"upload_id": upload_id, "put_url": put_url})
                    if "/uploads/" in url and url.endswith("/complete"):
                        upload_id = url.split("/uploads/", 1)[1].split("/", 1)[0]
                        return Response(200, {
                            "upload_id": upload_id,
                            "sha256": self.completions[upload_id],
                        })
                    if url.endswith("/api/v3/edit/materials"):
                        return Response(201, {
                            "material_id": "material-owned-a", "sha256": material_sha,
                        })
                    if url.endswith("/api/v3/edit/voices"):
                        return Response(200, {"items": [{"voice_id": "voice-owned-b"}]})
                    raise AssertionError(url)

            matrix = {
                "authorization_ref": "approval-test",
                "cases": [
                    {
                        "case_id": "case_01", "input_type": "uploaded_audio",
                        "authorization_ref": "approval-test",
                        "source": {
                            "alias": "bindings/case_01/source", "sha256": source_sha,
                            "owner_alias": "owner_a", "authorization_ref": "approval-test",
                            "media_type": "audio/mpeg",
                        },
                        "materials": [{
                            "alias": "bindings/case_01/material_01", "sha256": material_sha,
                            "owner_alias": "owner_a", "authorization_ref": "approval-test",
                            "media_type": "image/jpeg",
                        }],
                    },
                    {
                        "case_id": "case_02", "input_type": "script_to_audio_video",
                        "authorization_ref": "approval-test",
                        "source": {
                            "alias": "bindings/case_02/source", "sha256": script_sha,
                            "owner_alias": "owner_b", "authorization_ref": "approval-test",
                            "media_type": "text/plain",
                        },
                        "materials": [],
                    },
                ],
            }
            bindings = {
                "version": "2.0",
                "owners": [
                    {"owner_alias": "owner_a", "session_env": "AI_EDIT_V3_TEST_SESSION_A"},
                    {"owner_alias": "owner_b", "session_env": "AI_EDIT_V3_TEST_SESSION_B"},
                ],
                "cases": [
                    {
                        "case_id": "case_01", "owner_alias": "owner_a",
                        "source": {
                            "kind": "upload", "alias": "bindings/case_01/source",
                            "authorization_ref": "approval-test", "path": str(source),
                            "sha256": source_sha, "upload_type": "main_audio",
                            "content_type": "audio/mpeg",
                        },
                        "materials": [{
                            "kind": "upload", "alias": "bindings/case_01/material_01",
                            "owner_alias": "owner_a", "authorization_ref": "approval-test",
                            "path": str(material), "sha256": material_sha,
                            "content_type": "image/jpeg",
                        }],
                    },
                    {
                        "case_id": "case_02", "owner_alias": "owner_b",
                        "source": {
                            "kind": "tts", "alias": "bindings/case_02/source",
                            "authorization_ref": "approval-test", "text_path": str(script),
                            "sha256": script_sha, "voice_id": "voice-owned-b",
                        },
                        "materials": [],
                    },
                ],
            }
            matrix_path = root / "matrix.json"
            bindings_path = root / "bindings.json"
            matrix_path.write_text(json.dumps(matrix), encoding="utf-8")
            bindings_path.write_text(json.dumps(bindings), encoding="utf-8")
            opener = Opener()
            primary = load_test_session({"AI_EDIT_V3_TEST_SESSION": "primary"}, lambda _: "")
            api = HttpRealRunApi(
                base_url="https://test.example", session=primary,
                bindings_path=bindings_path, opener=opener,
                environment={
                    "AI_EDIT_V3_TEST_SESSION_A": "session-a",
                    "AI_EDIT_V3_TEST_SESSION_B": "session-b",
                },
            )

            api.upload_authorized_sources(matrix_path, "approval-test", None)

            self.assertEqual(
                ("material-owned-a",),
                api._authorized_cases["case_01"]["material_asset_ids"],
            )
            self.assertEqual(
                "authorized narration",
                api._authorized_cases["case_02"]["source_fields"]["tts_input"]["text"],
            )
            authenticated = [
                request.get_header("Authorization")
                for request, _timeout in opener.requests
                if request.get_method() != "PUT"
            ]
            self.assertEqual(
                ["Bearer session-a"] * 5 + ["Bearer session-b"],
                authenticated,
            )

    def test_http_real_api_rejects_unsafe_origin_and_invalid_capability_body(self) -> None:
        session = load_test_session(
            {"AI_EDIT_V3_TEST_SESSION": "session-secret"}, lambda _: ""
        )
        with tempfile.TemporaryDirectory() as folder:
            bindings = Path(folder) / "bindings.json"
            bindings.write_text('{"version":"1.0","sources":[]}', encoding="utf-8")
            for base_url in (
                "http://test.example",
                "https://user@test.example",
                "https://test.example/path",
                "https://test.example?query=1",
                "https://test.example/#fragment",
            ):
                with self.subTest(base_url=base_url):
                    with self.assertRaises(RealRunUnavailable):
                        HttpRealRunApi(
                            base_url=base_url,
                            session=session,
                            bindings_path=bindings,
                        )

            class OversizedResponse:
                status = 200

                def read(self, size=-1):
                    return b"x" * size

                def close(self):
                    pass

            class OversizedOpener:
                def open(self, request, timeout):
                    return OversizedResponse()

            api = HttpRealRunApi(
                base_url="https://test.example",
                session=session,
                bindings_path=bindings,
                opener=OversizedOpener(),
            )
            with self.assertRaises(RealRunUnavailable):
                api.capabilities()

    def test_http_real_api_rejects_legacy_binding_version_before_network(self) -> None:
        class Opener:
            def __init__(self):
                self.requests = []

            def open(self, request, timeout):
                self.requests.append((request, timeout))
                raise AssertionError("legacy bindings must fail before network")

        session = load_test_session(
            {"AI_EDIT_V3_TEST_SESSION": "session-secret"}, lambda _: ""
        )
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.mp3"
            source.write_bytes(b"authorized-audio")
            bindings = Path(folder) / "bindings.json"
            bindings.write_text(json.dumps({
                "version": "1.0",
                "sources": [{
                    "case_id": "case_01",
                    "alias": "bindings/case_01/source",
                    "owner_alias": "owner_a",
                    "authorization_ref": "approval-test-only",
                    "path": str(source.resolve()),
                    "sha256": __import__("hashlib").sha256(source.read_bytes()).hexdigest(),
                    "upload_type": "main_audio",
                    "content_type": "audio/mpeg",
                }],
            }), encoding="utf-8")
            matrix = Path(folder) / "matrix.json"
            matrix.write_text(json.dumps({
                "version": "3.1",
                "authorization_ref": "approval-test-only",
                "cases": [{
                    "case_id": "case_01",
                    "input_type": "uploaded_audio",
                    "authorization_ref": "approval-test-only",
                    "source": {
                        "alias": "bindings/case_01/source",
                        "sha256": __import__("hashlib").sha256(source.read_bytes()).hexdigest(),
                        "media_type": "audio/mpeg",
                        "owner_alias": "owner_a",
                        "authorization_ref": "approval-test-only",
                    },
                }],
            }), encoding="utf-8")
            opener = Opener()
            api = HttpRealRunApi(
                base_url="https://test.example",
                session=session,
                bindings_path=bindings,
                opener=opener,
            )

            with self.assertRaises(RealRunUnavailable):
                api.upload_authorized_sources(matrix, "approval-test-only", None)

        self.assertEqual([], opener.requests)

    def test_http_real_api_rejects_tampered_binding_before_network(self) -> None:
        class Opener:
            def __init__(self):
                self.requests = []

            def open(self, request, timeout):
                self.requests.append(request)
                raise AssertionError("network must not be reached")

        session = load_test_session(
            {"AI_EDIT_V3_TEST_SESSION": "session-secret"}, lambda _: ""
        )
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.mp3"
            source.write_bytes(b"tampered")
            bindings = Path(folder) / "bindings.json"
            bindings.write_text(json.dumps({
                "version": "1.0",
                "sources": [{
                    "case_id": "case_01", "alias": "bindings/case_01/source",
                    "owner_alias": "owner_a", "authorization_ref": "approval-test-only",
                    "path": str(source.resolve()), "sha256": "a" * 64,
                    "upload_type": "main_audio", "content_type": "audio/mpeg",
                }],
            }), encoding="utf-8")
            matrix = Path(folder) / "matrix.json"
            matrix.write_text(json.dumps({
                "authorization_ref": "approval-test-only",
                "cases": [{
                    "case_id": "case_01",
                    "input_type": "uploaded_audio",
                    "authorization_ref": "approval-test-only",
                    "source": {
                        "alias": "bindings/case_01/source",
                        "sha256": "b" * 64,
                        "media_type": "audio/mpeg",
                        "owner_alias": "owner_a",
                        "authorization_ref": "approval-test-only",
                    },
                }],
            }), encoding="utf-8")
            opener = Opener()
            api = HttpRealRunApi(
                base_url="https://test.example", session=session,
                bindings_path=bindings, opener=opener,
            )
            with self.assertRaises(RealRunUnavailable):
                api.upload_authorized_sources(matrix, "approval-test-only", None)
        self.assertEqual(opener.requests, [])

    def test_http_real_api_rejects_binding_authority_drift_before_network(self) -> None:
        class Opener:
            def __init__(self):
                self.requests = []

            def open(self, request, timeout):
                self.requests.append(request)
                raise AssertionError("network must not be reached")

        session = load_test_session(
            {"AI_EDIT_V3_TEST_SESSION": "session-secret"}, lambda _: ""
        )
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "source.mp3"
            source.write_bytes(b"authorized-audio")
            digest = __import__("hashlib").sha256(source.read_bytes()).hexdigest()
            base_binding = {
                "case_id": "case_01",
                "alias": "bindings/case_01/source",
                "owner_alias": "owner_a",
                "authorization_ref": "approval-test-only",
                "path": str(source.resolve()),
                "sha256": digest,
                "upload_type": "main_audio",
                "content_type": "audio/mpeg",
            }
            base_matrix = {
                "authorization_ref": "approval-test-only",
                "cases": [{
                    "case_id": "case_01",
                    "input_type": "uploaded_audio",
                    "authorization_ref": "approval-test-only",
                    "source": {
                        "alias": "bindings/case_01/source",
                        "sha256": digest,
                        "media_type": "audio/mpeg",
                        "owner_alias": "owner_a",
                        "authorization_ref": "approval-test-only",
                    },
                }],
            }
            scenarios = {
                "wrong_owner": ([{**base_binding, "owner_alias": "owner_b"}], base_matrix, "approval-test-only"),
                "missing_case": ([], base_matrix, "approval-test-only"),
                "extra_case": ([base_binding, {**base_binding, "case_id": "case_02"}], base_matrix, "approval-test-only"),
                "authorization_mismatch": ([base_binding], base_matrix, "different-approval"),
            }
            for name, (sources, matrix_payload, authority) in scenarios.items():
                with self.subTest(name=name):
                    bindings = Path(folder) / f"bindings-{name}.json"
                    bindings.write_text(json.dumps({
                        "version": "1.0", "sources": sources,
                    }), encoding="utf-8")
                    matrix = Path(folder) / f"matrix-{name}.json"
                    matrix.write_text(json.dumps(matrix_payload), encoding="utf-8")
                    opener = Opener()
                    api = HttpRealRunApi(
                        base_url="https://test.example", session=session,
                        bindings_path=bindings, opener=opener,
                    )
                    with self.assertRaises(RealRunUnavailable):
                        api.upload_authorized_sources(matrix, authority, None)
                    self.assertEqual(opener.requests, [])

    def test_real_runner_refuses_every_preflight_mismatch_before_upload(self) -> None:
        scenarios = (
            (RealRunConfig("a" * 40, "production", "approval"), {}, "environment_not_test"),
            (RealRunConfig("a" * 40, "test", ""), {}, "authorization_missing"),
            (RealRunConfig("a" * 40, "test", "approval"), {"environment": "production"}, "deployed_environment_mismatch"),
            (RealRunConfig("b" * 40, "test", "approval"), {}, "deployed_sha_mismatch"),
            (RealRunConfig("a" * 40, "test", "approval"), {"active_v3_jobs": 1}, "active_v3_jobs"),
            (RealRunConfig("a" * 40, "test", "approval"), {"v3_enabled": False}, "v3_not_enabled"),
            (RealRunConfig("a" * 40, "test", "approval"), {"providers_ready": False}, "providers_not_ready"),
            (RealRunConfig("a" * 40, "test", "approval"), {"accepts_uploads": False}, "uploads_not_ready"),
            (RealRunConfig("a" * 40, "test", "approval"), {"accepts_new_jobs": False}, "new_jobs_not_ready"),
        )
        for config, overrides, reason in scenarios:
            with self.subTest(reason=reason):
                api = FakeRealRunApi(**overrides)
                result = run_real_acceptance(api, config)
                self.assertEqual((result.exit_code, result.reason), (2, reason))
                self.assertEqual(api.upload_calls, [])

    def test_test_environment_cli_preflights_then_uploads_and_executes_once(self) -> None:
        api = FakeRealRunApi()
        matrix = Path(__file__).parent / "fixtures/ai_edit_v3/acceptance-20.json"
        with patch.dict(os.environ, {
            "AI_EDIT_V3_EXPECTED_SHA": "a" * 40,
            "AI_EDIT_V3_ACCEPTANCE_AUTHORIZATION_REF": "approval-test-only",
        }, clear=False), patch(
            "scripts.ai_edit_v3_acceptance.build_real_run_api", return_value=api,
        ), patch(
            "scripts.ai_edit_v3_acceptance.execute_preflighted_cases", return_value=0,
        ) as execute:
            exit_code = main([
                "run", "--environment", "test", "--matrix", str(matrix),
                "--run-id", "00000000-0000-4000-8000-000000000001",
                "--concurrency", "1",
            ])

        self.assertEqual(exit_code, 0)
        self.assertEqual(api.upload_calls, ["upload"])
        execute.assert_called_once()
        self.assertIs(execute.call_args.args[0], api)

    def test_test_environment_sha_mismatch_never_uploads_or_executes(self) -> None:
        api = FakeRealRunApi(deployed_sha="b" * 40)
        matrix = Path(__file__).parent / "fixtures/ai_edit_v3/acceptance-20.json"
        with patch.dict(os.environ, {
            "AI_EDIT_V3_EXPECTED_SHA": "a" * 40,
            "AI_EDIT_V3_ACCEPTANCE_AUTHORIZATION_REF": "approval-test-only",
        }, clear=False), patch(
            "scripts.ai_edit_v3_acceptance.build_real_run_api", return_value=api,
        ), patch("scripts.ai_edit_v3_acceptance.execute_preflighted_cases") as execute:
            exit_code = main([
                "run", "--environment", "test", "--matrix", str(matrix),
                "--run-id", "00000000-0000-4000-8000-000000000001",
                "--concurrency", "1",
            ])

        self.assertEqual(exit_code, 2)
        self.assertEqual(api.upload_calls, [])
        execute.assert_not_called()

    def test_capability_transport_or_shape_failure_is_exit_two_before_upload(self) -> None:
        class BrokenApi(FakeRealRunApi):
            def __init__(self, response=None, error=None) -> None:
                super().__init__()
                self.response = response
                self.error = error

            def capabilities(self):
                if self.error is not None:
                    raise self.error
                return self.response

        config = RealRunConfig("a" * 40, "test", "approval")
        scenarios = (
            (BrokenApi(error=RealRunUnavailable("adapter unavailable")), "capabilities_unavailable"),
            (BrokenApi(error=OSError("network down")), "capabilities_unavailable"),
            (BrokenApi(error=ValueError("invalid json")), "capabilities_unavailable"),
            (BrokenApi(response=None), "capabilities_invalid"),
            (BrokenApi(response=[]), "capabilities_invalid"),
        )
        for api, reason in scenarios:
            with self.subTest(reason=reason, response=api.response):
                result = run_real_acceptance(api, config)
                self.assertEqual((result.exit_code, result.reason), (2, reason))
                self.assertEqual(api.upload_calls, [])

    def test_current_deployed_capability_contract_fails_closed_without_upload(self) -> None:
        api = FakeRealRunApi()
        api._capabilities = {
            "items": {}, "runtime_versions": {}, "current_schema_hashes": {},
            "historical_schema_hashes": {}, "allows_existing_reads": True,
            "accepts_uploads": True, "accepts_new_jobs": True,
            "feature_enabled": True,
        }
        result = run_real_acceptance(
            api, RealRunConfig("a" * 40, "test", "approval"),
        )
        self.assertEqual((result.exit_code, result.reason), (2, "deployed_environment_mismatch"))
        self.assertEqual(api.upload_calls, [])

    def test_authorized_source_upload_failure_stops_before_case_execution(self) -> None:
        class UploadFailureApi(FakeRealRunApi):
            def upload_authorized_sources(self, *args) -> None:
                raise OSError("upload unavailable")

        result = run_real_acceptance(
            UploadFailureApi(), RealRunConfig("a" * 40, "test", "approval"),
        )
        self.assertEqual(
            (result.exit_code, result.reason),
            (2, "authorized_source_upload_failed"),
        )

        class AdapterUploadFailureApi(FakeRealRunApi):
            def upload_authorized_sources(self, *args) -> None:
                raise RealRunUnavailable("binding unavailable")

        adapter_result = run_real_acceptance(
            AdapterUploadFailureApi(), RealRunConfig("a" * 40, "test", "approval"),
        )
        self.assertEqual(
            (adapter_result.exit_code, adapter_result.reason),
            (2, "authorized_source_upload_failed"),
        )

    def test_configured_authority_without_real_adapter_fails_closed(self) -> None:
        with patch.dict(os.environ, {
            "AI_EDIT_V3_EXPECTED_SHA": "a" * 40,
            "AI_EDIT_V3_ACCEPTANCE_AUTHORIZATION_REF": "approval-test-only",
        }, clear=False):
            exit_code = main([
                "run", "--environment", "test", "--matrix", "matrix.json",
                "--run-id", "00000000-0000-4000-8000-000000000001",
                "--concurrency", "1",
            ])
        self.assertEqual(exit_code, 2)
    def test_restart_uses_persisted_job_and_idempotency_key(self) -> None:
        api = FakeV3Api()
        checkpoint = CaseCheckpoint(
            case_id="case_01",
            idempotency_key="acceptance:run-01:case-01",
            job_id="job-17",
        )

        resumed = resume_or_create_case(checkpoint, api)

        self.assertEqual(resumed.job_id, "job-17")
        self.assertEqual(resumed.idempotency_key, "acceptance:run-01:case-01")
        self.assertEqual(api.created_idempotency_keys, [])
        self.assertEqual(api.fetched_job_ids, ["job-17"])

    def test_run_cli_calls_execute_run_command_once(self) -> None:
        with patch("scripts.ai_edit_v3_acceptance.execute_run_command", return_value=0) as execute:
            exit_code = main([
                "run", "--environment", "local-fake", "--matrix", "matrix.json",
                "--run-id", "run-01", "--concurrency", "5", "--subset", "parallel-5",
            ])
        self.assertEqual(0, exit_code)
        execute.assert_called_once()
        args = execute.call_args.args[0]
        self.assertEqual("local-fake", args.environment)
        self.assertEqual(Path("matrix.json"), args.matrix)
        self.assertEqual("run-01", args.run_id)
        self.assertEqual(5, args.concurrency)
        self.assertEqual("parallel-5", args.subset)

    def test_run_cli_propagates_nonzero_exit(self) -> None:
        with patch("scripts.ai_edit_v3_acceptance.execute_run_command", return_value=4):
            exit_code = main([
                "run", "--environment", "local-fake", "--matrix", "matrix.json",
                "--run-id", "run-02", "--concurrency", "1",
            ])
        self.assertEqual(4, exit_code)

    def test_verify_cli_opens_named_report_and_runs_strict_verification(self) -> None:
        with patch("scripts.ai_edit_v3_acceptance.execute_verify_command", return_value=0) as execute:
            exit_code = main(["verify", "--report", "report.json", "--strict"])
        self.assertEqual(0, exit_code)
        execute.assert_called_once()
        args = execute.call_args.args[0]
        self.assertEqual(Path("report.json"), args.report)
        self.assertTrue(args.strict)

    def test_verify_cli_rejects_missing_or_invalid_evidence(self) -> None:
        from scripts.ai_edit_v3_acceptance import execute_verify_command

        with tempfile.TemporaryDirectory() as folder:
            missing = type("Args", (), {"report": Path(folder) / "missing.json", "strict": True})()
            invalid_path = Path(folder) / "invalid.json"
            invalid_path.write_text("{invalid", encoding="utf-8")
            invalid = type("Args", (), {"report": invalid_path, "strict": True})()
            self.assertEqual(4, execute_verify_command(missing))
            self.assertEqual(4, execute_verify_command(invalid))

    def test_collect_case_evidence_is_immutable_and_drops_signed_url(self) -> None:
        root = Path(__file__).resolve().parents[1]
        response = json.loads((
            root / "tests/fixtures/ai_edit_v3/acceptance-responses/completed.json"
        ).read_text(encoding="utf-8"))
        matrix = json.loads((
            root / "tests/fixtures/ai_edit_v3/acceptance-20.json"
        ).read_text(encoding="utf-8"))
        response["normalized_request_sha256"] = matrix["cases"][0]["source"]["sha256"]
        api = EvidenceFakeApi(response)
        with tempfile.TemporaryDirectory() as folder:
            case_dir = Path(folder) / "case_01"
            evidence = collect_case_evidence(api, matrix["cases"][0], case_dir)
            persisted = (case_dir / "evidence.json").read_text(encoding="utf-8")
            with self.assertRaises(FileExistsError):
                collect_case_evidence(api, matrix["cases"][0], case_dir)

        self.assertEqual("asset-local-01", evidence.asset_id)
        self.assertNotIn("playback_url", persisted)
        self.assertNotIn("token=fake-signed-value", persisted)
        self.assertEqual("https://playback.invalid/final.mp4?token=fake-signed-value", api.range_url)
        self.assertEqual(
            ["upload", "quote", "create:acceptance:local-fake:case_01", "poll", "result", "range"],
            api.operations[:6],
        )

    def test_collect_case_evidence_polls_with_backoff_and_monotonic_deadline(self) -> None:
        root = Path(__file__).resolve().parents[1]
        response = json.loads((
            root / "tests/fixtures/ai_edit_v3/acceptance-responses/completed.json"
        ).read_text(encoding="utf-8"))
        matrix = json.loads((
            root / "tests/fixtures/ai_edit_v3/acceptance-20.json"
        ).read_text(encoding="utf-8"))
        response["normalized_request_sha256"] = matrix["cases"][0]["source"]["sha256"]

        class PollingApi(EvidenceFakeApi):
            def __init__(self, payload):
                super().__init__(payload)
                self.states = iter(("queued", "failed", "rendering", "completed"))

            def get_job(self, job_id):
                self.operations.append("poll")
                return {"job_id": job_id, "status": next(self.states)}

        now = [100.0]
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            now[0] += seconds

        with tempfile.TemporaryDirectory() as folder:
            collect_case_evidence(
                PollingApi(response), matrix["cases"][0], Path(folder) / "case_01",
                clock=lambda: now[0], sleep=sleep, poll_timeout_seconds=60,
            )

        self.assertEqual([2.0, 3.0, 4.5], sleeps)

    def test_run_cases_strictly_rejects_new_or_existing_invalid_evidence(self) -> None:
        root = Path(__file__).resolve().parents[1]
        response = json.loads((
            root / "tests/fixtures/ai_edit_v3/acceptance-responses/completed.json"
        ).read_text(encoding="utf-8"))
        matrix = json.loads((
            root / "tests/fixtures/ai_edit_v3/acceptance-20.json"
        ).read_text(encoding="utf-8"))
        case = matrix["cases"][0]
        response["normalized_request_sha256"] = case["source"]["sha256"]

        class InvalidRangeApi(EvidenceFakeApi):
            def verify_range(self, playback_url):
                return False

        with tempfile.TemporaryDirectory() as folder:
            run_dir = Path(folder)
            summary = run_cases(
                AcceptanceConfig(
                    "strict-run", run_dir,
                    lambda _case: InvalidRangeApi(json.loads(json.dumps(response))),
                ),
                RunManifest((case,)), concurrency=1,
            )
            self.assertEqual(4, summary.result_code)
            evidence_path = run_dir / "case_01/evidence.json"
            self.assertTrue(evidence_path.exists())

            payload = json.loads(evidence_path.read_text(encoding="utf-8"))
            payload["quote"] = {}
            evidence_path.write_text(json.dumps(payload), encoding="utf-8")
            resumed = run_cases(
                AcceptanceConfig("strict-run", run_dir, lambda _case: None),
                RunManifest((case,)), concurrency=1,
            )
            self.assertEqual(4, resumed.result_code)

    def test_collect_resumes_persisted_checkpoint_without_upload_quote_or_create(self) -> None:
        root = Path(__file__).resolve().parents[1]
        response = json.loads((
            root / "tests/fixtures/ai_edit_v3/acceptance-responses/completed.json"
        ).read_text(encoding="utf-8"))
        matrix = json.loads((
            root / "tests/fixtures/ai_edit_v3/acceptance-20.json"
        ).read_text(encoding="utf-8"))
        response["normalized_request_sha256"] = matrix["cases"][0]["source"]["sha256"]
        api = EvidenceFakeApi(response)
        with tempfile.TemporaryDirectory() as folder:
            case_dir = Path(folder) / "case_01"
            case_dir.mkdir()
            (case_dir / "checkpoint.json").write_text(json.dumps({
                "case_id": "case_01",
                "idempotency_key": "acceptance:local-fake:case_01",
                "job_id": "job-local-01",
                "normalized_request_sha256": matrix["cases"][0]["source"]["sha256"],
                "upload_id": "upload-case_01",
                "quote": response["quote"],
            }), encoding="utf-8")
            evidence = collect_case_evidence(api, matrix["cases"][0], case_dir)
        self.assertEqual("acceptance:local-fake:case_01", evidence.idempotency_key)
        self.assertEqual(["poll", "poll", "result", "range"], api.operations)

    def test_local_fake_run_writes_twenty_immutable_case_evidence_files(self) -> None:
        root = Path(__file__).resolve().parents[1]
        matrix = root / "tests/fixtures/ai_edit_v3/acceptance-20.json"
        with tempfile.TemporaryDirectory() as folder, patch.dict(
            os.environ, {"AI_EDIT_V3_ACCEPTANCE_OUTPUT_ROOT": folder}
        ):
            argv = [
                "run", "--environment", "local-fake", "--matrix", str(matrix),
                "--run-id", "run-local-01", "--concurrency", "5",
            ]
            self.assertEqual(0, main(argv))
            run_dir = Path(folder) / "run-local-01"
            report_text = (run_dir / "report.json").read_text(encoding="utf-8")
            report = json.loads(report_text)
            self.assertEqual(20, report["case_count"])
            self.assertEqual("completed", report["status"])
            self.assertEqual(20, len(list(run_dir.glob("case_*/evidence.json"))))
            self.assertNotIn("token=", report_text)
            self.assertEqual(0, main([
                "verify", "--report", str(run_dir / "report.json"), "--strict",
            ]))
            evidence_path = run_dir / "case_01/evidence.json"
            tampered = json.loads(evidence_path.read_text(encoding="utf-8"))
            tampered["status"] = "refunded"
            evidence_path.write_text(json.dumps(tampered), encoding="utf-8")
            self.assertEqual(4, main([
                "verify", "--report", str(run_dir / "report.json"), "--strict",
            ]))
            self.assertEqual(4, main(argv))

    def test_local_fake_run_defaults_to_non_served_artifact_directory(self) -> None:
        root = Path(__file__).resolve().parents[1]
        matrix = root / "tests/fixtures/ai_edit_v3/acceptance-20.json"
        previous_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AI_EDIT_V3_ACCEPTANCE_OUTPUT_ROOT", None)
            try:
                os.chdir(folder)
                self.assertEqual(0, main([
                    "run", "--environment", "local-fake", "--matrix", str(matrix),
                    "--run-id", "run-default-01", "--concurrency", "1",
                    "--subset", "parallel-5",
                ]))
            finally:
                os.chdir(previous_cwd)
            self.assertTrue((
                Path(folder) / ".artifacts/ai-edit-v3/acceptance/run-default-01/report.json"
            ).is_file())
            self.assertFalse((
                Path(folder) / "server/content_out/ai-edit-v3-acceptance/run-default-01"
            ).exists())

    def test_failed_local_fake_run_reports_and_verifies_exit_three(self) -> None:
        root = Path(__file__).resolve().parents[1]
        matrix = root / "tests/fixtures/ai_edit_v3/acceptance-20.json"
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {
            "AI_EDIT_V3_ACCEPTANCE_OUTPUT_ROOT": folder,
            "AI_EDIT_V3_ACCEPTANCE_FAKE_RESPONSE": "refunded.json",
        }):
            argv = [
                "run", "--environment", "local-fake", "--matrix", str(matrix),
                "--run-id", "run-refunded-01", "--concurrency", "1",
                "--subset", "parallel-5",
            ]
            self.assertEqual(3, main(argv))
            report = Path(folder) / "run-refunded-01/report.json"
            self.assertEqual(3, main(["verify", "--report", str(report), "--strict"]))

    def test_test_environment_stops_before_real_api_construction(self) -> None:
        with patch("scripts.ai_edit_v3_acceptance.build_real_run_api") as build:
            exit_code = main([
                "run", "--environment", "test", "--matrix", "matrix.json",
                "--run-id", "run-test-01", "--concurrency", "1",
            ])
        self.assertEqual(2, exit_code)
        build.assert_not_called()

    def test_local_fake_run_resumes_after_partial_process_failure(self) -> None:
        import scripts.ai_edit_v3_acceptance as command

        root = Path(__file__).resolve().parents[1]
        matrix = root / "tests/fixtures/ai_edit_v3/acceptance-20.json"
        original = command.collect_case_evidence

        def crash_on_second(api, case, run_dir, *, run_id):
            if case["case_id"] == "case_02":
                raise OSError("simulated_process_loss")
            return original(api, case, run_dir, run_id=run_id)

        with tempfile.TemporaryDirectory() as folder, patch.dict(
            os.environ, {"AI_EDIT_V3_ACCEPTANCE_OUTPUT_ROOT": folder}
        ):
            argv = [
                "run", "--environment", "local-fake", "--matrix", str(matrix),
                "--run-id", "run-resume-01", "--concurrency", "1",
            ]
            with patch(
                "server.content_domains.ai_edit_v3.acceptance_export.collect_case_evidence",
                side_effect=crash_on_second,
            ):
                self.assertEqual(4, main(argv))
            run_dir = Path(folder) / "run-resume-01"
            self.assertTrue((run_dir / "case_01/evidence.json").exists())
            self.assertFalse((run_dir / "report.json").exists())
            self.assertEqual(0, main(argv))
            self.assertEqual(20, len(list(run_dir.glob("case_*/evidence.json"))))

    def test_session_is_injected_only_from_environment_or_prompt_and_redacted(self) -> None:
        prompted: list[str] = []
        from_environment = load_test_session(
            {"AI_EDIT_V3_TEST_SESSION": "session-from-env"},
            lambda prompt: prompted.append(prompt) or "unexpected",
        )
        self.assertEqual("session-from-env", from_environment.reveal())
        self.assertEqual([], prompted)
        self.assertNotIn("session-from-env", repr(from_environment))
        from_prompt = load_test_session(
            {}, lambda prompt: prompted.append(prompt) or "session-from-prompt"
        )
        self.assertEqual("session-from-prompt", from_prompt.reveal())
        self.assertEqual(1, len(prompted))
        self.assertNotIn("session-from-prompt", repr(from_prompt))

    def test_all_five_fake_terminal_response_fixtures_are_classified(self) -> None:
        root = Path(__file__).resolve().parents[1]
        expected = {
            "completed.json": 0,
            "refunded.json": 3,
            "prehold_absent.json": 3,
            "failed_reconciliation_pending.json": 3,
            "failed_asset_decision_pending.json": 3,
        }
        fixture_dir = root / "tests/fixtures/ai_edit_v3/acceptance-responses"
        for name, code in expected.items():
            with self.subTest(name=name):
                response = json.loads((fixture_dir / name).read_text(encoding="utf-8"))
                self.assertEqual(code, terminal_result_code(response))

    def test_nested_signed_provider_url_is_rejected_before_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "evidence.json"
            with self.assertRaises(ValueError):
                write_json_exclusive(path, {
                    "provider_usage": [{
                        "download_url": "https://cos.invalid/a?X-Amz-Signature=credential"
                    }]
                })
            self.assertFalse(path.exists())

    def test_atomic_writer_never_publishes_partial_final_file(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "evidence.json"
            with patch("server.content_domains.ai_edit_v3.acceptance_export.os.link", side_effect=OSError):
                with self.assertRaises(OSError):
                    write_json_exclusive(path, {"status": "completed"})
            self.assertFalse(path.exists())

    def test_failed_terminal_fixtures_use_common_collect_and_verify_path(self) -> None:
        root = Path(__file__).resolve().parents[1]
        matrix = json.loads((
            root / "tests/fixtures/ai_edit_v3/acceptance-20.json"
        ).read_text(encoding="utf-8"))
        fixture_dir = root / "tests/fixtures/ai_edit_v3/acceptance-responses"
        for name in (
            "refunded.json", "prehold_absent.json",
            "failed_reconciliation_pending.json", "failed_asset_decision_pending.json",
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as folder:
                response = json.loads((fixture_dir / name).read_text(encoding="utf-8"))
                response["normalized_request_sha256"] = matrix["cases"][0]["source"]["sha256"]
                api = EvidenceFakeApi(response)
                case_dir = Path(folder) / "case_01"
                evidence = collect_case_evidence(api, matrix["cases"][0], case_dir)
                self.assertEqual(3, terminal_result_code({"status": evidence.status, "quote": evidence.quote}))
                self.assertTrue(verify_case_evidence(case_dir, strict=True).passed)
                creates = [item for item in api.operations if item.startswith("create:")]
                self.assertEqual(["create:acceptance:local-fake:case_01"], creates)

    def test_strict_verifier_rejects_forged_refunded_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            case_dir = Path(folder)
            (case_dir / "evidence.json").write_text(json.dumps({
                "case_id": "wrong",
                "idempotency_key": "acceptance:other:wrong",
                "status": "refunded",
                "normalized_request_sha256": "garbage",
                "quote": {},
                "job_id": "",
                "stage_timings_ms": {},
                "material_decisions": [],
                "provider_usage": [],
                "audio_evidence": {},
                "qc": {},
                "settlement": {},
                "range_verified": False
            }), encoding="utf-8")
            verdict = verify_case_evidence(case_dir, strict=True)
        self.assertFalse(verdict.passed)
        self.assertIn("quote_invalid", verdict.errors)
        self.assertIn("settlement_invalid", verdict.errors)

    def test_completed_evidence_rejects_refunded_settlement_and_wrong_request_sha(self) -> None:
        root = Path(__file__).resolve().parents[1]
        response = json.loads((
            root / "tests/fixtures/ai_edit_v3/acceptance-responses/completed.json"
        ).read_text(encoding="utf-8"))
        matrix = json.loads((
            root / "tests/fixtures/ai_edit_v3/acceptance-20.json"
        ).read_text(encoding="utf-8"))
        response["normalized_request_sha256"] = matrix["cases"][0]["source"]["sha256"]
        with tempfile.TemporaryDirectory() as folder:
            case_dir = Path(folder) / "case_01"
            collect_case_evidence(EvidenceFakeApi(response), matrix["cases"][0], case_dir)
            path = case_dir / "evidence.json"
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["settlement"] = {
                "state": "refunded", "charged_points": 0, "refunded_points": 30,
            }
            path.write_text(json.dumps(payload), encoding="utf-8")
            verdict = verify_case_evidence(case_dir, strict=True)
        self.assertFalse(verdict.passed)
        self.assertIn("status_settlement_mismatch", verdict.errors)

        wrong = json.loads(json.dumps(response))
        wrong["normalized_request_sha256"] = "9" * 64
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(ValueError, "normalized_request_sha256_mismatch"):
                collect_case_evidence(
                    EvidenceFakeApi(wrong), matrix["cases"][0], Path(folder) / "case_01"
                )


if __name__ == "__main__":
    unittest.main()
