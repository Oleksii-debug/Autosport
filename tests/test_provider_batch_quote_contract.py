from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from autosport.ingestion import IngestionEngine
from autosport.ingestion_health import SourceHealthStore
from autosport.market_bus import MarketEventBus
from autosport.providers import ProviderBatch, ProviderQuote
from autosport.storage import SQLiteMarketStore


class _MalformedQuotesProvider:
    source_id = "fixture"

    def __init__(self, quotes: object) -> None:
        self.quotes = quotes

    def read_batch(self, max_items: int = 1000) -> ProviderBatch:
        del max_items
        return ProviderBatch(self.source_id, self.quotes)  # type: ignore[arg-type]


class _ChangingQuoteTuple(tuple):
    def __new__(cls, quote: ProviderQuote) -> _ChangingQuoteTuple:
        return super().__new__(cls, (quote,))

    def __init__(self, quote: ProviderQuote) -> None:
        del quote
        self.iterations = 0

    def __iter__(self):
        self.iterations += 1
        if self.iterations == 1:
            return tuple.__iter__(self)
        return iter((object(),))


class _ChangingQualityFlagsTuple(tuple):
    def __new__(cls) -> _ChangingQualityFlagsTuple:
        return super().__new__(cls, ("PROVIDER_WARNING",))

    def __init__(self) -> None:
        self.iterations = 0

    def __iter__(self):
        self.iterations += 1
        if self.iterations == 1:
            return tuple.__iter__(self)
        return iter(("MUTATED_PROVIDER_WARNING",))


def _quote() -> ProviderQuote:
    return ProviderQuote(
        provider_event_id="event-1",
        provider_market_id="winner",
        provider_selection_id="player-a",
        decimal_odds=Decimal("2.0"),
        observed_ts="2026-09-14T09:10:00+00:00",
        sequence=1,
    )


class _MalformedQualityFlagsProvider:
    source_id = "fixture"

    def __init__(self, quality_flags: object) -> None:
        self.quality_flags = quality_flags

    def read_batch(self, max_items: int = 1000) -> ProviderBatch:
        del max_items
        return ProviderBatch(
            self.source_id,
            (_quote(),),
            quality_flags=self.quality_flags,  # type: ignore[arg-type]
        )


class ProviderBatchQuoteContractTests(unittest.TestCase):
    def test_batch_rejects_mutable_quote_container(self) -> None:
        with self.assertRaisesRegex(TypeError, "quotes must be a tuple"):
            ProviderBatch("fixture", [_quote()])  # type: ignore[arg-type]

    def test_batch_rejects_tuple_subclass_before_iteration(self) -> None:
        quotes = _ChangingQuoteTuple(_quote())

        with self.assertRaisesRegex(TypeError, "quotes must be a tuple"):
            ProviderBatch("fixture", quotes)

        self.assertEqual(quotes.iterations, 0)

    def test_batch_rejects_non_quote_tuple_member(self) -> None:
        with self.assertRaisesRegex(TypeError, "quote must be ProviderQuote"):
            ProviderBatch("fixture", (_quote(), object()))  # type: ignore[arg-type]

    def test_batch_rejects_quality_flags_tuple_subclass_before_iteration(self) -> None:
        quality_flags = _ChangingQualityFlagsTuple()

        with self.assertRaisesRegex(TypeError, "quality_flags must be a tuple"):
            ProviderBatch("fixture", (_quote(),), quality_flags=quality_flags)

        self.assertEqual(quality_flags.iterations, 0)

    def test_malformed_quote_container_fails_before_market_persistence(self) -> None:
        hostile_quotes = _ChangingQuoteTuple(_quote())
        cases = (
            ([_quote()], "quotes must be a tuple"),
            (hostile_quotes, "quotes must be a tuple"),
            ((_quote(), object()), "quote must be ProviderQuote"),
        )
        for quotes, message in cases:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                store = SQLiteMarketStore(root / "market.db")
                try:
                    health = SourceHealthStore(root / "source-health.json")
                    engine = IngestionEngine(
                        MarketEventBus(store),
                        health_store=health,
                        clock=lambda: "2026-09-14T09:10:01+00:00",
                    )

                    with self.assertRaisesRegex(TypeError, message):
                        engine.poll_once(_MalformedQuotesProvider(quotes), max_items=10)

                    self.assertEqual(store.events(), [])
                    state = health.get("fixture")
                    self.assertEqual(state.status, "failed")
                    self.assertEqual(state.poll_count, 1)
                    self.assertEqual(state.total_failures, 1)
                    self.assertEqual(state.total_received, 0)
                    self.assertEqual(state.total_accepted, 0)
                    self.assertEqual(state.total_rejected, 0)
                    self.assertIsNone(state.last_cursor)
                finally:
                    store.close()

        self.assertEqual(hostile_quotes.iterations, 0)

    def test_hostile_quality_flags_fail_before_market_or_success_progress(self) -> None:
        quality_flags = _ChangingQualityFlagsTuple()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = SQLiteMarketStore(root / "market.db")
            try:
                health = SourceHealthStore(root / "source-health.json")
                engine = IngestionEngine(
                    MarketEventBus(store),
                    health_store=health,
                    clock=lambda: "2026-09-14T09:10:01+00:00",
                )

                with self.assertRaisesRegex(TypeError, "quality_flags must be a tuple"):
                    engine.poll_once(
                        _MalformedQualityFlagsProvider(quality_flags),
                        max_items=10,
                    )

                self.assertEqual(quality_flags.iterations, 0)
                self.assertEqual(store.events(), [])
                state = health.get("fixture")
                self.assertEqual(state.status, "failed")
                self.assertEqual(state.poll_count, 1)
                self.assertEqual(state.total_failures, 1)
                self.assertEqual(state.total_received, 0)
                self.assertEqual(state.total_accepted, 0)
                self.assertEqual(state.total_rejected, 0)
                self.assertIsNone(state.last_success_at)
                self.assertIsNone(state.last_cursor)
                self.assertEqual(state.quality_flags, ())
            finally:
                store.close()

    def test_canonical_tuple_of_provider_quotes_remains_valid(self) -> None:
        quote = _quote()
        batch = ProviderBatch("fixture", (quote,), cursor="1")

        self.assertEqual(batch.quotes, (quote,))
        self.assertEqual(batch.cursor, "1")


if __name__ == "__main__":
    unittest.main()
