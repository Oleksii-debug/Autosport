from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from autosport.causal_collector import CollectorDeltaStore


GATE = "a" * 64
AUTH = "b" * 64
ARTIFACT = "c" * 64
ARTIFACT_KIND = "parlay-complete-game-board-v1"


class CollectorCycleObservationArtifactTests(unittest.TestCase):
    def _store(self, root: str) -> CollectorDeltaStore:
        return CollectorDeltaStore(Path(root) / "collector.db")

    def _scheduled_start(self, store: CollectorDeltaStore) -> tuple[int, dict[str, object]]:
        schedule = store._ensure_collector_schedule(
            source_id="parlayapi:table_tennis",
            run_id="run-1",
            stream_epoch="epoch-1",
            anchor_at="2100-01-01T00:00:00+00:00",
            interval_seconds=10,
            max_items=250,
            evaluation_start_slot_ordinal=0,
            evaluation_end_slot_ordinal=1,
            start_gate_binding_sha256=GATE,
        )
        store._authorize_collector_schedule_start_gate(
            source_id="parlayapi:table_tennis",
            run_id="run-1",
            schedule_id=schedule["schedule_id"],
            gate_binding_sha256=GATE,
            authorization_sha256=AUTH,
        )
        slot = store._next_collector_schedule_slot(
            source_id="parlayapi:table_tennis",
            run_id="run-1",
        )
        cycle_seq = store._begin_scheduled_collector_cycle(
            source_id="parlayapi:table_tennis",
            run_id="run-1",
            stream_epoch="epoch-1",
            max_items=250,
            slot_ordinal=slot["slot_ordinal"],
            due_at=slot["due_at"],
            attempted_at=slot["due_at"],
        )
        return cycle_seq, schedule

    def _finish(
        self,
        store: CollectorDeltaStore,
        cycle_seq: int,
        *,
        status: str = "SUCCESS",
    ) -> None:
        store._finish_collector_cycle(
            source_id="parlayapi:table_tennis",
            cycle_seq=cycle_seq,
            status=status,
            completed_at="2100-01-01T00:00:01+00:00",
            catalog_changes=(),
            observed_delta_ids=(),
            committed_delta_ids=(),
            duplicate_delta_ids=(),
            error_code=None if status == "SUCCESS" else "forced_failure",
        )

    def test_success_terminal_binds_exact_artifact_and_schedule_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            cycle_seq, schedule = self._scheduled_start(store)
            artifact = store._record_collector_cycle_observation_artifact(
                source_id="parlayapi:table_tennis",
                cycle_seq=cycle_seq,
                artifact_kind=ARTIFACT_KIND,
                artifact_sha256=ARTIFACT,
            )
            self._finish(store, cycle_seq)

            evidence = store.collector_cycle_observation_artifact_evidence(
                source_id="parlayapi:table_tennis",
                cycle_seq=cycle_seq,
                artifact_kind=ARTIFACT_KIND,
                artifact_sha256=ARTIFACT,
            )

            self.assertEqual(evidence["artifact_id"], artifact["artifact_id"])
            self.assertEqual(evidence["artifact_sha256"], ARTIFACT)
            self.assertEqual(evidence["artifact_kind"], ARTIFACT_KIND)
            self.assertEqual(evidence["schedule_id"], schedule["schedule_id"])
            self.assertEqual(evidence["authorization_sha256"], AUTH)
            self.assertEqual(evidence["slot_ordinal"], 0)
            self.assertEqual(
                evidence["attempted_at"],
                "2100-01-01T00:00:00+00:00",
            )
            self.assertEqual(
                evidence["completed_at"],
                "2100-01-01T00:00:01+00:00",
            )
            self.assertEqual(len(evidence["terminal_sha256"]), 64)
            self.assertEqual(len(evidence["evidence_sha256"]), 64)

            terminal = store.collector_cycle_evidence(
                source_id="parlayapi:table_tennis",
                start_cycle_seq=cycle_seq,
                end_cycle_seq=cycle_seq,
            )[0]["terminal"]
            self.assertEqual(
                terminal["observed_artifacts"],
                [
                    {
                        "artifact_id": artifact["artifact_id"],
                        "artifact_kind": ARTIFACT_KIND,
                        "artifact_sha256": ARTIFACT,
                    }
                ],
            )

    def test_artifact_cannot_be_added_before_start_or_after_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            with self.assertRaisesRegex(ValueError, "START evidence is missing"):
                store._record_collector_cycle_observation_artifact(
                    source_id="parlayapi:table_tennis",
                    cycle_seq=1,
                    artifact_kind=ARTIFACT_KIND,
                    artifact_sha256=ARTIFACT,
                )

            cycle_seq, _schedule = self._scheduled_start(store)
            self._finish(store, cycle_seq)
            with self.assertRaisesRegex(ValueError, "cannot follow terminal"):
                store._record_collector_cycle_observation_artifact(
                    source_id="parlayapi:table_tennis",
                    cycle_seq=cycle_seq,
                    artifact_kind=ARTIFACT_KIND,
                    artifact_sha256=ARTIFACT,
                )

    def test_pending_or_failed_cycle_cannot_resolve_positive_artifact_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            cycle_seq, _schedule = self._scheduled_start(store)
            store._record_collector_cycle_observation_artifact(
                source_id="parlayapi:table_tennis",
                cycle_seq=cycle_seq,
                artifact_kind=ARTIFACT_KIND,
                artifact_sha256=ARTIFACT,
            )
            with self.assertRaisesRegex(ValueError, "evidence is unavailable"):
                store.collector_cycle_observation_artifact_evidence(
                    source_id="parlayapi:table_tennis",
                    cycle_seq=cycle_seq,
                    artifact_kind=ARTIFACT_KIND,
                    artifact_sha256=ARTIFACT,
                )

            self._finish(store, cycle_seq, status="LOCAL_FAILURE")
            with self.assertRaisesRegex(ValueError, "not SUCCESS-terminal"):
                store.collector_cycle_observation_artifact_evidence(
                    source_id="parlayapi:table_tennis",
                    cycle_seq=cycle_seq,
                    artifact_kind=ARTIFACT_KIND,
                    artifact_sha256=ARTIFACT,
                )

    def test_unscheduled_cycle_artifact_is_not_campaign_authority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            cycle_seq = store._begin_collector_cycle(
                source_id="ungated-source",
                run_id="run-1",
                stream_epoch="epoch-1",
                attempted_at="2100-01-01T00:00:00+00:00",
            )
            store._record_collector_cycle_observation_artifact(
                source_id="ungated-source",
                cycle_seq=cycle_seq,
                artifact_kind=ARTIFACT_KIND,
                artifact_sha256=ARTIFACT,
            )
            store._finish_collector_cycle(
                source_id="ungated-source",
                cycle_seq=cycle_seq,
                status="SUCCESS",
                completed_at="2100-01-01T00:00:01+00:00",
                catalog_changes=(),
                observed_delta_ids=(),
                committed_delta_ids=(),
                duplicate_delta_ids=(),
            )
            with self.assertRaisesRegex(ValueError, "not bound to authorized schedule"):
                store.collector_cycle_observation_artifact_evidence(
                    source_id="ungated-source",
                    cycle_seq=cycle_seq,
                    artifact_kind=ARTIFACT_KIND,
                    artifact_sha256=ARTIFACT,
                )

    def test_artifact_rows_are_sql_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            cycle_seq, _schedule = self._scheduled_start(store)
            artifact = store._record_collector_cycle_observation_artifact(
                source_id="parlayapi:table_tennis",
                cycle_seq=cycle_seq,
                artifact_kind=ARTIFACT_KIND,
                artifact_sha256=ARTIFACT,
            )
            connection = sqlite3.connect(store.path)
            try:
                with self.assertRaises(sqlite3.DatabaseError):
                    connection.execute(
                        "UPDATE collector_cycle_artifacts_v1 "
                        "SET artifact_sha256=? WHERE artifact_id=?",
                        ("d" * 64, artifact["artifact_id"]),
                    )
                connection.rollback()
                with self.assertRaises(sqlite3.DatabaseError):
                    connection.execute(
                        "DELETE FROM collector_cycle_artifacts_v1 WHERE artifact_id=?",
                        (artifact["artifact_id"],),
                    )
            finally:
                connection.rollback()
                connection.close()


if __name__ == "__main__":
    unittest.main()
