import unittest

from scripts.ai_edit_v3_acceptance import GateSummary, build_go_no_go


class GoNoGoDecisionTests(unittest.TestCase):
    def summary(self, passed=True, *blockers, capacity_blocked=False):
        return GateSummary(passed, tuple(blockers), capacity_blocked)

    def test_any_safety_failure_forces_no_go(self) -> None:
        decision = build_go_no_go(
            machine=self.summary(),
            human=self.summary(),
            faults=self.summary(False, "cross_owner_material"),
            capacity=self.summary(),
            regressions=self.summary(),
        )
        self.assertEqual("NO_GO", decision.status)
        self.assertEqual(("cross_owner_material",), decision.blockers)

    def test_all_pass_only_allows_production_review(self) -> None:
        decision = build_go_no_go(
            machine=self.summary(), human=self.summary(), faults=self.summary(),
            capacity=self.summary(), regressions=self.summary(),
        )
        self.assertEqual("GO_FOR_PRODUCTION_REVIEW", decision.status)
        self.assertEqual((), decision.blockers)

    def test_capacity_only_blockage_is_not_no_go_or_go(self) -> None:
        decision = build_go_no_go(
            machine=self.summary(), human=self.summary(), faults=self.summary(),
            capacity=self.summary(False, capacity_blocked=True),
            regressions=self.summary(),
        )
        self.assertEqual("CAPACITY_BLOCKED", decision.status)

    def test_every_non_capacity_gate_failure_is_no_go(self) -> None:
        names = ("machine", "human", "faults", "regressions")
        for failed_name in names:
            with self.subTest(gate=failed_name):
                gates = {name: self.summary() for name in (
                    "machine", "human", "faults", "capacity", "regressions"
                )}
                gates[failed_name] = self.summary(False, f"{failed_name}_failed")
                decision = build_go_no_go(**gates)
                self.assertEqual("NO_GO", decision.status)
                self.assertEqual((f"{failed_name}_failed",), decision.blockers)

    def test_capacity_block_does_not_hide_another_failure(self) -> None:
        decision = build_go_no_go(
            machine=self.summary(False, "machine_failed"),
            human=self.summary(), faults=self.summary(),
            capacity=self.summary(False, capacity_blocked=True),
            regressions=self.summary(),
        )
        self.assertEqual("NO_GO", decision.status)
        self.assertEqual(("machine_failed",), decision.blockers)

    def test_contradictory_or_unexplained_gate_is_rejected(self) -> None:
        invalid = (
            (True, ("unexpected_blocker",), False),
            (False, (), False),
            (True, (), True),
            (False, ("capacity_reason",), True),
        )
        for passed, blockers, capacity_blocked in invalid:
            with self.subTest(
                passed=passed, blockers=blockers,
                capacity_blocked=capacity_blocked,
            ):
                with self.assertRaises(ValueError):
                    GateSummary(passed, blockers, capacity_blocked)


if __name__ == "__main__":
    unittest.main()
