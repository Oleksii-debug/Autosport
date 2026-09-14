import unittest
from decimal import Decimal

from autosport.agents import PaperBaselineAgent
from autosport.paper_strategy import PaperValueAgent


class PaperStrategyConfigIntegrityTests(unittest.TestCase):
    def test_baseline_rejects_malformed_or_nonfinite_stake_at_construction(self) -> None:
        for value in ("NaN", "sNaN", "Infinity", "-Infinity", "not-a-decimal"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    ValueError,
                    "paper baseline stake must be a finite decimal",
                ):
                    PaperBaselineAgent(value)

    def test_baseline_rejects_nonpositive_stake_at_construction(self) -> None:
        for value in ("0", "-0.01"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "paper baseline stake must be positive"):
                    PaperBaselineAgent(value)

    def test_value_strategy_rejects_malformed_or_nonfinite_stake_at_construction(self) -> None:
        for value in ("NaN", "sNaN", "Infinity", "-Infinity", "not-a-decimal"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    ValueError,
                    "paper value stake must be a finite decimal",
                ):
                    PaperValueAgent({}, stake=value)

    def test_value_strategy_rejects_nonpositive_stake_at_construction(self) -> None:
        for value in ("0", "-0.01"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "paper value stake must be positive"):
                    PaperValueAgent({}, stake=value)

    def test_value_strategy_rejects_malformed_or_nonfinite_minimum_edge(self) -> None:
        for value in ("NaN", "sNaN", "Infinity", "-Infinity", "not-a-decimal"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    ValueError,
                    "minimum expected profit per unit must be a finite decimal",
                ):
                    PaperValueAgent({}, minimum_expected_profit_per_unit=value)

    def test_valid_numeric_configuration_is_normalized_to_decimal(self) -> None:
        baseline = PaperBaselineAgent("12.50")
        value = PaperValueAgent(
            {},
            stake="7.25",
            minimum_expected_profit_per_unit="-0.10",
        )
        self.assertEqual(baseline.stake, Decimal("12.50"))
        self.assertEqual(value.stake, Decimal("7.25"))
        self.assertEqual(value.minimum_edge, Decimal("-0.10"))


if __name__ == "__main__":
    unittest.main()
