from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from autosport.domain import MarketType
from autosport.ingestion import IngestionEngine
from autosport.ingestion_health import IngestionPolicy, SourceHealthStore
from autosport.market_bus import MarketEventBus
from autosport.providers import CanonicalNormalizer, InMemoryProvider, ProviderQuote
from autosport.storage import SQLiteMarketStore


class ProviderPayloadIsolationTests(unittest.TestCase):
    @staticmethod
    def _quote(**overrides: object) -> ProviderQuote:
        values: dict[str, object] = {
            "provider_event_id": "event-1",
            "provider_market_id": "winner",
            "provider_selection_id": "player-a",
            "decimal_odds": Decimal("2.0"),
            "observed_ts": "2026-09-14T03:00:00+00:00",
            "sequence": 1,
            "market_type": MarketType.WINNER,
            "status": "open",
            "source_ts": None,
            "score_state": None,
            "metadata": {"provider": "fixture", "nested": [1, {"ok": True}]},
        }
        values.update(overrides)
        return ProviderQuote(**values)  # type: ignore[arg-type]

    @staticmethod
    def _deep_metadata(depth: int = 80) -> dict[str, object]:
        root: dict[str, object] = {}
        current = root
        for _ in range(depth):
            child: dict[str, object] = {}
            current["next"] = child
            current = child
        current["leaf"] = "ok"
        return root

    def test_normalizer_rejects_noncanonical_nonidentity_payload_fields(self) -> None:
        normalizer = CanonicalNormalizer()
        cases: tuple[tuple[str, object, type[BaseException], str], ...] = (
            ("observed_ts", 7, TypeError, "observed_ts"),
            ("observed_ts", "2026-09-14T03:00:00", ValueError, "timezone-aware"),
            ("market_type", "winner", TypeError, "market_type"),
            ("status", 7, TypeError, "status"),
            ("status", " open", ValueError, "status"),
            ("source_ts", 7, TypeError, "source_ts"),
            ("source_ts", "not-a-timestamp", ValueError, "source_ts"),
            ("score_state", 7, TypeError, "score_state"),
            ("metadata", [("key", "value")], TypeError, "metadata"),
            ("metadata", {"nested": ("tuple",)}, TypeError, "non-canonical JSON value type tuple"),
            ("metadata", {7: "value"}, TypeError, "non-string JSON object key"),
            ("metadata", {"bad": float("nan")}, ValueError, "non-finite JSON number"),
        )
        for field_name, value, error_type, message in cases:
            with self.subTest(field_name=field_name, value=value):
                quote = self._quote(**{field_name: value})
                with self.assertRaisesRegex(error_type, message):
                    normalizer.normalize("fixture", quote)

    def test_normalizer_rejects_cyclic_and_excessively_deep_metadata_without_recursion_error(self) -> None:
        cyclic: dict[str, object] = {}
        cyclic["self"] = cyclic

        normalizer = CanonicalNormalizer()
        with self.assertRaisesRegex(ValueError, "cyclic JSON container"):
            normalizer.normalize("fixture", self._quote(metadata=cyclic))
        with self.assertRaisesRegex(ValueError, "maximum JSON nesting depth"):
            normalizer.normalize("fixture", self._quote(metadata=self._deep_metadata()))

    def test_valid_nonidentity_payload_is_preserved_exactly(self) -> None:
        shared = {"ok": True}
        metadata = {
            "provider": "fixture",
            "nested": [1, shared],
            "same-again": shared,
            "ratio": 1.25,
        }
        quote = self._quote(
            status="suspended",
            source_ts="2026-09-14T02:59:59Z",
            score_state="1-0",
            metadata=metadata,
        )

        event = CanonicalNormalizer().normalize("fixture", quote)

        self.assertEqual(event.status, "suspended")
        self.assertEqual(event.source_ts, "2026-09-14T02:59:59Z")
        self.assertEqual(event.score_state, "1-0")
        self.assertEqual(event.metadata, quote.metadata)
        self.assertIsNot(event.metadata, quote.metadata)

    def test_normalizer_detaches_nested_provider_metadata_in_both_directions(self) -> None:
        shared = {"ok": True}
        nested: list[object] = [1, shared]
        metadata: dict[str, object] = {
            "nested": nested,
            "same-again": shared,
        }
        quote = self._quote(metadata=metadata)

        event = CanonicalNormalizer().normalize("fixture", quote)

        self.assertEqual(event.metadata, metadata)
        self.assertIsNot(event.metadata, metadata)
        self.assertIsNot(event.metadata["nested"], nested)
        self.assertIsNot(event.metadata["same-again"], shared)

        shared["ok"] = False
        nested.append("provider-only")
        self.assertEqual(event.metadata["nested"], [1, {"ok": True}])
        self.assertEqual(event.metadata["same-again"], {"ok": True})

        event_nested = event.metadata["nested"]
        self.assertIsInstance(event_nested, list)
        event_nested.append("event-only")
        event_same_again = event.metadata["same-again"]
        self.assertIsInstance(event_same_again, dict)
        event_same_again["event"] = True

        self.assertEqual(nested, [1, {"ok": False}, "provider-only"])
        self.assertEqual(shared, {"ok": False})

    def test_malformed_quote_is_rejected_without_rolling_back_valid_siblings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteMarketStore(Path(tmp) / "market.db")
            try:
                bus = MarketEventBus(store)
                engine = IngestionEngine(
                    bus,
                    policy=IngestionPolicy(max_batch_size=10),
                    clock=lambda: "2026-09-14T03:00:05+00:00",
                )
                quotes = [
                    self._quote(provider_selection_id="valid-a", sequence=1),
                    self._quote(provider_selection_id="bad-status", sequence=2, status=7),
                    self._quote(
                        provider_selection_id="bad-metadata",
                        sequence=3,
                        metadata={"nested": ("tuple",)},
                    ),
                    self._quote(
                        provider_selection_id="bad-observed-time",
                        sequence=4,
                        observed_ts="not-a-timestamp",
                    ),
                    self._quote(provider_selection_id="valid-b", sequence=5),
                ]

                stats = engine.poll_once(InMemoryProvider("fixture", quotes), max_items=10)

                self.assertEqual(stats.received, 5)
                self.assertEqual(stats.accepted, 2)
                self.assertEqual(stats.rejected, 3)
                self.assertEqual(
                    [event.selection_id for event in store.events()],
                    ["fixture:valid-a", "fixture:valid-b"],
                )
            finally:
                store.close()

    def test_cyclic_and_deep_metadata_are_per_quote_rejections_not_provider_failures(self) -> None:
        cyclic: dict[str, object] = {}
        cyclic["self"] = cyclic
        deep = self._deep_metadata()

        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteMarketStore(Path(tmp) / "market.db")
            try:
                health = SourceHealthStore(Path(tmp) / "source-health.json")
                engine = IngestionEngine(
                    MarketEventBus(store),
                    policy=IngestionPolicy(max_batch_size=10),
                    health_store=health,
                    clock=lambda: "2026-09-14T03:00:05+00:00",
                )
                quotes = [
                    self._quote(provider_selection_id="valid-a", sequence=1),
                    self._quote(
                        provider_selection_id="cyclic-metadata",
                        sequence=2,
                        metadata=cyclic,
                    ),
                    self._quote(
                        provider_selection_id="deep-metadata",
                        sequence=3,
                        metadata=deep,
                    ),
                    self._quote(provider_selection_id="valid-b", sequence=4),
                ]

                stats = engine.poll_once(InMemoryProvider("fixture", quotes), max_items=10)

                self.assertEqual(stats.received, 4)
                self.assertEqual(stats.accepted, 2)
                self.assertEqual(stats.rejected, 2)
                state = health.get("fixture")
                self.assertEqual(state.total_failures, 0)
                self.assertEqual(state.total_received, 4)
                self.assertEqual(state.total_accepted, 2)
                self.assertEqual(state.total_rejected, 2)
                self.assertEqual(
                    [event.selection_id for event in store.events()],
                    ["fixture:valid-a", "fixture:valid-b"],
                )
            finally:
                store.close()

    def test_wrong_typed_source_timestamp_is_quality_rejection_not_poll_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteMarketStore(Path(tmp) / "market.db")
            try:
                health = SourceHealthStore(Path(tmp) / "source-health.json")
                bus = MarketEventBus(store)
                engine = IngestionEngine(
                    bus,
                    policy=IngestionPolicy(max_batch_size=10),
                    health_store=health,
                    clock=lambda: "2026-09-14T03:00:05+00:00",
                )
                quotes = [
                    self._quote(provider_selection_id="bad-source-time", sequence=1, source_ts=7),
                    self._quote(
                        provider_selection_id="valid",
                        sequence=2,
                        source_ts="2026-09-14T03:00:00+00:00",
                    ),
                ]

                stats = engine.poll_once(InMemoryProvider("fixture", quotes), max_items=10)

                self.assertEqual(stats.accepted, 1)
                self.assertEqual(stats.rejected, 1)
                self.assertIn("INVALID_SOURCE_TIMESTAMP", stats.quality_flags)
                state = health.get("fixture")
                self.assertEqual(state.status, "degraded")
                self.assertEqual(state.total_failures, 0)
                self.assertEqual(state.total_received, 2)
                self.assertEqual(state.total_accepted, 1)
                self.assertEqual(state.total_rejected, 1)
                self.assertEqual(len(store.events()), 1)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
