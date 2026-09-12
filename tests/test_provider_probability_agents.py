import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from autosport.agents import AgentContext, AgentOrchestrator, MarketMirrorAgent
from autosport.domain import MarketEvent, MarketType
from autosport.evidence import EvidenceItem
from autosport.ingestion import IngestionEngine
from autosport.market_bus import MarketEventBus
from autosport.paper import PaperBook
from autosport.paper_strategy import Forecast, PaperValueAgent
from autosport.probability import ForecastObservation, brier_score, log_loss, normalize_two_or_more_way_market, paper_value
from autosport.providers import InMemoryProvider, ProviderQuote
from autosport.storage import SQLiteMarketStore


class ProviderProbabilityAgentTests(unittest.TestCase):
    def test_provider_batch_normalizes_persists_and_notifies_only_new_events(self):
        quotes = [
            ProviderQuote("event-1", "winner", "a", Decimal("1.8"), "2026-01-01T00:00:00+00:00", 1, MarketType.WINNER),
            ProviderQuote("event-1", "winner", "b", Decimal("2.2"), "2026-01-01T00:00:00+00:00", 2, MarketType.WINNER),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteMarketStore(Path(tmp) / "provider.db")
            bus = MarketEventBus(store)
            received = []
            bus.subscribe(received.append)
            engine = IngestionEngine(bus)
            provider = InMemoryProvider("source-x", quotes)
            stats = engine.poll_once(provider, max_items=10)
            self.assertEqual(stats.received, 2)
            self.assertEqual(stats.accepted, 2)
            self.assertEqual(len(received), 2)
            self.assertEqual(received[0].event_id, "source-x:event-1")
            self.assertEqual(len(store.events()), 2)
            store.close()

    def test_market_probability_normalization_and_value(self):
        quotes = [
            MarketEvent.from_dict({"event_id":"e","market_id":"m","selection_id":"a","decimal_odds":"2.0","observed_ts":"2026-01-01T00:00:00+00:00","source_id":"s","sequence":1}),
            MarketEvent.from_dict({"event_id":"e","market_id":"m","selection_id":"b","decimal_odds":"2.0","observed_ts":"2026-01-01T00:00:00+00:00","source_id":"s","sequence":2}),
        ]
        normalized = normalize_two_or_more_way_market(quotes)
        self.assertEqual(normalized[quotes[0].quote_key].fair_probability, Decimal("0.5"))
        estimate = paper_value(quotes[0].quote_key, "0.60", "2.0")
        self.assertEqual(estimate.expected_profit_per_unit, Decimal("0.200"))

    def test_calibration_metrics(self):
        observations = [ForecastObservation(0.8, 1), ForecastObservation(0.2, 0)]
        self.assertAlmostEqual(brier_score(observations), 0.04)
        self.assertGreater(log_loss(observations), 0)

    def test_paper_value_agent_opens_only_virtual_ticket(self):
        event = MarketEvent.from_dict({"event_id":"e","market_id":"m","selection_id":"a","decimal_odds":"2.0","observed_ts":"2026-01-01T00:00:01+00:00","source_id":"s","sequence":1})
        forecast = Forecast(event.quote_key, Decimal("0.70"), "model-1", "2026-01-01T00:00:00+00:00")
        book = PaperBook("10000")
        context = AgentContext(book, replay_run_id="paper-run")
        orchestrator = AgentOrchestrator([MarketMirrorAgent(), PaperValueAgent({event.quote_key: forecast}, "50", "0.05")], context)
        orchestrator.on_market_event(event)
        self.assertEqual(len(book.tickets), 1)
        self.assertEqual(book.balance, Decimal("9950"))

    def test_evidence_rejects_future_result_fields(self):
        with self.assertRaisesRegex(ValueError, "future-result"):
            EvidenceItem("x", "2026-01-01T00:00:00+00:00", "test", "research", {"final_result": "a"})


if __name__ == "__main__":
    unittest.main()
