from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

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

    def library(self):
        (self.root / "index.jsonl").write_text(
            "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in self.rows),
            encoding="utf-8",
        )
        return MaterialLibrary(self.root)

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
