from __future__ import annotations

import unittest
from dataclasses import replace
from decimal import Decimal

from autosport.agents import AgentContext, PaperBaselineAgent
from autosport.domain import MarketEvent, MarketType
from autosport.paper import PaperBook


class BaselineMarketStatusTruthTests(unittest.TestCase):
    def _event(self, status: str, sequence: int) -> MarketEvent:
        return MarketEvent(
            event_id="event-1",
            market_id="market-1",
            selection_id="selection-1",
            decimal_odds=Decimal("2.00"),
            observed_ts=f"2026-01-01T00:00:0{sequence}+00:00",
            source_id="fixture",
            sequence=sequence,
            market_type=MarketType.WINNER,
            status=status,
            metadata={"paper_signal": True, "paper_signal_id": "signal-1"},
        )

    def test_suspended_and_closed_signals_do_not_open_paper_tickets(self) -> None:
        for status in ("suspended", "closed"):
            with self.subTest(status=status):
                book = PaperBook("1000")
                agent = PaperBaselineAgent("50")
                context = AgentContext(book)

                agent.on_market_event(self._event(status, 1), context)

                self.assertEqual(book.balance, Decimal("1000"))
                self.assertEqual(book.tickets, {})

    def test_suspended_signal_is_not_consumed_before_market_reopens(self) -> None:
        book = PaperBook("1000")
        agent = PaperBaselineAgent("50")
        context = AgentContext(book)
        suspended = self._event("suspended", 1)
        reopened = replace(
            suspended,
            status="open",
            sequence=2,
            observed_ts="2026-01-01T00:00:02+00:00",
        )

        agent.on_market_event(suspended, context)
        agent.on_market_event(reopened, context)

        self.assertEqual(book.balance, Decimal("950"))
        self.assertEqual(len(book.tickets), 1)
        ticket = next(iter(book.tickets.values()))
        self.assertEqual(ticket.legs[0].quote_key, reopened.quote_key)
        self.assertEqual(ticket.placed_at, reopened.observed_ts)


if __name__ == "__main__":
    unittest.main()
