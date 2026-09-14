import unittest
from decimal import Decimal

from autosport.domain import TicketLeg
from autosport.paper import PaperBook
from autosport.risk import PaperRiskPolicy


class PaperRiskFiniteIntegrityTests(unittest.TestCase):
    @staticmethod
    def _permissive_policy() -> PaperRiskPolicy:
        return PaperRiskPolicy(
            max_ticket_fraction=Decimal("0.50"),
            max_committed_fraction=Decimal("0.90"),
            minimum_cash_reserve_fraction=Decimal("0"),
        )

    def test_non_finite_or_malformed_stake_is_denied_without_exception(self) -> None:
        book = PaperBook("100")
        policy = self._permissive_policy()

        for value in ("NaN", "sNaN", "Infinity", "-Infinity", "not-a-decimal"):
            with self.subTest(value=value):
                decision = policy.evaluate(book, value)
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.reason, "stake must be a finite decimal")

        self.assertEqual(book.balance, Decimal("100"))
        self.assertEqual(book.tickets, {})

    def test_non_finite_book_financial_state_is_denied_without_exception(self) -> None:
        policy = self._permissive_policy()
        for field_name in ("initial_bankroll", "balance"):
            for value in (
                Decimal("NaN"),
                Decimal("sNaN"),
                Decimal("Infinity"),
                Decimal("-Infinity"),
            ):
                with self.subTest(field_name=field_name, value=str(value)):
                    book = PaperBook("100")
                    setattr(book, field_name, value)
                    decision = policy.evaluate(book, "1")
                    self.assertFalse(decision.allowed)
                    self.assertEqual(decision.reason, "virtual bankroll state is invalid")

    def test_noncanonical_or_negative_book_state_is_denied(self) -> None:
        policy = self._permissive_policy()
        cases = (
            ("initial_bankroll", Decimal("0")),
            ("initial_bankroll", "100"),
            ("balance", Decimal("-1")),
            ("balance", "100"),
        )
        for field_name, value in cases:
            with self.subTest(field_name=field_name, value=repr(value)):
                book = PaperBook("100")
                setattr(book, field_name, value)
                decision = policy.evaluate(book, "1")
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.reason, "virtual bankroll state is invalid")

    def test_corrupt_open_ticket_stake_is_denied_without_committed_stake_exception(self) -> None:
        book = PaperBook("100")
        ticket = book.open_ticket(
            [TicketLeg("event-1", "market-1", "selection-1", Decimal("2"))],
            "10",
        )
        ticket.stake = Decimal("sNaN")

        decision = self._permissive_policy().evaluate(book, "1")

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "virtual bankroll state is invalid")

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
