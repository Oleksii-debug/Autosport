from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from autosport.agents import AgentContext, PaperBaselineAgent
from autosport.decision_ledger import JsonlDecisionLedger
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

    def test_explicit_back_preserves_exchange_side_and_semantics_identity(self) -> None:
        book = PaperBook("1000")
        agent = PaperBaselineAgent("50")
        context = AgentContext(book)
        event = replace(
            self._event("open", 1),
            sport="soccer",
            exchange_side="back",
            market_semantics_id="soccer:h2h:v1",
        )

        agent.on_market_event(event, context)

        self.assertEqual(book.balance, Decimal("950"))
        self.assertEqual(len(book.tickets), 1)
        ticket = next(iter(book.tickets.values()))
        leg = ticket.legs[0]
        self.assertEqual(leg.exchange_side, "back")
        self.assertEqual(leg.market_semantics_id, event.market_semantics_id)
        self.assertEqual(leg.quote_key, event.quote_key)

    def test_decision_evidence_binds_concrete_semantics_and_preserves_legacy_shape(self) -> None:
        for semantics in ("soccer:h2h:v1", None):
            with self.subTest(semantics=semantics), tempfile.TemporaryDirectory() as tmp:
                book = PaperBook("1000")
                ledger = JsonlDecisionLedger(Path(tmp) / "decision.jsonl")
                context = AgentContext(
                    book,
                    replay_run_id="replay-1",
                    decision_ledger=ledger,
                )
                agent = PaperBaselineAgent("50")
                event = replace(
                    self._event("open", 1),
                    sport="soccer",
                    exchange_side="back",
                    market_semantics_id=semantics,
                )

                agent.on_market_event(event, context)

                records = ledger.verified_records()
                self.assertEqual(len(records), 1)
                payload = records[0].payload
                if semantics is None:
                    self.assertNotIn("market_semantics_id", payload)
                else:
                    self.assertEqual(payload["market_semantics_id"], semantics)

    def test_explicit_lay_fails_closed_without_consuming_signal(self) -> None:
        book = PaperBook("1000")
        agent = PaperBaselineAgent("50")
        context = AgentContext(book)
        lay_event = replace(
            self._event("open", 1),
            sport="soccer",
            exchange_side="lay",
            market_semantics_id="soccer:h2h:v1",
        )
        back_event = replace(
            lay_event,
            exchange_side="back",
            sequence=2,
            observed_ts="2026-01-01T00:00:02+00:00",
        )

        agent.on_market_event(lay_event, context)

        self.assertEqual(book.balance, Decimal("1000"))
        self.assertEqual(book.tickets, {})

        agent.on_market_event(back_event, context)

        self.assertEqual(book.balance, Decimal("950"))
        self.assertEqual(len(book.tickets), 1)
        ticket = next(iter(book.tickets.values()))
        self.assertEqual(ticket.legs[0].exchange_side, "back")
        self.assertEqual(ticket.legs[0].market_semantics_id, back_event.market_semantics_id)
        self.assertEqual(ticket.legs[0].quote_key, back_event.quote_key)


if __name__ == "__main__":
    unittest.main()
