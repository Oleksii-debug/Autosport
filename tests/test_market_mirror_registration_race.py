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


class FocusedMirrorDependencyRegistrationRaceTests(unittest.TestCase):
    @staticmethod
    def event(
        *,
        source_id: str = "provider-a",
        selection_id: str,
        sequence: int = 1,
        odds: str = "2.00",
    ) -> MarketEvent:
        timestamp = f"2026-09-21T12:00:{sequence:02d}+00:00"
        return MarketEvent(
            event_id="event-1",
            market_id="winner",
            selection_id=selection_id,
            decimal_odds=Decimal(odds),
            observed_ts=timestamp,
            source_id=source_id,
            sequence=sequence,
            status="open",
            source_ts=timestamp,
            ingest_ts=timestamp,
        )

    def test_registration_recovers_update_drained_before_dependency_publication(self) -> None:
        mirror = MarketMirror()
        invalidations = BoundedMirrorInvalidationBuffer(mirror)
        dependencies = FocusedMirrorDependencyIndex(mirror)

        baseline = self.event(selection_id="selection-a")
        late_matching = self.event(selection_id="selection-b")
        late_unrelated = self.event(
            source_id="provider-b",
            selection_id="selection-c",
        )
        invalidations.accept_persisted(baseline)

        original_snapshot = mirror.snapshot
        snapshot_calls = 0

        def racing_snapshot():
            nonlocal snapshot_calls
            snapshot_calls += 1
            if snapshot_calls != 1:
                return original_snapshot()

            captured_before_registration = original_snapshot()
            invalidations.accept_persisted(late_matching)
            invalidations.accept_persisted(late_unrelated)

            # Model a consumer draining and routing the pending batch while register()
            # is between its seed snapshot and publishing the new dependency.
            missed_batch = invalidations.drain(max_items=10)
            self.assertEqual(dependencies.affected_inputs(missed_batch), ())
            self.assertEqual(invalidations.pending_count, 0)
            return captured_before_registration

        with patch.object(mirror, "snapshot", side_effect=racing_snapshot):
            dependencies.register(
                "decision-provider-a",
                source_ids="provider-a",
                event_ids="event-1",
                market_ids="winner",
            )

        self.assertGreaterEqual(snapshot_calls, 2)
        self.assertEqual(
            dependencies.matching_keys("decision-provider-a"),
            tuple(
                sorted(
                    (
                        ("provider-a", baseline.quote_key),
                        ("provider-a", late_matching.quote_key),
                    )
                )
            ),
        )

        view = dependencies.incremental_decision_view(
            "decision-provider-a",
            as_of=datetime(2026, 9, 21, 12, 0, 10, tzinfo=timezone.utc),
            max_age=timedelta(minutes=1),
        )
        self.assertEqual(
            tuple(event.selection_id for event in view.events),
            ("selection-a", "selection-b"),
        )
        self.assertEqual(invalidations.drain().changed_keys, ())


if __name__ == "__main__":
    unittest.main()
