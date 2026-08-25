from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from server import matrix_template_api as matrix


class MatrixTemplateApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.skill = self.root / "skill"
        (self.skill / "assets/templates").mkdir(parents=True)
        (self.skill / "scripts").mkdir()
        templates = [{
            "id": "native-bold" if index == 0 else f"template-{index:02d}",
            "name": f"模板 {index}", "description": "测试模板",
            "tags": ["测试"], "layout": {}, "render": {},
        } for index in range(13)]
        (self.skill / "assets/templates/catalog.json").write_text(
            json.dumps({"version": 1, "templates": templates}, ensure_ascii=False),
            encoding="utf-8",
        )
        (self.skill / "scripts/render_video.py").write_text("# fixture\n", encoding="utf-8")
        self.service = matrix.MatrixTemplateService(
            data_root=self.root / "data",
            skill_root=self.skill,
            library_url="http://127.0.0.1:8111",
            library_token="library-token",
            start_worker=False,
        )

    def tearDown(self):
        self.service.shutdown()
        self.temp.cleanup()

    def test_catalog_and_payload_contract(self):
        self.assertEqual(13, len(self.service.catalog))
        payload = self.service.validate_payload({
            "top_text": "AI 工作流",
            "bottom_text": "评论区留下关键词",
            "template_id": "native-bold",
        })
        self.assertEqual(8.0, payload["duration"])
        self.assertTrue(payload["bgm"])
        for invalid in (
            {"top_text": "A", "bottom_text": "行动"},
            {"top_text": "有效标题", "bottom_text": "A"},
            {"top_text": "有效标题", "bottom_text": "有效行动", "template_id": "bad"},
        ):
            with self.assertRaises(ValueError):
                self.service.validate_payload(invalid)

    def test_request_id_is_idempotent_and_payload_bound(self):
        body = {"top_text": "AI 工作流", "bottom_text": "评论区留下关键词"}
        first = self.service.submit(body, "request-1")
        second = self.service.submit(body, "request-1")
        self.assertEqual(first["job_id"], second["job_id"])
        with self.assertRaisesRegex(ValueError, "another payload"):
            self.service.submit({**body, "bottom_text": "私信领取资料"}, "request-1")

    def test_material_selection_requires_video_unique_sha_and_optional_bgm(self):
        payload = self.service.validate_payload({
            "top_text": "AI 工作流", "bottom_text": "评论区留下关键词",
        })
        captured = {}

        def selection(_method, _path, body):
            captured.update(body)
            return {"materials": [
                {"scene_id": "media_01", "sha256": "a" * 64, "media_type": "video", "record_id": "v1"},
                {"scene_id": "media_02", "sha256": "b" * 64, "media_type": "image", "record_id": "i1"},
                {"scene_id": "bgm", "sha256": "c" * 64, "media_type": "bgm", "record_id": "m1"},
            ]}

        with mock.patch.object(self.service, "_library_request", side_effect=selection):
            materials = self.service._select_materials(payload, "f" * 32)
        self.assertEqual(["video", "image", "bgm"], [item["media_type"] for item in materials])
        self.assertEqual("video", captured["scenes"][0]["media_type"])
        self.assertEqual("portrait", captured["orientation"])
        self.assertEqual(3, len(set(item["sha256"] for item in materials)))

    def test_execute_builds_skill_project_and_returns_provenance(self):
        payload = self.service.validate_payload({
            "top_text": "AI 工作流", "bottom_text": "评论区留下关键词",
        })
        job, _ = self.service.store.create("execute-1", payload)
        materials = [
            {"scene_id": "media_01", "sha256": "a" * 64, "media_type": "video", "record_id": "v1", "match_level": "exact"},
            {"scene_id": "media_02", "sha256": "b" * 64, "media_type": "image", "record_id": "i1", "match_level": "loose"},
            {"scene_id": "bgm", "sha256": "c" * 64, "media_type": "bgm", "record_id": "m1", "match_level": "random"},
        ]
        counter = iter(range(3))

        def download(item, target):
            suffix = ".mp4" if item["media_type"] == "video" else ".jpg" if item["media_type"] == "image" else ".mp3"
            path = target / (str(next(counter)) + suffix)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"asset")
            return path

        def render(project_path):
            output = project_path.parent / "output/final.mp4"
            output.parent.mkdir(parents=True)
            output.write_bytes(b"video")

        with mock.patch.object(self.service, "_select_materials", return_value=materials), \
             mock.patch.object(self.service, "_download", side_effect=download), \
             mock.patch.object(self.service, "_render", side_effect=render), \
             mock.patch.object(self.service, "_probe", return_value={"duration": 8.0, "width": 1080, "height": 1920}):
            result = self.service._execute(job["job_id"])

        project = json.loads((self.service.data_root / job["job_id"] / "project.json").read_text(encoding="utf-8"))
        self.assertEqual({"template_id": "native-bold"}, project["layout"])
        self.assertFalse(project["voice"]["enabled"])
        self.assertEqual(2, len(project["scenes"][0]["media"]))
        self.assertEqual("huangque-internal-api", project["material_library"]["index_source"])
        self.assertEqual("/v1/files/%s.mp4" % job["job_id"], result["file_url"])
        self.assertEqual(["v1", "i1", "m1"], [item["record_id"] for item in result["material_manifest"]])

    def test_http_auth_templates_submit_and_status(self):
        server = matrix.build_server("127.0.0.1", 0, self.service, "api-token")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:%d" % server.server_port

        def request(path, method="GET", body=None, token=None, request_id=None):
            data = json.dumps(body).encode() if body is not None else None
            req = urllib.request.Request(base + path, data=data, method=method)
            if token:
                req.add_header("Authorization", "Bearer " + token)
            if request_id:
                req.add_header("X-Request-Id", request_id)
            return urllib.request.urlopen(req, timeout=3)

        try:
            with request("/health") as response:
                self.assertEqual(13, json.load(response)["templates"])
            with self.assertRaises(urllib.error.HTTPError) as denied:
                request("/v1/templates")
            self.assertEqual(401, denied.exception.code)
            with request("/v1/templates", token="api-token") as response:
                self.assertEqual(13, len(json.load(response)["templates"]))
            with request(
                "/v1/jobs", "POST",
                {"top_text": "AI 工作流", "bottom_text": "评论区留下关键词"},
                "api-token", "http-request-1",
            ) as response:
                job = json.load(response)
            self.assertEqual("pending", job["status"])
            with request("/v1/jobs/" + job["job_id"], token="api-token") as response:
                self.assertEqual(job["job_id"], json.load(response)["job_id"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_shutdown_terminates_active_render_process_group(self):
        project_root = self.root / "cancel-job"
        project_root.mkdir()
        project = project_root / "project.json"
        project.write_text("{}", encoding="utf-8")
        (self.skill / "scripts/render_video.py").write_text(
            "import time\ntime.sleep(30)\n", encoding="utf-8"
        )
        errors = []

        def render():
            try:
                self.service._render(project)
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=render)
        thread.start()
        deadline = time.time() + 3
        while time.time() < deadline:
            with self.service.process_lock:
                if self.service.active_process is not None:
                    break
            time.sleep(0.02)
        self.service.shutdown()
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertTrue(errors)
        self.assertFalse((project_root / "output/final.mp4").exists())


if __name__ == "__main__":
    unittest.main()
