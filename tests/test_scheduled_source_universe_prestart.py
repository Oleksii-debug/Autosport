from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import autosport.scheduled_source_universe as scheduled_module

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


    def test_prestart_alias_rebind_fails_before_hostile_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "collector.db"
            store = CollectorDeltaStore(path)
            original = scheduled_module._CANONICAL_PRESTART_ENSURE
            hostile_calls: list[str] = []

            def hostile(*args, **kwargs):
                hostile_calls.append("called")
                return original(*args, **kwargs)

            scheduled_module._CANONICAL_PRESTART_ENSURE = hostile
            try:
                with self.assertRaisesRegex(
                    ScheduledSourceUniverseError,
                    "canonical dispatch authority is rebound",
                ):
                    self._prepare(store, path)
            finally:
                scheduled_module._CANONICAL_PRESTART_ENSURE = original

            self.assertEqual(hostile_calls, [])
            self.assertIsNone(
                store._collector_schedule_start_gate_status(
                    source_id="source-x",
                    run_id="run-1",
                )
            )

    def test_path_equality_rebind_cannot_admit_wrong_prestart_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "collector.db"
            wrong_path = Path(tmp) / "wrong.db"
            store = CollectorDeltaStore(path)
            had_own_equality = "__eq__" in vars(Path)
            original_own_equality = vars(Path).get("__eq__")
            hostile_calls: list[bool] = []

            def hostile_equality(_left, _right):
                hostile_calls.append(True)
                return True

            setattr(Path, "__eq__", hostile_equality)
            try:
                with self.assertRaisesRegex(
                    ScheduledSourceUniverseError,
                    "product-expected authority path",
                ):
                    self._prepare(store, wrong_path)
            finally:
                if had_own_equality:
                    setattr(Path, "__eq__", original_own_equality)
                else:
                    delattr(Path, "__eq__")

            self.assertEqual(hostile_calls, [])
            self.assertIsNone(
                store._collector_schedule_start_gate_status(
                    source_id="source-x",
                    run_id="run-1",
                )
            )

    def test_canonical_path_equality_code_mutation_fails_before_prestart(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "collector.db"
            store = CollectorDeltaStore(path)
            equality = scheduled_module._CANONICAL_PATH_EQUALITY
            original_code = equality.__code__

            def hostile_equality(_left, _right):
                raise AssertionError("hostile pre-START equality executed")

            equality.__code__ = hostile_equality.__code__
            try:
                with self.assertRaisesRegex(
                    ScheduledSourceUniverseError,
                    "path comparison authority is rebound or mutated",
                ):
                    self._prepare(store, path)
            finally:
                equality.__code__ = original_code

            self.assertIsNone(
                store._collector_schedule_start_gate_status(
                    source_id="source-x",
                    run_id="run-1",
                )
            )

    def test_prepared_issuer_rebind_fails_before_schedule_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "collector.db"
            store = CollectorDeltaStore(path)
            original = PreparedScheduledSourceUniverse.__dict__["_issue"]
            hostile_calls: list[str] = []

            def hostile_issue(cls, payload):
                hostile_calls.append("called")
                return original.__func__(cls, payload)

            PreparedScheduledSourceUniverse._issue = classmethod(hostile_issue)
            try:
                with self.assertRaisesRegex(
                    ScheduledSourceUniverseError,
                    "result issuance surface is rebound or mutated",
                ):
                    self._prepare(store, path)
            finally:
                PreparedScheduledSourceUniverse._issue = original

            self.assertEqual(hostile_calls, [])
            self.assertIsNone(
                store._collector_schedule_start_gate_status(
                    source_id="source-x",
                    run_id="run-1",
                )
            )


if __name__ == "__main__":
    unittest.main()
