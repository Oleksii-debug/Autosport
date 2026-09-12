import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from autosport.domain import MarketEvent, TicketLeg
from autosport.paper import PaperBook
from autosport.portfolio import PortfolioEngine
from autosport.replay import FutureLeakageError, ReplayEngine, ReplayLeakageFirewall
from autosport.storage import SQLiteMarketStore


class CoreTests(unittest.TestCase):
    def test_market_store_is_idempotent_and_orders_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteMarketStore(Path(tmp) / "market.db")
            first = MarketEvent.from_dict({"event_id":"e","market_id":"m","selection_id":"a","decimal_odds":"1.8","observed_ts":"2026-01-01T00:00:00+00:00","source_id":"s","sequence":1})
            second = MarketEvent.from_dict({"event_id":"e","market_id":"m","selection_id":"a","decimal_odds":"2.0","observed_ts":"2026-01-01T00:00:01+00:00","source_id":"s","sequence":2})
            self.assertTrue(store.append(first))
            self.assertFalse(store.append(first))
            self.assertTrue(store.append(second))
            self.assertEqual([e.sequence for e in store.events()], [1, 2])
            self.assertEqual(store.current()["a"].decimal_odds, Decimal("2.0"))
            store.close()

    def test_replay_seals_future_result_until_completion(self):
        event = MarketEvent.from_dict({"event_id":"e","market_id":"m","selection_id":"a","decimal_odds":"1.8","observed_ts":"2026-01-01T00:00:00+00:00","source_id":"s","sequence":1})
        firewall = ReplayLeakageFirewall({"e":"a"})
        with self.assertRaises(FutureLeakageError):
            firewall.result_for("e")
        ReplayEngine([event], firewall).run(lambda _event: None)
        self.assertEqual(firewall.result_for("e"), "a")

    def test_virtual_bank_and_settlement(self):
        book = PaperBook("100")
        ticket = book.open_ticket([TicketLeg("e","m","a",Decimal("2.0"))], "10")
        self.assertEqual(book.balance, Decimal("90"))
        book.settle(ticket.ticket_id, {"a"})
        self.assertEqual(book.balance, Decimal("110.0"))

    def test_portfolio_exact_bounds_for_exclusive_match(self):
        book = PaperBook("100")
        a = book.open_ticket([TicketLeg("e","m","a",Decimal("2.0"))], "10")
        b = book.open_ticket([TicketLeg("e","m","b",Decimal("3.0"))], "10")
        report = PortfolioEngine().analyse([a, b], exclusive_groups=[{"a", "b"}])
        self.assertEqual(report.mode, "exact")
        self.assertEqual(report.scenario_count, 2)
        self.assertEqual(report.worst_case, Decimal("0.0"))
        self.assertEqual(report.best_case, Decimal("10.0"))


if __name__ == "__main__":
    unittest.main()
