import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from autosport.domain import MarketEvent, TicketLeg, TicketStatus
from autosport.market_bus import MarketEventBus
from autosport.paper import PaperBook
from autosport.risk import PaperRiskPolicy
from autosport.settlement import SettlementEngine
from autosport.storage import SQLiteMarketStore


class SettlementRiskBusTests(unittest.TestCase):
    def test_multievent_parlay_settles_only_when_resolved_or_loss_known(self):
        book = PaperBook("1000")
        a = TicketLeg("e1", "winner", "a", Decimal("2"))
        b = TicketLeg("e2", "winner", "b", Decimal("3"))
        ticket = book.open_ticket([a, b], "10")
        settlement = SettlementEngine()
        settlement.record({a.quote_key: "win"})
        self.assertEqual(settlement.settle_ready(book), [])
        settlement.record({b.quote_key: "win"})
        self.assertEqual(settlement.settle_ready(book), [ticket.ticket_id])
        self.assertEqual(ticket.status, TicketStatus.WON)
        self.assertEqual(book.balance, Decimal("1050"))

    def test_loss_can_settle_parlay_before_other_legs_finish(self):
        book = PaperBook("1000")
        a = TicketLeg("e1", "winner", "a", Decimal("2"))
        b = TicketLeg("e2", "winner", "b", Decimal("3"))
        ticket = book.open_ticket([a, b], "10")
        settlement = SettlementEngine({a.quote_key: "loss"})
        self.assertEqual(settlement.settle_ready(book), [ticket.ticket_id])
        self.assertEqual(ticket.status, TicketStatus.LOST)

    def test_risk_policy_caps_virtual_exposure(self):
        book = PaperBook("10000")
        policy = PaperRiskPolicy()
        self.assertTrue(policy.evaluate(book, "50").allowed)
        self.assertFalse(policy.evaluate(book, "500").allowed)

    def test_bus_persists_before_notifying_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteMarketStore(Path(tmp) / "bus.db")
            bus = MarketEventBus(store)
            received = []
            bus.subscribe(received.append)
            event = MarketEvent.from_dict({"event_id":"e","market_id":"m","selection_id":"a","decimal_odds":"1.8","observed_ts":"2026-01-01T00:00:00+00:00","source_id":"s","sequence":1})
            self.assertTrue(bus.publish(event))
            self.assertFalse(bus.publish(event))
            self.assertEqual(len(received), 1)
            self.assertEqual(len(store.events()), 1)
            store.close()


if __name__ == "__main__":
    unittest.main()
