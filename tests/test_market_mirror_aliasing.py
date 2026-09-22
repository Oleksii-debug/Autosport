from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from autosport.domain import MarketEvent
from autosport.market_mirror import MarketMirror, MirrorUpdate


class MarketMirrorAliasingTests(unittest.TestCase):
    @staticmethod
    def event() -> MarketEvent:
        return MarketEvent(
            event_id="event-1",
            market_id="market-1",
            selection_id="selection-1",
            decimal_odds=Decimal("2.00"),
            observed_ts="2026-09-16T19:00:00+00:00",
            ingest_ts="2026-09-16T19:00:00+00:00",
            source_id="provider-a",
            sequence=1,
            metadata={"nested": {"state": "original"}},
        )

    @staticmethod
    def stored(mirror: MarketMirror) -> MarketEvent:
        event = mirror.get(
            "provider-a", "event-1", "market-1", "selection-1"
        )
        if event is None:
            raise AssertionError("expected mirrored event")
        return event

    def test_apply_owns_nested_metadata_snapshot(self) -> None:
        mirror = MarketMirror()
        event = self.event()

        self.assertEqual(mirror.apply(event).status, MirrorUpdate.APPLIED)
        event.metadata["nested"]["state"] = "caller-mutated"

        self.assertEqual(
            self.stored(mirror).metadata["nested"]["state"],
            "original",
        )

    def test_read_surfaces_do_not_expose_internal_metadata_aliases(self) -> None:
        mirror = MarketMirror()
        mirror.apply(self.event())

        from_get = self.stored(mirror)
        from_get.metadata["nested"]["state"] = "get-mutated"

        from_snapshot = mirror.snapshot()[0]
        from_snapshot.metadata["nested"]["state"] = "snapshot-mutated"

        from_active = mirror.active_snapshot(
            as_of=datetime(2026, 9, 16, 19, 0, tzinfo=timezone.utc),
            max_age=timedelta(minutes=1),
        )[0]
        from_active.metadata["nested"]["state"] = "active-mutated"

        self.assertEqual(
            self.stored(mirror).metadata["nested"]["state"],
            "original",
        )
        self.assertEqual(
            mirror.snapshot()[0].metadata["nested"]["state"],
            "original",
        )


if __name__ == "__main__":
    unittest.main()
