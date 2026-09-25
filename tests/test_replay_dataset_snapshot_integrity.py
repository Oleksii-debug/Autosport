from __future__ import annotations

import unittest
from copy import deepcopy

from autosport.domain import MarketEvent
from autosport.replay import ReplayEngine


class ReplayDatasetSnapshotIntegrityTests(unittest.TestCase):
    @staticmethod
    def _event(
        event_id: str,
        *,
        sequence: int,
        observed_ts: str,
        ingest_ts: str,
        metadata: dict[str, object] | None = None,
    ) -> MarketEvent:
        return MarketEvent.from_dict(
            {
                "event_id": event_id,
                "market_id": "winner",
                "selection_id": "home",
                "decimal_odds": "2.0",
                "observed_ts": observed_ts,
                "ingest_ts": ingest_ts,
                "source_id": "source-a",
                "sequence": sequence,
                "metadata": metadata or {},
            }
        )

    def test_caller_metadata_mutation_after_init_cannot_change_strategy_visible_event(self) -> None:
        original_metadata = {
            "nested": {
                "provider_state": "original",
                "evidence": ["first"],
            }
        }
        event = self._event(
            "event-a",
            sequence=1,
            observed_ts="2026-09-21T10:00:00+00:00",
            ingest_ts="2026-09-21T10:00:01+00:00",
            metadata=original_metadata,
        )
        engine = ReplayEngine([event])
        frozen_hash = engine.dataset_hash

        event.metadata["nested"]["provider_state"] = "caller-mutated"
        event.metadata["nested"]["evidence"].append("late-mutation")
        event.metadata["caller_only"] = True

        seen: list[dict[str, object]] = []
        run = engine.run(
            lambda delivered: seen.append(deepcopy(delivered.metadata)),
            run_id="snapshot-metadata",
        )

        self.assertEqual(engine.dataset_hash, frozen_hash)
        self.assertEqual(run.dataset_hash, frozen_hash)
        self.assertEqual(seen, [original_metadata])

    def test_public_event_membership_is_not_mutable_after_hash_freeze(self) -> None:
        first = self._event(
            "event-a",
            sequence=1,
            observed_ts="2026-09-21T10:00:00+00:00",
            ingest_ts="2026-09-21T10:00:01+00:00",
        )
        injected = self._event(
            "event-b",
            sequence=2,
            observed_ts="2026-09-21T10:00:02+00:00",
            ingest_ts="2026-09-21T10:00:03+00:00",
        )
        engine = ReplayEngine([first])
        frozen_hash = engine.dataset_hash

        with self.assertRaises(AttributeError):
            engine.events.append(injected)

        seen: list[str] = []
        run = engine.run(
            lambda delivered: seen.append(delivered.event_id),
            run_id="immutable-event-membership",
        )
        self.assertEqual(seen, ["event-a"])
        self.assertEqual(run.event_count, 1)
        self.assertEqual(run.dataset_hash, frozen_hash)

    def test_callback_metadata_mutation_cannot_rewrite_engine_audit_snapshot(self) -> None:
        event = self._event(
            "event-a",
            sequence=1,
            observed_ts="2026-09-21T10:00:00+00:00",
            ingest_ts="2026-09-21T10:00:01+00:00",
            metadata={"nested": {"provider_state": "original"}},
        )
        engine = ReplayEngine([event])
        frozen_hash = engine.dataset_hash

        def mutate(delivered: MarketEvent) -> None:
            delivered.metadata["nested"]["provider_state"] = "strategy-mutated"
            delivered.metadata["strategy_only"] = True

        run = engine.run(mutate, run_id="callback-snapshot")

        self.assertEqual(run.dataset_hash, frozen_hash)
        self.assertEqual(
            engine.events[0].metadata,
            {"nested": {"provider_state": "original"}},
        )


if __name__ == "__main__":
    unittest.main()
