from __future__ import annotations

import json
import unittest
from pathlib import Path

from server.content_domains.ai_edit_v3.director_compiler import compile_edit_plan
from server.content_domains.ai_edit_v3.overlay_catalog import load_overlay_placement_catalog


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "ai_edit_v3" / "director-decisions"


class VisualParityRegressionTests(unittest.TestCase):
    """Protect against the five-scene fixed visual combination seen in production."""

    def generate_from_fixture(self, name: str):
        decision = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
        captions = [
            {"id": f"caption_{index:03d}", "start_ms": (index - 1) * 5000,
             "end_ms": index * 5000, "text": f"第{index}段权威字幕"}
            for index in range(1, 6)
        ]
        candidates = [
            {"id": f"candidate_{index:02d}", "start_ms": (index - 1) * 5000,
             "end_ms": index * 5000, "caption_ids": [f"caption_{index:03d}"],
             "authoritative_text": f"第{index}段权威字幕", "protected_fact_ids": [],
             "available_material_ids": [], "speaker_available": True}
            for index in range(1, 6)
        ]
        capabilities = {
            "layout_capabilities": ["quote_reversal", "speaker_left_info_right", "number_proof", "steps_stack", "cta_offer"],
            "layout_variants": {"quote_reversal": ["diagonal_statement"], "speaker_left_info_right": ["speaker_focus"], "number_proof": ["metric_focus"], "steps_stack": ["stacked_steps"], "cta_offer": ["offer_hold"]},
            "overlay_capabilities": ["standard_caption", "headline_block", "number_proof", "step_indicator", "cta_hold"],
            "animation_capabilities": ["wipe", "slide", "count_up", "stagger", "scale"],
            "transition_capabilities": ["hard_cut", "soft_wipe", "directional_slide"],
            "theme_capabilities": {"palette_id": ["midnight_gold"], "typography_id": ["editorial_sans"], "density": ["balanced"], "motion_energy": ["low", "medium", "high"], "image_fit": ["cover", "smart_crop"]},
            "output_ratio": "16:9",
            "overlay_placement_budgets": load_overlay_placement_catalog(ROOT / "server" / "ai_edit_v3_renderer"),
        }
        return compile_edit_plan(decision, candidates=candidates, timeline={"duration_ms": 25000, "captions": captions, "ratio": "16:9"}, materials=[], capabilities=capabilities, variation_seed=7)

    def test_long_content_does_not_collapse_to_fixed_visual_combination(self):
        plan = self.generate_from_fixture("varied-valid.json")
        combinations = {
            (scene["layout_id"], scene["layout_variant"], tuple(item["component_id"] for item in scene["overlay_instances"]), tuple(item["preset"] for item in scene["animations"]), scene["transition"])
            for scene in plan["scenes"]
        }
        self.assertGreaterEqual(len(combinations), 4)
        self.assertNotEqual({"standard_caption"}, {item["component_id"] for scene in plan["scenes"] for item in scene["overlay_instances"]})

    def test_recorded_live_defect_shape_is_fixed_and_content_free(self):
        shape = json.loads((FIXTURES / "live-fixed-defect-shape.json").read_text(encoding="utf-8"))
        self.assertEqual(5, shape["scene_count"])
        self.assertEqual("midnight_gold", shape["palette_id"])
        for field, expected in (("layout_variants", ["balanced_a"]), ("overlay_ids", ["standard_caption"]), ("animation_presets", ["subtitle_pop"]), ("transitions", ["hard_cut"])):
            self.assertEqual(expected, shape[field])


if __name__ == "__main__":
    unittest.main()
