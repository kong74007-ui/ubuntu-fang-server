from __future__ import annotations

import hashlib
import json
import random
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from server import matrix_template_api as matrix


class MatrixTemplateApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.skill = self.root / "skill"
        (self.skill / "assets/templates").mkdir(parents=True)
        font_root = self.skill / "assets/fonts"
        font_root.mkdir()
        bundled = []
        for index, family in enumerate(sorted(matrix.BASE_FONT_FAMILIES)):
            path = font_root / f"base-{index}.ttf"
            path.write_bytes(family.encode("utf-8"))
            bundled.append({
                "family": family, "file": path.name,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            })
        (font_root / "sources.json").write_text(
            json.dumps({"fonts": bundled}), encoding="utf-8"
        )
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
        self.assertNotIn("font_family", payload)
        fonts = self.service.public_fonts()
        self.assertEqual({""} | matrix.BASE_FONT_FAMILIES, {
            item["value"] for item in fonts
        })
        self.assertEqual("自动搭配", fonts[0]["label"])
        for invalid in (
            {"top_text": "A", "bottom_text": "行动"},
            {"top_text": "有效标题", "bottom_text": "A"},
            {"top_text": "有效标题", "bottom_text": "有效行动", "template_id": "bad"},
            {"top_text": "有效标题", "bottom_text": "有效行动", "font_family": "Missing Font"},
        ):
            with self.assertRaises(ValueError):
                self.service.validate_payload(invalid)

    def test_duration_boundary_counts_visible_chinese_and_english_only(self):
        accepted = self.service.validate_payload({
            "top_text": "中" * 60,
            "bottom_text": "A" * 7 + "，。！？",
            "template_id": "native-bold",
        })
        self.assertEqual(14.9, accepted["duration"])
        with self.assertRaisesRegex(ValueError, "文案过长"):
            self.service.validate_payload({
                "top_text": "中" * 60,
                "bottom_text": "A" * 8,
                "template_id": "native-bold",
            })

    def test_balanced_title_is_frozen_without_changing_source_copy(self):
        title = "想开店又怕养团队？1个人+AI员工也能运行一家门店"
        expected = "想开店又怕养团队？\n1个人+AI员工\n也能运行一家门店"
        self.assertEqual(expected, matrix._balanced_title(title, 12, 3))
        self.assertEqual(
            "想开店又怕养团队？\n1个人+AI员工也能运行一家门店",
            matrix._balanced_title(title, 13, 2),
        )
        job = self.service.submit({
            "top_text": title,
            "bottom_text": "轻团队也能稳定运营",
            "template_id": "native-bold",
            "bgm": False,
        }, "balanced-title")
        payload = json.loads(self.service.store.get(job["job_id"])["payload"])
        self.assertEqual(title, payload["top_text"])
        self.assertEqual(expected, payload["_display_top_text"])
        replay = self.service.submit({
            "top_text": title,
            "bottom_text": "轻团队也能稳定运营",
            "template_id": "native-bold",
            "bgm": False,
        }, "balanced-title")
        self.assertEqual(job["job_id"], replay["job_id"])

    def test_balanced_title_preserves_content_and_never_emits_empty_lines(self):
        english = matrix._balanced_title(
            "ABCDEFGHIJKLM NOPQRSTUVWXYZ", 12, 3
        )
        self.assertEqual(
            ["ABCDEFGHIJKLM", "NOPQRSTUVWXYZ"], english.splitlines()
        )

        samples = [
            "品牌  Alpha   X200  已经支持  3个 门店",
            "（新品）AI助手，不会拆开标点",
            "想开店又怕养团队？1个人+AI员工也能运行一家门店",
            "MODEL-X200 Pro 现在支持10家门店",
            "数据增长40%，但是成本没有增加。",
        ]
        rng = random.Random(20260827)
        atoms = ["AI", "X200", "品牌", "门店", "3个", "已经", "不会", "增长40%", "（新品）"]
        for _ in range(40):
            samples.append(" ".join(rng.choice(atoms) for _ in range(rng.randint(2, 8))))

        closing = set("，。！？；：、,.!?;:)]}）】》」』+%％")
        opening = set("([{（【《「『+")
        for source in samples:
            with self.subTest(source=source):
                first = matrix._balanced_title(source, 12, 4)
                second = matrix._balanced_title(source, 12, 4)
                self.assertEqual(first, second)
                lines = first.splitlines()
                self.assertTrue(lines)
                self.assertLessEqual(len(lines), 4)
                self.assertTrue(all(line.strip() for line in lines))
                self.assertTrue(all(line[0] not in closing for line in lines))
                self.assertTrue(all(line[-1] not in opening for line in lines))
                normalized = " ".join(source.split())
                candidates = {""}
                for index, line in enumerate(lines):
                    if index == 0:
                        candidates = {line}
                    else:
                        candidates = {
                            prefix + separator + line
                            for prefix in candidates for separator in ("", " ")
                        }
                self.assertIn(normalized, candidates)

    def test_font_selection_uses_baseline_and_only_available_private_fonts(self):
        allowed = matrix.BASE_FONT_FAMILIES
        self.assertEqual(13, len(matrix.FONT_VARIANTS))
        represented = set()
        for template_id in matrix.FONT_VARIANTS:
            with self.subTest(template_id=template_id):
                selections = [
                    matrix._font_selection(template_id, format(index, "032x"))
                    for index in range(30)
                ]
                self.assertEqual(
                    selections[7],
                    matrix._font_selection(template_id, format(7, "032x")),
                )
                self.assertGreaterEqual(
                    len({item["variant"] for item in selections}), 2)
                self.assertTrue(all(
                    item["top_font"] in allowed and item["bottom_font"] in allowed
                    for item in selections
                ))
                for _, top_font, bottom_font in matrix.FONT_VARIANTS[template_id]:
                    represented.update((top_font, bottom_font))
        self.assertEqual(allowed, represented)
        fallback = matrix._font_selection("future-template", "f" * 32)
        self.assertIn(fallback["top_font"], allowed)
        self.assertIn(fallback["bottom_font"], allowed)
        private_represented = {
            font for options in matrix.PRIVATE_FONT_VARIANTS.values()
            for _, top_font, bottom_font in options
            for font in (top_font, bottom_font)
            if font in matrix.PRIVATE_FONT_FAMILIES
        }
        self.assertEqual(matrix.PRIVATE_FONT_FAMILIES, private_represented)
        selections = [
            matrix._font_selection("native-bold", format(index, "032x"), {"AaHouDiHei"})
            for index in range(100)
        ]
        self.assertTrue(any(item["top_font"] == "AaHouDiHei" for item in selections))
        self.assertTrue(all(
            item["top_font"] in allowed | {"AaHouDiHei"}
            and item["bottom_font"] in allowed | {"AaHouDiHei"}
            for item in selections
        ))

    def test_private_font_manifest_is_verified_and_staged_inside_job(self):
        private_root = self.root / "private-fonts"
        private_root.mkdir()
        private_file = private_root / "AaHouDiHei.ttf"
        private_file.write_bytes(b"private-font")
        private_hash = hashlib.sha256(private_file.read_bytes()).hexdigest()
        (private_root / "sources.json").write_text(json.dumps({
            "schema_version": 1,
            "fonts": [{
                "family": "AaHouDiHei", "file": private_file.name,
                "sha256": private_hash, "authorized": True,
            }],
        }), encoding="utf-8")
        self.service.private_fonts = matrix._load_private_fonts(private_root)
        selected_payload = self.service.validate_payload({
            "top_text": "指定字体标题", "bottom_text": "指定字体行动文案",
            "font_family": "AaHouDiHei", "bgm": False,
        })
        frozen = self.service._freeze_font_provenance("b" * 32, selected_payload)
        self.assertEqual({
            "variant": "user-selected",
            "top_font": "AaHouDiHei",
            "bottom_font": "AaHouDiHei",
        }, frozen["_font_provenance"]["selection"])
        self.assertIn("AaHouDiHei", {
            item["value"] for item in self.service.public_fonts()
        })
        job_root = self.root / "data" / ("a" * 32)
        relative = self.service._stage_project_fonts(job_root, {"fonts": [{
            "family": "AaHouDiHei", "file": private_file.name,
            "sha256": private_hash, "source": "private",
        }]})
        self.assertEqual("assets/fonts", relative)
        staged_root = job_root / relative
        staged = json.loads((staged_root / "sources.json").read_text(encoding="utf-8"))
        self.assertEqual(5, len(staged["fonts"]))
        self.assertEqual(private_hash, hashlib.sha256(
            (staged_root / private_file.name).read_bytes()
        ).hexdigest())
        private_file.write_bytes(b"changed")
        with self.assertRaisesRegex(matrix.MatrixTemplateError, "has changed"):
            matrix._load_private_fonts(private_root)

    def test_job_freezes_font_selection_sha_and_bundle_across_restart(self):
        body = {
            "top_text": "AI 工作流", "bottom_text": "评论区留下关键词",
            "template_id": "native-bold", "bgm": False,
        }
        old_job = self.service.submit(body, "font-before-private")
        old_payload = json.loads(self.service.store.get(old_job["job_id"])["payload"])
        old_provenance = old_payload["_font_provenance"]
        empty_fingerprint = self.service.health()["private_font_bundle_sha256"]

        private_root = self.root / "restart-private-fonts"
        private_root.mkdir()
        private_file = private_root / "AaHouDiHei.ttf"
        private_file.write_bytes(b"private-font-v1")
        private_hash = hashlib.sha256(private_file.read_bytes()).hexdigest()
        (private_root / "sources.json").write_text(json.dumps({
            "schema_version": 1,
            "fonts": [{
                "family": "AaHouDiHei", "file": private_file.name,
                "sha256": private_hash, "authorized": True,
            }],
        }), encoding="utf-8")
        restarted = matrix.MatrixTemplateService(
            data_root=self.service.data_root,
            skill_root=self.skill,
            library_url="http://127.0.0.1:8111",
            library_token="library-token",
            private_font_root=private_root,
            start_worker=False,
        )
        try:
            recovered = json.loads(restarted.store.get(old_job["job_id"])["payload"])
            self.assertEqual(old_provenance, recovered["_font_provenance"])
            self.assertNotEqual(
                empty_fingerprint,
                restarted.health()["private_font_bundle_sha256"],
            )
            private_job_id = next(
                format(index, "032x") for index in range(1, 1000)
                if matrix._font_selection("native-bold", format(index, "032x"), {"AaHouDiHei"})["top_font"] == "AaHouDiHei"
            )
            with mock.patch.object(matrix.uuid, "uuid4", return_value=SimpleNamespace(hex=private_job_id)):
                new_job = restarted.submit(body, "font-after-private")
            new_payload = json.loads(restarted.store.get(new_job["job_id"])["payload"])
            provenance = new_payload["_font_provenance"]
            self.assertEqual("AaHouDiHei", provenance["selection"]["top_font"])
            self.assertEqual(private_hash, next(
                item["sha256"] for item in provenance["fonts"]
                if item["family"] == "AaHouDiHei"
            ))
            private_file.write_bytes(b"private-font-drift")
            with self.assertRaisesRegex(matrix.MatrixTemplateError, "has changed"):
                restarted._stage_project_fonts(
                    self.root / "drift-job", provenance
                )
        finally:
            restarted.shutdown()

    def test_request_id_is_idempotent_and_payload_bound(self):
        body = {"top_text": "AI 工作流", "bottom_text": "评论区留下关键词"}
        first = self.service.submit(body, "request-1")
        second = self.service.submit(body, "request-1")
        self.assertEqual(first["job_id"], second["job_id"])
        with self.assertRaisesRegex(ValueError, "another payload"):
            self.service.submit({**body, "bottom_text": "私信领取资料"}, "request-1")

    def test_concurrent_request_id_creates_one_job_and_one_queue_entry(self):
        body = {"top_text": "AI 工作流", "bottom_text": "评论区留下关键词"}
        barrier = threading.Barrier(3)
        results = []
        errors = []

        def submit():
            barrier.wait()
            try:
                results.append(self.service.submit(body, "concurrent-request"))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=submit) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=3)
        self.assertFalse(errors)
        self.assertEqual(2, len(results))
        self.assertEqual(1, len({item["job_id"] for item in results}))
        self.assertEqual(1, self.service.jobs.qsize())

    def test_admission_caps_waiting_but_restart_recovers_running_plus_full_queue(self):
        payload = self.service.validate_payload({
            "top_text": "AI 工作流", "bottom_text": "评论区留下关键词",
            "bgm": False,
        })
        for index in range(20):
            self.service.store.create(f"waiting-{index}", payload)
        with self.assertRaises(matrix.QueueCapacityError):
            self.service.store.create("waiting-overflow", payload)

        with self.service.store.connect() as db:
            db.execute(
                """INSERT INTO jobs(
                    id,request_id,status,payload,result,error,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?)""",
                ("f" * 32, "former-running", "running",
                 json.dumps(payload, ensure_ascii=False), None, None, 1, 1),
            )
        recovered = matrix.MatrixTemplateService(
            data_root=self.service.data_root,
            skill_root=self.skill,
            library_url="http://127.0.0.1:8111",
            library_token="library-token",
            start_worker=False,
        )
        try:
            self.assertEqual(21, recovered.jobs.qsize())
            self.assertEqual(21, len(recovered.store.pending_ids()))
        finally:
            recovered.shutdown()

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
        job, _ = self.service.store.create(
            "execute-1", payload, freeze_payload=self.service._freeze_font_provenance
        )
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
        self.assertEqual("native-bold", project["layout"]["template_id"])
        self.assertEqual(project["font_selection"]["top_font"], project["layout"]["top_font"])
        self.assertEqual(project["font_selection"]["bottom_font"], project["layout"]["bottom_font"])
        self.assertEqual(project["font_selection"], result["font_selection"])
        self.assertEqual(project["scenes"][0]["top_text"], result["display_top_text"])
        self.assertEqual("AI 工作流\n评论区留下关键词", project["source_text"])
        frozen = json.loads(self.service.store.get(job["job_id"])["payload"])["_font_provenance"]
        self.assertEqual(frozen["fonts"], result["font_files"])
        self.assertEqual(
            frozen["private_bundle_sha256"], result["private_font_bundle_sha256"]
        )
        self.assertFalse(project["voice"]["enabled"])
        self.assertEqual(2, len(project["scenes"][0]["media"]))
        self.assertEqual("huangque-internal-api", project["material_library"]["index_source"])
        self.assertEqual("/v1/files/%s.mp4" % job["job_id"], result["file_url"])
        self.assertEqual(["v1", "i1", "m1"], [item["record_id"] for item in result["material_manifest"]])
        self.assertTrue((self.service.data_root / job["job_id"] / "output/published.mp4").is_file())
        self.assertFalse((self.service.data_root / job["job_id"] / "output/final.mp4").exists())
        self.service.store.update(job["job_id"], "completed", result=result)
        self.assertEqual(1, self.service.cleanup_once(
            now=matrix._now() + self.service.retention_seconds + 1
        ))
        persisted = self.service.store.public(self.service.store.get(job["job_id"]))["result"]
        self.assertEqual(result["font_files"], persisted["font_files"])
        self.assertEqual(
            result["private_font_bundle_sha256"], persisted["private_font_bundle_sha256"]
        )

    def test_probe_failure_removes_unpublished_output(self):
        payload = self.service.validate_payload({
            "top_text": "AI 工作流", "bottom_text": "评论区留下关键词",
            "bgm": False,
        })
        job, _ = self.service.store.create(
            "probe-failure", payload, freeze_payload=self.service._freeze_font_provenance
        )
        root = self.service.data_root / job["job_id"]

        def render(_project_path):
            output = root / "output/final.mp4"
            output.parent.mkdir(parents=True)
            output.write_bytes(b"invalid")

        with mock.patch.object(self.service, "_select_materials", return_value=[]), \
             mock.patch.object(self.service, "_render", side_effect=render), \
             mock.patch.object(self.service, "_probe", side_effect=matrix.MatrixTemplateError("bad probe")):
            with self.assertRaisesRegex(matrix.MatrixTemplateError, "bad probe"):
                self.service._execute(job["job_id"])
        self.assertFalse((root / "output/final.mp4").exists())
        self.assertFalse((root / "output/published.mp4").exists())

    def test_completed_persistence_failure_removes_published_output(self):
        payload = self.service.validate_payload({
            "top_text": "AI 工作流", "bottom_text": "评论区留下关键词",
        })
        job, _ = self.service.store.create("persist-failure", payload)
        output = self.service.data_root / job["job_id"] / "output/published.mp4"

        def execute(_job_id):
            output.parent.mkdir(parents=True)
            output.write_bytes(b"published")
            return {"file_url": f"/v1/files/{job['job_id']}.mp4"}

        original_update = self.service.store.update

        def update(job_id, status, **kwargs):
            if status == "completed":
                raise OSError("database write failed")
            return original_update(job_id, status, **kwargs)

        with mock.patch.object(self.service, "_execute", side_effect=execute), \
             mock.patch.object(self.service.store, "update", side_effect=update):
            self.assertTrue(self.service._run_job(job["job_id"]))
        self.assertEqual("failed", self.service.store.get(job["job_id"])["status"])
        self.assertFalse(output.exists())

    def test_running_write_failure_requeues_once_without_duplicate_execution(self):
        payload = self.service.validate_payload({
            "top_text": "AI 工作流", "bottom_text": "评论区留下关键词",
        })
        job, _ = self.service.store.create("running-write-retry", payload)
        original_update = self.service.store.update
        running_calls = 0
        execute_calls = 0
        duplicate_enqueue_results = []

        def update(job_id, status, **kwargs):
            nonlocal running_calls
            if status == "running":
                running_calls += 1
                if running_calls <= matrix.STATUS_WRITE_ATTEMPTS:
                    raise OSError("database temporarily unavailable")
            return original_update(job_id, status, **kwargs)

        def execute(_job_id):
            nonlocal execute_calls
            execute_calls += 1
            duplicate_enqueue_results.append(self.service._enqueue(job["job_id"]))
            return {"file_url": f"/v1/files/{job['job_id']}.mp4"}

        self.service._enqueue(job["job_id"])
        with mock.patch.object(self.service.store, "update", side_effect=update), \
             mock.patch.object(self.service, "_execute", side_effect=execute), \
             mock.patch.object(matrix, "STATUS_WRITE_RETRY_SECONDS", 0), \
             mock.patch.object(matrix, "JOB_REQUEUE_SECONDS", 0):
            self.service.worker = threading.Thread(target=self.service._worker, daemon=True)
            self.service.worker.start()
            deadline = time.time() + 2
            while time.time() < deadline:
                if self.service.store.get(job["job_id"])["status"] == "completed":
                    break
                time.sleep(0.01)
            self.assertEqual("completed", self.service.store.get(job["job_id"])["status"])
            self.assertTrue(self.service.worker.is_alive())
            self.assertEqual(1, execute_calls)
            self.assertEqual([False], duplicate_enqueue_results)
            self.assertEqual(matrix.STATUS_WRITE_ATTEMPTS + 1, running_calls)
        self.service.shutdown()

    def test_failed_write_failure_requeues_and_keeps_worker_alive(self):
        payload = self.service.validate_payload({
            "top_text": "AI 工作流", "bottom_text": "评论区留下关键词",
        })
        job, _ = self.service.store.create("failed-write-retry", payload)
        original_update = self.service.store.update
        failed_calls = 0
        execute_calls = 0
        active = 0
        max_active = 0

        def update(job_id, status, **kwargs):
            nonlocal failed_calls
            if status == "failed":
                failed_calls += 1
                if failed_calls <= matrix.STATUS_WRITE_ATTEMPTS:
                    raise OSError("database temporarily unavailable")
            return original_update(job_id, status, **kwargs)

        def execute(_job_id):
            nonlocal execute_calls, active, max_active
            execute_calls += 1
            active += 1
            max_active = max(max_active, active)
            active -= 1
            raise matrix.MatrixTemplateError("render failed")

        self.service._enqueue(job["job_id"])
        with mock.patch.object(self.service.store, "update", side_effect=update), \
             mock.patch.object(self.service, "_execute", side_effect=execute), \
             mock.patch.object(matrix, "STATUS_WRITE_RETRY_SECONDS", 0), \
             mock.patch.object(matrix, "JOB_REQUEUE_SECONDS", 0):
            self.service.worker = threading.Thread(target=self.service._worker, daemon=True)
            self.service.worker.start()
            deadline = time.time() + 2
            while time.time() < deadline:
                if self.service.store.get(job["job_id"])["status"] == "failed":
                    break
                time.sleep(0.01)
            self.assertEqual("failed", self.service.store.get(job["job_id"])["status"])
            self.assertTrue(self.service.worker.is_alive())
            self.assertEqual(2, execute_calls)
            self.assertEqual(1, max_active)
        self.service.shutdown()

    def test_health_returns_503_when_an_expected_worker_is_dead(self):
        self.service.workers_expected = True
        self.service.worker = threading.Thread(target=lambda: None)
        self.service.worker.start()
        self.service.worker.join(timeout=1)
        cleanup_stop = threading.Event()
        self.service.cleanup_worker = threading.Thread(target=cleanup_stop.wait, daemon=True)
        self.service.cleanup_worker.start()
        server = matrix.build_server("127.0.0.1", 0, self.service, "api-token")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with self.assertRaises(urllib.error.HTTPError) as unavailable:
                urllib.request.urlopen(
                    "http://127.0.0.1:%d/health" % server.server_port, timeout=3
                )
            self.assertEqual(503, unavailable.exception.code)
            body = json.loads(unavailable.exception.read())
            self.assertFalse(body["ok"])
            self.assertFalse(body["worker_alive"])
            self.assertTrue(body["cleanup_worker_alive"])
            worker_stop = threading.Event()
            self.service.worker = threading.Thread(target=worker_stop.wait, daemon=True)
            self.service.worker.start()
            cleanup_stop.set()
            self.service.cleanup_worker.join(timeout=1)
            with self.assertRaises(urllib.error.HTTPError) as cleanup_unavailable:
                urllib.request.urlopen(
                    "http://127.0.0.1:%d/health" % server.server_port, timeout=3
                )
            cleanup_body = json.loads(cleanup_unavailable.exception.read())
            self.assertFalse(cleanup_body["ok"])
            self.assertTrue(cleanup_body["worker_alive"])
            self.assertFalse(cleanup_body["cleanup_worker_alive"])
            worker_stop.set()
        finally:
            cleanup_stop.set()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            self.service.shutdown()

    def test_persistent_status_failure_keeps_job_and_marks_readiness_degraded(self):
        payload = self.service.validate_payload({
            "top_text": "AI 工作流", "bottom_text": "评论区留下关键词",
        })
        job, _ = self.service.store.create("persistent-status-failure", payload)
        self.service._enqueue(job["job_id"])
        self.service.workers_expected = True
        cleanup_stop = threading.Event()
        self.service.cleanup_worker = threading.Thread(target=cleanup_stop.wait, daemon=True)
        self.service.cleanup_worker.start()
        with mock.patch.object(
            self.service.store, "update", side_effect=OSError("database unavailable")
        ), mock.patch.object(matrix, "STATUS_WRITE_RETRY_SECONDS", 0), \
             mock.patch.object(matrix, "JOB_REQUEUE_SECONDS", 1):
            self.service.worker = threading.Thread(target=self.service._worker, daemon=True)
            self.service.worker.start()
            deadline = time.time() + 2
            while time.time() < deadline and not self.service.worker_degraded.is_set():
                time.sleep(0.01)
            health = self.service.health()
            self.assertFalse(health["ok"])
            self.assertTrue(health["worker_alive"])
            self.assertTrue(health["worker_degraded"])
            self.assertEqual("pending", self.service.store.get(job["job_id"])["status"])
            self.assertIn(job["job_id"], self.service.store.pending_ids())
            cleanup_stop.set()
            self.service.shutdown()

    def test_file_delivery_requires_completed_bound_result_and_marks_delivery(self):
        body = {"top_text": "AI 工作流", "bottom_text": "评论区留下关键词"}
        job = self.service.submit(body, "file-contract")
        root = self.service.data_root / job["job_id"]
        output = root / "output/published.mp4"
        output.parent.mkdir(parents=True)
        output.write_bytes(b"published-video")

        for status, result in (
            ("pending", None),
            ("running", None),
            ("failed", None),
            ("completed", {"file_url": "/v1/files/wrong.mp4"}),
        ):
            self.service.store.update(job["job_id"], status, result=result, error="failed" if status == "failed" else None)
            with self.assertRaises(FileNotFoundError):
                with self.service.open_completed_file(job["job_id"]):
                    pass

        result = {"file_url": f"/v1/files/{job['job_id']}.mp4"}
        self.service.store.update(job["job_id"], "completed", result=result)
        with self.service.open_completed_file(job["job_id"]) as handle:
            self.assertEqual(b"published-video", handle.read())
        row = self.service.store.get(job["job_id"])
        self.assertIsNotNone(row["delivered_at"])

    def test_cleanup_skips_active_and_removes_expired_terminal_jobs(self):
        payload = self.service.validate_payload({
            "top_text": "AI 工作流", "bottom_text": "评论区留下关键词",
        })
        completed, _ = self.service.store.create("cleanup-completed", payload)
        failed, _ = self.service.store.create("cleanup-failed", payload)
        active, _ = self.service.store.create("cleanup-active", payload)
        for job in (completed, failed, active):
            (self.service.data_root / job["job_id"] / "output").mkdir(parents=True)
        completed_output = self.service.data_root / completed["job_id"] / "output/published.mp4"
        active_output = self.service.data_root / active["job_id"] / "output/published.mp4"
        completed_output.write_bytes(b"completed")
        active_output.write_bytes(b"active")
        self.service.store.update(completed["job_id"], "completed", result={
            "file_url": f"/v1/files/{completed['job_id']}.mp4",
        })
        self.service.store.update(failed["job_id"], "failed", error="failed")
        self.service.store.update(active["job_id"], "completed", result={
            "file_url": f"/v1/files/{active['job_id']}.mp4",
        })
        with self.service.store.connect() as db:
            db.execute(
                "UPDATE jobs SET updated_at=1 WHERE id IN (?,?)",
                (completed["job_id"], failed["job_id"]),
            )

        with self.service.open_completed_file(active["job_id"]):
            with self.service.store.connect() as db:
                db.execute("UPDATE jobs SET updated_at=1 WHERE id=?", (active["job_id"],))
            self.assertEqual(2, self.service.cleanup_once(now=matrix.DEFAULT_RETENTION_SECONDS + 2))
            self.assertTrue((self.service.data_root / active["job_id"]).exists())

        self.assertEqual(1, self.service.cleanup_once(now=matrix.DEFAULT_RETENTION_SECONDS + 2))
        for job in (completed, failed, active):
            self.assertFalse((self.service.data_root / job["job_id"]).exists())
            self.assertIsNotNone(self.service.store.get(job["job_id"])["cleaned_at"])

    def test_disk_high_water_rejects_new_job_but_allows_idempotent_replay(self):
        body = {"top_text": "AI 工作流", "bottom_text": "评论区留下关键词"}
        first = self.service.submit(body, "disk-replay")
        full = SimpleNamespace(total=100, used=96, free=4)
        with mock.patch.object(matrix.shutil, "disk_usage", return_value=full):
            replay = self.service.submit(body, "disk-replay")
            self.assertEqual(first["job_id"], replay["job_id"])
            with self.assertRaises(matrix.DiskCapacityError):
                self.service.submit(body, "disk-new")

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
                catalog = json.load(response)
                self.assertEqual(13, len(catalog["templates"]))
                self.assertEqual("", catalog["default_font"])
                self.assertEqual(5, len(catalog["fonts"]))
            with request(
                "/v1/preflight", "POST",
                {"top_text": "中" * 60, "bottom_text": "A" * 7 + "，。！？"},
                "api-token",
            ) as response:
                preflight = json.load(response)
            self.assertEqual((14.9, 3), (
                preflight["duration"], preflight["required_visuals"]))
            self.assertEqual([], self.service.store.pending_ids())
            self.assertEqual(0, self.service.jobs.qsize())
            with self.assertRaises(urllib.error.HTTPError) as too_long:
                request(
                    "/v1/preflight", "POST",
                    {"top_text": "中" * 60, "bottom_text": "A" * 8},
                    "api-token",
                )
            self.assertEqual(400, too_long.exception.code)
            with request(
                "/v1/jobs", "POST",
                {"top_text": "AI 工作流", "bottom_text": "评论区留下关键词"},
                "api-token", "http-request-1",
            ) as response:
                job = json.load(response)
            self.assertEqual("pending", job["status"])
            with request("/v1/jobs/" + job["job_id"], token="api-token") as response:
                self.assertEqual(job["job_id"], json.load(response)["job_id"])
            output = self.service.data_root / job["job_id"] / "output/published.mp4"
            output.parent.mkdir(parents=True)
            output.write_bytes(b"published-video")
            with self.assertRaises(urllib.error.HTTPError) as pending_file:
                request(f"/v1/files/{job['job_id']}.mp4", token="api-token")
            self.assertEqual(404, pending_file.exception.code)
            self.service.store.update(job["job_id"], "failed", error="probe failed")
            with self.assertRaises(urllib.error.HTTPError) as failed_file:
                request(f"/v1/files/{job['job_id']}.mp4", token="api-token")
            self.assertEqual(404, failed_file.exception.code)
            self.service.store.update(job["job_id"], "completed", result={
                "file_url": f"/v1/files/{job['job_id']}.mp4",
            })
            with request(f"/v1/files/{job['job_id']}.mp4", token="api-token") as response:
                self.assertEqual(b"published-video", response.read())
            deadline = time.time() + 1
            while time.time() < deadline and self.service.store.get(job["job_id"])["delivered_at"] is None:
                time.sleep(0.01)
            self.assertIsNotNone(self.service.store.get(job["job_id"])["delivered_at"])
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
