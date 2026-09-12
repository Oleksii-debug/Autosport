from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from autosport.domain import EventKind, MarketEvent, MarketKey
from autosport.market_state import MarketState
from autosport.paper_book import PaperBook, TicketLeg, TicketStatus
from autosport.replay import CausalReplay


class CoreBootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.key_a = MarketKey("fixture", "m1", "winner", "a")
        self.key_b = MarketKey("fixture", "m1", "winner", "b")
        self.t0 = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)

    def event(self, event_id: str, seq: int, seconds: int, key: MarketKey, odds: str) -> MarketEvent:
        return MarketEvent(
            event_id=event_id,
            sequence=seq,
            observed_at=self.t0 + timedelta(seconds=seconds),
            key=key,
            kind=EventKind.ODDS,
            decimal_odds=Decimal(odds),
        )

    def test_replay_does_not_release_future_event(self) -> None:
        replay = CausalReplay([
            self.event("late", 2, 10, self.key_a, "2.20"),
            self.event("early", 1, 0, self.key_a, "1.60"),
        ])
        released = replay.advance_to(self.t0)
        self.assertEqual([item.event_id for item in released], ["early"])
        self.assertFalse(replay.finished)
        released = replay.advance_to(self.t0 + timedelta(seconds=10))
        self.assertEqual([item.event_id for item in released], ["late"])
        self.assertTrue(replay.finished)

    def test_event_driven_step_releases_equal_timestamp_batch(self) -> None:
        replay = CausalReplay([
            self.event("a", 1, 0, self.key_a, "1.60"),
            self.event("b", 2, 0, self.key_b, "2.30"),
            self.event("c", 3, 3, self.key_a, "1.80"),
        ])
        self.assertEqual({e.event_id for e in replay.step()}, {"a", "b"})
        self.assertEqual([e.event_id for e in replay.step()], ["c"])

    def test_market_projection_is_idempotent_and_rejects_rewind(self) -> None:
        state = MarketState()
        newer = self.event("newer", 10, 10, self.key_a, "1.90")
        self.assertTrue(state.apply(newer))
        self.assertFalse(state.apply(newer))
        with self.assertRaises(ValueError):
            state.apply(self.event("stale", 9, 11, self.key_a, "2.00"))
        self.assertEqual(state.get(self.key_a).decimal_odds, Decimal("1.90"))

    def test_paper_book_uses_exact_decimal_and_idempotent_settlement(self) -> None:
        book = PaperBook(Decimal("10000"))
        ticket = book.place(
            "t1",
            Decimal("50"),
            [TicketLeg("m1", "winner", "a", Decimal("1.50")), TicketLeg("m2", "winner", "b", Decimal("2.00"))],
        )
        self.assertEqual(ticket.quoted_odds, Decimal("3.0000"))
        self.assertEqual(book.cash, Decimal("9950"))
        won = book.settle("t1", TicketStatus.WON)
        self.assertEqual(won.payout, Decimal("150.0000"))
        self.assertEqual(book.cash, Decimal("10100.0000"))
        self.assertIs(book.settle("t1", TicketStatus.WON), won)


class AccessibilityContractTests(unittest.TestCase):
    def test_primary_ui_is_semantic_visible_and_copyable_by_default(self) -> None:
        root = Path(__file__).resolve().parents[1]
        html = (root / "web" / "index.html").read_text(encoding="utf-8")
        js = (root / "web" / "app.js").read_text(encoding="utf-8")
        compact = "".join(html.lower().split())
        self.assertIn("<main>", html.lower())
        self.assertIn("<h1>", html.lower())
        self.assertIn("<h2", html.lower())
        self.assertIn("<table", html.lower())
        self.assertIn('role="status"', html.lower())
        self.assertNotIn("user-select:none", compact)
        self.assertNotIn("preventdefault", js.lower())
        self.assertIn('id="market-summary"', html)
        self.assertIn('id="portfolio-summary"', html)
        self.assertIn("textContent", js)


if __name__ == "__main__":
    unittest.main()
