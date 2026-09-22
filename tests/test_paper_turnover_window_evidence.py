import unittest
from dataclasses import replace
from decimal import Decimal

from autosport.domain import MarketEvent, TicketLeg
from autosport.economic_goal import EconomicGoalContract
from autosport.paper import PaperBook
from autosport.risk import PaperRiskPolicy, ProposedTicketRiskContext


class PaperTurnoverWindowAuthorityGuardTests(unittest.TestCase):
    @staticmethod
    def _leg(suffix: str) -> TicketLeg:
        return TicketLeg(
            f"event-{suffix}",
            f"market-{suffix}",
            f"selection-{suffix}",
            Decimal("2"),
        )

    @classmethod
    def _context(cls) -> ProposedTicketRiskContext:
        leg = cls._leg("candidate")
        quote = MarketEvent(
            event_id=leg.event_id,
            market_id=leg.market_id,
            selection_id=leg.selection_id,
            decimal_odds=leg.locked_odds,
            observed_ts="2026-09-16T15:00:00+00:00",
            source_id="provider-1",
            sequence=1,
            source_ts="2026-09-16T14:59:59+00:00",
            ingest_ts="2026-09-16T15:00:01+00:00",
        )
        return ProposedTicketRiskContext(
            legs=(leg,),
            quotes=(quote,),
            bankroll_id="paper-bankroll",
            currency="USD",
            measurement_window_start="2026-09-16T14:00:00+00:00",
            measurement_window_end="2026-09-16T15:00:00+00:00",
            proposal_ts="2026-09-16T15:00:02+00:00",
        )

    @staticmethod
    def _policy() -> PaperRiskPolicy:
        goal = EconomicGoalContract(
            goal_id="goal-turnover-window-authority",
            revision=1,
            bankroll_id="paper-bankroll",
            currency="USD",
            max_stake_fraction=Decimal("1"),
            max_session_loss_fraction=Decimal("1"),
            max_day_loss_fraction=Decimal("1"),
            max_drawdown_fraction=Decimal("1"),
            max_capital_at_risk_fraction=Decimal("1"),
            max_turnover_fraction=Decimal("0.05"),
            max_risk_of_ruin=Decimal("1"),
            max_concurrent_positions=10,
        )
        return PaperRiskPolicy(
            max_ticket_fraction=Decimal("1"),
            max_committed_fraction=Decimal("1"),
            minimum_cash_reserve_fraction=Decimal("0"),
            economic_goal=goal,
        )

    @classmethod
    def _book_with_old_turnover(cls) -> PaperBook:
        book = PaperBook("100")
        ticket = book.open_ticket(
            [cls._leg("prior")],
            Decimal("50"),
            placed_at="2026-09-16T12:00:00+00:00",
        )
        book.settle(ticket.ticket_id, set(), {ticket.legs[0].quote_key})
        return book

    def test_caller_measurement_window_cannot_mint_turnover_headroom(self) -> None:
        decision = self._policy().evaluate(
            self._book_with_old_turnover(),
            Decimal("0.01"),
            context=self._context(),
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "economic goal turnover limit exceeded")

    def test_unanchored_caller_window_cannot_mint_turnover_headroom(self) -> None:
        decision = self._policy().evaluate(
            self._book_with_old_turnover(),
            Decimal("0.01"),
            context=replace(self._context(), proposal_ts=None),
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(decision.reason, "economic goal turnover limit exceeded")


if __name__ == "__main__":
    unittest.main()
