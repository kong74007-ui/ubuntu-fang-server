from __future__ import annotations

import copy
from dataclasses import replace
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from server.content_domains.ai_edit_v3.director_candidates import SceneCandidate
from server.content_domains.ai_edit_v3.director_decision import (
    DirectorDecisionError,
    ValidatedDecision,
    _apply_scoped_material_purpose_repair,
    _repair_expected_constraint,
    generate_director_decision,
    validate_director_decision,
)
from server.content_domains.ai_edit_v3.director_compiler import compile_edit_plan
from server.content_domains.ai_edit_v3.director_layout_policy import (
    MAX_REQUIRED_MATERIAL_SLOTS,
    MAX_TOTAL_MATERIAL_SLOTS,
    SCENE_STRUCTURE_POLICY,
    SPEAKER_VISIBILITY_POLICY,
    layout_requirements_for,
)
from server.content_domains.ai_edit_v3.contracts import LeaseClaim, canonical_json, request_fingerprint, schema_sha256
from server.content_domains.ai_edit_v3.providers.base import ProviderResult
from server.content_domains.ai_edit_v3.runtime import get_or_generate_director_decision
from server.content_domains.ai_edit_v3.overlay_catalog import load_overlay_placement_catalog
from server.content_domains.ai_edit_v3.store import LeaseLost, StoreConflictError, V3Store, open_store


CANDIDATES = (
    SceneCandidate("candidate_01", 0, 4000, ("caption_001",), "权威原文。", ("fact_001",), ("material_01",), True),
    SceneCandidate("candidate_02", 4000, 8000, ("caption_002",), "第二句。", (), (), True),
)
ROOT = Path(__file__).resolve().parents[1]
OVERLAY_CATALOG = load_overlay_placement_catalog(ROOT / "server" / "ai_edit_v3_renderer")
CAPABILITIES = {
    "layout_capabilities": ["quote_reversal", "speaker_fullscreen"],
    "layout_variants": {"quote_reversal": ["diagonal_statement"], "speaker_fullscreen": ["clean_center"]},
    "overlay_capabilities": ["headline_block", "standard_caption"],
    "overlay_variants": {"headline_block": ["primary"], "standard_caption": ["default"]},
    "overlay_animation_targets": {"headline_block": ["metric_value"], "standard_caption": []},
    "layout_animation_targets": {"quote_reversal": ["scene_root"], "speaker_fullscreen": []},
    "animation_capabilities": ["wipe", "fade"],
    "transition_capabilities": ["soft_wipe", "hard_cut"],
    "theme_profile_ids": ["editorial_clean"],
    "identity_match_capability": False,
    "output_ratio": "9:16",
    "overlay_placement_budgets": OVERLAY_CATALOG,
}


def valid_decision():
    return {
        "version": "1.0",
        "creative_concept": "以问题开场，再给出证据。",
        "narrative_pattern": "question_proof",
        "theme_profile_id": "editorial_clean",
        "design_intent": {"density": "balanced", "motion_energy": "medium", "image_fit": "smart_crop", "decoration_intensity": "medium"},
        "scene_directives": [
            {
                "scene_id": "candidate_01", "narrative_role": "hook", "layout_id": "quote_reversal", "layout_variant": "diagonal_statement",
                "headline": {"text_kind": "verbatim", "source_caption_ids": ["caption_001"]},
                "overlay_instances": [{"instance_id": "hook_headline", "component_id": "headline_block", "content_ref": "headline", "placement": "title_safe"}],
                "material_bindings": [{"slot_id": "primary", "material_id": "material_01", "required": False}],
                "material_slot_directives": [],
                "animations": [{"target_id": "hook_headline", "preset": "wipe", "direction": "left", "duration_ms": 520, "delay_ms": 80}],
                "transition": "soft_wipe", "sound_events": [{"role": "reversal", "priority": "optional", "offset_ms": 120}],
            },
            {
                "scene_id": "candidate_02", "narrative_role": "proof", "layout_id": "speaker_fullscreen", "layout_variant": "clean_center",
                "highlight": {"text_kind": "verbatim", "source_caption_ids": ["caption_002"]},
                "overlay_instances": [{"instance_id": "caption", "component_id": "standard_caption", "content_ref": "highlight", "placement": "subtitle_safe"}],
                "material_bindings": [],
                "material_slot_directives": [{"slot_id": "context_visual", "semantic": "结构化证据插画", "purpose": "evidence", "priority": "optional", "ratio": "auto"}],
                "animations": [{"target_id": "caption", "preset": "fade", "direction": "none", "duration_ms": 300, "delay_ms": 0}],
                "transition": "hard_cut", "sound_events": [],
            },
        ],
        "audio_intent": {"bgm_description": "克制的无歌词电子氛围", "energy": "medium", "dialogue_priority": True},
    }


def production_video_case(layouts=("speaker_fullscreen", "quote_reversal", "speaker_fullscreen")):
    candidates = tuple(
        SceneCandidate(
            f"candidate_{index:02d}",
            (index - 1) * 4000,
            index * 4000,
            (f"caption_{index:03d}",),
            f"authoritative sentence {index}",
            (),
            (),
            True,
            ((f"caption_{index:03d}", f"authoritative sentence {index}"),),
        )
        for index in range(1, 4)
    )
    capabilities = copy.deepcopy(CAPABILITIES)
    capabilities["material_binding_mode"] = "semantic_slots_only"
    capabilities["layout_requirements"] = layout_requirements_for(
        capabilities["layout_capabilities"]
    )
    capabilities["max_required_material_slots"] = MAX_REQUIRED_MATERIAL_SLOTS
    capabilities["max_total_material_slots"] = MAX_TOTAL_MATERIAL_SLOTS
    capabilities["speaker_visibility_policy"] = copy.deepcopy(
        SPEAKER_VISIBILITY_POLICY
    )
    capabilities["scene_structure_policy"] = copy.deepcopy(
        SCENE_STRUCTURE_POLICY
    )
    capabilities["theme_capabilities"] = {
        "palette_id": ["midnight_gold"],
        "typography_id": ["editorial_sans"],
        "density": ["airy", "balanced", "dense"],
        "motion_energy": ["low", "medium", "high"],
        "image_fit": ["contain", "cover", "smart_crop"],
    }
    directives = []
    for index, layout_id in enumerate(layouts, 1):
        directives.append({
            "scene_id": f"candidate_{index:02d}",
            "narrative_role": "hook" if index == 1 else "proof",
            "layout_id": layout_id,
            "layout_variant": (
                "clean_center"
                if layout_id == "speaker_fullscreen"
                else "diagonal_statement"
            ),
            "headline": {
                "text_kind": "verbatim",
                "source_caption_ids": [f"caption_{index:03d}"],
            },
            "overlay_instances": [{
                "instance_id": f"caption_{index:02d}",
                "component_id": "standard_caption",
                "content_ref": "headline",
                "placement": "subtitle_safe",
            }],
            "material_bindings": [],
            "material_slot_directives": [],
            "animations": [{
                "target_id": f"caption_{index:02d}",
                "preset": "fade",
                "direction": "none",
                "duration_ms": 300,
                "delay_ms": 0,
            }],
            "transition": "hard_cut",
            "sound_events": [],
        })
    decision = {
        "version": "1.0",
        "creative_concept": "speaker-led evidence rhythm",
        "narrative_pattern": "speaker_evidence",
        "theme_profile_id": "editorial_clean",
        "design_intent": {
            "density": "balanced",
            "motion_energy": "medium",
            "image_fit": "smart_crop",
            "decoration_intensity": "medium",
        },
        "scene_directives": directives,
        "audio_intent": {
            "bgm_description": "restrained instrumental bed",
            "energy": "medium",
            "dialogue_priority": True,
        },
    }
    return candidates, capabilities, decision


class DirectorDecisionValidationTests(unittest.TestCase):
    def test_valid_decision_is_canonical_and_complete(self):
        self.assertEqual(valid_decision(), validate_director_decision(valid_decision(), candidates=CANDIDATES, capabilities=CAPABILITIES))

    def test_short_scene_rejects_sound_events_and_accepts_an_empty_list(self):
        candidates = (replace(CANDIDATES[0], end_ms=400), CANDIDATES[1])
        decision = valid_decision()

        with self.assertRaisesRegex(
            DirectorDecisionError,
            "director_sound_event_timeline_invalid",
        ) as caught:
            validate_director_decision(
                decision,
                candidates=candidates,
                capabilities=CAPABILITIES,
            )
        self.assertEqual(
            "$.scene_directives[0].sound_events",
            caught.exception.path,
        )

        decision["scene_directives"][0]["sound_events"] = []
        self.assertEqual(
            decision,
            validate_director_decision(
                decision,
                candidates=candidates,
                capabilities=CAPABILITIES,
            ),
        )

    def test_decision_accepts_in_direction_for_a_declared_animation_target(self):
        value = valid_decision()
        value["scene_directives"][0]["animations"][0]["direction"] = "in"
        self.assertEqual(
            "in",
            validate_director_decision(
                value, candidates=CANDIDATES, capabilities=CAPABILITIES
            )["scene_directives"][0]["animations"][0]["direction"],
        )

    def test_decision_rejects_illegal_overlay_placement_and_authority_over_budget(self):
        misplaced = valid_decision()
        misplaced["scene_directives"][0]["overlay_instances"][0]["placement"] = "lower_third"
        with self.assertRaisesRegex(DirectorDecisionError, "director_overlay_placement_invalid"):
            validate_director_decision(misplaced, candidates=CANDIDATES, capabilities=CAPABILITIES)

        oversized = (replace(CANDIDATES[0], authoritative_text="权" * 76), CANDIDATES[1])
        with self.assertRaisesRegex(DirectorDecisionError, "director_overlay_text_budget_exceeded"):
            validate_director_decision(valid_decision(), candidates=oversized, capabilities=CAPABILITIES)

    def test_rejects_unknown_fields_code_paths_urls_and_over_limit_parameters(self):
        mutations = []
        value = valid_decision(); value["javascript"] = "alert(1)"; mutations.append(value)
        value = valid_decision(); value["creative_concept"] = "https://example.invalid/x"; mutations.append(value)
        value = valid_decision(); value["scene_directives"][0]["animations"][0]["duration_ms"] = 2001; mutations.append(value)
        for value in mutations:
            with self.subTest(value=value), self.assertRaises(DirectorDecisionError):
                validate_director_decision(value, candidates=CANDIDATES, capabilities=CAPABILITIES)

    def test_rejects_scene_component_variant_animation_transition_material_and_text_drift(self):
        changes = (
            ("scene_id", "candidate_12"), ("layout_id", "unknown_layout"),
            ("layout_variant", "unknown_variant"), ("transition", "card_match_cut"),
        )
        for key, replacement in changes:
            value = valid_decision(); value["scene_directives"][0][key] = replacement
            with self.subTest(key=key), self.assertRaises(DirectorDecisionError):
                validate_director_decision(value, candidates=CANDIDATES, capabilities=CAPABILITIES)
        for field, replacement in (("component_id", "unknown_component"), ("content_ref", "highlight")):
            value = valid_decision(); value["scene_directives"][0]["overlay_instances"][0][field] = replacement
            with self.subTest(field=field), self.assertRaises(DirectorDecisionError):
                validate_director_decision(value, candidates=CANDIDATES, capabilities=CAPABILITIES)
        value = valid_decision(); value["scene_directives"][0]["animations"][0]["preset"] = "unknown_animation"
        with self.assertRaises(DirectorDecisionError): validate_director_decision(value, candidates=CANDIDATES, capabilities=CAPABILITIES)
        value = valid_decision(); value["scene_directives"][0]["overlay_instances"][0]["variant"] = "unknown_variant"
        with self.assertRaises(DirectorDecisionError): validate_director_decision(value, candidates=CANDIDATES, capabilities=CAPABILITIES)
        value = valid_decision(); value["scene_directives"][0]["material_bindings"][0]["material_id"] = "material_99"
        with self.assertRaises(DirectorDecisionError): validate_director_decision(value, candidates=CANDIDATES, capabilities=CAPABILITIES)

    def test_animation_can_target_only_declared_public_layout_or_overlay_targets(self):
        value = valid_decision()
        value["scene_directives"][0]["overlay_instances"][0]["variant"] = "primary"
        value["scene_directives"][0]["animations"][0]["target_id"] = "metric_value"
        self.assertEqual(value, validate_director_decision(value, candidates=CANDIDATES, capabilities=CAPABILITIES))
        value["scene_directives"][0]["animations"][0]["target_id"] = "private_selector"
        with self.assertRaises(DirectorDecisionError):
            validate_director_decision(value, candidates=CANDIDATES, capabilities=CAPABILITIES)
        value = valid_decision(); value["scene_directives"][0]["headline"]["source_caption_ids"] = ["caption_002"]
        with self.assertRaises(DirectorDecisionError): validate_director_decision(value, candidates=CANDIDATES, capabilities=CAPABILITIES)

    def test_rejects_duplicate_or_missing_scenes_and_slot_collisions(self):
        value = valid_decision(); value["scene_directives"].pop()
        with self.assertRaises(DirectorDecisionError): validate_director_decision(value, candidates=CANDIDATES, capabilities=CAPABILITIES)
        value = valid_decision(); value["scene_directives"][1]["scene_id"] = "candidate_01"
        with self.assertRaises(DirectorDecisionError): validate_director_decision(value, candidates=CANDIDATES, capabilities=CAPABILITIES)
        value = valid_decision(); value["scene_directives"][0]["material_slot_directives"] = [{"slot_id": "primary", "semantic": "冲突", "purpose": "context", "priority": "required", "ratio": "auto"}]
        with self.assertRaises(DirectorDecisionError): validate_director_decision(value, candidates=CANDIDATES, capabilities=CAPABILITIES)
    def test_production_layout_policy_rejects_source_material_and_binding_drift(self):
        capabilities = copy.deepcopy(CAPABILITIES)
        capabilities["layout_capabilities"] = [
            "speaker_left_info_right", "speaker_fullscreen",
        ]
        capabilities["layout_variants"] = {
            "speaker_left_info_right": ["evidence_panel"],
            "speaker_fullscreen": ["clean_center"],
        }
        capabilities["layout_animation_targets"] = {
            "speaker_left_info_right": [], "speaker_fullscreen": [],
        }
        capabilities["material_binding_mode"] = "semantic_slots_only"
        capabilities["layout_requirements"] = layout_requirements_for(
            capabilities["layout_capabilities"]
        )
        capabilities["max_required_material_slots"] = MAX_REQUIRED_MATERIAL_SLOTS
        capabilities["max_total_material_slots"] = MAX_TOTAL_MATERIAL_SLOTS
        capabilities["speaker_visibility_policy"] = copy.deepcopy(
            SPEAKER_VISIBILITY_POLICY
        )
        capabilities["scene_structure_policy"] = copy.deepcopy(
            SCENE_STRUCTURE_POLICY
        )

        direct_binding = valid_decision()
        direct_binding["scene_directives"][0]["layout_id"] = "speaker_left_info_right"
        direct_binding["scene_directives"][0]["layout_variant"] = "evidence_panel"
        with self.assertRaisesRegex(
            DirectorDecisionError, "director_material_binding_forbidden"
        ):
            validate_director_decision(
                direct_binding, candidates=CANDIDATES, capabilities=capabilities
            )

        valid = valid_decision()
        valid["scene_directives"][0]["layout_id"] = "speaker_left_info_right"
        valid["scene_directives"][0]["layout_variant"] = "evidence_panel"
        valid["scene_directives"][0]["material_bindings"] = []
        valid["scene_directives"][0]["material_slot_directives"] = [{
            "slot_id": "evidence_visual",
            "semantic": "与观点相符的证据画面",
            "purpose": "evidence",
            "priority": "required",
            "ratio": "auto",
        }]
        self.assertEqual(
            valid,
            validate_director_decision(
                valid, candidates=CANDIDATES, capabilities=capabilities
            ),
        )

        duplicate_global_slot = copy.deepcopy(valid)
        duplicate_global_slot["scene_directives"][1]["material_slot_directives"][0]["slot_id"] = "evidence_visual"
        with self.assertRaisesRegex(
            DirectorDecisionError, "director_material_slot_duplicate"
        ):
            validate_director_decision(
                duplicate_global_slot,
                candidates=CANDIDATES,
                capabilities=capabilities,
            )

        missing_material = copy.deepcopy(valid)
        missing_material["scene_directives"][0]["material_slot_directives"] = []
        with self.assertRaisesRegex(
            DirectorDecisionError, "director_layout_material_missing"
        ):
            validate_director_decision(
                missing_material, candidates=CANDIDATES, capabilities=capabilities
            )

        audio_candidates = tuple(
            replace(candidate, speaker_available=False) for candidate in CANDIDATES
        )
        with self.assertRaisesRegex(
            DirectorDecisionError, "director_layout_source_incompatible"
        ):
            validate_director_decision(
                valid, candidates=audio_candidates, capabilities=capabilities
            )

    def test_material_slot_purpose_matches_the_edit_plan_contract(self):
        value = valid_decision()
        value["scene_directives"][1]["material_slot_directives"][0]["purpose"] = "decoration"
        self.assertEqual(
            value,
            validate_director_decision(
                value, candidates=CANDIDATES, capabilities=CAPABILITIES
            ),
        )
        value["scene_directives"][1]["material_slot_directives"][0]["purpose"] = "background"
        with self.assertRaisesRegex(
            DirectorDecisionError, "director_decision_schema_invalid"
        ):
            validate_director_decision(
                value, candidates=CANDIDATES, capabilities=CAPABILITIES
            )

    def test_semantic_material_policy_limits_required_slots_across_the_video(self):
        candidates = tuple(
            SceneCandidate(
                f"candidate_{index:02d}",
                (index - 1) * 1000,
                index * 1000,
                (f"caption_{index:03d}",),
                f"权威文案{index}",
                (),
                (),
                False,
                ((f"caption_{index:03d}", f"权威文案{index}"),),
            )
            for index in range(1, 8)
        )
        capabilities = copy.deepcopy(CAPABILITIES)
        capabilities["layout_capabilities"] = ["number_proof"]
        capabilities["layout_variants"] = {
            "number_proof": ["numeric_centerpiece"]
        }
        capabilities["layout_animation_targets"] = {"number_proof": []}
        capabilities["material_binding_mode"] = "semantic_slots_only"
        capabilities["layout_requirements"] = layout_requirements_for(
            capabilities["layout_capabilities"]
        )
        capabilities["max_required_material_slots"] = MAX_REQUIRED_MATERIAL_SLOTS
        capabilities["max_total_material_slots"] = MAX_TOTAL_MATERIAL_SLOTS
        capabilities["speaker_visibility_policy"] = copy.deepcopy(
            SPEAKER_VISIBILITY_POLICY
        )
        capabilities["scene_structure_policy"] = copy.deepcopy(
            SCENE_STRUCTURE_POLICY
        )
        decision = {
            "version": "1.0",
            "creative_concept": "数据证明",
            "narrative_pattern": "number_proof",
            "theme_profile_id": "editorial_clean",
            "design_intent": {
                "density": "balanced",
                "motion_energy": "medium",
                "image_fit": "smart_crop",
                "decoration_intensity": "medium",
            },
            "scene_directives": [
                {
                    "scene_id": f"candidate_{index:02d}",
                    "narrative_role": "proof",
                    "layout_id": "number_proof",
                    "layout_variant": "numeric_centerpiece",
                    "headline": {
                        "text_kind": "verbatim",
                        "source_caption_ids": [f"caption_{index:03d}"],
                    },
                    "overlay_instances": [{
                        "instance_id": f"caption_{index:02d}",
                        "component_id": "standard_caption",
                        "content_ref": "headline",
                        "placement": "subtitle_safe",
                    }],
                    "material_bindings": [],
                    "material_slot_directives": [{
                        "slot_id": f"candidate_{index:02d}_evidence",
                        "semantic": f"第{index}场的证据图",
                        "purpose": "evidence",
                        "priority": "required",
                        "ratio": "auto",
                    }],
                    "animations": [{
                        "target_id": f"caption_{index:02d}",
                        "preset": "fade",
                        "direction": "none",
                        "duration_ms": 300,
                        "delay_ms": 0,
                    }],
                    "transition": "hard_cut",
                    "sound_events": [],
                }
                for index in range(1, 8)
            ],
            "audio_intent": {
                "bgm_description": "克制的无歌词背景音乐",
                "energy": "medium",
                "dialogue_priority": True,
            },
        }

        with self.assertRaisesRegex(
            DirectorDecisionError, "director_required_material_limit_exceeded"
        ):
            validate_director_decision(
                decision, candidates=candidates, capabilities=capabilities
            )
        self.assertEqual(
            6,
            len(validate_director_decision(
                {**decision, "scene_directives": decision["scene_directives"][:6]},
                candidates=candidates[:6],
                capabilities=capabilities,
            )["scene_directives"]),
        )

    def test_video_director_enforces_opening_visibility_and_structure_before_render(self):
        candidates = tuple(
            SceneCandidate(
                f"candidate_{index:02d}",
                (index - 1) * 4000,
                index * 4000,
                (f"caption_{index:03d}",),
                f"权威文案{index}",
                (),
                (),
                True,
                ((f"caption_{index:03d}", f"权威文案{index}"),),
            )
            for index in range(1, 4)
        )
        capabilities = copy.deepcopy(CAPABILITIES)
        capabilities["material_binding_mode"] = "semantic_slots_only"
        capabilities["layout_requirements"] = layout_requirements_for(
            capabilities["layout_capabilities"]
        )
        capabilities["max_required_material_slots"] = MAX_REQUIRED_MATERIAL_SLOTS
        capabilities["max_total_material_slots"] = MAX_TOTAL_MATERIAL_SLOTS
        capabilities["speaker_visibility_policy"] = copy.deepcopy(
            SPEAKER_VISIBILITY_POLICY
        )
        capabilities["scene_structure_policy"] = copy.deepcopy(
            SCENE_STRUCTURE_POLICY
        )

        def decision_for(layouts):
            directives = []
            for index, layout_id in enumerate(layouts, 1):
                directives.append({
                    "scene_id": f"candidate_{index:02d}",
                    "narrative_role": "hook" if index == 1 else "proof",
                    "layout_id": layout_id,
                    "layout_variant": (
                        "clean_center" if layout_id == "speaker_fullscreen"
                        else "diagonal_statement"
                    ),
                    "headline": {
                        "text_kind": "verbatim",
                        "source_caption_ids": [f"caption_{index:03d}"],
                    },
                    "overlay_instances": [{
                        "instance_id": f"caption_{index:02d}",
                        "component_id": "standard_caption",
                        "content_ref": "headline",
                        "placement": "subtitle_safe",
                    }],
                    "material_bindings": [],
                    "material_slot_directives": [],
                    "animations": [{
                        "target_id": f"caption_{index:02d}",
                        "preset": "fade",
                        "direction": "none",
                        "duration_ms": 300,
                        "delay_ms": 0,
                    }],
                    "transition": "hard_cut",
                    "sound_events": [],
                })
            return {
                "version": "1.0",
                "creative_concept": "人物讲解与证据交替",
                "narrative_pattern": "speaker_evidence",
                "theme_profile_id": "editorial_clean",
                "design_intent": {
                    "density": "balanced",
                    "motion_energy": "medium",
                    "image_fit": "smart_crop",
                    "decoration_intensity": "medium",
                },
                "scene_directives": directives,
                "audio_intent": {
                    "bgm_description": "克制的无歌词背景音乐",
                    "energy": "medium",
                    "dialogue_priority": True,
                },
            }

        with self.assertRaisesRegex(
            DirectorDecisionError, "director_opening_speaker_required"
        ):
            validate_director_decision(
                decision_for(("quote_reversal", "speaker_fullscreen", "speaker_fullscreen")),
                candidates=candidates,
                capabilities=capabilities,
            )
        with self.assertRaisesRegex(
            DirectorDecisionError, "director_speaker_visibility_exceeded"
        ):
            validate_director_decision(
                decision_for(("speaker_fullscreen", "quote_reversal", "quote_reversal")),
                candidates=candidates,
                capabilities=capabilities,
            )
        with self.assertRaisesRegex(
            DirectorDecisionError, "director_scene_structure_repetitive"
        ):
            validate_director_decision(
                decision_for(("speaker_fullscreen", "speaker_fullscreen", "speaker_fullscreen")),
                candidates=candidates,
                capabilities=capabilities,
            )
        self.assertEqual(
            3,
            len(validate_director_decision(
                decision_for(("speaker_fullscreen", "quote_reversal", "speaker_fullscreen")),
                candidates=candidates,
                capabilities=capabilities,
            )["scene_directives"]),
        )


class DirectorDecisionGenerationTests(unittest.TestCase):
    def test_scene_directive_repair_constraints_are_specific_and_safe(self):
        cases = {
            "director_scene_coverage_invalid": "scene_directives_exact_candidate_order_and_count",
            "director_scene_structure_repetitive": "scene_signatures_meet_distinct_and_adjacency_policy",
            "director_speaker_visibility_exceeded": "speaker_hidden_duration_within_max_ratio",
        }

        for error_code, expected in cases.items():
            with self.subTest(error_code=error_code):
                self.assertEqual(
                    expected,
                    _repair_expected_constraint(
                        DirectorDecisionError(error_code, "$.scene_directives")
                    ),
                )

        self.assertEqual(
            "transition_from_capabilities_transition_capabilities",
            _repair_expected_constraint(
                DirectorDecisionError(
                    "director_transition_unknown",
                    "$.scene_directives[1].transition",
                )
            ),
        )
        self.assertEqual(
            "sound_events_empty_when_candidate_shorter_than_500ms",
            _repair_expected_constraint(
                DirectorDecisionError(
                    "director_sound_event_timeline_invalid",
                    "$.scene_directives[0].sound_events",
                )
            ),
        )
        self.assertEqual(
            "material_purpose_matches_selected_layout_semantic_slots",
            _repair_expected_constraint(
                DirectorDecisionError(
                    "director_material_purpose_invalid",
                    "$.scene_directives[2].material_slot_directives[1].purpose",
                )
            ),
        )

    def test_short_scene_sound_event_uses_the_single_targeted_repair(self):
        candidates = (replace(CANDIDATES[0], end_ms=400), CANDIDATES[1])
        initial = valid_decision()
        repaired = copy.deepcopy(initial)
        repaired["scene_directives"][0]["sound_events"] = []
        calls = []

        class Provider:
            def generate_decision(self, request, **kwargs):
                calls.append(copy.deepcopy(request))
                payload = initial if len(calls) == 1 else repaired
                return ProviderResult(
                    "dashscope",
                    "director",
                    f"request-{len(calls)}",
                    {"content": json.dumps(payload, ensure_ascii=False)},
                    {},
                    1,
                )

        context = SimpleNamespace(
            job_id="job-short-scene-sfx-repair",
            request={"safe": True},
            candidates=candidates,
            capabilities=CAPABILITIES,
            deadline_at=123.0,
        )
        result = generate_director_decision(context, Provider())

        self.assertEqual(2, len(calls))
        self.assertEqual([], result.value["scene_directives"][0]["sound_events"])
        self.assertEqual(
            "sound_events_empty_when_candidate_shorter_than_500ms",
            calls[1]["repair"]["expected_constraint"],
        )

    def test_unambiguous_material_purpose_is_canonicalized_without_repair(self):
        candidates, capabilities, initial = production_video_case()
        initial["scene_directives"][0]["material_slot_directives"] = [{
            "slot_id": "candidate_01_evidence",
            "semantic": "abstract platform evidence",
            "purpose": "context",
            "priority": "optional",
            "ratio": "auto",
        }]
        calls = []

        class Provider:
            def generate_decision(self, request, **kwargs):
                calls.append(copy.deepcopy(request))
                return ProviderResult(
                    "dashscope",
                    "director",
                    "request-material-local",
                    {"content": json.dumps(initial, ensure_ascii=False)},
                    {},
                    1,
                )

        context = SimpleNamespace(
            job_id="job-material-local",
            request={"safe": True},
            candidates=candidates,
            capabilities=capabilities,
            deadline_at=123.0,
        )
        result = generate_director_decision(context, Provider())

        self.assertEqual(1, len(calls))
        self.assertEqual(
            "evidence",
            result.value["scene_directives"][0]["material_slot_directives"][0][
                "purpose"
            ],
        )
        self.assertEqual(
            result.value,
            validate_director_decision(
                result.value,
                candidates=candidates,
                capabilities=capabilities,
            ),
        )

    def test_material_purpose_scoped_merge_discards_non_target_changes(self):
        candidates, capabilities, initial = production_video_case(
            ("material_fullscreen_speaker_pip", "speaker_fullscreen", "speaker_fullscreen")
        )
        capabilities["layout_capabilities"].append(
            "material_fullscreen_speaker_pip"
        )
        capabilities["layout_variants"]["material_fullscreen_speaker_pip"] = [
            "pip_round"
        ]
        capabilities["layout_animation_targets"][
            "material_fullscreen_speaker_pip"
        ] = []
        capabilities["layout_requirements"] = layout_requirements_for(
            capabilities["layout_capabilities"]
        )
        initial["scene_directives"][0]["layout_variant"] = "pip_round"
        initial["scene_directives"][0]["material_slot_directives"] = [{
            "slot_id": "candidate_01_product",
            "semantic": "generic product concept",
            "purpose": "evidence",
            "priority": "required",
            "ratio": "auto",
        }]
        repair = copy.deepcopy(initial)
        repair["scene_directives"][0]["material_slot_directives"][0][
            "purpose"
        ] = "product"
        repair["creative_concept"] = "discarded whole-response rewrite"
        repair["scene_directives"][1]["layout_id"] = "quote_reversal"
        repair["scene_directives"][1]["layout_variant"] = "diagonal_statement"
        error = DirectorDecisionError(
            "director_material_purpose_invalid",
            "$.scene_directives[0].material_slot_directives[0].purpose",
        )

        recovered = _apply_scoped_material_purpose_repair(
            repair, initial, error, capabilities
        )
        expected = copy.deepcopy(initial)
        expected["scene_directives"][0]["material_slot_directives"][0][
            "purpose"
        ] = "product"

        self.assertEqual(expected, recovered)
        self.assertEqual(
            recovered,
            validate_director_decision(
                recovered,
                candidates=candidates,
                capabilities=capabilities,
            ),
        )

    def test_material_then_visibility_repair_uses_frozen_envelope_and_initial_fallback(self):
        candidates, capabilities, initial = production_video_case(
            ("speaker_fullscreen", "quote_reversal", "quote_reversal")
        )
        capabilities["layout_capabilities"].append("steps_stack")
        capabilities["layout_variants"]["steps_stack"] = ["vertical_steps"]
        capabilities["layout_animation_targets"]["steps_stack"] = []
        capabilities["layout_requirements"] = layout_requirements_for(
            capabilities["layout_capabilities"]
        )
        initial["scene_directives"][0]["material_slot_directives"] = [{
            "slot_id": "candidate_01_evidence",
            "semantic": "abstract platform evidence",
            "purpose": "context",
            "priority": "optional",
            "ratio": "auto",
        }]
        repair = copy.deepcopy(initial)
        repair["scene_directives"][0]["material_slot_directives"][0][
            "purpose"
        ] = "evidence"
        for index in (1, 2):
            directive = repair["scene_directives"][index]
            directive["layout_id"] = "steps_stack"
            directive["layout_variant"] = "vertical_steps"
            directive["material_slot_directives"] = [{
                "slot_id": f"candidate_{index + 1:02d}_decoration",
                "semantic": "required decorative process marker",
                "purpose": "decoration",
                "priority": "required",
                "ratio": "auto",
            }]
        calls = []

        class Provider:
            def generate_decision(self, request, **kwargs):
                calls.append(copy.deepcopy(request))
                payload = initial if len(calls) == 1 else repair
                return ProviderResult(
                    "dashscope",
                    "director",
                    f"request-{len(calls)}",
                    {"content": json.dumps(payload, ensure_ascii=False)},
                    {},
                    1,
                )

        context = SimpleNamespace(
            job_id="job-material-visibility-chain",
            request={"safe": True},
            candidates=candidates,
            capabilities=capabilities,
            deadline_at=123.0,
        )
        result = generate_director_decision(context, Provider())

        self.assertEqual(2, len(calls))
        self.assertEqual(
            "speaker_hidden_duration_within_max_ratio",
            calls[1]["repair"]["expected_constraint"],
        )
        envelope = calls[1]["repair"]["constraint_envelope"]
        self.assertEqual(12000, envelope["total_duration_ms"])
        self.assertEqual(4800, envelope["max_hidden_ms"])
        self.assertEqual([4000, 4000, 4000], [
            item["duration_ms"] for item in envelope["scene_candidate_bounds"]
        ])
        self.assertEqual(
            ["speaker_fullscreen", "speaker_fullscreen", "quote_reversal"],
            [item["layout_id"] for item in result.value["scene_directives"]],
        )
        self.assertEqual("request-1", result.provider_request_id)
        self.assertEqual(
            result.value,
            validate_director_decision(
                result.value,
                candidates=candidates,
                capabilities=capabilities,
            ),
        )

    def test_one_unknown_transition_is_canonicalized_without_spending_repair(self):
        candidates, capabilities, decision = production_video_case()
        decision["scene_directives"][0]["transition"] = "cross_fade"
        calls = []

        class Provider:
            def generate_decision(self, request, **kwargs):
                calls.append((request, kwargs))
                return ProviderResult(
                    "dashscope",
                    "director",
                    "request-1",
                    {"content": json.dumps(decision, ensure_ascii=False)},
                    {},
                    1,
                )

        context = SimpleNamespace(
            job_id="job-transition-fallback",
            request={"safe": True},
            candidates=candidates,
            capabilities=capabilities,
            deadline_at=123.0,
        )
        result = generate_director_decision(context, Provider())

        self.assertEqual(1, len(calls))
        self.assertEqual("hard_cut", result.value["scene_directives"][0]["transition"])
        self.assertIn("cross_fade", result.raw_output_json)
        self.assertEqual(
            hashlib.sha256(canonical_json(result.value)).hexdigest(),
            result.decision_sha256,
        )
        self.assertNotEqual(result.raw_output_sha256, result.decision_sha256)

    def test_transition_fallback_exposes_visibility_error_to_targeted_repair(self):
        candidates, capabilities, invalid = production_video_case(
            ("speaker_fullscreen", "quote_reversal", "quote_reversal")
        )
        invalid["scene_directives"][0]["transition"] = "cross_fade"
        _, _, repaired = production_video_case()
        calls = []

        class Provider:
            def generate_decision(self, request, **kwargs):
                calls.append(request)
                payload = invalid if len(calls) == 1 else repaired
                return ProviderResult(
                    "dashscope",
                    "director",
                    f"request-{len(calls)}",
                    {"content": json.dumps(payload, ensure_ascii=False)},
                    {},
                    1,
                )

        context = SimpleNamespace(
            job_id="job-targeted-repair",
            request={"safe": True},
            candidates=candidates,
            capabilities=capabilities,
            deadline_at=123.0,
        )
        result = generate_director_decision(context, Provider())

        self.assertEqual(2, len(calls))
        self.assertEqual(
            "speaker_hidden_duration_within_max_ratio",
            calls[1]["repair"]["expected_constraint"],
        )
        self.assertEqual("speaker_fullscreen", result.value["scene_directives"][2]["layout_id"])

    def test_second_visibility_failure_gets_minimal_deterministic_speaker_fallback(self):
        candidates, capabilities, invalid = production_video_case(
            ("speaker_fullscreen", "quote_reversal", "quote_reversal")
        )
        calls = []

        class Provider:
            def generate_decision(self, request, **kwargs):
                calls.append(request)
                return ProviderResult(
                    "dashscope",
                    "director",
                    f"request-{len(calls)}",
                    {"content": json.dumps(invalid, ensure_ascii=False)},
                    {},
                    1,
                )

        context = SimpleNamespace(
            job_id="job-speaker-fallback",
            request={"safe": True},
            candidates=candidates,
            capabilities=capabilities,
            deadline_at=123.0,
        )
        result = generate_director_decision(context, Provider())

        self.assertEqual(2, len(calls))
        self.assertEqual(
            ["speaker_fullscreen", "speaker_fullscreen", "quote_reversal"],
            [item["layout_id"] for item in result.value["scene_directives"]],
        )
        self.assertEqual(
            "clean_center", result.value["scene_directives"][1]["layout_variant"]
        )
        self.assertEqual(
            invalid["scene_directives"][1]["headline"],
            result.value["scene_directives"][1]["headline"],
        )
        self.assertEqual(
            invalid["scene_directives"][1]["overlay_instances"],
            result.value["scene_directives"][1]["overlay_instances"],
        )
        timeline = {
            "duration_ms": 12000,
            "ratio": "9:16",
            "captions": [
                {
                    "id": f"caption_{index:03d}",
                    "start_ms": (index - 1) * 4000,
                    "end_ms": index * 4000,
                    "text": f"authoritative sentence {index}",
                }
                for index in range(1, 4)
            ],
        }
        compiled = compile_edit_plan(
            result.value,
            candidates=candidates,
            timeline=timeline,
            materials=[],
            capabilities=capabilities,
            variation_seed=1,
        )
        self.assertEqual(
            ["speaker_fullscreen", "speaker_fullscreen", "quote_reversal"],
            [item["layout_id"] for item in compiled["scenes"]],
        )

    def test_schema_broken_repair_recovers_strict_initial_visibility_candidate(self):
        candidates, capabilities, initial = production_video_case(
            ("speaker_fullscreen", "quote_reversal", "quote_reversal")
        )
        _, _, broken_repair = production_video_case()
        broken_repair["scene_directives"][0]["highlight"] = "model-authored copy"
        calls = []

        class Provider:
            def generate_decision(self, request, **kwargs):
                calls.append(request)
                payload = initial if len(calls) == 1 else broken_repair
                return ProviderResult(
                    "dashscope",
                    "director",
                    f"request-{len(calls)}",
                    {"content": json.dumps(payload, ensure_ascii=False)},
                    {},
                    1,
                )

        context = SimpleNamespace(
            job_id="job-initial-speaker-recovery",
            request={"safe": True},
            candidates=candidates,
            capabilities=capabilities,
            deadline_at=123.0,
        )
        result = generate_director_decision(context, Provider())

        self.assertEqual(2, len(calls))
        self.assertEqual(
            "director_speaker_visibility_exceeded",
            calls[1]["repair"]["error_code"],
        )
        self.assertEqual(
            "speaker_hidden_duration_within_max_ratio",
            calls[1]["repair"]["expected_constraint"],
        )
        self.assertEqual(
            ["speaker_fullscreen", "speaker_fullscreen", "quote_reversal"],
            [item["layout_id"] for item in result.value["scene_directives"]],
        )
        self.assertNotIn("highlight", result.value["scene_directives"][0])
        initial_raw_json = canonical_json(initial).decode("utf-8")
        self.assertEqual("request-1", result.provider_request_id)
        self.assertEqual(initial_raw_json, result.raw_output_json)
        self.assertEqual(
            hashlib.sha256(initial_raw_json.encode("utf-8")).hexdigest(),
            result.raw_output_sha256,
        )
        self.assertEqual(
            result.value,
            validate_director_decision(
                result.value,
                candidates=candidates,
                capabilities=capabilities,
            ),
        )
        timeline = {
            "duration_ms": 12000,
            "ratio": "9:16",
            "captions": [
                {
                    "id": f"caption_{index:03d}",
                    "start_ms": (index - 1) * 4000,
                    "end_ms": index * 4000,
                    "text": f"authoritative sentence {index}",
                }
                for index in range(1, 4)
            ],
        }
        compiled = compile_edit_plan(
            result.value,
            candidates=candidates,
            timeline=timeline,
            materials=[],
            capabilities=capabilities,
            variation_seed=1,
        )
        self.assertEqual(
            ["speaker_fullscreen", "speaker_fullscreen", "quote_reversal"],
            [item["layout_id"] for item in compiled["scenes"]],
        )

    def test_initial_recovery_requires_raw_initial_to_be_speaker_only_invalid(self):
        candidates, capabilities, initial = production_video_case(
            ("speaker_fullscreen", "quote_reversal", "quote_reversal")
        )
        initial["scene_directives"][0]["transition"] = "cross_fade"
        broken_repair = copy.deepcopy(initial)
        broken_repair["scene_directives"][0]["highlight"] = "model-authored copy"
        calls = []

        class Provider:
            def generate_decision(self, request, **kwargs):
                calls.append(request)
                payload = initial if len(calls) == 1 else broken_repair
                return ProviderResult(
                    "dashscope",
                    "director",
                    f"request-{len(calls)}",
                    {"content": json.dumps(payload, ensure_ascii=False)},
                    {},
                    1,
                )

        context = SimpleNamespace(
            job_id="job-no-nonstrict-initial-recovery",
            request={"safe": True},
            candidates=candidates,
            capabilities=capabilities,
            deadline_at=123.0,
        )
        with self.assertRaises(DirectorDecisionError) as raised:
            generate_director_decision(context, Provider())

        self.assertEqual(2, len(calls))
        self.assertEqual("director_decision_invalid", raised.exception.code)
        self.assertEqual("director_decision_schema_invalid", raised.exception.detail_code)
        self.assertEqual("$.scene_directives[0].highlight", raised.exception.path)

    def test_initial_recovery_does_not_mask_unsafe_repair(self):
        candidates, capabilities, initial = production_video_case(
            ("speaker_fullscreen", "quote_reversal", "quote_reversal")
        )
        unsafe_repair = copy.deepcopy(initial)
        unsafe_repair["creative_concept"] = "https://example.invalid/director"
        calls = []

        class Provider:
            def generate_decision(self, request, **kwargs):
                calls.append(request)
                payload = initial if len(calls) == 1 else unsafe_repair
                return ProviderResult(
                    "dashscope",
                    "director",
                    f"request-{len(calls)}",
                    {"content": json.dumps(payload, ensure_ascii=False)},
                    {},
                    1,
                )

        context = SimpleNamespace(
            job_id="job-no-unsafe-repair-mask",
            request={"safe": True},
            candidates=candidates,
            capabilities=capabilities,
            deadline_at=123.0,
        )
        with self.assertRaises(DirectorDecisionError) as raised:
            generate_director_decision(context, Provider())

        self.assertEqual(2, len(calls))
        self.assertEqual("director_decision_invalid", raised.exception.code)
        self.assertEqual("director_decision_unsafe_value", raised.exception.detail_code)
        self.assertEqual("$.creative_concept", raised.exception.path)

    def test_initial_recovery_rejects_visible_text_schema_plus_unsafe_repair(self):
        candidates, capabilities, initial = production_video_case(
            ("speaker_fullscreen", "quote_reversal", "quote_reversal")
        )
        _, _, broken_repair = production_video_case()
        broken_repair["scene_directives"][0]["highlight"] = "model-authored copy"
        broken_repair["creative_concept"] = "https://example.invalid/director"
        calls = []

        class Provider:
            def generate_decision(self, request, **kwargs):
                calls.append(request)
                payload = initial if len(calls) == 1 else broken_repair
                return ProviderResult(
                    "dashscope",
                    "director",
                    f"request-{len(calls)}",
                    {"content": json.dumps(payload, ensure_ascii=False)},
                    {},
                    1,
                )

        context = SimpleNamespace(
            job_id="job-no-combined-unsafe-repair-mask",
            request={"safe": True},
            candidates=candidates,
            capabilities=capabilities,
            deadline_at=123.0,
        )
        with self.assertRaises(DirectorDecisionError) as raised:
            generate_director_decision(context, Provider())

        self.assertEqual(2, len(calls))
        self.assertEqual("director_decision_invalid", raised.exception.code)
        self.assertEqual("director_decision_schema_invalid", raised.exception.detail_code)
        self.assertEqual("$.scene_directives[0].highlight", raised.exception.path)

    def test_initial_recovery_rejects_visible_text_plus_second_schema_error(self):
        candidates, capabilities, initial = production_video_case(
            ("speaker_fullscreen", "quote_reversal", "quote_reversal")
        )
        _, _, broken_repair = production_video_case()
        broken_repair["scene_directives"][0]["highlight"] = "model-authored copy"
        broken_repair["version"] = "2.0"
        calls = []

        class Provider:
            def generate_decision(self, request, **kwargs):
                calls.append(request)
                payload = initial if len(calls) == 1 else broken_repair
                return ProviderResult(
                    "dashscope",
                    "director",
                    f"request-{len(calls)}",
                    {"content": json.dumps(payload, ensure_ascii=False)},
                    {},
                    1,
                )

        context = SimpleNamespace(
            job_id="job-no-second-schema-error-mask",
            request={"safe": True},
            candidates=candidates,
            capabilities=capabilities,
            deadline_at=123.0,
        )
        with self.assertRaises(DirectorDecisionError) as raised:
            generate_director_decision(context, Provider())

        self.assertEqual(2, len(calls))
        self.assertEqual("director_decision_invalid", raised.exception.code)
        self.assertEqual("director_decision_schema_invalid", raised.exception.detail_code)
        self.assertEqual("$.scene_directives[0].highlight", raised.exception.path)

    def test_initial_recovery_revalidates_scene_structure_fail_closed(self):
        original_candidates, capabilities, initial = production_video_case(
            ("speaker_fullscreen", "quote_reversal", "quote_reversal")
        )
        candidates = (
            replace(original_candidates[0], start_ms=0, end_ms=1000),
            replace(original_candidates[1], start_ms=1000, end_ms=7000),
            replace(original_candidates[2], start_ms=7000, end_ms=13000),
        )
        _, _, broken_repair = production_video_case()
        broken_repair["scene_directives"][0]["highlight"] = "model-authored copy"
        calls = []

        class Provider:
            def generate_decision(self, request, **kwargs):
                calls.append(request)
                payload = initial if len(calls) == 1 else broken_repair
                return ProviderResult(
                    "dashscope",
                    "director",
                    f"request-{len(calls)}",
                    {"content": json.dumps(payload, ensure_ascii=False)},
                    {},
                    1,
                )

        context = SimpleNamespace(
            job_id="job-initial-fallback-full-revalidation",
            request={"safe": True},
            candidates=candidates,
            capabilities=capabilities,
            deadline_at=123.0,
        )
        with self.assertRaises(DirectorDecisionError) as raised:
            generate_director_decision(context, Provider())

        self.assertEqual(2, len(calls))
        self.assertEqual("director_decision_invalid", raised.exception.code)
        self.assertEqual("director_decision_schema_invalid", raised.exception.detail_code)

    def test_speaker_fallback_preserves_required_product_slot(self):
        candidates, capabilities, decision = production_video_case(
            ("speaker_fullscreen", "product_hero", "product_hero")
        )
        capabilities["layout_capabilities"] = [
            "speaker_fullscreen",
            "product_hero",
            "material_fullscreen_speaker_pip",
        ]
        capabilities["layout_variants"].update({
            "product_hero": ["center_pedestal"],
            "material_fullscreen_speaker_pip": ["pip_round", "pip_card"],
        })
        capabilities["layout_animation_targets"].update({
            "product_hero": [],
            "material_fullscreen_speaker_pip": [],
        })
        capabilities["layout_requirements"] = layout_requirements_for(
            capabilities["layout_capabilities"]
        )
        for index in (1, 2):
            directive = decision["scene_directives"][index]
            directive["layout_variant"] = "center_pedestal"
            directive["material_slot_directives"] = [{
                "slot_id": f"candidate_{index + 1:02d}_product",
                "semantic": "non-branded product concept",
                "purpose": "product",
                "priority": "required",
                "ratio": "auto",
            }]
        calls = []

        class Provider:
            def generate_decision(self, request, **kwargs):
                calls.append(request)
                return ProviderResult(
                    "dashscope",
                    "director",
                    f"request-{len(calls)}",
                    {"content": json.dumps(decision, ensure_ascii=False)},
                    {},
                    1,
                )

        context = SimpleNamespace(
            job_id="job-product-fallback",
            request={"safe": True},
            candidates=candidates,
            capabilities=capabilities,
            deadline_at=123.0,
        )
        result = generate_director_decision(context, Provider())

        changed = result.value["scene_directives"][1]
        self.assertEqual("material_fullscreen_speaker_pip", changed["layout_id"])
        self.assertEqual(
            decision["scene_directives"][1]["material_slot_directives"],
            changed["material_slot_directives"],
        )

    def test_speaker_fallback_fails_closed_when_material_semantics_are_incompatible(self):
        candidates, capabilities, decision = production_video_case(
            ("speaker_fullscreen", "steps_stack", "steps_stack")
        )
        capabilities["layout_capabilities"] = ["speaker_fullscreen", "steps_stack"]
        capabilities["layout_variants"]["steps_stack"] = ["vertical_steps"]
        capabilities["layout_animation_targets"]["steps_stack"] = []
        capabilities["layout_requirements"] = layout_requirements_for(
            capabilities["layout_capabilities"]
        )
        for index in (1, 2):
            directive = decision["scene_directives"][index]
            directive["layout_variant"] = "vertical_steps"
            directive["material_slot_directives"] = [{
                "slot_id": f"candidate_{index + 1:02d}_decoration",
                "semantic": "required decorative process marker",
                "purpose": "decoration",
                "priority": "required",
                "ratio": "auto",
            }]

        class Provider:
            def generate_decision(self, request, **kwargs):
                return ProviderResult(
                    "dashscope",
                    "director",
                    "request-incompatible",
                    {"content": json.dumps(decision, ensure_ascii=False)},
                    {},
                    1,
                )

        context = SimpleNamespace(
            job_id="job-incompatible-fallback",
            request={"safe": True},
            candidates=candidates,
            capabilities=capabilities,
            deadline_at=123.0,
        )
        with self.assertRaises(DirectorDecisionError) as raised:
            generate_director_decision(context, Provider())

        self.assertEqual("director_decision_invalid", raised.exception.code)
        self.assertEqual(
            "director_speaker_visibility_exceeded", raised.exception.detail_code
        )

    def test_only_one_unknown_transition_is_locally_normalized_per_response(self):
        candidates, capabilities, decision = production_video_case()
        decision["scene_directives"][0]["transition"] = "cross_fade"
        decision["scene_directives"][1]["transition"] = "dissolve"
        calls = []

        class Provider:
            def generate_decision(self, request, **kwargs):
                calls.append(request)
                return ProviderResult(
                    "dashscope",
                    "director",
                    f"request-{len(calls)}",
                    {"content": json.dumps(decision, ensure_ascii=False)},
                    {},
                    1,
                )

        context = SimpleNamespace(
            job_id="job-two-transition-failures",
            request={"safe": True},
            candidates=candidates,
            capabilities=capabilities,
            deadline_at=123.0,
        )
        with self.assertRaises(DirectorDecisionError) as raised:
            generate_director_decision(context, Provider())

        self.assertEqual(2, len(calls))
        self.assertEqual("director_decision_invalid", raised.exception.code)
        self.assertEqual("director_transition_unknown", raised.exception.detail_code)

    def test_one_bounded_repair_contains_only_safe_evidence(self):
        calls = []
        class Provider:
            def generate_decision(self, request, **kwargs):
                calls.append((request, kwargs))
                payload = {"invalid": True} if len(calls) == 1 else valid_decision()
                return ProviderResult("dashscope", "director", f"request-{len(calls)}", {"content": json.dumps(payload, ensure_ascii=False)}, {}, 1)
        context = SimpleNamespace(job_id="job-1", request={"scene_candidates": [candidate.__dict__ if hasattr(candidate, "__dict__") else {slot: getattr(candidate, slot) for slot in candidate.__slots__} for candidate in CANDIDATES], "capabilities": CAPABILITIES}, candidates=CANDIDATES, capabilities=CAPABILITIES, deadline_at=123.0)
        result = generate_director_decision(context, Provider(), max_repairs=1)
        self.assertEqual("1.0", result.value["version"])
        self.assertEqual(2, len(calls))
        repair = calls[1][0]
        self.assertEqual({"frozen_request", "previous_response_sha256", "repair"}, set(repair))
        self.assertNotIn("previous_response", repair)

    def test_visible_text_schema_repair_names_the_safe_reference_shape(self):
        calls = []

        class Provider:
            def generate_decision(self, request, **kwargs):
                calls.append(request)
                if len(calls) == 1:
                    invalid = valid_decision()
                    invalid["scene_directives"][0]["highlight"] = "model-authored copy"
                    return ProviderResult(
                        "dashscope",
                        "director",
                        "request-1",
                        {"content": json.dumps(invalid, ensure_ascii=False)},
                        {},
                        1,
                    )
                return ProviderResult(
                    "dashscope",
                    "director",
                    "request-2",
                    {"content": json.dumps(valid_decision(), ensure_ascii=False)},
                    {},
                    1,
                )

        context = SimpleNamespace(
            job_id="job-1",
            request={"safe": True},
            candidates=CANDIDATES,
            capabilities=CAPABILITIES,
            deadline_at=123.0,
        )

        result = generate_director_decision(context, Provider(), max_repairs=1)

        self.assertEqual("1.0", result.value["version"])
        self.assertEqual(
            "visible_text_reference_object_or_omit",
            calls[1]["repair"]["expected_constraint"],
        )
        self.assertNotIn("model-authored copy", json.dumps(calls[1], ensure_ascii=False))

    def test_visible_text_scoped_repair_discards_unrelated_material_purpose_change(self):
        candidates, capabilities, initial = production_video_case()
        initial["scene_directives"][0]["highlight"] = "model-authored copy"
        initial["scene_directives"][0]["material_slot_directives"] = [{
            "slot_id": "candidate_01_evidence",
            "semantic": "abstract platform capability evidence",
            "purpose": "evidence",
            "priority": "optional",
            "ratio": "auto",
        }]
        repair = copy.deepcopy(initial)
        repair["creative_concept"] = "unrelated repair rewrite"
        repair["scene_directives"][0]["highlight"] = {
            "text_kind": "compressed",
            "source_caption_ids": ["caption_001"],
        }
        repair["scene_directives"][0]["material_slot_directives"][0][
            "purpose"
        ] = "context"
        calls = []

        class Provider:
            def generate_decision(self, request, **kwargs):
                calls.append(request)
                payload = initial if len(calls) == 1 else repair
                return ProviderResult(
                    "dashscope",
                    "director",
                    f"request-{len(calls)}",
                    {"content": json.dumps(payload, ensure_ascii=False)},
                    {},
                    1,
                )

        context = SimpleNamespace(
            job_id="job-visible-text-scoped-repair",
            request={"safe": True},
            candidates=candidates,
            capabilities=capabilities,
            deadline_at=123.0,
        )
        result = generate_director_decision(context, Provider(), max_repairs=1)

        self.assertEqual(2, len(calls))
        self.assertEqual(
            "visible_text_reference_object_or_omit",
            calls[1]["repair"]["expected_constraint"],
        )
        self.assertTrue(calls[1]["repair"]["preserve_all_other_fields"])
        self.assertEqual(
            {
                "scene_id": "candidate_01",
                "field": "highlight",
                "allowed_text_kinds": ["verbatim", "compressed"],
                "allowed_source_caption_ids": ["caption_001"],
                "omission_allowed": True,
            },
            calls[1]["repair"]["target_context"],
        )
        self.assertEqual(initial["creative_concept"], result.value["creative_concept"])
        self.assertEqual(
            "evidence",
            result.value["scene_directives"][0]["material_slot_directives"][0][
                "purpose"
            ],
        )
        self.assertEqual(
            repair["scene_directives"][0]["highlight"],
            result.value["scene_directives"][0]["highlight"],
        )
        repair_raw_json = canonical_json(repair).decode("utf-8")
        self.assertEqual("request-2", result.provider_request_id)
        self.assertEqual(repair_raw_json, result.raw_output_json)
        self.assertEqual(
            hashlib.sha256(repair_raw_json.encode("utf-8")).hexdigest(),
            result.raw_output_sha256,
        )
        self.assertNotEqual(result.raw_output_sha256, result.decision_sha256)
        self.assertEqual(
            result.value,
            validate_director_decision(
                result.value,
                candidates=candidates,
                capabilities=capabilities,
            ),
        )

    def test_visible_text_scoped_repair_allows_safe_omission(self):
        candidates, capabilities, initial = production_video_case()
        initial["scene_directives"][0]["highlight"] = "model-authored copy"
        repair = copy.deepcopy(initial)
        repair["scene_directives"][0].pop("highlight")
        calls = []

        class Provider:
            def generate_decision(self, request, **kwargs):
                calls.append(request)
                payload = initial if len(calls) == 1 else repair
                return ProviderResult(
                    "dashscope",
                    "director",
                    f"request-{len(calls)}",
                    {"content": json.dumps(payload, ensure_ascii=False)},
                    {},
                    1,
                )

        context = SimpleNamespace(
            job_id="job-visible-text-safe-omission",
            request={"safe": True},
            candidates=candidates,
            capabilities=capabilities,
            deadline_at=123.0,
        )
        result = generate_director_decision(context, Provider(), max_repairs=1)

        self.assertNotIn("highlight", result.value["scene_directives"][0])
        self.assertTrue(calls[1]["repair"]["target_context"]["omission_allowed"])

    def test_visible_text_scoped_repair_rejects_omission_with_dangling_overlay(self):
        candidates, capabilities, initial = production_video_case()
        first = initial["scene_directives"][0]
        first["highlight"] = "model-authored copy"
        first["overlay_instances"][0]["content_ref"] = "highlight"
        repair = copy.deepcopy(initial)
        repair["scene_directives"][0].pop("highlight")
        calls = []

        class Provider:
            def generate_decision(self, request, **kwargs):
                calls.append(request)
                payload = initial if len(calls) == 1 else repair
                return ProviderResult(
                    "dashscope",
                    "director",
                    f"request-{len(calls)}",
                    {"content": json.dumps(payload, ensure_ascii=False)},
                    {},
                    1,
                )

        context = SimpleNamespace(
            job_id="job-visible-text-unsafe-omission",
            request={"safe": True},
            candidates=candidates,
            capabilities=capabilities,
            deadline_at=123.0,
        )
        with self.assertRaises(DirectorDecisionError) as raised:
            generate_director_decision(context, Provider(), max_repairs=1)

        self.assertEqual(2, len(calls))
        self.assertFalse(calls[1]["repair"]["target_context"]["omission_allowed"])
        self.assertEqual("director_decision_invalid", raised.exception.code)
        self.assertEqual(
            "director_decision_schema_invalid", raised.exception.detail_code
        )
        self.assertEqual("$.scene_directives[0].highlight", raised.exception.path)

    def test_visible_text_scoped_repair_rejects_unsafe_discarded_field(self):
        candidates, capabilities, initial = production_video_case()
        initial["scene_directives"][0]["highlight"] = "model-authored copy"
        repair = copy.deepcopy(initial)
        repair["scene_directives"][0]["highlight"] = {
            "text_kind": "verbatim",
            "source_caption_ids": ["caption_001"],
        }
        repair["creative_concept"] = "https://example.invalid/unsafe"
        calls = []

        class Provider:
            def generate_decision(self, request, **kwargs):
                calls.append(request)
                payload = initial if len(calls) == 1 else repair
                return ProviderResult(
                    "dashscope",
                    "director",
                    f"request-{len(calls)}",
                    {"content": json.dumps(payload, ensure_ascii=False)},
                    {},
                    1,
                )

        context = SimpleNamespace(
            job_id="job-visible-text-unsafe-discarded-field",
            request={"safe": True},
            candidates=candidates,
            capabilities=capabilities,
            deadline_at=123.0,
        )
        with self.assertRaises(DirectorDecisionError) as raised:
            generate_director_decision(context, Provider(), max_repairs=1)

        self.assertEqual("director_decision_invalid", raised.exception.code)
        self.assertEqual("director_decision_unsafe_value", raised.exception.detail_code)
        self.assertEqual("$.creative_concept", raised.exception.path)

    def test_visible_text_scoped_repair_rejects_caption_outside_target_scene(self):
        candidates, capabilities, initial = production_video_case()
        initial["scene_directives"][0]["highlight"] = "model-authored copy"
        repair = copy.deepcopy(initial)
        repair["scene_directives"][0]["highlight"] = {
            "text_kind": "verbatim",
            "source_caption_ids": ["caption_002"],
        }
        calls = []

        class Provider:
            def generate_decision(self, request, **kwargs):
                calls.append(request)
                payload = initial if len(calls) == 1 else repair
                return ProviderResult(
                    "dashscope",
                    "director",
                    f"request-{len(calls)}",
                    {"content": json.dumps(payload, ensure_ascii=False)},
                    {},
                    1,
                )

        context = SimpleNamespace(
            job_id="job-visible-text-wrong-caption",
            request={"safe": True},
            candidates=candidates,
            capabilities=capabilities,
            deadline_at=123.0,
        )
        with self.assertRaises(DirectorDecisionError) as raised:
            generate_director_decision(context, Provider(), max_repairs=1)

        self.assertEqual("director_decision_invalid", raised.exception.code)
        self.assertEqual("director_text_reference_invalid", raised.exception.detail_code)
        self.assertEqual("$.scene_directives[0].highlight", raised.exception.path)

    def test_visible_text_scoped_repair_rejects_target_scene_identity_change(self):
        candidates, capabilities, initial = production_video_case()
        initial["scene_directives"][0]["highlight"] = "model-authored copy"
        repair = copy.deepcopy(initial)
        repair["scene_directives"][0]["highlight"] = {
            "text_kind": "verbatim",
            "source_caption_ids": ["caption_001"],
        }
        repair["scene_directives"][0]["scene_id"] = "candidate_02"
        calls = []

        class Provider:
            def generate_decision(self, request, **kwargs):
                calls.append(request)
                payload = initial if len(calls) == 1 else repair
                return ProviderResult(
                    "dashscope",
                    "director",
                    f"request-{len(calls)}",
                    {"content": json.dumps(payload, ensure_ascii=False)},
                    {},
                    1,
                )

        context = SimpleNamespace(
            job_id="job-visible-text-scene-identity-change",
            request={"safe": True},
            candidates=candidates,
            capabilities=capabilities,
            deadline_at=123.0,
        )
        with self.assertRaises(DirectorDecisionError) as raised:
            generate_director_decision(context, Provider(), max_repairs=1)

        self.assertEqual("director_decision_invalid", raised.exception.code)
        self.assertEqual(
            "director_decision_schema_invalid", raised.exception.detail_code
        )
        self.assertEqual("$.scene_directives[0].highlight", raised.exception.path)

    def test_visible_text_scoped_repair_does_not_normalize_a_second_initial_defect(self):
        for defect, expected_code, expected_path in (
            (
                lambda value: value["scene_directives"][0].__setitem__(
                    "transition", "cross_fade"
                ),
                "director_transition_unknown",
                "$.scene_directives[0].transition",
            ),
            (
                lambda value: (
                    value["scene_directives"][1].__setitem__(
                        "layout_id", "quote_reversal"
                    ),
                    value["scene_directives"][1].__setitem__(
                        "layout_variant", "diagonal_statement"
                    ),
                    value["scene_directives"][2].__setitem__(
                        "layout_id", "quote_reversal"
                    ),
                    value["scene_directives"][2].__setitem__(
                        "layout_variant", "diagonal_statement"
                    ),
                ),
                "director_speaker_visibility_exceeded",
                "$.scene_directives",
            ),
        ):
            with self.subTest(expected_code=expected_code):
                candidates, capabilities, initial = production_video_case()
                initial["scene_directives"][0]["highlight"] = "model-authored copy"
                defect(initial)
                repair = copy.deepcopy(initial)
                repair["scene_directives"][0]["highlight"] = {
                    "text_kind": "verbatim",
                    "source_caption_ids": ["caption_001"],
                }
                calls = []

                class Provider:
                    def generate_decision(self, request, **kwargs):
                        calls.append(request)
                        payload = initial if len(calls) == 1 else repair
                        return ProviderResult(
                            "dashscope",
                            "director",
                            f"request-{len(calls)}",
                            {"content": json.dumps(payload, ensure_ascii=False)},
                            {},
                            1,
                        )

                context = SimpleNamespace(
                    job_id=f"job-visible-text-second-defect-{expected_code}",
                    request={"safe": True},
                    candidates=candidates,
                    capabilities=capabilities,
                    deadline_at=123.0,
                )
                with self.assertRaises(DirectorDecisionError) as raised:
                    generate_director_decision(context, Provider(), max_repairs=1)

                self.assertEqual("director_decision_invalid", raised.exception.code)
                self.assertEqual(expected_code, raised.exception.detail_code)
                self.assertEqual(expected_path, raised.exception.path)

    def test_nested_visible_text_schema_repair_uses_the_same_safe_constraint(self):
        calls = []

        class Provider:
            def generate_decision(self, request, **kwargs):
                calls.append(request)
                if len(calls) == 1:
                    invalid = valid_decision()
                    invalid["scene_directives"][0]["highlight"] = {
                        "text_kind": "compressed",
                        "source_caption_ids": "caption_001",
                    }
                    payload = invalid
                else:
                    payload = valid_decision()
                return ProviderResult(
                    "dashscope",
                    "director",
                    f"request-{len(calls)}",
                    {"content": json.dumps(payload, ensure_ascii=False)},
                    {},
                    1,
                )

        context = SimpleNamespace(
            job_id="job-1",
            request={"safe": True},
            candidates=CANDIDATES,
            capabilities=CAPABILITIES,
            deadline_at=123.0,
        )

        generate_director_decision(context, Provider(), max_repairs=1)

        self.assertEqual(
            "visible_text_reference_object_or_omit",
            calls[1]["repair"]["expected_constraint"],
        )

    def test_second_invalid_response_fails(self):
        calls = []
        class Provider:
            def generate_decision(self, request, **kwargs):
                calls.append(request)
                return ProviderResult(
                    "dashscope",
                    "director",
                    f"request-{len(calls)}",
                    {"content": json.dumps({"invalid": True})},
                    {},
                    1,
                )
        context = SimpleNamespace(job_id="job-1", request={"safe": True}, candidates=CANDIDATES, capabilities=CAPABILITIES, deadline_at=123.0)
        with self.assertRaisesRegex(DirectorDecisionError, "director_decision_invalid") as raised:
            generate_director_decision(context, Provider(), max_repairs=1)
        error = raised.exception
        self.assertEqual("director_decision_schema_invalid", error.detail_code)
        self.assertEqual("$", error.path)
        self.assertEqual(2, error.attempt_count)
        self.assertEqual(
            [
                {
                    "attempt": 1,
                    "purpose": "initial",
                    "request_id": "request-1",
                    "response_sha256": error.attempts[0]["response_sha256"],
                    "validation_code": "director_decision_schema_invalid",
                    "field_path": "$",
                },
                {
                    "attempt": 2,
                    "purpose": "repair",
                    "request_id": "request-2",
                    "response_sha256": error.attempts[1]["response_sha256"],
                    "validation_code": "director_decision_schema_invalid",
                    "field_path": "$",
                },
            ],
            list(error.attempts),
        )
        self.assertRegex(error.attempts[0]["response_sha256"], r"^[0-9a-f]{64}$")

    def test_invalid_request_id_is_hashed_without_leaking_provider_text(self):
        for secret_id in (
            "https://provider.invalid/request?token=SECRET\n",
            "sk-1234567890SECRET",
        ):
            class Provider:
                def generate_decision(self, request, **kwargs):
                    return SimpleNamespace(
                        request_id=secret_id,
                        payload={"content": "Bearer SECRET"},
                    )
            context = SimpleNamespace(job_id="job-1", request={"safe": True}, candidates=CANDIDATES, capabilities=CAPABILITIES, deadline_at=123.0)
            with self.subTest(secret_id=secret_id), self.assertRaises(DirectorDecisionError) as raised:
                generate_director_decision(context, Provider(), max_repairs=1)
            encoded = json.dumps(list(raised.exception.attempts), sort_keys=True)
            self.assertNotIn("SECRET", encoded)
            self.assertNotIn("provider.invalid", encoded)
            self.assertNotIn("request_id\"", encoded)
            self.assertIn("request_id_present", encoded)
            self.assertIn("request_id_sha256", encoded)

    def test_frozen_request_rejects_url_path_and_secret_bearing_values(self):
        class Provider:
            def generate_decision(self, *args, **kwargs):
                raise AssertionError("unsafe request must fail before provider call")
        for request in (
            {"asset_url": "https://cos.invalid/file?signature=secret"},
            {"asset": "C:\\private\\input.mp4"},
            {"metadata": {"authorization": "Bearer secret"}},
        ):
            context = SimpleNamespace(job_id="job-1", request=request, candidates=CANDIDATES, capabilities=CAPABILITIES, deadline_at=123.0)
            with self.subTest(request=request), self.assertRaisesRegex(DirectorDecisionError, "director_request_unsafe"):
                generate_director_decision(context, Provider(), max_repairs=1)


class DirectorDecisionPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.db = root / "ai_edit_v3.db"
        self.v2 = root / "ai_edit_v2.db"
        self.v2.write_bytes(b"V2 identity marker")
        self.store = V3Store(self.db, v2_db_path=self.v2, environment="test")
        self.store.insert_pricing_version("price-v1", {}, status="published", created_at=1, published_at=1)
        self.store.insert_quote("alice", "quote-1", {}, pricing_version="price-v1", min_points=1, max_points=1, breakdown={}, expires_at=999, created_at=2)
        connection = open_store(self.db, v2_db_path=self.v2)
        try:
            connection.execute(
                """INSERT INTO edit_v3_jobs(job_id,environment,owner_id,state,
                   normalized_request_json,request_sha256,quote_id,idempotency_key,
                   created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                ("job-1", "test", "alice", "created_draft", "{}", request_fingerprint({}), "quote-1", "key-1", 3, 3),
            )
            connection.execute(
                "UPDATE edit_v3_jobs SET state='planning',worker_id='worker-1',fencing_token=7,lease_until=1000 WHERE job_id='job-1'"
            )
            connection.execute(
                """INSERT INTO edit_v3_stage_attempts(id,job_id,stage,attempt,worker_id,
                   fencing_token,status,input_sha256,started_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                ("attempt-planning", "job-1", "planning", 1, "worker-1", 7, "running", "d" * 64, 4),
            )
        finally:
            connection.close()
        self.claim = LeaseClaim("job-1", "worker-1", 7, 1000)

    @staticmethod
    def decision(value=None):
        value = valid_decision() if value is None else value
        encoded = canonical_json(value)
        return ValidatedDecision(
            value=value,
            provider_request_id="request-1",
            raw_output_json=encoded.decode("utf-8"),
            raw_output_sha256=hashlib.sha256(encoded).hexdigest(),
            decision_sha256=hashlib.sha256(encoded).hexdigest(),
            schema_sha256=schema_sha256("director-decision-v1.schema.json"),
            candidates_sha256="c" * 64,
        )

    def test_persistence_is_canonical_immutable_and_replay_skips_provider(self):
        stored = self.store.save_director_decision(self.claim, "attempt-planning", self.decision(), now_ms=10)
        replay = self.store.save_director_decision(self.claim, "attempt-planning", self.decision(), now_ms=11)
        self.assertEqual(stored, replay)
        self.assertEqual(canonical_json(valid_decision()).decode("utf-8"), stored["normalized_decision_json"])

        changed = valid_decision(); changed["creative_concept"] = "different"
        with self.assertRaisesRegex(StoreConflictError, "director_decision_conflict"):
            self.store.save_director_decision(self.claim, "attempt-planning", self.decision(changed), now_ms=12)

        with self.assertRaises(LeaseLost):
            self.store.save_director_decision(
                LeaseClaim("job-1", "worker-1", 6, 1000),
                "attempt-planning",
                self.decision(),
                now_ms=13,
            )

        class Provider:
            def generate_decision(self, *args, **kwargs):
                raise AssertionError("persisted replay must not call Qwen")
        context = SimpleNamespace(job_id="job-1", request={}, candidates=CANDIDATES, capabilities=CAPABILITIES, deadline_at=1.0)
        result = get_or_generate_director_decision(
            self.store,
            self.claim,
            "attempt-planning",
            context,
            Provider(),
            now_ms=13,
        )
        self.assertEqual(valid_decision(), result.value)


if __name__ == "__main__":
    unittest.main()
