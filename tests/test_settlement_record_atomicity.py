import unittest

from autosport.settlement import SettlementEngine


class SettlementRecordAtomicityTests(unittest.TestCase):
    def test_invalid_batch_does_not_partially_mutate_outcomes(self) -> None:
        settlement = SettlementEngine({"existing": "win"})
        before = dict(settlement.outcomes)

        with self.assertRaisesRegex(ValueError, "unsupported outcome: pending"):
            settlement.record({"new": "void", "bad": "pending"})

        self.assertEqual(settlement.outcomes, before)

    def test_conflicting_batch_does_not_partially_mutate_outcomes(self) -> None:
        settlement = SettlementEngine({"existing": "win"})
        before = dict(settlement.outcomes)

        with self.assertRaisesRegex(ValueError, "conflicting settlement for existing"):
            settlement.record({"new": "void", "existing": "loss"})

        self.assertEqual(settlement.outcomes, before)

    def test_idempotent_replay_and_new_outcome_commit_together(self) -> None:
        settlement = SettlementEngine({"existing": "win"})

        settlement.record({"existing": "win", "new": "void"})

        self.assertEqual(
            settlement.outcomes,
            {"existing": "win", "new": "void"},
        )


if __name__ == "__main__":
    unittest.main()
