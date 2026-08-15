from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = (
    ROOT
    / "deploy"
    / "pixelle-video"
    / "overrides"
    / "pixelle_video"
    / "services"
    / "talking_material.py"
)


def load_module():
    if not MODULE_PATH.is_file():
        raise AssertionError(f"missing talking material override: {MODULE_PATH}")
    spec = importlib.util.spec_from_file_location("pixelle_talking_material_test", MODULE_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TalkingMaterialTests(unittest.TestCase):
    def test_missing_config_is_disabled(self):
        module = load_module()

        self.assertEqual(
            module.normalize_talking_material(None, ["scene_01"]),
            {
                "enabled": False,
                "ratio": 0.3,
                "default_avatar_asset_id": "",
                "scenes": [],
            },
        )

    def test_enabled_config_rejects_unknown_scene(self):
        module = load_module()

        with self.assertRaisesRegex(ValueError, "unknown scene_id"):
            module.normalize_talking_material(
                {
                    "enabled": True,
                    "ratio": 0.3,
                    "default_avatar_asset_id": "avatar_" + "a" * 32,
                    "scenes": [{"scene_id": "scene_99", "enabled": True}],
                },
                ["scene_01"],
            )

    def test_recommend_scene_ids_prioritize_edges_before_interior(self):
        module = load_module()

        scenes = [{"scene_id": f"scene_{index:02d}"} for index in range(1, 6)]

        self.assertEqual(
            module.recommend_scene_ids(scenes, 0.4),
            ["scene_01", "scene_05"],
        )

    def test_recommend_scene_ids_skip_center_adjacent_interior(self):
        module = load_module()

        scenes = [{"scene_id": f"scene_{index:02d}"} for index in range(1, 8)]

        self.assertEqual(
            module.recommend_scene_ids(scenes, 0.5),
            ["scene_01", "scene_07", "scene_04", "scene_02"],
        )

    def test_disabled_talking_windows_are_empty(self):
        module = load_module()

        self.assertEqual(
            module.build_talking_windows(
                [
                    {"text": "one", "duration": 1.4},
                    {"text": "two", "duration": 1.8},
                    {"text": "three", "duration": 2.7},
                ],
                enabled=False,
            ),
            [],
        )

    def test_four_two_second_cues_pack_toward_six_seconds(self):
        module = load_module()

        self.assertEqual(
            module.build_talking_windows(
                [
                    {"text": "one", "duration": 2.0},
                    {"text": "two", "duration": 2.0},
                    {"text": "three", "duration": 2.0},
                    {"text": "four", "duration": 2.0},
                ],
                enabled=True,
            ),
            [
                {"cue_start": 0, "cue_end": 3, "duration": 6.0},
                {"cue_start": 3, "cue_end": 4, "duration": 2.0},
            ],
        )

    def test_short_cues_are_packed_without_text_loss(self):
        module = load_module()

        self.assertEqual(
            module.build_talking_windows(
                [
                    {"text": "一", "duration": 1.4},
                    {"text": "二", "duration": 1.8},
                    {"text": "三", "duration": 2.7},
                ],
                enabled=True,
            ),
            [
                {"cue_start": 0, "cue_end": 3, "duration": 5.9},
            ],
        )


if __name__ == "__main__":
    unittest.main()
