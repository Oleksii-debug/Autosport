import unittest
from decimal import Decimal

from autosport.paper import PaperBook
from autosport.risk import PaperRiskPolicy


class PaperRiskFiniteIntegrityTests(unittest.TestCase):
    def test_non_finite_or_malformed_stake_is_denied_without_exception(self) -> None:
        book = PaperBook("100")
        policy = PaperRiskPolicy(
            max_ticket_fraction=Decimal("0.50"),
            max_committed_fraction=Decimal("0.90"),
            minimum_cash_reserve_fraction=Decimal("0"),
        )

        for value in ("NaN", "sNaN", "Infinity", "-Infinity", "not-a-decimal"):
            with self.subTest(value=value):
                decision = policy.evaluate(book, value)
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.reason, "stake must be a finite decimal")

        self.assertEqual(book.balance, Decimal("100"))
        self.assertEqual(book.tickets, {})

    def test_non_finite_policy_fraction_is_rejected_at_construction(self) -> None:
        defaults = {
            "max_ticket_fraction": Decimal("0.02"),
            "max_committed_fraction": Decimal("0.20"),
            "minimum_cash_reserve_fraction": Decimal("0.20"),
        }
        for field_name in defaults:
            for value in (Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")):
                with self.subTest(field_name=field_name, value=str(value)):
                    values = dict(defaults)
                    values[field_name] = value
                    with self.assertRaisesRegex(ValueError, f"{field_name} must be a finite decimal"):
                        PaperRiskPolicy(**values)

    def test_policy_fraction_outside_unit_interval_is_rejected_at_construction(self) -> None:
        defaults = {
            "max_ticket_fraction": Decimal("0.02"),
            "max_committed_fraction": Decimal("0.20"),
            "minimum_cash_reserve_fraction": Decimal("0.20"),
        }
        for field_name in defaults:
            for value in (Decimal("-0.0001"), Decimal("1.0001")):
                with self.subTest(field_name=field_name, value=str(value)):
                    values = dict(defaults)
                    values[field_name] = value
                    with self.assertRaisesRegex(
                        ValueError,
                        f"{field_name} must be between 0 and 1 inclusive",
                    ):
                        PaperRiskPolicy(**values)

    def test_unit_interval_boundaries_remain_valid_configuration(self) -> None:
        policy = PaperRiskPolicy(
            max_ticket_fraction="0",
            max_committed_fraction="1",
            minimum_cash_reserve_fraction="1",
        )
        self.assertEqual(policy.max_ticket_fraction, Decimal("0"))
        self.assertEqual(policy.max_committed_fraction, Decimal("1"))
        self.assertEqual(policy.minimum_cash_reserve_fraction, Decimal("1"))
        self.assertFalse(policy.evaluate(PaperBook("100"), "1").allowed)

    def test_string_policy_fractions_normalize_and_finite_control_remains_allowed(self) -> None:
        policy = PaperRiskPolicy(
            max_ticket_fraction="0.50",
            max_committed_fraction="0.90",
            minimum_cash_reserve_fraction="0.10",
        )
        self.assertEqual(policy.max_ticket_fraction, Decimal("0.50"))
        self.assertEqual(policy.max_committed_fraction, Decimal("0.90"))
        self.assertEqual(policy.minimum_cash_reserve_fraction, Decimal("0.10"))
        self.assertTrue(policy.evaluate(PaperBook("100"), "10").allowed)


if __name__ == "__main__":
    unittest.main()
