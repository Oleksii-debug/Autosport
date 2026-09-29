from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from autosport.causal_collector import CollectorDeltaStore
from autosport.scheduled_source_universe import (
    PreparedScheduledSourceUniverse,
    ScheduledSourceUniverseError,
    prepare_scheduled_source_universe,
)


GATE_A = "a" * 64
GATE_B = "b" * 64
AUTH_A = "c" * 64


class ScheduledSourceUniversePrestartTests(unittest.TestCase):
    def _prepare(
        self,
        store: CollectorDeltaStore,
        path: Path,
        *,
        gate: str = GATE_A,
    ) -> PreparedScheduledSourceUniverse:
        return prepare_scheduled_source_universe(
            store,
            expected_store_path=path,
            expected_source_id="source-x",
            expected_run_id="run-1",
            expected_stream_epoch="epoch-1",
            anchor_at="2026-01-01T00:00:00+00:00",
            interval_seconds=10,
            max_items=250,
            evaluation_start_slot_ordinal=0,
            evaluation_end_slot_ordinal=1,
            gate_binding_sha256=gate,
        )

    def test_prepare_issues_exact_pristine_gate_without_start_authority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "collector.db"
            store = CollectorDeltaStore(path)

            prepared = self._prepare(store, path)

            self.assertIs(type(prepared), PreparedScheduledSourceUniverse)
            self.assertEqual(prepared.source_id, "source-x")
            self.assertEqual(prepared.run_id, "run-1")
            self.assertEqual(prepared.stream_epoch, "epoch-1")
            self.assertEqual(prepared.next_slot_ordinal, 0)
            self.assertEqual(prepared.gate_binding_sha256, GATE_A)
            self.assertEqual(len(prepared.schedule_id), 64)
            self.assertEqual(len(prepared.prestart_sha256), 64)

            slot = store._next_collector_schedule_slot(
                source_id="source-x",
                run_id="run-1",
            )
            with self.assertRaisesRegex(ValueError, "not durably authorized"):
                store._begin_scheduled_collector_cycle(
                    source_id="source-x",
                    run_id="run-1",
                    stream_epoch="epoch-1",
                    max_items=250,
                    slot_ordinal=slot["slot_ordinal"],
                    due_at=slot["due_at"],
                    attempted_at=slot["due_at"],
                )

    def test_prepared_value_is_not_caller_constructible(self) -> None:
        with self.assertRaisesRegex(TypeError, "resolver-issued"):
            PreparedScheduledSourceUniverse()

    def test_restart_reresolves_exact_pristine_boundary_without_rebinding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "collector.db"
            first = CollectorDeltaStore(path)
            prepared = self._prepare(first, path)

            reopened = CollectorDeltaStore(path)
            repeated = self._prepare(reopened, path)

            self.assertEqual(repeated, prepared)
            self.assertEqual(
                reopened._collector_schedule_start_gate_status(
                    source_id="source-x",
                    run_id="run-1",
                )["authorization_sha256"],
                None,
            )

    def test_prepare_fails_after_gate_authorization_or_start(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "collector.db"
            store = CollectorDeltaStore(path)
            prepared = self._prepare(store, path)
            store._authorize_collector_schedule_start_gate(
                source_id=prepared.source_id,
                run_id=prepared.run_id,
                schedule_id=prepared.schedule_id,
                gate_binding_sha256=prepared.gate_binding_sha256,
                authorization_sha256=AUTH_A,
            )

            with self.assertRaisesRegex(
                ScheduledSourceUniverseError,
                "pristine unauthorized",
            ):
                self._prepare(store, path)

            slot = store._next_collector_schedule_slot(
                source_id="source-x",
                run_id="run-1",
            )
            store._begin_scheduled_collector_cycle(
                source_id="source-x",
                run_id="run-1",
                stream_epoch="epoch-1",
                max_items=250,
                slot_ordinal=slot["slot_ordinal"],
                due_at=slot["due_at"],
                attempted_at=slot["due_at"],
            )
            with self.assertRaisesRegex(
                ScheduledSourceUniverseError,
                "pristine unauthorized",
            ):
                self._prepare(store, path)

    def test_gate_retarget_is_rejected_by_canonical_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "collector.db"
            store = CollectorDeltaStore(path)
            self._prepare(store, path)
            with self.assertRaisesRegex(
                ScheduledSourceUniverseError,
                "cannot establish canonical pre-START",
            ):
                self._prepare(store, path, gate=GATE_B)

    def test_wrong_expected_store_path_is_rejected_before_preparation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "collector.db"
            store = CollectorDeltaStore(path)
            with self.assertRaisesRegex(
                ScheduledSourceUniverseError,
                "product-expected authority path",
            ):
                self._prepare(store, Path(tmp) / "other.db")

            self.assertIsNone(
                store._collector_schedule_start_gate_status(
                    source_id="source-x",
                    run_id="run-1",
                )
            )

    def test_instance_rebinding_of_prestart_seam_fails_before_hostile_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "collector.db"
            store = CollectorDeltaStore(path)
            hostile_calls: list[str] = []
            store._next_collector_schedule_slot = lambda **_kwargs: (
                hostile_calls.append("called") or {}
            )
            with self.assertRaisesRegex(
                ScheduledSourceUniverseError,
                "instance-rebound",
            ):
                self._prepare(store, path)
            self.assertEqual(hostile_calls, [])

    def test_class_rebinding_of_prestart_seam_fails_before_hostile_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "collector.db"
            store = CollectorDeltaStore(path)
            original = CollectorDeltaStore._collector_schedule_start_gate_status
            hostile_calls: list[str] = []

            def hostile(self, **_kwargs):
                hostile_calls.append("called")
                return None

            CollectorDeltaStore._collector_schedule_start_gate_status = hostile
            try:
                with self.assertRaisesRegex(
                    ScheduledSourceUniverseError,
                    "class-rebound",
                ):
                    self._prepare(store, path)
                self.assertEqual(hostile_calls, [])
            finally:
                CollectorDeltaStore._collector_schedule_start_gate_status = original


if __name__ == "__main__":
    unittest.main()
