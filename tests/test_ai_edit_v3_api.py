import io
import json
import unittest

from server.content_domains.ai_edit_v3.api import dispatch
from server.content_domains.ai_edit_v3.service import ServiceError


class FakeHandler:
    def __init__(self, body=None, headers=None):
        raw = b"" if body is None else body if isinstance(body, bytes) else body.encode("utf-8")
        self.headers = dict(headers or {})
        if body is not None and "Content-Length" not in self.headers:
            self.headers["Content-Length"] = str(len(raw))
        self.rfile = io.BytesIO(raw)
        self.wfile = io.BytesIO()
        self.statuses = []
        self.response_headers = []
        self.ended = 0

    def send_response(self, status):
        self.statuses.append(status)

    def send_header(self, name, value):
        self.response_headers.append((name, value))

    def end_headers(self):
        self.ended += 1

    def response_json(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))


class StubService:
    def __init__(self):
        self.calls = []
        self.failure = None

    def now(self):
        return 1_000

    def _result(self, name, *arguments, **keywords):
        self.calls.append((name, arguments, keywords))
        if self.failure is not None:
            raise self.failure
        return {"route": name}

    def get_capabilities(self, owner):
        return self._result("capabilities", owner)

    def list_platform_assets(self, owner):
        return self._result("platform-assets", owner)

    def list_audio_assets(self, owner):
        return self._result("audio-assets", owner)

    def list_voices(self, owner):
        return self._result("voices", owner)

    def list_templates(self, owner):
        return self._result("templates", owner)

    def create_upload(self, owner, request, *, now):
        return self._result("uploads", owner, request, now=now)

    def complete_upload(self, owner, upload_id, *, now):
        return self._result("upload-complete", owner, upload_id, now=now)

    def create_material(self, owner, upload_id, *, now):
        return self._result("materials", owner, upload_id, now=now)

    def quote(self, owner, request, *, now):
        return self._result("quote", owner, request, now=now)

    def create_job(self, owner, request, quote_id, idempotency_key, *, now):
        return self._result(
            "jobs-create", owner, request, quote_id, idempotency_key, now=now
        )

    def list_jobs(self, owner, *, cursor, limit):
        return self._result("jobs-list", owner, cursor=cursor, limit=limit)

    def get_job(self, owner, job_id):
        return self._result("job-detail", owner, job_id)

    def get_plan(self, owner, job_id):
        return self._result("job-plan", owner, job_id)

    def get_result(self, owner, job_id):
        return self._result("job-result", owner, job_id)

    def retry_job(self, owner, job_id, idempotency_key, *, now):
        return self._result("job-retry", owner, job_id, idempotency_key, now=now)


class V3ApiDispatchTests(unittest.TestCase):
    user = {"username": "alice"}

    def setUp(self):
        self.service = StubService()

    def call(self, method, path, *, body=None, headers=None, user=user):
        if body is not None and not isinstance(body, (str, bytes)):
            body = json.dumps(body, separators=(",", ":"))
        handler = FakeHandler(body, headers)
        handled = dispatch(
            handler,
            method,
            path,
            user,
            service=self.service,
        )
        return handled, handler

    def test_dispatches_every_section_17_route(self):
        routes = (
            ("GET", "/api/v3/edit/capabilities", None, {}, "capabilities", 200),
            ("GET", "/api/v3/edit/platform-assets", None, {}, "platform-assets", 200),
            ("GET", "/api/v3/edit/audio-assets", None, {}, "audio-assets", 200),
            ("GET", "/api/v3/edit/voices", None, {}, "voices", 200),
            ("GET", "/api/v3/edit/templates", None, {}, "templates", 200),
            (
                "POST",
                "/api/v3/edit/uploads",
                {
                    "upload_type": "material_image",
                    "filename": "image.png",
                    "content_type": "image/png",
                    "size_bytes": 10,
                },
                {},
                "uploads",
                201,
            ),
            (
                "POST",
                "/api/v3/edit/uploads/upload-1/complete",
                {},
                {},
                "upload-complete",
                200,
            ),
            (
                "POST",
                "/api/v3/edit/materials",
                {"upload_id": "upload-1"},
                {},
                "materials",
                201,
            ),
            (
                "POST",
                "/api/v3/edit/quote",
                {"input_type": "uploaded_video"},
                {},
                "quote",
                201,
            ),
            (
                "POST",
                "/api/v3/edit/jobs",
                {"quote_id": "quote-1", "input_type": "uploaded_video"},
                {"Idempotency-Key": "client-key-1"},
                "jobs-create",
                202,
            ),
            ("GET", "/api/v3/edit/jobs?cursor=next&limit=7", None, {}, "jobs-list", 200),
            ("GET", "/api/v3/edit/jobs/job-1", None, {}, "job-detail", 200),
            ("GET", "/api/v3/edit/jobs/job-1/plan", None, {}, "job-plan", 200),
            ("GET", "/api/v3/edit/jobs/job-1/result", None, {}, "job-result", 200),
            (
                "POST",
                "/api/v3/edit/jobs/job-1/retry",
                {},
                {"Idempotency-Key": "client-key-2"},
                "job-retry",
                202,
            ),
        )

        for method, path, body, headers, expected_call, status in routes:
            with self.subTest(method=method, path=path):
                self.service.calls.clear()
                handled, handler = self.call(
                    method, path, body=body, headers=headers
                )
                self.assertTrue(handled)
                self.assertEqual(handler.statuses, [status])
                self.assertEqual(handler.ended, 1)
                self.assertEqual(handler.response_json()["route"], expected_call)
                self.assertEqual(self.service.calls[0][0], expected_call)

    def test_non_v3_is_unhandled_and_unknown_v3_is_one_404_response(self):
        handled, handler = self.call("GET", "/api/v2/edit/jobs")
        self.assertFalse(handled)
        self.assertEqual(handler.statuses, [])

        handled, handler = self.call("GET", "/api/v3/edit/not-a-route")
        self.assertTrue(handled)
        self.assertEqual(handler.statuses, [404])
        self.assertEqual(handler.ended, 1)
        self.assertEqual(handler.response_json()["error_code"], "not_found")

    def test_all_routes_require_authentication_without_calling_service(self):
        for method, path, body in (
            ("GET", "/api/v3/edit/capabilities", None),
            ("POST", "/api/v3/edit/uploads", {}),
            ("GET", "/api/v3/edit/jobs/job-1", None),
        ):
            with self.subTest(path=path):
                self.service.calls.clear()
                handled, handler = self.call(method, path, body=body, user=None)
                self.assertTrue(handled)
                self.assertEqual(handler.statuses, [401])
                self.assertEqual(self.service.calls, [])

    def test_strict_json_rejects_duplicates_nonfinite_and_oversize(self):
        for body in ('{"upload_id":"one","upload_id":"two"}', '{"value":NaN}'):
            with self.subTest(body=body):
                handled, handler = self.call(
                    "POST", "/api/v3/edit/materials", body=body
                )
                self.assertTrue(handled)
                self.assertEqual(handler.statuses, [400])
                self.assertEqual(handler.response_json()["error_code"], "invalid_json")
        handled, handler = self.call(
            "POST",
            "/api/v3/edit/materials",
            body=b"{}",
            headers={"Content-Length": "65537"},
        )
        self.assertTrue(handled)
        self.assertEqual(handler.statuses, [413])
        self.assertEqual(self.service.calls, [])

    def test_job_and_retry_require_bounded_client_idempotency_key(self):
        cases = (
            ("/api/v3/edit/jobs", {"quote_id": "quote-1"}, {}),
            (
                "/api/v3/edit/jobs",
                {"quote_id": "quote-1"},
                {"Idempotency-Key": "x" * 129},
            ),
            (
                "/api/v3/edit/jobs/job-1/retry",
                {},
                {"Idempotency-Key": "retry:stolen"},
            ),
        )
        for path, body, headers in cases:
            with self.subTest(path=path, headers=headers):
                handled, handler = self.call(
                    "POST", path, body=body, headers=headers
                )
                self.assertTrue(handled)
                self.assertEqual(handler.statuses, [400])
                self.assertEqual(
                    handler.response_json()["error_code"],
                    "idempotency_key_invalid",
                )

    def test_service_errors_are_sanitized_and_capacity_sets_retry_after(self):
        self.service.failure = ServiceError(
            "capacity_unavailable",
            "capacity is temporarily unavailable",
            status=503,
            retry_after=19,
        )
        _handled, handler = self.call(
            "POST", "/api/v3/edit/quote", body={"input_type": "uploaded_video"}
        )
        self.assertEqual(handler.statuses, [503])
        self.assertIn(("Retry-After", "19"), handler.response_headers)
        self.assertNotIn("Traceback", repr(handler.response_json()))

        self.service.failure = ServiceError(
            "not_found", "resource was not found", status=404
        )
        _handled, handler = self.call("GET", "/api/v3/edit/jobs/foreign-job")
        self.assertEqual(handler.statuses, [404])
        self.assertEqual(handler.response_json()["error_code"], "not_found")

        self.service.failure = RuntimeError(
            "sqlite C:\\private\\ai_edit_v3.db?token=secret"
        )
        _handled, handler = self.call("GET", "/api/v3/edit/jobs/job-1")
        self.assertEqual(handler.statuses, [500])
        payload = repr(handler.response_json())
        self.assertNotIn("private", payload)
        self.assertNotIn("token", payload)
        self.assertNotIn("sqlite", payload)

    def test_disabled_write_and_unready_catalog_are_explicit_503(self):
        for path, error_code in (
            ("/api/v3/edit/uploads", "feature_disabled"),
            ("/api/v3/edit/platform-assets", "platform_assets_unavailable"),
        ):
            with self.subTest(path=path):
                self.service.failure = ServiceError(
                    error_code, "capability is unavailable", status=503
                )
                method = "POST" if path.endswith("uploads") else "GET"
                body = {} if method == "POST" else None
                _handled, handler = self.call(method, path, body=body)
                self.assertEqual(handler.statuses, [503])
                self.assertEqual(handler.response_json()["error_code"], error_code)

    def test_method_mismatch_and_invalid_job_query_are_bounded(self):
        handled, handler = self.call("DELETE", "/api/v3/edit/jobs/job-1")
        self.assertTrue(handled)
        self.assertEqual(handler.statuses, [405])

        for query in ("limit=0", "limit=101", "limit=abc", "unknown=1"):
            with self.subTest(query=query):
                _handled, handler = self.call(
                    "GET", f"/api/v3/edit/jobs?{query}"
                )
                self.assertEqual(handler.statuses, [400])


if __name__ == "__main__":
    unittest.main()
