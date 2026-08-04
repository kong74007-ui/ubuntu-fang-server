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
    generate_director_decision,
    validate_director_decision,
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


class DirectorDecisionValidationTests(unittest.TestCase):
    def test_valid_decision_is_canonical_and_complete(self):
        self.assertEqual(valid_decision(), validate_director_decision(valid_decision(), candidates=CANDIDATES, capabilities=CAPABILITIES))

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


class DirectorDecisionGenerationTests(unittest.TestCase):
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

    def test_second_invalid_response_fails(self):
        class Provider:
            def generate_decision(self, request, **kwargs):
                return {"invalid": True}
        context = SimpleNamespace(job_id="job-1", request={"safe": True}, candidates=CANDIDATES, capabilities=CAPABILITIES, deadline_at=123.0)
        with self.assertRaisesRegex(DirectorDecisionError, "director_decision_invalid"):
            generate_director_decision(context, Provider(), max_repairs=1)

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
