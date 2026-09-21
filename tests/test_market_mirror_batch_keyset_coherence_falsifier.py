from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

from autosport.domain import MarketEvent
from autosport.market_mirror import MarketMirror
from autosport.market_mirror_runtime import (
    BoundedMirrorInvalidationBuffer,
    FocusedMirrorDependencyIndex,
)


class BatchKeysetSnapshotCoherenceFalsifierTests(unittest.TestCase):
    @staticmethod
    def _event(
        *,
        selection: str,
        sequence: int = 1,
        odds: str = "2.00",
    ) -> MarketEvent:
        timestamp = "2026-09-21T14:45:00+00:00"
        return MarketEvent(
            event_id="event-1",
            market_id="market-1",
            selection_id=selection,
            decimal_odds=Decimal(odds),
            observed_ts=timestamp,
            source_id="provider-a",
            sequence=sequence,
            status="open",
            source_ts=timestamp,
            ingest_ts=timestamp,
        )

    def test_new_matching_key_cannot_be_omitted_from_reported_mirror_revision(self) -> None:
        mirror = MarketMirror()
        invalidations = BoundedMirrorInvalidationBuffer(mirror)
        invalidations.accept_persisted(self._event(selection="selection-a"))
        routed = invalidations.drain()

        dependencies = FocusedMirrorDependencyIndex(mirror)
        dependencies.register(
            "portfolio-input",
            source_ids="provider-a",
            event_ids="event-1",
            market_ids="market-1",
        )
        self.assertEqual(
            dependencies.affected_inputs(routed),
            ("portfolio-input",),
        )

        real_read = mirror.active_view_for_keys
        injected = False

        def add_matching_key_before_capture(keys, *, as_of, max_age):
            nonlocal injected
            if not injected:
                injected = True
                invalidations.accept_persisted(
                    self._event(
                        selection="selection-b",
                        odds="2.10",
                    )
                )
            return real_read(keys, as_of=as_of, max_age=max_age)

        as_of = datetime(2026, 9, 21, 14, 45, 10, tzinfo=timezone.utc)
        with patch.object(
            mirror,
            "active_view_for_keys",
            side_effect=add_matching_key_before_capture,
        ):
            captured = dependencies.incremental_decision_views(
                ("portfolio-input",),
                as_of=as_of,
                max_age=timedelta(minutes=1),
            )["portfolio-input"]

        canonical_same_revision = mirror.active_view(
            as_of=as_of,
            max_age=timedelta(minutes=1),
            source_ids="provider-a",
            event_ids="event-1",
            market_ids="market-1",
        )

        self.assertEqual(captured.revision, canonical_same_revision.revision)
        self.assertEqual(
            tuple((event.source_id, event.quote_key) for event in captured.events),
            tuple(
                (event.source_id, event.quote_key)
                for event in canonical_same_revision.events
            ),
            (
                "an incremental portfolio view must not claim a canonical mirror "
                "revision while omitting a newly matching quote already present "
                "in that same revision"
            ),
        )


if __name__ == "__main__":
    unittest.main()
