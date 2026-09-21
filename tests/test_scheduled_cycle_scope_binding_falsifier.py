from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from autosport.causal_collector import CollectorDeltaStore
from autosport.collector_service import CollectorServiceConfig
from autosport.scheduled_cycle_coverage import (
    freeze_acquisition_schedule,
    resolve_scheduled_cycle_coverage,
)


def _config() -> CollectorServiceConfig:
    return CollectorServiceConfig(
        max_items=10,
        poll_interval_seconds=10,
        retry_attempts=1,
        initial_backoff_seconds=1,
        max_backoff_seconds=1,
        jitter_fraction=0,
        max_store_bytes=1_000_000,
    )


def _freeze(
    store: CollectorDeltaStore,
    *,
    query_scope: dict[str, str],
):
    return freeze_acquisition_schedule(
        store,
        source_id="source-x",
        adapter_id="adapter-v1",
        sport="table_tennis",
        query_scope=query_scope,
        schedule_policy_version="cadence-v1",
        window_start="2026-01-01T00:00:10+00:00",
        window_end="2026-01-01T00:00:40+00:00",
        campaign_id="campaign-1",
        protocol_id="protocol-1",
        collector_config=_config(),
        _clock=lambda: "2026-01-01T00:00:00+00:00",
    )


class ScheduledCycleScopeBindingFalsifier(unittest.TestCase):
    def test_one_start_set_cannot_complete_two_distinct_query_scope_schedules(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = CollectorDeltaStore(Path(tmp) / "collector.db")
            league_a = _freeze(store, query_scope={"league": "league-a"})
            league_b = _freeze(store, query_scope={"league": "league-b"})

            self.assertNotEqual(league_a.schedule_id, league_b.schedule_id)
            self.assertNotEqual(
                league_a.commitment_sha256,
                league_b.commitment_sha256,
            )

            for attempted_at in (
                "2026-01-01T00:00:10+00:00",
                "2026-01-01T00:00:20+00:00",
                "2026-01-01T00:00:30+00:00",
            ):
                store._begin_collector_cycle(
                    source_id="source-x",
                    run_id="run-shared",
                    stream_epoch="epoch-shared",
                    attempted_at=attempted_at,
                )

            coverage_a = resolve_scheduled_cycle_coverage(
                store,
                schedule_id=league_a.schedule_id,
            )
            coverage_b = resolve_scheduled_cycle_coverage(
                store,
                schedule_id=league_b.schedule_id,
            )

            self.assertFalse(
                coverage_a.schedule_coverage_complete
                and coverage_b.schedule_coverage_complete,
                "the same unscoped START rows must not positively satisfy two "
                "different frozen query scopes",
            )


if __name__ == "__main__":
    unittest.main()
