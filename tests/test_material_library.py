from __future__ import annotations

import concurrent.futures
import collections
import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from server import material_library as material_library_module
from server.material_library import MaterialLibrary, MaterialLibraryError, MaterialShortageError


class MaterialLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "files").mkdir()
        self.rows = []

    def tearDown(self):
        self.temp.cleanup()

    def add(self, name, *, media=".jpg", status="可使用", direction="竖屏", **fields):
        payload = f"asset:{name}".encode()
        digest = hashlib.sha256(payload).hexdigest()
        path = Path("files") / f"{name}{media}"
        (self.root / path).write_bytes(payload)
        row = {
            "record_id": name,
            "sha256": digest,
            "素材名称": name,
            "状态": status,
            "画面方向": direction,
            "server_relative_path": path.as_posix(),
            **fields,
        }
        self.rows.append(row)
        return digest

    def library(self, usage_path=None):
        (self.root / "index.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in self.rows),
            encoding="utf-8",
        )
        return MaterialLibrary(self.root, usage_path=usage_path)

    def test_exact_loose_random_and_no_duplicates(self):
        exact = self.add("exact", 标签=["医美", "抗衰"], 一级场景="美容院")
        loose = self.add("loose", 标签=["科技"])
        random = self.add("random", 标签=["风景"])
        result = self.library().select(
            [
                {"scene_id": "s1", "query": "医美 抗衰", "media_type": "image"},
                {"scene_id": "s2", "query": "科技", "media_type": "image"},
                {"scene_id": "s3", "query": "不存在", "media_type": "image"},
            ],
            seed="job-1",
        )
        self.assertEqual([exact, loose, random], [item["sha256"] for item in result["materials"]])
        self.assertEqual(["exact", "loose", "random"], [item["match_level"] for item in result["materials"]])
        self.assertEqual(3, len(set(result["used_sha256"])))
        self.assertFalse(result["ai_fallback"])

    def test_used_sha_is_never_reselected(self):
        first = self.add("first", 标签=["产品"])
        second = self.add("second", 标签=["产品"])
        result = self.library().select(
            [{"scene_id": "s1", "query": "产品", "media_type": "image"}],
            used_sha256=[first],
        )
        self.assertEqual(second, result["materials"][0]["sha256"])

    def test_same_orientation_is_preferred_before_cross_orientation_exact_match(self):
        self.add("blocked", status="待审核", 标签=["人物"])
        self.add("wide", direction="横屏", 标签=["人物", "医生"], 一级场景="诊室")
        portrait = self.add("portrait", 标签=["人物"])
        result = self.library().select(
            [{"scene_id": "s1", "query": "人物 医生 诊室", "media_type": "image"}],
            orientation="竖屏",
        )
        self.assertEqual(portrait, result["materials"][0]["sha256"])
        self.assertEqual("same", result["materials"][0]["orientation_match"])

    def test_cross_orientation_fallback_fills_unique_scenes_without_ai(self):
        portrait = self.add("portrait", direction="竖屏", 标签=["人物"])
        wide_a = self.add("wide-a", direction="横屏", 标签=["门店"])
        wide_b = self.add("wide-b", direction="横屏", 标签=["产品"])
        result = self.library().select([
            {"scene_id": "s1", "query": "人物", "media_type": "image"},
            {"scene_id": "s2", "query": "门店", "media_type": "image"},
            {"scene_id": "s3", "query": "产品", "media_type": "image"},
        ], orientation="portrait", seed="fallback")
        self.assertEqual(
            {portrait, wide_a, wide_b},
            {item["sha256"] for item in result["materials"]},
        )
        self.assertEqual(
            ["same", "fallback", "fallback"],
            [item["orientation_match"] for item in result["materials"]],
        )
        self.assertFalse(result["ai_fallback"])

    def test_video_image_and_bgm_types_are_supported(self):
        image = self.add("still", media=".png", 标签=["产品"])
        video = self.add("motion", media=".mp4", 标签=["产品"])
        bgm = self.add("music", media=".mp3", 标签=["轻快"])
        result = self.library().select(
            [
                {"scene_id": "s1", "query": "产品", "media_type": "image"},
                {"scene_id": "s2", "query": "产品", "media_type": "video"},
                {"scene_id": "music", "query": "轻快", "media_type": "bgm"},
            ]
        )
        self.assertEqual([image, video, bgm], [item["sha256"] for item in result["materials"]])

    def test_shortage_fails_without_ai_fallback(self):
        only = self.add("only", 标签=["产品"])
        with self.assertRaises(MaterialShortageError):
            self.library().select(
                [
                    {"scene_id": "s1", "query": "产品", "media_type": "image"},
                    {"scene_id": "s2", "query": "产品", "media_type": "image"},
                ],
                used_sha256=[only],
            )

    def test_path_escape_is_rejected(self):
        outside = self.root.parent / "outside.jpg"
        outside.write_bytes(b"outside")
        self.rows.append(
            {
                "record_id": "escape",
                "sha256": hashlib.sha256(b"outside").hexdigest(),
                "素材名称": "escape",
                "状态": "可使用",
                "server_relative_path": "../outside.jpg",
            }
        )
        with self.assertRaises(MaterialLibraryError):
            self.library().refresh()

    def test_selection_is_deterministic_for_same_seed(self):
        for name in ("a", "b", "c"):
            self.add(name, 标签=["无关"])
        library = self.library()
        scene = [{"scene_id": "s1", "query": "随机", "media_type": "image"}]
        first = library.select(scene, seed="same")["materials"][0]["sha256"]
        second = library.select(scene, seed="same")["materials"][0]["sha256"]
        self.assertEqual(first, second)

    def test_random_mode_ignores_semantic_scores_and_is_deterministic(self):
        exact = self.add("exact", 标签=["产品", "获客"])
        other_a = self.add("other-a", 标签=["风景"])
        other_b = self.add("other-b", 标签=["办公"])
        shas = (exact, other_a, other_b)
        seed = next(
            str(index) for index in range(100)
            if min(
                shas,
                key=lambda sha: hashlib.sha256(
                    f"{index}:s1:0:{sha}".encode("utf-8")
                ).hexdigest(),
            ) != exact
        )
        library = self.library()
        scene = [{
            "scene_id": "s1", "query": "产品 获客",
            "media_type": "image",
        }]
        first = library.select(
            scene, seed=seed, selection_mode="random",
        )
        second = library.select(
            scene, seed=seed, selection_mode="random",
        )
        self.assertEqual(first, second)
        self.assertNotEqual(exact, first["materials"][0]["sha256"])
        self.assertEqual("random", first["materials"][0]["match_level"])
        self.assertEqual(0, first["materials"][0]["match_score"])
        self.assertEqual("random", first["selection_mode"])
        self.assertEqual(
            ["random_all_orientations_unique"], first["fallback_policy"]
        )

    def test_random_mode_can_select_cross_orientation_from_full_pool(self):
        portrait = self.add(
            "portrait", direction="竖屏", 标签=["人物"],
        )
        wide = self.add(
            "wide", direction="横屏", 标签=["人物"],
        )
        seed = next(
            str(index) for index in range(100)
            if min(
                (portrait, wide),
                key=lambda sha: hashlib.sha256(
                    f"{index}:s1:0:{sha}".encode("utf-8")
                ).hexdigest(),
            ) == wide
        )

        result = self.library().select(
            [{"scene_id": "s1", "query": "人物", "media_type": "image"}],
            orientation="竖屏", seed=seed, selection_mode="random",
        )

        self.assertEqual(wide, result["materials"][0]["sha256"])
        self.assertEqual("fallback", result["materials"][0]["orientation_match"])

    def test_random_mode_eventually_reaches_every_eligible_video(self):
        expected = {
            self.add(
                f"video-{index}", media=".mp4",
                direction="竖屏" if index < 2 else "横屏",
            )
            for index in range(10)
        }
        library = self.library()
        called = set()
        for index in range(500):
            result = library.select(
                [{"scene_id": "s1", "media_type": "video"}],
                orientation="竖屏", seed=f"job-{index}",
                selection_mode="random",
            )
            called.add(result["materials"][0]["sha256"])
            if called == expected:
                break

        self.assertEqual(expected, called)

    def test_round_robin_persists_before_return_and_survives_restart(self):
        first = self.add("round-robin-a", media=".mp4")
        second = self.add("round-robin-b", media=".mp4")
        usage_path = self.root / "state" / "usage.json"
        usage_path.parent.mkdir()
        scene = [{"scene_id": "s1", "media_type": "video"}]
        library = self.library(usage_path)

        selected_first = library.select(
            scene, seed="same", selection_mode="round_robin",
        )["materials"][0]["sha256"]
        self.assertTrue(usage_path.is_file())

        restarted = MaterialLibrary(self.root, usage_path=usage_path)
        selected_second = restarted.select(
            scene, seed="same", selection_mode="round_robin",
        )["materials"][0]["sha256"]

        self.assertEqual({first, second}, {selected_first, selected_second})

    def test_round_robin_selection_and_persistence_are_one_critical_section(self):
        for index in range(3):
            self.add(f"round-robin-{index}", media=".mp4")
        usage_path = self.root / "state" / "usage.json"
        usage_path.parent.mkdir()
        library = self.library(usage_path)
        scene = [{"scene_id": "s1", "media_type": "video"}]
        real_available = library._is_available

        def slow_available(material):
            time.sleep(0.02)
            return real_available(material)

        with mock.patch.object(
            library, "_is_available", side_effect=slow_available,
        ), concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            selected = list(pool.map(
                lambda _index: library.select(
                    scene, seed="same", selection_mode="round_robin",
                )["materials"][0]["sha256"],
                range(6),
            ))

        self.assertEqual([2, 2, 2], sorted(collections.Counter(selected).values()))
        persisted = json.loads(usage_path.read_text(encoding="utf-8"))
        self.assertEqual([2, 2, 2], sorted(
            int(item["count"]) for item in persisted.values()
        ))

    def test_random_and_round_robin_requests_do_not_deadlock_each_other(self):
        for index in range(4):
            self.add(f"mixed-mode-{index}", media=".mp4")
        usage_path = self.root / "state" / "usage.json"
        usage_path.parent.mkdir()
        library = self.library(usage_path)
        scene = [{"scene_id": "s1", "media_type": "video"}]

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = [
                pool.submit(
                    library.select, scene, seed=f"job-{index}",
                    selection_mode=(
                        "round_robin" if index % 2 else "random"
                    ),
                )
                for index in range(8)
            ]
            results = [future.result(timeout=3) for future in futures]

        self.assertEqual(8, len(results))

    def test_round_robin_save_failure_is_visible_and_rolls_back_memory(self):
        for index in range(2):
            self.add(f"save-failure-{index}", media=".mp4")
        usage_path = self.root / "state" / "usage.json"
        usage_path.parent.mkdir()
        library = self.library(usage_path)
        scene = [{"scene_id": "s1", "media_type": "video"}]

        with mock.patch.object(
            material_library_module.os, "replace",
            side_effect=PermissionError("read only"),
        ), self.assertRaisesRegex(MaterialLibraryError, "usage state"):
            library.select(
                scene, seed="same", selection_mode="round_robin",
            )

        self.assertFalse(usage_path.exists())
        selected = library.select(
            scene, seed="same", selection_mode="round_robin",
        )["materials"][0]["sha256"]
        restarted = MaterialLibrary(self.root, usage_path=usage_path)
        self.assertNotEqual(
            selected,
            restarted.select(
                scene, seed="same", selection_mode="round_robin",
            )["materials"][0]["sha256"],
        )

    def test_invalid_usage_state_fails_closed_without_touching_the_index(self):
        self.add("valid-index", media=".mp4")
        usage_path = self.root / "state" / "usage.json"
        usage_path.parent.mkdir()
        usage_path.write_text('{"not-a-sha":{"count":1}}', encoding="utf-8")
        self.library()

        with self.assertRaisesRegex(MaterialLibraryError, "usage state"):
            MaterialLibrary(self.root, usage_path=usage_path)

        self.assertTrue((self.root / "index.jsonl").is_file())
        self.assertEqual(
            '{"not-a-sha":{"count":1}}',
            usage_path.read_text(encoding="utf-8"),
        )

    def test_corrupted_exact_match_falls_back_to_healthy_material(self):
        self.add("corrupted", 标签=["产品", "获客"])
        healthy = self.add("healthy", 标签=["风景"])
        (self.root / "files/corrupted.jpg").write_bytes(b"tampered")
        result = self.library().select([{
            "scene_id": "s1", "query": "产品 获客",
            "media_type": "image",
        }], seed="health-check")
        self.assertEqual(healthy, result["materials"][0]["sha256"])
        self.assertEqual("random", result["materials"][0]["match_level"])

    def test_atomic_replacement_during_hash_never_caches_old_inode_as_healthy(self):
        self.add("replaced", 标签=["产品", "获客"])
        healthy = self.add("healthy-replacement", 标签=["风景"])
        library = self.library()
        target = self.root / "files/replaced.jpg"
        original_sha256_file = material_library_module.sha256_file
        replaced = False

        def replace_after_hash(path):
            nonlocal replaced
            digest = original_sha256_file(path)
            if not replaced and Path(path) == target:
                replacement = self.root / "files/replacement.part"
                replacement.write_bytes(b"tampered")
                replacement.replace(target)
                replaced = True
            return digest

        with mock.patch.object(
            material_library_module, "sha256_file",
            side_effect=replace_after_hash,
        ):
            result = library.select([{
                "scene_id": "s1", "query": "产品 获客",
                "media_type": "image",
            }], seed="atomic-replacement")
        self.assertTrue(replaced)
        self.assertEqual(healthy, result["materials"][0]["sha256"])

    def test_first_concurrent_selection_hashes_one_sha_only_once(self):
        expected = self.add("single-flight", 标签=["随机"])
        library = self.library()
        original_sha256_file = material_library_module.sha256_file
        calls = 0

        def slow_hash(path):
            nonlocal calls
            calls += 1
            time.sleep(0.05)
            return original_sha256_file(path)

        scene = [{
            "scene_id": "s1", "query": "随机", "media_type": "image",
        }]
        with mock.patch.object(
            material_library_module, "sha256_file", side_effect=slow_hash,
        ), concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
            values = list(pool.map(
                lambda _index: library.select(
                    scene, seed="single-flight", selection_mode="random",
                )["materials"][0]["sha256"],
                range(16),
            ))
        self.assertEqual([expected] * 16, values)
        self.assertEqual(1, calls)

    def test_scene_contract_rejects_non_objects_and_more_than_twenty_one(self):
        self.add("only", 标签=["库存"])
        library = self.library()
        with self.assertRaisesRegex(ValueError, "object"):
            library.select(["bad"])
        with self.assertRaisesRegex(ValueError, "21"):
            library.select([{"scene_id": str(index)} for index in range(22)])
        with self.assertRaisesRegex(ValueError, "selection_mode"):
            library.select([{"scene_id": "s1"}], selection_mode="weighted")

    def test_real_export_schema_accepts_uppercase_sha_and_subject_alias(self):
        payload = b"real-export"
        digest = hashlib.sha256(payload).hexdigest()
        path = Path("files") / "real-export.jpg"
        (self.root / path).write_bytes(payload)
        self.rows.append({
            "record_id": "real-export",
            "SHA256": digest,
            "素材名称": "real-export",
            "状态": "可使用",
            "画面方向": "竖屏",
            "画面主体": "专业医生",
            "server_relative_path": path.as_posix(),
        })
        self.add("unrelated", 标签=["自然风景"])

        result = self.library().select(
            [{"scene_id": "s1", "query": "专业医生", "media_type": "image"}],
            orientation="portrait",
        )

        self.assertEqual(digest, result["materials"][0]["sha256"])
        self.assertEqual("loose", result["materials"][0]["match_level"])

    def test_conflicting_sha_aliases_fail_closed(self):
        payload = b"conflicting-export"
        path = Path("files") / "conflicting-export.jpg"
        (self.root / path).write_bytes(payload)
        self.rows.append({
            "record_id": "conflicting-export",
            "sha256": hashlib.sha256(payload).hexdigest(),
            "SHA256": "f" * 64,
            "素材名称": "conflicting-export",
            "状态": "可使用",
            "画面方向": "竖屏",
            "server_relative_path": path.as_posix(),
        })

        with self.assertRaisesRegex(MaterialLibraryError, "conflicting material sha256"):
            self.library().refresh()

    def test_chinese_sentence_matches_individual_tags_without_spaces(self):
        expected = self.add("beauty", 标签=["医美", "抗衰"])
        self.add("other", 标签=["办公", "会议"])
        result = self.library().select(
            [{"scene_id": "s1", "query": "今天讲讲医美抗衰项目", "media_type": "image"}]
        )
        self.assertEqual(expected, result["materials"][0]["sha256"])
        self.assertEqual("exact", result["materials"][0]["match_level"])


if __name__ == "__main__":
    unittest.main()
