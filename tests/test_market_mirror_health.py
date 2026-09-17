import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from autosport.domain import MarketEvent
from autosport.ingestion_health import SourceHealthStore
from autosport.market_mirror import MarketMirror
from autosport.market_mirror_health import (
    HealthGatedMirrorDecisionIndex,
    ProviderDecisionEligibility,
)
from autosport.market_mirror_runtime import FocusedMirrorDependencyIndex


class HealthGatedMirrorDecisionIndexTests(unittest.TestCase):
    @staticmethod
    def event(source_id: str, *, sequence: int = 1, odds: str = "2.00") -> MarketEvent:
        return MarketEvent(
            event_id="event-1",
            market_id="winner",
            selection_id="selection-a",
            decimal_odds=Decimal(odds),
            observed_ts="2026-09-17T12:00:00+00:00",
            source_id=source_id,
            sequence=sequence,
            status="open",
            source_ts="2026-09-17T12:00:00+00:00",
            ingest_ts="2026-09-17T12:00:01+00:00",
        )

    @staticmethod
    def record_healthy(store: SourceHealthStore, source_id: str, *, now: str) -> None:
        store.record_success(
            source_id,
            now=now,
            received=1,
            accepted=1,
            rejected=0,
            cursor="cursor-1",
            latest_source_ts="2026-09-17T12:00:00+00:00",
            quality_flags=(),
        )

    def build_gate(self, directory: str) -> tuple[
        MarketMirror,
        FocusedMirrorDependencyIndex,
        SourceHealthStore,
        HealthGatedMirrorDecisionIndex,
    ]:
        mirror = MarketMirror()
        dependencies = FocusedMirrorDependencyIndex(mirror)
        dependencies.register("decision", event_ids="event-1", market_ids="winner")
        health_store = SourceHealthStore(Path(directory) / "source_health.json")
        gate = HealthGatedMirrorDecisionIndex(
            dependencies,
            health_store,
            max_health_age=timedelta(seconds=30),
        )
        return mirror, dependencies, health_store, gate

    def test_healthy_recent_provider_remains_decision_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            mirror, _, health_store, gate = self.build_gate(directory)
            mirror.apply(self.event("provider-a"))
            self.record_healthy(
                health_store,
                "provider-a",
                now="2026-09-17T12:00:05+00:00",
            )

            health = gate.provider_health(
                "provider-a",
                as_of=datetime(2026, 9, 17, 12, 0, 10, tzinfo=timezone.utc),
            )
            view = gate.decision_view(
                "decision",
                as_of=datetime(2026, 9, 17, 12, 0, 10, tzinfo=timezone.utc),
                max_age=timedelta(minutes=1),
            )

            self.assertEqual(health.eligibility, ProviderDecisionEligibility.ELIGIBLE)
            self.assertTrue(health.eligible)
            self.assertEqual(health.replay_boundary.transition_order, 1)
            self.assertEqual(len(view.events), 1)
            self.assertEqual(view.events[0].source_id, "provider-a")
            self.assertEqual(view.health_boundaries, (health.replay_boundary,))

    def test_failed_provider_is_removed_even_while_quote_is_still_fresh(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            mirror, _, health_store, gate = self.build_gate(directory)
            mirror.apply(self.event("provider-a"))
            self.record_healthy(
                health_store,
                "provider-a",
                now="2026-09-17T12:00:03+00:00",
            )
            health_store.record_failure(
                "provider-a",
                now="2026-09-17T12:00:08+00:00",
                error=ConnectionError("provider disconnected"),
            )

            as_of = datetime(2026, 9, 17, 12, 0, 10, tzinfo=timezone.utc)
            health = gate.provider_health("provider-a", as_of=as_of)
            view = gate.decision_view(
                "decision",
                as_of=as_of,
                max_age=timedelta(minutes=1),
            )

            self.assertEqual(health.eligibility, ProviderDecisionEligibility.FAILED)
            self.assertFalse(health.eligible)
            self.assertEqual(view.events, ())
            self.assertEqual(
                gate.affected_inputs_for_source(
                    "provider-a",
                    as_of=as_of,
                    max_age=timedelta(minutes=1),
                ),
                ("decision",),
            )

    def test_later_failure_does_not_rewrite_historical_health_or_decision_view(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            mirror, _, health_store, gate = self.build_gate(directory)
            mirror.apply(self.event("provider-a"))
            self.record_healthy(
                health_store,
                "provider-a",
                now="2026-09-17T12:00:03+00:00",
            )
            health_store.record_failure(
                "provider-a",
                now="2026-09-17T12:00:08+00:00",
                error=ConnectionError("future relative to replay"),
            )

            historical = datetime(2026, 9, 17, 12, 0, 5, tzinfo=timezone.utc)
            health = gate.provider_health("provider-a", as_of=historical)
            view = gate.decision_view(
                "decision",
                as_of=historical,
                max_age=timedelta(minutes=1),
            )

            self.assertEqual(health.eligibility, ProviderDecisionEligibility.ELIGIBLE)
            self.assertEqual(health.source_status, "healthy")
            self.assertEqual(len(view.events), 1)
            self.assertEqual(view.events[0].source_id, "provider-a")

    def test_equal_time_transition_cannot_rewrite_bound_health_decision_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            mirror, dependencies, health_store, gate = self.build_gate(directory)
            mirror.apply(self.event("provider-a"))
            same_time = "2026-09-17T12:00:08+00:00"
            as_of = datetime(2026, 9, 17, 12, 0, 8, tzinfo=timezone.utc)
            self.record_healthy(health_store, "provider-a", now=same_time)

            original = gate.decision_view(
                "decision",
                as_of=as_of,
                max_age=timedelta(minutes=1),
            )
            self.assertEqual(len(original.events), 1)
            self.assertEqual(len(original.health_boundaries), 1)
            original_boundary = original.health_boundaries[0]
            self.assertEqual(original_boundary.source_id, "provider-a")
            self.assertEqual(original_boundary.recorded_at, same_time)
            self.assertEqual(original_boundary.transition_order, 1)

            health_store.record_failure(
                "provider-a",
                now=same_time,
                error=ConnectionError("same evidence time, later durable transition"),
            )

            fresh = gate.decision_view(
                "decision",
                as_of=as_of,
                max_age=timedelta(minutes=1),
            )
            self.assertEqual(fresh.events, ())
            self.assertEqual(fresh.health_boundaries[0].transition_order, 2)

            reopened_store = SourceHealthStore(Path(directory) / "source_health.json")
            reopened_gate = HealthGatedMirrorDecisionIndex(
                dependencies,
                reopened_store,
                max_health_age=timedelta(seconds=30),
            )
            replay = reopened_gate.decision_view(
                "decision",
                as_of=as_of,
                max_age=timedelta(minutes=1),
                health_boundaries={
                    boundary.source_id: boundary for boundary in original.health_boundaries
                },
            )

            self.assertEqual(replay.events, original.events)
            self.assertEqual(replay.health_boundaries, original.health_boundaries)
            replay_health = reopened_gate.provider_health(
                "provider-a",
                as_of=as_of,
                replay_boundary=original_boundary,
            )
            self.assertEqual(replay_health.eligibility, ProviderDecisionEligibility.ELIGIBLE)
            self.assertEqual(replay_health.source_status, "healthy")

    def test_degraded_provider_quality_is_quarantined_from_decision_view(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            mirror, _, health_store, gate = self.build_gate(directory)
            mirror.apply(self.event("provider-a"))
            health_store.record_success(
                "provider-a",
                now="2026-09-17T12:00:05+00:00",
                received=1,
                accepted=1,
                rejected=0,
                cursor="cursor-1",
                latest_source_ts="2026-09-17T12:00:00+00:00",
                quality_flags=("PARTIAL_SNAPSHOT",),
            )

            as_of = datetime(2026, 9, 17, 12, 0, 10, tzinfo=timezone.utc)
            health = gate.provider_health("provider-a", as_of=as_of)
            view = gate.decision_view(
                "decision",
                as_of=as_of,
                max_age=timedelta(minutes=1),
            )

            self.assertEqual(health.eligibility, ProviderDecisionEligibility.DEGRADED)
            self.assertEqual(view.events, ())

    def test_stale_or_pre_first_transition_provider_health_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            mirror, _, health_store, gate = self.build_gate(directory)
            mirror.apply(self.event("provider-a"))
            self.record_healthy(
                health_store,
                "provider-a",
                now="2026-09-17T12:00:05+00:00",
            )

            stale = gate.provider_health(
                "provider-a",
                as_of=datetime(2026, 9, 17, 12, 1, 0, tzinfo=timezone.utc),
            )
            before_first = gate.provider_health(
                "provider-a",
                as_of=datetime(2026, 9, 17, 12, 0, 4, tzinfo=timezone.utc),
            )

            self.assertEqual(stale.eligibility, ProviderDecisionEligibility.STALE_HEALTH)
            self.assertEqual(before_first.eligibility, ProviderDecisionEligibility.UNKNOWN)

    def test_one_failed_provider_does_not_remove_other_healthy_provider(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            mirror, _, health_store, gate = self.build_gate(directory)
            mirror.apply(self.event("provider-a", odds="2.00"))
            mirror.apply(self.event("provider-b", odds="2.20"))
            self.record_healthy(
                health_store,
                "provider-a",
                now="2026-09-17T12:00:04+00:00",
            )
            self.record_healthy(
                health_store,
                "provider-b",
                now="2026-09-17T12:00:04+00:00",
            )
            health_store.record_failure(
                "provider-a",
                now="2026-09-17T12:00:07+00:00",
                error=ConnectionError("provider-a disconnected"),
            )

            view = gate.decision_view(
                "decision",
                as_of=datetime(2026, 9, 17, 12, 0, 10, tzinfo=timezone.utc),
                max_age=timedelta(minutes=1),
            )

            self.assertEqual(view.revision, 2)
            self.assertEqual(len(view.events), 1)
            self.assertEqual(view.events[0].source_id, "provider-b")

    def test_recovery_makes_still_fresh_quote_eligible_again(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            mirror, _, health_store, gate = self.build_gate(directory)
            mirror.apply(self.event("provider-a"))
            self.record_healthy(
                health_store,
                "provider-a",
                now="2026-09-17T12:00:03+00:00",
            )
            health_store.record_failure(
                "provider-a",
                now="2026-09-17T12:00:06+00:00",
                error=TimeoutError("timeout"),
            )
            self.record_healthy(
                health_store,
                "provider-a",
                now="2026-09-17T12:00:09+00:00",
            )

            view = gate.decision_view(
                "decision",
                as_of=datetime(2026, 9, 17, 12, 0, 10, tzinfo=timezone.utc),
                max_age=timedelta(minutes=1),
            )

            self.assertEqual(len(view.events), 1)
            self.assertEqual(view.events[0].source_id, "provider-a")

    def test_unknown_provider_health_is_not_silently_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            mirror, _, _, gate = self.build_gate(directory)
            mirror.apply(self.event("provider-a"))

            as_of = datetime(2026, 9, 17, 12, 0, 10, tzinfo=timezone.utc)
            self.assertEqual(
                gate.provider_health("provider-a", as_of=as_of).eligibility,
                ProviderDecisionEligibility.UNKNOWN,
            )
            self.assertEqual(
                gate.decision_view(
                    "decision",
                    as_of=as_of,
                    max_age=timedelta(minutes=1),
                ).events,
                (),
            )


if __name__ == "__main__":
    unittest.main()
