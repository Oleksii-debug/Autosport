import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from autosport.domain import MarketEvent
from autosport.ingestion_health import SourceHealthStore
from autosport.market_mirror import MarketMirror, MirrorSnapshot
from autosport.market_mirror_health import (
    HealthGatedMirrorDecisionIndex,
    ProviderDecisionEligibility,
)
from autosport.market_mirror_runtime import FocusedMirrorDependencyIndex


class ProviderHealthReplayBoundaryTests(unittest.TestCase):
    @staticmethod
    def _event() -> MarketEvent:
        return MarketEvent(
            event_id="event-1",
            market_id="winner",
            selection_id="selection-a",
            decimal_odds=Decimal("2.00"),
            observed_ts="2026-09-17T12:00:00+00:00",
            source_id="provider-a",
            sequence=1,
            status="open",
            source_ts="2026-09-17T12:00:00+00:00",
            ingest_ts="2026-09-17T12:00:01+00:00",
        )

    @staticmethod
    def _record_success(store: SourceHealthStore, *, now: str) -> None:
        store.record_success(
            "provider-a",
            now=now,
            received=1,
            accepted=1,
            rejected=0,
            cursor="cursor-1",
            latest_source_ts="2026-09-17T12:00:00+00:00",
            quality_flags=(),
        )

    @staticmethod
    def _gate(directory: str) -> tuple[HealthGatedMirrorDecisionIndex, SourceHealthStore]:
        mirror = MarketMirror()
        mirror.apply(ProviderHealthReplayBoundaryTests._event())
        dependencies = FocusedMirrorDependencyIndex(mirror)
        dependencies.register("decision", event_ids="event-1", market_ids="winner")
        store = SourceHealthStore(Path(directory) / "source_health.json")
        return (
            HealthGatedMirrorDecisionIndex(
                dependencies,
                store,
                max_health_age=timedelta(seconds=30),
            ),
            store,
        )

    def test_fresh_historical_boundary_never_exposes_future_health_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            gate, store = self._gate(directory)
            self._record_success(store, now="2026-09-17T12:00:03+00:00")
            store.record_failure(
                "provider-a",
                now="2026-09-17T12:00:08+00:00",
                error=ConnectionError("future relative to historical replay"),
            )
            as_of = datetime(2026, 9, 17, 12, 0, 5, tzinfo=timezone.utc)

            health = gate.provider_health("provider-a", as_of=as_of)
            self.assertEqual(health.eligibility, ProviderDecisionEligibility.ELIGIBLE)
            self.assertEqual(health.replay_boundary.transition_order, 1)
            self.assertEqual(
                health.replay_boundary.recorded_at,
                "2026-09-17T12:00:03+00:00",
            )

            view = gate.decision_view(
                "decision",
                as_of=as_of,
                max_age=timedelta(minutes=1),
            )
            self.assertIsInstance(view, MirrorSnapshot)
            self.assertEqual(len(view.events), 1)
            self.assertEqual(view.health_boundaries, (health.replay_boundary,))

            replay = gate.decision_view(
                "decision",
                as_of=as_of,
                max_age=timedelta(minutes=1),
                health_boundaries={"provider-a": health.replay_boundary},
            )
            self.assertEqual(replay.events, view.events)
            self.assertEqual(replay.health_boundaries, view.health_boundaries)

    def test_before_first_health_transition_binds_zero_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            gate, store = self._gate(directory)
            self._record_success(store, now="2026-09-17T12:00:05+00:00")

            health = gate.provider_health(
                "provider-a",
                as_of=datetime(2026, 9, 17, 12, 0, 4, tzinfo=timezone.utc),
            )

            self.assertEqual(health.eligibility, ProviderDecisionEligibility.UNKNOWN)
            self.assertEqual(health.replay_boundary.transition_order, 0)
            self.assertIsNone(health.replay_boundary.recorded_at)


if __name__ == "__main__":
    unittest.main()
