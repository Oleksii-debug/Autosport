from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from autosport.outcome_trust import OutcomeLineageBinding, TrustedOutcomeRevision
from autosport.run_registry import RunRegistry, UnresolvedExperimentError


class OutcomeRevisionRegistrySerializationTests(unittest.TestCase):
    @staticmethod
    def _sha(label: str) -> str:
        return hashlib.sha256(label.encode("utf-8")).hexdigest()

    def _binding(self) -> OutcomeLineageBinding:
        revision = TrustedOutcomeRevision(
            revision=1,
            revision_id="results-r1",
            record_sha256=self._sha("r1"),
        )
        return OutcomeLineageBinding(
            source_identity="official-results:serialization-test",
            record_id="event-results:2026-01-01",
            root_revision_id=revision.revision_id,
            root_record_sha256=revision.record_sha256,
            revisions=(revision,),
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

            def product_clock() -> str:
                if threading.current_thread().name == "writer-a":
                    return "2026-01-01T10:00:00Z"
                return "2026-01-01T11:00:00Z"

            with (
                patch.object(RunRegistry, "_read", new=fenced_read),
                patch("autosport.run_registry._utc_now", side_effect=product_clock),
            ):
                thread_a = threading.Thread(target=writer_a, name="writer-a")
                thread_b = threading.Thread(target=writer_b, name="writer-b")
                thread_a.start()
                self.assertTrue(first_read_captured.wait(timeout=5))
                thread_b.start()
                self.assertTrue(second_started.wait(timeout=5))

                # The second instance has started but must not enter the original
                # read/modify/write transaction while writer A owns the path fence.
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
            revisions = durable["outcome_lineage_trust"][0]["revisions"]
            self.assertEqual(
                revisions[0]["first_available_at"],
                "2026-01-01T10:00:00.000000Z",
            )

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


if __name__ == "__main__":
    unittest.main()
