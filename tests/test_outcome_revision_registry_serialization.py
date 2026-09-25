from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
from types import FunctionType
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from autosport.outcome_trust import (
    OutcomeLineageBinding,
    OutcomeLineageTrustError,
    TrustedOutcomeRevision,
)
from autosport.run_registry import RunRegistry, UnresolvedExperimentError


class OutcomeRevisionRegistrySerializationTests(unittest.TestCase):
    @staticmethod
    def _sha(label: str) -> str:
        return hashlib.sha256(label.encode("utf-8")).hexdigest()

    @staticmethod
    def _instant(value: str) -> datetime:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    def _binding(self, *labels: str) -> OutcomeLineageBinding:
        if not labels:
            labels = ("r1",)
        revisions = tuple(
            TrustedOutcomeRevision(
                revision=index,
                revision_id=f"results-r{index}",
                record_sha256=self._sha(label),
            )
            for index, label in enumerate(labels, start=1)
        )
        return OutcomeLineageBinding(
            source_identity="official-results:serialization-test",
            record_id="event-results:2026-01-01",
            root_revision_id=revisions[0].revision_id,
            root_record_sha256=revisions[0].record_sha256,
            revisions=revisions,
        )

    def _resolve(
        self,
        registry: RunRegistry,
        cutoff: str,
    ) -> TrustedOutcomeRevision | None:
        return registry.outcome_revision_as_of(
            source_identity="official-results:serialization-test",
            record_id="event-results:2026-01-01",
            cutoff=cutoff,
        )

    def test_two_instances_cannot_publish_from_the_same_stale_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run_registry.json"
            first = RunRegistry.initialize_pristine(path)
            second = RunRegistry(path)
            binding = self._binding()

            first_read_captured = threading.Event()
            release_first_read = threading.Event()
            second_started = threading.Event()
            second_reached_read = threading.Event()
            original_read = RunRegistry._read
            read_once = {"writer-a": False}

            def fenced_read(registry: RunRegistry):
                name = threading.current_thread().name
                state = original_read(registry)
                if name == "writer-a" and not read_once["writer-a"]:
                    read_once["writer-a"] = True
                    first_read_captured.set()
                    if not release_first_read.wait(timeout=5):
                        raise AssertionError("test did not release first registry read")
                elif name == "writer-b":
                    second_reached_read.set()
                return state

            outcomes: dict[str, object] = {}

            def writer_a() -> None:
                try:
                    outcomes["a"] = first.begin(
                        self._sha("market-a"),
                        self._sha("results-a"),
                        "baseline-v1",
                        "run-a",
                        outcome_lineage=binding,
                    )
                except BaseException as exc:  # pragma: no cover - surfaced below
                    outcomes["a"] = exc

            def writer_b() -> None:
                second_started.set()
                try:
                    outcomes["b"] = second.begin(
                        self._sha("market-b"),
                        self._sha("results-b"),
                        "baseline-v1",
                        "run-b",
                        outcome_lineage=binding,
                    )
                except BaseException as exc:
                    outcomes["b"] = exc

            with patch.object(RunRegistry, "_read", new=fenced_read):
                thread_a = threading.Thread(target=writer_a, name="writer-a")
                thread_b = threading.Thread(target=writer_b, name="writer-b")
                thread_a.start()
                self.assertTrue(first_read_captured.wait(timeout=5))
                thread_b.start()
                self.assertTrue(second_started.wait(timeout=5))
                self.assertFalse(second_reached_read.wait(timeout=0.25))
                release_first_read.set()
                thread_a.join(timeout=5)
                thread_b.join(timeout=5)

            self.assertFalse(thread_a.is_alive())
            self.assertFalse(thread_b.is_alive())
            self.assertIsInstance(outcomes.get("a"), str)
            self.assertIsInstance(outcomes.get("b"), UnresolvedExperimentError)

            durable = json.loads(path.read_text(encoding="utf-8"))
            run_ids = {entry["run_id"] for entry in durable["runs"].values()}
            self.assertEqual(run_ids, {"run-a"})
            available = durable["outcome_lineage_trust"][0]["revisions"][0][
                "first_available_at"
            ]
            self.assertEqual(self._instant(available).utcoffset().total_seconds(), 0)

    def test_positive_availability_is_sampled_after_durable_unknown_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run_registry.json"
            registry = RunRegistry.initialize_pristine(path)
            original_write = RunRegistry._write
            events: list[str] = []

            def traced_write(target: RunRegistry, state: dict) -> None:
                trust = state.get("outcome_lineage_trust", [])
                if trust:
                    revision = trust[0]["revisions"][0]
                    events.append(
                        "write:positive"
                        if "first_available_at" in revision
                        else "write:unknown"
                    )
                else:
                    events.append("write:other")
                original_write(target, state)

            before = datetime.now(timezone.utc)
            with patch.object(RunRegistry, "_write", new=traced_write):
                registry.begin(
                    self._sha("market"),
                    self._sha("results"),
                    "baseline-v1",
                    "run-causal",
                    outcome_lineage=self._binding("r1"),
                )
            after = datetime.now(timezone.utc)

            self.assertEqual(events, ["write:unknown", "write:positive"])
            durable = json.loads(path.read_text(encoding="utf-8"))
            available = durable["outcome_lineage_trust"][0]["revisions"][0][
                "first_available_at"
            ]
            available_dt = self._instant(available)
            self.assertLessEqual(before, available_dt)
            self.assertLessEqual(available_dt, after)

    def test_rebinding_run_registry_clock_cannot_mint_first_availability(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run_registry.json"
            registry = RunRegistry.initialize_pristine(path)

            with patch(
                "autosport.run_registry._utc_now",
                return_value="1900-01-01T00:00:00Z",
            ) as rebound_clock:
                with self.assertRaisesRegex(
                    OutcomeLineageTrustError,
                    "clock authority was rebound",
                ):
                    registry.begin(
                        self._sha("market-rebind"),
                        self._sha("results-rebind"),
                        "baseline-v1",
                        "run-rebind",
                        outcome_lineage=self._binding("r1"),
                    )
                rebound_clock.assert_not_called()

            durable = json.loads(path.read_text(encoding="utf-8"))
            revision = durable["outcome_lineage_trust"][0]["revisions"][0]
            self.assertNotIn("first_available_at", revision)
            self.assertEqual(durable["runs"], {})
            self.assertIsNone(self._resolve(registry, "9999-12-31T23:59:59Z"))

    def test_rebinding_clock_and_private_token_still_cannot_mint_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run_registry.json"
            registry = RunRegistry.initialize_pristine(path)

            def attacker_clock() -> str:
                return "1900-01-01T00:00:00Z"

            with (
                patch("autosport.run_registry._utc_now", new=attacker_clock),
                patch(
                    "autosport._outcome_availability_registry_serialization._PRODUCT_UTC_NOW",
                    new=attacker_clock,
                ),
            ):
                with self.assertRaisesRegex(
                    OutcomeLineageTrustError,
                    "clock authority was rebound",
                ):
                    registry.begin(
                        self._sha("market-double-rebind"),
                        self._sha("results-double-rebind"),
                        "baseline-v1",
                        "run-double-rebind",
                        outcome_lineage=self._binding("r1"),
                    )

            durable = json.loads(path.read_text(encoding="utf-8"))
            revision = durable["outcome_lineage_trust"][0]["revisions"][0]
            self.assertNotIn("first_available_at", revision)
            self.assertEqual(durable["runs"], {})

    def test_rebinding_run_registry_clock_cannot_extend_trusted_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run_registry.json"
            registry = RunRegistry.initialize_pristine(path)
            first_key = registry.begin(
                self._sha("market-r1"),
                self._sha("results-r1"),
                "baseline-v1",
                "run-r1",
                outcome_lineage=self._binding("r1"),
            )
            registry.complete(first_key)
            before = json.loads(path.read_text(encoding="utf-8"))
            first_available = before["outcome_lineage_trust"][0]["revisions"][0][
                "first_available_at"
            ]

            with patch(
                "autosport.run_registry._utc_now",
                return_value="1900-01-01T00:00:00Z",
            ) as rebound_clock:
                with self.assertRaisesRegex(
                    OutcomeLineageTrustError,
                    "clock authority was rebound",
                ):
                    registry.begin(
                        self._sha("market-r2"),
                        self._sha("results-r2"),
                        "baseline-v1",
                        "run-r2",
                        outcome_lineage=self._binding("r1", "r2"),
                    )
                rebound_clock.assert_not_called()

            durable = json.loads(path.read_text(encoding="utf-8"))
            trust = durable["outcome_lineage_trust"][0]["revisions"]
            self.assertEqual(len(trust), 1)
            self.assertEqual(trust[0]["first_available_at"], first_available)
            run_ids = {entry["run_id"] for entry in durable["runs"].values()}
            self.assertNotIn("run-r2", run_ids)

    def test_crash_after_identity_publication_stays_unknown_and_retry_binds(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run_registry.json"
            registry = RunRegistry.initialize_pristine(path)
            binding = self._binding("r1")
            original_write = RunRegistry._write
            write_count = 0

            def crash_before_positive_write(target: RunRegistry, state: dict) -> None:
                nonlocal write_count
                write_count += 1
                if write_count == 2:
                    raise RuntimeError("crash after identity publication")
                original_write(target, state)

            with patch.object(
                RunRegistry,
                "_write",
                new=crash_before_positive_write,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "crash after identity publication",
                ):
                    registry.begin(
                        self._sha("market-crash"),
                        self._sha("results-crash"),
                        "baseline-v1",
                        "run-crash",
                        outcome_lineage=binding,
                    )

            reopened = RunRegistry(path)
            self.assertIsNone(self._resolve(reopened, "9999-12-31T23:59:59Z"))
            self.assertEqual(reopened.in_progress(), ())
            staged = json.loads(path.read_text(encoding="utf-8"))
            revision = staged["outcome_lineage_trust"][0]["revisions"][0]
            self.assertNotIn("first_available_at", revision)
            self.assertEqual(staged["runs"], {})

            key = reopened.begin(
                self._sha("market-crash"),
                self._sha("results-crash"),
                "baseline-v1",
                "run-crash",
                outcome_lineage=binding,
            )

            self.assertIsInstance(key, str)
            durable = json.loads(path.read_text(encoding="utf-8"))
            available = durable["outcome_lineage_trust"][0]["revisions"][0][
                "first_available_at"
            ]
            resolved = self._resolve(reopened, available)
            self.assertIsNotNone(resolved)
            self.assertEqual(resolved.revision, 1)
            self.assertEqual(resolved.first_available_at, available)

    def test_extension_publishes_available_prefix_unknown_suffix_first(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run_registry.json"
            registry = RunRegistry.initialize_pristine(path)

            first_key = registry.begin(
                self._sha("market-r1"),
                self._sha("results-r1"),
                "baseline-v1",
                "run-r1",
                outcome_lineage=self._binding("r1"),
            )
            registry.complete(first_key)
            before = json.loads(path.read_text(encoding="utf-8"))
            first_available = before["outcome_lineage_trust"][0]["revisions"][0][
                "first_available_at"
            ]

            original_write = RunRegistry._write
            writes: list[tuple[list[bool], set[str]]] = []

            def traced_write(target: RunRegistry, state: dict) -> None:
                trust = state.get("outcome_lineage_trust", [])
                if trust:
                    revisions = trust[0]["revisions"]
                    writes.append(
                        (
                            [
                                "first_available_at" in revision
                                for revision in revisions
                            ],
                            {
                                entry["run_id"]
                                for entry in state["runs"].values()
                            },
                        )
                    )
                original_write(target, state)

            with patch.object(RunRegistry, "_write", new=traced_write):
                second_key = registry.begin(
                    self._sha("market-r2"),
                    self._sha("results-r2"),
                    "baseline-v1",
                    "run-r2",
                    outcome_lineage=self._binding("r1", "r2"),
                )

            self.assertIsInstance(second_key, str)
            self.assertEqual(writes[0][0], [True, False])
            self.assertNotIn("run-r2", writes[0][1])
            self.assertEqual(writes[-1][0], [True, True])
            self.assertIn("run-r2", writes[-1][1])

            durable = json.loads(path.read_text(encoding="utf-8"))
            revisions = durable["outcome_lineage_trust"][0]["revisions"]
            self.assertEqual(revisions[0]["first_available_at"], first_available)
            self.assertLessEqual(
                self._instant(first_available),
                self._instant(revisions[1]["first_available_at"]),
            )
            at_r2 = self._resolve(registry, revisions[1]["first_available_at"])
            self.assertIsNotNone(at_r2)
            self.assertEqual(at_r2.revision, 2)

    def test_serialized_rmw_metadata_does_not_expose_unlocked_predecessors(self) -> None:
        for method_name in (
            "complete",
            "abort_uncommitted",
            "reconcile_completed_summary",
        ):
            method = getattr(RunRegistry, method_name)
            self.assertTrue(
                getattr(method, "_autosport_registry_rmw_serialized", False),
                method_name,
            )
            self.assertTrue(
                getattr(method, "_autosport_predecessor_unreachable", False),
                method_name,
            )
            self.assertIsNone(getattr(method, "__wrapped__", None), method_name)

            pending: list[object] = [method]
            seen: set[int] = set()
            reachable: list[FunctionType] = []
            while pending:
                value = pending.pop()
                if not isinstance(value, FunctionType) or id(value) in seen:
                    continue
                seen.add(id(value))
                reachable.append(value)
                if value.__defaults__:
                    pending.extend(value.__defaults__)
                if value.__kwdefaults__:
                    pending.extend(value.__kwdefaults__.values())
                wrapped = getattr(value, "__wrapped__", None)
                if wrapped is not None:
                    pending.append(wrapped)
                if value.__closure__:
                    for cell in value.__closure__:
                        try:
                            pending.append(cell.cell_contents)
                        except ValueError:
                            pass

            self.assertEqual(reachable, [method], method_name)

    def test_all_run_registry_read_modify_write_methods_are_serialized(self) -> None:
        for method_name in (
            "begin",
            "complete",
            "abort_uncommitted",
            "reconcile_completed_summary",
        ):
            method = getattr(RunRegistry, method_name)
            self.assertTrue(
                getattr(method, "_autosport_registry_rmw_serialized", False),
                method_name,
            )
        self.assertTrue(
            getattr(RunRegistry.begin, "_autosport_outcome_two_phase", False)
        )
        self.assertTrue(
            getattr(RunRegistry.begin, "_autosport_product_clock_sealed", False)
        )


if __name__ == "__main__":
    unittest.main()
