from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import autosport._outcome_availability_clock_dispatch_guard as availability_guard
import autosport.outcome_trust as outcome_trust
import autosport.run_registry as run_registry_module
from autosport.outcome_trust import (
    OutcomeLineageBinding,
    OutcomeLineageTrustError,
    TrustedOutcomeRevision,
)
from autosport.run_registry import RunRegistry


class OutcomeAvailabilityGlobalStrictFenceTests(unittest.TestCase):
    @staticmethod
    def _sha(label: str) -> str:
        return hashlib.sha256(label.encode("utf-8")).hexdigest()

    def _binding(
        self,
        *,
        source_identity: str,
        record_id: str,
        label: str,
    ) -> OutcomeLineageBinding:
        revision = TrustedOutcomeRevision(
            revision=1,
            revision_id=f"{record_id}-r1",
            record_sha256=self._sha(label),
        )
        return OutcomeLineageBinding(
            source_identity=source_identity,
            record_id=record_id,
            root_revision_id=revision.revision_id,
            root_record_sha256=revision.record_sha256,
            revisions=(revision,),
        )

    def _new_registry(self, root: str) -> RunRegistry:
        return RunRegistry.initialize_pristine(Path(root) / "run_registry.json")

    def _begin(self, registry: RunRegistry, binding: OutcomeLineageBinding, run_id: str) -> str:
        return registry.begin(
            self._sha(f"market:{run_id}"),
            self._sha(f"results:{run_id}"),
            "baseline-v1",
            run_id,
            outcome_lineage=binding,
        )

    def test_equal_representable_tick_is_not_strictly_after_established_history(self) -> None:
        fence = "2026-01-01T10:00:00Z"

        with self.assertRaisesRegex(
            OutcomeLineageTrustError,
            "strictly later than registry-wide established",
        ):
            availability_guard._strictly_after_registry_fence(
                "2026-01-01T10:00:00+00:00",
                fence,
                canonical_timestamp=outcome_trust._canonical_timestamp,
                parse_timestamp=outcome_trust._parse_timestamp,
                error_type=OutcomeLineageTrustError,
            )

        self.assertEqual(
            availability_guard._strictly_after_registry_fence(
                "2026-01-01T10:00:00.000001Z",
                fence,
                canonical_timestamp=outcome_trust._canonical_timestamp,
                parse_timestamp=outcome_trust._parse_timestamp,
                error_type=OutcomeLineageTrustError,
            ),
            "2026-01-01T10:00:00.000001Z",
        )

    def test_cross_lineage_clock_rollback_leaves_new_identity_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run_registry.json"
            registry = self._new_registry(tmp)
            first = self._binding(
                source_identity="official-results:first",
                record_id="event:first",
                label="first",
            )
            first_key = self._begin(registry, first, "run-first")
            registry.complete(first_key)

            # Model a legitimate previously-established product timestamp followed
            # by a wall-clock rollback. RunRegistry intentionally does not claim a
            # rollback-proof wall clock, so the causal fence must come from durable
            # already-established product history rather than the current clock.
            future = "2999-01-01T00:00:00.000000Z"
            state = json.loads(path.read_text(encoding="utf-8"))
            for binding in state["outcome_lineage_trust"]:
                for revision in binding["revisions"]:
                    revision["first_available_at"] = future
            for entry in state["runs"].values():
                lineage = entry.get("outcome_lineage")
                if lineage is not None:
                    for revision in lineage["revisions"]:
                        revision["first_available_at"] = future
            path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")

            reopened = RunRegistry(path)
            second = self._binding(
                source_identity="official-results:second",
                record_id="event:second",
                label="second",
            )
            with self.assertRaisesRegex(
                OutcomeLineageTrustError,
                "strictly later than registry-wide established",
            ):
                self._begin(reopened, second, "run-second")

            durable = json.loads(path.read_text(encoding="utf-8"))
            by_identity = {
                (binding["source_identity"], binding["record_id"]): binding
                for binding in durable["outcome_lineage_trust"]
            }
            staged = by_identity[("official-results:second", "event:second")]
            self.assertNotIn("first_available_at", staged["revisions"][0])
            self.assertNotIn(
                "run-second",
                {entry["run_id"] for entry in durable["runs"].values()},
            )

    def test_rebound_registry_read_fails_before_attacker_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = self._new_registry(tmp)
            binding = self._binding(
                source_identity="official-results:read-rebind",
                record_id="event:read-rebind",
                label="read-rebind",
            )
            hostile_calls = 0

            def hostile_read(_registry):
                nonlocal hostile_calls
                hostile_calls += 1
                raise AssertionError("rebound RunRegistry._read must not execute")

            with patch.object(RunRegistry, "_read", new=hostile_read):
                with self.assertRaisesRegex(
                    OutcomeLineageTrustError,
                    "read authority dispatch was rebound",
                ):
                    self._begin(registry, binding, "run-read-rebind")

            self.assertEqual(hostile_calls, 0)

    def test_rebound_registry_trust_parser_fails_before_attacker_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = self._new_registry(tmp)
            binding = self._binding(
                source_identity="official-results:parser-rebind",
                record_id="event:parser-rebind",
                label="parser-rebind",
            )
            hostile_calls = 0

            def hostile_bindings(_state):
                nonlocal hostile_calls
                hostile_calls += 1
                raise AssertionError("rebound trust parser must not execute")

            with patch.object(
                RunRegistry,
                "_outcome_lineage_trust_bindings",
                new=staticmethod(hostile_bindings),
            ):
                with self.assertRaisesRegex(
                    OutcomeLineageTrustError,
                    "outcome trust parser dispatch was rebound",
                ):
                    self._begin(registry, binding, "run-parser-rebind")

            self.assertEqual(hostile_calls, 0)


    def test_rebound_run_registry_payload_parser_fails_before_attacker_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = self._new_registry(tmp)
            binding = self._binding(
                source_identity="official-results:payload-parser-rebind",
                record_id="event:payload-parser-rebind",
                label="payload-parser-rebind",
            )
            hostile_calls = 0

            def hostile_parser(_value, *, context):
                nonlocal hostile_calls
                hostile_calls += 1
                raise AssertionError(
                    f"rebound RunRegistry payload parser must not execute: {context}"
                )

            with patch.object(
                run_registry_module,
                "outcome_lineage_binding_from_payload",
                new=hostile_parser,
            ):
                with self.assertRaisesRegex(
                    OutcomeLineageTrustError,
                    "RunRegistry outcome lineage payload parser dispatch was rebound",
                ):
                    self._begin(registry, binding, "run-payload-parser-rebind")

            self.assertEqual(hostile_calls, 0)

    def test_rebound_outcome_trust_payload_parser_fails_before_attacker_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = self._new_registry(tmp)
            binding = self._binding(
                source_identity="official-results:source-parser-rebind",
                record_id="event:source-parser-rebind",
                label="source-parser-rebind",
            )
            hostile_calls = 0

            def hostile_parser(_value, *, context):
                nonlocal hostile_calls
                hostile_calls += 1
                raise AssertionError(
                    f"rebound outcome-trust payload parser must not execute: {context}"
                )

            with patch.object(
                outcome_trust,
                "outcome_lineage_binding_from_payload",
                new=hostile_parser,
            ):
                with self.assertRaisesRegex(
                    OutcomeLineageTrustError,
                    "outcome trust lineage payload parser dispatch was rebound",
                ):
                    self._begin(registry, binding, "run-source-parser-rebind")

            self.assertEqual(hostile_calls, 0)


if __name__ == "__main__":
    unittest.main()
