from __future__ import annotations

import unittest
from decimal import Decimal

from autosport.agents import AgentContext
from autosport.domain import MarketEvent, MarketType
from autosport.paper import PaperBook
from autosport.paper_strategy import Forecast, PaperValueAgent


class PaperStrategyPriceTruthTests(unittest.TestCase):
    def _event(self, *, execution_quote_verified: bool, semantics: str) -> MarketEvent:
        return MarketEvent(
            event_id="event-1",
            market_id="market-1",
            selection_id="selection-1",
            decimal_odds=Decimal("3.0"),
            observed_ts="2026-01-01T12:00:00+00:00",
            source_id="betfair_exchange_historical",
            sequence=1,
            market_type=MarketType.WINNER,
            status="open",
            source_ts="2026-01-01T12:00:00+00:00",
            ingest_ts="2026-01-02T12:00:00+00:00",
            metadata={
                "price_semantics": semantics,
                "execution_quote_verified": execution_quote_verified,
            },
        )

    def _agent(self, event: MarketEvent) -> PaperValueAgent:
        forecast = Forecast(
            quote_key=event.quote_key,
            probability=Decimal("0.50"),
            model_id="truth-test",
            as_of_ts="2026-01-01T11:59:00+00:00",
        )
        return PaperValueAgent({event.quote_key: forecast}, stake="50")

    def test_last_traded_price_without_execution_verification_cannot_open_ticket(self) -> None:
        event = self._event(
            execution_quote_verified=False,
            semantics="last_traded_price",
        )
        context = AgentContext(PaperBook("1000"))

        self._agent(event).on_market_event(event, context)

        self.assertEqual(context.paper_book.balance, Decimal("1000"))
        self.assertEqual(context.paper_book.tickets, {})

    def test_verified_executable_control_path_can_still_open_ticket(self) -> None:
        event = self._event(
            execution_quote_verified=True,
            semantics="executable_quote",
        )
        context = AgentContext(PaperBook("1000"))

        self._agent(event).on_market_event(event, context)

        self.assertEqual(len(context.paper_book.tickets), 1)
        ticket = next(iter(context.paper_book.tickets.values()))
        self.assertEqual(ticket.legs[0].locked_odds, Decimal("3.0"))
        self.assertEqual(context.paper_book.balance, Decimal("950"))


if __name__ == "__main__":
    unittest.main()
