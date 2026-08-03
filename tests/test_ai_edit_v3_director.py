from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from server.content_domains.ai_edit_v3.director import (
    DirectorError,
    extract_single_json,
    generate_edit_plan,
    validate_edit_plan,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "ai_edit_v3" / "valid-edit-plan-2.0.json"


def valid_timeline():
    return SimpleNamespace(
        duration_ms=4000,
        captions=(
            SimpleNamespace(id="caption_01", start_ms=0, end_ms=2000, text="真实方法"),
            SimpleNamespace(id="caption_02", start_ms=2000, end_ms=4000, text="提升效率"),
        ),
    )


def capabilities():
    return {
        "layout_capabilities": ["speaker_fullscreen", "speaker_right_evidence_left"],
        "overlay_capabilities": ["headline_block", "info_card"],
        "animation_capabilities": ["fade", "slide"],
        "transition_capabilities": ["hard_cut", "soft_wipe"],
        "theme_capabilities": {
            "palette_id": ["midnight_gold"],
            "typography_id": ["editorial_sans"],
            "density": ["balanced"],
            "motion_energy": ["medium"],
            "image_fit": ["cover"],
        },
    }


class FakeDirector:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.calls: list[dict[str, object]] = []

    def generate_plan(self, request: object, **kwargs: object) -> object:
        self.calls.append({"request": request, **kwargs})
        return self.responses[len(self.calls) - 1]


class DirectorContractTests(unittest.TestCase):
    def test_invalid_primary_gets_one_repair_and_invalid_repair_fails(self) -> None:
        provider = FakeDirector([{"version": "bad"}, {"version": "still-bad"}])
        context = SimpleNamespace(
            request={"transcript_sha256": "abc"},
            timeline=SimpleNamespace(duration_ms=1000, captions=()),
            capabilities={},
            job_id="j1",
            deadline_at=100.0,
        )

        with self.assertRaisesRegex(DirectorError, "director_schema_invalid"):
            generate_edit_plan(context, provider)

        self.assertEqual([call["purpose"] for call in provider.calls], ["initial", "repair"])
        self.assertNotEqual(provider.calls[0]["request"], provider.calls[1]["request"])

    def test_extract_single_json_is_strict_and_bounded(self) -> None:
        self.assertEqual(extract_single_json(b'{"version":"2.0"}'), {"version": "2.0"})
        bad_cases = {
            "duplicate": '{"a":1,"a":2}',
            "nan": '{"a":NaN}',
            "trailing": '{"a":1} text',
            "multiple": '{}{}',
            "fence": '```json\n{}\n```',
            "string": json.dumps({"a": "x" * 4001}),
            "depth": "[" * 25 + "0" + "]" * 25,
        }
        for name, raw in bad_cases.items():
            with self.subTest(name=name):
                with self.assertRaises(DirectorError):
                    extract_single_json(raw)
        with self.assertRaisesRegex(DirectorError, "director_json_too_large"):
            extract_single_json(" " * (512 * 1024 + 1))

    def test_schema_and_cross_field_validation_use_frozen_contract(self) -> None:
        plan = json.loads(FIXTURE.read_text(encoding="utf-8"))
        result = validate_edit_plan(plan, timeline=valid_timeline(), capabilities=capabilities())
        self.assertEqual(result, plan)
        changed = copy.deepcopy(plan)
        changed["captions"][0]["text"] = "虚假方法"
        with self.assertRaisesRegex(DirectorError, "accurate_text_changed"):
            validate_edit_plan(changed, timeline=valid_timeline(), capabilities=capabilities())
        smuggled = copy.deepcopy(plan)
        smuggled["scenes"][0]["css"] = "url(file:///etc/passwd)"
        with self.assertRaisesRegex(DirectorError, "director_schema_invalid"):
            validate_edit_plan(smuggled, timeline=valid_timeline(), capabilities=capabilities())

    def test_valid_repair_is_accepted_once(self) -> None:
        plan = json.loads(FIXTURE.read_text(encoding="utf-8"))
        provider = FakeDirector([{"version": "bad"}, json.dumps(plan, ensure_ascii=False)])
        context = SimpleNamespace(
            request={"transcript_sha256": "abc", "frozen": True},
            timeline=valid_timeline(),
            capabilities=capabilities(),
            job_id="j2",
            deadline_at=100.0,
        )

        result = generate_edit_plan(context, provider)

        self.assertEqual(result.value, plan)
        self.assertEqual(result.provider_request_id, None)
        self.assertEqual([call["purpose"] for call in provider.calls], ["initial", "repair"])
        repair = provider.calls[1]["request"]
        self.assertEqual(repair["frozen_request"], context.request)
        self.assertNotIn("raw_output", repair)


if __name__ == "__main__":
    unittest.main()
