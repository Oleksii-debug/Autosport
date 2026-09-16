from decimal import Decimal
import tempfile
import unittest
from pathlib import Path

from autosport.domain import MarketEvent
from autosport.market_mirror import MarketMirror, MirrorUpdate
from autosport.storage import SQLiteMarketStore


class MarketMirrorTests(unittest.TestCase):
    @staticmethod
    def event(
        *,
        source: str = "provider-a",
        event: str = "event-1",
        market: str = "market-1",
        selection: str = "selection-1",
        sequence: int = 1,
        odds: str = "2.00",
        status: str = "open",
    ) -> MarketEvent:
        return MarketEvent(
            event_id=event,
            market_id=market,
            selection_id=selection,
            decimal_odds=Decimal(odds),
            observed_ts="2026-09-16T19:00:00+00:00",
            source_id=source,
            sequence=sequence,
            status=status,
        )

    def test_new_and_forward_updates_are_applied(self) -> None:
        mirror = MarketMirror()

        first = mirror.apply(self.event(sequence=1, odds="2.00"))
        second = mirror.apply(self.event(sequence=2, odds="2.10"))

        self.assertEqual(first.status, MirrorUpdate.APPLIED)
        self.assertEqual(second.status, MirrorUpdate.APPLIED)
        self.assertEqual(second.previous_sequence, 1)
        self.assertEqual(second.current_sequence, 2)
        self.assertEqual(mirror.get("provider-a", "event-1", "market-1", "selection-1").decimal_odds, Decimal("2.10"))

    def test_duplicate_sequence_is_idempotent(self) -> None:
        mirror = MarketMirror()
        event = self.event(sequence=7, odds="2.20")

        self.assertEqual(mirror.apply(event).status, MirrorUpdate.APPLIED)
        result = mirror.apply(event)

        self.assertEqual(result.status, MirrorUpdate.DUPLICATE)
        self.assertEqual(result.previous_sequence, 7)
        self.assertEqual(len(mirror), 1)

    def test_stale_sequence_is_ignored(self) -> None:
        mirror = MarketMirror()
        mirror.apply(self.event(sequence=9, odds="2.30"))

        result = mirror.apply(self.event(sequence=8, odds="2.00"))

        self.assertEqual(result.status, MirrorUpdate.STALE)
        self.assertEqual(result.current_sequence, 9)
        self.assertEqual(mirror.get("provider-a", "event-1", "market-1", "selection-1").decimal_odds, Decimal("2.30"))

    def test_same_sequence_with_different_payload_fails_closed(self) -> None:
        mirror = MarketMirror()
        mirror.apply(self.event(sequence=3, odds="2.00"))

        with self.assertRaises(ValueError):
            mirror.apply(self.event(sequence=3, odds="2.01"))

    def test_provider_identity_prevents_cross_provider_aliasing(self) -> None:
        mirror = MarketMirror()
        mirror.apply(self.event(source="provider-a", sequence=1, odds="2.00"))
        mirror.apply(self.event(source="provider-b", sequence=1, odds="1.90"))

        self.assertEqual(len(mirror), 2)
        self.assertEqual(
            mirror.get("provider-a", "event-1", "market-1", "selection-1").decimal_odds,
            Decimal("2.00"),
        )
        self.assertEqual(
            mirror.get("provider-b", "event-1", "market-1", "selection-1").decimal_odds,
            Decimal("1.90"),
        )

    def test_inactive_quotes_remain_auditable_but_are_excluded_from_active_snapshot(self) -> None:
        mirror = MarketMirror()
        mirror.apply(self.event(selection="open", sequence=1, status="open"))
        mirror.apply(self.event(selection="suspended", sequence=1, status="suspended"))
        mirror.apply(self.event(selection="closed", sequence=1, status="closed"))

        self.assertEqual(len(mirror.snapshot()), 3)
        self.assertEqual(tuple(e.selection_id for e in mirror.active_snapshot()), ("open",))

    def test_snapshot_order_is_deterministic(self) -> None:
        mirror = MarketMirror()
        mirror.apply(self.event(source="provider-b", selection="b", sequence=1))
        mirror.apply(self.event(source="provider-a", selection="z", sequence=1))
        mirror.apply(self.event(source="provider-a", selection="a", sequence=1))

        keys = tuple((e.source_id, e.quote_key) for e in mirror.snapshot())
        self.assertEqual(
            keys,
            (
                ("provider-a", "event-1|market-1|a"),
                ("provider-a", "event-1|market-1|z"),
                ("provider-b", "event-1|market-1|b"),
            ),
        )

    def test_persist_and_reopen_restores_latest_state_and_sequence_guard(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "market.db"
            store = SQLiteMarketStore(db_path)
            try:
                mirror = MarketMirror()
                mirror.apply(self.event(sequence=1, odds="2.00"))
                mirror.apply(self.event(sequence=2, odds="2.20"))

                self.assertEqual(mirror.persist(store), 1)
                self.assertEqual(mirror.persist(store), 0)
            finally:
                store.close()

            reopened_store = SQLiteMarketStore(db_path)
            try:
                restored = MarketMirror.from_store(reopened_store)
                restored_event = restored.get(
                    "provider-a", "event-1", "market-1", "selection-1"
                )
                self.assertIsNotNone(restored_event)
                self.assertEqual(restored_event.decimal_odds, Decimal("2.20"))
                self.assertEqual(restored_event.sequence, 2)

                stale = restored.apply(self.event(sequence=1, odds="1.50"))
                self.assertEqual(stale.status, MirrorUpdate.STALE)

                forward = restored.apply(self.event(sequence=3, odds="2.40"))
                self.assertEqual(forward.status, MirrorUpdate.APPLIED)
                self.assertEqual(
                    restored.get(
                        "provider-a", "event-1", "market-1", "selection-1"
                    ).decimal_odds,
                    Decimal("2.40"),
                )
            finally:
                reopened_store.close()

    def test_persist_reopen_preserves_multiple_provider_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                mirror = MarketMirror()
                mirror.apply(self.event(source="provider-a", sequence=4, odds="2.00"))
                mirror.apply(self.event(source="provider-b", sequence=4, odds="1.80"))
                self.assertEqual(mirror.persist(store), 2)
            finally:
                store.close()

            reopened_store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                restored = MarketMirror.from_store(reopened_store)
                self.assertEqual(len(restored), 2)
                self.assertEqual(
                    restored.get(
                        "provider-a", "event-1", "market-1", "selection-1"
                    ).decimal_odds,
                    Decimal("2.00"),
                )
                self.assertEqual(
                    restored.get(
                        "provider-b", "event-1", "market-1", "selection-1"
                    ).decimal_odds,
                    Decimal("1.80"),
                )
            finally:
                reopened_store.close()


if __name__ == "__main__":
    unittest.main()
