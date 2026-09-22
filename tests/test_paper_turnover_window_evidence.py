import unittest
from dataclasses import replace
from decimal import Decimal

from autosport.domain import MarketEvent, TicketLeg
from autosport.economic_goal import EconomicGoalContract
from autosport.paper import PaperBook
from autosport.risk import PaperRiskPolicy, ProposedTicketRiskContext


class PaperTurnoverWindowEvidenceTests(unittest.TestCase):
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
    def _policy(max_turnover_fraction: str) -> PaperRiskPolicy:
        goal = EconomicGoalContract(
            goal_id="goal-turnover-window",
            revision=1,
            bankroll_id="paper-bankroll",
            currency="USD",
            max_stake_fraction=Decimal("1"),
            max_session_loss_fraction=Decimal("1"),
            max_day_loss_fraction=Decimal("1"),
            max_drawdown_fraction=Decimal("1"),
            max_capital_at_risk_fraction=Decimal("1"),
            max_turnover_fraction=Decimal(max_turnover_fraction),
            max_risk_of_ruin=Decimal("1"),
            max_concurrent_positions=10,
        )
        return PaperRiskPolicy(
            max_ticket_fraction=Decimal("1"),
            max_committed_fraction=Decimal("1"),
            minimum_cash_reserve_fraction=Decimal("0"),
            economic_goal=goal,
        )

    @staticmethod
    def _open_and_void(
        book: PaperBook,
        leg: TicketLeg,
        stake: str,
        placed_at: str,
    ) -> None:
        ticket = book.open_ticket(
            [leg],
            Decimal(stake),
            placed_at=placed_at,
        )
        book.settle(
            ticket.ticket_id,
            set(),
            {ticket.legs[0].quote_key},
        )

    def test_causal_measurement_window_excludes_prior_paper_turnover(self) -> None:
        book = PaperBook("100")
        self._open_and_void(
            book,
            self._leg("prior"),
            "50",
            "2026-09-16T12:00:00+00:00",
        )
        policy = self._policy("0.05")
        context = self._context()

        self.assertTrue(policy.evaluate(book, Decimal("5"), context=context).allowed)
        blocked = policy.evaluate(book, Decimal("5.01"), context=context)
        self.assertFalse(blocked.allowed)
        self.assertEqual(blocked.reason, "economic goal turnover limit exceeded")

        all_history = replace(
            context,
            measurement_window_start=None,
            measurement_window_end=None,
        )
        blocked_all_history = policy.evaluate(
            book,
            Decimal("0.01"),
            context=all_history,
        )
        self.assertFalse(blocked_all_history.allowed)
        self.assertEqual(
            blocked_all_history.reason,
            "economic goal turnover limit exceeded",
        )

    def test_unanchored_measurement_window_cannot_narrow_turnover_history(self) -> None:
        book = PaperBook("100")
        self._open_and_void(
            book,
            self._leg("prior-unanchored"),
            "50",
            "2026-09-16T12:00:00+00:00",
        )
        policy = self._policy("0.05")
        unanchored = replace(self._context(), proposal_ts=None)

        blocked = policy.evaluate(book, Decimal("0.01"), context=unanchored)

        self.assertFalse(blocked.allowed)
        self.assertEqual(blocked.reason, "economic goal turnover limit exceeded")

    def test_turnover_window_boundaries_are_inclusive_by_ticket_placed_at(self) -> None:
        book = PaperBook("100")
        self._open_and_void(
            book,
            self._leg("start"),
            "2",
            "2026-09-16T14:00:00+00:00",
        )
        self._open_and_void(
            book,
            self._leg("end"),
            "2",
            "2026-09-16T15:00:00+00:00",
        )
        self._open_and_void(
            book,
            self._leg("before"),
            "40",
            "2026-09-16T13:59:59+00:00",
        )
        self._open_and_void(
            book,
            self._leg("after"),
            "40",
            "2026-09-16T15:00:01+00:00",
        )
        policy = self._policy("0.05")
        context = self._context()

        self.assertTrue(policy.evaluate(book, Decimal("1"), context=context).allowed)
        blocked = policy.evaluate(book, Decimal("1.01"), context=context)
        self.assertFalse(blocked.allowed)
        self.assertEqual(blocked.reason, "economic goal turnover limit exceeded")


if __name__ == "__main__":
    unittest.main()
