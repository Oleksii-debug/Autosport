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
    def test_market_store_is_idempotent_orders_and_scopes_quote_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteMarketStore(Path(tmp) / "market.db")
            first = MarketEvent.from_dict({"event_id":"e1","market_id":"m","selection_id":"a","decimal_odds":"1.8","observed_ts":"2026-01-01T00:00:00+00:00","source_id":"s","sequence":1})
            second = MarketEvent.from_dict({"event_id":"e1","market_id":"m","selection_id":"a","decimal_odds":"2.0","observed_ts":"2026-01-01T00:00:01+00:00","source_id":"s","sequence":2})
            same_provider_selection_other_event = MarketEvent.from_dict({"event_id":"e2","market_id":"m","selection_id":"a","decimal_odds":"3.0","observed_ts":"2026-01-01T00:00:01+00:00","source_id":"s","sequence":2})
            self.assertEqual(store.append_many([first, first, second, same_provider_selection_other_event]), 3)
            self.assertEqual([e.sequence for e in store.events("e1")], [1, 2])
            current = store.current()
            self.assertEqual(current[first.quote_key].decimal_odds, Decimal("2.0"))
            self.assertEqual(current[same_provider_selection_other_event.quote_key].decimal_odds, Decimal("3.0"))
            store.close()

    def test_replay_seals_future_result_and_has_stable_dataset_hash(self):
        event = MarketEvent.from_dict({"event_id":"e","market_id":"m","selection_id":"a","decimal_odds":"1.8","observed_ts":"2026-01-01T00:00:00+00:00","source_id":"s","sequence":1})
        firewall = ReplayLeakageFirewall({"e":"a"})
        with self.assertRaises(FutureLeakageError):
            firewall.result_for("e")
        first = ReplayEngine([event], firewall)
        second = ReplayEngine([event])
        self.assertEqual(first.dataset_hash, second.dataset_hash)
        run = first.run(lambda _event: None, run_id="fixed-run")
        self.assertEqual(run.event_count, 1)
        self.assertEqual(run.run_id, "fixed-run")
        self.assertEqual(firewall.result_for("e"), "a")

    def test_virtual_bank_settlement_and_restart_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            book = PaperBook("100")
            ticket = book.open_ticket([TicketLeg("e","m","a",Decimal("2.0"))], "10")
            self.assertEqual(book.balance, Decimal("90"))
            snapshot = Path(tmp) / "paper.json"
            book.save(snapshot)
            restored = PaperBook.load(snapshot)
            self.assertEqual(restored.balance, Decimal("90"))
            restored.settle(ticket.ticket_id, {"a"})
            self.assertEqual(restored.balance, Decimal("110.0"))

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
