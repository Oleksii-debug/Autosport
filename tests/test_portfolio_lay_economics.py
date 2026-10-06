from __future__ import annotations

import unittest
from decimal import Decimal

import autosport.portfolio as portfolio_module
from autosport.domain import TicketLeg
from autosport.paper import PaperBook
from autosport.portfolio import PortfolioEngine


class PortfolioLayEconomicsTests(unittest.TestCase):
    @staticmethod
    def _lay_ticket():
        book = PaperBook("100")
        leg = TicketLeg(
            "event-1",
            "market-1",
            "selection-1",
            Decimal("3.00"),
            sport="soccer",
            exchange_side="lay",
            market_semantics_id="exchange.match.odds.v1",
        )
        ticket = book.open_ticket(
            [leg],
            "10",
            placed_at="2026-10-06T00:00:00+00:00",
        )
        return ticket, leg

    def test_scenario_profit_uses_lay_liability_when_selection_wins(self) -> None:
        ticket, leg = self._lay_ticket()

        profit = PortfolioEngine.scenario_profit(
            [ticket],
            {leg.settlement_key},
        )

        self.assertEqual(profit, Decimal("-20"))

    def test_scenario_profit_uses_lay_stake_when_selection_loses(self) -> None:
        ticket, leg = self._lay_ticket()

        profit = PortfolioEngine.scenario_profit(
            [ticket],
            set(),
        )

        self.assertEqual(profit, Decimal("10"))

    def test_scenario_profit_settlements_preserves_lay_net_pnl(self) -> None:
        ticket, leg = self._lay_ticket()

        self.assertEqual(
            PortfolioEngine.scenario_profit_settlements(
                [ticket],
                {leg.settlement_key: "win"},
            ),
            Decimal("-20"),
        )
        self.assertEqual(
            PortfolioEngine.scenario_profit_settlements(
                [ticket],
                {leg.settlement_key: "loss"},
            ),
            Decimal("10"),
        )
        self.assertEqual(
            PortfolioEngine.scenario_profit_settlements(
                [ticket],
                {leg.settlement_key: "void"},
            ),
            Decimal("0"),
        )

    def test_scenario_profit_rejects_raw_quote_key_for_semantic_lay_ticket(self) -> None:
        ticket, leg = self._lay_ticket()

        with self.assertRaisesRegex(ValueError, "missing terminal settlement evidence"):
            PortfolioEngine.scenario_profit_settlements(
                [ticket],
                {leg.quote_key: "win"},
            )

    def test_analyse_enumerates_lay_terminal_profit_and_liability_loss(self) -> None:
        ticket, _ = self._lay_ticket()

        report = PortfolioEngine(max_exact_states=8).analyse([ticket])

        self.assertEqual(report.scenario_count, 2)
        self.assertEqual(report.worst_case, Decimal("-20"))
        self.assertEqual(report.best_case, Decimal("10"))
        self.assertEqual(report.mean_case, Decimal("-5"))

    def test_affected_tickets_uses_settlement_identity(self) -> None:
        ticket, leg = self._lay_ticket()

        self.assertEqual(
            PortfolioEngine.affected_tickets([ticket], leg.settlement_key),
            [ticket.ticket_id],
        )
        self.assertEqual(
            PortfolioEngine.affected_tickets([ticket], leg.quote_key),
            [],
        )

    def test_multi_leg_lay_fails_closed_before_portfolio_evaluation(self) -> None:
        book = PaperBook("100")
        first = TicketLeg(
            "event-1",
            "market-1",
            "selection-1",
            Decimal("3.00"),
            exchange_side="lay",
            market_semantics_id="exchange.match.odds.v1",
        )
        second = TicketLeg(
            "event-2",
            "market-2",
            "selection-2",
            Decimal("2.00"),
            exchange_side="lay",
            market_semantics_id="exchange.match.odds.v1",
        )

        with self.assertRaisesRegex(
            ValueError,
            "single-leg LAY",
        ):
            book.open_ticket(
                [first, second],
                "10",
                placed_at="2026-10-06T00:00:00+00:00",
            )

    def test_portfolio_capital_authority_fails_closed_on_calculator_code_drift(self) -> None:
        calculator = portfolio_module.locked_capital_for_exchange_side
        original_code = calculator.__code__
        try:
            def hostile(*_args, **_kwargs):
                raise AssertionError("mutated calculator executed")

            calculator.__code__ = hostile.__code__
            ticket, _ = self._lay_ticket()
            with self.assertRaisesRegex(
                ValueError,
                "locked-capital exposure authority changed",
            ):
                PortfolioEngine.scenario_profit([ticket], set())
        finally:
            calculator.__code__ = original_code


if __name__ == "__main__":
    unittest.main()
