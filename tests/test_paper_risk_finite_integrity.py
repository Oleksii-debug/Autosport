import unittest
from decimal import Decimal, localcontext

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

    @staticmethod
    def _leg() -> TicketLeg:
        return TicketLeg("event-1", "market-1", "selection-1", Decimal("2"))

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
        ticket = book.open_ticket([self._leg()], "10")
        ticket.stake = Decimal("sNaN")

        decision = self._permissive_policy().evaluate(book, "1")

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "virtual bankroll state is invalid")

    def test_corrupt_ticket_status_cannot_silently_remove_committed_exposure(self) -> None:
        book = PaperBook("100")
        ticket = book.open_ticket([self._leg()], "40")
        ticket.status = "open"  # type: ignore[assignment]
        policy = PaperRiskPolicy(
            max_ticket_fraction=Decimal("0.50"),
            max_committed_fraction=Decimal("0.50"),
            minimum_cash_reserve_fraction=Decimal("0"),
        )

        decision = policy.evaluate(book, "20")

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "virtual bankroll state is invalid")

    def test_negative_ticket_stake_cannot_reduce_committed_exposure(self) -> None:
        book = PaperBook("100")
        ticket = book.open_ticket([self._leg()], "10")
        ticket.stake = Decimal("-9")

        decision = self._permissive_policy().evaluate(book, "10")

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "virtual bankroll state is invalid")

    def test_malformed_ticket_object_is_denied_without_attribute_error(self) -> None:
        book = PaperBook("100")
        book.tickets["bad"] = object()  # type: ignore[assignment]

        decision = self._permissive_policy().evaluate(book, "1")

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "virtual bankroll state is invalid")

    def test_aliased_ticket_identity_cannot_fabricate_valid_ledger_state(self) -> None:
        book = PaperBook("100")
        ticket = book.open_ticket([self._leg()], "10")
        book.tickets["alias"] = ticket
        # The durable loader rejects this duplicated identity, but the economic validator alone
        # would count the aliased open stake twice and accept this fabricated matching balance.
        book.balance = Decimal("80")

        decision = self._permissive_policy().evaluate(book, "1")

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "virtual bankroll state is invalid")

    def test_inflated_balance_breaking_ledger_equation_is_denied(self) -> None:
        book = PaperBook("100")
        book.open_ticket([self._leg()], "50")
        book.balance = Decimal("100")
        policy = PaperRiskPolicy(
            max_ticket_fraction=Decimal("0.50"),
            max_committed_fraction=Decimal("1.00"),
            minimum_cash_reserve_fraction=Decimal("0.40"),
        )

        decision = policy.evaluate(book, "20")

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "virtual bankroll state is invalid")

    def test_sub_precision_ledger_corruption_cannot_round_back_to_stored_balance(self) -> None:
        book = PaperBook("100")
        ticket = book.open_ticket([self._leg()], "10")
        ticket.stake = Decimal("10.000000000000000000000000001")

        decision = self._permissive_policy().evaluate(book, "1")

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "virtual bankroll state is invalid")

    def test_inexact_risk_limit_rounding_cannot_relax_ticket_cap(self) -> None:
        book = PaperBook("7")
        policy = PaperRiskPolicy(
            max_ticket_fraction=Decimal("0.14285714285714285714285714295"),
            max_committed_fraction=Decimal("1"),
            minimum_cash_reserve_fraction=Decimal("0"),
        )
        # Exact cap is 1.00000000000000000000000000065. At prec=28 this product
        # rounds upward to 1.000000000000000000000000001, which would admit this stake.
        stake = Decimal("1.0000000000000000000000000008")

        decision = policy.evaluate(book, stake)

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "virtual bankroll state is invalid")

    def test_finite_operands_whose_derived_risk_arithmetic_overflows_are_denied(self) -> None:
        book = PaperBook("9E+999999")
        book.open_ticket([self._leg()], "9E+999999")
        policy = PaperRiskPolicy(
            max_ticket_fraction=Decimal("1"),
            max_committed_fraction=Decimal("1"),
            minimum_cash_reserve_fraction=Decimal("0"),
        )

        decision = policy.evaluate(book, "9E+999999")

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "virtual bankroll state is invalid")

    def test_risk_arithmetic_does_not_depend_on_or_mutate_caller_decimal_context(self) -> None:
        book = PaperBook("100")
        policy = self._permissive_policy()

        with localcontext() as caller:
            caller.prec = 3
            caller.Emax = 1
            caller.Emin = -1
            caller.clear_flags()
            decision = policy.evaluate(book, "10")
            self.assertFalse(any(caller.flags.values()))

        self.assertTrue(decision.allowed)
        self.assertEqual(decision.reason, "allowed")

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
