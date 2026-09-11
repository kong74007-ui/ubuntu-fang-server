from __future__ import annotations

import copy
import hashlib
import html
import json
import os
import random
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from server import material_library_api, matrix_template_api as matrix


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
        self.assertEqual("shared", payload["material_policy"])
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

    def test_material_policy_rejects_unknown_values(self):
        body = {
            "top_text": "有效标题", "bottom_text": "有效行动",
        }
        for policy in ("private", "", True, ["shared"]):
            with self.subTest(policy=policy), self.assertRaisesRegex(
                ValueError, "material_policy"
            ):
                self.service.validate_payload(
                    dict(body, material_policy=policy)
                )

    def test_old_job_without_material_policy_replays_as_shared(self):
        body = {
            "top_text": "旧任务策略兼容",
            "bottom_text": "继续按共享素材回放",
            "bgm": False,
        }
        legacy = self.service.validate_payload(body)
        legacy.pop("material_policy")
        accepted, _created = self.service.store.create(
            "legacy-material-policy", legacy,
            freeze_payload=self.service._freeze_font_provenance,
        )

        replay = self.service.submit(body, "legacy-material-policy")

        self.assertEqual(accepted["job_id"], replay["job_id"])

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

    def test_reference_duration_is_between_8_and_15_seconds(self):
        outcomes = set()
        for index in range(2048):
            value = matrix._reference_duration(
                f"{index:032x}", "ref-01-fixture-01",
            )
            self.assertGreaterEqual(value, 8)
            self.assertLessEqual(value, 15)
            outcomes.add(value)
        self.assertEqual({8, 9, 10, 11, 12, 13, 14, 15}, outcomes)

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
        service._library_readiness_cache = (float("inf"), {
            "ready": True,
            "selection_contract_version": 2,
            "clip_contract_version": 3,
        })
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
                {"scene_id": "media_03", "sha256": "c" * 64, "media_type": "image", "record_id": "i2"},
                {"scene_id": "bgm", "sha256": "d" * 64, "media_type": "bgm", "record_id": "m1"},
            ]}

        with mock.patch.object(self.service, "_library_request", side_effect=selection):
            materials = self.service._select_materials(payload, "f" * 32)
        self.assertEqual(
            ["video", "image", "image", "bgm"],
            [item["media_type"] for item in materials],
        )
        self.assertEqual("video", captured["scenes"][0]["media_type"])
        self.assertTrue(all(
            2 <= scene["clip_duration_seconds"] <= 3
            for scene in captured["scenes"][:-1]
        ))
        self.assertEqual("portrait", captured["orientation"])
        self.assertEqual("round_robin", captured["selection_mode"])
        self.assertEqual([], captured["used_sha256"])
        self.assertEqual(4, len(set(item["sha256"] for item in materials)))

    def test_non_batch_material_selection_is_frozen_and_replayed_after_restart(self):
        payload = self.service.validate_payload({
            "top_text": "素材切片冻结",
            "bottom_text": "评论区获取资料",
            "bgm": False,
        })
        payload = self.service._freeze_font_provenance("a" * 32, payload)
        self.assertEqual(
            2, payload["_material_selection_contract_version"],
        )
        calls = []

        def selection(_method, _path, body):
            calls.append(body)
            generation = len(calls)
            materials = []
            for index, scene in enumerate(body["scenes"], 1):
                duration = float(scene["clip_duration_seconds"])
                materials.append({
                    "scene_id": scene["scene_id"],
                    "sha256": format(generation * 100 + index, "064x"),
                    "media_type": "video",
                    "record_id": f"video-{generation}-{index}",
                    "clip_id": format(generation * 1000 + index, "064x"),
                    "clip_start_seconds": float(index - 1) * 3,
                    "clip_duration_seconds": round(duration, 3),
                    "clip_slot_index": index,
                    "clip_slot_count": len(body["scenes"]),
                })
            return {
                "selection_contract_version": 2,
                "clip_contract_version": 3,
                "materials": materials,
            }

        job_id = "b" * 32
        with mock.patch.object(
            self.service, "_library_request", side_effect=selection,
        ):
            first = self.service._select_materials(payload, job_id)
            second = self.service._select_materials(payload, job_id)

        self.assertEqual(first, second)
        self.assertEqual(1, len(calls))
        self.assertTrue(all(item.get("clip_id") for item in first))
        frozen = self.service.store.material_selection(job_id)
        self.assertEqual(2, frozen["selection_contract_version"])
        self.assertEqual(first, frozen["materials"])

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
                side_effect=AssertionError("frozen selection must replay"),
            ):
                self.assertEqual(
                    first, restarted._select_materials(payload, job_id),
                )
        finally:
            restarted.shutdown()

    def test_remote_receipt_closes_crash_before_local_freeze(self):
        library_root = self.root / "material-library"
        files = library_root / "files"
        files.mkdir(parents=True)
        rows = []
        for index in range(5):
            content = f"video-{index}".encode("ascii")
            sha256 = hashlib.sha256(content).hexdigest()
            path = files / f"video-{index}.mp4"
            path.write_bytes(content)
            rows.append({
                "record_id": f"video-{index}",
                "sha256": sha256,
                "素材名称": f"video-{index}",
                "状态": "可使用",
                "画面方向": "横屏",
                "时长秒": 10.0,
                "导入批次": f"batch-{index}",
                "二级场景": f"scene-{index}",
                "server_relative_path": path.relative_to(
                    library_root
                ).as_posix(),
            })
        (library_root / "index.jsonl").write_text(
            "".join(
                json.dumps(row, ensure_ascii=False) + "\n" for row in rows
            ),
            encoding="utf-8",
        )
        usage_path = self.root / "material-state/usage.json"
        usage_path.parent.mkdir()
        library_server = material_library_api.build_server(
            "127.0.0.1", 0, library_root, "library-token",
            usage_path=usage_path,
        )
        library_thread = threading.Thread(
            target=library_server.serve_forever, daemon=True,
        )
        library_thread.start()
        library_url = "http://127.0.0.1:%d" % library_server.server_port
        self.service.library_url = library_url
        payload = self.service.validate_payload({
            "top_text": "远端回执恢复",
            "bottom_text": "评论区获取资料",
            "bgm": False,
        })
        payload = self.service._freeze_font_provenance("f" * 32, payload)
        job_id = "f" * 32

        try:
            with mock.patch.object(
                self.service.store, "reserve_job_materials",
                side_effect=SystemExit("simulated crash"),
            ), self.assertRaisesRegex(SystemExit, "simulated crash"):
                self.service._select_materials(payload, job_id)

            receipt_path = library_server.library._receipt_path(
                "matrix-template:" + job_id
            )
            receipt = json.loads(
                receipt_path.read_text(encoding="utf-8")
            )["result"]["materials"]
            restarted = matrix.MatrixTemplateService(
                data_root=self.service.data_root,
                skill_root=self.skill,
                library_url=library_url,
                library_token="library-token",
                start_worker=False,
            )
            try:
                recovered = restarted._select_materials(payload, job_id)
            finally:
                restarted.shutdown()

            self.assertEqual(receipt, recovered)
            after = json.loads(usage_path.read_text(encoding="utf-8"))
            source_counts = [
                value["count"]
                for key, value in after.items()
                if key in {row["sha256"] for row in rows}
            ]
            self.assertEqual([1, 1, 1], sorted(source_counts))
        finally:
            library_server.shutdown()
            library_server.server_close()
            library_thread.join(timeout=2)

    def test_new_job_rejects_old_or_incomplete_clip_contract(self):
        payload = self.service.validate_payload({
            "top_text": "切片契约校验",
            "bottom_text": "评论区获取资料",
            "bgm": False,
        })
        payload = self.service._freeze_font_provenance("c" * 32, payload)
        scenes, _count, _reference = self.service._material_scenes(payload)
        legacy_materials = [{
            "scene_id": scene["scene_id"],
            "sha256": format(index, "064x"),
            "media_type": "video",
            "record_id": f"video-{index}",
        } for index, scene in enumerate(scenes, 1)]

        with mock.patch.object(
            self.service, "_library_request",
            return_value={"materials": legacy_materials},
        ), self.assertRaisesRegex(
            matrix.MatrixTemplateError, "能力版本不兼容",
        ):
            self.service._select_materials(payload, "c" * 32)

        with mock.patch.object(
            self.service, "_library_request", return_value={
                "selection_contract_version": 2,
                "clip_contract_version": 3,
                "materials": legacy_materials,
            },
        ), self.assertRaisesRegex(
            matrix.MatrixTemplateError, "切片契约不完整",
        ):
            self.service._select_materials(payload, "d" * 32)

    def test_legacy_material_selection_row_is_marked_contract_v1(self):
        path = self.root / "legacy-material-selection.db"
        database = sqlite3.connect(path)
        try:
            database.execute("""CREATE TABLE batch_material_selections(
                job_id TEXT PRIMARY KEY,
                batch_id TEXT NOT NULL,
                materials TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )""")
            database.execute(
                "INSERT INTO batch_material_selections("
                "job_id,batch_id,materials,created_at) VALUES(?,?,?,?)",
                ("e" * 32, "", "[]", 1),
            )
            database.commit()
        finally:
            database.close()

        store = matrix.JobStore(path)
        selection = store.material_selection("e" * 32)

        self.assertEqual(1, selection["selection_contract_version"])
        self.assertEqual([], selection["materials"])

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
                {"scene_id": "media_03", "sha256": available[2], "media_type": "image", "record_id": "i-" + available[2][:4]},
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
            self.assertEqual(15, len({
                item["sha256"] for materials in results.values()
                for item in materials if item["media_type"] in {"image", "video"}
            }))
            self.assertEqual([0, 3, 6, 9, 12], sorted(
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
            {"scene_id": "media_01", "sha256": "a" * 64, "media_type": "video", "record_id": "v1", "match_level": "exact", "clip_id": "e" * 64, "clip_start_seconds": 1.25, "clip_duration_seconds": 2.667, "clip_slot_index": 2, "clip_slot_count": 4},
            {"scene_id": "media_02", "sha256": "b" * 64, "media_type": "image", "record_id": "i1", "match_level": "loose"},
            {"scene_id": "media_03", "sha256": "c" * 64, "media_type": "image", "record_id": "i2", "match_level": "loose"},
            {"scene_id": "bgm", "sha256": "d" * 64, "media_type": "bgm", "record_id": "m1", "match_level": "random"},
        ]
        counter = iter(range(4))

        def download(item, target, job_id=""):
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
             mock.patch.object(self.service, "_reference_video_duration", return_value=10.0), \
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
        self.assertEqual(3, len(project["scenes"][0]["media"]))
        self.assertEqual(1.25, project["scenes"][0]["media"][0]["start"])
        self.assertEqual("huangque-internal-api", project["material_library"]["index_source"])
        self.assertEqual("/v1/files/%s.mp4" % job["job_id"], result["file_url"])
        self.assertEqual(
            ["v1", "i1", "i2", "m1"],
            [item["record_id"] for item in result["material_manifest"]],
        )
        self.assertEqual(2, result["material_selection_contract_version"])
        self.assertEqual(3, result["material_clip_contract_version"])
        self.assertEqual("e" * 64, result["material_manifest"][0]["clip_id"])
        self.assertEqual(1.25, result["material_manifest"][0]["clip_start_seconds"])
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

    def test_old_pending_native_job_renders_after_public_catalog_removal(self):
        payload = self.service.validate_payload({
            "top_text": "旧任务继续完成",
            "bottom_text": "新任务不再开放旧模板",
            "template_id": "full-overlay-bold",
            "bgm": False,
        })
        job, _created = self.service.store.create(
            "legacy-upgrade-recovery", payload,
            freeze_payload=self.service._freeze_font_provenance,
        )
        self.service.store.update(job["job_id"], "running")
        renderer = self.skill / "scripts/render_video.py"
        renderer.write_text(
            """import json
import sys
from pathlib import Path

project_path = Path(sys.argv[1])
project = json.loads(project_path.read_text(encoding=\"utf-8\"))
catalog = json.loads(
    (Path(__file__).parents[1] / \"assets/templates/catalog.json\")
    .read_text(encoding=\"utf-8\")
)
allowed = {item[\"id\"] for item in catalog[\"templates\"]}
if project[\"layout\"][\"template_id\"] not in allowed:
    raise SystemExit(9)
output = project_path.parent / \"output/final.mp4\"
output.parent.mkdir(parents=True, exist_ok=True)
output.write_bytes(b\"ftyp\" + b\"x\" * 2048)
""",
            encoding="utf-8",
        )
        restarted = matrix.MatrixTemplateService(
            data_root=self.service.data_root,
            skill_root=self.skill,
            library_url="http://127.0.0.1:8111",
            library_token="library-token",
            legacy_templates_enabled=False,
            start_worker=False,
        )
        materials = [
            {
                "scene_id": "media_01", "sha256": "a" * 64,
                "media_type": "video", "record_id": "legacy-video",
                "match_level": "random", "clip_id": "e" * 64,
                "clip_start_seconds": 0.5, "clip_duration_seconds": 2.667,
                "clip_slot_index": 1, "clip_slot_count": 1,
            },
            {
                "scene_id": "media_02", "sha256": "b" * 64,
                "media_type": "image", "record_id": "legacy-image-1",
                "match_level": "random",
            },
            {
                "scene_id": "media_03", "sha256": "c" * 64,
                "media_type": "image", "record_id": "legacy-image-2",
                "match_level": "random",
            },
        ]
        counter = iter(range(len(materials)))

        def download(item, target, job_id=""):
            suffix = ".mp4" if item["media_type"] == "video" else ".jpg"
            path = target / (str(next(counter)) + suffix)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"asset")
            return path

        try:
            self.assertEqual([], restarted.catalog)
            with self.assertRaisesRegex(ValueError, "请选择有效模板"):
                restarted.validate_payload({
                    "top_text": "不能新建旧模板",
                    "bottom_text": "接单前直接拒绝",
                    "template_id": "full-overlay-bold",
                })
            with mock.patch.object(
                restarted, "_select_materials", return_value=materials,
            ), mock.patch.object(
                restarted, "_download", side_effect=download,
            ), mock.patch.object(
                restarted, "_reference_video_duration", return_value=10.0,
            ), mock.patch.object(
                restarted, "_probe",
                return_value={"duration": 8.0, "width": 1080, "height": 1920},
            ):
                result = restarted._execute(job["job_id"])
            project = json.loads(
                (restarted.data_root / job["job_id"] / "project.json")
                .read_text(encoding="utf-8")
            )
            self.assertEqual(
                "full-overlay-bold", project["layout"]["template_id"],
            )
            self.assertEqual("ffmpeg", result["engine"])
            self.assertTrue(
                (restarted.data_root / job["job_id"] / "output/published.mp4")
                .is_file()
            )
        finally:
            restarted.shutdown()

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
        self.service._library_request = mock.Mock(return_value={
            "ok": True,
            "records": 1,
            "selection_contract_version": 2,
            "clip_contract_version": 3,
        })
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
                    {"2": 0, "3": 0, "4": 0}, health["reference_top_layer_counts"]
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
            self.assertEqual((14.9, 5), (
                preflight["duration"], preflight["required_visuals"]))
            self.assertEqual(2, preflight["material_selection_contract_version"])
            self.assertEqual(3, preflight["material_clip_contract_version"])
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

    def test_health_and_preflight_reject_old_material_library_contract(self):
        class OldMaterialLibraryHandler(BaseHTTPRequestHandler):
            def log_message(self, _format, *_args):
                return

            def do_GET(self):
                body = json.dumps({
                    "ok": True,
                    "records": 10,
                    "selection_contract_version": 2,
                    "clip_contract_version": 2,
                }).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        old_library = ThreadingHTTPServer(
            ("127.0.0.1", 0), OldMaterialLibraryHandler,
        )
        old_library_thread = threading.Thread(
            target=old_library.serve_forever, daemon=True,
        )
        old_library_thread.start()
        self.service.library_url = (
            "http://127.0.0.1:%d" % old_library.server_port
        )
        self.service.enforce_library_readiness = True
        self.service._library_readiness_cache = None
        health = self.service.health()
        self.assertFalse(health["ok"])
        self.assertFalse(health["material_library_ready"])
        self.assertEqual(2, health["material_clip_contract_version"])

        server = matrix.build_server(
            "127.0.0.1", 0, self.service, "api-token",
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            request = urllib.request.Request(
                "http://127.0.0.1:%d/v1/preflight" % server.server_port,
                data=json.dumps({
                    "top_text": "素材服务版本检查",
                    "bottom_text": "评论区获取资料",
                    "template_id": matrix.TRIPLE_STRIP_TEMPLATE_ID,
                }).encode("utf-8"),
                method="POST",
                headers={"Authorization": "Bearer api-token"},
            )
            with self.assertRaises(urllib.error.HTTPError) as rejected:
                urllib.request.urlopen(request, timeout=3)
            self.assertEqual(409, rejected.exception.code)
            self.assertEqual([], self.service.store.pending_ids())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            old_library.shutdown()
            old_library.server_close()
            old_library_thread.join(timeout=2)

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
        styles = [
            "* { box-sizing: border-box; }",
            ".top, .bottom { width: 100%; padding-left: 42px; padding-right: 42px; }",
            (
                ".top1, .top2, .top3, .bottom1, .bottom2 "
                "{ max-width: 996px; letter-spacing: .01em; }"
            ),
        ]
        for index in range(1, 18):
            variant = f"v{index:02d}"
            if variant == matrix.REFERENCE_V01_VARIANT:
                styles.extend((
                    '.v01 .top1 { font: 400 70px/1.08 "MaShan"; color: #f7f5ec; -webkit-text-stroke: 11px #789822; }',
                    '.v01 .top2 { font: 400 64px/1.15 "MaShan"; color: #f8f7ef; -webkit-text-stroke: 9px #789822; }',
                    '.v01 .top3 { font-size: 52px; font-weight: 900; color: #fff; -webkit-text-stroke: 7px #111; }',
                    '.v01 .bottom1 { font: 400 56px/1.05 "MaShan"; color: #fff; -webkit-text-stroke: 7px #111; }',
                    '.v01 .bottom2 { max-width: 900px; padding: 14px 26px; font: 400 74px/1.15 "MaShan"; background: #f5f4ee; color: #426d24; border-radius: 22px; }',
                ))
                continue
            if variant == matrix.REFERENCE_FEATURED_VARIANT:
                styles.extend((
                    '.v05 .top1 { font: 900 102px/1.02 "NotoSC"; color: #f4f7f2; -webkit-text-stroke: 12px #203449; text-shadow: 8px 10px 0 #07111e; }',
                    '.v05 .top2 { font: 900 104px/1.01 "NotoSC"; color: #f4f7f2; -webkit-text-stroke: 13px #203449; text-shadow: 9px 11px 0 #07111e; }',
                    '.v05 .top3 { font: 900 68px/1.04 "NotoSC"; color: #fff8d9; -webkit-text-stroke: 9px #26394a; text-shadow: 7px 8px 0 #07111e; }',
                    '.v05 .bottom1 { font: 900 68px/1.05 "NotoSC"; color: #ffe000; -webkit-text-stroke: 9px #263e32; }',
                    '.v05 .bottom2 { max-width: 930px; padding: 18px 34px 24px; font: 900 70px/1.06 "NotoSC"; background: #f4c900; color: #26362d; border-radius: 28px; }',
                ))
                continue
            if variant == matrix.REFERENCE_V07_VARIANT:
                styles.extend((
                    '.v07 .top1 { font-family: "NotoSC"; font-size: 118px; font-weight: 900; letter-spacing: -0.045em; color: #d4140d; -webkit-text-stroke: 13px #ffe9be; paint-order: stroke fill; }',
                    '.v07 .top2 { font-family: "NotoSC"; font-size: 82px; font-weight: 900; letter-spacing: -0.045em; color: #ffd51c; -webkit-text-stroke: 11px #101010; paint-order: stroke fill; }',
                    '.v07 .top3 { font-family: "NotoSC"; font-size: 51px; font-weight: 900; letter-spacing: -0.045em; color: #ffd51c; -webkit-text-stroke: 8px #101010; paint-order: stroke fill; }',
                    '.v07 .bottom1 { font-family: "NotoSC"; font-size: 57px; font-weight: 900; letter-spacing: -0.045em; color: #d4140d; -webkit-text-stroke: 9px #ffe9be; paint-order: stroke fill; }',
                    '.v07 .bottom2 { font-family: "NotoSC"; font-size: 86px; font-weight: 900; letter-spacing: -0.045em; color: #d4140d; -webkit-text-stroke: 11px #ffe9be; paint-order: stroke fill; }',
                ))
                continue
            if index == 10:
                styles.extend((
                    '.v10 .top { top: 92px; }',
                    '.v10 .top1 { font: 400 85px/1.02 "XiaoWei"; color: #fff; -webkit-text-stroke: 8px #111; }',
                    '.v10 .top2 { font: 400 78px/1.03 "XiaoWei"; color: #ffd819; -webkit-text-stroke: 8px #111; }',
                    '.v10 .top3 { font-size: 65px; font-weight: 800; color: #fff; -webkit-text-stroke: 6px #111; }',
                    '.v10 .bottom { padding-left: 68px; }',
                    '.v10 .bottom1 { font: 400 68px/1.04 "XiaoWei"; color: #fff; -webkit-text-stroke: 8px #111; }',
                    '.v10 .bottom2 { font: 400 80px/1.03 "XiaoWei"; color: #fff; -webkit-text-stroke: 9px #111; }',
                ))
                continue
            if index == 12:
                styles.extend((
                    '.v12 .top1 { font: 400 80px/1.08 "MaShan"; color: #fff; -webkit-text-stroke: 10px #111; }',
                    '.v12 .top2 { font: 400 62px/1.05 "MaShan"; color: #fff; -webkit-text-stroke: 9px #111; }',
                    '.v12 .top3 { font: 400 70px/1.12 "MaShan"; color: #ffe036; -webkit-text-stroke: 7px #111; }',
                    '.v12 .bottom1 { font: 400 58px/1.08 "MaShan"; color: #fff; -webkit-text-stroke: 8px #111; }',
                    '.v12 .bottom2 { font: 400 62px/1.08 "MaShan"; color: #ffe036; -webkit-text-stroke: 8px #111; }',
                ))
                continue
            if index == 16:
                styles.extend((
                    '.v16 .top1 { font: 400 80px/1.1 "XiaoWei"; color: #fff1af; -webkit-text-stroke: 5px #111; }',
                    '.v16 .top2 { font: 400 68px/1.04 "XiaoWei"; color: #fff; -webkit-text-stroke: 7px #111; }',
                    '.v16 .top3 { font-size: 52px; font-weight: 900; color: #fff; -webkit-text-stroke: 6px #111; }',
                    '.v16 .bottom1 { font: 400 70px/1.04 "XiaoWei"; color: #fff0b0; -webkit-text-stroke: 8px #111; }',
                    '.v16 .bottom2 { font: 400 70px/1.04 "XiaoWei"; color: #fff; -webkit-text-stroke: 8px #111; }',
                ))
                continue
            styles.extend((
                f".{variant} .top1 {{ font-size: 80px; }}",
                f".{variant} .top2 {{ font-size: 60px; }}",
                f".{variant} .bottom2 {{ font-size: {80 if index == 4 else 70}px; }}",
            ))
            if index in top3_variants:
                styles.append(f".{variant} .top3 {{ font-size: 50px; }}")
            if index == 4:
                styles.append(".v04 .top3 { padding: 14px 24px; }")
            if index == 6:
                styles.append(".v06 .bottom2 { padding: 20px 36px; }")
            if index == 8:
                styles.append(".v08 .bottom2 { padding: 10px 24px; }")
        reference_variables = html.escape(json.dumps([
            {"id": "videoA", "type": "string"},
            {"id": "videoB", "type": "string"},
            {"id": "videoC", "type": "string"},
            {"id": "bgm", "type": "string"},
        ], separators=(",", ":")), quote=True)
        timeline_fixture = """
<div id="root">
  <video data-hf-id="a" id="videoA" class="clip media-video" data-start="0" data-duration="2.666667" data-var-src="videoA" src="a.mp4"></video>
  <video data-hf-id="b" id="videoB" class="clip media-video" data-start="2.666667" data-duration="2.666666" data-var-src="videoB" src="b.mp4"></video>
  <video data-hf-id="c" id="videoC" class="clip media-video" data-start="5.333333" data-duration="2.666667" data-var-src="videoC" src="c.mp4"></video>
  <audio id="bgm" data-start="0" data-duration="8" data-var-src="bgm" src="assets/bgm/silence.m4a"></audio>
  <section id="typography" class="clip text-layer" data-start="0" data-duration="8"></section>
</div>
<script>
      const duration = 8;
""" + matrix.REFERENCE_DYNAMIC_TIMING_JS + """
      const videos = [
        document.getElementById("videoA"),
        document.getElementById("videoB"),
        document.getElementById("videoC")
      ];
""" + matrix.REFERENCE_BASE_TIMELINE_JS + """
</script>
"""
        (pack / "index.html").write_text(
            f'<html data-composition-variables="{reference_variables}"><head><style>\n'
            + "\n".join(styles) + "\n</style>\n"
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

    def _reference_bgm_inputs(self, job_id: str):
        payload = self.service.validate_payload({
            "top_text": "团队8个人每天产出100条短视频",
            "bottom_text": "想进健康赛道评论区留言",
            "template_id": "ref-17-fixture-17",
            "bgm": True,
        })
        payload = self.service._freeze_font_provenance(job_id, payload)
        payload["_reference_template"]["duration"] = 14
        payload["_reference_template"]["editing_plan"] = (
            matrix._reference_editing_plan(
                job_id, "ref-17-fixture-17", 5
            )
        )
        prefix = job_id[:8]
        materials = []
        paths = []
        for index in range(1, 6):
            path = self.root / f"{prefix}-bgm-video-{index}.mp4"
            path.write_bytes(f"video-{index}".encode("ascii"))
            paths.append(path)
            materials.append({"media_type": "video", "record_id": f"v{index}"})
        bgm_path = self.root / f"{prefix}-selected-bgm.mp3"
        bgm_path.write_bytes(b"selected-bgm")
        paths.append(bgm_path)
        materials.append({"media_type": "bgm", "record_id": "bgm-1"})
        return payload, materials, paths

    def test_reference_catalog_is_19_and_ignores_font_selection(self):
        self.assertEqual(19, len(self.service.catalog))
        self.assertEqual(17, len(self.service.reference_templates))
        for item in self.service.reference_templates.values():
            self.assertEqual(3, item["required_visuals"])
            self.assertEqual(5, item["required_visuals_max"])
            self.assertEqual(
                [2.0, 3.0], item["clip_duration_range_seconds"],
            )
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
            (1, 996, 2, 4, 2),
            (
                self.service.templates["ref-02-fixture-02"]["semantic_layout"]["version"],
                self.service.templates["ref-02-fixture-02"]["semantic_layout"]["max_width_px"],
                self.service.templates["ref-02-fixture-02"]["semantic_layout"]["layers"]["top1"]["max_lines"],
                self.service.templates["ref-02-fixture-02"]["semantic_layout"]["layers"]["top2"]["max_lines"],
                self.service.templates["ref-02-fixture-02"]["semantic_layout"]["layers"]["bottom2"]["max_lines"],
            ),
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
            {
                "top2": "Smiley Sans Oblique",
                "bottom1": "Smiley Sans Oblique",
                "bottom2": "Smiley Sans Oblique",
            },
            self.service.templates["ref-16-fixture-16"]["fixed_fonts"],
        )
        self.assertEqual(
            {}, self.service.templates["ref-04-fixture-04"]["fixed_fonts"]
        )
        self.assertEqual(
            ["Smiley Sans Oblique"],
            self.service.health()["reference_fixed_private_fonts"],
        )
        self.assertEqual(
            [
                "v01", "v02", "v03", "v04", "v05", "v06",
                "v07", "v08", "v09", "v10", "v11", "v12",
                "v13", "v14", "v15", "v16", "v17",
            ],
            self.service.health()["reference_semantic_layout_templates"],
        )
        self.assertEqual(
            ("Ma Shan Zheng", 70, 400, 11, 2),
            tuple(
                self.service.reference_semantic_layouts["v01"]["top1"][key]
                for key in (
                    "family", "font_size_px", "font_weight",
                    "stroke_px", "max_lines",
                )
            ),
        )
        self.assertEqual(
            ("Smiley Sans Oblique", 62, 400, 4),
            tuple(
                self.service.reference_semantic_layouts["v02"]["top2"][key]
                for key in (
                    "family", "font_size_px", "font_weight", "max_lines",
                )
            ),
        )
        self.assertEqual(
            (900, 900, 900, 900),
            tuple(
                self.service.reference_semantic_layouts["v05"][layer]["font_weight"]
                for layer in ("top1", "top2", "top3", "bottom2")
            ),
        )
        self.assertEqual(
            80,
            self.service.reference_semantic_layouts["v04"]["bottom2"][
                "font_size_px"
            ],
        )
        self.assertEqual(
            (80, 70),
            tuple(
                self.service.reference_semantic_layouts["v12"][layer][
                    "font_size_px"
                ]
                for layer in ("top1", "top3")
            ),
        )
        self.assertEqual(
            (80, 68, 70),
            tuple(
                self.service.reference_semantic_layouts["v16"][layer][
                    "font_size_px"
                ]
                for layer in ("top1", "top2", "bottom2")
            ),
        )
        self.assertEqual(
            ("Smiley Sans Oblique", "Smiley Sans Oblique"),
            tuple(
                self.service.reference_semantic_layouts["v16"][layer][
                    "family"
                ]
                for layer in ("top2", "bottom2")
            ),
        )
        self.assertEqual(
            970,
            self.service.reference_semantic_layouts["v10"]["bottom2"][
                "max_width_px"
            ],
        )
        self.assertEqual(
            (85, 65),
            tuple(
                self.service.reference_semantic_layouts["v10"][layer][
                    "font_size_px"
                ]
                for layer in ("top1", "top3")
            ),
        )
        self.assertEqual(
            970,
            next(
                item for item in self.service.catalog
                if item.get("variant") == "v10"
            )["semantic_layout"]["layers"]["bottom2"]["max_width_px"],
        )
        expected_widths = {
            ("v01", "bottom2"): 848,
            ("v04", "top3"): 948,
            ("v05", "bottom2"): 862,
            ("v06", "bottom2"): 924,
            ("v08", "bottom2"): 948,
            ("v10", "bottom2"): 970,
        }
        self.assertEqual(
            expected_widths,
            {
                key: self.service.reference_semantic_layouts[key[0]][key[1]][
                    "max_width_px"
                ]
                for key in expected_widths
            },
        )
        self.assertEqual(
            (102, 104, 68, 70),
            tuple(
                self.service.reference_semantic_layouts["v05"][layer]["font_size_px"]
                for layer in ("top1", "top2", "top3", "bottom2")
            ),
        )
        for item in self.service.reference_templates.values():
            expected_layers = {"top1", "top2", "bottom2"}
            if item["text_layers"]["top"] == 3:
                expected_layers.add("top3")
            if item["variant"] == matrix.REFERENCE_V07_VARIANT:
                expected_layers.update({"top3", "bottom1"})
            self.assertEqual(
                expected_layers,
                set(self.service.reference_semantic_layouts[item["variant"]]),
            )
        self.assertEqual(
            {
                "v01", "v04", "v05", "v06", "v08",
                "v10", "v11", "v12", "v16", "v17",
            },
            {
                item["variant"] for item in self.service.reference_templates.values()
                if item["text_layers"]["top"] == 3
            },
        )
        self.assertEqual(
            {"v07"},
            {
                item["variant"] for item in self.service.reference_templates.values()
                if item["text_layers"]["top"] == 4
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
        self.assertTrue(3 <= self.service.required_visuals(frozen) <= 5)
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

    def test_reference_catalog_rejects_unmeasurable_bottom_style(self):
        index_path = (
            self.reference_skill / "assets/templates"
            / matrix.REFERENCE_PACK_ID / "index.html"
        )
        source = index_path.read_text(encoding="utf-8")
        expected = ".v09 .bottom2 { font-size: 70px; }"
        self.assertIn(expected, source)
        index_path.write_text(
            source.replace(expected, ".v09 .bottom2 { color: #fff; }", 1),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            matrix.MatrixTemplateError, "font size is missing",
        ):
            self.service._load_reference_catalog()

    def test_reference_measure_font_applies_weight_axis_and_separates_cache(self):
        created = []

        class VariableFont:
            def __init__(self):
                self.weight = None

            def get_variation_axes(self):
                return [{
                    "minimum": 100, "default": 100,
                    "maximum": 900, "name": b"Weight",
                }]

            def set_variation_by_axes(self, values):
                self.weight = values[0]

        def truetype(_path, _size):
            font = VariableFont()
            created.append(font)
            return font

        self.service.reference_measure_fonts.clear()
        with mock.patch.object(matrix.ImageFont, "truetype", side_effect=truetype):
            regular = self.service._reference_measure_font(
                "Noto Sans SC", 104, 400,
            )
            bold = self.service._reference_measure_font(
                "Noto Sans SC", 104, 900,
            )
            self.assertIs(
                bold,
                self.service._reference_measure_font(
                    "Noto Sans SC", 104, 900,
                ),
            )
        self.assertEqual((400, 900), (regular.weight, bold.weight))
        self.assertEqual(2, len(created))

    def test_reference_measure_font_rejects_synthetic_static_weight(self):
        class StaticFont:
            def get_variation_axes(self):
                raise OSError("not variable")

        self.service.reference_measure_fonts.clear()
        with mock.patch.object(
            matrix.ImageFont, "truetype", return_value=StaticFont(),
        ), self.assertRaisesRegex(
            matrix.MatrixTemplateError, "synthetic weight",
        ):
            self.service._reference_measure_font(
                "Ma Shan Zheng", 70, 900,
            )

    def test_reference_css_font_weight_drift_is_exposed_in_contract(self):
        index_path = (
            self.reference_skill / "assets/templates"
            / matrix.REFERENCE_PACK_ID / "index.html"
        )
        source = index_path.read_text(encoding="utf-8")
        expected = ".v09 .top1 { font-size: 80px; }"
        self.assertIn(expected, source)
        index_path.write_text(
            source.replace(
                expected,
                ".v09 .top1 { font-size: 80px; font-weight: 900; }",
                1,
            ),
            encoding="utf-8",
        )
        version = SimpleNamespace(
            returncode=0,
            stdout=matrix.REFERENCE_HYPERFRAMES_VERSION + "\n",
            stderr="",
        )
        with mock.patch.object(matrix.subprocess, "run", return_value=version):
            self.service._load_reference_catalog()
        self.assertEqual(
            900,
            self.service.reference_semantic_layouts["v09"]["top1"][
                "font_weight"
            ],
        )

    def test_reference_parent_width_rejects_v10_two_line_browser_overflow(self):
        metrics = self.service.reference_semantic_layouts["v10"]["bottom2"]
        bottom = "MMMMMMMMMMMMMM，MMMMMMMMMMMMMM"
        self.assertEqual(970, metrics["max_width_px"])
        with mock.patch.object(
            self.service, "_reference_text_width", return_value=980.4,
        ), self.assertRaisesRegex(ValueError, "无法在完整语义边界内排入"):
            self.service._pack_reference_semantic_span(
                bottom, 0, len(bottom), [14], metrics,
            )

    def test_reference_parent_padding_unit_drift_fails_closed(self):
        index_path = (
            self.reference_skill / "assets/templates"
            / matrix.REFERENCE_PACK_ID / "index.html"
        )
        source = index_path.read_text(encoding="utf-8")
        expected = ".v10 .bottom { padding-left: 68px; }"
        self.assertIn(expected, source)
        index_path.write_text(
            source.replace(
                expected, ".v10 .bottom { padding-left: 6vw; }", 1,
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            matrix.MatrixTemplateError, "padding is unsupported",
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

    def test_v01_template_rejects_font_size_drift(self):
        index_path = (
            self.reference_skill / "assets/templates"
            / matrix.REFERENCE_PACK_ID / "index.html"
        )
        source = index_path.read_text(encoding="utf-8")
        index_path.write_text(
            source.replace(
                'font: 400 70px/1.08 "MaShan";',
                'font: 400 68px/1.08 "MaShan";',
                1,
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            matrix.MatrixTemplateError,
            "v01 HyperFrames template style contract changed",
        ):
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

    def _v07_semantic_layout(self, top, bottom, top1_end):
        breaks = [i for i, ch in enumerate(top) if ch in "，。！？；,.!?;"]
        return {
            "version": matrix.REFERENCE_SEMANTIC_LAYOUT_VERSION,
            "model": "test-model",
            "source_sha256": matrix._reference_semantic_source_sha256(top, bottom),
            "top1_end": top1_end,
            "top_break_after": breaks,
            "bottom_break_after": [
                i for i, ch in enumerate(bottom) if ch in "，。！？；,.!?;"
            ],
        }

    @staticmethod
    def _v07_fake_width(value, metrics):
        text = matrix._hide_reference_edge_punctuation(value)
        if not text:
            return 0.0
        return len(text) * float(metrics["font_size_px"]) * 0.8

    def test_v07_semantic_layout_splits_five_layers(self):
        top = (
            "999元成为会员。全年免费喝茶。"
            "女性成长/喝茶/商业思维/沙龙。链接1000位深圳湾沙龙主理人"
        )
        bottom = "感兴趣留下666"
        top_breaks = [i for i, ch in enumerate(top) if ch == "。"]
        with mock.patch.object(
            self.service, "_reference_text_width",
            side_effect=self._v07_fake_width,
        ):
            payload = self.service.validate_payload({
                "top_text": top,
                "bottom_text": bottom,
                "template_id": "ref-07-fixture-07",
                "semantic_layout": self._v07_semantic_layout(
                    top, bottom, top_breaks[0]
                ),
                "bgm": False,
            })
            frozen = self.service._freeze_font_provenance("7" * 32, payload)
        reference = frozen["_reference_template"]
        self.assertEqual(4, reference["top_layer_count"])
        text = reference["text"]
        self.assertEqual(
            top,
            "".join(text[key] for key in ("top1", "top2", "top3", "bottom1")),
        )
        self.assertEqual(bottom, text["bottom2"])
        self.assertNotEqual("", text["bottom1"])
        display = reference["display_text"]
        self.assertEqual("999元成为会员", display["top1"])
        self.assertEqual("全年免费喝茶", display["top2"])
        self.assertEqual("女性成长/喝茶/商业思维/沙龙", display["top3"])
        self.assertEqual("链接1000位深圳湾沙龙主理人", display["bottom1"])
        self.assertEqual("感兴趣留下666", display["bottom2"])
        joined = "".join(text[key] for key in ("top1", "top2", "top3", "bottom1", "bottom2"))
        for protected in ("999元", "1000位", "深圳湾", "主理人", "666"):
            self.assertIn(protected, joined)

    def test_v07_semantic_layout_allows_empty_bottom1_for_short_copy(self):
        top = "深圳女性成长局，一起链接资源"
        bottom = "回复同行"
        top_breaks = [i for i, ch in enumerate(top) if ch in "，。！？；,.!?;"]
        with mock.patch.object(
            self.service, "_reference_text_width",
            side_effect=self._v07_fake_width,
        ):
            payload = self.service.validate_payload({
                "top_text": top,
                "bottom_text": bottom,
                "template_id": "ref-07-fixture-07",
                "semantic_layout": self._v07_semantic_layout(
                    top, bottom, top_breaks[0]
                ),
                "bgm": False,
            })
            frozen = self.service._freeze_font_provenance("8" * 32, payload)
        text = frozen["_reference_template"]["text"]
        self.assertEqual(
            top,
            "".join(text[key] for key in ("top1", "top2", "top3", "bottom1")),
        )
        self.assertEqual(bottom, text["bottom2"])
        self.assertEqual("", text["bottom1"])
        self.assertEqual("", frozen["_reference_template"]["display_text"]["bottom1"])

    def test_v07_semantic_layout_is_idempotent_per_request_id(self):
        top = "999元成为会员。全年免费喝茶。女性成长/喝茶/商业思维/沙龙。链接1000位深圳湾沙龙主理人"
        bottom = "感兴趣留下666"
        top_breaks = [i for i, ch in enumerate(top) if ch == "。"]
        raw = {
            "top_text": top,
            "bottom_text": bottom,
            "template_id": "ref-07-fixture-07",
            "semantic_layout": self._v07_semantic_layout(
                top, bottom, top_breaks[0]
            ),
            "bgm": False,
        }
        with mock.patch.object(
            self.service, "_reference_text_width",
            side_effect=self._v07_fake_width,
        ):
            first = self.service.submit(raw, "v07-idempotent-1")
            second = self.service.submit(raw, "v07-idempotent-1")
        self.assertEqual(first["job_id"], second["job_id"])
        stored = json.loads(self.service.store.get(first["job_id"])["payload"])
        text = stored["_reference_template"]["text"]
        self.assertEqual("链接1000位深圳湾沙龙主理人", text["bottom1"])
        self.assertEqual("感兴趣留下666", text["bottom2"])

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
        with self.assertRaisesRegex(ValueError, "必须提供 AI 语义排版"):
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
            with self.assertRaisesRegex(ValueError, "必须提供 AI 语义排版"):
                restarted.submit(raw, "legacy-reference-layout-new-after-restart")
        finally:
            restarted.shutdown()

    def test_new_reference_submission_requires_ai_semantic_layout(self):
        raw = {
            "top_text": "我在广州组了一个健康赛道创业者的圈子",
            "bottom_text": "评论区扣888",
            "template_id": "ref-04-fixture-04",
            "bgm": False,
        }
        with self.assertRaisesRegex(ValueError, "必须提供 AI 语义排版"):
            self.service.submit(raw, "reference-without-semantic-layout")
        self.assertIsNone(
            self.service.store.get_by_request_id(
                "reference-without-semantic-layout"
            )
        )

    def test_reference_material_selection_uses_dynamic_two_to_three_second_slots(self):
        payload = self.service.validate_payload({
            "top_text": "活动标题",
            "bottom_text": "报名获取资料",
            "template_id": "ref-02-fixture-02",
            "bgm": False,
        })
        payload = self.service._freeze_font_provenance("2" * 32, payload)
        payload["_reference_template"]["duration"] = 14
        captured = {}

        def selection(_method, _path, body):
            captured.update(body)
            return {
                "selection_contract_version": 2,
                "clip_contract_version": 3,
                "materials": [{
                    "scene_id": f"media_{index:02d}",
                    "sha256": format(index, "064x"),
                    "media_type": "video",
                    "record_id": f"video-{index}",
                    "clip_id": format(index + 100, "064x"),
                    "clip_start_seconds": float(index - 1) * 3,
                    "clip_duration_seconds": 2.8,
                    "clip_slot_index": index,
                    "clip_slot_count": 5,
                } for index in range(1, 6)],
            }

        with mock.patch.object(self.service, "_library_request", side_effect=selection):
            materials = self.service._select_materials(payload, "2" * 32)
        self.assertEqual(5, len(materials))
        self.assertEqual(
            ["video"] * 5,
            [scene["media_type"] for scene in captured["scenes"]],
        )
        self.assertEqual(
            [2.8] * 5,
            [scene["clip_duration_seconds"] for scene in captured["scenes"]],
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

    def test_reference_segment_timing_keeps_every_clip_between_two_and_three_seconds(self):
        starts, durations, offsets = matrix._reference_segment_timing(
            14, [94.3, 3.9, 9.897, 6.0, 4.0]
        )
        for actual, expected in zip(starts, [0.0, 2.8, 5.6, 8.4, 11.2]):
            self.assertAlmostEqual(expected, actual)
        self.assertTrue(all(
            matrix.REFERENCE_MIN_SEGMENT_SECONDS
            <= duration <= matrix.REFERENCE_MAX_SEGMENT_SECONDS
            for duration in durations
        ))
        self.assertAlmostEqual(14.0, sum(durations))
        self.assertTrue(all(
            duration <= source - matrix.REFERENCE_MEDIA_SAFETY_SECONDS + 0.001
            for duration, source in zip(
                durations, [94.3, 3.9, 9.897, 6.0, 4.0],
            )
        ))
        seeded = matrix._reference_segment_timing(
            14, [94.3, 3.9, 9.897, 6.0, 4.0], seed="stable-job",
        )
        self.assertEqual(
            seeded,
            matrix._reference_segment_timing(
                14, [94.3, 3.9, 9.897, 6.0, 4.0], seed="stable-job",
            ),
        )
        self.assertTrue(all(
            0 <= offset <= source - duration - matrix.REFERENCE_MEDIA_SAFETY_SECONDS + 0.001
            for offset, duration, source in zip(
                seeded[2], seeded[1], [94.3, 3.9, 9.897, 6.0, 4.0],
            )
        ))

        starts, durations, offsets = matrix._reference_segment_timing(
            12, [30.0, 30.0, 30.0, 30.0]
        )
        self.assertEqual([0.0, 3.0, 6.0, 9.0], starts)
        self.assertEqual([3.0, 3.0, 3.0, 3.0], durations)

        with self.assertRaisesRegex(
            matrix.MatrixTemplateError, "单素材可用时长不足"
        ):
            matrix._reference_segment_timing(
                14, [3.0, 3.0, 3.0, 3.0, 2.8]
            )

    def test_visual_count_keeps_every_8_to_15_second_clip_in_range(self):
        expected = {8: 3, 9: 3, 10: 4, 11: 4, 12: 4,
                    13: 5, 14: 5, 15: 5}

        self.assertEqual(
            expected,
            {duration: matrix._required_visuals(duration) for duration in expected},
        )
        for duration, count in expected.items():
            self.assertTrue(2 <= duration / count <= 3)

    def test_reference_timeline_replaces_existing_media_offsets_and_rejects_bad_values(self):
        html = """
<video id="videoA" data-start="0" data-duration="1" data-media-start="99"></video>
<video id="videoB" data-start="1" data-duration="1"></video>
<video id="videoC" data-start="2" data-duration="1"></video>
<audio id="bgm" data-start="0" data-duration="3"></audio>
<section id="typography" data-start="0" data-duration="3"></section>
<script>      const segment = duration / 3;
      const segmentStarts = [0, segment, segment * 2];
      const segmentDurations = [segment, segment, duration - segment * 2];</script>
"""
        rendered = matrix._rewrite_reference_timeline(
            html, 9, [0, 3, 6], [3, 3, 3], [1.25, 0, 4.5],
        )

        self.assertEqual(1, rendered.count('data-media-start="1.25"'))
        self.assertEqual(1, rendered.count('data-media-start="0"'))
        self.assertEqual(1, rendered.count('data-media-start="4.5"'))
        self.assertNotIn('data-media-start="99"', rendered)
        for offsets in ([0, 1], [0, -1, 2], [0, float("nan"), 2]):
            with self.subTest(offsets=offsets), self.assertRaisesRegex(
                matrix.MatrixTemplateError, "时间轴参数无效",
            ):
                matrix._rewrite_reference_timeline(
                    html, 9, [0, 3, 6], [3, 3, 3], offsets,
                )

    def test_reference_editing_plan_is_deterministic_varied_and_color_neutral(self):
        first = matrix._reference_editing_plan("1" * 32, "ref-01-fixture-01")
        repeated = matrix._reference_editing_plan(
            "1" * 32, "ref-01-fixture-01"
        )
        second = matrix._reference_editing_plan("2" * 32, "ref-01-fixture-01")

        self.assertEqual(first, repeated)
        self.assertNotEqual(first["seed"], second["seed"])
        self.assertNotEqual(
            (first["segments"], first["transitions"], first["bookends"]),
            (second["segments"], second["transitions"], second["bookends"]),
        )
        self.assertEqual([], first["color_effects"])
        self.assertEqual(3, len(first["segments"]))
        self.assertEqual(2, len(first["transitions"]))
        self.assertEqual(
            3, len({item["motion"] for item in first["segments"]})
        )
        self.assertEqual(
            2, len({item["name"] for item in first["transitions"]})
        )
        matrix._validate_reference_editing_plan(first)

        five = matrix._reference_editing_plan(
            "4" * 32, "ref-03-fixture-03", 5
        )
        self.assertEqual(5, len(five["segments"]))
        self.assertEqual(4, len(five["transitions"]))
        matrix._validate_reference_editing_plan(five)

        unsupported_window = json.loads(json.dumps(first))
        unsupported_window["transitions"][0].update({
            "start": 2.0,
            "duration": 0.2,
        })
        with self.assertRaisesRegex(
            matrix.MatrixTemplateError, "剪辑方案无效"
        ):
            matrix._validate_reference_editing_plan(unsupported_window)

    def test_reference_editing_script_has_no_color_effects_or_text_animation(self):
        plan = matrix._reference_editing_plan("3" * 32, "ref-03-fixture-03")
        source = """<html><head></head><body>
<video id="videoA" class="clip media-video"></video>
<video id="videoB" class="clip media-video"></video>
<video id="videoC" class="clip media-video"></video>
<section id="typography"></section><script>""" \
            + matrix.REFERENCE_BASE_TIMELINE_JS + """</script></body></html>"""
        rendered = matrix._inject_reference_editing_plan(
            source, plan,
        )

        self.assertEqual(1, rendered.count(matrix.REFERENCE_EDITING_SCRIPT_ID))
        self.assertIn('window.__timelines["main"] = timeline', rendered)
        self.assertIn('const videos = ["videoA", "videoB", "videoC"]', rendered)
        self.assertNotIn(matrix.REFERENCE_BASE_TIMELINE_JS, rendered)
        self.assertEqual(1, rendered.count("gsap.timeline({paused: true})"))
        for element_id in ("videoA", "videoB", "videoC"):
            self.assertIn(f'id="{element_id}-transition"', rendered)
            self.assertIn(f'id="{element_id}-motion"', rendered)
        self.assertNotIn('getElementById("typography")', rendered)
        normalized_rendered = rendered.lower()
        for forbidden in matrix.REFERENCE_FORBIDDEN_COLOR_EFFECTS:
            self.assertNotIn(forbidden, normalized_rendered)

        invalid = json.loads(json.dumps(plan))
        invalid["color_effects"] = ["grayscale"]
        with self.assertRaisesRegex(
            matrix.MatrixTemplateError, "剪辑方案无效"
        ):
            matrix._inject_reference_editing_plan(
                "<html><body></body></html>", invalid
            )

    def test_reference_timeline_expands_to_five_video_slots(self):
        html = """
<html data-composition-variables="[{&quot;id&quot;:&quot;videoA&quot;,&quot;type&quot;:&quot;string&quot;},{&quot;id&quot;:&quot;videoB&quot;,&quot;type&quot;:&quot;string&quot;},{&quot;id&quot;:&quot;videoC&quot;,&quot;type&quot;:&quot;string&quot;},{&quot;id&quot;:&quot;bgm&quot;,&quot;type&quot;:&quot;string&quot;}]">
<video data-hf-id="a" id="videoA" data-start="0" data-duration="1" data-var-src="videoA" src="a.mp4"></video>
<video data-hf-id="b" id="videoB" data-start="1" data-duration="1" data-var-src="videoB" src="b.mp4"></video>
<video data-hf-id="c" id="videoC" data-start="2" data-duration="1" data-var-src="videoC" src="c.mp4"></video>
<audio id="bgm" data-start="0" data-duration="3"></audio>
<section id="typography" data-start="0" data-duration="3"></section>
<script>
      const segment = duration / 3;
      const segmentStarts = [0, segment, segment * 2];
      const segmentDurations = [segment, segment, duration - segment * 2];
      const videos = [
        document.getElementById("videoA"),
        document.getElementById("videoB"),
        document.getElementById("videoC")
      ];
</script>
</html>
"""
        sources = [f"assets/input/video-{index}.mp4" for index in range(1, 6)]

        expanded = matrix._expand_reference_video_slots(html, sources)
        rendered = matrix._rewrite_reference_timeline(
            expanded, 14,
            [0, 2.8, 5.6, 8.4, 11.2], [2.8] * 5,
            [0, 1, 2, 3, 4],
        )

        self.assertIn('id="videoD"', rendered)
        self.assertIn('id="videoE"', rendered)
        self.assertIn('src="assets/input/video-4.mp4"', rendered)
        self.assertIn('src="assets/input/video-5.mp4"', rendered)
        self.assertNotRegex(rendered, r'id="video[DE]"[^>]*data-var-src')
        self.assertEqual(1, rendered.count('&quot;id&quot;:&quot;videoD&quot;'))
        self.assertEqual(1, rendered.count('&quot;id&quot;:&quot;videoE&quot;'))
        self.assertEqual(5, rendered.count('document.getElementById("video'))
        self.assertIn(
            "const segmentDurations = [2.8, 2.8, 2.8, 2.8, 2.8];",
            rendered,
        )
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
        payload["_reference_template"]["editing_plan"] = (
            matrix._reference_editing_plan(
                "3" * 32, "ref-03-fixture-03", 5
            )
        )
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
        clip_starts = [1.0, 0.5, 1.0, 1.5, 0.5]
        for index in range(1, 6):
            path = self.root / f"source-{index}.mp4"
            path.write_bytes(f"video-{index}".encode("ascii"))
            paths.append(path)
            materials.append({
                "media_type": "video", "record_id": f"v{index}",
                "clip_start_seconds": clip_starts[index - 1],
                "clip_duration_seconds": 2.8,
            })
        process = mock.Mock()
        process.returncode = 0
        process.communicate.return_value = (b"", b"")
        process.poll.return_value = 0
        with mock.patch.object(
            self.service, "_reference_video_duration",
            side_effect=[94.3, 3.9, 9.897, 6.0, 4.0],
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
        self.assertEqual(
            1, index.count(matrix.REFERENCE_CTA_SAFE_AREA_STYLE_ID)
        )
        self.assertIn(
            '#root .bottom{bottom:15%}', index
        )
        self.assertIn(
            '@font-face{font-family:"HQSmileySansOblique";', index
        )
        self.assertIn(
            '.v03 .top2{font-family:"HQSmileySansOblique"!important;'
            'font-size:62px!important}', index
        )
        self.assertIn(
            'id="videoA" class="clip media-video" data-start="0" data-duration="2.8" data-media-start="1"',
            index,
        )
        self.assertIn(
            'id="videoB" class="clip media-video" data-start="2.8" data-duration="2.8" data-media-start="0.5"',
            index,
        )
        self.assertIn(
            'id="videoC" class="clip media-video" data-start="5.6" data-duration="2.8" data-media-start="1"',
            index,
        )
        self.assertIn(
            'id="videoD" class="clip media-video" data-start="8.4" data-duration="2.8" data-media-start="1.5"',
            index,
        )
        self.assertIn(
            'id="videoE" class="clip media-video" data-start="11.2" data-duration="2.8" data-media-start="0.5"',
            index,
        )
        self.assertIn('id="bgm" data-start="0" data-duration="14"', index)
        self.assertIn('id="typography" class="clip text-layer" data-start="0" data-duration="14"', index)
        self.assertIn("const segmentStarts = [0, 2.8, 5.6, 8.4, 11.2];", index)
        self.assertIn(
            "const segmentDurations = [2.8, 2.8, 2.8, 2.8, 2.8];",
            index,
        )
        self.assertNotIn(matrix.REFERENCE_DYNAMIC_TIMING_JS, index)
        self.assertEqual(
            1, index.count(matrix.REFERENCE_EDITING_SCRIPT_ID)
        )
        self.assertEqual(
            payload["_reference_template"]["editing_plan"]["seed"],
            variables["_editing_plan"]["seed"],
        )
        self.assertEqual(5, len(variables["_editing_plan"]["segments"]))
        self.assertEqual(4, len(variables["_editing_plan"]["transitions"]))
        self.assertEqual([], variables["_editing_plan"]["color_effects"])
        self.assertEqual(1, index.count("gsap.timeline({paused: true})"))
        for element_id in matrix.REFERENCE_VIDEO_IDS:
            self.assertIn(f'id="{element_id}-transition"', index)
            self.assertIn(f'id="{element_id}-motion"', index)
        for asset_index, source in enumerate(paths, 1):
            copied = workdir / f"assets/input/video-{asset_index}.mp4"
            self.assertEqual(source.read_bytes(), copied.read_bytes())
        self.assertFalse(list(workdir.rglob("*.part")))
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

    def test_v16_uses_smiley_for_top2_and_both_bottom_layers(self):
        payload = self.service.validate_payload({
            "top_text": "深圳共享创业平台",
            "bottom_text": "评论区回复777",
            "template_id": "ref-16-fixture-16",
            "bgm": False,
        })
        payload = self.service._freeze_font_provenance("f" * 32, payload)
        fixed_fonts = payload["_reference_template"]["fixed_fonts"]
        self.assertEqual(
            {"top2": 68, "bottom1": 70, "bottom2": 70},
            {
                layer: item["font_size_px"]
                for layer, item in fixed_fonts.items()
            },
        )
        self.assertTrue(all(
            item["family"] == "Smiley Sans Oblique"
            and item["alias"] == "HQSmileySansOblique"
            and item["file"] == "SmileySans-Oblique.ttf"
            for item in fixed_fonts.values()
        ))
        self.assertEqual(
            1,
            sum(
                item["family"] == "Smiley Sans Oblique"
                and item["source"] == "private"
                for item in payload["_font_provenance"]["fonts"]
            ),
        )
        style = matrix._reference_private_font_style("v16", fixed_fonts)
        self.assertEqual(1, style.count('@font-face{font-family:"HQSmileySansOblique";'))
        for layer, size in (("top2", 68), ("bottom1", 70), ("bottom2", 70)):
            self.assertIn(
                f'.v16 .{layer}'
                '{font-family:"HQSmileySansOblique"!important;'
                f'font-size:{size}px!important}}',
                style,
            )
        payload["_reference_template"]["duration"] = 8
        payload["_reference_template"]["editing_plan"] = matrix._reference_editing_plan(
            "f" * 32, payload["template_id"], matrix._required_visuals(8)
        )
        materials = []
        paths = []
        for index in range(1, 4):
            path = self.root / f"v16-source-{index}.mp4"
            path.write_bytes(f"video-{index}".encode("ascii"))
            paths.append(path)
            materials.append({
                "media_type": "video", "record_id": f"v16-{index}",
            })
        process = mock.Mock(returncode=0)
        process.communicate.return_value = (b"", b"")
        process.poll.return_value = 0
        with mock.patch.object(
            self.service, "_reference_video_duration", return_value=30.0,
        ), mock.patch.object(matrix.subprocess, "Popen", return_value=process):
            variables = self.service._render_reference(
                payload, "f" * 32, materials, paths,
            )
        self.assertEqual("v16", variables["variant"])
        workdir = self.service.data_root / ("f" * 32) / "hyperframes"
        fonts = [
            path.name for path in (workdir / "assets/fonts").iterdir()
            if path.name == "SmileySans-Oblique.ttf"
        ]
        self.assertEqual(["SmileySans-Oblique.ttf"], fonts)
        rendered_index = (workdir / "index.html").read_text(encoding="utf-8")
        self.assertEqual(
            1,
            rendered_index.count(
                '@font-face{font-family:"HQSmileySansOblique";'
            ),
        )
        for layer in ("top2", "bottom1", "bottom2"):
            self.assertIn(f'.v16 .{layer}', rendered_index)

    def test_reference_render_uses_selected_bgm_as_authored_audio_source(self):
        job_id = "6" * 32
        payload, materials, paths = self._reference_bgm_inputs(job_id)
        bgm_path = paths[-1]
        process = mock.Mock(returncode=0)
        process.communicate.return_value = (b"", b"")
        process.poll.return_value = 0
        prepared = {}

        def prepare_bgm(source, destination, duration, *, deadline_at):
            prepared.update({
                "source": source, "destination": destination,
                "duration": duration, "deadline_at": deadline_at,
            })
            destination.write_bytes(source.read_bytes())

        with mock.patch.object(
            self.service, "_reference_video_duration", return_value=30.0,
        ), mock.patch.object(
            self.service, "_prepare_reference_bgm", side_effect=prepare_bgm,
        ), mock.patch.object(matrix.subprocess, "Popen", return_value=process):
            variables = self.service._render_reference(
                payload, job_id, materials, paths
            )
        workdir = self.service.data_root / job_id / "hyperframes"
        index = (workdir / "index.html").read_text(encoding="utf-8")
        self.assertEqual(14.0, prepared["duration"])
        self.assertGreater(prepared["deadline_at"], time.time())
        self.assertEqual(bgm_path, prepared["source"])
        self.assertEqual(workdir / "assets/input/bgm.m4a", prepared["destination"])
        self.assertEqual("assets/input/bgm.m4a", variables["bgm"])
        self.assertRegex(
            index,
            r'<audio\b(?=[^>]*\bid="bgm")(?=[^>]*\bdata-var-src="bgm")'
            r'[^>]*\ssrc="assets/input/bgm\.m4a"[^>]*>',
        )
        self.assertNotRegex(
            index,
            r'<audio\b(?=[^>]*\bid="bgm")[^>]*'
            r'\ssrc="assets/bgm/silence\.m4a"[^>]*>',
        )

    def test_reference_bgm_is_looped_or_trimmed_to_video_duration(self):
        source = self.root / "source-bgm.mp3"
        source.write_bytes(b"source-bgm")
        destination = self.root / "prepared/bgm.m4a"
        captured = {}

        def popen(command, **kwargs):
            captured["command"] = command
            captured["kwargs"] = kwargs
            Path(command[-1]).write_bytes(b"prepared-bgm")
            process = mock.Mock(returncode=0)
            process.communicate.return_value = (b"", b"")
            process.poll.return_value = 0
            captured["process"] = process
            return process

        with mock.patch.object(matrix.subprocess, "Popen", side_effect=popen):
            self.service._prepare_reference_bgm(
                source, destination, 14.0, deadline_at=time.time() + 300
            )

        self.assertEqual(b"prepared-bgm", destination.read_bytes())
        command = captured["command"]
        self.assertEqual(
            ["-stream_loop", "-1"],
            command[command.index("-stream_loop"):command.index("-stream_loop") + 2],
        )
        self.assertEqual("14", command[command.index("-t") + 1])
        self.assertEqual("aac", command[command.index("-c:a") + 1])
        self.assertEqual("48000", command[command.index("-ar") + 1])
        timeout = captured["process"].communicate.call_args.kwargs["timeout"]
        self.assertGreater(timeout, 119)
        self.assertLessEqual(
            timeout, matrix.REFERENCE_BGM_PREPARE_TIMEOUT_SECONDS
        )
        self.assertEqual(set(), self.service.active_processes)

    @unittest.skipUnless(
        shutil.which("ffmpeg") and shutil.which("ffprobe"),
        "FFmpeg tools are unavailable",
    )
    def test_reference_bgm_preparation_has_exact_media_duration(self):
        source = self.root / "short-bgm.wav"
        destination = self.root / "exact-bgm.m4a"
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1.2",
            "-c:a", "pcm_s16le", str(source),
        ], check=True)

        self.service._prepare_reference_bgm(
            source, destination, 3.0, deadline_at=time.time() + 30
        )

        probe = subprocess.run([
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(destination),
        ], check=True, capture_output=True, text=True)
        self.assertAlmostEqual(3.0, float(probe.stdout.strip()), places=2)

    def test_five_reference_bgm_jobs_share_two_hyperframes_slots(self):
        cases = [
            (format(index + 10, "032x"), *self._reference_bgm_inputs(
                format(index + 10, "032x")
            ))
            for index in range(5)
        ]
        barrier = threading.Barrier(6)
        lock = threading.Lock()
        active = 0
        peak = 0
        completed = []
        errors = []

        def prepare(source, destination, duration, *, deadline_at):
            nonlocal active, peak
            self.assertTrue(source.is_file())
            self.assertEqual(14, duration)
            self.assertGreater(deadline_at, time.time())
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.05)
            destination.write_bytes(b"prepared-bgm")
            with lock:
                active -= 1

        def popen(_command, **_kwargs):
            process = mock.Mock(returncode=0)
            process.communicate.return_value = (b"", b"")
            process.poll.return_value = 0
            return process

        def render(case):
            job_id, payload, materials, paths = case
            barrier.wait()
            try:
                self.service._render_reference(
                    payload, job_id, materials, paths,
                    deadline_at=time.time() + 5,
                )
                with lock:
                    completed.append(job_id)
            except Exception as exc:
                with lock:
                    errors.append(exc)

        with mock.patch.object(
            self.service, "_reference_video_duration", return_value=30.0,
        ), mock.patch.object(
            self.service, "_prepare_reference_bgm", side_effect=prepare,
        ), mock.patch.object(matrix.subprocess, "Popen", side_effect=popen):
            threads = [threading.Thread(target=render, args=(case,)) for case in cases]
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join(timeout=3)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertFalse(errors)
        self.assertEqual(5, len(completed))
        self.assertEqual(2, peak)
        self.assertEqual(set(), self.service.active_processes)

    def test_reference_bgm_deadline_timeout_does_not_start_render(self):
        job_id = "d" * 32
        payload, materials, paths = self._reference_bgm_inputs(job_id)
        process = mock.Mock(returncode=None)
        process.communicate.side_effect = subprocess.TimeoutExpired(
            cmd="ffmpeg", timeout=1
        )
        process.poll.return_value = None

        with mock.patch.object(
            self.service, "_reference_video_duration", return_value=30.0,
        ), mock.patch.object(
            matrix.subprocess, "Popen", return_value=process,
        ) as popen, mock.patch.object(
            self.service, "_terminate"
        ) as terminate, self.assertRaisesRegex(
            matrix.MatrixTemplateError, "超过总时限"
        ):
            self.service._render_reference(
                payload, job_id, materials, paths,
                deadline_at=time.time() + 1,
            )

        self.assertEqual(1, popen.call_count)
        terminate.assert_called_once_with(process)
        timeout = process.communicate.call_args.kwargs["timeout"]
        self.assertGreater(timeout, 0)
        self.assertLessEqual(timeout, 1)
        workdir = self.service.data_root / job_id / "hyperframes"
        self.assertFalse((workdir / "assets/input/bgm.m4a").exists())
        self.assertFalse((workdir / "assets/input/.bgm.m4a.part.m4a").exists())
        self.assertEqual(set(), self.service.active_processes)
        self.assertTrue(self.service.hyperframes_slots.acquire(timeout=0.1))
        self.service.hyperframes_slots.release()

    def test_shutdown_terminates_real_tracked_process_without_residual(self):
        errors = []

        def run():
            try:
                self.service._run_tracked_process(
                    [matrix.sys.executable, "-c", "import time; time.sleep(30)"],
                    timeout_seconds=60,
                    timeout_error="slow process timeout",
                )
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=run)
        thread.start()
        process = None
        deadline = time.time() + 3
        while time.time() < deadline:
            with self.service.process_lock:
                process = self.service.active_process
            if process is not None:
                break
            time.sleep(0.02)
        self.assertIsNotNone(process)

        self.service.shutdown()
        thread.join(timeout=3)

        self.assertFalse(thread.is_alive())
        self.assertTrue(errors)
        self.assertIn("服务正在停止", str(errors[0]))
        self.assertIsNotNone(process.poll())
        self.assertEqual(set(), self.service.active_processes)

    def test_v02_semantic_layout_uses_frozen_source_indices_and_font_widths(self):
        top = "团队8个人，每天产出100条短视频，覆盖全部短视频平台，"
        bottom = "有想进军健康赛道的，勾兑勾兑"
        top_commas = [index for index, char in enumerate(top) if char == "，"]
        bottom_comma = bottom.index("，")
        semantic_layout = {
            "version": 1,
            "model": "gpt-4.1-mini",
            "source_sha256": matrix._reference_semantic_source_sha256(top, bottom),
            "top1_end": top_commas[1],
            "top_break_after": top_commas[:2],
            "bottom_break_after": [bottom_comma],
        }
        widths = {
            "团队8个人，每天产出100条短视频": 1362.6,
            "团队8个人": 492.3,
            "每天产出100条短视频": 896.5,
            "覆盖全部短视频平台": 519.6,
            "有想进军健康赛道的，勾兑勾兑": 1120.0,
            "有想进军健康赛道的": 807.0,
            "勾兑勾兑": 334.3,
        }

        def measured(value, _metrics):
            return widths[matrix._hide_reference_edge_punctuation(value)]

        with mock.patch.object(
            self.service, "_reference_text_width", side_effect=measured,
        ):
            payload = self.service.validate_payload({
                "top_text": top,
                "bottom_text": bottom,
                "template_id": "ref-02-fixture-02",
                "bgm": False,
                "semantic_layout": semantic_layout,
            })
            frozen = self.service._freeze_font_provenance("6" * 32, payload)
        reference = frozen["_reference_template"]
        self.assertEqual(
            "团队8个人\n每天产出100条短视频",
            reference["display_text"]["top1"],
        )
        self.assertEqual(
            "覆盖全部短视频平台", reference["display_text"]["top2"],
        )
        self.assertEqual(
            "有想进军健康赛道的\n勾兑勾兑",
            reference["display_text"]["bottom2"],
        )
        self.assertEqual(
            top,
            reference["text"]["top1"] + reference["text"]["top2"],
        )
        self.assertEqual(bottom, reference["text"]["bottom2"])

    def test_v02_semantic_layout_rejects_break_inside_number_phrase(self):
        top = "团队8个人，每天产出100条短视频"
        bottom = "评论区扣888"
        with self.assertRaisesRegex(ValueError, "top1 语义边界无效"):
            self.service.validate_payload({
                "top_text": top,
                "bottom_text": bottom,
                "template_id": "ref-02-fixture-02",
                "bgm": False,
                "semantic_layout": {
                    "version": 1,
                    "model": "gpt-4.1-mini",
                    "source_sha256": matrix._reference_semantic_source_sha256(top, bottom),
                    "top1_end": 2,
                    "top_break_after": [2, top.index("，")],
                    "bottom_break_after": [],
                },
            })

    def test_v05_semantic_layout_preserves_chinese_verb_and_uses_top3(self):
        top = "团队8个人，每天产出100条短视频，覆盖全部短视频平台，"
        bottom = "有想进军健康赛道的，勾兑勾兑"
        commas = [index for index, char in enumerate(top) if char == "，"]
        verb_end = top.index("100") - 1
        layout = {
            "version": 1,
            "model": "gpt-4.1-mini",
            "source_sha256": matrix._reference_semantic_source_sha256(top, bottom),
            "top1_end": commas[0],
            "top_break_after": [commas[0], verb_end, commas[1]],
            "bottom_break_after": [bottom.index("，")],
        }

        def measured(value, metrics):
            display = matrix._hide_reference_edge_punctuation(value)
            size = int(metrics["font_size_px"])
            if size == 102:
                return 500.0
            if size == 104:
                return {
                    "每天产出100条短视频，覆盖全部短视频平台": 1800.0,
                    "100条短视频，覆盖全部短视频平台": 1300.0,
                    "每天产出100条短视频": 1200.0,
                    "每天产出": 430.0,
                    "100条短视频": 560.0,
                }[display]
            if size == 68:
                return {
                    "100条短视频，覆盖全部短视频平台": 1100.0,
                    "100条短视频": 500.0,
                    "覆盖全部短视频平台": 450.0,
                }[display]
            if size == 70:
                return {
                    "有想进军健康赛道的，勾兑勾兑": 1000.0,
                    "有想进军健康赛道的": 700.0,
                    "勾兑勾兑": 300.0,
                }[display]
            raise AssertionError((display, metrics))

        with mock.patch.object(
            self.service, "_reference_text_width", side_effect=measured,
        ):
            payload = self.service.validate_payload({
                "top_text": top,
                "bottom_text": bottom,
                "template_id": "ref-05-fixture-05",
                "bgm": False,
                "semantic_layout": layout,
            })
            frozen = self.service._freeze_font_provenance("a" * 32, payload)
        reference = frozen["_reference_template"]
        self.assertEqual(
            top,
            reference["text"]["top1"]
            + reference["text"]["top2"]
            + reference["text"]["top3"],
        )
        self.assertEqual(bottom, reference["text"]["bottom2"])
        self.assertNotIn("产\n出", "\n".join(reference["display_text"].values()))
        self.assertIn("每天产出", reference["display_text"]["top2"])
        self.assertEqual(
            "覆盖全部短视频平台", reference["display_text"]["top3"]
        )
        self.assertEqual(
            "有想进军健康赛道的\n勾兑勾兑",
            reference["display_text"]["bottom2"],
        )

    def test_all_reference_variants_accept_semantic_layout_without_rewriting(self):
        top = "开场标题，完整说明，补充信息。"
        bottom = "评论区回复111"
        commas = [index for index, char in enumerate(top) if char == "，"]
        layout = {
            "version": 1,
            "model": "gpt-4.1-mini",
            "source_sha256": matrix._reference_semantic_source_sha256(top, bottom),
            "top1_end": commas[0],
            "top_break_after": commas,
            "bottom_break_after": [],
        }
        with mock.patch.object(
            self.service, "_reference_text_width",
            side_effect=lambda value, _metrics: len(value) * 30,
        ):
            for index, item in enumerate(
                self.service.reference_templates.values(), 1,
            ):
                with self.subTest(variant=item["variant"]):
                    payload = self.service.validate_payload({
                        "top_text": top,
                        "bottom_text": bottom,
                        "template_id": item["id"],
                        "bgm": False,
                        "semantic_layout": layout,
                    })
                    frozen = self.service._freeze_font_provenance(
                        f"{index:032x}", payload,
                    )
                    reference = frozen["_reference_template"]
                    self.assertEqual(
                        top,
                        reference["text"]["top1"]
                        + reference["text"]["top2"]
                        + reference["text"]["top3"],
                    )
                    self.assertEqual(bottom, reference["text"]["bottom2"])

    def test_semantic_number_tokens_match_all_supported_forms(self):
        for value, phrase in (
            ("团队8个人", "8个人"),
            ("团队8 个人", "8 个人"),
            ("产出100条短视频", "100条"),
            ("产出100 条短视频", "100 条"),
            ("团队十二个人", "十二个人"),
            ("团队一百个人", "一百个人"),
            ("覆盖3.5万人", "3.5万人"),
            ("产出1,000条视频", "1,000条"),
        ):
            with self.subTest(value=value):
                start = value.index(phrase)
                end = start + len(phrase)
                breaks = matrix._normalize_reference_breaks(
                    list(range(len(value) - 1)), value, "顶部",
                )
                if start:
                    self.assertIn(start - 1, breaks)
                for protected in range(start, end - 1):
                    self.assertNotIn(protected, breaks)

    def test_semantic_numeric_lists_keep_comma_boundaries(self):
        for value in (
            "2025，2026",
            "1，2，3个方案",
        ):
            with self.subTest(value=value):
                commas = [
                    index for index, char in enumerate(value) if char == "，"
                ]
                breaks = matrix._normalize_reference_breaks(
                    list(range(len(value) - 1)), value, "顶部",
                )
                self.assertTrue(commas)
                self.assertTrue(all(index in breaks for index in commas))

    def test_semantic_layout_preserves_number_phrases_in_final_lines(self):
        top = "团队8个人和8 个人，十二个人和一百个人，覆盖3.5万人"
        bottom = "产出100条和100 条，领取1,000条案例"
        layout = {
            "version": 1,
            "model": "gpt-4.1-mini",
            "source_sha256": matrix._reference_semantic_source_sha256(top, bottom),
            "top1_end": top.index("，"),
            "top_break_after": list(range(len(top) - 1)),
            "bottom_break_after": list(range(len(bottom) - 1)),
        }
        with mock.patch.object(
            self.service, "_reference_text_width",
            side_effect=lambda value, _metrics: len(value) * 20,
        ):
            payload = self.service.validate_payload({
                "top_text": top,
                "bottom_text": bottom,
                "template_id": "ref-02-fixture-02",
                "bgm": False,
                "semantic_layout": layout,
            })
            frozen = self.service._freeze_font_provenance("4" * 32, payload)
        reference = frozen["_reference_template"]
        self.assertEqual(top, reference["text"]["top1"] + reference["text"]["top2"])
        self.assertEqual(bottom, reference["text"]["bottom2"])
        display = "\n".join(reference["display_text"].values())
        for phrase in (
            "8个人", "8 个人", "十二个人", "一百个人",
            "3.5万人", "100条", "100 条", "1,000条",
        ):
            self.assertIn(phrase, display)
        for forbidden in (
            "8\n个人", "8 \n个人",
            "100\n条", "100 \n条",
            "十\n二个人", "一\n百个人",
            "3.\n5万人", "1,\n000条",
        ):
            self.assertNotIn(forbidden, display)

    def test_semantic_year_list_reaches_final_layout_with_comma_breaks(self):
        top = "2025，2026，2027，2028年连续增长"
        bottom = "评论区扣111"
        commas = [index for index, char in enumerate(top) if char == "，"]
        layout = {
            "version": 1,
            "model": "gpt-4.1-mini",
            "source_sha256": matrix._reference_semantic_source_sha256(top, bottom),
            "top1_end": commas[1],
            "top_break_after": commas,
            "bottom_break_after": [],
        }
        with mock.patch.object(
            self.service, "_reference_text_width",
            side_effect=lambda value, _metrics: len(value) * 80,
        ):
            payload = self.service.validate_payload({
                "top_text": top,
                "bottom_text": bottom,
                "template_id": "ref-02-fixture-02",
                "bgm": False,
                "semantic_layout": layout,
            })
            frozen = self.service._freeze_font_provenance("5" * 32, payload)
        reference = frozen["_reference_template"]
        self.assertEqual(top, reference["text"]["top1"] + reference["text"]["top2"])
        self.assertEqual(bottom, reference["text"]["bottom2"])
        self.assertEqual("2025，2026", reference["display_text"]["top1"])
        self.assertIn("\n", reference["display_text"]["top2"])
        self.assertIn("2027", reference["display_text"]["top2"])
        self.assertIn("2028年连续增长", reference["display_text"]["top2"])

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
        for index in range(1, 6):
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

    def test_legacy_reference_job_without_new_metadata_keeps_original_style_and_timeline(self):
        payload = self.service.validate_payload({
            "top_text": "郑州AI创业活动",
            "bottom_text": "评论区回复关键词",
            "template_id": "ref-03-fixture-03",
            "bgm": False,
        })
        payload = self.service._freeze_font_provenance("7" * 32, payload)
        payload["_reference_template"].pop("fixed_fonts")
        payload["_reference_template"].pop("editing_plan")
        payload["_reference_template"]["duration"] = 8
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
            variables = self.service._render_reference(
                payload, "7" * 32, materials, paths
            )
        workdir = self.service.data_root / ("7" * 32) / "hyperframes"
        index = (workdir / "index.html").read_text(encoding="utf-8")
        self.assertNotIn(matrix.REFERENCE_PRIVATE_FONT_STYLE_ID, index)
        self.assertNotIn(matrix.REFERENCE_EDITING_SCRIPT_ID, index)
        self.assertNotIn(matrix.REFERENCE_EDITING_STYLE_ID, index)
        self.assertIn(matrix.REFERENCE_BASE_TIMELINE_JS, index)
        self.assertNotIn("-transition\"", index)
        self.assertNotIn("-motion\"", index)
        self.assertNotIn("_editing_plan", variables)
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

        def download(_item, target, job_id=""):
            path = target / f"{next(counter)}.mp4"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"video")
            return path

        captured_deadline = {}

        def render_reference(frozen, job_id, _materials, _paths, *, deadline_at):
            captured_deadline["value"] = deadline_at
            captured_deadline["plan"] = frozen["_reference_template"]["editing_plan"]
            output = self.service.data_root / job_id / "output/final.mp4"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"video")
            return {
                **frozen["_reference_template"]["text"],
                "duration": frozen["_reference_template"]["duration"],
                "_editing_plan": frozen["_reference_template"]["editing_plan"],
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
        self.assertEqual(
            captured_deadline["plan"],
            result["editing_plan"],
        )
        self.assertEqual([], result["editing_plan"]["color_effects"])
        self.assertTrue(
            (self.service.data_root / job["job_id"] / "output/published.mp4").is_file()
        )
        row = self.service.store.get(job["job_id"])
        self.assertEqual(
            row["created_at"] + self.service.hyperframes_total_timeout_seconds,
            captured_deadline["value"],
        )


class NineGridTemplateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.skill = self.root / "skill"
        HyperFramesReferenceTemplateTests._write_skill_fixture(
            self.skill, reference=False,
        )
        self.nine_grid = self.root / "nine-grid"
        self._write_nine_grid_fixture(self.nine_grid)
        self.cli = self.root / "hyperframes-0.8.33"
        self.cli.write_bytes(b"cli")
        self.motion_v2_cli = self.root / "hyperframes-0.8.34"
        self.motion_v2_cli.write_bytes(b"cli")
        self.browser = self.root / "chrome"
        self.browser.write_bytes(b"browser")
        self.bgm_hash_patch = mock.patch.object(
            matrix, "NINE_GRID_BOUND_BGM_SHA256", self.bgm_hash,
        )
        self.bgm_hash_patch.start()
        def version(command, **_kwargs):
            value = (
                "0.8.34" if str(command[0]) == str(self.motion_v2_cli)
                else "0.8.33"
            )
            return SimpleNamespace(
                returncode=0, stdout=value + "\n", stderr="",
            )

        with mock.patch.object(matrix.subprocess, "run", side_effect=version):
            self.service = matrix.MatrixTemplateService(
                data_root=self.root / "data",
                skill_root=self.skill,
                nine_grid_root=self.nine_grid,
                nine_grid_hyperframes_cli=self.cli,
                hyperframes_browser=self.browser,
                library_url="http://127.0.0.1:8111",
                library_token="library-token",
                pexels_api_key="test-pexels-key",
                start_worker=False,
            )

    def tearDown(self):
        self.service.shutdown()
        self.bgm_hash_patch.stop()
        self.temp.cleanup()

    def _write_nine_grid_fixture(self, root: Path) -> None:
        (root / "assets/audio").mkdir(parents=True)
        (root / "assets/fonts").mkdir(parents=True)
        (root / "assets/vendor").mkdir(parents=True)
        audio = root / "assets/audio/reference-bgm.m4a"
        audio.write_bytes(b"bound-bgm")
        self.bgm_hash = hashlib.sha256(audio.read_bytes()).hexdigest()
        for filename in (
            "NotoSerifSC-Variable.ttf", "NotoSansSC-Variable.ttf",
        ):
            (root / "assets/fonts" / filename).write_bytes(
                filename.encode("ascii")
            )
        (root / "assets/vendor/gsap.min.js").write_text(
            "window.gsap={};", encoding="utf-8",
        )
        variables = [
            {"id": "top_text", "type": "string", "default": "顶部标题"},
            {"id": "bottom_text", "type": "string", "default": "底部行动"},
        ] + [
            {"id": f"grid{index}", "type": "string",
             "default": f"assets/grid/{index:02}.mp4"}
            for index in range(1, 10)
        ] + [
            {"id": f"main{index}", "type": "string",
             "default": f"assets/main/{index:02}.mp4"}
            for index in range(1, 4)
        ]
        schema = html.escape(json.dumps(variables), quote=True)
        videos = "\n".join(
            f'<video id="grid-video{index}" data-var-src="grid{index}" '
            f'src="grid-{index}.mp4"></video>'
            for index in range(1, 10)
        ) + "\n" + "\n".join(
            f'<video id="main-video{index}" data-var-src="main{index}" '
            f'data-media-start="0" src="main-{index}.mp4"></video>'
            for index in range(1, 4)
        )
        (root / "index.html").write_text(
            f'<html data-composition-variables="{schema}"><head></head><body>'
            '<div id="root" data-composition-id="nine-grid-reveal">'
            '<span id="top-text" data-var-text="top_text">顶部标题</span>'
            '<div id="tagline" data-var-text="bottom_text">底部行动</div>'
            f'{videos}<audio id="bgm" data-volume="1" '
            'src="assets/audio/reference-bgm.m4a"></audio></div>'
            '</body></html>',
            encoding="utf-8",
        )
        for filename in ("hyperframes.json", "index.motion.json"):
            (root / filename).write_text("{}\n", encoding="utf-8")
        (root / "template.json").write_text(json.dumps({
            "id": matrix.NINE_GRID_TEMPLATE_ID,
            "name": "九宫格开场·全屏展示",
            "version": matrix.NINE_GRID_TEMPLATE_VERSION,
            "renderer": "hyperframes@0.8.33",
            "canvas": [1080, 1920], "fps": 30, "duration": 12,
            "text_fields": ["top_text", "bottom_text"],
            "text_limits": {"top_text": 60, "bottom_text": 80},
            "text_layout": {
                "mode": "semantic-then-width",
                "semantic_layout_required": True,
                "top_max_lines": 4, "bottom_max_lines": 4,
                "hide_edge_punctuation": True, "truncate": False,
            },
            "bgm": {
                "mode": "bound", "path": "assets/audio/reference-bgm.m4a",
                "sha256": self.bgm_hash, "duration": 12,
                "start": 0, "volume": 1,
            },
        }, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def semantic(top: str, bottom: str) -> dict:
        top_breaks = [
            index for index, char in enumerate(top[:-1])
            if char in "，。！？；：、,.!?;:|｜ "
        ]
        bottom_breaks = [
            index for index, char in enumerate(bottom[:-1])
            if char in "，。！？；：、,.!?;:|｜ "
        ]
        return {
            "version": 1,
            "model": "gpt-4.1-mini",
            "source_sha256": matrix._reference_semantic_source_sha256(
                top, bottom,
            ),
            "top1_end": top_breaks[0] if top_breaks else len(top) - 1,
            "top_break_after": top_breaks,
            "bottom_break_after": bottom_breaks,
        }

    @staticmethod
    def text_width(value: str, _role: str, size: int) -> float:
        return len(matrix._hide_reference_edge_punctuation(value)) * size

    def test_catalog_and_payload_use_shared_copy_contract(self):
        self.assertEqual(3, len(self.service.catalog))
        template = self.service.catalog[-1]
        self.assertEqual(matrix.NINE_GRID_TEMPLATE_ID, template["id"])
        self.assertEqual("fixed_12", template["duration_mode"])
        self.assertEqual(9, template["required_visuals"])
        self.assertEqual("bound", template["bgm_mode"])
        self.assertTrue(template["bgm_optional"])
        top = "团队8个人，每天产出100条短视频"
        bottom = "想了解完整方法，评论区扣888"
        with mock.patch.object(
            self.service, "_nine_grid_text_width", side_effect=self.text_width,
        ):
            payload = self.service.validate_payload({
                "top_text": top,
                "bottom_text": bottom,
                "template_id": matrix.NINE_GRID_TEMPLATE_ID,
                "semantic_layout": self.semantic(top, bottom),
                "bgm": True,
            }, require_reference_semantic_layout=True)
            frozen = self.service._freeze_font_provenance(
                "a" * 32, payload,
            )
        self.assertEqual(12.0, payload["duration"])
        self.assertEqual(9, self.service.required_visuals(payload))
        self.assertEqual(
            top, frozen["_nine_grid_template"]["text"]["source"]["top_text"],
        )
        self.assertEqual(
            "template-locked",
            frozen["_font_provenance"]["selection"]["variant"],
        )
        self.assertTrue(frozen["_nine_grid_template"]["bgm_enabled"])

    def test_bgm_false_is_frozen_and_rewrites_bound_track_to_silence(self):
        top = "九宫格标题"
        bottom = "评论区获取资料"
        with mock.patch.object(
            self.service, "_nine_grid_text_width", side_effect=self.text_width,
        ):
            payload = self.service.validate_payload({
                "top_text": top,
                "bottom_text": bottom,
                "template_id": matrix.NINE_GRID_TEMPLATE_ID,
                "semantic_layout": self.semantic(top, bottom),
                "bgm": False,
            }, require_reference_semantic_layout=True)
            frozen = self.service._freeze_font_provenance(
                "c" * 32, payload,
            )
        source = (
            '<audio id="bgm" data-volume="1" '
            'src="assets/audio/reference-bgm.m4a"></audio>'
        )

        rewritten = self.service._rewrite_nine_grid_bgm(source, False)

        self.assertFalse(payload["bgm"])
        self.assertFalse(frozen["_nine_grid_template"]["bgm_enabled"])
        self.assertIn('data-volume="0"', rewritten)
        self.assertNotIn('data-volume="1"', rewritten)

    def test_new_nine_grid_job_requires_ai_semantic_layout(self):
        with self.assertRaisesRegex(ValueError, "必须提供 AI 语义排版"):
            self.service.validate_payload({
                "top_text": "九宫格标题",
                "bottom_text": "评论区获取资料",
                "template_id": matrix.NINE_GRID_TEMPLATE_ID,
            }, require_reference_semantic_layout=True)

    def test_material_scenes_request_nine_unique_three_second_videos(self):
        payload = {
            "top_text": "九宫格标题", "bottom_text": "评论区获取资料",
            "template_id": matrix.NINE_GRID_TEMPLATE_ID,
            "duration": 12.0, "bgm": True,
        }
        scenes, count, hyperframes = self.service._material_scenes(payload)

        self.assertEqual(9, count)
        self.assertTrue(hyperframes)
        self.assertEqual(9, len(scenes))
        self.assertEqual({"video"}, {item["media_type"] for item in scenes})
        self.assertEqual(
            {3.0}, {item["clip_duration_seconds"] for item in scenes},
        )

    def test_nine_grid_splits_bookends_library_and_middle_pexels(self):
        payload = {
            "top_text": "九宫格标题", "bottom_text": "评论区获取资料",
            "template_id": matrix.NINE_GRID_TEMPLATE_ID,
            "duration": 12.0, "bgm": True,
            "_material_selection_contract_version": 2,
        }

        def material(index):
            return {
                "scene_id": f"media_{index:02d}",
                "record_id": f"record-{index}",
                "sha256": format(index, "064x"),
                "media_type": "video", "match_level": "random",
                "clip_id": format(index + 100, "064x"),
                "clip_start_seconds": float(index),
                "clip_duration_seconds": 3.0,
                "clip_slot_index": 1, "clip_slot_count": 1,
            }

        library_materials = [material(1), material(9)]
        pexels_materials = [material(index) for index in range(2, 9)]
        response = {
            "materials": library_materials,
            "selection_contract_version": 2,
            "clip_contract_version": 3,
        }
        with mock.patch.object(
            self.service, "_library_request", return_value=response,
        ) as library, mock.patch.object(
            self.service, "_select_pexels_materials",
            return_value=pexels_materials,
        ) as pexels:
            selected = self.service._select_materials_once(
                payload, "d" * 32,
            )

        self.assertEqual(
            [f"media_{index:02d}" for index in range(1, 10)],
            [item["scene_id"] for item in selected],
        )
        self.assertEqual(2, len(library.call_args.args[2]["scenes"]))
        self.assertEqual(7, len(pexels.call_args.args[0]))

    def test_prepare_clip_freezes_selected_window_and_adds_hidden_tail(self):
        source = self.root / "source.mp4"
        source.write_bytes(b"source")
        destination = self.root / "prepared.mp4"
        captured = {}

        def run(command, **_kwargs):
            captured["command"] = command
            Path(command[-1]).write_bytes(b"prepared" * 256)
            return 0, b"", b""

        with mock.patch.object(
            self.service, "_run_tracked_process", side_effect=run,
        ), mock.patch.object(
            self.service, "_reference_video_duration", return_value=3.233,
        ):
            self.service._prepare_nine_grid_clip(
                source, destination, 12.5, deadline_at=time.time() + 30,
            )

        command = captured["command"]
        self.assertEqual("12.5", command[command.index("-ss") + 1])
        self.assertEqual("3.233333", command[command.index("-t") + 1])
        self.assertIn(
            "trim=duration=3.000000", command[command.index("-vf") + 1]
        )
        self.assertIn(
            "tpad=stop_mode=clone:stop_duration=0.233333",
            command[command.index("-vf") + 1],
        )
        self.assertTrue(destination.is_file())

    def test_render_uses_nine_clips_bound_bgm_and_pinned_cli(self):
        top = "团队8个人，每天产出100条短视频"
        bottom = "想了解完整方法，评论区扣888"
        with mock.patch.object(
            self.service, "_nine_grid_text_width", side_effect=self.text_width,
        ):
            payload = self.service.validate_payload({
                "top_text": top,
                "bottom_text": bottom,
                "template_id": matrix.NINE_GRID_TEMPLATE_ID,
                "semantic_layout": self.semantic(top, bottom),
                "bgm": False,
            }, require_reference_semantic_layout=True)
            payload = self.service._freeze_font_provenance("b" * 32, payload)
        materials = [{
            "scene_id": f"media_{index:02d}",
            "record_id": f"record-{index}",
            "sha256": format(index, "064x"),
            "media_type": "video",
            "match_level": "random",
            "clip_id": format(index + 100, "064x"),
            "clip_start_seconds": float(index),
            "clip_duration_seconds": 3.0,
            "clip_slot_index": 1,
            "clip_slot_count": 1,
        } for index in range(1, 10)]
        paths = []
        for index in range(1, 10):
            path = self.root / f"source-{index}.mp4"
            path.write_bytes(b"video")
            paths.append(path)
        prepared = []
        captured = {}

        def prepare(source, destination, start, **_kwargs):
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"prepared" * 256)
            prepared.append((source, destination, start))

        class Process:
            returncode = 0

            def communicate(process_self, timeout=None):
                output = Path(
                    captured["command"][captured["command"].index("--output") + 1]
                )
                output.write_bytes(b"rendered" * 256)
                return b"", b""

        def popen(command, **_kwargs):
            captured["command"] = command
            return Process()

        with mock.patch.object(
            self.service, "_prepare_nine_grid_clip", side_effect=prepare,
        ), mock.patch.object(
            self.service, "_validate_reference_visual_coverage",
        ), mock.patch.object(matrix.subprocess, "Popen", side_effect=popen):
            variables = self.service._render_nine_grid(
                payload, "b" * 32, materials, paths,
                deadline_at=time.time() + 60,
            )

        self.assertEqual(9, len(prepared))
        self.assertEqual(str(self.cli.resolve()), captured["command"][0])
        self.assertEqual("assets/input/video-1.mp4", variables["main1"])
        self.assertEqual("assets/input/video-5.mp4", variables["main2"])
        self.assertEqual("assets/input/video-9.mp4", variables["main3"])
        self.assertEqual(
            self.bgm_hash, variables["_bound_bgm"]["sha256"],
        )
        self.assertFalse(variables["_bound_bgm"]["enabled"])
        index = (
            self.service.data_root / ("b" * 32)
            / "hyperframes-nine-grid/index.html"
        ).read_text(encoding="utf-8")
        self.assertIn('data-volume="0"', index)
        self.assertNotIn('src="grid-', index)
        self.assertNotIn('src="main-', index)
        self.assertEqual(2, index.count('src="assets/input/video-1.mp4"'))
        self.assertEqual(2, index.count('src="assets/input/video-5.mp4"'))
        self.assertEqual(2, index.count('src="assets/input/video-9.mp4"'))


class FixedSkillTemplateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.skill = self.root / "skill"
        HyperFramesReferenceTemplateTests._write_skill_fixture(
            self.skill, reference=False,
        )
        self.cli = self.root / "hyperframes-0.8.33"
        self.cli.write_bytes(b"cli")
        self.motion_v2_cli = self.root / "hyperframes-0.8.34"
        self.motion_v2_cli.write_bytes(b"cli")
        self.browser = self.root / "chrome"
        self.browser.write_bytes(b"browser")
        self.configs = copy.deepcopy(matrix.FIXED_SKILL_TEMPLATE_CONFIGS)
        self.template_roots = {}
        for template_id in matrix.FIXED_SKILL_TEMPLATE_IDS:
            root = self.root / template_id
            self._write_template_fixture(template_id, root)
            self.template_roots[template_id] = root
        self.config_patch = mock.patch.object(
            matrix, "FIXED_SKILL_TEMPLATE_CONFIGS", self.configs,
        )
        self.config_patch.start()
        def version(command, **_kwargs):
            value = (
                "0.8.34" if str(command[0]) == str(self.motion_v2_cli)
                else "0.8.33"
            )
            return SimpleNamespace(
                returncode=0, stdout=value + "\n", stderr="",
            )

        with mock.patch.object(matrix.subprocess, "run", side_effect=version):
            self.service = matrix.MatrixTemplateService(
                data_root=self.root / "data",
                skill_root=self.skill,
                triple_strip_root=self.template_roots[
                    matrix.TRIPLE_STRIP_TEMPLATE_ID
                ],
                yellow_banner_root=self.template_roots[
                    matrix.YELLOW_BANNER_TEMPLATE_ID
                ],
                fan_whip_root=self.template_roots[
                    matrix.FAN_WHIP_TEMPLATE_ID
                ],
                brush_panel_root=self.template_roots[
                    matrix.BRUSH_PANEL_TEMPLATE_ID
                ],
                nine_grid_hyperframes_cli=self.cli,
                motion_v2_hyperframes_cli=self.motion_v2_cli,
                hyperframes_browser=self.browser,
                library_url="http://127.0.0.1:8111",
                library_token="library-token",
                legacy_templates_enabled=False,
                start_worker=False,
            )

    def tearDown(self):
        self.service.shutdown()
        self.config_patch.stop()
        self.temp.cleanup()

    def _write_template_fixture(self, template_id: str, root: Path) -> None:
        config = self.configs[template_id]
        (root / "assets/fonts").mkdir(parents=True)
        (root / "assets/vendor").mkdir(parents=True)
        audio = root / config["bgm_path"]
        audio.parent.mkdir(parents=True, exist_ok=True)
        audio.write_bytes((template_id + "-bgm").encode("ascii"))
        config["bgm_sha256"] = hashlib.sha256(audio.read_bytes()).hexdigest()
        for filename in config["font_files"].values():
            (root / "assets/fonts" / filename).write_bytes(
                (template_id + filename).encode("ascii")
            )
        for filename in ("gsap.min.js", "yellow-banner-motion.js"):
            (root / "assets/vendor" / filename).write_text(
                "window.fixture=true;", encoding="utf-8",
            )
        fields = tuple(config.get("expected_fields") or (
            ("title", "subtitle", "ctaLine1", "ctaLine2")
            if template_id == matrix.TRIPLE_STRIP_TEMPLATE_ID else
            ("title", "subtitle1", "subtitle2", "sourceLabel", "body", "cta")
        ))
        variable_ids = fields + tuple(config.get("extra_variable_ids", ()))
        schema = html.escape(json.dumps([
            {"id": field, "type": "string", "default": field}
            for field in variable_ids
        ]), quote=True)
        text_nodes = "".join(
            f'<p id="{field}" data-var-text="{field}">{field}</p>'
            for field in fields
        )
        grading = (
            '<video data-color-grading="{}"></video>' * 2
            if template_id == matrix.YELLOW_BANNER_TEMPLATE_ID else ""
        )
        composition_id = config.get("composition_id", template_id)
        audio_id = config.get("audio_id", "bound-bgm")
        (root / "index.html").write_text(
            f'<html data-composition-variables="{schema}"><head></head><body>'
            f'<div id="root" data-composition-id="{composition_id}">'
            f'{grading}{text_nodes}<audio id="{audio_id}" data-volume="1" '
            f'src="{config["bgm_path"]}"></audio></div></body></html>',
            encoding="utf-8",
        )
        package = {
            "scripts": {
                name: (
                    "npx --yes hyperframes@"
                    f'{config.get("hyperframes_version", "0.8.33")} {command}'
                )
                for name, command in (
                    ("dev", "preview"), ("check", "check"),
                    ("render", "render"), ("publish", "publish"),
                )
            },
        }
        (root / "package.json").write_text(
            json.dumps(package), encoding="utf-8",
        )
        for relative in config.get("required_files", ()):
            path = root.joinpath(*str(relative).split("/"))
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_text("{}\n", encoding="utf-8")
        if template_id in {
            matrix.TRIPLE_STRIP_TEMPLATE_ID,
            matrix.YELLOW_BANNER_TEMPLATE_ID,
        }:
            (root / "hyperframes.json").write_text(
                "{}\n", encoding="utf-8",
            )
        if template_id == matrix.TRIPLE_STRIP_TEMPLATE_ID:
            (root / "index.motion.json").write_text("{}\n", encoding="utf-8")
            compositions = root / "compositions"
            compositions.mkdir()
            (compositions / "opening.html").write_text(
                "<html></html>", encoding="utf-8",
            )
            for index in range(1, 6):
                (compositions / f"main-{index:02d}.html").write_text(
                    "<html></html>", encoding="utf-8",
                )
            manifest = {
                "id": template_id, "version": 1, "renderer": "hyperframes",
                "width": 1080, "height": 1920, "fps": 30,
                "duration": 17.6, "openingSlots": 3, "mainSlots": 5,
                "cutFrames": [0, 117, 199, 281, 363, 445, 528],
            }
        elif template_id == matrix.YELLOW_BANNER_TEMPLATE_ID:
            manifest = {
                "id": template_id, "version": 1, "renderer": "hyperframes",
                "hyperframesVersion": "0.8.33",
                "width": 1080, "height": 1920, "fps": 30,
                "duration": 302 / 30, "frames": 302, "mediaSlots": 3,
                "cutFrames": [0, 86, 183, 302],
            }
        else:
            manifest = {
                "id": template_id, "renderer": "hyperframes",
                "width": 1080, "height": 1920, "fps": 30,
                "duration": config["duration"], "frames": config["frames"],
                "media": {
                    "requiredDistinctVideos": config["required_visuals"],
                    "minimumPreparedDuration": config["slot_frames"][0] / 30,
                    "paths": list(config["media_paths"]),
                },
            }
        manifest[config.get("bgm_manifest_key", "boundBgm")] = {
            "path": config["bgm_path"],
            "sha256": config["bgm_sha256"],
            "duration": config["bgm_duration"],
        }
        (root / "template.json").write_text(
            json.dumps(manifest), encoding="utf-8",
        )

    @staticmethod
    def semantic(top: str, bottom: str) -> dict:
        top_breaks = [
            index for index, char in enumerate(top[:-1])
            if char in "，。！？；：、,.!?;:|｜ "
        ]
        bottom_breaks = [
            index for index, char in enumerate(bottom[:-1])
            if char in "，。！？；：、,.!?;:|｜ "
        ]
        return {
            "version": 1,
            "model": "gpt-4.1-mini",
            "source_sha256": matrix._reference_semantic_source_sha256(
                top, bottom,
            ),
            "top1_end": top_breaks[0] if top_breaks else len(top) - 1,
            "top_break_after": top_breaks,
            "bottom_break_after": bottom_breaks,
        }

    @staticmethod
    def text_width(value: str, metrics: dict) -> float:
        display = matrix._hide_reference_edge_punctuation(value)
        return len(display) * int(metrics["font_size_px"])

    def test_catalog_exposes_four_fixed_templates_after_existing_catalog(self):
        self.assertEqual(4, len(self.service.catalog))
        self.assertEqual(
            list(matrix.FIXED_SKILL_TEMPLATE_IDS),
            [item["id"] for item in self.service.catalog],
        )
        self.assertEqual(
            matrix.TRIPLE_STRIP_TEMPLATE_ID,
            self.service.default_template_id,
        )
        self.assertNotIn("full-overlay-bold", self.service.templates)
        self.assertNotIn("poster-split", self.service.templates)
        for template_id in matrix.FIXED_SKILL_TEMPLATE_IDS:
            item = self.service.templates[template_id]
            config = self.configs[template_id]
            self.assertEqual("fixed", item["duration_mode"])
            self.assertEqual(config["duration"], item["fixed_duration_seconds"])
            self.assertEqual(config["required_visuals"], item["required_visuals"])
            self.assertTrue(item["bgm_optional"])
            self.assertEqual("bound", item["bgm_mode"])
        health = self.service.health()
        self.assertEqual(
            sorted(matrix.FIXED_SKILL_TEMPLATE_IDS),
            health["fixed_skill_templates"],
        )
        self.assertEqual(4, health["fixed_skill_template_count"])
        self.assertEqual("mixed", health["fixed_skill_hyperframes_version"])
        self.assertEqual(
            {"0.8.33": 2, "0.8.34": 2},
            health["fixed_skill_hyperframes_versions"],
        )

    def test_shared_sixty_eighty_copy_contract_preserves_source_text(self):
        top = "创业团队，" * 12
        bottom = "评论区扣888，" * 10
        self.assertEqual((60, 80), (len(top), len(bottom)))
        semantic = self.semantic(top, bottom)
        with mock.patch.object(
            self.service, "_reference_text_width", side_effect=self.text_width,
        ):
            for template_id in matrix.FIXED_SKILL_TEMPLATE_IDS:
                with self.subTest(template_id=template_id):
                    payload = self.service.validate_payload({
                        "top_text": top,
                        "bottom_text": bottom,
                        "template_id": template_id,
                        "semantic_layout": semantic,
                        "bgm": False,
                    }, require_reference_semantic_layout=True)
                    frozen = self.service._freeze_font_provenance(
                        template_id.replace("-", "")[:32].ljust(32, "0"),
                        payload,
                    )
                    contract = frozen["_fixed_skill_template"]
                    self.assertEqual(top, contract["text"]["source"]["top_text"])
                    self.assertEqual(
                        bottom, contract["text"]["source"]["bottom_text"],
                    )
                    self.assertFalse(contract["bgm_enabled"])
                    self.assertEqual(
                        self.configs[template_id]["duration"],
                        payload["duration"],
                    )

    def test_material_scenes_are_video_only_and_do_not_request_extra_bgm(self):
        for template_id in matrix.FIXED_SKILL_TEMPLATE_IDS:
            config = self.configs[template_id]
            payload = {
                "top_text": "活动标题", "bottom_text": "评论区扣888",
                "template_id": template_id,
                "duration": config["duration"], "bgm": True,
            }
            scenes, count, hyperframes = self.service._material_scenes(payload)
            self.assertEqual(config["required_visuals"], count)
            self.assertTrue(hyperframes)
            self.assertEqual(count, len(scenes))
            self.assertEqual({"video"}, {item["media_type"] for item in scenes})
            self.assertEqual(
                [round(frames / 30.0, 6) for frames in config["slot_frames"]],
                [item["clip_duration_seconds"] for item in scenes],
            )

    def test_owned_public_bound_bgm_is_accepted_without_shared_library(self):
        template_id = matrix.TRIPLE_STRIP_TEMPLATE_ID
        top = "团队8个人，每天产出100条短视频"
        bottom = "评论区扣888"
        user_root = self.service.data_root / matrix.USER_ASSET_DIRNAME
        user_root.mkdir(parents=True)
        user_materials = []
        for index in range(self.configs[template_id]["required_visuals"]):
            content = ("owned-bound-bgm-%d" % index).encode()
            sha = hashlib.sha256(content).hexdigest()
            (user_root / (sha + ".mp4")).write_bytes(content)
            user_materials.append({
                "sha256": sha, "media_type": "video",
            })
        with mock.patch.object(
            self.service, "_reference_text_width", side_effect=self.text_width,
        ), mock.patch.object(
            self.service, "_inspect_user_asset", return_value=30.0,
        ), mock.patch.object(
            self.service, "_library_request",
            side_effect=AssertionError("bound BGM must not use shared library"),
        ):
            accepted = self.service.submit({
                "top_text": top,
                "bottom_text": bottom,
                "template_id": template_id,
                "semantic_layout": self.semantic(top, bottom),
                "material_policy": "owned_public",
                "user_materials": user_materials,
                "bgm": True,
            }, "owned-public-bound-bgm")
            payload = json.loads(
                self.service.store.get(accepted["job_id"])["payload"]
            )
            selected = self.service._select_materials(
                payload, accepted["job_id"]
            )

        self.assertEqual("pending", accepted["status"])
        self.assertTrue(payload["_fixed_skill_template"]["bgm_enabled"])
        self.assertEqual({"user"}, {item["provider"] for item in selected})

    def test_fixed_templates_split_bookends_library_and_middle_pexels(self):
        self.service.pexels_api_key = "configured-pexels-key"
        for template_id in matrix.FIXED_SKILL_TEMPLATE_IDS:
            config = self.configs[template_id]
            payload = {
                "top_text": "活动标题", "bottom_text": "评论区扣888",
                "template_id": template_id,
                "duration": config["duration"], "bgm": True,
                "_material_selection_contract_version": 2,
            }
            count = config["required_visuals"]
            durations = [
                round(frames / 30.0, 6) for frames in config["slot_frames"]
            ]
            plan = matrix._material_source_plan(
                count,
                include_middle_library=(
                    template_id in matrix.MOTION_V2_TEMPLATE_IDS
                ),
                seed="d" * 32,
            )
            library_indexes = [
                index for index, source in enumerate(plan)
                if source == "huangque"
            ]
            pexels_indexes = [
                index for index, source in enumerate(plan)
                if source == "pexels"
            ]

            def material(index):
                return {
                    "scene_id": f"media_{index + 1:02d}",
                    "record_id": f"record-{index + 1}",
                    "sha256": format(index + 1, "064x"),
                    "media_type": "video", "match_level": "random",
                    "clip_id": format(index + 101, "064x"),
                    "clip_start_seconds": float(index + 1),
                    "clip_duration_seconds": durations[index],
                    "clip_slot_index": 1, "clip_slot_count": 1,
                }

            library_materials = [material(index) for index in library_indexes]
            pexels_materials = [material(index) for index in pexels_indexes]
            response = {
                "materials": library_materials,
                "selection_contract_version": 2,
                "clip_contract_version": 3,
            }
            with self.subTest(template_id=template_id), mock.patch.object(
                self.service, "_library_request", return_value=response,
            ) as library, mock.patch.object(
                self.service, "_select_pexels_materials",
                return_value=pexels_materials,
            ) as pexels:
                selected = self.service._select_materials_once(
                    payload, "d" * 32,
                )
            self.assertEqual(
                [f"media_{index + 1:02d}" for index in range(count)],
                [item["scene_id"] for item in selected],
            )
            self.assertEqual(
                len(library_indexes),
                len(library.call_args.args[2]["scenes"]),
            )
            self.assertEqual(
                len(pexels_indexes), len(pexels.call_args.args[0]),
            )
            self.assertEqual(
                [durations[index] for index in library_indexes],
                [
                    scene["clip_duration_seconds"]
                    for scene in library.call_args.args[2]["scenes"]
                ],
            )

    def test_production_without_pexels_uses_library_for_every_slot(self):
        def version(command, **_kwargs):
            value = (
                "0.8.34" if str(command[0]) == str(self.motion_v2_cli)
                else "0.8.33"
            )
            return SimpleNamespace(returncode=0, stdout=value + "\n", stderr="")

        with mock.patch.object(matrix.subprocess, "run", side_effect=version):
            production = matrix.MatrixTemplateService(
                data_root=self.root / "production-no-pexels-data",
                skill_root=self.skill,
                triple_strip_root=self.template_roots[
                    matrix.TRIPLE_STRIP_TEMPLATE_ID
                ],
                yellow_banner_root=self.template_roots[
                    matrix.YELLOW_BANNER_TEMPLATE_ID
                ],
                fan_whip_root=self.template_roots[matrix.FAN_WHIP_TEMPLATE_ID],
                brush_panel_root=self.template_roots[
                    matrix.BRUSH_PANEL_TEMPLATE_ID
                ],
                nine_grid_hyperframes_cli=self.cli,
                motion_v2_hyperframes_cli=self.motion_v2_cli,
                hyperframes_browser=self.browser,
                library_url="http://127.0.0.1:8111",
                library_token="library-token",
                pexels_api_key="",
                concurrency=5,
                legacy_templates_enabled=False,
                start_worker=True,
            )

        library_scene_groups = []
        selected = []

        def library_request(method, path, body=None, *, timeout=30):
            if method == "GET" and path == "/v1/ping":
                return {
                    "ok": True, "records": 10,
                    "selection_contract_version": 2,
                    "clip_contract_version": 3,
                }
            self.assertEqual(("POST", "/v1/select"), (method, path))
            scenes = body["scenes"]
            library_scene_groups.append([scene["scene_id"] for scene in scenes])
            materials = []
            for index, scene in enumerate(scenes, 1):
                identity = hashlib.sha256(
                    (body["selection_id"] + ":" + scene["scene_id"]).encode()
                ).hexdigest()
                materials.append({
                    "scene_id": scene["scene_id"],
                    "record_id": "library-" + scene["scene_id"],
                    "sha256": identity,
                    "media_type": "video",
                    "provider": "huangque",
                    "match_level": "random",
                    "clip_id": hashlib.sha256(
                        (identity + ":clip").encode()
                    ).hexdigest(),
                    "clip_start_seconds": float(index),
                    "clip_duration_seconds": scene["clip_duration_seconds"],
                    "clip_slot_index": 1,
                    "clip_slot_count": 1,
                })
            return {
                "materials": materials,
                "selection_contract_version": 2,
                "clip_contract_version": 3,
            }

        def execute(job_id):
            row = production.store.get(job_id)
            payload = json.loads(row["payload"])
            selected.extend(production._select_materials_once(payload, job_id))
            return {"file_url": f"/v1/files/{job_id}.mp4"}

        server = matrix.build_server("127.0.0.1", 0, production, "api-token")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def post(path, body, request_id=""):
            headers = {
                "Authorization": "Bearer api-token",
                "Content-Type": "application/json",
            }
            if request_id:
                headers["X-Request-Id"] = request_id
            request = urllib.request.Request(
                "http://127.0.0.1:%d%s" % (server.server_port, path),
                data=json.dumps(body).encode("utf-8"),
                method="POST", headers=headers,
            )
            try:
                return urllib.request.urlopen(request, timeout=3)
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                self.fail(f"POST {path} returned HTTP {exc.code}: {detail}")

        body = {
            "top_text": "创业，提效",
            "bottom_text": "评论，获取",
            "template_id": matrix.FAN_WHIP_TEMPLATE_ID,
            "semantic_layout": self.semantic(
                "创业，提效", "评论，获取",
            ),
            "bgm": False,
        }
        try:
            with mock.patch.object(
                production, "_library_request", side_effect=library_request,
            ), mock.patch.object(
                production, "_select_pexels_materials",
                side_effect=AssertionError("Pexels must not be called"),
            ) as pexels, mock.patch.object(
                production, "_execute", side_effect=execute,
            ), mock.patch.object(
                production, "_reference_text_width", side_effect=self.text_width,
            ):
                health = production.health()
                self.assertTrue(health["ok"])
                self.assertTrue(health["material_library_ready"])
                self.assertFalse(health["pexels_material_ready"])
                self.assertTrue(health["pexels_material_optional"])
                self.assertEqual(5, health["worker_count"])

                with post("/v1/preflight", body) as response:
                    self.assertEqual(200, response.status)
                    self.assertTrue(json.load(response)["ok"])
                with post(
                    "/v1/jobs", body, "production-no-pexels-job",
                ) as response:
                    self.assertEqual(202, response.status)
                    job = json.load(response)

                deadline = time.time() + 3
                while time.time() < deadline:
                    row = production.store.get(job["job_id"])
                    if row["status"] == "completed":
                        break
                    time.sleep(0.01)
                self.assertEqual("completed", row["status"])
                self.assertEqual(
                    self.configs[matrix.FAN_WHIP_TEMPLATE_ID]["required_visuals"],
                    len(selected),
                )
                self.assertEqual({"huangque"}, {
                    item["provider"] for item in selected
                })
                self.assertEqual(
                    [[item["scene_id"] for item in selected]],
                    library_scene_groups,
                )
                pexels.assert_not_called()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            production.shutdown()

    def test_fixed_template_rejects_legacy_three_second_slot_receipt(self):
        template_id = matrix.TRIPLE_STRIP_TEMPLATE_ID
        config = self.configs[template_id]
        payload = {
            "top_text": "活动标题", "bottom_text": "评论区扣888",
            "template_id": template_id,
            "duration": config["duration"], "bgm": False,
            "_material_selection_contract_version": 2,
        }
        materials = [{
            "scene_id": f"media_{index:02d}",
            "record_id": f"record-{index}",
            "sha256": format(index, "064x"),
            "media_type": "video", "match_level": "random",
            "clip_id": format(index + 100, "064x"),
            "clip_start_seconds": float(index),
            "clip_duration_seconds": 3.0,
            "clip_slot_index": 1, "clip_slot_count": 1,
        } for index in range(1, config["required_visuals"] + 1)]
        response = {
            "materials": materials,
            "selection_contract_version": 2,
            "clip_contract_version": 3,
        }

        with mock.patch.object(
            self.service, "_library_request", return_value=response,
        ), self.assertRaisesRegex(
            matrix.MatrixTemplateError, "素材库返回的切片契约不完整",
        ):
            self.service._select_materials_once(payload, "d" * 32)

    def test_fixed_clip_rejects_window_past_source_end_without_repositioning(self):
        source = self.root / "short-fixed-source.mp4"
        source.write_bytes(b"source")
        destination = self.root / "fixed-output.mp4"

        with mock.patch.object(
            self.service, "_reference_video_duration", return_value=4.5,
        ), mock.patch.object(
            self.service, "_run_tracked_process",
            side_effect=AssertionError("insufficient source must not render"),
        ), self.assertRaisesRegex(
            matrix.MatrixTemplateError, "固定 Skill 模板素材时长不足",
        ):
            self.service._prepare_fixed_skill_clip(
                source, destination, 1.0, 117, 640,
                deadline_at=time.time() + 30,
            )

    def test_fan_whip_stills_are_generated_from_prepared_job_videos(self):
        workdir = self.root / "fan-stills"
        source = workdir / "assets/media/01-aspect-fixed.mp4"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"prepared-video")
        config = {
            "still_frames": ((
                "assets/media/01-aspect-fixed.mp4",
                "assets/media/01-aspect-fixed.jpg",
                0.5,
            ),),
        }
        captured = {}

        def run(command, **_kwargs):
            captured["command"] = command
            Path(command[-1]).write_bytes(b"jpeg" * 300)
            return 0, b"", b""

        with mock.patch.object(
            self.service, "_run_tracked_process", side_effect=run,
        ):
            self.service._prepare_fixed_skill_stills(
                workdir, config, deadline_at=time.time() + 30,
            )

        target = workdir / "assets/media/01-aspect-fixed.jpg"
        self.assertTrue(target.is_file())
        self.assertEqual("0.5", captured["command"][
            captured["command"].index("-ss") + 1
        ])

    def test_render_stages_frozen_fields_media_and_optional_bgm(self):
        class Process:
            returncode = 0

            def communicate(self, timeout=None):
                return b"", b""

        top = "团队8个人，每天产出100条短视频"
        bottom = "评论区扣888"
        semantic = self.semantic(top, bottom)
        with mock.patch.object(
            self.service, "_reference_text_width", side_effect=self.text_width,
        ):
            for template_id in matrix.FIXED_SKILL_TEMPLATE_IDS:
                with self.subTest(template_id=template_id):
                    config = self.configs[template_id]
                    payload = self.service.validate_payload({
                        "top_text": top, "bottom_text": bottom,
                        "template_id": template_id,
                        "semantic_layout": semantic, "bgm": False,
                    }, require_reference_semantic_layout=True)
                    payload = self.service._freeze_font_provenance(
                        template_id.replace("-", "")[:32].ljust(32, "1"),
                        payload,
                    )
                    durations = [
                        round(frames / 30.0, 6)
                        for frames in config["slot_frames"]
                    ]
                    materials = [{
                        "scene_id": f"media_{index:02d}",
                        "sha256": format(index, "064x"),
                        "media_type": "video",
                        "clip_start_seconds": float(index),
                        "clip_duration_seconds": durations[index - 1],
                    } for index in range(1, config["required_visuals"] + 1)]
                    paths = []
                    for index in range(config["required_visuals"]):
                        path = self.root / f"source-{template_id}-{index}.mp4"
                        path.write_bytes(b"source")
                        paths.append(path)
                    prepared = []

                    def prepare(source, destination, start, frames, height,
                                *, deadline_at):
                        prepared.append((destination, start, frames, height))
                        return float(start)

                    with mock.patch.object(
                        self.service, "_prepare_fixed_skill_clip",
                        side_effect=prepare,
                    ), mock.patch.object(
                        self.service, "_prepare_fixed_skill_stills",
                    ), mock.patch.object(
                        matrix.subprocess, "Popen", return_value=Process(),
                    ) as popen, mock.patch.object(
                        self.service, "_validate_reference_visual_coverage",
                    ):
                        values = self.service._render_fixed_skill_template(
                            payload, template_id.replace("-", "")[:32].ljust(32, "2"),
                            materials, paths, deadline_at=time.time() + 60,
                        )
                    self.assertEqual(config["required_visuals"], len(prepared))
                    self.assertEqual(
                        list(zip(config["slot_frames"], durations)),
                        [
                            (frames, materials[index]["clip_duration_seconds"])
                            for index, (_destination, _start, frames, _height)
                            in enumerate(prepared)
                        ],
                    )
                    self.assertFalse(values["_bound_bgm"]["enabled"])
                    command = popen.call_args.args[0]
                    self.assertIn("--strict-variables", command)
                    workdir = Path(command[2])
                    index_html = (workdir / "index.html").read_text(
                        encoding="utf-8",
                    )
                    self.assertIn('data-volume="0"', index_html)
                    self.assertIn('id="matrix-fixed-skill-copy"', index_html)
                    if template_id == matrix.YELLOW_BANNER_TEMPLATE_ID:
                        self.assertNotIn("data-color-grading=", index_html)
                        self.assertIn("filter:blur(14px)", index_html)


class PexelsMaterialRoutingTests(unittest.TestCase):
    """Pexels 中国场景混合素材路由回归测试。"""

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
            json.dumps({"fonts": bundled}), encoding="utf-8",
        )
        (self.skill / "scripts").mkdir()
        templates = [{
            "id": template_id, "name": f"模板 {index}",
            "description": "测试模板", "tags": ["测试"], "layout": {}, "render": {},
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
            pexels_api_key="test-pexels-key",
            start_worker=False,
        )

    def tearDown(self):
        self.service.shutdown()
        self.temp.cleanup()

    @staticmethod
    def _pexels_video(video_id, file_id=0, duration=10.0, width=1080, height=1920,
                      quality="hd"):
        return {
            "id": video_id,
            "duration": duration,
            "url": f"https://www.pexels.com/video/sample-{video_id}/",
            "user": {"name": f"作者{video_id}", "url": f"https://www.pexels.com/@a{video_id}/"},
            "video_files": [{
                "id": file_id or video_id * 10,
                "file_type": "video/mp4",
                "width": width, "height": height, "quality": quality,
                "link": f"https://videos.pexels.com/video-files/{video_id}/x.mp4",
            }],
        }

    def _response(self, body=b"{}", content_type="application/json"):
        class _Headers:
            def get_content_type(self):
                return content_type
        class _Resp:
            def __init__(self):
                self.headers = _Headers()
                self._buf = body
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self, _n=-1):
                data = self._buf
                self._buf = b""
                return data
        return _Resp()

    # 1-8：来源顺序与片段数量
    def test_source_plan_bookends_library_middle_pexels(self):
        self.assertEqual(
            ("huangque", "pexels", "pexels"), matrix._material_source_plan(3),
        )
        self.assertEqual(
            ("huangque", "pexels", "pexels", "huangque"),
            matrix._material_source_plan(4),
        )
        self.assertEqual(
            ("huangque", "pexels", "pexels", "pexels", "huangque"),
            matrix._material_source_plan(5),
        )
        self.assertEqual(
            ("huangque", "pexels", "pexels", "pexels", "pexels",
             "pexels", "pexels", "huangque"),
            matrix._material_source_plan(8),
        )
        self.assertEqual(
            ("huangque", "pexels", "pexels", "pexels", "pexels",
             "pexels", "pexels", "pexels", "huangque"),
            matrix._material_source_plan(9),
        )

    def test_never_six_clips(self):
        with self.assertRaises(matrix.MatrixTemplateError):
            matrix._material_source_plan(6)

    def test_motion_v2_source_plan_adds_one_stable_middle_library_slot(self):
        for count in (5, 7):
            seen_middle = set()
            for index in range(64):
                seed = format(index, "032x")
                first = matrix._material_source_plan(
                    count, include_middle_library=True, seed=seed,
                )
                second = matrix._material_source_plan(
                    count, include_middle_library=True, seed=seed,
                )
                library_indexes = [
                    slot for slot, source in enumerate(first)
                    if source == "huangque"
                ]
                self.assertEqual(first, second)
                self.assertEqual(3, len(library_indexes))
                self.assertEqual([0, count - 1], [
                    library_indexes[0], library_indexes[-1],
                ])
                seen_middle.add(library_indexes[1])
            self.assertEqual(set(range(1, count - 1)), seen_middle)
        self.assertLessEqual(matrix._required_visuals(15.0), 5)
        self.assertGreaterEqual(matrix._required_visuals(7.0), 3)

    def test_clip_duration_2_to_3_seconds(self):
        for duration in (7.0, 8.0, 9.0, 10.0, 11.0, 12.0, 13.0, 14.0, 15.0):
            count = matrix._required_visuals(duration)
            segment = duration / count
            self.assertTrue(
                matrix.REFERENCE_MIN_SEGMENT_SECONDS <= segment
                <= matrix.REFERENCE_MAX_SEGMENT_SECONDS,
                (duration, count, segment),
            )

    def test_first_clip_always_huangque(self):
        for count in (3, 4, 5, 8, 9):
            self.assertEqual("huangque", matrix._material_source_plan(count)[0])

    def test_last_clip_3_pexels_45_huangque(self):
        self.assertEqual("pexels", matrix._material_source_plan(3)[-1])
        self.assertEqual("huangque", matrix._material_source_plan(4)[-1])
        self.assertEqual("huangque", matrix._material_source_plan(5)[-1])
        self.assertEqual("huangque", matrix._material_source_plan(8)[-1])
        self.assertEqual("huangque", matrix._material_source_plan(9)[-1])

    # 9：BGM 始终来自黄雀
    def test_bgm_always_huangque(self):
        payload = self.service.validate_payload({
            "top_text": "大健康行业", "bottom_text": "评论交流", "bgm": True,
        })
        self.assertEqual("shared", payload["material_policy"])
        library_scenes = []
        def fake_library(method, path, body):
            library_scenes.append(body.get("scenes") or [])
            return {"materials": [], "selection_contract_version": 1,
                    "clip_contract_version": 1}
        pexels_scenes = []
        def fake_pexels(scenes, job_id, used_sha256=()):
            pexels_scenes.extend(scenes)
            return []
        with mock.patch.object(self.service, "_library_request", side_effect=fake_library), \
             mock.patch.object(self.service, "_select_pexels_materials", side_effect=fake_pexels), \
             mock.patch.object(self.service, "_validate_material_selection",
                               side_effect=lambda p, v, c: v):
            self.service._select_materials_once(payload, "a" * 32)
        library_scene_ids = {s["scene_id"] for group in library_scenes for s in group}
        pexels_scene_ids = {s["scene_id"] for s in pexels_scenes}
        self.assertIn("bgm", library_scene_ids)
        self.assertNotIn("bgm", pexels_scene_ids)

    # 10：Pexels 请求参数
    def test_pexels_search_uses_locale_portrait_medium(self):
        captured = {}
        def fake_urlopen(request, timeout=None):
            captured["url"] = request.full_url
            captured["headers"] = dict(request.headers)
            return self._response(json.dumps({"videos": []}).encode())
        with mock.patch.object(matrix.urllib.request, "urlopen", side_effect=fake_urlopen):
            self.service._pexels_search("中国城市生活")
        self.assertIn("orientation=portrait", captured["url"])
        self.assertIn("size=medium", captured["url"])
        self.assertIn("locale=zh-CN", captured["url"])
        self.assertIn("per_page=80", captured["url"])
        self.assertEqual("test-pexels-key", captured["headers"].get("Authorization"))

    # 11：顾客文案不作为搜索词
    def test_query_not_from_customer_copy(self):
        for job_id in ("a" * 32, "b" * 32, "c" * 32):
            query = matrix.MatrixTemplateService._pexels_search_query(job_id)
            self.assertIn(query, matrix.PEXELS_CHINA_QUERIES)
        self.assertNotIn("大健康", matrix.PEXELS_CHINA_QUERIES)

    # 12-13：去重
    def test_same_job_no_duplicate_pexels_id(self):
        def fake_search(query):
            return {"videos": [self._pexels_video(i) for i in range(1, 12)]}
        scenes = [{"scene_id": f"m{i}", "clip_duration_seconds": 2.5} for i in range(3)]
        with mock.patch.object(self.service, "_pexels_search", side_effect=fake_search):
            selected = self.service._select_pexels_materials(scenes, "a" * 32)
        ids = [item["provider_video_id"] for item in selected]
        self.assertEqual(len(ids), len(set(ids)))

    def test_pexels_receipt_preserves_frame_exact_clip_duration(self):
        duration = 82 / 30

        def fake_search(_query):
            return {"videos": [self._pexels_video(1)]}

        with mock.patch.object(
            self.service, "_pexels_search", side_effect=fake_search,
        ):
            selected = self.service._select_pexels_materials([{
                "scene_id": "frame-exact",
                "clip_duration_seconds": duration,
            }], "f" * 32)

        self.assertEqual(duration, selected[0]["clip_duration_seconds"])

    def test_batch_no_duplicate_pexels_id(self):
        def fake_search(query):
            return {"videos": [self._pexels_video(i) for i in range(1, 20)]}
        scenes = [{"scene_id": f"m{i}", "clip_duration_seconds": 2.5} for i in range(5)]
        with mock.patch.object(self.service, "_pexels_search", side_effect=fake_search):
            first = self.service._select_pexels_materials(scenes, "b" * 32)
        used = [item["sha256"] for item in first]
        with mock.patch.object(self.service, "_pexels_search", side_effect=fake_search):
            second = self.service._select_pexels_materials(
                scenes, "c" * 32, used_sha256=used,
            )
        first_ids = {item["provider_video_id"] for item in first}
        second_ids = {item["provider_video_id"] for item in second}
        self.assertFalse(first_ids & second_ids)

    # 14：重试复用冻结结果（确定性）
    def test_retry_reuses_frozen(self):
        def fake_search(query):
            return {"videos": [self._pexels_video(i) for i in range(1, 15)]}
        scenes = [{"scene_id": f"m{i}", "clip_duration_seconds": 2.5} for i in range(3)]
        with mock.patch.object(self.service, "_pexels_search", side_effect=fake_search):
            first = self.service._select_pexels_materials(scenes, "d" * 32)
        with mock.patch.object(self.service, "_pexels_search", side_effect=fake_search):
            second = self.service._select_pexels_materials(scenes, "d" * 32)
        self.assertEqual(
            [item["provider_video_id"] for item in first],
            [item["provider_video_id"] for item in second],
        )

    # 15：搜索缓存 24 小时
    def test_search_cache_24h(self):
        calls = []
        def fake_urlopen(request, timeout=None):
            calls.append(request.full_url)
            return self._response(json.dumps({"videos": []}).encode())
        with mock.patch.object(matrix.urllib.request, "urlopen", side_effect=fake_urlopen):
            self.service._pexels_search("中国商务团队")
            self.service._pexels_search("中国商务团队")
        self.assertEqual(1, len(calls))
        cache_dir = self.service.data_root / ".pexels-search-cache"
        self.assertTrue(any(cache_dir.iterdir()))

    # 16：401/429/超时/无效 JSON 失败
    def test_pexels_failures(self):
        def urlopen_401(*a, **k):
            raise urllib.error.HTTPError("url", 401, "Unauthorized", {}, None)
        with mock.patch.object(matrix.urllib.request, "urlopen", side_effect=urlopen_401):
            with self.assertRaises(matrix.MatrixTemplateError) as ctx:
                self.service._pexels_search("中国城市生活")
            self.assertIn("密钥无效", str(ctx.exception))

        def urlopen_429(*a, **k):
            raise urllib.error.HTTPError("url", 429, "Too Many", {}, None)
        with mock.patch.object(matrix.urllib.request, "urlopen", side_effect=urlopen_429):
            with self.assertRaises(matrix.MatrixTemplateError) as ctx:
                self.service._pexels_search("中国城市生活")
            self.assertIn("额度已用完", str(ctx.exception))

        with mock.patch.object(matrix.urllib.request, "urlopen",
                               side_effect=urllib.error.URLError("timeout")):
            with self.assertRaises(matrix.MatrixTemplateError):
                self.service._pexels_search("中国城市生活")

        with mock.patch.object(matrix.urllib.request, "urlopen",
                               return_value=self._response(b"not-json")):
            with self.assertRaises(matrix.MatrixTemplateError):
                self.service._pexels_search("中国城市生活")

    # 17：素材不足失败，不回退黄雀
    def test_insufficient_no_fallback(self):
        def fake_search(query):
            return {"videos": [self._pexels_video(1, duration=1.0)]}
        scenes = [{"scene_id": f"m{i}", "clip_duration_seconds": 2.5} for i in range(3)]
        with mock.patch.object(self.service, "_pexels_search", side_effect=fake_search):
            with self.assertRaises(matrix.MatrixTemplateError) as ctx:
                self.service._select_pexels_materials(scenes, "e" * 32)
            self.assertIn("素材不足", str(ctx.exception))

    # 18：下载类型/大小/SHA 检查
    def test_download_checks(self):
        item = {"sha256": "a" * 64, "source_url": "https://videos.pexels.com/x.mp4"}
        target = self.root / "dl"
        target.mkdir()
        with mock.patch.object(matrix.urllib.request, "urlopen",
                               return_value=self._response(b"x", "text/html")):
            with self.assertRaises(matrix.MatrixTemplateError):
                self.service._download_pexels(item, target)
        with mock.patch.object(matrix.urllib.request, "urlopen",
                               return_value=self._response(b"", "video/mp4")):
            with self.assertRaises(matrix.MatrixTemplateError):
                self.service._download_pexels(item, target)
        payload = b"fake-mp4-content"
        with mock.patch.object(matrix.urllib.request, "urlopen",
                               return_value=self._response(payload, "video/mp4")):
            path = self.service._download_pexels(item, target)
        self.assertTrue(path.exists())
        self.assertEqual(hashlib.sha256(payload).hexdigest(), item["content_sha256"])

    # 19：密钥不进入结果与日志
    def test_key_not_in_result_or_logs(self):
        with mock.patch.object(self.service, "require_library_ready", return_value={
            "ready": True, "selection_contract_version": 2, "clip_contract_version": 3,
        }):
            health = self.service.health()
        self.assertNotIn("test-pexels-key", json.dumps(health, ensure_ascii=False))

    # 20：Pexels 配置可选，但存在时必须通过权限和内容检查
    def test_installer_accepts_only_safe_optional_pexels_env(self):
        install = (Path(__file__).resolve().parents[1]
                   / "deploy/matrix-template-video/install.sh").read_text(encoding="utf-8")
        self.assertIn("PEXELS_ENV_FILE", install)
        self.assertIn('if [[ -e "${PEXELS_ENV_FILE}" ]]', install)
        self.assertIn("root:admin", install)
        self.assertIn("640", install)
        self.assertIn("PEXELS_API_KEY", install)
        self.assertLess(install.index("PEXELS_ENV_FILE"), install.index("systemctl"))
        unit = (Path(__file__).resolve().parents[1]
                / "deploy/systemd/huangque-matrix-template.service").read_text(
                    encoding="utf-8",
                )
        self.assertIn("EnvironmentFile=-/etc/huangque/pexels.env", unit)

    # 21：部署健康门禁检查新字段
    def test_health_gate_fields(self):
        with mock.patch.object(self.service, "require_library_ready", return_value={
            "ready": True, "selection_contract_version": 2, "clip_contract_version": 3,
        }):
            health = self.service.health()
        self.assertIs(health["pexels_material_ready"], True)
        self.assertIs(health["pexels_material_optional"], True)
        self.assertEqual(
            "huangque-bookends-extra-middle-pexels-v2",
            health["material_source_policy"],
        )

    # 22：内容哈希跨崩溃恢复冻结
    def test_content_hash_frozen_across_crash_recovery(self):
        item = {
            "scene_id": "media_02", "record_id": "pexels-video-1",
            "sha256": "b" * 64, "source_identity": "b" * 64,
            "media_type": "video", "provider": "pexels",
            "source_url": "https://videos.pexels.com/x.mp4",
        }
        self.service.store.reserve_job_materials("j" * 32, [item], 1)
        target = self.root / "dl"
        target.mkdir()
        # 首次下载：持久化在 _download_pexels 内部完成（无调用方后续更新）
        with mock.patch.object(matrix.urllib.request, "urlopen",
                               return_value=self._response(b"first-bytes", "video/mp4")):
            self.service._download_pexels(item, target, job_id="j" * 32)
        first_hash = item["content_sha256"]
        self.assertTrue(matrix.SHA_RE.fullmatch(first_hash))
        # 模拟进程退出后重启：从 DB 读回冻结选择，content_sha256 必须已持久化
        recovered = self.service.store.material_selection("j" * 32)["materials"][0]
        self.assertEqual(first_hash, recovered["content_sha256"])
        # 同一来源返回不同字节 → fail closed，而不是接受第二次下载
        with mock.patch.object(matrix.urllib.request, "urlopen",
                               return_value=self._response(b"other-bytes", "video/mp4")):
            with self.assertRaises(matrix.MatrixTemplateError) as ctx:
                self.service._download_pexels(recovered, target, job_id="j" * 32)
            self.assertIn("发生变化", str(ctx.exception))

    # 23：source_identity 与 content_sha256 语义区分
    def test_manifest_source_identity_vs_content_sha256(self):
        payload = self.service.validate_payload({
            "top_text": "大健康行业", "bottom_text": "评论交流",
        })
        job, _ = self.service.store.create(
            "manifest-1", payload,
            freeze_payload=self.service._freeze_font_provenance,
        )
        pexels_item = {
            "scene_id": "media_01", "record_id": "pexels-video-123",
            "sha256": "c" * 64, "source_identity": "c" * 64,
            "media_type": "video", "match_level": "pexels_china_query",
            "clip_id": "d" * 64, "clip_start_seconds": 1.0,
            "clip_duration_seconds": 2.5, "clip_slot_index": 1, "clip_slot_count": 1,
            "provider": "pexels", "provider_video_id": 123,
            "provider_file_id": 456, "provider_url": "https://www.pexels.com/video/123/",
            "contributor_name": "作者", "contributor_url": "https://www.pexels.com/@a/",
            "search_query": "中国城市生活",
        }
        materials = [pexels_item]
        self.service.store.reserve_job_materials(job["job_id"], materials, 1)

        def download(item, target, job_id=""):
            item["content_sha256"] = "e" * 64  # 实际下载文件哈希，与 source_identity 不同
            path = target / "0.mp4"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"asset")
            return path

        def render(project_path):
            output = project_path.parent / "output/final.mp4"
            output.parent.mkdir(parents=True)
            output.write_bytes(b"video")

        with mock.patch.object(self.service, "_select_materials", return_value=materials), \
             mock.patch.object(self.service, "_download", side_effect=download), \
             mock.patch.object(self.service, "_reference_video_duration", return_value=10.0), \
             mock.patch.object(self.service, "_render", side_effect=render), \
             mock.patch.object(self.service, "_probe",
                               return_value={"duration": 7.0, "width": 1080, "height": 1920}):
            result = self.service._execute(job["job_id"])
        manifest = result["material_manifest"][0]
        self.assertEqual("c" * 64, manifest["source_identity"])
        self.assertEqual("e" * 64, manifest["content_sha256"])
        self.assertNotEqual(manifest["source_identity"], manifest["content_sha256"])
class UserMaterialsTests(unittest.TestCase):
    """用户自带素材（provider=user）内测功能回归测试。"""

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
            json.dumps({"fonts": bundled}), encoding="utf-8",
        )
        (self.skill / "scripts").mkdir()
        templates = [{
            "id": template_id, "name": f"模板 {index}",
            "description": "测试模板", "tags": ["测试"], "layout": {}, "render": {},
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
        self.inspect_patch = mock.patch.object(
            self.service, "_inspect_user_asset", return_value=30.0,
        )
        self.inspect_patch.start()

    def tearDown(self):
        self.inspect_patch.stop()
        self.service.shutdown()
        self.temp.cleanup()

    def _start_server(self):
        server = matrix.build_server("127.0.0.1", 0, self.service, "api-token")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread, "http://127.0.0.1:%d" % server.server_port

    def _store_user_asset(self, content: bytes, suffix: str = ".mp4") -> str:
        sha = hashlib.sha256(content).hexdigest()
        root = self.service.data_root / matrix.USER_ASSET_DIRNAME
        root.mkdir(parents=True, exist_ok=True)
        (root / (sha + suffix)).write_bytes(content)
        return sha

    def test_owned_public_without_upload_uses_all_pexels_and_no_shared_library(self):
        self.service.pexels_api_key = "test-pexels-key"
        body = {
            "top_text": "普通用户素材策略",
            "bottom_text": "没有素材时全部使用公网",
            "material_policy": "owned_public",
            "bgm": False,
        }
        selected = [{
            "scene_id": "media_%02d" % index,
            "record_id": "pexels-%d" % index,
            "sha256": format(index, "064x"),
            "media_type": "video", "provider": "pexels",
            "clip_id": format(index + 100, "064x"),
            "clip_start_seconds": 0.0,
            "clip_duration_seconds": 8 / 3,
            "clip_slot_index": 1, "clip_slot_count": 1,
        } for index in range(1, 4)]
        with mock.patch.object(
            self.service, "_library_request",
            side_effect=AssertionError("owned_public must not use library"),
        ), mock.patch.object(
            self.service, "_select_pexels_materials", return_value=selected,
        ) as pexels:
            accepted = self.service.submit(body, "owned-public-no-user")
            payload = json.loads(self.service.store.get(accepted["job_id"])["payload"])
            result = self.service._select_materials(payload, accepted["job_id"])
            replay = self.service.submit(body, "owned-public-no-user")
            replay_result = self.service._select_materials(
                payload, replay["job_id"],
            )
        self.assertEqual(3, len(result))
        self.assertEqual(accepted["job_id"], replay["job_id"])
        self.assertEqual(result, replay_result)
        self.assertEqual({"pexels"}, {item["provider"] for item in result})
        pexels.assert_called_once()

    def test_owned_public_user_materials_must_exist_and_be_visual(self):
        self.service.pexels_api_key = "test-pexels-key"
        nonvisual_sha = self._store_user_asset(b"owned-user-audio")
        cases = (
            ({"sha256": "f" * 64, "media_type": "video"}, "不存在"),
            ({"sha256": nonvisual_sha, "media_type": "audio"}, "图片或视频"),
        )
        for index, (material, message) in enumerate(cases):
            request_id = "owned-public-invalid-user-%d" % index
            with self.subTest(material=material), self.assertRaisesRegex(
                matrix.MatrixTemplateError, message
            ):
                self.service.submit({
                    "top_text": "普通用户素材校验",
                    "bottom_text": "素材必须存在且类型有效",
                    "material_policy": "owned_public",
                    "user_materials": [material],
                    "bgm": False,
                }, request_id)
            self.assertIsNone(
                self.service.store.get_by_request_id(request_id)
            )

    def test_owned_public_preflight_skips_shared_library(self):
        self.service.pexels_api_key = "test-pexels-key"
        user_sha = self._store_user_asset(b"owned-preflight-video")
        server, thread, base = self._start_server()
        try:
            body = json.dumps({
                "top_text": "普通用户预检策略",
                "bottom_text": "只检查自有和公共素材",
                "material_policy": "owned_public",
                "user_materials": [{
                    "sha256": user_sha, "media_type": "video",
                }],
                "bgm": False,
            }).encode()
            request = urllib.request.Request(
                base + "/v1/preflight", data=body, method="POST",
                headers={"Authorization": "Bearer api-token"},
            )
            with mock.patch.object(
                self.service, "_library_request",
                side_effect=AssertionError("owned_public must not use library"),
            ), urllib.request.build_opener(
                urllib.request.ProxyHandler({})
            ).open(request, timeout=3) as response:
                result = json.load(response)
            self.assertEqual("owned_public", result["payload"]["material_policy"])
            self.assertEqual([], self.service.store.pending_ids())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_owned_public_partial_user_materials_fill_from_pexels_only(self):
        self.service.pexels_api_key = "test-pexels-key"
        user_sha = self._store_user_asset(b"owned-user-image", ".jpg")
        body = {
            "top_text": "普通用户部分素材",
            "bottom_text": "剩余画面使用公共素材",
            "material_policy": "owned_public",
            "duration": 10,
            "user_materials": [{
                "sha256": user_sha, "media_type": "image",
            }],
            "bgm": False,
        }

        def pexels(scenes, _job_id, used_sha256=()):
            self.assertEqual(["media_02", "media_03", "media_04"], [
                scene["scene_id"] for scene in scenes
            ])
            self.assertIn(user_sha, used_sha256)
            return [{
                "scene_id": scene["scene_id"],
                "record_id": "pexels-%d" % index,
                "sha256": format(index, "064x"),
                "media_type": "video", "provider": "pexels",
                "clip_id": format(index + 100, "064x"),
                "clip_start_seconds": 0.0,
                "clip_duration_seconds": scene["clip_duration_seconds"],
                "clip_slot_index": 1, "clip_slot_count": 1,
            } for index, scene in enumerate(scenes, 1)]

        with mock.patch.object(
            self.service, "_library_request",
            side_effect=AssertionError("owned_public must not use library"),
        ), mock.patch.object(
            self.service, "_select_pexels_materials", side_effect=pexels,
        ) as select_pexels:
            accepted = self.service.submit(body, "owned-public-partial")
            payload = json.loads(
                self.service.store.get(accepted["job_id"])["payload"]
            )
            selected = self.service._select_materials(
                payload, accepted["job_id"]
            )

        self.assertEqual(1, select_pexels.call_count)
        self.assertEqual(
            ["user", "pexels", "pexels", "pexels"],
            [item["provider"] for item in selected],
        )
        self.assertEqual(
            ["media_01", "media_02", "media_03", "media_04"],
            [item["scene_id"] for item in selected],
        )

    def test_three_user_materials_plus_ten_second_template_adds_one_pexels(self):
        self.service.pexels_api_key = "test-pexels-key"
        materials = [{
            "sha256": self._store_user_asset(
                ("owned-%d" % index).encode(), ".mp4",
            ),
            "media_type": "video",
        } for index in range(3)]
        body = {
            "top_text": "十秒模板三份素材",
            "bottom_text": "只补一个公网画面",
            "material_policy": "owned_public",
            "user_materials": materials,
            "duration": 10,
            "bgm": False,
        }
        with mock.patch.object(
            self.service, "_library_request",
            side_effect=AssertionError("owned_public must not use library"),
        ), mock.patch.object(
            self.service, "_select_pexels_materials",
            return_value=[{
                "scene_id": "media_04", "record_id": "pexels-4",
                "sha256": "f" * 64, "media_type": "video",
                "provider": "pexels", "clip_id": "e" * 64,
                "clip_start_seconds": 0.0, "clip_duration_seconds": 2.5,
                "clip_slot_index": 1, "clip_slot_count": 1,
            }],
        ) as pexels:
            accepted = self.service.submit(body, "owned-public-three-plus-one")
            payload = json.loads(self.service.store.get(accepted["job_id"])["payload"])
            selected = self.service._select_materials(payload, accepted["job_id"])
        self.assertEqual(["user", "user", "user", "pexels"], [
            item["provider"] for item in selected
        ])
        self.assertEqual(1, len(pexels.call_args.args[0]))

    def test_owned_public_rejects_too_many_user_materials(self):
        self.service.pexels_api_key = "test-pexels-key"
        materials = [{
            "sha256": self._store_user_asset(
                ("overflow-%d" % index).encode(), ".mp4",
            ),
            "media_type": "video",
        } for index in range(5)]
        with self.assertRaisesRegex(matrix.MatrixTemplateError, "需要 4 个"):
            self.service.submit({
                "top_text": "十秒模板素材超限",
                "bottom_text": "最多只能使用四个",
                "duration": 10, "bgm": False,
                "material_policy": "owned_public",
                "user_materials": materials,
            }, "owned-public-too-many")

    def test_required_visuals_uses_duration_and_fixed_template_contracts(self):
        self.service.reference_templates["ref-test"] = {
            "required_visuals": 3, "required_visuals_max": 5,
        }
        self.assertEqual([3, 3, 4, 4, 5, 5], [
            self.service.required_visuals({
                "template_id": "ref-test", "duration": duration,
            })
            for duration in (8, 9, 10, 12, 13, 15)
        ])
        self.assertEqual(9, self.service.required_visuals({
            "template_id": matrix.NINE_GRID_TEMPLATE_ID, "duration": 8,
        }))
        self.assertEqual(
            matrix.FIXED_SKILL_TEMPLATE_CONFIGS[
                matrix.TRIPLE_STRIP_TEMPLATE_ID
            ]["required_visuals"],
            self.service.required_visuals({
                "template_id": matrix.TRIPLE_STRIP_TEMPLATE_ID,
                "duration": 8,
            }),
        )

    def test_owned_public_manifest_contains_only_user_and_pexels_sources(self):
        payload = {
            "template_id": "full-overlay-bold", "duration": 10,
            "top_text": "素材清单", "bottom_text": "来源必须可审计",
            "bgm": False, "material_policy": "owned_public",
        }
        materials = [{
            "scene_id": "media_01", "sha256": "a" * 64,
            "media_type": "image", "provider": "user",
            "clip_start_seconds": 0.0, "clip_duration_seconds": 2.5,
        }] + [{
            "scene_id": "media_%02d" % index,
            "record_id": "pexels-%d" % index,
            "sha256": format(index, "064x"), "media_type": "video",
            "provider": "pexels", "provider_video_id": 1000 + index,
            "clip_start_seconds": float(index), "clip_duration_seconds": 2.5,
        } for index in range(2, 5)]
        manifest = self.service._material_manifest(payload, materials)
        self.assertEqual([1, 2, 3, 4], [item["slot"] for item in manifest])
        self.assertEqual(["user", "pexels", "pexels", "pexels"], [
            item["source"] for item in manifest
        ])
        self.assertFalse({"shared", "library", "yuelei"} & {
            item["source"] for item in manifest
        })
        self.assertEqual("a" * 64, manifest[0]["sha256"])
        self.assertEqual(1002, manifest[1]["pexels_id"])

    def test_pexels_fills_all_missing_slots_from_one_search(self):
        scenes = [{
            "scene_id": "media_%02d" % index,
            "clip_duration_seconds": 2.5,
        } for index in range(1, 5)]
        videos = [{
            "id": index, "duration": 20,
            "video_files": [{
                "id": index * 10, "file_type": "video/mp4",
                "width": 1080, "height": 1920, "quality": "hd",
                "link": "https://videos.pexels.com/%d.mp4" % index,
            }],
            "user": {"name": "creator", "url": "https://pexels.com/u"},
            "url": "https://pexels.com/video/%d" % index,
        } for index in range(1, 21)]
        with mock.patch.object(
            self.service, "_pexels_search", return_value={"videos": videos},
        ) as search:
            selected = self.service._select_pexels_materials(
                scenes, "a" * 32,
            )
        self.assertEqual(4, len(selected))
        search.assert_called_once()

    def test_user_material_clip_and_media_validation(self):
        image_sha = self._store_user_asset(b"image", ".jpg")
        video_sha = self._store_user_asset(b"video", ".mp4")
        with self.assertRaisesRegex(matrix.MatrixTemplateError, "图片素材不能设置"):
            self.service.submit({
                "top_text": "图片不能设置入点", "bottom_text": "请直接使用图片",
                "bgm": False, "material_policy": "owned_public",
                "user_materials": [{
                    "sha256": image_sha, "media_type": "image",
                    "clip_start_seconds": 1,
                }],
            }, "owned-image-start")
        with mock.patch.object(
            self.service, "_inspect_user_asset", return_value=2.0,
        ), self.assertRaisesRegex(matrix.MatrixTemplateError, "入点超过"):
            self.service.submit({
                "top_text": "视频入点越界", "bottom_text": "提交前明确拒绝",
                "bgm": False, "material_policy": "owned_public",
                "user_materials": [{
                    "sha256": video_sha, "media_type": "video",
                    "clip_start_seconds": 1,
                }],
            }, "owned-video-start")

    def test_real_media_inspection_rejects_wrong_mime(self):
        bad_image = self.service.data_root / "wrong.mp4"
        bad_image.parent.mkdir(parents=True, exist_ok=True)
        bad_image.write_bytes(b"not-a-video")
        with self.assertRaisesRegex(matrix.MatrixTemplateError, "无法读取"):
            matrix.MatrixTemplateService._inspect_user_asset(bad_image, "video")

    def test_owned_public_rejects_shared_bgm_before_acceptance(self):
        self.service.pexels_api_key = "test-pexels-key"
        user_sha = self._store_user_asset(b"owned-user-video")
        body = {
            "top_text": "普通用户背景音乐",
            "bottom_text": "禁止共享素材降级",
            "material_policy": "owned_public",
            "user_materials": [{
                "sha256": user_sha, "media_type": "video",
            }],
            "bgm": True,
        }
        with mock.patch.object(
            self.service, "_library_request",
            side_effect=AssertionError("owned_public must not use library"),
        ), self.assertRaisesRegex(matrix.MatrixTemplateError, "共享背景音乐"):
            self.service.submit(body, "owned-public-bgm")
        self.assertIsNone(
            self.service.store.get_by_request_id("owned-public-bgm")
        )

    def test_owned_public_rejects_missing_pexels_before_acceptance(self):
        user_sha = self._store_user_asset(b"owned-user-video")
        body = {
            "top_text": "普通用户公共素材",
            "bottom_text": "公共素材不可用就拒绝",
            "material_policy": "owned_public",
            "user_materials": [{
                "sha256": user_sha, "media_type": "video",
            }],
            "bgm": False,
        }
        with mock.patch.object(
            self.service, "_library_request",
            side_effect=AssertionError("owned_public must not use library"),
        ), self.assertRaisesRegex(matrix.MatrixTemplateError, "Pexels"):
            self.service.submit(body, "owned-public-no-pexels")
        self.assertIsNone(
            self.service.store.get_by_request_id("owned-public-no-pexels")
        )

    def test_shared_exact_user_materials_keep_existing_source_bypass(self):
        materials = []
        for index, media_type in enumerate(("video", "image", "video"), 1):
            suffix = ".jpg" if media_type == "image" else ".mp4"
            materials.append({
                "sha256": self._store_user_asset(
                    ("shared-user-%d" % index).encode(), suffix,
                ),
                "media_type": media_type,
            })
        payload = self.service.validate_payload({
            "top_text": "开发测试共享策略",
            "bottom_text": "完整自带素材保持兼容",
            "user_materials": materials,
            "bgm": False,
        })
        payload = self.service._freeze_font_provenance("a" * 32, payload)
        with mock.patch.object(
            self.service, "_library_request",
            side_effect=AssertionError("exact user materials bypass library"),
        ), mock.patch.object(
            self.service, "_select_pexels_materials",
            side_effect=AssertionError("exact user materials bypass Pexels"),
        ):
            selected = self.service._select_materials(payload, "a" * 32)
        self.assertEqual("shared", payload["material_policy"])
        self.assertEqual(["user", "user", "user"], [
            item["provider"] for item in selected
        ])

    def test_user_asset_endpoint_requires_authorization(self):
        server, thread, base = self._start_server()
        try:
            body = b"raw-bytes"
            sha = hashlib.sha256(body).hexdigest()
            req = urllib.request.Request(
                base + "/v1/user-assets", data=body, method="POST",
            )
            req.add_header("X-HQ-Asset-Sha256", sha)
            req.add_header("Content-Type", "image/jpeg")
            with self.assertRaises(urllib.error.HTTPError) as denied:
                urllib.request.urlopen(req, timeout=3)
            self.assertEqual(401, denied.exception.code)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_user_asset_endpoint_rejects_missing_or_invalid_sha(self):
        server, thread, base = self._start_server()
        try:
            req = urllib.request.Request(
                base + "/v1/user-assets", data=b"x", method="POST",
            )
            req.add_header("Authorization", "Bearer api-token")
            req.add_header("Content-Type", "image/jpeg")
            with self.assertRaises(urllib.error.HTTPError) as missing:
                urllib.request.urlopen(req, timeout=3)
            self.assertEqual(400, missing.exception.code)
            req = urllib.request.Request(
                base + "/v1/user-assets", data=b"x", method="POST",
            )
            req.add_header("Authorization", "Bearer api-token")
            req.add_header("X-HQ-Asset-Sha256", "../not-a-sha256")
            req.add_header("Content-Type", "image/jpeg")
            with self.assertRaises(urllib.error.HTTPError) as invalid:
                urllib.request.urlopen(req, timeout=3)
            self.assertEqual(400, invalid.exception.code)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_user_asset_endpoint_rejects_unsupported_content_type(self):
        server, thread, base = self._start_server()
        try:
            body = b"raw-bytes"
            sha = hashlib.sha256(body).hexdigest()
            req = urllib.request.Request(
                base + "/v1/user-assets", data=body, method="POST",
            )
            req.add_header("Authorization", "Bearer api-token")
            req.add_header("X-HQ-Asset-Sha256", sha)
            req.add_header("Content-Type", "audio/mpeg")
            with self.assertRaises(urllib.error.HTTPError) as unsupported:
                urllib.request.urlopen(req, timeout=3)
            self.assertEqual(400, unsupported.exception.code)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_user_asset_endpoint_stores_and_serves_file(self):
        server, thread, base = self._start_server()
        try:
            body = b"\xff\xd8\xffuser-jpeg-payload"
            sha = hashlib.sha256(body).hexdigest()
            req = urllib.request.Request(
                base + "/v1/user-assets", data=body, method="POST",
            )
            req.add_header("Authorization", "Bearer api-token")
            req.add_header("X-HQ-Asset-Sha256", sha)
            req.add_header("Content-Type", "image/jpeg")
            with urllib.request.urlopen(req, timeout=3) as response:
                result = json.load(response)
            self.assertTrue(result["ok"])
            self.assertEqual(sha, result["sha256"])
            self.assertEqual(len(body), result["bytes"])
            path = self.service.user_asset_path(sha)
            self.assertIsNotNone(path)
            self.assertEqual(sha + ".jpg", path.name)
            self.assertEqual(body, path.read_bytes())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_user_asset_endpoint_rejects_sha_mismatch_without_leaking_files(self):
        server, thread, base = self._start_server()
        try:
            body = b"actual-bytes"
            req = urllib.request.Request(
                base + "/v1/user-assets", data=body, method="POST",
            )
            req.add_header("Authorization", "Bearer api-token")
            req.add_header("X-HQ-Asset-Sha256", "f" * 64)
            req.add_header("Content-Type", "image/jpeg")
            with self.assertRaises(urllib.error.HTTPError) as mismatch:
                urllib.request.urlopen(req, timeout=3)
            self.assertEqual(400, mismatch.exception.code)
            root = self.service.data_root / matrix.USER_ASSET_DIRNAME
            self.assertEqual([], list(root.glob("*")) if root.is_dir() else [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_user_asset_endpoint_rejects_oversize(self):
        server, thread, base = self._start_server()
        try:
            with mock.patch.object(matrix, "MAX_USER_ASSET_BYTES", 4):
                body = b"12345"
                sha = hashlib.sha256(body).hexdigest()
                req = urllib.request.Request(
                    base + "/v1/user-assets", data=body, method="POST",
                )
                req.add_header("Authorization", "Bearer api-token")
                req.add_header("X-HQ-Asset-Sha256", sha)
                req.add_header("Content-Type", "image/jpeg")
                with self.assertRaises(urllib.error.HTTPError) as oversize:
                    urllib.request.urlopen(req, timeout=3)
                self.assertEqual(400, oversize.exception.code)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_user_asset_path_rejects_invalid_sha(self):
        self.assertIsNone(self.service.user_asset_path("../etc/passwd"))
        self.assertIsNone(self.service.user_asset_path("abc"))
        self.assertIsNone(self.service.user_asset_path("f" * 63))

    def test_download_user_asset_reuses_local_file_and_validates_sha(self):
        body = b"user-video-bytes"
        sha = hashlib.sha256(body).hexdigest()
        root = self.service.data_root / matrix.USER_ASSET_DIRNAME
        root.mkdir(parents=True)
        (root / (sha + ".mp4")).write_bytes(body)
        target = self.root / "jobs" / "j1" / "assets"
        target.mkdir(parents=True)
        downloaded = self.service._download_user_asset(
            {"sha256": sha, "provider": matrix.MATERIAL_PROVIDER_USER}, target,
        )
        self.assertEqual(sha + ".mp4", downloaded.name)
        self.assertEqual(body, downloaded.read_bytes())

    def test_download_user_asset_rejects_tampered_file(self):
        sha = hashlib.sha256(b"expected-bytes").hexdigest()
        root = self.service.data_root / matrix.USER_ASSET_DIRNAME
        root.mkdir(parents=True)
        (root / (sha + ".mp4")).write_bytes(b"tampered")
        target = self.root / "jobs" / "j1" / "assets"
        target.mkdir(parents=True)
        with self.assertRaisesRegex(matrix.MatrixTemplateError, "校验失败"):
            self.service._download_user_asset(
                {"sha256": sha, "provider": matrix.MATERIAL_PROVIDER_USER}, target,
            )

    def test_cleanup_user_assets_removes_expired_and_keeps_recent(self):
        root = self.service.data_root / matrix.USER_ASSET_DIRNAME
        root.mkdir(parents=True)
        now = int(time.time())
        expired = root / ("e" * 64 + ".jpg")
        recent = root / ("a" * 64 + ".png")
        expired.write_bytes(b"old")
        recent.write_bytes(b"new")
        retention = self.service.retention_seconds
        os.utime(expired, (now - retention - 60, now - retention - 60))
        os.utime(recent, (now, now))
        removed = self.service.cleanup_user_assets(now=now)
        self.assertEqual(1, removed)
        self.assertFalse(expired.exists())
        self.assertTrue(recent.exists())


if __name__ == "__main__":
    unittest.main()
