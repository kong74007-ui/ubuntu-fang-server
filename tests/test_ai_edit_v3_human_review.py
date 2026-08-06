import json
import tempfile
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from server.content_domains.ai_edit_v3.acceptance_export import (
    DIMENSION_ANCHORS,
    HumanReview,
    build_blind_review_package,
    human_acceptance_passes,
    reconcile_human_reviews,
    validate_creative_distribution,
    validate_human_review,
)
from scripts.ai_edit_v3_acceptance import main


def scores(*, facts: int = 2, materials: int = 2, hook: int = 2,
           narrative: int = 1, layout: int = 1, captions: int = 2,
           audio: int = 2, visual: int = 1) -> dict[str, int]:
    return {
        "事实准确": facts,
        "素材相关": materials,
        "前三秒钩子": hook,
        "叙事节奏": narrative,
        "布局清晰": layout,
        "字幕可读": captions,
        "声音质量": audio,
        "视觉一致性": visual,
    }


REGISTRY_SHA256 = "24e8fe3df5763b1e95e621afb766316fc32e90494f64ebfdcdb757b9e8a395d0"
LAYOUT_VARIANTS = {
    "speaker_fullscreen": ("clean_center", "headline_top"),
    "speaker_left_info_right": ("card_stack", "number_focus"),
    "speaker_right_evidence_left": ("document_panel", "comparison_panel"),
    "material_fullscreen_speaker_pip": ("pip_round", "pip_card"),
    "product_hero": ("center_pedestal", "split_copy"),
    "editorial_collage": ("magazine_grid", "layered_cards"),
    "comparison_split": ("vertical_divide", "before_after_slider"),
    "steps_stack": ("vertical_steps", "numbered_cards"),
}


class HumanReviewTests(unittest.TestCase):
    def test_same_review_object_cannot_fill_both_reviewer_slots(self) -> None:
        review = HumanReview("reviewer-a", {"case_01": scores()})
        with self.assertRaisesRegex(ValueError, "^review_object_reused$"):
            reconcile_human_reviews(review, review, None)

    def test_duplicate_reviewer_id_is_rejected_for_distinct_objects(self) -> None:
        first = HumanReview("reviewer-a", {"case_01": scores()})
        second = HumanReview("reviewer-a", {"case_01": scores()})
        with self.assertRaisesRegex(ValueError, "^reviewer_id_reused$"):
            reconcile_human_reviews(first, second, None)
        with self.assertRaisesRegex(ValueError, "^reviewer_id_reused$"):
            reconcile_human_reviews(
                HumanReview("Reviewer-A", {"case_01": scores()}),
                HumanReview("reviewer-a", {"case_01": scores()}),
                None,
            )

    def test_two_reviewer_average_13_with_nonzero_critical_scores_passes(self) -> None:
        first = HumanReview("reviewer-a", {"case_01": scores()})
        second = HumanReview("reviewer-b", {"case_01": scores()})

        summary = reconcile_human_reviews(first, second, None)

        self.assertTrue(summary.cases["case_01"].publishable)
        self.assertEqual(summary.cases["case_01"].average_total, 13)

    def test_split_personal_decision_requires_unique_third_reviewer(self) -> None:
        first = HumanReview("reviewer-a", {"case_01": scores()})
        second = HumanReview(
            "reviewer-b",
            {"case_01": scores(materials=0, narrative=2)},
        )
        with self.assertRaisesRegex(ValueError, "^third_reviewer_required:case_01$"):
            reconcile_human_reviews(first, second, None)

    def test_three_reviewer_rule_uses_average_votes_and_all_critical_scores(self) -> None:
        first = HumanReview("reviewer-a", {"case_01": scores()})
        second = HumanReview(
            "reviewer-b",
            {"case_01": scores(materials=0, narrative=2)},
        )
        third = HumanReview("reviewer-c", {"case_01": scores()})

        summary = reconcile_human_reviews(first, second, third)

        self.assertFalse(summary.cases["case_01"].publishable)
        self.assertEqual(
            summary.cases["case_01"].reason,
            "critical_dimension_zero:素材相关",
        )

    def test_three_reviewer_average_13_and_two_personal_passes_is_publishable(self) -> None:
        first = HumanReview("reviewer-a", {"case_01": scores(narrative=2)})
        second = HumanReview("reviewer-b", {"case_01": scores(visual=0)})
        third = HumanReview("reviewer-c", {"case_01": scores()})

        summary = reconcile_human_reviews(first, second, third)

        self.assertTrue(summary.cases["case_01"].publishable)
        self.assertEqual(summary.cases["case_01"].average_total, 13)
        self.assertEqual(summary.cases["case_01"].personal_passes, 2)

    def test_third_reviewer_is_forbidden_when_primary_decisions_agree(self) -> None:
        with self.assertRaisesRegex(ValueError, "third_reviewer_not_required"):
            reconcile_human_reviews(
                HumanReview("reviewer-a", {"case_01": scores()}),
                HumanReview("reviewer-b", {"case_01": scores()}),
                HumanReview("reviewer-c", {}),
            )

    def test_literal_anchor_table_has_eight_dimensions_and_three_scores(self) -> None:
        self.assertEqual(
            tuple(DIMENSION_ANCHORS),
            (
                "事实准确", "素材相关", "前三秒钩子", "叙事节奏",
                "布局清晰", "字幕可读", "声音质量", "视觉一致性",
            ),
        )
        self.assertTrue(
            all(tuple(anchors) == (0, 1, 2) for anchors in DIMENSION_ANCHORS.values())
        )
        schema_path = (
            Path(__file__).parent / "fixtures" / "ai_edit_v3" / "human-review.schema.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        schema_anchors = set(
            schema["$defs"]["justification"]["properties"]["anchor"]["enum"]
        )
        self.assertEqual(
            schema_anchors,
            {anchor for anchors in DIMENSION_ANCHORS.values() for anchor in anchors.values()},
        )

    def test_review_file_requires_literal_anchors_justifications_and_exact_cases(self) -> None:
        case_scores = scores()
        payload = {
            "version": "1.0",
            "reviewer_id": "reviewer-a",
            "cases": {
                "case_01": {
                    "scores": case_scores,
                    "justifications": {
                        name: {
                            "anchor": DIMENSION_ANCHORS[name][score],
                            "note": f"case_01 {name} observed",
                        }
                        for name, score in case_scores.items()
                    },
                }
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "review.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            review = validate_human_review(
                path,
                expected_cases={"case_01"},
                reviewer_id="reviewer-a",
            )
            self.assertEqual(review.cases["case_01"], case_scores)

            payload["provider"] = "must-not-pass"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "human_review_json_invalid"):
                validate_human_review(path, expected_cases={"case_01"}, reviewer_id="reviewer-a")
            payload.pop("provider")

            payload["cases"]["case_01"]["extra"] = True
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "human_review_case_invalid"):
                validate_human_review(path, expected_cases={"case_01"}, reviewer_id="reviewer-a")
            payload["cases"]["case_01"].pop("extra")

            payload["cases"]["case_01"]["justifications"]["事实准确"]["prompt"] = "hidden"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "human_review_anchor_invalid"):
                validate_human_review(path, expected_cases={"case_01"}, reviewer_id="reviewer-a")
            payload["cases"]["case_01"]["justifications"]["事实准确"].pop("prompt")

            payload["cases"]["case_01"]["justifications"]["事实准确"]["anchor"] = "自定义"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "human_review_anchor_invalid"):
                validate_human_review(
                    path,
                    expected_cases={"case_01"},
                    reviewer_id="reviewer-a",
                )

    def test_creative_distribution_rejects_mechanical_template_package(self) -> None:
        registry_file = (
            Path(__file__).parents[1] / "server" / "ai_edit_v3_renderer" / "registry-sha256.txt"
        )
        self.assertEqual(
            registry_file.read_text(encoding="utf-8").strip(),
            f"sha256:{REGISTRY_SHA256}",
        )
        layout_ids = tuple(LAYOUT_VARIANTS)
        passing = []
        for index in range(32):
            layout = layout_ids[index % len(layout_ids)]
            passing.append({
                "layout": layout,
                "variant": LAYOUT_VARIANTS[layout][(index // len(layout_ids)) % 2],
                "registry_sha256": REGISTRY_SHA256,
            })
        self.assertTrue(validate_creative_distribution(passing).passed)

        mechanical = [
            {
                "layout": "speaker_fullscreen",
                "variant": LAYOUT_VARIANTS["speaker_fullscreen"][index % 2],
                "registry_sha256": REGISTRY_SHA256,
            }
            for index in range(20)
        ]
        report = validate_creative_distribution(mechanical)
        self.assertFalse(report.passed)
        self.assertIn("layout_diversity_below_8", report.errors)
        self.assertIn("layout_share_above_35_percent:speaker_fullscreen", report.errors)

        malformed = passing.copy()
        malformed[0] = {"layout": "layout_alias", "variant": "color_blue"}
        report = validate_creative_distribution(malformed)
        self.assertIn("layout_unknown:0", report.errors)
        self.assertIn("layout_registry_mismatch:0", report.errors)

    def test_twenty_case_gate_requires_at_least_sixteen_publishable(self) -> None:
        first_cases = {}
        second_cases = {}
        for index in range(1, 21):
            case_id = f"case_{index:02d}"
            value = scores() if index <= 16 else scores(hook=0, narrative=0)
            first_cases[case_id] = value
            second_cases[case_id] = dict(value)
        summary = reconcile_human_reviews(
            HumanReview("reviewer-a", first_cases),
            HumanReview("reviewer-b", second_cases),
            None,
        )

        self.assertEqual(summary.publishable_count, 16)
        self.assertTrue(human_acceptance_passes(summary, expected_cases=20))

        failing = dict(second_cases)
        failing["case_16"] = scores(hook=0, narrative=0)
        first_failing = dict(first_cases)
        first_failing["case_16"] = scores(hook=0, narrative=0)
        summary = reconcile_human_reviews(
            HumanReview("reviewer-a", first_failing),
            HumanReview("reviewer-b", failing),
            None,
        )
        self.assertFalse(human_acceptance_passes(summary, expected_cases=20))

    def test_invalid_or_self_reviewer_ids_are_rejected(self) -> None:
        for reviewer_id in ("ai", "self", "bad id", "../reviewer"):
            with self.subTest(reviewer_id=reviewer_id):
                with self.assertRaisesRegex(ValueError, "reviewer_id_invalid"):
                    reconcile_human_reviews(
                        HumanReview(reviewer_id, {"case_01": scores()}),
                        HumanReview("reviewer-b", {"case_01": scores()}),
                        None,
                    )

    def test_blind_package_uses_a_strict_allowlist(self) -> None:
        cases = [{
            "case_id": f"case_{index:02d}",
            "media_filename": "hyperframes_secret-template-x.mp4",
            "renderer": "hyperframes",
            "template_id": "secret-template",
            "prompt": "secret prompt",
            "provider": "secret provider",
            "implementation": {"layout": "speaker_fullscreen"},
        } for index in range(1, 21)]
        package = build_blind_review_package(cases)

        self.assertEqual(len(package["cases"]), 20)
        self.assertEqual(package["cases"][0], {
            "case_id": "case_01", "media_filename": "blind_001.mp4",
        })
        self.assertNotIn("secret", json.dumps(package))
        with self.assertRaisesRegex(ValueError, "blind_package_case_set_invalid"):
            build_blind_review_package(cases[:-1])

    def test_blind_export_cli_never_persists_implementation_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory_text:
            directory = Path(directory_text)
            source = directory / "source.json"
            output = directory / "blind.json"
            source.write_text(json.dumps({"cases": [{
                "case_id": f"case_{index:02d}",
                "media_filename": "hyperframes_secret-template-x.mp4",
                "renderer": "hyperframes",
                "template_id": "template-secret",
                "prompt": "prompt-secret",
                "provider": "provider-secret",
            } for index in range(1, 21)]}), encoding="utf-8")

            self.assertEqual(main([
                "blind-export", "--source", str(source), "--output", str(output),
            ]), 0)
            raw = output.read_text(encoding="utf-8")
            self.assertNotIn("hyperframes", raw)
            self.assertNotIn("secret", raw)
            self.assertEqual(main([
                "blind-export", "--source", str(source), "--output", str(output),
            ]), 4)


if __name__ == "__main__":
    unittest.main()
