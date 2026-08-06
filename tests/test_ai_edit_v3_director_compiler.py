from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from server.content_domains.ai_edit_v3.contracts import ContractError, canonical_json, validate_edit_plan
from server.content_domains.ai_edit_v3.director_compiler import compile_edit_plan
from server.content_domains.ai_edit_v3.director_layout_policy import layout_requirements_for
from server.content_domains.ai_edit_v3.overlay_catalog import load_overlay_placement_catalog


ROOT = Path(__file__).resolve().parents[1]
OVERLAY_CATALOG = load_overlay_placement_catalog(ROOT / "server" / "ai_edit_v3_renderer")
DECISION = json.loads((ROOT / "tests" / "fixtures" / "ai_edit_v3" / "director-decisions" / "varied-valid.json").read_text(encoding="utf-8"))
CAPTIONS = [{"id": f"caption_{index:03d}", "start_ms": (index - 1) * 5000, "end_ms": index * 5000, "text": f"第{index}段权威字幕"} for index in range(1, 6)]
CANDIDATES = [{"id": f"candidate_{index:02d}", "start_ms": (index - 1) * 5000, "end_ms": index * 5000, "caption_ids": [f"caption_{index:03d}"], "authoritative_text": f"第{index}段权威字幕", "protected_fact_ids": [], "available_material_ids": [], "speaker_available": True} for index in range(1, 6)]
CAPABILITIES = {"layout_capabilities": ["quote_reversal", "speaker_left_info_right", "number_proof", "steps_stack", "cta_offer"], "layout_variants": {"quote_reversal": ["diagonal_statement"], "speaker_left_info_right": ["speaker_focus"], "number_proof": ["metric_focus"], "steps_stack": ["stacked_steps"], "cta_offer": ["offer_hold"]}, "overlay_capabilities": ["standard_caption", "headline_block", "number_proof", "step_indicator", "cta_hold"], "animation_capabilities": ["wipe", "slide", "count_up", "stagger", "scale"], "transition_capabilities": ["hard_cut", "soft_wipe", "directional_slide"], "theme_capabilities": {"palette_id": ["midnight_gold"], "typography_id": ["editorial_sans"], "density": ["balanced"], "motion_energy": ["low", "medium", "high"], "image_fit": ["cover", "smart_crop"]}, "output_ratio": "9:16", "overlay_placement_budgets": OVERLAY_CATALOG}


class DirectorCompilerTests(unittest.TestCase):
    def compile(self):
        return compile_edit_plan(DECISION, candidates=CANDIDATES, timeline={"duration_ms": 25000, "captions": CAPTIONS, "ratio": "9:16"}, materials=[], capabilities=CAPABILITIES, variation_seed=11)

    def test_compiler_uses_candidates_for_timing_and_preserves_directives(self):
        plan = self.compile()
        self.assertEqual("1.0", plan["visual_program_version"])
        self.assertEqual([(item["start_ms"], item["end_ms"]) for item in CANDIDATES], [(scene["start_ms"], scene["end_ms"]) for scene in plan["scenes"]])
        self.assertEqual(["第1段权威字幕", "第2段权威字幕", "第3段权威字幕", "第4段权威字幕", "第5段权威字幕"], [scene["headline"]["text"] for scene in plan["scenes"]])
        self.assertEqual([item["layout_variant"] for item in DECISION["scene_directives"]], [item["layout_variant"] for item in plan["scenes"]])
        self.assertEqual([item["overlay_instances"] for item in DECISION["scene_directives"]], [item["overlay_instances"] for item in plan["scenes"]])
        self.assertEqual([item["transition"] for item in DECISION["scene_directives"]], [item["transition"] for item in plan["scenes"]])

    def test_compiler_preserves_the_decision_in_direction_in_the_visual_edit_plan(self):
        plan = self.compile()
        self.assertEqual("in", DECISION["scene_directives"][-1]["animations"][0]["direction"])
        self.assertEqual("in", plan["scenes"][-1]["animations"][0]["direction"])
        validate_edit_plan(plan, timeline={"duration_ms": 25000, "accurate_captions": CAPTIONS, **CAPABILITIES})

    def test_compiler_is_canonical_and_rejects_invented_caption_reference(self):
        first = self.compile()
        second = self.compile()
        self.assertEqual(canonical_json(first), canonical_json(second))
        invalid = copy.deepcopy(DECISION)
        invalid["scene_directives"][0]["headline"]["source_caption_ids"] = ["caption_999"]
        with self.assertRaisesRegex(ValueError, "director_text_reference_invalid"):
            compile_edit_plan(invalid, candidates=CANDIDATES, timeline={"duration_ms": 25000, "captions": CAPTIONS, "ratio": "9:16"}, materials=[], capabilities=CAPABILITIES, variation_seed=11)

    def test_visual_overlay_projection_rejects_missing_reordered_and_mismatched_ids(self):
        plan = self.compile()
        timeline = {"duration_ms": 25000, "accurate_captions": CAPTIONS, **CAPABILITIES}
        for mutate in (
            lambda value: value["scenes"][0].pop("overlay_instances"),
            lambda value: value["scenes"][0].update({"overlay_ids": ["cta_hold"]}),
            lambda value: value["scenes"][0].update({"overlay_instances": list(reversed(value["scenes"][0]["overlay_instances"]))}),
        ):
            value = copy.deepcopy(plan)
            mutate(value)
            with self.subTest(value=value), self.assertRaises(ContractError):
                validate_edit_plan(value, timeline=timeline)

    def test_compiler_rejects_illegal_overlay_placement_and_authority_over_budget(self):
        misplaced = copy.deepcopy(DECISION)
        misplaced["scene_directives"][0]["overlay_instances"][0]["placement"] = "lower_third"
        with self.assertRaisesRegex(ValueError, "director_overlay_placement_invalid"):
            compile_edit_plan(misplaced, candidates=CANDIDATES, timeline={"duration_ms": 25000, "captions": CAPTIONS, "ratio": "9:16"}, materials=[], capabilities=CAPABILITIES, variation_seed=11)

        oversized_captions = copy.deepcopy(CAPTIONS)
        oversized_captions[0]["text"] = "权" * 76
        with self.assertRaisesRegex(ValueError, "director_overlay_text_budget_exceeded"):
            compile_edit_plan(DECISION, candidates=CANDIDATES, timeline={"duration_ms": 25000, "captions": oversized_captions, "ratio": "9:16"}, materials=[], capabilities=CAPABILITIES, variation_seed=11)

    def test_semantic_material_slot_projects_the_frozen_renderer_layout_slot(self):
        decision = copy.deepcopy(DECISION)
        decision["scene_directives"][1]["material_slot_directives"] = [{
            "slot_id": "candidate_02_evidence",
            "semantic": "与第二段权威文案对应的证据图",
            "purpose": "evidence",
            "priority": "required",
            "ratio": "auto",
        }]
        capabilities = copy.deepcopy(CAPABILITIES)
        capabilities["layout_requirements"] = layout_requirements_for(
            capabilities["layout_capabilities"]
        )

        plan = compile_edit_plan(
            decision,
            candidates=CANDIDATES,
            timeline={"duration_ms": 25000, "captions": CAPTIONS, "ratio": "9:16"},
            materials=[],
            capabilities=capabilities,
            variation_seed=11,
        )

        self.assertEqual(
            "evidence",
            plan["scenes"][1]["material_slots"][0]["layout_slot_id"],
        )
        self.assertEqual(
            "candidate_02_evidence",
            plan["materials"][0]["request_id"],
        )


if __name__ == "__main__":
    unittest.main()
