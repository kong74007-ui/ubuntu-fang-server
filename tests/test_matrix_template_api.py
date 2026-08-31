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
            "id": template_id,
            "name": f"模板 {index}", "description": "测试模板",
            "tags": ["测试"], "layout": {}, "render": {},
        } for index, template_id in enumerate(("full-overlay-bold", "poster-split"))]
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
        self.assertEqual(2, len(self.service.catalog))
        payload = self.service.validate_payload({
            "top_text": "AI 工作流",
            "bottom_text": "评论区留下关键词",
            "template_id": "full-overlay-bold",
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
            {"top_text": "有效标题", "bottom_text": "有效行动", "batch_id": "bad", "batch_index": 1, "batch_size": 5},
        ):
            with self.assertRaises(ValueError):
                self.service.validate_payload(invalid)

    def test_catalog_rejects_missing_private_domain_layout(self):
        catalog_path = self.skill / "assets/templates/catalog.json"
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        catalog["templates"][-1]["id"] = "replacement-template"
        catalog_path.write_text(
            json.dumps(catalog, ensure_ascii=False), encoding="utf-8"
        )
        with self.assertRaisesRegex(
            matrix.MatrixTemplateError,
            "required private-domain templates are missing",
        ):
            self.service._load_catalog()

    def test_private_domain_layouts_preserve_auto_and_explicit_font_contracts(self):
        for index, template_id in enumerate(("full-overlay-bold", "poster-split"), 1):
            with self.subTest(template_id=template_id, mode="automatic"):
                automatic = self.service.validate_payload({
                    "top_text": "私域布局自动字体",
                    "bottom_text": "评论区获取活动资料",
                    "template_id": template_id,
                    "bgm": False,
                })
                frozen = self.service._freeze_font_provenance(
                    format(index, "032x"), automatic,
                )["_font_provenance"]
                self.assertNotEqual("user-selected", frozen["selection"]["variant"])
                self.assertTrue({
                    frozen["selection"]["top_font"],
                    frozen["selection"]["bottom_font"],
                }.issubset(matrix.BASE_FONT_FAMILIES))

            with self.subTest(template_id=template_id, mode="explicit"):
                explicit = self.service.validate_payload({
                    "top_text": "私域布局显式字体",
                    "bottom_text": "评论区获取活动资料",
                    "template_id": template_id,
                    "font_family": "Noto Sans SC",
                    "bgm": False,
                })
                frozen = self.service._freeze_font_provenance(
                    format(index + 10, "032x"), explicit,
                )["_font_provenance"]
                self.assertEqual({
                    "variant": "user-selected",
                    "top_font": "Noto Sans SC",
                    "bottom_font": "Noto Sans SC",
                }, frozen["selection"])
                self.assertEqual(["bundled"], [
                    item["source"] for item in frozen["fonts"]
                ])

    def test_duration_boundary_counts_visible_chinese_and_english_only(self):
        accepted = self.service.validate_payload({
            "top_text": "中" * 60,
            "bottom_text": "A" * 7 + "，。！？",
            "template_id": "full-overlay-bold",
        })
        self.assertEqual(14.9, accepted["duration"])
        with self.assertRaisesRegex(ValueError, "文案过长"):
            self.service.validate_payload({
                "top_text": "中" * 60,
                "bottom_text": "A" * 8,
                "template_id": "full-overlay-bold",
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
            "template_id": "full-overlay-bold",
            "bgm": False,
        }, "balanced-title")
        payload = json.loads(self.service.store.get(job["job_id"])["payload"])
        self.assertEqual(title, payload["top_text"])
        self.assertEqual(expected, payload["_display_top_text"])
        replay = self.service.submit({
            "top_text": title,
            "bottom_text": "轻团队也能稳定运营",
            "template_id": "full-overlay-bold",
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
        self.assertEqual(2, len(matrix.FONT_VARIANTS))
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
        self.assertEqual(
            {"Noto Sans SC", "ZCOOL XiaoWei", "ZCOOL KuaiLe"}, represented
        )
        fallback = matrix._font_selection("future-template", "f" * 32)
        self.assertIn(fallback["top_font"], allowed)
        self.assertIn(fallback["bottom_font"], allowed)
        private_represented = {
            font for options in matrix.PRIVATE_FONT_VARIANTS.values()
            for _, top_font, bottom_font in options
            for font in (top_font, bottom_font)
            if font in matrix.PRIVATE_FONT_FAMILIES
        }
        self.assertEqual(
            {"AaHouDiHei", "Kingnam Bobo", "zihunbiantaoti"},
            private_represented,
        )
        selections = [
            matrix._font_selection("full-overlay-bold", format(index, "032x"), {"AaHouDiHei"})
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
            "template_id": "full-overlay-bold", "bgm": False,
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
                if matrix._font_selection("full-overlay-bold", format(index, "032x"), {"AaHouDiHei"})["top_font"] == "AaHouDiHei"
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

    def test_idempotent_private_font_replay_survives_bundle_removal(self):
        private_root = self.root / "idempotent-private-fonts"
        private_root.mkdir()
        private_file = private_root / "AaHouDiHei.ttf"
        private_file.write_bytes(b"private-font")
        (private_root / "sources.json").write_text(json.dumps({
            "schema_version": 1,
            "fonts": [{
                "family": "AaHouDiHei", "file": private_file.name,
                "sha256": hashlib.sha256(private_file.read_bytes()).hexdigest(),
                "authorized": True,
            }],
        }), encoding="utf-8")
        data_root = self.root / "idempotent-data"
        initial = matrix.MatrixTemplateService(
            data_root=data_root, skill_root=self.skill,
            library_url="http://127.0.0.1:8111", library_token="library-token",
            private_font_root=private_root, start_worker=False,
        )
        body = {
            "top_text": "指定字体标题", "bottom_text": "指定字体行动文案",
            "template_id": "full-overlay-bold", "font_family": "AaHouDiHei",
            "bgm": False,
        }
        try:
            accepted = initial.submit(body, "accepted-private-font")
            initial.store.update(accepted["job_id"], "completed", result={
                "font_selection": {"top_font": "AaHouDiHei"},
            })
            initial.store.mark_cleaned(accepted["job_id"])
        finally:
            initial.shutdown()
        (private_root / "sources.json").unlink()
        restarted = matrix.MatrixTemplateService(
            data_root=data_root, skill_root=self.skill,
            library_url="http://127.0.0.1:8111", library_token="library-token",
            private_font_root=private_root, start_worker=False,
        )
        try:
            replay = restarted.submit(body, "accepted-private-font")
            self.assertEqual(accepted["job_id"], replay["job_id"])
            self.assertEqual("completed", replay["status"])
            self.assertIn("cleaned_at", replay)
            with self.assertRaisesRegex(ValueError, "another payload"):
                restarted.submit(
                    dict(body, font_family="Noto Sans SC"),
                    "accepted-private-font",
                )
            with self.assertRaisesRegex(ValueError, "当前可用字体"):
                restarted.submit(body, "removed-private-font-new-key")
        finally:
            restarted.shutdown()

    def test_request_id_is_idempotent_and_payload_bound(self):
        body = {"top_text": "AI 工作流", "bottom_text": "评论区留下关键词"}
        first = self.service.submit(body, "request-1")
        second = self.service.submit(body, "request-1")
        self.assertEqual(first["job_id"], second["job_id"])
        with self.assertRaisesRegex(ValueError, "another payload"):
            self.service.submit({**body, "bottom_text": "私信领取资料"}, "request-1")

    def test_retired_template_replays_existing_request_but_rejects_new_request(self):
        body = {"top_text": "历史标题", "bottom_text": "历史行动文案", "bgm": False}
        stored_payload = {
            **body,
            "template_id": "native-bold",
            "duration": matrix._duration(body["top_text"], body["bottom_text"], None),
        }
        existing, _created = self.service.store.create(
            "retired-template-replay", stored_payload
        )

        replay = self.service.submit(body, "retired-template-replay")
        explicit_replay = self.service.submit(
            {**body, "template_id": "native-bold"}, "retired-template-replay"
        )
        self.assertEqual(existing["job_id"], replay["job_id"])
        self.assertEqual(existing["job_id"], explicit_replay["job_id"])
        with self.assertRaisesRegex(ValueError, "请选择有效模板"):
            self.service.submit(
                {**body, "template_id": "native-bold"}, "retired-template-new"
            )
        with self.assertRaisesRegex(ValueError, "another payload"):
            self.service.submit(
                {**body, "bottom_text": "不同内容"}, "retired-template-replay"
            )

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
        with mock.patch.object(
            self.service, "_freeze_font_provenance",
            wraps=self.service._freeze_font_provenance,
        ) as freeze:
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join(timeout=3)
            self.assertEqual(1, freeze.call_count)
        self.assertFalse(errors)
        self.assertEqual(2, len(results))
        self.assertEqual(1, len({item["job_id"] for item in results}))
        self.assertEqual(1, self.service.jobs.qsize())

    def test_five_workers_execute_five_jobs_concurrently(self):
        service = matrix.MatrixTemplateService(
            data_root=self.root / "concurrency-five-data",
            skill_root=self.skill,
            library_url="http://127.0.0.1:8111",
            library_token="library-token",
            concurrency=5,
            start_worker=True,
            cleanup_interval_seconds=3600,
        )
        active = 0
        peak = 0
        lock = threading.Lock()
        all_started = threading.Event()
        release = threading.Event()

        def execute(job_id):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                if active == 5:
                    all_started.set()
            release.wait(3)
            with lock:
                active -= 1
            return {"file_url": f"/v1/files/{job_id}.mp4"}

        try:
            with mock.patch.object(service, "_execute", side_effect=execute):
                jobs = [service.submit({
                    "top_text": f"并发标题{index}",
                    "bottom_text": "并发行动文案",
                    "bgm": False,
                }, f"concurrency-five-{index}") for index in range(5)]
                self.assertTrue(all_started.wait(3))
                self.assertEqual(5, peak)
                health = service.health()
                self.assertEqual(5, health["worker_count"])
                self.assertEqual(5, health["concurrency"])
                release.set()
                deadline = time.time() + 3
                while time.time() < deadline:
                    if all(service.store.get(job["job_id"])["status"] == "completed" for job in jobs):
                        break
                    time.sleep(0.01)
                self.assertTrue(all(
                    service.store.get(job["job_id"])["status"] == "completed"
                    for job in jobs
                ))
        finally:
            release.set()
            service.shutdown()

    def test_successful_worker_cannot_clear_another_jobs_degraded_state(self):
        service = matrix.MatrixTemplateService(
            data_root=self.root / "degraded-two-worker-data",
            skill_root=self.skill,
            library_url="http://127.0.0.1:8111",
            library_token="library-token",
            concurrency=2,
            start_worker=False,
        )
        first = service.submit({
            "top_text": "失败任务标题", "bottom_text": "失败任务行动文案",
            "bgm": False,
        }, "degraded-first")
        second = service.submit({
            "top_text": "成功任务标题", "bottom_text": "成功任务行动文案",
            "bgm": False,
        }, "degraded-second")
        first_calls = 0
        second_done = threading.Event()

        def run_job(job_id):
            nonlocal first_calls
            if job_id == first["job_id"]:
                first_calls += 1
                return first_calls > 1
            deadline = time.time() + 2
            while time.time() < deadline and not service.worker_degraded.is_set():
                time.sleep(0.005)
            second_done.set()
            return True

        service.workers_expected = True
        service.workers = [
            threading.Thread(target=service._worker, daemon=True)
            for _ in range(2)
        ]
        service.worker = service.workers[0]
        try:
            with mock.patch.object(service, "_run_job", side_effect=run_job), \
                 mock.patch.object(matrix, "JOB_REQUEUE_SECONDS", 0.2):
                for worker in service.workers:
                    worker.start()
                self.assertTrue(second_done.wait(2))
                self.assertTrue(service.health()["worker_degraded"])
                self.assertEqual(1, service.health()["degraded_jobs"])
                deadline = time.time() + 3
                while time.time() < deadline and service.health()["worker_degraded"]:
                    time.sleep(0.01)
                self.assertFalse(service.health()["worker_degraded"])
                self.assertEqual(0, service.health()["degraded_jobs"])
                self.assertEqual(2, first_calls)
        finally:
            service.shutdown()

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
        self.assertEqual([], captured["used_sha256"])
        self.assertEqual(3, len(set(item["sha256"] for item in materials)))

    def test_concurrent_batch_jobs_reserve_distinct_visual_materials(self):
        batch_id = "b" * 32
        requests = []
        request_lock = threading.Lock()
        visual_pool = [format(index, "064x") for index in range(1, 21)]
        bgm_sha = "f" * 64

        def select(_method, _path, body):
            with request_lock:
                requests.append(dict(body))
            used = set(body.get("used_sha256") or [])
            available = [value for value in visual_pool if value not in used]
            return {"materials": [
                {"scene_id": "media_01", "sha256": available[0], "media_type": "video", "record_id": "v-" + available[0][:4]},
                {"scene_id": "media_02", "sha256": available[1], "media_type": "image", "record_id": "i-" + available[1][:4]},
                {"scene_id": "bgm", "sha256": bgm_sha, "media_type": "bgm", "record_id": "bgm-1"},
            ]}

        results = {}
        errors = []
        barrier = threading.Barrier(6)

        def run(index):
            payload = self.service.validate_payload({
                "top_text": "批量素材标题", "bottom_text": "批量素材行动文案",
                "batch_id": batch_id, "batch_index": index, "batch_size": 5,
            })
            job_id = format(index, "032x")
            barrier.wait()
            try:
                results[index] = self.service._select_materials(payload, job_id)
            except Exception as exc:
                errors.append(exc)

        with mock.patch.object(self.service, "_library_request", side_effect=select):
            threads = [threading.Thread(target=run, args=(index,)) for index in range(1, 6)]
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join(timeout=3)
            self.assertFalse(errors)
            self.assertEqual(5, len(results))
            self.assertEqual(10, len({
                item["sha256"] for materials in results.values()
                for item in materials if item["media_type"] in {"image", "video"}
            }))
            self.assertEqual([0, 2, 4, 6, 8], sorted(
                len(item["used_sha256"]) for item in requests
            ))
            before = len(requests)
            frozen = self.service._select_materials(
                self.service.validate_payload({
                    "top_text": "批量素材标题", "bottom_text": "批量素材行动文案",
                    "batch_id": batch_id, "batch_index": 1, "batch_size": 5,
                }), format(1, "032x")
            )
            self.assertEqual(results[1], frozen)
            self.assertEqual(before, len(requests))
            with self.assertRaisesRegex(
                matrix.MatrixTemplateError, "同批次视觉素材重复"
            ) as duplicate:
                self.service.store.reserve_batch_materials(
                    batch_id, "f" * 32, results[1]
                )
            self.assertNotIn("UNIQUE", str(duplicate.exception))
        restarted = matrix.MatrixTemplateService(
            data_root=self.service.data_root,
            skill_root=self.skill,
            library_url="http://127.0.0.1:8111",
            library_token="library-token",
            start_worker=False,
        )
        try:
            with mock.patch.object(
                restarted, "_library_request",
                side_effect=AssertionError("frozen batch selection must survive restart"),
            ):
                restored = restarted._select_materials(
                    restarted.validate_payload({
                        "top_text": "批量素材标题", "bottom_text": "批量素材行动文案",
                        "batch_id": batch_id, "batch_index": 1, "batch_size": 5,
                    }), format(1, "032x")
                )
            self.assertEqual(results[1], restored)
        finally:
            restarted.shutdown()

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
        self.assertEqual("full-overlay-bold", project["layout"]["template_id"])
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
                health = json.load(response)
                self.assertEqual(2, health["templates"])
                self.assertEqual(5, health["max_batch_size"])
                self.assertEqual(
                    {"2": 0, "3": 0}, health["reference_top_layer_counts"]
                )
                self.assertEqual([], health["reference_fixed_private_fonts"])
                self.assertEqual({
                    "ffmpeg": 1,
                    "hyperframes": 2,
                }, health["engine_concurrency"])
            with self.assertRaises(urllib.error.HTTPError) as denied:
                request("/v1/templates")
            self.assertEqual(401, denied.exception.code)
            with request("/v1/templates", token="api-token") as response:
                catalog = json.load(response)
                self.assertEqual(
                    {"full-overlay-bold", "poster-split"},
                    {item["id"] for item in catalog["templates"]},
                )
                self.assertEqual("", catalog["default_font"])
                self.assertEqual(5, len(catalog["fonts"]))
                self.assertEqual(5, catalog["max_batch_size"])
                self.assertEqual(2, catalog["hyperframes_concurrency"])
                self.assertEqual({
                    "ffmpeg": 1,
                    "hyperframes": 2,
                }, catalog["engine_concurrency"])
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


class HyperFramesReferenceTemplateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.skill = self.root / "skill"
        self.reference_skill = self.root / "reference-skill"
        self._write_skill_fixture(self.skill, reference=False)
        self._write_skill_fixture(self.reference_skill, reference=True)
        self.private_font_root = self.root / "private-fonts"
        self.private_font_root.mkdir()
        private_font = self.private_font_root / "SmileySans-Oblique.ttf"
        private_font.write_bytes(b"smiley-sans-private-fixture")
        (self.private_font_root / "sources.json").write_text(
            json.dumps({
                "schema_version": 1,
                "fonts": [{
                    "family": "Smiley Sans Oblique",
                    "file": private_font.name,
                    "sha256": hashlib.sha256(private_font.read_bytes()).hexdigest(),
                    "authorized": True,
                }],
            }),
            encoding="utf-8",
        )
        self.cli = self.root / "hyperframes"
        self.cli.write_bytes(b"cli")
        self.gsap = self.root / "gsap.min.js"
        self.gsap.write_text("window.gsap={};", encoding="utf-8")
        self.browser = self.root / "chrome"
        self.browser.write_bytes(b"browser")
        version = SimpleNamespace(returncode=0, stdout="0.8.16\n", stderr="")
        with mock.patch.object(matrix.subprocess, "run", return_value=version):
            self.service = matrix.MatrixTemplateService(
                data_root=self.root / "data",
                skill_root=self.skill,
                private_font_root=self.private_font_root,
                reference_skill_root=self.reference_skill,
                hyperframes_cli=self.cli,
                hyperframes_gsap=self.gsap,
                hyperframes_browser=self.browser,
                library_url="http://127.0.0.1:8111",
                library_token="library-token",
                start_worker=False,
            )

    def tearDown(self):
        self.service.shutdown()
        self.temp.cleanup()

    @staticmethod
    def _write_skill_fixture(root: Path, *, reference: bool) -> None:
        template_root = root / "assets/templates"
        font_root = root / "assets/fonts"
        scripts = root / "scripts"
        template_root.mkdir(parents=True)
        font_root.mkdir(parents=True)
        scripts.mkdir()
        family_files = {
            "Noto Sans SC": "NotoSansSC-Variable.ttf",
            "Ma Shan Zheng": "MaShanZheng-Regular.ttf",
            "ZCOOL KuaiLe": "ZCOOLKuaiLe-Regular.ttf",
            "ZCOOL XiaoWei": "ZCOOLXiaoWei-Regular.ttf",
        }
        bundled = []
        for family, filename in family_files.items():
            path = font_root / filename
            path.write_bytes(family.encode("utf-8"))
            bundled.append({
                "family": family,
                "file": filename,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            })
        (font_root / "sources.json").write_text(
            json.dumps({"fonts": bundled}), encoding="utf-8"
        )
        if not reference:
            templates = [{
                "id": template_id,
                "name": template_id,
                "description": "fixture",
                "tags": [],
                "layout": {},
                "render": {},
            } for template_id in ("full-overlay-bold", "poster-split")]
            (template_root / "catalog.json").write_text(
                json.dumps({"version": 1, "templates": templates}), encoding="utf-8"
            )
            (scripts / "render_video.py").write_text("# fixture\n", encoding="utf-8")
            return

        pack = template_root / matrix.REFERENCE_PACK_ID
        (pack / "assets/bgm").mkdir(parents=True)
        (pack / "assets/bgm/silence.m4a").write_bytes(b"silence")
        top3_variants = {1, 4, 5, 6, 7, 8, 10, 11, 12, 16, 17}
        styles = []
        for index in range(1, 18):
            variant = f"v{index:02d}"
            if variant == matrix.REFERENCE_FEATURED_VARIANT:
                styles.extend((
                    '.v05 .top1 { font: 900 102px/1.02 "NotoSC"; color: #f4f7f2; -webkit-text-stroke: 12px #203449; text-shadow: 8px 10px 0 #07111e; }',
                    '.v05 .top2 { font: 900 104px/1.01 "NotoSC"; color: #f4f7f2; -webkit-text-stroke: 13px #203449; text-shadow: 9px 11px 0 #07111e; }',
                    '.v05 .top3 { font: 900 68px/1.04 "NotoSC"; color: #fff8d9; -webkit-text-stroke: 9px #26394a; text-shadow: 7px 8px 0 #07111e; }',
                    '.v05 .bottom1 { font: 900 68px/1.05 "NotoSC"; color: #ffe000; -webkit-text-stroke: 9px #263e32; }',
                    '.v05 .bottom2 { font: 900 70px/1.06 "NotoSC"; background: #f4c900; color: #26362d; border-radius: 28px; }',
                ))
                continue
            styles.extend((
                f".{variant} .top1 {{ font-size: 80px; }}",
                f".{variant} .top2 {{ font-size: 60px; }}",
            ))
            if index in top3_variants:
                styles.append(f".{variant} .top3 {{ font-size: 50px; }}")
        timeline_fixture = """
<div id="root">
  <video id="videoA" class="clip media-video" data-start="0" data-duration="2.666667"></video>
  <video id="videoB" class="clip media-video" data-start="2.666667" data-duration="2.666666"></video>
  <video id="videoC" class="clip media-video" data-start="5.333333" data-duration="2.666667"></video>
  <audio id="bgm" data-start="0" data-duration="8"></audio>
  <section id="typography" class="clip text-layer" data-start="0" data-duration="8"></section>
</div>
<script>
      const duration = 8;
""" + matrix.REFERENCE_DYNAMIC_TIMING_JS + """
</script>
"""
        (pack / "index.html").write_text(
            "<html><head><style>\n" + "\n".join(styles) + "\n</style>\n"
            + matrix.REFERENCE_GSAP_CDN
            + "\n</head><body>" + timeline_fixture + "</body></html>\n",
            encoding="utf-8",
        )
        (pack / "hyperframes.json").write_text("{}\n", encoding="utf-8")
        (pack / "preview-data.js").write_text("// fixture\n", encoding="utf-8")
        templates = [{
            "id": f"ref-{index:02d}-fixture-{index:02d}",
            "variant": f"v{index:02d}",
            "name": f"参考模板 {index}",
            "description": "固定字体参考模板",
        } for index in range(1, 18)]
        (pack / "manifest.json").write_text(json.dumps({
            "version": 2,
            "pack_id": matrix.REFERENCE_PACK_ID,
            "engine": "hyperframes",
            "hyperframes_version": matrix.REFERENCE_HYPERFRAMES_VERSION,
            "resolution": "1080x1920",
            "fps": 30,
            "templates": templates,
        }), encoding="utf-8")

    def test_reference_catalog_is_19_and_ignores_font_selection(self):
        self.assertEqual(19, len(self.service.catalog))
        self.assertEqual(
            ["full-overlay-bold", "poster-split", "ref-05-fixture-05"],
            [item["id"] for item in self.service.catalog[:3]],
        )
        template_id = "ref-01-fixture-01"
        template = self.service.templates[template_id]
        self.assertEqual("hyperframes", template["engine"])
        self.assertFalse(template["font_selectable"])
        self.assertEqual("template_locked", template["font_mode"])
        self.assertEqual({"top": 3, "bottom": 2}, template["text_layers"])
        self.assertEqual(
            {"top": 2, "bottom": 2},
            self.service.templates["ref-02-fixture-02"]["text_layers"],
        )
        self.assertEqual(
            {"top2": "Smiley Sans Oblique"},
            self.service.templates["ref-02-fixture-02"]["fixed_fonts"],
        )
        self.assertEqual(
            {"top2": "Smiley Sans Oblique"},
            self.service.templates["ref-03-fixture-03"]["fixed_fonts"],
        )
        self.assertEqual(
            {}, self.service.templates["ref-04-fixture-04"]["fixed_fonts"]
        )
        self.assertEqual(
            ["Smiley Sans Oblique"],
            self.service.health()["reference_fixed_private_fonts"],
        )
        self.assertEqual(
            {
                "v01", "v04", "v05", "v06", "v07", "v08",
                "v10", "v11", "v12", "v16", "v17",
            },
            {
                item["variant"] for item in self.service.reference_templates.values()
                if item["text_layers"]["top"] == 3
            },
        )
        payload = self.service.validate_payload({
            "top_text": "AI创业活动",
            "bottom_text": "评论区回复关键词",
            "template_id": template_id,
            "font_family": "not/a/valid/font/value",
            "duration": 15,
            "bgm": False,
        })
        self.assertNotIn("font_family", payload)
        self.assertEqual(3, self.service.required_visuals(payload))
        frozen = self.service._freeze_font_provenance("1" * 32, payload)
        self.assertEqual(
            "template-locked",
            frozen["_font_provenance"]["selection"]["variant"],
        )
        self.assertEqual(4, len(frozen["_font_provenance"]["fonts"]))
        self.assertTrue(8 <= frozen["_reference_template"]["duration"] <= 15)
        self.assertEqual("", frozen["_reference_template"]["text"]["bottom1"])
        self.assertEqual(
            "评论区回复关键词",
            frozen["_reference_template"]["text"]["bottom2"],
        )
        batch = self.service.validate_payload({
            "top_text": "批量活动标题",
            "bottom_text": "评论区回复关键词",
            "template_id": template_id,
            "batch_id": "a" * 32,
            "batch_index": 3,
            "batch_size": 5,
        })
        self.assertEqual(("a" * 32, 3, 5), (
            batch["batch_id"], batch["batch_index"], batch["batch_size"],
        ))

    def test_reference_catalog_rejects_missing_required_top_style(self):
        index_path = (
            self.reference_skill / "assets/templates"
            / matrix.REFERENCE_PACK_ID / "index.html"
        )
        source = index_path.read_text(encoding="utf-8")
        self.assertIn(".v02 .top2 {", source)
        index_path.write_text(
            source.replace(".v02 .top2 {", ".v02 .missing {", 1),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            matrix.MatrixTemplateError, "top layer styles are incomplete"
        ):
            self.service._load_reference_catalog()

    def test_reference_catalog_rejects_missing_fixed_private_font(self):
        self.service.private_fonts.pop("Smiley Sans Oblique")
        with self.assertRaisesRegex(
            matrix.MatrixTemplateError, "fixed private font is unavailable"
        ):
            self.service._load_reference_catalog()

    def test_featured_template_rejects_style_or_first_frame_drift(self):
        index_path = (
            self.reference_skill / "assets/templates"
            / matrix.REFERENCE_PACK_ID / "index.html"
        )
        source = index_path.read_text(encoding="utf-8")
        index_path.write_text(
            source.replace(
                '-webkit-text-stroke: 13px #203449;',
                '-webkit-text-stroke: 7px #111111;',
                1,
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(matrix.MatrixTemplateError, "style contract"):
            self.service._load_reference_catalog()

        index_path.write_text(
            source.replace(
                'id="typography" class="clip text-layer" data-start="0"',
                'id="typography" class="clip text-layer" data-start="0.1"',
                1,
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(matrix.MatrixTemplateError, "first frame"):
            self.service._load_reference_catalog()

    def test_reference_layers_preserve_copy_and_enforce_width_budget(self):
        top = "一家店不雇人，AI 当店员。以前组团队，现在也能开。"
        bottom = "评论区回复关键词，我把资料发你。"
        layers = matrix._reference_text_layers(top, bottom)
        top_layers = [layers["top1"], layers["top2"], layers["top3"]]
        bottom_layers = [layers["bottom1"], layers["bottom2"]]
        self.assertEqual(top, "".join(layers[key] for key in ("top1", "top2", "top3")))
        self.assertEqual(bottom, "".join(layers[key] for key in ("bottom1", "bottom2")))
        self.assertTrue(all(matrix._visual_width(item) <= 12 for item in top_layers))
        self.assertTrue(all(matrix._visual_width(item) <= 15 for item in bottom_layers))
        self.assertNotIn("组\n团队", "\n".join(top_layers))
        self.assertNotIn("AI \n当店员", "\n".join(top_layers))
        self.assertNotIn("关键\n词", "\n".join(bottom_layers))

        _source, display = matrix._reference_text_layout(top, bottom)
        edge_punctuation = set(matrix._REFERENCE_EDGE_PUNCTUATION)
        for value in display.values():
            if value:
                self.assertNotIn(value[0], edge_punctuation)
                self.assertNotIn(value[-1], edge_punctuation)
        self.assertIn("，", display["top3"])
        self.assertEqual("一家店不雇人，", layers["top1"])
        self.assertEqual("一家店不雇人", display["top1"])

        compact = matrix._reference_text_layers(
            "AI创业者活动", "评论区回复关键词获取活动资料"
        )
        self.assertEqual("AI创业者活动", compact["top1"])
        self.assertFalse(compact["top2"] or compact["top3"])
        self.assertEqual("评论区回复关键词", compact["bottom1"])
        self.assertEqual("获取活动资料", compact["bottom2"])
        self.assertNotIn("关键\n词", matrix._balanced_title(
            "评论区回复关键词获取活动资料", 15, 2
        ))
        self.assertNotIn("组团\n队", matrix._balanced_title(
            "以前开店要组团队盯店熬到凌晨", 12, 3
        ))

    def test_semantic_layers_preserve_english_and_mixed_spacing(self):
        samples = [
            "OpenAI, Codex, Agent workflow.",
            "AI team: sales, service, delivery.",
            "品牌 Alpha X200，支持 3 个门店。",
        ]
        for source in samples:
            with self.subTest(source=source):
                normalized = " ".join(source.split())
                layers = matrix._semantic_layers(source, 12, 3)
                self.assertEqual(normalized, "".join(layers))
                self.assertTrue(all(matrix._visual_width(item) <= 12 for item in layers))
        self.assertTrue(any(
            item.endswith(" ")
            for item in matrix._semantic_layers(samples[0], 12, 3)[:-1]
        ))
        self.assertTrue(any(
            item.endswith(" ")
            for item in matrix._semantic_layers(samples[1], 12, 3)[:-1]
        ))

    def test_semantic_layers_split_long_clause_and_fail_closed_when_unsafe(self):
        source = "一个人也能稳定开店持续接单，报名，领取。"
        layers = matrix._semantic_layers(source, 8, 3)
        self.assertEqual(source, "".join(layers))
        self.assertEqual(3, len(layers))
        self.assertTrue(all(matrix._visual_width(item) <= 8 for item in layers))

        no_punctuation = "AI创业者组团队开店接单资源共享"
        layers = matrix._semantic_layers(no_punctuation, 8, 3)
        self.assertEqual(no_punctuation, "".join(layers))
        self.assertTrue(all(matrix._visual_width(item) <= 8 for item in layers))
        joined = "\n".join(layers)
        for protected in ("创业者", "组团队", "资源共享"):
            for index in range(1, len(protected)):
                self.assertNotIn(protected[:index] + "\n" + protected[index:], joined)

        with self.assertRaisesRegex(ValueError, "宽度预算"):
            matrix._semantic_layers("中" * 25, 8, 3)
        with self.assertRaisesRegex(ValueError, "安全断句"):
            matrix._semantic_layers("ABCDEFGHIJKLMNOPQRSTUVWXYZ1234", 12, 2)

    def test_reference_template_rejects_copy_that_cannot_fit_layers(self):
        with self.assertRaisesRegex(ValueError, "顶部文案过长"):
            self.service.validate_payload({
                "top_text": "ABCDEFGHIJKLMNOPQRSTUVWXYZ1234",
                "bottom_text": "报名获取资料",
                "template_id": "ref-01-fixture-01",
            })
        with self.assertRaisesRegex(ValueError, "底部文案过长"):
            self.service.validate_payload({
                "top_text": "活动标题",
                "bottom_text": "ABCDEFGHIJKLMNOPQRSTUVWXYZ1234",
                "template_id": "ref-01-fixture-01",
            })

    def test_reference_template_accepts_real_long_copy_as_six_visual_lines(self):
        top = (
            "一家店，不雇人，AI 当店员，24 小时接单，老板该干嘛干嘛。"
            "以前开店要组团队、盯店、熬到凌晨，现在一个人就能开。"
        )
        bottom = "想了解的评论区扣「111」，我把资料发你。"
        payload = self.service.validate_payload({
            "top_text": top,
            "bottom_text": bottom,
            "template_id": "ref-17-fixture-17",
            "bgm": False,
        })
        frozen = self.service._freeze_font_provenance("8" * 32, payload)
        reference = frozen["_reference_template"]
        self.assertEqual(3, reference["top_layer_count"])
        self.assertEqual(
            top,
            "".join(reference["text"][key] for key in ("top1", "top2", "top3")),
        )
        self.assertEqual(
            bottom,
            "".join(reference["text"][key] for key in ("bottom1", "bottom2")),
        )
        top_lines = "\n".join(
            reference["display_text"][key] for key in ("top1", "top2", "top3")
        ).splitlines()
        bottom_lines = "\n".join(
            reference["display_text"][key] for key in ("bottom1", "bottom2")
        ).splitlines()
        self.assertEqual(
            [2, 2, 2],
            [
                len(reference["display_text"][key].splitlines())
                for key in ("top1", "top2", "top3")
            ],
        )
        self.assertEqual(6, len(top_lines))
        self.assertEqual(2, len(bottom_lines))
        self.assertTrue(all(matrix._visual_width(line) <= 12 for line in top_lines))
        self.assertTrue(all(matrix._visual_width(line) <= 15 for line in bottom_lines))
        edge_punctuation = set(matrix._REFERENCE_EDGE_PUNCTUATION)
        all_lines = top_lines + bottom_lines
        self.assertTrue(all(line[0] not in edge_punctuation for line in all_lines))
        self.assertTrue(all(line[-1] not in edge_punctuation for line in all_lines))
        self.assertIn("，", top_lines[0])
        self.assertIn("，", top_lines[1])
        self.assertEqual("\n".join(top_lines), frozen["_display_top_text"])
        self.assertEqual(
            "开店\n持续增长",
            matrix._reference_display_layers({
                "top1": "，开店。\n！持续增长？",
            })["top1"],
        )

    def test_two_layer_reference_template_moves_all_copy_out_of_top3(self):
        top = (
            "一家店，不雇人，AI 当店员，24 小时接单，老板该干嘛干嘛。"
            "以前开店要组团队、盯店、熬到凌晨，现在一个人就能开。"
        )
        bottom = "想了解的评论区扣「111」，我把资料发你。"
        payload = self.service.validate_payload({
            "top_text": top,
            "bottom_text": bottom,
            "template_id": "ref-02-fixture-02",
            "bgm": False,
        })
        frozen = self.service._freeze_font_provenance("2" * 32, payload)
        reference = frozen["_reference_template"]

        self.assertEqual(2, reference["top_layer_count"])
        self.assertEqual("", reference["text"]["top3"])
        self.assertEqual("", reference["display_text"]["top3"])
        self.assertEqual(
            top,
            reference["text"]["top1"] + reference["text"]["top2"],
        )
        self.assertEqual([2, 4], [
            len(reference["display_text"][key].splitlines())
            for key in ("top1", "top2")
        ])
        self.assertEqual(
            "\n".join(
                reference["display_text"][key]
                for key in ("top1", "top2")
            ),
            frozen["_display_top_text"],
        )

    def test_legacy_oversized_reference_request_remains_idempotent_after_restart(self):
        request_id = "legacy-reference-layout"
        top = "ABCDEFGHIJKLMNOPQRSTUVWXYZ1234"
        bottom = "报名获取资料"
        template_id = "ref-01-fixture-01"
        stored_payload = {
            "top_text": top,
            "bottom_text": bottom,
            "template_id": template_id,
            "duration": matrix._duration(top, bottom, None),
            "bgm": False,
            "_reference_template": {
                "pack_id": matrix.REFERENCE_PACK_ID,
                "engine": "hyperframes",
                "hyperframes_version": matrix.REFERENCE_HYPERFRAMES_VERSION,
                "variant": "v01",
                "duration": 8,
                "text": {
                    "top1": top,
                    "top2": "",
                    "top3": "",
                    "bottom1": "",
                    "bottom2": bottom,
                },
            },
        }
        existing, created = self.service.store.create(request_id, stored_payload)
        self.assertTrue(created)
        self.assertNotIn(
            "display_text",
            json.loads(self.service.store.get(existing["job_id"])["payload"])[
                "_reference_template"
            ],
        )
        raw = {
            "top_text": top,
            "bottom_text": bottom,
            "template_id": template_id,
            "bgm": False,
        }

        replay = self.service.submit(raw, request_id)
        self.assertEqual(existing["job_id"], replay["job_id"])
        with self.assertRaisesRegex(ValueError, "another payload"):
            self.service.submit({**raw, "top_text": "改" + top[1:]}, request_id)
        with self.assertRaisesRegex(ValueError, "顶部文案过长"):
            self.service.submit(raw, "legacy-reference-layout-new")

        version = SimpleNamespace(returncode=0, stdout="0.8.16\n", stderr="")
        with mock.patch.object(matrix.subprocess, "run", return_value=version):
            restarted = matrix.MatrixTemplateService(
                data_root=self.service.data_root,
                skill_root=self.skill,
                private_font_root=self.private_font_root,
                reference_skill_root=self.reference_skill,
                hyperframes_cli=self.cli,
                hyperframes_gsap=self.gsap,
                hyperframes_browser=self.browser,
                library_url="http://127.0.0.1:8111",
                library_token="library-token",
                start_worker=False,
            )
        try:
            replay_after_restart = restarted.submit(raw, request_id)
            self.assertEqual(existing["job_id"], replay_after_restart["job_id"])
            with self.assertRaisesRegex(ValueError, "another payload"):
                restarted.submit({**raw, "bottom_text": "修改行动文案"}, request_id)
            with self.assertRaisesRegex(ValueError, "顶部文案过长"):
                restarted.submit(raw, "legacy-reference-layout-new-after-restart")
        finally:
            restarted.shutdown()

    def test_reference_material_selection_requires_three_videos(self):
        payload = self.service.validate_payload({
            "top_text": "活动标题",
            "bottom_text": "报名获取资料",
            "template_id": "ref-02-fixture-02",
            "bgm": False,
        })
        captured = {}

        def selection(_method, _path, body):
            captured.update(body)
            return {"materials": [{
                "scene_id": f"media_{index:02d}",
                "sha256": format(index, "064x"),
                "media_type": "video",
                "record_id": f"video-{index}",
            } for index in range(1, 4)]}

        with mock.patch.object(self.service, "_library_request", side_effect=selection):
            materials = self.service._select_materials(payload, "2" * 32)
        self.assertEqual(3, len(materials))
        self.assertEqual(
            ["video", "video", "video"],
            [scene["media_type"] for scene in captured["scenes"]],
        )

    def test_five_reference_batch_jobs_reserve_fifteen_distinct_videos(self):
        batch_id = "c" * 32
        visual_pool = [format(index, "064x") for index in range(1, 21)]
        requests = []
        results = {}
        errors = []
        request_lock = threading.Lock()
        barrier = threading.Barrier(6)

        def selection(_method, _path, body):
            with request_lock:
                requests.append(dict(body))
            used = set(body.get("used_sha256") or [])
            available = [value for value in visual_pool if value not in used]
            return {"materials": [{
                "scene_id": f"media_{index:02d}",
                "sha256": available[index - 1],
                "media_type": "video",
                "record_id": "video-" + available[index - 1][:4],
            } for index in range(1, 4)]}

        def select_for(index):
            payload = self.service.validate_payload({
                "top_text": "批量活动标题",
                "bottom_text": "评论区回复关键词",
                "template_id": "ref-02-fixture-02",
                "batch_id": batch_id,
                "batch_index": index,
                "batch_size": 5,
                "bgm": False,
            })
            barrier.wait()
            try:
                results[index] = self.service._select_materials(
                    payload, format(index + 100, "032x")
                )
            except Exception as exc:
                errors.append(exc)

        with mock.patch.object(
            self.service, "_library_request", side_effect=selection
        ):
            threads = [
                threading.Thread(target=select_for, args=(index,))
                for index in range(1, 6)
            ]
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join(timeout=3)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertFalse(errors)
        self.assertEqual(5, len(results))
        self.assertEqual(15, len({
            item["sha256"]
            for materials in results.values()
            for item in materials
        }))
        self.assertEqual([0, 3, 6, 9, 12], sorted(
            len(item["used_sha256"]) for item in requests
        ))

    def test_five_reference_waiters_timeout_without_starting_render(self):
        self.service.hyperframes_slot_timeout_seconds = 0.05
        held_slots = [
            self.service.hyperframes_slots.acquire(timeout=0.1)
            for _ in range(self.service.hyperframes_concurrency)
        ]
        self.assertTrue(all(held_slots))
        barrier = threading.Barrier(6)
        errors = []
        lock = threading.Lock()

        def wait_for_slot():
            barrier.wait()
            try:
                self.service._acquire_hyperframes_slot(time.time() + 1)
            except Exception as exc:
                with lock:
                    errors.append(exc)
            else:
                self.service.hyperframes_slots.release()

        threads = [threading.Thread(target=wait_for_slot) for _ in range(5)]
        started = time.monotonic()
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=1)
        elapsed = time.monotonic() - started
        for _ in held_slots:
            self.service.hyperframes_slots.release()

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(5, len(errors))
        self.assertTrue(all(
            isinstance(exc, matrix.MatrixTemplateError)
            and "排队超时" in str(exc)
            for exc in errors
        ))
        self.assertLess(elapsed, 0.8)
        self.assertEqual(set(), self.service.active_processes)

    def test_five_reference_jobs_queue_behind_two_render_slots(self):
        self.service.hyperframes_slot_timeout_seconds = 1
        barrier = threading.Barrier(6)
        lock = threading.Lock()
        active = 0
        peak = 0
        completed = []
        errors = []

        def use_slot(index):
            nonlocal active, peak
            barrier.wait()
            try:
                self.service._acquire_hyperframes_slot(time.time() + 2)
                with lock:
                    active += 1
                    peak = max(peak, active)
                time.sleep(0.05)
                with lock:
                    active -= 1
                    completed.append(index)
                self.service.hyperframes_slots.release()
            except Exception as exc:
                with lock:
                    errors.append(exc)

        threads = [
            threading.Thread(target=use_slot, args=(index,))
            for index in range(5)
        ]
        started = time.monotonic()
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=2)
        elapsed = time.monotonic() - started

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertFalse(errors)
        self.assertEqual(5, len(completed))
        self.assertEqual(2, peak)
        self.assertGreaterEqual(elapsed, 0.1)

    def test_reference_segment_timing_caps_short_media_without_gaps(self):
        starts, durations = matrix._reference_segment_timing(
            14, [94.3, 3.9, 9.897]
        )
        for actual, expected in zip(starts, [0.0, 5.1, 8.9]):
            self.assertAlmostEqual(expected, actual)
        for actual, expected in zip(durations, [5.1, 3.8, 5.1]):
            self.assertAlmostEqual(expected, actual)
        self.assertAlmostEqual(14.0, sum(durations))
        self.assertTrue(all(
            duration <= source - matrix.REFERENCE_MEDIA_SAFETY_SECONDS + 0.001
            for duration, source in zip(durations, [94.3, 3.9, 9.897])
        ))

        starts, durations = matrix._reference_segment_timing(
            12, [30.0, 30.0, 30.0]
        )
        self.assertEqual([0.0, 4.0, 8.0], starts)
        self.assertEqual([4.0, 4.0, 4.0], durations)

        with self.assertRaisesRegex(
            matrix.MatrixTemplateError, "素材总时长不足"
        ):
            matrix._reference_segment_timing(14, [3.0, 3.0, 3.0])

    def test_reference_visual_coverage_rejects_sustained_black(self):
        clean = mock.Mock(returncode=0)
        clean.communicate.return_value = (
            b"", b"black_start:2 black_end:2.49 black_duration:0.49\n"
        )
        clean.poll.return_value = 0
        blocked = mock.Mock(returncode=0)
        blocked.communicate.return_value = (
            b"", b"black_start:8.03 black_end:13.97 black_duration:5.94\n"
        )
        blocked.poll.return_value = 0
        with mock.patch.object(matrix.subprocess, "Popen", return_value=clean):
            self.service._validate_reference_visual_coverage(
                self.root / "clean.mp4"
            )
        with mock.patch.object(matrix.subprocess, "Popen", return_value=blocked), \
             self.assertRaisesRegex(
                 matrix.MatrixTemplateError, "存在持续黑屏"
             ):
            self.service._validate_reference_visual_coverage(
                self.root / "blocked.mp4"
            )

    def test_reference_video_duration_falls_back_to_container(self):
        probe = SimpleNamespace(
            returncode=0,
            stdout=json.dumps({
                "streams": [{"codec_type": "video", "duration": "N/A"}],
                "format": {"duration": "3.900000"},
            }),
        )
        with mock.patch.object(matrix.subprocess, "run", return_value=probe):
            self.assertEqual(
                3.9,
                self.service._reference_video_duration(self.root / "video.mp4"),
            )

    def test_reference_render_uses_locked_variables_and_local_gsap(self):
        payload = self.service.validate_payload({
            "top_text": "深圳AI创业者活动",
            "bottom_text": "评论区回复OPC报名",
            "template_id": "ref-03-fixture-03",
            "font_family": "Noto Sans SC",
            "bgm": False,
        })
        payload = self.service._freeze_font_provenance("3" * 32, payload)
        payload["_reference_template"]["duration"] = 14
        fixed = payload["_reference_template"]["fixed_fonts"]["top2"]
        self.assertEqual("Smiley Sans Oblique", fixed["family"])
        self.assertEqual("HQSmileySansOblique", fixed["alias"])
        self.assertEqual("SmileySans-Oblique.ttf", fixed["file"])
        self.assertEqual(62, fixed["font_size_px"])
        self.assertTrue(any(
            item["family"] == "Smiley Sans Oblique"
            and item["source"] == "private"
            for item in payload["_font_provenance"]["fonts"]
        ))
        materials = []
        paths = []
        for index in range(1, 4):
            path = self.root / f"source-{index}.mp4"
            path.write_bytes(f"video-{index}".encode("ascii"))
            paths.append(path)
            materials.append({"media_type": "video", "record_id": f"v{index}"})
        process = mock.Mock()
        process.returncode = 0
        process.communicate.return_value = (b"", b"")
        process.poll.return_value = 0
        with mock.patch.object(
            self.service, "_reference_video_duration",
            side_effect=[94.3, 3.9, 9.897],
        ), mock.patch.object(
            matrix.subprocess, "Popen", return_value=process
        ) as popen:
            variables = self.service._render_reference(
                payload, "3" * 32, materials, paths
            )
        command = popen.call_args_list[0].args[0]
        self.assertEqual(str(self.cli), command[0])
        self.assertIn("--strict-variables", command)
        self.assertEqual("v03", variables["variant"])
        self.assertNotIn("font_family", variables)
        workdir = self.service.data_root / ("3" * 32) / "hyperframes"
        index = (workdir / "index.html").read_text(encoding="utf-8")
        self.assertIn(matrix.REFERENCE_GSAP_LOCAL, index)
        self.assertNotIn(matrix.REFERENCE_GSAP_CDN, index)
        self.assertIn(matrix.REFERENCE_EMPTY_LAYER_STYLE, index)
        self.assertIn(
            '@font-face{font-family:"HQSmileySansOblique";', index
        )
        self.assertIn(
            '.v03 .top2{font-family:"HQSmileySansOblique"!important;'
            'font-size:62px!important}', index
        )
        self.assertIn(
            'id="videoA" class="clip media-video" data-start="0" data-duration="5.1"',
            index,
        )
        self.assertIn(
            'id="videoB" class="clip media-video" data-start="5.1" data-duration="3.8"',
            index,
        )
        self.assertIn(
            'id="videoC" class="clip media-video" data-start="8.9" data-duration="5.1"',
            index,
        )
        self.assertIn('id="bgm" data-start="0" data-duration="14"', index)
        self.assertIn('id="typography" class="clip text-layer" data-start="0" data-duration="14"', index)
        self.assertIn("const segmentStarts = [0, 5.1, 8.9];", index)
        self.assertIn("const segmentDurations = [5.1, 3.8, 5.1];", index)
        self.assertNotIn(matrix.REFERENCE_DYNAMIC_TIMING_JS, index)
        self.assertEqual(
            set(matrix.REFERENCE_FONT_FILES) | {"SmileySans-Oblique.ttf"},
            {path.name for path in (workdir / "assets/fonts").iterdir()},
        )

    def test_v02_top2_matches_v03_fixed_font_and_size(self):
        payload = self.service.validate_payload({
            "top_text": "深圳AI沙龙\n高质量AI获客圈子",
            "bottom_text": "评论区回复666",
            "template_id": "ref-02-fixture-02",
            "bgm": False,
        })
        payload = self.service._freeze_font_provenance("2" * 32, payload)
        fixed = payload["_reference_template"]["fixed_fonts"]["top2"]
        self.assertEqual(
            ("Smiley Sans Oblique", "HQSmileySansOblique", 62),
            (fixed["family"], fixed["alias"], fixed["font_size_px"]),
        )
        self.assertIn(
            '.v02 .top2{font-family:"HQSmileySansOblique"!important;'
            'font-size:62px!important}',
            matrix._reference_private_font_style("v02", {"top2": fixed}),
        )

    def test_reference_render_hides_only_edge_punctuation(self):
        payload = self.service.validate_payload({
            "top_text": "开店，AI接单。团队持续增长！",
            "bottom_text": "评论区回复关键词，领取资料。",
            "template_id": "ref-03-fixture-03",
            "bgm": False,
        })
        payload = self.service._freeze_font_provenance("9" * 32, payload)
        reference = payload["_reference_template"]
        self.assertEqual(
            payload["top_text"],
            "".join(reference["text"][key] for key in ("top1", "top2", "top3")),
        )
        for value in reference["display_text"].values():
            if value:
                self.assertNotIn(value[0], set(matrix._REFERENCE_EDGE_PUNCTUATION))
                self.assertNotIn(value[-1], set(matrix._REFERENCE_EDGE_PUNCTUATION))
        self.assertIn("，", "".join(reference["display_text"].values()))

        materials = []
        paths = []
        for index in range(1, 4):
            path = self.root / f"edge-{index}.mp4"
            path.write_bytes(f"video-{index}".encode("ascii"))
            paths.append(path)
            materials.append({"media_type": "video", "record_id": f"edge-{index}"})
        process = mock.Mock()
        process.returncode = 0
        process.communicate.return_value = (b"", b"")
        process.poll.return_value = 0
        with mock.patch.object(
            self.service, "_reference_video_duration", return_value=30.0,
        ), mock.patch.object(matrix.subprocess, "Popen", return_value=process):
            variables = self.service._render_reference(
                payload, "9" * 32, materials, paths
            )
        self.assertEqual(reference["display_text"]["top1"], variables["top1"])
        self.assertEqual(reference["display_text"]["bottom2"], variables["bottom2"])

    def test_legacy_fixed_font_job_without_size_keeps_original_size(self):
        payload = self.service.validate_payload({
            "top_text": "郑州AI创业活动",
            "bottom_text": "评论区回复关键词",
            "template_id": "ref-03-fixture-03",
            "bgm": False,
        })
        payload = self.service._freeze_font_provenance("8" * 32, payload)
        fixed_fonts = payload["_reference_template"]["fixed_fonts"]
        fixed_fonts["top2"].pop("font_size_px")
        style = matrix._reference_private_font_style("v03", fixed_fonts)
        self.assertIn(
            '.v03 .top2{font-family:"HQSmileySansOblique"!important}', style
        )
        self.assertNotIn("font-size:", style)

    def test_fixed_font_size_rejects_untrusted_values(self):
        base = {
            "alias": "HQSmileySansOblique",
            "file": "SmileySans-Oblique.ttf",
        }
        for value in (True, 7, 241, "58"):
            with self.subTest(value=value), self.assertRaisesRegex(
                matrix.MatrixTemplateError, "元数据无效"
            ):
                matrix._reference_private_font_style(
                    "v03", {"top2": {**base, "font_size_px": value}}
                )

    def test_legacy_reference_job_without_fixed_font_keeps_original_style(self):
        payload = self.service.validate_payload({
            "top_text": "郑州AI创业活动",
            "bottom_text": "评论区回复关键词",
            "template_id": "ref-03-fixture-03",
            "bgm": False,
        })
        payload = self.service._freeze_font_provenance("7" * 32, payload)
        payload["_reference_template"].pop("fixed_fonts")
        payload["_font_provenance"]["fonts"] = [
            item for item in payload["_font_provenance"]["fonts"]
            if item["source"] != "private"
        ]
        materials = []
        paths = []
        for index in range(1, 4):
            path = self.root / f"legacy-font-{index}.mp4"
            path.write_bytes(f"video-{index}".encode("ascii"))
            paths.append(path)
            materials.append({"media_type": "video", "record_id": f"v{index}"})
        process = mock.Mock(returncode=0)
        process.communicate.return_value = (b"", b"")
        process.poll.return_value = 0
        with mock.patch.object(
            self.service, "_reference_video_duration", return_value=30.0,
        ), mock.patch.object(matrix.subprocess, "Popen", return_value=process):
            self.service._render_reference(
                payload, "7" * 32, materials, paths
            )
        workdir = self.service.data_root / ("7" * 32) / "hyperframes"
        index = (workdir / "index.html").read_text(encoding="utf-8")
        self.assertNotIn(matrix.REFERENCE_PRIVATE_FONT_STYLE_ID, index)
        self.assertFalse(
            (workdir / "assets/fonts/SmileySans-Oblique.ttf").exists()
        )

    def test_execute_routes_reference_template_to_hyperframes(self):
        payload = self.service.validate_payload({
            "top_text": "女性创业活动",
            "bottom_text": "评论区回复关键词",
            "template_id": "ref-04-fixture-04",
            "font_family": "AaHouDiHei",
            "bgm": False,
        })
        job, _ = self.service.store.create(
            "reference-execute",
            payload,
            freeze_payload=self.service._freeze_font_provenance,
        )
        materials = [{
            "scene_id": f"media_{index:02d}",
            "sha256": format(index, "064x"),
            "media_type": "video",
            "record_id": f"v{index}",
            "match_level": "exact",
        } for index in range(1, 4)]
        counter = iter(range(1, 4))

        def download(_item, target):
            path = target / f"{next(counter)}.mp4"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"video")
            return path

        captured_deadline = {}

        def render_reference(frozen, job_id, _materials, _paths, *, deadline_at):
            captured_deadline["value"] = deadline_at
            output = self.service.data_root / job_id / "output/final.mp4"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"video")
            return {
                **frozen["_reference_template"]["text"],
                "duration": frozen["_reference_template"]["duration"],
            }

        with mock.patch.object(self.service, "_select_materials", return_value=materials), \
             mock.patch.object(self.service, "_download", side_effect=download), \
             mock.patch.object(self.service, "_render_reference", side_effect=render_reference), \
             mock.patch.object(self.service, "_render", side_effect=AssertionError("FFmpeg renderer must not run")), \
             mock.patch.object(self.service, "_probe", return_value={"duration": 11.0, "width": 1080, "height": 1920}):
            result = self.service._execute(job["job_id"])

        self.assertEqual("hyperframes", result["engine"])
        self.assertEqual("template_locked", result["font_mode"])
        self.assertEqual("template-locked", result["font_selection"]["variant"])
        self.assertEqual(3, len(result["material_manifest"]))
        self.assertTrue(
            (self.service.data_root / job["job_id"] / "output/published.mp4").is_file()
        )
        row = self.service.store.get(job["job_id"])
        self.assertEqual(
            row["created_at"] + self.service.hyperframes_total_timeout_seconds,
            captured_deadline["value"],
        )


if __name__ == "__main__":
    unittest.main()
