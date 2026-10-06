from __future__ import annotations

import unittest
from decimal import (
    Decimal,
    Inexact,
    InvalidOperation,
    Overflow,
    ROUND_DOWN,
    Underflow,
    localcontext,
)

from autosport.domain import PaperTicket, TicketLeg, TicketStatus
from autosport.paper import PaperBook
from autosport.portfolio import PortfolioEngine


class PortfolioDecimalContextIntegrityTests(unittest.TestCase):
    @staticmethod
    def _multi_leg_ticket() -> tuple[PaperBook, PaperTicket]:
        book = PaperBook("1000")
        legs = tuple(
            TicketLeg(
                event_id=f"event-{index}",
                market_id="winner",
                selection_id="a",
                locked_odds=Decimal("1.234567890123456789"),
            )
            for index in range(8)
        )
        return book, book.open_ticket(legs, "10", placed_at="2026-01-01T00:00:00+00:00")

    @staticmethod
    def _hostile_caller_report(
        engine: PortfolioEngine,
        ticket: PaperTicket,
        *,
        precision: int,
    ):
        with localcontext() as caller:
            caller.prec = precision
            caller.rounding = ROUND_DOWN
            caller.Emin = -9
            caller.Emax = 9
            caller.traps[Inexact] = True
            caller.traps[InvalidOperation] = False
            caller.traps[Overflow] = False
            caller.traps[Underflow] = False
            caller.clear_flags()
            report = engine.analyse([ticket])
            caller_flags = tuple(caller.flags.values())
        return report, caller_flags

    def test_exact_report_is_independent_of_caller_decimal_context(self) -> None:
        _book, ticket = self._multi_leg_ticket()
        engine = PortfolioEngine()
        expected = engine.analyse([ticket])

        for precision in (6, 10, 28, 50):
            with self.subTest(precision=precision):
                actual, caller_flags = self._hostile_caller_report(
                    engine,
                    ticket,
                    precision=precision,
                )
                self.assertEqual(actual, expected)
                self.assertFalse(any(caller_flags))

    def test_approximate_report_is_independent_of_caller_decimal_context(self) -> None:
        _book, ticket = self._multi_leg_ticket()
        engine = PortfolioEngine(max_exact_states=1, sample_count=37, seed=11)
        expected = engine.analyse([ticket])

        for precision in (6, 10, 28, 50):
            with self.subTest(precision=precision):
                actual, caller_flags = self._hostile_caller_report(
                    engine,
                    ticket,
                    precision=precision,
                )
                self.assertEqual(actual, expected)
                self.assertFalse(any(caller_flags))

    def test_scenario_profit_matches_canonical_paper_settlement_economics(self) -> None:
        book, ticket = self._multi_leg_ticket()
        winning_quote_keys = {leg.quote_key for leg in ticket.legs}

        scenario_profit = PortfolioEngine.scenario_profit(
            [ticket],
            winning_quote_keys,
        )
        settled = book.settle(ticket.ticket_id, winning_quote_keys)

        self.assertEqual(settled.status, TicketStatus.WON)
        self.assertEqual(scenario_profit, settled.payout - settled.stake)

    def test_nonrepresentable_profit_fails_closed_under_nontrapping_caller(self) -> None:
        legs = (
            TicketLeg("event-a", "winner", "a", Decimal("9")),
            TicketLeg("event-b", "winner", "b", Decimal("9")),
        )
        ticket = PaperTicket(
            ticket_id="overflow-ticket",
            stake=Decimal("1E+999999"),
            legs=legs,
            placed_at="2026-01-01T00:00:00+00:00",
        )

        with localcontext() as caller:
            caller.Emax = 999999999
            caller.traps[Overflow] = False
            caller.clear_flags()
            with self.assertRaisesRegex(
                ValueError,
                "portfolio economics are not representable",
            ):
                PortfolioEngine.scenario_profit(
                    [ticket],
                    {leg.quote_key for leg in legs},
                )
            caller_flags = tuple(caller.flags.values())

        self.assertFalse(any(caller_flags))

    def test_nonfinite_mutable_ticket_economics_fail_closed(self) -> None:
        ticket = PaperTicket(
            ticket_id="nonfinite-ticket",
            stake=Decimal("NaN"),
            legs=(TicketLeg("event", "winner", "a", Decimal("2")),),
            placed_at="2026-01-01T00:00:00+00:00",
        )

        with self.assertRaisesRegex(ValueError, "stake must be a finite Decimal"):
            PortfolioEngine.scenario_profit([ticket], set())


if __name__ == "__main__":
    unittest.main()
