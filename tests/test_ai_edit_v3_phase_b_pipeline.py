from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

from server.content_domains.ai_edit_v3.pipeline import run_source_and_director_stages
from server.content_domains.ai_edit_v3.runtime import StageOutcome, build_phase_b_stage_handlers


class FakeRuntime:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def run_phase_b_stage(self, name: str, claim: object, db_path: Path) -> None:
        self.calls.append((name, claim.fencing_token))


class FakeCoordinator:
    def __init__(self):
        self.calls = []

    def run_stage(self, name, job, context):
        self.calls.append((name, job["job_id"], context.claim.fencing_token))
        transitions = {
            "generating_voice": "normalizing",
            "normalizing": "transcribing",
            "transcribing": "aligning",
            "aligning": "planning",
            "planning": "resolving_materials",
            "resolving_materials": "generating_images",
            "generating_images": "generating_audio",
        }
        return StageOutcome(
            transitions[name],
            {"artifact_sha256": "a" * 64, "skipped": name == "generating_voice"},
            job["stage_input_sha256"],
        )


class PhaseBPipelineContractTests(unittest.TestCase):
    def test_stages_are_checkpointed_in_frozen_order(self) -> None:
        runtime = FakeRuntime()
        claim = SimpleNamespace(job_id="j1", fencing_token=7)

        result = run_source_and_director_stages(
            claim,
            runtime,
            db_path=Path("ai_edit_v3.db"),
        )

        self.assertEqual(
            [name for name, _token in runtime.calls],
            [
                "generating_voice", "normalizing", "transcribing", "aligning",
                "planning", "resolving_materials", "generating_images",
            ],
        )
        self.assertTrue(all(token == 7 for _name, token in runtime.calls))
        self.assertEqual(result.next_stage, "generating_audio")

    def test_real_handler_map_keeps_pipeline_as_only_transition_writer(self) -> None:
        coordinator = FakeCoordinator()
        handlers = build_phase_b_stage_handlers(coordinator)
        self.assertEqual(
            tuple(handlers),
            (
                "generating_voice", "normalizing", "transcribing", "aligning",
                "planning", "resolving_materials", "generating_images",
            ),
        )
        context = SimpleNamespace(claim=SimpleNamespace(fencing_token=9), assert_active=lambda: None)
        job = {"job_id": "j1", "stage_input_sha256": "b" * 64}
        outcome = handlers["planning"](job, context)
        self.assertEqual(outcome.next_state, "resolving_materials")
        self.assertEqual(coordinator.calls, [("planning", "j1", 9)])


if __name__ == "__main__":
    unittest.main()
