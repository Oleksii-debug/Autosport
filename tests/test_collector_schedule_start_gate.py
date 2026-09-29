from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from autosport.causal_collector import CollectorDeltaStore


GATE_A = "a" * 64
GATE_B = "b" * 64
AUTH_A = "c" * 64
AUTH_B = "d" * 64


class CollectorScheduleStartGateTests(unittest.TestCase):
    def _store(self, root: str) -> CollectorDeltaStore:
        return CollectorDeltaStore(Path(root) / "collector.db")

    def _ensure(
        self,
        store: CollectorDeltaStore,
        *,
        gate: str | None,
    ) -> dict[str, object]:
        return store._ensure_collector_schedule(
            source_id="source-x",
            run_id="run-1",
            stream_epoch="epoch-1",
            anchor_at="2026-01-01T00:00:00+00:00",
            interval_seconds=10,
            max_items=250,
            evaluation_start_slot_ordinal=0,
            evaluation_end_slot_ordinal=1,
            start_gate_binding_sha256=gate,
        )

    def _begin_slot_zero(self, store: CollectorDeltaStore) -> int:
        slot = store._next_collector_schedule_slot(
            source_id="source-x",
            run_id="run-1",
        )
        self.assertEqual(slot["slot_ordinal"], 0)
        return store._begin_scheduled_collector_cycle(
            source_id="source-x",
            run_id="run-1",
            stream_epoch="epoch-1",
            max_items=250,
            slot_ordinal=slot["slot_ordinal"],
            due_at=slot["due_at"],
            attempted_at=slot["due_at"],
        )

    def test_gate_blocks_scheduled_start_until_exact_durable_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            schedule = self._ensure(store, gate=GATE_A)

            status = store._collector_schedule_start_gate_status(
                source_id="source-x",
                run_id="run-1",
            )
            self.assertEqual(
                status,
                {
                    "schedule_id": schedule["schedule_id"],
                    "gate_binding_sha256": GATE_A,
                    "authorization_sha256": None,
                },
            )

            with self.assertRaisesRegex(ValueError, "not durably authorized"):
                self._begin_slot_zero(store)

            connection = sqlite3.connect(store.path)
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM collector_cycle_starts_v1"
                    ).fetchone()[0],
                    0,
                )
            finally:
                connection.close()

            authorized = store._authorize_collector_schedule_start_gate(
                source_id="source-x",
                run_id="run-1",
                schedule_id=schedule["schedule_id"],
                gate_binding_sha256=GATE_A,
                authorization_sha256=AUTH_A,
            )
            self.assertEqual(authorized["authorization_sha256"], AUTH_A)
            self.assertEqual(self._begin_slot_zero(store), 1)

    def test_gate_install_is_rejected_after_any_collector_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._ensure(store, gate=None)
            self.assertEqual(self._begin_slot_zero(store), 1)

            with self.assertRaisesRegex(ValueError, "cannot be installed after"):
                self._ensure(store, gate=GATE_A)

            self.assertIsNone(
                store._collector_schedule_start_gate_status(
                    source_id="source-x",
                    run_id="run-1",
                )
            )

    def test_ordinary_reensure_cannot_remove_an_existing_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            schedule = self._ensure(store, gate=GATE_A)
            repeated = self._ensure(store, gate=None)
            self.assertEqual(repeated["schedule_id"], schedule["schedule_id"])
            self.assertEqual(
                store._collector_schedule_start_gate_status(
                    source_id="source-x",
                    run_id="run-1",
                )["gate_binding_sha256"],
                GATE_A,
            )
            with self.assertRaisesRegex(ValueError, "not durably authorized"):
                self._begin_slot_zero(store)

    def test_gate_binding_cannot_be_retargeted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            self._ensure(store, gate=GATE_A)
            with self.assertRaisesRegex(ValueError, "binding cannot change"):
                self._ensure(store, gate=GATE_B)

    def test_authorization_is_idempotent_only_for_exact_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            schedule = self._ensure(store, gate=GATE_A)
            first = store._authorize_collector_schedule_start_gate(
                source_id="source-x",
                run_id="run-1",
                schedule_id=schedule["schedule_id"],
                gate_binding_sha256=GATE_A,
                authorization_sha256=AUTH_A,
            )
            second = store._authorize_collector_schedule_start_gate(
                source_id="source-x",
                run_id="run-1",
                schedule_id=schedule["schedule_id"],
                gate_binding_sha256=GATE_A,
                authorization_sha256=AUTH_A,
            )
            self.assertEqual(first, second)

            with self.assertRaisesRegex(ValueError, "different authority"):
                store._authorize_collector_schedule_start_gate(
                    source_id="source-x",
                    run_id="run-1",
                    schedule_id=schedule["schedule_id"],
                    gate_binding_sha256=GATE_A,
                    authorization_sha256=AUTH_B,
                )

    def test_gate_and_authorization_rows_are_sql_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            schedule = self._ensure(store, gate=GATE_A)
            store._authorize_collector_schedule_start_gate(
                source_id="source-x",
                run_id="run-1",
                schedule_id=schedule["schedule_id"],
                gate_binding_sha256=GATE_A,
                authorization_sha256=AUTH_A,
            )

            connection = sqlite3.connect(store.path)
            try:
                with self.assertRaises(sqlite3.DatabaseError):
                    connection.execute(
                        "UPDATE collector_schedule_start_gates_v1 "
                        "SET gate_binding_sha256=? WHERE source_id=? AND run_id=?",
                        (GATE_B, "source-x", "run-1"),
                    )
                connection.rollback()
                with self.assertRaises(sqlite3.DatabaseError):
                    connection.execute(
                        "DELETE FROM collector_schedule_start_authorizations_v1 "
                        "WHERE source_id=? AND run_id=?",
                        ("source-x", "run-1"),
                    )
            finally:
                connection.rollback()
                connection.close()

    def test_malformed_gate_and_authorization_digests_fail_before_state_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                self._ensure(store, gate="not-a-digest")

            schedule = self._ensure(store, gate=GATE_A)
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                store._authorize_collector_schedule_start_gate(
                    source_id="source-x",
                    run_id="run-1",
                    schedule_id=schedule["schedule_id"],
                    gate_binding_sha256=GATE_A,
                    authorization_sha256="bad",
                )
            self.assertIsNone(
                store._collector_schedule_start_gate_status(
                    source_id="source-x",
                    run_id="run-1",
                )["authorization_sha256"]
            )


if __name__ == "__main__":
    unittest.main()
